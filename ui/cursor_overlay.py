"""
ui/cursor_overlay.py — Cursor Feedback Overlay (refactored)

Preserves all public API from original CursorFeedbackOverlay in overlay.py:
  - update_state(data)  → called from state events
  - update_dwell(data)  → called from cursor_dwell events

Improvements over original:
  - Dwell ring: 2 px rounded-cap arc, scale-in on progress start, completion pulse
  - Radial menu: glass disc with Lucide-style text icons (SVG-ready), spring hover
  - Wake/sleep progress ring: gold (waking) / blue (sleeping)
  - All colors from tokens.py; neon cyan/gold in HUD_ONLY
"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPainterPath, QRadialGradient
)
from PySide6.QtWidgets import QWidget, QApplication

from ui.tokens import T, COLOR
from ui.components import Spring, create_squircle_path


class CursorFeedbackOverlay(QWidget):
    """
    Transparent click-through overlay rendering:
      1. Standby hand-pointer ghost dot + wake ring (gold arc)
      2. Active sleep-hold ring (blue arc)
      3. Radial dwell menu (glass disc + 5 sector labels)
      4. Dwell progress ring (2 px arc, scale-in, completion pulse)
    """

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool                 |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("JARVIS_CURSOR_FEEDBACK")

        # Dwell ring state
        self.cur_x       = -999.0
        self.cur_y       = -999.0
        self.progress    = 0.0
        self.latched     = False
        self.flashing    = False
        self.flash_start = 0.0

        # Radial menu
        self.radial_open  = False
        self.radial_hover: Optional[str] = None
        self.radial_prog  = 0.0
        self._hover_spring = Spring(0.0, 300.0, 26.0)   # sector highlight spring
        self._hover_idx    = -1

        # Hand tracking
        self.active              = False
        self.gesture_prog        = 0.0
        self.landmarks           = []
        self.hand_visible_standby= False
        self.is_holding          = False

        # Ring scale-in spring (starts tiny, springs to full size on dwell start)
        self._ring_scale = Spring(0.0, 180.0, 16.0)

        self.dark_mode = T.is_dark()
        self.resize(160, 160)
        self._apply_click_through()

    def reload_config(self) -> None:
        """Live updates theme and appearance settings without restart."""
        self.dark_mode = T.is_dark()
        self.update()

    def _apply_click_through(self) -> None:
        if sys.platform != "win32":
            return
        try:
            from ui.materials import make_click_through
            make_click_through(int(self.winId()))
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # Public API (unchanged from original overlay.py)
    # ──────────────────────────────────────────────────────────────────────

    def update_state(self, data: dict) -> None:
        self.active    = data.get("active", False)
        new_prog       = data.get("progress", 0.0)
        self.landmarks = data.get("landmarks", [])

        now = time.time()
        was_holding = self.is_holding
        self.is_holding = (new_prog > 0.01)

        if was_holding and new_prog >= 0.95 and not self.flashing:
            self.flash_start = now
            self.flashing    = True

        self.gesture_prog = new_prog

        if not self.active:
            if self.landmarks and len(self.landmarks) > 0 and len(self.landmarks[0]) > 9:
                lm = self.landmarks[0]
                if len(lm) > 17:
                    hx = (lm[5][0] + lm[9][0] + lm[13][0] + lm[17][0]) / 4.0
                    hy = (lm[5][1] + lm[9][1] + lm[13][1] + lm[17][1]) / 4.0
                else:
                    hx, hy = lm[9][0], lm[9][1]
                screen = QApplication.primaryScreen()
                geo    = screen.geometry() if screen else QRectF(0, 0, 1920, 1080)
                tx = geo.x() + float(hx * geo.width())
                ty = geo.y() + float(hy * geo.height())
                if self.cur_x < -100 or not self.hand_visible_standby:
                    self.cur_x, self.cur_y = tx, ty
                else:
                    self.cur_x += 0.40 * (tx - self.cur_x)
                    self.cur_y += 0.40 * (ty - self.cur_y)
                self.hand_visible_standby = True
            else:
                self.hand_visible_standby = False
        else:
            self.hand_visible_standby = False
            if self.gesture_prog > 0.01:
                from PySide6.QtGui import QCursor
                pos = QCursor.pos()
                self.cur_x = float(pos.x())
                self.cur_y = float(pos.y())

        should_render = (
            self.radial_open or
            (self.progress > 0.01 and not self.latched) or
            (self.flashing or (now - self.flash_start < 0.35)) or
            (not self.active and self.hand_visible_standby) or
            (self.active and self.gesture_prog > 0.01)
        )

        if should_render and self.cur_x > -100:
            sz = 220 if self.radial_open else 160
            if self.width() != sz or self.height() != sz:
                self.resize(sz, sz)
            nx = int(self.cur_x - sz / 2.0)
            ny = int(self.cur_y - sz / 2.0)
            if self.x() != nx or self.y() != ny:
                self.move(nx, ny)
            if not self.isVisible():
                self.show()
            self.update()
        elif not should_render and not self.radial_open:
            if self.isVisible() and not (now - self.flash_start < 0.35):
                self.hide()

    def update_dwell(self, data: dict) -> None:
        self.cur_x  = data.get("x", self.cur_x)
        self.cur_y  = data.get("y", self.cur_y)
        new_prog    = data.get("progress", 0.0)
        was_flash   = self.flashing
        self.flashing = data.get("flashing", False)

        if not was_flash and self.flashing:
            self.flash_start = time.time()

        # Scale-in spring: start small when dwell begins
        if new_prog > 0.01 and self.progress <= 0.01:
            self._ring_scale.snap(0.3)
            self._ring_scale.set_target(1.0)

        self.progress     = new_prog
        self.latched      = data.get("latched", False)
        self.radial_open  = data.get("radial_open", False)
        self.radial_hover = data.get("radial_hover")
        self.radial_prog  = data.get("radial_progress", 0.0)

        # Update hover spring
        sectors = ["click","double_click","right_click","drag","scroll"]
        new_hover_idx = sectors.index(self.radial_hover) if self.radial_hover in sectors else -1
        if new_hover_idx != self._hover_idx:
            self._hover_idx = new_hover_idx
        self._hover_spring.set_target(float(new_hover_idx))
        self._hover_spring.update(0.016)

        # Animate ring scale spring
        self._ring_scale.update(0.016, T.reduce_motion())

        if not self.radial_open:
            from ui import glass
            glass.release_radial_backdrop(self)       # drop the disc-shaped mask / blur

        sz = 220 if self.radial_open else 160
        if self.width() != sz or self.height() != sz:
            self.resize(sz, sz)
        nx = int(self.cur_x - sz / 2.0)
        ny = int(self.cur_y - sz / 2.0)
        if self.x() != nx or self.y() != ny:
            self.move(nx, ny)
        if not self.isVisible():
            self.show()
        self.update()

    # ──────────────────────────────────────────────────────────────────────
    # Paint
    # ──────────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        cx    = float(self.width()) / 2.0
        cy    = float(self.height()) / 2.0
        dark  = self.dark_mode
        now   = time.time()

        GOLD  = COLOR.HUD_ONLY.stark_gold
        CYAN  = COLOR.HUD_ONLY.neon_cyan
        accent = T.color.accent.resolve(dark)

        # ── 1. Completion ripple pulse ────────────────────────────────────
        if self.flashing or (now - self.flash_start < 0.35):
            elapsed = now - self.flash_start
            frac    = min(elapsed / 0.35, 1.0)
            rr      = 16.0 + 30.0 * frac
            ra      = int(240 * (1.0 - frac))
            rc      = GOLD if not self.active else accent
            p.setPen(QPen(QColor(rc.red(), rc.green(), rc.blue(), ra), 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr, rr)

        # ── 2. Standby hand pointer + wake ring ───────────────────────────
        if not self.active and self.hand_visible_standby:
            is_waking  = self.gesture_prog > 0.02
            dot_col    = GOLD if is_waking else QColor(255, 255, 255)
            halo_col   = GOLD if is_waking else CYAN

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(halo_col.red(), halo_col.green(), halo_col.blue(), 55)))
            p.drawEllipse(QPointF(cx, cy), 13.0, 13.0)
            p.setBrush(QBrush(dot_col))
            p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)

            if self.gesture_prog > 0.01:
                r_prog = 24.0
                p.setPen(QPen(QColor(255, 255, 255, 50), 2.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(cx, cy), r_prog, r_prog)
                p.setPen(QPen(GOLD, 3.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                box = QRectF(cx - r_prog, cy - r_prog, r_prog * 2, r_prog * 2)
                p.drawArc(box, int(90 * 16), -int(self.gesture_prog * 360 * 16))
            return

        # ── 3. Active sleep-hold ring ─────────────────────────────────────
        if self.active and self.gesture_prog > 0.01:
            r_prog = 24.0
            p.setPen(QPen(QColor(255, 255, 255, 50), 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r_prog, r_prog)
            sleep_col = QColor(96, 165, 250)
            p.setPen(QPen(sleep_col, 3.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            box = QRectF(cx - r_prog, cy - r_prog, r_prog * 2, r_prog * 2)
            p.drawArc(box, int(90 * 16), -int(self.gesture_prog * 360 * 16))
            return

        # ── 4. Radial dwell menu ──────────────────────────────────────────
        if self.radial_open:
            self._draw_radial_menu(p, cx, cy, dark, accent)
            return

        # ── 5. Dwell progress ring (scale-in + arc) ───────────────────────
        if self.progress > 0.01 and not self.latched:
            scale    = min(1.0, max(0.0, self._ring_scale.value))
            r_dwell  = 14.0 * scale

            # Track
            p.setPen(QPen(QColor(255, 255, 255, 45), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r_dwell, r_dwell)

            # Progress arc (2 px, rounded cap)
            prog_pen = QPen(accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(prog_pen)
            box = QRectF(cx - r_dwell, cy - r_dwell, r_dwell * 2, r_dwell * 2)
            p.drawArc(box, int(90 * 16), -int(self.progress * 360 * 16))

    def _draw_radial_menu(self, p: QPainter, cx: float, cy: float,
                          dark: bool, accent: QColor) -> None:
        """Liquid-glass radial menu (painter lives in ui.glass; spring-morphed selection capsule)."""
        from ui import glass
        glass.paint_radial_menu(self, p, cx, cy, dark, accent)
