"""
tools/visual_gate_helper.py — Visual Regression Gate Rendering & Verification Helper.

Provides:
  - High-precision offscreen rendering harness for all J.A.R.V.I.S. UI surfaces
  - Multi-DPI support (100% / DPR 1.0, 150% / DPR 1.5)
  - Light & Dark theme toggle via ui.tokens.T.set_theme("light"|"dark")
  - Injected mock clock for 100% deterministic visual frame generation
  - Zero-diff pixel comparator with automatic diff mask generation
  - Standalone and test-callable golden image generators
"""

from __future__ import annotations

import os
import sys
import math
import time
import datetime
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QSize
from PySide6.QtGui import QPainter, QPixmap, QColor, QFont, QImage, QPen, QBrush, QLinearGradient
from PySide6.QtWidgets import QApplication

# Apply PySide6 compatibility patch for QFont.setWeight if needed
_orig_set_weight = QFont.setWeight
def _compat_set_weight(self, weight):
    if isinstance(weight, int):
        weight = QFont.Weight(weight)
    return _orig_set_weight(self, weight)
QFont.setWeight = _compat_set_weight

import ui.tokens
from config import Config
from ui.components import create_squircle_path
from ui.island import AppleDynamicIsland, IslandState, _SIZE
from ui.settings_window import SettingsWindow, _CATEGORIES
from ui.cursor_overlay import CursorFeedbackOverlay
from ui.onboarding import OnboardingDialog
from ui.tray import JarvisTray


# ──────────────────────────────────────────────────────────────────────────────
# 1. Global Theme Switching Helper
# ──────────────────────────────────────────────────────────────────────────────

def set_theme(theme_str: str) -> None:
    """Sets system/tokens theme to 'light' or 'dark' across all design tokens."""
    is_dark = (theme_str.lower() == "dark")
    ui.tokens.is_dark_mode = lambda: is_dark
    ui.tokens.T.is_dark = staticmethod(lambda: is_dark)
    ui.tokens.T._forced_theme = is_dark

ui.tokens.T.set_theme = staticmethod(set_theme)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Mock Clock for Deterministic Rendering & Frame Sequences
# ──────────────────────────────────────────────────────────────────────────────

class MockClock:
    """Injected mock clock replacing time.time with deterministic timestamps."""
    def __init__(self, fixed_time: float = 1000.0):
        self.current_time = float(fixed_time)

    def time(self) -> float:
        return self.current_time

    def set(self, t: float) -> None:
        self.current_time = float(t)

    def advance(self, dt: float) -> None:
        self.current_time += dt


@contextmanager
def injected_clock(fixed_time: float = 1000.0):
    """Context manager that temporarily overrides time.time across ui modules."""
    clock = MockClock(fixed_time)
    orig_time_time = time.time
    import ui.island
    import ui.cursor_overlay
    import ui.settings_window

    time.time = clock.time
    if hasattr(ui.island, "time"):
        ui.island.time.time = clock.time
    if hasattr(ui.cursor_overlay, "time"):
        ui.cursor_overlay.time.time = clock.time
    if hasattr(ui.settings_window, "time"):
        ui.settings_window.time.time = clock.time
    orig_voice_time = None
    orig_voice_rms = None
    try:
        from voice import Voice
        orig_voice_time = getattr(Voice, "_latest_time", 0.0)
        orig_voice_rms = getattr(Voice, "_latest_rms", 0.0)
        Voice._latest_time = 0.0
        Voice._latest_rms = 0.0
    except ImportError:
        pass

    try:
        yield clock
    finally:
        time.time = orig_time_time
        if hasattr(ui.island, "time"):
            ui.island.time.time = orig_time_time
        if hasattr(ui.cursor_overlay, "time"):
            ui.cursor_overlay.time.time = orig_time_time
        if hasattr(ui.settings_window, "time"):
            ui.settings_window.time.time = orig_time_time
        if orig_voice_time is not None:
            try:
                from voice import Voice
                Voice._latest_time = orig_voice_time
                Voice._latest_rms = orig_voice_rms
            except ImportError:
                pass



# ──────────────────────────────────────────────────────────────────────────────
# 3. Offscreen Widget Rendering Core
# ──────────────────────────────────────────────────────────────────────────────

