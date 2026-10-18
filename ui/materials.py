"""
ui/materials.py — Windows-native backdrop materials for J.A.R.V.I.S.

Provides:
  - apply_mica(hwnd, dark)      → Windows 11 Mica (DWMWA_SYSTEMBACKDROP_TYPE=2)
  - apply_acrylic(hwnd, dark)   → Windows 11 Acrylic (DWMWA_SYSTEMBACKDROP_TYPE=3)
  - apply_best_backdrop(hwnd, dark, prefer_mica) → picks best available
  - extend_frame(hwnd)          → DwmExtendFrameIntoClientArea (full-window frame)
  - set_dark_titlebar(hwnd, dark) → DWMWA_USE_IMMERSIVE_DARK_MODE
  - set_rounded_corners(hwnd)   → DWMWA_WINDOW_CORNER_PREFERENCE = ROUND (Win11)
  - fallback_solid_tint(widget, dark) → solid color for Win10 / transparency off

Design rules:
  - Settings window → Mica + DwmExtendFrameIntoClientArea
  - Island, radial menu, tooltips → Acrylic
  - Windows 10 or transparency-disabled → solid tinted surface fallback
  - NEVER compute per-frame blur; the DWM material is always hardware-driven
"""

from __future__ import annotations

import ctypes
import sys
from typing import Optional

try:
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QWidget
    _QT = True
except ImportError:
    _QT = False


# ──────────────────────────────────────────────────────────────────────────────
# Win32 constants
# ──────────────────────────────────────────────────────────────────────────────
DWMWA_USE_IMMERSIVE_DARK_MODE   = 20
DWMWA_WINDOW_CORNER_PREFERENCE  = 33
DWMWA_BORDER_COLOR              = 34
DWMWA_CAPTION_COLOR             = 35
DWMWA_TEXT_COLOR                = 36
DWMWA_SYSTEMBACKDROP_TYPE       = 38    # Win11 22H2+
DWMWA_MICA_EFFECT               = 1029  # legacy Win11 21H2

DWMSBT_AUTO       = 0
DWMSBT_NONE       = 1
DWMSBT_MICA       = 2    # Mica
DWMSBT_ACRYLIC    = 3    # Acrylic
DWMSBT_TABBEDMICA = 4    # Mica Alt

DWMWCP_DEFAULT = 0
DWMWCP_ROUND   = 2
DWMWCP_SMALL   = 3


def _build_number() -> int:
    """Return Windows build number (e.g. 22621 for 22H2)."""
    if sys.platform != "win32":
        return 0
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        val, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")
        winreg.CloseKey(key)
        return int(val)
    except Exception:
        return 0


def _transparency_enabled() -> bool:
    """Check if Windows transparency effects are enabled."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "EnableTransparency")
        winreg.CloseKey(key)
        return bool(val)
    except Exception:
        return True   # assume enabled if registry read fails


def _dwm_set(hwnd: int, attr: int, value: int) -> bool:
    """Call DwmSetWindowAttribute with a c_int value. Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        dwm = ctypes.windll.dwmapi
        v = ctypes.c_int(value)
        result = dwm.DwmSetWindowAttribute(
            hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)
        )
        return result == 0  # S_OK
    except Exception:
        return False


def _dwm_set_color(hwnd: int, attr: int, abgr: int) -> bool:
    """Set a DWMWA color attribute (COLORREF = ABGR)."""
    if sys.platform != "win32":
        return False
    try:
        dwm = ctypes.windll.dwmapi
        v = ctypes.c_uint(abgr)
        result = dwm.DwmSetWindowAttribute(
            hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)
        )
        return result == 0
    except Exception:
        return False


def _to_abgr(hex_str: str) -> int:
    """Convert '#RRGGBB' to a Windows COLORREF int (0x00BBGGRR)."""
    h = hex_str.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return b << 16 | g << 8 | r


# ──────────────────────────────────────────────────────────────────────────────
# DWM caption extension (needed for Mica to fill title bar area)
# ──────────────────────────────────────────────────────────────────────────────

class MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth",    ctypes.c_int),
        ("cxRightWidth",   ctypes.c_int),
        ("cyTopHeight",    ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def extend_frame(hwnd: int, top: int = -1) -> bool:
    """
    Extend the DWM frame into the client area.
    ``top=-1`` extends all four sides (full-window Mica/Acrylic).
    """
    if sys.platform != "win32":
        return False
    try:
        dwm = ctypes.windll.dwmapi
        m = MARGINS(top, top, top, top)
        result = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
        return result == 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def set_dark_titlebar(hwnd: int, dark: bool = True) -> bool:
    """Set DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 Build 18985+)."""
    return _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


def set_rounded_corners(hwnd: int, small: bool = False) -> bool:
    """Apply rounded window corners (Windows 11+ only)."""
    pref = DWMWCP_SMALL if small else DWMWCP_ROUND
    return _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, pref)


def set_border_color(hwnd: int, hex_color: str) -> bool:
    """Apply a custom DWM border color (Windows 11+)."""
    return _dwm_set_color(hwnd, DWMWA_BORDER_COLOR, _to_abgr(hex_color))


