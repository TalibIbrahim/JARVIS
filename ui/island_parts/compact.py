"""ui/island_parts/compact.py - Compact pill renderer for AppleDynamicIsland (verbatim move from island.py)."""

from __future__ import annotations

import time
from ui.tokens import COLOR, TYPE, qfont
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.island_parts.common import create_cached_squircle_path


class CompactPillMixin:
    """Compact pill renderer for AppleDynamicIsland (verbatim move from island.py).
    Methods only; all state lives on the AppleDynamicIsland instance."""

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
        # Worker health (once known) takes over the existing trailing dot.
        _wh = self._worker_health_color(time.monotonic())
        if _wh is not None:
            dot_col = _wh
            dot_r = 3.5
            if self._wh_from_col is not None and time.monotonic() - self._wh_t0 < 0.16:
                self.update()

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
