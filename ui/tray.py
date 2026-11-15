"""
ui/tray.py — J.A.R.V.I.S. System Tray Icon + Windows 11 Flyout Menu

Preserves the original JarvisTray public API:
  - update_status(active: bool)
  - All original menu actions

Improvements:
  - Menu styled with Apple HIG typography + tokens.py colors
  - Separators use hairline dividers at 8% alpha
  - Rendered tray icon fallback uses squircle + accent color
"""

from __future__ import annotations

import sys
import os
from typing import Optional

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QFont, QPixmap, QIcon, QAction, QPen
)
from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication

from ui.tokens import T, TYPE, qfont
from ui.components import create_squircle_path


def _get_app_icon() -> Optional[QIcon]:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
    for name in ("jarvis.ico", "jarvis.png", "jarvis_tray.png"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return QIcon(p)
    return None


def _make_fallback_icon(dark: bool) -> QIcon:
    """Render a 32×32 squircle tray icon with the 'J' letter."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    accent = T.color.accent.resolve(dark)
    path   = create_squircle_path(QRectF(4, 4, 24, 24), 6.0)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(accent))
    p.drawPath(path)

    p.setPen(QColor("#FFFFFF"))
    f = qfont(TYPE.headline)
    f.setWeight(QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(QRectF(0, 0, 32, 32), Qt.AlignmentFlag.AlignCenter, "J")
    p.end()
    return QIcon(pix)


def _make_hidden_icon(base: Optional[QIcon], dark: bool) -> QIcon:
    """Dimmed copy of the tray icon with a diagonal slash (Hide Jarvis is on).
    Derived at runtime from assets/jarvis_tray.png (falls back to the normal icon)."""
    src = None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "jarvis_tray.png")
    if os.path.exists(path):
        pm = QPixmap(path)
        if not pm.isNull():
            src = pm
    if src is None and base is not None and not base.isNull():
        src = base.pixmap(64, 64)
    if src is None or src.isNull():
        src = _make_fallback_icon(dark).pixmap(64, 64)
    size = 64
    src = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setOpacity(0.45)
    p.drawPixmap((size - src.width()) // 2, (size - src.height()) // 2, src)
    p.setOpacity(1.0)
    # slash: a soft dark under-stroke keeps the line readable on light and dark taskbars
    pen_c = QColor(255, 255, 255, 190) if dark else QColor(20, 20, 24, 200)
    under = QColor(0, 0, 0, 90) if dark else QColor(255, 255, 255, 120)
    a, b = QPointF(size * 0.16, size * 0.84), QPointF(size * 0.84, size * 0.16)
    p.setPen(QPen(under, size * 0.12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(a, b)
    p.setPen(QPen(pen_c, size * 0.07, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(a, b)
    p.end()
    return QIcon(out)


class JarvisTray(QSystemTrayIcon):
    """System tray icon with Windows 11-style context menu."""

    def __init__(self, overlay_mgr, app: QApplication):
        super().__init__(app)
        self.mgr = overlay_mgr
        self.app = app

        # Icon
        icon = _get_app_icon()
        if icon and not icon.isNull():
            self._icon_normal = icon
        else:
            self._icon_normal = _make_fallback_icon(T.is_dark())
        self._icon_hidden: Optional[QIcon] = None
        self._hidden = False
        self.setIcon(self._icon_normal)

        self.setToolTip("J.A.R.V.I.S. — Standby")
        self._build_menu()
        self.show()

    def _build_menu(self) -> None:
        dark    = T.is_dark()
        label_c = T.color.label.resolve(dark).name()
        bg_c    = T.color.elevated_background.resolve(dark).name()
        sep_c   = T.color.separator.resolve(dark).name()
        accent  = T.color.accent.resolve(dark).name()

        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{
                background: {bg_c};
                color: {label_c};
                border: 1px solid rgba(128,128,128,0.18);
                border-radius: 10px;
                padding: 4px 4px;
            }}
            QMenu::item {{
                padding: 7px 20px;
                border-radius: 6px;
                font-size: 12px;
            }}
            QMenu::item:selected {{
                background: {accent};
                color: #ffffff;
            }}
            QMenu::separator {{
                height: 1px;
                background: rgba(128,128,128,0.15);
                margin: 3px 8px;
            }}
        """)

        # Status (disabled, just informational)
        self._status_act = QAction("Status: Standby", menu)
        self._status_act.setEnabled(False)
        menu.addAction(self._status_act)
        menu.addSeparator()

        act_toggle = QAction("Wake / Standby  (Ctrl+Alt+J)", menu)
        act_toggle.triggered.connect(self._toggle_active)
        menu.addAction(act_toggle)

        self._act_gesture = QAction("Gesture mode", menu)
        self._act_gesture.setCheckable(True)
        self._act_gesture.setChecked(self._gesture_mode_now())
        self._act_gesture.triggered.connect(self._toggle_gesture_mode)
        menu.addAction(self._act_gesture)
        menu.aboutToShow.connect(lambda: self._act_gesture.setChecked(self._gesture_mode_now()))

        act_preview = QAction("Toggle Vision Preview", menu)
        act_preview.triggered.connect(self._toggle_preview)
        menu.addAction(act_preview)

        self._act_hide = QAction("Hide Jarvis  (Ctrl+Alt+H)", menu)
        self._act_hide.setCheckable(True)
        self._act_hide.setChecked(self._hidden_now())
        self._act_hide.triggered.connect(self._toggle_hidden)
        menu.addAction(self._act_hide)
        menu.aboutToShow.connect(self._sync_hidden)

        act_dwell = QAction("Toggle Dwell Click", menu)
        act_dwell.triggered.connect(self._toggle_dwell)
        menu.addAction(act_dwell)

        menu.addSeparator()

        act_settings = QAction("Settings...", menu)
        act_settings.triggered.connect(lambda: self.mgr.open_settings())
        menu.addAction(act_settings)

        act_update = QAction("Check for Updates...", menu)
        act_update.triggered.connect(self._check_updates)
        menu.addAction(act_update)

        menu.addSeparator()

        act_quit = QAction("Quit J.A.R.V.I.S.", menu)
        act_quit.triggered.connect(self.mgr.quit_app)
        menu.addAction(act_quit)

        self.setContextMenu(menu)

    def update_status(self, active: bool) -> None:
        status = "Online" if active else "Standby"
        self._status_act.setText(f"J.A.R.V.I.S.  —  {status}")
        self.setToolTip(f"J.A.R.V.I.S. — {status}")

    def _toggle_active(self) -> None:
        if self.mgr.ctx:
            self.mgr.ctx.set_active(None)

    def _gesture_mode_now(self) -> bool:
        fn = getattr(getattr(self.mgr, "ctx", None), "gesture_mode_on", None)
        if callable(fn):
            try:
                return fn() is True
            except Exception:
                pass
        isl = getattr(self.mgr, "island", None)
        return bool(getattr(isl, "gesture_mode", False))

    def _toggle_gesture_mode(self, checked: bool = False) -> None:
        """Calls Overlay.on_gesture_mode_toggle(new_state) (wired by main.py), else ctx.set_gesture_mode."""
        new_state = not self._gesture_mode_now()
        hook = getattr(self.mgr, "on_gesture_mode_toggle", None)
        ctx = getattr(self.mgr, "ctx", None)
        try:
            if callable(hook):
                hook(new_state)
            elif ctx is not None and hasattr(ctx, "set_gesture_mode"):
                ctx.set_gesture_mode(new_state)
            else:
                return
        except Exception as exc:
            print(f"[tray] gesture mode toggle failed: {exc}")
            return
        isl = getattr(self.mgr, "island", None)
        if hasattr(isl, "set_gesture_mode"):
            isl.set_gesture_mode(new_state)
        self._act_gesture.setChecked(new_state)

    def _hidden_now(self) -> bool:
        ctx = getattr(self.mgr, "ctx", None)
        fn = getattr(ctx, "is_hidden", None)
        if callable(fn):
            try:
                return fn() is True
            except Exception:
                pass
        return bool(getattr(getattr(self.mgr, "cfg", None), "island_hidden", False))

    def _toggle_hidden(self, checked: bool = False) -> None:
        """Hide / show Jarvis through ctx.set_hidden (the same path as Ctrl+Alt+H)."""
        new_state = not self._hidden_now()
        ctx = getattr(self.mgr, "ctx", None)
        fn = getattr(ctx, "set_hidden", None)
        try:
            if callable(fn):
                fn(new_state)
            else:
                cfg = getattr(self.mgr, "cfg", None)
                if cfg is None:
                    return
                cfg.island_hidden = new_state
        except Exception as exc:
            print(f"[tray] hide toggle failed: {exc}")
            return
        self.set_hidden_state(new_state)

    def _sync_hidden(self) -> None:
        self.set_hidden_state(self._hidden_now())

    def set_hidden_state(self, hidden: bool) -> None:
        """Reflect the hidden state: checkmark in the menu and the dimmed + slashed tray icon."""
        hidden = bool(hidden)
        self._act_hide.setChecked(hidden)
        if hidden == self._hidden:
            return
        self._hidden = hidden
        if hidden:
            if self._icon_hidden is None:
                self._icon_hidden = _make_hidden_icon(self._icon_normal, T.is_dark())
            self.setIcon(self._icon_hidden)
        else:
            self.setIcon(self._icon_normal)

    def _toggle_preview(self) -> None:
        if self.mgr.cfg:
            self.mgr.cfg.show_preview = not getattr(self.mgr.cfg, "show_preview", False)
            try:
                from config import save_config
                save_config(self.mgr.cfg)
            except Exception:
                pass
            if self.mgr.ctx and hasattr(self.mgr.ctx, "reload_config"):
                self.mgr.ctx.reload_config(self.mgr.cfg)

    def _toggle_dwell(self) -> None:
        if self.mgr.cfg:
            cur = getattr(self.mgr.cfg, "dwell_mode", "click")
            self.mgr.cfg.dwell_mode = "off" if cur != "off" else "click"

    def _check_updates(self) -> None:
        if self.mgr.updater:
            self.mgr.updater.check_async(notify_if_latest=True)
