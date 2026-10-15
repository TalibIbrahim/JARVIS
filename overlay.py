"""
Dynamic-Island overlay for J.A.R.V.I.S. using PySide6.

Features:
- Frameless, translucent, click-through, always-on-top window at top-center of the screen.
- Smooth spring-style expand/collapse animation between Standby and Active states.
- Tony Stark Arc-Reactor motif that pulses and spins faster when active.
- Wake-progress circular ring around the arc reactor during palm hold.
- Distinct states: Standby, Tracking, Gesture Flash, Listening, Speaking (with animated waveform), Error.
- Mini live hand-skeleton visualizer inside the island.
- System tray icon with status, Wake/Standby toggle, Vision Preview toggle, Settings dialog, and Quit.
- Comprehensive Settings window with camera picker, sensitivity sliders, audio toggles, and live calibration.
- Persists settings to jarvis_config.json.
- Thread-safe post() interface preserving full backward compatibility.
"""
import ctypes
import json
import math
import os
import queue
import sys
import time

try:
    from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
    from PySide6.QtGui import (
        QPainter, QColor, QPen, QBrush, QFont, QPainterPath,
        QLinearGradient, QRadialGradient, QPixmap, QIcon, QAction
    )
    from PySide6.QtWidgets import (
        QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
        QLabel, QSlider, QCheckBox, QPushButton, QComboBox,
        QGroupBox, QSystemTrayIcon, QMenu, QTabWidget
    )
    PYSIDE6_AVAILABLE = True
except ImportError:
    PYSIDE6_AVAILABLE = False
    import tkinter as tk

# Brand colors
CYAN = "#00e5ff"
CYAN_DIM = "#0088a3"
GLOW = "#08323b"
DARK_BG = "#05080e"
DARK_SURFACE = "#09101a"
SLEEP_EDGE = "#1c3b47"
GOLD = "#ffc94d"
WHITE = "#e8fbff"
RED = "#ff4444"


