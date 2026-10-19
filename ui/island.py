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

from ui.island_parts.common import (  # noqa: F401  (re-exported for existing importers)
    _PATH_CACHE, create_cached_squircle_path, IslandState, _SIZE,
    SPRING_K_RESP04, SPRING_C_DAMP07,
)
from ui.island_parts.compact import CompactPillMixin
from ui.island_parts.media import MediaPageMixin
from ui.island_parts.calendar import CalendarPageMixin
from ui.island_parts.weather import WeatherPageMixin
from ui.island_parts.hardware import HardwarePageMixin
from ui.island_parts.jarvis import JarvisPageMixin
from ui.island_parts.paging import PagingMixin




# ──────────────────────────────────────────────────────────────────────────────
# Dynamic Island widget
# ──────────────────────────────────────────────────────────────────────────────

class AppleDynamicIsland(
    CompactPillMixin, MediaPageMixin, CalendarPageMixin, WeatherPageMixin,
    HardwarePageMixin, JarvisPageMixin, PagingMixin, QWidget,
):
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
        # Worker health (None = unknown -> legacy activity dot, not health).
        self.worker_health_state: str | None = None
        self.worker_health_reason: str = ""
        self._wh_from_col: QColor | None = None
        self._wh_t0: float = 0.0
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

    _WH_COLORS = {"ok": "#34C759", "paused": "#8E8E93", "down": "#FF453A"}

    def set_worker_health(self, state: str, reason: str = "") -> None:
        """Worker-health status dot (GUI thread only; posted via Overlay)."""
        state = state if state in self._WH_COLORS else None
        reason = str(reason or "")[:28]
        if state == self.worker_health_state and reason == self.worker_health_reason:
            return
        # Crossfade origin = colour currently shown (150 ms, instant if reduce_motion).
        self._wh_from_col = self._worker_health_color(time.monotonic())
        self._wh_t0 = time.monotonic()
        self.worker_health_state = state
        self.worker_health_reason = reason
        self.update()

    def _worker_health_color(self, now_m: float) -> QColor | None:
        st = self.worker_health_state
        if st is None:
            return None
        target = QColor(self._WH_COLORS[st])
        src = self._wh_from_col
        if src is None or T.reduce_motion():
            return target
        k = (now_m - self._wh_t0) / 0.15
        if k >= 1.0:
            return target
        k = max(0.0, k)
        return QColor(
            int(src.red() + (target.red() - src.red()) * k),
            int(src.green() + (target.green() - src.green()) * k),
            int(src.blue() + (target.blue() - src.blue()) * k),
        )

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
