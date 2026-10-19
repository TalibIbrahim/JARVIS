"""Shared fakes for WakeWordDetector tests: no PyAudio device, no openwakeword, no network."""
import sys
import time
import types

import numpy as np

import voice
from config import Config

CHUNK = 1280


def silence():
    return np.zeros(CHUNK, dtype=np.int16).tobytes()


def loud(seed=0, amp=6000):
    rng = np.random.default_rng(seed)
    return rng.integers(-amp, amp, CHUNK).astype(np.int16).tobytes()


class FakeStream:
    """Counts reads. `source(n)` returns the n-th chunk (bytes)."""

    def __init__(self, source=None, on_read=None):
        self.source = source or (lambda n: silence())
        self.on_read = on_read
        self.reads = 0
        self.closed = False
        self.stopped = False

    def get_read_available(self):
        return CHUNK * 4

    def read(self, n, exception_on_overflow=False):
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        return self.source(self.reads)

    def is_stopped(self):
        return self.stopped

    def stop_stream(self):
        self.stopped = True

    def close(self):
        self.closed = True


class AudioEnv:
    def __init__(self):
        self.streams = []
        self.source = None
        self.on_read = None

    @property
    def opens(self):
        return len(self.streams)

    @property
    def reads(self):
        return sum(s.reads for s in self.streams)

    def open(self, p, **kw):
        s = FakeStream(self.source, self.on_read)
        self.streams.append(s)
        return s


def install_audio(monkeypatch):
    env = AudioEnv()
    monkeypatch.setattr(voice, "get_shared_pyaudio", lambda: object())
    monkeypatch.setattr(voice, "_shared_open", env.open)
    monkeypatch.setattr(voice, "_shared_close", lambda s: s.close())
    monkeypatch.setattr(voice, "find_best_microphone", lambda *a, **k: (0, "fake"))
    monkeypatch.setattr(voice, "_log_mic_open", lambda *a, **k: None)
    return env


def install_openwakeword(monkeypatch, scores):
    """Fake `openwakeword.model.Model`; the n-th constructed instance gets scores[n] (last repeats)."""
    made = []

    class Model:
        def __init__(self, *a, **k):
            self.score = scores[min(len(made), len(scores) - 1)]
            self.resets = 0
            self.predicted = 0
            made.append(self)

        def predict(self, arr):
            self.predicted += 1
            return {"hey_jarvis": self.score}

        def reset(self):
            self.resets += 1

    pkg = types.ModuleType("openwakeword")
    pkg.__path__ = []
    mod = types.ModuleType("openwakeword.model")
    mod.Model = Model
    monkeypatch.setitem(sys.modules, "openwakeword", pkg)
    monkeypatch.setitem(sys.modules, "openwakeword.model", mod)
    return made


def make_detector(monkeypatch, cfg=None, with_model=False, **kw):
    monkeypatch.setattr(voice.VADDetector, "_init_vad", lambda self: None)   # energy-only VAD, no Silero
    if not with_model:
        monkeypatch.setattr(voice.WakeWordDetector, "_init_model", lambda self: None)
    cfg = cfg or Config()
    cfg.wake_aliases = ["jarvis"]
    return voice.WakeWordDetector(cfg=cfg, **kw)


def wait_until(pred, timeout=3.0, step=0.01):
    """Bounded poll; returns pred() at the end."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def stop_and_join(wd, timeout=3.0):
    wd.stop.set()
    wd._ptt_event.set()           # wake an idle wait immediately
    wd.join(timeout)
    assert not wd.is_alive(), "WakeWordDetector thread did not exit"


class FakeAEC:
    """Stand-in for echo_canceller.EchoCanceller."""
    instances = []
    start_ok = True

    def __init__(self, *a, **k):
        self.closed = 0
        self.started = 0
        self.process_calls = 0
        self.mode = "echo"        # "echo": return a new array; "idle": return the input object
        FakeAEC.instances.append(self)

    def start(self):
        self.started += 1
        return FakeAEC.start_ok

    def process(self, arr):
        self.process_calls += 1
        return arr if self.mode == "idle" else (arr // 2).astype(np.int16)

    def close(self):
        self.closed += 1

    def stats(self):
        return {"enabled": True, "erle_db_estimate": 12.3, "frames": 1}


def install_fake_aec(monkeypatch, start_ok=True):
    import echo_canceller
    FakeAEC.instances = []
    FakeAEC.start_ok = start_ok
    monkeypatch.setattr(echo_canceller, "EchoCanceller", FakeAEC)
    return FakeAEC
