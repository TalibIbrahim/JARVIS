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
    from PySide6.QtGui import QColor, QFont, QLinearGradient
    _QT = True
    if not hasattr(QColor, "resolve"):
        QColor.resolve = lambda self, is_dark=True, high_contrast=False: self
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
    # Preserved backward-compatible tokens
    window: int = 12
    card: int = 12
    control: int = 8
    capsule: int = 9999   # fully-pill
    island: int = 22      # Dynamic Island resting
    island_expanded: int = 16

    # Dynamic Island Radii per MEASUREMENT_SHEET.md
    compact_pill: int = 18       # Stadium radius (H / 2 = 36 / 2 = 18 px)
    pill: int = 18               # alias
    expanded_card: int = 47      # Squircle radius ~25% card height (188 * 0.25 = 47 px)
    card_expanded: int = 47      # alias
    inner_tile: int = 17         # Inner tile squircle radius ~9% card height (17 px)
    tile: int = 17               # alias
    hardware_strip: int = 28     # Dedicated telemetry strip squircle radius (28 px)
    hardware: int = 28           # alias

    # Dynamic Island state-specific capsule radii
    wake_capsule: int = 22
    listening: int = 27
    thinking: int = 23
    prompt: int = 25
    speaking: int = 25
    result: int = 29
    hud_volume: int = 19
    hud_window: int = 19


RADIUS = RadiusTokens()


# ──────────────────────────────────────────────
# 1.5 Geometry & Scale presets
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class IslandSize:
    """Dimensions (width, height) and corner radius for an Island layout state."""
    width: float
    height: float
    radius: float

    # Support (w, h) tuple unpacking: w, h = size
    def __iter__(self):
        yield self.width
        yield self.height

    def __getitem__(self, idx: int) -> float:
        return (self.width, self.height, self.radius)[idx]

    def __len__(self) -> int:
        return 2

    @property
    def w(self) -> float:
        return self.width

    @property
    def h(self) -> float:
        return self.height

    @property
    def r(self) -> float:
        return self.radius

    def scaled(self, factor: float) -> "IslandSize":
        """Return scaled dimensions given a size scale preset (e.g. 0.85, 1.0, 1.15)."""
        return IslandSize(
            width=round(self.width * factor, 1),
            height=round(self.height * factor, 1),
            radius=round(self.radius * factor, 1),
        )


@dataclass(frozen=True)
class GeometryTokens:
    """Dynamic Island default geometries at 100% scale per MEASUREMENT_SHEET.md."""
    # Primary capsule layouts (100% scale)
    compact_pill: IslandSize = IslandSize(175.0, 36.0, 18.0)
    expanded_card: IslandSize = IslandSize(390.0, 188.0, 47.0)
    inner_tile: IslandSize = IslandSize(114.0, 68.0, 17.0)
    hardware_strip: IslandSize = IslandSize(446.0, 69.0, 28.0)

    # Assistant & HUD states
    idle: IslandSize = IslandSize(175.0, 36.0, 18.0)
    wake: IslandSize = IslandSize(280.0, 44.0, 22.0)
    listening: IslandSize = IslandSize(390.0, 54.0, 27.0)
    thinking: IslandSize = IslandSize(320.0, 46.0, 23.0)
    prompt: IslandSize = IslandSize(380.0, 50.0, 25.0)
    speaking: IslandSize = IslandSize(380.0, 50.0, 25.0)
    result: IslandSize = IslandSize(380.0, 58.0, 29.0)
    hud_volume: IslandSize = IslandSize(260.0, 38.0, 19.0)
    hud_window: IslandSize = IslandSize(290.0, 38.0, 19.0)

    # Modular inner tiles
    tile_2x3: IslandSize = IslandSize(114.0, 68.0, 17.0)
    weather_hourly_tile: IslandSize = IslandSize(366.0, 72.0, 17.0)
    calendar_schedule_tile: IslandSize = IslandSize(200.0, 140.0, 17.0)


@dataclass(frozen=True)
class ScalePresets:
    """Size scale presets for Dynamic Island scaling."""
    small: float = 0.85
    medium: float = 1.00   # default
    large: float = 1.15

    SMALL: float = 0.85
    MEDIUM: float = 1.00
    LARGE: float = 1.15


