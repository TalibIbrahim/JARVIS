"""
ui/components.py — J.A.R.V.I.S. Apple-HIG Reusable Component Library

Components:
  - Spring                 (critically-damped spring, numerical stability)
  - create_squircle_path   (G² continuous superellipse)
  - draw_soft_shadow       (two-layer QPainter shadow)
  - AppleSwitch            (51×31 iOS switch, spring thumb, all states)
  - SegmentedControl       (sliding pill thumb, light+dark)
  - HSlider                (28 px thumb, live value label, focus ring)
  - FocusRing              (3 px accent ring at 40%)
  - GroupedCard            (squircle card with inset rows + footnote)
  - SidebarItem            (icon tile + label row)
  - SidebarSearchField     (search bar)
  - PrimaryButton / PlainButton / TintButton / DestructiveButton
  - StatusPill             (small rounded status badge)
  - MicLevelMeter          (real-time audio level bar)
  - GlassLabel             (QLabel that gets a contrast plate when sitting loose on glass)
  - PageOrderList          (reorderable, switchable list with up/down capsule buttons)

Liquid Glass: cards, switches, segmented controls, sliders and buttons paint through ui/glass.py
(cached tint/plate, specular + glow + hairline edge layer, cached soft shadows). Controls are capsules;
inner radii are concentric (outer radius - padding). State changes are spring morphs (Spring).

All widgets are theme-aware at construction time and recheck dark mode
on each paintEvent so live system-theme changes are reflected without restart.
"""

from __future__ import annotations

import math
import time
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import (
    Qt, QTimer, QRectF, QPointF, QSize, Signal, Property, QPropertyAnimation,
    QEasingCurve, QObject
)
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QPainterPath, QLinearGradient,
    QRadialGradient, QPixmap, QIcon, QFontMetrics, QKeyEvent, QPalette,
    QFocusEvent, QMouseEvent, QPaintEvent, QResizeEvent
)
from PySide6.QtWidgets import (
    QWidget, QLabel, QHBoxLayout, QVBoxLayout, QFrame,
    QSizePolicy, QAbstractSlider, QSlider, QLineEdit,
    QPushButton, QScrollArea, QProgressBar, QComboBox
)

from ui.tokens import (
    T, COLOR, TYPE, SPRING, RADIUS, SPACING, SHADOW, SIZES,
    SpringToken, SPRING_DT_MIN, SPRING_DT_MAX, REDUCE_MOTION_MS,
    squircle_bezier_factor, qfont
)
from ui import glass as _glass          # glass imports components lazily (inside functions) -> no cycle


# ──────────────────────────────────────────────────────────────────────────────
# 1. Spring physics engine (standalone, no Qt)
# ──────────────────────────────────────────────────────────────────────────────

class Spring:
    """
    Critically-damped harmonic spring with sub-step numerical stability.

    F = −k(x − target) − c·v
    Uses adaptive sub-stepping (capped at dt/16ms rounds) to remain stable
    under frame stalls up to 50 ms.
    Clamp is set very wide (0.0 .. 8000.0) so sub-pixel values are allowed.
    """
    __slots__ = ("value", "target", "velocity", "k", "c")

    def __init__(
        self,
        value:     float = 0.0,
        stiffness: float = SPRING.standard.stiffness,
        damping:   float = SPRING.standard.damping,
    ):
        self.value    = float(value)
        self.target   = float(value)
        self.velocity = 0.0
        self.k        = float(stiffness)
        self.c        = float(damping)

    def from_preset(self, preset: SpringToken) -> "Spring":
        self.k = preset.stiffness
        self.c = preset.damping
        return self

    def set_target(self, target: float) -> None:
        self.target = float(target)

    def snap(self, value: float) -> None:
        """Immediately snap with no animation."""
        self.value    = float(value)
        self.target   = float(value)
        self.velocity = 0.0

    def update(self, dt: float, reduce_motion: bool = False) -> float:
        if reduce_motion:
            self.value    = self.target
            self.velocity = 0.0
            return self.value

        dt = min(max(float(dt), SPRING_DT_MIN), SPRING_DT_MAX)
        sub_steps = max(1, int(dt / 0.016) + 1)
        sub_dt = dt / sub_steps

        for _ in range(sub_steps):
            force = -self.k * (self.value - self.target) - self.c * self.velocity
            self.velocity += force * sub_dt
            self.value    += self.velocity * sub_dt

        # Wide clamp — allows sub-pixel values for opacity/scale springs
        self.value = min(max(self.value, 0.0), 8000.0)
        return self.value

    @property
    def settled(self) -> bool:
        return abs(self.value - self.target) < 0.3 and abs(self.velocity) < 0.5

    @property
    def vel(self) -> float:
        return self.velocity

    @vel.setter
    def vel(self, v: float) -> None:
        self.velocity = float(v)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Geometry helpers
# ──────────────────────────────────────────────────────────────────────────────

_SQUIRCLE_CACHE: dict[tuple[float, float, float], QPainterPath] = {}
_MAX_SQUIRCLE_CACHE = 512
_CONTINUOUS_CACHE: dict[tuple[int, int, int, int], QPainterPath] = {}


def quantize_half(v: float) -> int:
    """Quantize a length to 0.5 px steps (returns the integer number of half pixels)."""
    return int(round(float(v) * 2.0))


def _corner_params(r: float, smoothing: float, budget: float):
    """Figma-style corner smoothing parameters for ONE corner (all lengths in px).

    Returns (p, a, b, c, d, arc_len, arc_deg, beta_deg): the corner occupies p px along each edge;
    two cubic runs (a, b, c, d) flank a circular arc of radius r spanning arc_deg degrees.
    """
    s = max(0.0, min(1.0, smoothing))
    p = (1.0 + s) * r
    if p > budget:                       # not enough edge: shrink smoothing (-> circular / stadium)
        s = max(0.0, budget / r - 1.0)
        p = min(p, budget)
    arc_deg = 90.0 * (1.0 - s)
    arc_len = math.sin(math.radians(arc_deg / 2.0)) * r * math.sqrt(2.0)
    alpha = (90.0 - arc_deg) / 2.0
    p3p4 = r * math.tan(math.radians(alpha / 2.0))
    beta = 45.0 * s
    c = p3p4 * math.cos(math.radians(beta))
    d = c * math.tan(math.radians(beta))
    b = (p - arc_len - c - d) / 3.0
    a = 2.0 * b
    return p, a, b, c, d, arc_len, arc_deg, beta


def build_continuous_path(w: float, h: float, radius: float, smoothing: float = 0.6) -> QPainterPath:
    """Continuous-curvature (squircle) rounded rect at the origin, w x h.

    Apple/Figma corner smoothing: each corner is two cubic Beziers that ease the curvature in and out
    around a circular arc of radius `radius`, occupying (1 + smoothing) * radius along each edge.
    When radius >= min(w, h) / 2 the smoothing budget is zero and the result is a plain stadium.
    """
    path = QPainterPath()
    r = max(0.0, min(float(radius), w / 2.0, h / 2.0))
    if r <= 0.0:
        path.addRect(0.0, 0.0, w, h)
        return path
    budget = min(w, h) / 2.0
    p, a, b, c, d, arc_len, arc_deg, beta = _corner_params(r, smoothing, budget)
    th = math.radians(arc_deg)
    k = (4.0 / 3.0) * math.tan(th / 4.0) * r          # cubic handle length for the arc
    cb, sb = math.cos(math.radians(beta)), math.sin(math.radians(beta))

    def corner(vx: float, vy: float, dx: float, dy: float, nx: float, ny: float) -> None:
        sx, sy = vx - p * dx, vy - p * dy

        def pt(u: float, v: float) -> QPointF:
            return QPointF(sx + u * dx + v * nx, sy + u * dy + v * ny)

        ax, ay = a + b + c, d                          # arc start (local frame)
        bx, by = ax + arc_len, ay + arc_len            # arc end
        path.cubicTo(pt(a, 0.0), pt(a + b, 0.0), pt(ax, ay))
        path.cubicTo(pt(ax + k * cb, ay + k * sb), pt(bx - k * sb, by - k * cb), pt(bx, by))
        path.cubicTo(pt(bx + d, by + c), pt(bx + d, by + b + c), pt(bx + d, by + a + b + c))

    path.moveTo(p, 0.0)
    path.lineTo(w - p, 0.0)
    corner(w, 0.0, 1.0, 0.0, 0.0, 1.0)                 # top-right
    path.lineTo(w, h - p)
    corner(w, h, 0.0, 1.0, -1.0, 0.0)                  # bottom-right
    path.lineTo(p, h)
    corner(0.0, h, -1.0, 0.0, 0.0, -1.0)               # bottom-left
    path.lineTo(0.0, p)
    corner(0.0, 0.0, 0.0, -1.0, 1.0, 0.0)              # top-left
    path.closeSubpath()
    return path


