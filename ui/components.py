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
# 4. AppleSwitch — 51×31 iOS-style toggle
# ──────────────────────────────────────────────────────────────────────────────

class AppleSwitch(FocusRingMixin, QWidget):
    """
    51×31 px iOS/macOS-style animated toggle switch.

    States: off (gray track), on (green track), hover (slightly brighter track),
            pressed (track darkens), disabled (de-saturated), focus (accent ring).
    Thumb animates with SPRING.fast spring.
    """
    toggled = Signal(bool)

    W: int = SIZES.switch_w
    H: int = SIZES.switch_h
    THUMB: int = SIZES.switch_thumb

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(self.W, self.H)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._checked  = checked
        self._hovered  = False
        self._pressed  = False
        self._enabled  = True

        # Spring drives thumb x-position in normalised [0, 1]
        self._spring   = Spring(1.0 if checked else 0.0)
        self._spring.k = SPRING.fast.stiffness
        self._spring.c = SPRING.fast.damping
        self._thumb_t  = 1.0 if checked else 0.0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

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
        if not self._timer.isActive():
            self._timer.start(16)
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
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._enabled and self._pressed:
            self.setChecked(not self._checked)
        self._pressed = False
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
            self.update()

    # ── animation ─────────────────────────────────────────────────────────

    def _tick(self) -> None:
        reduce = T.reduce_motion()
        new_t  = self._spring.update(0.016, reduce)
        if abs(new_t - self._thumb_t) > 0.003:
            self._thumb_t = new_t
            self.update()
        if self._spring.settled:
            self._thumb_t = 1.0 if self._checked else 0.0
            self._timer.stop()

    # ── painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = T.is_dark()
        w, h = float(self.W), float(self.H)
        r    = h / 2.0

        t   = self._thumb_t          # 0.0 = off, 1.0 = on
        dis = not self._enabled

        # Track color — interpolate between off-gray and on-green
        if dis:
            if dark:
                track_col = QColor("#3A3A3C")
                track_col.setAlphaF(0.5)
            else:
                track_col = QColor("#C7C7CC")
                track_col.setAlphaF(0.6)
        else:
            off_col = QColor("#3A3A3C") if dark else QColor("#E5E5EA")
            on_col  = QColor("#30D158") if dark else QColor("#34C759")
            # Lerp RGB
            pr = off_col.red()   + (on_col.red()   - off_col.red())   * t
            pg = off_col.green() + (on_col.green()  - off_col.green()) * t
            pb = off_col.blue()  + (on_col.blue()   - off_col.blue())  * t
            track_col = QColor(int(pr), int(pg), int(pb))

            if self._pressed:
                # Darken on press
                track_col = track_col.darker(115)
            elif self._hovered:
                track_col = track_col.lighter(108)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_col))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)

        # Thumb
        margin   = 2.0
        travel   = w - self.THUMB - margin * 2
        thumb_x  = margin + t * travel
        thumb_y  = margin
        thumb_r  = self.THUMB / 2.0

        # Soft shadow beneath thumb
        p.setBrush(QBrush(QColor(0, 0, 0, 50 if not dis else 20)))
        p.drawEllipse(QRectF(thumb_x, thumb_y + 1.2, self.THUMB, self.THUMB))

        # White thumb
        thumb_col = QColor("#FFFFFF") if not dis else QColor("#FFFFFF" if dark else "#FFFFFF")
        if dis:
            thumb_col.setAlphaF(0.7)
        if self._pressed:
            # Thumb slightly smaller when pressed (tactile feel)
            shrink = 1.5
            p.setBrush(QBrush(thumb_col))
            p.drawEllipse(QRectF(
                thumb_x + shrink * t, thumb_y + shrink,
                self.THUMB - shrink * 2, self.THUMB - shrink * 2
            ))
        else:
            p.setBrush(QBrush(thumb_col))
            p.drawEllipse(QRectF(thumb_x, thumb_y, self.THUMB, self.THUMB))

        # Focus ring
        self._draw_focus_ring(p, QRectF(0, 0, w, h), r)

    def sizeHint(self) -> QSize:
        return QSize(self.W, self.H)


