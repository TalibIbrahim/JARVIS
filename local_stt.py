"""Local speech-to-text for J.A.R.V.I.S. (faster-whisper / CTranslate2, CPU int8).

One lazily-created singleton. The model is loaded on a background thread (first run downloads it into
%LOCALAPPDATA%\\JARVIS\\models\\stt) so the first command is never blocked; until it is ready, or if it
cannot load, `available()` is False and the caller uses Groq. Nothing here logs audio or text.
"""
import math
import os
import threading
import time

import jarvis_log

_log = jarvis_log.get_logger("voice")

IDLE, LOADING, READY, FAILED = "idle", "loading", "ready", "failed"

# Segments whose no_speech_prob is above this are treated as non-speech (Whisper hallucinates on noise).
NO_SPEECH_DROP = 0.6


def models_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JARVIS", "models", "stt")


class LocalTranscriber:
    def __init__(self, model_name="base.en", cpu_threads=4, download_root=None):
        self.model_name = model_name
        self.cpu_threads = cpu_threads
        self.download_root = download_root or models_dir()
        self._model = None
        self._state = IDLE
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()   # CTranslate2 model: one transcription at a time

    @property
    def state(self) -> str:
        return self._state

    def available(self) -> bool:
        return self._state == READY

    def start_loading(self):
        """Begin loading on a daemon thread (idempotent)."""
        with self._lock:
            if self._state != IDLE:
                return
            self._state = LOADING
        threading.Thread(target=jarvis_log.run_logged, args=("LocalSTTLoad", self._load),
                         name="LocalSTTLoad", daemon=True).start()

    def _load(self):
        t0 = time.monotonic()
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            from faster_whisper import WhisperModel
            kw = dict(device="cpu", compute_type="int8", cpu_threads=self.cpu_threads,
                      download_root=self.download_root)
            try:
                model = WhisperModel(self.model_name, local_files_only=True, **kw)
            except Exception:
                _log.info("local STT: model %s not cached; downloading into %s (first run only)",
                          self.model_name, self.download_root)
                os.makedirs(self.download_root, exist_ok=True)
                model = WhisperModel(self.model_name, **kw)
            self._model = model
            self._state = READY
            _log.info("local STT ready: model=%s load_ms=%d", self.model_name,
                      int((time.monotonic() - t0) * 1000))
        except Exception as exc:
            self._state = FAILED
            _log.warning("local STT unavailable (%r); using Groq", exc)

    def transcribe(self, pcm16: bytes, prompt: str = None, hotwords: str = None):
        """Transcribe 16 kHz mono int16 PCM. Returns (text, confidence 0..1). Raises if not ready.

        No default prompt: measured with base.en and tiny.en, ANY initial_prompt/hotwords containing "Jarvis"
        makes Whisper drop a leading "Jarvis" from the transcript, which would break "Jarvis open chrome".
        `hotwords` (the user's own vocabulary, never the wake aliases; see personal.vocabulary) was verified
        to keep a leading "Jarvis" and to fix near-miss names ("Maliniopos" -> "Malenia")."""
        import numpy as np
        if self._model is None:
            raise RuntimeError("local STT model not loaded")
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with self._run_lock:
            segments, _info = self._model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False, initial_prompt=prompt,
                temperature=0.0, without_timestamps=True,
                hotwords=hotwords or None)
            segs = list(segments)
        return summarize_segments(segs)


def summarize_segments(segs):
    """(text, confidence). confidence = token-weighted mean of exp(avg_logprob); 0.0 when nothing was kept."""
    texts, weights, probs = [], [], []
    for s in segs:
        if getattr(s, "no_speech_prob", 0.0) > NO_SPEECH_DROP:
            continue
        t = (s.text or "").strip()
        if not t:
            continue
        texts.append(t)
        w = max(1, len(t.split()))
        weights.append(w)
        probs.append(math.exp(min(0.0, float(s.avg_logprob))))
    if not texts:
        return "", 0.0
    conf = sum(p * w for p, w in zip(probs, weights)) / sum(weights)
    return " ".join(texts), float(conf)


_SINGLETON = None
_SINGLETON_LOCK = threading.Lock()


def get_local_transcriber(model_name="base.en") -> LocalTranscriber:
    """Process-wide transcriber; a different model name replaces it (the old one is garbage collected)."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None or _SINGLETON.model_name != model_name:
            _SINGLETON = LocalTranscriber(model_name)
        return _SINGLETON