def apply_mica(hwnd: int, dark: bool = True) -> bool:
    """
    Apply Windows 11 Mica material (DWMWA_SYSTEMBACKDROP_TYPE = 2).
    Requires Windows 11 Build 22000+ and transparency effects enabled.
    Returns True if successfully applied.
    """
    build = _build_number()
    if build < 22000:
        return False
    if not _transparency_enabled():
        return False

    ok = set_dark_titlebar(hwnd, dark)
    if build >= 22621:
        # 22H2+: use new DWMWA_SYSTEMBACKDROP_TYPE
        ok = _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MICA) and ok
    else:
        # 21H2: legacy attribute
        ok = _dwm_set(hwnd, DWMWA_MICA_EFFECT, 1) and ok

    extend_frame(hwnd, -1)  # required for Mica to paint title bar
    return ok


def is_transparency_available() -> bool:
    """Check if Windows transparency effects are enabled and platform supports it."""
    return _transparency_enabled() if sys.platform == "win32" else False


def apply_acrylic(hwnd: int, dark: bool = True, opacity: float = 1.0) -> bool:
    """
    Apply Windows 11 Acrylic material (DWMWA_SYSTEMBACKDROP_TYPE = 3).
    Falls back to Windows 10 SetWindowCompositionAttribute approach.
    Supports opacity slider scaling (0.0 - 1.0).
    Returns True if native backdrop was applied.
    """
    build = _build_number()
    clamped_op = max(0.0, min(1.0, float(opacity)))

    if build >= 22621 and _transparency_enabled():
        set_dark_titlebar(hwnd, dark)
        ok = _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_ACRYLIC)
        if ok:
            return True

    # Windows 10 / transparency off fallback: accent blur
    try:
        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState",   ctypes.c_int),
                ("AccentFlags",   ctypes.c_int),
                ("GradientColor", ctypes.c_int),
                ("AnimationId",   ctypes.c_int),
            ]

        class WCA_DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute",  ctypes.c_int),
                ("Data",       ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        user32 = ctypes.windll.user32
        fn = getattr(user32, "SetWindowCompositionAttribute", None)
        if fn:
            alpha = int(clamped_op * (0x88 if dark else 0x99))
            rgb = 0x202022 if dark else 0xF2F2F2
            tint = (alpha << 24) | rgb
            accent = AccentPolicy(4, 2, tint, 0)
            data = WCA_DATA(
                19,
                ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                ctypes.sizeof(accent),
            )
            fn(hwnd, ctypes.byref(data))
            return True
    except Exception:
        pass

    return False


def remove_backdrop(hwnd: int) -> bool:
    """
    Removes native DWM backdrops (Acrylic/Mica) so shaped/floating windows with
    WA_TranslucentBackground remain 100% invisible outside their vector path.
    """
    if sys.platform != "win32":
        return False
    ok = False
    try:
        ok = _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
    except Exception:
        pass
    try:
        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState",   ctypes.c_int),
                ("AccentFlags",   ctypes.c_int),
                ("GradientColor", ctypes.c_int),
                ("AnimationId",   ctypes.c_int),
            ]

        class WCA_DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute",  ctypes.c_int),
                ("Data",       ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        user32 = ctypes.windll.user32
        fn = getattr(user32, "SetWindowCompositionAttribute", None)
        if fn:
            accent = AccentPolicy(0, 0, 0, 0)
            data = WCA_DATA(
                19,
                ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                ctypes.sizeof(accent),
            )
            fn(hwnd, ctypes.byref(data))
            ok = True
    except Exception:
        pass
    return ok


def apply_best_backdrop(
    hwnd: int,
    dark: bool = True,
    prefer_mica: bool = False,
    opacity: float = 1.0,
) -> str:
    """
    Apply the best available backdrop for the given HWND.
    Returns one of: 'mica', 'acrylic', 'solid'
    """
    if prefer_mica and apply_mica(hwnd, dark):
        return "mica"
    if apply_acrylic(hwnd, dark, opacity=opacity):
        return "acrylic"
    # Solid tinted fallback
    set_dark_titlebar(hwnd, dark)
    return "solid"


def set_interactive(hwnd: int, interactive: bool) -> None:
    """
    Focus-safe extended window style manager.
    Dynamically toggles WS_EX_TRANSPARENT while strictly preserving
    WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TOOLWINDOW.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE       = -20
        WS_EX_LAYERED     = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE  = 0x08000000
        WS_EX_TOOLWINDOW  = 0x00000080

        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if interactive:
            new_style = (style & ~WS_EX_TRANSPARENT) | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        else:
            new_style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW

        if new_style != style:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
    except Exception as exc:
        print(f"[materials] set_interactive error: {exc}")


def make_click_through(hwnd: int) -> None:
    """
    Set WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE |
    WS_EX_TOOLWINDOW on an overlay window so clicks pass through.
    Preserves existing extended styles.
    """
    set_interactive(hwnd, False)


def make_snap_layouts_compatible(hwnd: int) -> None:
    """
    Set WS_EX_NOREDIRECTIONBITMAP on the settings window so Snap Layouts
    thumbnail works correctly with Mica/Acrylic.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_NOREDIRECTIONBITMAP = 0x00200000
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_NOREDIRECTIONBITMAP
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


def solid_tint_stylesheet(dark: bool, opacity: float = 1.0) -> str:
    """
    Return a QWidget stylesheet for a solid tinted fallback background
    (used when transparency is off or on Windows 10 without blur support).
    """
    if dark:
        alpha = int(max(0.0, min(1.0, float(opacity))) * 255)
        return f"background-color: rgba(19, 22, 23, {alpha / 255.0:.2f});"
    else:
        return "background-color: #F2F2F7;"