# ──────────────────────────────────────────────────────────────────────────────
# 5. SegmentedControl — sliding pill thumb
# ──────────────────────────────────────────────────────────────────────────────

class SegmentedControl(FocusRingMixin, QWidget):
    """
    macOS/iOS segmented control with an animated sliding pill thumb.
    Options: list of (id: str, label: str).
    Selected segment highlighted by a spring-animated squircle underlay.
    Light + dark theme aware.
    """
    valueChanged = Signal(str)

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
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)

        # Spring animates the thumb x-position (as fraction 0→1 per segment)
        self._idx = self._index_of(self._current)
        self._spring = Spring(float(self._idx))
        self._spring.k = SPRING.fast.stiffness
        self._spring.c = SPRING.fast.damping
        self._thumb_x  = float(self._idx)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

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
        if not self._timer.isActive():
            self._timer.start(16)
        if not silent:
            self.valueChanged.emit(oid)
        self.update()

    def _tick(self) -> None:
        new_x = self._spring.update(0.016, T.reduce_motion())
        if abs(new_x - self._thumb_x) > 0.002:
            self._thumb_x = new_x
            self.update()
        if self._spring.settled:
            self._thumb_x = float(self._idx)
            self._timer.stop()

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
        self.setValue(oid)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        n   = len(self.options)
        seg = self.width() / n
        new_hov = int(event.position().x() // seg)
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
        n     = len(self.options)
        seg_w = w / n
        hpad  = 3.0      # horizontal track padding
        vpad  = 3.0      # vertical track padding

        # Track background
        track_col = QColor("#3A3A3C") if dark else QColor("#E5E5EA")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_col))
        track_r = (h - vpad * 2) / 2.0
        p.drawRoundedRect(QRectF(hpad, vpad, w - hpad * 2, h - vpad * 2), track_r, track_r)

        # Animated pill thumb
        thumb_w = seg_w - 4.0
        thumb_h = h - vpad * 2 - 2.0
        thumb_x = hpad + 2.0 + self._thumb_x * seg_w
        thumb_y = vpad + 1.0
        thumb_r = thumb_h / 2.0

        # Drop shadow beneath thumb
        p.setBrush(QBrush(QColor(0, 0, 0, 35)))
        p.drawRoundedRect(QRectF(thumb_x, thumb_y + 1.0, thumb_w, thumb_h), thumb_r, thumb_r)

        thumb_surface = QColor("#FFFFFF") if dark else QColor("#FFFFFF")
        p.setBrush(QBrush(thumb_surface))
        p.drawRoundedRect(QRectF(thumb_x, thumb_y, thumb_w, thumb_h), thumb_r, thumb_r)

        # Hover hint
        if self._hovered is not None and self._hovered != self._idx:
            hover_x = hpad + 2.0 + self._hovered * seg_w
            hover_col = QColor(255, 255, 255, 15) if dark else QColor(0, 0, 0, 8)
            p.setBrush(QBrush(hover_col))
            p.drawRoundedRect(QRectF(hover_x, thumb_y, thumb_w, thumb_h), thumb_r, thumb_r)

        # Labels
        p.setPen(Qt.PenStyle.NoPen)
        label_font = qfont(TYPE.footnote)
        label_font.setWeight(QFont.Weight.Medium)
        p.setFont(label_font)

        for i, (oid, label) in enumerate(self.options):
            is_sel = (i == self._idx)
            seg_x  = i * seg_w
            text_col = QColor("#1C1C1E") if is_sel else (
                QColor("#AEAEB2") if dark else QColor("#6C6C70")
            )
            if is_sel:
                label_font.setWeight(QFont.Weight.DemiBold)
                p.setFont(label_font)
            else:
                label_font.setWeight(QFont.Weight.Medium)
                p.setFont(label_font)

            p.setPen(text_col)
            p.drawText(QRectF(seg_x, 0, seg_w, h), Qt.AlignmentFlag.AlignCenter, label)

        self._draw_focus_ring(p, QRectF(hpad, vpad, w - hpad * 2, h - vpad * 2), track_r)

    def sizeHint(self) -> QSize:
        return QSize(len(self.options) * 80, 30)


