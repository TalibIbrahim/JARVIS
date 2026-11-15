"""Frame timer is fast only while the island animates; gesture-mode indicator and hook."""
import sys
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from config import Config
from overlay import Overlay


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def ov(app):
    stop = threading.Event()
    o = Overlay(stop, cfg=Config())
    o.island.reset_to_idle()
    yield o
    stop.set()
    o.timer.stop()
    app.processEvents()



def test_fast_timer_inactive_when_idle(ov):
    ov.island.media_playing = False
    ov.island.active = False
    for _ in range(300):
        ov._tick()
        time.sleep(0.002)
    assert ov.island.animating is False
    assert ov.timer.interval() == Overlay.IDLE_TICK_MS > Overlay.FAST_TICK_MS


def test_fast_timer_returns_on_animation(ov):
    for _ in range(300):
        ov._tick()
    assert ov.timer.interval() == Overlay.IDLE_TICK_MS
    ov.island.media_playing = True
    ov.island.media_duration_sec = 3600.0
    ov._tick()
    assert ov.timer.interval() == Overlay.FAST_TICK_MS


def test_gesture_mode_flag_and_dirty(ov):
    isl = ov.island
    assert isl.gesture_mode is False
    ov.post("gesture_mode", on=True)
    ov._tick()
    assert isl.gesture_mode is True
    assert ov.on_gesture_mode_toggle is None
