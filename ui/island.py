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
import datetime
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
    SPRING_DT_MIN, SPRING_DT_MAX, RADIUS, GEOMETRY
)
from ui.materials import apply_acrylic, make_click_through, set_interactive
from ui.components import Spring, create_squircle_path

# ──────────────────────────────────────────────────────────────────────────────
# Squircle Path Cache (60 FPS Performance, Zero Heap Allocations)
# ──────────────────────────────────────────────────────────────────────────────

_PATH_CACHE: dict[tuple[float, float, float, float, float], QPainterPath] = {}

def create_cached_squircle_path(rect: QRectF, radius: float) -> QPainterPath:
    """Returns a cached QPainterPath for continuous squircle geometry."""
    key = (
        round(rect.x(), 1),
        round(rect.y(), 1),
        round(rect.width(), 1),
        round(rect.height(), 1),
        round(radius, 1)
    )
    path = _PATH_CACHE.get(key)
    if path is None:
        if len(_PATH_CACHE) > 512:
            _PATH_CACHE.clear()
        path = create_squircle_path(rect, radius)
        _PATH_CACHE[key] = path
    return path


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
    CARD_MEDIA          = auto()
    CARD_WEATHER        = auto()
    CARD_TIME           = auto()
    CARD_HARDWARE_GRID  = auto()
    CARD_HARDWARE_STRIP = auto()
    CARD_JARVIS         = auto()
    CARD_DISAMBIGUATE   = auto()
    CARD_TIMER          = auto()
    CARD_SENDING        = auto()
    CARD_RESULT         = auto()
    # Gesture HUD States
    HUD_VOLUME          = auto()
    HUD_WINDOW          = auto()


# ──────────────────────────────────────────────────────────────────────────────
# Target sizes per state (w, h)
# ──────────────────────────────────────────────────────────────────────────────

