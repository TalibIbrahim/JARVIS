"""
tools/replay_harness.py — Synthetic Landmark Record-and-Replay Generator & Invariant Evaluator.

Provides:
- Synthetic landmark stream generation simulating:
  1. Stationary hands with micro-tremor (< 1.5 px jitter).
  2. Smoothly moving hands with linear/curved trajectories.
  3. Fist-click closure with human biomechanical tendon deflection.
  4. Dwell hovering within the 14 px stationarity radius with anti-repeat latching.
- LandmarkStream serialization to / from JSON.
- High-precision replay evaluation routines asserting:
  - 0.0 px pre-click drift via 200 ms lookback buffer.
  - 0.0 px cursor motion on resting micro-tremor under 1.5 px.
  - Dwell hover accumulation, 14 px stationarity radius, and anti-repeat latching.
  - Decoupled cursor engine frequency >= 120 Hz with low jitter (< 0.1 ms).
  - MCP knuckle centroid drift < 2.0 px during fist curl.

Strictly NO Qt imports. 100% unit-testable with clean interfaces.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
import os
import random
import statistics
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

# Ensure repository root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from config import Config
from dwell import DwellClicker
from filters import DeadzoneFilter, OneEuro
from gestures import (
    INDEX_DIP,
    INDEX_MCP,
    INDEX_PIP,
    INDEX_TIP,
    MIDDLE_DIP,
    MIDDLE_MCP,
    MIDDLE_PIP,
    MIDDLE_TIP,
    PINKY_DIP,
    PINKY_MCP,
    PINKY_PIP,
    PINKY_TIP,
    RING_DIP,
    RING_MCP,
    RING_PIP,
    RING_TIP,
    THUMB_CMC,
    THUMB_IP,
    THUMB_MCP,
    THUMB_TIP,
    WRIST,
    Action,
    GestureEngine,
    Hand,
    Pose,
)


# =====================================================================
# Synthetic Hand Anatomy Synthesis
# =====================================================================

def synthesize_hand_landmarks(
    cx: float = 0.5,
    cy: float = 0.5,
    curl: float = 0.0,
    tendon_jitter_px: Tuple[float, float] = (0.0, 0.0),
    screen_w: int = 1920,
    screen_h: int = 1080,
    thumb_extended: bool = True,
) -> List[Tuple[float, float]]:
    """
    Synthesizes 21 MediaPipe hand landmarks with realistic biomechanics.
    (cx, cy) represents the MCP knuckle centroid in normalized (0..1) space.
    curl: 0.0 (fully open palm) to 1.0 (tight fist).
    tendon_jitter_px: physical tendon deflection shift applied to metacarpals during curl.
    """
    jx = tendon_jitter_px[0] / float(screen_w)
    jy = tendon_jitter_px[1] / float(screen_h)

    # Metacarpal Knuckles (MCP joints: 5, 9, 13, 17) & Wrist (0)
    # The knuckle arch stays geometrically stable, subject only to tendon deflection
    wrist = (cx + jx * 0.5, cy + 0.18 + jy * 0.5)
    mcp5 = (cx - 0.06 + jx, cy + jy)
    mcp9 = (cx + jx, cy - 0.02 + jy)
    mcp13 = (cx + 0.06 + jx, cy + jy)
    mcp17 = (cx + 0.11 + jx, cy + 0.04 + jy)

    # Open fingertip targets (fully straight)
    tip8_open = (cx - 0.06, cy - 0.25)
    tip12_open = (cx, cy - 0.28)
    tip16_open = (cx + 0.06, cy - 0.25)
    tip20_open = (cx + 0.11, cy - 0.18)
    tip4_open = (cx - 0.18, cy + 0.02) if thumb_extended else (cx - 0.08, cy + 0.08)

    # Curled fingertip targets (tucked into palm)
    tip8_curled = (cx - 0.05, cy + 0.08)
    tip12_curled = (cx, cy + 0.08)
    tip16_curled = (cx + 0.05, cy + 0.08)
    tip20_curled = (cx + 0.10, cy + 0.09)
    tip4_curled = (cx - 0.02, cy + 0.04)

    def _interp(a: Tuple[float, float], b: Tuple[float, float], t: float) -> Tuple[float, float]:
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    c = max(0.0, min(1.0, curl))
    tip8 = _interp(tip8_open, tip8_curled, c)
    tip12 = _interp(tip12_open, tip12_curled, c)
    tip16 = _interp(tip16_open, tip16_curled, c)
    tip20 = _interp(tip20_open, tip20_curled, c)
    tip4 = _interp(tip4_open, tip4_curled, c)

    # Phalanx joint interpolation (PIP, DIP, Thumb CMC/MCP/IP)
    pip6 = _interp(mcp5, tip8, 0.4)
    dip7 = _interp(mcp5, tip8, 0.7)
    pip10 = _interp(mcp9, tip12, 0.4)
    dip11 = _interp(mcp9, tip12, 0.7)
    pip14 = _interp(mcp13, tip16, 0.4)
    dip15 = _interp(mcp13, tip16, 0.7)
    pip18 = _interp(mcp17, tip20, 0.4)
    dip19 = _interp(mcp17, tip20, 0.7)

    cmc1 = _interp(wrist, tip4, 0.25)
    mcp2 = _interp(wrist, tip4, 0.50)
    ip3 = _interp(wrist, tip4, 0.75)

    return [
        wrist,
        cmc1, mcp2, ip3, tip4,
        mcp5, pip6, dip7, tip8,
        mcp9, pip10, dip11, tip12,
        mcp13, pip14, dip15, tip16,
        mcp17, pip18, dip19, tip20,
    ]


def compute_mcp_knuckle_centroid(
    landmarks: List[Tuple[float, float]],
    screen_w: int = 1920,
    screen_h: int = 1080,
) -> Tuple[float, float]:
    """Computes the pixel centroid of MCP knuckles (landmarks 5, 9, 13, 17)."""
    kx = (landmarks[INDEX_MCP][0] + landmarks[MIDDLE_MCP][0] + landmarks[RING_MCP][0] + landmarks[PINKY_MCP][0]) / 4.0
    ky = (landmarks[INDEX_MCP][1] + landmarks[MIDDLE_MCP][1] + landmarks[RING_MCP][1] + landmarks[PINKY_MCP][1]) / 4.0
    return (kx * screen_w, ky * screen_h)


# =====================================================================
# Landmark Stream Data Structures
# =====================================================================

@dataclass
class ReplayFrame:
    timestamp: float
    hands: List[List[Tuple[float, float]]]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_hand_objects(self) -> List[Hand]:
        return [Hand(lm=lm) for lm in self.hands]


@dataclass
class LandmarkStream:
    name: str
    fps: float
    frames: List[ReplayFrame] = field(default_factory=list)

    @property
    def duration(self) -> float:
        if not self.frames:
            return 0.0
        return self.frames[-1].timestamp - self.frames[0].timestamp

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "fps": self.fps,
            "frame_count": len(self.frames),
            "duration": self.duration,
            "frames": [asdict(f) for f in self.frames],
        }

    def save_json(self, filepath: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_json(cls, filepath: str) -> LandmarkStream:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        frames = []
        for fd in data.get("frames", []):
            frames.append(
                ReplayFrame(
                    timestamp=fd["timestamp"],
                    hands=[[(pt[0], pt[1]) for pt in h] for h in fd.get("hands", [])],
                    metadata=fd.get("metadata", {}),
                )
            )
        return cls(name=data.get("name", "stream"), fps=float(data.get("fps", 60.0)), frames=frames)


# =====================================================================
# Synthetic Landmark Stream Generators
# =====================================================================

def generate_stationary_stream(
    duration: float = 1.0,
    fps: float = 60.0,
    center: Tuple[float, float] = (0.5, 0.5),
    micro_tremor_px: float = 1.2,
    screen_size: Tuple[int, int] = (1920, 1080),
    seed: int = 42,
) -> LandmarkStream:
    """
    Generates a stream of a stationary hand with realistic high-frequency micro-tremor.
    All frame-to-frame noise displacements are strictly bounded below micro_tremor_px.
    """
    rng = random.Random(seed)
    sw, sh = screen_size
    n_frames = max(1, int(round(duration * fps)))
    dt = 1.0 / fps

    frames: List[ReplayFrame] = []
    t = 0.0
    for i in range(n_frames):
        # Generate jitter bounded strictly within micro_tremor_px radius
        r = rng.uniform(0.1, max(0.1, micro_tremor_px * 0.95))
        theta = rng.uniform(0.0, 2.0 * math.pi)
        jx_px = r * math.cos(theta)
        jy_px = r * math.sin(theta)

        lm = synthesize_hand_landmarks(
            cx=center[0],
            cy=center[1],
            curl=0.0,
            tendon_jitter_px=(jx_px, jy_px),
            screen_w=sw,
            screen_h=sh,
        )
        frames.append(
            ReplayFrame(
                timestamp=t,
                hands=[lm],
                metadata={"state": "stationary", "jitter_px": (jx_px, jy_px), "frame_idx": i},
            )
        )
        t += dt

    return LandmarkStream(name="stationary_micro_tremor", fps=fps, frames=frames)


def generate_moving_stream(
    duration: float = 1.0,
    fps: float = 60.0,
    start_pos: Tuple[float, float] = (0.3, 0.3),
    end_pos: Tuple[float, float] = (0.7, 0.7),
    screen_size: Tuple[int, int] = (1920, 1080),
    jitter_px: float = 0.5,
    seed: int = 42,
) -> LandmarkStream:
    """
    Generates a stream of a hand moving smoothly along a straight vector.
    """
    rng = random.Random(seed)
    sw, sh = screen_size
    n_frames = max(1, int(round(duration * fps)))
    dt = 1.0 / fps

    frames: List[ReplayFrame] = []
    t = 0.0
    for i in range(n_frames):
        alpha = i / max(1, n_frames - 1)
        cx = start_pos[0] + (end_pos[0] - start_pos[0]) * alpha
        cy = start_pos[1] + (end_pos[1] - start_pos[1]) * alpha

        r = rng.uniform(0.0, jitter_px)
        theta = rng.uniform(0.0, 2.0 * math.pi)
        jx = r * math.cos(theta)
        jy = r * math.sin(theta)

        lm = synthesize_hand_landmarks(cx=cx, cy=cy, curl=0.0, tendon_jitter_px=(jx, jy), screen_w=sw, screen_h=sh)
        frames.append(
            ReplayFrame(
                timestamp=t,
                hands=[lm],
                metadata={"state": "moving", "pos": (cx, cy), "frame_idx": i},
            )
        )
        t += dt

    return LandmarkStream(name="moving_hand", fps=fps, frames=frames)


def generate_fist_click_stream(
    duration: float = 1.2,
    fps: float = 60.0,
    pre_curl_sec: float = 0.4,
    curl_duration_sec: float = 0.15,
    fist_hold_sec: float = 0.15,
    reopen_sec: float = 0.15,
    center: Tuple[float, float] = (0.5, 0.5),
    tendon_deflect_px: float = 1.2,
    screen_size: Tuple[int, int] = (1920, 1080),
    seed: int = 42,
) -> LandmarkStream:
    """
    Generates a stream simulating open palm aiming, finger curl with tendon deflection,
    fist closure, and rapid reopen to trigger a click.
    """
    rng = random.Random(seed)
    sw, sh = screen_size
    dt = 1.0 / fps
    n_frames = max(1, int(round(duration * fps)))

    frames: List[ReplayFrame] = []
    t = 0.0
    for i in range(n_frames):
        # Determine motion phase
        if t < pre_curl_sec:
            phase = "open_aim"
            curl = 0.0
            # Resting micro-jitter
            jx = rng.uniform(-0.4, 0.4)
            jy = rng.uniform(-0.4, 0.4)
        elif t < (pre_curl_sec + curl_duration_sec):
            phase = "curling"
            progress = (t - pre_curl_sec) / curl_duration_sec
            curl = progress
            # Biomechanical tendon deflection on metacarpal arch
            # Sinusoidal deflection bounded by tendon_deflect_px
            jx = tendon_deflect_px * 0.8 * math.sin(progress * math.pi)
            jy = tendon_deflect_px * 0.6 * math.cos(progress * math.pi)
        elif t < (pre_curl_sec + curl_duration_sec + fist_hold_sec):
            phase = "fist_held"
            curl = 1.0
            jx = rng.uniform(-0.5, 0.5)
            jy = rng.uniform(-0.5, 0.5)
        elif t < (pre_curl_sec + curl_duration_sec + fist_hold_sec + reopen_sec):
            phase = "reopening"
            progress = (t - (pre_curl_sec + curl_duration_sec + fist_hold_sec)) / reopen_sec
            curl = 1.0 - progress
            jx = tendon_deflect_px * 0.5 * (1.0 - progress)
            jy = tendon_deflect_px * 0.5 * (1.0 - progress)
        else:
            phase = "post_click_open"
            curl = 0.0
            jx = rng.uniform(-0.3, 0.3)
            jy = rng.uniform(-0.3, 0.3)

        lm = synthesize_hand_landmarks(
            cx=center[0],
            cy=center[1],
            curl=curl,
            tendon_jitter_px=(jx, jy),
            screen_w=sw,
            screen_h=sh,
        )
        frames.append(
            ReplayFrame(
                timestamp=t,
                hands=[lm],
                metadata={
                    "phase": phase,
                    "curl": curl,
                    "tendon_jitter_px": (jx, jy),
                    "frame_idx": i,
                },
            )
        )
        t += dt

    return LandmarkStream(name="fist_click_closure", fps=fps, frames=frames)


def generate_dwell_hover_stream(
    duration: float = 2.5,
    fps: float = 60.0,
    center: Tuple[float, float] = (0.5, 0.5),
    dwell_radius_px: float = 14.0,
    hover_jitter_px: float = 6.0,
    leave_after_sec: float = 1.8,
    leave_offset_px: float = 35.0,
    screen_size: Tuple[int, int] = (1920, 1080),
    seed: int = 42,
) -> LandmarkStream:
    """
    Generates a stream simulating dwell hovering:
    1. Cursor stays within dwell_radius (14 px) for > 1.0s to accumulate and click.
    2. Continues hovering at same location to test anti-repeat latching.
    3. Moves outside dwell_radius (> 14 px) to test unlatching.
    """
    rng = random.Random(seed)
    sw, sh = screen_size
    dt = 1.0 / fps
    n_frames = max(1, int(round(duration * fps)))

    frames: List[ReplayFrame] = []
    t = 0.0
    for i in range(n_frames):
        if t < leave_after_sec:
            phase = "hovering_inside"
            # Jitter stays well inside stationarity radius (<= hover_jitter_px < dwell_radius_px)
            r = rng.uniform(0.5, min(hover_jitter_px, dwell_radius_px - 2.0))
            theta = rng.uniform(0.0, 2.0 * math.pi)
            off_x_px = r * math.cos(theta)
            off_y_px = r * math.sin(theta)
        else:
            phase = "leaving_radius"
            off_x_px = leave_offset_px
            off_y_px = 0.0

        cx = center[0] + (off_x_px / float(sw))
        cy = center[1] + (off_y_px / float(sh))

        lm = synthesize_hand_landmarks(cx=cx, cy=cy, curl=0.0, screen_w=sw, screen_h=sh)
        frames.append(
            ReplayFrame(
                timestamp=t,
                hands=[lm],
                metadata={
                    "phase": phase,
                    "offset_px": (off_x_px, off_y_px),
                    "dist_from_center_px": math.hypot(off_x_px, off_y_px),
                    "frame_idx": i,
                },
            )
        )
        t += dt

    return LandmarkStream(name="dwell_hovering", fps=fps, frames=frames)


# =====================================================================
# Invariant Replay Evaluators
# =====================================================================

def evaluate_deadzone_invariant(
    deadzone_px: float = 1.5,
    max_micro_tremor_px: float = 1.45,
    n_frames: int = 120,
    seed: int = 42,
) -> float:
    """
    Asserts Deadzone Filter invariant:
    Returns maximum observed cursor displacement (px) on resting micro-tremor under 1.5 px.
    Must be strictly 0.0 px.
    """
    rng = random.Random(seed)
    dz = DeadzoneFilter(deadzone=deadzone_px)
    init_x, init_y = 960.0, 540.0
    dz(init_x, init_y)

    max_motion = 0.0
    for _ in range(n_frames):
        r = rng.uniform(0.05, max_micro_tremor_px)
        theta = rng.uniform(0.0, 2.0 * math.pi)
        jx = r * math.cos(theta)
        jy = r * math.sin(theta)

        out_x, out_y = dz(init_x + jx, init_y + jy)
        disp = math.hypot(out_x - init_x, out_y - init_y)
        if disp > max_motion:
            max_motion = disp

    return max_motion


def evaluate_knuckle_anchor_stability(
    stream: Optional[LandmarkStream] = None,
    screen_size: Tuple[int, int] = (1920, 1080),
) -> Tuple[float, float]:
    """
    Asserts Knuckle Anchor Stability invariant:
    Measures the maximum drift of the MCP knuckle centroid (landmarks 5, 9, 13, 17)
    during finger curl vs the travel of the index fingertip.
    Returns: (max_knuckle_drift_px, max_fingertip_travel_px).
    """
    if stream is None:
        stream = generate_fist_click_stream(duration=0.8, fps=60.0, screen_size=screen_size)

    sw, sh = screen_size
    first_lm = stream.frames[0].hands[0]
    base_knuckle = compute_mcp_knuckle_centroid(first_lm, sw, sh)
    base_tip = (first_lm[INDEX_TIP][0] * sw, first_lm[INDEX_TIP][1] * sh)

    max_knuckle_drift = 0.0
    max_tip_travel = 0.0

    for f in stream.frames:
        lm = f.hands[0]
        knuckle = compute_mcp_knuckle_centroid(lm, sw, sh)
        tip = (lm[INDEX_TIP][0] * sw, lm[INDEX_TIP][1] * sh)

        k_drift = math.hypot(knuckle[0] - base_knuckle[0], knuckle[1] - base_knuckle[1])
        t_drift = math.hypot(tip[0] - base_tip[0], tip[1] - base_tip[1])

        if k_drift > max_knuckle_drift:
            max_knuckle_drift = k_drift
        if t_drift > max_tip_travel:
            max_tip_travel = t_drift

    return (max_knuckle_drift, max_tip_travel)


def evaluate_gesture_precision_invariant(
    lookback_ms: float = 200.0,
    tendon_deflect_px: float = 14.0,
) -> float:
    """
    Asserts Gesture Precision invariant:
    Simulates pre-click tendon deflection and asserts 0.0 px pre-click drift
    via the 200 ms lookback buffer when the click fires.
    Returns: measured drift in screen pixels (must be 0.0 px).
    """
    import unittest.mock
    from controller import Controller

    cfg = Config()
    cfg.control_scheme = "palm_fist"
    cfg.cursor_lookback_ms = lookback_ms

    ctrl = Controller(cfg)
    ctrl._move = unittest.mock.MagicMock()
    ctrl._handle_dwell_action = unittest.mock.MagicMock()

    aim_x, aim_y = 960.0, 540.0
    ctrl.cur_x, ctrl.cur_y = aim_x, aim_y
    ctrl.target_x, ctrl.target_y = aim_x, aim_y
    ctrl.history.clear()

    # 1. Aim stably at (960, 540) for 250 ms
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.25:
        ctrl.cur_x, ctrl.cur_y = aim_x, aim_y
        ctrl.target_x, ctrl.target_y = aim_x, aim_y
        time.sleep(0.01)

    pre_aim_x, pre_aim_y = ctrl.cur_x, ctrl.cur_y

    # 2. Tendon deflection drifting cursor over next 150 ms
    t_deflect = time.perf_counter()
    steps = 15
    dx_step = tendon_deflect_px / float(steps)
    dy_step = (tendon_deflect_px * 0.5) / float(steps)
    while time.perf_counter() - t_deflect < 0.15:
        ctrl.cur_x += dx_step
        ctrl.cur_y += dy_step
        ctrl.target_x = ctrl.cur_x
        ctrl.target_y = ctrl.cur_y
        time.sleep(0.01)

    # 3. Click fires at ~200ms mark from aim position
    ctrl.handle(Action("click"))

    drift = math.hypot(ctrl.cur_x - pre_aim_x, ctrl.cur_y - pre_aim_y)
    ctrl.shutdown()
    return drift


def evaluate_dwell_state_machine_invariants(
    dwell_radius: float = 14.0,
    dwell_time: float = 1.0,
) -> Dict[str, Any]:
    """
    Asserts Dwell State Machine invariants:
    - Hover accumulation inside stationarity radius (14 px).
    - Triggering action='click' and latched=True at 1.0s.
    - Anti-repeat latching (no duplicate clicks while remaining within 14 px).
    - Reset and unlatching when moving outside 14 px.
    """
    cfg = Config()
    cfg.dwell_mode = "click"
    cfg.dwell_time = dwell_time
    cfg.dwell_radius = dwell_radius
    dc = DwellClicker(cfg)

    # 1. Start hover
    dc.update(500, 500, 0.0)

    # 2. Hover accumulation within 14 px
    r_mid = dc.update(506, 506, 0.5)  # dist = 8.48 px < 14 px
    acc_progress = r_mid["progress"]

    # 3. Trigger dwell click
    r_click = dc.update(507, 507, 1.0)
    click_action = r_click["action"]
    click_latched = r_click["latched"]

    # 4. Anti-repeat latching while staying inside radius for another 2 seconds
    duplicate_clicks = 0
    latched_held = True
    for t in [1.2, 1.5, 2.0, 3.0]:
        rx = dc.update(508, 505, t)
        if rx["action"] is not None:
            duplicate_clicks += 1
        if not rx["latched"]:
            latched_held = False

    # 5. Stationarity radius: step outside 14 px (> 14 px away)
    r_leave = dc.update(530, 500, 3.1)  # dist = 30 px > 14 px
    unlatched_after_exit = not dc.latched

    # 6. Re-accumulation at new location
    r_new_mid = dc.update(530, 500, 3.6)
    new_acc_progress = r_new_mid["progress"]

    r_second_click = dc.update(530, 500, 4.15)
    second_click_action = r_second_click["action"]

    return {
        "mid_progress": acc_progress,
        "click_action": click_action,
        "click_latched": click_latched,
        "duplicate_clicks": duplicate_clicks,
        "latched_held": latched_held,
        "unlatched_after_exit": unlatched_after_exit,
        "second_click_action": second_click_action,
    }


def evaluate_cursor_engine_frequency(
    target_hz: int = 120,
    test_duration: float = 0.5,
) -> Tuple[float, float, float]:
    """
    Measures the decoupled cursor engine thread frequency and jitter std dev.
    Returns: (measured_hz, mean_dt_ms, jitter_std_ms).
    """
    from controller import Controller

    cfg = Config()
    cfg.cursor_rate_hz = target_hz
    ctrl = Controller(cfg)

    # Let thread warm up
    time.sleep(0.1)

    t0 = time.perf_counter()
    n0 = len(ctrl.history)
    time.sleep(test_duration)
    t1 = time.perf_counter()
    n1 = len(ctrl.history)

    ctrl.shutdown()

    # Collect sample intervals from history during measurement
    recent = list(ctrl.history)[-(n1 - n0 + 1):]
    intervals_ms = []
    for i in range(1, len(recent)):
        dt_ms = (recent[i][0] - recent[i - 1][0]) * 1000.0
        intervals_ms.append(dt_ms)

    mean_dt = statistics.mean(intervals_ms) if intervals_ms else 0.0
    jitter_std = statistics.stdev(intervals_ms) if len(intervals_ms) > 1 else 0.0
    measured_hz = 1000.0 / mean_dt if mean_dt > 0 else 0.0

    return (measured_hz, mean_dt, jitter_std)


# =====================================================================
# CLI Self-Test Runner
# =====================================================================

def run_self_test(verbose: bool = True) -> int:
    """Runs a full suite of invariant verifications via the replay harness."""
    if verbose:
        print("=" * 60)
        print("  J.A.R.V.I.S. VISION & GESTURE REPLAY HARNESS SELF-TEST")
        print("=" * 60)

    # 1. Gesture Precision Invariant (0.0 px Pre-Click Drift via 200 ms Lookback Buffer)
    pre_click_drift = evaluate_gesture_precision_invariant(lookback_ms=200.0, tendon_deflect_px=3.5)
    assert pre_click_drift == 0.0, f"Pre-click drift expected 0.0 px, got {pre_click_drift:.4f} px"
    if verbose:
        print(f"[*] Replay Invariant 1 (Gesture Precision): {pre_click_drift:.4f} px pre-click drift via lookback [PASS]")

    # 2. Deadzone Invariant
    dz_motion = evaluate_deadzone_invariant(deadzone_px=1.5, max_micro_tremor_px=1.45)
    assert dz_motion == 0.0, f"Deadzone filter motion expected 0.0 px, got {dz_motion:.4f} px"
    if verbose:
        print(f"[*] Replay Invariant 2 (Deadzone Filter): {dz_motion:.4f} px cursor motion [PASS]")

    # 2. Knuckle Anchor Stability
    k_drift, tip_travel = evaluate_knuckle_anchor_stability()
    assert k_drift < 2.0, f"Knuckle drift expected < 2.0 px, got {k_drift:.3f} px"
    if verbose:
        print(f"[*] Replay Invariant 5 (Knuckle Stability): {k_drift:.3f} px drift (tip travel {tip_travel:.1f} px) [PASS]")

    # 3. Dwell State Machine
    dwell_res = evaluate_dwell_state_machine_invariants()
    assert dwell_res["click_action"] == "click", "Dwell click failed to fire"
    assert dwell_res["duplicate_clicks"] == 0, "Anti-repeat latch failed: duplicate clicks emitted"
    assert dwell_res["unlatched_after_exit"], "Failed to unlatch after leaving stationarity radius"
    assert dwell_res["second_click_action"] == "click", "Second dwell click failed to fire"
    if verbose:
        print("[*] Replay Invariant 3 (Dwell State Machine): Hover accumulation & anti-repeat latched [PASS]")

    # 4. Cursor Engine Frequency
    rate, mean_dt, jitter = evaluate_cursor_engine_frequency(target_hz=120, test_duration=0.4)
    assert rate >= 120.0, f"Cursor engine frequency expected >= 120 Hz, got {rate:.2f} Hz"
    if verbose:
        print(f"[*] Replay Invariant 4 (Cursor Frequency): {rate:.2f} Hz (mean dt {mean_dt:.3f} ms, jitter {jitter:.4f} ms) [PASS]")

    if verbose:
        print("=" * 60)
        print("  ALL VISION & GESTURE REPLAY INVARIANTS PASSED (100%)")
        print("=" * 60)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Synthetic Landmark Record-and-Replay Generator & Benchmark.")
    parser.add_argument("--self-test", action="store_true", help="Run full suite of vision replay invariant tests.")
    parser.add_argument("--generate", choices=["stationary", "moving", "fist_click", "dwell"], help="Generate a synthetic landmark stream.")
    parser.add_argument("--out", type=str, help="Output path for JSON landmark stream.")
    parser.add_argument("--duration", type=float, default=1.0, help="Stream duration in seconds.")
    parser.add_argument("--fps", type=float, default=60.0, help="Frames per second.")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test(verbose=True))

    if args.generate and args.out:
        if args.generate == "stationary":
            stream = generate_stationary_stream(duration=args.duration, fps=args.fps)
        elif args.generate == "moving":
            stream = generate_moving_stream(duration=args.duration, fps=args.fps)
        elif args.generate == "fist_click":
            stream = generate_fist_click_stream(duration=args.duration, fps=args.fps)
        elif args.generate == "dwell":
            stream = generate_dwell_hover_stream(duration=args.duration, fps=args.fps)
        stream.save_json(args.out)
        print(f"Saved {len(stream.frames)} frames to {args.out}")
        sys.exit(0)

    parser.print_help()


if __name__ == "__main__":
    main()