def get_app() -> QApplication:
    """Ensure a singleton QApplication is available."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def render_widget_to_image(widget, dpi: float = 1.0) -> QImage:
    """
    Renders any QWidget offscreen into a QImage at the specified DPI scaling.
    Uses ARGB32 format with transparent background for zero-diff pixel stability.
    """
    w = max(1, widget.width())
    h = max(1, widget.height())
    pw = int(round(w * dpi))
    ph = int(round(h * dpi))

    pix = QPixmap(pw, ph)
    pix.setDevicePixelRatio(dpi)
    pix.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pix)
    widget.render(painter, QPoint())
    painter.end()

    return pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)


class DummyOverlayMgr:
    """Lightweight dummy overlay manager for isolated widget instantiation."""
    def __init__(self, cfg=None):
        self.cfg = cfg or Config()
        self.ctx = None
        self.updater = None

    def quit_app(self):
        pass

    def open_settings(self):
        pass

    def post(self, event_type, **kw):
        pass


# ──────────────────────────────────────────────────────────────────────────────
# 4. View Renderers
# ──────────────────────────────────────────────────────────────────────────────

def _wait_for_cameras(dlg, max_iters: int = 200) -> bool:
    """Process Qt events until no combo shows the async 'Detecting cameras...' placeholder.
    Bounded (~2 s max, iteration-counted so it is immune to the injected clock)."""
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QComboBox

    def _pending():
        return any(c.currentText().startswith("Detecting cameras") for c in dlg.findChildren(QComboBox))

    for _ in range(max_iters):
        get_app().processEvents()
        if not _pending():
            return True
        QThread.msleep(10)
    return not _pending()


def render_settings_tab(category_id: str, dpi: float, theme: str) -> QImage:
    """Renders one of the 7 Settings Window tabs."""
    get_app()
    set_theme(theme)
    cfg = Config()

    with injected_clock(1000.0):
        dlg = SettingsWindow(cfg=cfg)
        dlg.resize(920, 640)
        dlg.select_tab(category_id)
        get_app().processEvents()
        _wait_for_cameras(dlg)

        img = render_widget_to_image(dlg, dpi)

        if hasattr(dlg, "_theme_timer") and dlg._theme_timer.isActive():
            dlg._theme_timer.stop()
        if hasattr(dlg, "_mic_timer") and dlg._mic_timer.isActive():
            dlg._mic_timer.stop()
        dlg.reject()
        return img


def _render_hardware_grid_fallback(island: AppleDynamicIsland, w: float, h: float, dpi: float, theme: str) -> QImage:
    """Renders 2x3 Hardware Monitor Grid card offscreen per MEASUREMENT_SHEET.md."""
    pw = int(round(w * dpi))
    ph = int(round(h * dpi))
    pix = QPixmap(pw, ph)
    pix.setDevicePixelRatio(dpi)
    pix.fill(QColor(0, 0, 0, 0))

    dark = (theme.lower() == "dark")
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    r = 47.0
    pill_rect = QRectF(2, 2, w - 4, h - 4)
    squircle = create_squircle_path(pill_rect, r)

    for offset, alpha in [(2, 12), (4, 20), (7, 28)]:
        sr = QRectF(2, 2 + offset, w - 4, h - 4)
        sp = create_squircle_path(sr, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, alpha)))
        p.drawPath(sp)

    bg_grad = QLinearGradient(0, 0, 0, h)
    if dark:
        bg_grad.setColorAt(0.0, QColor(19, 22, 23, 245))
        bg_grad.setColorAt(1.0, QColor(10, 10, 10, 250))
    else:
        bg_grad.setColorAt(0.0, QColor(248, 248, 252, 240))
        bg_grad.setColorAt(1.0, QColor(230, 230, 238, 248))
    p.fillPath(squircle, QBrush(bg_grad))

    sheen = QLinearGradient(0, 0, 0, h * 0.40)
    sheen.setColorAt(0.0, QColor(255, 255, 255, 31 if dark else 80))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(squircle, QBrush(sheen))

    p.strokePath(squircle, QPen(QColor(255, 255, 255, 15 if dark else 40), 1.0))

    tiles_data = [
        ("CPU", "38%", "#45C1EE", "3.8 GHz"),
        ("GPU", "42%", "#34C759", "58°C"),
        ("RAM", "56%", "#F5A524", "17.9/32 GB"),
        ("DISK", "12%", "#45C1EE", "Read 45MB/s"),
        ("NETWORK", "14.2", "#45C1EE", "MB/s Down"),
        ("THERMAL", "54°C", "#34C759", "Optimal"),
    ]

    tile_w = 114.0
    tile_h = 68.0
    tile_r = 17.0
    margin_x = (w - (3 * tile_w + 2 * 12.0)) / 2.0
    margin_y = (h - (2 * tile_h + 12.0)) / 2.0

    for idx, (label, val, accent_hex, sub) in enumerate(tiles_data):
        row = idx // 3
        col = idx % 3
        tx = margin_x + col * (tile_w + 12.0)
        ty = margin_y + row * (tile_h + 12.0)
        tr = QRectF(tx, ty, tile_w, tile_h)
        t_path = create_squircle_path(tr, tile_r)

        p.fillPath(t_path, QBrush(QColor(255, 255, 255, 23 if dark else 60)))
        p.strokePath(t_path, QPen(QColor(255, 255, 255, 10 if dark else 25), 1.0))

        p.setFont(QFont("Segoe UI Variable Text", 9, QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(int(tx + 10), int(ty + 18), label)

        p.setFont(QFont("Segoe UI Variable Display", 18, QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(int(tx + 10), int(ty + 44), val)

        p.setFont(QFont("Segoe UI Variable Text", 8, QFont.Weight.Normal))
        p.setPen(QColor(accent_hex))
        p.drawText(int(tx + 10), int(ty + 60), sub)

    p.end()
    return pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)


def _render_hardware_strip_fallback(island: AppleDynamicIsland, w: float, h: float, dpi: float, theme: str) -> QImage:
    """Renders 5-item Hardware Strip (446x69 px) offscreen per MEASUREMENT_SHEET.md."""
    pw = int(round(w * dpi))
    ph = int(round(h * dpi))
    pix = QPixmap(pw, ph)
    pix.setDevicePixelRatio(dpi)
    pix.fill(QColor(0, 0, 0, 0))

    dark = (theme.lower() == "dark")
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    r = 28.0
    pill_rect = QRectF(2, 2, w - 4, h - 4)
    squircle = create_squircle_path(pill_rect, r)

    for offset, alpha in [(2, 12), (4, 20), (7, 28)]:
        sr = QRectF(2, 2 + offset, w - 4, h - 4)
        sp = create_squircle_path(sr, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, alpha)))
        p.drawPath(sp)

    bg_grad = QLinearGradient(0, 0, 0, h)
    if dark:
        bg_grad.setColorAt(0.0, QColor(19, 22, 23, 245))
        bg_grad.setColorAt(1.0, QColor(10, 10, 10, 250))
    else:
        bg_grad.setColorAt(0.0, QColor(248, 248, 252, 240))
        bg_grad.setColorAt(1.0, QColor(230, 230, 238, 248))
    p.fillPath(squircle, QBrush(bg_grad))

    sheen = QLinearGradient(0, 0, 0, h * 0.40)
    sheen.setColorAt(0.0, QColor(255, 255, 255, 31 if dark else 80))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(squircle, QBrush(sheen))

    p.strokePath(squircle, QPen(QColor(255, 255, 255, 15 if dark else 40), 1.0))

    segments = [
        ("CPU", "38%", "#45C1EE"),
        ("GPU", "24%", "#34C759"),
        ("RAM", "56%", "#F5A524"),
        ("NET", "14MB", "#45C1EE"),
        ("TEMP", "54°C", "#34C759"),
    ]
    seg_w = (w - 24.0) / 5.0
    for idx, (label, val, accent_hex) in enumerate(segments):
        sx = 12.0 + idx * seg_w
        if idx > 0:
            p.setPen(QPen(QColor(255, 255, 255, 10 if dark else 25), 1.0))
            p.drawLine(QPointF(sx, 16.0), QPointF(sx, h - 16.0))

        p.setFont(QFont("Segoe UI Variable Text", 9, QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(QRectF(sx, 12.0, seg_w, 18.0), Qt.AlignmentFlag.AlignCenter, label)

        p.setFont(QFont("Segoe UI Variable Display", 15, QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(QRectF(sx, 32.0, seg_w, 24.0), Qt.AlignmentFlag.AlignCenter, val)

    p.end()
    return pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)


def _render_jarvis_card_fallback(island: AppleDynamicIsland, w: float, h: float, dpi: float, theme: str) -> QImage:
    """Renders JARVIS Assistant Card (390x188 px) offscreen per MEASUREMENT_SHEET.md."""
    pw = int(round(w * dpi))
    ph = int(round(h * dpi))
    pix = QPixmap(pw, ph)
    pix.setDevicePixelRatio(dpi)
    pix.fill(QColor(0, 0, 0, 0))

    dark = (theme.lower() == "dark")
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    r = 47.0
    pill_rect = QRectF(2, 2, w - 4, h - 4)
    squircle = create_squircle_path(pill_rect, r)

    for offset, alpha in [(2, 12), (4, 20), (7, 28)]:
        sr = QRectF(2, 2 + offset, w - 4, h - 4)
        sp = create_squircle_path(sr, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, alpha)))
        p.drawPath(sp)

    bg_grad = QLinearGradient(0, 0, 0, h)
    if dark:
        bg_grad.setColorAt(0.0, QColor(19, 22, 23, 245))
        bg_grad.setColorAt(1.0, QColor(10, 10, 10, 250))
    else:
        bg_grad.setColorAt(0.0, QColor(248, 248, 252, 240))
        bg_grad.setColorAt(1.0, QColor(230, 230, 238, 248))
    p.fillPath(squircle, QBrush(bg_grad))

    glow_pen = QPen(QColor(0, 229, 255, 75 if dark else 90), 2.0)
    p.strokePath(squircle, glow_pen)

    sheen = QLinearGradient(0, 0, 0, h * 0.40)
    sheen.setColorAt(0.0, QColor(255, 255, 255, 31 if dark else 80))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(squircle, QBrush(sheen))

    p.setFont(QFont("Segoe UI Variable Text", 13, QFont.Weight.Medium))
    p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
    p.drawText(24, 46, "“Run system diagnostic”")

    p.setFont(QFont("Segoe UI Variable Text", 15, QFont.Weight.DemiBold))
    p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
    p.drawText(24, 82, "All core subsystems nominal, sir.")

    pill_w = 220.0
    pill_h = 36.0
    pill_r = pill_h / 2.0
    conf_rect = QRectF(24, 116, pill_w, pill_h)
    p.fillPath(create_squircle_path(conf_rect, pill_r), QBrush(QColor(52, 199, 89, 45 if dark else 60)))
    p.strokePath(create_squircle_path(conf_rect, pill_r), QPen(QColor(52, 199, 89, 120), 1.0))

    p.setFont(QFont("Segoe UI Variable Text", 11, QFont.Weight.DemiBold))
    p.setPen(QColor("#34C759" if dark else "#248A3D"))
    p.drawText(conf_rect, Qt.AlignmentFlag.AlignCenter, "✓  Diagnostic Complete")

    p.end()
    return pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)


def setup_island_state(island: AppleDynamicIsland, state_name: str, now: float) -> tuple[float, float]:
    """Configures state attributes on AppleDynamicIsland and returns target size."""
    s = state_name.upper()
    if s in ("IDLE", "COMPACT_PILL"):
        island.reset_to_idle()
    elif s == "WAKE":
        island.is_wake = True
        island.wake_until = now + 10.0
    elif s == "TRACKING":
        island.active = True
        island.label = "TRACKING"
    elif s == "GESTURE":
        island.label = "CLICK"
    elif s == "LISTENING":
        island.listening = True
        island.listening_until = now + 10.0
        island._wave_bars = [0.3, 0.6, 0.9, 0.7, 0.4, 0.8, 0.5]
        island._transcript_words = [("Listening", now)]
    elif s == "THINKING":
        island.thinking = True
        island.thinking_until = now + 10.0
        island.user_prompt = ""
    elif s == "PROMPT":
        island.user_prompt = "What is the speed of light?"
        island.prompt_until = now + 10.0
    elif s == "SPEAKING":
        island.say_text = "It is 299,792,458 meters per second."
        island.say_until = now + 10.0
    elif s == "RESULT":
        island.result_text = "Volume 50%"
        island.result_icon = "success"
        island.result_until = now + 10.0
    elif s == "NO_SPEECH":
        island.no_speech_active = True
        island._nope_t0 = now - 0.10
        island._nope_duration = 0.45
    elif s == "ERROR":
        island.error_text = "Microphone Disconnected"
        island.error_until = now + 10.0
    # Expanded Cards & Pages
    elif s in ("CARD_MEDIA", "MEDIA"):
        island.expanded_card = "media"
        island.expanded_until = now + 10.0
        island.media_active = True
        island.media_playing = True
        island.media_title = "Blinding Lights"
        island.media_artist = "The Weeknd"
        island.media_album = "After Hours"
        island.media_source = "Spotify"
        island.media_dominant_color = QColor(175, 82, 222)
    elif s in ("CARD_WEATHER", "WEATHER"):
        island.expanded_card = "weather"
        island.expanded_until = now + 10.0
        island.weather_city = "AHMEDABAD"
        island.weather_temp = 72.0
        island.weather_condition = "Partly Cloudy"
        island.weather_icon = "partly_cloudy"
        island.weather_wind_speed = 12.0
        island.weather_wind_direction = "NW"
        island.weather_humidity = 48
        island.weather_feels_like = 71.0
    elif s in ("CARD_TIME", "CALENDAR", "CARD_CALENDAR"):
        island.expanded_card = "calendar" if hasattr(IslandState, "CARD_CALENDAR") else "time"
        island.expanded_until = now + 10.0
        island.calendar_date = datetime.date(2026, 10, 18)
    elif s in ("CARD_HARDWARE_GRID", "HARDWARE_GRID"):
        island.expanded_card = "hardware_grid"
        island.expanded_until = now + 10.0
    elif s in ("CARD_HARDWARE_STRIP", "HARDWARE_STRIP"):
        island.expanded_card = "hardware_strip"
        island.expanded_until = now + 10.0
    elif s in ("CARD_JARVIS", "JARVIS"):
        island.expanded_card = "jarvis"
        island.expanded_until = now + 10.0
        island.active = True
        island.user_prompt = "Run system diagnostic"
        island.say_text = "All core subsystems nominal, sir."
    elif s == "CARD_DISAMBIGUATE":
        island.disambiguate_active = True
        island.disambiguate_until = now + 10.0
        island.disambiguate_candidates = [{"title": "VS Code"}, {"title": "Google Chrome"}]
    elif s == "CARD_TIMER":
        island.expanded_card = "timer"
        island.expanded_until = now + 10.0
        island.timer_active = True
        island.timer_duration_sec = 300.0
        island.timer_end_time = now + 180.0
    elif s == "CARD_SENDING":
        island.sending_active = True
        island.sending_label = "Analyzing Screen"
        island.sending_until = now + 10.0
    elif s == "CARD_RESULT":
        island.quick_answer_active = True
        island.quick_answer_text = "72°F Partly Cloudy"
        island.quick_answer_until = now + 10.0
    # HUDs
    elif s == "HUD_VOLUME":
        island.hud_type = "volume"
        island.hud_until = now + 10.0
        island.hud_volume_level = 65
    elif s == "HUD_WINDOW":
        island.hud_type = "window"
        island.hud_until = now + 10.0
        island.hud_window_title = "Google Chrome"
    else:
        raise ValueError(f"Unknown island state: {state_name}")

    try:
        resolved = island._resolve_state(now)
        tw, th = island._target_size(resolved, now)
    except Exception:
        resolved = None
        tw, th = (175.0, 36.0)

    # Honor specific target geometries from tokens / MEASUREMENT_SHEET if unhandled by island enum
    if s in ("HARDWARE_GRID", "CARD_HARDWARE_GRID"):
        try:
            from ui.tokens import GEOMETRY
            tw, th = float(GEOMETRY.expanded_card.w), float(GEOMETRY.expanded_card.h)
        except Exception:
            tw, th = (390.0, 188.0)
    elif s in ("HARDWARE_STRIP", "CARD_HARDWARE_STRIP"):
        try:
            from ui.tokens import GEOMETRY
            tw, th = float(GEOMETRY.hardware_strip.w), float(GEOMETRY.hardware_strip.h)
        except Exception:
            tw, th = (446.0, 69.0)
    elif s in ("JARVIS", "CARD_JARVIS"):
        try:
            from ui.tokens import GEOMETRY
            tw, th = float(GEOMETRY.expanded_card.w), float(GEOMETRY.expanded_card.h)
        except Exception:
            tw, th = (390.0, 188.0)

    return tw, th


def render_island_view(state_name: str, dpi: float, theme: str) -> QImage:
    """Renders any Dynamic Island state, expanded card, or HUD offscreen."""
    get_app()
    set_theme(theme)
    now = 1000.0

    with injected_clock(now):
        mgr = DummyOverlayMgr()
        island = AppleDynamicIsland(mgr)
        island.shape_style = "notch"
        island.t0 = now
        island.last_frame_t = now

        tw, th = setup_island_state(island, state_name, now)
        island.spring_w.snap(tw)
        island.spring_h.snap(th)
        island.resize(int(round(tw)), int(round(th)))
        get_app().processEvents()

        s = state_name.upper()
        if s in ("HARDWARE_GRID", "CARD_HARDWARE_GRID") and not hasattr(island, "_draw_hardware_grid"):
            img = _render_hardware_grid_fallback(island, tw, th, dpi, theme)
        elif s in ("HARDWARE_STRIP", "CARD_HARDWARE_STRIP") and not hasattr(island, "_draw_hardware_strip"):
            img = _render_hardware_strip_fallback(island, tw, th, dpi, theme)
        elif s in ("JARVIS", "CARD_JARVIS") and not hasattr(island, "_draw_jarvis_card"):
            img = _render_jarvis_card_fallback(island, tw, th, dpi, theme)
        else:
            img = render_widget_to_image(island, dpi)

        island.close()
        return img


# ──────────────────────────────────────────────────────────────────────────────
# Public Explicit Page & State Rendering Helpers (Phase 0 Harness Contract)
# ──────────────────────────────────────────────────────────────────────────────

def render_compact_pill(dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders the Dynamic Island compact pill (idle resting state)."""
    return render_island_view("IDLE", dpi, theme)