def continuous_path_cached(w: float, h: float, radius: float, smoothing: float = 0.6) -> QPainterPath:
    """Cached origin-anchored continuous path, keyed by (w, h, r) quantized to 0.5 px and smoothing to 0.01."""
    key = (quantize_half(w), quantize_half(h), quantize_half(radius), int(round(smoothing * 100.0)))
    path = _CONTINUOUS_CACHE.get(key)
    if path is None:
        if len(_CONTINUOUS_CACHE) >= _MAX_SQUIRCLE_CACHE:
            _CONTINUOUS_CACHE.clear()
        path = build_continuous_path(key[0] / 2.0, key[1] / 2.0, key[2] / 2.0, key[3] / 100.0)
        _CONTINUOUS_CACHE[key] = path
    return path


def create_squircle_path(rect: QRectF, radius: float, smoothing: float = 0.0) -> QPainterPath:
    """
    Rounded-rect path for `rect`. smoothing == 0 keeps the legacy circular-arc corner (bezier factor 0.552284);
    smoothing > 0 builds a true continuous (squircle) corner, e.g. 0.6 = Figma/Apple "corner smoothing".
    Results are cached by (rect.width(), rect.height(), radius[, smoothing]).
    """
    w = rect.width()
    h = rect.height()
    if smoothing > 0.0:
        base_path = continuous_path_cached(w, h, radius, smoothing)
        if rect.x() == 0.0 and rect.y() == 0.0:
            return QPainterPath(base_path)
        return base_path.translated(rect.x(), rect.y())
    key = (w, h, float(radius))
    base_path = _SQUIRCLE_CACHE.get(key)
    if base_path is None:
        if len(_SQUIRCLE_CACHE) >= _MAX_SQUIRCLE_CACHE:
            _SQUIRCLE_CACHE.clear()
        base_path = QPainterPath()
        r = min(radius, w / 2.0, h / 2.0)
        c = squircle_bezier_factor(r)

        base_path.moveTo(r, 0.0)
        base_path.lineTo(w - r, 0.0)
        base_path.cubicTo(w - r + c, 0.0, w, r - c, w, r)
        base_path.lineTo(w, h - r)
        base_path.cubicTo(w, h - r + c, w - r + c, h, w - r, h)
        base_path.lineTo(r, h)
        base_path.cubicTo(r - c, h, 0.0, h - r + c, 0.0, h - r)
        base_path.lineTo(0.0, r)
        base_path.cubicTo(0.0, r - c, r - c, 0.0, r, 0.0)
        base_path.closeSubpath()
        _SQUIRCLE_CACHE[key] = base_path

    x = rect.x()
    y = rect.y()
    if x == 0.0 and y == 0.0:
        return QPainterPath(base_path)
    path = QPainterPath(base_path)
    path.translate(x, y)
    return path


def draw_soft_shadow(
    painter: QPainter,
    path: QPainterPath,
    layers: tuple,          # tuple of ShadowLayer namedtuples
    offset_x: float = 0.0,
) -> None:
    """
    Draw two stacked soft shadow layers beneath a squircle path.
    Simulates blur by drawing progressively larger paths at lower alpha.
    Each layer is rendered with 3 expanding rings to approximate Gaussian blur.
    """
    for layer in layers:
        for spread in range(3):
            frac  = (spread + 1) / 3.0
            alpha = int(layer.alpha * (1.0 - frac * 0.6))
            grow  = layer.blur * frac * 0.35
            offset_rect = QRectF(
                path.boundingRect().adjusted(-grow, -grow, grow, grow)
            )
            # Shift by shadow offset
            shadow_path = QPainterPath(path)
            shadow_path.translate(offset_x, layer.offset_y + grow * 0.5)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(0, 0, 0, alpha)))
            painter.drawPath(shadow_path)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Focus ring mixin
# ──────────────────────────────────────────────────────────────────────────────

class FocusRingMixin:
    """
    Mixin for QWidgets that draws a 3 px accent-colored focus ring at 40% alpha
    whenever the widget has keyboard focus.
    Must be mixed before QWidget in MRO: class Foo(FocusRingMixin, QWidget).
    """
    _has_focus: bool = False

    def focusInEvent(self, event: QFocusEvent) -> None:
        self._has_focus = True
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self._has_focus = False
        self.update()
        super().focusOutEvent(event)

    def _draw_focus_ring(self, painter: QPainter, rect: QRectF, radius: float) -> None:
        if not self._has_focus:
            return
        dark = T.is_dark()
        accent = T.color.accent.resolve(dark)
        ring_col = QColor(accent.red(), accent.green(), accent.blue(), SIZES.focus_ring_a)
        pen = QPen(ring_col, SIZES.focus_ring_w)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = SIZES.focus_ring_w / 2.0
        path = create_squircle_path(rect.adjusted(inset, inset, -inset, -inset), radius + inset)
        painter.drawPath(path)


# ──────────────────────────────────────────────────────────────────────────────
# 4. AppleSwitch — capsule toggle, glass thumb that stretches on press (spring)
# ──────────────────────────────────────────────────────────────────────────────

class _SpringTicker:
    """Shared 60 fps tick bookkeeping: real dt (clamped by Spring.update) and an on-demand timer."""

    def _init_ticker(self) -> None:
        self._tick_last: Optional[float] = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _wake(self) -> None:
        if not self._timer.isActive():
            self._tick_last = time.perf_counter()
            self._timer.start(16)

    def _dt(self) -> float:
        now = time.perf_counter()
        last = self._tick_last if self._tick_last is not None else now - 0.016
        self._tick_last = now
        return min(max(now - last, SPRING_DT_MIN), SPRING_DT_MAX)


