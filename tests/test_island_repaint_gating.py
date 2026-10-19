"""Island repaint gating on the landmark change token (offscreen, virtual clock, no timers)."""
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from config import Config
from overlay import Overlay


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def island(app):
    stop = threading.Event()
    ov = Overlay(stop, cfg=Config())
    isl = ov.island
    isl.reset_to_idle()
    yield isl
    stop.set()
    ov.timer.stop()
    app.processEvents()


def _lms(k):
    return [[(0.1 + 0.01 * i + 0.001 * k, 0.2 + 0.01 * i) for i in range(21)]]


def _settle(isl, now, frames=240):
    for _ in range(frames):
        now += 1 / 60
        isl.update_frame(now)
    return now


def _tracking(isl):
    isl.active = True
    isl.media_playing = False
    isl.landmarks = _lms(0)
    return _settle(isl, time.time())


def _count(isl, now, seconds, post_every=None, start_k=1):
    isl.update = MagicMock()
    k = start_k
    n = int(seconds * 60)
    for f in range(n):
        if post_every and f % post_every == 0:
            isl.landmarks = _lms(k)
            k += 1
        now += 1 / 60
        isl.update_frame(now)
    return isl.update.call_count, k - start_k, now


def test_paints_track_post_rate_with_changing_landmarks(island):
    now = _tracking(island)
    paints, posts, _ = _count(island, now, 2.0, post_every=3)     # 20 Hz posts at 60 Hz ticks
    assert posts == 40
    assert 0.8 * posts <= paints <= 1.3 * posts, (paints, posts)


def test_no_paints_with_unchanged_landmarks(island):
    now = _tracking(island)
    paints, _, _ = _count(island, now, 2.0, post_every=None)
    assert paints == 0


def test_same_values_reposted_do_not_paint(island):
    now = _tracking(island)
    island.update = MagicMock()
    for f in range(60):
        island.landmarks = _lms(0)              # new list objects, identical coordinates
        now += 1 / 60
        island.update_frame(now)
    assert island.update.call_count == 0


def test_media_playing_keeps_frame_rate_updates(island):
    now = _tracking(island)
    island.media_playing = True
    island.media_duration_sec = 3600.0
    paints, _, _ = _count(island, now, 1.0, post_every=None)
    assert paints >= 55, paints


def test_reload_config_marks_dirty_and_repaints(island):
    now = _tracking(island)
    island.update = MagicMock()
    island.reload_config()
    assert island._dirty is True
    island.update.reset_mock()                 # reload_config may itself call update()
    island.update_frame(now + 1 / 60)
    assert island.update.call_count == 1
    assert island._dirty is False
