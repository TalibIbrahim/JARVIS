"""Personal "Hey Jarvis" verifier, learned on this PC from the user's own voice.

The stock openWakeWord model is trained on synthetic voices and can be lukewarm on a real one (scores of
0.1-0.4 where 0.5 is needed). While Jarvis is used, this module collects examples of the user's wake phrase
and of other speech, and periodically trains a small logistic-regression verifier on the wake model's own
audio embeddings (openwakeword.custom_verifier_model's idea, in numpy, because sklearn is not in the build).

Examples (only while cfg.learn_voice_clips is on; under personal.data_dir()/wake, capped, oldest evicted).
NO AUDIO IS STORED: each example is the wake model's embedding frames (a numeric fingerprint that cannot be
played back) plus the stock scores.
  pos/  the ~2 s before a wake that led to a command, and passive utterances that STARTED with the wake word
        while the wake model missed them (the most useful examples)
  neg/  the start of passive speech with no wake word; only its NEG_KEEP_FRAMES most wake-like frames

At run time (openWakeWord's own verifier semantics) the verifier takes over the score only where the stock
model is already >= VERIFY_FLOOR: it lifts the user's half-heard wakes over the threshold and pushes down
near-misses ("Hey Travis"). A verifier is installed only if, in cross-validation, it catches at least as
many wakes as the stock model and false-alarms no more often (see train()). Deleting voice clips in
Privacy & Learning deletes this folder too; the detector then drops the verifier within seconds.
Pure Python + numpy, no Qt. Nothing leaves the PC.
"""
import glob
import os
import queue
import shutil
import threading
import time

import numpy as np

import jarvis_log

_log = jarvis_log.get_logger("wake_trainer")

SR = 16000
CHUNK = 1280
CLIP_S = 2.0
MAX_POS = 150
MAX_NEG = 300
MIN_POS = 6
MIN_NEG = 12
MIN_NEW_CLIPS = 3             # retrain only when this many examples arrived since the last training
RETRAIN_MIN_INTERVAL_S = 1800.0
POS_TOP_FRAMES = 3            # training frames per positive (highest stock score: the end of "Jarvis")
NEG_KEEP_FRAMES = 10          # frames kept per negative (the most wake-like ones)
NEG_MAX_FRAMES = 4000
VERIFY_FLOOR = 0.1            # stock score at which the verifier is consulted (openWakeWord's default too)
VERIFY_THRESHOLD = 0.5        # prob used for the cross-validation check (the detector's threshold applies live)
MIN_RECALL = 0.75             # wakes the verifier must catch in cross-validation
MAX_FALSE_ALARM = 0.03        # other speech allowed to trigger in cross-validation
CV_FOLDS = 4
L2 = 10.0
HARD_NEG_WEIGHT = 20.0        # negatives where the stock model already stirs (>= VERIFY_FLOOR) matter most
MODEL_NAME = "hey_jarvis"
MODEL_IDLE_RELEASE_S = 60.0   # the worker thread (and its own wake model) exits after this long without work


def data_dir() -> str:
    import personal
    return os.path.join(personal.data_dir(), "wake")


def verifier_path() -> str:
    return os.path.join(data_dir(), "verifier.npz")


# ------------------------------------------------------------------ verifier

class Verifier:
    """Standardize -> logistic regression on the flattened (16 x 96) embedding window."""

    def __init__(self, mean, scale, w, b):
        self.mean = np.asarray(mean, dtype=np.float32)
        self.scale = np.asarray(scale, dtype=np.float32)
        self.w = np.asarray(w, dtype=np.float32)
        self.b = float(b)

    def prob(self, feats) -> float:
        x = (np.asarray(feats, dtype=np.float32).reshape(-1) - self.mean) / self.scale
        z = float(x @ self.w) + self.b
        return float(1.0 / (1.0 + np.exp(-max(-40.0, min(40.0, z)))))

    def probs(self, X) -> np.ndarray:
        X = (np.asarray(X, dtype=np.float32) - self.mean) / self.scale
        z = np.clip(X @ self.w + self.b, -40.0, 40.0)
        return 1.0 / (1.0 + np.exp(-z))

    def combine(self, stock: float, feats) -> float:
        """Live score: the verifier's where the stock model is at least VERIFY_FLOOR, else the stock score."""
        return self.prob(feats) if stock >= VERIFY_FLOOR else float(stock)

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp.npz"
        np.savez(tmp, mean=self.mean, scale=self.scale, w=self.w, b=np.float32(self.b))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        with np.load(path) as d:
            return cls(d["mean"], d["scale"], d["w"], float(d["b"]))


