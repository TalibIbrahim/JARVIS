"""ui/island_parts/common.py - shared constants and helpers for the island renderers.

Re-exported by ui.island so `from ui.island import IslandState, _SIZE, ...` keeps working.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from enum import auto, Enum
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)

from ui.components import continuous_path_cached, quantize_half
from ui.tokens import ISLAND, qfont

# ──────────────────────────────────────────────────────────────────────────────
# Continuous-corner path cache (keys quantized to 0.5 px so spring frames still hit)
# ──────────────────────────────────────────────────────────────────────────────

_PATH_CACHE: dict[tuple, QPainterPath] = {}


def create_cached_squircle_path(rect: QRectF, radius: float,
                                smoothing: float = ISLAND.corner_smoothing) -> QPainterPath:
    """Cached QPainterPath with a continuous (squircle) corner for `rect`.

    Keyed by quantized (x, y, w, h, r, smoothing); the returned object is shared, treat it as read-only.
    """
    key = (quantize_half(rect.x()), quantize_half(rect.y()), quantize_half(rect.width()),
           quantize_half(rect.height()), quantize_half(radius), int(round(smoothing * 100.0)))
    path = _PATH_CACHE.get(key)
    if path is None:
        if len(_PATH_CACHE) > 512:
            _PATH_CACHE.clear()
        base = continuous_path_cached(rect.width(), rect.height(), radius, smoothing)
        x, y = key[0] / 2.0, key[1] / 2.0
        path = base if (x == 0.0 and y == 0.0) else base.translated(x, y)
        _PATH_CACHE[key] = path
    return path


# ──────────────────────────────────────────────────────────────────────────────
# Colour helpers (tint maths, contrast)
# ──────────────────────────────────────────────────────────────────────────────

def lerp_color(a: QColor, b: QColor, t: float, alpha: Optional[int] = None) -> QColor:
    t = min(1.0, max(0.0, t))
    return QColor(
        int(round(a.red() + (b.red() - a.red()) * t)),
        int(round(a.green() + (b.green() - a.green()) * t)),
        int(round(a.blue() + (b.blue() - a.blue()) * t)),
        a.alpha() if alpha is None else alpha,
    )


def with_alpha(c: QColor, alpha: float) -> QColor:
    out = QColor(c)
    out.setAlpha(int(round(min(1.0, max(0.0, alpha)) * 255)))
    return out


def lighten(c: QColor, amount: float) -> QColor:
    """Blend toward white by `amount` (0..1)."""
    return lerp_color(c, QColor(255, 255, 255), amount, alpha=c.alpha())


def _lin(v: int) -> float:
    x = v / 255.0
    return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4


def luminance(c: QColor) -> float:
    return 0.2126 * _lin(c.red()) + 0.7152 * _lin(c.green()) + 0.0722 * _lin(c.blue())


def contrast_ratio(fg: QColor, bg: QColor) -> float:
    l1, l2 = luminance(fg), luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def ensure_contrast(fg: QColor, bg: QColor, minimum: float = ISLAND.tint.min_contrast) -> QColor:
    """Lighten `fg` toward white until it reaches `minimum`:1 on `bg` (opaque colours)."""
    if contrast_ratio(fg, bg) >= minimum:
        return fg
    lo, hi = 0.0, 1.0
    for _ in range(12):
        mid = (lo + hi) / 2.0
        if contrast_ratio(lighten(fg, mid), bg) >= minimum:
            hi = mid
        else:
            lo = mid
    return lighten(fg, hi)


# ──────────────────────────────────────────────────────────────────────────────
# Shell (surface gradient + hairline + optional single soft shadow), pre-rendered per size / dpr
# ──────────────────────────────────────────────────────────────────────────────

_SHELL_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_SHELL_CACHE_MAX = 10


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def paint_soft_shadow(p: QPainter, rect: QRectF, radius: float, spec, smoothing: float, scale: float = 1.0) -> None:
    """Single gaussian-like shadow under `rect`: stacked grown paths whose composite alpha follows
    peak * (1 - cdf(d / sigma)). Used only while pre-rendering the shell pixmap (never per frame)."""
    sigma = spec.sigma * scale
    n = 16
    lo, hi = -1.5 * sigma, 3.0 * sigma           # signed distance outside the edge
    prev = 0.0
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(n + 1):
        d = hi - (hi - lo) * i / n
        target = spec.alpha * (1.0 - _norm_cdf(d / sigma))
        if target <= prev + 1e-4:
            continue
        a = 1.0 - (1.0 - target) / (1.0 - prev)
        prev = target
        gx, gy = rect.x() - d, rect.y() + spec.dy * scale - d
        gw, gh = rect.width() + 2 * d, rect.height() + 2 * d
        path = continuous_path_cached(gw, gh, max(0.0, radius + d), ISLAND.corner_smoothing if smoothing is None else smoothing).translated(gx, gy)
        p.fillPath(path, QColor(0, 0, 0, int(round(min(1.0, max(0.0, a)) * 255))))


def build_shell_pixmap(w: int, h: int, dpr: float, radius: float, kind: str = "card",
                       notch: bool = False, tint_stops=None, tint_glow: Optional[QColor] = None) -> QPixmap:
    """Pre-render the island surface (no shadow): vertical gradient, tint, 1 px hairline, at island size w x h.

    notch=True extends the card 16 px above the widget so the top corners hide behind the bezel.
    """
    sm = ISLAND.corner_smoothing
    pix = QPixmap(max(1, int(math.ceil(w * dpr))), max(1, int(math.ceil(h * dpr))))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    top = -16.0 if notch else 0.0
    card = QRectF(0.0, top, float(w), float(h) - top)
    path = continuous_path_cached(card.width(), card.height(), radius, sm).translated(card.x(), card.y())
    if tint_stops:
        grad = QLinearGradient(0.0, card.top(), 0.0, card.bottom())
        for pos, col in tint_stops:
            grad.setColorAt(pos, col)
        p.fillPath(path, QBrush(grad))
        if tint_glow is not None:
            p.save()
            p.setClipPath(path)
            p.translate(card.center().x(), card.top())
            p.scale(1.0, ISLAND.tint.glow_ry)
            glow = QRadialGradient(0.0, 0.0, card.width() * 0.5)
            glow.setColorAt(0.0, tint_glow)
            glow.setColorAt(1.0, QColor(tint_glow.red(), tint_glow.green(), tint_glow.blue(), 0))
            p.fillRect(QRectF(-card.width(), -4.0, card.width() * 2.0, card.width()), QBrush(glow))
            p.restore()
    else:
        grad = QLinearGradient(0.0, card.top(), 0.0, card.bottom())
        grad.setColorAt(0.0, ISLAND.color.surface_top)
        grad.setColorAt(1.0, ISLAND.color.surface_bottom)
        p.fillPath(path, QBrush(grad))
    inset = card.adjusted(0.5, 0.5, -0.5, -0.5)
    hair = continuous_path_cached(inset.width(), inset.height(), max(0.0, radius - 0.5), sm).translated(inset.x(), inset.y())
    p.strokePath(hair, QPen(ISLAND.color.hairline, 1.0))
    p.end()
    return pix


def get_shell_pixmap(w: int, h: int, dpr: float, radius: float, kind: str, notch: bool,
                     tint_key=None, tint_stops=None, tint_glow=None, scale: float = 1.0) -> QPixmap:
    """LRU-cached shell pixmap keyed by (w, h, dpr, r, kind, notch, tint). Origin = island top-left
    (for notch the pixmap holds only the visible part, from y=0)."""
    key = (w, h, round(dpr, 3), quantize_half(radius), kind, notch, tint_key)
    pix = _SHELL_CACHE.get(key)
    if pix is not None:
        _SHELL_CACHE.move_to_end(key)
        return pix
    pix = build_shell_pixmap(w, h, dpr, radius, kind, notch, tint_stops, tint_glow)
    _SHELL_CACHE[key] = pix
    while len(_SHELL_CACHE) > _SHELL_CACHE_MAX:
        _SHELL_CACHE.popitem(last=False)
    return pix


# ──────────────────────────────────────────────────────────────────────────────
# Outside shadow: pre-rendered once per (radius, kind, dpr, scale) and drawn as a 9-slice so morph frames
# (a new size every frame) never re-blur. An axis too short to slice is rendered at its exact (0.5 px) size.
# ──────────────────────────────────────────────────────────────────────────────

_SHADOW_CACHE: "OrderedDict[tuple, QPixmap]" = OrderedDict()
_SHADOW_CACHE_MAX = 48
shadow_stats = {"builds": 0}


def shell_pad_for(scale: float) -> int:
    """Whole-pixel transparent window margin for a given size scale."""
    return int(math.ceil(float(ISLAND.shell_pad) * max(0.1, float(scale)) - 1e-6))


def _build_shadow_pixmap(bw: float, bh: float, dpr: float, radius: float, spec, scale: float, pad: int) -> QPixmap:
    sm = ISLAND.corner_smoothing
    pix = QPixmap(max(1, int(math.ceil((bw + 2 * pad) * dpr))), max(1, int(math.ceil((bh + 2 * pad) * dpr))))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint_soft_shadow(p, QRectF(pad, pad, bw, bh), radius, spec, sm, scale)
    p.end()
    shadow_stats["builds"] += 1
    return pix


def paint_island_shadow(p: QPainter, rect: QRectF, radius: float, kind: str, dpr: float, scale: float, pad: int) -> None:
    """Draw the outside shadow of the island card `rect` (island-local coords, may extend into the margin)."""
    spec = ISLAND.shadow.pill if kind == "pill" else ISLAND.shadow.card
    sm = ISLAND.corner_smoothing
    sigma = spec.sigma * scale
    hi = 3.0 * sigma
    dy = spec.dy * scale
    cx_in = int(math.ceil(radius * (1.0 + sm) + hi * sm)) + 3
    cy_in = cx_in + int(math.ceil(abs(dy)))
    W, H = rect.width(), rect.height()
    sl_x = W >= 2 * cx_in + 2
    sl_y = H >= 2 * cy_in + 2
    bw = float(2 * cx_in + 2) if sl_x else quantize_half(W) / 2.0
    bh = float(2 * cy_in + 2) if sl_y else quantize_half(H) / 2.0
    key = (round(bw, 1), round(bh, 1), sl_x, sl_y, round(dpr, 3), quantize_half(radius), kind, round(scale, 3), pad)
    pix = _SHADOW_CACHE.get(key)
    if pix is None:
        pix = _build_shadow_pixmap(bw, bh, dpr, radius, spec, scale, pad)
        _SHADOW_CACHE[key] = pix
        while len(_SHADOW_CACHE) > _SHADOW_CACHE_MAX:
            _SHADOW_CACHE.popitem(last=False)
    else:
        _SHADOW_CACHE.move_to_end(key)

    def cols(sl, n_in, dim, base, o):
        """[(src_start, src_end, dst_start, dst_end)] in logical px along one axis."""
        if not sl:
            return [(0.0, base + 2 * pad, o - pad, o + dim + pad)]
        a, b = pad + n_in, pad + base - n_in
        return [(0.0, a, o - pad, o + n_in),
                (a, b, o + n_in, o + dim - n_in),
                (b, base + 2 * pad, o + dim - n_in, o + dim + pad)]

    xs = cols(sl_x, cx_in, W, bw, rect.x())
    ys = cols(sl_y, cy_in, H, bh, rect.y())
    for iy, (sy0, sy1, dy0, dy1) in enumerate(ys):
        for ix, (sx0, sx1, dx0, dx1) in enumerate(xs):
            if sl_x and sl_y and ix == 1 and iy == 1:
                continue             # centre sits under the opaque card
            p.drawPixmap(QRectF(dx0, dy0, dx1 - dx0, dy1 - dy0), pix,
                         QRectF(sx0 * dpr, sy0 * dpr, (sx1 - sx0) * dpr, (sy1 - sy0) * dpr))


# ──────────────────────────────────────────────────────────────────────────────
# State enum
# ──────────────────────────────────────────────────────────────────────────────

class IslandState(Enum):
    IDLE          = auto()
    WAKE          = auto()
    TRACKING      = auto()
    GESTURE       = auto()
    LISTENING     = auto()
    THINKING      = auto()
    PROMPT        = auto()
    SPEAKING      = auto()
    RESULT        = auto()
    NO_SPEECH     = auto()
    ERROR         = auto()
    # Expanded Cards
    CARD_MEDIA          = auto()
    CARD_WEATHER        = auto()
    CARD_TIME           = auto()
    CARD_HARDWARE_GRID  = auto()
    CARD_HARDWARE_STRIP = auto()
    CARD_JARVIS         = auto()
    CARD_DISAMBIGUATE   = auto()
    CARD_TIMER          = auto()
    CARD_SENDING        = auto()
    CARD_RESULT         = auto()
    # Gesture HUD States
    HUD_VOLUME          = auto()
    HUD_WINDOW          = auto()


# ──────────────────────────────────────────────────────────────────────────────
# Target sizes per state (w, h)
# ──────────────────────────────────────────────────────────────────────────────

_SIZE: dict[IslandState, tuple[float, float]] = {
    IslandState.IDLE:                (175.0,  36.0),
    IslandState.WAKE:                (280.0,  44.0),
    IslandState.TRACKING:            (210.0,  36.0),
    IslandState.GESTURE:             (270.0,  40.0),
    IslandState.LISTENING:           (390.0,  54.0),
    IslandState.THINKING:            (320.0,  46.0),
    IslandState.PROMPT:              (360.0,  48.0),   # dynamically widened if needed
    IslandState.SPEAKING:            (380.0,  50.0),   # dynamically widened if needed
    IslandState.RESULT:              (380.0,  58.0),   # dynamically widened if needed
    IslandState.NO_SPEECH:           (175.0,  36.0),
    IslandState.ERROR:               (340.0,  48.0),
    IslandState.CARD_MEDIA:          (390.0, 188.0),
    IslandState.CARD_WEATHER:        (390.0, 188.0),
    IslandState.CARD_TIME:           (390.0, 188.0),
    IslandState.CARD_HARDWARE_GRID:  (390.0, 188.0),
    IslandState.CARD_HARDWARE_STRIP: (446.0,  69.0),
    IslandState.CARD_JARVIS:         (390.0, 188.0),
    IslandState.CARD_DISAMBIGUATE:   (440.0,  90.0),
    IslandState.CARD_TIMER:          (380.0, 150.0),
    IslandState.CARD_SENDING:        (300.0,  44.0),
    IslandState.CARD_RESULT:         (380.0,  58.0),
    IslandState.HUD_VOLUME:          (260.0,  38.0),
    IslandState.HUD_WINDOW:          (290.0,  38.0),
}


# Apple-grade spring constants:
# response ~0.4s: omega = 2*pi/0.4 = 15.71 -> k = omega^2 = 246.74
# damping ~0.7:   c = 2 * 0.7 * omega = 21.99
SPRING_K_RESP04: float = 246.74
SPRING_C_DAMP07: float = 22.00


# ──────────────────────────────────────────────────────────────────────────────
# Page-content cache + text helpers (static layers are rendered once per content key / size / dpr and blitted)
# ──────────────────────────────────────────────────────────────────────────────

def cached_page(owner, name: str, key: tuple, w: float, h: float, dpr: float, scale: float, paint) -> QPixmap:
    """Pixmap of a page's content, painted by `paint(painter)` in 1x logical coords, cached on `owner` under `name`.

    The cache holds one entry per page name and is rebuilt only when `key` (content + size + dpr) changes,
    so steady-state paints are a single drawPixmap."""
    store = owner.__dict__.setdefault("_page_pix", {})
    full = (key, round(dpr, 3), round(scale, 3), round(w, 1), round(h, 1))
    ent = store.get(name)
    if ent is not None and ent[0] == full:
        return ent[1]
    pix = QPixmap(max(1, int(round(w * scale * dpr))), max(1, int(round(h * scale * dpr))))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.GlobalColor.transparent)
    cp = QPainter(pix)
    cp.setRenderHint(QPainter.RenderHint.Antialiasing)
    cp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    cp.scale(scale, scale)
    paint(cp)
    cp.end()
    store[name] = (full, pix)
    return pix


def draw_text(p: QPainter, text: str, x: float, baseline: float, token, color: QColor,
              max_w: Optional[float] = None, align: str = "left") -> float:
    """Draw `text` at a baseline with a type token; elide to `max_w`; align left / center (x = centre) / right
    (x = right edge). Returns the drawn advance."""
    font = qfont(token)
    fm = QFontMetricsF(font)
    if max_w is not None:
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max_w)
    adv = fm.horizontalAdvance(text)
    if align == "center":
        x -= adv / 2.0
    elif align == "right":
        x -= adv
    p.setFont(font)
    p.setPen(color)
    p.drawText(QPointF(x, baseline), text)
    return adv


def fill_round(p: QPainter, rect: QRectF, radius: float, color: QColor) -> None:
    """Continuous-corner filled rounded tile (no stroke)."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPath(create_cached_squircle_path(rect, radius))


