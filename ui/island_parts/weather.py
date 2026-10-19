"""ui/island_parts/weather.py - Weather card renderer (MEASUREMENT_SHEET_v2 section 3.3) and temperature formatting.

The whole card content (city, vector condition icon, temperature, condition, divider, three metric tiles) is rendered
once into a pixmap keyed by the displayed strings (+ dpr, size scale) and blitted; it is rebuilt only when the data
changes. Real data only: wind / humidity / feels-like show "--" until the weather feed delivered them.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from ui.tokens import ISLAND, TYPE
from ui.island_parts.common import cached_page, draw_text, fill_round, paint_icon, with_alpha
from ui.island_parts.compact import glyph_kind

_L = ISLAND.weather
_C = ISLAND.color


def _paint_big_cloud(p: QPainter, x: float, y: float, w: float, h: float, color: QColor) -> None:
    """Three-lobe flat-bottom cloud filling the box (x, y, w, h) as one solid shape."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    path.addRoundedRect(QRectF(x, y + h * 0.40, w, h * 0.60), h * 0.30, h * 0.30)
    path.addEllipse(QPointF(x + w * 0.27, y + h * 0.56), h * 0.30, h * 0.30)
    path.addEllipse(QPointF(x + w * 0.52, y + h * 0.43), h * 0.43, h * 0.43)
    path.addEllipse(QPointF(x + w * 0.76, y + h * 0.60), h * 0.27, h * 0.27)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPath(path)