GEOMETRY = GeometryTokens()
SCALE = ScalePresets()


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

    # ── Dynamic Island Obsidian Surface & Borders ───────────────────────
    surface_top     = _c("#131617")        # Linear gradient top: deep obsidian
    surface_bottom  = _c("#0A0A0A")        # Linear gradient bottom: pure near-black
    capsule_border  = _c("#FFFFFF", 15)    # 1px hairline border: #FFFFFF at 6% alpha (rgba(255, 255, 255, 0.06), #FFFFFF0F)
    tile_fill       = _c("#FFFFFF", 23)    # Inner tiles: fill #FFFFFF at 9% alpha (rgba(255, 255, 255, 0.09), #FFFFFF17)
    tile_border     = _c("#FFFFFF", 10)    # Inner tiles: border 1px #FFFFFF at 4% alpha (rgba(255, 255, 255, 0.04), #FFFFFF0A)
    sheen_top       = _c("#FFFFFF", 31)    # Specular sheen top: 12% alpha highlight (#FFFFFF)
    sheen_bottom    = _c("#FFFFFF", 0)     # Specular sheen end: 0% alpha highlight (#FFFFFF)

    # ── Dynamic Island Palette & Accents ────────────────────────────────
    accent_blue     = _c("#45C1EE")        # Accent Sky Blue: Media scrubber, cool weather, primary highlights
    accent_sky_blue = _c("#45C1EE")        # Alias
    accent_amber    = _c("#F5A524")        # Accent Amber: RAM load warning, mid-temps, battery standby
    accent_red      = _c("#F0443A")        # Accent Red: Errors, critical temps >80°C, mic mute
    accent_green    = _c("#34C759")        # Accent Green: Active tracking, media playing, normal temps
    hud_cyan        = _c("#00E5FF")        # Cyan: Dynamic Island wake pulse, mini wireframe HUD
    repulsor_gold   = _c("#FFC94D")        # Gold: Dwell hover ring, confirmation ticks

    # ── Dynamic Island Text & Label Hierarchy ───────────────────────────
    label_primary   = _c("#F5F5F7")        # Primary text, heavy numerals, values (#F5F5F7)
    label_uppercase = _c("#8E9094")        # Uppercase / labels: tile headers, section titles (#8E9094)
    label_secondary = _c("#55585C")        # Secondary text: subtitles, timestamps, specs (#55585C)

    # ── HUD_ONLY — restricted to Island / CursorFeedbackOverlay ─────────
    # DO NOT USE in SettingsWindow or any static UI
    class HUD_ONLY:
        neon_cyan  = _c("#00E5FF")
        neon_cyan_dim = _c("#00E5FF", 160)
        stark_gold = _c("#FFC94D")
        stark_gold_dim = _c("#FFC94D", 160)
        waveform_purple = _c("#AF52DE", 200)
        waveform_white  = _c("#FFFFFFFF", 230)
        sky_blue        = _c("#45C1EE")
        accent_blue     = _c("#45C1EE")
        accent_amber    = _c("#F5A524")
        accent_red      = _c("#F0443A")
        accent_green    = _c("#34C759")


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
    metric          = TypeToken(_FONT, 11, 600,  0.0, tabular=True)
    metric_lg       = TypeToken(_FONT, 13, 600,  0.0, tabular=True)

    # Dynamic Island type scale per MEASUREMENT_SHEET.md
    heavy_numeral   = TypeToken(_FONT, 36, 700, -0.02, tabular=True)   # 32pt/36px Heavy
    card_hero       = TypeToken(_FONT, 24, 700, -0.01, tabular=True)   # 20pt/24px Bold
    semibold_value  = TypeToken(_FONT, 17, 600,  0.0)                  # 14pt/17px SemiBold
    uppercase_label = TypeToken(_FONT, 11, 600,  0.06)                 # 9pt/11px All Caps
    secondary_text  = TypeToken(_FONT, 13, 400,  0.0)                  # 11pt/13px Regular
    attribution     = TypeToken(_FONT, 10, 400,  0.0)                  # 9pt/10px Regular


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
    """Multi-layer soft shadow tokens."""
    card  = (ShadowLayer(1.5, 4.0, 25),  ShadowLayer(4.0, 16.0, 18))
    popup = (ShadowLayer(2.0, 8.0, 40),  ShadowLayer(8.0, 32.0, 28))
    # Dynamic Island 3-layer drop shadow per MEASUREMENT_SHEET.md:
    # Layer 1 (Tight): dy = +2px, blur = 6px, alpha = 12% (#0000001F)
    # Layer 2 (Ambient): dy = +5px, blur = 16px, alpha = 18% (#0000002E)
    # Layer 3 (Deep): dy = +10px, blur = 28px, alpha = 25% (#00000040)
    island = (
        ShadowLayer(2.0, 6.0, 31),
        ShadowLayer(5.0, 16.0, 46),
        ShadowLayer(10.0, 28.0, 64),
    )
    island_tight   = ShadowLayer(2.0, 6.0, 31)
    island_ambient = ShadowLayer(5.0, 16.0, 46)
    island_deep    = ShadowLayer(10.0, 28.0, 64)
    none  = ()