class AppleSwitch(FocusRingMixin, _SpringTicker, QWidget):
    """
    51x31 px capsule toggle (iOS/macOS 26 style): glass thumb with soft shadow that elongates while pressed.
    The thumb x-position and the press-stretch are springs (SPRING.fast).
    """
    toggled = Signal(bool)

    W: int = SIZES.switch_w
    H: int = SIZES.switch_h
    THUMB: int = SIZES.switch_thumb
    STRETCH: float = 7.0

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(self.W, self.H)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._checked  = checked
        self._hovered  = False
        self._pressed  = False
        self._enabled  = True

        # Spring drives thumb x-position in normalised [0, 1]; second spring drives the press stretch
        self._spring   = Spring(1.0 if checked else 0.0)
        self._spring.k = SPRING.fast.stiffness
        self._spring.c = SPRING.fast.damping
        self._press    = Spring(0.0, SPRING.fast.stiffness, SPRING.fast.damping)
        self._thumb_t  = 1.0 if checked else 0.0
        self._init_ticker()

    # ── public interface ──────────────────────────────────────────────────

    def isChecked(self) -> bool:
        return self._checked

    @property
    def checked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool, *, silent: bool = False) -> None:
        if self._checked == checked:
            return
        self._checked = checked
        self._spring.set_target(1.0 if checked else 0.0)
        self._wake()
        if not silent:
            self.toggled.emit(checked)
        self.update()

    def setEnabled(self, enabled: bool) -> None:
        if self._enabled == enabled:
            return
        self._enabled = enabled
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ForbiddenCursor)
        self.update()

    # ── events ────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._enabled:
            self._pressed = True
            self._press.set_target(1.0)
            self._wake()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._enabled and self._pressed:
            self.setChecked(not self._checked)
        self._pressed = False
        self._press.set_target(0.0)
        self._wake()
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return):
            if self._enabled:
                self.setChecked(not self._checked)
        else:
            super().keyPressEvent(event)

    def enterEvent(self, event) -> None:
        if not self._hovered:
            self._hovered = True
            self.update()

    def leaveEvent(self, event) -> None:
        if self._hovered or self._pressed:
            self._hovered = False
            self._pressed = False
            self._press.set_target(0.0)
            self._wake()
            self.update()

    # ── animation ─────────────────────────────────────────────────────────

    def _tick(self) -> None:
        reduce = T.reduce_motion()
        dt = self._dt()
        new_t = self._spring.update(dt, reduce)
        self._press.update(dt, reduce)
        if abs(new_t - self._thumb_t) > 0.003 or not self._press.settled:
            self._thumb_t = new_t
            self.update()
        if self._spring.settled and self._press.settled:
            self._thumb_t = 1.0 if self._checked else 0.0
            self._press.snap(1.0 if self._pressed else 0.0)
            self._timer.stop()
            self.update()

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = T.is_dark()
        w, h = float(self.W), float(self.H)
        r    = h / 2.0
        t    = self._thumb_t          # 0.0 = off, 1.0 = on
        dis  = not self._enabled

        if dis:
            track_col = QColor(120, 120, 128, 70 if dark else 60)
        else:
            off_col = QColor(120, 120, 128, 102 if dark else 82)
            on_col  = QColor("#30D158") if dark else QColor("#34C759")
            track_col = QColor(
                int(off_col.red()   + (on_col.red()   - off_col.red())   * t),
                int(off_col.green() + (on_col.green() - off_col.green()) * t),
                int(off_col.blue()  + (on_col.blue()  - off_col.blue())  * t),
                int(off_col.alpha() + (255 - off_col.alpha()) * t),
            )
            if self._hovered and not self._pressed:
                track_col = track_col.lighter(108)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_col))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)
        # hairline so the off track stays defined on glass
        hl = QColor(255, 255, 255, 30) if dark else QColor(0, 0, 0, 26)
        p.setPen(QPen(hl, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), r - 0.5, r - 0.5)

        # Thumb: capsule, elongates with the press spring; concentric with the track (r - margin)
        margin   = 2.0
        pv       = max(0.0, min(1.4, self._press.value))
        tw       = self.THUMB + self.STRETCH * pv
        th       = float(self.THUMB)
        travel   = w - tw - margin * 2
        thumb_x  = margin + t * travel
        thumb_y  = margin
        thumb    = QRectF(thumb_x, thumb_y, tw, th)

        dpr = self.devicePixelRatioF()
        if not dis:
            _glass.paint_shadow(p, QRectF(thumb_x + (tw - th) / 2, thumb_y, th, th), th / 2,
                                blur=6.0, alpha=0.30, dy=1.5, dpr=dpr, exclude_shape=False)
        thumb_col = QColor(255, 255, 255, 255 if not dis else 190)
        p.setPen(QPen(QColor(0, 0, 0, 22), 0.8))
        p.setBrush(QBrush(thumb_col))
        p.drawRoundedRect(thumb, th / 2.0, th / 2.0)

        self._draw_focus_ring(p, QRectF(0, 0, w, h), r)

    def sizeHint(self) -> QSize:
        return QSize(self.W, self.H)


# ──────────────────────────────────────────────────────────────────────────────
# 5. SegmentedControl — capsule glass track, sliding glass thumb (spring morph)
# ──────────────────────────────────────────────────────────────────────────────

