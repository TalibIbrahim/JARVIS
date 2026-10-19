"""ui/island_parts/compact.py - Compact pill renderer for AppleDynamicIsland (verbatim move from island.py)."""

from __future__ import annotations

import ctypes
import sys
import time
from ui.tokens import COLOR, TYPE, ISLAND, qfont
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.island_parts.common import create_cached_squircle_path


# ── clock format ('auto' follows the Windows short-time pattern) ──────────────
_AUTO_24H_CACHE: list = [0.0, False]


def _system_uses_24h() -> bool:
    now = time.monotonic()
    if now - _AUTO_24H_CACHE[0] < 30.0 and _AUTO_24H_CACHE[0] > 0.0:
        return bool(_AUTO_24H_CACHE[1])
    result = False
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(80)
            # LOCALE_SSHORTTIME = 0x79; locale name None = user default
            if ctypes.windll.kernel32.GetLocaleInfoEx(None, 0x79, buf, 80):
                result = "H" in buf.value
        except Exception:
            result = False
    _AUTO_24H_CACHE[0] = now
    _AUTO_24H_CACHE[1] = result
    return result


def clock_is_24h(cfg) -> bool:
    """island_clock_format: '12h' | '24h' | 'auto' (system)."""
    fmt = str(getattr(cfg, "island_clock_format", "auto") or "auto").lower()
    if fmt == "24h":
        return True
    if fmt == "12h":
        return False
    return _system_uses_24h()


def format_clock(now: float, cfg, with_period: bool = True) -> str:
    tm = time.localtime(now)
    if clock_is_24h(cfg):
        return time.strftime("%H:%M", tm)
    txt = time.strftime("%I:%M %p" if with_period else "%I:%M", tm)
    return txt[1:] if txt.startswith("0") else txt


# ── weather glyph (own vector drawings, 16 x 14 box, one accent colour like the reference) ──
def glyph_kind(icon_name: str) -> str:
    n = str(icon_name or "").lower().replace(" ", "_").replace("-", "_")
    if n in ("sunny", "sun", "mostly_sunny"):
        return "sun"
    if n in ("clear", "night", "clear_night", "moon"):
        return "night"
    if n in ("partly_cloudy", "cloud_sun", "partly_sunny", "mostly_cloudy"):
        return "partly"
    if n in ("rain", "rainy", "drizzle", "showers", "shower"):
        return "rain"
    if n in ("snow", "snowy", "flurries", "sleet"):
        return "snow"
    if n in ("storm", "thunderstorm", "thunder", "stormy"):
        return "storm"
    if n in ("fog", "foggy", "mist", "haze"):
        return "fog"
    return "cloud"


def _paint_cloud(p: QPainter, dx: float, dy: float, k: float) -> None:
    p.save()
    p.translate(dx, dy)
    p.scale(k, k)
    p.drawRoundedRect(QRectF(0.8, 6.2, 14.4, 6.6), 3.3, 3.3)
    p.drawEllipse(QPointF(5.3, 6.9), 3.5, 3.5)
    p.drawEllipse(QPointF(10.1, 5.7), 4.3, 4.3)
    p.restore()


