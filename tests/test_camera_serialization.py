"""
Camera access must be serialized: FrameGrabber.open_camera and list_cameras(force_refresh=True)
must never run VideoCapture open/release concurrently, and list_cameras must not probe the
index the grabber holds or is opening. Fake cv2.VideoCapture, no hardware.
"""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import tracker

OP_SLEEP = 0.05


class _Recorder:
    def __init__(self):
        self.ops = []  # (thread, op, idx, t0, t1)
        self.lock = threading.Lock()

    def rec(self, op, idx, t0, t1):
        with self.lock:
            self.ops.append((threading.current_thread().name, op, idx, t0, t1))


@pytest.fixture
def fake_cv2(monkeypatch):
    r = _Recorder()

    class FakeCap:
        def __init__(self, idx, backend=0):
            self.idx = idx
            t0 = time.perf_counter()
            time.sleep(OP_SLEEP)
            r.rec("open", idx, t0, time.perf_counter())

        def set(self, *a):
            time.sleep(OP_SLEEP)
            return True

        def get(self, *a):
            return 0

        def isOpened(self):
            return True

        def read(self):
            return True, np.zeros((720, 1280, 3), dtype=np.uint8)

        def release(self):
            t0 = time.perf_counter()
            time.sleep(OP_SLEEP)
            r.rec("release", self.idx, t0, time.perf_counter())

    monkeypatch.setattr(tracker.cv2, "VideoCapture", FakeCap)
    monkeypatch.setattr(tracker, "_ACTIVE_GRABBER_IDX", None)
    tracker.clear_camera_cache()
    yield r
    tracker.clear_camera_cache()


def test_open_camera_and_list_cameras_are_serialized(fake_cv2):
    cfg = SimpleNamespace(camera_index=0, frame_width=1280, frame_height=720, camera_mjpg=False, camera_fps=30)
    grabber = tracker.FrameGrabber(cfg, threading.Event())

    t_open = threading.Thread(target=grabber.open_camera, name="grabber-open")
    t_list = threading.Thread(target=lambda: tracker.list_cameras(force_refresh=True), name="lister")
    t_open.start()
    time.sleep(0.01)
    t_list.start()
    t_open.join(15)
    t_list.join(15)
    assert not t_open.is_alive() and not t_list.is_alive()

    ops = [o for o in fake_cv2.ops if o[1] in ("open", "release")]
    assert any(o[0] == "grabber-open" for o in ops)
    for i, a in enumerate(ops):
        for b in ops[i + 1:]:
            if a[0] != b[0]:
                overlap = min(a[4], b[4]) - max(a[3], b[3])
                assert overlap <= 0.001, f"overlapping camera ops: {a} vs {b}"

    held = {o[2] for o in ops if o[0] == "grabber-open" and o[1] == "open"}
    probed = {o[2] for o in ops if o[0] == "lister" and o[1] == "open"}
    assert not (held & probed), f"list_cameras probed index(es) {held & probed} held by FrameGrabber"