# ──────────────────────────────────────────────────────────────────────────────
# 6. HSlider — 28 px thumb, live value label, focus ring
# ──────────────────────────────────────────────────────────────────────────────

class HSlider(FocusRingMixin, QWidget):
    """
    Horizontal slider with a 28 px white thumb + soft shadow + focus ring.
    Emits valueChanged(float) on each tick.
    Optional unit suffix appended to the live label.
    """
    valueChanged = Signal(float)

    THUMB = SIZES.slider_thumb
    TRACK = SIZES.slider_track

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
        self.setFixedHeight(self.THUMB + 8)
        self.setMinimumWidth(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

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
        half_t = self.THUMB / 2.0
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
            self.setValue(self._x_from_pos(event.position().x()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag:
            self.setValue(self._x_from_pos(event.position().x()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag = False

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
        x0, x1, cy = self._track_rect()
        frac  = self._frac()
        thumb_cx = x0 + frac * (x1 - x0)
        half_th  = self.THUMB / 2.0
        half_tr  = self.TRACK / 2.0

        # Track background
        track_bg = QColor("#3A3A3C") if dark else QColor("#E5E5EA")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_bg))
        p.drawRoundedRect(QRectF(x0, cy - half_tr, x1 - x0, self.TRACK), half_tr, half_tr)

        # Fill portion
        accent = T.color.accent.resolve(dark)
        p.setBrush(QBrush(accent))
        fill_w = max(0.0, thumb_cx - x0)
        p.drawRoundedRect(QRectF(x0, cy - half_tr, fill_w, self.TRACK), half_tr, half_tr)

        # Thumb shadow
        p.setBrush(QBrush(QColor(0, 0, 0, 40)))
        p.drawEllipse(QPointF(thumb_cx, cy + 1.2), half_th, half_th)

        # Thumb
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.drawEllipse(QPointF(thumb_cx, cy), half_th, half_th)

        # Focus ring around thumb
        if self._has_focus:
            ring_c = QColor(accent.red(), accent.green(), accent.blue(), SIZES.focus_ring_a)
            p.setPen(QPen(ring_c, SIZES.focus_ring_w))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(thumb_cx, cy), half_th + SIZES.focus_ring_w, half_th + SIZES.focus_ring_w)


# ──────────────────────────────────────────────────────────────────────────────
# 7. GroupedCard
# ──────────────────────────────────────────────────────────────────────────────

class GroupedCard(QWidget):
    """
    Apple HIG System Settings inset grouped card.
    Paint is done in paintEvent using QPainter (not QSS) for squircle corners.
    Rows are separated by 1 px inset hairlines.
    Each row = left-side title+description stack, right-side control widget.
    Optional footnote text beneath the entire card.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._footnote: str   = ""
        self._footnote_label: Optional[QLabel] = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

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
            # Inset hairline divider
            div = QFrame(self._inner)
            div.setFixedHeight(1)
            # Hairline at ~8% alpha per spec
            dark = T.is_dark()
            div_color = "rgba(255, 255, 255, 20)" if dark else "rgba(0, 0, 0, 20)"
            div.setStyleSheet(f"background: {div_color}; border: none;")
            # Inset by left pad to match Apple's inset divider
            div_container = QWidget(self._inner)
            div_layout    = QHBoxLayout(div_container)
            div_layout.setContentsMargins(0, 0, 0, 0)
            div_layout.addSpacing(0)
            div_layout.addWidget(div)
            self._inner_layout.addWidget(div_container)

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
        lbl_title.setStyleSheet(f"color: {T.color.label.resolve(dark).name()};")
        text_col.addWidget(lbl_title)

        if description:
            lbl_desc = QLabel(description, row_widget)
            lbl_desc.setFont(qfont(TYPE.footnote))
            lbl_desc.setStyleSheet(f"color: {T.color.secondary_label.resolve(dark).name()};")
            lbl_desc.setWordWrap(True)
            text_col.addWidget(lbl_desc)

        row_layout.addLayout(text_col, stretch=1)

        if accessory:
            row_layout.addWidget(accessory)
        row_layout.addWidget(widget)

        self._inner_layout.addWidget(row_widget)
        self._rows.append({"title": title, "widget": widget})

    def set_footnote(self, text: str) -> None:
        """Add a footnote label beneath the card content."""
        self._footnote = text
        dark = T.is_dark()
        if self._footnote_label is None:
            self._footnote_label = QLabel(self)
            self._footnote_label.setFont(qfont(TYPE.footnote))
            self._footnote_label.setContentsMargins(SPACING.md, 0, SPACING.md, 0)
            self._footnote_label.setWordWrap(True)
            self._layout.addWidget(self._footnote_label)
        self._footnote_label.setText(text)
        self._footnote_label.setStyleSheet(f"color: {T.color.secondary_label.resolve(dark).name()}; font-size: 11px;")

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = T.is_dark()
        inner_geo = self._inner.geometry()
        rect = QRectF(inner_geo.x(), inner_geo.y(), inner_geo.width(), inner_geo.height())
        r    = float(RADIUS.card)

        # Shadow
        path = create_squircle_path(rect, r)
        draw_soft_shadow(p, path, SHADOW.card)

        # Card fill
        fill = T.color.card_background.resolve(dark)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill))
        p.drawPath(path)

        # Inner stroke (1 px hairline at ~8% alpha)
        stroke_col = QColor(255, 255, 255, 20) if dark else QColor(0, 0, 0, 14)
        p.setPen(QPen(stroke_col, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        inner_rect = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawPath(create_squircle_path(inner_rect, r - 0.5))

        # Top specular highlight
        spec_grad = QLinearGradient(0, rect.top(), 0, rect.top() + min(16.0, rect.height() * 0.3))
        spec_grad.setColorAt(0.0, QColor(255, 255, 255, 18 if dark else 50))
        spec_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(spec_grad))
        p.drawPath(create_squircle_path(rect.adjusted(1, 1, -1, rect.height()), r - 1))


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
# 9. Buttons
# ──────────────────────────────────────────────────────────────────────────────

class _BaseButton(FocusRingMixin, QWidget):
    """Internal base for all button variants."""
    clicked = Signal()

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
        self.setFixedHeight(height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        font = qfont(TYPE.body)
        font.setWeight(QFont.Weight.Medium)
        self._font = font

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

    def enterEvent(self, e) -> None:
        if not self._hovered:
            self._hovered = True
            self.update()

    def leaveEvent(self, e) -> None:
        if self._hovered or self._pressed:
            self._hovered = False
            self._pressed = False
            self.update()

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if self._enabled and e.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self._enabled and self._pressed:
            self.clicked.emit()
        self._pressed = False
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
        return QSize(text_w + SPACING.xl * 2, self.height())

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
    """Filled accent-color button (e.g., Save, Connect, Test)."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark   = T.is_dark()
        w, h   = float(self.width()), float(self.height())
        r      = float(RADIUS.control)
        rect   = QRectF(0, 0, w, h)
        path   = create_squircle_path(rect, r)
        accent = T.color.accent.resolve(dark)

        if not self._enabled:
            accent.setAlphaF(0.4)
        elif self._pressed:
            accent = accent.darker(120)
        elif self._hovered:
            accent = accent.lighter(110)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(accent))
        p.drawPath(path)

        text_col = QColor("#FFFFFF")
        if not self._enabled:
            text_col.setAlphaF(0.55)
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


class PlainButton(_BaseButton):
    """Tinted glass button (default secondary style)."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark  = T.is_dark()
        w, h  = float(self.width()), float(self.height())
        r     = float(RADIUS.control)
        rect  = QRectF(0, 0, w, h)
        path  = create_squircle_path(rect, r)

        if dark:
            base = QColor(255, 255, 255, 22 if not self._pressed else 12)
            if self._hovered and not self._pressed:
                base = QColor(255, 255, 255, 32)
        else:
            base = QColor(0, 0, 0, 14 if not self._pressed else 6)
            if self._hovered and not self._pressed:
                base = QColor(0, 0, 0, 22)

        if not self._enabled:
            base.setAlphaF(base.alphaF() * 0.5)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(base))
        p.drawPath(path)

        # Subtle border
        border = QColor(255, 255, 255, 18) if dark else QColor(0, 0, 0, 14)
        p.setPen(QPen(border, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(create_squircle_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), r))

        text_col = T.color.label.resolve(dark)
        if not self._enabled:
            text_col = T.color.tertiary_label.resolve(dark)
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


class DestructiveButton(_BaseButton):
    """Tinted destructive/danger button (red)."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark  = T.is_dark()
        w, h  = float(self.width()), float(self.height())
        r     = float(RADIUS.control)
        rect  = QRectF(0, 0, w, h)
        path  = create_squircle_path(rect, r)
        dest  = T.color.destructive.resolve(dark)

        if self._pressed:
            p.setBrush(QBrush(dest))
            text_col = QColor("#FFFFFF")
        elif self._hovered:
            tint = QColor(dest.red(), dest.green(), dest.blue(), 50)
            p.setBrush(QBrush(tint))
            text_col = dest
        else:
            tint = QColor(dest.red(), dest.green(), dest.blue(), 25)
            p.setBrush(QBrush(tint))
            text_col = dest

        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)
        border = QColor(dest.red(), dest.green(), dest.blue(), 80)
        p.setPen(QPen(border, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(create_squircle_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), r))

        if not self._enabled:
            text_col.setAlphaF(0.4)
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


