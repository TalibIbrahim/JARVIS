"""
ui/tokens.py — J.A.R.V.I.S. Design Token System
Single source of truth for all colors, type scale, spacing, radii, shadows, and motion.

Philosophy:
  - Apple Human Interface Guidelines (macOS Sonoma / iOS 17) on Windows
  - Semantic roles, not raw values — consumers never reference hex directly
  - Per-monitor DPI aware: px values are logical pixels scaled at call-site
  - Light + Dark per token; High-contrast overrides in HC namespace
  - Neon cyan (#00E5FF) and gold (#FFC94D) appear ONLY in HUD_ONLY tokens

Usage:
    from ui.tokens import T, resolve

    dark = T.is_dark()           # detect current Windows theme
    c = T.label(dark)            # QColor for primary label text
    r = T.radius.card            # int — card corner radius
    f = T.type.headline          # TypeToken(family, size, weight, tracking)
    sp = T.spring.standard       # SpringToken(stiffness, damping)
"""

from __future__ import annotations

import sys
import math
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

try:
    from PySide6.QtGui import QColor, QFont
    _QT = True
except ImportError:
    _QT = False

# ──────────────────────────────────────────────
# 0. Grid & geometry constants
# ──────────────────────────────────────────────
GRID: int = 4          # 4-pt base grid unit
HAIRLINE: float = 1.0  # 1 px logical

# ──────────────────────────────────────────────
# 1. Radius tokens
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class RadiusTokens:
    window: int = 12
    card: int = 12
    control: int = 8
    capsule: int = 9999   # fully-pill
    island: int = 22      # Dynamic Island resting
    island_expanded: int = 16


RADIUS = RadiusTokens()


# ──────────────────────────────────────────────
# 2. Color tokens  (light / dark dicts → QColor)
# ──────────────────────────────────────────────
def _c(hex_str: str, alpha: int = 255) -> "QColor":
    """Return a QColor from a hex string with optional alpha (0-255)."""
    if not _QT:
        return hex_str  # fallback: return raw string
    col = QColor(hex_str)
    if alpha != 255:
        col.setAlpha(alpha)
    return col


@dataclass(frozen=True)
class _DualColor:
    """A semantic color with separate dark and light variants."""
    dark: "QColor"
    light: "QColor"
    hc_dark: "QColor"   = field(default=None)   # high-contrast override
    hc_light: "QColor"  = field(default=None)

    def resolve(self, is_dark: bool, high_contrast: bool = False) -> "QColor":
        if high_contrast:
            col = self.hc_dark if is_dark else self.hc_light
            if col is not None:
                return col
        return self.dark if is_dark else self.light


