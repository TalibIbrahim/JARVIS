"""ui/island_parts/hardware.py - Hardware grid and strip renderers (MEASUREMENT_SHEET_v2 sections 3.5 / 3.6).

Both layouts render their static layer (tiles, icons, labels, values, bars) into a pixmap keyed by the displayed
strings (+ dpr, size scale) and blit it, so a steady 60 FPS frame is one drawPixmap; the pixmap is rebuilt only when a
sample changes the text or a bar. Real data only; unavailable sensors read "--".
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter
from ui.tokens import ISLAND, TYPE
from ui.island_parts.common import cached_page, draw_bar, draw_text, fill_round, paint_icon

_G = ISLAND.hardware
_S = ISLAND.strip
_C = ISLAND.color


def format_mbps(bits_per_s) -> str:
    """Throughput (the hardware feed posts bits/s): '1.2 Mbps' from 1 Mbps up, otherwise 'N kbps'."""
    bits = max(0.0, float(bits_per_s))
    if bits >= 1e6:
        return f"{bits / 1e6:.1f} Mbps"
    return f"{bits / 1e3:.0f} kbps"


class HardwarePageMixin:
    """Hardware grid and strip renderers. Methods only; all state lives on the island instance."""

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
    def _hw_bar_color(v, normal: QColor) -> QColor:
        """Category colour; red above 90 %."""
        return _C.red if (v is not None and v > 90) else normal

    # ── grid ────────────────────────────────────────────────────────────────

    def _hw_grid_tiles(self) -> tuple:
        """Enabled grid tiles in order: ((icon, LABEL, value text, pct, has_bar, icon colour hex, bar colour hex), ...)."""
        hw = self._hw_sample()
        cpu = self._hw_pct(hw.get("cpu"))
        ram_pct = self._hw_pct(hw.get("ram_pct"))
        gpu = self._hw_pct(hw.get("gpu"))
        disk = self._hw_pct(hw.get("disk_pct"))
        used, total = hw.get("ram_used_gb"), hw.get("ram_total_gb")
        ram_text = f"{used:.1f} / {total:.1f} GB" if (used is not None and total) else "--"
        up, down = hw.get("net_up_bps"), hw.get("net_down_bps")
        blue, amber, red = _C.accent, _C.amber, _C.red
        hx = lambda c: c.name()   # noqa: E731
        all_tiles = [
            ("cpu", "cpu", "CPU", self._hw_pct_text(cpu), cpu, True, hx(blue), hx(self._hw_bar_color(cpu, blue))),
            ("ram", "ram", "RAM", ram_text, ram_pct, True, hx(amber), hx(self._hw_bar_color(ram_pct, amber))),
            ("gpu", "gpu", "GPU", self._hw_pct_text(gpu), gpu, True, hx(red), hx(red)),
            ("net", "net_up", "NET UP", "--" if up is None else format_mbps(up), None, False, hx(blue), ""),
            ("net", "net_down", "NET DOWN", "--" if down is None else format_mbps(down), None, False, hx(blue), ""),
            ("disk", "disk", "DISK", self._hw_pct_text(disk), disk, True, hx(blue), hx(self._hw_bar_color(disk, blue))),
        ]
        return tuple(t[1:] for t in all_tiles if self._hw_enabled(t[0]))

    @staticmethod
    def _paint_hw_grid(p: QPainter, tiles: tuple) -> None:
        draw_text(p, "Hardware Monitor", _G.title_x, _G.title_baseline, TYPE.hw_title, _C.value)
        if not tiles:
            area = _G.area_bottom - _G.row_y0
            draw_text(p, "No sensors enabled", _G.w / 2.0, _G.row_y0 + area / 2.0 + 4.0, TYPE.tile_label,
                      _C.label, align="center")
            return
        rows = (len(tiles) + 1) // 2
        gap_y = _G.row_pitch - _G.tile_h
        used = rows * _G.tile_h + (rows - 1) * gap_y
        y0 = _G.row_y0 + max(0.0, (_G.area_bottom - _G.row_y0 - used) / 2.0)
        for idx, (icon, label, text, pct, has_bar, icon_hex, bar_hex) in enumerate(tiles):
            tx = _G.col_x[idx // rows]                 # column-major: CPU / RAM / GPU left, NET / DISK right
            ty = y0 + (idx % rows) * _G.row_pitch
            fill_round(p, QRectF(tx, ty, _G.tile_w, _G.tile_h), _G.tile_radius, _C.tile_fill)
            paint_icon(p, icon, tx + _G.icon_cx, ty + _G.icon_cy, _G.icon_d, QColor(icon_hex), stroke=1.55)
            x = tx + _G.text_x
            draw_text(p, label, x, ty + _G.label_baseline, TYPE.tile_label, _C.label)
            draw_text(p, text, x, ty + _G.value_baseline, TYPE.hw_value, _C.value, max_w=_G.tile_w - _G.text_x - 6.0)
            if has_bar:
                draw_bar(p, x, ty + _G.tile_h - _G.bar_bottom - _G.bar_h, _G.bar_w, _G.bar_h,
                         (pct or 0.0) / 100.0, QColor(bar_hex))

    def _draw_hardware_grid(self, p: QPainter, w: float, h: float,
                            now: float, t: float, dark: bool,
                            primary: QColor, secondary: QColor, accent: QColor) -> None:
        """Expanded Hardware Monitor 2 x 3 grid (390 x 188): title + CPU / RAM / GPU / NET UP / NET DOWN / DISK."""
        tiles = self._hw_grid_tiles()
        pix = cached_page(self, "hw_grid", tiles, _G.w, _G.h, getattr(self, "_paint_dpr", 1.0),
                          self._get_size_scale(), lambda cp: self._paint_hw_grid(cp, tiles))
        p.drawPixmap(QPointF(0.0, 0.0), pix)

    # ── strip ───────────────────────────────────────────────────────────────

    def _hw_strip_tiles(self) -> tuple:
        """((icon, LABEL, value text, pct, has_bar, icon colour hex, bar colour hex), ...) FPS first."""
        hw = self._hw_sample()
        fps = hw.get("fps")
        cpu = self._hw_pct(hw.get("cpu"))
        ram = self._hw_pct(hw.get("ram_pct"))
        gpu = self._hw_pct(hw.get("gpu"))
        disk = self._hw_pct(hw.get("disk_pct"))
        blue, amber, red = _C.accent, _C.amber, _C.red
        hx = lambda c: c.name()   # noqa: E731
        tiles = [("gauge", "FPS", "--" if not fps else f"{round(fps)}", None, False, hx(blue), "")]
        if self._hw_enabled("cpu"):
            tiles.append(("cpu", "CPU", self._hw_pct_text(cpu), cpu, True, hx(blue), hx(self._hw_bar_color(cpu, blue))))
        if self._hw_enabled("ram"):
            tiles.append(("ram", "RAM", self._hw_pct_text(ram), ram, True, hx(amber), hx(self._hw_bar_color(ram, amber))))
        if self._hw_enabled("gpu"):
            tiles.append(("gpu", "GPU", self._hw_pct_text(gpu), gpu, True, hx(red), hx(red)))
        if self._hw_enabled("disk"):
            tiles.append(("disk", "DISK", self._hw_pct_text(disk), disk, True, hx(blue), hx(self._hw_bar_color(disk, blue))))
        return tuple(tiles)

    @staticmethod
    def _paint_hw_strip(p: QPainter, tiles: tuple) -> None:
        widths = [_S.fps_w] + [_S.tile_w] * (len(tiles) - 1)
        total = sum(widths) + _S.gap * (len(tiles) - 1)
        x = _S.pad_l if len(tiles) == 5 else (_S.w - total) / 2.0
        for i, (icon, label, text, pct, has_bar, icon_hex, bar_hex) in enumerate(tiles):
            tw = widths[i]
            fill_round(p, QRectF(x, _S.tile_y, tw, _S.tile_h), _S.tile_radius, _C.tile_fill)
            paint_icon(p, icon, x + _S.icon_cx, _S.tile_y + _S.icon_cy, _S.icon_d, QColor(icon_hex), stroke=1.55)
            tx = x + _S.text_x
            draw_text(p, label, tx, _S.tile_y + _S.label_baseline, TYPE.tile_label, _C.label)
            if i == 0:
                draw_text(p, text, tx, _S.tile_y + _S.fps_value_baseline, TYPE.strip_fps, _C.value, max_w=tw - _S.text_x - 4.0)
            else:
                draw_text(p, text, tx, _S.tile_y + _S.value_baseline, TYPE.strip_value, _C.value, max_w=tw - _S.text_x - 4.0)
            if has_bar:
                bw = tw - _S.bar_x - _S.bar_right_pad
                draw_bar(p, x + _S.bar_x, _S.tile_y + _S.tile_h - _S.bar_bottom - _S.bar_h, bw, _S.bar_h,
                         (pct or 0.0) / 100.0, QColor(bar_hex))
            x += tw + _S.gap

    def _draw_hardware_strip(self, p: QPainter, w: float, h: float,
                             now: float, t: float, dark: bool,
                             primary: QColor, secondary: QColor, accent: QColor) -> None:
        """Hardware strip (446 x 69, full stadium): FPS tile + CPU / RAM / GPU / DISK tiles with bars."""
        tiles = self._hw_strip_tiles()
        pix = cached_page(self, "hw_strip", tiles, _S.w, _S.h, getattr(self, "_paint_dpr", 1.0),
                          self._get_size_scale(), lambda cp: self._paint_hw_strip(cp, tiles))
        p.drawPixmap(QPointF(0.0, 0.0), pix)