class SegmentedControl(FocusRingMixin, _SpringTicker, QWidget):
    """
    Capsule segmented control: recessed glass track, raised glass thumb with soft shadow. The thumb slides
    on a spring and stretches with its velocity; pressing squeezes it (second spring).
    Options: list of (id: str, label: str).
    """
    valueChanged = Signal(str)

    PAD = 3.0          # track padding -> thumb radius is concentric: (h/2) - PAD

    def __init__(
        self,
        options:  List[Tuple[str, str]],
        current:  str = "",
        parent:   Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.options  = options
        self._current = current or (options[0][0] if options else "")
        self._hovered: Optional[int] = None

        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

        # Spring animates the thumb x-position (as fraction 0→1 per segment)
        self._idx = self._index_of(self._current)
        self._spring = Spring(float(self._idx))
        self._spring.k = SPRING.fast.stiffness
        self._spring.c = SPRING.fast.damping
        self._press = Spring(0.0, SPRING.fast.stiffness, SPRING.fast.damping)
        self._thumb_x  = float(self._idx)
        self._init_ticker()

    def _index_of(self, oid: str) -> int:
        for i, (o, _) in enumerate(self.options):
            if o == oid:
                return i
        return 0

    def currentValue(self) -> str:
        return self._current

    def setValue(self, oid: str, *, silent: bool = False) -> None:
        if self._current == oid:
            return
        self._current = oid
        idx = self._index_of(oid)
        self._idx = idx
        self._spring.set_target(float(idx))
        self._wake()
        if not silent:
            self.valueChanged.emit(oid)
        self.update()

    def _tick(self) -> None:
        reduce = T.reduce_motion()
        dt = self._dt()
        new_x = self._spring.update(dt, reduce)
        self._press.update(dt, reduce)
        self._thumb_x = new_x
        self.update()
        if self._spring.settled and self._press.settled:
            self._thumb_x = float(self._idx)
            self._timer.stop()
            self.update()

    def _seg_rect(self, idx: int) -> QRectF:
        n   = len(self.options)
        w   = float(self.width())
        seg = w / n
        return QRectF(idx * seg, 0, seg, float(self.height()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        n   = len(self.options)
        seg = self.width() / n
        idx = int(event.position().x() // seg)
        idx = max(0, min(idx, n - 1))
        oid = self.options[idx][0]
        self._press.set_target(1.0)
        self._wake()
        self.setValue(oid)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press.set_target(0.0)
        self._wake()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        n   = len(self.options)
        seg = self.width() / n
        new_hov = max(0, min(n - 1, int(event.position().x() // seg)))
        if self._hovered != new_hov:
            self._hovered = new_hov
            self.update()

    def leaveEvent(self, event) -> None:
        if self._hovered is not None:
            self._hovered = None
            self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        n   = len(self.options)
        idx = self._idx
        if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            idx = min(idx + 1, n - 1)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            idx = max(idx - 1, 0)
        else:
            super().keyPressEvent(event)
            return
        self.setValue(self.options[idx][0])

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark  = T.is_dark()
        w, h  = float(self.width()), float(self.height())
        n     = max(1, len(self.options))
        seg_w = w / n
        pad   = self.PAD
        dpr   = self.devicePixelRatioF()

        # Recessed glass track (capsule)
        track_r = h / 2.0
        track_col = QColor(255, 255, 255, 20) if dark else QColor(0, 0, 0, 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_col))
        p.drawRoundedRect(QRectF(0, 0, w, h), track_r, track_r)
        p.setPen(QPen(QColor(255, 255, 255, 26) if dark else QColor(0, 0, 0, 22), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), track_r - 0.5, track_r - 0.5)

        # Raised thumb: stretches with spring velocity, squeezes when pressed
        vel      = abs(self._spring.velocity) * seg_w * 0.035
        stretch  = min(12.0, vel)
        squeeze  = 2.0 * max(0.0, min(1.0, self._press.value))
        base_w   = seg_w - 2 * pad
        tw       = base_w + stretch - squeeze
        th       = h - 2 * pad
        tw = max(th, round(tw / 2.0) * 2.0)                       # quantised: bounded edge-layer cache
        cx       = (self._thumb_x + 0.5) * seg_w
        thumb    = QRectF(cx - tw / 2.0, pad, tw, th)
        thumb_r  = _glass.concentric(track_r, pad)

        _glass.paint_shadow(p, QRectF(cx - base_w / 2.0, pad, base_w, th), thumb_r, blur=8.0,
                            alpha=0.32 if dark else 0.20, dy=2.0, dpr=dpr, exclude_shape=False)
        thumb_col = QColor(255, 255, 255, 56) if dark else QColor(255, 255, 255, 255)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(thumb_col))
        p.drawRoundedRect(thumb, thumb_r, thumb_r)
        p.drawPixmap(thumb.topLeft(), _glass.edge_layer(int(round(tw)), int(round(th)), thumb_r, dark, dpr,
                                                        specular=True, glow=False))

        # Hover hint
        if self._hovered is not None and self._hovered != self._idx:
            hx = self._hovered * seg_w + pad
            hover_col = QColor(255, 255, 255, 15) if dark else QColor(0, 0, 0, 10)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hover_col))
            p.drawRoundedRect(QRectF(hx, pad, base_w, th), thumb_r, thumb_r)

        # Labels
        label_font = qfont(TYPE.footnote)
        for i, (oid, label) in enumerate(self.options):
            is_sel = (i == self._idx)
            label_font.setWeight(QFont.Weight.DemiBold if is_sel else QFont.Weight.Medium)
            p.setFont(label_font)
            p.setPen(_glass.label_color("label" if is_sel else "secondary_label", dark))
            p.drawText(QRectF(i * seg_w, 0, seg_w, h), Qt.AlignmentFlag.AlignCenter, label)

        self._draw_focus_ring(p, QRectF(0, 0, w, h), track_r)

    def _seg_width(self) -> int:
        """Equal segments, each wide enough for the longest label (+ capsule padding)."""
        f = qfont(TYPE.footnote)
        f.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(f)
        widest = max((fm.horizontalAdvance(lbl) for _, lbl in self.options), default=40)
        return max(64, widest + 28)

    def sizeHint(self) -> QSize:
        return QSize(len(self.options) * self._seg_width(), 30)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


# ──────────────────────────────────────────────────────────────────────────────
# 6. HSlider — capsule track, glass capsule thumb (grows on press), live value
# ──────────────────────────────────────────────────────────────────────────────

class HSlider(FocusRingMixin, _SpringTicker, QWidget):
    """
    Horizontal slider: capsule track, glass capsule thumb with soft shadow (grows on press via spring),
    focus ring. Emits valueChanged(float) on each change.
    """
    valueChanged = Signal(float)

    THUMB = SIZES.slider_thumb            # thumb height
    THUMB_W = SIZES.slider_thumb + 10     # capsule thumb width
    TRACK = 6

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value:   float,
        unit:    str   = "",
        parent:  Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._min  = minimum
        self._max  = maximum
        self._val  = max(minimum, min(maximum, value))
        self._unit = unit
        self._drag = False
        self._press = Spring(0.0, SPRING.fast.stiffness, SPRING.fast.damping)
        self._init_ticker()
        self.setFixedHeight(self.THUMB + 8)
        self.setMinimumWidth(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _tick(self) -> None:
        self._press.update(self._dt(), T.reduce_motion())
        self.update()
        if self._press.settled:
            self._press.snap(1.0 if self._drag else 0.0)
            self._timer.stop()

    @property
    def value(self) -> float:
        return self._val

    def setValue(self, v: float, *, silent: bool = False) -> None:
        v = max(self._min, min(self._max, v))
        if abs(v - self._val) < 1e-9:
            return
        self._val = v
        self.update()
        if not silent:
            self.valueChanged.emit(v)

    def _frac(self) -> float:
        rng = self._max - self._min
        return (self._val - self._min) / rng if rng > 0 else 0.0

    def _track_rect(self) -> Tuple[float, float, float]:
        """Returns (x_start, x_end, cy)."""
        half_t = self.THUMB_W / 2.0
        x0 = half_t
        x1 = self.width() - half_t
        cy = self.height() / 2.0
        return x0, x1, cy

    def _x_from_pos(self, mouse_x: float) -> float:
        x0, x1, _ = self._track_rect()
        frac = (mouse_x - x0) / max(x1 - x0, 1.0)
        return self._min + max(0.0, min(1.0, frac)) * (self._max - self._min)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self._press.set_target(1.0)
            self._wake()
            self.setValue(self._x_from_pos(event.position().x()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag:
            self.setValue(self._x_from_pos(event.position().x()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag = False
        self._press.set_target(0.0)
        self._wake()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        step = (self._max - self._min) / 100.0
        if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self.setValue(self._val + step)
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self.setValue(self._val - step)
        elif event.key() == Qt.Key.Key_Home:
            self.setValue(self._min)
        elif event.key() == Qt.Key.Key_End:
            self.setValue(self._max)
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark  = T.is_dark()
        dpr   = self.devicePixelRatioF()
        x0, x1, cy = self._track_rect()
        frac  = self._frac()
        thumb_cx = x0 + frac * (x1 - x0)
        half_tr  = self.TRACK / 2.0

        # Recessed capsule track
        track_bg = QColor(120, 120, 128, 96 if dark else 70)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_bg))
        p.drawRoundedRect(QRectF(x0 - half_tr, cy - half_tr, x1 - x0 + self.TRACK, self.TRACK), half_tr, half_tr)

        # Fill portion
        accent = T.color.accent.resolve(dark)
        p.setBrush(QBrush(accent))
        fill_w = max(0.0, thumb_cx - x0) + half_tr
        p.drawRoundedRect(QRectF(x0 - half_tr, cy - half_tr, fill_w, self.TRACK), half_tr, half_tr)

        # Glass capsule thumb (grows slightly on press)
        pv = max(0.0, min(1.4, self._press.value))
        tw = self.THUMB_W * (1.0 + 0.10 * pv)
        th = self.THUMB * (1.0 + 0.10 * pv)
        thumb = QRectF(thumb_cx - tw / 2.0, cy - th / 2.0, tw, th)
        _glass.paint_shadow(p, QRectF(thumb_cx - self.THUMB_W / 2.0, cy - self.THUMB / 2.0,
                                      self.THUMB_W, self.THUMB), self.THUMB / 2.0, blur=9.0,
                            alpha=0.36 if dark else 0.24, dy=2.5, dpr=dpr, exclude_shape=False)
        p.setPen(QPen(QColor(0, 0, 0, 26), 0.8))
        p.setBrush(QBrush(QColor(255, 255, 255, 250)))
        p.drawRoundedRect(thumb, th / 2.0, th / 2.0)

        # Focus ring around thumb
        if self._has_focus:
            ring_c = QColor(accent.red(), accent.green(), accent.blue(), SIZES.focus_ring_a)
            p.setPen(QPen(ring_c, SIZES.focus_ring_w))
            p.setBrush(Qt.BrushStyle.NoBrush)
            g = SIZES.focus_ring_w
            p.drawRoundedRect(thumb.adjusted(-g, -g, g, g), th / 2.0 + g, th / 2.0 + g)


# ──────────────────────────────────────────────────────────────────────────────
# 7. GlassLabel + GroupedCard
# ──────────────────────────────────────────────────────────────────────────────

class GlassLabel(QLabel):
    """
    QLabel for text that sits directly on the window glass (page titles, subtitles, card footnotes).
    Paints a tight tint plate behind the text whenever the glass setting needs one for 4.5:1 contrast
    (plate alpha is solved in ui/glass.py; 0 on a tinted/solid window that already carries the text).
    """
    PAD_X = 8
    PAD_Y = 3

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setContentsMargins(self.PAD_X, self.PAD_Y, self.PAD_X, self.PAD_Y)

    def paintEvent(self, event: QPaintEvent) -> None:
        spec = _glass.current("window", T.is_dark())
        if spec.label_alpha > 0.02 and self.text():
            fm = self.fontMetrics()
            avail = max(10, self.width() - 2 * self.PAD_X)
            flags = int(Qt.TextFlag.TextWordWrap) if self.wordWrap() else 0
            br = fm.boundingRect(0, 0, avail, 10000, flags, self.text())
            tw = min(self.width(), br.width() + 2 * self.PAD_X)
            th = min(self.height(), br.height() + 2 * self.PAD_Y)
            rect = QRectF(0, 0, tw, th)
            r = min(th / 2.0, 12.0)
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(spec.label_plate_color)
            p.drawRoundedRect(rect, r, r)
            p.end()
        super().paintEvent(event)


class GroupedCard(QWidget):
    """
    Liquid-glass inset grouped card.
    Painted with the cached glass layers (plate, soft shadow, specular + glow + hairline edge) — the plate
    alpha follows the Clear/Tinted + Transparency setting and never drops below what 4.5:1 text needs.
    Rows are separated by 1 px hairlines. Each row = title+description stack on the left, control on the right.
    Optional footnote beneath the card.
    """
    SHADOW_PAD = 8

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._footnote: str   = ""
        self._footnote_label: Optional[QLabel] = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, self.SHADOW_PAD)
        self._layout.setSpacing(4)

        self._inner = QWidget(self)
        self._inner.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(
            SPACING.lg, SPACING.md, SPACING.lg, SPACING.md
        )
        self._inner_layout.setSpacing(0)
        self._layout.addWidget(self._inner)

    def add_row(
        self,
        title:       str,
        description: str,
        widget:      QWidget,
        *,
        accessory:   Optional[QWidget] = None,
    ) -> None:
        """
        Add a settings row.
        title      — headline text (Body/600)
        description — secondary text (Footnote)
        widget     — right-aligned control
        accessory  — optional extra right widget (e.g., a small info label)
        """
        if self._rows:
            self._add_divider()

        row_widget  = QWidget(self._inner)
        row_layout  = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 10, 0, 10)
        row_layout.setSpacing(SPACING.md)

        # Left: title + description
        text_col  = QVBoxLayout()
        text_col.setSpacing(2)

        dark = T.is_dark()

        lbl_title = QLabel(title, row_widget)
        title_font = qfont(TYPE.body)
        title_font.setWeight(QFont.Weight.DemiBold)
        lbl_title.setFont(title_font)
        lbl_title.setStyleSheet(f"color: {_glass.label_color('label', dark).name()};")
        text_col.addWidget(lbl_title)

        if description:
            lbl_desc = QLabel(description, row_widget)
            lbl_desc.setFont(qfont(TYPE.footnote))
            lbl_desc.setStyleSheet(f"color: {_glass.label_color('secondary_label', dark).name()};")
            lbl_desc.setWordWrap(True)
            text_col.addWidget(lbl_desc)

        row_layout.addLayout(text_col, stretch=1)

        if accessory:
            row_layout.addWidget(accessory)
        row_layout.addWidget(widget)

        self._inner_layout.addWidget(row_widget)
        self._rows.append({"title": title, "widget": widget})

    def add_widget(self, widget: QWidget, *, divider: bool = True) -> None:
        """Add a full-width content widget (e.g. PageOrderList) as a row of its own."""
        if self._rows and divider:
            self._add_divider()
        self._inner_layout.addWidget(widget)
        self._rows.append({"title": "", "widget": widget})

    def _add_divider(self) -> None:
        div = QFrame(self._inner)
        div.setFixedHeight(1)
        dark = T.is_dark()
        div_color = "rgba(255, 255, 255, 20)" if dark else "rgba(0, 0, 0, 20)"
        div.setStyleSheet(f"background: {div_color}; border: none;")
        self._inner_layout.addWidget(div)

    def set_footnote(self, text: str) -> None:
        """Add a footnote label beneath the card content."""
        self._footnote = text
        dark = T.is_dark()
        if self._footnote_label is None:
            self._footnote_label = GlassLabel(parent=self)
            self._footnote_label.setFont(qfont(TYPE.footnote))
            self._footnote_label.setContentsMargins(SPACING.md, GlassLabel.PAD_Y,
                                                    SPACING.md, GlassLabel.PAD_Y)
            self._footnote_label.setWordWrap(True)
            self._layout.addWidget(self._footnote_label)
        self._footnote_label.setText(text)
        self._footnote_label.setStyleSheet(
            f"color: {_glass.label_color('secondary_label', dark).name()}; font-size: 11px;")

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = T.is_dark()
        spec = _glass.current("window", dark)
        inner_geo = self._inner.geometry()
        rect = QRectF(inner_geo.x(), inner_geo.y(), inner_geo.width(), inner_geo.height())
        _glass.paint_glass(p, rect, _glass.GLASS.card_radius, spec, fill="plate", shadow=True,
                           dpr=self.devicePixelRatioF(), shadow_blur=12.0)


# ──────────────────────────────────────────────────────────────────────────────
# 8. SidebarSearchField
# ──────────────────────────────────────────────────────────────────────────────

class SidebarSearchField(FocusRingMixin, QWidget):
    """
    macOS-style search field for the Settings sidebar.
    Has a magnifying glass icon on the left and a clear button on the right.
    Emits textChanged(str).
    """
    textChanged = Signal(str)

    def __init__(self, placeholder: str = "Search", parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 0, 8, 0)
        layout.setSpacing(SPACING.xs)
        self.setFixedHeight(28)

        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText(placeholder)
        self._edit.setFrame(False)
        self._edit.setFont(qfont(TYPE.subheadline))
        dark = T.is_dark()
        label_c = T.color.label.resolve(dark).name()
        sec_c = T.color.secondary_label.resolve(dark).name()
        self._edit.setStyleSheet(f"background: transparent; border: none; color: {label_c}; padding: 0;")
        self._edit.textChanged.connect(self.textChanged)
        layout.addWidget(self._edit)

        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFocusProxy(self._edit)

    def text(self) -> str:
        return self._edit.text()

    def clear(self) -> None:
        self._edit.clear()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = T.is_dark()
        w, h = float(self.width()), float(self.height())
        r    = h / 2.0

        # Background capsule
        bg = QColor(120, 120, 128, 30) if dark else QColor(120, 120, 128, 22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)

        # Magnifying glass icon on the left
        icon_col = T.color.secondary_label.resolve(dark)
        p.setPen(QPen(icon_col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        mg_cx, mg_cy = 13.0, h / 2.0 - 1.0
        p.drawEllipse(QPointF(mg_cx, mg_cy), 4.2, 4.2)
        p.drawLine(QPointF(mg_cx + 3.0, mg_cy + 3.0), QPointF(mg_cx + 6.8, mg_cy + 6.8))

        # Subtle border
        border_col = QColor(255, 255, 255, 12) if dark else QColor(0, 0, 0, 10)
        p.setPen(QPen(border_col, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), r, r)

        # Focus ring
        self._draw_focus_ring(p, QRectF(0, 0, w, h), r)


class AppleComboBox(QComboBox):
    """
    Apple HIG styled QComboBox with a clean antialiased chevron down-arrow.
    """
    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        arrow_col = T.color.secondary_label.resolve(dark)
        p.setPen(QPen(arrow_col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        cx = float(self.width() - 14)
        cy = float(self.height() / 2.0)
        p.drawLine(QPointF(cx - 3.5, cy - 1.5), QPointF(cx, cy + 2.0))
        p.drawLine(QPointF(cx, cy + 2.0), QPointF(cx + 3.5, cy - 1.5))


# ──────────────────────────────────────────────────────────────────────────────
# 9. Buttons — capsule glass, floating soft shadow, spring hover/press morph
# ──────────────────────────────────────────────────────────────────────────────

class _BaseButton(FocusRingMixin, _SpringTicker, QWidget):
    """
    Internal base for all button variants. Capsule geometry; the widget is a few px larger than the
    visible capsule so the cached soft shadow can float under it. Hover/press are springs.
    """
    clicked = Signal()

    PX, PT, PB = 2, 1, 3          # shadow room: left/right, top, bottom

    def __init__(
        self,
        label:  str,
        height: int = 32,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._label   = label
        self._hovered = False
        self._pressed = False
        self._enabled = True
        self._vis_h   = height
        self.setFixedHeight(height + self.PT + self.PB)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        font = qfont(TYPE.body)
        font.setWeight(QFont.Weight.Medium)
        self._font = font
        self._hv = Spring(0.0, SPRING.fast.stiffness, SPRING.fast.damping)   # hover amount 0..1
        self._pv = Spring(0.0, SPRING.fast.stiffness, SPRING.fast.damping)   # press amount 0..1
        self._init_ticker()

    def text(self) -> str:
        return self._label

    def setText(self, label: str) -> None:
        if self._label == label:
            return
        self._label = label
        self.update()

    def setEnabled(self, v: bool) -> None:
        if self._enabled == v:
            return
        self._enabled = v
        self.setCursor(Qt.CursorShape.PointingHandCursor if v else Qt.CursorShape.ArrowCursor)
        self.update()

    def isEnabled(self) -> bool:
        return self._enabled

    def _tick(self) -> None:
        dt, rm = self._dt(), T.reduce_motion()
        self._hv.update(dt, rm)
        self._pv.update(dt, rm)
        self.update()
        if self._hv.settled and self._pv.settled:
            self._timer.stop()

    def enterEvent(self, e) -> None:
        if not self._hovered:
            self._hovered = True
            self._hv.set_target(1.0)
            self._wake()
            self.update()

    def leaveEvent(self, e) -> None:
        if self._hovered or self._pressed:
            self._hovered = False
            self._pressed = False
            self._hv.set_target(0.0)
            self._pv.set_target(0.0)
            self._wake()
            self.update()

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if self._enabled and e.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._pv.set_target(1.0)
            self._wake()
            self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self._enabled and self._pressed:
            self.clicked.emit()
        self._pressed = False
        self._pv.set_target(0.0)
        self._wake()
        self.update()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return):
            if self._enabled:
                self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def sizeHint(self) -> QSize:
        fm   = QFontMetrics(self._font)
        text_w = fm.horizontalAdvance(self._label)
        return QSize(text_w + SPACING.xl * 2 + 2 * self.PX, self.height())

    # -- geometry / painting helpers shared by the variants --------------------------------------------
    def _capsule(self) -> Tuple[QRectF, float]:
        """Visible capsule rect (shrinks a hair while pressed) and its radius (h / 2)."""
        pv = max(0.0, min(1.2, self._pv.value))
        w, h = float(self.width()), float(self._vis_h)
        rect = QRectF(self.PX, self.PT, w - 2 * self.PX, h)
        k = 1.2 * pv
        rect = rect.adjusted(k, k * 0.5, -k, -k * 0.5)
        return rect, rect.height() / 2.0

    def _paint_capsule(self, p: QPainter, rect: QRectF, r: float, fill: QColor, *,
                       shadow: float = 0.0, glow: bool = False, specular: bool = True) -> None:
        dark = T.is_dark()
        dpr = self.devicePixelRatioF()
        if shadow > 0.0 and self._enabled:
            _glass.paint_shadow(p, rect, r, blur=8.0, alpha=shadow, dy=2.0, dpr=dpr, exclude_shape=False)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(rect, r, r)
        edge = _glass.edge_layer(int(round(rect.width())), int(round(rect.height())), r, dark, dpr,
                                 specular=specular and self._enabled, glow=glow)
        p.drawPixmap(rect.topLeft(), edge)

    def _draw_label(
        self,
        p: QPainter,
        text_color: QColor,
        rect: QRectF,
    ) -> None:
        p.setFont(self._font)
        p.setPen(text_color)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)


class PrimaryButton(_BaseButton):
    """Filled accent capsule (e.g., Save, Connect, Test). Fill is tuned so the white label reads >= 4.5:1."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        rect, r = self._capsule()
        fill = _glass.solid_fill_for_white(QColor(T.color.accent.resolve(dark)))
        hv, pv = self._hv.value, self._pv.value
        if not self._enabled:
            fill.setAlphaF(0.4)
        else:
            if hv > 0.001:
                fill = fill.lighter(100 + int(7 * min(1.0, hv)))
            if pv > 0.001:
                fill = fill.darker(100 + int(12 * min(1.0, pv)))
        self._paint_capsule(p, rect, r, fill, shadow=0.30 if dark else 0.22)
        text_col = QColor("#FFFFFF")
        if not self._enabled:
            text_col.setAlphaF(0.7)
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


class PlainButton(_BaseButton):
    """Glass capsule (default secondary style)."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        rect, r = self._capsule()
        hv, pv = min(1.0, self._hv.value), min(1.0, self._pv.value)
        if dark:
            a = 24 + 12 * hv - 10 * pv
            fill = QColor(255, 255, 255, int(a))
        else:
            a = 14 + 8 * hv - 6 * pv
            fill = QColor(0, 0, 0, int(a))
        if not self._enabled:
            fill.setAlphaF(fill.alphaF() * 0.5)
        self._paint_capsule(p, rect, r, fill, shadow=0.0, glow=True)
        text_col = _glass.label_color("label", dark)
        if not self._enabled:
            text_col = QColor(T.color.tertiary_label.resolve(dark))
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


class DestructiveButton(_BaseButton):
    """Tinted destructive/danger capsule (red); label colour is fitted to >= 4.5:1 on its own tint."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        rect, r = self._capsule()
        dest = T.color.destructive.resolve(dark)
        hv, pv = min(1.0, self._hv.value), min(1.0, self._pv.value)
        pressed_solid = pv > 0.5
        tint = QColor(dest.red(), dest.green(), dest.blue(), 255 if pressed_solid else int(26 + 28 * hv))
        self._paint_capsule(p, rect, r, tint, glow=True)
        if pressed_solid:
            text_col = _glass.readable_on(dest)
        else:
            plate = (30, 30, 35) if dark else (255, 255, 255)
            text_col = _glass.fit_text(dest, plate)
        if not self._enabled:
            text_col.setAlphaF(0.5)
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


class SuccessButton(_BaseButton):
    """Filled success/green capsule; the label is whichever of white/ink reads best on the fill."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        rect, r = self._capsule()
        success = QColor(T.color.success.resolve(dark))
        hv, pv = self._hv.value, self._pv.value
        if not self._enabled:
            success.setAlphaF(0.4)
        else:
            if hv > 0.001:
                success = success.lighter(100 + int(6 * min(1.0, hv)))
            if pv > 0.001:
                success = success.darker(100 + int(10 * min(1.0, pv)))
        self._paint_capsule(p, rect, r, success, shadow=0.26 if dark else 0.18)
        text_col = _glass.readable_on(T.color.success.resolve(dark))
        if not self._enabled:
            text_col.setAlphaF(0.6)
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


class CapsuleIconButton(FocusRingMixin, _SpringTicker, QWidget):
    """Round glass capsule with a chevron ("up" | "down"); used by PageOrderList. Hover/press are springs."""
    clicked = Signal()

    def __init__(self, kind: str = "up", size: int = 30, tip: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._kind = kind
        self._enabled = True
        self._hovered = False
        self._pressed = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if tip:
            self.setToolTip(tip)
            self.setAccessibleName(tip)
        self._hv = Spring(0.0, SPRING.fast.stiffness, SPRING.fast.damping)
        self._pv = Spring(0.0, SPRING.fast.stiffness, SPRING.fast.damping)
        self._init_ticker()

    def setEnabled(self, v: bool) -> None:
        if self._enabled == v:
            return
        self._enabled = v
        self.setCursor(Qt.CursorShape.PointingHandCursor if v else Qt.CursorShape.ArrowCursor)
        self.update()

    def isEnabled(self) -> bool:
        return self._enabled

    def _tick(self) -> None:
        dt, rm = self._dt(), T.reduce_motion()
        self._hv.update(dt, rm)
        self._pv.update(dt, rm)
        self.update()
        if self._hv.settled and self._pv.settled:
            self._timer.stop()

    def enterEvent(self, e) -> None:
        self._hovered = True
        self._hv.set_target(1.0)
        self._wake()

    def leaveEvent(self, e) -> None:
        self._hovered = self._pressed = False
        self._hv.set_target(0.0)
        self._pv.set_target(0.0)
        self._wake()

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if self._enabled and e.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._pv.set_target(1.0)
            self._wake()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self._enabled and self._pressed:
            self.clicked.emit()
        self._pressed = False
        self._pv.set_target(0.0)
        self._wake()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return) and self._enabled:
            self.clicked.emit()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        dpr = self.devicePixelRatioF()
        s = float(self.width())
        hv, pv = min(1.0, self._hv.value), min(1.0, self._pv.value)
        k = 1.0 * pv
        rect = QRectF(k, k, s - 2 * k, s - 2 * k)
        r = rect.height() / 2.0
        a = (26 + 14 * hv - 10 * pv) if dark else (16 + 8 * hv - 6 * pv)
        fill = QColor(255, 255, 255, int(a)) if dark else QColor(0, 0, 0, int(a))
        if not self._enabled:
            fill.setAlphaF(fill.alphaF() * 0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawEllipse(rect)
        p.drawPixmap(rect.topLeft(), _glass.edge_layer(int(round(rect.width())), int(round(rect.height())), r,
                                                       dark, dpr, specular=self._enabled, glow=True))
        col = _glass.label_color("label", dark)
        if not self._enabled:
            col.setAlphaF(0.35)
        p.setPen(QPen(col, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        cx, cy = s / 2.0, s / 2.0
        d = 3.2 if self._kind == "up" else -3.2
        p.drawLine(QPointF(cx - 4.5, cy + d / 2), QPointF(cx, cy - d / 2))
        p.drawLine(QPointF(cx, cy - d / 2), QPointF(cx + 4.5, cy + d / 2))
        self._draw_focus_ring(p, QRectF(0, 0, s, s), s / 2.0)


# ──────────────────────────────────────────────────────────────────────────────
# 9b. PageOrderList — reorderable list with on/off switches and up/down capsule buttons
# ──────────────────────────────────────────────────────────────────────────────

class _PageRow(QWidget):
    """One glass row inside PageOrderList; inner radius is concentric with the card (card radius - pad)."""

    def __init__(self, pid: str, title: str, desc: str, enabled: bool, parent: QWidget):
        super().__init__(parent)
        self.pid = pid
        self.setFixedHeight(PageOrderList.ROW_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 10, 6)
        lay.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.lbl_title = QLabel(title, self)
        tf = qfont(TYPE.body)
        tf.setWeight(QFont.Weight.DemiBold)
        self.lbl_title.setFont(tf)
        self.lbl_desc = QLabel(desc, self)
        self.lbl_desc.setFont(qfont(TYPE.footnote))
        col.addWidget(self.lbl_title)
        col.addWidget(self.lbl_desc)
        lay.addLayout(col, 1)
        self.switch = AppleSwitch(enabled, self)
        self.btn_up = CapsuleIconButton("up", 30, "Move up", self)
        self.btn_down = CapsuleIconButton("down", 30, "Move down", self)
        lay.addWidget(self.switch)
        lay.addSpacing(4)
        lay.addWidget(self.btn_up)
        lay.addWidget(self.btn_down)
        self.refresh_colors(enabled)

    def refresh_colors(self, enabled: bool) -> None:
        dark = T.is_dark()
        pri = _glass.label_color("label", dark).name()
        sec = _glass.label_color("secondary_label", dark).name()
        self.lbl_title.setStyleSheet(f"color: {pri if enabled else sec}; background: transparent;")
        self.lbl_desc.setStyleSheet(f"color: {sec}; background: transparent;")

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        r = _glass.concentric(_glass.GLASS.card_radius, 6.0)
        p.setPen(QPen(QColor(255, 255, 255, 22) if dark else QColor(0, 0, 0, 18), 1.0))
        p.setBrush(QColor(255, 255, 255, 12) if dark else QColor(0, 0, 0, 7))
        p.drawRoundedRect(rect, r, r)


class PageOrderList(QWidget):
    """
    Reorderable list of island card pages. Each row: title + footnote, on/off switch, up/down capsule buttons.
    Reordering is a spring morph (rows glide to their new slots). Emits:
        orderChanged(list[str])          — full page id order
        enabledChanged(str, bool)        — page id, enabled
    """
    orderChanged = Signal(list)
    enabledChanged = Signal(str, bool)

    ROW_H = 54
    GAP = 6

    def __init__(self, pages: List[Tuple[str, str, str]], order: List[str], enabled: dict,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._defs = {pid: (title, desc) for pid, title, desc in pages}
        seen: List[str] = []
        for pid in list(order) + [pid for pid, _, _ in pages]:
            if pid in self._defs and pid not in seen:
                seen.append(pid)
        self._order = seen
        self._enabled = {pid: bool(enabled.get(pid, True)) for pid in self._defs}
        self.rows: dict = {}
        self._ys: dict = {}
        self._ticker = QTimer(self)
        self._ticker.timeout.connect(self._tick)
        self._last = time.perf_counter()
        n = len(self._order)
        self.setFixedHeight(n * self.ROW_H + max(0, n - 1) * self.GAP)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for pid in self._order:
            title, desc = self._defs[pid]
            row = _PageRow(pid, title, desc, self._enabled[pid], self)
            row.switch.toggled.connect(lambda v, k=pid: self._on_toggle(k, v))
            row.btn_up.clicked.connect(lambda k=pid: self.move_page(k, -1))
            row.btn_down.clicked.connect(lambda k=pid: self.move_page(k, +1))
            self.rows[pid] = row
            self._ys[pid] = Spring(self._slot_y(self._order.index(pid)), SPRING.fast.stiffness, SPRING.fast.damping)
        self._place(immediate=True)
        self._sync_buttons()

    # -- public ------------------------------------------------------------------------------------
    def order(self) -> List[str]:
        return list(self._order)

    def enabled_map(self) -> dict:
        return dict(self._enabled)

    def switch_for(self, pid: str) -> AppleSwitch:
        return self.rows[pid].switch

    def move_page(self, pid: str, delta: int) -> bool:
        if pid not in self._order:
            return False
        i = self._order.index(pid)
        j = i + int(delta)
        if j < 0 or j >= len(self._order):
            return False
        self._order[i], self._order[j] = self._order[j], self._order[i]
        for k, name in enumerate(self._order):
            self._ys[name].set_target(self._slot_y(k))
        self._sync_buttons()
        if not self._ticker.isActive():
            self._last = time.perf_counter()
            self._ticker.start(16)
        self.orderChanged.emit(list(self._order))
        return True

    # -- internals ---------------------------------------------------------------------------------
    def _slot_y(self, index: int) -> float:
        return float(index * (self.ROW_H + self.GAP))

    def _on_toggle(self, pid: str, v: bool) -> None:
        self._enabled[pid] = bool(v)
        self.rows[pid].refresh_colors(bool(v))
        self.enabledChanged.emit(pid, bool(v))

    def _sync_buttons(self) -> None:
        n = len(self._order)
        for i, pid in enumerate(self._order):
            self.rows[pid].btn_up.setEnabled(i > 0)
            self.rows[pid].btn_down.setEnabled(i < n - 1)
            self.rows[pid].btn_up.update()
            self.rows[pid].btn_down.update()

    def _place(self, immediate: bool = False) -> None:
        w = self.width()
        for pid, row in self.rows.items():
            if immediate:
                self._ys[pid].snap(self._slot_y(self._order.index(pid)))
            row.setGeometry(0, int(round(self._ys[pid].value)), w, self.ROW_H)

    def resizeEvent(self, event) -> None:
        self._place()
        super().resizeEvent(event)

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(max(now - self._last, SPRING_DT_MIN), SPRING_DT_MAX)
        self._last = now
        rm = T.reduce_motion()
        done = True
        for pid, sp in self._ys.items():
            sp.update(dt, rm)
            if not sp.settled:
                done = False
        self._place()
        if done:
            for pid, sp in self._ys.items():
                sp.snap(sp.target)
            self._place()
            self._ticker.stop()


# ──────────────────────────────────────────────────────────────────────────────
# 10. StatusPill
# ──────────────────────────────────────────────────────────────────────────────

class StatusPill(QWidget):
    """
    Small rounded badge / status indicator.
    variant: 'default' | 'success' | 'warning' | 'error' | 'accent'
    """
    def __init__(
        self,
        text:    str     = "",
        variant: str     = "default",
        parent:  Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._text    = text
        self._variant = variant
        self._font    = qfont(TYPE.caption_2)
        self._font.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(self._font)
        tw = fm.horizontalAdvance(text)
        self.setFixedSize(tw + SPACING.md * 2, 20)

    def setText(self, text: str) -> None:
        if self._text == text:
            return
        self._text = text
        fm = QFontMetrics(self._font)
        tw = fm.horizontalAdvance(text)
        self.setFixedSize(tw + SPACING.md * 2, 20)
        self.update()

    def setVariant(self, variant: str) -> None:
        if self._variant == variant:
            return
        self._variant = variant
        self.update()

    def set_variant(self, variant: str) -> None:
        self.setVariant(variant)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = T.is_dark()
        w, h = float(self.width()), float(self.height())
        r    = h / 2.0
        rect = QRectF(0, 0, w, h)

        v = self._variant
        if v == "success":
            bg  = T.color.success.resolve(dark)
            tc  = _glass.readable_on(bg)
        elif v == "warning":
            bg  = T.color.warning.resolve(dark)
            tc  = _glass.readable_on(bg)
        elif v == "error":
            bg  = _glass.solid_fill_for_white(T.color.destructive.resolve(dark))
            tc  = QColor("#FFFFFF")
        elif v == "accent":
            bg  = _glass.solid_fill_for_white(T.color.accent.resolve(dark))
            tc  = QColor("#FFFFFF")
        else:
            bg  = QColor(120, 120, 128, 50)
            tc  = _glass.label_color("secondary_label", dark)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(rect, r, r)
        p.setPen(tc)
        p.setFont(self._font)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)


# ──────────────────────────────────────────────────────────────────────────────
# 11. MicLevelMeter
# ──────────────────────────────────────────────────────────────────────────────

class MicLevelMeter(QWidget):
    """
    Real-time microphone level meter — 5 vertical bars animated by RMS level.
    Call update_level(norm_rms: float) from a QTimer to drive animation.
    """
    BAR_COUNT = 12

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(24)
        self.setMinimumWidth(60)
        self._level    = 0.0   # 0..1 normalised
        self._smoothed = [0.0] * self.BAR_COUNT

    def update_level(self, norm_rms: float) -> None:
        self._level = max(0.0, min(1.0, float(norm_rms)))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark   = T.is_dark()
        accent = T.color.accent.resolve(dark)
        w, h   = float(self.width()), float(self.height())
        n      = self.BAR_COUNT
        bar_w  = max(2.0, (w - n + 1) / n)
        gap    = 1.5

        t = time.time()
        for i in range(n):
            frac   = i / (n - 1)
            # Drive each bar from level with a frequency offset
            bar_level = self._level * (0.4 + 0.6 * max(0.0, math.sin(t * 8.0 + frac * math.pi)))
            bar_level = min(1.0, bar_level)

            bar_h = max(2.0, bar_level * h)
            bx    = i * (bar_w + gap)
            by    = h - bar_h

            # Inactive track
            track_col = QColor(120, 120, 128, 40)
            p.setBrush(QBrush(track_col))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(bx, 0, bar_w, h), bar_w / 2.0, bar_w / 2.0)

            # Active fill — green below 70%, yellow at 70-90%, red above 90%
            if bar_level < 0.7:
                fill = T.color.success.resolve(dark)
            elif bar_level < 0.9:
                fill = T.color.warning.resolve(dark)
            else:
                fill = T.color.destructive.resolve(dark)

            p.setBrush(QBrush(fill))
            p.drawRoundedRect(QRectF(bx, by, bar_w, bar_h), bar_w / 2.0, bar_w / 2.0)

        # Repaint at 30 fps while visible
        QTimer.singleShot(33, self.update)


# ──────────────────────────────────────────────────────────────────────────────
# 11b. SensitivityPreviewWidget — Animated Gesture Sensitivity Demo
# ──────────────────────────────────────────────────────────────────────────────

class SensitivityPreviewWidget(QWidget):
    """
    Live animated preview widget for gesture sensitivity settings.
    Simulates cyclic hand opening/closing to visually demonstrate how
    the configured thresholds separate Move, Neutral (deadband), and Click states.
    """
    def __init__(self, open_thresh: float = 0.70, fist_thresh: float = 0.28, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(38)
        self.setMinimumWidth(210)
        self._open_thresh = open_thresh
        self._fist_thresh = fist_thresh
        self._t0 = time.time()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(33)

    def set_thresholds(self, open_thresh: float, fist_thresh: float) -> None:
        self._open_thresh = float(open_thresh)
        self._fist_thresh = float(fist_thresh)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = T.is_dark()
        w, h = float(self.width()), float(self.height())
        now = time.time()
        elapsed = now - self._t0

        # Simulate hand openness moving between 0.15 and 0.90 in a 3.5s cycle
        cycle = (math.sin(elapsed * 2.0) + 1.0) / 2.0
        openness = 0.18 + cycle * (0.86 - 0.18)

        # Classify simulated state
        if openness >= self._open_thresh:
            state_text = "MOVE"
            state_col = T.color.success.resolve(dark)
        elif openness <= self._fist_thresh:
            state_text = "CLICK"
            state_col = COLOR.HUD_ONLY.stark_gold
        else:
            state_text = "NEUTRAL"
            state_col = T.color.secondary_label.resolve(dark)

        # Track bar
        bar_x = 8.0
        bar_y = 12.0
        bar_w = w - 85.0
        bar_h = 14.0
        bar_r = 7.0

        # Background track
        track_bg = QColor(120, 120, 128, 40)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_bg))
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), bar_r, bar_r)

        # Threshold zones: Fist zone (red/gold), Neutral zone (gray), Open zone (green)
        fist_x = bar_x + bar_w * self._fist_thresh
        open_x = bar_x + bar_w * self._open_thresh

        # Active fill level
        current_x = bar_x + bar_w * openness
        fill_w = max(4.0, current_x - bar_x)
        p.setBrush(QBrush(state_col))
        p.drawRoundedRect(QRectF(bar_x, bar_y, min(fill_w, bar_w), bar_h), bar_r, bar_r)

        # Threshold markers (1 px lines)
        p.setPen(QPen(QColor(255, 255, 255, 180 if dark else 220), 1.5))
        p.drawLine(QPointF(fist_x, bar_y - 2), QPointF(fist_x, bar_y + bar_h + 2))
        p.drawLine(QPointF(open_x, bar_y - 2), QPointF(open_x, bar_y + bar_h + 2))

        # Status badge on the right
        badge_x = bar_x + bar_w + 10.0
        badge_w = 60.0
        badge_h = 22.0
        badge_y = (h - badge_h) / 2.0

        badge_bg = QColor(state_col.red(), state_col.green(), state_col.blue(), 45)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(badge_bg))
        p.drawRoundedRect(QRectF(badge_x, badge_y, badge_w, badge_h), 6.0, 6.0)

        badge_font = qfont(TYPE.caption_2)
        badge_font.setWeight(QFont.Weight.Bold)
        p.setFont(badge_font)
        p.setPen(state_col)
        p.drawText(QRectF(badge_x, badge_y, badge_w, badge_h), Qt.AlignmentFlag.AlignCenter, state_text)


# ──────────────────────────────────────────────────────────────────────────────
# 12. Page container helper
# ──────────────────────────────────────────────────────────────────────────────

def make_scroll_page(title: str, subtitle: str) -> Tuple[QWidget, QVBoxLayout]:
    """
    Build a scrollable settings page with a Large Title header.
    Returns (page_widget, content_layout) — add cards to content_layout.
    """
    page        = QWidget()
    page_layout = QVBoxLayout(page)
    page_layout.setContentsMargins(0, 0, 0, 0)
    page_layout.setSpacing(0)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setStyleSheet("""
        QScrollArea { background: transparent; border: none; }
        QScrollBar:vertical {
            background: transparent;
            width: 5px;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            background: rgba(120,120,128,0.4);
            border-radius: 2px;
            min-height: 24px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    """)

    content        = QWidget()
    content_layout = QVBoxLayout(content)
    content_layout.setContentsMargins(SPACING.xl, SPACING.xl, SPACING.xl, SPACING.xl)
    content_layout.setSpacing(SPACING.md + 2)      # cards add their own 8 px shadow room

    def _flush_left(lbl: QWidget) -> None:
        # GlassLabel has plate padding; a negative-margin row keeps the text flush with the cards
        row = QHBoxLayout()
        row.setContentsMargins(-GlassLabel.PAD_X, 0, -GlassLabel.PAD_X, 0)
        row.addWidget(lbl)
        row.addStretch()
        content_layout.addLayout(row)

    # Large Title (text sits loose on the window -> GlassLabel gives it a contrast plate when needed)
    lbl_title = GlassLabel(title)
    lbl_title.setFont(qfont(TYPE.large_title))
    _flush_left(lbl_title)

    if subtitle:
        lbl_sub = GlassLabel(subtitle)
        lbl_sub.setFont(qfont(TYPE.callout))
        lbl_sub.setStyleSheet(f"color: {_glass.label_color('secondary_label', T.is_dark()).name()};")
        _flush_left(lbl_sub)
        content_layout.addSpacing(SPACING.xxs)

    scroll.setWidget(content)
    page_layout.addWidget(scroll)

    return page, content_layout


def slider_card_row(
    card:        GroupedCard,
    title:       str,
    description: str,
    value:       float,
    minimum:     float,
    maximum:     float,
    unit:        str,
    on_change:   Callable[[float], None],
) -> HSlider:
    """
    Convenience: add an HSlider row to a GroupedCard.
    Returns the HSlider widget.
    """
    row     = QWidget()
    layout  = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACING.sm)

    slider  = HSlider(minimum, maximum, value, unit, row)
    slider.setFixedWidth(140)

    dark = T.is_dark()
    lbl_val = QLabel(f"{value:.2f} {unit}")
    lbl_val.setFont(qfont(TYPE.metric))
    lbl_val.setStyleSheet(
        f"color: {_glass.fit_text(T.color.accent.resolve(dark), (30, 30, 35) if dark else (255, 255, 255)).name()};"
        " min-width: 56px;"
    )
    lbl_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _handle(v: float) -> None:
        lbl_val.setText(f"{v:.2f} {unit}")
        on_change(v)

    slider.valueChanged.connect(_handle)
    layout.addWidget(slider)
    layout.addWidget(lbl_val)

    card.add_row(title, description, row)
    return slider
