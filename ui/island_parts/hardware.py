"""ui/island_parts/hardware.py - Hardware grid and strip renderers ."""

from __future__ import annotations

import time

from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.island_parts.common import create_cached_squircle_path


class HardwarePageMixin:
    """Hardware grid and strip renderers .
    Methods only; all state lives on the AppleDynamicIsland instance."""

    _HW_STALE_S = 6.0  # a sample older than this (feed paused / stopped) is shown as unavailable

    def _hw_sample(self) -> dict:
        """Latest hardware_state sample (dict) or {} when none / stale. Values may be None (= unavailable)."""
        st = self.hardware_state
        if not st or (time.time() - self.hardware_state_t) > self._HW_STALE_S:
            return {}
        return st

    def _hw_enabled(self, key: str) -> bool:
        cfg = getattr(self.mgr, "cfg", None)
        return bool(getattr(cfg, f"island_hw_{key}", True))

    @staticmethod
    def _hw_pct(v):
        return None if v is None else max(0.0, min(100.0, float(v)))

    @staticmethod
    def _hw_pct_text(v) -> str:
        return "--" if v is None else f"{round(v):.0f}%"

    @staticmethod
    def _hw_bar_color(v, normal: str) -> str:
        return "#F0443A" if (v is not None and v > 90) else normal

    def _draw_hardware_tile_icon(self, p: QPainter, icon_name: str, cx: float, cy: float, size: float, color: QColor) -> None:
        """Renders vector hardware icon for grid and strip tiles."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if icon_name == "cpu":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 5.0, cy - 5.0, 10.0, 10.0), 1.5, 1.5)
            for d in [-3.0, 0.0, 3.0]:
                p.drawLine(QPointF(cx + d, cy - 7.0), QPointF(cx + d, cy - 5.0))
                p.drawLine(QPointF(cx + d, cy + 5.0), QPointF(cx + d, cy + 7.0))
                p.drawLine(QPointF(cx - 7.0, cy + d), QPointF(cx - 5.0, cy + d))
                p.drawLine(QPointF(cx + 5.0, cy + d), QPointF(cx + 7.0, cy + d))
        elif icon_name == "ram":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 6.0, cy - 3.5, 12.0, 7.0), 1.0, 1.0)
            p.drawLine(QPointF(cx - 2.0, cy - 3.5), QPointF(cx - 2.0, cy - 1.0))
            p.drawLine(QPointF(cx + 2.0, cy - 3.5), QPointF(cx + 2.0, cy - 1.0))
        elif icon_name == "gpu":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx - 6.0, cy - 4.0, 12.0, 8.0), 1.5, 1.5)
            p.drawEllipse(QPointF(cx, cy), 2.2, 2.2)
        elif icon_name == "net_up":
            p.setPen(QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy + 4.5), QPointF(cx, cy - 4.5))
            p.drawLine(QPointF(cx - 3.0, cy - 1.5), QPointF(cx, cy - 4.5))
            p.drawLine(QPointF(cx + 3.0, cy - 1.5), QPointF(cx, cy - 4.5))
        elif icon_name == "net_down":
            p.setPen(QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx, cy - 4.5), QPointF(cx, cy + 4.5))
            p.drawLine(QPointF(cx - 3.0, cy + 1.5), QPointF(cx, cy + 4.5))
            p.drawLine(QPointF(cx + 3.0, cy + 1.5), QPointF(cx, cy + 4.5))
        elif icon_name == "disk":
            p.setPen(QPen(color, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy - 3.0), 5.5, 2.0)
            p.drawArc(QRectF(cx - 5.5, cy - 2.0, 11.0, 4.0), 180 * 16, 180 * 16)
            p.drawArc(QRectF(cx - 5.5, cy + 1.0, 11.0, 4.0), 180 * 16, 180 * 16)
            p.drawLine(QPointF(cx - 5.5, cy - 3.0), QPointF(cx - 5.5, cy + 3.0))
            p.drawLine(QPointF(cx + 5.5, cy - 3.0), QPointF(cx + 5.5, cy + 3.0))
        else:
            p.setPen(QPen(color, 1.2))
            p.drawEllipse(QPointF(cx, cy), 3.5, 3.5)

        p.restore()

    def _draw_hardware_grid(self, p: QPainter, w: float, h: float,
                            now: float, t: float, dark: bool,
                            primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Expanded Hardware Monitor 2x3 Grid Card (390x188 px):
        6 modular tiles (radius ~17px, fill #FFFFFF17, border 1px #FFFFFF0A):
        1. CPU %: icon, label "CPU", value, 4px progress bar in sky blue #45C1EE (shifts to red #F0443A if >90%).
        2. RAM: icon, label "RAM", value (e.g. "8.2 / 16.0 GB"), 4px bar in amber #F5A524 (shifts to red if >90%).
        3. GPU %: icon, label "GPU", value (or "--"), 4px bar in red #F0443A.
        4. NET UP: up arrow icon, label "NET UP", value (e.g. "1.2 MB/s"), no bar.
        5. NET DOWN: down arrow icon, label "NET DOWN", value (e.g. "14.5 MB/s"), no bar.
        6. DISK %: icon, label "DISK", value, 4px bar in sky blue #45C1EE (shifts to red if >90%).
        """
        scale = self._get_size_scale()

        hw = self._hw_sample()
        cpu_val = self._hw_pct(hw.get("cpu"))
        ram_pct = self._hw_pct(hw.get("ram_pct"))
        gpu_pct = self._hw_pct(hw.get("gpu"))
        disk_val = self._hw_pct(hw.get("disk_pct"))
        ram_used, ram_total = hw.get("ram_used_gb"), hw.get("ram_total_gb")
        ram_text = f"{ram_used:.1f} / {ram_total:.1f} GB" if (ram_used is not None and ram_total) else "--"

        try:
            from hardware_adapter import format_speed
        except Exception:
            format_speed = lambda v: f"{v:.0f} B/s"  # noqa: E731
        up, down = hw.get("net_up_bps"), hw.get("net_down_bps")
        net_up_str = "--" if up is None else format_speed(up)
        net_down_str = "--" if down is None else format_speed(down)

        tile_w = 108.0 * scale
        tile_h = 68.0 * scale
        tile_r = 17.0 * scale
        gap_x = 8.0 * scale
        gap_y = 10.0 * scale

        margin_x = 18.0 * scale

        all_tiles = [
            ("cpu", "CPU", self._hw_pct_text(cpu_val), cpu_val, True, self._hw_bar_color(cpu_val, "#45C1EE")),
            ("ram", "RAM", ram_text, ram_pct, True, self._hw_bar_color(ram_pct, "#F5A524")),
            ("gpu", "GPU", self._hw_pct_text(gpu_pct), gpu_pct, True, "#F0443A"),
            ("net_up", "NET UP", net_up_str, None, False, ""),
            ("net_down", "NET DOWN", net_down_str, None, False, ""),
            ("disk", "DISK", self._hw_pct_text(disk_val), disk_val, True, self._hw_bar_color(disk_val, "#45C1EE")),
        ]
        cfg_key = {"cpu": "cpu", "ram": "ram", "gpu": "gpu", "net_up": "net", "net_down": "net", "disk": "disk"}
        tiles_data = [t for t in all_tiles if self._hw_enabled(cfg_key[t[0]])]
        if not tiles_data:
            p.setFont(QFont("Segoe UI Variable Text", int(10.5 * scale), QFont.Weight.Normal))
            p.setPen(QColor("#8E9094"))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "No sensors enabled")
            return
        n_rows = (len(tiles_data) + 2) // 3
        margin_y = (h - (n_rows * tile_h + (n_rows - 1) * gap_y)) / 2.0

        tile_bg = QColor(255, 255, 255, 23 if dark else 55)
        tile_border = QColor(255, 255, 255, 10 if dark else 25)

        for idx, (icon_name, label, val_text, pct, has_bar, bar_color) in enumerate(tiles_data):
            row = idx // 3
            col = idx % 3
            tx = margin_x + col * (tile_w + gap_x)
            ty = margin_y + row * (tile_h + gap_y)
            tr = QRectF(tx, ty, tile_w, tile_h)
            t_path = create_cached_squircle_path(tr, tile_r)

            # Inset tile fill #FFFFFF17 and border 1px #FFFFFF0A
            p.fillPath(t_path, QBrush(tile_bg))
            p.strokePath(t_path, QPen(tile_border, 1.0))

            # Icon & Label
            icx = tx + 14.0 * scale
            icy = ty + 16.0 * scale
            self._draw_hardware_tile_icon(p, icon_name, icx, icy, 12.0 * scale, QColor("#8E9094"))

            p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
            p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
            p.drawText(QRectF(tx + 24.0 * scale, ty + 8.0 * scale, tile_w - 30.0 * scale, 16.0 * scale),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

            # Value
            if label == "RAM":
                p.setFont(QFont("Segoe UI Variable Display", int(10.5 * scale), QFont.Weight.Bold))
            elif len(val_text) >= 8:
                p.setFont(QFont("Segoe UI Variable Display", int(12.0 * scale), QFont.Weight.Bold))
            else:
                p.setFont(QFont("Segoe UI Variable Display", int(15.0 * scale), QFont.Weight.Bold))
            p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
            p.drawText(QRectF(tx + 10.0 * scale, ty + 24.0 * scale, tile_w - 20.0 * scale, 22.0 * scale),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, val_text)

            # 4px Progress Bar
            if has_bar:
                bar_x = tx + 10.0 * scale
                bar_y = ty + tile_h - 12.0 * scale
                bar_w = tile_w - 20.0 * scale
                bar_h = 4.0 * scale
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(255, 255, 255, 22 if dark else 40)))
                p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2.0 * scale, 2.0 * scale)

                fill_w = bar_w * min(1.0, max(0.0, (pct or 0.0) / 100.0))
                if fill_w > 0:
                    p.setBrush(QBrush(QColor(bar_color)))
                    p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2.0 * scale, 2.0 * scale)

    def _draw_hardware_strip(self, p: QPainter, w: float, h: float,
                             now: float, t: float, dark: bool,
                             primary: QColor, secondary: QColor, accent: QColor) -> None:
        """
        Hardware 5-Item Telemetry Strip (446x69 px, radius 28px):
        5 horizontal segments: FPS (JARVIS tracking FPS), CPU, RAM, GPU, DISK.
        Switchable via island_hardware_layout ("grid" vs "strip").
        """
        scale = self._get_size_scale()

        hw = self._hw_sample()
        fps_val = hw.get("fps")
        cpu_val = self._hw_pct(hw.get("cpu"))
        ram_pct = self._hw_pct(hw.get("ram_pct"))
        gpu_pct = self._hw_pct(hw.get("gpu"))
        disk_val = self._hw_pct(hw.get("disk_pct"))

        segments = [("FPS", "--" if not fps_val else f"{round(fps_val)} FPS", "#45C1EE")]
        if self._hw_enabled("cpu"):
            segments.append(("CPU", self._hw_pct_text(cpu_val), self._hw_bar_color(cpu_val, "#45C1EE")))
        if self._hw_enabled("ram"):
            segments.append(("RAM", self._hw_pct_text(ram_pct), self._hw_bar_color(ram_pct, "#F5A524")))
        if self._hw_enabled("gpu"):
            segments.append(("GPU", self._hw_pct_text(gpu_pct), "#F0443A"))
        if self._hw_enabled("disk"):
            segments.append(("DISK", self._hw_pct_text(disk_val), self._hw_bar_color(disk_val, "#45C1EE")))

        mx = 14.0 * scale
        seg_w = (w - 2.0 * mx) / len(segments)
        line_col = QColor(255, 255, 255, 10 if dark else 25)

        for idx, (label, val_text, accent_hex) in enumerate(segments):
            sx = mx + idx * seg_w
            if idx > 0:
                p.setPen(QPen(line_col, 1.0))
                p.drawLine(QPointF(sx, 16.0 * scale), QPointF(sx, h - 16.0 * scale))

            p.setFont(QFont("Segoe UI Variable Text", int(8.5 * scale), QFont.Weight.DemiBold))
            p.setPen(QColor("#8E9094" if dark else "#6B6E76"))
            p.drawText(QRectF(sx, 14.0 * scale, seg_w, 16.0 * scale), Qt.AlignmentFlag.AlignCenter, label)

            p.setFont(QFont("Segoe UI Variable Display", int(15.0 * scale), QFont.Weight.Bold))
            p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
            p.drawText(QRectF(sx, 32.0 * scale, seg_w, 24.0 * scale), Qt.AlignmentFlag.AlignCenter, val_text)
