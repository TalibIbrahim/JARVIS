"""ui/island_parts/jarvis.py - JARVIS assistant card in the weather / calendar family.

Weather-style layout: assistant state glyph + word, camera FPS, last command + result, worker health when down,
and the big PAUSE / RESUME TRACKING control (>= 46 px tall, hit-test id "jarvis_pause" / button id "jarvis_toggle_tracking").

The static layer (state panel / glyph / tiles / texts) is cached per content key, size and dpr; only the pause
control's hover / flash / dwell fill is painted per frame (its icon and text are a small cached pixmap).
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from ui.tokens import ISLAND, TYPE
from ui.island_parts.common import (IslandState, cached_page, create_cached_squircle_path, draw_text, fill_round,
                                    paint_icon, with_alpha)

_L = ISLAND.jarvis
_C = ISLAND.color

# state key -> (word, sub line)
_STATES = {
    "active":    ("Active", "Tracking active"),
    "paused":    ("Paused", "Tracking paused"),
    "listening": ("Listening", "Voice input"),
    "thinking":  ("Thinking", "Working on it"),
    "speaking":  ("Speaking", "Voice reply"),
    "standby":   ("Standby", "Tracking off"),
}


def jarvis_geometry() -> dict:
    """1x card-origin rects: {"tiles": [QRectF, QRectF], "pause": QRectF, "pause_r": float}."""
    total = 2 * _L.tile_h + _L.pause_h + 2 * _L.col_gap
    y0 = (_L.h - total) / 2.0
    t1 = QRectF(_L.col_x, y0, _L.col_w, _L.tile_h)
    t2 = QRectF(_L.col_x, y0 + _L.tile_h + _L.col_gap, _L.col_w, _L.tile_h)
    pz = QRectF(_L.col_x, y0 + 2 * (_L.tile_h + _L.col_gap), _L.col_w, _L.pause_h)
    return {"tiles": [t1, t2], "pause": pz, "pause_r": _L.tile_radius}


def paint_state_glyph(p: QPainter, cx: float, cy: float, d: float, kind: str) -> None:
    """Ring / arc mark: accent open ring + centre dot when active, grey ring + pause bars when paused."""
    thick = max(2.6, d * 0.085)
    r = d / 2.0 - thick / 2.0
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(_C.track, thick))
    p.drawEllipse(rect)
    grey = with_alpha(_C.secondary, 0.85)
    if kind == "paused":
        p.setPen(QPen(grey, thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawEllipse(rect)
        bw, bh, gap = d * 0.085, d * 0.30, d * 0.10
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grey)
        p.drawRoundedRect(QRectF(cx - gap / 2.0 - bw, cy - bh / 2.0, bw, bh), bw / 2.0, bw / 2.0)
        p.drawRoundedRect(QRectF(cx + gap / 2.0, cy - bh / 2.0, bw, bh), bw / 2.0, bw / 2.0)
    else:
        col = grey if kind == "standby" else _C.accent
        p.setPen(QPen(col, thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 90 * 16, -300 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(cx, cy), d * 0.12, d * 0.12)
    p.restore()


class JarvisPageMixin:
    """JARVIS status card renderer. Methods only; all state lives on the AppleDynamicIsland instance."""

    # ── data ────────────────────────────────────────────────────────────────

    def _assistant_status(self) -> dict:
        """ctx.assistant_status() or an idle default when no ctx is wired."""
        ctx = self._ctx()
        if ctx is not None and hasattr(ctx, "assistant_status"):
            try:
                return dict(ctx.assistant_status())
            except Exception:
                pass
        return {"state": "idle", "last_command": "", "last_result": ""}

    def _jarvis_state_key(self, status: dict, paused: bool) -> str:
        if paused:
            return "paused"
        st = str(status.get("state", "idle"))
        if st in ("listening", "thinking", "speaking"):
            return st
        ctx = self._ctx()
        tracking = (ctx is not None and hasattr(ctx, "is_tracking_paused")) or bool(self.active)
        return "active" if tracking else "standby"

    def _jarvis_content(self) -> tuple:
        """Displayed values (also the cache key): (state, fps text, last command, result, health reason)."""
        status = self._assistant_status()
        paused = self._tracking_is_paused()
        key = self._jarvis_state_key(status, paused)
        hw = self.hardware_state or {}
        fps = hw.get("fps") if (time.time() - self.hardware_state_t) <= 6.0 else None
        fps_text = "--" if not fps else f"{round(fps)}"
        cmd = str(status.get("last_command", "") or "").strip().replace("_", " ")
        cmd = (cmd[:1].upper() + cmd[1:]) if cmd else ""
        res = str(status.get("last_result", "") or "") if cmd else ""
        down = ""
        if self.worker_health_state == "down":
            down = "Down: " + (self.worker_health_reason or "worker stopped")
        return (key, fps_text, cmd, res, down)

    # ── cached static layer ─────────────────────────────────────────────────

    @staticmethod
    def _paint_value_tile(p: QPainter, rect: QRectF, icon: str, label: str, value: str, value_col: QColor,
                          trail_icon: Optional[tuple] = None) -> None:
        """Weather-style tile: dim accent icon, grey label, white value (+ optional trailing result icon)."""
        fill_round(p, rect, _L.tile_radius, _C.tile_fill)
        paint_icon(p, icon, rect.x() + 16.0, rect.y() + 14.8, 12.0, with_alpha(_C.accent, 0.45), stroke=1.5)
        tx = rect.x() + _L.tile_text_x
        draw_text(p, label, tx, rect.y() + _L.tile_label_baseline, TYPE.tile_label, _C.label)
        avail = rect.width() - _L.tile_text_x - 10.0 - (16.0 if trail_icon else 0.0)
        draw_text(p, value, tx, rect.y() + _L.tile_value_baseline, TYPE.weather_value, value_col, max_w=avail)
        if trail_icon:
            name, col = trail_icon
            paint_icon(p, name, rect.right() - 17.0, rect.y() + _L.tile_value_baseline - 4.6, 11.0, col, stroke=1.7)

    def _paint_jarvis_content(self, p: QPainter, c: tuple) -> None:
        key, fps_text, cmd, res, down = c
        word, sub = _STATES[key]
        glyph = "paused" if key == "paused" else ("standby" if key == "standby" else "active")
        t1, t2 = jarvis_geometry()["tiles"]
        if res == "fail":
            trail = ("cross", _C.red)
        elif res:
            trail = ("check", with_alpha(_C.label, 1.0))
        else:
            trail = None
        cmd_text, cmd_col = (cmd, _C.value) if cmd else ("None yet", _C.secondary)
        draw_text(p, "Assistant", _L.b_cap_x, _L.b_cap_baseline, TYPE.weather_city, _C.label)
        paint_state_glyph(p, _L.b_ring_cx, _L.b_ring_cy, _L.b_ring_d, glyph)
        draw_text(p, word, _L.b_word_x, _L.b_word_baseline, _L.word_b, _C.value, max_w=_L.div_x - _L.b_word_x - 8.0)
        if down:
            draw_text(p, down, _L.b_sub_x, _L.b_sub_baseline, TYPE.weather_cond, _C.red, max_w=_L.div_x - _L.b_sub_x - 10.0)
        else:
            draw_text(p, sub, _L.b_sub_x, _L.b_sub_baseline, TYPE.weather_cond, _C.value, max_w=_L.div_x - _L.b_sub_x - 10.0)
        p.fillRect(QRectF(_L.div_x - 0.5, 35.0, 1.0, 118.8), _C.divider)
        self._paint_value_tile(p, t1, "command", "Last Command", cmd_text, cmd_col, trail)
        self._paint_value_tile(p, t2, "camera", "Camera FPS", fps_text, _C.value if fps_text != "--" else _C.secondary)

    # ── pause control (per-frame fill, cached icon + text) ──────────────────

    @staticmethod
    def _paint_pause_content(p: QPainter, paused: bool, h: float) -> None:
        paint_icon(p, "play" if paused else "pause", 16.0, h / 2.0, 11.0, _C.accent)
        dy = (h - _L.tile_h) / 2.0
        draw_text(p, "Tracking", _L.tile_text_x, _L.tile_label_baseline + dy, TYPE.tile_label, _C.label)
        draw_text(p, "Resume" if paused else "Pause", _L.tile_text_x, _L.tile_value_baseline + dy,
                  TYPE.weather_value, _C.value)

    def _draw_jarvis_pause(self, p: QPainter, geo: dict, paused: bool, scale: float, now: float) -> None:
        rect: QRectF = geo["pause"]
        r = geo["pause_r"]
        hover = self._hovered_btn in ("jarvis_toggle_tracking", "jarvis_pause")
        flash = hover and now < self._btn_flash_until
        base = QColor(_C.tile_fill)
        if flash:
            base.setAlpha(min(255, base.alpha() + 70))
        elif hover:
            base.setAlpha(min(255, base.alpha() + 36))
        rs = QRectF(rect.x() * scale, rect.y() * scale, rect.width() * scale, rect.height() * scale)
        path = create_cached_squircle_path(rs, r * scale)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawPath(path)
        if hover and self._btn_dwell_elapsed > 0.03:
            prog = min(1.0, self._btn_dwell_elapsed / 1.0)
            p.save()
            p.setClipPath(path)
            p.fillRect(QRectF(rs.x(), rs.y(), rs.width() * prog, rs.height()), with_alpha(_C.accent, 0.30))
            p.restore()
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(with_alpha(_C.accent, 0.35 + 0.65 * prog), 1.4 * scale))
            p.drawPath(path)
        pix = cached_page(self, "jarvis_pause", (paused,), rect.width(), rect.height(),
                          getattr(self, "_paint_dpr", 1.0), scale,
                          lambda cp: self._paint_pause_content(cp, paused, rect.height()))
        p.drawPixmap(QPointF(rs.x(), rs.y()), pix)

    def _draw_jarvis_card(self, p: QPainter, w: float, h: float,
                          now: float, t: float, dark: bool,
                          primary: QColor, secondary: QColor, accent: QColor) -> None:
        """Expanded JARVIS card (390 x 188): cached content layer + live pause control."""
        scale = self._get_size_scale()
        c = self._jarvis_content()
        pix = cached_page(self, "jarvis", c, _L.w, _L.h, getattr(self, "_paint_dpr", 1.0), scale,
                          lambda cp: self._paint_jarvis_content(cp, c))
        p.drawPixmap(QPointF(0.0, 0.0), pix)
        self._draw_jarvis_pause(p, jarvis_geometry(), c[0] == "paused", scale, now)

    # ── hit geometry (island-local px) ──────────────────────────────────────

    def _jarvis_pause_rect(self) -> QRectF:
        """Pause / resume control rect in island-local px (matches what _draw_jarvis_pause draws)."""
        r = jarvis_geometry()["pause"]
        s = self._get_size_scale()
        return QRectF(r.x() * s, r.y() * s, r.width() * s, r.height() * s)

    def _publish_hit_snapshot(self, w: float, h: float, state) -> None:
        """JARVIS card: pause control + page dots + card in the thread-safe snapshot; other states use PagingMixin."""
        if state != IslandState.CARD_JARVIS or not self.expanded_card:
            return super()._publish_hit_snapshot(w, h, state)
        ir = self.island_rect()
        ox, oy = float(ir.x()), float(ir.y())
        targets = []

        def add(x, y, rw, rh, tid):
            targets.append(((ox + x, oy + y, rw, rh), tid))

        pr = self._jarvis_pause_rect()
        add(pr.x(), pr.y(), pr.width(), pr.height(), "jarvis_pause")
        scale = self._get_size_scale()
        dot_x, ys, pitch = self._dots_geometry(w, h)
        half_w = ISLAND.dots.hit_half_w * scale
        x0 = dot_x - half_w
        for page, dy in zip(self.card_pages, ys):
            add(x0, dy - pitch / 2.0, min(2 * half_w, w - x0), pitch, f"dot:{page}")
        add(0.0, 0.0, w, h, "card")
        new = tuple(targets)
        old_gen, old = self._hit_snapshot
        if new != old:
            self._hit_snapshot = (old_gen + 1, new)
