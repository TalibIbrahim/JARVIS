"""
ui/glass.py — Liquid Glass material for J.A.R.V.I.S. (Settings, radial menu, tooltips, cards, controls).

The island itself stays solid black and does NOT use this module.

What lives here
---------------
  * GLASS tokens and the user setting (glass_mode "clear"|"tinted", glass_transparency 0..1), applied live.
  * GlassSpec: the resolved material for one surface: theme (dark/light), accent, backdrop luminance,
    window tint alpha, text-plate alpha, border/specular/glow alphas. The tint alphas are SOLVED so that the
    primary and secondary label colours keep >= 4.5:1 contrast over the estimated backdrop
    (content-adaptive: a bright wallpaper under a dark window raises the tint; a dark one allows clear glass).
  * Environment: Windows "Transparency effects" flag and wallpaper luminance, re-checked cheaply (TTL) so
    toggling the Windows setting or changing the wallpaper updates live. Transparency OFF => SOLID surface,
    no blur anywhere (see ui/materials.apply_material).
  * Painted layers (cached pixmaps, never per-frame effects): soft shadow, edge layer (1 px top specular
    gradient, pre-rendered soft inner glow, hairline border).
  * GlassTooltip (+ attach_tooltips) and paint_radial_menu (the dwell radial menu).
  * concentric(): inner radius = outer radius - padding.

Layer order for a glass surface (bottom -> top):
  real backdrop blur (DWM Mica/Acrylic or Win10 accent blur; none when solid)
  -> tint (window_color)  -> [card plate (plate_color)] -> content -> edge layer (specular + glow + border)
"""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QRectF, QPointF, QSize, QTimer, QEvent, QObject, QPoint, QRect
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPixmap, QImage, QLinearGradient, QPainterPath, QFont,
    QFontMetrics, QImageReader, QRegion,
)
from PySide6.QtWidgets import QWidget, QApplication

from ui import materials as _materials
from ui.tokens import T, TYPE, qfont, SPRING_DT_MIN, SPRING_DT_MAX


# ──────────────────────────────────────────────────────────────────────────────
# Tokens
# ──────────────────────────────────────────────────────────────────────────────

class GlassTokens:
    # painted layers (fractions of 255)
    specular_alpha: float = 0.35      # 1 px top-edge highlight, white
    glow_alpha: float = 0.06          # soft inner glow, white (peak, at the edge)
    border_dark: float = 0.12         # hairline, white on dark
    border_light: float = 0.10        # hairline, black on light
    glow_steps: int = 7               # pre-rendered concentric strokes that fake the blur of the glow
    # contrast
    min_contrast: float = 4.5         # WCAG AA for body text
    target_contrast: float = 4.7      # solver aims slightly above the floor
    # geometry
    card_radius: float = 18.0
    card_pad: float = 16.0            # inner padding used for concentric radii
    window_radius: float = 0.0        # OS-framed top-level window (Win11 rounds it itself)
    tooltip_radius: float = 12.0
    # transparency floors (fraction of tint alpha left at transparency == 1)
    window_floor_tinted: float = 0.60
    window_floor_clear: float = 0.20
    # defaults
    default_mode: str = "tinted"
    default_transparency: float = 0.6


GLASS = GlassTokens()

_WINDOW_RGB = {True: (22, 22, 26), False: (246, 246, 249)}     # tint colour (neutral, theme based)
_PLATE_RGB = {True: (30, 30, 35), False: (255, 255, 255)}      # text plate behind labels


def concentric(outer_radius: float, padding: float) -> float:
    """Concentric corner geometry: inner radius = outer radius - padding (never negative)."""
    return max(0.0, float(outer_radius) - float(padding))


# ──────────────────────────────────────────────────────────────────────────────
# Contrast math (WCAG 2.x relative luminance)
# ──────────────────────────────────────────────────────────────────────────────