class ColorTokens:
    """
    Semantic color roles — access via T.color.<role>.resolve(dark_bool).
    Never add display-only (non-semantic) colors here unless they belong
    to the HUD_ONLY namespace.
    """

    # ── Labels ──────────────────────────────────────────────────────────
    label = _DualColor(
        dark=_c("#F5F5F7"),
        light=_c("#1C1C1E"),
        hc_dark=_c("#FFFFFF"),
        hc_light=_c("#000000"),
    )
    secondary_label = _DualColor(
        dark=_c("#AEAEB2"),
        light=_c("#6C6C70"),
        hc_dark=_c("#E0E0E0"),
        hc_light=_c("#3A3A3C"),
    )
    tertiary_label = _DualColor(
        dark=_c("#8E8E93"),
        light=_c("#8E8E93"),
        hc_dark=_c("#C4C4C6"),
        hc_light=_c("#5A5A5E"),
    )
    quaternary_label = _DualColor(
        dark=_c("#636366"),
        light=_c("#ADADB0"),
    )

    # ── Separator / hairline ────────────────────────────────────────────
    separator = _DualColor(
        dark=_c("#38383A"),
        light=_c("#C6C6C8"),
    )
    opaque_separator = _DualColor(
        dark=_c("#2C2C2E"),
        light=_c("#E5E5EA"),
    )

    # ── Fills (control backgrounds, input fields) ───────────────────────
    fill_primary = _DualColor(
        dark=_c("#787880", 91),    # ~36% alpha on dark
        light=_c("#78788028", 36),
    )
    fill_secondary = _DualColor(
        dark=_c("#78788051", 51),
        light=_c("#7878801E", 30),
    )
    fill_tertiary = _DualColor(
        dark=_c("#7878803B", 59),
        light=_c("#7878800F", 24),
    )

    # ── Surfaces ────────────────────────────────────────────────────────
    grouped_background = _DualColor(
        dark=_c("#1C1C1E"),
        light=_c("#F2F2F7"),
    )
    elevated_background = _DualColor(
        dark=_c("#2C2C2E"),
        light=_c("#FFFFFF"),
    )
    card_background = _DualColor(
        dark=_c("#1C1C1E", 204),   # 80% opaque
        light=_c("#FFFFFF", 229),  # 90% opaque
    )
    sidebar_background = _DualColor(
        dark=_c("#18181B"),
        light=_c("#E9E9EE"),
    )

    # ── Accent system ────────────────────────────────────────────────────
    accent = _DualColor(
        dark=_c("#0A84FF"),
        light=_c("#007AFF"),
        hc_dark=_c("#409CFF"),
        hc_light=_c("#0040DD"),
    )
    accent_label = _DualColor(   # text on accent backgrounds
        dark=_c("#FFFFFF"),
        light=_c("#FFFFFF"),
    )

    # ── Semantic states ──────────────────────────────────────────────────
    success = _DualColor(
        dark=_c("#30D158"),
        light=_c("#34C759"),
        hc_dark=_c("#30E46E"),
        hc_light=_c("#248A3D"),
    )
    warning = _DualColor(
        dark=_c("#FF9F0A"),
        light=_c("#FF9500"),
        hc_dark=_c("#FFD60A"),
        hc_light=_c("#C93400"),
    )
    destructive = _DualColor(
        dark=_c("#FF453A"),
        light=_c("#FF3B30"),
        hc_dark=_c("#FF6961"),
        hc_light=_c("#D70015"),
    )

    # ── Glass / material helpers ─────────────────────────────────────────
    glass_tint_dark   = _c("#1A1A1EE0", 224)   # 88% alpha
    glass_tint_light  = _c("#F0F0F5E8", 232)   # 91% alpha
    glass_specular    = _c("#FFFFFF1E", 30)      # top highlight
    glass_inner_dark  = _c("#FFFFFF42", 66)      # 26% white stroke
    glass_inner_light = _c("#00000014", 20)      # 8% black stroke

    # ── HUD_ONLY — restricted to Island / CursorFeedbackOverlay ─────────
    # DO NOT USE in SettingsWindow or any static UI
    class HUD_ONLY:
        neon_cyan  = _c("#00E5FF")
        neon_cyan_dim = _c("#00E5FF", 160)
        stark_gold = _c("#FFC94D")
        stark_gold_dim = _c("#FFC94D", 160)
        waveform_purple = _c("#AF52DE", 200)
        waveform_white  = _c("#FFFFFFFF", 230)


COLOR = ColorTokens()


# ──────────────────────────────────────────────
# 3. Type-scale tokens
# ──────────────────────────────────────────────
class TypeToken(NamedTuple):
    family:   str
    size:     int          # logical px
    weight:   int          # QFont.Weight int value (400=Normal, 600=DemiBold, 700=Bold)
    tracking: float = 0.0  # letter-spacing in em units
    tabular:  bool  = False

    def font(self) -> "QFont":
        """Return a QFont for this token, if PySide6 is available."""
        if not _QT:
            return None
        f = QFont(self.family)
        f.setPixelSize(self.size)
        f.setWeight(QFont.Weight(self.weight))
        if self.tabular:
            f.setFeature(b"tnum", 1)   # Qt 6.7+ font feature
        return f

    def family_fallback(self) -> str:
        return self.family


# Font family precedence: prefer Inter if installed, fall back to Segoe UI Variable, then Segoe UI
_FONT = "Inter"  # Users can install Inter from assets/; runtime fallback is automatic via Qt


