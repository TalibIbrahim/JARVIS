"""Tunable settings. Override any value by creating jarvis_config.json next to main.py."""
import json
import os
from dataclasses import dataclass, field, fields


@dataclass
class Config:
    # --- camera ---
    # --- camera & tracking quality ---
    camera_index: int = 0
    frame_width: int = 1280
    frame_height: int = 720
    camera_fps: int = 30
    camera_mjpg: bool = True
    threaded_capture: bool = True
    model_complexity: int = 1           # 0 = lightweight, 1 = higher accuracy
    min_detection_confidence: float = 0.6
    min_tracking_confidence: float = 0.6
    show_preview: bool = False          # HUD window with the tracked skeleton (or run with --preview)

    # --- control scheme ---
    control_scheme: str = "palm_fist"   # "palm_fist" (new default) or "pinch"
    palm_anchor: str = "knuckles"       # "knuckles" (MCPs 5,9,13,17), "palm_center" (0 & 9), "centroid" (0,5,9,13,17), "index_mcp" (5)

    # --- palm & fist pose classification ---
    open_enter_thresh: float = 0.70     # openness score above this = OPEN PALM
    open_exit_thresh: float = 0.58      # openness drops below this = exits OPEN (enters NEUTRAL)
    fist_enter_thresh: float = 0.28     # openness below this = FIST
    fist_exit_thresh: float = 0.38      # openness above this = exits FIST
    fist_click_max_hold: float = 0.40   # fist closed & reopened within this duration = click
    fist_double_window: float = 0.65    # two quick fists inside this window = double click
    fist_drag_min_hold: float = 0.35    # fist held longer than this becomes a drag
    reopen_freeze_ms: float = 150.0     # cursor stays frozen this long after reopening
    thumbs_up_hold: float = 1.2         # hold thumbs-up this long while awake to sleep
    pose_debounce_frames: int = 2       # frames required to confirm pose transition

    # --- tracking box & anchor ---
    # Fraction of the camera frame ignored on every edge.
    box_margin: float = 0.18
    anchor_point: str = "blend"         # used when in pinch scheme: "blend", "tip", "pip", "mcp", "palm_offset"
    anchor_blend_weight: float = 0.65   # blend ratio: 65% tip + 35% PIP

    # --- pointer mapping & precision ---
    tracking_mode: str = "relative"     # "relative" (trackpad-style) or "absolute"
    pointer_speed: float = 1.15         # pointer speed multiplier
    accel_gain: float = 1.8             # pointer acceleration curve gain
    accel_threshold: float = 0.035      # hand velocity threshold for acceleration
    precision_mode: bool = True         # magnetic slow-down during fine aiming
    precision_gain: float = 0.35        # gain multiplier during fine aim
    precision_threshold: float = 0.015  # velocity threshold for precision mode
    deadzone_px: float = 1.5            # suppresses resting micro-jitter below this threshold (pixels)
    clutch_enabled: bool = True         # pause pose / leaving margin box stops cursor

    # --- gesture timing / thresholds (distances are relative to hand size) ---
    activate_hold: float = 0.8          # seconds of open palm to wake up
    deactivate_hold: float = 1.5        # seconds of open palm to go to standby
    palm_grace: float = 0.15            # grace period to absorb single-frame landmark flicker during palm hold
    pinch_down: float = 0.28            # thumb-index distance that counts as a pinch
    pinch_up: float = 0.42              # distance at which the pinch is released (hysteresis)
    pinch_freeze: float = 0.55          # cursor freezes while fingers close, so clicks don't drift
    drag_hold: float = 0.35             # pinch held longer than this becomes a drag
    double_click_window: float = 0.45   # second quick pinch inside this window = double click
    zoom_step: float = 0.035            # change in distance between index tips per zoom notch
    zoom_enter_delay: float = 0.25      # both hands must be visible this long before zoom starts
    scroll_step: float = 0.025          # vertical hand travel per scroll notch (peace sign)
    scroll_invert: bool = False
    lost_grace: float = 0.4             # tolerate this long without a hand before releasing everything

    # --- cursor smoothing & decoupling ---
    smooth_min_cutoff: float = 0.6      # lower = smoother when slow, higher = more responsive
    smooth_beta: float = 0.02           # higher = less lag during fast moves
    smooth_d_cutoff: float = 1.0
    cursor_rate_hz: int = 120           # decoupled cursor update rate (120+ Hz)
    cursor_lookback_ms: float = 200.0   # position lookback buffer to eliminate click drift

    # --- dwell click ---
    dwell_mode: str = "click"           # "off", "click", "radial"
    dwell_time: float = 1.0             # seconds of stationarity required to trigger
    dwell_radius: float = 14.0          # radius in screen pixels allowed during dwell
    dwell_repeat_delay: float = 0.8     # must leave radius before next dwell

    # --- voice ---
    voice_enabled: bool = True
    speak: bool = True                  # spoken replies
    wake_word: str = "jarvis"
    wake_aliases: list = field(default_factory=lambda: ["jarvis", "jarvas", "jervis", "gervais"])
    command_window: float = 6.0         # seconds Jarvis keeps listening after "Jarvis?"

    # --- ui / apple hig appearance ---
    theme_mode: str = "auto"            # "auto" (follows Windows), "dark", "light"
    glass_blur: bool = True             # Windows 11 Acrylic / Windows 10 blur behind
    reduce_motion: bool = False         # reduce UI animations
    show_skeleton_mini: bool = True     # mini skeleton preview inside dynamic island

    # --- updates ---
    auto_update_check: bool = True      # automatically check for GitHub releases on startup


def load_config(path=None):
    cfg = Config()
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            known = {f.name for f in fields(Config)}
            for key, value in data.items():
                if key in known:
                    setattr(cfg, key, value)
        except Exception as exc:  # bad JSON should never stop the app from starting
            print(f"[config] ignoring {path}: {exc}")
    return cfg