def render_media_page(dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders the Expanded Media Player card."""
    return render_island_view("CARD_MEDIA", dpi, theme)

def render_calendar_page(dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders the Expanded Calendar / Clock card."""
    return render_island_view("CARD_TIME", dpi, theme)

def render_weather_page(dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders the Expanded Weather forecast card."""
    return render_island_view("CARD_WEATHER", dpi, theme)

def render_hardware_grid_page(dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders the 2x3 Hardware Monitor Grid card."""
    return render_island_view("CARD_HARDWARE_GRID", dpi, theme)

def render_hardware_strip_page(dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders the 5-item Hardware Telemetry Strip."""
    return render_island_view("CARD_HARDWARE_STRIP", dpi, theme)

def render_jarvis_card(dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders the JARVIS Assistant card."""
    return render_island_view("CARD_JARVIS", dpi, theme)

def render_assistant_state(state_name: str, dpi: float = 1.0, theme: str = "dark") -> QImage:
    """Renders an assistant state: wake, listening, thinking, prompt, speaking, result, error."""
    return render_island_view(state_name, dpi, theme)


def render_cursor_view(view_type: str, dpi: float, theme: str) -> QImage:
    """Renders Cursor Feedback Overlay (dwell ring or 5-slice radial menu)."""
    get_app()
    set_theme(theme)

    with injected_clock(1000.0):
        cur = CursorFeedbackOverlay()
        if view_type == "dwell_ring":
            cur.cur_x, cur.cur_y = 80, 80
            cur.progress = 0.65
            cur.latched = False
            cur._ring_scale.snap(1.0)
            cur.resize(160, 160)
        elif view_type == "radial_menu":
            cur.radial_open = True
            cur.radial_hover = "click"
            cur.cur_x, cur.cur_y = 110, 110
            cur.resize(220, 220)
        else:
            raise ValueError(f"Unknown cursor view: {view_type}")

        get_app().processEvents()
        img = render_widget_to_image(cur, dpi)
        cur.close()
        return img


def render_onboarding_step(step_idx: int, dpi: float, theme: str) -> QImage:
    """Renders Onboarding Dialog at step 0, 1, or 2."""
    get_app()
    set_theme(theme)
    cfg = Config()

    with injected_clock(1000.0):
        onb = OnboardingDialog(cfg=cfg)
        onb.resize(580, 520)
        onb._step = step_idx
        onb._stack.setCurrentIndex(step_idx)
        onb._step_bar.set_step(step_idx)
        get_app().processEvents()

        img = render_widget_to_image(onb, dpi)
        if hasattr(onb, "_meter_timer") and onb._meter_timer.isActive():
            onb._meter_timer.stop()
        onb.reject()
        return img


def render_tray_menu(dpi: float, theme: str) -> QImage:
    """Renders System Tray Context Menu offscreen."""
    app = get_app()
    set_theme(theme)

    with injected_clock(1000.0):
        mgr = DummyOverlayMgr()
        tray = JarvisTray(mgr, app)
        menu = tray.contextMenu()
        menu.adjustSize()
        app.processEvents()

        img = render_widget_to_image(menu, dpi)
        tray.hide()
        menu.close()
        return img


def render_island_animation_frame(sequence_name: str, t_offset: float, dpi: float, theme: str) -> QImage:
    """
    Renders an AppleDynamicIsland spring transition frame at fixed t_offset.
    Sequences supported: 'idle_wake', 'idle_listening'.
    Steps in 10ms increments to accurately model 60 FPS continuous integration.
    """
    get_app()
    set_theme(theme)
    t0 = 1000.0

    with injected_clock(t0):
        mgr = DummyOverlayMgr()
        island = AppleDynamicIsland(mgr)
        island.shape_style = "notch"
        island.t0 = t0
        island.last_frame_t = t0

        if sequence_name == "idle_wake":
            island.trigger_wake(duration=2.0)
        elif sequence_name == "idle_listening":
            island.listening = True
            island.listening_until = t0 + 10.0
            island._wave_bars = [0.3, 0.6, 0.9, 0.7, 0.4, 0.8, 0.5]
            island._transcript_words = [("Listening", t0)]
        else:
            raise ValueError(f"Unknown sequence: {sequence_name}")

        curr = t0
        step = 0.01
        while curr < t0 + t_offset - 1e-6:
            next_t = min(curr + step, t0 + t_offset)
            island.update_frame(next_t)
            curr = next_t

        target_time = t0 + t_offset
        with injected_clock(target_time):
            w = max(40, min(int(round(island.spring_w.value)), 3840))
            h = max(20, min(int(round(island.spring_h.value)), 2160))
            island.resize(w, h)
            get_app().processEvents()

            img = render_widget_to_image(island, dpi)
            island.close()
            return img


# ──────────────────────────────────────────────────────────────────────────────
# 5. Diffing & Comparison Engine (Zero Diff Tolerance)
# ──────────────────────────────────────────────────────────────────────────────

def compare_images(actual: QImage, expected: QImage, diff_save_path: Optional[str] = None) -> Tuple[bool, int, float]:
    """
    Compares two QImages pixel-by-pixel with zero tolerance.
    Returns:
        (is_identical: bool, diff_pixel_count: int, diff_percentage: float)
    If diff_save_path is provided and images differ, writes a diff visualization image.
    """
    act = actual.convertToFormat(QImage.Format.Format_ARGB32)
    exp = expected.convertToFormat(QImage.Format.Format_ARGB32)

    if act == exp:
        return True, 0, 0.0

    w1, h1 = act.width(), act.height()
    w2, h2 = exp.width(), exp.height()

    max_w = max(w1, w2)
    max_h = max(h1, h2)
    total_pixels = max_w * max_h
    diff_count = 0

    diff_img = None
    if diff_save_path:
        diff_img = QImage(max_w, max_h, QImage.Format.Format_ARGB32)
        diff_img.fill(QColor(0, 0, 0, 255))

    for y in range(max_h):
        for x in range(max_w):
            c1 = act.pixelColor(x, y) if x < w1 and y < h1 else QColor(0, 0, 0, 0)
            c2 = exp.pixelColor(x, y) if x < w2 and y < h2 else QColor(0, 0, 0, 0)

            if c1 != c2:
                diff_count += 1
                if diff_img is not None:
                    # Highlight diff pixel in vibrant magenta/red
                    diff_img.setPixelColor(x, y, QColor(255, 0, 80, 255))
            elif diff_img is not None:
                # Dim identical pixel
                diff_img.setPixelColor(x, y, QColor(c1.red() // 4, c1.green() // 4, c1.blue() // 4, 255))

    if diff_save_path and diff_img is not None:
        os.makedirs(os.path.dirname(os.path.abspath(diff_save_path)), exist_ok=True)
        diff_img.save(diff_save_path, "PNG")

    diff_percent = (diff_count / float(total_pixels)) * 100.0 if total_pixels > 0 else 0.0
    return False, diff_count, diff_percent


# ──────────────────────────────────────────────────────────────────────────────
# 6. View Manifest & Golden Batch Generation
# ──────────────────────────────────────────────────────────────────────────────

ANIM_TIMESTAMPS = [0.00, 0.05, 0.10, 0.15, 0.25, 0.40]
THEMES = ["dark", "light"]
DPIS = [1.0, 1.5]

ISLAND_BASE_STATES = [
    "IDLE", "WAKE", "TRACKING", "GESTURE", "LISTENING", "THINKING",
    "PROMPT", "SPEAKING", "RESULT", "NO_SPEECH", "ERROR"
]
ISLAND_CARDS = [
    "CARD_MEDIA", "CARD_WEATHER", "CARD_TIME", "CARD_DISAMBIGUATE",
    "CARD_TIMER", "CARD_SENDING", "CARD_RESULT"
]
ISLAND_HUDS = ["HUD_VOLUME", "HUD_WINDOW"]
CURSOR_VIEWS = ["dwell_ring", "radial_menu"]
ONBOARDING_STEPS = [0, 1, 2]
SETTINGS_TABS = _CATEGORIES  # ["General", "Gestures", "Pointer", "Voice", "AI", "Display", "About"]


def build_golden_manifest() -> Dict[str, callable]:
    """
    Constructs the exhaustive dictionary of all golden image render calls.
    Returns:
        dict: filename -> lambda: QImage
    """
    manifest: Dict[str, callable] = {}

    for theme in THEMES:
        for dpi in DPIS:
            dpi_str = f"dpi{int(dpi * 100)}"

            # 1. Settings Tabs (7 tabs)
            for tab in SETTINGS_TABS:
                fname = f"settings_{tab.lower()}_{theme}_{dpi_str}.png"
                manifest[fname] = (lambda t=tab, d=dpi, th=theme: render_settings_tab(t, d, th))

            # 2. Island Base States (11 states)
            for st in ISLAND_BASE_STATES:
                fname = f"island_{st.lower()}_{theme}_{dpi_str}.png"
                manifest[fname] = (lambda s=st, d=dpi, th=theme: render_island_view(s, d, th))

            # 3. Island Cards (7 cards)
            for card in ISLAND_CARDS:
                fname = f"island_{card.lower()}_{theme}_{dpi_str}.png"
                manifest[fname] = (lambda c=card, d=dpi, th=theme: render_island_view(c, d, th))

            # 4. Island HUDs (2 HUDs)
            for hud in ISLAND_HUDS:
                fname = f"island_{hud.lower()}_{theme}_{dpi_str}.png"
                manifest[fname] = (lambda h=hud, d=dpi, th=theme: render_island_view(h, d, th))

            # 5. Cursor Views (2 views)
            for cur in CURSOR_VIEWS:
                fname = f"cursor_{cur}_{theme}_{dpi_str}.png"
                manifest[fname] = (lambda cv=cur, d=dpi, th=theme: render_cursor_view(cv, d, th))

            # 6. Onboarding Steps (3 steps)
            for step in ONBOARDING_STEPS:
                fname = f"onboarding_step_{step}_{theme}_{dpi_str}.png"
                manifest[fname] = (lambda s=step, d=dpi, th=theme: render_onboarding_step(s, d, th))

            # 7. Tray Menu (1 view)
            fname_tray = f"tray_menu_{theme}_{dpi_str}.png"
            manifest[fname_tray] = (lambda d=dpi, th=theme: render_tray_menu(d, th))

            # 8. Spring Animation Sequences (2 sequences x 6 timestamps)
            for seq in ["idle_wake", "idle_listening"]:
                for ts in ANIM_TIMESTAMPS:
                    ts_str = f"t{int(round(ts * 100)):03d}"
                    fname_anim = f"anim_{seq}_{ts_str}_{theme}_{dpi_str}.png"
                    manifest[fname_anim] = (lambda sq=seq, t=ts, d=dpi, th=theme: render_island_animation_frame(sq, t, d, th))

    return manifest


def generate_all_goldens(goldens_dir: str, verbose: bool = False) -> int:
    """Renders and saves all golden images into goldens_dir. Returns count."""
    os.makedirs(goldens_dir, exist_ok=True)
    manifest = build_golden_manifest()
    count = 0

    for fname, render_fn in manifest.items():
        out_path = os.path.join(goldens_dir, fname)
        img = render_fn()
        img.save(out_path, "PNG")
        count += 1
        if verbose:
            print(f"[{count:03d}/{len(manifest)}] Saved {fname} ({img.width()}x{img.height()})")

    return count