_SIZE: dict[IslandState, tuple[float, float]] = {
    IslandState.IDLE:                (175.0,  36.0),
    IslandState.WAKE:                (280.0,  44.0),
    IslandState.TRACKING:            (210.0,  36.0),
    IslandState.GESTURE:             (270.0,  40.0),
    IslandState.LISTENING:           (390.0,  54.0),
    IslandState.THINKING:            (320.0,  46.0),
    IslandState.PROMPT:              (360.0,  48.0),   # dynamically widened if needed
    IslandState.SPEAKING:            (380.0,  50.0),   # dynamically widened if needed
    IslandState.RESULT:              (380.0,  58.0),   # dynamically widened if needed
    IslandState.NO_SPEECH:           (175.0,  36.0),
    IslandState.ERROR:               (340.0,  48.0),
    IslandState.CARD_MEDIA:          (390.0, 188.0),
    IslandState.CARD_WEATHER:        (390.0, 188.0),
    IslandState.CARD_TIME:           (390.0, 188.0),
    IslandState.CARD_HARDWARE_GRID:  (390.0, 188.0),
    IslandState.CARD_HARDWARE_STRIP: (446.0,  69.0),
    IslandState.CARD_JARVIS:         (390.0, 188.0),
    IslandState.CARD_DISAMBIGUATE:   (440.0,  90.0),
    IslandState.CARD_TIMER:          (380.0, 150.0),
    IslandState.CARD_SENDING:        (300.0,  44.0),
    IslandState.CARD_RESULT:         (380.0,  58.0),
    IslandState.HUD_VOLUME:          (260.0,  38.0),
    IslandState.HUD_WINDOW:          (290.0,  38.0),
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
        self.spring_w    = Spring(175.0, SPRING_K_RESP04, SPRING_C_DAMP07)
        self.spring_h    = Spring(36.0,  SPRING_K_RESP04, SPRING_C_DAMP07)
        self.spring_prog = Spring(0.0,   240.0, 24.0)
        self.spring_glow = Spring(0.0,   SPRING_K_RESP04, SPRING_C_DAMP07)
        self.spring_hud_volume = Spring(0.5, SPRING_K_RESP04, SPRING_C_DAMP07)

        # Content cross-fade spring
        self._opacity_spring = Spring(1.0, 180.0, 18.0)

        # Expanded card paging & pin open
        self.pin_open: bool = False
        self.card_pages: list[str] = ["media", "time", "weather", "hardware", "jarvis"]

        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        style_val = getattr(cfg, "island_shape_style", getattr(self.mgr, "shape_style", "pill")) if (cfg or self.mgr) else "pill"
        if hasattr(style_val, "value"):
            style_val = style_val.value
        self.shape_style: str = str(style_val).lower().strip() if style_val else "pill"

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
        self.thinking_status: Optional[str] = None
        self._thinking_start_t: float = 0.0
        self._listening_start_t: float = 0.0
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
        self.weather_unit_symbol: str = "°F"
        self.weather_condition: str = "Partly Cloudy"
        self.weather_icon: str = "partly_cloudy"
        self.weather_high: float = 76.0
        self.weather_low: float = 58.0
        self.weather_feels_like: float = 71.0
        self.weather_city: str = "Ahmedabad"
        self.weather_wind_speed: float = 12.0
        self.weather_wind_direction: str = "NW"
        self.weather_humidity: int = 48
        self.weather_hourly: list[dict] = [
            {"time": "Now",  "temp": 72.0, "icon": "partly_cloudy"},
            {"time": "1 PM", "temp": 74.0, "icon": "sunny"},
            {"time": "2 PM", "temp": 75.0, "icon": "sunny"},
            {"time": "3 PM", "temp": 73.0, "icon": "cloudy"},
            {"time": "4 PM", "temp": 70.0, "icon": "rain"},
            {"time": "5 PM", "temp": 68.0, "icon": "partly_cloudy"},
        ]

        # Hardware metrics state & lifecycle
        self._cached_hardware_metrics: Optional[Any] = None

        # Media state
        self.media_active: bool = False
        self.media_playing: bool = False
        self.media_title: str = "Blinding Lights"
        self.media_artist: str = "The Weeknd"
        self.media_album: str = "After Hours"
        self.media_source: str = "Spotify"
        self.media_duration_sec: float = 200.0
        self.media_position_sec: float = 84.0
        self.media_progress: float = 84.0 / 200.0
        self.media_album_art: Optional[QPixmap] = None
        self.media_dominant_color: QColor = QColor(175, 82, 222)
        self.media_average_color: Optional[QColor] = None
        self.media_secondary_color: Optional[QColor] = None
        self._media_bars: list[float] = [0.35, 0.75, 0.50, 0.85]
        self._dragging_scrubber: bool = False

        # Calendar state
        self.calendar_date: Optional[datetime.date] = None
        self._cached_calendar_data = None
        self._cached_calendar_key = None

        # Timer state
        self.timer_active: bool = False
        self.timer_duration_sec: float = 300.0
        self.timer_end_time: float = 0.0

        # Expanded Card & Interactivity
        self.expanded_card: Optional[str] = None  # "weather", "media", "time"
        self.expanded_until: float = 0.0
        self._card_expand_t: float = 0.0
        self.is_interactive: bool = False
        self._opened_via_hover: bool = False
        self._hover_leave_t: float = 0.0
        self._hover_enter_t: float = 0.0
        self._hovered_btn: Optional[str] = None
        self._btn_dwell_elapsed: float = 0.0
        self._btn_flash_until: float = 0.0
        self._prev_active_gesture: str = ""
        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        self.waveform_color: str = str(getattr(cfg, "island_waveform_color", "blue")) if cfg else "blue"

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

        # Repaint consolidation & dirty region tracking
        self._is_hovering: bool = False
        self._prev_is_hovering: bool = False
        self._prev_springs_active: bool = False
        self._last_rendered_state: Optional[IslandState] = None
        self._last_rendered_dark: Optional[bool] = None
        self._last_rendered_min: Optional[int] = None
        self._last_rendered_temp: Optional[float] = None
        self._dirty: bool = True

        self.resize(175, 36)
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

    def set_shape_style(self, style: str) -> None:
        """Dynamically switch between 'pill' (floating stadium capsule) and 'notch' (bezel-attached)."""
        val = str(getattr(style, "value", style)).lower().strip()
        if val in ("pill", "notch"):
            self.shape_style = val
            if hasattr(self.mgr, "shape_style"):
                try:
                    from island_manager import IslandShapeStyle
                    self.mgr.shape_style = IslandShapeStyle.from_str(val)
                except Exception:
                    pass
            self._center_top()
            self.update()

    def reload_config(self) -> None:
        """Live apply settings without restart."""
        self.dark_mode, _ = self._read_theme()
        self._icon_cache.clear()
        self._cached_calendar_data = None
        self._cached_calendar_key = None

        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        if cfg:
            shape_style = getattr(cfg, "island_shape_style", getattr(self.mgr, "shape_style", None))
            if shape_style:
                val = str(getattr(shape_style, "value", shape_style)).lower().strip()
                if val in ("pill", "notch"):
                    self.shape_style = val
                    if hasattr(self.mgr, "shape_style"):
                        try:
                            from island_manager import IslandShapeStyle
                            self.mgr.shape_style = IslandShapeStyle.from_str(val)
                        except Exception:
                            pass
            city = getattr(cfg, "island_weather_city", getattr(cfg, "weather_city", None))
            if city:
                self.weather_city = city
            self.pin_open = bool(getattr(cfg, "island_pin_open", self.pin_open))
            pages = getattr(cfg, "island_pages", ["media", "calendar", "weather", "hardware", "jarvis"])
            enabled_map = getattr(cfg, "island_pages_enabled", {})
            active_pages = []
            for p in pages:
                canon = "time" if p == "calendar" else p
                if enabled_map.get(p, True) and enabled_map.get(canon, True):
                    if canon not in active_pages:
                        active_pages.append(canon)
            if active_pages:
                self.card_pages = active_pages

            hw_layout = getattr(cfg, "island_hardware_layout", "grid")
            if self.expanded_card in ("hardware", "hardware_grid", "hardware_strip"):
                self.expanded_card = "hardware_strip" if hw_layout == "strip" else "hardware_grid"

            wf_color = getattr(cfg, "island_waveform_color", None)
            if wf_color:
                self.waveform_color = str(wf_color)

            if sys.platform == "win32" and hasattr(self, "winId"):
                try:
                    from ui.materials import apply_acrylic, remove_backdrop
                    if getattr(cfg, "island_backdrop_blur", False):
                        apply_acrylic(int(self.winId()), dark=self.dark_mode)
                    else:
                        remove_backdrop(int(self.winId()))
                except Exception:
                    pass

            now = time.time()
            st = self._resolve_state(now)
            tw, th = self._target_size(st, now)
            self.spring_w.set_target(tw)
            self.spring_h.set_target(th)
            self._center_top()
            self.update()

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
        """Apply WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE and remove DWM rectangular backdrop."""
        self.set_interactive(False)
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            from ui.materials import apply_acrylic, remove_backdrop
            cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
            if getattr(cfg, "island_backdrop_blur", False):
                apply_acrylic(hwnd, self.dark_mode)
            else:
                remove_backdrop(hwnd)
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
                
                # Floating pill floats below top bezel; notch attaches flush to top bezel
                is_pill = getattr(self, "shape_style", "pill") == "pill"
                top_margin = 10 if is_pill else 0
                cy = geom.y() + top_margin
                
                cur = self.geometry()
                if cur.x() != cx or cur.y() != cy or cur.width() != w or cur.height() != h:
                    self.setGeometry(cx, cy, w, h)
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
        """Expand the island into an interactive card (weather, media, time, hardware, jarvis)."""
        self.disambiguate_active = False
        self.disambiguate_candidates = []
        self.disambiguate_callback = None
        self._hovered_chip_idx = None
        self._chip_dwell_elapsed = 0.0
        c = card_name.lower()
        if c == "calendar":
            c = "time"
        elif c in ("hardware", "hardware_grid"):
            cfg = getattr(self.mgr, "cfg", None)
            if getattr(cfg, "island_hardware_layout", "grid") == "strip":
                c = "hardware_strip"
            else:
                c = "hardware_grid"
        elif c in ("jarvis", "jarvis_card"):
            c = "jarvis"
        self.expanded_card = c
        now = time.time()
        self.expanded_until = now + 4.0
        self._card_expand_t = now
        self.set_interactive(True)
        self._sync_hardware_adapter_lifecycle(now)
        self.spring_w.vel = max(self.spring_w.vel, 60.0)
        self.spring_h.vel = max(self.spring_h.vel, 60.0)

    def collapse_card(self) -> None:
        """Collapse expanded card back to compact pill."""
        self.expanded_card = None
        self.expanded_until = 0.0
        self._opened_via_hover = False
        self._hover_leave_t = 0.0
        self.set_interactive(False)
        self._sync_hardware_adapter_lifecycle(time.time())

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
            if self._listening_start_t == 0.0:
                self._listening_start_t = now
            return IslandState.LISTENING
        else:
            self._listening_start_t = 0.0

        if self.thinking and now < self.thinking_until:
            if self._thinking_start_t == 0.0:
                self._thinking_start_t = now
            if self.user_prompt:
                return IslandState.PROMPT
            return IslandState.THINKING
        else:
            self._thinking_start_t = 0.0
        if now < self.prompt_until and self.user_prompt:
            if now < self.say_until:
                return IslandState.SPEAKING
            return IslandState.PROMPT
        if now < self.say_until:
            return IslandState.SPEAKING
        if self.label in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT", "SCROLL"):
            return IslandState.GESTURE
        # Expanded Cards
        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        is_pinned = getattr(self, "pin_open", False) or getattr(cfg, "island_pin_open", False)
        if self.expanded_card and (is_pinned or now < self.expanded_until):
            if self.expanded_card == "media":
                return IslandState.CARD_MEDIA
            elif self.expanded_card == "weather":
                return IslandState.CARD_WEATHER
            elif self.expanded_card in ("time", "calendar"):
                return IslandState.CARD_TIME
            elif self.expanded_card in ("hardware", "hardware_grid"):
                return IslandState.CARD_HARDWARE_GRID
            elif self.expanded_card == "hardware_strip":
                return IslandState.CARD_HARDWARE_STRIP
            elif self.expanded_card in ("jarvis", "jarvis_card"):
                return IslandState.CARD_JARVIS
            elif self.expanded_card == "timer":
                return IslandState.CARD_TIMER
        if self.active:
            return IslandState.TRACKING
        return IslandState.IDLE

    def _get_size_scale(self) -> float:
        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        size_mode = getattr(cfg, "island_size_scale", getattr(cfg, "island_size", "medium")).lower()
        if size_mode in ("compact", "small"):
            return 0.85
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
            IslandState.IDLE, IslandState.CARD_MEDIA, IslandState.CARD_WEATHER,
            IslandState.CARD_TIME, IslandState.CARD_DISAMBIGUATE, IslandState.CARD_TIMER,
            IslandState.CARD_HARDWARE_GRID, IslandState.CARD_HARDWARE_STRIP, IslandState.CARD_JARVIS
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
            return (390.0 * scale, 188.0 * scale)
        elif state == IslandState.CARD_WEATHER:
            return (390.0 * scale, 188.0 * scale)
        elif state == IslandState.CARD_TIME:
            return (390.0 * scale, 188.0 * scale)
        elif state == IslandState.CARD_HARDWARE_GRID:
            return (390.0 * scale, 188.0 * scale)
        elif state == IslandState.CARD_HARDWARE_STRIP:
            return (446.0 * scale, 69.0 * scale)
        elif state == IslandState.CARD_JARVIS:
            return (390.0 * scale, 188.0 * scale)
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
        elif state == IslandState.THINKING:
            cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
            ui_status_line = getattr(cfg, "ui_status_line_enabled", True)
            elapsed = now - self._thinking_start_t if self._thinking_start_t > 0.0 else 0.0
            tw = 320.0
            if ui_status_line and elapsed > 1.5:
                status_txt = getattr(self, "thinking_status", None) or getattr(self, "status_text", None) or "Consulting Groq LLM..."
                tw = min(480.0, max(320.0, 60.0 + len(status_txt) * 7.2 + 50.0))
            return (tw * scale, 46.0 * scale)
        elif state == IslandState.IDLE:
            if self.compact_mode_override == "time_weather":
                return (175.0 * scale, 32.0 * scale)
            compact_mode = self._get_current_compact_mode()
            if compact_mode == "media":
                return (186.0 * scale, 32.0 * scale)
            elif compact_mode == "timer":
                return (152.0 * scale, 32.0 * scale)
            elif compact_mode == "time_weather":
                return (175.0 * scale, 36.0 * scale)
            elif compact_mode == "time":
                return (132.0 * scale, 32.0 * scale)
            else:
                return (175.0 * scale, 36.0 * scale)
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
        self._sync_hardware_adapter_lifecycle(now)

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
        if state == IslandState.LISTENING or self.listening or any(b > 0.01 for b in self._wave_bars):
            self._update_wave_bars(now)

        self._center_top()

        # ── Redundant Repaint Suppression ────────────────────────────────
        springs_active = not (
            self.spring_w.settled and
            self.spring_h.settled and
            self.spring_prog.settled and
            self.spring_glow.settled and
            self.spring_hud_volume.settled and
            self._opacity_spring.settled
        )

        state_changed = (state != getattr(self, "_last_rendered_state", None))
        theme_changed = (self.dark_mode != getattr(self, "_last_rendered_dark", None))
        springs_moving = springs_active or getattr(self, "_prev_springs_active", False)
        hover_active = getattr(self, "_is_hovering", False) or getattr(self, "_prev_is_hovering", False) or (self._hover_enter_t > 0.0)
        dwell_active = (self._hovered_btn is not None or self._hovered_chip_idx is not None or now < getattr(self, "_btn_flash_until", 0.0))

        anim_active = (
            state in (
                IslandState.WAKE,
                IslandState.LISTENING,
                IslandState.THINKING,
                IslandState.CARD_SENDING,
                IslandState.SPEAKING,
            )
            or self.no_speech_active
            or (self.target_prompt and self.user_prompt != self.target_prompt)
            or self.media_playing
            or (state == IslandState.CARD_MEDIA and any(abs(b - 0.15) > 0.01 for b in self._media_bars))
            or (state in (IslandState.CARD_TIMER, IslandState.IDLE) and self.timer_active and not getattr(self, "timer_paused", False))
            or (state == IslandState.TRACKING and self.active and self.landmarks)
            or (self.disambiguate_active and bool(self.disambiguate_candidates))
            or getattr(self, "_dirty", False)
        )
        if reduce and (now - self._crossfade_start < (REDUCE_MOTION_MS / 1000.0)):
            anim_active = True

        clock_tick = False
        if state == IslandState.IDLE:
            cm = self._get_current_compact_mode()
            if cm in ("time", "time_weather"):
                current_min = int(now) // 60
                if current_min != getattr(self, "_last_rendered_min", None) or self.weather_temp != getattr(self, "_last_rendered_temp", None):
                    clock_tick = True
                    self._last_rendered_min = current_min
                    self._last_rendered_temp = self.weather_temp
            elif cm == "media":
                if any(abs(b - 0.15) > 0.01 for b in self._media_bars):
                    anim_active = True

        needs_repaint = (
            state_changed or
            theme_changed or
            springs_moving or
            hover_active or
            dwell_active or
            anim_active or
            clock_tick
        )

        if needs_repaint:
            self.update()
            self._last_rendered_state = state
            self._last_rendered_dark = self.dark_mode
            self._dirty = False

        self._prev_springs_active = springs_active
        self._prev_is_hovering = getattr(self, "_is_hovering", False)

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
        self._is_hovering = is_hovering

        # When cursor hovers over the island, remove WS_EX_TRANSPARENT so mouse clicks can peek/interact!
        if is_hovering and not self.is_interactive:
            self.set_interactive(True)
        elif not is_hovering and self.expanded_card is None and not self.disambiguate_active and self.is_interactive:
            self.set_interactive(False)

        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
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

        # 1. Compact -> Expansion triggers (Hover peek / Fist-click)
        elif self.expanded_card is None and not self.is_wake and not self.listening and not self.thinking:
            if is_hovering:
                if self._hover_enter_t == 0.0:
                    self._hover_enter_t = now
                thresh = 0.25 if expand_trig == "hover" else 1.0
                if now - self._hover_enter_t >= thresh:
                    cm = self._get_current_compact_mode()
                    if self.media_playing or self.media_active or getattr(self, "media_title", ""):
                        card = "media"
                    elif cm == "timer" or self.timer_active:
                        card = "timer"
                    else:
                        card = "weather"
                    self._opened_via_hover = True
                    self._hover_leave_t = 0.0
                    self.expand_card(card)
                    self._hover_enter_t = 0.0
            else:
                self._hover_enter_t = 0.0

            # Hand-tracking fist-click trigger
            if is_hovering and self.label == "CLICK" and self._prev_active_gesture != "CLICK":
                cm = self._get_current_compact_mode()
                if self.media_playing or self.media_active or getattr(self, "media_title", ""):
                    card = "media"
                elif cm == "timer" or self.timer_active:
                    card = "timer"
                else:
                    card = "weather"
                self._opened_via_hover = False
                self.expand_card(card)

        # 2. Expanded Card Interaction & Auto-collapse
        elif self.expanded_card:
            is_pinned = getattr(self, "pin_open", False) or getattr(cfg, "island_pin_open", False)

            # Hover-peek auto-collapse: when opened via hover, collapse shortly after cursor leaves
            if self._opened_via_hover and not is_pinned:
                if is_hovering:
                    self._hover_leave_t = 0.0
                    self.expanded_until = max(self.expanded_until, now + 1.5)
                else:
                    if self._hover_leave_t == 0.0:
                        self._hover_leave_t = now
                    elif now - self._hover_leave_t >= 0.35:
                        self.collapse_card()
                        return
            elif not is_pinned and now >= self.expanded_until:
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
                            self._opened_via_hover = False
                    else:
                        self._hovered_btn = btn
                        self._btn_dwell_elapsed = 0.0

                    # Fist-click gesture over button
                    if self.label == "CLICK" and self._prev_active_gesture != "CLICK":
                        self._trigger_button(btn)
                        self._btn_dwell_elapsed = 0.0
                        self._btn_flash_until = now + 0.25
                        self._opened_via_hover = False
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
        self._opened_via_hover = False

        if self.expanded_card:
            dot_idx = self._dot_at(pos)
            if dot_idx is not None and dot_idx < len(self.card_pages):
                self.expand_card(self.card_pages[dot_idx])
                return

        if self.expanded_card == "media":
            btn = self._button_at(pos)
            if btn:
                self._trigger_button(btn)
                self._btn_flash_until = now + 0.25
                return

            if self._scrubber_hit_rect().contains(pos):
                sr = self._scrubber_rect()
                ratio = min(1.0, max(0.0, (pos.x() - sr.x()) / sr.width()))
                self.media_progress = ratio
                self.media_position_sec = ratio * self.media_duration_sec
                self._dragging_scrubber = True
                self.update()
                return

        elif self.expanded_card in ("timer", "jarvis"):
            btn = self._button_at(pos)
            if btn:
                self._trigger_button(btn)
                self._btn_flash_until = now + 0.25
                return

        elif self.expanded_card is None:
            cm = self._get_current_compact_mode()
            if self.media_playing or self.media_active or getattr(self, "media_title", ""):
                card = "media"
            elif cm == "timer" or self.timer_active:
                card = "timer"
            else:
                card = "weather"
            self._opened_via_hover = False
            self.expand_card(card)

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.disambiguate_active and self.disambiguate_candidates:
            self.disambiguate_until = max(self.disambiguate_until, time.time() + 4.0)
            self._hovered_chip_idx = self._chip_at(event.position())
            super().mouseMoveEvent(event)
            return

        if getattr(self, "_dragging_scrubber", False) and (event.buttons() & Qt.MouseButton.LeftButton):
            sr = self._scrubber_rect()
            ratio = min(1.0, max(0.0, (event.position().x() - sr.x()) / sr.width()))
            self.media_progress = ratio
            self.media_position_sec = ratio * self.media_duration_sec
            self.expanded_until = time.time() + 4.0
            self.update()
            super().mouseMoveEvent(event)
            return

        self.expanded_until = time.time() + 4.0
        if self.expanded_card in ("media", "timer", "jarvis"):
            self._hovered_btn = self._button_at(event.position())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._dragging_scrubber = False
        super().mouseReleaseEvent(event)

    def _button_at(self, pos: QPointF | QPoint) -> Optional[str]:
        w = max(float(self.width()), self.spring_w.target)
        scale = self._get_size_scale()

        if self.expanded_card in ("jarvis", "jarvis_card"):
            lx = 22.0 * scale
            content_w = (w - 28.0 * scale) - lx
            ty1 = 38.0 * scale
            th1 = 66.0 * scale
            gap = 8.0 * scale
            status_w = 202.0 * scale
            btn_w = content_w - status_w - gap
            btn_x = lx + status_w + gap
            if QRectF(btn_x, ty1, btn_w, th1).contains(pos):
                return "jarvis_toggle_tracking"
            return None

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
        sy = 96.0 * scale
        sw = w - 48.0 * scale
        sh = 4.0 * scale
        return QRectF(sx, sy, sw, sh)

    def _scrubber_hit_rect(self) -> QRectF:
        sr = self._scrubber_rect()
        scale = self._get_size_scale()
        return QRectF(sr.x() - 4.0 * scale, sr.y() - 10.0 * scale, sr.width() + 8.0 * scale, sr.height() + 20.0 * scale)

    def _trigger_button(self, btn: str) -> None:
        self.expanded_until = time.time() + 4.0
        if btn == "jarvis_toggle_tracking":
            is_paused = getattr(self, "tracking_paused", False)
            self.tracking_paused = not is_paused
            self.active = not self.tracking_paused
            if hasattr(self.mgr, "toggle_tracking"):
                self.mgr.toggle_tracking()
            elif hasattr(self.mgr, "set_tracking_paused"):
                self.mgr.set_tracking_paused(self.tracking_paused)
            self.update()
        elif btn == "play_pause":
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
        success = False
        try:
            from media_adapter import get_media_adapter
            adapter = get_media_adapter()
            if self.media_playing:
                success = adapter.play()
            else:
                success = adapter.pause()
        except Exception as exc:
            print(f"[island] media_toggle_play error: {exc}")

        if not success and sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
            except Exception:
                pass
        self.update()

    def media_prev(self) -> None:
        success = False
        try:
            from media_adapter import get_media_adapter
            success = get_media_adapter().previous_track()
        except Exception as exc:
            print(f"[island] media_prev error: {exc}")

        if not success and sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB1, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB1, 0, 2, 0)
            except Exception:
                pass
        self.update()

    def media_next(self) -> None:
        success = False
        try:
            from media_adapter import get_media_adapter
            success = get_media_adapter().next_track()
        except Exception as exc:
            print(f"[island] media_next error: {exc}")

        if not success and sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB0, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB0, 0, 2, 0)
            except Exception:
                pass
        self.update()

    def format_temp(self, temp_val: float) -> str:
        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        units = getattr(cfg, "island_weather_units", getattr(cfg, "weather_units", "auto")).lower()
        current_sym = getattr(self, "weather_unit_symbol", None)

        if current_sym == "°F" and units == "celsius":
            c = int(round((temp_val - 32.0) * 5.0 / 9.0))
            return f"{c}°"
        elif current_sym == "°C" and units == "fahrenheit":
            f = int(round(temp_val * 9.0 / 5.0 + 32.0))
            return f"{f}°"
        elif units == "celsius" and not current_sym and temp_val > 50.0:
            c = int(round((temp_val - 32.0) * 5.0 / 9.0))
            return f"{c}°"

        return f"{int(round(temp_val))}°"

    def _sync_hardware_adapter_lifecycle(self, now: float) -> None:
        """Polls hardware adapter only while hardware page is active; pauses when collapsed or on other pages."""
        is_hw_active = (
            self.expanded_card in ("hardware", "hardware_grid", "hardware_strip")
            and (getattr(self, "pin_open", False) or now < self.expanded_until)
        )
        try:
            from hardware_adapter import get_hardware_adapter
            adapter = get_hardware_adapter()
            if is_hw_active:
                if adapter.is_paused():
                    adapter.resume()
                self._cached_hardware_metrics = adapter.get_metrics()
            else:
                if not adapter.is_paused():
                    adapter.pause()
        except Exception:
            pass

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

        state   = self._resolve_state(now)
        dark    = self.dark_mode
        reduce  = T.reduce_motion()

        if state in (IslandState.CARD_MEDIA, IslandState.CARD_WEATHER, IslandState.CARD_TIME, IslandState.CARD_HARDWARE_GRID, IslandState.CARD_JARVIS):
            r = 47.0
        elif state == IslandState.CARD_HARDWARE_STRIP:
            r = 28.0
        elif state in (IslandState.CARD_DISAMBIGUATE, IslandState.CARD_TIMER):
            r = 36.0
        elif state == IslandState.IDLE:
            r = min(h / 2.0, 18.0)
        else:
            r = min(h / 2.0, 36.0)

        if reduce:
            elapsed = now - self._crossfade_start
            opacity = min(1.0, max(0.0, elapsed / (REDUCE_MOTION_MS / 1000.0)))
        else:
            opacity = min(1.0, max(0.0, self._opacity_spring.value))

        is_pill = getattr(self, "shape_style", "pill") == "pill"
        if is_pill:
            # Floating stadium capsule with 4 rounded squircle corners
            pill_rect = QRectF(2, 2, w - 4, h - 4)
            squircle  = create_cached_squircle_path(pill_rect, r)

            # ── 1. Soft multi-layer drop shadow ──────────────────────────────
            for offset, alpha in [(2, 12), (4, 20), (7, 28)]:
                sr = QRectF(2, 2 + offset, w - 4, h - 4)
                sp = create_cached_squircle_path(sr, r)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(0, 0, 0, alpha)))
                p.drawPath(sp)

            # ── 2. Glass tint fill (Obsidian gradient) ────────────────────────
            bg_grad = QLinearGradient(0, 2, 0, h - 2)
            cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
            op = float(getattr(cfg, "island_backdrop_opacity", 0.95)) if cfg else 0.95
            op_scale = max(0.05, min(1.0, op / 0.95))
            if dark:
                bg_grad.setColorAt(0.0, QColor(19, 22, 23, int(round(245 * op_scale))))
                bg_grad.setColorAt(1.0, QColor(10, 10, 10, int(round(250 * op_scale))))
            else:
                bg_grad.setColorAt(0.0, QColor(248, 248, 252, int(round(240 * op_scale))))
                bg_grad.setColorAt(1.0, QColor(230, 230, 238, int(round(248 * op_scale))))

            p.fillPath(squircle, QBrush(bg_grad))
        else:
            # Bezel-attached notch: top edge flush against screen bezel, bottom corners rounded
            pill_rect = QRectF(2, -16, w - 4, h + 14)
            squircle  = create_cached_squircle_path(pill_rect, r)

            # ── 1. Soft multi-layer drop shadow ──────────────────────────────
            for offset, alpha in [(2, 12), (4, 20), (7, 28)]:
                sr = QRectF(2, -16 + offset, w - 4, h + 14)
                sp = create_cached_squircle_path(sr, r)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(0, 0, 0, alpha)))
                p.drawPath(sp)

            # ── 2. Glass tint fill (Obsidian gradient) ────────────────────────
            bg_grad = QLinearGradient(0, -16, 0, h)
            cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
            op = float(getattr(cfg, "island_backdrop_opacity", 0.95)) if cfg else 0.95
            op_scale = max(0.05, min(1.0, op / 0.95))
            if dark:
                bg_grad.setColorAt(0.0, QColor(19, 22, 23, int(round(245 * op_scale))))
                bg_grad.setColorAt(1.0, QColor(10, 10, 10, int(round(250 * op_scale))))
            else:
                bg_grad.setColorAt(0.0, QColor(248, 248, 252, int(round(240 * op_scale))))
                bg_grad.setColorAt(1.0, QColor(230, 230, 238, int(round(248 * op_scale))))

            p.fillPath(squircle, QBrush(bg_grad))

        # Media Card dominant & average gradient glass ambient tint
        if state == IslandState.CARD_MEDIA and self.media_dominant_color:
            if getattr(self, "media_album_art", None) and not self.media_album_art.isNull():
                card_grad = QLinearGradient(0, 0, w, h)
                c_start = QColor(self.media_dominant_color)
                c_start.setAlpha(65 if dark else 45)

                mid_c = getattr(self, "media_secondary_color", None) or getattr(self, "media_average_color", None) or self.media_dominant_color
                c_mid = QColor(mid_c)
                c_mid.setAlpha(35 if dark else 24)

                c_end = QColor(mid_c)
                c_end.setAlpha(10 if dark else 6)

                card_grad.setColorAt(0.0, c_start)
                card_grad.setColorAt(0.55, c_mid)
                card_grad.setColorAt(1.0, c_end)
                p.fillPath(squircle, QBrush(card_grad))
            else:
                tint = QColor(self.media_dominant_color)
                tint.setAlpha(36 if dark else 24)
                p.fillPath(squircle, QBrush(tint))

        # ── 3. Soft edge glow pulse in WAKE or CARD_SENDING / CARD_JARVIS ──
        glow_val = self.spring_glow.value
        if state == IslandState.CARD_JARVIS:
            glow_pen = QPen(QColor(0, 229, 255, 75 if dark else 90), 2.0)
            p.strokePath(squircle, glow_pen)
        elif state == IslandState.CARD_SENDING and not reduce:
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
        if is_pill:
            sheen = QLinearGradient(0, 2.0, 0, 2.0 + (h - 2.0) * 0.40)
        else:
            sheen = QLinearGradient(0, -16, 0, h * 0.40)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 31 if dark else 80))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(squircle, QBrush(sheen))

        # ── 6. Inner stroke (1 px hairline, state-colored) ───────────────
        self._draw_inner_stroke(p, squircle, state, t, dark)

        # ── 7. Content (with opacity cross-fade) ─────────────────────────
        p.setOpacity(opacity)
        self._draw_content(p, w, h, now, t, state, dark, reduce)
        p.setOpacity(1.0)
        self._last_rendered_state = state
        self._last_rendered_dark = dark
        self._dirty = False

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

        elif state in (
            IslandState.CARD_MEDIA, IslandState.CARD_WEATHER, IslandState.CARD_TIME,
            IslandState.CARD_DISAMBIGUATE, IslandState.CARD_TIMER,
            IslandState.CARD_HARDWARE_GRID, IslandState.CARD_HARDWARE_STRIP, IslandState.CARD_JARVIS
        ):
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
            self._draw_thinking(p, w, h, cy, t, dark, primary, accent, CYAN, now=now)

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
            self._draw_calendar_card(p, w, h, now, dark, primary, secondary, accent)

        elif state == IslandState.CARD_TIMER:
            self._draw_timer_card(p, w, h, now, dark, primary, secondary, accent)

        elif state == IslandState.CARD_HARDWARE_GRID:
            self._draw_hardware_grid(p, w, h, now, t, dark, primary, secondary, accent)

        elif state == IslandState.CARD_HARDWARE_STRIP:
            self._draw_hardware_strip(p, w, h, now, t, dark, primary, secondary, accent)

        elif state == IslandState.CARD_JARVIS:
            self._draw_jarvis_card(p, w, h, now, t, dark, primary, secondary, accent)

        elif state == IslandState.HUD_VOLUME:
            self._draw_hud_volume(p, w, h, cy, dark, primary, secondary, accent)

        elif state == IslandState.HUD_WINDOW:
            self._draw_hud_window(p, w, h, cy, dark, primary, secondary, accent)

        else:
            # Standby IDLE & Compact Widgets
            self._draw_compact_widget(p, w, h, cy, dark, primary, secondary, accent, now, t)

        # Page dot column for expanded card layouts
        if state in (
            IslandState.CARD_MEDIA, IslandState.CARD_WEATHER, IslandState.CARD_TIME,
            IslandState.CARD_HARDWARE_GRID, IslandState.CARD_JARVIS
        ):
            self._draw_page_dots(p, w, h, dark)

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

        # Trailing 6px status dot (Green/Gray/Red)
        dot_cx = w - 14.0
        dot_r = 3.0
        if now < self.error_until or self.is_ai_error:
            dot_col = COLOR.accent_red
        elif self.active or self.listening or self.media_playing:
            dot_col = COLOR.accent_green
        else:
            dot_col = QColor("#8E8E93" if dark else "#AEAEB2")

        if compact_mode == "media":
            # ── Media compact layout ─────────────────────────────────────
            # Leading side: 20x20 album art thumbnail (rounded squircle)
            thumb_size = 20.0
            tx = 8.0
            ty = cy - thumb_size / 2.0
            thumb_rect = QRectF(tx, ty, thumb_size, thumb_size)
            thumb_path = create_cached_squircle_path(thumb_rect, 5.0)

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
            text_rect = QRectF(34.0, 0, w - 34.0 - 44.0, h)
            title = self.media_title if len(self.media_title) <= 14 else self.media_title[:13] + "…"
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)

            # 4 audio-reactive animated bars
            bx0 = w - 40.0
            bar_w = 2.2
            gap = 1.8
            max_bh = 13.0
            p.setPen(Qt.PenStyle.NoPen)
            wf_cols = self._get_waveform_colors()
            for i in range(4):
                bh = max(3.0, self._media_bars[i] * max_bh)
                bx = bx0 + i * (bar_w + gap)
                by = cy - bh / 2.0
                if getattr(self, "waveform_color", "blue") == "multicolor":
                    bar_col = QColor(175, 82, 222) if i % 2 == 0 else QColor(0, 229, 255)
                else:
                    bar_col = wf_cols[1] if i % 2 == 0 else wf_cols[2]
                p.setBrush(QBrush(bar_col))
                p.drawRoundedRect(QRectF(bx, by, bar_w, bh), bar_w / 2.0, bar_w / 2.0)

            # Trailing 6px status dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(dot_col))
            p.drawEllipse(QPointF(dot_cx, cy), dot_r, dot_r)

        elif compact_mode == "timer":
            # ── Timer compact layout ─────────────────────────────────────
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
            p.drawArc(arc_rect, 90 * 16, -int(frac * 360 * 16))

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
            text_rect = QRectF(32.0 * scale, 0, w - 54.0 * scale, h)
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, time_txt)

            # Trailing 6px status dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(dot_col))
            p.drawEllipse(QPointF(dot_cx, cy), dot_r, dot_r)

        elif compact_mode in ("time_weather", "default"):
            # ── Time + Weather compact layout ────────────────────────────
            # Left side: tabular clock
            cfg = getattr(self.mgr, "cfg", None)
            fmt_24 = (getattr(cfg, "island_clock_format", "12h") == "24h")
            tm = time.localtime(now)
            time_str = time.strftime("%H:%M" if fmt_24 else "%I:%M", tm).lstrip("0")
            if not time_str:
                time_str = "12:00"

            time_font = qfont(TYPE.caption_1)
            time_font.setWeight(QFont.Weight.DemiBold)
            try:
                time_font.setFeature(b"tnum", 1)
            except Exception:
                pass
            p.setFont(time_font)
            p.setPen(primary)
            p.drawText(QRectF(14.0, 0, 48.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, time_str)

            # Center: 1px subtle hairline divider
            div_x = 68.0
            p.setPen(QPen(QColor(255, 255, 255, 23 if dark else 35), 1.0))
            p.drawLine(QPointF(div_x, cy - 6.0), QPointF(div_x, cy + 6.0))

            # Right side: weather SVG glyph + temp
            temp_str = self.format_temp(self.weather_temp)
            temp_w = p.fontMetrics().horizontalAdvance(temp_str)
            tx = w - 14.0 - 8.0 - temp_w
            p.drawText(QRectF(tx, 0, temp_w + 4.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, temp_str)

            gx = tx - 14.0
            self._draw_weather_glyph(p, self.weather_icon, gx, cy, 14.0, dark)

            # Trailing 6px status dot (Green / Gray / Red)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(dot_col))
            p.drawEllipse(QPointF(dot_cx, cy), dot_r, dot_r)

        elif compact_mode == "time":
            # ── Time compact layout ──────────────────────────────────────
            cfg = getattr(self.mgr, "cfg", None)
            fmt_24 = (getattr(cfg, "island_clock_format", "12h") == "24h")
            tm = time.localtime(now)
            time_str = time.strftime("%H:%M" if fmt_24 else "%I:%M %p", tm).lstrip("0")
            time_font = qfont(TYPE.caption_1)
            time_font.setWeight(QFont.Weight.DemiBold)
            try:
                time_font.setFeature(b"tnum", 1)
            except Exception:
                pass
            p.setFont(time_font)
            p.setPen(primary)
            p.drawText(QRectF(14.0, 0, w - 38.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter, time_str)

            # Trailing 6px status dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(dot_col))
            p.drawEllipse(QPointF(dot_cx, cy), dot_r, dot_r)

        else:
            # ── Minimal standby IDLE capsule ─────────────────────────────
            lbl_font = qfont(TYPE.callout)
            lbl_font.setWeight(QFont.Weight.DemiBold)
            p.setFont(lbl_font)
            p.setPen(secondary if not self.active else primary)
            p.drawText(int(18.0), int(cy + 4), "Jarvis")

            # Trailing 6px status dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(dot_col))
            p.drawEllipse(QPointF(dot_cx, cy), dot_r, dot_r)

    # ──────────────────────────────────────────────────────────────────────
    # Expanded Cards Renderers
    # ──────────────────────────────────────────────────────────────────────

    def _draw_source_badge(self, p: QPainter, bx: float, by: float, size: float, source: str, dark: bool, scale: float) -> None:
        """Renders small source-app badge in corner of album art (Apple HIG)."""
        r = size / 2.0
        cx = bx + r
        cy = by + r

        # Dark circular background
        p.setPen(QPen(QColor(255, 255, 255, 30 if dark else 60), 1.0))
        p.setBrush(QBrush(QColor(16, 18, 20, 245) if dark else QColor(245, 245, 250, 245)))
        p.drawEllipse(QPointF(cx, cy), r, r)

        src = (source or "Spotify").lower()
        if "spotify" in src:
            # Green spotify soundwave arcs
            p.setPen(QPen(QColor("#1ED760"), 1.2 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for off, rad in [(-2.0, 4.6), (0.0, 3.4), (2.0, 2.2)]:
                arc_rect = QRectF(cx - rad * scale, cy + off * scale - rad * scale, rad * 2.0 * scale, rad * 2.0 * scale)
                p.drawArc(arc_rect, 40 * 16, 100 * 16)
        else:
            # Musical note glyph
            p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
            p.setFont(QFont("Segoe UI Variable Text", int(8 * scale), QFont.Weight.Bold))
            p.drawText(QRectF(bx, by - 0.5 * scale, size, size), Qt.AlignmentFlag.AlignCenter, "♪")

    def _draw_media_card(self, p: QPainter, w: float, h: float,
                         now: float, t: float, dark: bool,
                         primary: QColor, secondary: QColor, accent: QColor, CYAN: QColor) -> None:
        """
        Expanded Media Card (390x188 px):
        - Left: 56-60 px rounded album art (radius ~14px) with small source-app badge in corner.
        - Title (semibold), artist and album in secondary gray (#8E9094).
        - Tiny accent equalizer bars top right (animated when playing).
        - Scrubber: 4px track, accent-colored fill, white thumb, elapsed time on left
          and remaining time (-X:XX) on right in tabular numerals. Interactive click/drag seek hit target.
        - Three round controls: previous track, large play/pause in a darker disc, next track.
        - Dominant color tinting: controls, progress fill, and equalizer bars tinted from album art dominant color
          (fallback to sky blue #45C1EE).
        - Mouse click handling for previous, play/pause, next, and scrubber seek.
        """
        scale = self._get_size_scale()

        # Dominant color tinting with sky blue (#45C1EE) fallback
        if hasattr(self, "media_dominant_color") and self.media_dominant_color and self.media_dominant_color.isValid():
            dom_accent = self.media_dominant_color
        else:
            dom_accent = QColor("#45C1EE")

        # 1. 58 px Album Art Thumbnail with squircle radius 14 px
        art_size = 58.0 * scale
        ax = 22.0 * scale
        ay = 22.0 * scale
        art_rect = QRectF(ax, ay, art_size, art_size)
        art_path = create_cached_squircle_path(art_rect, 14.0 * scale)

        # Soft shadow & ambient tint
        for off, a in [(2, 18), (5, 32)]:
            s_rect = QRectF(ax, ay + off, art_size, art_size)
            s_path = create_cached_squircle_path(s_rect, 14.0 * scale)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, a)))
            p.drawPath(s_path)

        # Dominant glow behind artwork
        glow = QRadialGradient(ax + art_size / 2.0, ay + art_size / 2.0, art_size * 0.85)
        glow.setColorAt(0.0, QColor(dom_accent.red(), dom_accent.green(), dom_accent.blue(), 90))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(ax + art_size / 2.0, ay + art_size / 2.0), art_size * 0.75, art_size * 0.75)

        # Draw Artwork Pixmap
        art_pix = self._get_album_art(art_size)
        p.save()
        p.setClipPath(art_path)
        p.drawPixmap(QPointF(ax, ay), art_pix)
        p.restore()
        p.strokePath(art_path, QPen(QColor(255, 255, 255, 60 if dark else 90), 1.0))

        # Small source-app badge in corner of album art
        badge_size = 18.0 * scale
        bx = ax + art_size - badge_size + 2.0 * scale
        by = ay + art_size - badge_size + 2.0 * scale
        self._draw_source_badge(p, bx, by, badge_size, self.media_source, dark, scale)

        # 2. Track Title & Artist / Album
        tx = ax + art_size + 14.0 * scale
        eq_x = w - 42.0 * scale
        tw = eq_x - tx - 8.0 * scale

        title_font = qfont(TYPE.headline)
        title_font.setPointSize(int(14 * scale))
        title_font.setWeight(QFont.Weight.DemiBold)
        title_rect = QRectF(tx, ay + 2.0 * scale, tw, 22.0 * scale)
        self._draw_marquee_text(p, self.media_title, title_font, title_rect, primary, now - self._card_expand_t)

        album_str = getattr(self, "media_album", "")
        if album_str:
            artist_album_txt = f"{self.media_artist} — {album_str}"
        else:
            artist_album_txt = self.media_artist

        artist_font = qfont(TYPE.subheadline)
        artist_font.setPointSize(int(12 * scale))
        artist_font.setWeight(QFont.Weight.Normal)
        artist_rect = QRectF(tx, ay + 24.0 * scale, tw, 20.0 * scale)
        self._draw_marquee_text(p, artist_album_txt, artist_font, artist_rect, QColor("#8E9094"), now - self._card_expand_t)

        # 3. Tiny accent equalizer bars top right (animated when playing)
        eq_y = ay + 4.0 * scale
        bar_w = 2.4 * scale
        bar_gap = 1.6 * scale
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(dom_accent))
        for i in range(4):
            bx_i = eq_x + i * (bar_w + bar_gap)
            if self.media_playing:
                b_val = self._media_bars[i % len(self._media_bars)]
                bh_i = max(3.0 * scale, b_val * 14.0 * scale)
            else:
                bh_i = 3.0 * scale
            by_i = eq_y + 14.0 * scale - bh_i
            p.drawRoundedRect(QRectF(bx_i, by_i, bar_w, bh_i), 1.0 * scale, 1.0 * scale)

        # 4. Scrubber Bar with Tabular Numerals
        sr = self._scrubber_rect()
        # Track background: 4px track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 28 if dark else 50)))
        p.drawRoundedRect(sr, 2.0 * scale, 2.0 * scale)

        # Accent-colored fill
        prog_w = sr.width() * min(1.0, max(0.0, self.media_progress))
        if prog_w > 0:
            fill_rect = QRectF(sr.x(), sr.y(), prog_w, sr.height())
            p.setBrush(QBrush(dom_accent))
            p.drawRoundedRect(fill_rect, 2.0 * scale, 2.0 * scale)

        # White thumb
        hx = sr.x() + prog_w
        hy = sr.y() + sr.height() / 2.0
        p.setBrush(QBrush(QColor(255, 255, 255, 255)))
        p.drawEllipse(QPointF(hx, hy), 4.8 * scale, 4.8 * scale)

        # Numerals in tabular format
        num_font = qfont(TYPE.caption_2)
        num_font.setPointSize(int(10 * scale))
        num_font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
        p.setFont(num_font)
        p.setPen(QColor("#8E9094"))

        time_text_y = sr.y() + sr.height() + 3.0 * scale
        el_m = int(self.media_position_sec // 60)
        el_s = int(self.media_position_sec % 60)
        el_str = f"{el_m}:{el_s:02d}"
        p.drawText(QRectF(sr.x(), time_text_y, 60.0 * scale, 16.0 * scale), Qt.AlignmentFlag.AlignLeft, el_str)

        rem_sec = max(0.0, self.media_duration_sec - self.media_position_sec)
        rem_m = int(rem_sec // 60)
        rem_s = int(rem_sec % 60)
        rem_str = f"-{rem_m}:{rem_s:02d}"
        p.drawText(QRectF(sr.right() - 60.0 * scale, time_text_y, 60.0 * scale, 16.0 * scale), Qt.AlignmentFlag.AlignRight, rem_str)

        # 5. Playback Controls (Prev, Play-Pause in a darker disc, Next)
        cy_btn = 138.0 * scale
        prev_cx = (w / 2.0 - 52.0 * scale)
        play_cx = (w / 2.0)
        next_cx = (w / 2.0 + 52.0 * scale)

        self._draw_playback_button(p, "prev", prev_cx, cy_btn, 16.0 * scale, primary, scale, now, dom_accent)
        self._draw_playback_button(p, "play_pause", play_cx, cy_btn, 20.0 * scale, primary, scale, now, dom_accent)
        self._draw_playback_button(p, "next", next_cx, cy_btn, 16.0 * scale, primary, scale, now, dom_accent)

        # Footnote source app text
        foot_font = qfont(TYPE.caption_2)
        p.setFont(foot_font)
        p.setPen(QColor("#8E9094"))
        src_txt = f"♪ {self.media_source}"
        p.drawText(QRectF(w - 110.0 * scale, cy_btn - 8.0 * scale, 84.0 * scale, 16.0 * scale),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, src_txt)

    def _draw_playback_button(self, p: QPainter, btn_id: str, cx: float, cy: float,
                              r: float, color: QColor, scale: float, now: float,
                              dom_accent: Optional[QColor] = None) -> None:
        is_hover = (self._hovered_btn == btn_id)
        is_flash = (now < self._btn_flash_until and self._hovered_btn == btn_id)
        accent_col = dom_accent if dom_accent else COLOR.HUD_ONLY.neon_cyan

        if btn_id == "play_pause":
            # Large play/pause in a darker disc
            disc_r = r + 2.0 * scale
            p.setPen(QPen(QColor(255, 255, 255, 20), 1.0))
            if is_hover or is_flash:
                p.setBrush(QBrush(QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 55 if is_flash else 35)))
            else:
                p.setBrush(QBrush(QColor(255, 255, 255, 24)))
            p.drawEllipse(QPointF(cx, cy), disc_r, disc_r)
        else:
            # Hover circle highlight for prev / next
            if is_hover or is_flash:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(255, 255, 255, 55 if is_flash else 22)))
                p.drawEllipse(QPointF(cx, cy), r + 3.0 * scale, r + 3.0 * scale)

        # Hand-tracking dwell ring (fills over 1.0s)
        if is_hover and self._btn_dwell_elapsed > 0.05:
            dwell_prog = min(1.0, self._btn_dwell_elapsed / 1.0)
            dwell_pen = QPen(accent_col, 2.2 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
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
                    QPointF(cx - 5.0 * scale, cy - 7.5 * scale),
                    QPointF(cx + 7.0 * scale, cy),
                    QPointF(cx - 5.0 * scale, cy + 7.5 * scale)
                ])
                p.drawPolygon(tri)
            else:
                # Pause bars
                bw = 3.5 * scale
                bh = 14.0 * scale
                p.drawRoundedRect(QRectF(cx - 5.5 * scale, cy - bh / 2.0, bw, bh), 1.5 * scale, 1.5 * scale)
                p.drawRoundedRect(QRectF(cx + 2.0 * scale, cy - bh / 2.0, bw, bh), 1.5 * scale, 1.5 * scale)

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

    def _draw_weather_tile_icon(self, p: QPainter, icon_type: str, cx: float, cy: float, size: float, color: QColor) -> None:
        """Renders muted vector icon for weather metric tiles (wind, feels like, humidity)."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if icon_type == "wind":
            # Stylized wind gust streaks
            pen = QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(cx - 6.0, cy - 3.0), QPointF(cx + 3.0, cy - 3.0))
            p.drawLine(QPointF(cx - 4.0, cy + 2.5), QPointF(cx + 6.0, cy + 2.5))
            p.drawArc(QRectF(cx + 1.0, cy - 6.0, 5.0, 5.0), 0, 180 * 16)
        elif icon_type == "feels_like":
            # Thermometer glyph
            pen = QPen(color, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 2.0, cy - 6.0, 4.0, 7.5), 1.5, 1.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawEllipse(QPointF(cx, cy + 3.5), 3.2, 3.2)
        elif icon_type == "humidity":
            # Droplet glyph
            path = QPainterPath()
            path.moveTo(cx, cy - 6.0)
            path.cubicTo(cx - 4.5, cy - 1.0, cx - 4.0, cy + 4.5, cx, cy + 4.5)
            path.cubicTo(cx + 4.0, cy + 4.5, cx + 4.5, cy - 1.0, cx, cy - 6.0)
            path.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawPath(path)

        p.restore()

    def _draw_weather_card(self, p: QPainter, w: float, h: float,
                          now: float, t: float, dark: bool,
                          primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded Weather Card (390x188 px):
        - Left side: City name in uppercase gray (#8E9094, e.g. "AHMEDABAD"), clean weather vector icon
          + large temperature (heavy numerals #F5F5F7), condition text (semibold).
        - Vertical hairline divider.
        - Right side: Three stacked tiles (radius ~17px, fill #FFFFFF17, border 1px #FFFFFF0A):
          1. Wind: speed + direction arrow, label "WIND", muted icon.
          2. Feels Like: temperature, label "FEELS LIKE", muted icon.
          3. Humidity: percentage, label "HUMIDITY", muted icon.
        - Attribution footnote at bottom: "Weather data by Open-Meteo.com" (9 pt, #55585C).
        """
        scale = self._get_size_scale()

        # ── 1. Left Side: Hero City & Temp ────────────────────────────────
        lx = 22.0 * scale
        left_w = 172.0 * scale

        # City name in uppercase gray (#8E9094)
        city_str = (self.weather_city or "AHMEDABAD").upper()
        city_font = qfont(TYPE.footnote)
        city_font.setPointSize(int(11 * scale))
        city_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(city_font)
        p.setPen(QColor("#8E9094"))
        p.drawText(QRectF(lx, 22.0 * scale, left_w, 18.0 * scale), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, city_str)

        # Weather Vector Icon + Large Heavy Temperature Numerals
        icon_cx = lx + 22.0 * scale
        icon_cy = 68.0 * scale
        self._draw_weather_glyph(p, self.weather_icon, icon_cx, icon_cy, 36.0 * scale, dark)

        temp_str = self.format_temp(self.weather_temp)
        temp_font = QFont("Segoe UI Variable Display", int(36 * scale), QFont.Weight.Bold)
        temp_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        p.setFont(temp_font)
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(QRectF(lx + 48.0 * scale, 44.0 * scale, left_w - 48.0 * scale, 48.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, temp_str)

        # Condition text (semibold)
        cond_font = qfont(TYPE.body)
        cond_font.setPointSize(int(13 * scale))
        cond_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(cond_font)
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(QRectF(lx, 98.0 * scale, left_w, 20.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.weather_condition)

        # Attribution footnote at bottom: "Weather data by Open-Meteo.com" (9 pt, #55585C)
        foot_font = qfont(TYPE.caption_2)
        foot_font.setPointSize(int(8.5 * scale))
        p.setFont(foot_font)
        p.setPen(QColor("#55585C"))
        p.drawText(QRectF(lx, 150.0 * scale, w - lx - 30.0 * scale, 18.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Weather data by Open-Meteo.com")

        # ── 2. Vertical Hairline Divider ──────────────────────────────────
        div_x = lx + left_w + 10.0 * scale
        p.setPen(QPen(QColor(255, 255, 255, 18 if dark else 35), 1.0))
        p.drawLine(QPointF(div_x, 22.0 * scale), QPointF(div_x, 142.0 * scale))

        # ── 3. Right Side: Three Stacked Tiles ────────────────────────────
        rx = div_x + 12.0 * scale
        rw = (w - 28.0 * scale) - rx  # Leave space before page dots
        tile_h = 36.0 * scale
        tile_gap = 6.0 * scale
        tile_r = 17.0 * scale

        tiles = [
            ("wind", "WIND", f"{int(round(getattr(self, 'weather_wind_speed', 12.0)))} mph {getattr(self, 'weather_wind_direction', 'NW')} ↖"),
            ("feels_like", "FEELS LIKE", self.format_temp(self.weather_feels_like)),
            ("humidity", "HUMIDITY", f"{int(round(getattr(self, 'weather_humidity', 48)))}%"),
        ]

        label_font = qfont(TYPE.caption_2)
        label_font.setPointSize(int(8 * scale))
        label_font.setWeight(QFont.Weight.DemiBold)

        val_font = qfont(TYPE.subheadline)
        val_font.setPointSize(int(11 * scale))
        val_font.setWeight(QFont.Weight.Bold)

        for idx, (icon_type, label, val_text) in enumerate(tiles):
            ty = 20.0 * scale + idx * (tile_h + tile_gap)
            tr = QRectF(rx, ty, rw, tile_h)
            t_path = create_cached_squircle_path(tr, tile_r)

            # Inset tile fill #FFFFFF17 and border 1px #FFFFFF0A
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(255, 255, 255, 23 if dark else 55)))
            p.drawPath(t_path)
            p.strokePath(t_path, QPen(QColor(255, 255, 255, 10 if dark else 25), 1.0))

            # Muted icon on left
            icx = rx + 15.0 * scale
            icy = ty + tile_h / 2.0
            self._draw_weather_tile_icon(p, icon_type, icx, icy, 14.0 * scale, QColor("#8E9094"))

            # Label (e.g. "WIND", "FEELS LIKE", "HUMIDITY")
            p.setFont(label_font)
            p.setPen(QColor("#8E9094"))
            p.drawText(QRectF(rx + 28.0 * scale, ty + 4.0 * scale, rw - 32.0 * scale, 12.0 * scale),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

            # Value text
            p.setFont(val_font)
            p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
            p.drawText(QRectF(rx + 28.0 * scale, ty + 16.0 * scale, rw - 32.0 * scale, 16.0 * scale),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val_text)

    def _get_calendar_data(self, now: float):
        """Retrieves and caches CalendarData for zero-allocation 60 FPS rendering."""
        cfg = getattr(self.mgr, "cfg", None)
        first_weekday = getattr(cfg, "island_first_weekday", "locale")

        if self.calendar_date is not None:
            today = self.calendar_date
        else:
            try:
                today = datetime.date.fromtimestamp(now)
            except Exception:
                today = datetime.date(2026, 10, 18)

        key = (today.year, today.month, today.day, first_weekday)
        if self._cached_calendar_data is not None and self._cached_calendar_key == key:
            return self._cached_calendar_data

        try:
            import calendar_model
            data = calendar_model.get_calendar_data(target_date=today, first_weekday=first_weekday, today=today)
        except Exception:
            data = None

        self._cached_calendar_data = data
        self._cached_calendar_key = key
        return data

    def _draw_calendar_card(self, p: QPainter, w: float, h: float,
                            now: float, dark: bool,
                            primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded Calendar Page Card (390x188 px):
        - Left tile: Inset tile (radius ~17px, fill #FFFFFF17, border 1px #FFFFFF0A):
          * Month name in accent color (#45C1EE).
          * Year in gray (#8E9094).
          * Very large bold day number (heavy font, #F5F5F7).
          * Weekday name at the bottom (#8E9094).
        - Right side:
          * Weekday header row over a hairline divider.
          * Weekends dimmed (#55585C).
          * Current month days in #F5F5F7.
          * Today highlighted as a filled accent circle (#45C1EE) with white text.
        """
        scale = self._get_size_scale()
        data = self._get_calendar_data(now)
        if not data:
            return

        # ── 1. Left Tile: Inset Tile ──────────────────────────────────────
        lx = 20.0 * scale
        ly = 20.0 * scale
        tile_w = 112.0 * scale
        tile_h = h - 40.0 * scale  # 148.0 * scale
        tile_r = 17.0 * scale

        t_rect = QRectF(lx, ly, tile_w, tile_h)
        t_path = create_cached_squircle_path(t_rect, tile_r)

        # Inset tile fill #FFFFFF17 and border 1px #FFFFFF0A
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 23 if dark else 55)))
        p.drawPath(t_path)
        p.strokePath(t_path, QPen(QColor(255, 255, 255, 10 if dark else 25), 1.0))

        # Month name in accent color (#45C1EE)
        month_font = qfont(TYPE.footnote)
        month_font.setPointSize(int(11 * scale))
        month_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(month_font)
        p.setPen(QColor("#45C1EE"))
        p.drawText(QRectF(lx, ly + 14.0 * scale, tile_w, 18.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, data.month_name.upper())

        # Year in gray (#8E9094)
        year_font = qfont(TYPE.caption_1)
        year_font.setPointSize(int(10 * scale))
        year_font.setWeight(QFont.Weight.Medium)
        p.setFont(year_font)
        p.setPen(QColor("#8E9094"))
        p.drawText(QRectF(lx, ly + 32.0 * scale, tile_w, 16.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, str(data.year))

        # Very large bold day number (heavy font, #F5F5F7)
        day_font = QFont("Segoe UI Variable Display", int(44 * scale), QFont.Weight.Bold)
        day_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        p.setFont(day_font)
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(QRectF(lx, ly + 46.0 * scale, tile_w, 58.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, str(data.day))

        # Weekday name at the bottom (#8E9094)
        weekday_font = qfont(TYPE.subheadline)
        weekday_font.setPointSize(int(12 * scale))
        weekday_font.setWeight(QFont.Weight.Medium)
        p.setFont(weekday_font)
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(QRectF(lx, ly + 114.0 * scale, tile_w, 20.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, data.weekday_name)

        # ── 2. Right Side: Calendar Grid ──────────────────────────────────
        rx = lx + tile_w + 16.0 * scale
        rw = (w - 28.0 * scale) - rx  # leaving room before the 5 dots on right edge
        ry = ly
        rh = tile_h

        # Weekday header row
        hdr_h = 20.0 * scale
        col_w = rw / 7.0
        hdr_font = qfont(TYPE.caption_2)
        hdr_font.setPointSize(int(9 * scale))
        hdr_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(hdr_font)

        for c_idx in range(7):
            hdr_text = data.weekday_headers[c_idx]
            is_weekend_col = (data.grid[0][c_idx].is_weekend if data.grid else False)
            if is_weekend_col:
                p.setPen(QColor("#55585C"))
            else:
                p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
            p.drawText(QRectF(rx + c_idx * col_w, ry + 2.0 * scale, col_w, hdr_h),
                       Qt.AlignmentFlag.AlignCenter, hdr_text)

        # Hairline divider
        div_y = ry + hdr_h + 4.0 * scale
        p.setPen(QPen(QColor(255, 255, 255, 18 if dark else 35), 1.0))
        p.drawLine(QPointF(rx + 2.0 * scale, div_y), QPointF(rx + rw - 2.0 * scale, div_y))

        # Grid of days
        grid_top = div_y + 4.0 * scale
        avail_h = (ry + rh) - grid_top
        num_rows = len(data.grid)
        row_h = avail_h / max(1, num_rows)

        day_font = qfont(TYPE.caption_1)
        day_font.setPointSize(int(10 * scale))
        day_font.setWeight(QFont.Weight.Normal)

        today_font = qfont(TYPE.caption_1)
        today_font.setPointSize(int(10 * scale))
        today_font.setWeight(QFont.Weight.Bold)

        for r_idx, week in enumerate(data.grid):
            for c_idx, day in enumerate(week):
                cx = rx + c_idx * col_w + col_w / 2.0
                cy = grid_top + r_idx * row_h + row_h / 2.0
                cell_rect = QRectF(rx + c_idx * col_w, grid_top + r_idx * row_h, col_w, row_h)

                if day.is_today:
                    # Today highlighted as a filled accent circle (#45C1EE) with white text
                    cr = min(col_w, row_h) * 0.40
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QBrush(QColor("#45C1EE")))
                    p.drawEllipse(QPointF(cx, cy), cr, cr)

                    p.setFont(today_font)
                    p.setPen(QColor("#FFFFFF"))
                    p.drawText(cell_rect, Qt.AlignmentFlag.AlignCenter, str(day.day_number))

                elif not day.is_current_month:
                    p.setFont(day_font)
                    p.setPen(QColor("#3A3D40" if dark else "#B8B8C0"))
                    p.drawText(cell_rect, Qt.AlignmentFlag.AlignCenter, str(day.day_number))

                elif day.is_weekend:
                    # Weekends dimmed (#55585C)
                    p.setFont(day_font)
                    p.setPen(QColor("#55585C" if dark else "#8E9094"))
                    p.drawText(cell_rect, Qt.AlignmentFlag.AlignCenter, str(day.day_number))

                else:
                    # Current month days in #F5F5F7
                    p.setFont(day_font)
                    p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
                    p.drawText(cell_rect, Qt.AlignmentFlag.AlignCenter, str(day.day_number))

    def _draw_time_card(self, p: QPainter, w: float, h: float,
                        now: float, dark: bool, primary: QColor, secondary: QColor,
                        accent: Optional[QColor] = None) -> None:
        """Expanded Calendar & Time Card."""
        self._draw_calendar_card(p, w, h, now, dark, primary, secondary, accent or QColor("#45C1EE"))

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
    # Hardware Grid, Strip & JARVIS Assistant Card Renderers
    # ──────────────────────────────────────────────────────────────────────

    def _draw_hardware_tile_icon(self, p: QPainter, icon_name: str, cx: float, cy: float, size: float, color: QColor) -> None:
        """Renders vector hardware icon for grid and strip tiles."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if icon_name == "cpu":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 5.0, cy - 5.0, 10.0, 10.0), 1.5, 1.5)
            for d in [-3.0, 0.0, 3.0]:
                p.drawLine(QPointF(cx + d, cy - 7.0), QPointF(cx + d, cy - 5.0))
                p.drawLine(QPointF(cx + d, cy + 5.0), QPointF(cx + d, cy + 7.0))
                p.drawLine(QPointF(cx - 7.0, cy + d), QPointF(cx - 5.0, cy + d))
                p.drawLine(QPointF(cx + 5.0, cy + d), QPointF(cx + 7.0, cy + d))
        elif icon_name == "ram":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 6.0, cy - 3.5, 12.0, 7.0), 1.0, 1.0)
            p.drawLine(QPointF(cx - 2.0, cy - 3.5), QPointF(cx - 2.0, cy - 1.0))
            p.drawLine(QPointF(cx + 2.0, cy - 3.5), QPointF(cx + 2.0, cy - 1.0))
        elif icon_name == "gpu":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 6.0, cy - 4.0, 12.0, 8.0), 1.5, 1.5)
            p.drawEllipse(QPointF(cx, cy), 2.2, 2.2)
        elif icon_name == "net_up":
            p.setPen(QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy + 4.5), QPointF(cx, cy - 4.5))
            p.drawLine(QPointF(cx - 3.0, cy - 1.5), QPointF(cx, cy - 4.5))
            p.drawLine(QPointF(cx + 3.0, cy - 1.5), QPointF(cx, cy - 4.5))
        elif icon_name == "net_down":
            p.setPen(QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx, cy + 4.5))
            p.drawLine(QPointF(cx - 3.0, cy + 1.5), QPointF(cx, cy + 4.5))
            p.drawLine(QPointF(cx + 3.0, cy + 1.5), QPointF(cx, cy + 4.5))
        elif icon_name == "disk":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy - 3.0), 5.5, 2.0)
            p.drawArc(QRectF(cx - 5.5, cy - 2.0, 11.0, 4.0), 180 * 16, 180 * 16)
            p.drawArc(QRectF(cx - 5.5, cy + 1.0, 11.0, 4.0), 180 * 16, 180 * 16)
            p.drawLine(QPointF(cx - 5.5, cy - 3.0), QPointF(cx - 5.5, cy + 3.0))
            p.drawLine(QPointF(cx + 5.5, cy - 3.0), QPointF(cx + 5.5, cy + 3.0))
        else:
            p.setPen(QPen(color, 1.2))
            p.drawEllipse(QPointF(cx, cy), 3.5, 3.5)

        p.restore()

    def _draw_hardware_grid(self, p: QPainter, w: float, h: float,
                            now: float, t: float, dark: bool,
                            primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded Hardware Monitor 2x3 Grid Card (390x188 px):
        6 modular tiles (radius ~17px, fill #FFFFFF17, border 1px #FFFFFF0A):
        1. CPU %: icon, label "CPU", value, 4px progress bar in sky blue #45C1EE (shifts to red #F0443A if >90%).
        2. RAM: icon, label "RAM", value (e.g. "8.2 / 16.0 GB"), 4px bar in amber #F5A524 (shifts to red if >90%).
        3. GPU %: icon, label "GPU", value (or "--"), 4px bar in red #F0443A.
        4. NET UP: up arrow icon, label "NET UP", value (e.g. "1.2 MB/s"), no bar.
        5. NET DOWN: down arrow icon, label "NET DOWN", value (e.g. "14.5 MB/s"), no bar.
        6. DISK %: icon, label "DISK", value, 4px bar in sky blue #45C1EE (shifts to red if >90%).
        """
        scale = self._get_size_scale()

        m = self._cached_hardware_metrics
        if not m:
            try:
                from hardware_adapter import get_hardware_adapter
                m = get_hardware_adapter().get_metrics()
            except Exception:
                m = None

        cpu_val = (m.cpu_percent if m and m.cpu_percent > 0 else 38.0)
        ram_used = (m.ram_used_gb if m and m.ram_used_gb > 0 else 8.2)
        ram_total = (m.ram_total_gb if m and m.ram_total_gb > 0 else 16.0)
        ram_pct = (m.ram_percent if m and m.ram_percent > 0 else (ram_used / ram_total * 100.0))
        gpu_pct = (m.gpu_percent if m and m.gpu_percent is not None else 24.0)
        disk_val = (m.disk_percent if m and m.disk_percent > 0 else 54.0)

        try:
            from hardware_adapter import format_speed
            net_up_str = format_speed(m.net_up_bytes_sec) if m and m.net_up_bytes_sec > 0 else "1.2 MB/s"
            net_down_str = format_speed(m.net_down_bytes_sec) if m and m.net_down_bytes_sec > 0 else "14.5 MB/s"
        except Exception:
            net_up_str = "1.2 MB/s"
            net_down_str = "14.5 MB/s"

        tile_w = 108.0 * scale
        tile_h = 68.0 * scale
        tile_r = 17.0 * scale
        gap_x = 8.0 * scale
        gap_y = 10.0 * scale

        margin_x = 18.0 * scale
        margin_y = (h - (2.0 * tile_h + gap_y)) / 2.0

        tiles_data = [
            ("cpu", "CPU", f"{round(cpu_val):.0f}%", cpu_val, True, "#F0443A" if cpu_val > 90 else "#45C1EE"),
            ("ram", "RAM", f"{ram_used:.1f} / {ram_total:.1f} GB", ram_pct, True, "#F0443A" if ram_pct > 90 else "#F5A524"),
            ("gpu", "GPU", f"{round(gpu_pct):.0f}%" if gpu_pct is not None else "--", gpu_pct if gpu_pct is not None else 0.0, True, "#F0443A"),
            ("net_up", "NET UP", net_up_str, 0.0, False, ""),
            ("net_down", "NET DOWN", net_down_str, 0.0, False, ""),
            ("disk", "DISK", f"{round(disk_val):.0f}%", disk_val, True, "#F0443A" if disk_val > 90 else "#45C1EE"),
        ]

        tile_bg = QColor(255, 255, 255, 23 if dark else 55)
        tile_border = QColor(255, 255, 255, 10 if dark else 25)

        for idx, (icon_name, label, val_text, pct, has_bar, bar_color) in enumerate(tiles_data):
            row = idx // 3
            col = idx % 3
            tx = margin_x + col * (tile_w + gap_x)
            ty = margin_y + row * (tile_h + gap_y)
            tr = QRectF(tx, ty, tile_w, tile_h)
            t_path = create_cached_squircle_path(tr, tile_r)

            # Inset tile fill #FFFFFF17 and border 1px #FFFFFF0A
            p.fillPath(t_path, QBrush(tile_bg))
            p.strokePath(t_path, QPen(tile_border, 1.0))

            # Icon & Label
            icx = tx + 14.0 * scale
            icy = ty + 16.0 * scale
            self._draw_hardware_tile_icon(p, icon_name, icx, icy, 12.0 * scale, QColor("#8E9094"))

            p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
            p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
            p.drawText(QRectF(tx + 24.0 * scale, ty + 8.0 * scale, tile_w - 30.0 * scale, 16.0 * scale),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

            # Value
            if label == "RAM":
                p.setFont(QFont("Segoe UI Variable Display", int(10.5 * scale), QFont.Weight.Bold))
            elif len(val_text) >= 8:
                p.setFont(QFont("Segoe UI Variable Display", int(12.0 * scale), QFont.Weight.Bold))
            else:
                p.setFont(QFont("Segoe UI Variable Display", int(15.0 * scale), QFont.Weight.Bold))
            p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
            p.drawText(QRectF(tx + 10.0 * scale, ty + 24.0 * scale, tile_w - 20.0 * scale, 22.0 * scale),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val_text)

            # 4px Progress Bar
            if has_bar:
                bar_x = tx + 10.0 * scale
                bar_y = ty + tile_h - 12.0 * scale
                bar_w = tile_w - 20.0 * scale
                bar_h = 4.0 * scale
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(255, 255, 255, 22 if dark else 40)))
                p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2.0 * scale, 2.0 * scale)

                fill_w = bar_w * min(1.0, max(0.0, pct / 100.0))
                if fill_w > 0:
                    p.setBrush(QBrush(QColor(bar_color)))
                    p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2.0 * scale, 2.0 * scale)

    def _draw_hardware_strip(self, p: QPainter, w: float, h: float,
                             now: float, t: float, dark: bool,
                             primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Hardware 5-Item Telemetry Strip (446x69 px, radius 28px):
        5 horizontal segments: FPS (JARVIS tracking FPS), CPU, RAM, GPU, DISK.
        Switchable via island_hardware_layout ("grid" vs "strip").
        """
        scale = self._get_size_scale()

        m = self._cached_hardware_metrics
        if not m:
            try:
                from hardware_adapter import get_hardware_adapter
                m = get_hardware_adapter().get_metrics()
            except Exception:
                m = None

        fps_val = (m.fps if m and m.fps > 0 else 60.0)
        cpu_val = (m.cpu_percent if m and m.cpu_percent > 0 else 38.0)
        ram_used = (m.ram_used_gb if m and m.ram_used_gb > 0 else 8.2)
        ram_total = (m.ram_total_gb if m and m.ram_total_gb > 0 else 16.0)
        ram_pct = (m.ram_percent if m and m.ram_percent > 0 else (ram_used / ram_total * 100.0))
        gpu_pct = (m.gpu_percent if m and m.gpu_percent is not None else 24.0)
        disk_val = (m.disk_percent if m and m.disk_percent > 0 else 54.0)

        segments = [
            ("FPS", f"{round(fps_val)} FPS", "#45C1EE"),
            ("CPU", f"{round(cpu_val):.0f}%", "#F0443A" if cpu_val > 90 else "#45C1EE"),
            ("RAM", f"{round(ram_pct):.0f}%", "#F0443A" if ram_pct > 90 else "#F5A524"),
            ("GPU", f"{round(gpu_pct):.0f}%" if gpu_pct is not None else "--", "#F0443A"),
            ("DISK", f"{round(disk_val):.0f}%", "#F0443A" if disk_val > 90 else "#45C1EE"),
        ]

        mx = 14.0 * scale
        seg_w = (w - 2.0 * mx) / 5.0
        line_col = QColor(255, 255, 255, 10 if dark else 25)

        for idx, (label, val_text, accent_hex) in enumerate(segments):
            sx = mx + idx * seg_w
            if idx > 0:
                p.setPen(QPen(line_col, 1.0))
                p.drawLine(QPointF(sx, 16.0 * scale), QPointF(sx, h - 16.0 * scale))

            p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
            p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
            p.drawText(QRectF(sx, 14.0 * scale, seg_w, 16.0 * scale), Qt.AlignmentFlag.AlignCenter, label)

            p.setFont(QFont("Segoe UI Variable Display", int(15.0 * scale), QFont.Weight.Bold))
            p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
            p.drawText(QRectF(sx, 32.0 * scale, seg_w, 24.0 * scale), Qt.AlignmentFlag.AlignCenter, val_text)

    def _draw_jarvis_card(self, p: QPainter, w: float, h: float,
                          now: float, t: float, dark: bool,
                          primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded JARVIS Assistant Card (390x188 px):
        - Header: "J.A.R.V.I.S." with pulsing green status dot.
        - Modular tiles (radius ~17px, fill #FFFFFF17, border 1px #FFFFFF0A):
          1. Assistant Status tile: Tracking active/paused, camera status, mic state.
          2. Quick toggle button: "Pause Tracking" / "Resume Tracking" (interactive target).
          3. Last Command tile: Displaying the last spoken/executed command and its result status.
        """
        scale = self._get_size_scale()
        lx = 22.0 * scale
        content_w = (w - 28.0 * scale) - lx
        tile_r = 17.0 * scale

        tile_bg = QColor(255, 255, 255, 23 if dark else 55)
        tile_border = QColor(255, 255, 255, 10 if dark else 25)

        # ── 1. Header: "J.A.R.V.I.S." with pulsing green status dot ──────────
        p.setFont(QFont("Segoe UI Variable Display", int(13.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(int(lx), int(29.0 * scale), "J.A.R.V.I.S.")
        adv = p.fontMetrics().horizontalAdvance("J.A.R.V.I.S.")

        # Pulsing green status dot
        dot_cx = lx + adv + 10.0 * scale
        dot_cy = 24.0 * scale
        dot_r = 3.2 * scale
        pulse_alpha = int(180 + 75 * math.sin(t * 3.5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(52, 199, 89, int(pulse_alpha * 0.35))))
        p.drawEllipse(QPointF(dot_cx, dot_cy), dot_r + 2.5 * scale, dot_r + 2.5 * scale)
        p.setBrush(QBrush(QColor(52, 199, 89, pulse_alpha)))
        p.drawEllipse(QPointF(dot_cx, dot_cy), dot_r, dot_r)

        # Trailing category tag
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094"))
        p.drawText(QRectF(lx, 15.0 * scale, content_w, 16.0 * scale),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "VOICE & VISION")

        # ── 2. Row 1: Status Tile + Quick Toggle Button ─────────────────────
        ty1 = 38.0 * scale
        th1 = 66.0 * scale
        gap = 8.0 * scale
        status_w = 202.0 * scale
        btn_w = content_w - status_w - gap
        btn_x = lx + status_w + gap

        # Tile 1: Assistant Status Tile
        s_rect = QRectF(lx, ty1, status_w, th1)
        s_path = create_cached_squircle_path(s_rect, tile_r)
        p.fillPath(s_path, QBrush(tile_bg))
        p.strokePath(s_path, QPen(tile_border, 1.0))

        # Status tile title
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(QRectF(lx + 12.0 * scale, ty1 + 7.0 * scale, status_w - 24.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "ASSISTANT STATUS")

        # Status metrics
        is_tracking = self.active and not getattr(self, "tracking_paused", False)
        # Tracking indicator dot + text
        track_dot_y = ty1 + 30.0 * scale
        track_dot_col = QColor("#34C759") if is_tracking else QColor("#FF9F0A")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_dot_col))
        p.drawEllipse(QPointF(lx + 16.0 * scale, track_dot_y), 3.0 * scale, 3.0 * scale)

        p.setFont(QFont("Segoe UI Variable Text", int(9.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        track_txt = "Tracking: Active" if is_tracking else "Tracking: Paused"
        p.drawText(QRectF(lx + 24.0 * scale, track_dot_y - 8.0 * scale, status_w - 32.0 * scale, 16.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, track_txt)

        # Camera & Mic indicators
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.Normal))
        p.setPen(QColor("#8E9094"))
        cam_mic_txt = "Camera: 60 FPS   •   Mic: Online"
        p.drawText(QRectF(lx + 12.0 * scale, ty1 + 45.0 * scale, status_w - 24.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, cam_mic_txt)

        # Tile 2: Quick Toggle Button (Interactive target)
        b_rect = QRectF(btn_x, ty1, btn_w, th1)
        b_path = create_cached_squircle_path(b_rect, tile_r)

        is_hover = (self._hovered_btn == "jarvis_toggle_tracking")
        is_flash = (now < self._btn_flash_until and is_hover)

        if is_flash:
            b_bg = QColor(0, 229, 255, 65)
            b_stroke = QPen(QColor(0, 229, 255, 220), 1.4 * scale)
        elif is_hover:
            b_bg = QColor(255, 255, 255, 42 if dark else 75)
            b_stroke = QPen(QColor(0, 229, 255, 180), 1.2 * scale)
        else:
            b_bg = tile_bg
            b_stroke = QPen(tile_border, 1.0)

        p.fillPath(b_path, QBrush(b_bg))
        p.strokePath(b_path, b_stroke)

        # Dwell progress ring around button if dwelling
        if is_hover and self._btn_dwell_elapsed > 0.03:
            dwell_pen = QPen(COLOR.HUD_ONLY.neon_cyan, 2.0 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(dwell_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            d_rect = b_rect.adjusted(-2.5 * scale, -2.5 * scale, 2.5 * scale, 2.5 * scale)
            p.drawRoundedRect(d_rect, tile_r + 2.5 * scale, tile_r + 2.5 * scale)

        # Icon inside button
        ic_cx = btn_x + btn_w / 2.0
        ic_cy = ty1 + 20.0 * scale
        if is_tracking:
            # Pause icon (two vertical bars)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#FF9F0A")))
            bw = 3.0 * scale
            bh = 10.0 * scale
            p.drawRoundedRect(QRectF(ic_cx - 4.5 * scale, ic_cy - bh / 2.0, bw, bh), 1.0 * scale, 1.0 * scale)
            p.drawRoundedRect(QRectF(ic_cx + 1.5 * scale, ic_cy - bh / 2.0, bw, bh), 1.0 * scale, 1.0 * scale)
            btn_txt = "Pause Tracking"
        else:
            # Play / Resume triangle
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#34C759")))
            tri = QPolygonF([
                QPointF(ic_cx - 4.0 * scale, ic_cy - 5.5 * scale),
                QPointF(ic_cx + 5.5 * scale, ic_cy),
                QPointF(ic_cx - 4.0 * scale, ic_cy + 5.5 * scale)
            ])
            p.drawPolygon(tri)
            btn_txt = "Resume Tracking"

        p.setFont(QFont("Segoe UI Variable Text", int(9.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(QRectF(btn_x + 4.0 * scale, ty1 + 31.0 * scale, btn_w - 8.0 * scale, 16.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, btn_txt)

        p.setFont(QFont("Segoe UI Variable Text", int(7.5 * scale), QFont.Weight.Normal))
        p.setPen(QColor("#8E9094"))
        p.drawText(QRectF(btn_x + 4.0 * scale, ty1 + 47.0 * scale, btn_w - 8.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, "Click / Dwell")

        # ── 3. Row 2: Last Command Tile ─────────────────────────────────────
        ty2 = ty1 + th1 + 8.0 * scale
        th2 = 60.0 * scale
        cmd_rect = QRectF(lx, ty2, content_w, th2)
        cmd_path = create_cached_squircle_path(cmd_rect, tile_r)

        p.fillPath(cmd_path, QBrush(tile_bg))
        p.strokePath(cmd_path, QPen(tile_border, 1.0))

        # Title: "LAST COMMAND"
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(QRectF(lx + 12.0 * scale, ty2 + 7.0 * scale, 140.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "LAST COMMAND")

        # Pill badge on right: "✓ Executed"
        badge_w = 78.0 * scale
        badge_h = 17.0 * scale
        badge_rx = lx + content_w - badge_w - 10.0 * scale
        badge_ry = ty2 + 6.0 * scale
        bdg_rect = QRectF(badge_rx, badge_ry, badge_w, badge_h)
        bdg_path = create_cached_squircle_path(bdg_rect, badge_h / 2.0)
        p.fillPath(bdg_path, QBrush(QColor(52, 199, 89, 35)))
        p.strokePath(bdg_path, QPen(QColor(52, 199, 89, 90), 1.0))

        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(QColor("#34C759"))
        p.drawText(bdg_rect, Qt.AlignmentFlag.AlignCenter, "✓ Executed")

        # Command text
        p.setFont(QFont("Segoe UI Variable Display", int(11.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        user_cmd = f"“{self.user_prompt or 'Run system diagnostic'}”"
        p.drawText(QRectF(lx + 12.0 * scale, ty2 + 22.0 * scale, content_w - 24.0 * scale, 18.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, user_cmd)

        # Result summary
        p.setFont(QFont("Segoe UI Variable Text", int(9.5 * scale), QFont.Weight.Normal))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        reply_txt = self.say_text or "All core subsystems nominal, sir."
        p.drawText(QRectF(lx + 12.0 * scale, ty2 + 39.0 * scale, content_w - 24.0 * scale, 15.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, reply_txt)

    def _draw_page_dots(self, p: QPainter, w: float, h: float, dark: bool) -> None:
        """
        Draws the vertical column of 5 page indicator dots along the right edge:
        - Position: x = w - 16 px, vertically centered
        - Pages: Media, Time/Calendar, Weather, Hardware, Jarvis
        - Active dot: 100% solid white (or near-black in light mode)
        - Inactive dot: 30% alpha
        """
        scale = self._get_size_scale()
        dot_x = w - 16.0 * scale
        num_dots = len(self.card_pages)
        dot_spacing = 14.0 * scale
        total_h = (num_dots - 1) * dot_spacing
        start_y = (h - total_h) / 2.0
        r_dot = 3.0 * scale

        curr_page = self.expanded_card or "weather"
        if curr_page == "calendar":
            curr_page = "time"
        elif curr_page in ("hardware_grid", "hardware_strip"):
            curr_page = "hardware"
        elif curr_page in ("sending", "jarvis_card"):
            curr_page = "jarvis"

        active_idx = self.card_pages.index(curr_page) if curr_page in self.card_pages else 2

        active_col = QColor(255, 255, 255, 255) if dark else QColor(28, 28, 30, 255)
        inactive_col = QColor(255, 255, 255, 77) if dark else QColor(0, 0, 0, 77)

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(num_dots):
            dy = start_y + i * dot_spacing
            if i == active_idx:
                p.setBrush(QBrush(active_col))
            else:
                p.setBrush(QBrush(inactive_col))
            p.drawEllipse(QPointF(dot_x, dy), r_dot, r_dot)

    def _dot_at(self, pos: QPointF | QPoint) -> Optional[int]:
        """Hit test for the page dots on expanded cards."""
        if not self.expanded_card:
            return None
        w = max(float(self.width()), self.spring_w.target)
        h = max(float(self.height()), self.spring_h.target)
        scale = self._get_size_scale()
        dot_x = w - 16.0 * scale
        num_dots = len(self.card_pages)
        dot_spacing = 14.0 * scale
        total_h = (num_dots - 1) * dot_spacing
        start_y = (h - total_h) / 2.0
        hit_radius = 12.0 * scale

        for i in range(num_dots):
            dy = start_y + i * dot_spacing
            if math.hypot(pos.x() - dot_x, pos.y() - dy) <= hit_radius:
                return i
        return None

    def wheelEvent(self, event) -> None:
        """Mouse wheel scrolling to switch between expanded card pages."""
        if self.expanded_card:
            curr_page = self.expanded_card
            if curr_page == "calendar":
                curr_page = "time"
            elif curr_page in ("hardware_grid", "hardware_strip"):
                curr_page = "hardware"
            elif curr_page in ("sending", "jarvis_card"):
                curr_page = "jarvis"

            idx = self.card_pages.index(curr_page) if curr_page in self.card_pages else 0
            delta = event.angleDelta().y()
            if delta < 0:
                new_idx = (idx + 1) % len(self.card_pages)
            elif delta > 0:
                new_idx = (idx - 1) % len(self.card_pages)
            else:
                return
            self.expand_card(self.card_pages[new_idx])
            event.accept()
            return
        super().wheelEvent(event)

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
        """Sets thumbnail from raw bytes and samples dominant & average gradient colors."""
        pix = QPixmap()
        if pix.loadFromData(data):
            self.media_album_art = pix
            img = pix.toImage().scaled(32, 32)
            r, g, b = 0, 0, 0
            n = img.width() * img.height()
            r_top, g_top, b_top, n_top = 0, 0, 0, 0
            r_bot, g_bot, b_bot, n_bot = 0, 0, 0, 0
            half_h = img.height() // 2
            for y in range(img.height()):
                for x in range(img.width()):
                    c = img.pixelColor(x, y)
                    r += c.red()
                    g += c.green()
                    b += c.blue()
                    if y < half_h:
                        r_top += c.red()
                        g_top += c.green()
                        b_top += c.blue()
                        n_top += 1
                    else:
                        r_bot += c.red()
                        g_bot += c.green()
                        b_bot += c.blue()
                        n_bot += 1
            if n > 0:
                avg_col = QColor(int(r / n), int(g / n), int(b / n))
                top_col = QColor(int(r_top / n_top), int(g_top / n_top), int(b_top / n_top)) if n_top > 0 else avg_col
                bot_col = QColor(int(r_bot / n_bot), int(g_bot / n_bot), int(b_bot / n_bot)) if n_bot > 0 else avg_col
                try:
                    from media_adapter import extract_dominant_color
                    hex_c = extract_dominant_color(data)
                    if hex_c and hex_c.startswith("#"):
                        self.media_dominant_color = QColor(hex_c)
                    else:
                        self.media_dominant_color = top_col
                except Exception:
                    self.media_dominant_color = top_col

                self.media_average_color = avg_col
                self.media_secondary_color = bot_col
            self._icon_cache.clear()
            self.update()

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

        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        ui_cancel_hint = getattr(cfg, "ui_cancel_hint_enabled", True)
        elapsed = now - self._listening_start_t if self._listening_start_t > 0.0 else 0.0

        if ui_cancel_hint and elapsed > 3.0:
            hint_font = qfont(TYPE.caption_2)
            p.setFont(hint_font)
            hint_col = QColor(T.color.secondary_label.resolve(dark))
            if not reduce:
                hint_col.setAlphaF(min(1.0, (elapsed - 3.0) / 0.35))
            p.setPen(hint_col)
            hint_rect = QRectF(text_x, cy - 14.0, (w - 68.0) - text_x, 14.0)
            p.drawText(hint_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "Say 'cancel' to dismiss")

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

    def _get_waveform_colors(self) -> list[QColor]:
        """Returns 7 QColor entries for the audio-reactive waveform bars."""
        c_name = str(getattr(self, "waveform_color", "blue")).lower().strip()
        if c_name == "multicolor":
            return [
                QColor(175, 82, 222, 200),
                QColor(0,  240, 255, 220),
                QColor(0,  122, 255, 230),
                QColor(255, 255, 255, 240),
                QColor(0,  122, 255, 230),
                QColor(0,  240, 255, 220),
                QColor(175, 82, 222, 200),
            ]
        if c_name in ("blue", "default"):
            return [
                QColor(0, 110, 230, 200),
                QColor(0, 180, 255, 225),
                QColor(0, 122, 255, 235),
                QColor(210, 240, 255, 255),
                QColor(0, 122, 255, 235),
                QColor(0, 180, 255, 225),
                QColor(0, 110, 230, 200),
            ]
        if c_name == "cyan":
            return [
                QColor(0, 160, 200, 200),
                QColor(0, 220, 245, 225),
                QColor(50, 200, 240, 240),
                QColor(225, 250, 255, 255),
                QColor(50, 200, 240, 240),
                QColor(0, 220, 245, 225),
                QColor(0, 160, 200, 200),
            ]
        if c_name == "green":
            return [
                QColor(30, 170, 70, 200),
                QColor(52, 215, 95, 225),
                QColor(40, 195, 80, 240),
                QColor(225, 255, 235, 255),
                QColor(40, 195, 80, 240),
                QColor(52, 215, 95, 225),
                QColor(30, 170, 70, 200),
            ]
        if c_name == "orange":
            return [
                QColor(225, 120, 0, 200),
                QColor(255, 175, 40, 225),
                QColor(255, 150, 10, 240),
                QColor(255, 245, 225, 255),
                QColor(255, 150, 10, 240),
                QColor(255, 175, 40, 225),
                QColor(225, 120, 0, 200),
            ]
        if c_name == "purple":
            return [
                QColor(140, 60, 185, 200),
                QColor(190, 105, 235, 225),
                QColor(175, 82, 222, 240),
                QColor(250, 235, 255, 255),
                QColor(175, 82, 222, 240),
                QColor(190, 105, 235, 225),
                QColor(140, 60, 185, 200),
            ]
        if c_name == "red":
            return [
                QColor(215, 45, 35, 200),
                QColor(255, 95, 85, 225),
                QColor(255, 69, 58, 240),
                QColor(255, 235, 235, 255),
                QColor(255, 69, 58, 240),
                QColor(255, 95, 85, 225),
                QColor(215, 45, 35, 200),
            ]
        if c_name == "pink":
            return [
                QColor(215, 30, 70, 200),
                QColor(255, 80, 120, 225),
                QColor(255, 45, 85, 240),
                QColor(255, 235, 245, 255),
                QColor(255, 45, 85, 240),
                QColor(255, 80, 120, 225),
                QColor(215, 30, 70, 200),
            ]
        if c_name in ("monochrome", "white", "gray", "grey"):
            return [
                QColor(160, 160, 165, 200),
                QColor(210, 210, 215, 225),
                QColor(230, 230, 235, 240),
                QColor(255, 255, 255, 255),
                QColor(230, 230, 235, 240),
                QColor(210, 210, 215, 225),
                QColor(160, 160, 165, 200),
            ]

        base = QColor(c_name)
        if not base.isValid():
            base = QColor(0, 122, 255)
        r, g, b = base.red(), base.green(), base.blue()
        c_dark = QColor(int(r * 0.78), int(g * 0.78), int(b * 0.78), 200)
        c_bright = QColor(min(255, int(r * 1.25 + 25)), min(255, int(g * 1.25 + 25)), min(255, int(b * 1.25 + 25)), 225)
        c_main = QColor(r, g, b, 240)
        c_crest = QColor(min(255, int(r * 0.35 + 255 * 0.65)), min(255, int(g * 0.35 + 255 * 0.65)), min(255, int(b * 0.35 + 255 * 0.65)), 255)
        return [c_dark, c_bright, c_main, c_crest, c_main, c_bright, c_dark]

    def _get_waveform_base_color(self) -> QColor:
        """Returns the primary base QColor for waveform accent elements."""
        cols = self._get_waveform_colors()
        return cols[2]

    def _draw_waveform_bars(self, p: QPainter, w: float, cy: float) -> None:
        n       = 7
        bar_w   = 3.0
        gap     = 3.5
        total_w = n * bar_w + (n - 1) * gap
        wx0     = w - total_w - 18.0
        max_h   = 18.0

        colors = self._get_waveform_colors()
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

        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        ui_progressive_shimmer = getattr(cfg, "ui_progressive_shimmer_enabled", True)

        cur_x = x
        for word, arrival_t in self._transcript_words:
            word_w = fm.horizontalAdvance(word)
            if cur_x + word_w > x + max_w:
                p.setPen(primary)
                p.drawText(int(cur_x), int(y), "…")
                break

            if reduce:
                alpha = 1.0
                col = QColor(primary)
            else:
                age = max(0.0, now - arrival_t)
                alpha = min(1.0, max(0.0, age / 0.140))
                col = QColor(primary)
                if ui_progressive_shimmer and age < 2.0:
                    recency = 1.0 - (age / 2.0)
                    shimmer_wave = 0.5 + 0.5 * math.sin(now * 7.5 - cur_x * 0.06)
                    boost = int(55.0 * recency * shimmer_wave)
                    r = min(255, col.red() + boost)
                    g = min(255, col.green() + boost)
                    b = min(255, col.blue() + boost)
                    col = QColor(r, g, b)

            col.setAlphaF(alpha)
            p.setPen(col)
            p.drawText(int(cur_x), int(y), word)
            cur_x += word_w + space_w

    def _draw_thinking(self, p: QPainter, w: float, h: float, cy: float,
                       t: float, dark: bool, primary: QColor, accent: QColor, CYAN: QColor,
                       now: float = 0.0) -> None:
        if now == 0.0:
            now = time.time()

        self._draw_siri_orb(p, orb_cx=28.0, orb_cy=cy, t=t, speed_mult=2.8)

        text_x = 48.0
        top_font = qfont(TYPE.callout)
        top_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(top_font)
        p.setPen(accent)
        p.drawText(int(text_x), int(cy - 2), "Jarvis")

        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        ui_status_line = getattr(cfg, "ui_status_line_enabled", True)
        ui_cancel_hint = getattr(cfg, "ui_cancel_hint_enabled", True)
        reduce = T.reduce_motion()

        elapsed = now - self._thinking_start_t if self._thinking_start_t > 0.0 else 0.0

        # Thinking Status Line (> 1.5s): update secondary status line
        if ui_status_line and elapsed > 1.5:
            thinking_label = getattr(self, "thinking_status", None) or getattr(self, "status_text", None) or "Consulting Groq LLM..."
        else:
            thinking_label = "Thinking..."

        sub_font = qfont(TYPE.caption_1)
        p.setFont(sub_font)
        p.setPen(primary)
        p.drawText(int(text_x), int(cy + 11), thinking_label)

        # Cancellation Hint (> 3.0s): footnote/caption style
        if ui_cancel_hint and elapsed > 3.0:
            hint_font = qfont(TYPE.caption_2)
            p.setFont(hint_font)
            hint_col = QColor(T.color.secondary_label.resolve(dark))
            if not reduce:
                hint_col.setAlphaF(min(1.0, (elapsed - 3.0) / 0.35))
            p.setPen(hint_col)
            hint_rect = QRectF(text_x, cy - 14.0, (w - 46.0) - text_x, 14.0)
            p.drawText(hint_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "Say 'cancel' to dismiss")

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
