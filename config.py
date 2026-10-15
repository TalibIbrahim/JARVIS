"""Tunable settings. Override any value by creating jarvis_config.json next to main.py."""
import json
import os
from dataclasses import dataclass, field, fields


@dataclass
class Config:
    # --- camera ---
    camera_index: int = 0
    frame_width: int = 640
    frame_height: int = 480
    show_preview: bool = False          # HUD window with the tracked skeleton (or run with --preview)

    # --- tracking box ---
    # Fraction of the camera frame ignored on every edge. Your hand only has to
    # cover the middle (1 - 2*margin) of the frame to reach the whole screen.
    box_margin: float = 0.18

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

    # --- cursor smoothing (One Euro filter, pixel space) ---
    smooth_min_cutoff: float = 1.0      # lower = smoother when slow, higher = more responsive
    smooth_beta: float = 0.03           # higher = less lag during fast moves

    # --- voice ---
    voice_enabled: bool = True
    speak: bool = True                  # spoken replies
    wake_word: str = "jarvis"
    wake_aliases: list = field(default_factory=lambda: ["jarvis", "jarvas", "jervis", "gervais"])
    command_window: float = 6.0         # seconds Jarvis keeps listening after "Jarvis?"


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
