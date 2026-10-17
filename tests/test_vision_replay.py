"""
tests/test_vision_replay.py — Vision & Gesture Replay Invariants Test Suite.

Verifies:
- Replay test 1: Gesture precision invariant — assert 0.0 px pre-click drift via 200 ms lookback buffer when fist closes.
- Replay test 2: Deadzone filter — assert 0.0 px cursor motion on resting micro-tremor under 1.5 px.
- Replay test 3: Dwell state machine — assert hover accumulation, stationarity radius (14 px), and anti-repeat latching.
- Replay test 4: Cursor engine frequency — assert controller loop runs at >= 120 Hz with low jitter.
- Replay test 5: Knuckle anchor stability — assert MCP knuckle centroid (landmarks 5, 9, 13, 17) drift is < 2.0 px during fist curl.

Strictly NO Qt imports. 100% unit-testable with clean interfaces.
"""

from __future__ import annotations

import math
import os
import random
import statistics
import sys
import time
import unittest.mock

import pytest

# Ensure repository root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from config import Config
from controller import Controller, LookbackBuffer
from dwell import DwellClicker
from filters import DeadzoneFilter
from gestures import (
    INDEX_MCP,
    INDEX_TIP,
    MIDDLE_MCP,
    PINKY_MCP,
    RING_MCP,
    Action,
    GestureEngine,
    Hand,
)
from tools.replay_harness import (
    LandmarkStream,
    ReplayFrame,
    compute_mcp_knuckle_centroid,
    evaluate_cursor_engine_frequency,
    evaluate_deadzone_invariant,
    evaluate_dwell_state_machine_invariants,
    evaluate_gesture_precision_invariant,
    evaluate_knuckle_anchor_stability,
    generate_dwell_hover_stream,
    generate_fist_click_stream,
    generate_moving_stream,
    generate_stationary_stream,
    synthesize_hand_landmarks,
)


# =====================================================================
# Replay Test 1: Gesture Precision Invariant (0.0 px Pre-Click Drift)
# =====================================================================

def test_replay_gesture_precision_lookback_drift_elimination():
    """
    Asserts 0.0 px pre-click drift via the 200 ms lookback buffer when fist closes.
    Even with significant tendon deflection (e.g. 15 px) right before closure,
    the lookback buffer returns the cursor to the exact pre-curl aim coordinates.
    """
    drift_px = evaluate_gesture_precision_invariant(lookback_ms=200.0, tendon_deflect_px=15.0)
    assert drift_px == 0.0, f"Expected 0.0 px pre-click drift, got {drift_px:.4f} px"


@pytest.mark.parametrize("lookback_ms", [150.0, 200.0, 250.0])
def test_replay_gesture_precision_various_lookback_windows(lookback_ms: float):
    """Verifies lookback buffer invariant holds across calibrated lookback windows."""
    cfg = Config(control_scheme="palm_fist", cursor_lookback_ms=lookback_ms)
    ctrl = Controller(cfg)
    ctrl._move = unittest.mock.MagicMock()
    ctrl._handle_dwell_action = unittest.mock.MagicMock()

    aim_x, aim_y = 800.0, 450.0
    ctrl.cur_x, ctrl.cur_y = aim_x, aim_y
    ctrl.target_x, ctrl.target_y = aim_x, aim_y
    ctrl.history.clear()

    # Pre-curl aim hold (300 ms)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.30:
        ctrl.cur_x, ctrl.cur_y = aim_x, aim_y
        ctrl.target_x, ctrl.target_y = aim_x, aim_y
        time.sleep(0.01)

    pre_aim_x, pre_aim_y = ctrl.cur_x, ctrl.cur_y

    # Simulate 12 px deflection during final 100 ms of closure
    t_curl = time.perf_counter()
    while time.perf_counter() - t_curl < 0.10:
        ctrl.cur_x += 1.2
        ctrl.cur_y += 0.8
        ctrl.target_x = ctrl.cur_x
        ctrl.target_y = ctrl.cur_y
        time.sleep(0.01)

    # Click triggers
    ctrl.handle(Action("click"))

    drift = math.hypot(ctrl.cur_x - pre_aim_x, ctrl.cur_y - pre_aim_y)
    ctrl.shutdown()
    assert drift == 0.0, f"Expected 0.0 px drift with lookback {lookback_ms}ms, got {drift:.4f} px"


# =====================================================================
# Replay Test 2: Deadzone Filter (0.0 px Motion on Micro-Tremor)
# =====================================================================

def test_replay_deadzone_filter_suppresses_resting_micro_tremor():
    """
    Asserts 0.0 px cursor motion on resting hand micro-tremor under 1.5 px.
    Simulates 200 synthetic frames with realistic multi-directional jitter < 1.45 px.
    """
    max_motion = evaluate_deadzone_invariant(deadzone_px=1.5, max_micro_tremor_px=1.45, n_frames=200)
    assert max_motion == 0.0, f"Expected 0.0 px cursor motion, got {max_motion:.4f} px"


def test_replay_deadzone_filter_allows_intentional_motion():
    """
    Verifies that while resting tremor (< 1.5 px) produces 0.0 px motion,
    intentional motion (> 1.5 px) smoothly moves the cursor without stickiness.
    """
    dz = DeadzoneFilter(deadzone=1.5)
    ox, oy = dz(500.0, 500.0)
    assert (ox, oy) == (500.0, 500.0)

    # Micro-tremor: 1.2 px jump -> 0.0 px movement
    mx, my = dz(501.2, 500.0)
    assert (mx, my) == (500.0, 500.0)

    # Intentional motion: 10 px jump -> smooth attenuated advance
    ix, iy = dz(510.0, 500.0)
    assert ix > 500.0
    assert ix == pytest.approx(500.0 + (10.0 - 1.5), abs=1e-4)


