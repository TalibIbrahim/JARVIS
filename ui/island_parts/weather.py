"""ui/island_parts/weather.py - Weather card renderer, vector weather glyphs and temperature formatting (verbatim move from island.py)."""

from __future__ import annotations

import math
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.tokens import TYPE, qfont
from ui.island_parts.common import create_cached_squircle_path


class WeatherPageMixin:
    """Weather card renderer, vector weather glyphs and temperature formatting (verbatim move from island.py).
    Methods only; all state lives on the AppleDynamicIsland instance."""

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