class SuccessButton(_BaseButton):
    """Filled success/green button."""

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark    = T.is_dark()
        w, h    = float(self.width()), float(self.height())
        r       = float(RADIUS.control)
        rect    = QRectF(0, 0, w, h)
        path    = create_squircle_path(rect, r)
        success = T.color.success.resolve(dark)

        if not self._enabled:
            success.setAlphaF(0.4)
        elif self._pressed:
            success = success.darker(115)
        elif self._hovered:
            success = success.lighter(108)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(success))
        p.drawPath(path)

        text_col = QColor("#FFFFFF")
        if not self._enabled:
            text_col.setAlphaF(0.5)
        self._draw_label(p, text_col, rect)
        self._draw_focus_ring(p, rect, r)


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
            tc  = QColor("#FFFFFF")
        elif v == "warning":
            bg  = T.color.warning.resolve(dark)
            tc  = QColor("#000000") if not dark else QColor("#1C1C1E")
        elif v == "error":
            bg  = T.color.destructive.resolve(dark)
            tc  = QColor("#FFFFFF")
        elif v == "accent":
            bg  = T.color.accent.resolve(dark)
            tc  = QColor("#FFFFFF")
        else:
            bg  = QColor(120, 120, 128, 50)
            tc  = T.color.secondary_label.resolve(dark)

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
    content_layout.setSpacing(SPACING.lg)

    # Large Title
    lbl_title = QLabel(title)
    lbl_title.setFont(qfont(TYPE.large_title))
    content_layout.addWidget(lbl_title)

    if subtitle:
        lbl_sub = QLabel(subtitle)
        lbl_sub.setFont(qfont(TYPE.callout))
        content_layout.addWidget(lbl_sub)
        content_layout.addSpacing(SPACING.sm)

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
        f"color: {T.color.accent.resolve(dark).name()}; min-width: 56px;"
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
