"""Cursor glide replay harness: real Controller + cursor thread, mocked _move/pyautogui, no devices.

Hand updates are replayed at 15/20/30 Hz (with +-20% jitter) in real time; the cursor position is sampled at 240 Hz.
Every loop is bounded by a wall-clock duration and the controller is always shut down.
"""
import math
import random
import time
import unittest.mock as um

import numpy as np
import pytest

import controller as controller_mod
from config import Config
from controller import Controller
from gestures import Action


def line(t):
    return 0.2 + 0.1 * t, 0.5


def stopgo(t):
    if t < 1.2:
        x = 0.1 * t
    elif t < 1.8:
        x = 0.12
    else:
        x = 0.12 + 0.1 * (t - 1.8)
    return 0.2 + x, 0.5


def still(t):
    return 0.3, 0.5


@pytest.fixture(autouse=True)
def _no_real_input():
    with um.patch.object(controller_mod, "pyautogui", um.MagicMock()):
        yield


def _make(glide=True, mode="relative"):
    cfg = Config(control_scheme="palm_fist")
    cfg.cursor_glide = glide
    cfg.tracking_mode = mode
    c = Controller(cfg)
    c._move = um.MagicMock()
    c._handle_dwell_action = um.MagicMock()
    c.active = True
    c.cur_x = c.target_x = c.sw * 0.2
    c.cur_y = c.target_y = c.sh * 0.5
    return c


def _run(path, hz, glide, dur, seed=1, mode="relative", precision=False, events=(), post=0.0):
    """events: [(t, kind)] extra actions injected. Returns (controller_state, updates, samples, extras)."""
    rnd = random.Random(seed)
    c = _make(glide, mode)
    try:
        t0 = time.perf_counter()
        next_u = next_s = 0.0
        ups, samp, extras = [], [], []
        evs = sorted(events)
        while True:
            t = time.perf_counter() - t0
            if t > dur:
                break
            if t >= next_u:
                x, y = path(t)
                c.handle(Action("move", x=x, y=y, precision=precision))
                ups.append((t, c.target_x, c.target_y))
                next_u = t + (1.0 / hz) * (1 + rnd.uniform(-0.2, 0.2))
            while evs and t >= evs[0][0]:
                _, kind = evs.pop(0)
                before = (c.cur_x, c.cur_y)
                c.handle(Action(kind, age=0.1) if kind == "rewind" else Action(kind))
                extras.append((t, kind, before, (c.cur_x, c.cur_y), c._move.call_args))
            if t >= next_s:
                samp.append((t, c.cur_x, c.cur_y, c.target_x, c.target_y, c._gv_vx, c._gv_vy))
                next_s += 1 / 240.0
            time.sleep(0.0005)
        if post:
            end = time.perf_counter() + post
            while time.perf_counter() < end:
                time.sleep(0.005)
        return (c.cur_x, c.cur_y), np.array(ups), np.array(samp), extras
    finally:
        c.shutdown()


def _cv(samp, lo, hi):
    s, x = samp[:, 0], samp[:, 1]
    ts = np.arange(lo, min(hi, s[-1]), 1 / 120.0)
    step = np.diff(np.interp(ts, s, x))
    return step.std() / step.mean()


@pytest.mark.parametrize("hz", [15, 20, 30])
def test_step_cv_lower_with_glide(hz):
    cv = {}
    for glide in (False, True):
        _, _, samp, _ = _run(line, hz, glide, 2.2)
        cv[glide] = _cv(samp, 0.5, 2.1)
    assert cv[True] < cv[False], cv


@pytest.mark.parametrize("hz", [15, 20, 30])
def test_overshoot_at_stop_within_1_5_px(hz):
    _, _, samp, _ = _run(stopgo, hz, True, 2.4)
    s, x = samp[:, 0], samp[:, 1]
    idx = (s > 1.2) & (s < 1.8)
    settled = np.interp(1.79, s, x)
    assert x[idx].max() - settled <= 1.5


def test_no_prediction_in_absolute_mode():
    _, _, samp, _ = _run(line, 20, True, 1.2, mode="absolute")
    assert np.all(samp[:, 5] == 0) and np.all(samp[:, 6] == 0)


def test_no_prediction_in_precision_mode():
    _, _, samp, _ = _run(line, 20, True, 1.2, precision=True)
    assert np.all(samp[:, 5] == 0) and np.all(samp[:, 6] == 0)


def test_glide_actually_leads_target_in_relative_mode():
    # positive control for the blocking tests below
    _, _, samp, _ = _run(line, 20, True, 1.6)
    moving = samp[:, 0] > 0.6
    assert (samp[moving, 1] - samp[moving, 3]).max() > 0.5


@pytest.mark.parametrize("kind", ["click", "rewind", "drag_start"])
def test_no_prediction_within_150ms_of_event(kind):
    t_ev = 1.0
    _, _, samp, extras = _run(line, 20, True, t_ev + 0.14, events=[(t_ev, kind)])
    assert extras and extras[0][1] == kind
    win = (samp[:, 0] > extras[0][0] + 0.005) & (samp[:, 0] < t_ev + 0.14)
    assert win.sum() > 5
    # blocked: the cursor never runs ahead of the target (exponential approach from behind)
    assert (samp[win, 1] - samp[win, 3]).max() <= 1e-6


def test_click_position_identical_with_glide_on_and_off():
    clicks = {}
    for glide in (False, True):
        _, _, samp, extras = _run(still, 20, glide, 0.9, events=[(0.8, "click")])
        t, kind, before, after, call = extras[0]
        # pre-click drift: the click is issued exactly at the (snapped) cursor position
        assert call.args == (round(after[0]), round(after[1]))
        clicks[glide] = call.args
    assert clicks[True] == clicks[False]


def test_click_snaps_to_history_lookback_with_glide():
    # moving hand: cursor snapped to the lookback sample, zero drift between snap and the move() call
    _, _, _, extras = _run(line, 20, True, 1.1, events=[(1.0, "click")])
    t, kind, before, after, call = extras[0]
    assert call.args == (round(after[0]), round(after[1]))
    assert math.hypot(after[0] - call.args[0], after[1] - call.args[1]) <= 0.5


def test_glide_off_has_no_prediction_and_matches_old_behaviour():
    _, _, samp, _ = _run(line, 20, False, 1.6)
    # cursor never ahead of target when glide is off
    assert (samp[:, 1] - samp[:, 3]).max() <= 1e-6
    # and the glide lead never enters the target-chasing math: cursor trails the hand path
    moving = samp[:, 0] > 0.6
    assert (samp[moving, 3] - samp[moving, 1]).min() >= -1e-6