# =====================================================================
# Replay Test 3: Dwell State Machine Invariants
# =====================================================================

def test_replay_dwell_state_machine_accumulation_radius_and_latching():
    """
    Asserts:
    1. Hover accumulation inside 14 px stationarity radius.
    2. Click trigger at dwell_time (1.0s).
    3. Anti-repeat latching (0 duplicate clicks while staying inside radius).
    4. Reset and unlatching when moving outside 14 px.
    5. Ability to accumulate and trigger a second dwell click at a new location.
    """
    res = evaluate_dwell_state_machine_invariants(dwell_radius=14.0, dwell_time=1.0)

    assert 0.48 <= res["mid_progress"] <= 0.52, f"Expected ~0.50 progress, got {res['mid_progress']}"
    assert res["click_action"] == "click", "Dwell click action was not emitted at 1.0s"
    assert res["click_latched"] is True, "DwellClicker was not latched upon click trigger"
    assert res["duplicate_clicks"] == 0, f"Anti-repeat failed: {res['duplicate_clicks']} duplicate clicks emitted"
    assert res["latched_held"] is True, "Anti-repeat latch was prematurely dropped"
    assert res["unlatched_after_exit"] is True, "Failed to unlatch after moving outside 14 px stationarity radius"
    assert res["second_click_action"] == "click", "Second dwell click failed to trigger after re-accumulating"


def test_replay_dwell_stream_simulation():
    """
    Feeds a synthetic dwell hover stream through DwellClicker to verify
    temporal dynamics and phase progression.
    """
    cfg = Config()
    cfg.dwell_mode = "click"
    cfg.dwell_time = 1.0
    cfg.dwell_radius = 14.0
    dc = DwellClicker(cfg)

    stream = generate_dwell_hover_stream(
        duration=2.5,
        fps=60.0,
        dwell_radius_px=14.0,
        hover_jitter_px=6.0,
        leave_after_sec=1.6,
        leave_offset_px=30.0,
    )

    sw, sh = 1920, 1080
    clicks = []
    latched_frames = 0
    unlatched_frames = 0

    for f in stream.frames:
        lm = f.hands[0]
        # MCP knuckles centroid as screen position
        cx, cy = compute_mcp_knuckle_centroid(lm, sw, sh)
        r = dc.update(cx, cy, f.timestamp)
        if r["action"] == "click":
            clicks.append(f.timestamp)
        if dc.latched:
            latched_frames += 1
        else:
            unlatched_frames += 1

    # Exactly 1 click while inside the radius
    assert len(clicks) == 1, f"Expected exactly 1 dwell click, got {len(clicks)}"
    assert 0.95 <= clicks[0] <= 1.05, f"Click fired at unexpected time: {clicks[0]}s"
    # Latched during remaining hover time (from 1.0s to 1.6s)
    assert latched_frames > 25
    # Unlatched after moving away (> 1.6s)
    assert not dc.latched


# =====================================================================
# Replay Test 4: Cursor Engine Frequency & Jitter (>= 120 Hz)
# =====================================================================

def test_replay_cursor_engine_frequency_and_jitter():
    """
    Asserts controller loop runs at >= 120 Hz with low jitter.
    Measured rate must be >= 120.0 Hz, mean interval ~8.33 ms, jitter std < 0.5 ms.
    """
    rate, mean_dt, jitter_std = evaluate_cursor_engine_frequency(target_hz=120, test_duration=0.4)

    assert rate >= 120.0, f"Expected cursor loop frequency >= 120.0 Hz, measured {rate:.2f} Hz"
    assert 7.5 <= mean_dt <= 9.0, f"Mean dt out of range: {mean_dt:.3f} ms"
    assert jitter_std < 0.50, f"Jitter standard deviation too high: {jitter_std:.4f} ms (expected < 0.5 ms)"


# =====================================================================
# Replay Test 5: Knuckle Anchor Stability (< 2.0 px Drift)
# =====================================================================

def test_replay_knuckle_anchor_stability_during_fist_curl():
    """
    Asserts MCP knuckle centroid (landmarks 5, 9, 13, 17) drift is < 2.0 px
    during fist curl, despite fingertips travelling > 100 px.
    """
    k_drift, tip_travel = evaluate_knuckle_anchor_stability()

    assert k_drift < 2.0, f"Knuckle centroid drift must be < 2.0 px, measured {k_drift:.3f} px"
    assert tip_travel > 100.0, f"Expected fingertip travel > 100 px, measured {tip_travel:.1f} px"
    # Knuckle stability ratio: knuckles should be at least 50x more stable than fingertips
    ratio = tip_travel / max(k_drift, 1e-4)
    assert ratio > 50.0, f"Knuckle stability ratio too low: {ratio:.1f}x"


# =====================================================================
# Replay Stream Serialization Tests
# =====================================================================

def test_replay_stream_save_and_load(tmp_path):
    """Verifies that LandmarkStream serializes to JSON and deserializes identically."""
    filepath = str(tmp_path / "test_stream.json")
    stream = generate_fist_click_stream(duration=0.5, fps=30.0)
    stream.save_json(filepath)

    loaded = LandmarkStream.load_json(filepath)
    assert loaded.name == stream.name
    assert loaded.fps == stream.fps
    assert len(loaded.frames) == len(stream.frames)

    orig_first_lm = stream.frames[0].hands[0]
    load_first_lm = loaded.frames[0].hands[0]
    assert len(orig_first_lm) == len(load_first_lm) == 21
    for pt1, pt2 in zip(orig_first_lm, load_first_lm):
        assert pt1[0] == pytest.approx(pt2[0], abs=1e-6)
        assert pt1[1] == pytest.approx(pt2[1], abs=1e-6)
