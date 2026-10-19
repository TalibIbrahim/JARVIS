"""PagingMixin.wheelEvent: accumulation, 350 ms cooldown, reversal/gap reset (time patched, no Qt event loop)."""
from unittest.mock import patch

import pytest

from ui.island_parts import paging
from ui.island_parts.paging import PagingMixin


class _Pt:
    def __init__(self, y):
        self._y = y

    def y(self):
        return self._y


class _Ev:
    def __init__(self, angle=0, pixel=0):
        self._a, self._p, self.accepted = angle, pixel, False

    def angleDelta(self):
        return _Pt(self._a)

    def pixelDelta(self):
        return _Pt(self._p)

    def accept(self):
        self.accepted = True


class _Host(PagingMixin):
    def __init__(self):
        self.expanded_card = "media"
        self.steps = []

    def page_step(self, direction=1):
        self.steps.append(direction)


class _Clock:
    t = 1000.0


@pytest.fixture
def rig():
    clk = _Clock()
    clk.t = 1000.0
    host = _Host()
    with patch.object(paging.time, "monotonic", lambda: clk.t):
        yield host, clk


def wheel(host, clk, dt, angle=0, pixel=0):
    clk.t += dt
    ev = _Ev(angle, pixel)
    host.wheelEvent(ev)
    return ev


def test_single_notch_forward_once(rig):
    host, clk = rig
    ev = wheel(host, clk, 0.0, -120)
    assert host.steps == [1] and ev.accepted


def test_positive_is_previous(rig):
    host, clk = rig
    wheel(host, clk, 0.0, 120)
    assert host.steps == [-1]


def test_four_small_events_step_once(rig):
    host, clk = rig
    for _ in range(4):
        wheel(host, clk, 0.05, -30)
    assert host.steps == [1]


def test_cooldown_then_step(rig):
    host, clk = rig
    wheel(host, clk, 0.0, -120)
    wheel(host, clk, 0.2, -120)  # inside 350 ms cooldown
    assert host.steps == [1]
    wheel(host, clk, 0.2, -120)  # 400 ms after first step
    assert host.steps == [1, 1]


def test_reversal_resets_accumulator(rig):
    host, clk = rig
    wheel(host, clk, 0.0, -90)
    wheel(host, clk, 0.05, 60)   # reversal: acc restarts at +60
    wheel(host, clk, 0.05, 60)   # +120 -> previous
    assert host.steps == [-1]


def test_gap_resets_accumulator(rig):
    host, clk = rig
    wheel(host, clk, 0.0, -90)
    wheel(host, clk, 0.5, -90)   # gap > 300 ms: -90 only
    assert host.steps == []
    wheel(host, clk, 0.05, -30)
    assert host.steps == [1]


def test_touchpad_flick_bounded(rig):
    host, clk = rig
    for _ in range(50):
        wheel(host, clk, 0.02, -10)  # 50 events over 1 s
    assert 1 <= len(host.steps) <= 3


def test_pixel_delta_only(rig):
    host, clk = rig
    wheel(host, clk, 0.0, 0, -120)
    assert host.steps == [1]
    wheel(host, clk, 0.5, 0, 120)
    assert host.steps == [1, -1]


def test_zero_delta_ignored(rig):
    host, clk = rig
    wheel(host, clk, 0.0, 0, 0)
    assert host.steps == []
