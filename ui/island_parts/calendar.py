"""ui/island_parts/calendar.py - Calendar / time card renderer (verbatim move from island.py)."""

from __future__ import annotations

import datetime
from typing import Optional
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.tokens import TYPE, qfont
from ui.island_parts.common import create_cached_squircle_path


class CalendarPageMixin:
    """Calendar / time card renderer (verbatim move from island.py).
    Methods only; all state lives on the AppleDynamicIsland instance."""

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
