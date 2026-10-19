"""ui/island_parts/hardware.py - Hardware grid and strip renderers plus adapter lifecycle (verbatim move from island.py)."""

from __future__ import annotations

from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.island_parts.common import create_cached_squircle_path


class HardwarePageMixin:
    """Hardware grid and strip renderers plus adapter lifecycle (verbatim move from island.py).
    Methods only; all state lives on the AppleDynamicIsland instance."""

    def _sync_hardware_adapter_lifecycle(self, now: float) -> None:
        """Polls hardware adapter only while hardware page is active; pauses when collapsed or on other pages."""
        is_hw_active = (
            self.expanded_card in ("hardware", "hardware_grid", "hardware_strip")
            and (getattr(self, "pin_open", False) or now < self.expanded_until)
        )
        try:
            from hardware_adapter import get_hardware_adapter
            adapter = get_hardware_adapter()
            if is_hw_active:
                if adapter.is_paused():
                    adapter.resume()
                self._cached_hardware_metrics = adapter.get_metrics()
            else:
                if not adapter.is_paused():
                    adapter.pause()
        except Exception:
            pass

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

        m = self._cached_hardware_metrics
        if not m:
            try:
                from hardware_adapter import get_hardware_adapter
                m = get_hardware_adapter().get_metrics()
            except Exception:
                m = None

        cpu_val = (m.cpu_percent if m and m.cpu_percent > 0 else 38.0)
        ram_used = (m.ram_used_gb if m and m.ram_used_gb > 0 else 8.2)
        ram_total = (m.ram_total_gb if m and m.ram_total_gb > 0 else 16.0)
        ram_pct = (m.ram_percent if m and m.ram_percent > 0 else (ram_used / ram_total * 100.0))
        gpu_pct = (m.gpu_percent if m and m.gpu_percent is not None else 24.0)
        disk_val = (m.disk_percent if m and m.disk_percent > 0 else 54.0)

        try:
            from hardware_adapter import format_speed
            net_up_str = format_speed(m.net_up_bytes_sec) if m and m.net_up_bytes_sec > 0 else "1.2 MB/s"
            net_down_str = format_speed(m.net_down_bytes_sec) if m and m.net_down_bytes_sec > 0 else "14.5 MB/s"
        except Exception:
            net_up_str = "1.2 MB/s"
            net_down_str = "14.5 MB/s"

        tile_w = 108.0 * scale
        tile_h = 68.0 * scale
        tile_r = 17.0 * scale
        gap_x = 8.0 * scale
        gap_y = 10.0 * scale

        margin_x = 18.0 * scale
        margin_y = (h - (2.0 * tile_h + gap_y)) / 2.0

        tiles_data = [
            ("cpu", "CPU", f"{round(cpu_val):.0f}%", cpu_val, True, "#F0443A" if cpu_val > 90 else "#45C1EE"),
            ("ram", "RAM", f"{ram_used:.1f} / {ram_total:.1f} GB", ram_pct, True, "#F0443A" if ram_pct > 90 else "#F5A524"),
            ("gpu", "GPU", f"{round(gpu_pct):.0f}%" if gpu_pct is not None else "--", gpu_pct if gpu_pct is not None else 0.0, True, "#F0443A"),
            ("net_up", "NET UP", net_up_str, 0.0, False, ""),
            ("net_down", "NET DOWN", net_down_str, 0.0, False, ""),
            ("disk", "DISK", f"{round(disk_val):.0f}%", disk_val, True, "#F0443A" if disk_val > 90 else "#45C1EE"),
        ]

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

                fill_w = bar_w * min(1.0, max(0.0, pct / 100.0))
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

        m = self._cached_hardware_metrics
        if not m:
            try:
                from hardware_adapter import get_hardware_adapter
                m = get_hardware_adapter().get_metrics()
            except Exception:
                m = None

        fps_val = (m.fps if m and m.fps > 0 else 60.0)
        cpu_val = (m.cpu_percent if m and m.cpu_percent > 0 else 38.0)
        ram_used = (m.ram_used_gb if m and m.ram_used_gb > 0 else 8.2)
        ram_total = (m.ram_total_gb if m and m.ram_total_gb > 0 else 16.0)
        ram_pct = (m.ram_percent if m and m.ram_percent > 0 else (ram_used / ram_total * 100.0))
        gpu_pct = (m.gpu_percent if m and m.gpu_percent is not None else 24.0)
        disk_val = (m.disk_percent if m and m.disk_percent > 0 else 54.0)

        segments = [
            ("FPS", f"{round(fps_val)} FPS", "#45C1EE"),
            ("CPU", f"{round(cpu_val):.0f}%", "#F0443A" if cpu_val > 90 else "#45C1EE"),
            ("RAM", f"{round(ram_pct):.0f}%", "#F0443A" if ram_pct > 90 else "#F5A524"),
            ("GPU", f"{round(gpu_pct):.0f}%" if gpu_pct is not None else "--", "#F0443A"),
            ("DISK", f"{round(disk_val):.0f}%", "#F0443A" if disk_val > 90 else "#45C1EE"),
        ]

        mx = 14.0 * scale
        seg_w = (w - 2.0 * mx) / 5.0
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