class DynamicIsland(QWidget):
    """The floating notch pill that hugs the top-center of the screen."""

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

        self.w = 140.0
        self.h = 32.0
        self.target_w = 140.0
        self.target_h = 32.0

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

        self.t0 = time.time()
        self.resize(int(self.w), int(self.h))
        self._center_top()

    def make_click_through(self):
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            # WS_EX_LAYERED (0x80000) | WS_EX_TRANSPARENT (0x20) | WS_EX_TOOLWINDOW (0x80) | WS_EX_NOACTIVATE (0x08000000)
            user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
        except Exception as exc:
            print(f"[overlay] click-through error: {exc}")

    def _center_top(self):
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.geometry()
            cx = geom.x() + (geom.width() - int(self.w)) // 2
            self.setGeometry(cx, geom.y(), int(round(self.w)), int(round(self.h)))

    def update_frame(self, now):
        # Determine target dimensions based on state
        is_error = now < self.error_until
        is_speaking = now < self.say_until
        is_listening = now < self.listening_until

        if is_error:
            tw, th = 340.0, 48.0
        elif is_speaking:
            tw, th = 450.0, 64.0
        elif is_listening:
            tw, th = 330.0, 46.0
        elif self.label in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT"):
            tw, th = 330.0, 46.0
        elif self.active:
            tw, th = 280.0, 44.0
        else:
            tw, th = 140.0, 32.0

        self.target_w = tw
        self.target_h = th

        # Spring-style smooth interpolation
        self.w += (self.target_w - self.w) * 0.20
        self.h += (self.target_h - self.h) * 0.20

        self._center_top()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        now = time.time()
        t = now - self.t0
        w, h = float(self.width()), float(self.height())
        is_speaking = now < self.say_until
        is_listening = now < self.listening_until
        is_error = now < self.error_until

        # 1. Soft Outer Glow
        glow_col = QColor(RED if is_error else (CYAN if self.active else GLOW))
        glow_col.setAlpha(45 if self.active else 20)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow_col))
        p.drawRoundedRect(QRectF(1, -20, w - 2, h + 20), 22, 22)

        # 2. Dynamic Island Body Pill
        pill_path = QPainterPath()
        pill_path.addRoundedRect(QRectF(2, -20, w - 4, h + 18), 20, 20)
        
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor("#09101a"))
        grad.setColorAt(1.0, QColor("#05080e"))
        p.fillPath(pill_path, QBrush(grad))

        # Border
        border_col = QColor(RED if is_error else (CYAN if self.active else SLEEP_EDGE))
        border_pen = QPen(border_col, 1.2)
        p.strokePath(pill_path, border_pen)

        # 3. Tony Stark Arc Reactor Motif (Left)
        rx, ry = 24.0, h / 2.0
        R = 8.0 + 3.0 * min(max((h - 32.0) / 16.0, 0.0), 1.0)

        # Outer ring
        p.setPen(QPen(border_col, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(rx, ry), R, R)

        # Segmented spinning blades
        spin_speed = 240.0 if self.active else 35.0
        spin = (t * spin_speed) % 360.0
        blade_pen = QPen(QColor(CYAN if self.active else CYAN_DIM), 1.8)
        p.setPen(blade_pen)
        blade_box = QRectF(rx - R + 2.0, ry - R + 2.0, (R - 2.0) * 2.0, (R - 2.0) * 2.0)
        for k in range(3):
            p.drawArc(blade_box, int((spin + k * 120.0) * 16), int(60.0 * 16))

        # Pulsing Core
        pulse = 0.5 + 0.5 * math.sin(t * (6.0 if self.active else 2.5))
        core_r = 2.0 + (1.5 * pulse if self.active else 0.4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(WHITE if self.active else CYAN_DIM)))
        p.drawEllipse(QPointF(rx, ry), core_r, core_r)

        # Wake-progress Circular Ring around Arc Reactor (Palm Hold)
        if self.progress > 0.01:
            ring_pen = QPen(QColor(GOLD), 2.5)
            p.setPen(ring_pen)
            ring_box = QRectF(rx - R - 2.5, ry - R - 2.5, (R + 2.5) * 2.0, (R + 2.5) * 2.0)
            p.drawArc(ring_box, int(90 * 16), -int(self.progress * 360 * 16))

        # 4. Typography & State Content
        tx = rx + R + 14.0
        font = QFont("Consolas", 9, QFont.Weight.Bold)
        p.setFont(font)

        if h < 38.0:
            # Compact standby mode
            p.setPen(QColor(CYAN if self.active else SLEEP_EDGE))
            p.drawText(int(tx), int(ry + 4), "J.A.R.V.I.S")
        else:
            # Expanded mode
            p.setPen(QColor(CYAN))
            p.drawText(int(tx), int(ry - 4), "J.A.R.V.I.S")

            if is_error:
                p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
                p.setPen(QColor(RED))
                p.drawText(int(tx), int(ry + 12), self.error_text)
            elif is_speaking:
                # Animated Audio Waveform
                wave_x = w - 46.0
                for i in range(5):
                    bar_h = 3.0 + 9.0 * abs(math.sin(t * 8.0 + i * 0.9))
                    p.fillRect(QRectF(wave_x + i * 5, ry - bar_h / 2, 3, bar_h), QColor(CYAN))

                p.setFont(QFont("Consolas", 8))
                p.setPen(QColor(WHITE))
                text_disp = self.say_text if len(self.say_text) <= 42 else self.say_text[:40] + "…"
                p.drawText(int(tx), int(ry + 12), text_disp)
            elif is_listening:
                p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
                p.setPen(QColor(CYAN))
                p.drawText(int(tx), int(ry + 12), "LISTENING...")
            else:
                hot = self.label in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT")
                p.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
                p.setPen(QColor(GOLD if hot else CYAN_DIM))
                p.drawText(int(tx), int(ry + 12), self.label)

        # 5. Live Mini Hand-Skeleton (when active and tracked)
        if self.active and self.landmarks and w > 240.0:
            skel_x = w - 40.0
            skel_y = ry - 14.0
            box_sz = 28.0
            
            p.setPen(QPen(QColor(30, 60, 75), 1.0))
            p.setBrush(QBrush(QColor(10, 20, 30, 160)))
            p.drawRoundedRect(QRectF(skel_x, skel_y, box_sz, box_sz), 4, 4)

            hand_lm = self.landmarks[0]
            pts = [(skel_x + lm[0] * box_sz, skel_y + lm[1] * box_sz) for lm in hand_lm]
            
            p.setPen(QPen(QColor(CYAN), 1.0))
            # Draw primary finger rays
            rays = [(0, 4), (0, 8), (0, 12), (0, 16), (0, 20)]
            for a, b in rays:
                if a < len(pts) and b < len(pts):
                    p.drawLine(QPointF(pts[a][0], pts[a][1]), QPointF(pts[b][0], pts[b][1]))
            # Pinch indicator dot
            if len(pts) > 8:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(GOLD if self.label in ("CLICK", "DRAG") else CYAN)))
                p.drawEllipse(QPointF(pts[8][0], pts[8][1]), 2, 2)