class TypeScale:
    large_title = TypeToken(_FONT, 26, 600, -0.025)
    title       = TypeToken(_FONT, 20, 600, -0.015)
    title_2     = TypeToken(_FONT, 17, 600,  0.0)
    title_3     = TypeToken(_FONT, 15, 600,  0.0)
    headline    = TypeToken(_FONT, 15, 600,  0.0)
    body        = TypeToken(_FONT, 13, 400,  0.0)
    callout     = TypeToken(_FONT, 12, 400,  0.0)
    subheadline = TypeToken(_FONT, 11, 400,  0.0)
    footnote    = TypeToken(_FONT, 11, 400,  0.0)
    caption_1   = TypeToken(_FONT, 11, 400,  0.0)
    caption_2   = TypeToken(_FONT, 10, 400,  0.0)
    # Live-metric variant: tabular numerals for FPS, RMS etc.
    metric      = TypeToken(_FONT, 11, 600,  0.0, tabular=True)
    metric_lg   = TypeToken(_FONT, 13, 600,  0.0, tabular=True)


TYPE = TypeScale()


def qfont(token: TypeToken, scale: float = 1.0) -> "QFont":
    """
    Return a QFont from a TypeToken. ``scale`` is a DPI scale factor
    (e.g. 1.5 at 150% DPI); pixel sizes are pre-multiplied.
    """
    if not _QT:
        return None
    f = QFont()
    # Fallback font family chain
    families = [token.family, "Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "SF Pro Text", "-apple-system", "Helvetica Neue", "Arial"]
    f.setFamilies(families)
    f.setPixelSize(max(1, round(token.size * scale)))
    f.setWeight(QFont.Weight(token.weight))
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    if token.tabular:
        try:
            f.setFeature(b"tnum", 1)
        except Exception:
            pass  # older Qt; ignore silently
    return f


# ──────────────────────────────────────────────
# 4. Spacing tokens (multiples of GRID = 4 pt)
# ──────────────────────────────────────────────
class SpacingTokens:
    xxs  = GRID * 1     #  4
    xs   = GRID * 2     #  8
    sm   = GRID * 3     # 12
    md   = GRID * 4     # 16
    lg   = GRID * 5     # 20
    xl   = GRID * 6     # 24
    xxl  = GRID * 8     # 32
    xxxl = GRID * 10    # 40


SPACING = SpacingTokens()


# ──────────────────────────────────────────────
# 5. Shadow tokens
# ──────────────────────────────────────────────
class ShadowLayer(NamedTuple):
    offset_y: float      # px
    blur:     float      # px  (not native Qt; used by custom QPainter approach)
    alpha:    int        # 0-255

class ShadowTokens:
    """Two stacked soft layers: tight (close) + wide (ambient)."""
    card  = (ShadowLayer(1.5, 4.0, 25),  ShadowLayer(4.0, 16.0, 18))
    popup = (ShadowLayer(2.0, 8.0, 40),  ShadowLayer(8.0, 32.0, 28))
    island= (ShadowLayer(2.0, 6.0, 22),  ShadowLayer(5.0, 20.0, 18))
    none  = ()

SHADOW = ShadowTokens()


# ──────────────────────────────────────────────
# 6. Motion / Spring tokens
# ──────────────────────────────────────────────
class SpringToken(NamedTuple):
    stiffness: float
    damping:   float
    # Approximate response time:  τ ≈ 2*damping / stiffness
    # Standard (0.35 s): k=120, c=14 → τ≈0.23 s, well-damped
    # Fast     (0.20 s): k=220, c=20 → τ≈0.18 s
    # Slow     (0.50 s): k=60,  c=10 → τ≈0.33 s

class SpringPresets:
    standard    = SpringToken(120.0, 14.0)    # Island size, Settings panels
    fast        = SpringToken(220.0, 20.0)    # Switch thumb, segmented thumb
    slow        = SpringToken(60.0,  10.0)    # Page cross-fade opacity
    island      = SpringToken(190.0, 22.0)    # Current island spring (unchanged)
    dwell       = SpringToken(240.0, 24.0)    # Dwell progress arc
    hover       = SpringToken(300.0, 26.0)    # Hover highlight underlay

SPRING = SpringPresets()

# Reduce-motion crossfade duration (ms)
REDUCE_MOTION_MS: int = 120

# Max spring dt clamping (numerical stability)
SPRING_DT_MIN: float = 1e-4
SPRING_DT_MAX: float = 0.05


# ──────────────────────────────────────────────
# 7. Control size constants
# ──────────────────────────────────────────────
class ControlSizes:
    switch_w:      int = 51
    switch_h:      int = 31
    switch_thumb:  int = 27     # diameter
    slider_thumb:  int = 28     # diameter
    slider_track:  int = 4      # track height
    sidebar_item:  int = 36     # sidebar row height
    sidebar_icon:  int = 22     # icon tile size
    sidebar_w:     int = 220    # sidebar width
    row_height:    int = 52     # settings row min-height
    focus_ring_w:  int = 3      # focus ring border width
    focus_ring_a:  int = 102    # focus ring alpha (40%)

SIZES = ControlSizes()


# ──────────────────────────────────────────────
# 8. System theme detection
# ──────────────────────────────────────────────
def is_dark_mode() -> bool:
    """Detect Windows Light/Dark mode from registry. Returns True for dark."""
    if sys.platform != "win32":
        return True
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return val == 0   # 0 = dark apps theme
    except Exception:
        return True


def is_high_contrast() -> bool:
    """Detect Windows High Contrast mode."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        class HIGHCONTRAST(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwFlags", ctypes.c_uint),
                ("lpszDefaultScheme", ctypes.c_wchar_p),
            ]
        hc = HIGHCONTRAST()
        hc.cbSize = ctypes.sizeof(hc)
        ctypes.windll.user32.SystemParametersInfoW(0x0042, hc.cbSize, ctypes.byref(hc), 0)
        return bool(hc.dwFlags & 0x1)   # HCF_HIGHCONTRASTON
    except Exception:
        return False


def is_reduce_motion() -> bool:
    """Detect Windows 'Show animations in Windows' setting (inverted = reduce)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        pv = ctypes.c_bool(False)
        # SPI_GETCLIENTAREAANIMATION = 0x1042
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(pv), 0)
        return not pv.value   # False means animations are OFF → reduce motion
    except Exception:
        return False