def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def rel_luminance(rgb: Tuple[float, float, float]) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_ratio(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> float:
    la, lb = rel_luminance(a), rel_luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def composite(top: Tuple[float, float, float], alpha: float,
              bottom: Tuple[float, float, float]) -> Tuple[float, float, float]:
    a = max(0.0, min(1.0, alpha))
    return tuple(top[i] * a + bottom[i] * (1.0 - a) for i in range(3))     # type: ignore[return-value]


# On glass the light-theme secondary label is one step darker than the system token (#6C6C70 only reaches
# ~5:1 on pure white, which leaves no room for any translucency).
_SECONDARY_LIGHT_ON_GLASS = (0x58, 0x58, 0x5D)


def label_color(kind: str, dark: bool) -> QColor:
    """Label colour to use on glass: kind in {"label", "secondary_label"} (others pass through the tokens)."""
    if kind == "secondary_label" and not dark:
        return QColor(*_SECONDARY_LIGHT_ON_GLASS)
    return QColor(getattr(T.color, kind).resolve(dark))      # always a COPY: tokens are shared, callers mutate alpha


def _label_rgb(kind: str, dark: bool) -> Tuple[int, int, int]:
    col = label_color(kind, dark)
    return (col.red(), col.green(), col.blue())


def _solve_alpha(fg: Tuple[float, float, float], tint: Tuple[float, float, float],
                 under: Tuple[float, float, float], base_alpha: float, target: float) -> float:
    """Smallest alpha >= base_alpha such that contrast(fg, tint@alpha over `under`) >= target (cap 1.0)."""
    def ok(a: float) -> bool:
        return contrast_ratio(fg, composite(tint, a, under)) >= target
    a0 = max(0.0, min(1.0, base_alpha))
    if ok(a0):
        return a0
    if not ok(1.0):
        return 1.0
    lo, hi = a0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _rgb(c: QColor) -> Tuple[float, float, float]:
    return (c.red(), c.green(), c.blue())


def readable_on(fill: QColor, target: float = 4.6) -> QColor:
    """White or near-black label, whichever reads >= target on ``fill`` (best of the two otherwise)."""
    f = _rgb(fill)
    white, ink = (255, 255, 255), (12, 14, 16)
    cw, ci = contrast_ratio(white, f), contrast_ratio(ink, f)
    if cw >= target and (cw >= ci or ci < target):
        return QColor(255, 255, 255)
    return QColor(*ink) if ci >= cw else QColor(255, 255, 255)


def solid_fill_for_white(fill: QColor, target: float = 4.6) -> QColor:
    """Darken an accent fill until white text on it reaches ``target`` (e.g. #0A84FF -> ~#0071E3)."""
    c = QColor(fill)
    for _ in range(40):
        if contrast_ratio((255, 255, 255), _rgb(c)) >= target:
            break
        c = QColor(int(c.red() * 0.97), int(c.green() * 0.97), int(c.blue() * 0.97), fill.alpha())
    return c


def fit_text(fg: QColor, bg: Tuple[float, float, float], target: float = 4.6) -> QColor:
    """Push a coloured label toward black/white (whichever helps) until it reads >= target on ``bg``."""
    c = QColor(fg)
    goal = (0, 0, 0) if rel_luminance(bg) > 0.4 else (255, 255, 255)
    for i in range(1, 25):
        if contrast_ratio(_rgb(c), bg) >= target:
            return c
        k = i / 24.0
        c = QColor(*(int(fg.red() + (goal[0] - fg.red()) * k), int(fg.green() + (goal[1] - fg.green()) * k),
                     int(fg.blue() + (goal[2] - fg.blue()) * k)))
    return c


# ──────────────────────────────────────────────────────────────────────────────
# Resolved material
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GlassSpec:
    dark: bool
    mode: str                     # "clear" | "tinted"
    transparency: float           # 0 = solid .. 1 = most transparent
    material: str                 # "mica" | "acrylic" | "blur" | "solid"
    backdrop: float               # estimated gray level (0..255) of what is behind the window
    window_rgb: Tuple[int, int, int]
    window_alpha: float
    plate_rgb: Tuple[int, int, int]
    plate_alpha: float            # card plate (always carries the text inside cards)
    label_alpha: float = 0.0      # plate behind loose text on the window (titles, footnotes); 0 when not needed

    @property
    def blurred(self) -> bool:
        return self.material != "solid"

    @property
    def window_color(self) -> QColor:
        r, g, b = self.window_rgb
        return QColor(r, g, b, int(round(self.window_alpha * 255)))

    @property
    def plate_color(self) -> QColor:
        r, g, b = self.plate_rgb
        return QColor(r, g, b, int(round(self.plate_alpha * 255)))

    @property
    def label_plate_color(self) -> QColor:
        r, g, b = self.plate_rgb
        return QColor(r, g, b, int(round(self.label_alpha * 255)))

    @property
    def border_color(self) -> QColor:
        return QColor(255, 255, 255, int(GLASS.border_dark * 255)) if self.dark else \
            QColor(0, 0, 0, int(GLASS.border_light * 255))

    @property
    def key(self) -> tuple:
        return (self.dark, self.mode, round(self.transparency, 3), self.material,
                round(self.window_alpha, 3), round(self.plate_alpha, 3), round(self.label_alpha, 3))

    # -- numeric contrast of the actual painted stack over `backdrop` ------------------------------------
    def under_window(self) -> Tuple[float, float, float]:
        bd = (self.backdrop,) * 3
        return composite(self.window_rgb, self.window_alpha, bd)

    def under_plate(self) -> Tuple[float, float, float]:
        return composite(self.plate_rgb, self.plate_alpha, self.under_window())

    def under_label_plate(self) -> Tuple[float, float, float]:
        return composite(self.plate_rgb, self.label_alpha, self.under_window())

    def contrast_report(self) -> Dict[str, float]:
        """Contrast of the painted stack over the (estimated) backdrop. *_on_window is the bare tint (no plate)."""
        pri, sec = _label_rgb("label", self.dark), _label_rgb("secondary_label", self.dark)
        win, plate, lab = self.under_window(), self.under_plate(), self.under_label_plate()
        return {
            "primary_on_window": contrast_ratio(pri, win),
            "secondary_on_window": contrast_ratio(sec, win),
            "primary_on_label_plate": contrast_ratio(pri, lab),
            "secondary_on_label_plate": contrast_ratio(sec, lab),
            "primary_on_card": contrast_ratio(pri, plate),
            "secondary_on_card": contrast_ratio(sec, plate),
        }

    def min_text_contrast(self, popup: bool = False) -> float:
        """Worst contrast any text on this surface can have (what the 4.5:1 guarantee is about)."""
        r = self.contrast_report()
        if popup:       # popups put text directly on the tint
            return min(r["primary_on_window"], r["secondary_on_window"])
        return min(r["primary_on_label_plate"], r["secondary_on_label_plate"],
                   r["primary_on_card"], r["secondary_on_card"])


@lru_cache(maxsize=256)
def _resolve(dark: bool, mode: str, t: float, material: str, backdrop: float,
             accent: Tuple[int, int, int], popup: bool) -> GlassSpec:
    t = max(0.0, min(1.0, t))
    mode = "clear" if mode == "clear" else "tinted"
    # tint colour: neutral theme colour with a whisper (5%) of the accent
    base = _WINDOW_RGB[dark]
    wrgb = tuple(int(round(base[i] * 0.95 + accent[i] * 0.05)) for i in range(3))
    prgb = _PLATE_RGB[dark]

    solid = (material == "solid")
    floor = GLASS.window_floor_clear if mode == "clear" else GLASS.window_floor_tinted
    w_base = 1.0 if solid else 1.0 - (1.0 - floor) * t
    bd = (float(backdrop),) * 3
    sec = _label_rgb("secondary_label", dark)
    pri = _label_rgb("label", dark)
    tgt = GLASS.target_contrast

    if solid:
        w_alpha = 1.0                     # opaque surface: nothing shows through
        win = wrgb
    elif popup:
        # popups put text straight on the tint: the tint itself must carry the labels (content-adaptive)
        w_alpha = max(_solve_alpha(sec, wrgb, bd, w_base, tgt), _solve_alpha(pri, wrgb, bd, w_base, tgt))
        win = composite(wrgb, w_alpha, bd)
    else:
        # windows follow the user's Clear/Tinted + transparency freely; all text then sits on plates
        w_alpha = w_base
        win = composite(wrgb, w_alpha, bd)
    # loose text on the window (titles, footnotes): plate only as strong as needed (0 when the tint suffices)
    l_alpha = 0.0 if popup else max(_solve_alpha(sec, prgb, win, 0.0, tgt), _solve_alpha(pri, prgb, win, 0.0, tgt))
    # cards carry their text: user-facing translucency, never below what the contrast needs
    p_floor = 0.30 if mode == "clear" else 0.50
    p_base = 0.92 - (0.92 - p_floor) * t
    p_alpha = max(_solve_alpha(sec, prgb, win, p_base, tgt), _solve_alpha(pri, prgb, win, p_base, tgt))
    return GlassSpec(dark, mode, t, material, float(backdrop), wrgb, w_alpha, prgb, p_alpha, l_alpha)


def resolve_spec(dark: bool, mode: str, transparency: float, material: str,
                 backdrop_gray: Optional[float] = None,
                 accent: Optional[Tuple[int, int, int]] = None, popup: bool = False) -> GlassSpec:
    """Pure resolver (used by tests/captures too). ``backdrop_gray`` None => theme prior."""
    if backdrop_gray is None:
        backdrop_gray = _PRIOR_BACKDROP[bool(dark)]
    if accent is None:
        c = T.color.accent.resolve(dark)
        accent = (c.red(), c.green(), c.blue())
    return _resolve(bool(dark), str(mode), round(float(transparency), 3), str(material),
                    float(round(backdrop_gray)), tuple(accent), bool(popup))


# What we assume is behind a window when the wallpaper is unknown: bright-ish for dark windows (worst case
# for light text), dark-ish for light windows.
_PRIOR_BACKDROP = {True: 170.0, False: 90.0}


# ──────────────────────────────────────────────────────────────────────────────
# Live environment + user setting
# ──────────────────────────────────────────────────────────────────────────────

class _State:
    mode: str = GLASS.default_mode
    transparency: float = GLASS.default_transparency
    env_checked: float = 0.0
    transparency_on: bool = True
    wallpaper_path: str = ""
    wallpaper_mtime: float = -1.0
    wp_bright: Optional[float] = None      # gray level of the brightest area (what a dark window must beat)
    wp_dark: Optional[float] = None        # gray level of the darkest area (what a light window must beat)
    force_backdrop: Optional[float] = None  # tests/captures
    listeners: list = []


_S = _State()
_ENV_TTL = 2.0


def _clamp01(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return GLASS.default_transparency
    if v != v:
        return GLASS.default_transparency
    return max(0.0, min(1.0, v))


def configure(cfg=None, *, mode: Optional[str] = None, transparency: Optional[float] = None) -> bool:
    """Read glass_mode / glass_transparency from cfg (getattr with defaults) or explicit args. True if changed."""
    if cfg is not None:
        mode = getattr(cfg, "glass_mode", GLASS.default_mode) if mode is None else mode
        transparency = getattr(cfg, "glass_transparency", GLASS.default_transparency) \
            if transparency is None else transparency
    new_mode = _S.mode if mode is None else ("clear" if str(mode) == "clear" else "tinted")
    new_t = _S.transparency if transparency is None else _clamp01(transparency)
    changed = (new_mode != _S.mode) or abs(new_t - _S.transparency) > 1e-6
    _S.mode, _S.transparency = new_mode, new_t
    if changed:
        _notify()
    return changed


def settings() -> Tuple[str, float]:
    return _S.mode, _S.transparency


def add_listener(fn: Callable[[], None]) -> None:
    """Register a repaint callback. Bound methods are held weakly (dialogs come and go)."""
    import weakref
    ref = weakref.WeakMethod(fn) if hasattr(fn, "__self__") else (lambda f=fn: f)
    for r in _S.listeners:
        if r() == fn:
            return
    _S.listeners.append(ref)


def remove_listener(fn: Callable[[], None]) -> None:
    _S.listeners[:] = [r for r in _S.listeners if r() is not None and r() != fn]


def _notify() -> None:
    _S.listeners[:] = [r for r in _S.listeners if r() is not None]
    for r in list(_S.listeners):
        fn = r()
        if fn is None:
            continue
        try:
            fn()
        except Exception:
            pass


def _sample_wallpaper() -> None:
    path = ""
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
            path = str(winreg.QueryValueEx(k, "WallPaper")[0] or "")
            winreg.CloseKey(k)
        except Exception:
            path = ""
    try:
        mtime = os.path.getmtime(path) if path and os.path.exists(path) else -1.0
    except OSError:
        mtime = -1.0
    if path == _S.wallpaper_path and mtime == _S.wallpaper_mtime:
        return
    _S.wallpaper_path, _S.wallpaper_mtime = path, mtime
    _S.wp_bright = _S.wp_dark = None
    if mtime < 0:
        return
    try:
        rd = QImageReader(path)
        rd.setAutoTransform(True)
        rd.setScaledSize(QSize(32, 18))          # JPEG decodes at reduced size: cheap
        img = rd.read()
        if img.isNull():
            return
        img = img.convertToFormat(QImage.Format.Format_RGB32)
        vals = []
        for y in range(img.height()):
            for x in range(img.width()):
                c = QColor(img.pixel(x, y))
                vals.append(0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue())
        vals.sort()
        n = len(vals)
        # 12x8-ish areas of a heavily blurred backdrop ~ percentiles of the small thumbnail
        _S.wp_bright = vals[int(n * 0.92)]
        _S.wp_dark = vals[int(n * 0.08)]
    except Exception:
        _S.wp_bright = _S.wp_dark = None


def environment(force: bool = False) -> bool:
    """Re-check Windows transparency + wallpaper (TTL 2 s). Returns True when something changed."""
    now = time.monotonic()
    if not force and now - _S.env_checked < _ENV_TTL:
        return False
    _S.env_checked = now
    before = (_S.transparency_on, _S.wallpaper_path, _S.wallpaper_mtime)
    try:
        _S.transparency_on = bool(_materials.is_transparency_available())
    except Exception:
        _S.transparency_on = True
    _sample_wallpaper()
    changed = before != (_S.transparency_on, _S.wallpaper_path, _S.wallpaper_mtime)
    if changed:
        QTimer.singleShot(0, _notify)         # never repaint listeners from inside a paintEvent
    return changed


def transparency_enabled() -> bool:
    environment()
    return _S.transparency_on


def backdrop_gray(dark: bool) -> float:
    if _S.force_backdrop is not None:
        return _S.force_backdrop
    environment()
    v = _S.wp_bright if dark else _S.wp_dark
    return float(v) if v is not None else _PRIOR_BACKDROP[bool(dark)]


def current(role: str = "window", dark: Optional[bool] = None) -> GlassSpec:
    """The live spec for ``role`` ("window" | "popup"). Cheap: all inputs cached, resolver memoised."""
    environment()
    if dark is None:
        dark = T.is_dark()
    mat = _materials.resolve_material("window" if role == "window" else "popup")
    if not _S.transparency_on:
        mat = "solid"
    return resolve_spec(dark, _S.mode, _S.transparency, mat, backdrop_gray(dark), popup=(role != "window"))


# ──────────────────────────────────────────────────────────────────────────────
# Painted, cached layers
# ──────────────────────────────────────────────────────────────────────────────

_EDGE_CACHE: Dict[tuple, QPixmap] = {}
_SHADOW_CACHE: Dict[tuple, Tuple[QPixmap, int]] = {}


def _cache_put(cache: dict, key, value, limit: int = 48) -> None:
    if len(cache) >= limit:
        cache.pop(next(iter(cache)))
    cache[key] = value


def _shape(rect: QRectF, radius: float) -> QPainterPath:
    from ui.components import create_squircle_path
    return create_squircle_path(rect, radius, 0.6)


def edge_layer(w: int, h: int, radius: float, dark: bool, dpr: float = 1.0,
               specular: bool = True, glow: bool = True) -> QPixmap:
    """Pre-rendered: soft inner glow (white ~6%), hairline border, 1 px top specular gradient (white ~35%)."""
    key = (int(w), int(h), int(round(radius * 2)), bool(dark), round(dpr, 2), specular, glow)
    pm = _EDGE_CACHE.get(key)
    if pm is not None:
        return pm
    pm = QPixmap(max(1, int(math.ceil(w * dpr))), max(1, int(math.ceil(h * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(Qt.BrushStyle.NoBrush)

    def path_inset(i: float) -> QPainterPath:
        return _shape(QRectF(i, i, w - 2 * i, h - 2 * i), max(0.0, radius - i))

    if glow:
        n = GLASS.glow_steps
        for i in range(n):
            a = GLASS.glow_alpha * 255.0 * (1.0 - i / n) ** 2
            p.setPen(QPen(QColor(255, 255, 255, int(round(a))), 1.0))
            p.drawPath(path_inset(1.5 + i))
    # hairline border
    bc = QColor(255, 255, 255, int(GLASS.border_dark * 255)) if dark else QColor(0, 0, 0, int(GLASS.border_light * 255))
    p.setPen(QPen(bc, 1.0))
    p.drawPath(path_inset(0.5))
    if specular:
        fade = max(10.0, radius * 1.4)
        g = QLinearGradient(0, 0, 0, fade)
        g.setColorAt(0.0, QColor(255, 255, 255, int(GLASS.specular_alpha * 255)))
        g.setColorAt(0.55, QColor(255, 255, 255, int(GLASS.specular_alpha * 255 * 0.25)))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(QPen(QBrush(g), 1.0))
        p.drawPath(path_inset(1.0))
    p.end()
    _cache_put(_EDGE_CACHE, key, pm)
    return pm


def _blur_alpha(a, r: int):
    """3 passes of a box blur on a float array (numpy) ~ gaussian; r in px of the (small) image."""
    import numpy as np
    if r < 1:
        return a
    for axis in (0, 1, 0, 1, 0, 1):
        k = 2 * r + 1
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r + 1, r)
        c = np.cumsum(np.pad(a, pad), axis=axis)
        if axis == 0:
            a = (c[k:, :] - c[:-k, :])[: a.shape[0], :] / k
        else:
            a = (c[:, k:] - c[:, :-k])[:, : a.shape[1]] / k
    return a


def shadow_pixmap(w: int, h: int, radius: float, blur: float, alpha: float,
                  dpr: float = 1.0) -> Tuple[QPixmap, int]:
    """Cached soft shadow (true blur via downscaled box filter). Returns (pixmap, margin px)."""
    key = (int(w), int(h), int(round(radius * 2)), int(blur), int(alpha * 255), round(dpr, 2))
    hit = _SHADOW_CACHE.get(key)
    if hit is not None:
        return hit
    m = int(math.ceil(blur * 1.5))
    W, H = w + 2 * m, h + 2 * m
    sc = 2.0
    sw, sh = max(2, int(math.ceil(W / sc))), max(2, int(math.ceil(H / sc)))
    img = QImage(sw, sh, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(1.0 / sc, 1.0 / sc)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 255))
    p.drawPath(_shape(QRectF(m, m, w, h), radius))
    p.end()
    try:
        import numpy as np
        ptr = img.constBits()
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(sh, img.bytesPerLine() // 4, 4)[:, :sw, 3].astype(np.float32) / 255.0
        arr = _blur_alpha(arr, max(1, int(round(blur / sc / 1.7))))
        out = QImage(sw, sh, QImage.Format.Format_ARGB32_Premultiplied)
        a8 = (arr * alpha * 255.0).clip(0, 255).astype(np.uint8)
        buf = np.zeros((sh, sw, 4), dtype=np.uint8)
        buf[:, :, 3] = a8                      # premultiplied black: rgb stays 0
        out = QImage(buf.data, sw, sh, sw * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    except Exception:
        out = img
    pm = QPixmap.fromImage(out.scaled(int(W * dpr), int(H * dpr), Qt.AspectRatioMode.IgnoreAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
    pm.setDevicePixelRatio(dpr)
    _cache_put(_SHADOW_CACHE, key, (pm, m))
    return pm, m


def paint_shadow(p: QPainter, rect: QRectF, radius: float, blur: float = 14.0, alpha: float = 0.28,
                 dy: float = 3.0, dpr: float = 1.0, exclude_shape: bool = True) -> None:
    """Soft drop shadow beneath a floating glass element (cached pixmap blit). The element's own footprint
    is excluded so a translucent surface is not darkened by its own shadow."""
    pm, m = shadow_pixmap(int(round(rect.width())), int(round(rect.height())), radius, blur, alpha, dpr)
    p.save()
    if exclude_shape:
        outer = QPainterPath()
        outer.addRect(rect.adjusted(-m - 2, -m - 2, m + 2, m + dy + 2))
        p.setClipPath(outer.subtracted(_shape(rect, radius)), Qt.ClipOperation.IntersectClip)
    p.drawPixmap(QPointF(rect.x() - m, rect.y() - m + dy), pm)
    p.restore()


def paint_glass(p: QPainter, rect: QRectF, radius: float, spec: GlassSpec, *, fill: str = "plate",
                shadow: bool = False, dpr: float = 1.0, specular: bool = True, glow: bool = True,
                shadow_blur: float = 14.0) -> None:
    """Fill (tint or plate), optional soft shadow under, then the edge layer on top."""
    if shadow:
        paint_shadow(p, rect, radius, shadow_blur, 0.30 if spec.dark else 0.18, 3.0, dpr)
    col = spec.plate_color if fill == "plate" else spec.window_color
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(col)
    p.drawPath(_shape(rect, radius))
    p.drawPixmap(rect.topLeft(), edge_layer(int(round(rect.width())), int(round(rect.height())),
                                            radius, spec.dark, dpr, specular, glow))


def paint_window_background(p: QPainter, size: QSize, spec: GlassSpec, dpr: float = 1.0) -> None:
    """Top-level window: tint over the real backdrop (CompositionMode_Source so alpha is exact)."""
    p.save()
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    p.fillRect(QRectF(0, 0, size.width(), size.height()), spec.window_color)
    p.restore()
    p.drawPixmap(0, 0, edge_layer(size.width(), size.height(), GLASS.window_radius, spec.dark, dpr))


# ──────────────────────────────────────────────────────────────────────────────
# Spring helper (reuses ui.components.Spring; dt clamped to [SPRING_DT_MIN, SPRING_DT_MAX])
# ──────────────────────────────────────────────────────────────────────────────

def clamp_dt(dt: float) -> float:
    return min(max(float(dt), SPRING_DT_MIN), SPRING_DT_MAX)


# ──────────────────────────────────────────────────────────────────────────────
# Tooltip
# ──────────────────────────────────────────────────────────────────────────────

class GlassTooltip(QWidget):
    """Frameless, never-activating glass tooltip: blur + tint + specular/glow/border, text on a tint plate."""
    _instance: Optional["GlassTooltip"] = None

    PAD_X, PAD_Y, MAX_W = 12, 8, 320

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._text = ""
        self._material = "solid"
        self._font = qfont(TYPE.footnote)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    @classmethod
    def instance(cls) -> "GlassTooltip":
        if cls._instance is None:
            cls._instance = GlassTooltip()
        return cls._instance

    def set_text(self, text: str) -> None:
        self._text = text
        fm = QFontMetrics(self._font)
        r = fm.boundingRect(QRect(0, 0, self.MAX_W, 1000), int(Qt.TextFlag.TextWordWrap), text)
        self.resize(r.width() + 2 * self.PAD_X + 2, r.height() + 2 * self.PAD_Y + 2)
        self.update()

    def show_at(self, global_pos: QPoint, text: str, msec: int = 6000) -> None:
        if not text:
            self.hide()
            return
        self.set_text(text)
        scr = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else QRect(0, 0, 1920, 1080)
        x = min(global_pos.x() + 12, geo.right() - self.width() - 4)
        y = global_pos.y() + 20
        if y + self.height() > geo.bottom():
            y = global_pos.y() - self.height() - 8
        self.move(max(geo.left() + 2, x), max(geo.top() + 2, y))
        self.show()
        self._apply_material()
        self._timer.start(msec)

    def _apply_material(self) -> None:
        spec = current("popup")
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            if spec.blurred:
                self._material = _materials.apply_material(hwnd, "popup", spec.dark)
                # clip the blur to the rounded shape
                r = min(GLASS.tooltip_radius, self.height() / 2.0)
                path = _shape(QRectF(0, 0, self.width(), self.height()), r)
                self.setMask(QRegion(path.toFillPolygon().toPolygon()))
            else:
                _materials.remove_backdrop(hwnd)
                self.clearMask()
                self._material = "solid"
        except Exception:
            pass

    def paintEvent(self, event) -> None:
        spec = current("popup")
        dpr = self.devicePixelRatioF()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        r = min(GLASS.tooltip_radius, self.height() / 2.0)
        rect = QRectF(0, 0, self.width(), self.height())
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(spec.window_color)
        p.drawPath(_shape(rect, r))
        p.restore()
        p.drawPixmap(0, 0, edge_layer(self.width(), self.height(), r, spec.dark, dpr))
        p.setFont(self._font)
        p.setPen(label_color("label", spec.dark))
        p.drawText(QRectF(self.PAD_X + 1, self.PAD_Y + 1, self.width() - 2 * self.PAD_X - 2,
                          self.height() - 2 * self.PAD_Y - 2),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap),
                   self._text)


class _TooltipFilter(QObject):
    _TYPES = (QEvent.Type.ToolTip, QEvent.Type.Leave, QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
              QEvent.Type.Wheel, QEvent.Type.Hide, QEvent.Type.WindowDeactivate)

    def eventFilter(self, obj, ev) -> bool:
        et = ev.type()
        if et == QEvent.Type.ToolTip:
            text = obj.toolTip() if hasattr(obj, "toolTip") else ""
            if text:
                GlassTooltip.instance().show_at(ev.globalPos(), text)
                return True
            return False
        if et in self._TYPES and GlassTooltip._instance is not None and GlassTooltip._instance.isVisible():
            GlassTooltip._instance.hide()
        return False


_TT_FILTER: Optional[_TooltipFilter] = None


def attach_tooltips(root: QWidget) -> int:
    """Route every widget under ``root`` that has a tooltip through GlassTooltip. Returns how many."""
    global _TT_FILTER
    if _TT_FILTER is None:
        _TT_FILTER = _TooltipFilter(QApplication.instance())
    try:                                           # any tooltip we do not route still matches the glass colours
        from PySide6.QtWidgets import QToolTip
        spec = current("popup")
        pal = QToolTip.palette()
        base = QColor(*spec.window_rgb)
        pal.setColor(pal.ColorRole.ToolTipBase, base)
        pal.setColor(pal.ColorRole.ToolTipText, label_color("label", spec.dark))
        QToolTip.setPalette(pal)
    except Exception:
        pass
    n = 0
    for w in [root] + root.findChildren(QWidget):
        if w.toolTip():
            w.installEventFilter(_TT_FILTER)
            n += 1
    return n


# ──────────────────────────────────────────────────────────────────────────────
# Radial (dwell) menu
# ──────────────────────────────────────────────────────────────────────────────

RADIAL_SECTORS = (("click", "Click"), ("double_click", "Double"), ("right_click", "Right"),
                  ("drag", "Drag"), ("scroll", "Scroll"))
RADIAL_R = 65.0
RADIAL_ORBIT = 44.0
RADIAL_CAP_W, RADIAL_CAP_H = 54.0, 24.0


def _radial_state(widget):
    st = getattr(widget, "_glass_radial", None)
    if st is None:
        from ui.components import Spring
        st = {"x": Spring(0.0, 320.0, 28.0), "y": Spring(0.0, 320.0, 28.0), "a": Spring(0.0, 260.0, 26.0),
              "t": time.perf_counter(), "idx": -1, "mat": None}
        widget._glass_radial = st
    return st


def radial_sector_center(idx: int, cx: float, cy: float) -> Tuple[float, float]:
    ang = math.radians(idx * (360.0 / len(RADIAL_SECTORS)) - 90.0)
    return cx + RADIAL_ORBIT * math.cos(ang), cy + RADIAL_ORBIT * math.sin(ang)


def ensure_radial_backdrop(widget) -> None:
    """Blur the disc only: window region = the disc, so the blur never leaks into the square window."""
    st = _radial_state(widget)
    spec = current("popup")
    sig = (spec.blurred, spec.dark, widget.width(), widget.height())
    if st.get("mat") == sig or sys.platform != "win32":
        return
    st["mat"] = sig
    try:
        hwnd = int(widget.winId())
        if spec.blurred:
            cx, cy = widget.width() / 2.0, widget.height() / 2.0
            r = RADIAL_R
            widget.setMask(QRegion(QRect(int(cx - r), int(cy - r), int(2 * r), int(2 * r)), QRegion.RegionType.Ellipse))
            _materials.apply_material(hwnd, "popup", spec.dark)
        else:
            widget.clearMask()
            _materials.remove_backdrop(hwnd)
    except Exception:
        pass


def release_radial_backdrop(widget) -> None:
    """Radial closed: drop the disc-shaped region and any blur so the dwell ring window is unclipped."""
    st = getattr(widget, "_glass_radial", None)
    if not st or st.get("mat") is None:
        return
    st["mat"] = None
    if sys.platform != "win32":
        return
    try:
        widget.clearMask()
        _materials.remove_backdrop(int(widget.winId()))
    except Exception:
        pass


def paint_radial_menu(widget, p: QPainter, cx: float, cy: float, dark: bool, accent: QColor) -> None:
    """
    Liquid-glass radial menu. ``widget`` supplies radial_hover / radial_prog (CursorFeedbackOverlay API).
    The selection capsule is spring-morphed between sectors (Spring, dt clamped); everything static is
    cached pixmaps.
    """
    ensure_radial_backdrop(widget)
    spec = current("popup", dark)
    st = _radial_state(widget)
    dpr = widget.devicePixelRatioF() if hasattr(widget, "devicePixelRatioF") else 1.0
    now = time.perf_counter()
    dt = clamp_dt(now - st["t"])
    st["t"] = now
    ids = [s[0] for s in RADIAL_SECTORS]
    hover = getattr(widget, "radial_hover", None)
    idx = ids.index(hover) if hover in ids else -1
    reduce = T.reduce_motion()
    if idx >= 0:
        tx, ty = radial_sector_center(idx, cx, cy)
        if st["idx"] < 0:
            st["x"].snap(tx)
            st["y"].snap(ty)
        st["x"].set_target(tx)
        st["y"].set_target(ty)
        st["a"].set_target(1.0)
    else:
        st["a"].set_target(0.0)
    st["idx"] = idx
    for k in ("x", "y", "a"):
        st[k].update(dt, reduce)
    animating = not (st["x"].settled and st["y"].settled and st["a"].settled)

    R = RADIAL_R
    disc = QRectF(cx - R, cy - R, 2 * R, 2 * R)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if not spec.blurred:                       # masked window clips shadows when blurred; solid has room
        paint_shadow(p, disc, R, 18.0, 0.34 if dark else 0.22, 4.0, dpr)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(spec.window_color)
    p.drawEllipse(disc)
    p.drawPixmap(disc.topLeft(), edge_layer(int(2 * R), int(2 * R), R, dark, dpr))
    # inner ring: concentric with the disc
    ring_r = concentric(R, 12.0)
    p.setPen(QPen(QColor(255, 255, 255, 22) if dark else QColor(0, 0, 0, 16), 0.8))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)

    font = qfont(TYPE.caption_2)
    font.setWeight(QFont.Weight.Bold)
    # morphing selection capsule (accent glass)
    a = max(0.0, min(1.0, st["a"].value))
    if a > 0.02:
        w = RADIAL_CAP_W + 2.0 * (1.0 - a)
        cap = QRectF(st["x"].value - w / 2, st["y"].value - RADIAL_CAP_H / 2, w, RADIAL_CAP_H)
        paint_shadow(p, cap, RADIAL_CAP_H / 2, 8.0, 0.30 * a, 2.0, dpr)
        fillc = solid_fill_for_white(QColor(accent))          # white label must read >= 4.5:1 on the capsule
        col = QColor(fillc.red(), fillc.green(), fillc.blue(), int(240 * a))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(cap, RADIAL_CAP_H / 2, RADIAL_CAP_H / 2)
        p.drawPixmap(cap.topLeft(), edge_layer(int(round(cap.width())), int(round(cap.height())),
                                               RADIAL_CAP_H / 2, True, dpr))
        prog = float(getattr(widget, "radial_prog", 0.0) or 0.0)
        if prog > 0.01 and idx >= 0:
            p.setPen(QPen(QColor(255, 255, 255, 170), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            inset = 3.0
            p.drawArc(QRectF(cap.x() + inset, cap.y() + inset, cap.height() - 2 * inset, cap.height() - 2 * inset),
                      90 * 16, -int(prog * 360 * 16))
    p.setFont(font)
    for i, (sid, label) in enumerate(RADIAL_SECTORS):
        sx, sy = radial_sector_center(i, cx, cy)
        selected = (i == idx) and a > 0.5
        p.setPen(QColor("#FFFFFF") if selected else label_color("label", dark))
        p.drawText(QRectF(sx - RADIAL_CAP_W / 2, sy - RADIAL_CAP_H / 2, RADIAL_CAP_W, RADIAL_CAP_H),
                   Qt.AlignmentFlag.AlignCenter, label)
    if animating:
        QTimer.singleShot(16, widget.update)