def _paint_big_sun(p: QPainter, cx: float, cy: float, d: float, color: QColor, rays: tuple) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(cx, cy), d / 2.0, d / 2.0)
    p.setPen(QPen(color, 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    r0, r1 = d / 2.0 + 3.0, d / 2.0 + 7.4
    for deg in rays:
        a = math.radians(deg)
        p.drawLine(QPointF(cx + math.cos(a) * r0, cy + math.sin(a) * r0),
                   QPointF(cx + math.cos(a) * r1, cy + math.sin(a) * r1))


def paint_condition_icon(p: QPainter, kind: str, color: QColor) -> None:
    """Large vector condition icon in the weather icon box (own drawing; one accent colour as in the reference)."""
    ix, iy = _L.icon_x, _L.icon_y
    cx, cy, cw, ch = _L.cloud_x, _L.cloud_y, _L.cloud_w, _L.cloud_h
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    sun_cx, sun_cy = ix + _L.sun_d / 2.0, iy + _L.sun_d / 2.0 + 0.4
    if kind == "partly":
        _paint_big_sun(p, sun_cx, sun_cy, _L.sun_d, color, (180.0, 225.0, 270.0, 315.0))
        _paint_big_cloud(p, cx, cy, cw, ch, color)
    elif kind == "sun":
        _paint_big_sun(p, ix + 27.0, iy + 17.0, 20.0, color, tuple(range(0, 360, 45)))
    elif kind == "night":
        moon = QPainterPath()
        moon.addEllipse(QPointF(ix + 27.0, iy + 17.2), 14.0, 14.0)
        cut = QPainterPath()
        cut.addEllipse(QPointF(ix + 34.0, iy + 12.0), 12.0, 12.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(moon.subtracted(cut))
    elif kind == "fog":
        p.setPen(QPen(color, 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for yy, x0, x1 in ((iy + 6.0, 6.0, 48.0), (iy + 17.0, 1.5, 53.0), (iy + 28.0, 10.0, 44.0)):
            p.drawLine(QPointF(ix + x0, yy), QPointF(ix + x1, yy))
    else:
        top = cy - 7.0 if kind in ("rain", "snow", "storm") else cy - 3.0
        _paint_big_cloud(p, ix + 7.0, top - 4.0, 41.0, 25.0, color)
        if kind == "rain":
            p.setPen(QPen(color, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for x in (ix + 18.0, ix + 28.0, ix + 38.0):
                p.drawLine(QPointF(x, iy + 28.0), QPointF(x - 3.0, iy + 34.0))
        elif kind == "snow":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            for x in (ix + 18.0, ix + 28.0, ix + 38.0):
                p.drawEllipse(QPointF(x, iy + 31.0), 2.2, 2.2)
        elif kind == "storm":
            bolt = QPainterPath()
            bolt.moveTo(ix + 30.0, iy + 23.0)
            bolt.lineTo(ix + 22.0, iy + 31.0)
            bolt.lineTo(ix + 27.0, iy + 31.0)
            bolt.lineTo(ix + 24.0, iy + 38.0)
            bolt.lineTo(ix + 34.0, iy + 28.5)
            bolt.lineTo(ix + 29.0, iy + 28.5)
            bolt.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPath(bolt)
    p.restore()


def format_updated_ago(updated_at: Optional[float], now: float) -> str:
    """'Updated 2h ago' style age of the weather sample ('' when unknown)."""
    if updated_at is None:
        return ""
    age = max(0.0, now - float(updated_at))
    if age < 90.0:
        return "Updated just now"
    if age < 3600.0:
        return f"Updated {int(age // 60)}m ago"
    if age < 86400.0:
        return f"Updated {int(age // 3600)}h ago"
    return f"Updated {int(age // 86400)}d ago"


class WeatherPageMixin:
    """Weather card renderer and temperature formatting. Methods only; all state lives on the island instance."""

    def format_temp(self, temp_val: float) -> str:
        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        units = str(getattr(cfg, "island_weather_units", "auto")).lower()
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

    def _wind_uses_mph(self) -> bool:
        """island_weather_units: celsius -> km/h, fahrenheit -> mph, auto -> follow the temperature symbol."""
        cfg = getattr(self.mgr, "cfg", None) or getattr(self, "cfg", None)
        units = str(getattr(cfg, "island_weather_units", "auto")).lower()
        if units == "fahrenheit":
            return True
        if units == "celsius":
            return False
        return getattr(self, "weather_unit_symbol", None) == "°F"

    def _weather_strings(self, now: float) -> tuple:
        """Displayed strings (also the cache key): (city, temp, condition, kind, wind, wind_deg, feels, hum, updated)."""
        kmh = getattr(self, "weather_wind_kmh", None)
        if kmh is None:
            wind = "--"
        elif self._wind_uses_mph():
            wind = f"{int(round(float(kmh) / 1.609344))} mph"
        else:
            wind = f"{int(round(float(kmh)))} km/h"
        deg = getattr(self, "weather_wind_dir_deg", None)
        deg = None if (kmh is None or deg is None) else int(round(float(deg))) % 360
        feels = getattr(self, "weather_feels_like", None)
        hum = getattr(self, "weather_humidity", None)
        updated = ""
        if getattr(self, "weather_stale", False):
            updated = format_updated_ago(getattr(self, "weather_updated_at", None), now)
        return (str(self.weather_city or ""), self.format_temp(self.weather_temp), str(self.weather_condition or ""),
                glyph_kind(self.weather_icon), wind, deg,
                "--" if feels is None else self.format_temp(feels),
                "--" if hum is None else f"{int(round(float(hum)))}%", updated)

    @staticmethod
    def _paint_weather_content(p: QPainter, s: tuple) -> None:
        city, temp, cond, kind, wind, deg, feels, hum, updated = s
        draw_text(p, city, _L.city_x, _L.city_baseline, TYPE.weather_city, _C.label)
        paint_condition_icon(p, kind, _C.accent)
        draw_text(p, temp, _L.temp_x, _L.temp_baseline, TYPE.weather_temp, _C.value)
        draw_text(p, cond, _L.cond_x, _L.cond_baseline, TYPE.weather_cond, _C.value, max_w=_L.div_cx - _L.cond_x - 10.0)
        if updated:
            draw_text(p, updated, _L.cond_x, _L.updated_baseline, TYPE.weather_city, _C.label)
        p.fillRect(QRectF(_L.div_cx - _L.div_w / 2.0, _L.div_y, _L.div_w, _L.div_h), _C.divider)

        icon_col = with_alpha(_C.accent, _L.tile_icon_alpha)
        tiles = (("Wind", "wind", wind), ("Feels Like", "thermo", feels), ("Humidity", "drop", hum))
        for (label, icon, value), ty in zip(tiles, _L.tile_ys):
            fill_round(p, QRectF(_L.tile_x, ty, _L.tile_w, _L.tile_h), _L.tile_radius, _C.tile_fill)
            cy = ty + (_L.tile_wind_cy if icon == "wind" else _L.tile_icon_cy)
            paint_icon(p, icon, _L.tile_x + _L.tile_icon_x + 6.5, cy, 14.0, icon_col, stroke=1.7)
            tx = _L.tile_x + _L.tile_text_x
            avail = _L.tile_w - _L.tile_text_x - 10.0
            draw_text(p, label, tx, ty + _L.tile_label_baseline, TYPE.tile_label, _C.label)
            adv = draw_text(p, value, tx, ty + _L.tile_value_baseline, TYPE.weather_value, _C.value, max_w=avail)
            if icon == "wind" and deg is not None:
                a = _L.arrow_size
                paint_icon(p, "arrow_up", tx + adv + _L.arrow_gap + a / 2.0, ty + _L.tile_value_baseline - 4.6, a,
                           _C.value, stroke=1.9, rotate_deg=float(deg))

    def _draw_weather_card(self, p: QPainter, w: float, h: float,
                           now: float, t: float, dark: bool,
                           primary: QColor, secondary: QColor, accent: QColor) -> None:
        """Expanded Weather card (390 x 188): cached content layer, one drawPixmap per frame."""
        scale = self._get_size_scale()
        s = self._weather_strings(now)
        pix = cached_page(self, "weather", s, _L.w, _L.h, getattr(self, "_paint_dpr", 1.0), scale,
                          lambda cp: self._paint_weather_content(cp, s))
        p.drawPixmap(QPointF(0.0, 0.0), pix)