SHADOW = ShadowTokens()


# ──────────────────────────────────────────────
# 6. Motion / Spring tokens
# ──────────────────────────────────────────────
class SpringToken(NamedTuple):
    stiffness: float
    damping:   float

    @property
    def k(self) -> float:
        return self.stiffness

    @property
    def c(self) -> float:
        return self.damping

    @property
    def omega(self) -> float:
        return math.sqrt(self.stiffness)

    @property
    def zeta(self) -> float:
        return self.damping / (2.0 * math.sqrt(self.stiffness))

    @property
    def period(self) -> float:
        return 2.0 * math.pi / math.sqrt(self.stiffness)


class SpringPresets:
    """
    Critically-damped harmonic spring presets per MEASUREMENT_SHEET.md:
    F = -k(x - target) - c*v
    """
    standard    = SpringToken(246.74, 22.0)   # τ≈0.40s, ζ=0.70 (Apple HIG standard expansion)
    fast        = SpringToken(300.0,  26.0)   # τ≈0.36s, ζ=0.75 (Button press, switch toggle, scrubber)
    slow        = SpringToken(120.0,  16.0)   # τ≈0.57s, ζ=0.73 (Card page switch crossfade)
    hud_pulse   = SpringToken(190.0,  18.0)   # τ≈0.45s, ζ=0.65 (Acoustic wake aura pulse, volume notches)

    # Legacy & specialized presets preserved for backward compatibility
    island      = SpringToken(246.74, 22.0)   # Dynamic Island expansion
    island_legacy = SpringToken(190.0, 22.0)  # Legacy island preset
    dwell       = SpringToken(240.0,  24.0)   # Dwell progress arc
    hover       = SpringToken(300.0,  26.0)   # Hover highlight underlay

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

    # Dynamic Island primary dimensions at 100% scale
    compact_pill_w:    int = 175
    compact_pill_h:    int = 36
    expanded_card_w:   int = 390
    expanded_card_h:   int = 188
    hardware_strip_w:  int = 446
    hardware_strip_h:  int = 69
    scale_small:       float = 0.85
    scale_medium:      float = 1.00
    scale_large:       float = 1.15

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
    color    = COLOR
    type     = TYPE
    spring   = SPRING
    radius   = RADIUS
    spacing  = SPACING
    shadow   = SHADOW
    sizes    = SIZES
    grid     = GRID
    geometry = GEOMETRY
    scale    = SCALE

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

    @staticmethod
    def capsule_gradient(height: float = 1.0) -> "QLinearGradient":
        """Linear vertical gradient for Dynamic Island capsule: surface_top -> surface_bottom."""
        if not _QT:
            return None
        grad = QLinearGradient(0.0, 0.0, 0.0, float(height))
        grad.setColorAt(0.0, COLOR.surface_top)
        grad.setColorAt(1.0, COLOR.surface_bottom)
        return grad

    @staticmethod
    def specular_gradient(height: float = 1.0) -> "QLinearGradient":
        """Top 40% specular sheen gradient: 12% alpha white -> 0% alpha white."""
        if not _QT:
            return None
        grad = QLinearGradient(0.0, 0.0, 0.0, float(height) * 0.40)
        grad.setColorAt(0.0, COLOR.sheen_top)
        grad.setColorAt(1.0, COLOR.sheen_bottom)
        return grad
