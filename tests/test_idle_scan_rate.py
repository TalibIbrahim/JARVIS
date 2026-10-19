"""Tracker standby scan sleeps 0.12 s; active engine / recent hand / preview skip it. No camera, no mediapipe."""
import threading
import time as _real_time
import types

import numpy as np

import tracker
from config import Config


class _TimeProxy:
    def __init__(self, sleeps):
        self._sleeps = sleeps

    def sleep(self, s):
        self._sleeps.append(s)

    def __getattr__(self, name):
        return getattr(_real_time, name)


class _FakeGrabber:
    STALL_AFTER_S = 3.0
    fps = 30.0
    failures = 0
    opened = True

    def __init__(self, *a, **k):
        self._n = 0

    def open_camera(self):
        return True

    def start(self):
        pass

    def is_alive(self):
        return True

    def stalled_for(self):
        return 0.0

    def release(self):
        pass

    def reload_config(self, cfg):
        pass

    def get_latest(self):
        self._n += 1
        assert self._n < 50, "loop did not terminate"
        return np.zeros((4, 4, 3), np.uint8), _real_time.perf_counter()


def _drive(monkeypatch, engine_active, hand_in_first_frame, show_preview=False, frames=2):
    sleeps = []
    stop = threading.Event()
    calls = {"n": 0}

    class _Hands:
        def __init__(self, **k):
            pass

        def process(self, img):
            calls["n"] += 1
            lms = types.SimpleNamespace(landmark=[types.SimpleNamespace(x=0.5, y=0.5) for _ in range(21)])
            has = hand_in_first_frame and calls["n"] == 1
            if calls["n"] >= frames:
                stop.set()
            return types.SimpleNamespace(multi_hand_landmarks=[lms] if has else None)

        def close(self):
            pass

    fake_mp = types.SimpleNamespace(solutions=types.SimpleNamespace(
        hands=types.SimpleNamespace(Hands=_Hands, HAND_CONNECTIONS=[])))
    monkeypatch.setattr(tracker, "FrameGrabber", _FakeGrabber)
    monkeypatch.setattr(tracker, "_get_mp", lambda: fake_mp)
    monkeypatch.setattr(tracker, "time", _TimeProxy(sleeps))
    monkeypatch.setattr(tracker.Tracker, "_preview", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tracker.cv2, "waitKey", lambda *_: -1)
    monkeypatch.setattr(tracker.cv2, "getWindowProperty", lambda *a, **k: 1)
    monkeypatch.setattr(tracker.cv2, "destroyAllWindows", lambda *a, **k: None)
    monkeypatch.setattr(tracker.cv2, "imshow", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tracker.cv2, "destroyWindow", lambda *a, **k: None, raising=False)

    cfg = Config()
    cfg.threaded_capture = True
    cfg.show_preview = show_preview
    engine = types.SimpleNamespace(active=engine_active, update=lambda found, now: [],
                                   status=lambda now: {"active": engine_active, "label": "", "progress": 0.0})
    ctx = types.SimpleNamespace(say=lambda *a, **k: None, chirp=lambda *a, **k: None)
    overlay = types.SimpleNamespace(post=lambda *a, **k: None)
    controller = types.SimpleNamespace(handle=lambda a: None, _reset_filters=lambda: None)
    t = tracker.Tracker(cfg, engine, controller, overlay, ctx, stop)
    t._run_body()
    assert stop.is_set()
    return sleeps


def test_standby_without_hand_sleeps_012(monkeypatch):
    sleeps = _drive(monkeypatch, engine_active=False, hand_in_first_frame=False)
    assert sleeps.count(0.12) == 2           # one per loop iteration, both frames
    assert 0.06 not in sleeps


def test_active_engine_does_not_sleep(monkeypatch):
    sleeps = _drive(monkeypatch, engine_active=True, hand_in_first_frame=False)
    assert 0.12 not in sleeps and 0.06 not in sleeps


def test_recent_hand_skips_standby_sleep(monkeypatch):
    sleeps = _drive(monkeypatch, engine_active=False, hand_in_first_frame=True)
    assert sleeps.count(0.12) == 1           # first iteration only; hand seen => no sleep on the second


def test_show_preview_does_not_sleep(monkeypatch):
    sleeps = _drive(monkeypatch, engine_active=False, hand_in_first_frame=False, show_preview=True)
    assert 0.12 not in sleeps
