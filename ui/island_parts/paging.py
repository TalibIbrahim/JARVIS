"""ui/island_parts/paging.py - Page-dot column drawing, hit-testing and wheel paging (verbatim move from island.py)."""

from __future__ import annotations

import math
from typing import Optional
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtCore import QPoint, QPointF, Qt


class PagingMixin:
    """Page-dot column drawing, hit-testing and wheel paging (verbatim move from island.py).
    Methods only; all state lives on the AppleDynamicIsland instance."""

    def _draw_page_dots(self, p: QPainter, w: float, h: float, dark: bool) -> None:
        """
        Draws the vertical column of 5 page indicator dots along the right edge:
        - Position: x = w - 16 px, vertically centered
        - Pages: Media, Time/Calendar, Weather, Hardware, Jarvis
        - Active dot: 100% solid white (or near-black in light mode)
        - Inactive dot: 30% alpha
        """
        scale = self._get_size_scale()
        dot_x = w - 16.0 * scale
        num_dots = len(self.card_pages)
        dot_spacing = 14.0 * scale
        total_h = (num_dots - 1) * dot_spacing
        start_y = (h - total_h) / 2.0
        r_dot = 3.0 * scale

        curr_page = self.expanded_card or "weather"
        if curr_page == "calendar":
            curr_page = "time"
        elif curr_page in ("hardware_grid", "hardware_strip"):
            curr_page = "hardware"
        elif curr_page in ("sending", "jarvis_card"):
            curr_page = "jarvis"

        active_idx = self.card_pages.index(curr_page) if curr_page in self.card_pages else 2

        active_col = QColor(255, 255, 255, 255) if dark else QColor(28, 28, 30, 255)
        inactive_col = QColor(255, 255, 255, 77) if dark else QColor(0, 0, 0, 77)

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(num_dots):
            dy = start_y + i * dot_spacing
            if i == active_idx:
                p.setBrush(QBrush(active_col))
            else:
                p.setBrush(QBrush(inactive_col))
            p.drawEllipse(QPointF(dot_x, dy), r_dot, r_dot)

    def _dot_at(self, pos: QPointF | QPoint) -> Optional[int]:
        """Hit test for the page dots on expanded cards."""
        if not self.expanded_card:
            return None
        w = max(float(self.width()), self.spring_w.target)
        h = max(float(self.height()), self.spring_h.target)
        scale = self._get_size_scale()
        dot_x = w - 16.0 * scale
        num_dots = len(self.card_pages)
        dot_spacing = 14.0 * scale
        total_h = (num_dots - 1) * dot_spacing
        start_y = (h - total_h) / 2.0
        hit_radius = 12.0 * scale

        for i in range(num_dots):
            dy = start_y + i * dot_spacing
            if math.hypot(pos.x() - dot_x, pos.y() - dy) <= hit_radius:
                return i
        return None

    def wheelEvent(self, event) -> None:
        """Mouse wheel scrolling to switch between expanded card pages."""
        if self.expanded_card:
            curr_page = self.expanded_card
            if curr_page == "calendar":
                curr_page = "time"
            elif curr_page in ("hardware_grid", "hardware_strip"):
                curr_page = "hardware"
            elif curr_page in ("sending", "jarvis_card"):
                curr_page = "jarvis"

            idx = self.card_pages.index(curr_page) if curr_page in self.card_pages else 0
            delta = event.angleDelta().y()
            if delta < 0:
                new_idx = (idx + 1) % len(self.card_pages)
            elif delta > 0:
                new_idx = (idx - 1) % len(self.card_pages)
            else:
                return
            self.expand_card(self.card_pages[new_idx])
            event.accept()
            return
        super().wheelEvent(event)