def fit_logistic(X, y, weights=None, l2=L2, iters=400, lr=0.5) -> Verifier:
    """L2-regularized logistic regression by full-batch gradient descent (small data). Each class carries
    half the total weight; `weights` (optional) shifts weight between examples of the same class."""
    X = np.asarray(X, dtype=np.float32)
    y = np.asarray(y, dtype=np.float32)
    mean = X.mean(axis=0)
    scale = X.std(axis=0) + 1e-3
    Xs = (X - mean) / scale
    n, d = Xs.shape
    sw = np.ones(n, dtype=np.float32) if weights is None else np.asarray(weights, dtype=np.float32).copy()
    for cls in (0.0, 1.0):
        m = y == cls
        if m.any():
            sw[m] *= 0.5 / sw[m].sum()
    w = np.zeros(d, dtype=np.float32)
    b = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Xs @ w + b, -40.0, 40.0)))
        g = (p - y) * sw
        w -= lr * (Xs.T @ g + l2 * w / n)
        b -= lr * float(g.sum())
    return Verifier(mean, scale, w, b)


# ------------------------------------------------------------------ features

def _new_model():
    import voice
    voice._import_openwakeword_lean()
    from openwakeword.model import Model
    return Model(wakeword_models=[MODEL_NAME], inference_framework="onnx")


