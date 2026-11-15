"""Camera on-demand: Tracker idles (thread alive, device + MediaPipe released) while the camera is off."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import tracker
import thread_supervisor


class _Cap:
    opened = 0
    released = 0
    frame_value = 100

    def __init__(self, idx, backend=0):
        _Cap.opened += 1
        self.idx = idx
        self._rel = False

    def set(self, *a):
        return True

    def get(self, *a):
        return 0

    def isOpened(self):
        return True

    def read(self):
        time.sleep(0.002)
        return True, np.full((48, 64, 3), _Cap.frame_value, dtype=np.uint8)

    def release(self):
        if not self._rel:
            self._rel = True
            _Cap.released += 1


class _Hands:
    created = 0
    closed = 0

    def __init__(self, **kw):
        _Hands.created += 1

    def process(self, rgb):
        return SimpleNamespace(multi_hand_landmarks=None)

    def close(self):
        _Hands.closed += 1


class _Engine:
    active = True
    dragging = False

    def update(self, found, now):
        return []

    def status(self, now):
        return {"active": True, "label": "X", "progress": 0.0}

    def request(self, want):
        self.active = bool(want)


class _Overlay:
    def __init__(self):
        self.events = []

    def post(self, kind, **d):
        self.events.append((kind, d))


class _Ctx:
    def __init__(self):
        self.auto_off = 0

    def say(self, t):
        pass

    def chirp(self, k=True):
        pass

    def gesture_mode_auto_off(self):
        self.auto_off += 1


class _Ctl:
    def __init__(self):
        self.actions = []

    def handle(self, a):
        self.actions.append(a.kind)

    def _reset_filters(self):
        pass


@pytest.fixture
def env(monkeypatch):
    _Cap.opened = _Cap.released = 0
    _Cap.frame_value = 100
    _Hands.created = _Hands.closed = 0
    monkeypatch.setattr(tracker.cv2, "VideoCapture", _Cap)
    monkeypatch.setattr(tracker, "_ACTIVE_GRABBER_IDX", None)
    mp = SimpleNamespace(solutions=SimpleNamespace(hands=SimpleNamespace(
        Hands=_Hands, HAND_CONNECTIONS=[])))
    monkeypatch.setattr(tracker, "_get_mp", lambda: mp)
    tracker.clear_camera_cache()
    made = []

    def make(autostart=False, idle=0.0):
        cfg = SimpleNamespace(camera_index=0, frame_width=64, frame_height=48, camera_mjpg=False, camera_fps=30,
                              camera_autostart=autostart, gesture_idle_timeout_s=idle,
                              show_preview=False, model_complexity=0, min_detection_confidence=0.5,
                              min_tracking_confidence=0.5)
        stop = threading.Event()
        t = tracker.Tracker(cfg, _Engine(), _Ctl(), _Overlay(), _Ctx(), stop)
        made.append((t, stop))
        return t, stop

    yield make
    for t, stop in made:
        stop.set()
        t.set_camera_wanted(False)
        if t.ident is not None:
            t.join(8)
    tracker.clear_camera_cache()


def _wait(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_starts_off_when_autostart_false(env):
    t, _ = env(autostart=False)
    assert t.camera_wanted is False
    t.start()
    assert _wait(lambda: t.camera_state()[0] == "off")
    time.sleep(0.2)
    assert _Cap.opened == 0 and _Hands.created == 0
    assert t.is_alive()


def test_autostart_true_opens(env):
    t, _ = env(autostart=True)
    t.start()
    assert _wait(lambda: _Hands.created == 1)
    assert _Cap.opened >= 1


def test_on_off_releases_device_and_hands_and_thread_survives(env):
    t, _ = env()
    t.start()
    t.set_camera_wanted(True)
    assert _wait(lambda: _Hands.created == 1 and t._grabber is not None)
    assert _wait(lambda: t.camera_state()[0] == "ok")
    t.set_camera_wanted(False)
    assert _wait(lambda: _Hands.closed == 1)
    assert _wait(lambda: _Cap.released == _Cap.opened)
    assert _wait(lambda: t.camera_state()[0] == "off")
    assert tracker._ACTIVE_GRABBER_IDX is None
    assert t.is_alive()
    assert "lost" in t.controller.actions       # failsafe release ran
    t.set_camera_wanted(True)                   # and it can come back
    assert _wait(lambda: _Hands.created == 2)


def test_idle_timeout_turns_camera_off(env):
    t, _ = env(autostart=True, idle=0.3)
    t.start()
    assert _wait(lambda: _Hands.created == 1)
    assert _wait(lambda: t.camera_wanted is False, 5)
    assert t.ctx.auto_off == 1
    assert _wait(lambda: t.camera_state() == ("off", "idle timeout"))
    assert _wait(lambda: _Cap.released == _Cap.opened)
    assert t.is_alive()


def test_idle_timeout_not_while_dragging(env):
    t, _ = env(autostart=True, idle=0.3)
    t.engine.dragging = True
    t.start()
    assert _wait(lambda: _Hands.created == 1)
    time.sleep(0.8)
    assert t.camera_wanted is True


def test_snapshot_when_off_opens_and_releases(env):
    t, _ = env()
    frame = t.snapshot_frame(2.0)      # works without the thread running
    assert frame is not None and frame.shape == (48, 64, 3)
    assert _Cap.opened == 1 and _Cap.released == 1
    assert _Hands.created == 0
    assert tracker._ACTIVE_GRABBER_IDX is None


def test_snapshot_skips_dark_warmup_frames(env):
    t, _ = env()
    reads = {"n": 0}
    orig = _Cap.read

    def read(self):
        reads["n"] += 1
        ok, f = orig(self)
        f[:] = 0 if reads["n"] < 8 else 120
        return ok, f

    _Cap.read = read
    try:
        frame = t.snapshot_frame(3.0)
    finally:
        _Cap.read = orig
    assert frame is not None and frame.mean() > 100


def test_snapshot_when_on_uses_live_frame(env):
    t, _ = env(autostart=True)
    t.start()
    assert _wait(lambda: _Hands.created == 1)
    opened = _Cap.opened
    assert t.snapshot_frame(2.0) is not None
    assert _Cap.opened == opened


def test_health_reports_off_not_down(env):
    t, _ = env()
    t.start()
    assert _wait(lambda: t.camera_state()[0] == "off")
    h = thread_supervisor._compute_health([], engine=None, tracker=t)
    assert h["workers"]["Camera"] == "off"
    assert h["state"] == "ok"
