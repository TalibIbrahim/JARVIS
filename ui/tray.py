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

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (
    QPainter, QColor, QBrush, QFont, QPixmap, QIcon, QAction
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


class JarvisTray(QSystemTrayIcon):
    """System tray icon with Windows 11-style context menu."""

    def __init__(self, overlay_mgr, app: QApplication):
        super().__init__(app)
        self.mgr = overlay_mgr
        self.app = app

        # Icon
        icon = _get_app_icon()
        if icon and not icon.isNull():
            self.setIcon(icon)
        else:
            self.setIcon(_make_fallback_icon(T.is_dark()))

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

        act_preview = QAction("Toggle Vision Preview", menu)
        act_preview.triggered.connect(self._toggle_preview)
        menu.addAction(act_preview)

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