class SettingsWindow(QDialog):
    """Configuration, tuning sliders, and interactive calibration wizard."""

    def __init__(self, cfg, ctx, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.ctx = ctx
        self.setWindowTitle("J.A.R.V.I.S. // System Settings")
        self.setFixedSize(540, 560)
        self.setStyleSheet("""
            QDialog {
                background-color: #0b1118;
                color: #e8fbff;
                font-family: Consolas, Segoe UI, sans-serif;
            }
            QGroupBox {
                border: 1px solid #1c3b47;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 14px;
                font-weight: bold;
                color: #00e5ff;
            }
            QLabel {
                color: #b0d6df;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #142832;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #00e5ff;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #ffc94d;
                border: 1px solid #ffffff;
                width: 14px;
                margin-top: -4px;
                margin-bottom: -4px;
                border-radius: 7px;
            }
            QCheckBox {
                color: #e8fbff;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #00e5ff;
                border-radius: 3px;
                background: #0d1a24;
            }
            QCheckBox::indicator:checked {
                background: #00e5ff;
            }
            QPushButton {
                background-color: #122c38;
                border: 1px solid #00e5ff;
                border-radius: 4px;
                color: #00e5ff;
                padding: 6px 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #00e5ff;
                color: #05080e;
            }
            QComboBox {
                background-color: #0d1a24;
                border: 1px solid #1c3b47;
                border-radius: 4px;
                color: #e8fbff;
                padding: 4px 8px;
            }
        """)

        layout = QVBoxLayout(self)

        # Tabs: Settings & Calibration
        tabs = QTabWidget()
        tabs.setStyleSheet("QTabBar::tab { background: #0e1c26; color: #b0d6df; padding: 6px 16px; } QTabBar::tab:selected { background: #163647; color: #00e5ff; font-weight: bold; }")
        layout.addWidget(tabs)

        # --- Tab 1: General Settings ---
        tab_general = QWidget()
        gen_layout = QVBoxLayout(tab_general)

        # 1. Camera
        cam_grp = QGroupBox("Camera Input")
        cam_l = QHBoxLayout(cam_grp)
        cam_l.addWidget(QLabel("Camera Device:"))
        self.cam_combo = QComboBox()
        self.cam_combo.addItems([f"Camera {i}" for i in range(4)])
        self.cam_combo.setCurrentIndex(min(max(cfg.camera_index, 0), 3))
        cam_l.addWidget(self.cam_combo)
        gen_layout.addWidget(cam_grp)

        # 2. Gesture Sensitivity
        sens_grp = QGroupBox("Sensitivity & Movement")
        sens_l = QVBoxLayout(sens_grp)

        # Box Margin
        bm_l = QHBoxLayout()
        self.bm_label = QLabel(f"Screen Reach Margin: {cfg.box_margin*100:.0f}%")
        self.bm_slider = QSlider(Qt.Orientation.Horizontal)
        self.bm_slider.setRange(5, 35)
        self.bm_slider.setValue(int(cfg.box_margin * 100))
        self.bm_slider.valueChanged.connect(lambda v: self.bm_label.setText(f"Screen Reach Margin: {v}%"))
        bm_l.addWidget(self.bm_label)
        bm_l.addWidget(self.bm_slider)
        sens_l.addLayout(bm_l)

        # Pinch Threshold
        pinch_l = QHBoxLayout()
        self.pinch_label = QLabel(f"Pinch Click Distance: {cfg.pinch_down:.2f}")
        self.pinch_slider = QSlider(Qt.Orientation.Horizontal)
        self.pinch_slider.setRange(15, 45)
        self.pinch_slider.setValue(int(cfg.pinch_down * 100))
        self.pinch_slider.valueChanged.connect(lambda v: self.pinch_label.setText(f"Pinch Click Distance: {v/100:.2f}"))
        pinch_l.addWidget(self.pinch_label)
        pinch_l.addWidget(self.pinch_slider)
        sens_l.addLayout(pinch_l)

        # Smoothing Cutoff
        smooth_l = QHBoxLayout()
        self.smooth_label = QLabel(f"Cursor Smoothing: {cfg.smooth_min_cutoff:.1f}")
        self.smooth_slider = QSlider(Qt.Orientation.Horizontal)
        self.smooth_slider.setRange(4, 30)
        self.smooth_slider.setValue(int(cfg.smooth_min_cutoff * 10))
        self.smooth_slider.valueChanged.connect(lambda v: self.smooth_label.setText(f"Cursor Smoothing: {v/10:.1f}"))
        smooth_l.addWidget(self.smooth_label)
        smooth_l.addWidget(self.smooth_slider)
        sens_l.addLayout(smooth_l)

        # Invert Scroll
        self.scroll_inv_cb = QCheckBox("Invert Scroll Direction")
        self.scroll_inv_cb.setChecked(cfg.scroll_invert)
        sens_l.addWidget(self.scroll_inv_cb)

        gen_layout.addWidget(sens_grp)

        # 3. Audio & Voice
        audio_grp = QGroupBox("Voice & Speech")
        audio_l = QVBoxLayout(audio_grp)
        self.voice_cb = QCheckBox("Enable Voice Commands (Wake Word: 'Jarvis')")
        self.voice_cb.setChecked(cfg.voice_enabled)
        self.tts_cb = QCheckBox("Spoken Voice Replies (Text-to-Speech)")
        self.tts_cb.setChecked(cfg.speak)
        audio_l.addWidget(self.voice_cb)
        audio_l.addWidget(self.tts_cb)
        gen_layout.addWidget(audio_grp)

        tabs.addTab(tab_general, "Controls")

        # --- Tab 2: Live Calibration ---
        tab_calib = QWidget()
        cal_layout = QVBoxLayout(tab_calib)

        cal_info = QLabel(
            "Perform live calibration to tune gesture thresholds to your hand:\n"
            "1. Hold hand open to view baseline metrics.\n"
            "2. Pinch your thumb and index finger together.\n"
            "3. Click 'Measure & Calibrate' below."
        )
        cal_info.setWordWrap(True)
        cal_layout.addWidget(cal_info)

        self.cal_live_label = QLabel("Current Pinch TI Ratio: --  | Pose: --")
        self.cal_live_label.setStyleSheet("color: #ffc94d; font-size: 13px; font-weight: bold;")
        cal_layout.addWidget(self.cal_live_label)

        self.cal_btn = QPushButton("Measure Current Pinch as Click Point")
        self.cal_btn.clicked.connect(self._measure_pinch)
        cal_layout.addWidget(self.cal_btn)

        self.cal_status = QLabel("Ready for calibration.")
        cal_layout.addWidget(self.cal_status)
        cal_layout.addStretch()

        tabs.addTab(tab_calib, "Hand Calibration")

        # Bottom Buttons
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save & Apply")
        save_btn.clicked.connect(self._save_and_apply)
        cancel_btn = QPushButton("Close")
        cancel_btn.clicked.connect(self.close)
        btn_layout.addStretch()
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        # Timer to update live calibration telemetry
        self.cal_timer = QTimer(self)
        self.cal_timer.timeout.connect(self._update_cal_telemetry)
        self.cal_timer.start(100)

    def _update_cal_telemetry(self):
        st = self.ctx.engine.status(time.time())
        dbg = st.get("debug")
        if dbg:
            ti = dbg.get("pinch_ti", 0.0)
            pose_desc = "PINCH" if ti < self.cfg.pinch_down else "OPEN"
            self.cal_live_label.setText(f"Current Pinch TI Ratio: {ti:.3f}  | State: {pose_desc}")

    def _measure_pinch(self):
        st = self.ctx.engine.status(time.time())
        dbg = st.get("debug")
        if dbg:
            ti = dbg.get("pinch_ti", 0.0)
            if ti > 0.05:
                # Set pinch_down slightly above the measured contact distance for easy activation
                new_down = round(ti + 0.04, 2)
                self.pinch_slider.setValue(int(new_down * 100))
                self.cal_status.setText(f"Calibrated! Pinch threshold set to {new_down:.2f}")
                return
        self.cal_status.setText("No hand detected. Place hand in camera view and try again.")

    def _save_and_apply(self):
        self.cfg.camera_index = self.cam_combo.currentIndex()
        self.cfg.box_margin = self.bm_slider.value() / 100.0
        self.cfg.pinch_down = self.pinch_slider.value() / 100.0
        self.cfg.pinch_up = self.cfg.pinch_down + 0.14
        self.cfg.pinch_freeze = self.cfg.pinch_down + 0.27
        self.cfg.smooth_min_cutoff = self.smooth_slider.value() / 10.0
        self.cfg.scroll_invert = self.scroll_inv_cb.isChecked()
        self.cfg.voice_enabled = self.voice_cb.isChecked()
        self.cfg.speak = self.tts_cb.isChecked()

        # Persist to jarvis_config.json
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
        try:
            data = {
                "camera_index": self.cfg.camera_index,
                "box_margin": self.cfg.box_margin,
                "pinch_down": self.cfg.pinch_down,
                "pinch_up": self.cfg.pinch_up,
                "pinch_freeze": self.cfg.pinch_freeze,
                "smooth_min_cutoff": self.cfg.smooth_min_cutoff,
                "scroll_invert": self.cfg.scroll_invert,
                "voice_enabled": self.cfg.voice_enabled,
                "speak": self.cfg.speak,
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            self.ctx.say("Settings updated, sir.")
        except Exception as exc:
            print(f"[config] save failed: {exc}")
        self.close()


class JarvisTray(QSystemTrayIcon):
    """System tray icon with quick actions and settings access."""

    def __init__(self, overlay_mgr, app):
        super().__init__(app)
        self.mgr = overlay_mgr
        self.setIcon(self._create_icon())
        self.setToolTip("J.A.R.V.I.S. PC Control")

        menu = QMenu()
        menu.setStyleSheet("""
            QMenu { background: #0b1118; color: #e8fbff; border: 1px solid #1c3b47; }
            QMenu::item:selected { background: #122c38; color: #00e5ff; }
        """)

        self.status_action = QAction("Status: Standby", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()

        wake_action = menu.addAction("Wake / Standby")
        wake_action.triggered.connect(self._toggle_wake)

        preview_action = menu.addAction("Toggle Vision Preview")
        preview_action.triggered.connect(self._toggle_preview)

        settings_action = menu.addAction("Settings & Calibration...")
        settings_action.triggered.connect(self.mgr.open_settings)

        menu.addSeparator()
        quit_action = menu.addAction("Quit Jarvis")
        quit_action.triggered.connect(self.mgr.quit_app)

        self.setContextMenu(menu)
        self.show()

    def _create_icon(self):
        pm = QPixmap(32, 32)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(CYAN), 2))
        p.drawEllipse(4, 4, 24, 24)
        p.setBrush(QBrush(QColor(CYAN)))
        p.drawEllipse(11, 11, 10, 10)
        p.end()
        return QIcon(pm)

    def _toggle_wake(self):
        if self.mgr.ctx:
            self.mgr.ctx.set_active(None)

    def _toggle_preview(self):
        if self.mgr.cfg:
            self.mgr.cfg.show_preview = not self.mgr.cfg.show_preview

    def update_status(self, active):
        self.status_action.setText(f"Status: {'Online' if active else 'Standby'}")


class Overlay:
    """Public interface for the Dynamic-Island overlay."""

    def __init__(self, stop_event, cfg=None, ctx=None):
        self.stop = stop_event
        self.cfg = cfg
        self.ctx = ctx
        self.q = queue.Queue()
        self.settings_dlg = None

        if PYSIDE6_AVAILABLE:
            self.app = QApplication.instance() or QApplication(sys.argv)
            self.island = DynamicIsland(self)
            self.tray = JarvisTray(self, self.app)
            self.island.show()
            self.island.make_click_through()

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

    def open_settings(self):
        if PYSIDE6_AVAILABLE and self.cfg and self.ctx:
            if self.settings_dlg is None or not self.settings_dlg.isVisible():
                self.settings_dlg = SettingsWindow(self.cfg, self.ctx)
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
                    self.tray.update_status(d["active"])
                elif kind == "say":
                    self.island.say_text = d["text"]
                    self.island.say_until = now + 4.2
                elif kind == "listening":
                    self.island.listening = True
                    self.island.listening_until = d.get("until", now + 5.0)
                elif kind == "error":
                    self.island.error_text = d.get("text", "ERROR")
                    self.island.error_until = now + 5.0
        except queue.Empty:
            pass

    def _tick(self):
        if self.stop.is_set():
            self.app.quit()
            return
        now = time.time()
        self._drain(now)
        self.island.update_frame(now)

    # --- Tkinter fallback if PySide6 missing ---
    def _init_tk_fallback(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#05080d")
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"300x44+{(sw - 300) // 2}+0")
        self.label_tk = tk.Label(self.root, text="JARVIS STANDBY", fg="#00e5ff", bg="#05080d")
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
                    self.label_tk.config(text=f"JARVIS // {d['label']}")
        except queue.Empty:
            pass
        self.root.after(20, self._tick_tk)
