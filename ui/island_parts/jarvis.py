"""ui/island_parts/jarvis.py - JARVIS status card renderer ."""

from __future__ import annotations

import math
from ui.tokens import COLOR
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.island_parts.common import create_cached_squircle_path


class JarvisPageMixin:
    """JARVIS status card renderer .
    Methods only; all state lives on the AppleDynamicIsland instance."""

    def _draw_jarvis_card(self, p: QPainter, w: float, h: float,
                          now: float, t: float, dark: bool,
                          primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded JARVIS Assistant Card (390x188 px):
        - Header: "J.A.R.V.I.S." with pulsing green status dot.
        - Modular tiles (radius ~17px, fill #FFFFFF17, border 1px #FFFFFF0A):
          1. Assistant Status tile: Tracking active/paused, camera status, mic state.
          2. Quick toggle button: "Pause Tracking" / "Resume Tracking" (interactive target).
          3. Last Command tile: Displaying the last spoken/executed command and its result status.
        """
        scale = self._get_size_scale()
        lx = 22.0 * scale
        content_w = (w - 28.0 * scale) - lx
        tile_r = 17.0 * scale

        # Real data: ctx.assistant_status() (state, last_command, last_result) + camera FPS from hardware_state
        status = self._assistant_status()

        tile_bg = QColor(255, 255, 255, 23 if dark else 55)
        tile_border = QColor(255, 255, 255, 10 if dark else 25)

        # ── 1. Header: "J.A.R.V.I.S." with pulsing green status dot ──────────
        p.setFont(QFont("Segoe UI Variable Display", int(13.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(int(lx), int(29.0 * scale), "J.A.R.V.I.S.")
        adv = p.fontMetrics().horizontalAdvance("J.A.R.V.I.S.")

        # Pulsing green status dot
        dot_cx = lx + adv + 10.0 * scale
        dot_cy = 24.0 * scale
        dot_r = 3.2 * scale
        pulse_alpha = int(180 + 75 * math.sin(t * 3.5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(52, 199, 89, int(pulse_alpha * 0.35))))
        p.drawEllipse(QPointF(dot_cx, dot_cy), dot_r + 2.5 * scale, dot_r + 2.5 * scale)
        p.setBrush(QBrush(QColor(52, 199, 89, pulse_alpha)))
        p.drawEllipse(QPointF(dot_cx, dot_cy), dot_r, dot_r)

        # Trailing category tag
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094"))
        p.drawText(QRectF(lx, 15.0 * scale, content_w, 16.0 * scale),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "VOICE & VISION")

        # ── 2. Row 1: Status Tile + Quick Toggle Button ─────────────────────
        ty1 = 38.0 * scale
        th1 = 66.0 * scale
        gap = 8.0 * scale
        status_w = 202.0 * scale
        btn_w = content_w - status_w - gap
        btn_x = lx + status_w + gap

        # Tile 1: Assistant Status Tile
        s_rect = QRectF(lx, ty1, status_w, th1)
        s_path = create_cached_squircle_path(s_rect, tile_r)
        p.fillPath(s_path, QBrush(tile_bg))
        p.strokePath(s_path, QPen(tile_border, 1.0))

        # Status tile title
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(QRectF(lx + 12.0 * scale, ty1 + 7.0 * scale, status_w - 24.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "ASSISTANT STATUS")

        # Status metrics
        ctx = self._ctx()
        if ctx is not None and hasattr(ctx, "is_tracking_paused"):
            is_tracking = not self._tracking_is_paused()
        else:
            is_tracking = self.active and not self._tracking_is_paused()
        # Tracking indicator dot + text
        track_dot_y = ty1 + 30.0 * scale
        track_dot_col = QColor("#34C759") if is_tracking else QColor("#FF9F0A")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_dot_col))
        p.drawEllipse(QPointF(lx + 16.0 * scale, track_dot_y), 3.0 * scale, 3.0 * scale)

        p.setFont(QFont("Segoe UI Variable Text", int(9.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        track_txt = "Tracking: Active" if is_tracking else "Tracking: Paused"
        p.drawText(QRectF(lx + 24.0 * scale, track_dot_y - 8.0 * scale, status_w - 32.0 * scale, 16.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, track_txt)

        # Camera & Mic indicators
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.Normal))
        p.setPen(QColor("#8E9094"))
        hw = self.hardware_state or {}
        fps = hw.get("fps")
        cam_txt = f"Camera: {round(fps)} FPS" if fps else "Camera: --"
        cam_mic_txt = f"{cam_txt}   •   {str(status.get('state', 'idle')).capitalize()}"
        if self.worker_health_state == "down":
            p.setPen(QColor("#FF453A"))
            cam_mic_txt = "Down: " + (self.worker_health_reason or "worker stopped")
        p.drawText(QRectF(lx + 12.0 * scale, ty1 + 45.0 * scale, status_w - 24.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, cam_mic_txt)

        # Tile 2: Quick Toggle Button (Interactive target)
        b_rect = QRectF(btn_x, ty1, btn_w, th1)
        b_path = create_cached_squircle_path(b_rect, tile_r)

        is_hover = (self._hovered_btn in ("jarvis_toggle_tracking", "jarvis_pause"))
        is_flash = (now < self._btn_flash_until and is_hover)

        if is_flash:
            b_bg = QColor(0, 229, 255, 65)
            b_stroke = QPen(QColor(0, 229, 255, 220), 1.4 * scale)
        elif is_hover:
            b_bg = QColor(255, 255, 255, 42 if dark else 75)
            b_stroke = QPen(QColor(0, 229, 255, 180), 1.2 * scale)
        else:
            b_bg = tile_bg
            b_stroke = QPen(tile_border, 1.0)

        p.fillPath(b_path, QBrush(b_bg))
        p.strokePath(b_path, b_stroke)

        # Dwell progress ring around button if dwelling
        if is_hover and self._btn_dwell_elapsed > 0.03:
            dwell_pen = QPen(COLOR.HUD_ONLY.neon_cyan, 2.0 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(dwell_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            d_rect = b_rect.adjusted(-2.5 * scale, -2.5 * scale, 2.5 * scale, 2.5 * scale)
            p.drawRoundedRect(d_rect, tile_r + 2.5 * scale, tile_r + 2.5 * scale)

        # Icon inside button
        ic_cx = btn_x + btn_w / 2.0
        ic_cy = ty1 + 20.0 * scale
        if is_tracking:
            # Pause icon (two vertical bars)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#FF9F0A")))
            bw = 3.0 * scale
            bh = 10.0 * scale
            p.drawRoundedRect(QRectF(ic_cx - 4.5 * scale, ic_cy - bh / 2.0, bw, bh), 1.0 * scale, 1.0 * scale)
            p.drawRoundedRect(QRectF(ic_cx + 1.5 * scale, ic_cy - bh / 2.0, bw, bh), 1.0 * scale, 1.0 * scale)
            btn_txt = "Pause Tracking"
        else:
            # Play / Resume triangle
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#34C759")))
            tri = QPolygonF([
                QPointF(ic_cx - 4.0 * scale, ic_cy - 5.5 * scale),
                QPointF(ic_cx + 5.5 * scale, ic_cy),
                QPointF(ic_cx - 4.0 * scale, ic_cy + 5.5 * scale)
            ])
            p.drawPolygon(tri)
            btn_txt = "Resume Tracking"

        p.setFont(QFont("Segoe UI Variable Text", int(9.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(QRectF(btn_x + 4.0 * scale, ty1 + 31.0 * scale, btn_w - 8.0 * scale, 16.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, btn_txt)

        p.setFont(QFont("Segoe UI Variable Text", int(7.5 * scale), QFont.Weight.Normal))
        p.setPen(QColor("#8E9094"))
        p.drawText(QRectF(btn_x + 4.0 * scale, ty1 + 47.0 * scale, btn_w - 8.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignCenter, "Click / Dwell")

        # ── 3. Row 2: Last Command Tile ─────────────────────────────────────
        ty2 = ty1 + th1 + 8.0 * scale
        th2 = 60.0 * scale
        cmd_rect = QRectF(lx, ty2, content_w, th2)
        cmd_path = create_cached_squircle_path(cmd_rect, tile_r)

        p.fillPath(cmd_path, QBrush(tile_bg))
        p.strokePath(cmd_path, QPen(tile_border, 1.0))

        # Title: "LAST COMMAND"
        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(QRectF(lx + 12.0 * scale, ty2 + 7.0 * scale, 140.0 * scale, 14.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "LAST COMMAND")

        last_cmd = str(status.get("last_command", "") or "").strip()
        last_res = str(status.get("last_result", "") or "")

        if not last_cmd:
            # Empty state: nothing has been executed yet
            p.setFont(QFont("Segoe UI Variable Text", int(10.5 * scale), QFont.Weight.Normal))
            p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
            p.drawText(QRectF(lx + 12.0 * scale, ty2 + 24.0 * scale, content_w - 24.0 * scale, 22.0 * scale),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "No commands yet")
            return

        ok = last_res != "fail"
        badge_col = QColor(52, 199, 89) if ok else QColor(255, 69, 58)
        badge_w = 78.0 * scale
        badge_h = 17.0 * scale
        badge_rx = lx + content_w - badge_w - 10.0 * scale
        badge_ry = ty2 + 6.0 * scale
        bdg_rect = QRectF(badge_rx, badge_ry, badge_w, badge_h)
        bdg_path = create_cached_squircle_path(bdg_rect, badge_h / 2.0)
        p.fillPath(bdg_path, QBrush(QColor(badge_col.red(), badge_col.green(), badge_col.blue(), 35)))
        p.strokePath(bdg_path, QPen(QColor(badge_col.red(), badge_col.green(), badge_col.blue(), 90), 1.0))

        p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
        p.setPen(badge_col)
        p.drawText(bdg_rect, Qt.AlignmentFlag.AlignCenter, "Executed" if ok else "Failed")

        # Command text
        p.setFont(QFont("Segoe UI Variable Display", int(11.5 * scale), QFont.Weight.Bold))
        p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
        p.drawText(QRectF(lx + 12.0 * scale, ty2 + 22.0 * scale, content_w - 24.0 * scale, 18.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, last_cmd.capitalize())

        # Result summary
        p.setFont(QFont("Segoe UI Variable Text", int(9.5 * scale), QFont.Weight.Normal))
        p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
        p.drawText(QRectF(lx + 12.0 * scale, ty2 + 39.0 * scale, content_w - 24.0 * scale, 15.0 * scale),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "Completed" if ok else "Did not complete")

    def _assistant_status(self) -> dict:
        """ctx.assistant_status() or an idle default when no ctx is wired."""
        ctx = self._ctx()
        if ctx is not None and hasattr(ctx, "assistant_status"):
            try:
                return dict(ctx.assistant_status())
            except Exception:
                pass
        return {"state": "idle", "last_command": "", "last_result": ""}