def draw_bar(p: QPainter, x: float, y: float, w: float, h: float, ratio: float, fill: QColor,
             track: QColor = ISLAND.color.track) -> None:
    """Round-capped progress bar: track then fill (`ratio` 0..1)."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(track)
    p.drawRoundedRect(QRectF(x, y, w, h), h / 2.0, h / 2.0)
    fw = w * min(1.0, max(0.0, ratio))
    if fw > 0.0:
        fw = max(fw, h)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(x, y, fw, h), h / 2.0, h / 2.0)


# ──────────────────────────────────────────────────────────────────────────────
# Own vector icon set (Lucide / Phosphor style: 16 x 16 grid, round caps and joins, ~1.5 stroke)
# ──────────────────────────────────────────────────────────────────────────────

def _ln(path: QPainterPath, x0: float, y0: float, x1: float, y1: float) -> None:
    path.moveTo(x0, y0)
    path.lineTo(x1, y1)


def _icon_path(name: str) -> tuple:
    """(stroke path, filled path) on the 16 x 16 grid for icon `name`."""
    s, f = QPainterPath(), QPainterPath()
    if name == "cpu":
        s.addRoundedRect(QRectF(3.4, 3.4, 9.2, 9.2), 2.2, 2.2)
        s.addRoundedRect(QRectF(6.1, 6.1, 3.8, 3.8), 0.9, 0.9)
        for k in (6.2, 9.8):
            _ln(s, k, 1.3, k, 3.4)
            _ln(s, k, 12.6, k, 14.7)
            _ln(s, 1.3, k, 3.4, k)
            _ln(s, 12.6, k, 14.7, k)
    elif name == "ram":
        s.addRoundedRect(QRectF(1.4, 3.6, 13.2, 7.4), 1.6, 1.6)
        for k in (4.6, 7.0, 9.4, 11.8):
            _ln(s, k, 11.0, k, 13.0)
        for k in (4.6, 8.0, 11.4):
            _ln(s, k, 6.0, k, 8.6)
    elif name == "gpu":
        s.addRoundedRect(QRectF(1.3, 3.8, 13.4, 8.2), 1.8, 1.8)
        s.addEllipse(QPointF(5.7, 7.9), 2.2, 2.2)
        _ln(s, 9.6, 6.7, 12.4, 6.7)
        _ln(s, 9.6, 9.1, 12.4, 9.1)
        _ln(s, 3.2, 12.0, 3.2, 13.7)
    elif name == "disk":
        s.addEllipse(QPointF(8.0, 4.4), 5.3, 2.2)
        s.moveTo(2.7, 4.4)
        s.lineTo(2.7, 11.6)
        s.moveTo(13.3, 4.4)
        s.lineTo(13.3, 11.6)
        s.moveTo(2.7, 8.0)
        s.arcTo(QRectF(2.7, 5.9, 10.6, 4.2), 180.0, 180.0)
        s.moveTo(2.7, 11.6)
        s.arcTo(QRectF(2.7, 9.5, 10.6, 4.2), 180.0, 180.0)
    elif name == "net_up":
        _ln(s, 8.0, 13.2, 8.0, 3.2)
        s.moveTo(4.4, 6.7)
        s.lineTo(8.0, 3.2)
        s.lineTo(11.6, 6.7)
    elif name == "net_down":
        _ln(s, 8.0, 2.8, 8.0, 12.8)
        s.moveTo(4.4, 9.3)
        s.lineTo(8.0, 12.8)
        s.lineTo(11.6, 9.3)
    elif name == "gauge":
        s.addEllipse(QPointF(8.0, 8.0), 6.3, 6.3)
        _ln(s, 8.0, 8.0, 10.9, 5.1)
        for ang in (200.0, 250.0, 290.0, 340.0):
            a = math.radians(ang)
            _ln(s, 8.0 + math.cos(a) * 4.5, 8.0 + math.sin(a) * 4.5, 8.0 + math.cos(a) * 5.2, 8.0 + math.sin(a) * 5.2)
        f.addEllipse(QPointF(8.0, 8.0), 1.0, 1.0)
    elif name == "wind":
        _ln(s, 1.6, 4.6, 14.4, 4.6)
        _ln(s, 1.6, 8.0, 11.0, 8.0)
        _ln(s, 1.6, 11.4, 7.6, 11.4)
    elif name == "thermo":
        f.addRoundedRect(QRectF(6.5, 1.5, 3.0, 8.8), 1.5, 1.5)
        f.addEllipse(QPointF(8.0, 11.7), 2.9, 2.9)
    elif name == "drop":
        f.moveTo(8.0, 1.4)
        f.cubicTo(8.0, 1.4, 3.3, 6.1, 3.3, 9.7)
        f.arcTo(QRectF(3.3, 5.3, 9.4, 9.4), 180.0, 180.0)
        f.cubicTo(12.7, 6.1, 8.0, 1.4, 8.0, 1.4)
        f.closeSubpath()
    elif name == "check":
        s.moveTo(3.2, 8.6)
        s.lineTo(6.6, 12.0)
        s.lineTo(12.8, 4.4)
    elif name == "cross":
        _ln(s, 4.0, 4.0, 12.0, 12.0)
        _ln(s, 12.0, 4.0, 4.0, 12.0)
    elif name == "pause":
        f.addRoundedRect(QRectF(3.6, 2.8, 3.1, 10.4), 1.2, 1.2)
        f.addRoundedRect(QRectF(9.3, 2.8, 3.1, 10.4), 1.2, 1.2)
    elif name == "play":
        f.moveTo(4.6, 3.2)
        f.lineTo(12.4, 8.0)
        f.lineTo(4.6, 12.8)
        f.closeSubpath()
    elif name == "command":
        s.moveTo(3.2, 4.4)
        s.lineTo(7.4, 8.0)
        s.lineTo(3.2, 11.6)
        _ln(s, 8.8, 12.0, 13.0, 12.0)
    elif name == "camera":
        s.addRoundedRect(QRectF(1.5, 4.6, 13.0, 8.6), 2.0, 2.0)
        s.addEllipse(QPointF(8.0, 8.9), 2.4, 2.4)
        s.moveTo(5.4, 4.6)
        s.lineTo(6.5, 2.8)
        s.lineTo(9.5, 2.8)
        s.lineTo(10.6, 4.6)
    elif name == "arrow_up":
        # wind direction arrow (north = up; the caller rotates it)
        _ln(s, 8.0, 14.0, 8.0, 2.4)
        s.moveTo(3.6, 6.6)
        s.lineTo(8.0, 2.4)
        s.lineTo(12.4, 6.6)
    return s, f


_ICON_PATHS: dict = {}


def paint_icon(p: QPainter, name: str, cx: float, cy: float, size: float, color: QColor,
               stroke: float = 1.5, rotate_deg: float = 0.0) -> None:
    """Draw icon `name` centred on (cx, cy) in a `size` x `size` box. Used while rendering cached page layers."""
    paths = _ICON_PATHS.get(name)
    if paths is None:
        paths = _ICON_PATHS[name] = _icon_path(name)
    s, f = paths
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(cx, cy)
    if rotate_deg:
        p.rotate(rotate_deg)
    k = size / 16.0
    p.scale(k, k)
    p.translate(-8.0, -8.0)
    if not f.isEmpty():
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(f)
    if not s.isEmpty():
        p.setPen(QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(s)
    p.restore()