def clip_frames(model, pcm):
    """(features [N, 1536], stock scores [N]) for every 80 ms chunk of a clip, model state reset first."""
    model.reset()
    n_in = model.model_inputs[MODEL_NAME]
    audio = np.concatenate([np.zeros(SR // 2, dtype=np.int16), np.asarray(pcm, dtype=np.int16)])
    feats, scores = [], []
    for i in range(0, len(audio) - CHUNK + 1, CHUNK):
        s = float(model.predict(audio[i:i + CHUNK]).get(MODEL_NAME, 0.0))
        if i < SR // 2:
            continue                                  # warm-up silence
        feats.append(model.preprocessor.get_features(n_in).reshape(-1))
        scores.append(s)
    if not feats:
        return np.zeros((0, n_in * 96), dtype=np.float32), np.zeros(0, dtype=np.float32)
    return np.asarray(feats, dtype=np.float32), np.asarray(scores, dtype=np.float32)


def _top_frames(feats, scores, k):
    if len(scores) <= k:
        return feats, scores
    keep = np.sort(np.argsort(scores)[-k:])
    return feats[keep], scores[keep]


def train(pos, neg, seed=0):
    """Train and check a verifier. pos/neg: lists of (features, stock scores) examples (see clip_frames).
    Returns (Verifier or None, report dict).

    The check is CV_FOLDS-fold cross-validation in the live semantics (verifier consulted only where the
    stock score >= VERIFY_FLOOR): the verifier must catch at least MIN_RECALL of the wakes, false-alarm on
    at most MAX_FALSE_ALARM of the other speech, and be no worse than the stock model on either."""
    pos = [e for e in pos if len(e[1])]
    neg = [e for e in neg if len(e[1])]
    report = {"pos": len(pos), "neg": len(neg), "ok": False}
    if len(pos) < MIN_POS or len(neg) < MIN_NEG:
        report["reason"] = "not_enough_examples"
        return None, report
    rng = np.random.default_rng(seed)

    def build(p_idx, n_idx):
        Pf = np.vstack([_top_frames(*pos[i], POS_TOP_FRAMES)[0] for i in p_idx])
        Nf = np.vstack([neg[i][0] for i in n_idx])
        Ns = np.concatenate([neg[i][1] for i in n_idx])
        if len(Nf) > NEG_MAX_FRAMES:
            keep = rng.choice(len(Nf), NEG_MAX_FRAMES, replace=False)
            Nf, Ns = Nf[keep], Ns[keep]
        X = np.vstack([Pf, Nf])
        y = np.concatenate([np.ones(len(Pf)), np.zeros(len(Nf))])
        wts = np.concatenate([np.ones(len(Pf)), np.where(Ns >= VERIFY_FLOOR, HARD_NEG_WEIGHT, 1.0)])
        return fit_logistic(X, y, wts)

    def peak(v, example):
        feats, stock = example
        if v is None:
            return float(stock.max())
        return float(np.where(stock >= VERIFY_FLOOR, v.probs(feats), stock).max())

    p_perm, n_perm = rng.permutation(len(pos)), rng.permutation(len(neg))
    hits, fas, s_hits, s_fas = [], [], [], []
    for k in range(CV_FOLDS):
        p_te, n_te = set(p_perm[k::CV_FOLDS].tolist()), set(n_perm[k::CV_FOLDS].tolist())
        v = build([i for i in range(len(pos)) if i not in p_te], [i for i in range(len(neg)) if i not in n_te])
        hits += [peak(v, pos[i]) >= VERIFY_THRESHOLD for i in p_te]
        fas += [peak(v, neg[i]) >= VERIFY_THRESHOLD for i in n_te]
        s_hits += [peak(None, pos[i]) >= VERIFY_THRESHOLD for i in p_te]
        s_fas += [peak(None, neg[i]) >= VERIFY_THRESHOLD for i in n_te]
    recall, false_alarm = float(np.mean(hits)), float(np.mean(fas))
    stock_recall, stock_fa = float(np.mean(s_hits)), float(np.mean(s_fas))
    report.update(recall=round(recall, 3), false_alarm=round(false_alarm, 3),
                  stock_recall=round(stock_recall, 3), stock_false_alarm=round(stock_fa, 3))
    if (recall < MIN_RECALL or false_alarm > MAX_FALSE_ALARM
            or recall < stock_recall or false_alarm > stock_fa):
        report["reason"] = "failed_check"
        return None, report
    report["ok"] = True
    return build(range(len(pos)), range(len(neg))), report    # passed: refit on everything


# ------------------------------------------------------------------ example store + background retraining

class WakeTrainer:
    """Collects examples and retrains, all on one low-priority background thread. add() never blocks."""

    def __init__(self, enabled=lambda: True, model_factory=_new_model):
        self._enabled = enabled
        self._model_factory = model_factory
        self._q = queue.Queue(maxsize=16)
        self._lock = threading.Lock()
        self._thread = None
        self._new = 0
        self._last_train = 0.0
        self._seq = 0
        self.last_report = None

    @staticmethod
    def _dir(kind):
        return os.path.join(data_dir(), kind)

    def counts(self) -> dict:
        return {k: len(glob.glob(os.path.join(self._dir(k), "*.npz"))) for k in ("pos", "neg")}

    def add(self, kind: str, pcm16: bytes, at_end: bool = False) -> bool:
        """Queue a CLIP_S example (the last seconds when at_end, else the first). kind: "pos" | "neg".
        Dropped (False) when learning is off, the clip is under 0.5 s, or the worker is backed up."""
        if kind not in ("pos", "neg") or not pcm16 or not self._enabled():
            return False
        n = int(CLIP_S * SR) * 2
        pcm16 = bytes(pcm16)[: len(pcm16) - (len(pcm16) % 2)]
        clip = pcm16[-n:] if at_end else pcm16[:n]
        if len(clip) < SR:
            return False
        try:
            self._q.put_nowait((kind, clip))
        except queue.Full:
            return False
        self._ensure_thread()
        return True

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=jarvis_log.run_logged, args=("WakeTrainer", self._run),
                                                name="WakeTrainer", daemon=True)
                self._thread.start()

    def _run(self):
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -2)
        except Exception:
            pass
        model = None
        while True:
            try:
                kind, clip = self._q.get(timeout=MODEL_IDLE_RELEASE_S)
            except queue.Empty:
                return          # idle: the thread (and its wake model) goes away; add() starts a new one
            try:
                if not self._enabled():
                    continue
                model = model or self._model_factory()
                self._store(kind, *clip_frames(model, np.frombuffer(clip, dtype=np.int16)))
                if self._due():
                    self.train_now()
            except Exception as exc:
                _log.warning("wake example failed: %r", exc)

    def _store(self, kind, feats, scores):
        if kind == "neg":
            feats, scores = _top_frames(feats, scores, NEG_KEEP_FRAMES)
        with self._lock:
            self._seq += 1
            stem = f"{int(time.time() * 1000):013d}_{self._seq:04d}"
        folder = self._dir(kind)
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, stem + ".tmp.npz")
        np.savez(tmp, feats=feats.astype(np.float16), scores=scores.astype(np.float32))
        os.replace(tmp, os.path.join(folder, stem + ".npz"))
        files = sorted(glob.glob(os.path.join(folder, "*.npz")))
        for p in files[:max(0, len(files) - (MAX_POS if kind == "pos" else MAX_NEG))]:
            try:
                os.remove(p)
            except OSError:
                pass
        with self._lock:
            self._new += 1

    def _due(self) -> bool:
        with self._lock:
            if self._new < MIN_NEW_CLIPS:
                return False
            if self._last_train and time.monotonic() - self._last_train < RETRAIN_MIN_INTERVAL_S:
                return False
        c = self.counts()
        return c["pos"] >= MIN_POS and c["neg"] >= MIN_NEG

    def _load(self, kind):
        out = []
        for p in sorted(glob.glob(os.path.join(self._dir(kind), "*.npz"))):
            try:
                with np.load(p) as d:
                    out.append((d["feats"].astype(np.float32), d["scores"].astype(np.float32)))
            except Exception as exc:
                _log.warning("unreadable wake example %s: %r", os.path.basename(p), exc)
        return out

    def train_now(self):
        """Train from the stored examples and install the verifier if it passes. Runs on the caller's thread."""
        with self._lock:
            self._new = 0
            self._last_train = time.monotonic()
        t0 = time.monotonic()
        v, report = train(self._load("pos"), self._load("neg"))
        report["ms"] = int((time.monotonic() - t0) * 1000)
        self.last_report = report
        if v is not None and self._enabled() and os.path.isdir(data_dir()):
            v.save(verifier_path())
            _log.info("personal wake verifier updated: %s", report)
        else:
            _log.info("personal wake verifier not updated: %s", report)
        return v

    @staticmethod
    def delete_all():
        shutil.rmtree(data_dir(), ignore_errors=True)


class VerifierHandle:
    """The detector's view of verifier.npz: reloads when the file changes or disappears (checked <= 1/5 s)."""
    CHECK_S = 5.0

    def __init__(self, path_fn=verifier_path):
        self._path_fn = path_fn
        self._next = 0.0
        self._mtime = None
        self.verifier = None

    def get(self):
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self.CHECK_S
            try:
                mtime = os.path.getmtime(self._path_fn())
            except OSError:
                mtime = None
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    self.verifier = Verifier.load(self._path_fn()) if mtime is not None else None
                    _log.info("personal wake verifier %s", "loaded" if self.verifier else "removed")
                except Exception as exc:
                    _log.warning("personal wake verifier load failed: %r", exc)
                    self.verifier = None
        return self.verifier
