"""ui/island_parts/calendar.py - Calendar / time card renderer (MEASUREMENT_SHEET_v2).

The whole card content (left date panel, weekday header, rule, month grid) is rendered once into a pixmap keyed by
(date, first weekday, dpr, size scale) and blitted; it is redrawn only when the day (or config) changes.
"""

from __future__ import annotations

import datetime
from typing import Optional
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPixmap
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.tokens import ISLAND, TYPE, qfont
from ui.island_parts.common import create_cached_squircle_path

_L = ISLAND.calendar
_C = ISLAND.color


class CalendarPageMixin:
    """Calendar / time card renderer. Methods only; all state lives on the AppleDynamicIsland instance."""

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

    # ── cached content pixmap ───────────────────────────────────────────────

    def _calendar_pixmap(self, data, dpr: float, scale: float) -> QPixmap:
        key = (self._cached_calendar_key, round(dpr, 3), round(scale, 3))
        if getattr(self, "_cal_pix_key", None) == key and getattr(self, "_cal_pix", None) is not None:
            return self._cal_pix
        pix = QPixmap(max(1, int(round(_L.w * scale * dpr))), max(1, int(round(_L.h * scale * dpr))))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.GlobalColor.transparent)
        cp = QPainter(pix)
        cp.setRenderHint(QPainter.RenderHint.Antialiasing)
        cp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        cp.scale(scale, scale)
        self._paint_calendar_content(cp, data)
        cp.end()
        self._cal_pix, self._cal_pix_key = pix, key
        return pix

    @staticmethod
    def _draw_centered(p: QPainter, text: str, cx: float, baseline: float, token, color: QColor) -> None:
        font = qfont(token)
        fm = QFontMetricsF(font)
        p.setFont(font)
        p.setPen(color)
        p.drawText(QPointF(cx - fm.horizontalAdvance(text) / 2.0, baseline), text)

    def _paint_calendar_content(self, p: QPainter, data) -> None:
        """Paint the full card content in 1x logical coordinates (origin = card top-left)."""
        # Left panel
        px, py = _L.panel_x, _L.panel_y
        panel = QRectF(px, py, _L.panel_w, _L.panel_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_C.tile_fill)
        p.drawPath(create_cached_squircle_path(panel, _L.panel_radius))
        cx = px + _L.panel_w / 2.0
        self._draw_centered(p, data.month_name[:1].upper() + data.month_name[1:].lower(), cx, py + _L.month_baseline, TYPE.cal_month, _C.accent)
        self._draw_centered(p, str(data.year), cx, py + _L.year_baseline, TYPE.cal_year, _C.dim)
        self._draw_centered(p, str(data.day), cx, py + _L.day_baseline, TYPE.cal_day, _C.value)
        self._draw_centered(p, data.weekday_name, cx, py + _L.weekday_baseline, TYPE.cal_weekday, _C.secondary)

        # Weekday header + hairline rule
        for c_idx, letter in enumerate(data.weekday_headers):
            self._draw_centered(p, letter, _L.col_x0 + c_idx * _L.col_pitch, _L.dow_baseline, TYPE.cal_dow, _C.dow)
        p.fillRect(QRectF(_L.rule_x0, _L.rule_y - _L.rule_h / 2.0, _L.rule_x1 - _L.rule_x0, _L.rule_h), _C.divider)

        # Month grid: in-month digits bright, weekend columns dim, out-of-month cells blank (as in the reference),
        # today = accent circle with a white digit
        rows = len(data.grid)
        pitch = _L.row_pitch if rows <= 5 else (4 * _L.row_pitch) / (rows - 1)
        font = qfont(TYPE.cal_digit)
        fm = QFontMetricsF(font)
        p.setFont(font)
        for r_idx, week in enumerate(data.grid):
            baseline = _L.row_baseline0 + r_idx * pitch
            for c_idx, day in enumerate(week):
                if not day.is_current_month:
                    continue
                dcx = _L.col_x0 + c_idx * _L.col_pitch
                txt = str(day.day_number)
                if day.is_today:
                    rr = _L.today_d / 2.0
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(_C.accent)
                    p.drawEllipse(QPointF(dcx, baseline - _L.digit_h / 2.0 + _L.today_dy), rr, rr)
                    p.setPen(_C.white)
                elif day.is_weekend:
                    p.setPen(_C.dim)
                else:
                    p.setPen(_C.digit)
                p.drawText(QPointF(dcx - fm.horizontalAdvance(txt) / 2.0, baseline), txt)

    def _draw_calendar_card(self, p: QPainter, w: float, h: float,
                            now: float, dark: bool,
                            primary: QColor, secondary: QColor, accent: QColor) -> None:
        """Expanded Calendar Page Card (390x188): cached date panel + weekday header + month grid."""
        scale = self._get_size_scale()
        data = self._get_calendar_data(now)
        if not data:
            return
        pix = self._calendar_pixmap(data, getattr(self, "_paint_dpr", 1.0), scale)
        p.drawPixmap(QPointF(0.0, 0.0), pix)

    def _draw_time_card(self, p: QPainter, w: float, h: float,
                        now: float, dark: bool, primary: QColor, secondary: QColor,
                        accent: Optional[QColor] = None) -> None:
        """Expanded Calendar & Time Card."""
        self._draw_calendar_card(p, w, h, now, dark, primary, secondary, accent or QColor(_C.accent))
