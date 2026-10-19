"""
ui/tokens.py — J.A.R.V.I.S. Design Token System
Single source of truth for all colors, type scale, spacing, radii, shadows, and motion.

Philosophy:
  - Apple HIG, Liquid Glass (iOS 26–27 / macOS 26–27), on Windows
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
    expanded_card: float = 44.5  # MEASUREMENT_SHEET_v2: 24% of H=188, continuous (squircle) corner
    card_expanded: float = 44.5  # alias
    inner_tile: float = 9.0      # MEASUREMENT_SHEET_v2: inner tiles 8.8-9.4
    tile: float = 9.0            # alias
    calendar_panel: float = 11.8 # calendar left panel (8% of its height)
    media_art: float = 15.4      # media album art (24% of 65.5)
    hardware_strip: float = 34.5 # strip = H/2 (full stadium)
    hardware: float = 34.5       # alias

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
    expanded_card: IslandSize = IslandSize(390.0, 188.0, 44.5)
    inner_tile: IslandSize = IslandSize(114.0, 68.0, 17.0)
    hardware_strip: IslandSize = IslandSize(446.0, 69.0, 34.5)

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
    surface_top     = _c("#11191B")        # MEASUREMENT_SHEET_v2 gradient top (teal-tinted)
    surface_bottom  = _c("#0C0C0C")        # MEASUREMENT_SHEET_v2 gradient bottom
    capsule_border  = _c("#FFFFFF", 13)    # 1px hairline: white 5%
    tile_fill       = _c("#FFFFFF", 20)    # Inner tiles: white 8%
    tile_border     = _c("#FFFFFF", 0)     # Inner tiles: no visible stroke in the reference
    sheen_top       = _c("#FFFFFF", 0)     # No specular sheen in the reference
    sheen_bottom    = _c("#FFFFFF", 0)     # Specular sheen end: 0% alpha highlight (#FFFFFF)

    # ── Dynamic Island Palette & Accents ────────────────────────────────
    accent_blue     = _c("#4FC2E8")        # Accent blue (sheet v2)
    accent_sky_blue = _c("#4FC2E8")        # Alias
    accent_amber    = _c("#EBAB38")        # Accent amber (sheet v2)
    accent_red      = _c("#E1605A")        # Accent red (sheet v2)
    accent_green    = _c("#38C555")        # Accent green (sheet v2)
    hud_cyan        = _c("#00E5FF")        # Cyan: Dynamic Island wake pulse, mini wireframe HUD
    repulsor_gold   = _c("#FFC94D")        # Gold: Dwell hover ring, confirmation ticks

    # ── Dynamic Island Text & Label Hierarchy ───────────────────────────
    label_primary   = _c("#FAFAFA")        # Value white (sheet v2)
    label_uppercase = _c("#6B6F72")        # Tile labels (sheet v2)
    label_secondary = _c("#828282")        # Secondary text: artist, times, weekday (sheet v2)

    # ── HUD_ONLY — restricted to Island / CursorFeedbackOverlay ─────────
    # DO NOT USE in SettingsWindow or any static UI
    class HUD_ONLY:
        neon_cyan  = _c("#00E5FF")
        neon_cyan_dim = _c("#00E5FF", 160)
        stark_gold = _c("#FFC94D")
        stark_gold_dim = _c("#FFC94D", 160)
        waveform_purple = _c("#AF52DE", 200)
        waveform_white  = _c("#FFFFFFFF", 230)
        sky_blue        = _c("#4FC2E8")
        accent_blue     = _c("#4FC2E8")
        accent_amber    = _c("#EBAB38")
        accent_red      = _c("#E1605A")
        accent_green    = _c("#38C555")


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
_ISLAND_FONT = "Segoe UI Variable Text"       # island v2 roles (falls back to Segoe UI on Windows 10)
_ISLAND_DISPLAY = "Segoe UI Variable Display"
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

    # Island v2 roles (MEASUREMENT_SHEET_v2 section 4). Segoe UI Variable first, then the qfont() chain.
    pill_time       = TypeToken(_ISLAND_FONT, 14, 600)
    pill_temp       = TypeToken(_ISLAND_FONT, 14, 600)
    hw_title        = TypeToken(_ISLAND_FONT, 13, 700)                     # sheet 12.5
    tile_label      = TypeToken(_ISLAND_FONT, 11, 400)
    hw_value        = TypeToken(_ISLAND_FONT, 14, 700, 0.0, tabular=True)
    strip_value     = TypeToken(_ISLAND_FONT, 13, 700, 0.0, tabular=True)
    strip_fps       = TypeToken(_ISLAND_FONT, 18, 700, 0.0, tabular=True)
    weather_city    = TypeToken(_ISLAND_FONT, 11, 400)
    weather_temp    = TypeToken(_ISLAND_DISPLAY, 45, 700)
    weather_cond    = TypeToken(_ISLAND_FONT, 14, 600)
    weather_value   = TypeToken(_ISLAND_FONT, 14, 600)
    cal_month       = TypeToken(_ISLAND_FONT, 13, 700)
    cal_year        = TypeToken(_ISLAND_FONT, 11, 400)
    cal_day         = TypeToken(_ISLAND_DISPLAY, 66, 700, 0.0, tabular=True)
    cal_weekday     = TypeToken(_ISLAND_FONT, 13, 700)
    cal_dow         = TypeToken(_ISLAND_FONT, 15, 700)
    cal_digit       = TypeToken(_ISLAND_FONT, 15, 700, 0.0, tabular=True)
    media_title     = TypeToken(_ISLAND_FONT, 14, 600)
    media_artist    = TypeToken(_ISLAND_FONT, 12, 400)
    media_album     = TypeToken(_ISLAND_FONT, 12, 400)
    media_time      = TypeToken(_ISLAND_FONT, 11, 400, 0.0, tabular=True)


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
# 9.5 Island v2 tokens (docs/reference/MEASUREMENT_SHEET_v2.md, all values 1x logical px)
# ──────────────────────────────────────────────
def _white(alpha: float) -> "QColor":
    """White at a fractional alpha (0..1)."""
    return _c("#FFFFFF", int(round(alpha * 255)))


# Continuous (Apple-style, Figma "corner smoothing") corner construction: see ui.components.
CORNER_SMOOTHING: float = 0.6

# Transparent window margin that holds the outside shadow (logical px at size scale 1: 3 sigma + dy of the
# card shadow = 3 * 8.5 + 6 = 31.5 -> 32). The island window is the island rect grown by this on every side
# (scaled by the island size scale; Qt logical px already follow the monitor DPI).
SHELL_PAD: float = 32.0


class SurfaceShadow(NamedTuple):
    alpha: float     # peak alpha 0..1
    sigma: float     # gaussian sigma, px
    dy: float        # vertical offset, px


class IslandColors:
    """Measured island colours (sheet sections 1, 5, 7)."""
    surface_top      = _c("#11191B")
    surface_bottom   = _c("#0C0C0C")
    hairline         = _white(0.05)
    tile_fill        = _white(0.08)
    divider          = _white(0.10)
    track            = _white(0.16)
    accent           = _c("#4FC2E8")
    amber            = _c("#EBAB38")
    red              = _c("#E1605A")
    green            = _c("#38C555")
    value            = _c("#FAFAFA")
    label            = _c("#6B6F72")
    secondary        = _c("#828282")
    dim              = _c("#6B6E72")
    dow              = _c("#60666A")       # calendar weekday header
    digit            = _c("#E6E9EA")       # calendar in-month digit
    white            = _c("#FFFFFF")
    # Media
    badge_fill       = _c("#0F1018")
    media_neutral    = _c("#C8CCD0")       # tint used when there is no album art
    bg_neutral       = _c("#0C0C0C")       # tinted background fades to this by 75% height
    # Brand marks drawn in the media source badge (own vector drawings)
    spotify_green    = _c("#1ED760")
    chrome_red       = _c("#EA4335")
    chrome_yellow    = _c("#FBBC05")
    chrome_green     = _c("#34A853")
    chrome_blue      = _c("#4285F4")
    # Page dots
    dot_inactive     = _white(0.25)
    dot_active       = _white(0.95)
    # Media buttons (never tinted)
    btn_side_fill    = _white(0.037)
    btn_play_fill    = _white(0.083)


class IslandTint:
    """Media tint rules (sheet section 7). Derived from the art dominant colour T."""
    bg_value         = 0.115     # V of the tinted gradient at the card edge
    bg_saturation    = 0.62      # x S of T (sheet: ~0.8x of the HSV-S of its measured colours; fitted against samples)
    bg_stops         = ((0.0, 0.0), (0.50, 0.35), (0.70, 0.76), (0.75, 1.0))   # (y fraction, blend toward neutral)
    glow_alpha       = 0.045     # top-centre elliptical glow (T over the gradient)
    glow_ry          = 0.45      # vertical radius of the glow relative to its horizontal radius
    scrub_start_lift = 0.25      # scrubber fill: T lightened 25% at the start
    knob_ring_lift   = 0.50
    pause_alpha      = 0.88
    side_glyph_alpha = 0.62
    wave_alpha       = 0.75
    title_mix        = 0.05      # title: white lerped 5% toward T
    text_mix         = 0.04      # secondary text cast
    min_contrast     = 4.5


class IslandShadows:
    card = SurfaceShadow(0.5, 8.5, 6.0)
    pill = SurfaceShadow(0.3, 4.0, 3.0)


class PillLayout:
    w, h = 175.0, 36.0
    time_x = 21.7
    divider_w, divider_h = 1.0, 18.3
    glyph_w, glyph_h = 16.0, 14.0
    glyph_gap = 10.7            # glyph right edge -> temperature
    right_pad = 25.3            # temperature right edge -> pill right edge
    dot_d = 8.0
    dot_gap = 9.4               # temperature -> dot
    dot_right = 15.7            # dot right edge -> pill right edge
    text_nudge = -0.5           # optical vertical offset of the pill text (reference cap centre sits 0.5 px high)


class MediaLayout:
    w, h = 390.0, 188.0
    art_x, art_y, art_size, art_radius = 24.5, 20.7, 65.5, 15.4
    badge_d, badge_inset = 19.0, 4.0
    text_x = 109.7
    title_baseline, artist_baseline, album_baseline = 48.9, 69.0, 83.5
    text_wave_gap = 14.0
    wave_x, wave_bar_w, wave_pitch, wave_cy = 332.0, 2.5, 5.0, 53.45
    wave_heights = (12.5, 7.5, 16.3, 10.0, 15.0, 6.3, 11.3)
    scrub_x, scrub_w, scrub_cy, scrub_h = 74.0, 241.4, 116.3, 4.5
    knob_d, knob_core_d = 10.0, 7.5
    time_left_x, time_right_edge, time_baseline = 47.0, 348.0, 119.7
    ctrl_cy = 151.7
    prev_cx, play_cx, next_cx = 128.8, 195.3, 259.9
    side_d, play_d = 33.0, 46.4
    side_glyph_w, side_glyph_h = 11.0, 13.0
    pause_bar_w, pause_gap, pause_h = 3.8, 3.8, 16.0
    hit_side_r, hit_play_r = 22.0, 26.0     # click / dwell targets (larger than the drawn discs)


class LyricsLayout:
    """Lyrics strip (media card) and lyric line (compact media pill). Card-origin 1x px.
    The strip takes the album line's slot (card stays 390 x 188): current line 13 semibold white, next line
    12 regular label grey, in the title column. The toggle glyph sits under the waveform."""
    x = 109.7                                  # = MediaLayout.text_x
    right = 326.0                              # = wave_x - text_wave_gap (also clears the toggle glyph)
    cur_baseline, next_baseline = 88.5, 103.8
    cur_font = TypeToken(_ISLAND_FONT, 13, 600)
    next_font = TypeToken(_ISLAND_FONT, 12, 400)
    lead_s = 0.25                              # line shows this long before its timestamp
    anim_s = 0.18                              # line change: upward slide + crossfade
    slide_px = 12.0
    not_found_s = 3.0                          # "Lyrics unavailable" is shown once, this long, then hidden
    toggle_cx, toggle_cy, toggle_d, toggle_hit_r = 345.0, 84.0, 18.0, 15.0
    shimmer_w, shimmer_h, shimmer_period = 150.0, 8.0, 1.4
    # compact pill
    pill_w, pill_h = 240.0, 58.0               # compact media pill while a lyric line shows (normal: 186 x 32); a stadium
    pill_thumb, pill_thumb_x, pill_thumb_r = 34.0, 12.0, 8.0   # album art thumb, vertically centred
    pill_x = 56.0                              # text left (thumb_x + thumb + 10)
    pill_right_pad = 48.0                      # bars start at w-40: 8 px gap after the text slot
    pill_font = TypeToken(_ISLAND_FONT, 13, 600)
    pill_line_h = 16.5                         # word-wrapped onto at most 2 lines, end of line 2 elided
    pill_max_lines = 2
    pill_slide = 4.0                           # line change: 180 ms crossfade + this upward slide


class CalendarLayout:
    w, h = 390.0, 188.0
    panel_x, panel_y, panel_w, panel_h, panel_radius = 22.6, 17.0, 95.6, 151.2, 11.8
    month_baseline, year_baseline, day_baseline, weekday_baseline = 16.9, 31.9, 96.2, 139.3   # panel-relative
    dow_baseline = 29.3
    col_x0, col_pitch = 152.4, 28.8
    rule_y, rule_x0, rule_x1, rule_h = 37.5, 139.9, 337.2, 1.0
    row_baseline0, row_pitch, digit_h = 59.1, 26.5, 10.3
    today_d = 23.0
    today_dy = 1.4              # reference circle centre sits 1.4 px below the digit centre


class WeatherLayout:
    """Weather card (sheet 3.3). Baselines derive from cap tops + cap heights; all in card-origin 1x px."""
    w, h = 390.0, 188.0
    city_x, city_baseline = 26.7, 39.1
    icon_x, icon_y, icon_w, icon_h = 25.7, 59.6, 54.5, 34.4
    sun_d = 17.5                                  # sun disc diameter (rays extend outside the icon box)
    cloud_x, cloud_y, cloud_w, cloud_h = 40.4, 69.4, 40.1, 24.6
    temp_x, temp_baseline = 81.7, 95.7
    cond_x, cond_baseline = 27.2, 135.3
    updated_baseline = 151.4                      # "Updated 2h ago" (stale only)
    div_cx, div_y, div_w, div_h = 206.3, 35.0, 1.0, 118.8
    tile_x, tile_w, tile_h, tile_radius = 220.0, 148.0, 41.1, 9.4
    tile_ys = (25.7, 74.0, 122.3)
    tile_icon_x = 10.0                            # icon box left (12 wide)
    tile_icon_cy = 14.8                           # icon centre from the tile top (wind sits on the label row)
    tile_wind_cy = 11.4
    tile_text_x = 30.8
    tile_label_baseline, tile_value_baseline = 15.1, 31.8
    tile_icon_alpha = 0.55
    arrow_size = 10.5                             # wind direction arrow box
    arrow_gap = 4.0


class HardwareGridLayout:
    """Hardware 2 x 3 grid (sheet 3.5)."""
    w, h = 390.0, 188.0
    title_x, title_baseline = 25.0, 27.6
    tile_w, tile_h, tile_radius = 165.5, 38.9, 8.8
    col_x = (24.4, 199.4)
    row_y0, row_pitch = 38.9, 45.1
    area_bottom = 168.3                           # tile area bottom (19.7 pad) used when rows are hidden
    icon_cx, icon_cy, icon_d = 15.7, 14.8, 17.5   # icon centre from the tile top-left
    text_x = 30.7
    label_baseline, value_baseline = 11.3, 27.0
    bar_x, bar_w, bar_h, bar_bottom = 30.7, 125.0, 3.0, 3.8


class HardwareStripLayout:
    """Hardware strip (sheet 3.6): stadium, FPS tile + CPU / RAM / GPU / DISK tiles."""
    w, h = 446.0, 69.0
    pad_l = 12.5
    tile_y, tile_h, tile_radius = 12.0, 47.0, 9.3
    fps_w, tile_w, gap = 90.3, 75.9, 7.0
    icon_cx, icon_cy, icon_d = 15.5, 15.4, 16.0
    text_x = 28.8
    label_baseline, value_baseline, fps_value_baseline = 12.1, 31.3, 35.0
    bar_x, bar_right_pad, bar_h, bar_bottom = 12.5, 7.0, 3.0, 5.0


class JarvisLayout:
    """JARVIS page in the weather family. Pause control is >= 46 px tall (dwell target)."""
    w, h = 390.0, 188.0
    pause_h = 48.0                                # drawn and hit-tested control height
    # glyph + word, divider, right column
    b_ring_cx, b_ring_cy, b_ring_d = 43.0, 76.0, 34.0
    b_word_x, b_word_baseline = 68.0, 87.0
    b_cap_x, b_cap_baseline = 26.7, 39.1          # "Assistant" caption (city slot)
    b_sub_x, b_sub_baseline = 27.2, 135.3
    div_x = 206.3
    # shared right column
    col_x, col_w, tile_h, tile_radius = 220.0, 148.0, 41.1, 9.4
    col_gap = 7.2
    tile_text_x = 30.8
    tile_label_baseline, tile_value_baseline = 15.1, 31.8
    word_b = TypeToken(_ISLAND_DISPLAY, 28, 700)
    result_alpha = 0.7


class DotsLayout:
    d, d_active = 5.0, 5.6
    pitch = 8.2
    right_inset = 9.0
    anim_s = 0.18
    hit_half_w = 12.0


class IslandTokens:
    color = IslandColors
    tint = IslandTint
    shadow = IslandShadows
    pill = PillLayout
    media = MediaLayout
    lyrics = LyricsLayout
    calendar = CalendarLayout
    weather = WeatherLayout
    hardware = HardwareGridLayout
    strip = HardwareStripLayout
    jarvis = JarvisLayout
    dots = DotsLayout
    corner_smoothing = CORNER_SMOOTHING
    shell_pad = SHELL_PAD
    crossfade_ms = REDUCE_MOTION_MS
    tile_radius = 9.0


ISLAND = IslandTokens()

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
    island   = ISLAND

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