def windows_accent_color() -> "QColor":
    """Return the current Windows accent color as a QColor."""
    if sys.platform != "win32" or not _QT:
        return _c("#007AFF")
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        val, _ = winreg.QueryValueEx(key, "AccentColor")
        winreg.CloseKey(key)
        b = (val >> 16) & 0xFF
        g = (val >> 8) & 0xFF
        r = val & 0xFF
        return QColor(r, g, b)
    except Exception:
        return _c("#007AFF")


# ──────────────────────────────────────────────
# 9. Squircle path helper (pure math, no Qt dep)
# ──────────────────────────────────────────────
def squircle_bezier_factor(radius: float) -> float:
    """
    Bézier control-point factor for G² continuous curvature (superellipse).
    Apple uses ≈0.5523 rather than the circular 0.5523 to give tighter corners.
    Empirically Apple's value ≈ 0.552284 gives near-perfect superellipse match.
    """
    return radius * 0.552284


# ──────────────────────────────────────────────
# 10. Convenience accessor namespace
# ──────────────────────────────────────────────
class T:
    """
    Single import target:  ``from ui.tokens import T``
    Then:
        T.color.label.resolve(dark)
        T.type.headline.font()
        T.spring.standard
        T.radius.card
        T.spacing.md
        T.sizes.switch_w
        T.is_dark()
        T.reduce_motion()
    """
    color   = COLOR
    type    = TYPE
    spring  = SPRING
    radius  = RADIUS
    spacing = SPACING
    shadow  = SHADOW
    sizes   = SIZES
    grid    = GRID

    @staticmethod
    def is_dark() -> bool:
        return is_dark_mode()

    @staticmethod
    def high_contrast() -> bool:
        return is_high_contrast()

    @staticmethod
    def reduce_motion() -> bool:
        return is_reduce_motion()

    @staticmethod
    def accent(dark: Optional[bool] = None) -> "QColor":
        d = dark if dark is not None else is_dark_mode()
        return COLOR.accent.resolve(d)

    @staticmethod
    def label(dark: Optional[bool] = None) -> "QColor":
        d = dark if dark is not None else is_dark_mode()
        return COLOR.label.resolve(d)

    @staticmethod
    def secondary_label(dark: Optional[bool] = None) -> "QColor":
        d = dark if dark is not None else is_dark_mode()
        return COLOR.secondary_label.resolve(d)

    @staticmethod
    def font(token: TypeToken, dpi_scale: float = 1.0) -> "QFont":
        return qfont(token, dpi_scale)
