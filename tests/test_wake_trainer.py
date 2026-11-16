"""Personal "Hey Jarvis" verifier: training + safety check, example store (no audio), consent gating,
live reload/removal, detector scoring, example labeling and deletion."""
import glob
import os
import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

import personal
import voice
import wake_trainer as W
from config import Config
from voice_fakes import CHUNK, FakeStream, loud, make_detector, silence

D = 16 * 96


def _examples(n, center, stock, rng, frames=12, spread=1.0):
    out = []
    for _ in range(n):
        f = rng.normal(center, spread, size=(frames, D)).astype(np.float32)
        s = np.full(frames, 0.01, dtype=np.float32)
        s[frames // 2] = stock                     # one wake-like frame per example
        out.append((f, s))
    return out


@pytest.fixture
def profile_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROFILE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- training

def test_verifier_lifts_half_heard_wakes_without_false_alarms():
    rng = np.random.default_rng(1)
    pos = _examples(20, +0.6, 0.3, rng)            # stock 0.3: below the 0.5 threshold, above the floor
    neg = _examples(40, -0.6, 0.3, rng)            # equally wake-like to the stock model
    v, rep = W.train(pos, neg)
    assert rep["ok"] and v is not None, rep
    assert rep["stock_recall"] == 0.0 and rep["recall"] >= 0.9
    assert rep["false_alarm"] <= rep["stock_false_alarm"]
    f, s = _examples(1, +0.6, 0.3, rng)[0]
    assert v.combine(float(s.max()), f[np.argmax(s)]) >= 0.5
    f, s = _examples(1, -0.6, 0.3, rng)[0]
    assert v.combine(float(s.max()), f[np.argmax(s)]) < 0.5


def test_verifier_never_consulted_below_floor():
    rng = np.random.default_rng(2)
    v, _ = W.train(_examples(20, +0.6, 0.3, rng), _examples(40, -0.6, 0.3, rng))
    f = rng.normal(+0.6, 1.0, size=D)
    assert v.combine(0.05, f) == pytest.approx(0.05)


def test_useless_verifier_is_refused():
    rng = np.random.default_rng(3)
    pos = _examples(20, 0.0, 0.3, rng)             # indistinguishable from the negatives
    neg = _examples(40, 0.0, 0.3, rng)
    v, rep = W.train(pos, neg)
    assert v is None and rep["reason"] == "failed_check"


def test_not_worse_than_stock_is_required():
    rng = np.random.default_rng(4)
    pos = _examples(20, 0.0, 0.9, rng)             # stock already catches every wake
    neg = _examples(40, 0.0, 0.01, rng)
    v, rep = W.train(pos, neg)
    assert rep["stock_recall"] == 1.0
    assert v is None or rep["recall"] >= rep["stock_recall"]


def test_too_few_examples():
    rng = np.random.default_rng(5)
    v, rep = W.train(_examples(2, 1, 0.3, rng), _examples(40, -1, 0.3, rng))
    assert v is None and rep["reason"] == "not_enough_examples"


def test_verifier_save_load_roundtrip(tmp_path):
    rng = np.random.default_rng(6)
    v, _ = W.train(_examples(20, +0.6, 0.3, rng), _examples(40, -0.6, 0.3, rng))
    path = str(tmp_path / "v.npz")
    v.save(path)
    v2 = W.Verifier.load(path)
    x = rng.normal(size=D)
    assert v2.prob(x) == pytest.approx(v.prob(x), abs=1e-6)


# ---------------------------------------------------------------- example store

class FakeOWW:
    """Minimal openWakeWord Model surface used by clip_frames / the detector."""

    def __init__(self, score=0.0):
        self.model_inputs = {"hey_jarvis": 16}
        self.score = score
        self._last = np.zeros(CHUNK, dtype=np.int16)
        self.preprocessor = self
        self.resets = 0

    def reset(self):
        self.resets += 1

    def predict(self, arr):
        self._last = np.asarray(arr)
        return {"hey_jarvis": self.score}

    def get_features(self, n):
        return np.full((1, n, 96), float(np.abs(self._last).mean()) / 1000.0, dtype=np.float32)


def _trainer(enabled=True):
    return W.WakeTrainer(enabled=lambda: enabled, model_factory=FakeOWW)


def _pcm(seconds=2.5, amp=3000):
    return np.random.default_rng(0).integers(-amp, amp, int(16000 * seconds)).astype(np.int16).tobytes()


def _drain(tr, kind, n, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and tr.counts()[kind] < n:
        time.sleep(0.02)
    return tr.counts()[kind]


def test_examples_are_fingerprints_not_audio(profile_dir):
    tr = _trainer()
    assert tr.add("pos", _pcm())
    assert tr.add("neg", _pcm())
    assert _drain(tr, "pos", 1) == 1 and _drain(tr, "neg", 1) == 1
    files = glob.glob(os.path.join(str(profile_dir), "wake", "**", "*"), recursive=True)
    assert not [f for f in files if f.endswith(".wav")]
    with np.load(glob.glob(os.path.join(str(profile_dir), "wake", "neg", "*.npz"))[0]) as d:
        assert set(d.files) == {"feats", "scores"}
        assert d["feats"].shape[0] <= W.NEG_KEEP_FRAMES


def test_nothing_stored_without_consent(profile_dir):
    tr = _trainer(enabled=False)
    assert not tr.add("pos", _pcm())
    assert not os.path.exists(os.path.join(str(profile_dir), "wake"))


def test_short_clip_ignored(profile_dir):
    assert not _trainer().add("pos", _pcm(seconds=0.3))


def test_examples_capped(profile_dir, monkeypatch):
    monkeypatch.setattr(W, "MAX_NEG", 3)
    tr = _trainer()
    for _ in range(6):
        tr._store("neg", np.zeros((4, D), np.float32), np.zeros(4, np.float32))
    assert tr.counts()["neg"] == 3


def test_train_now_installs_and_handle_reloads_then_drops(profile_dir, monkeypatch):
    rng = np.random.default_rng(7)
    tr = _trainer()
    for f, s in _examples(20, +0.6, 0.3, rng):
        tr._store("pos", f, s)
    for f, s in _examples(40, -0.6, 0.3, rng):
        tr._store("neg", f, s)
    h = W.VerifierHandle()
    monkeypatch.setattr(W.VerifierHandle, "CHECK_S", 0.0)
    assert h.get() is None
    assert tr.train_now() is not None
    assert os.path.exists(W.verifier_path())
    assert h.get() is not None
    personal.delete_voice_clips()                  # Privacy & Learning: delete voice clips
    assert not os.path.exists(W.data_dir())
    assert h.get() is None


# ---------------------------------------------------------------- detector

def test_detector_applies_verifier_only_above_floor(monkeypatch):
    wd = make_detector(monkeypatch)
    wd.model = FakeOWW(score=0.3)
    v = MagicMock()
    v.prob.return_value = 0.97
    wd._verifier = MagicMock(get=MagicMock(return_value=v))
    arr = np.frombuffer(loud(1), dtype=np.int16)
    assert wd._wake_score(arr, 3000.0, None) == pytest.approx(0.97)
    wd.model.score = 0.05
    assert wd._wake_score(arr, 3000.0, None) == pytest.approx(0.05)


def test_detector_survives_broken_verifier(monkeypatch):
    wd = make_detector(monkeypatch)
    wd.model = FakeOWW(score=0.3)
    v = MagicMock()
    v.prob.side_effect = ValueError("shape")
    wd._verifier = MagicMock(get=MagicMock(return_value=v))
    assert wd._wake_score(np.frombuffer(loud(1), dtype=np.int16), 3000.0, None) == pytest.approx(0.3)


def test_wake_sample_sent_only_when_a_command_followed(monkeypatch):
    wd = make_detector(monkeypatch)
    wd.model = FakeOWW(score=0.0)
    samples = []
    wd.on_wake_sample = samples.append
    wd.on_wake = lambda: None
    wd.on_utterance = lambda raw, is_command=True: None
    for i in range(10):
        wd._wake_ring.append(loud(i))
    plan = ["S"] * 8

    def src(n):
        return loud(n) if n <= len(plan) else silence()
    wd._on_wake_fired(FakeStream(src), [], CHUNK, 16000)
    assert len(samples) == 1 and len(samples[0]) == 10 * CHUNK * 2
    wd._on_wake_fired(FakeStream(lambda n: silence()), [], CHUNK, 16000)   # wake, then nothing said
    assert len(samples) == 1


# ---------------------------------------------------------------- labeling (Voice side)

@pytest.mark.parametrize("heard,kind", [
    ("hey jarvis what's the time", "pos"),
    ("jarvis, open chrome", "pos"),
    ("pass me the controller", "neg"),
    ("did you see the game last night", "neg"),
    ("hey joffas what's the time", None),          # misheard wake: never a negative
    ("joffas open chrome", None),
    ("hey guys what's up", None),
    ("i told jarvis to open it", None),            # wake word mid-sentence: not a clean positive
])
def test_passive_examples_labeled_safely(monkeypatch, heard, kind):
    cfg = Config()
    cfg.wake_aliases = ["jarvis"]
    v = voice.Voice(cfg, MagicMock(), threading.Event())
    added = []
    v._wake_trainer = MagicMock(add=lambda k, pcm, at_end=False: added.append(k))
    v._utt_ctx = {"raw": _pcm()}
    matched = "jarvis" if "jarvis" in heard else None
    v._learn_wake_example(heard, matched)
    assert added == ([kind] if kind else [])