def paint_weather_glyph(p: QPainter, kind: str, color: QColor) -> None:
    """Draw glyph `kind` into the 16 x 14 logical box at the painter origin."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    line = QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    if kind == "sun":
        p.drawEllipse(QPointF(8.0, 7.0), 3.3, 3.3)
        p.setPen(line)
        import math
        for i in range(8):
            a = i * math.pi / 4.0
            p.drawLine(QPointF(8.0 + math.cos(a) * 5.0, 7.0 + math.sin(a) * 5.0),
                       QPointF(8.0 + math.cos(a) * 6.5, 7.0 + math.sin(a) * 6.5))
    elif kind == "night":
        moon = QPainterPath()
        moon.addEllipse(QPointF(7.6, 7.0), 5.6, 5.6)
        cut = QPainterPath()
        cut.addEllipse(QPointF(10.6, 5.0), 4.9, 4.9)
        p.drawPath(moon.subtracted(cut))
    elif kind == "partly":
        p.drawEllipse(QPointF(4.1, 4.3), 3.5, 3.5)
        _paint_cloud(p, 3.4, 3.7, 0.78)
    elif kind == "rain":
        _paint_cloud(p, 0.0, -2.4, 0.9)
        p.setPen(line)
        for x in (4.6, 8.0, 11.4):
            p.drawLine(QPointF(x, 10.2), QPointF(x - 1.0, 12.6))
    elif kind == "snow":
        _paint_cloud(p, 0.0, -2.4, 0.9)
        for x in (4.6, 8.0, 11.4):
            p.drawEllipse(QPointF(x, 11.9), 1.0, 1.0)
    elif kind == "storm":
        _paint_cloud(p, 0.0, -2.6, 0.9)
        bolt = QPainterPath()
        bolt.moveTo(9.2, 8.2)
        bolt.lineTo(6.0, 11.2)
        bolt.lineTo(7.9, 11.2)
        bolt.lineTo(6.8, 13.9)
        bolt.lineTo(10.4, 10.3)
        bolt.lineTo(8.5, 10.3)
        bolt.closeSubpath()
        p.drawPath(bolt)
    elif kind == "fog":
        p.setPen(QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(2.5, 3.6), QPointF(13.5, 3.6))
        p.drawLine(QPointF(1.0, 7.0), QPointF(15.0, 7.0))
        p.drawLine(QPointF(3.5, 10.4), QPointF(12.5, 10.4))
    else:
        _paint_cloud(p, 0.0, 0.0, 1.0)


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
            # Lyric line (lyrics on + in-pill + synced) draws the tall pill in logical (1x) coordinates
            ly = self._lyrics_pill_active(now)
            if ly is not None:
                s = self._get_size_scale()
                p.save()
                p.scale(s, s)
                w, h, cy = w / s, h / s, cy / s
                dot_cx = w - 14.0
                self._draw_lyrics_pill(p, ly, w, h, cy, now, ISLAND.color.white)
            else:
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

            # Center text: the track title when no lyric line is showing
            if ly is None:
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
            if ly is not None:
                p.restore()

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
            # Optional status dot: health once known, else only when something is happening (error / active).
            show_dot = (_wh is not None) or (now < self.error_until) or self.is_ai_error                 or self.active or self.listening or self.media_playing
            self._draw_pill_time_weather(p, w, h, cy, now, dot_col if show_dot else None)

        elif compact_mode == "time":
            # ── Time compact layout ──────────────────────────────────────
            cfg = getattr(self.mgr, "cfg", None)
            time_str = format_clock(now, cfg, with_period=True)
            scale = self._get_size_scale()
            p.save()
            p.scale(scale, scale)
            font = qfont(TYPE.pill_time)
            p.setFont(font)
            p.setPen(ISLAND.color.value)
            fm = QFontMetricsF(font)
            lw = w / scale
            p.drawText(QPointF((lw - fm.horizontalAdvance(time_str)) / 2.0 - 6.0, cy / scale + fm.capHeight() / 2.0), time_str)
            p.restore()

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

    # ── time | divider | weather glyph + temperature (+ optional status dot) ──────────

    def _pill_glyph_pixmap(self, kind: str, dpr: float, scale: float) -> QPixmap:
        color = ISLAND.color.accent
        key = ("pill_glyph", kind, round(dpr, 3), round(scale, 3), color.rgba())
        pix = self._icon_cache.get(key)
        if pix is None:
            lw, lh = ISLAND.pill.glyph_w * scale, ISLAND.pill.glyph_h * scale
            pix = QPixmap(max(1, int(round(lw * dpr))), max(1, int(round(lh * dpr))))
            pix.setDevicePixelRatio(dpr)
            pix.fill(Qt.GlobalColor.transparent)
            gp = QPainter(pix)
            gp.scale(scale, scale)
            paint_weather_glyph(gp, kind, color)
            gp.end()
            self._icon_cache[key] = pix
        return pix

    def _draw_pill_time_weather(self, p: QPainter, w: float, h: float, cy: float, now: float,
                                dot_col) -> None:
        L = ISLAND.pill
        scale = self._get_size_scale()
        cfg = getattr(self.mgr, "cfg", None)
        dpr = getattr(self, "_paint_dpr", 1.0)

        p.save()
        p.scale(scale, scale)
        lw, lh, lcy = w / scale, h / scale, cy / scale

        # time (left)
        tfont = qfont(TYPE.pill_time)
        tfm = QFontMetricsF(tfont)
        p.setFont(tfont)
        p.setPen(ISLAND.color.value)
        p.drawText(QPointF(L.time_x, lcy + tfm.capHeight() / 2.0 + L.text_nudge), format_clock(now, cfg, with_period=True))

        # divider: 1 x 18.3, centred on the pill
        p.fillRect(QRectF(lw / 2.0 - L.divider_w / 2.0, lcy - L.divider_h / 2.0, L.divider_w, L.divider_h),
                   ISLAND.color.divider)

        # weather: glyph + temperature, right-anchored (shifted left when the status dot is shown)
        temp = self.format_temp(self.weather_temp)
        pfont = qfont(TYPE.pill_temp)
        pfm = QFontMetricsF(pfont)
        temp_w = pfm.horizontalAdvance(temp)
        if dot_col is not None:
            dot_left = lw - L.dot_right - L.dot_d
            temp_right = dot_left - L.dot_gap
        else:
            temp_right = lw - L.right_pad
        temp_left = temp_right - temp_w
        p.setFont(pfont)
        p.setPen(ISLAND.color.value)
        p.drawText(QPointF(temp_left, lcy + pfm.capHeight() / 2.0 + L.text_nudge), temp)

        kind = glyph_kind(self.weather_icon)
        gpix = self._pill_glyph_pixmap(kind, dpr * scale, 1.0)
        gx = temp_left - L.glyph_gap - L.glyph_w
        p.drawPixmap(QPointF(gx, lcy - L.glyph_h / 2.0), gpix)

        if dot_col is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(dot_col))
            p.drawEllipse(QPointF(lw - L.dot_right - L.dot_d / 2.0, lcy), L.dot_d / 2.0, L.dot_d / 2.0)
        p.restore()
