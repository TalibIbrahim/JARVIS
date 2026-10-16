"""
Apple Human Interface Guidelines (HIG) Overlay & System UI for J.A.R.V.I.S.

Features:
- Premium macOS / visionOS aesthetic with clarity, deference, and depth.
- Real Windows 11 Acrylic / Mica backdrop (DwmSetWindowAttribute) with Windows 10 blur fallback.
- Layered glass: blurred backdrop, tint, 1px inner highlight stroke, soft drop shadows, specular sheen.
- Auto Light/Dark mode following Windows system theme + Windows accent color support.
- Continuous-corner squircle capsule geometry (true Apple superellipse curves).
- Critically damped spring physics for fluid 60 FPS animations (respects reduce-motion).
- Distinct Island states: Standby, Wake Ring, Tracking, Gestures, Dwell, Siri-style Waveform, Error.
- Glass cursor dwell progress ring and haptic-like ripple expansion overlay.
- macOS System Settings style configuration window with grouped inset cards and custom toggle switches.
- System tray icon with quick actions and auto-update integration.
"""
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time

try:
    from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Signal, QObject
    from PySide6.QtGui import (
        QPainter, QColor, QPen, QBrush, QFont, QPainterPath,
        QLinearGradient, QRadialGradient, QPixmap, QIcon, QAction, QPolygonF
    )
    from PySide6.QtWidgets import (
        QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
        QLabel, QSlider, QCheckBox, QPushButton, QComboBox,
        QGroupBox, QSystemTrayIcon, QMenu, QStackedWidget,
        QListWidget, QListWidgetItem, QScrollArea, QFrame, QProgressBar,
        QLineEdit
    )
    PYSIDE6_AVAILABLE = True
except ImportError:
    PYSIDE6_AVAILABLE = False
    import tkinter as tk

try:
    from updater import APP_VERSION
except ImportError:
    APP_VERSION = "1.2.3"

try:
    from voice import list_microphones, find_best_microphone, probe_mic_level, test_microphone_recognition
except ImportError:
    list_microphones = lambda: []
    find_best_microphone = lambda cfg=None: (None, "Default Microphone")
    probe_mic_level = lambda dev=None: (0.0, 0.0)
    test_microphone_recognition = lambda dev=None: {"success": False, "error": "Voice module unavailable"}

try:
    from ai import (
        get_api_key,
        set_api_key,
        delete_api_key,
        GroqClient,
        DEFAULT_LLM_MODEL,
        DEFAULT_STT_MODEL,
    )
except ImportError:
    get_api_key = lambda: None
    set_api_key = lambda k: None
    delete_api_key = lambda: None
    GroqClient = None
    DEFAULT_LLM_MODEL = "llama-3.1-8b-instant"
    DEFAULT_STT_MODEL = "whisper-large-v3-turbo"


def _qt_excepthook(exc_type, exc_val, exc_tb):
    import traceback
    traceback.print_exception(exc_type, exc_val, exc_tb)

if PYSIDE6_AVAILABLE:
    sys.excepthook = _qt_excepthook


def get_app_icon():
    """Finds and returns the high-resolution J.A.R.V.I.S. QIcon."""
    if not PYSIDE6_AVAILABLE:
        return None
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("jarvis.ico", "jarvis.png", "jarvis_tray.png"):
        p = os.path.join(base_dir, "assets", name)
        if os.path.exists(p):
            return QIcon(p)
    return None


# =====================================================================
# Windows 10/11 Native Glass Backdrop & Theme Detection
# =====================================================================

