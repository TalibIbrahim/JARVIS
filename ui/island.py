"""
ui/island.py — J.A.R.V.I.S. Dynamic Island (Apple HIG, Apple-grade Assistant Animation States,
Compact Widgets, Expanded Cards, Focus-Safe Interactivity & Fullscreen Suppression)

Features:
- Compact Widgets:
    * Time + Weather: leading clock (12h/24h), trailing weather SVG glyph + temp, concentric corners.
    * Media: leading 20x20 rounded album art thumbnail, trailing 4 audio-reactive animated bars.
    * Timer: leading circular progress ring, trailing remaining time.
    * Standby / Off: minimal tiny capsule.
- Expanded Cards:
    * Media Card: 56 px artwork thumbnail with soft shadow & dominant color tint,
      marquee title/artist, scrubber bar (tabular numerals), prev/play-pause/next buttons, source footnote.
    * Weather Card: current temp, condition, high/low, feels-like, 5-hour hourly row, clean vector SVG icons.
    * Time Card: large clock display, full date, next timer / alarm.
- Focus-Safe Interactivity (WS_EX_NOACTIVATE):
    * Idle / compact: fully click-through (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE).
    * Expanded: dynamically interactive for bounding rect (removes WS_EX_TRANSPARENT, preserves WS_EX_NOACTIVATE).
    * Media buttons respond to standard mouse clicks, hand-tracking dwell ring (1.0s), and fist-click gesture.
    * Auto-collapse after 4 seconds of inactivity.
- Fullscreen Suppression:
    * Smoothly auto-hides when fullscreen game/app is active, smoothly restores on desktop return.
- Assistant States:
    * WAKE, LISTENING (Siri orb + 7-bar waveform + live transcript), THINKING (accelerated orb + shimmer),
      RESULT (13 pt card + icon), NO_SPEECH (nope wiggle), ERROR, TRACKING, GESTURE, PROMPT, SPEAKING.
- Performance: 60 FPS locked, DPI-safe, multi-monitor aware, cached vector pixmaps, spring physics only.
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from enum import auto, Enum
from typing import List, Optional, Tuple, Dict

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QPainterPath,
    QLinearGradient, QRadialGradient, QPixmap, QCursor, QMouseEvent, QPolygonF,
    QFontMetrics
)
from PySide6.QtWidgets import QWidget, QApplication

from ui.tokens import (
    T, TYPE, COLOR, qfont, REDUCE_MOTION_MS,
    SPRING_DT_MIN, SPRING_DT_MAX
)
from ui.materials import apply_acrylic, make_click_through
from ui.components import Spring, create_squircle_path


# ──────────────────────────────────────────────────────────────────────────────
# State enum
# ──────────────────────────────────────────────────────────────────────────────

class IslandState(Enum):
    IDLE          = auto()
    WAKE          = auto()
    TRACKING      = auto()
    GESTURE       = auto()
    LISTENING     = auto()
    THINKING      = auto()
    PROMPT        = auto()
    SPEAKING      = auto()
    RESULT        = auto()
    NO_SPEECH     = auto()
    ERROR         = auto()
    # Expanded Cards
    CARD_MEDIA        = auto()
    CARD_WEATHER      = auto()
    CARD_TIME         = auto()
    CARD_DISAMBIGUATE = auto()
    CARD_TIMER        = auto()
    CARD_SENDING      = auto()
    CARD_RESULT       = auto()
    # Gesture HUD States
    HUD_VOLUME        = auto()
    HUD_WINDOW        = auto()


# ──────────────────────────────────────────────────────────────────────────────
# Target sizes per state (w, h)
# ──────────────────────────────────────────────────────────────────────────────

_SIZE: dict[IslandState, tuple[float, float]] = {
    IslandState.IDLE:              (110.0,  30.0),
    IslandState.WAKE:              (280.0,  44.0),
    IslandState.TRACKING:          (210.0,  36.0),
    IslandState.GESTURE:           (270.0,  40.0),
    IslandState.LISTENING:         (390.0,  54.0),
    IslandState.THINKING:          (320.0,  46.0),
    IslandState.PROMPT:            (360.0,  48.0),   # dynamically widened if needed
    IslandState.SPEAKING:          (380.0,  50.0),   # dynamically widened if needed
    IslandState.RESULT:            (380.0,  58.0),   # dynamically widened if needed
    IslandState.NO_SPEECH:         (110.0,  30.0),
    IslandState.ERROR:             (340.0,  48.0),
    IslandState.CARD_MEDIA:        (400.0, 168.0),
    IslandState.CARD_WEATHER:      (400.0, 180.0),
    IslandState.CARD_TIME:         (370.0, 142.0),
    IslandState.CARD_DISAMBIGUATE: (440.0,  90.0),
    IslandState.CARD_TIMER:        (380.0, 150.0),
    IslandState.CARD_SENDING:      (300.0,  44.0),
    IslandState.CARD_RESULT:       (380.0,  58.0),
    IslandState.HUD_VOLUME:        (260.0,  38.0),
    IslandState.HUD_WINDOW:        (290.0,  38.0),
}


# Apple-grade spring constants:
# response ~0.4s: omega = 2*pi/0.4 = 15.71 -> k = omega^2 = 246.74
# damping ~0.7:   c = 2 * 0.7 * omega = 21.99
SPRING_K_RESP04: float = 246.74
SPRING_C_DAMP07: float = 22.00


# ──────────────────────────────────────────────────────────────────────────────
# Dynamic Island widget
# ──────────────────────────────────────────────────────────────────────────────

class AppleDynamicIsland(QWidget):
    """
    Floating top-center Apple HIG glass capsule.
    Paints in paintEvent using QPainter — no child widgets.
    All transitions driven by Spring physics with overshooting dynamic expansion.
    Supports compact widgets, expanded interactive cards, focus-safe WS_EX_NOACTIVATE
    interactivity, and fullscreen suppression.
    """

    def __init__(self, overlay_mgr):
        super().__init__()
        self.mgr = overlay_mgr

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint      |
            Qt.WindowType.WindowStaysOnTopHint     |
            Qt.WindowType.Tool                     |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("JARVIS_DYNAMIC_ISLAND")
        self.setMouseTracking(True)

        # Apple-grade springs (response ~0.4s, damping ~0.7)
        self.spring_w    = Spring(110.0, SPRING_K_RESP04, SPRING_C_DAMP07)
        self.spring_h    = Spring(30.0,  SPRING_K_RESP04, SPRING_C_DAMP07)
        self.spring_prog = Spring(0.0,   240.0, 24.0)
        self.spring_glow = Spring(0.0,   SPRING_K_RESP04, SPRING_C_DAMP07)
        self.spring_hud_volume = Spring(0.5, SPRING_K_RESP04, SPRING_C_DAMP07)

        # Content cross-fade spring
        self._opacity_spring = Spring(1.0, 180.0, 18.0)

        # ── State fields ───────────────────────────────────────────────
        self.active          = False
        self.label           = "STANDBY"
        self.progress        = 0.0
        self.hands           = 0
        self.landmarks: List = []

        # Wake state
        self.is_wake         = False
        self.wake_until      = 0.0
        self.wake_duration   = 0.50

        # Voice & Assistant states
        self.say_text        = ""
        self.say_until       = 0.0
        self.listening       = False
        self.listening_until = 0.0
        self.user_prompt     = ""
        self.target_prompt   = ""
        self.prompt_type_index = 0
        self.last_type_time  = 0.0
        self.is_live_speech  = False
        self.prompt_until    = 0.0
        self.error_text      = ""
        self.error_until     = 0.0
        self.precision_active= False
        self.thinking        = False
        self.thinking_until  = 0.0
        self.is_ai_error     = False
        self.is_transcribing = False

        # Result state
        self.result_text     = ""
        self.result_icon     = "success"
        self.result_until    = 0.0

        # Sending Indicator (Screen snapshot privacy awareness)
        self.sending_active: bool  = False
        self.sending_label: str    = "Analyzing Screen"
        self.sending_until: float  = 0.0

        # Quick Answer Card
        self.quick_answer_active: bool = False
        self.quick_answer_text: str    = ""
        self.quick_answer_icon: str    = "globe"
        self.quick_answer_until: float = 0.0

        # No-speech horizontal nope wiggle state
        self.no_speech_active= False
        self._nope_t0        = 0.0
        self._nope_duration  = 0.45
        self._nope_offset_x  = 0.0

        # Live partial transcript word-by-word fading
        self._transcript_words: list[tuple[str, float]] = []

        # 7 Audio-reactive waveform bar heights (eased toward RMS)
        self._wave_bars      = [0.0] * 7

        # ── Gesture Visual HUDs State ─────────────────────────────────
        self.hud_type: Optional[str] = None     # "volume" or "window"
        self.hud_until: float        = 0.0
        self.hud_volume_level: int   = 50
        self.hud_volume_muted: bool  = False
        self.hud_window_title: str   = ""

        # ── Compact Widgets & Expanded Cards State ────────────────────
        self.compact_mode_override: Optional[str] = None

        # Weather state
        self.weather_temp: float = 72.0
        self.weather_condition: str = "Partly Cloudy"
        self.weather_icon: str = "partly_cloudy"
        self.weather_high: float = 76.0
        self.weather_low: float = 58.0
        self.weather_feels_like: float = 71.0
        self.weather_city: str = "Cupertino"
        self.weather_hourly: list[dict] = [
            {"time": "Now",  "temp": 72.0, "icon": "partly_cloudy"},
            {"time": "1 PM", "temp": 74.0, "icon": "sunny"},
            {"time": "2 PM", "temp": 75.0, "icon": "sunny"},
            {"time": "3 PM", "temp": 73.0, "icon": "cloudy"},
            {"time": "4 PM", "temp": 70.0, "icon": "rain"},
            {"time": "5 PM", "temp": 68.0, "icon": "partly_cloudy"},
        ]

        # Media state
        self.media_active: bool = False
        self.media_playing: bool = False
        self.media_title: str = "Blinding Lights"
        self.media_artist: str = "The Weeknd"
        self.media_source: str = "Spotify"
        self.media_duration_sec: float = 200.0
        self.media_position_sec: float = 84.0
        self.media_progress: float = 84.0 / 200.0
        self.media_album_art: Optional[QPixmap] = None
        self.media_dominant_color: QColor = QColor(175, 82, 222)
        self._media_bars: list[float] = [0.35, 0.75, 0.50, 0.85]

        # Timer state
        self.timer_active: bool = False
        self.timer_duration_sec: float = 300.0
        self.timer_end_time: float = 0.0

        # Expanded Card & Interactivity
        self.expanded_card: Optional[str] = None  # "weather", "media", "time"
        self.expanded_until: float = 0.0
        self._card_expand_t: float = 0.0
        self.is_interactive: bool = False
        self._hover_enter_t: float = 0.0
        self._hovered_btn: Optional[str] = None
        self._btn_dwell_elapsed: float = 0.0
        self._btn_flash_until: float = 0.0
        self._prev_active_gesture: str = ""

        # Disambiguation Card State
        self.disambiguate_active: bool = False
        self.disambiguate_header: str = "Which one, sir?"
        self.disambiguate_candidates: list = []
        self.disambiguate_callback = None
        self.disambiguate_until: float = 0.0
        self._hovered_chip_idx: Optional[int] = None
        self._chip_dwell_elapsed: float = 0.0

        # Fullscreen suppression
        self.is_fullscreen_suppressed: bool = False
        self._last_fs_check: float = 0.0

        # State transition tracking
        self._prev_state     = IslandState.IDLE
        self._crossfade_start= 0.0
        self._demo_mode      = False

        # Pixmap cache for crisp 60 FPS vector icons
        self._icon_cache: dict[tuple[str, bool, float, int], QPixmap] = {}

        self.t0              = time.time()
        self.last_frame_t    = time.time()

        # Theme
        self.dark_mode, _ = self._read_theme()

        self.resize(110, 30)
        self._center_top()

    def _read_theme(self):
        dark = T.is_dark()
        cfg = getattr(self.mgr, "cfg", None)
        if cfg:
            mode = getattr(cfg, "theme_mode", "auto")
            if mode == "dark":
                dark = True
            elif mode == "light":
                dark = False
        return dark, None

    def reload_config(self) -> None:
        """Live apply settings without restart."""
        self.dark_mode, _ = self._read_theme()
        cfg = getattr(self.mgr, "cfg", None)
        if cfg:
            city = getattr(cfg, "island_weather_city", None)
            if city:
                self.weather_city = city

    def set_interactive(self, interactive: bool) -> None:
        """
        Dynamically enables / disables interactivity without focus stealing.
        When interactive is False: WS_EX_TRANSPARENT | WS_EX_NOACTIVATE (click-through).
        When interactive is True:  WS_EX_NOACTIVATE (receives clicks, active app keeps focus).
        """
        if sys.platform != "win32":
            self.is_interactive = interactive
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED     = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE  = 0x08000000
            WS_EX_TOOLWINDOW  = 0x00000080

            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if interactive:
                # Clear transparent, ensure noactivate
                new_style = (style & ~WS_EX_TRANSPARENT) | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            else:
                # Ensure transparent and noactivate
                new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW

            if new_style != style:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            self.is_interactive = interactive
        except Exception as exc:
            print(f"[island] set_interactive error: {exc}")

    def make_click_through(self) -> None:
        """Apply WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE and Acrylic."""
        self.set_interactive(False)
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            if getattr(getattr(self.mgr, "cfg", None), "glass_blur", True):
                apply_acrylic(hwnd, self.dark_mode)
        except Exception as exc:
            print(f"[island] backdrop error: {exc}")

    def _center_top(self):
        try:
            screen = QApplication.primaryScreen()
            if screen:
                geom = screen.geometry()
                w = max(40, min(int(round(self.spring_w.value)), 3840))
                h = max(20, min(int(round(self.spring_h.value)), 2160))
                # Add horizontal nope offset during NO_SPEECH
                nope_dx = int(round(self._nope_offset_x))
                cx = geom.x() + (geom.width() - w) // 2 + nope_dx
                self.setGeometry(cx, geom.y(), w, h)
        except Exception as exc:
            print(f"[island] center_top: {exc}")

    # ──────────────────────────────────────────────────────────────────────
    # State Trigger Actions (Interruptible & smooth)
    # ──────────────────────────────────────────────────────────────────────

    def trigger_wake(self, duration: float = 0.50) -> None:
        """
        Trigger WAKE state:
        Expands into a wider capsule with overshooting spring and soft edge glow pulse.
        Interruptible: re-expands mid-collapse seamlessly.
        """
        now = time.time()
        self.is_wake = True
        self.wake_duration = max(0.2, float(duration))
        self.wake_until = now + self.wake_duration
        self.no_speech_active = False
        self._nope_t0 = 0.0
        self._nope_offset_x = 0.0
        self.expanded_card = None
        self.disambiguate_active = False
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0
        self.set_interactive(False)

        # Overshoot spring impulse
        tw, th = _SIZE[IslandState.WAKE]
        scale = self._get_size_scale()
        self.spring_w.set_target(tw * scale)
        self.spring_h.set_target(th * scale)
        self.spring_w.vel = max(self.spring_w.vel, 120.0)

        # Pulse edge glow spring
        self.spring_glow.snap(1.0)
        self.spring_glow.set_target(0.0)

    def trigger_wake_pulse(self) -> None:
        """Slight overshooting pulse on wake detection without changing to wake enum."""
        tw, th = _SIZE[IslandState.LISTENING]
        scale = self._get_size_scale()
        self.spring_w.set_target(tw * scale)
        self.spring_h.set_target(th * scale)
        self.spring_w.vel = max(self.spring_w.vel, 140.0)
        self.spring_glow.snap(0.9)
        self.spring_glow.set_target(0.0)

    def trigger_result(self, text: str, icon: str = "success", duration: float = 2.5) -> None:
        """
        Trigger RESULT state:
        Morphs to a taller card with 1 line of 13 pt text and an icon.
        Collapses after duration seconds (longer for long text).
        """
        now = time.time()
        self.result_text = str(text).strip()
        self.result_icon = str(icon).lower()
        if len(self.result_text) > 40:
            duration = max(duration, 2.5 + (len(self.result_text) - 40) * 0.06)
        self.result_until = now + duration

        self.sending_active = False
        self.sending_until = 0.0
        self.quick_answer_active = False
        self.quick_answer_until = 0.0
        self.thinking = False
        self.thinking_until = 0.0
        self.listening = False
        self.listening_until = 0.0
        self.is_wake = False
        self.wake_until = 0.0
        self.expanded_card = None
        self.disambiguate_active = False
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0
        self.set_interactive(False)

    def set_sending_indicator(self, active: bool = True, label: str = "Analyzing Screen", timeout: float = 8.0) -> None:
        """
        Trigger CARD_SENDING state:
        Leading side: pulsating aperture / camera-lens vector icon with soft cyan/amber security glow pulse.
        Capsule: label ("Analyzing Screen", 13 pt) with subtle sweeping shimmer.
        Explicit privacy awareness: user visibly sees when a snapshot is being processed.
        """
        now = time.time()
        self.sending_active = bool(active)
        if self.sending_active:
            self.sending_label = str(label or "Analyzing Screen").strip()
            self.sending_until = now + max(1.0, float(timeout)) if timeout > 0 else 0.0
            self.quick_answer_active = False
            self.quick_answer_until = 0.0
            self.thinking = False
            self.thinking_until = 0.0
            self.listening = False
            self.listening_until = 0.0
            self.is_wake = False
            self.wake_until = 0.0
            self.expanded_card = None
            self.disambiguate_active = False
            self.disambiguate_candidates = []
            self.disambiguate_callback = None
            self._hovered_chip_idx = None
            self._chip_dwell_elapsed = 0.0
            self.set_interactive(False)

            # Apple spring impulse with soft security glow pulse
            tw, th = self._target_size(IslandState.CARD_SENDING, now)
            self.spring_w.set_target(tw)
            self.spring_h.set_target(th)
            self.spring_w.vel = max(self.spring_w.vel, 80.0)
            self.spring_glow.snap(0.85)
            self.spring_glow.set_target(0.0)
        else:
            self.sending_until = 0.0

    def trigger_quick_answer(self, text: str, icon: str = "globe", duration: float = 4.0) -> None:
        """
        Trigger Quick Answer Card (CARD_RESULT):
        When a quick answer / timezone result arrives, island displays CARD_RESULT with vector icon
        (globe/clock for timezone, book for facts), 13 pt clean text, and tabular numerals.
        """
        now = time.time()
        self.quick_answer_active = True
        self.quick_answer_text = str(text).strip()
        self.quick_answer_icon = str(icon).lower()
        if len(self.quick_answer_text) > 35:
            duration = max(duration, 3.5 + (len(self.quick_answer_text) - 35) * 0.05)
        self.quick_answer_until = now + duration

        # Mirror to result fields for backward compatibility
        self.result_text = self.quick_answer_text
        self.result_icon = self.quick_answer_icon
        self.result_until = self.quick_answer_until

        self.sending_active = False
        self.sending_until = 0.0
        self.thinking = False
        self.thinking_until = 0.0
        self.listening = False
        self.listening_until = 0.0
        self.is_wake = False
        self.wake_until = 0.0
        self.expanded_card = None
        self.disambiguate_active = False
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0
        self.set_interactive(False)

        # Apple spring impulse
        tw, th = self._target_size(IslandState.CARD_RESULT, now)
        self.spring_w.set_target(tw)
        self.spring_h.set_target(th)
        self.spring_w.vel = max(self.spring_w.vel, 100.0)

    def trigger_hud_volume(self, level: int = 50, muted: bool = False, duration: float = 1.5) -> None:
        """
        Display compact Volume Capsule (HUD_VOLUME state).
        Auto-collapses back after duration seconds (default 1.5s).
        """
        now = time.time()
        self.hud_type = "volume"
        self.hud_until = now + duration
        self.hud_volume_level = max(0, min(100, int(level)))
        self.hud_volume_muted = bool(muted)
        self.spring_hud_volume.set_target(self.hud_volume_level / 100.0)

        tw, th = self._target_size(IslandState.HUD_VOLUME, now)
        self.spring_w.set_target(tw)
        self.spring_h.set_target(th)
        self.spring_w.vel = max(self.spring_w.vel, 80.0)
        self.update()

    def trigger_hud_window(self, title: str = "", duration: float = 1.5) -> None:
        """
        Display Window Switch HUD (HUD_WINDOW state).
        Shows app switcher icon and truncated target window title.
        Auto-collapses back after duration seconds (default 1.5s).
        """
        now = time.time()
        self.hud_type = "window"
        self.hud_until = now + duration
        self.hud_window_title = (title or "Switch Window").strip()

        tw, th = self._target_size(IslandState.HUD_WINDOW, now)
        self.spring_w.set_target(tw)
        self.spring_h.set_target(th)
        self.spring_w.vel = max(self.spring_w.vel, 80.0)
        self.update()

    def trigger_no_speech(self) -> None:
        """
        Trigger NO_SPEECH:
        After silence, shrinks back to IDLE with horizontal 'nope' nudge, no error text.
        """
        now = time.time()
        self.listening = False
        self.listening_until = 0.0
        self.is_transcribing = False
        self.is_wake = False
        self.wake_until = 0.0
        self.user_prompt = ""
        self.target_prompt = ""
        self._transcript_words = []
        self.no_speech_active = True
        self._nope_t0 = now
        self._nope_duration = 0.45
        self.expanded_card = None
        self.disambiguate_active = False
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0
        self.hud_type = None
        self.hud_until = 0.0
        self.set_interactive(False)

        # Target IDLE size
        self.spring_w.set_target(_SIZE[IslandState.IDLE][0])
        self.spring_h.set_target(_SIZE[IslandState.IDLE][1])

    def reset_to_idle(self) -> None:
        """Return cleanly to standby IDLE pill."""
        self.is_wake = False
        self.wake_until = 0.0
        self.listening = False
        self.listening_until = 0.0
        self.thinking = False
        self.thinking_until = 0.0
        self.hud_type = None
        self.hud_until = 0.0
        self.result_until = 0.0
        self.result_text = ""
        self.sending_active = False
        self.sending_until = 0.0
        self.sending_label = "Analyzing Screen"
        self.quick_answer_active = False
        self.quick_answer_until = 0.0
        self.quick_answer_text = ""
        self.error_until = 0.0
        self.say_until = 0.0
        self.prompt_until = 0.0
        self.user_prompt = ""
        self.target_prompt = ""
        self._transcript_words = []
        self.no_speech_active = False
        self._nope_t0 = 0.0
        self._nope_offset_x = 0.0
        self.active = False
        self.label = "STANDBY"
        self.expanded_card = None
        self.compact_mode_override = None
        self.disambiguate_active = False
        self.disambiguate_until = 0.0
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0
        self.set_interactive(False)

    def expand_card(self, card_name: str = "weather") -> None:
        """Expand the island into an interactive card (weather, media, or time)."""
        self.disambiguate_active = False
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0
        self.expanded_card = card_name.lower()
        now = time.time()
        self.expanded_until = now + 4.0
        self._card_expand_t = now
        self.set_interactive(True)
        self.spring_w.vel = max(self.spring_w.vel, 60.0)
        self.spring_h.vel = max(self.spring_h.vel, 60.0)

    def collapse_card(self) -> None:
        """Collapse expanded card back to compact pill."""
        self.expanded_card = None
        self.expanded_until = 0.0
        self.set_interactive(False)

    @staticmethod
    def _candidate_title(cand: any) -> str:
        """Extract user-facing label/title from candidate string or dict."""
        if isinstance(cand, dict):
            return str(cand.get("title") or cand.get("name") or cand.get("label") or cand.get("text") or "Option")
        return str(cand)

    def trigger_disambiguate(
        self,
        candidates: list,
        callback=None,
        header: str = "Which one, sir?",
        duration: float = 12.0
    ) -> None:
        """
        Trigger Disambiguation Card UI:
        Expands island into Disambiguation Card with up to 3 candidates.
        Enables interactive rect with WS_EX_NOACTIVATE without focus stealing.
        """
        now = time.time()
        self.disambiguate_candidates = list(candidates)[:3]
        if not self.disambiguate_candidates:
            return
        self.disambiguate_callback = callback
        self.disambiguate_header = header or "Which one, sir?"
        self.disambiguate_active = True
        self.disambiguate_until = now + max(4.0, float(duration))
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0

        # Reset transient states
        self.thinking = False
        self.thinking_until = 0.0
        self.listening = False
        self.listening_until = 0.0
        self.is_wake = False
        self.wake_until = 0.0
        self.result_until = 0.0
        self.result_text = ""
        self.no_speech_active = False
        self._nope_t0 = 0.0
        self._nope_offset_x = 0.0
        self.expanded_card = None

        # Enable interactive rect without focus stealing
        self.set_interactive(True)

        # Apple-grade spring impulse with slight overshoot
        tw, th = self._target_size(IslandState.CARD_DISAMBIGUATE, now)
        self.spring_w.set_target(tw)
        self.spring_h.set_target(th)
        self.spring_w.vel = max(self.spring_w.vel, 80.0)
        self.spring_h.vel = max(self.spring_h.vel, 60.0)

    def select_candidate(self, index: int) -> None:
        """
        Select a candidate chip by index:
        Invokes callback or executes candidate action, triggers brief RESULT checkmark,
        and smoothly collapses back to idle.
        """
        if not self.disambiguate_candidates or index < 0 or index >= len(self.disambiguate_candidates):
            return
        cand = self.disambiguate_candidates[index]
        title = self._candidate_title(cand)
        cb = self.disambiguate_callback

        # Reset disambiguate fields
        self.disambiguate_active = False
        self.disambiguate_until = 0.0
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0

        # Invoke selection callback or candidate action
        if callable(cb):
            try:
                cb(cand)
            except TypeError:
                try:
                    cb(index, cand)
                except Exception as exc:
                    print(f"[island] disambiguate callback error: {exc}")
            except Exception as exc:
                print(f"[island] disambiguate callback error: {exc}")
        elif isinstance(cand, dict):
            act = cand.get("action") or cand.get("callback")
            if callable(act):
                try:
                    act()
                except Exception as exc:
                    print(f"[island] candidate action error: {exc}")

        # Post event to overlay manager if present
        if hasattr(self, "mgr") and self.mgr and hasattr(self.mgr, "post"):
            self.mgr.post("disambiguate_selected", candidate=cand, index=index, title=title)

        # Show brief RESULT checkmark, then smoothly collapse back to idle
        self.trigger_result(text=title, icon="success", duration=1.6)

    def _chip_rects(self, w: float, h: float, scale: float) -> list[QRectF]:
        """Compute bounding rects for each candidate chip in the Disambiguation Card."""
        n = len(self.disambiguate_candidates)
        if n == 0:
            return []
        margin = 16.0 * scale
        gap = 10.0 * scale
        chip_h = 36.0 * scale
        cy = 38.0 * scale
        total_avail = w - (2.0 * margin)
        total_gaps = (n - 1) * gap
        chip_w = max(40.0 * scale, (total_avail - total_gaps) / n)

        rects = []
        for i in range(n):
            rx = margin + i * (chip_w + gap)
            rects.append(QRectF(rx, cy, chip_w, chip_h))
        return rects

    def _chip_at(self, pos: QPointF | QPoint) -> Optional[int]:
        """Hit test candidate chips."""
        if not self.disambiguate_candidates:
            return None
        w = max(float(self.width()), self.spring_w.target)
        h = max(float(self.height()), self.spring_h.target)
        scale = self._get_size_scale()
        rects = self._chip_rects(w, h, scale)
        for i, rect in enumerate(rects):
            if rect.contains(pos):
                return i
        return None

    def set_fullscreen(self, is_fs: bool) -> None:
        """Handles fullscreen suppression auto-hide and restore."""
        cfg = getattr(self.mgr, "cfg", None)
        if not getattr(cfg, "island_hide_fullscreen", getattr(cfg, "hide_fullscreen", False)):
            return
        if self.is_fullscreen_suppressed != is_fs:
            self.is_fullscreen_suppressed = is_fs
            if is_fs:
                state = self._resolve_state(time.time())
                if state in (IslandState.IDLE, IslandState.CARD_MEDIA, IslandState.CARD_WEATHER, IslandState.CARD_TIME, IslandState.CARD_DISAMBIGUATE, IslandState.CARD_TIMER):
                    self.spring_h.set_target(0.0)
            else:
                now = time.time()
                tw, th = self._target_size(self._resolve_state(now), now)
                self.spring_h.set_target(th)
                self.spring_h.vel = 80.0

    def set_partial_transcript(self, text: str, now: float) -> None:
        """Update live partial transcript with word-by-word arrival timestamps."""
        text = text.strip()
        if text == self.target_prompt:
            return
        self.target_prompt = text
        self.user_prompt = text
        self.prompt_type_index = len(text)
        words = text.split()

        new_words: list[tuple[str, float]] = []
        for i, w in enumerate(words):
            if i < len(self._transcript_words) and self._transcript_words[i][0] == w:
                new_words.append(self._transcript_words[i])
            else:
                new_words.append((w, now))
        self._transcript_words = new_words

    # ──────────────────────────────────────────────────────────────────────
    # State resolver
    # ──────────────────────────────────────────────────────────────────────

    def _resolve_state(self, now: float) -> IslandState:
        if now < self.error_until:
            return IslandState.ERROR
        if self.quick_answer_active and now < self.quick_answer_until and self.quick_answer_text:
            return IslandState.CARD_RESULT
        if now < self.result_until and self.result_text:
            return IslandState.RESULT
        if self.sending_active and (self.sending_until == 0.0 or now < self.sending_until):
            return IslandState.CARD_SENDING
        if self.disambiguate_active and now < self.disambiguate_until and self.disambiguate_candidates:
            return IslandState.CARD_DISAMBIGUATE
        if self.is_wake and now < self.wake_until:
            return IslandState.WAKE
        if self.hud_type == "volume" and now < self.hud_until:
            return IslandState.HUD_VOLUME
        if self.hud_type == "window" and now < self.hud_until:
            return IslandState.HUD_WINDOW
        if self.no_speech_active and (now < self._nope_t0 + self._nope_duration):
            return IslandState.NO_SPEECH
        if self.listening or now < self.listening_until:
            return IslandState.LISTENING
        if self.thinking and now < self.thinking_until:
            if self.user_prompt:
                return IslandState.PROMPT
            return IslandState.THINKING
        if now < self.prompt_until and self.user_prompt:
            if now < self.say_until:
                return IslandState.SPEAKING
            return IslandState.PROMPT
        if now < self.say_until:
            return IslandState.SPEAKING
        if self.label in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT", "SCROLL"):
            return IslandState.GESTURE
        if self.active:
            return IslandState.TRACKING
        # Expanded Cards
        if self.expanded_card and now < self.expanded_until:
            if self.expanded_card == "media":
                return IslandState.CARD_MEDIA
            elif self.expanded_card == "weather":
                return IslandState.CARD_WEATHER
            elif self.expanded_card == "time":
                return IslandState.CARD_TIME
            elif self.expanded_card == "timer":
                return IslandState.CARD_TIMER
        return IslandState.IDLE

    def _get_size_scale(self) -> float:
        cfg = getattr(self.mgr, "cfg", None)
        size_mode = getattr(cfg, "island_size", "default")
        if size_mode == "compact":
            return 0.90
        elif size_mode == "large":
            return 1.15
        return 1.0

    def _get_current_compact_mode(self) -> str:
        if self.compact_mode_override:
            return self.compact_mode_override

        cfg = getattr(self.mgr, "cfg", None)
        show_media = getattr(cfg, "island_show_now_playing", True)
        if (self.media_playing or self.media_active) and show_media:
            return "media"

        if self.timer_active:
            return "timer"

        idle_disp = getattr(cfg, "island_idle_display", "time_weather")
        show_weather = getattr(cfg, "island_show_weather", True)

        if idle_disp == "time_weather":
            return "time_weather" if show_weather else "time"
        elif idle_disp == "time":
            return "time"
        return "off"

    def _target_size(self, state: IslandState, now: float) -> tuple[float, float]:
        scale = self._get_size_scale()

        if self.is_fullscreen_suppressed and state in (
            IslandState.IDLE, IslandState.CARD_MEDIA, IslandState.CARD_WEATHER, IslandState.CARD_TIME, IslandState.CARD_DISAMBIGUATE, IslandState.CARD_TIMER
        ):
            return (60.0 * scale, 0.0)

        if state == IslandState.CARD_DISAMBIGUATE:
            n = max(1, min(3, len(self.disambiguate_candidates)))
            max_len = max([len(self._candidate_title(c)) for c in self.disambiguate_candidates] or [8])
            if n == 1:
                base_w = 260.0
            elif n == 2:
                base_w = 360.0 + max(0.0, (max_len - 10) * 8.0)
            else:
                base_w = 440.0 + max(0.0, (max_len - 10) * 8.0)
            base_w = min(560.0, base_w)
            return (base_w * scale, 90.0 * scale)
        elif state == IslandState.CARD_MEDIA:
            return (400.0 * scale, 168.0 * scale)
        elif state == IslandState.CARD_WEATHER:
            return (400.0 * scale, 180.0 * scale)
        elif state == IslandState.CARD_TIME:
            return (370.0 * scale, 142.0 * scale)
        elif state == IslandState.CARD_TIMER:
            return (380.0 * scale, 150.0 * scale)
        elif state in (IslandState.RESULT, IslandState.CARD_RESULT):
            disp = self.quick_answer_text if (state == IslandState.CARD_RESULT and self.quick_answer_text) else self.result_text
            tw = min(540.0, max(360.0, 90.0 + len(disp) * 8.2))
            return (tw * scale, 58.0 * scale)
        elif state == IslandState.CARD_SENDING:
            lbl = self.sending_label or "Analyzing Screen"
            tw = min(460.0, max(270.0, 85.0 + len(lbl) * 8.6))
            return (tw * scale, 44.0 * scale)
        elif state == IslandState.LISTENING and self.user_prompt:
            tw = min(500.0, max(390.0, 110.0 + len(self.user_prompt) * 6.5))
            return (tw * scale, 54.0 * scale)
        elif state == IslandState.SPEAKING:
            disp = max(len(self.user_prompt), len(self.say_text))
            tw = min(520.0, max(340.0, 120.0 + disp * 6.5))
            return (tw * scale, 50.0 * scale)
        elif state == IslandState.PROMPT:
            disp = max(len(self.user_prompt), 20)
            tw = min(500.0, max(300.0, 130.0 + disp * 7.0))
            return (tw * scale, 48.0 * scale)
        elif state == IslandState.IDLE:
            compact_mode = self._get_current_compact_mode()
            if compact_mode == "media":
                return (186.0 * scale, 32.0 * scale)
            elif compact_mode == "timer":
                return (152.0 * scale, 32.0 * scale)
            elif compact_mode == "time_weather":
                return (176.0 * scale, 32.0 * scale)
            elif compact_mode == "time":
                return (132.0 * scale, 32.0 * scale)
            else:
                return (110.0 * scale, 30.0 * scale)
        elif state == IslandState.HUD_VOLUME:
            return (260.0 * scale, 38.0 * scale)
        elif state == IslandState.HUD_WINDOW:
            fm = QFontMetrics(QFont(TYPE.body.family, 10, QFont.Weight.DemiBold))
            tw = fm.horizontalAdvance(self.hud_window_title or "Switch Window")
            target_w = max(240.0, min(360.0, 44.0 + tw + 28.0))
            return (target_w * scale, 38.0 * scale)

        tw, th = _SIZE[state]
        return (tw * scale, th * scale)

    # ──────────────────────────────────────────────────────────────────────
    # Animation frame update (called by Overlay._tick at 60 Hz)
    # ──────────────────────────────────────────────────────────────────────

    def update_frame(self, now: float) -> None:
        dt = max(now - self.last_frame_t, SPRING_DT_MIN)
        self.last_frame_t = now

        reduce = T.reduce_motion()

        # Handle NO_SPEECH horizontal 'nope' nudge
        if self.no_speech_active and self._nope_t0 > 0.0:
            elapsed = now - self._nope_t0
            if elapsed < self._nope_duration:
                amp = 8.0 * math.exp(-6.0 * elapsed)
                self._nope_offset_x = amp * math.sin(2.0 * math.pi * 7.5 * elapsed)
            else:
                self._nope_offset_x = 0.0
                self._nope_t0 = 0.0
                self.no_speech_active = False

        # Progressive typewriter for prompt if not in partial transcript mode
        if self.target_prompt and not self._transcript_words and self.user_prompt != self.target_prompt:
            if reduce:
                self.user_prompt = self.target_prompt
                self.prompt_type_index = len(self.target_prompt)
            else:
                if now - self.last_type_time >= 0.016:
                    step = max(1, int((now - self.last_type_time) / 0.016) * 2)
                    self.prompt_type_index = min(len(self.target_prompt), self.prompt_type_index + step)
                    self.user_prompt = self.target_prompt[:self.prompt_type_index]
                    self.last_type_time = now

        # Media playback progress simulation & visualizer bars
        if self.media_playing:
            self.media_position_sec = min(self.media_duration_sec, self.media_position_sec + dt)
            self.media_progress = self.media_position_sec / max(1.0, self.media_duration_sec)
            for i in range(4):
                target_b = 0.25 + 0.70 * abs(math.sin(now * 9.0 + i * 1.45))
                self._media_bars[i] += 0.35 * (target_b - self._media_bars[i])
        else:
            for i in range(4):
                self._media_bars[i] += 0.20 * (0.15 - self._media_bars[i])

        # Interaction & Auto-collapse checks
        self._update_interaction(now, dt)

        state = self._resolve_state(now)
        tw, th = self._target_size(state, now)

        # State transition detection
        if state != self._prev_state:
            if reduce:
                self._crossfade_start = now
            else:
                self._opacity_spring.snap(0.0)
                self._opacity_spring.set_target(1.0)
            self._prev_state = state

        self.spring_w.set_target(tw)
        self.spring_h.set_target(th)
        self.spring_prog.set_target(self.progress)

        self.spring_w.update(dt, reduce)
        self.spring_h.update(dt, reduce)
        self.spring_prog.update(dt, reduce)
        self.spring_glow.update(dt, reduce)
        self.spring_hud_volume.update(dt, reduce)
        self._opacity_spring.update(dt, reduce)

        # Ease 7 waveform bars toward live RMS level
        self._update_wave_bars(now)

        self._center_top()
        self.update()

    def _update_wave_bars(self, now: float) -> None:
        live_vol = 0.0
        try:
            from voice import Voice
            if now - getattr(Voice, "_latest_time", 0.0) < 0.8:
                raw = float(getattr(Voice, "_latest_rms", 0.0))
                if raw > 25.0:
                    vol = min(max((raw - 25.0) / 320.0, 0.0), 1.0)
                    live_vol = 1.0 - (1.0 - vol) ** 3
        except Exception:
            pass

        t = now - self.t0
        weights = [0.45, 0.65, 0.88, 1.0, 0.88, 0.65, 0.45]
        for i in range(7):
            if live_vol > 0.01:
                target = live_vol * weights[i] * (0.35 + 0.65 * abs(math.sin(t * 7.5 + i * 0.85)))
            else:
                target = 0.10 * weights[i] * abs(math.sin(t * 2.5 + i * 0.6))
            self._wave_bars[i] += 0.32 * (target - self._wave_bars[i])

    def _update_interaction(self, now: float, dt: float) -> None:
        """Handles cursor hover/dwell expansion, card button dwell, and auto-collapse."""
        cursor_pos = QCursor.pos()
        geom = self.geometry()
        is_hovering = geom.contains(cursor_pos)

        cfg = getattr(self.mgr, "cfg", None)
        expand_trig = getattr(cfg, "island_expand_trigger", "hover")

        # 0. Disambiguation Card Interaction & Auto-collapse
        if self.disambiguate_active and self.disambiguate_candidates:
            if now >= self.disambiguate_until:
                self.disambiguate_active = False
                self.disambiguate_candidates = []
                self.disambiguate_callback = None
                self._hovered_chip_idx = None
                self._chip_dwell_elapsed = 0.0
                self.set_interactive(False)
                return

            if is_hovering:
                local_pt = self.mapFromGlobal(cursor_pos)
                chip_idx = self._chip_at(local_pt)
                if chip_idx is not None:
                    if chip_idx == self._hovered_chip_idx:
                        self._chip_dwell_elapsed += dt
                        if self._chip_dwell_elapsed >= 1.0:
                            # 1.0s dwell gesture selects the chip!
                            self.select_candidate(chip_idx)
                            return
                    else:
                        self._hovered_chip_idx = chip_idx
                        self._chip_dwell_elapsed = 0.0

                    # Fist-click gesture over chip
                    if self.label == "CLICK" and self._prev_active_gesture != "CLICK":
                        self.select_candidate(chip_idx)
                        return
                else:
                    self._hovered_chip_idx = None
                    self._chip_dwell_elapsed = 0.0
            else:
                self._hovered_chip_idx = None
                self._chip_dwell_elapsed = 0.0

        # 1. Compact -> Expansion triggers
        elif self.expanded_card is None and not self.is_wake and not self.listening and not self.thinking:
            if is_hovering:
                if self._hover_enter_t == 0.0:
                    self._hover_enter_t = now
                thresh = 0.25 if expand_trig == "hover" else 1.0
                if now - self._hover_enter_t >= thresh:
                    cm = self._get_current_compact_mode()
                    card = "media" if cm == "media" else ("timer" if cm == "timer" else "weather")
                    self.expand_card(card)
                    self._hover_enter_t = 0.0
            else:
                self._hover_enter_t = 0.0

            # Hand-tracking fist-click trigger
            if is_hovering and self.label == "CLICK" and self._prev_active_gesture != "CLICK":
                cm = self._get_current_compact_mode()
                card = "media" if cm == "media" else ("timer" if cm == "timer" else "weather")
                self.expand_card(card)

        # 2. Expanded Card Interaction & Auto-collapse
        elif self.expanded_card:
            # Auto-collapse after 4 seconds of no interaction
            if now >= self.expanded_until:
                self.collapse_card()
                return

            if is_hovering:
                local_pt = self.mapFromGlobal(cursor_pos)
                btn = self._button_at(local_pt)
                if btn:
                    if btn == self._hovered_btn:
                        self._btn_dwell_elapsed += dt
                        if self._btn_dwell_elapsed >= 1.0:
                            # 1.0s dwell gesture triggers button!
                            self._trigger_button(btn)
                            self._btn_dwell_elapsed = 0.0
                            self._btn_flash_until = now + 0.25
                    else:
                        self._hovered_btn = btn
                        self._btn_dwell_elapsed = 0.0

                    # Fist-click gesture over button
                    if self.label == "CLICK" and self._prev_active_gesture != "CLICK":
                        self._trigger_button(btn)
                        self._btn_dwell_elapsed = 0.0
                        self._btn_flash_until = now + 0.25
                else:
                    self._hovered_btn = None
                    self._btn_dwell_elapsed = 0.0
            else:
                self._hovered_btn = None
                self._btn_dwell_elapsed = 0.0

        self._prev_active_gesture = self.label

    def _check_fullscreen_foreground(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from fullscreen_detector import is_fullscreen_active
            if is_fullscreen_active():
                return True
        except Exception:
            pass
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            my_hwnd = int(self.winId()) if hasattr(self, "winId") else 0
            if hwnd == my_hwnd:
                return False

            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            if cls_buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"):
                return False

            import ctypes.wintypes
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            screen = QApplication.primaryScreen()
            if screen:
                geom = screen.geometry()
                if (rect.left <= geom.x() and rect.top <= geom.y() and
                    rect.right >= geom.x() + geom.width() and
                    rect.bottom >= geom.y() + geom.height()):
                    return True
        except Exception:
            pass
        return False

    # ──────────────────────────────────────────────────────────────────────
    # Mouse & Button Events (Focus-Safe WS_EX_NOACTIVATE)
    # ──────────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        now = time.time()
        pos = event.position()

        if self.disambiguate_active and self.disambiguate_candidates:
            chip_idx = self._chip_at(pos)
            if chip_idx is not None:
                self.select_candidate(chip_idx)
                return
            else:
                self.disambiguate_until = now + 6.0
                return

        self.expanded_until = now + 4.0

        if self.expanded_card == "media":
            btn = self._button_at(pos)
            if btn:
                self._trigger_button(btn)
                self._btn_flash_until = now + 0.25
                return

            sr = self._scrubber_rect()
            if sr.contains(pos):
                ratio = min(1.0, max(0.0, (pos.x() - sr.x()) / sr.width()))
                self.media_progress = ratio
                self.media_position_sec = ratio * self.media_duration_sec
                return

        elif self.expanded_card == "timer":
            btn = self._button_at(pos)
            if btn:
                self._trigger_button(btn)
                self._btn_flash_until = now + 0.25
                return

        elif self.expanded_card is None:
            cm = self._get_current_compact_mode()
            card = "media" if cm == "media" else ("timer" if cm == "timer" else "weather")
            self.expand_card(card)

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.disambiguate_active and self.disambiguate_candidates:
            self.disambiguate_until = max(self.disambiguate_until, time.time() + 4.0)
            self._hovered_chip_idx = self._chip_at(event.position())
            super().mouseMoveEvent(event)
            return
        self.expanded_until = time.time() + 4.0
        if self.expanded_card in ("media", "timer"):
            self._hovered_btn = self._button_at(event.position())
        super().mouseMoveEvent(event)

    def _button_at(self, pos: QPointF | QPoint) -> Optional[str]:
        w = max(float(self.width()), self.spring_w.target)
        scale = self._get_size_scale()

        if self.expanded_card == "timer":
            cy_btn = 126.0 * scale
            bw = 70.0 * scale
            bh = 22.0 * scale
            cancel_cx = w / 2.0 - 44.0 * scale
            pause_cx  = w / 2.0 + 44.0 * scale
            if QRectF(cancel_cx - bw / 2.0, cy_btn - bh / 2.0, bw, bh).contains(pos):
                return "timer_cancel"
            if QRectF(pause_cx - bw / 2.0, cy_btn - bh / 2.0, bw, bh).contains(pos):
                return "timer_pause_resume"
            return None

        cy = 136.0 * scale
        prev_cx = (w / 2.0 - 52.0 * scale)
        play_cx = (w / 2.0)
        next_cx = (w / 2.0 + 52.0 * scale)

        if math.hypot(pos.x() - prev_cx, pos.y() - cy) <= 22.0 * scale:
            return "prev"
        if math.hypot(pos.x() - play_cx, pos.y() - cy) <= 26.0 * scale:
            return "play_pause"
        if math.hypot(pos.x() - next_cx, pos.y() - cy) <= 22.0 * scale:
            return "next"
        return None

    def _scrubber_rect(self) -> QRectF:
        w = max(float(self.width()), self.spring_w.target)
        scale = self._get_size_scale()
        sx = 24.0 * scale
        sy = 94.0 * scale
        sw = w - 48.0 * scale
        sh = 14.0 * scale
        return QRectF(sx, sy, sw, sh)

    def _trigger_button(self, btn: str) -> None:
        self.expanded_until = time.time() + 4.0
        if btn == "play_pause":
            self.media_toggle_play()
        elif btn == "prev":
            self.media_prev()
        elif btn == "next":
            self.media_next()
        elif btn == "timer_pause_resume":
            if self.timer_active:
                if getattr(self, "timer_paused", False):
                    self.timer_end_time = time.time() + getattr(self, "timer_paused_rem", 0.0)
                    self.timer_paused = False
                else:
                    self.timer_paused_rem = max(0.0, self.timer_end_time - time.time())
                    self.timer_paused = True
        elif btn == "timer_cancel":
            self.timer_active = False
            self.timer_paused = False
            self.collapse_card()

    def media_toggle_play(self) -> None:
        self.media_playing = not self.media_playing
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
            except Exception:
                pass

    def media_prev(self) -> None:
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB1, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB1, 0, 2, 0)
            except Exception:
                pass

    def media_next(self) -> None:
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB0, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB0, 0, 2, 0)
            except Exception:
                pass

    def format_temp(self, temp_f: float) -> str:
        cfg = getattr(self.mgr, "cfg", None)
        units = getattr(cfg, "island_weather_units", "auto").lower()
        if units == "celsius":
            c = int(round((temp_f - 32.0) * 5.0 / 9.0))
            return f"{c}°"
        return f"{int(round(temp_f))}°"

    # ──────────────────────────────────────────────────────────────────────
    # Cached Vector Icons (DPI-Safe, 60 FPS)
    # ──────────────────────────────────────────────────────────────────────

    def _get_icon_pixmap(self, icon_name: str, dark: bool, dpr: float) -> QPixmap:
        key = (icon_name, dark, dpr, 24)
        if key in self._icon_cache:
            return self._icon_cache[key]

        size = 24
        px_w = int(size * dpr)
        px_h = int(size * dpr)
        pix = QPixmap(px_w, px_h)
        pix.fill(Qt.GlobalColor.transparent)
        pix.setDevicePixelRatio(dpr)

        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx, cy = size / 2.0, size / 2.0
        badge_r = 11.0

        if icon_name in ("success", "tick", "check", "confirmed"):
            bg_col = QColor(48, 209, 88, 48)
            border_col = QColor(48, 209, 88, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            tick = QPainterPath()
            tick.moveTo(cx - 4.8, cy - 0.2)
            tick.lineTo(cx - 1.2, cy + 3.4)
            tick.lineTo(cx + 4.8, cy - 3.4)
            p.strokePath(tick, QPen(QColor(48, 209, 88), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))

        elif icon_name in ("error", "alert"):
            bg_col = QColor(255, 69, 58, 48)
            border_col = QColor(255, 69, 58, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            p.setPen(QPen(QColor(255, 69, 58), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx, cy + 1.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(255, 69, 58)))
            p.drawEllipse(QPointF(cx, cy + 4.2), 1.2, 1.2)

        elif icon_name in ("warning", "amber"):
            bg_col = QColor(255, 159, 10, 48)
            border_col = QColor(255, 159, 10, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            p.setPen(QPen(QColor(255, 159, 10), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx, cy + 1.0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(255, 159, 10)))
            p.drawEllipse(QPointF(cx, cy + 4.2), 1.2, 1.2)

        elif icon_name in ("timer", "stopwatch", "clock"):
            bg_col = QColor(255, 159, 10, 48)
            border_col = QColor(255, 159, 10, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            p.setPen(QPen(QColor(255, 159, 10), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - badge_r + 2.0), QPointF(cx, cy - 3.8))
            p.drawEllipse(QPointF(cx, cy + 0.8), 4.8, 4.8)
            p.drawLine(QPointF(cx, cy + 0.8), QPointF(cx, cy - 2.0))
            p.drawLine(QPointF(cx, cy + 0.8), QPointF(cx + 2.2, cy + 0.8))

        elif icon_name in ("work", "code"):
            bg_col = QColor(0, 122, 255, 48)
            border_col = QColor(0, 122, 255, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            p.setPen(QPen(QColor(0, 122, 255), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx - 3.8, cy - 3.0), QPointF(cx - 0.8, cy))
            p.drawLine(QPointF(cx - 0.8, cy), QPointF(cx - 3.8, cy + 3.0))
            p.drawLine(QPointF(cx + 0.8, cy + 3.0), QPointF(cx + 4.2, cy + 3.0))

        elif icon_name in ("focus", "target"):
            bg_col = QColor(175, 82, 222, 48)
            border_col = QColor(175, 82, 222, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            p.setPen(QPen(QColor(175, 82, 222), 1.6, Qt.PenStyle.SolidLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), 5.2, 5.2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(175, 82, 222)))
            p.drawEllipse(QPointF(cx, cy), 2.2, 2.2)

        elif icon_name in ("evening", "moon"):
            bg_col = QColor(255, 159, 10, 48)
            border_col = QColor(255, 159, 10, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            m_path = QPainterPath()
            m_path.moveTo(cx + 1.5, cy - 5.0)
            m_path.arcTo(QRectF(cx - 5.0, cy - 5.0, 10.0, 10.0), 90, -180)
            m_path.arcTo(QRectF(cx - 2.8, cy - 4.2, 7.5, 8.4), -90, 180)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(255, 159, 10)))
            p.drawPath(m_path)

        elif icon_name in ("globe", "world", "earth", "timezone"):
            bg_col = QColor(0, 229, 255, 48)
            border_col = QColor(0, 229, 255, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            col = QColor(0, 229, 255)
            p.setPen(QPen(col, 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            r_g = 6.2
            p.drawEllipse(QPointF(cx, cy), r_g, r_g)
            p.drawLine(QPointF(cx - r_g, cy), QPointF(cx + r_g, cy))
            p.drawEllipse(QPointF(cx, cy), r_g * 0.45, r_g)

        elif icon_name in ("book", "fact", "facts", "wiki", "knowledge"):
            bg_col = QColor(255, 159, 10, 48)
            border_col = QColor(255, 159, 10, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            col = QColor(255, 159, 10)
            p.setPen(QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            left_page = QPainterPath()
            left_page.moveTo(cx, cy + 4.2)
            left_page.quadTo(cx - 3.2, cy + 3.2, cx - 6.2, cy + 3.8)
            left_page.lineTo(cx - 6.2, cy - 3.2)
            left_page.quadTo(cx - 3.2, cy - 3.8, cx, cy - 2.8)
            left_page.closeSubpath()
            p.drawPath(left_page)
            right_page = QPainterPath()
            right_page.moveTo(cx, cy + 4.2)
            right_page.quadTo(cx + 3.2, cy + 3.2, cx + 6.2, cy + 3.8)
            right_page.lineTo(cx + 6.2, cy - 3.2)
            right_page.quadTo(cx + 3.2, cy - 3.8, cx, cy - 2.8)
            right_page.closeSubpath()
            p.drawPath(right_page)
            p.drawLine(QPointF(cx, cy - 2.8), QPointF(cx, cy + 4.2))

        elif icon_name in ("aperture", "camera", "lens", "screen"):
            bg_col = QColor(0, 229, 255, 48)
            border_col = QColor(0, 229, 255, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            p.setPen(QPen(QColor(0, 229, 255), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            r_out = 6.2
            r_in = 2.4
            p.drawEllipse(QPointF(cx, cy), r_out, r_out)
            for i in range(6):
                a = i * (math.pi / 3.0)
                x1 = cx + r_in * math.cos(a)
                y1 = cy + r_in * math.sin(a)
                x2 = cx + r_out * math.cos(a + 0.8)
                y2 = cy + r_out * math.sin(a + 0.8)
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        else:
            bg_col = QColor(0, 122, 255, 48)
            border_col = QColor(0, 122, 255, 220)
            p.setPen(QPen(border_col, 1.3))
            p.setBrush(QBrush(bg_col))
            p.drawEllipse(QPointF(cx, cy), badge_r, badge_r)

            p.setPen(QPen(QColor(0, 122, 255), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawEllipse(QPointF(cx, cy - 3.8), 1.1, 1.1)
            p.drawLine(QPointF(cx, cy - 0.5), QPointF(cx, cy + 4.5))

        p.end()
        self._icon_cache[key] = pix
        return pix

    # ──────────────────────────────────────────────────────────────────────
    # Paint
    # ──────────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        now  = time.time()
        t    = now - self.t0
        w    = float(self.width())
        h    = float(self.height())
        r    = min(h / 2.0, 36.0)

        state   = self._resolve_state(now)
        dark    = self.dark_mode
        reduce  = T.reduce_motion()

        if reduce:
            elapsed = now - self._crossfade_start
            opacity = min(1.0, max(0.0, elapsed / (REDUCE_MOTION_MS / 1000.0)))
        else:
            opacity = min(1.0, max(0.0, self._opacity_spring.value))

        pill_rect = QRectF(2, -16, w - 4, h + 14)
        squircle  = create_squircle_path(pill_rect, r)

        # ── 1. Soft multi-layer drop shadow ──────────────────────────────
        for offset, alpha in [(2, 12), (4, 20), (7, 28)]:
            sr = QRectF(2, -16 + offset, w - 4, h + 14)
            sp = create_squircle_path(sr, r)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, alpha)))
            p.drawPath(sp)

        # ── 2. Glass tint fill ────────────────────────────────────────────
        bg_grad = QLinearGradient(0, -16, 0, h)
        if dark:
            bg_grad.setColorAt(0.0, QColor(36, 36, 42, 222))
            bg_grad.setColorAt(1.0, QColor(18, 18, 22, 238))
        else:
            bg_grad.setColorAt(0.0, QColor(248, 248, 252, 228))
            bg_grad.setColorAt(1.0, QColor(230, 230, 238, 238))

        p.fillPath(squircle, QBrush(bg_grad))

        # Media Card dominant color glass ambient tint
        if state == IslandState.CARD_MEDIA and self.media_dominant_color:
            tint = QColor(self.media_dominant_color)
            tint.setAlpha(36 if dark else 24)
            p.fillPath(squircle, QBrush(tint))

        # ── 3. Soft edge glow pulse in WAKE or CARD_SENDING ───────────────
        glow_val = self.spring_glow.value
        if state == IslandState.CARD_SENDING and not reduce:
            pulse_a = int(60 + 50 * math.sin(t * 6.0))
            glow_pen = QPen(QColor(0, 229, 255, pulse_a), 2.2)
            p.strokePath(squircle, glow_pen)
        elif state == IslandState.WAKE or glow_val > 0.02:
            pulse_a = int(min(255, max(0, glow_val * 160)))
            glow_pen = QPen(QColor(0, 229, 255, pulse_a), 2.4)
            p.strokePath(squircle, glow_pen)

        # ── 4. Subtle shimmer sweep across capsule in THINKING / CARD_SENDING
        if (state in (IslandState.THINKING, IslandState.CARD_SENDING)) and not reduce:
            self._draw_shimmer(p, squircle, w, h, t, dark)

        # ── 5. Top specular sheen ─────────────────────────────────────────
        sheen = QLinearGradient(0, -16, 0, h * 0.45)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 28 if dark else 72))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(squircle, QBrush(sheen))

        # ── 6. Inner stroke (1 px hairline, state-colored) ───────────────
        self._draw_inner_stroke(p, squircle, state, t, dark)

        # ── 7. Content (with opacity cross-fade) ─────────────────────────
        p.setOpacity(opacity)
        self._draw_content(p, w, h, now, t, state, dark, reduce)
        p.setOpacity(1.0)

    def _draw_shimmer(self, p: QPainter, path: QPainterPath, w: float, h: float, t: float, dark: bool) -> None:
        p.save()
        p.setClipPath(path)
        shimmer_dur = 1.2
        phase = (t % shimmer_dur) / shimmer_dur
        sx = -100.0 + phase * (w + 200.0)
        grad = QLinearGradient(sx, 0, sx + 80.0, h)
        grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.5, QColor(0, 229, 255, 45 if dark else 60))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(sx, 0, 80.0, h), QBrush(grad))
        p.restore()

    def _draw_inner_stroke(self, p: QPainter, path: QPainterPath,
                           state: IslandState, t: float, dark: bool) -> None:
        grad = QLinearGradient(0, 0, 0, float(self.height()))

        if state == IslandState.ERROR:
            c0 = QColor("#FF9F0A") if self.is_ai_error else QColor("#FF453A")
            c1 = QColor("#D97706") if self.is_ai_error else QColor("#D70015")
            grad.setColorAt(0.0, c0)
            grad.setColorAt(1.0, c1)

        elif state == IslandState.CARD_SENDING:
            pulse = int(140 + 80 * math.sin(t * 5.0))
            grad.setColorAt(0.0, QColor(0, 229, 255, pulse))
            grad.setColorAt(0.5, QColor(255, 159, 10, int(pulse * 0.85)))
            grad.setColorAt(1.0, QColor(0, 229, 255, int(pulse * 0.6)))

        elif state in (IslandState.RESULT, IslandState.CARD_RESULT):
            icon = self.quick_answer_icon if (state == IslandState.CARD_RESULT and self.quick_answer_icon) else self.result_icon
            if icon in ("error", "alert"):
                grad.setColorAt(0.0, QColor("#FF453A"))
                grad.setColorAt(1.0, QColor("#D70015"))
            elif icon in ("warning", "amber"):
                grad.setColorAt(0.0, QColor("#FF9F0A"))
                grad.setColorAt(1.0, QColor("#D97706"))
            elif icon in ("globe", "world", "earth", "timezone"):
                grad.setColorAt(0.0, QColor("#00E5FF"))
                grad.setColorAt(1.0, QColor("#007AFF"))
            elif icon in ("book", "fact", "facts", "wiki", "knowledge"):
                grad.setColorAt(0.0, QColor("#FFD60A"))
                grad.setColorAt(1.0, QColor("#FF9F0A"))
            elif icon in ("clock", "time", "timer", "stopwatch"):
                grad.setColorAt(0.0, QColor("#00E5FF"))
                grad.setColorAt(1.0, QColor("#5856D6"))
            else:
                grad.setColorAt(0.0, QColor("#30D158"))
                grad.setColorAt(1.0, QColor("#248A3D"))

        elif state in (IslandState.WAKE, IslandState.LISTENING):
            grad.setColorAt(0.0, QColor(0,  240, 255, 230))
            grad.setColorAt(0.5, QColor(175, 82, 222, 230))
            grad.setColorAt(1.0, QColor(255, 45, 85,  230))

        elif state in (IslandState.THINKING, IslandState.PROMPT):
            pulse = int(140 + 75 * math.sin(t * 6.0))
            grad.setColorAt(0.0, QColor(0, 229, 255, pulse))
            grad.setColorAt(1.0, QColor(0, 0, 0, 60))

        elif state in (IslandState.CARD_MEDIA, IslandState.CARD_WEATHER, IslandState.CARD_TIME, IslandState.CARD_DISAMBIGUATE, IslandState.CARD_TIMER):
            grad.setColorAt(0.0, QColor(255, 255, 255, 75 if dark else 180))
            grad.setColorAt(0.35, QColor(255, 255, 255, 25 if dark else 60))
            grad.setColorAt(1.0, QColor(0, 0, 0, 80 if dark else 30))

        else:
            if dark:
                grad.setColorAt(0.0, QColor(255, 255, 255, 65))
                grad.setColorAt(1.0, QColor(0,   0,   0,  75))
            else:
                grad.setColorAt(0.0, QColor(255, 255, 255, 200))
                grad.setColorAt(1.0, QColor(0,   0,   0,   25))

        p.strokePath(path, QPen(QBrush(grad), 1.0))

    def _draw_content(self, p: QPainter, w: float, h: float,
                      now: float, t: float, state: IslandState, dark: bool, reduce: bool) -> None:
        cy      = h / 2.0
        accent  = T.color.accent.resolve(dark)
        primary = T.color.label.resolve(dark)
        secondary = T.color.secondary_label.resolve(dark)

        CYAN = COLOR.HUD_ONLY.neon_cyan
        GOLD = COLOR.HUD_ONLY.stark_gold

        # ── State content routing ─────────────────────────────────────────

        if state == IslandState.WAKE:
            self._draw_wake(p, w, h, cy, t, dark, primary, accent, CYAN)

        elif state == IslandState.LISTENING:
            self._draw_listening(p, w, h, cy, t, now, dark, primary, accent, reduce)

        elif state == IslandState.THINKING:
            self._draw_thinking(p, w, h, cy, t, dark, primary, accent, CYAN)

        elif state == IslandState.CARD_SENDING:
            self._draw_sending_indicator(p, w, h, cy, t, dark, primary, accent, CYAN)

        elif state in (IslandState.RESULT, IslandState.CARD_RESULT):
            self._draw_result(p, w, h, cy, dark, primary)

        elif state == IslandState.ERROR:
            self._draw_error(p, cy, primary, dark)

        elif state in (IslandState.PROMPT, IslandState.SPEAKING):
            self._draw_prompt_speaking(p, w, h, cy, t, now, state, dark, primary, accent, CYAN)

        elif state == IslandState.GESTURE:
            self._draw_gesture(p, cy, primary, GOLD)

        elif state == IslandState.TRACKING:
            self._draw_tracking(p, w, h, cy, dark, primary, secondary, accent, state)

        elif state == IslandState.CARD_DISAMBIGUATE:
            self._draw_disambiguation_card(p, w, h, now, t, dark, primary, secondary, accent)

        elif state == IslandState.CARD_MEDIA:
            self._draw_media_card(p, w, h, now, t, dark, primary, secondary, accent, CYAN)

        elif state == IslandState.CARD_WEATHER:
            self._draw_weather_card(p, w, h, now, t, dark, primary, secondary, accent)

        elif state == IslandState.CARD_TIME:
            self._draw_time_card(p, w, h, now, dark, primary, secondary)

        elif state == IslandState.CARD_TIMER:
            self._draw_timer_card(p, w, h, now, dark, primary, secondary, accent)

        elif state == IslandState.HUD_VOLUME:
            self._draw_hud_volume(p, w, h, cy, dark, primary, secondary, accent)

        elif state == IslandState.HUD_WINDOW:
            self._draw_hud_window(p, w, h, cy, dark, primary, secondary, accent)

        else:
            # Standby IDLE & Compact Widgets
            self._draw_compact_widget(p, w, h, cy, dark, primary, secondary, accent, now, t)

    def _draw_disambiguation_card(self, p: QPainter, w: float, h: float,
                                  now: float, t: float, dark: bool,
                                  primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Disambiguation Card UI:
        Header: "Which one, sir?" (11 pt secondary muted text).
        Row of 2-3 numbered chips with Apple HIG pill styling:
          - Circular badge with number 1, 2, or 3.
          - Candidate title (e.g. "Spotify", "SoundCloud").
          - Hover / highlight state with soft glow.
        Hand-tracking cursor support:
          - Easing dwell ring around the chip (1.0s fill).
        """
        scale = self._get_size_scale()

        # 1. Header: "Which one, sir?" (11 pt secondary muted text)
        head_font = qfont(TYPE.subheadline)
        head_font.setPointSize(int(11 * scale))
        head_font.setWeight(QFont.Weight.Medium)
        p.setFont(head_font)
        p.setPen(secondary)
        head_rect = QRectF(0, 11.0 * scale, w, 18.0 * scale)
        header_text = self.disambiguate_header or "Which one, sir?"
        p.drawText(head_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, header_text)

        # 2. Numbered Chips Row
        rects = self._chip_rects(w, h, scale)
        for i, chip_rect in enumerate(rects):
            if i >= len(self.disambiguate_candidates):
                break
            cand = self.disambiguate_candidates[i]
            title = self._candidate_title(cand)
            is_hover = (self._hovered_chip_idx == i)
            pill_r = chip_rect.height() / 2.0

            # Hover / Highlight state with soft glow
            if is_hover:
                # Multi-layer soft glow around the pill
                glow1 = chip_rect.adjusted(-3.0 * scale, -3.0 * scale, 3.0 * scale, 3.0 * scale)
                p.setPen(QPen(QColor(0, 229, 255, 45), 2.5 * scale))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(glow1, glow1.height() / 2.0, glow1.height() / 2.0)

                glow2 = chip_rect.adjusted(-1.5 * scale, -1.5 * scale, 1.5 * scale, 1.5 * scale)
                p.setPen(QPen(QColor(0, 229, 255, 95), 1.5 * scale))
                p.drawRoundedRect(glow2, glow2.height() / 2.0, glow2.height() / 2.0)

                chip_bg = QColor(255, 255, 255, 45 if dark else 75)
                chip_border = QPen(QColor(0, 229, 255, 210), 1.4 * scale)
            else:
                chip_bg = QColor(255, 255, 255, 18 if dark else 35)
                chip_border = QPen(QColor(255, 255, 255, 34 if dark else 65), 1.0 * scale)

            # Draw Chip Pill Background & Border
            p.setPen(chip_border)
            p.setBrush(QBrush(chip_bg))
            p.drawRoundedRect(chip_rect, pill_r, pill_r)

            # Circular Badge (22 px diameter) with number 1, 2, or 3
            badge_d = 22.0 * scale
            bx = chip_rect.x() + 7.0 * scale
            by = chip_rect.y() + (chip_rect.height() - badge_d) / 2.0
            badge_rect = QRectF(bx, by, badge_d, badge_d)

            p.setPen(Qt.PenStyle.NoPen)
            if is_hover:
                p.setBrush(QBrush(COLOR.HUD_ONLY.neon_cyan))
                p.drawEllipse(badge_rect)
                num_color = QColor(10, 14, 20)
            else:
                p.setBrush(QBrush(QColor(255, 255, 255, 36 if dark else 55)))
                p.drawEllipse(badge_rect)
                num_color = primary

            badge_font = qfont(TYPE.footnote)
            badge_font.setPointSize(int(11 * scale))
            badge_font.setWeight(QFont.Weight.Bold)
            p.setFont(badge_font)
            p.setPen(num_color)
            p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, str(i + 1))

            # Candidate Title
            tx = bx + badge_d + 8.0 * scale
            tw = chip_rect.right() - tx - 10.0 * scale
            title_rect = QRectF(tx, chip_rect.y(), tw, chip_rect.height())

            title_font = qfont(TYPE.subheadline)
            title_font.setPointSize(int(12 * scale))
            title_font.setWeight(QFont.Weight.DemiBold)
            p.setFont(title_font)
            p.setPen(primary if not is_hover else (QColor(255, 255, 255) if dark else QColor(0, 0, 0)))

            metrics = p.fontMetrics()
            elided_title = metrics.elidedText(title, Qt.TextElideMode.ElideRight, int(tw))
            p.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_title)

            # Hand-tracking cursor easing dwell ring (1.0s fill)
            if is_hover and self._chip_dwell_elapsed > 0.03:
                raw_dwell = min(1.0, max(0.0, self._chip_dwell_elapsed / 1.0))
                # Smooth cubic easing (smoothstep)
                eased = raw_dwell * raw_dwell * (3.0 - 2.0 * raw_dwell)

                # 1. Circular dwell ring around badge
                dwell_pen = QPen(COLOR.HUD_ONLY.neon_cyan, 2.2 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                p.setPen(dwell_pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                d_rect = badge_rect.adjusted(-3.5 * scale, -3.5 * scale, 3.5 * scale, 3.5 * scale)
                p.drawArc(d_rect, 90 * 16, -int(eased * 360 * 16))

                # 2. Outer dwell ring around the chip
                d_chip = chip_rect.adjusted(-2.5 * scale, -2.5 * scale, 2.5 * scale, 2.5 * scale)
                p.setPen(QPen(QColor(0, 229, 255, int(110 + 100 * eased)), (1.5 + 1.2 * eased) * scale))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(d_chip, d_chip.height() / 2.0, d_chip.height() / 2.0)

    # ──────────────────────────────────────────────────────────────────────
    # Compact Widgets Layouts
    # ──────────────────────────────────────────────────────────────────────

    def _draw_compact_widget(self, p: QPainter, w: float, h: float, cy: float,
                             dark: bool, primary: QColor, secondary: QColor,
                             accent: QColor, now: float, t: float) -> None:
        compact_mode = self._get_current_compact_mode()

        if compact_mode == "media":
            # ── Media compact layout ─────────────────────────────────────
            # Leading side: 20x20 album art thumbnail (rounded squircle)
            thumb_size = 20.0
            tx = 8.0
            ty = cy - thumb_size / 2.0
            thumb_rect = QRectF(tx, ty, thumb_size, thumb_size)
            thumb_path = create_squircle_path(thumb_rect, 5.0)

            p.save()
            p.setClipPath(thumb_path)
            art = self._get_album_art(thumb_size)
            p.drawPixmap(QPointF(tx, ty), art)
            p.restore()

            p.strokePath(thumb_path, QPen(QColor(255, 255, 255, 40), 1.0))

            # Center text: track title
            font = qfont(TYPE.caption_1)
            font.setWeight(QFont.Weight.DemiBold)
            p.setFont(font)
            p.setPen(primary)
            text_rect = QRectF(34.0, 0, w - 34.0 - 28.0, h)
            title = self.media_title if len(self.media_title) <= 14 else self.media_title[:13] + "…"
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)

            # Trailing side: 4 audio-reactive animated bars
            bx0 = w - 24.0
            bar_w = 2.2
            gap = 1.8
            max_bh = 13.0
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(4):
                bh = max(3.0, self._media_bars[i] * max_bh)
                bx = bx0 + i * (bar_w + gap)
                by = cy - bh / 2.0
                bar_col = QColor(175, 82, 222) if i % 2 == 0 else QColor(0, 229, 255)
                p.setBrush(QBrush(bar_col))
                p.drawRoundedRect(QRectF(bx, by, bar_w, bh), bar_w / 2.0, bar_w / 2.0)

        elif compact_mode == "timer":
            # ── Timer compact layout ─────────────────────────────────────
            # Leading side: progress ring
            scale = self._get_size_scale()
            rx, ry = 16.0 * scale, cy
            ring_r = 7.5 * scale
            pen_w = 2.2 * scale
            p.setPen(QPen(QColor(255, 255, 255, 35 if dark else 55), pen_w))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(rx, ry), ring_r, ring_r)

            if getattr(self, "timer_paused", False):
                rem = getattr(self, "timer_paused_rem", 0.0)
            else:
                rem = max(0.0, self.timer_end_time - now) if self.timer_active else 180.0
            dur = max(1.0, self.timer_duration_sec)
            frac = min(1.0, max(0.0, rem / dur))

            arc_pen = QPen(QColor("#FF9F0A"), pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(arc_pen)
            arc_rect = QRectF(rx - ring_r, ry - ring_r, ring_r * 2.0, ring_r * 2.0)
            # Circular progress ring decreasing smoothly as remaining time updates
            p.drawArc(arc_rect, 90 * 16, -int(frac * 360 * 16))

            # Trailing side: tabular numerals
            font = qfont(TYPE.metric, scale)
            font.setPointSize(int(11 * scale))
            font.setWeight(QFont.Weight.DemiBold)
            try:
                font.setFeature(b"tnum", 1)
            except Exception:
                pass
            p.setFont(font)
            p.setPen(primary)
            if rem >= 3600:
                hrs = int(rem // 3600)
                mins = int((rem % 3600) // 60)
                secs = int(rem % 60)
                time_txt = f"{hrs:d}:{mins:02d}:{secs:02d}"
            else:
                mins = int(rem // 60)
                secs = int(rem % 60)
                time_txt = f"{mins:02d}:{secs:02d}"
            text_rect = QRectF(32.0 * scale, 0, w - 44.0 * scale, h)
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, time_txt)

        elif compact_mode == "time_weather":
            # ── Time + Weather compact layout ────────────────────────────
            # Leading side: Current time (honoring 12h/24h)
            cfg = getattr(self.mgr, "cfg", None)
            fmt_24 = (getattr(cfg, "island_clock_format", "12h") == "24h")
            tm = time.localtime(now)
            time_str = time.strftime("%H:%M" if fmt_24 else "%I:%M", tm).lstrip("0")
            if not time_str:
                time_str = "12:00"

            time_font = qfont(TYPE.caption_1)
            time_font.setWeight(QFont.Weight.DemiBold)
            p.setFont(time_font)
            p.setPen(primary)
            p.drawText(QRectF(14.0, 0, 52.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, time_str)

            # Trailing side: weather SVG glyph + temperature
            temp_str = self.format_temp(self.weather_temp)
            temp_w = p.fontMetrics().horizontalAdvance(temp_str)
            tx = w - temp_w - 14.0
            p.drawText(QRectF(tx, 0, temp_w + 4.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, temp_str)

            # Weather glyph (concentric offset)
            gx = tx - 18.0
            self._draw_weather_glyph(p, self.weather_icon, gx, cy, 15.0, dark)

        elif compact_mode == "time":
            # ── Time compact layout ──────────────────────────────────────
            cfg = getattr(self.mgr, "cfg", None)
            fmt_24 = (getattr(cfg, "island_clock_format", "12h") == "24h")
            tm = time.localtime(now)
            time_str = time.strftime("%H:%M" if fmt_24 else "%I:%M %p", tm).lstrip("0")
            time_font = qfont(TYPE.caption_1)
            time_font.setWeight(QFont.Weight.DemiBold)
            p.setFont(time_font)
            p.setPen(primary)
            p.drawText(QRectF(14.0, 0, w - 28.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, time_str)

        else:
            # ── Minimal standby IDLE capsule ─────────────────────────────
            dot_x = 18.0
            dot_r = 4.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(secondary))
            p.drawEllipse(QPointF(dot_x, cy), dot_r, dot_r)
            lbl_font = qfont(TYPE.callout)
            lbl_font.setWeight(QFont.Weight.DemiBold)
            p.setFont(lbl_font)
            p.setPen(secondary if not self.active else primary)
            p.drawText(int(dot_x + 14.0), int(cy + 4), "Jarvis")

    # ──────────────────────────────────────────────────────────────────────
    # Expanded Cards Renderers
    # ──────────────────────────────────────────────────────────────────────

    def _draw_media_card(self, p: QPainter, w: float, h: float,
                         now: float, t: float, dark: bool,
                         primary: QColor, secondary: QColor, accent: QColor, CYAN: QColor) -> None:
        """
        Expanded Media Card:
        56 px artwork thumbnail with soft shadow and dominant color tint,
        track title & artist (marquee scrolling only if truncated),
        scrubber bar with elapsed/remaining time (tabular numerals),
        prev / play-pause / next buttons with dwell ring and fist-click support,
        source app footnote.
        """
        scale = self._get_size_scale()

        # 1. 56 px Artwork Thumbnail
        art_size = 56.0 * scale
        ax = 20.0 * scale
        ay = 20.0 * scale
        art_rect = QRectF(ax, ay, art_size, art_size)
        art_path = create_squircle_path(art_rect, 13.0 * scale)

        # Soft shadow & ambient tint
        for off, a in [(3, 20), (6, 35)]:
            s_rect = QRectF(ax, ay + off, art_size, art_size)
            s_path = create_squircle_path(s_rect, 13.0 * scale)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, a)))
            p.drawPath(s_path)

        # Dominant glow behind artwork
        glow = QRadialGradient(ax + art_size / 2.0, ay + art_size / 2.0, art_size * 0.8)
        glow.setColorAt(0.0, QColor(self.media_dominant_color.red(), self.media_dominant_color.green(), self.media_dominant_color.blue(), 100))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(ax + art_size / 2.0, ay + art_size / 2.0), art_size * 0.7, art_size * 0.7)

        # Draw Artwork Pixmap
        art_pix = self._get_album_art(art_size)
        p.save()
        p.setClipPath(art_path)
        p.drawPixmap(QPointF(ax, ay), art_pix)
        p.restore()
        p.strokePath(art_path, QPen(QColor(255, 255, 255, 60), 1.0))

        # 2. Track Title & Artist (Marquee scrolling only if truncated)
        tx = ax + art_size + 16.0 * scale
        tw = w - tx - 20.0 * scale

        title_font = qfont(TYPE.headline)
        title_font.setPointSize(int(14 * scale))
        title_font.setWeight(QFont.Weight.DemiBold)
        title_rect = QRectF(tx, ay + 4.0 * scale, tw, 22.0 * scale)
        self._draw_marquee_text(p, self.media_title, title_font, title_rect, primary, now - self._card_expand_t)

        artist_font = qfont(TYPE.subheadline)
        artist_font.setPointSize(int(12 * scale))
        artist_font.setWeight(QFont.Weight.Medium)
        artist_rect = QRectF(tx, ay + 26.0 * scale, tw, 20.0 * scale)
        self._draw_marquee_text(p, self.media_artist, artist_font, artist_rect, secondary, now - self._card_expand_t)

        # 3. Scrubber Bar with Tabular Numerals
        sr = self._scrubber_rect()
        # Track background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 30 if dark else 50)))
        p.drawRoundedRect(sr, 2.0, 2.0)

        # Filled progress
        prog_w = sr.width() * min(1.0, max(0.0, self.media_progress))
        if prog_w > 0:
            fill_rect = QRectF(sr.x(), sr.y(), prog_w, sr.height())
            p.setBrush(QBrush(primary))
            p.drawRoundedRect(fill_rect, 2.0, 2.0)

        # Scrubber handle
        hx = sr.x() + prog_w
        hy = sr.y() + sr.height() / 2.0
        p.setBrush(QBrush(QColor(255, 255, 255, 255)))
        p.drawEllipse(QPointF(hx, hy), 4.5 * scale, 4.5 * scale)

        # Numerals
        num_font = qfont(TYPE.caption_2)
        num_font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
        p.setFont(num_font)
        p.setPen(secondary)
        el_m = int(self.media_position_sec // 60)
        el_s = int(self.media_position_sec % 60)
        el_str = f"{el_m}:{el_s:02d}"
        p.drawText(QRectF(sr.x(), sr.y() + sr.height() + 2.0, 50.0, 16.0), Qt.AlignmentFlag.AlignLeft, el_str)

        rem_sec = max(0.0, self.media_duration_sec - self.media_position_sec)
        rem_m = int(rem_sec // 60)
        rem_s = int(rem_sec % 60)
        rem_str = f"-{rem_m}:{rem_s:02d}"
        p.drawText(QRectF(sr.right() - 50.0, sr.y() + sr.height() + 2.0, 50.0, 16.0), Qt.AlignmentFlag.AlignRight, rem_str)

        # 4. Playback Controls (Prev, Play-Pause, Next)
        cy_btn = 136.0 * scale
        prev_cx = (w / 2.0 - 52.0 * scale)
        play_cx = (w / 2.0)
        next_cx = (w / 2.0 + 52.0 * scale)

        self._draw_playback_button(p, "prev", prev_cx, cy_btn, 16.0 * scale, primary, scale, now)
        self._draw_playback_button(p, "play_pause", play_cx, cy_btn, 20.0 * scale, primary, scale, now)
        self._draw_playback_button(p, "next", next_cx, cy_btn, 16.0 * scale, primary, scale, now)

        # 5. Source app footnote
        foot_font = qfont(TYPE.caption_2)
        p.setFont(foot_font)
        p.setPen(secondary)
        src_txt = f"♪ {self.media_source}"
        p.drawText(QRectF(w - 110.0 * scale, cy_btn - 8.0 * scale, 90.0 * scale, 16.0 * scale),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, src_txt)

    def _draw_playback_button(self, p: QPainter, btn_id: str, cx: float, cy: float,
                              r: float, color: QColor, scale: float, now: float) -> None:
        is_hover = (self._hovered_btn == btn_id)
        is_flash = (now < self._btn_flash_until and self._hovered_btn == btn_id)

        # Hover circle highlight
        if is_hover or is_flash:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(255, 255, 255, 60 if is_flash else 25)))
            p.drawEllipse(QPointF(cx, cy), r + 4.0 * scale, r + 4.0 * scale)

        # Hand-tracking dwell ring (fills over 1.0s)
        if is_hover and self._btn_dwell_elapsed > 0.05:
            dwell_prog = min(1.0, self._btn_dwell_elapsed / 1.0)
            dwell_pen = QPen(COLOR.HUD_ONLY.neon_cyan, 2.2 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(dwell_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            d_rect = QRectF(cx - r - 4.0 * scale, cy - r - 4.0 * scale, (r + 4.0 * scale) * 2.0, (r + 4.0 * scale) * 2.0)
            p.drawArc(d_rect, 90 * 16, -int(dwell_prog * 360 * 16))

        # Vector glyphs
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))

        if btn_id == "play_pause":
            if not self.media_playing:
                # Play triangle
                tri = QPolygonF([
                    QPointF(cx - 5.0 * scale, cy - 8.0 * scale),
                    QPointF(cx + 7.0 * scale, cy),
                    QPointF(cx - 5.0 * scale, cy + 8.0 * scale)
                ])
                p.drawPolygon(tri)
            else:
                # Pause bars
                bw = 3.5 * scale
                bh = 15.0 * scale
                p.drawRoundedRect(QRectF(cx - 5.5 * scale, cy - bh / 2.0, bw, bh), 1.5, 1.5)
                p.drawRoundedRect(QRectF(cx + 2.0 * scale, cy - bh / 2.0, bw, bh), 1.5, 1.5)

        elif btn_id == "prev":
            # Bar left
            p.drawRoundedRect(QRectF(cx - 8.0 * scale, cy - 7.0 * scale, 2.0 * scale, 14.0 * scale), 1.0, 1.0)
            # Double triangle left
            tri1 = QPolygonF([
                QPointF(cx + 6.0 * scale, cy - 7.0 * scale),
                QPointF(cx, cy),
                QPointF(cx + 6.0 * scale, cy + 7.0 * scale)
            ])
            tri2 = QPolygonF([
                QPointF(cx, cy - 7.0 * scale),
                QPointF(cx - 6.0 * scale, cy),
                QPointF(cx, cy + 7.0 * scale)
            ])
            p.drawPolygon(tri1)
            p.drawPolygon(tri2)

        elif btn_id == "next":
            # Double triangle right
            tri1 = QPolygonF([
                QPointF(cx - 6.0 * scale, cy - 7.0 * scale),
                QPointF(cx, cy),
                QPointF(cx - 6.0 * scale, cy + 7.0 * scale)
            ])
            tri2 = QPolygonF([
                QPointF(cx, cy - 7.0 * scale),
                QPointF(cx + 6.0 * scale, cy),
                QPointF(cx, cy + 7.0 * scale)
            ])
            p.drawPolygon(tri1)
            p.drawPolygon(tri2)
            # Bar right
            p.drawRoundedRect(QRectF(cx + 6.0 * scale, cy - 7.0 * scale, 2.0 * scale, 14.0 * scale), 1.0, 1.0)

    def _draw_weather_card(self, p: QPainter, w: float, h: float,
                          now: float, t: float, dark: bool,
                          primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded Weather Card:
        Current temp, condition label, high/low, feels-like, and next 5 hours mini row.
        Clean vector SVG icons, zero emoji.
        """
        scale = self._get_size_scale()

        # Top section: City & Condition
        city_font = qfont(TYPE.headline)
        city_font.setPointSize(int(15 * scale))
        city_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(city_font)
        p.setPen(primary)
        p.drawText(int(24 * scale), int(30 * scale), self.weather_city)

        cond_font = qfont(TYPE.subheadline)
        cond_font.setPointSize(int(12 * scale))
        p.setFont(cond_font)
        p.setPen(primary)
        p.drawText(int(24 * scale), int(48 * scale), self.weather_condition)

        hi_lo = f"H: {self.format_temp(self.weather_high)}  L: {self.format_temp(self.weather_low)}  •  Feels like {self.format_temp(self.weather_feels_like)}"
        sub_font = qfont(TYPE.caption_1)
        p.setFont(sub_font)
        p.setPen(secondary)
        p.drawText(int(24 * scale), int(66 * scale), hi_lo)

        # Large current temperature
        temp_font = qfont(TYPE.large_title)
        temp_font.setPointSize(int(34 * scale))
        temp_font.setWeight(QFont.Weight.Light)
        p.setFont(temp_font)
        p.setPen(primary)
        temp_str = self.format_temp(self.weather_temp)
        tw = p.fontMetrics().horizontalAdvance(temp_str)
        p.drawText(QRectF(w - tw - 64 * scale, 16 * scale, tw + 8, 48 * scale),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, temp_str)

        # 36 px vector weather glyph top-right
        self._draw_weather_glyph(p, self.weather_icon, w - 34 * scale, 40 * scale, 32.0 * scale, dark)

        # Separator line
        p.setPen(QPen(QColor(255, 255, 255, 30 if dark else 50), 1.0))
        p.drawLine(QPointF(24 * scale, 82 * scale), QPointF(w - 24 * scale, 82 * scale))

        # Bottom section: 5-hour hourly row
        col_w = (w - 48 * scale) / 5.0
        time_f = qfont(TYPE.caption_2)
        p.setFont(time_f)

        for i, item in enumerate(self.weather_hourly[:5]):
            slot_x = 24 * scale + i * col_w
            slot_cx = slot_x + col_w / 2.0

            # Hour label
            p.setPen(secondary)
            p.drawText(QRectF(slot_x, 90 * scale, col_w, 16 * scale), Qt.AlignmentFlag.AlignCenter, item["time"])

            # Mini vector icon
            self._draw_weather_glyph(p, item["icon"], slot_cx, 122 * scale, 18.0 * scale, dark)

            # Temp
            p.setFont(qfont(TYPE.caption_1))
            p.setPen(primary)
            p.drawText(QRectF(slot_x, 142 * scale, col_w, 18 * scale),
                       Qt.AlignmentFlag.AlignCenter, self.format_temp(item["temp"]))
            p.setFont(time_f)

    def _draw_time_card(self, p: QPainter, w: float, h: float,
                        now: float, dark: bool, primary: QColor, secondary: QColor) -> None:
        """
        Expanded Time Card:
        Large clock display, full date, and next timer/alarm.
        """
        scale = self._get_size_scale()

        cfg = getattr(self.mgr, "cfg", None)
        fmt_24 = (getattr(cfg, "island_clock_format", "12h") == "24h")
        tm = time.localtime(now)

        # Large clock display
        clock_font = qfont(TYPE.large_title)
        clock_font.setPointSize(int(36 * scale))
        clock_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(clock_font)
        p.setPen(primary)

        time_str = time.strftime("%H:%M" if fmt_24 else "%I:%M", tm).lstrip("0")
        if not time_str:
            time_str = "12:00"
        p.drawText(int(24 * scale), int(48 * scale), time_str)

        if not fmt_24:
            am_pm = time.strftime("%p", tm)
            p.setFont(qfont(TYPE.subheadline))
            p.setPen(secondary)
            tw = p.fontMetrics().horizontalAdvance(time_str)
            p.drawText(int(28 * scale + tw * 1.8), int(36 * scale), am_pm)

        # Full date
        date_str = time.strftime("%A, %B %d", tm)
        date_font = qfont(TYPE.subheadline)
        p.setFont(date_font)
        p.setPen(secondary)
        p.drawText(int(24 * scale), int(72 * scale), date_str)

        # Separator line
        p.setPen(QPen(QColor(255, 255, 255, 30 if dark else 50), 1.0))
        p.drawLine(QPointF(24 * scale, 88 * scale), QPointF(w - 24 * scale, 88 * scale))

        # Timer / Alarm status row
        sub_font = qfont(TYPE.footnote)
        p.setFont(sub_font)
        if self.timer_active:
            rem = max(0.0, self.timer_end_time - now)
            mins = int(rem // 60)
            secs = int(rem % 60)
            p.setPen(QColor("#FF9F0A"))
            p.drawText(int(24 * scale), int(114 * scale), f"⏱  Timer: {mins:02d}:{secs:02d} remaining")
        else:
            p.setPen(secondary)
            p.drawText(int(24 * scale), int(114 * scale), "⏰  Alarm: 7:00 AM  •  Tomorrow")

    def _draw_timer_card(self, p: QPainter, w: float, h: float,
                         now: float, dark: bool, primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded Timer Card:
        Renders large remaining time in tabular numerals, live horizontal progress bar,
        end time pill, and focus-safe action controls.
        """
        scale = self._get_size_scale()
        dpr = self.devicePixelRatio()

        if getattr(self, "timer_paused", False):
            rem = getattr(self, "timer_paused_rem", 0.0)
        else:
            rem = max(0.0, self.timer_end_time - now) if self.timer_active else 180.0
        dur = max(1.0, self.timer_duration_sec)
        prog = min(1.0, max(0.0, 1.0 - (rem / dur)))

        # 1. Header: Icon badge + Title + End Time
        ix = 24.0 * scale
        iy = 16.0 * scale
        icon_pix = self._get_icon_pixmap("timer", dark, dpr)
        p.drawPixmap(QPointF(ix, iy), icon_pix)

        title_font = qfont(TYPE.headline)
        title_font.setPointSize(int(13 * scale))
        title_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(title_font)
        p.setPen(primary)
        p.drawText(int(ix + 32.0 * scale), int(iy + 17.0 * scale), "Timer")

        # End Time (trailing)
        end_tm = time.localtime(self.timer_end_time if self.timer_active else now + 180.0)
        end_str = time.strftime("%I:%M %p", end_tm).lstrip("0")
        foot_font = qfont(TYPE.caption_1)
        p.setFont(foot_font)
        p.setPen(secondary)
        end_txt = f"Ends at {end_str}"
        p.drawText(QRectF(w - 140.0 * scale, iy, 116.0 * scale, 24.0 * scale),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, end_txt)

        # 2. Large Tabular Remaining Time
        large_font = qfont(TYPE.metric_lg)
        large_font.setPointSize(int(36 * scale))
        large_font.setWeight(QFont.Weight.Bold)
        try:
            large_font.setFeature(b"tnum", 1)
        except Exception:
            pass
        p.setFont(large_font)
        p.setPen(primary)

        if rem >= 3600:
            hrs = int(rem // 3600)
            mins = int((rem % 3600) // 60)
            secs = int(rem % 60)
            rem_str = f"{hrs:d}:{mins:02d}:{secs:02d}"
        else:
            mins = int(rem // 60)
            secs = int(rem % 60)
            rem_str = f"{mins:02d}:{secs:02d}"

        p.drawText(int(24.0 * scale), int(78.0 * scale), rem_str)

        # Status badge beside remaining time
        if getattr(self, "timer_paused", False):
            badge_text = "PAUSED"
            badge_col = QColor("#FFD60A")
        elif rem <= 0.0:
            badge_text = "DONE"
            badge_col = QColor("#30D158")
        else:
            badge_text = "COUNTDOWN"
            badge_col = QColor("#FF9F0A")

        p.setFont(qfont(TYPE.caption_2))
        time_w = p.fontMetrics().horizontalAdvance(rem_str)
        badge_x = 24.0 * scale + time_w * 2.3 + 14.0 * scale
        badge_y = 56.0 * scale
        badge_rect = QRectF(badge_x, badge_y, 74.0 * scale, 18.0 * scale)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(badge_col.red(), badge_col.green(), badge_col.blue(), 35)))
        p.drawRoundedRect(badge_rect, 9.0 * scale, 9.0 * scale)
        p.setPen(badge_col)
        p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

        # 3. Horizontal Progress Bar
        bx = 24.0 * scale
        by = 94.0 * scale
        bw = w - 48.0 * scale
        bh = 6.0 * scale
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 30 if dark else 55)))
        p.drawRoundedRect(QRectF(bx, by, bw, bh), bh / 2.0, bh / 2.0)

        # Filled portion
        fill_w = max(bh, bw * prog)
        grad = QLinearGradient(bx, by, bx + bw, by)
        grad.setColorAt(0.0, QColor("#FF9F0A"))
        grad.setColorAt(1.0, QColor("#FFB340"))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(bx, by, fill_w, bh), bh / 2.0, bh / 2.0)

        # Elapsed and Remaining underneath progress bar
        el_sec = dur - rem
        el_m = int(el_sec // 60)
        el_s = int(el_sec % 60)
        el_txt = f"{el_m:02d}:{el_s:02d}"
        rem_m = int(rem // 60)
        rem_s = int(rem % 60)
        rem_txt = f"-{rem_m:02d}:{rem_s:02d}"

        p.setFont(qfont(TYPE.caption_2))
        p.setPen(secondary)
        p.drawText(QRectF(bx, by + bh + 3.0, 50.0 * scale, 14.0 * scale), Qt.AlignmentFlag.AlignLeft, el_txt)
        p.drawText(QRectF(bx + bw - 50.0 * scale, by + bh + 3.0, 50.0 * scale, 14.0 * scale), Qt.AlignmentFlag.AlignRight, rem_txt)

        # 4. Action Buttons (Cancel / Pause-Resume)
        cy_btn = 126.0 * scale
        cancel_cx = w / 2.0 - 44.0 * scale
        pause_cx  = w / 2.0 + 44.0 * scale
        self._draw_timer_button(p, "timer_cancel", cancel_cx, cy_btn, "Cancel", secondary, scale, now)
        is_paused = getattr(self, "timer_paused", False)
        play_label = "Resume" if is_paused else "Pause"
        self._draw_timer_button(p, "timer_pause_resume", pause_cx, cy_btn, play_label, QColor("#FF9F0A"), scale, now)

    def _draw_timer_button(self, p: QPainter, btn_id: str, cx: float, cy: float,
                           label: str, col: QColor, scale: float, now: float) -> None:
        is_hover = (self._hovered_btn == btn_id)
        is_flash = (now < self._btn_flash_until and self._hovered_btn == btn_id)

        bw = 70.0 * scale
        bh = 22.0 * scale
        rect = QRectF(cx - bw / 2.0, cy - bh / 2.0, bw, bh)

        bg_alpha = 75 if is_flash else (45 if is_hover else 25)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, bg_alpha)))
        p.drawRoundedRect(rect, bh / 2.0, bh / 2.0)

        if is_hover and self._btn_dwell_elapsed > 0.05:
            dwell_prog = min(1.0, self._btn_dwell_elapsed / 1.0)
            dwell_pen = QPen(COLOR.HUD_ONLY.neon_cyan, 1.8 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(dwell_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), bh / 2.0, bh / 2.0)

        font = qfont(TYPE.caption_1)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(col)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    # ──────────────────────────────────────────────────────────────────────
    # Vector Weather Glyph Drawing (No Emoji!)
    # ──────────────────────────────────────────────────────────────────────

    def _draw_weather_glyph(self, p: QPainter, icon_name: str, cx: float, cy: float, size: float, dark: bool) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if icon_name in ("sunny", "clear"):
            # Golden sun disc + radiating rays
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#FF9F0A")))
            p.drawEllipse(QPointF(cx, cy), size * 0.30, size * 0.30)
            p.setPen(QPen(QColor("#FF9F0A"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(8):
                ang = i * (math.pi / 4.0)
                r0 = size * 0.38
                r1 = size * 0.50
                p.drawLine(QPointF(cx + math.cos(ang) * r0, cy + math.sin(ang) * r0),
                           QPointF(cx + math.cos(ang) * r1, cy + math.sin(ang) * r1))

        elif icon_name in ("cloudy", "overcast"):
            # Clean Apple cloud squircle
            self._draw_cloud_shape(p, cx, cy, size * 0.9, QColor("#8E8E93" if dark else "#AEAEB2"))

        elif icon_name in ("partly_cloudy", "cloud_sun"):
            # Sun peeking out behind top-right of cloud
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#FF9F0A")))
            p.drawEllipse(QPointF(cx + size * 0.20, cy - size * 0.20), size * 0.24, size * 0.24)
            self._draw_cloud_shape(p, cx - size * 0.08, cy + size * 0.08, size * 0.82,
                                   QColor("#E5E5EA" if dark else "#8E8E93"))

        elif icon_name in ("rain", "drizzle"):
            # Cloud + 3 diagonal rain streaks
            self._draw_cloud_shape(p, cx, cy - size * 0.12, size * 0.80, QColor("#8E8E93"))
            p.setPen(QPen(QColor("#64D2FF"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for dx in [-size * 0.22, 0.0, size * 0.22]:
                p.drawLine(QPointF(cx + dx, cy + size * 0.14),
                           QPointF(cx + dx - size * 0.10, cy + size * 0.38))

        elif icon_name in ("snow", "flurries"):
            # Cloud + 3 snow dots
            self._draw_cloud_shape(p, cx, cy - size * 0.12, size * 0.80, QColor("#8E8E93"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#E0F7FA")))
            for dx in [-size * 0.22, 0.0, size * 0.22]:
                p.drawEllipse(QPointF(cx + dx, cy + size * 0.26), size * 0.08, size * 0.08)

        elif icon_name in ("thunderstorm", "storm"):
            # Storm cloud + golden lightning bolt
            self._draw_cloud_shape(p, cx, cy - size * 0.14, size * 0.80, QColor("#636366"))
            bolt = QPainterPath()
            bolt.moveTo(cx + size * 0.04, cy + size * 0.02)
            bolt.lineTo(cx - size * 0.08, cy + size * 0.20)
            bolt.lineTo(cx + size * 0.02, cy + size * 0.20)
            bolt.lineTo(cx - size * 0.06, cy + size * 0.44)
            bolt.lineTo(cx + size * 0.10, cy + size * 0.18)
            bolt.lineTo(cx, cy + size * 0.18)
            bolt.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#FFD60A")))
            p.drawPath(bolt)

        else:
            self._draw_cloud_shape(p, cx, cy, size * 0.9, QColor("#8E8E93"))

        p.restore()

    def _draw_cloud_shape(self, p: QPainter, cx: float, cy: float, size: float, color: QColor) -> None:
        """Renders an Apple-grade multi-lobe vector cloud."""
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        w = size * 0.88
        h = size * 0.44
        # Base pill
        p.drawRoundedRect(QRectF(cx - w / 2.0, cy - h / 4.0, w, h), h / 2.0, h / 2.0)
        # Left lobe
        p.drawEllipse(QPointF(cx - w * 0.20, cy - h * 0.35), h * 0.65, h * 0.65)
        # Main center-right lobe
        p.drawEllipse(QPointF(cx + w * 0.12, cy - h * 0.45), h * 0.85, h * 0.85)

    def set_thumbnail_bytes(self, data: bytes) -> None:
        """Sets thumbnail from raw bytes and samples dominant color."""
        pix = QPixmap()
        if pix.loadFromData(data):
            self.media_album_art = pix
            img = pix.toImage().scaled(16, 16)
            r, g, b = 0, 0, 0
            n = img.width() * img.height()
            for y in range(img.height()):
                for x in range(img.width()):
                    c = img.pixelColor(x, y)
                    r += c.red()
                    g += c.green()
                    b += c.blue()
            if n > 0:
                self.media_dominant_color = QColor(int(r / n), int(g / n), int(b / n))
            self._icon_cache.clear()

    def _get_album_art(self, size: float) -> QPixmap:
        """Returns crisp DPI-safe cached album art pixmap."""
        dpr = self.devicePixelRatio()
        key = ("album_art", self.dark_mode, dpr, int(size))
        if key in self._icon_cache:
            return self._icon_cache[key]

        px = int(size * dpr)

        if self.media_album_art and not self.media_album_art.isNull():
            scaled = self.media_album_art.scaled(
                px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            self._icon_cache[key] = scaled
            return scaled

        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)
        pix.setDevicePixelRatio(dpr)

        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Procedural Apple Music vinyl & gradient
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0.0, QColor("#8A2387"))
        grad.setColorAt(0.5, QColor("#E94057"))
        grad.setColorAt(1.0, QColor("#F27121"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6.0, 6.0)

        # Subtle vinyl rings
        p.setPen(QPen(QColor(255, 255, 255, 30), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = size / 2.0, size / 2.0
        p.drawEllipse(QPointF(cx, cy), size * 0.35, size * 0.35)
        p.drawEllipse(QPointF(cx, cy), size * 0.22, size * 0.22)

        # Music note
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 230)))
        note_r = size * 0.10
        p.drawEllipse(QPointF(cx - size * 0.06, cy + size * 0.08), note_r, note_r * 0.75)
        p.drawRect(QRectF(cx + size * 0.02, cy - size * 0.14, size * 0.05, size * 0.22))
        p.end()

        self._icon_cache[key] = pix
        return pix

    def _draw_marquee_text(self, p: QPainter, text: str, font: QFont, rect: QRectF,
                           color: QColor, elapsed: float) -> None:
        """Draws marquee scrolling text only if truncated, perfectly stationary otherwise."""
        p.setFont(font)
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(text)
        avail_w = rect.width()

        p.save()
        p.setClipRect(rect)

        if text_w <= avail_w:
            p.setPen(color)
            p.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        else:
            lead_pause = 1.5
            scroll_spd = 28.0
            gap = 40.0
            cycle = (text_w + gap) / scroll_spd + lead_pause
            phase = elapsed % cycle
            offset = 0.0 if phase < lead_pause else (phase - lead_pause) * scroll_spd

            p.setPen(color)
            y = rect.y() + (rect.height() + fm.ascent() - fm.descent()) / 2.0
            p.drawText(int(rect.x() - offset), int(y), text)
            p.drawText(int(rect.x() - offset + text_w + gap), int(y), text)

        p.restore()

    # ──────────────────────────────────────────────────────────────────────
    # Assistant State Renderers (Existing Apple HIG States)
    # ──────────────────────────────────────────────────────────────────────

    def _draw_wake(self, p: QPainter, w: float, h: float, cy: float,
                   t: float, dark: bool, primary: QColor, accent: QColor, CYAN: QColor) -> None:
        orb_cx, orb_cy = 28.0, cy
        orb_r = 11.0 + 2.0 * math.sin(t * 8.0)
        g_orb = QRadialGradient(orb_cx, orb_cy, orb_r)
        g_orb.setColorAt(0.0,  QColor(255, 255, 255, 255))
        g_orb.setColorAt(0.40, QColor(0,  240, 255, 240))
        g_orb.setColorAt(0.75, QColor(175, 82, 222, 220))
        g_orb.setColorAt(1.0,  QColor(255, 45,  85,  0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g_orb))
        p.drawEllipse(QPointF(orb_cx, orb_cy), orb_r, orb_r)

        text_x = 48.0
        title_font = qfont(TYPE.callout)
        title_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(title_font)
        p.setPen(primary)
        p.drawText(int(text_x), int(cy - 2), "Jarvis")

        sub_font = qfont(TYPE.caption_1)
        sub_font.setWeight(QFont.Weight.Medium)
        p.setFont(sub_font)
        p.setPen(CYAN)
        p.drawText(int(text_x), int(cy + 11), "Ready for directive")

    def _draw_listening(self, p: QPainter, w: float, h: float, cy: float,
                        t: float, now: float, dark: bool, primary: QColor, accent: QColor, reduce: bool) -> None:
        self._draw_siri_orb(p, orb_cx=28.0, orb_cy=cy, t=t, speed_mult=1.0)
        self._draw_waveform_bars(p, w, cy)

        text_x = 48.0
        max_text_w = max(60.0, w - text_x - 70.0)

        title_font = qfont(TYPE.caption_1)
        title_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(title_font)
        p.setPen(accent)
        title = "Processing..." if self.is_transcribing else "Listening..."
        p.drawText(int(text_x), int(cy - 3), title)

        self._draw_partial_transcript(p, text_x, cy + 11, max_text_w, now, dark, primary, reduce)

    def _draw_siri_orb(self, p: QPainter, orb_cx: float, orb_cy: float, t: float, speed_mult: float) -> None:
        blobs = [
            (QColor(0,   122, 255), 0.0),
            (QColor(175, 82,  222), math.pi * 0.5),
            (QColor(0,   240, 255), math.pi),
            (QColor(255, 45,  85),  math.pi * 1.5),
        ]

        for col, phase in blobs:
            angle = t * 2.2 * speed_mult + phase
            dist  = 3.2 + 1.6 * math.sin(t * 1.4 * speed_mult + phase)
            bx    = orb_cx + math.cos(angle) * dist
            by    = orb_cy + math.sin(angle) * dist
            br    = 6.5 + 2.0 * math.sin(t * 3.2 * speed_mult + phase * 1.5)

            g = QRadialGradient(bx, by, br)
            g.setColorAt(0.0, QColor(col.red(), col.green(), col.blue(), 230))
            g.setColorAt(0.55, QColor(col.red(), col.green(), col.blue(), 130))
            g.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(bx, by), br, br)

        core = QRadialGradient(orb_cx, orb_cy, 6.0)
        core.setColorAt(0.0, QColor(255, 255, 255, 240))
        core.setColorAt(0.6, QColor(255, 255, 255, 90))
        core.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(core))
        p.drawEllipse(QPointF(orb_cx, orb_cy), 6.0, 6.0)

    def _draw_waveform_bars(self, p: QPainter, w: float, cy: float) -> None:
        n       = 7
        bar_w   = 3.0
        gap     = 3.5
        total_w = n * bar_w + (n - 1) * gap
        wx0     = w - total_w - 18.0
        max_h   = 18.0

        colors = [
            QColor(175, 82, 222, 200),
            QColor(0,  240, 255, 220),
            QColor(0,  122, 255, 230),
            QColor(255, 255, 255, 240),
            QColor(0,  122, 255, 230),
            QColor(0,  240, 255, 220),
            QColor(175, 82, 222, 200),
        ]
        for i in range(n):
            bar_h = max(3.0, self._wave_bars[i] * max_h)
            bx    = wx0 + i * (bar_w + gap)
            by    = cy - bar_h / 2.0
            r     = bar_w / 2.0
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(colors[i]))
            p.drawRoundedRect(QRectF(bx, by, bar_w, bar_h), r, r)

    def _draw_partial_transcript(self, p: QPainter, x: float, y: float, max_w: float,
                                 now: float, dark: bool, primary: QColor, reduce: bool) -> None:
        if not self._transcript_words:
            p.setPen(T.color.secondary_label.resolve(dark))
            p.drawText(int(x), int(y), "Jarvis is ready")
            return

        font = qfont(TYPE.caption_1)
        font.setWeight(QFont.Weight.Medium)
        p.setFont(font)
        fm = p.fontMetrics()
        space_w = fm.horizontalAdvance(" ")

        cur_x = x
        for word, arrival_t in self._transcript_words:
            word_w = fm.horizontalAdvance(word)
            if cur_x + word_w > x + max_w:
                p.setPen(primary)
                p.drawText(int(cur_x), int(y), "…")
                break

            if reduce:
                alpha = 1.0
            else:
                age = now - arrival_t
                alpha = min(1.0, max(0.0, age / 0.140))

            col = QColor(primary)
            col.setAlphaF(alpha)
            p.setPen(col)
            p.drawText(int(cur_x), int(y), word)
            cur_x += word_w + space_w

    def _draw_thinking(self, p: QPainter, w: float, h: float, cy: float,
                       t: float, dark: bool, primary: QColor, accent: QColor, CYAN: QColor) -> None:
        self._draw_siri_orb(p, orb_cx=28.0, orb_cy=cy, t=t, speed_mult=2.8)

        text_x = 48.0
        top_font = qfont(TYPE.callout)
        top_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(top_font)
        p.setPen(accent)
        p.drawText(int(text_x), int(cy - 2), "Jarvis")

        sub_font = qfont(TYPE.caption_1)
        p.setFont(sub_font)
        p.setPen(primary)
        p.drawText(int(text_x), int(cy + 11), "Thinking...")

        rx = w - 42.0
        for i in range(3):
            phase = t * 7.0 - i * 0.9
            alpha = int(70 + 185 * max(0.0, math.sin(phase)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), alpha)))
            p.drawEllipse(QPointF(rx + i * 8, cy), 2.5, 2.5)

    def _draw_sending_indicator(self, p: QPainter, w: float, h: float, cy: float,
                                t: float, dark: bool, primary: QColor, accent: QColor, CYAN: QColor) -> None:
        """
        Draws the Island Sending Indicator (CARD_SENDING):
        - Leading side: pulsating aperture / camera-lens vector icon with soft cyan/amber security glow pulse.
        - Capsule: "Analyzing Screen" (13 pt) with subtle sweeping shimmer.
        - Explicit privacy awareness: user visibly sees when a snapshot is being processed.
        """
        reduce = T.reduce_motion()
        pulse = 0.5 + 0.5 * math.sin(t * 5.0) if not reduce else 0.5
        ix = 24.0

        # 1. Soft cyan / amber security glow pulse behind aperture
        if not reduce:
            glow_r = 15.0 + 3.0 * pulse
            rad = QRadialGradient(ix, cy, glow_r)
            c_cyan = QColor(0, 229, 255, int(85 + 55 * pulse))
            c_amber = QColor(255, 179, 64, int(65 + 45 * (1.0 - pulse)))
            rad.setColorAt(0.0, c_cyan)
            rad.setColorAt(0.55, c_amber)
            rad.setColorAt(1.0, QColor(0, 229, 255, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rad))
            p.drawEllipse(QPointF(ix, cy), glow_r, glow_r)

        # 2. Camera lens barrel outer rim
        r_barrel = 10.5
        barrel_pen = QPen(QColor(0, 229, 255, 200) if pulse > 0.5 else QColor(255, 179, 64, 200), 1.3)
        p.setPen(barrel_pen)
        p.setBrush(QBrush(QColor(12, 16, 24, 230 if dark else 190)))
        p.drawEllipse(QPointF(ix, cy), r_barrel, r_barrel)

        # 3. Inner lens housing ring
        r_inner_rim = 8.0
        p.setPen(QPen(QColor(255, 255, 255, 90 if dark else 130), 0.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(ix, cy), r_inner_rim, r_inner_rim)

        # 4. Pulsating aperture iris blades (6 blades opening/closing)
        r_iris = 2.6 + 1.2 * pulse
        p.setPen(QPen(QColor(200, 235, 255, 210 if dark else 170), 1.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        rot_offset = t * 1.5 if not reduce else 0.0
        for i in range(6):
            a = math.radians(i * 60.0 + rot_offset * 15.0)
            x1 = ix + r_iris * math.cos(a)
            y1 = cy + r_iris * math.sin(a)
            tangent = a + math.pi / 2.8
            x2 = x1 + (r_inner_rim - r_iris + 2.0) * math.cos(tangent)
            y2 = y1 + (r_inner_rim - r_iris + 2.0) * math.sin(tangent)
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # 5. Central aperture pupil & privacy security amber/cyan indicator dot
        pupil_r = 1.6 + 0.4 * pulse
        dot_col = QColor(255, 159, 10, int(220 + 35 * math.sin(t * 6.0))) if math.sin(t * 3.0) > 0 else QColor(0, 229, 255, int(220 + 35 * math.sin(t * 6.0)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(dot_col))
        p.drawEllipse(QPointF(ix, cy), pupil_r, pupil_r)

        # 6. Capsule label: "Analyzing Screen" (13 pt)
        tx = ix + 18.0
        lbl_font = qfont(TYPE.subheadline)
        lbl_font.setPointSize(13)
        lbl_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(lbl_font)
        p.setPen(primary)

        text_rect = QRectF(tx, 0, w - tx - 28.0, h)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.sending_label)

        # 7. Trailing privacy sensor dot (amber pulse, explicit privacy awareness)
        rx = w - 18.0
        sensor_alpha = int(140 + 100 * math.sin(t * 5.0)) if not reduce else 180
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 159, 10, sensor_alpha)))
        p.drawEllipse(QPointF(rx, cy), 2.5, 2.5)

    def _draw_result(self, p: QPainter, w: float, h: float, cy: float, dark: bool, primary: QColor) -> None:
        dpr = self.devicePixelRatio()
        icon_name = self.quick_answer_icon if (self.quick_answer_active and self.quick_answer_icon) else self.result_icon
        display_text = self.quick_answer_text if (self.quick_answer_active and self.quick_answer_text) else self.result_text
        icon_pix = self._get_icon_pixmap(icon_name, dark, dpr)
        ix = 24.0
        p.drawPixmap(QPointF(ix - 12.0, cy - 12.0), icon_pix)

        tx = ix + 18.0
        res_font = qfont(TYPE.subheadline)
        res_font.setPointSize(13)
        res_font.setWeight(QFont.Weight.DemiBold)
        try:
            res_font.setFeature(b"tnum", 1)
        except Exception:
            pass
        p.setFont(res_font)
        p.setPen(primary)

        text_rect = QRectF(tx, 0, w - tx - 16.0, h)
        metrics = p.fontMetrics()
        elided = metrics.elidedText(display_text, Qt.TextElideMode.ElideRight, int(w - tx - 16.0))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

    def _draw_error(self, p: QPainter, cy: float, primary: QColor, dark: bool) -> None:
        dpr = self.devicePixelRatio()
        icon_name = "alert" if self.is_ai_error else "error"
        icon_pix = self._get_icon_pixmap(icon_name, dark, dpr)
        ix = 24.0
        p.drawPixmap(QPointF(ix - 12.0, cy - 12.0), icon_pix)

        tx = ix + 18.0
        top_font = qfont(TYPE.footnote)
        top_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(top_font)
        err_col = QColor("#FF9F0A") if self.is_ai_error else QColor("#FF453A")
        p.setPen(err_col)
        p.drawText(int(tx), int(cy - 2), "Jarvis Alert")

        sub_font = qfont(TYPE.caption_1)
        p.setFont(sub_font)
        p.setPen(primary)
        p.drawText(int(tx), int(cy + 11), self.error_text[:46] or "Error")

    def _draw_prompt_speaking(self, p: QPainter, w: float, h: float, cy: float,
                              t: float, now: float, state: IslandState, dark: bool,
                              primary: QColor, accent: QColor, CYAN: QColor) -> None:
        dot_x = 18.0
        p.setPen(QPen(CYAN, 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(dot_x, cy), 5.5, 5.5)
        pulse = 0.65 + 0.35 * math.sin(t * 7.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 255)))
        p.drawEllipse(QPointF(dot_x, cy), 2.2 * pulse, 2.2 * pulse)

        tx = dot_x + 14.0
        top_font = qfont(TYPE.caption_1)
        top_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(top_font)
        p.setPen(CYAN if dark else accent)

        prompt_disp = self.user_prompt.strip()
        is_typing = (self.prompt_type_index < len(self.target_prompt)) or self.is_live_speech
        if is_typing and int(now * 5.0) % 2 == 0:
            prompt_disp = prompt_disp + "▍"
        elif len(prompt_disp) > 42:
            prompt_disp = prompt_disp[:39] + "…"
        p.drawText(int(tx), int(cy - 4), f'"{prompt_disp}"')

        sub_font = qfont(TYPE.caption_1)
        sub_font.setWeight(QFont.Weight.Medium)
        p.setFont(sub_font)

        if state == IslandState.SPEAKING and self.say_text:
            p.setPen(primary)
            disp = self.say_text if len(self.say_text) <= 44 else self.say_text[:41] + "…"
            p.drawText(int(tx), int(cy + 11), disp)
            self._draw_waveform_bars(p, w, cy)
        else:
            p.setPen(QColor("#30D158"))
            p.drawText(int(tx), int(cy + 11), "Directive confirmed")

    def _draw_gesture(self, p: QPainter, cy: float, primary: QColor, GOLD: QColor) -> None:
        dot_x = 18.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(GOLD))
        p.drawEllipse(QPointF(dot_x, cy), 4.0, 4.0)

        tx = dot_x + 14.0
        top_font = qfont(TYPE.callout)
        top_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(top_font)
        p.setPen(primary)
        p.drawText(int(tx), int(cy - 2), "Jarvis")

        sub_font = qfont(TYPE.caption_1)
        sub_font.setWeight(QFont.Weight.Medium)
        p.setFont(sub_font)
        p.setPen(GOLD)
        p.drawText(int(tx), int(cy + 11), self.label.title())

    def _draw_tracking(self, p: QPainter, w: float, h: float, cy: float,
                       dark: bool, primary: QColor, secondary: QColor, accent: QColor, state: IslandState) -> None:
        dot_x = 18.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#30D158")))
        p.drawEllipse(QPointF(dot_x, cy), 4.0, 4.0)

        tx = dot_x + 14.0
        top_font = qfont(TYPE.callout)
        top_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(top_font)
        p.setPen(primary)
        p.drawText(int(tx), int(cy - 2), "Jarvis")

        sub_font = qfont(TYPE.caption_1)
        sub_font.setWeight(QFont.Weight.Medium)
        p.setFont(sub_font)
        p.setPen(secondary)
        p.drawText(int(tx), int(cy + 11), "Tracking")

        if (self.active and self.landmarks and
                getattr(getattr(self.mgr, "cfg", None), "show_skeleton_mini", True) and
                w > 200.0):
            self._draw_mini_skeleton(p, w, h, cy, dark, accent, state)

    def _draw_mini_skeleton(self, p: QPainter, w: float, h: float, cy: float, dark: bool, accent: QColor, state: IslandState):
        skel_box = 24.0
        skel_x   = w - skel_box - 16.0
        skel_y   = cy - skel_box / 2.0

        hand_lm = self.landmarks[0]
        pts = [(skel_x + lm[0] * skel_box, skel_y + lm[1] * skel_box) for lm in hand_lm]

        bone_col = QColor(255, 255, 255, 75 if dark else 120)
        p.setPen(QPen(bone_col, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))

        rays = [(0, 4), (0, 8), (0, 12), (0, 16), (0, 20)]
        for a, b in rays:
            if a < len(pts) and b < len(pts):
                p.drawLine(QPointF(pts[a][0], pts[a][1]), QPointF(pts[b][0], pts[b][1]))

        if len(pts) > 8:
            is_hot = (state == IslandState.GESTURE)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(accent if is_hot else QColor(255, 255, 255, 160)))
            p.drawEllipse(QPointF(pts[8][0], pts[8][1]), 2.2, 2.2)

    def _draw_hud_volume(self, p: QPainter, w: float, h: float, cy: float, dark: bool,
                          primary: QColor, secondary: QColor, accent: QColor) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Leading Speaker Vector Icon (adapts to level)
        icon_x = 18.0
        icon_w = 20.0

        # Speaker body vector path
        sp_path = QPainterPath()
        sp_path.moveTo(icon_x, cy - 3.5)
        sp_path.lineTo(icon_x + 3.5, cy - 3.5)
        sp_path.lineTo(icon_x + 8.5, cy - 7.5)
        sp_path.lineTo(icon_x + 8.5, cy + 7.5)
        sp_path.lineTo(icon_x + 3.5, cy + 3.5)
        sp_path.lineTo(icon_x, cy + 3.5)
        sp_path.closeSubpath()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(primary))
        p.drawPath(sp_path)

        lvl = max(0, min(100, int(self.hud_volume_level)))
        muted = bool(self.hud_volume_muted or (lvl == 0))

        if muted:
            mute_pen = QPen(secondary, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(mute_pen)
            p.drawLine(QPointF(icon_x + 11.0, cy - 5.0), QPointF(icon_x + 17.0, cy + 5.0))
        else:
            wave_pen = QPen(primary, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(wave_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if lvl > 0:
                p.drawArc(QRectF(icon_x + 3.0, cy - 4.5, 9.0, 9.0), -45 * 16, 90 * 16)
            if lvl >= 33:
                p.drawArc(QRectF(icon_x + 3.0, cy - 7.5, 15.0, 15.0), -45 * 16, 90 * 16)
            if lvl >= 66:
                p.drawArc(QRectF(icon_x + 3.0, cy - 10.5, 21.0, 21.0), -45 * 16, 90 * 16)

        # 2. Trailing Tabular Percentage Label
        pct_text = f"{lvl}%"
        num_font = QFont(TYPE.body.family, 10, QFont.Weight.DemiBold)
        num_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        p.setFont(num_font)
        fm = p.fontMetrics()
        txt_w = fm.horizontalAdvance(pct_text)
        txt_x = w - 18.0 - txt_w
        p.setPen(primary)
        p.drawText(QRectF(txt_x, cy - 10.0, txt_w, 20.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, pct_text)

        # 3. Center Rounded Progress Bar in Apple Accent Color
        bar_x = icon_x + icon_w + 10.0
        bar_w = max(10.0, txt_x - 12.0 - bar_x)
        bar_h = 7.0
        bar_y = cy - bar_h / 2.0
        bar_r = bar_h / 2.0

        # Background track
        track_col = QColor(255, 255, 255, 45) if dark else QColor(0, 0, 0, 35)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_col))
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), bar_r, bar_r)

        # Smooth filled progress driven by spring
        fill_ratio = max(0.0, min(1.0, getattr(self.spring_hud_volume, "value", lvl / 100.0)))
        fill_w = max(0.0, bar_w * fill_ratio)
        if fill_w > 0.0:
            track_path = QPainterPath()
            track_path.addRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), bar_r, bar_r)
            fill_path = QPainterPath()
            fill_path.addRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), bar_r, bar_r)
            p.save()
            p.setClipPath(track_path)
            p.setBrush(QBrush(accent))
            p.drawPath(fill_path)
            p.restore()

        p.restore()

    def _draw_hud_window(self, p: QPainter, w: float, h: float, cy: float, dark: bool,
                          primary: QColor, secondary: QColor, accent: QColor) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1. Leading App Switcher Vector Icon
        icon_x = 18.0

        # Back window tile
        back_rect = QRectF(icon_x + 4.0, cy - 7.5, 12.0, 11.0)
        p.setPen(QPen(secondary, 1.2))
        p.setBrush(QBrush(QColor(255, 255, 255, 30) if dark else QColor(0, 0, 0, 20)))
        p.drawRoundedRect(back_rect, 2.5, 2.5)

        # Front window tile
        front_rect = QRectF(icon_x, cy - 4.0, 12.0, 11.0)
        p.setPen(QPen(accent, 1.4))
        front_bg = QColor(accent.red(), accent.green(), accent.blue(), 55)
        p.setBrush(QBrush(front_bg))
        p.drawRoundedRect(front_rect, 2.5, 2.5)

        # Front window header accent line
        p.setPen(QPen(accent, 1.2))
        p.drawLine(QPointF(icon_x, cy - 1.0), QPointF(icon_x + 12.0, cy - 1.0))

        # 2. Center Target Window Title (Truncated with Ellipsis)
        title_text = (self.hud_window_title or "Switch Window").strip()
        title_font = QFont(TYPE.body.family, 10, QFont.Weight.DemiBold)
        title_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        p.setFont(title_font)
        fm = p.fontMetrics()

        text_x = icon_x + 16.0 + 8.0
        avail_w = max(20.0, w - 18.0 - text_x)
        elided = fm.elidedText(title_text, Qt.TextElideMode.ElideRight, int(avail_w))

        p.setPen(primary)
        p.drawText(QRectF(text_x, 0.0, avail_w, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        p.restore()


# ──────────────────────────────────────────────────────────────────────────────
# Hidden Demo Mode Controller (--island-demo)
# ──────────────────────────────────────────────────────────────────────────────

class IslandDemoController:
    """
    Cycles smoothly through every Assistant state, Widget card, Live Activity, and Routine:
      0: Compact Time + Weather (2.5s)
      1: Expanded Weather Card (3.5s)
      2: Compact Media (Now Playing, 2.5s)
      3: Expanded Media Card (artwork, scrubber, buttons, 4.0s)
      4: Compact Timer (2.5s) - leading progress ring decreasing smoothly, trailing tabular numerals
      5: WAKE (1.2s) - Overshooting expansion & edge glow pulse
      6: LISTENING (4.2s) - Siri orb + live audio waveform + transcript word fade-in
      7: THINKING (2.0s) - Siri orb speeds up + shimmer sweep across capsule
      8: DISAMBIGUATION (3.8s) - Disambiguation Card ("Which one, sir?", chips, easing dwell ring resolving selection)
      9: RESULT (2.5s) - Morphs to taller card with 13 pt text + green tick badge
      10: Expanded Timer Card (3.5s) - large tabular countdown, live progress bar, end time, controls
      11: Routine State: Work Mode (2.8s) - multi-action automation trigger
      12: Routine State: Focus Mode (2.8s) - deep work sprint timer & playlist active
      13: Routine State: Evening Mode (2.8s) - ambient relaxation & daily summary
      14: COLLAPSE (1.2s) - Smoothly springs back toward IDLE
      15: WAKE (mid-collapse interruptibility test, 0.8s)
      16: NO_SPEECH (2.5s) - Silence timeout 'nope' horizontal wiggle collapse
      17: Screen Awareness Sending Indicator (2.8s) - pulsating aperture vector icon, cyan/amber security glow pulse, sweeping shimmer
      18: Quick Answer Card (3.2s) - globe/clock timezone ("Tokyo: 22:45 JST (+9 hrs)") with tabular numerals
      19: Gesture HUD: Volume (1.8s) - compact capsule, speaker glyph adapting to 65%, Apple accent fill
      20: Gesture HUD: Volume (1.8s) - smooth spring transition to 90% volume
      21: Gesture HUD: Window Switch (2.0s) - App switcher vector icon + "Visual Studio Code - island.py"
      22: Gesture HUD: Window Switch (2.0s) - App switcher vector icon + "Google Chrome - Apple Human Interface Guidelines"
      23: Auto-collapse back to Idle (1.5s)
    """

    def __init__(self, island: AppleDynamicIsland):
        self.island = island
        self.island._demo_mode = True
        self.step = 0
        self.step_start = time.time()
        self.timer = QTimer()
        self.timer.timeout.connect(self._step_tick)
        self.active = False

    def start(self) -> None:
        self.active = True
        self.step = 0
        self.step_start = time.time()
        self._apply_step(0)
        self.timer.start(50)

    def stop(self) -> None:
        self.active = False
        self.timer.stop()

    def _step_tick(self) -> None:
        if not self.active:
            return
        now = time.time()
        elapsed = now - self.step_start
        durations = [2.5, 3.5, 2.5, 4.0, 2.5, 1.2, 4.2, 2.0, 3.8, 2.5, 3.5, 2.8, 2.8, 2.8, 1.2, 0.8, 2.5, 2.8, 3.2, 1.8, 1.8, 2.0, 2.0, 1.5]

        # Feed audio & transcript during LISTENING
        if self.step == 6:
            self._feed_listening(elapsed)

        # Feed simulated dwell & resolve during DISAMBIGUATION
        if self.step == 8 and self.island.disambiguate_active:
            if elapsed < 0.6:
                self.island._hovered_chip_idx = None
                self.island._chip_dwell_elapsed = 0.0
            elif elapsed < 1.8:
                self.island._hovered_chip_idx = 0
                self.island._chip_dwell_elapsed = min(1.0, (elapsed - 0.6) / 1.0)
            elif elapsed >= 1.8:
                self.island.select_candidate(0)

        if elapsed >= durations[self.step % len(durations)]:
            self.step = (self.step + 1) % len(durations)
            self.step_start = now
            self._apply_step(self.step)

    def _feed_listening(self, elapsed: float) -> None:
        now = time.time()
        try:
            from voice import Voice
            Voice._latest_time = now
            Voice._latest_rms = 160.0 + 130.0 * math.sin(elapsed * 7.5)
        except Exception:
            pass

        sample_words = "Jarvis what is the current weather forecast for today".split()
        num_words = min(len(sample_words), int(elapsed / 0.42) + 1)
        self.island.set_partial_transcript(" ".join(sample_words[:num_words]), now)

    def _apply_step(self, step: int) -> None:
        now = time.time()
        if step == 0:
            # Compact Time + Weather
            self.island.reset_to_idle()
            self.island.compact_mode_override = "time_weather"
        elif step == 1:
            # Expanded Weather Card
            self.island.expand_card("weather")
        elif step == 2:
            # Compact Media
            self.island.collapse_card()
            self.island.compact_mode_override = "media"
            self.island.media_active = True
            self.island.media_playing = True
        elif step == 3:
            # Expanded Media Card
            self.island.expand_card("media")
        elif step == 4:
            # Compact Timer
            self.island.collapse_card()
            self.island.compact_mode_override = "timer"
            self.island.timer_active = True
            self.island.timer_duration_sec = 300.0
            self.island.timer_end_time = now + 272.0
        elif step == 5:
            # Assistant WAKE
            self.island.compact_mode_override = None
            self.island.timer_active = False
            self.island.trigger_wake(duration=1.2)
        elif step == 6:
            # Assistant LISTENING
            self.island.wake_until = 0.0
            self.island.listening = True
            self.island.listening_until = now + 5.0
            self.island.set_partial_transcript("Jarvis", now)
        elif step == 7:
            # Assistant THINKING
            self.island.listening = False
            self.island.listening_until = 0.0
            self.island.thinking = True
            self.island.thinking_until = now + 2.5
        elif step == 8:
            # Disambiguation Card UI
            self.island.thinking = False
            self.island.thinking_until = 0.0
            candidates = [
                {"title": "Spotify", "action": "open_spotify"},
                {"title": "SoundCloud", "action": "open_soundcloud"},
                {"title": "Apple Music", "action": "open_apple_music"}
            ]
            self.island.trigger_disambiguate(candidates, header="Which one, sir?", duration=6.0)
        elif step == 9:
            # Assistant RESULT
            self.island.thinking = False
            self.island.thinking_until = 0.0
            self.island.trigger_result("Spotify selected", icon="success", duration=2.5)
        elif step == 10:
            # Expanded Timer Card
            self.island.collapse_card()
            self.island.compact_mode_override = "timer"
            self.island.timer_active = True
            self.island.timer_duration_sec = 300.0
            self.island.timer_end_time = now + 268.0
            self.island.expand_card("timer")
        elif step == 11:
            # Routine state: Work Mode
            self.island.collapse_card()
            self.island.timer_active = False
            self.island.compact_mode_override = None
            self.island.trigger_result("Work Mode: Workspace ready", icon="work", duration=2.8)
        elif step == 12:
            # Routine state: Focus Mode
            self.island.trigger_result("Focus Mode: 45m sprint active", icon="focus", duration=2.8)
        elif step == 13:
            # Routine state: Evening Mode
            self.island.trigger_result("Evening Mode: Wind down set", icon="evening", duration=2.8)
        elif step == 14:
            # COLLAPSE
            self.island.reset_to_idle()
        elif step == 15:
            # Re-expand mid-collapse smoothly
            self.island.trigger_wake(duration=0.8)
        elif step == 16:
            # NO_SPEECH
            self.island.wake_until = 0.0
            self.island.trigger_no_speech()
        elif step == 17:
            # Screen Awareness Sending Indicator (CARD_SENDING)
            self.island.reset_to_idle()
            self.island.set_sending_indicator(active=True, label="Analyzing Screen", timeout=2.8)
        elif step == 18:
            # Quick Answer Result (CARD_RESULT: Timezone with tabular numerals & globe icon)
            self.island.trigger_quick_answer("Tokyo: 22:45 JST (+9 hrs)", icon="globe", duration=3.2)
        elif step == 19:
            # Gesture HUD: Volume HUD at 65% (Apple accent progress bar, speaker icon, tabular label)
            self.island.reset_to_idle()
            self.island.trigger_hud_volume(level=65, muted=False, duration=1.8)
        elif step == 20:
            # Gesture HUD: Volume HUD dynamically updated to 90%
            self.island.trigger_hud_volume(level=90, muted=False, duration=1.8)
        elif step == 21:
            # Gesture HUD: Window Switch HUD (VS Code)
            self.island.trigger_hud_window(title="Visual Studio Code - island.py", duration=2.0)
        elif step == 22:
            # Gesture HUD: Window Switch HUD (Browser with longer title testing ellipsis)
            self.island.trigger_hud_window(title="Google Chrome - Apple Human Interface Guidelines Design Tokens", duration=2.0)
        elif step == 23:
            # Auto-collapses back to IDLE
            self.island.reset_to_idle()


# ──────────────────────────────────────────────────────────────────────
# Main execution & Standalone Demo
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    island = AppleDynamicIsland(overlay_mgr=None)
    island.show()
    island.make_click_through()

    # 60 FPS animation timer
    frame_timer = QTimer()
    frame_timer.timeout.connect(lambda: island.update_frame(time.time()))
    frame_timer.start(16)

    demo = IslandDemoController(island)
    demo.start()
    print("[JARVIS] Dynamic Island running in --island-demo mode. Press Ctrl+C or close window.")

    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        pass