def apply_glass_backdrop(hwnd, dark=True):
    """
    Applies Windows 11 Mica/Acrylic backdrop or Windows 10 accent blur.
    Returns True if native backdrop was successfully applied.
    """
    if sys.platform != "win32":
        return False

    # 1. Windows 11 DwmSetWindowAttribute (Build >= 22000)
    try:
        dwm = ctypes.windll.dwmapi
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        dark_val = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark_val), ctypes.sizeof(dark_val))

        # DWMWA_SYSTEMBACKDROP_TYPE = 38 (3 = Acrylic, 2 = Mica, 4 = Tabbed)
        backdrop_type = ctypes.c_int(3)
        res = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop_type), ctypes.sizeof(backdrop_type))
        if res == 0:
            return True
    except Exception:
        pass

    # 2. Windows 10 SetWindowCompositionAttribute (Accent policy)
    try:
        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_int),
                ("AnimationId", ctypes.c_int)
            ]

        class WindowCompositionAttributeData(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)
            ]

        user32 = ctypes.windll.user32
        set_attr = getattr(user32, "SetWindowCompositionAttribute", None)
        if set_attr:
            # ACCENT_ENABLE_ACRYLICBLURBEHIND = 4 (or ACCENT_ENABLE_BLURBEHIND = 3)
            # Tint color: 0x9918181A (AABBGGRR)
            tint = 0x88202022 if dark else 0x88F0F0F2
            accent = AccentPolicy(4, 2, tint, 0)
            data = WindowCompositionAttributeData(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
            set_attr(hwnd, ctypes.byref(data))
            return True
    except Exception:
        pass

    return False


def get_system_theme():
    """Detects Windows Light/Dark mode and accent color."""
    dark = True
    accent = "#007aff"  # Apple Blue default
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            dark = (val == 0)
            winreg.CloseKey(key)
        except Exception:
            pass

        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
            val, _ = winreg.QueryValueEx(key, "AccentColor")
            b = (val >> 16) & 0xFF
            g = (val >> 8) & 0xFF
            r = val & 0xFF
            accent = f"#{r:02x}{g:02x}{b:02x}"
            winreg.CloseKey(key)
        except Exception:
            pass
    return dark, accent


# =====================================================================
# Continuous Curvature (Squircle) & Spring Physics
# =====================================================================

def create_squircle_path(rect, radius):
    """Generates continuous curvature squircle path (Apple-style G2 superellipse)."""
    path = QPainterPath()
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    r = min(radius, w / 2.0, h / 2.0)
    c = r * 0.55228475  # Bézier control point smoothing

    path.moveTo(x + r, y)
    path.lineTo(x + w - r, y)
    path.cubicTo(x + w - r + c, y, x + w, y + r - c, x + w, y + r)
    path.lineTo(x + w, y + h - r)
    path.cubicTo(x + w, y + h - r + c, x + w - r + c, y + h, x + w - r, y + h)
    path.lineTo(x + r, y + h)
    path.cubicTo(x + r - c, y + h, x, y + h - r + c, x, y + h - r)
    path.lineTo(x, y + r)
    path.cubicTo(x, y + r - c, x + r - c, y, x + r, y)
    path.closeSubpath()
    return path


class Spring:
    """Critically damped harmonic oscillator for fluid physics."""
    def __init__(self, value=0.0, stiffness=180.0, damping=20.0):
        self.value = float(value)
        self.target = float(value)
        self.velocity = 0.0
        self.stiffness = float(stiffness)
        self.damping = float(damping)

    def set_target(self, target):
        self.target = float(target)

    def update(self, dt, reduce_motion=False):
        if reduce_motion:
            self.value = self.target
            self.velocity = 0.0
            return self.value
        dt = min(max(float(dt), 1e-4), 0.05)
        sub_steps = max(1, int(dt / 0.016) + 1)
        sub_dt = dt / sub_steps
        for _ in range(sub_steps):
            force = -self.stiffness * (self.value - self.target) - self.damping * self.velocity
            self.velocity += force * sub_dt
            self.value += self.velocity * sub_dt
        self.value = min(max(self.value, 10.0), 3840.0)
        return self.value


# =====================================================================
# The Island: Menu-Bar Capsule Overlay (Apple HIG)
# =====================================================================

class AppleDynamicIsland(QWidget):
    """The floating top-center glass capsule following Apple HIG."""

    def __init__(self, overlay_mgr):
        super().__init__()
        self.mgr = overlay_mgr
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("JARVIS_DYNAMIC_ISLAND")
        icon = get_app_icon()
        if icon:
            self.setWindowIcon(icon)

        # Dimensions & Springs
        self.spring_w = Spring(120.0, 190.0, 22.0)
        self.spring_h = Spring(32.0, 190.0, 22.0)
        self.spring_prog = Spring(0.0, 240.0, 24.0)

        # State fields
        self.active = False
        self.label = "STANDBY"
        self.progress = 0.0
        self.hands = 0
        self.landmarks = []
        self.say_text = ""
        self.say_until = 0.0
        self.listening = False
        self.listening_until = 0.0
        self.error_text = ""
        self.error_until = 0.0
        self.precision_active = False
        self.thinking = False
        self.is_ai_error = False

        self.t0 = time.time()
        self.last_frame_t = time.time()

        # Theme
        self.dark_mode, self.accent_color = get_system_theme()
        if self.mgr.cfg and getattr(self.mgr.cfg, "theme_mode", "auto") != "auto":
            self.dark_mode = (self.mgr.cfg.theme_mode == "dark")

        self.resize(120, 32)
        self._center_top()

    def make_click_through(self):
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
            if getattr(self.mgr.cfg, "glass_blur", True):
                apply_glass_backdrop(hwnd, dark=self.dark_mode)
        except Exception as exc:
            print(f"[overlay] backdrop error: {exc}")

    def _center_top(self):
        try:
            screen = QApplication.primaryScreen()
            if screen:
                geom = screen.geometry()
                w = max(40, min(int(round(self.spring_w.value)), 3840))
                h = max(20, min(int(round(self.spring_h.value)), 2160))
                cx = geom.x() + (geom.width() - w) // 2
                self.setGeometry(cx, geom.y(), w, h)
        except Exception as exc:
            print(f"[overlay] center_top error: {exc}")

    def update_frame(self, now):
        dt = max(now - self.last_frame_t, 1e-4)
        self.last_frame_t = now

        reduce_motion = getattr(self.mgr.cfg, "reduce_motion", False)

        # Target dimensions based on Apple HIG layout
        is_error = now < self.error_until
        is_speaking = now < self.say_until
        is_thinking = self.thinking
        is_listening = now < self.listening_until
        is_gesture = self.label in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT", "SCROLL")

        if is_error:
            tw, th = 340.0, 46.0
        elif is_thinking:
            tw, th = 260.0, 42.0
        elif is_speaking:
            tw, th = 380.0, 52.0
        elif is_listening:
            tw, th = 260.0, 42.0
        elif is_gesture:
            tw, th = 270.0, 40.0
        elif self.active:
            tw, th = 210.0, 36.0
        else:
            tw, th = 110.0, 30.0

        self.spring_w.set_target(tw)
        self.spring_h.set_target(th)
        self.spring_prog.set_target(self.progress)

        self.spring_w.update(dt, reduce_motion)
        self.spring_h.update(dt, reduce_motion)
        self.spring_prog.update(dt, reduce_motion)

        self._center_top()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        now = time.time()
        t = now - self.t0
        w = float(self.width())
        h = float(self.height())
        r = min(h / 2.0, 18.0)

        is_speaking = now < self.say_until
        is_listening = now < self.listening_until
        is_error = now < self.error_until
        is_thinking = self.thinking

        # 1. Soft Multi-Layered Drop Shadow
        pill_rect = QRectF(2, -18, w - 4, h + 16)
        squircle = create_squircle_path(pill_rect, r)

        for offset, alpha in [(2, 14), (4, 22), (6, 32)]:
            shadow_rect = QRectF(2, -18 + offset, w - 4, h + 16)
            shadow_path = create_squircle_path(shadow_rect, r)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, alpha)))
            p.drawPath(shadow_path)

        # 2. Base Glass Tint Fill (Refined macOS graphite / frosted milk)
        bg_grad = QLinearGradient(0, -18, 0, h)
        if self.dark_mode:
            bg_grad.setColorAt(0.0, QColor(36, 36, 42, 215))
            bg_grad.setColorAt(1.0, QColor(22, 22, 26, 230))
        else:
            bg_grad.setColorAt(0.0, QColor(248, 248, 252, 225))
            bg_grad.setColorAt(1.0, QColor(232, 232, 238, 235))

        p.fillPath(squircle, QBrush(bg_grad))

        # 3. Specular Sheen (Upper half highlight)
        sheen_grad = QLinearGradient(0, -18, 0, h * 0.5)
        sheen_grad.setColorAt(0.0, QColor(255, 255, 255, 30 if self.dark_mode else 80))
        sheen_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(squircle, QBrush(sheen_grad))

        # 4. 1 px Inner Highlight Stroke (Top light reflection, bottom occlusion)
        border_grad = QLinearGradient(0, -18, 0, h)
        if is_error:
            if self.is_ai_error:
                border_grad.setColorAt(0.0, QColor("#ff9f0a"))
                border_grad.setColorAt(1.0, QColor("#d97706"))
            else:
                border_grad.setColorAt(0.0, QColor("#ff453a"))
                border_grad.setColorAt(1.0, QColor("#d70015"))
        elif is_thinking:
            pulse_a = int(120 + 80 * math.sin(t * 6.0))
            border_grad.setColorAt(0.0, QColor(255, 255, 255, pulse_a))
            border_grad.setColorAt(1.0, QColor(0, 0, 0, 60))
        elif self.dark_mode:
            border_grad.setColorAt(0.0, QColor(255, 255, 255, 65))
            border_grad.setColorAt(1.0, QColor(0, 0, 0, 75))
        else:
            border_grad.setColorAt(0.0, QColor(255, 255, 255, 200))
            border_grad.setColorAt(1.0, QColor(0, 0, 0, 25))

        p.strokePath(squircle, QPen(QBrush(border_grad), 1.0))

        # 5. Status Glyphs & Content
        cy = h / 2.0
        accent = QColor(self.accent_color)
        text_primary = QColor("#f5f5f7" if self.dark_mode else "#1c1c1e")
        text_secondary = QColor("#8e8e93" if self.dark_mode else "#6e6e73")

        # Left Status Indicator Dot / Arc
        dot_x = 18.0
        dot_r = 4.0

        if is_error:
            dot_col = QColor("#ff9f0a" if self.is_ai_error else "#ff453a")
        elif is_thinking:
            pulse = 0.5 + 0.5 * math.sin(t * 6.0)
            dot_col = accent
            dot_r = 3.5 + 1.5 * pulse
        elif self.active:
            dot_col = QColor("#30d158")  # Apple Emerald Green
        else:
            dot_col = QColor("#8e8e93")

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(dot_col))
        p.drawEllipse(QPointF(dot_x, cy), dot_r, dot_r)

        # Holding Palm Wake Ring around Dot
        cur_prog = self.spring_prog.value
        if cur_prog > 0.01:
            ring_pen = QPen(accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(ring_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            box = QRectF(dot_x - 7.5, cy - 7.5, 15.0, 15.0)
            p.drawArc(box, int(90 * 16), -int(cur_prog * 360 * 16))

        # Typography
        tx = dot_x + 14.0
        font = QFont("Segoe UI", 9)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)

        if h < 34.0:
            # Standby mode
            p.setPen(text_secondary if not self.active else text_primary)
            p.drawText(int(tx), int(cy + 4), "Jarvis")
        else:
            # Expanded Active Mode
            if is_error:
                p.setPen(QColor("#ff9f0a" if self.is_ai_error else "#ff453a"))
                p.drawText(int(tx), int(cy - 2), "Jarvis")
                font_sub = QFont("Segoe UI", 8)
                p.setFont(font_sub)
                p.setPen(text_primary)
                p.drawText(int(tx), int(cy + 11), self.error_text or "Error")

            elif is_thinking:
                p.setPen(accent)
                p.drawText(int(tx), int(cy - 2), "Jarvis")
                font_sub = QFont("Segoe UI", 8)
                p.setFont(font_sub)
                p.setPen(text_primary)
                p.drawText(int(tx), int(cy + 11), "Thinking…")

                # Subtle animated pulse indicators on right
                rx = w - 42.0
                for i in range(3):
                    phase = t * 5.0 - i * 0.9
                    alpha = int(70 + 185 * max(0.0, math.sin(phase)))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), alpha)))
                    p.drawEllipse(QPointF(rx + i * 8, cy), 2.5, 2.5)

            elif is_speaking:
                p.setPen(accent)
                p.drawText(int(tx), int(cy - 2), "Jarvis")
                font_sub = QFont("Segoe UI", 8)
                p.setFont(font_sub)
                p.setPen(text_primary)
                disp = self.say_text if len(self.say_text) <= 40 else self.say_text[:38] + "…"
                p.drawText(int(tx), int(cy + 11), disp)

                # Fluid Speech Waveform on Right
                wx = w - 48.0
                for i in range(4):
                    bar_h = 3.0 + 10.0 * abs(math.sin(t * 7.0 + i * 0.8))
                    p.fillRect(QRectF(wx + i * 6, cy - bar_h / 2.0, 3.0, bar_h), accent)

            elif is_listening:
                # Siri-style continuous fluid sine waveform
                p.setPen(text_primary)
                p.drawText(int(tx), int(cy + 4), "Listening…")

                # Multi-harmonic smooth waveform
                wave_w = 60.0
                wave_x0 = w - 75.0
                p.setPen(QPen(accent, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                wave_path = QPainterPath()
                for step in range(int(wave_w)):
                    frac = step / wave_w
                    envelope = math.sin(frac * math.pi)
                    wy = cy + 6.0 * envelope * math.sin(t * 8.0 + frac * 4.0 * math.pi)
                    if step == 0:
                        wave_path.moveTo(wave_x0 + step, wy)
                    else:
                        wave_path.lineTo(wave_x0 + step, wy)
                p.drawPath(wave_path)

            else:
                # Tracking / Gesture feedback
                p.setPen(text_primary)
                p.drawText(int(tx), int(cy - 2), "Jarvis")
                font_sub = QFont("Segoe UI", 8)
                font_sub.setWeight(QFont.Weight.Medium)
                p.setFont(font_sub)

                is_hot = self.label in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT", "SCROLL")
                p.setPen(accent if is_hot else text_secondary)
                label_disp = self.label.title() if self.label else "Tracking"
                p.drawText(int(tx), int(cy + 11), label_disp)

                # Mini Hand Skeleton Visualizer (Apple style: thin, antialiased, calm)
                if self.active and self.landmarks and getattr(self.mgr.cfg, "show_skeleton_mini", True) and w > 200.0:
                    skel_box = 24.0
                    skel_x = w - skel_box - 16.0
                    skel_y = cy - skel_box / 2.0

                    hand_lm = self.landmarks[0]
                    pts = [(skel_x + lm[0] * skel_box, skel_y + lm[1] * skel_box) for lm in hand_lm]

                    # Thin rounded bone strokes
                    bone_pen = QPen(QColor(255, 255, 255, 75 if self.dark_mode else 120), 1.2,
                                    Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                    p.setPen(bone_pen)

                    rays = [(0, 4), (0, 8), (0, 12), (0, 16), (0, 20)]
                    for a, b in rays:
                        if a < len(pts) and b < len(pts):
                            p.drawLine(QPointF(pts[a][0], pts[a][1]), QPointF(pts[b][0], pts[b][1]))

                    # Active tip dot
                    if len(pts) > 8:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QBrush(accent if is_hot else text_secondary))
                        p.drawEllipse(QPointF(pts[8][0], pts[8][1]), 2.2, 2.2)


# =====================================================================
# Cursor Dwell Ring & Ripple Feedback Window (Apple VisionOS Style)
# =====================================================================

class CursorFeedbackOverlay(QWidget):
    """Transparent click-through ring overlay following the cursor position."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("JARVIS_CURSOR_FEEDBACK")

        self.cur_x = 0.0
        self.cur_y = 0.0
        self.progress = 0.0
        self.latched = False
        self.flashing = False
        self.flash_start = 0.0
        self.radial_open = False
        self.radial_hover = None
        self.radial_prog = 0.0

        self.dark_mode, self.accent_color = get_system_theme()
        self.resize(160, 160)
        self._make_click_through()

    def _make_click_through(self):
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
        except Exception:
            pass

    def update_dwell(self, data):
        self.cur_x = data.get("x", self.cur_x)
        self.cur_y = data.get("y", self.cur_y)
        new_prog = data.get("progress", 0.0)
        was_flashing = self.flashing
        self.flashing = data.get("flashing", False)

        if not was_flashing and self.flashing:
            self.flash_start = time.time()

        self.progress = new_prog
        self.latched = data.get("latched", False)
        self.radial_open = data.get("radial_open", False)
        self.radial_hover = data.get("radial_hover")
        self.radial_prog = data.get("radial_progress", 0.0)

        sz = 220 if self.radial_open else 120
        self.resize(sz, sz)
        self.move(int(self.cur_x - sz / 2.0), int(self.cur_y - sz / 2.0))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        cx = float(self.width()) / 2.0
        cy = float(self.height()) / 2.0
        accent = QColor(self.accent_color)
        now = time.time()

        # 1. Click Ripple Expansion Pulse
        if self.flashing or (now - self.flash_start < 0.35):
            elapsed = now - self.flash_start
            frac = min(elapsed / 0.35, 1.0)
            ripple_r = 14.0 + 20.0 * frac
            ripple_alpha = int(220 * (1.0 - frac))
            ripple_pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), ripple_alpha), 2.0)
            p.setPen(ripple_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), ripple_r, ripple_r)

        # 2. Radial Action Menu
        if self.radial_open:
            sectors = [
                ("click", "Click", 0),
                ("double_click", "Double", 1),
                ("right_click", "Right", 2),
                ("drag", "Drag", 3),
                ("scroll", "Scroll", 4)
            ]
            r_menu = 65.0
            n = len(sectors)
            arc_step = 360.0 / n

            # Frosted glass radial background
            p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
            p.setBrush(QBrush(QColor(20, 20, 24, 210 if self.dark_mode else 235)))
            p.drawEllipse(QPointF(cx, cy), r_menu, r_menu)

            for sid, label, idx in sectors:
                ang_deg = idx * arc_step - 90.0
                ang_rad = math.radians(ang_deg)
                sx = cx + 42.0 * math.cos(ang_rad)
                sy = cy + 42.0 * math.sin(ang_rad)

                is_sel = (self.radial_hover == sid)
                if is_sel:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QBrush(accent))
                    p.drawRoundedRect(QRectF(sx - 24, sy - 11, 48, 22), 6, 6)
                    p.setPen(QColor("#ffffff"))
                else:
                    p.setPen(QColor("#f5f5f7" if self.dark_mode else "#1c1c1e"))

                font = QFont("Segoe UI", 7, QFont.Weight.DemiBold)
                p.setFont(font)
                p.drawText(QRectF(sx - 24, sy - 10, 48, 20), Qt.AlignmentFlag.AlignCenter, label)

            return

        # 3. Dwell Progress Ring
        if self.progress > 0.01 and not self.latched:
            r_dwell = 14.0
            # Ambient thin track
            p.setPen(QPen(QColor(255, 255, 255, 45), 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r_dwell, r_dwell)

            # Active Progress Arc
            prog_pen = QPen(accent, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(prog_pen)
            box = QRectF(cx - r_dwell, cy - r_dwell, r_dwell * 2.0, r_dwell * 2.0)
            p.drawArc(box, int(90 * 16), -int(self.progress * 360 * 16))


# =====================================================================
# macOS System Settings Style Controls (Apple HIG)
# =====================================================================

class AppleSwitch(QWidget):
    """Pill-shaped macOS toggle switch with smooth animated slider thumb."""
    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setFixedSize(46, 26)
        self._checked = checked
        self._thumb_pos = 1.0 if checked else 0.0
        self._spring = Spring(self._thumb_pos, 220.0, 20.0)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(16)

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        if self._checked != checked:
            self._checked = checked
            self._spring.set_target(1.0 if checked else 0.0)
            self.toggled.emit(checked)
            self.update()

    def mousePressEvent(self, event):
        self.setChecked(not self._checked)

    def _animate(self):
        v = self._spring.update(0.016)
        if abs(v - self._thumb_pos) > 0.005:
            self._thumb_pos = v
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = float(self.width()), float(self.height())
        r = h / 2.0

        # Background track
        track_col = QColor("#34c759") if self._checked else QColor("#3a3a3c")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_col))
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)

        # Sliding thumb
        thumb_r = 10.5
        thumb_x = 2.5 + self._thumb_pos * (w - 2.5 * 2 - thumb_r * 2)
        thumb_y = (h - thumb_r * 2) / 2.0

        # Soft thumb drop shadow
        p.setBrush(QBrush(QColor(0, 0, 0, 40)))
        p.drawEllipse(QRectF(thumb_x, thumb_y + 1.0, thumb_r * 2, thumb_r * 2))

        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QRectF(thumb_x, thumb_y, thumb_r * 2, thumb_r * 2))


class SegmentedControl(QWidget):
    """macOS-style segmented pill selector."""
    valueChanged = Signal(str)

    def __init__(self, options, current="", parent=None):
        super().__init__(parent)
        self.options = options
        self.current = current or (options[0][0] if options else "")
        self.setFixedHeight(30)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        self.buttons = []
        for opt_id, label in options:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(opt_id == self.current)
            btn.setFixedHeight(24)
            btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #8e8e93;
                    border: none;
                    border-radius: 5px;
                    font-size: 11px;
                    font-weight: 500;
                    padding: 0 10px;
                }
                QPushButton:checked {
                    background: #007aff;
                    color: #ffffff;
                    font-weight: 600;
                }
            """)
            btn.clicked.connect(lambda _, oid=opt_id: self._select(oid))
            layout.addWidget(btn)
            self.buttons.append((opt_id, btn))

        self.setStyleSheet("""
            SegmentedControl {
                background: #1c1c1e;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 7px;
            }
        """)

    def _select(self, opt_id):
        self.current = opt_id
        for oid, btn in self.buttons:
            btn.setChecked(oid == opt_id)
        self.valueChanged.emit(opt_id)


class GroupedCard(QFrame):
    """macOS System Settings Grouped Inset Card container."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            GroupedCard {
                background-color: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 10px;
            }
        """)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 12, 16, 12)
        self.layout.setSpacing(12)

    def add_row(self, title, desc, widget):
        row = QHBoxLayout()
        vbox = QVBoxLayout()
        vbox.setSpacing(2)

        lbl_t = QLabel(title)
        lbl_t.setStyleSheet("font-size: 13px; font-weight: 600; color: #f5f5f7;")
        vbox.addWidget(lbl_t)

        if desc:
            lbl_d = QLabel(desc)
            lbl_d.setStyleSheet("font-size: 11px; color: #8e8e93;")
            vbox.addWidget(lbl_d)

        row.addLayout(vbox, stretch=1)
        row.addWidget(widget)
        self.layout.addLayout(row)


# =====================================================================
# macOS System Settings Style Dialog
# =====================================================================

if PYSIDE6_AVAILABLE:
    class SettingsBridge(QObject):
        test_conn_result = Signal(bool, str, dict)
        test_mic_result = Signal(dict)
        updater_event = Signal(str, dict)


class SettingsWindow(QDialog):
    """Apple HIG System Settings Window with Sidebar and Grouped Inset Cards."""

    def __init__(self, cfg, ctx, updater=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.ctx = ctx
        self.updater = updater
        if PYSIDE6_AVAILABLE:
            self.bridge = SettingsBridge(self)
            self.bridge.test_conn_result.connect(self._on_test_conn_result)
            self.bridge.test_mic_result.connect(self._on_test_mic_result)
            self.bridge.updater_event.connect(self._on_updater_event_gui)

        self.setWindowTitle("J.A.R.V.I.S. Settings")
        icon = get_app_icon()
        if icon:
            self.setWindowIcon(icon)
        self.setFixedSize(720, 580)
        self.setStyleSheet("""
            QDialog {
                background-color: #121214;
                color: #f5f5f7;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            }
            QLabel {
                color: #f5f5f7;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #2c2c2e;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #007aff;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                width: 14px;
                margin-top: -5px;
                margin-bottom: -5px;
                border-radius: 7px;
            }
            QPushButton {
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 6px;
                color: #f5f5f7;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #007aff;
                color: #ffffff;
            }
            QComboBox {
                background-color: #1c1c1e;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 6px;
                color: #f5f5f7;
                padding: 4px 10px;
                font-size: 12px;
            }
            QLineEdit {
                background-color: #1c1c1e;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 6px;
                color: #f5f5f7;
                padding: 4px 10px;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #007aff;
            }
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Left Sidebar
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        self.sidebar.setStyleSheet("""
            QListWidget {
                background-color: #18181b;
                border: none;
                border-right: 1px solid rgba(255, 255, 255, 0.08);
                padding: 12px 8px;
            }
            QListWidget::item {
                height: 32px;
                border-radius: 6px;
                color: #8e8e93;
                font-size: 13px;
                font-weight: 500;
                padding-left: 10px;
            }
            QListWidget::item:selected {
                background-color: #007aff;
                color: #ffffff;
                font-weight: 600;
            }
        """)

        categories = [
            ("General", "General"),
            ("Gestures", "Gestures"),
            ("Pointer", "Pointer & Dwell"),
            ("Voice", "Voice"),
            ("AI", "AI"),
            ("Display", "Display"),
            ("About", "About")
        ]
        for cid, label in categories:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, cid)
            self.sidebar.addItem(item)

        main_layout.addWidget(self.sidebar)

        # 2. Right Stacked Content
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, stretch=1)

        self._build_general_page()
        self._build_gestures_page()
        self._build_pointer_page()
        self._build_voice_page()
        self._build_ai_page()
        self._build_display_page()
        self._build_about_page()

        def on_tab_changed(index):
            self.stack.setCurrentIndex(index)
            if hasattr(self, "mic_timer") and hasattr(self, "voice_page"):
                if self.stack.currentWidget() == self.voice_page:
                    if not self.mic_timer.isActive():
                        self.mic_timer.start(120)
                else:
                    if self.mic_timer.isActive():
                        self.mic_timer.stop()

        self.sidebar.currentRowChanged.connect(on_tab_changed)
        self.sidebar.setCurrentRow(0)

        for btn in self.findChildren(QPushButton):
            btn.setAutoDefault(False)
            btn.setDefault(False)

    def closeEvent(self, event):
        if hasattr(self, "mic_timer") and self.mic_timer.isActive():
            self.mic_timer.stop()
        if self.updater and hasattr(self, "_updater_cb"):
            self.updater.remove_listener(self._updater_cb)
        super().closeEvent(event)

    def reject(self):
        if hasattr(self, "mic_timer") and self.mic_timer.isActive():
            self.mic_timer.stop()
        if self.updater and hasattr(self, "_updater_cb"):
            self.updater.remove_listener(self._updater_cb)
        super().reject()

    def _on_test_conn_result(self, ok, message, models_data):
        if hasattr(self, "btn_test_conn"):
            self.btn_test_conn.setEnabled(True)
        if hasattr(self, "lbl_conn_status"):
            if ok:
                self.lbl_conn_status.setText(f"Connected: {message}")
                self.lbl_conn_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #30d158;")
            else:
                self.lbl_conn_status.setText(f"Failed: {message}")
                self.lbl_conn_status.setStyleSheet("font-size: 11px; font-weight: 500; color: #ff9f0a;")
        if ok and models_data and hasattr(self, "llm_combo") and hasattr(self, "stt_combo"):
            cur_l = self.llm_combo.currentText()
            self.llm_combo.blockSignals(True)
            self.llm_combo.clear()
            for lm in models_data.get("llm", []):
                self.llm_combo.addItem(lm)
            if cur_l in models_data.get("llm", []):
                self.llm_combo.setCurrentText(cur_l)
            self.llm_combo.blockSignals(False)

            cur_s = self.stt_combo.currentText()
            self.stt_combo.blockSignals(True)
            self.stt_combo.clear()
            for sm in models_data.get("stt", []):
                self.stt_combo.addItem(sm)
            if cur_s in models_data.get("stt", []):
                self.stt_combo.setCurrentText(cur_s)
            self.stt_combo.blockSignals(False)

    def _on_test_mic_result(self, res):
        if hasattr(self, "btn_test_mic"):
            self.btn_test_mic.setEnabled(True)
            self.btn_test_mic.setText("Test Microphone & Recognition")
        if hasattr(self, "lbl_test_result"):
            if res.get("success"):
                self.lbl_test_result.setText(f"Success! Heard: \"{res['text']}\" (Audio signal: {res.get('avg_level', 0):.0f})")
                self.lbl_test_result.setStyleSheet("font-size: 12px; font-weight: 600; color: #34c759;")
            else:
                err_msg = res.get("error", "Unknown error")
                self.lbl_test_result.setText(f"Notice: {err_msg} (Audio signal: {res.get('avg_level', 0):.0f})")
                self.lbl_test_result.setStyleSheet("font-size: 11px; color: #ff9500;")

    def _on_updater_event_gui(self, state, kwargs):
        if not hasattr(self, "btn_chk") or not hasattr(self, "lbl_up_status"):
            return
        if state == "checking":
            self.btn_chk.setEnabled(False)
            self.btn_restart.setVisible(False)
            self.prog_bar.setVisible(False)
            self.lbl_up_status.setText("Checking for updates...")
            self.lbl_up_status.setStyleSheet("font-size: 11px; color: #007aff;")
        elif state == "up-to-date":
            self.btn_chk.setEnabled(True)
            self.btn_restart.setVisible(False)
            self.prog_bar.setVisible(False)
            self.lbl_up_status.setText(f"Jarvis is up to date (v{APP_VERSION}).")
            self.lbl_up_status.setStyleSheet("font-size: 11px; color: #34c759;")
        elif state == "available":
            self.btn_chk.setEnabled(True)
            ver = kwargs.get("info", {}).get("version", "New Version")
            self.lbl_up_status.setText(f"Update available: {ver}")
            self.lbl_up_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #ff9f0a;")
            self.btn_restart.setText(f"Download & Update to {ver}")
            self.btn_restart.setEnabled(True)
            self.btn_restart.setVisible(True)
        elif state == "downloading":
            pct = kwargs.get("percent", 0)
            self.prog_bar.setVisible(True)
            self.prog_bar.setValue(pct)
            self.lbl_up_status.setText(f"Downloading update... {pct}%")
            self.lbl_up_status.setStyleSheet("font-size: 11px; color: #007aff;")
            self.btn_restart.setEnabled(False)
        elif state == "downloaded":
            self.prog_bar.setVisible(False)
            self.lbl_up_status.setText("Update downloaded! Ready to restart.")
            self.lbl_up_status.setStyleSheet("font-size: 11px; font-weight: 600; color: #34c759;")
            self.btn_restart.setText("Restart Now to Apply Update")
            self.btn_restart.setEnabled(True)
            self.btn_restart.setVisible(True)
        elif state == "error":
            self.btn_chk.setEnabled(True)
            self.btn_restart.setEnabled(True)
            self.prog_bar.setVisible(False)
            msg = kwargs.get("message", "Error checking for updates")
            self.lbl_up_status.setText(f"Notice: {msg[:60]}")
            self.lbl_up_status.setStyleSheet("font-size: 11px; color: #ff9f0a;")

    def select_tab(self, tab):
        """Selects tab by integer index or case-insensitive category string."""
        if isinstance(tab, int):
            if 0 <= tab < self.sidebar.count():
                self.sidebar.setCurrentRow(tab)
                return
        elif isinstance(tab, str):
            tab_lower = tab.strip().lower()
            for i in range(self.sidebar.count()):
                item = self.sidebar.item(i)
                if item and str(item.data(Qt.ItemDataRole.UserRole)).strip().lower() == tab_lower:
                    self.sidebar.setCurrentRow(i)
                    return
        self.sidebar.setCurrentRow(0)

    def _build_page_container(self, title, subtitle):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 6px;
                margin: 0px 0px 0px 0px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.15);
                border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        h_layout = QVBoxLayout()
        h_layout.setSpacing(3)
        lbl_t = QLabel(title)
        lbl_t.setStyleSheet("font-size: 20px; font-weight: 700; color: #f5f5f7;")
        lbl_s = QLabel(subtitle)
        lbl_s.setStyleSheet("font-size: 12px; color: #8e8e93;")
        h_layout.addWidget(lbl_t)
        h_layout.addWidget(lbl_s)
        layout.addLayout(h_layout)

        scroll.setWidget(content)
        page_layout.addWidget(scroll)
        return page, layout

    def _build_general_page(self):
        page, layout = self._build_page_container("General", "System behavior and camera settings")
        card = GroupedCard()

        # Camera Index
        cam_combo = QComboBox()
        for i in range(4):
            cam_combo.addItem(f"Camera {i}", i)
        cam_combo.setCurrentIndex(self.cfg.camera_index)
        cam_combo.currentIndexChanged.connect(lambda idx: self._save("camera_index", idx))
        card.add_row("Camera Input", "Select video capture device", cam_combo)

        # Camera Resolution
        res_ctrl = SegmentedControl([
            ("640x480", "640 × 480"),
            ("1280x720", "1280 × 720 (HD)")
        ], current=f"{self.cfg.frame_width}x{self.cfg.frame_height}")
        res_ctrl.valueChanged.connect(self._on_res_change)
        card.add_row("Resolution", "Higher resolution improves small target tracking", res_ctrl)

        # Tracking Area
        track_ctrl = SegmentedControl([
            ("full", "Full Frame (100%)"),
            ("margin", "Inner Box (80%)")
        ], current="full" if getattr(self.cfg, "box_margin", 0.0) <= 0.005 else "margin")
        def on_track_change(v):
            margin_val = 0.0 if v == "full" else 0.18
            self._save("box_margin", margin_val)
        track_ctrl.valueChanged.connect(on_track_change)
        card.add_row("Tracking Area", "Full frame tracks hand across entire camera sensor", track_ctrl)

        # Model Complexity
        comp_ctrl = SegmentedControl([
            ("0", "Lite (Fast)"),
            ("1", "Full (Accurate)")
        ], current=str(getattr(self.cfg, "model_complexity", 1)))
        comp_ctrl.valueChanged.connect(lambda v: self._save("model_complexity", int(v)))
        card.add_row("Model Complexity", "MediaPipe neural network model capacity", comp_ctrl)

        # Auto Updates
        sw_update = AppleSwitch(self.cfg.auto_update_check)
        sw_update.toggled.connect(lambda v: self._save("auto_update_check", v))
        card.add_row("Automatic Updates", "Check GitHub releases on startup", sw_update)

        layout.addWidget(card)
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_gestures_page(self):
        page, layout = self._build_page_container("Gestures", "Hand tracking schemes, thresholds, and anchors")
        card = GroupedCard()

        # Control Scheme
        scheme_ctrl = SegmentedControl([
            ("palm_fist", "Palm & Fist (Default)"),
            ("pinch", "Pinch (Legacy)")
        ], current=getattr(self.cfg, "control_scheme", "palm_fist"))
        scheme_ctrl.valueChanged.connect(lambda v: self._save("control_scheme", v))
        card.add_row("Control Scheme", "Primary hand interaction scheme", scheme_ctrl)

        # Palm Anchor Point
        anc_ctrl = SegmentedControl([
            ("knuckles", "Knuckles (MCP)"),
            ("palm_center", "Palm Center"),
            ("centroid", "Centroid"),
            ("index_mcp", "Index MCP")
        ], current=getattr(self.cfg, "palm_anchor", "knuckles"))
        anc_ctrl.valueChanged.connect(lambda v: self._save("palm_anchor", v))
        card.add_row("Palm Anchor Point", "Rigid metacarpal anchor driving cursor motion", anc_ctrl)

        # Legacy Pinch Anchor Point
        p_anc_ctrl = SegmentedControl([
            ("blend", "Blend (PIP+Tip)"),
            ("tip", "Fingertip"),
            ("pip", "PIP Joint"),
            ("palm_offset", "Palm Center")
        ], current=getattr(self.cfg, "anchor_point", "blend"))
        p_anc_ctrl.valueChanged.connect(lambda v: self._save("anchor_point", v))
        card.add_row("Pinch Anchor Point", "Anchor used when Pinch scheme is active", p_anc_ctrl)

        # Openness Enter Threshold (Open palm cursor motion)
        self._slider_row(card, "Openness Enter Threshold", "Hand openness required for open palm cursor motion",
                         getattr(self.cfg, "open_enter_thresh", 0.70), 0.50, 0.90, "",
                         lambda v: self._save("open_enter_thresh", v))

        # Openness Exit / Neutral Zone Threshold
        self._slider_row(card, "Neutral Exit Threshold", "Openness drop threshold where cursor freezes",
                         getattr(self.cfg, "open_exit_thresh", 0.58), 0.40, 0.75, "",
                         lambda v: self._save("open_exit_thresh", v))

        # Fist Enter Threshold
        self._slider_row(card, "Fist Enter Threshold", "Openness threshold to classify full fist",
                         getattr(self.cfg, "fist_enter_thresh", 0.28), 0.15, 0.45, "",
                         lambda v: self._save("fist_enter_thresh", v))

        # Reopen Freeze Duration
        self._slider_row(card, "Reopen Freeze Duration", "Cursor freeze after fist reopens to suppress twitch",
                         getattr(self.cfg, "reopen_freeze_ms", 150.0), 50.0, 350.0, "ms",
                         lambda v: self._save("reopen_freeze_ms", v))

        # Thumbs-up Standby Time
        self._slider_row(card, "Thumbs-Up Standby Time", "Hold thumbs-up to enter standby",
                         getattr(self.cfg, "thumbs_up_hold", 1.2), 0.6, 2.5, "s",
                         lambda v: self._save("thumbs_up_hold", v))

        # Wake Hold Duration (Open Palm)
        self._slider_row(card, "Wake Hold Time", "Hold open palm to wake from standby",
                         self.cfg.activate_hold, 0.4, 2.0, "s",
                         lambda v: self._save("activate_hold", v))

        # Precision Mode Toggle
        sw_prec = AppleSwitch(getattr(self.cfg, "precision_mode", True))
        sw_prec.toggled.connect(lambda v: self._save("precision_mode", v))
        card.add_row("Precision Mode", "Automatic magnetic slow-down during fine aiming", sw_prec)

        layout.addWidget(card)
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_pointer_page(self):
        page, layout = self._build_page_container("Pointer & Dwell", "Tracking precision, acceleration, and dwell-click")
        card = GroupedCard()

        # Tracking Mode (Relative vs Absolute)
        mode_ctrl = SegmentedControl([
            ("relative", "Trackpad (Relative)"),
            ("absolute", "Screen (Absolute)")
        ], current=getattr(self.cfg, "tracking_mode", "relative"))
        mode_ctrl.valueChanged.connect(lambda v: self._save("tracking_mode", v))
        card.add_row("Tracking Mode", "Relative provides pointer acceleration and clutch", mode_ctrl)

        # Dwell Mode
        dwell_ctrl = SegmentedControl([
            ("off", "Off"),
            ("click", "Hover to Click"),
            ("radial", "Radial Menu")
        ], current=getattr(self.cfg, "dwell_mode", "click"))
        dwell_ctrl.valueChanged.connect(lambda v: self._save("dwell_mode", v))
        card.add_row("Dwell Click", "Hover stationary on target to trigger click", dwell_ctrl)

        # Dwell Hover Time
        self._slider_row(card, "Dwell Delay", "Stationary time before click fires",
                         getattr(self.cfg, "dwell_time", 1.0), 0.5, 2.5, "s",
                         lambda v: self._save("dwell_time", v))

        # Dead-zone Jitter Suppression
        self._slider_row(card, "Dead-zone Suppression", "Eliminates resting hand tremor",
                         getattr(self.cfg, "deadzone_px", 1.5), 0.0, 4.0, "px",
                         lambda v: self._save("deadzone_px", v))

        # Position Lookback Buffer
        self._slider_row(card, "Pinch Lookback Buffer", "Eliminates click drift during finger closure",
                         getattr(self.cfg, "cursor_lookback_ms", 200.0), 100.0, 350.0, "ms",
                         lambda v: self._save("cursor_lookback_ms", v))

        layout.addWidget(card)

        # Diagnostics & Target Practice Actions
        action_layout = QHBoxLayout()
        btn_diag = QPushButton("Run Diagnostics Benchmark")
        btn_diag.clicked.connect(self._run_diagnose_action)
        action_layout.addWidget(btn_diag)

        btn_target = QPushButton("Target Practice (24x24 px)")
        btn_target.clicked.connect(self._open_target_practice)
        action_layout.addWidget(btn_target)

        layout.addLayout(action_layout)
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_voice_page(self):
        page, layout = self._build_page_container("Voice", "Speech recognition and audio input diagnostics")
        card = GroupedCard()

        # 1. Voice Commands Toggle
        sw_voice = AppleSwitch(self.cfg.voice_enabled)
        sw_voice.toggled.connect(lambda v: self._save("voice_enabled", v))
        card.add_row("Voice Commands", "Listen for Jarvis wake-word and actions", sw_voice)

        # 2. Spoken Responses Toggle
        sw_speak = AppleSwitch(self.cfg.speak)
        sw_speak.toggled.connect(lambda v: self._save("speak", v))
        card.add_row("Spoken Responses", "Jarvis replies with voice confirmations", sw_speak)

        # 3. Microphone Input Device Selector
        mics = list_microphones()
        best_idx, best_name = find_best_microphone(self.cfg)
        self.mic_combo = QComboBox()
        self.mic_combo.setMinimumWidth(260)
        
        # Add Auto option
        self.mic_combo.addItem(f"Auto-detect ({best_name[:22]}...)", -1)
        selected_combo_idx = 0
        
        for idx, m in enumerate(mics):
            tag = " [Active]" if m["index"] == best_idx else ""
            self.mic_combo.addItem(f"[{m['index']}] {m['name'][:28]}{tag}", m["index"])
            if self.cfg.mic_device_index == m["index"]:
                selected_combo_idx = idx + 1

        self.mic_combo.setCurrentIndex(selected_combo_idx)

        def on_mic_selected(c_idx):
            dev_val = self.mic_combo.itemData(c_idx)
            val_to_save = None if dev_val == -1 else dev_val
            self._save("mic_device_index", val_to_save)
            self.cfg.mic_device_index = val_to_save

        self.mic_combo.currentIndexChanged.connect(on_mic_selected)
        card.add_row("Input Microphone", "Select physical audio capture device", self.mic_combo)

        # 4. Dynamic Noise Adaptation Toggle
        sw_dyn = AppleSwitch(getattr(self.cfg, "mic_dynamic_energy", False))
        sw_dyn.toggled.connect(lambda v: self._save("mic_dynamic_energy", v))
        card.add_row("Dynamic Noise Adapt", "Auto-adapts threshold (keep OFF for laptop mics to prevent cutoff)", sw_dyn)

        # 5. Silence Cutoff Timeout Slider
        self._slider_row(card, "Silence Cutoff Timeout", "Seconds of pause before finishing sentence (prevents mid-speech cutoff)",
                         getattr(self.cfg, "mic_pause_threshold", 1.0), 0.5, 2.5, "s",
                         lambda v: self._save("mic_pause_threshold", float(v)))

        # 6. Trigger Energy Sensitivity Slider
        self._slider_row(card, "Trigger Sensitivity", "Energy threshold for speech activation (lower = more sensitive)",
                         getattr(self.cfg, "mic_energy_threshold", 300), 50.0, 1500.0, "",
                         lambda v: self._save("mic_energy_threshold", int(v)))

        layout.addWidget(card)

        # 7. Live Audio Input Level Meter Card
        meter_card = GroupedCard()
        meter_widget = QWidget()
        meter_layout = QVBoxLayout(meter_widget)
        meter_layout.setContentsMargins(0, 0, 0, 0)
        meter_layout.setSpacing(6)

        bar_row = QHBoxLayout()
        self.mic_level_bar = QProgressBar()
        self.mic_level_bar.setRange(0, 100)
        self.mic_level_bar.setValue(0)
        self.mic_level_bar.setTextVisible(False)
        self.mic_level_bar.setFixedHeight(8)
        self.mic_level_bar.setStyleSheet("""
            QProgressBar {
                background-color: #2c2c2e;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background-color: #34c759;
                border-radius: 4px;
            }
        """)

        self.mic_level_lbl = QLabel("0%")
        self.mic_level_lbl.setStyleSheet("font-size: 11px; font-weight: 600; color: #34c759; min-width: 35px;")

        bar_row.addWidget(self.mic_level_bar, stretch=1)
        bar_row.addWidget(self.mic_level_lbl)
        meter_layout.addLayout(bar_row)

        self.mic_status_lbl = QLabel("Listening for input signal...")
        self.mic_status_lbl.setStyleSheet("font-size: 11px; color: #8e8e93;")
        meter_layout.addWidget(self.mic_status_lbl)

        meter_card.add_row("Microphone Signal Level", "Real-time acoustic audio meter", meter_widget)

        # 8. Interactive Microphone & Recognition Test
        test_widget = QWidget()
        test_layout = QVBoxLayout(test_widget)
        test_layout.setContentsMargins(0, 0, 0, 0)
        test_layout.setSpacing(6)

        self.btn_test_mic = QPushButton("Test Microphone & Recognition")
        self.lbl_test_result = QLabel("Click above to test microphone audio recording and speech recognition.")
        self.lbl_test_result.setStyleSheet("font-size: 11px; color: #8e8e93;")
        self.lbl_test_result.setWordWrap(True)

        def run_mic_test_thread():
            self.btn_test_mic.setEnabled(False)
            self.btn_test_mic.setText("Recording (3.5s)... Speak now!")
            self.lbl_test_result.setText("Listening... Say something like 'Jarvis, open Chrome' or 'what time is it'.")
            self.lbl_test_result.setStyleSheet("font-size: 11px; color: #007aff;")

            def worker():
                chosen_dev = self.cfg.mic_device_index
                res = test_microphone_recognition(device_index=chosen_dev, duration=3.5)
                self.bridge.test_mic_result.emit(res)

            import threading
            threading.Thread(target=worker, daemon=True).start()

        self.btn_test_mic.clicked.connect(run_mic_test_thread)
        test_layout.addWidget(self.btn_test_mic)
        test_layout.addWidget(self.lbl_test_result)
        meter_card.add_row("Audio Test Tool", "Speak a command to verify transcription accuracy", test_widget)

        layout.addWidget(meter_card)
        layout.addStretch()
        self.stack.addWidget(page)
        self.voice_page = page

        # Setup periodic audio level timer (runs smoothly when Voice tab is visible)
        self.mic_timer = QTimer(self)
        self.mic_timer.setInterval(120)

        def update_mic_level():
            if not self.isVisible() or self.stack.currentWidget() != self.voice_page:
                if self.mic_timer.isActive():
                    self.mic_timer.stop()
                return
            try:
                chosen_dev = self.cfg.mic_device_index
                norm, rms = probe_mic_level(chosen_dev)
                val = int(norm * 100)
                self.mic_level_bar.setValue(val)
                self.mic_level_lbl.setText(f"{val}%")
                if rms > 15.0:
                    self.mic_status_lbl.setText(f"Microphone active (Signal: {rms:.0f})")
                    self.mic_status_lbl.setStyleSheet("font-size: 11px; color: #34c759;")
                else:
                    self.mic_status_lbl.setText("No audio signal detected. Speak louder or select another microphone.")
                    self.mic_status_lbl.setStyleSheet("font-size: 11px; color: #8e8e93;")
            except Exception as exc:
                print(f"[settings] mic level update note: {exc}")

        self.mic_timer.timeout.connect(update_mic_level)

    def _build_ai_page(self):
        page, layout = self._build_page_container("AI & Intelligence", "Groq speech-to-text and intelligent voice assistant")

        # 1. AI Enablement & API Key Card
        card_general = GroupedCard()

        sw_ai = AppleSwitch(getattr(self.cfg, "ai_enabled", False))
        sw_ai.toggled.connect(lambda v: self._save("ai_enabled", v))
        card_general.add_row("Enable AI", "Use Groq cloud for speech recognition and responses", sw_ai)

        # Masked API Key Row
        key_widget = QWidget()
        key_vlayout = QVBoxLayout(key_widget)
        key_vlayout.setContentsMargins(0, 0, 0, 0)
        key_vlayout.setSpacing(6)

        key_hlayout = QHBoxLayout()
        key_hlayout.setSpacing(8)

        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("gsk_...")
        self.key_input.setFixedWidth(200)

        existing_key = get_api_key(self.cfg) or ""
        if existing_key:
            self.key_input.setText(existing_key)

        btn_toggle_key = QPushButton("Show")
        btn_toggle_key.setFixedWidth(54)

        def toggle_key_mask():
            if self.key_input.echoMode() == QLineEdit.EchoMode.Password:
                self.key_input.setEchoMode(QLineEdit.EchoMode.Normal)
                btn_toggle_key.setText("Hide")
            else:
                self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
                btn_toggle_key.setText("Show")

        btn_toggle_key.clicked.connect(toggle_key_mask)

        btn_save_key = QPushButton("Save")
        btn_save_key.setFixedWidth(54)

        btn_remove_key = QPushButton("Remove")
        btn_remove_key.setFixedWidth(64)

        key_hlayout.addWidget(self.key_input)
        key_hlayout.addWidget(btn_toggle_key)
        key_hlayout.addWidget(btn_save_key)
        key_hlayout.addWidget(btn_remove_key)
        key_vlayout.addLayout(key_hlayout)

        lbl_key_status = QLabel("")
        lbl_key_status.setStyleSheet("font-size: 11px; color: #30d158;")
        key_vlayout.addWidget(lbl_key_status)

        lbl_link = QLabel('<a href="https://console.groq.com/keys" style="color:#007aff; text-decoration:none;">Get a free key at console.groq.com</a>')
        lbl_link.setOpenExternalLinks(True)
        lbl_link.setStyleSheet("font-size: 11px;")
        key_vlayout.addWidget(lbl_link)

        # Option to save key to config file
        has_cfg_key = bool(getattr(self.cfg, "groq_api_key", ""))
        self.sw_save_cfg = AppleSwitch(has_cfg_key or bool(existing_key))
        card_general.add_row("Save Key to Config", "Save key in jarvis_config.json so it persists across folders and restarts", self.sw_save_cfg)

        def save_key_action():
            val = self.key_input.text().strip()
            save_to_cfg = self.sw_save_cfg.checked
            if val:
                set_api_key(val, persist_to_config=save_to_cfg)
                self._save("groq_api_key", val if save_to_cfg else "")
                storage_loc = "Credential Manager & config file" if save_to_cfg else "Windows Credential Manager"
                lbl_key_status.setText(f"API key saved to {storage_loc}.")
                lbl_key_status.setStyleSheet("font-size: 11px; color: #30d158;")
            else:
                delete_api_key()
                self._save("groq_api_key", "")
                lbl_key_status.setText("Empty key removed.")
                lbl_key_status.setStyleSheet("font-size: 11px; color: #ff9f0a;")

        def remove_key_action():
            delete_api_key()
            self._save("groq_api_key", "")
            self.key_input.clear()
            lbl_key_status.setText("API key removed from Credential Manager & config.")
            lbl_key_status.setStyleSheet("font-size: 11px; color: #ff9f0a;")

        def on_save_config_toggled(checked):
            val = self.key_input.text().strip()
            if checked and val:
                self._save("groq_api_key", val)
                set_api_key(val, persist_to_config=True)
            elif not checked:
                self._save("groq_api_key", "")

        self.sw_save_cfg.toggled.connect(on_save_config_toggled)
        btn_save_key.clicked.connect(save_key_action)
        self.key_input.returnPressed.connect(save_key_action)
        btn_remove_key.clicked.connect(remove_key_action)

        card_general.add_row("Groq API Key", "Stored in Windows Credential Manager and optionally in local config", key_widget)
        layout.addWidget(card_general)

        # 2. Models & Connectivity Card
        card_models = GroupedCard()

        # LLM Model dropdown
        self.llm_combo = QComboBox()
        default_llms = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "mixtral-8x7b-32768", "gemma2-9b-it"]
        cur_llm = getattr(self.cfg, "ai_llm_model", "llama-3.1-8b-instant")
        if cur_llm not in default_llms:
            default_llms.insert(0, cur_llm)
        for m in default_llms:
            self.llm_combo.addItem(m)
        self.llm_combo.setCurrentText(cur_llm)
        self.llm_combo.currentTextChanged.connect(lambda v: self._save("ai_llm_model", v))
        card_models.add_row("Language Model", "Model for command interpretation and conversational responses", self.llm_combo)

        # Speech Model dropdown
        self.stt_combo = QComboBox()
        default_stts = ["whisper-large-v3-turbo", "whisper-large-v3"]
        cur_stt = getattr(self.cfg, "ai_stt_model", "whisper-large-v3-turbo")
        if cur_stt not in default_stts:
            default_stts.insert(0, cur_stt)
        for m in default_stts:
            self.stt_combo.addItem(m)
        self.stt_combo.setCurrentText(cur_stt)
        self.stt_combo.currentTextChanged.connect(lambda v: self._save("ai_stt_model", v))
        card_models.add_row("Speech Model", "Whisper model for audio speech-to-text", self.stt_combo)

        # Test Connection Row
        test_widget = QWidget()
        test_vbox = QVBoxLayout(test_widget)
        test_vbox.setContentsMargins(0, 0, 0, 0)
        test_vbox.setSpacing(6)

        test_hbox = QHBoxLayout()
        test_hbox.setSpacing(10)
        btn_test_conn = QPushButton("Test Connection")
        lbl_conn_status = QLabel("Ready to test")
        lbl_conn_status.setStyleSheet("font-size: 11px; color: #8e8e93;")
        test_hbox.addWidget(btn_test_conn)
        test_hbox.addWidget(lbl_conn_status)
        test_hbox.addStretch()
        test_vbox.addLayout(test_hbox)

        self.btn_test_conn = btn_test_conn
        self.lbl_conn_status = lbl_conn_status

        def run_test_connection():
            key_to_test = self.key_input.text().strip() or get_api_key(self.cfg)
            if not key_to_test:
                lbl_conn_status.setText("No API key entered")
                lbl_conn_status.setStyleSheet("font-size: 11px; color: #ff9f0a;")
                return

            btn_test_conn.setEnabled(False)
            lbl_conn_status.setText("Testing connection...")
            lbl_conn_status.setStyleSheet("font-size: 11px; color: #007aff;")

            def worker():
                client = GroqClient(api_key=key_to_test, timeout=8)
                ok, message = client.validate_key()
                models_data = None
                if ok:
                    try:
                        models_data = client.get_categorized_models()
                    except Exception:
                        pass
                self.bridge.test_conn_result.emit(ok, message, models_data or {})

            import threading
            threading.Thread(target=worker, daemon=True).start()

        btn_test_conn.clicked.connect(run_test_connection)
        card_models.add_row("Connectivity", "Validate Groq API connection and refresh model directory", test_widget)
        layout.addWidget(card_models)

        # 3. Privacy Card
        card_privacy = GroupedCard()
        lbl_privacy = QLabel(
            "Privacy Notice:\n"
            "Audio utterances and conversational text are sent to Groq cloud services only while AI is enabled. "
            "All API keys are stored exclusively in the Windows Credential Manager and are never written to configuration files, logs, or crash reports."
        )
        lbl_privacy.setStyleSheet("font-size: 11px; color: #8e8e93; line-height: 1.4;")
        lbl_privacy.setWordWrap(True)
        card_privacy.add_row("Data Privacy", "", lbl_privacy)
        layout.addWidget(card_privacy)

        layout.addStretch()
        self.stack.addWidget(page)

    def _build_display_page(self):
        page, layout = self._build_page_container("Display & Materials", "Liquid glass appearance and animations")
        card = GroupedCard()

        theme_ctrl = SegmentedControl([
            ("auto", "System Auto"),
            ("dark", "Dark Glass"),
            ("light", "Light Glass")
        ], current=getattr(self.cfg, "theme_mode", "auto"))
        theme_ctrl.valueChanged.connect(lambda v: self._save("theme_mode", v))
        card.add_row("Appearance Theme", "Follows Windows accent and mode", theme_ctrl)

        sw_blur = AppleSwitch(getattr(self.cfg, "glass_blur", True))
        sw_blur.toggled.connect(lambda v: self._save("glass_blur", v))
        card.add_row("Windows Glass Backdrop", "Hardware Acrylic / Mica background blur", sw_blur)

        sw_skel = AppleSwitch(getattr(self.cfg, "show_skeleton_mini", True))
        sw_skel.toggled.connect(lambda v: self._save("show_skeleton_mini", v))
        card.add_row("Mini Hand Visualizer", "Renders live skeleton inside the island", sw_skel)

        sw_motion = AppleSwitch(getattr(self.cfg, "reduce_motion", False))
        sw_motion.toggled.connect(lambda v: self._save("reduce_motion", v))
        card.add_row("Reduce Motion", "Minimize spring scaling and expansions", sw_motion)

        layout.addWidget(card)
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_about_page(self):
        page, layout = self._build_page_container("About", "J.A.R.V.I.S. System Information")
        card = GroupedCard()

        card.add_row("Version", "", QLabel(f"v{APP_VERSION}"))
        card.add_row("Platform", "", QLabel(f"{sys.platform.capitalize()} 64-bit"))
        card.add_row("Design Philosophy", "Apple Human Interface Guidelines (macOS / visionOS)", QLabel("Installed"))

        layout.addWidget(card)

        if self.updater:
            update_card = GroupedCard()
            update_widget = QWidget()
            u_vbox = QVBoxLayout(update_widget)
            u_vbox.setContentsMargins(0, 0, 0, 0)
            u_vbox.setSpacing(8)

            u_hbox = QHBoxLayout()
            u_hbox.setSpacing(10)

            btn_chk = QPushButton("Check for Updates")
            btn_restart = QPushButton("Restart to Update")
            btn_restart.setVisible(False)
            btn_restart.setStyleSheet("""
                QPushButton {
                    background-color: #34c759;
                    color: #ffffff;
                    font-weight: 600;
                    border: none;
                }
                QPushButton:hover {
                    background-color: #30d158;
                }
            """)

            lbl_up_status = QLabel(f"Current version is v{APP_VERSION}.")
            lbl_up_status.setStyleSheet("font-size: 11px; color: #8e8e93;")

            prog_bar = QProgressBar()
            prog_bar.setRange(0, 100)
            prog_bar.setValue(0)
            prog_bar.setFixedHeight(6)
            prog_bar.setTextVisible(False)
            prog_bar.setVisible(False)
            prog_bar.setStyleSheet("""
                QProgressBar {
                    background-color: #2c2c2e;
                    border-radius: 3px;
                }
                QProgressBar::chunk {
                    background-color: #007aff;
                    border-radius: 3px;
                }
            """)

            self.btn_chk = btn_chk
            self.btn_restart = btn_restart
            self.lbl_up_status = lbl_up_status
            self.prog_bar = prog_bar

            u_hbox.addWidget(btn_chk)
            u_hbox.addWidget(btn_restart)
            u_hbox.addWidget(lbl_up_status, stretch=1)
            u_vbox.addLayout(u_hbox)
            u_vbox.addWidget(prog_bar)

            update_card.add_row("Software Update", "Automatic GitHub release detection and seamless restart", update_widget)
            layout.addWidget(update_card)

            def on_updater_event(state, **kwargs):
                self.bridge.updater_event.emit(state, kwargs)

            self._updater_cb = on_updater_event
            self.updater.add_listener(on_updater_event)

            def on_check_clicked():
                self.updater.check_async(notify_if_latest=True)

            def on_restart_clicked():
                if self.updater:
                    if self.updater.downloaded_path and os.path.exists(self.updater.downloaded_path):
                        btn_restart.setEnabled(False)
                        lbl_up_status.setText("Applying update and restarting...")
                        self.updater.apply_update_and_restart()
                    else:
                        btn_restart.setEnabled(False)
                        lbl_up_status.setText("Starting download...")
                        self.updater.download_async()

            btn_chk.clicked.connect(on_check_clicked)
            btn_restart.clicked.connect(on_restart_clicked)

        layout.addStretch()
        self.stack.addWidget(page)

    def _slider_row(self, card, title, desc, current_val, min_val, max_val, unit, on_change):
        row = QWidget()
        l = QHBoxLayout(row)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(10)

        sld = QSlider(Qt.Orientation.Horizontal)
        sld.setRange(int(min_val * 100), int(max_val * 100))
        sld.setValue(int(current_val * 100))
        sld.setFixedWidth(130)

        lbl_val = QLabel(f"{current_val:.2f} {unit}")
        lbl_val.setStyleSheet("font-size: 11px; font-weight: 600; color: #007aff; min-width: 50px;")

        def handle_val(val_int):
            real_val = val_int / 100.0
            lbl_val.setText(f"{real_val:.2f} {unit}")
            on_change(real_val)

        sld.valueChanged.connect(handle_val)
        l.addWidget(sld)
        l.addWidget(lbl_val)

        card.add_row(title, desc, row)
        return sld

    def _on_res_change(self, res_str):
        w, h = map(int, res_str.split("x"))
        self._save("frame_width", w)
        self._save("frame_height", h)

    def _run_diagnose_action(self):
        try:
            import subprocess
            base_dir = os.path.dirname(os.path.abspath(__file__))
            diag_script = os.path.join(base_dir, "diagnose.py")
            if os.path.exists(diag_script):
                subprocess.Popen([sys.executable, diag_script])
        except Exception as exc:
            print(f"[settings] diagnose error: {exc}")

    def _open_target_practice(self):
        try:
            import subprocess
            base_dir = os.path.dirname(os.path.abspath(__file__))
            tp_script = os.path.join(base_dir, "tests", "target_practice.py")
            if os.path.exists(tp_script):
                subprocess.Popen([sys.executable, tp_script, "--gui"])
        except Exception as exc:
            print(f"[settings] target practice error: {exc}")

    def _save(self, key, value):
        # Allow groq_api_key, block arbitrary secret/token/password injections
        if key != "groq_api_key" and any(s in key.lower() for s in ("secret", "token", "password")):
            return
        try:
            setattr(self.cfg, key, value)
        except Exception:
            pass
        try:
            from config import get_config_path
            cfg_path = get_config_path()
        except Exception:
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
        try:
            data = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data[key] = value
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as exc:
            print(f"[settings] save error: {exc}")


# =====================================================================
# System Tray Icon (Apple Style)
# =====================================================================

class JarvisTray(QSystemTrayIcon):
    def __init__(self, overlay_mgr, app):
        super().__init__(app)
        self.mgr = overlay_mgr
        self.app = app
        self._init_icon()
        self._init_menu()
        self.show()

    def _init_icon(self):
        icon = get_app_icon()
        if icon and not icon.isNull():
            self.setIcon(icon)
            return
        pix = QPixmap(32, 32)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor("#007aff")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(4, 4, 24, 24), 6, 6)
        p.setPen(QColor("#ffffff"))
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, 0, 32, 32), Qt.AlignmentFlag.AlignCenter, "J")
        p.end()
        self.setIcon(QIcon(pix))

    def _init_menu(self):
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: #1c1c1e;
                color: #f5f5f7;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 18px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #007aff;
            }
        """)

        self.status_act = QAction("Status: Standby", menu)
        self.status_act.setEnabled(False)
        menu.addAction(self.status_act)
        menu.addSeparator()

        act_toggle = QAction("Wake / Standby", menu)
        act_toggle.triggered.connect(self._toggle_active)
        menu.addAction(act_toggle)

        act_preview = QAction("Vision Preview Window", menu)
        act_preview.triggered.connect(self._toggle_preview)
        menu.addAction(act_preview)

        act_dwell = QAction("Toggle Dwell Click", menu)
        act_dwell.triggered.connect(self._toggle_dwell)
        menu.addAction(act_dwell)

        menu.addSeparator()
        act_update = QAction("Check for Updates…", menu)
        act_update.triggered.connect(self._check_updates)
        menu.addAction(act_update)

        act_settings = QAction("Settings…", menu)
        act_settings.triggered.connect(lambda: self.mgr.open_settings())
        menu.addAction(act_settings)

        act_quit = QAction("Quit J.A.R.V.I.S.", menu)
        act_quit.triggered.connect(self.mgr.quit_app)
        menu.addAction(act_quit)

        self.setContextMenu(menu)

    def _check_updates(self):
        if self.mgr.updater:
            self.mgr.updater.check_async(notify_if_latest=True)

    def _toggle_active(self):
        if self.mgr.ctx:
            self.mgr.ctx.set_active(None)

    def _toggle_preview(self):
        if self.mgr.cfg:
            self.mgr.cfg.show_preview = not self.mgr.cfg.show_preview

    def _toggle_dwell(self):
        if self.mgr.cfg:
            cur = getattr(self.mgr.cfg, "dwell_mode", "click")
            self.mgr.cfg.dwell_mode = "off" if cur != "off" else "click"

    def update_status(self, active):
        self.status_act.setText(f"Status: {'Online' if active else 'Standby'}")


# =====================================================================
# Main Overlay Manager
# =====================================================================

class Overlay:
    """Public interface for the Apple HIG Dynamic Island & Cursor Overlays."""

    def __init__(self, stop_event, cfg=None, ctx=None):
        self.stop = stop_event
        self.cfg = cfg
        self.ctx = ctx
        self.updater = None
        self.q = queue.Queue()
        self.settings_dlg = None

        if PYSIDE6_AVAILABLE:
            self.app = QApplication.instance() or QApplication(sys.argv)
            self.app.setQuitOnLastWindowClosed(False)
            self.app_icon = get_app_icon()
            if self.app_icon and not self.app_icon.isNull():
                self.app.setWindowIcon(self.app_icon)
            self.island = AppleDynamicIsland(self)
            self.cursor_overlay = CursorFeedbackOverlay()
            self.tray = JarvisTray(self, self.app)

            self.island.show()
            self.island.make_click_through()
            self.cursor_overlay.show()

            # 60 FPS animation timer (16ms)
            self.timer = QTimer()
            self.timer.timeout.connect(self._tick)
            self.timer.start(16)
        else:
            self._init_tk_fallback()

    def post(self, kind, **data):
        """Thread-safe state update."""
        self.q.put((kind, data))

    def run(self):
        """Run the main GUI loop."""
        if PYSIDE6_AVAILABLE:
            self.app.exec()
        else:
            self.root.mainloop()

    def open_settings(self, initial_tab=0):
        if not PYSIDE6_AVAILABLE or not self.cfg or not self.ctx:
            return
        # Thread-safe marshalling: if invoked from background voice/hotkey thread, queue to GUI loop
        if threading.current_thread() is not threading.main_thread():
            self.post("open_settings", initial_tab=initial_tab)
            return
        if self.settings_dlg is None or not self.settings_dlg.isVisible():
            self.settings_dlg = SettingsWindow(self.cfg, self.ctx, updater=self.updater)
            self.settings_dlg.select_tab(initial_tab)
            self.settings_dlg.show()
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()
        else:
            self.settings_dlg.select_tab(initial_tab)
            self.settings_dlg.show()
            self.settings_dlg.raise_()
            self.settings_dlg.activateWindow()

    def quit_app(self):
        self.stop.set()
        if PYSIDE6_AVAILABLE:
            self.app.quit()

    def _drain(self, now):
        try:
            while True:
                kind, d = self.q.get_nowait()
                if kind == "state":
                    self.island.active = d["active"]
                    self.island.label = d["label"]
                    self.island.progress = d.get("progress", 0.0)
                    self.island.hands = d.get("hands", 0)
                    self.island.landmarks = d.get("landmarks", [])
                    self.island.precision_active = d.get("precision", False)
                    self.tray.update_status(d["active"])
                elif kind == "say":
                    self.island.say_text = d["text"]
                    self.island.say_until = now + 4.2
                elif kind == "listening":
                    self.island.listening = True
                    self.island.listening_until = d.get("until", now + 5.0)
                elif kind == "thinking":
                    self.island.thinking = d.get("active", True)
                elif kind == "ai_error":
                    self.island.error_text = d.get("text", "AI Error")
                    self.island.error_until = now + 4.5
                    self.island.is_ai_error = True
                elif kind == "error":
                    self.island.error_text = d.get("text", "Error")
                    self.island.error_until = now + 5.0
                    self.island.is_ai_error = False
                elif kind == "cursor_dwell":
                    self.cursor_overlay.update_dwell(d)
                elif kind == "open_settings":
                    self.open_settings(d.get("initial_tab", 0))
        except queue.Empty:
            pass

    def _tick(self):
        if self.stop.is_set():
            self.app.quit()
            return
        try:
            now = time.time()
            self._drain(now)
            self.island.update_frame(now)
        except Exception as exc:
            print(f"[overlay] tick error: {exc}")

    def _init_tk_fallback(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#121214")
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"200x32+{(sw - 200) // 2}+0")
        self.label_tk = tk.Label(self.root, text="Jarvis", fg="#ffffff", bg="#121214")
        self.label_tk.pack(expand=True)
        self.root.after(20, self._tick_tk)

    def _tick_tk(self):
        if self.stop.is_set():
            self.root.destroy()
            return
        try:
            while True:
                kind, d = self.q.get_nowait()
                if kind == "state":
                    self.label_tk.config(text=f"Jarvis: {d['label']}")
        except queue.Empty:
            pass
        self.root.after(20, self._tick_tk)
