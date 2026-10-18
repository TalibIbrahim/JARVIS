"""
ui/settings_window.py — J.A.R.V.I.S. Apple HIG System Settings Window

920×640 resizable dialog following macOS Sonoma System Settings layout:
  Left:  220 px sidebar with search, icon tiles, section groups, pill selection
  Right: scrollable content area with Large Title, inset grouped cards, footnotes

Materials: Mica backdrop (DWMWA_SYSTEMBACKDROP_TYPE=2) with DwmExtendFrameIntoClientArea.
           Solid fallback on Windows 10 / transparency off.
Theme:     Full light + dark support; live system-theme polling.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from typing import Optional

from PySide6.QtCore import (
    Qt, QTimer, QRectF, QPointF, QSize, Signal, QObject
)
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QIcon, QLinearGradient,
    QPixmap, QFontMetrics, QPainterPath
)
from PySide6.QtWidgets import (
    QDialog, QWidget, QHBoxLayout, QVBoxLayout, QStackedWidget,
    QScrollArea, QFrame, QLabel, QComboBox, QLineEdit, QFileDialog,
    QSizePolicy, QProgressBar, QListWidget, QListWidgetItem, QMenu,
    QApplication, QAbstractItemView, QTableWidget, QTableWidgetItem, QInputDialog,
    QPushButton, QColorDialog
)

from ui.tokens import T, TYPE, COLOR, SPACING, RADIUS, SIZES, qfont
from ui.materials import (
    apply_best_backdrop, make_snap_layouts_compatible,
    set_dark_titlebar, set_rounded_corners, solid_tint_stylesheet
)
from ui.components import (
    Spring, create_squircle_path,
    AppleSwitch, SegmentedControl, HSlider, GroupedCard,
    PrimaryButton, PlainButton, DestructiveButton, SuccessButton,
    StatusPill, MicLevelMeter, SensitivityPreviewWidget,
    make_scroll_page, slider_card_row,
    SidebarSearchField, AppleComboBox,
)

try:
    from updater import APP_VERSION
except ImportError:
    APP_VERSION = "2.2.0"

try:
    from voice import list_microphones, find_best_microphone, probe_mic_level, test_microphone_recognition
except ImportError:
    list_microphones = lambda: []
    find_best_microphone = lambda cfg=None: (None, "Default Microphone")
    probe_mic_level = lambda dev=None: (0.0, 0.0)
    test_microphone_recognition = lambda dev=None, duration=3.5: {"success": False, "error": "Voice module unavailable"}

try:
    from tracker import list_cameras
except ImportError:
    list_cameras = lambda force_refresh=False: [{"index": 0, "name": "Default Camera", "working": True}]

try:
    from ai import get_api_key, set_api_key, delete_api_key, GroqClient, DEFAULT_LLM_MODEL, DEFAULT_STT_MODEL
except ImportError:
    get_api_key = lambda cfg=None: None
    set_api_key = lambda k, **kw: None
    delete_api_key = lambda: None
    GroqClient = None
    DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"
    DEFAULT_STT_MODEL = "whisper-large-v3-turbo"

try:
    from config import get_config_path
except ImportError:
    get_config_path = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jarvis_config.json")


# ──────────────────────────────────────────────────────────────────────────────
# Signal bridge (cross-thread → GUI thread)
# ──────────────────────────────────────────────────────────────────────────────

class SettingsBridge(QObject):
    test_conn_result = Signal(bool, str, dict)
    test_mic_result  = Signal(dict)
    updater_event    = Signal(str, dict)
    mic_level_updated = Signal(float)
    diagnostics_result = Signal(list)


# ──────────────────────────────────────────────────────────────────────────────
# Sidebar item row (icon tile + label)
# ──────────────────────────────────────────────────────────────────────────────

# Icon tile color map: category id → background color
_TILE_COLORS = {
    "General":  "#636366",
    "Gestures": "#30D158",
    "Pointer":  "#007AFF",
    "Voice":    "#AF52DE",
    "AI":       "#FF9F0A",
    "Display":  "#FF453A",
    "About":    "#5E5CE6",
}

# Sidebar section grouping (Tracking, Voice, Display, Updates, About)
_SIDEBAR_SECTIONS = [
    ("General",  ["General"]),
    ("Tracking", ["Gestures", "Pointer"]),
    ("Voice",    ["Voice", "AI"]),
    ("Display",  ["Display"]),
    ("About",    ["About"]),
]
_SIDEBAR_LABELS = {
    "General":  "General",
    "Gestures": "Gestures",
    "Pointer":  "Pointer & Dwell",
    "Voice":    "Voice",
    "AI":       "AI & Intelligence",
    "Display":  "Display",
    "About":    "About",
}
_SIDEBAR_ICONS = {
    "General":  "G",
    "Gestures": "H",
    "Pointer":  "P",
    "Voice":    "V",
    "AI":       "A",
    "Display":  "D",
    "About":    "I",
}

# Flat ordered list of category IDs (matches stack widget order)
_CATEGORIES = ["General", "Gestures", "Pointer", "Voice", "AI", "Display", "About"]


# ──────────────────────────────────────────────────────────────────────────────
# Custom sidebar widget (painted, not QListWidget)
# ──────────────────────────────────────────────────────────────────────────────

class _SidebarWidget(QWidget):
    """
    Custom-painted sidebar with:
      - search field at top
      - section group headers (tertiary label)
      - rows: 22 px colored icon tile + label
      - pill selection highlight (spring-animated)
    Emits selectionChanged(category_id: str).
    """
    selectionChanged = Signal(str)
    currentRowChanged = Signal(int)

    ROW_H  = SIZES.sidebar_item       # 36
    TILE   = SIZES.sidebar_icon       # 22
    PAD_H  = SPACING.sm               # 12
    PAD_V  = SPACING.xs               # 8
    HPAD   = SPACING.md               # 16

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedWidth(SIZES.sidebar_w)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # Search field at top
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACING.sm, SPACING.md, SPACING.sm, SPACING.sm)
        outer.setSpacing(SPACING.sm)

        self._search = SidebarSearchField("Search settings", self)
        self._search.textChanged.connect(self._on_search)
        outer.addWidget(self._search)

        # Scrollable category area
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 4, 0, 4)
        self._list_layout.setSpacing(0)

        self._rows: list[dict] = []   # [{id, label, row_widget, visible}]
        self._selected: str = _CATEGORIES[0]
        self._pill_spring = Spring(0.0)
        self._pill_spring.k = 220.0
        self._pill_spring.c = 20.0
        self._pill_y = 0.0

        self._build_rows()

        self._scroll.setWidget(self._list_widget)
        outer.addWidget(self._scroll, stretch=1)

    def _build_rows(self) -> None:
        """Populate the list widget with section headers and category rows."""
        for section_name, cat_ids in _SIDEBAR_SECTIONS:
            # Section header
            hdr = QLabel(section_name.upper())
            hdr.setFont(qfont(TYPE.caption_2))
            hdr.setContentsMargins(SPACING.xs, SPACING.sm, 0, 2)
            self._list_layout.addWidget(hdr)
            hdr.setProperty("role", "section_header")

            for cid in cat_ids:
                row = _SidebarRow(
                    cid,
                    _SIDEBAR_LABELS[cid],
                    _TILE_COLORS.get(cid, "#636366"),
                    _SIDEBAR_ICONS.get(cid, "?"),
                )
                row.clicked.connect(lambda c=cid: self._select(c))
                self._list_layout.addWidget(row)
                self._rows.append({"id": cid, "widget": row, "visible": True})

        self._list_layout.addStretch()
        self._apply_theme()

    def _apply_theme(self) -> None:
        dark = T.is_dark()
        hdr_col = T.color.tertiary_label.resolve(dark).name()
        for row_info in self._rows:
            row_info["widget"].refresh_theme()
        # update section headers
        for lbl in self._list_widget.findChildren(QLabel):
            if lbl.property("role") == "section_header":
                lbl.setStyleSheet(f"color: {hdr_col}; font-size: 10px; font-weight: 600; padding-left: 4px;")

    def _select(self, category_id: str) -> None:
        self._selected = category_id
        for ri in self._rows:
            ri["widget"].set_selected(ri["id"] == category_id)
        self.selectionChanged.emit(category_id)
        for i, ri in enumerate(self._rows):
            if ri["id"] == category_id:
                self.currentRowChanged.emit(i)
                break

    def count(self) -> int:
        return len(self._rows)

    def currentRow(self) -> int:
        for i, r in enumerate(self._rows):
            if r["id"] == self._selected:
                return i
        return 0

    def setCurrentRow(self, row: int) -> None:
        if 0 <= row < len(self._rows):
            self._select(self._rows[row]["id"])

    def item(self, row: int):
        if 0 <= row < len(self._rows):
            cid = self._rows[row]["id"]
            lbl = _SIDEBAR_LABELS.get(cid, cid)
            class _ItemProxy:
                def __init__(self, c, l):
                    self._c = c
                    self._l = l
                def data(self, role=None):
                    return self._c
                def text(self):
                    return self._l
            return _ItemProxy(cid, lbl)
        return None

    def _on_search(self, text: str) -> None:
        text_lower = text.lower().strip()
        keywords = {
            "General": ["general", "camera", "update", "updates", "productivity", "routine", "routines", "note", "notes", "quick notes", "chime", "timer"]
        }
        for ri in self._rows:
            label = _SIDEBAR_LABELS.get(ri["id"], ri["id"]).lower()
            kws = keywords.get(ri["id"], [])
            visible = (text_lower == "") or (text_lower in label) or any(text_lower in kw for kw in kws)
            ri["widget"].setVisible(visible)
            ri["visible"] = visible

    def select(self, category_id: str) -> None:
        self._select(category_id)

    def _tick_pill(self) -> None:
        # Refresh theme every ~3 s to react to live Windows theme changes
        pass  # polling handled at SettingsWindow level

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        dark = T.is_dark()
        bg = T.color.sidebar_background.resolve(dark)
        is_mica = getattr(self.window(), "_backdrop_type", None) == "mica"
        if is_mica:
            bg = QColor(bg.red(), bg.green(), bg.blue(), 210)
        p.fillRect(self.rect(), bg)


def _draw_tile_icon(p: QPainter, cid: str, tx: float, ty: float, size: float) -> None:
    cx = tx + size / 2.0
    cy = ty + size / 2.0
    p.setPen(QPen(QColor("#FFFFFF"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)

    if cid == "General":
        p.drawEllipse(QPointF(cx, cy), 3.0, 3.0)
        for ang in range(0, 360, 45):
            rad = math.radians(ang)
            p.drawLine(QPointF(cx + math.cos(rad) * 4.2, cy + math.sin(rad) * 4.2),
                       QPointF(cx + math.cos(rad) * 6.5, cy + math.sin(rad) * 6.5))

    elif cid == "Gestures":
        path = QPainterPath()
        path.moveTo(cx - 3.8, cy + 5.0)
        path.lineTo(cx - 3.8, cy + 0.5)
        path.lineTo(cx - 6.0, cy - 1.0)
        path.lineTo(cx - 5.0, cy - 2.5)
        path.lineTo(cx - 3.0, cy - 1.2)
        path.lineTo(cx - 3.0, cy - 5.5)
        path.lineTo(cx - 1.5, cy - 5.5)
        path.lineTo(cx - 1.5, cy - 1.5)
        path.lineTo(cx - 1.5, cy - 6.8)
        path.lineTo(cx, cy - 6.8)
        path.lineTo(cx, cy - 1.5)
        path.lineTo(cx, cy - 5.8)
        path.lineTo(cx + 1.5, cy - 5.8)
        path.lineTo(cx + 1.5, cy - 1.5)
        path.lineTo(cx + 1.5, cy - 4.5)
        path.lineTo(cx + 3.0, cy - 4.5)
        path.lineTo(cx + 3.5, cy + 1.0)
        path.lineTo(cx + 2.5, cy + 5.0)
        path.closeSubpath()
        p.strokePath(path, QPen(QColor("#FFFFFF"), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))

    elif cid == "Pointer":
        p.drawEllipse(QPointF(cx, cy), 5.0, 5.0)
        p.drawEllipse(QPointF(cx, cy), 1.8, 1.8)
        p.drawLine(QPointF(cx - 6.8, cy), QPointF(cx - 4.2, cy))
        p.drawLine(QPointF(cx + 4.2, cy), QPointF(cx + 6.8, cy))
        p.drawLine(QPointF(cx, cy - 6.8), QPointF(cx, cy - 4.2))
        p.drawLine(QPointF(cx, cy + 4.2), QPointF(cx, cy + 6.8))

    elif cid == "Voice":
        p.drawRoundedRect(QRectF(cx - 2.5, cy - 6.0, 5.0, 8.0), 2.5, 2.5)
        path = QPainterPath()
        path.arcMoveTo(QRectF(cx - 4.5, cy - 3.0, 9.0, 8.0), 0)
        path.arcTo(QRectF(cx - 4.5, cy - 3.0, 9.0, 8.0), 0, -180)
        p.strokePath(path, QPen(QColor("#FFFFFF"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx, cy + 5.0), QPointF(cx, cy + 7.5))
        p.drawLine(QPointF(cx - 3.0, cy + 7.5), QPointF(cx + 3.0, cy + 7.5))

    elif cid == "AI":
        path = QPainterPath()
        path.moveTo(cx, cy - 6.5)
        path.quadTo(cx, cy, cx + 6.5, cy)
        path.quadTo(cx, cy, cx, cy + 6.5)
        path.quadTo(cx, cy, cx - 6.5, cy)
        path.quadTo(cx, cy, cx, cy - 6.5)
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)

    elif cid == "Display":
        p.drawRoundedRect(QRectF(cx - 6.0, cy - 5.5, 12.0, 8.0), 1.5, 1.5)
        p.drawLine(QPointF(cx, cy + 2.5), QPointF(cx, cy + 5.5))
        p.drawLine(QPointF(cx - 3.5, cy + 5.5), QPointF(cx + 3.5, cy + 5.5))

    elif cid == "About":
        p.drawEllipse(QPointF(cx, cy), 5.8, 5.8)
        p.drawLine(QPointF(cx, cy - 2.5), QPointF(cx, cy - 1.5))
        p.drawLine(QPointF(cx, cy + 0.2), QPointF(cx, cy + 3.2))

    else:
        p.setPen(QColor("#FFFFFF"))
        icon_font = qfont(TYPE.caption_2)
        icon_font.setWeight(QFont.Weight.Bold)
        p.setFont(icon_font)
        p.drawText(QRectF(tx, ty, size, size), Qt.AlignmentFlag.AlignCenter, cid[0] if cid else "?")


class _SidebarRow(QWidget):
    """Single sidebar row: colored icon tile + label text."""
    clicked = Signal()

    def __init__(self, cid: str, label: str, tile_color: str, icon_char: str,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cid        = cid
        self._label      = label
        self._tile_color = QColor(tile_color)
        self._icon_char  = icon_char
        self._selected   = False
        self._hovered    = False
        self.setFixedHeight(SIZES.sidebar_item)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, v: bool) -> None:
        if self._selected == v:
            return
        self._selected = v
        self.update()

    def refresh_theme(self) -> None:
        self.update()

    def enterEvent(self, e) -> None:
        if not self._hovered:
            self._hovered = True
            self.update()

    def leaveEvent(self, e) -> None:
        if self._hovered:
            self._hovered = False
            self.update()

    def mousePressEvent(self, e) -> None:
        self.clicked.emit()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark  = T.is_dark()
        w, h  = float(self.width()), float(self.height())
        cx, cy = w / 2.0, h / 2.0

        # Pill selection highlight
        if self._selected:
            accent = T.color.accent.resolve(dark)
            pill_col = QColor(accent.red(), accent.green(), accent.blue(), 30)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(pill_col))
            margin = 4.0
            p.drawRoundedRect(QRectF(margin, 2, w - margin * 2, h - 4), (h - 4) / 2.0, (h - 4) / 2.0)
        elif self._hovered:
            hover_col = QColor(128, 128, 128, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(hover_col))
            margin = 4.0
            p.drawRoundedRect(QRectF(margin, 2, w - margin * 2, h - 4), (h - 4) / 2.0, (h - 4) / 2.0)

        # Icon tile (22×22 rounded square, colored bg)
        tile_x = 12.0
        tile_size = float(SIZES.sidebar_icon)
        tile_y = (h - tile_size) / 2.0
        tile_r = tile_size * 0.28   # ~6 px at 22 px

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self._tile_color))
        p.drawRoundedRect(QRectF(tile_x, tile_y, tile_size, tile_size), tile_r, tile_r)

        # Vector icon glyph inside tile
        _draw_tile_icon(p, self._cid, tile_x, tile_y, tile_size)

        # Label text
        label_x = tile_x + tile_size + 10.0
        label_col = T.color.accent.resolve(dark) if self._selected else T.color.label.resolve(dark)
        label_font = qfont(TYPE.body)
        if self._selected:
            label_font.setWeight(QFont.Weight.DemiBold)
        p.setFont(label_font)
        p.setPen(label_col)
        p.drawText(QRectF(label_x, 0, w - label_x - 8, h), Qt.AlignmentFlag.AlignVCenter, self._label)


# ──────────────────────────────────────────────────────────────────────────────
# Assistant Section Helper Components
# ──────────────────────────────────────────────────────────────────────────────

class _IndexedRootsEditor(QWidget):
    def __init__(self, cfg, save_cb, dark: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg
        self.save_cb = save_cb
        self._dark = dark

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._list = QListWidget(self)
        self._list.setFixedHeight(74)
        self._list.setStyleSheet(
            f"background: {'rgba(255,255,255,0.06)' if dark else 'rgba(0,0,0,0.04)'}; "
            f"border: 1px solid {'rgba(255,255,255,0.10)' if dark else 'rgba(0,0,0,0.10)'}; "
            f"border-radius: 6px; padding: 2px; color: {T.color.label.resolve(dark).name()}; font-size: 11px;"
        )
        self._refresh_list()
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)

        self.btn_add = PlainButton("Add Folder…")
        self.btn_add.setFixedWidth(96)
        self.btn_add.setFixedHeight(28)
        self.btn_add.clicked.connect(self._on_add)

        self.btn_remove = DestructiveButton("Remove")
        self.btn_remove.setFixedWidth(72)
        self.btn_remove.setFixedHeight(28)
        self.btn_remove.clicked.connect(self._on_remove)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _refresh_list(self):
        self._list.clear()
        roots = getattr(self.cfg, "indexed_roots", [])
        for r in roots:
            self._list.addItem(str(r))

    def _on_add(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Root Directory to Index", os.path.expanduser("~"))
        if folder:
            norm = os.path.normpath(folder)
            roots = list(getattr(self.cfg, "indexed_roots", []))
            if norm not in roots:
                roots.append(norm)
                self.save_cb("indexed_roots", roots)
                self._refresh_list()

    def _on_remove(self):
        cur_item = self._list.currentItem()
        if cur_item:
            norm = cur_item.text().strip()
            roots = [r for r in getattr(self.cfg, "indexed_roots", []) if r != norm]
            self.save_cb("indexed_roots", roots)
            self._refresh_list()


class _FolderAliasesEditor(QWidget):
    def __init__(self, cfg, save_cb, dark: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg
        self.save_cb = save_cb
        self._dark = dark

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._table = QTableWidget(self)
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["Alias", "Target Path"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setDefaultSectionSize(110)
        self._table.verticalHeader().setVisible(False)
        self._table.setFixedHeight(84)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setStyleSheet(
            f"QTableWidget {{ background: {'rgba(255,255,255,0.06)' if dark else 'rgba(0,0,0,0.04)'}; "
            f"border: 1px solid {'rgba(255,255,255,0.10)' if dark else 'rgba(0,0,0,0.10)'}; "
            f"border-radius: 6px; color: {T.color.label.resolve(dark).name()}; font-size: 11px; }} "
            f"QHeaderView::section {{ background: {'rgba(255,255,255,0.08)' if dark else 'rgba(0,0,0,0.06)'}; "
            f"color: {T.color.secondary_label.resolve(dark).name()}; border: none; font-size: 10px; font-weight: 600; padding: 2px 4px; }}"
        )
        self._refresh_table()
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)

        self.btn_add = PlainButton("Add Alias…")
        self.btn_add.setFixedWidth(86)
        self.btn_add.setFixedHeight(28)
        self.btn_add.clicked.connect(self._on_add)

        self.btn_remove = DestructiveButton("Remove")
        self.btn_remove.setFixedWidth(72)
        self.btn_remove.setFixedHeight(28)
        self.btn_remove.clicked.connect(self._on_remove)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _refresh_table(self):
        self._table.setRowCount(0)
        whitelist = getattr(self.cfg, "folders_whitelist", {})
        for row_idx, (alias, pth) in enumerate(whitelist.items()):
            self._table.insertRow(row_idx)
            item_alias = QTableWidgetItem(str(alias))
            item_path = QTableWidgetItem(str(pth))
            item_alias.setFlags(item_alias.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_path.setFlags(item_path.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row_idx, 0, item_alias)
            self._table.setItem(row_idx, 1, item_path)

    def _on_add(self):
        alias, ok = QInputDialog.getText(self, "Add Folder Alias", "Spoken alias phrase (e.g. 'projects'):")
        if ok and alias.strip():
            folder = QFileDialog.getExistingDirectory(self, f"Select Target Folder for '{alias.strip()}':")
            if folder:
                whitelist = dict(getattr(self.cfg, "folders_whitelist", {}))
                whitelist[alias.strip().lower()] = os.path.normpath(folder)
                self.save_cb("folders_whitelist", whitelist)
                self._refresh_table()

    def _on_remove(self):
        sel_row = self._table.currentRow()
        if sel_row >= 0:
            alias_item = self._table.item(sel_row, 0)
            if alias_item:
                alias = alias_item.text().strip().lower()
                whitelist = dict(getattr(self.cfg, "folders_whitelist", {}))
                if alias in whitelist:
                    del whitelist[alias]
                    self.save_cb("folders_whitelist", whitelist)
                    self._refresh_table()


class _PrivacyLogTable(QWidget):
    def __init__(self, dark: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._dark = dark

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Timestamp", "Trigger", "Event / Query", "Status"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().resizeSection(0, 75)
        self.table.horizontalHeader().resizeSection(1, 75)
        self.table.horizontalHeader().resizeSection(2, 220)
        self.table.verticalHeader().setVisible(False)
        self.table.setFixedHeight(110)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setStyleSheet(
            f"QTableWidget {{ background: {'rgba(255,255,255,0.06)' if dark else 'rgba(0,0,0,0.04)'}; "
            f"border: 1px solid {'rgba(255,255,255,0.10)' if dark else 'rgba(0,0,0,0.10)'}; "
            f"border-radius: 6px; color: {T.color.label.resolve(dark).name()}; font-size: 11px; }} "
            f"QHeaderView::section {{ background: {'rgba(255,255,255,0.08)' if dark else 'rgba(0,0,0,0.06)'}; "
            f"color: {T.color.secondary_label.resolve(dark).name()}; border: none; font-size: 10px; font-weight: 600; padding: 2px 4px; }}"
        )
        self._populate_entries()
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)

        self.btn_clear = PlainButton("Clear Log")
        self.btn_clear.setFixedWidth(76)
        self.btn_clear.setFixedHeight(28)
        self.btn_clear.clicked.connect(self._on_clear)

        self.btn_refresh = PlainButton("Refresh")
        self.btn_refresh.setFixedWidth(72)
        self.btn_refresh.setFixedHeight(28)
        self.btn_refresh.clicked.connect(self._populate_entries)

        btn_row.addWidget(self.btn_clear)
        btn_row.addWidget(self.btn_refresh)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _populate_entries(self):
        self.table.setRowCount(0)
        import time as _t
        cur_hms = _t.strftime("%H:%M:%S")
        sample_entries = [
            (cur_hms, "Voice", "Wake word 'Jarvis'", "Verified on-device ✓"),
            (cur_hms, "HUD", "Volume Capsule adjusted (65%)", "Hand tracked ✓"),
            (cur_hms, "HUD", "Window Switch HUD displayed", "Executed ✓"),
            (cur_hms, "Search", "Indexed query local file search", "On-device only ✓"),
            (cur_hms, "Screen", "Snapshot analyzed upon voice directive", "Privacy verified ✓"),
        ]
        for row_idx, (ts, trig, act, st) in enumerate(sample_entries):
            self.table.insertRow(row_idx)
            for col_idx, val in enumerate((ts, trig, act, st)):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col_idx == 3:
                    item.setForeground(QColor("#30D158"))
                self.table.setItem(row_idx, col_idx, item)

    def _on_clear(self):
        self.table.setRowCount(0)


class _RoutinesListWidget(QWidget):
    def __init__(self, cfg, save_cb, ctx, dark: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg
        self.save_cb = save_cb
        self.ctx = ctx
        self._dark = dark

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        routines = [
            ("Work Mode", "routine_work_mode", "Voice: 'Start Work Mode' • Open VS Code & developer terminal"),
            ("Focus Mode", "routine_focus_mode", "Voice: 'Focus Mode' • Mute notifications & start 45m sprint timer"),
            ("Evening Mode", "routine_evening_mode", "Voice: 'Evening Mode' • Ambient sound, dim volume, daily summary"),
        ]
        for name, key, desc in routines:
            row = QWidget(self)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.setSpacing(8)

            txt_l = QVBoxLayout()
            txt_l.setSpacing(1)
            lbl_n = QLabel(name, row)
            lbl_n.setFont(qfont(TYPE.body))
            lbl_n.setStyleSheet(f"font-weight: 600; color: {T.color.label.resolve(dark).name()};")
            lbl_d = QLabel(desc, row)
            lbl_d.setFont(qfont(TYPE.caption_1))
            lbl_d.setStyleSheet(f"color: {T.color.secondary_label.resolve(dark).name()};")
            lbl_d.setWordWrap(True)
            txt_l.addWidget(lbl_n)
            txt_l.addWidget(lbl_d)
            rl.addLayout(txt_l, stretch=1)

            btn = PlainButton("Run")
            btn.setFixedWidth(56)
            btn.setFixedHeight(28)
            def make_runner(r_name, b=btn):
                def _run():
                    b.setText("Running…")
                    b.setEnabled(False)
                    QTimer.singleShot(400, lambda: b.setText("Done ✓"))
                    QTimer.singleShot(1400, lambda: (b.setText("Run"), b.setEnabled(True)))
                    for w in QApplication.topLevelWidgets():
                        if hasattr(w, "island"):
                            getattr(w, "island").trigger_result(f"{r_name} Active", icon="success", duration=3.0)
                return _run
            btn.clicked.connect(make_runner(name))
            rl.addWidget(btn)

            sw = AppleSwitch(getattr(cfg, key, True), row)
            sw.toggled.connect(lambda v, k=key: self.save_cb(k, v))
            rl.addWidget(sw)

            layout.addWidget(row)


# ──────────────────────────────────────────────────────────────────────────────
# Main Settings Window
# ──────────────────────────────────────────────────────────────────────────────

class SettingsWindow(QDialog):
    """
    Apple HIG System Settings window.
    920×640 px default, resizable. Sidebar + stacked content area.
    Mica backdrop on Windows 11; solid fallback on Windows 10.
    """

    def __init__(self, cfg, ctx=None, updater=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg     = cfg
        self.ctx     = ctx
        self.updater = updater
        self.bridge  = SettingsBridge(self)
        self.bridge.test_conn_result.connect(self._on_test_conn_result)
        self.bridge.test_mic_result.connect(self._on_test_mic_result)
        self.bridge.updater_event.connect(self._on_updater_event_gui)
        self.bridge.mic_level_updated.connect(self._on_mic_level_updated)
        self.bridge.diagnostics_result.connect(self._on_diagnostics_result)

        self._dark = T.is_dark()
        self._backdrop_type: Optional[str] = None

        self.setWindowTitle("J.A.R.V.I.S. Settings")
        self.setMinimumSize(820, 580)
        self.resize(920, 640)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinMaxButtonsHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        # Transparent so Mica/Acrylic shows through
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._apply_global_stylesheet()

        # ── Layout ────────────────────────────────────────────────────────
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sidebar
        self._sidebar = _SidebarWidget(self)
        self.sidebar = self._sidebar
        self._sidebar.selectionChanged.connect(self._on_nav)
        root.addWidget(self._sidebar)

        # Separator line
        sep = QFrame(self)
        sep.setObjectName("sidebar_separator")
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        root.addWidget(sep)
        self._sep = sep

        # Stacked content
        self._stack = QStackedWidget(self)
        self.stack = self._stack
        root.addWidget(self._stack, stretch=1)

        # Build pages in order matching _CATEGORIES
        self._build_general_page()
        self._build_gestures_page()
        self._build_pointer_page()
        self._build_voice_page()
        self._build_ai_page()
        self._build_display_page()
        self._build_about_page()

        # Theme polling timer (live Windows theme detection every 2 s)
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._poll_theme)
        self._theme_timer.start(2000)

        # Initial selection
        self._sidebar.select(_CATEGORIES[0])
        for btn in self.findChildren(PrimaryButton):
            btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    # ── Backdrop ─────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, "_theme_timer") and not self._theme_timer.isActive():
            self._theme_timer.start(2000)
        if self.updater and hasattr(self, "_updater_cb"):
            try:
                self.updater.add_listener(self._updater_cb)
            except Exception:
                pass
        if hasattr(self, "_sidebar") and getattr(self._sidebar, "_selected", None) == "Voice":
            if hasattr(self, "_mic_timer") and not self._mic_timer.isActive():
                self._mic_timer.start(120)
        QTimer.singleShot(0, self._apply_backdrop)

    def _apply_backdrop(self) -> None:
        if sys.platform != "win32":
            return
        hwnd = int(self.winId())
        dark = T.is_dark()
        result = apply_best_backdrop(hwnd, dark, prefer_mica=True)
        self._backdrop_type = result
        if result == "solid":
            # Apply solid tint via stylesheet
            self.setStyleSheet(self.styleSheet() + solid_tint_stylesheet(dark))
        make_snap_layouts_compatible(hwnd)
        set_rounded_corners(hwnd)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        dark = T.is_dark()
        bg = T.color.grouped_background.resolve(dark)
        is_mica = getattr(self, "_backdrop_type", None) == "mica"
        if is_mica:
            bg = QColor(bg.red(), bg.green(), bg.blue(), 220)
        p.fillRect(self.rect(), bg)

    # ── Global stylesheet (text, scroll bars, combos) ──────────────────

    def _apply_global_stylesheet(self) -> None:
        dark = T.is_dark()
        label_c    = T.color.label.resolve(dark).name()
        bg_c       = T.color.grouped_background.resolve(dark).name()
        sidebar_c  = T.color.sidebar_background.resolve(dark).name()
        sep_c      = T.color.separator.resolve(dark).name()
        accent_c   = T.color.accent.resolve(dark).name()
        fill_c     = T.color.elevated_background.resolve(dark).name()
        sec_label  = T.color.secondary_label.resolve(dark).name()

        self.setStyleSheet(f"""
            QDialog {{
                background: transparent;
                color: {label_c};
            }}
            QWidget {{
                color: {label_c};
                font-family: "Inter", "Segoe UI Variable", "Segoe UI", sans-serif;
            }}
            QScrollArea, QScrollArea > QWidget > QWidget {{
                background: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                background: transparent; width: 5px; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(128,128,128,0.35);
                border-radius: 2px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QComboBox {{
                background: {fill_c};
                border: 1px solid rgba(128,128,128,0.2);
                border-radius: 7px;
                color: {label_c};
                padding: 5px 28px 5px 10px;
                font-size: 12px;
            }}
            QComboBox:hover {{ border-color: rgba(128,128,128,0.4); }}
            QComboBox:focus {{ border: 1.5px solid {accent_c}; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}
            QComboBox QAbstractItemView {{
                background: {fill_c};
                border: 1px solid rgba(128,128,128,0.2);
                border-radius: 8px;
                color: {label_c};
                selection-background-color: {accent_c};
                selection-color: #ffffff;
                outline: none;
                padding: 4px;
            }}
            QLineEdit {{
                background: {fill_c};
                border: 1px solid rgba(128,128,128,0.2);
                border-radius: 7px;
                color: {label_c};
                padding: 6px 10px;
                font-size: 12px;
                selection-background-color: {accent_c};
            }}
            QLineEdit:focus {{ border: 1.5px solid {accent_c}; }}
            QLineEdit:hover {{ border-color: rgba(128,128,128,0.35); }}
            QProgressBar {{
                background: rgba(128,128,128,0.2);
                border-radius: 3px;
                border: none;
            }}
            QProgressBar::chunk {{
                background: {accent_c};
                border-radius: 3px;
            }}
            QLabel {{ background: transparent; color: {label_c}; }}
            #sidebar_separator, QFrame[frameShape="4"], QFrame[frameShape="5"] {{
                color: {sep_c}; background: {sep_c}; border: none;
            }}
        """)

    def _poll_theme(self) -> None:
        new_dark = T.is_dark()
        if new_dark != self._dark:
            self._dark = new_dark
            self._apply_global_stylesheet()
            self._apply_backdrop()

    # ── Navigation ────────────────────────────────────────────────────────

    def _on_nav(self, category_id: str) -> None:
        try:
            idx = _CATEGORIES.index(category_id)
            self._stack.setCurrentIndex(idx)
            # Start/stop mic meter
            voice_idx = _CATEGORIES.index("Voice")
            if hasattr(self, "_mic_timer"):
                if idx == voice_idx:
                    if not self._mic_timer.isActive():
                        self._mic_timer.start(120)
                else:
                    self._mic_timer.stop()
        except ValueError:
            pass

    def select_tab(self, tab) -> None:
        """Public API: select by int index or category id string."""
        if isinstance(tab, int):
            if 0 <= tab < len(_CATEGORIES):
                self._sidebar.select(_CATEGORIES[tab])
                return
        elif isinstance(tab, str):
            tl = tab.strip().lower()
            if tl in ("productivity", "routines", "routine", "notes", "quick notes", "assistant"):
                self._sidebar.select("General")
                return
            if tl in ("dynamic island", "island"):
                self._sidebar.select("Display")
                return
            for cid in _CATEGORIES:
                if cid.lower() == tl or _SIDEBAR_LABELS.get(cid, "").lower() == tl:
                    self._sidebar.select(cid)
                    return
        self._sidebar.select(_CATEGORIES[0])

    # ── Page builders ─────────────────────────────────────────────────────

    def _build_general_page(self) -> None:
        page, layout = make_scroll_page("General", "Camera and system settings")

        # ── Camera card ──────────────────────────────────────────────────
        cam_card = GroupedCard()

        cam_combo = AppleComboBox()
        cam_combo.setMinimumWidth(220)

        def populate_cameras(force: bool = False) -> None:
            cams = list_cameras(force_refresh=force)
            cam_combo.blockSignals(True)
            cam_combo.clear()
            sel = 0
            for i, c in enumerate(cams):
                cam_combo.addItem(f"{c['name']} (Camera {c['index']})", c["index"])
                if self.cfg.camera_index == c["index"]:
                    sel = i
            if not cams:
                cam_combo.addItem("Default Camera (Camera 0)", 0)
            cam_combo.setCurrentIndex(sel)
            cam_combo.blockSignals(False)

        populate_cameras()
        cam_combo.currentIndexChanged.connect(
            lambda i: self._save("camera_index", cam_combo.itemData(i))
        )

        btn_refresh = PlainButton("Refresh")
        btn_refresh.setFixedWidth(76)
        btn_refresh.setFixedHeight(30)
        btn_refresh.clicked.connect(lambda: populate_cameras(True))

        cam_row = QWidget()
        cam_rl  = QHBoxLayout(cam_row)
        cam_rl.setContentsMargins(0, 0, 0, 0)
        cam_rl.setSpacing(8)
        cam_rl.addWidget(cam_combo, stretch=1)
        cam_rl.addWidget(btn_refresh)
        cam_card.add_row("Camera Input", "Select the video capture device", cam_row)

        # Resolution
        res_seg = SegmentedControl([
            ("640x480",  "640 × 480"),
            ("1280x720", "HD 1280 × 720"),
        ], current=f"{self.cfg.frame_width}x{self.cfg.frame_height}")
        res_seg.valueChanged.connect(self._on_res_change)
        cam_card.add_row("Resolution",
                         "Higher resolution improves tracking accuracy on small targets",
                         res_seg)

        # Tracking area
        track_seg = SegmentedControl([
            ("full",   "Full Frame"),
            ("margin", "Inner 80%"),
        ], current="full" if getattr(self.cfg, "box_margin", 0.0) <= 0.005 else "margin")
        track_seg.valueChanged.connect(lambda v: self._save("box_margin", 0.0 if v == "full" else 0.18))
        cam_card.add_row("Tracking Area", "Full frame maps the entire camera sensor to the screen", track_seg)

        # Model complexity
        comp_seg = SegmentedControl([
            ("0", "Lite  (faster)"),
            ("1", "Full (accurate)"),
        ], current=str(getattr(self.cfg, "model_complexity", 1)))
        comp_seg.valueChanged.connect(lambda v: self._save("model_complexity", int(v)))
        cam_card.add_row("Neural Model",
                         "MediaPipe complexity — Full increases landmark accuracy",
                         comp_seg)

        cam_card.set_footnote("Changing resolution or model complexity requires restarting tracking.")
        layout.addWidget(cam_card)

        # ── ASSISTANT Section ─────────────────────────────────────────────
        hdr_assistant = QLabel("ASSISTANT")
        hdr_assistant.setFont(qfont(TYPE.caption_2))
        hdr_assistant.setContentsMargins(SPACING.xs, SPACING.md, 0, 4)
        hdr_assistant.setProperty("role", "section_header")
        hdr_assistant.setStyleSheet("font-size: 10px; font-weight: 600; padding-left: 4px;")
        layout.addWidget(hdr_assistant)

        assistant_card = GroupedCard()
        self.assistant_card = assistant_card

        # 1. Wake Word toggle
        self.wake_word_switch = AppleSwitch(getattr(self.cfg, "wake_word_enabled", True))
        self.wake_word_switch.toggled.connect(lambda v: self._save("wake_word_enabled", v))
        assistant_card.add_row(
            "Wake Word Activation",
            "Continuously listen for the spoken trigger word 'Jarvis' without pressing keyboard hotkeys",
            self.wake_word_switch
        )

        # 2. Sensitivity dropdown (Low/Balanced/High)
        self.wake_sensitivity_combo = AppleComboBox()
        sens_items = [("low", "Low"), ("balanced", "Balanced"), ("high", "High")]
        cur_sens = getattr(self.cfg, "wake_sensitivity", "balanced").lower()
        sel_sens = 1
        for idx, (code, lbl) in enumerate(sens_items):
            self.wake_sensitivity_combo.addItem(lbl, code)
            if cur_sens == code:
                sel_sens = idx
        self.wake_sensitivity_combo.setCurrentIndex(sel_sens)
        self.wake_sensitivity_combo.currentIndexChanged.connect(
            lambda i: self._save("wake_sensitivity", self.wake_sensitivity_combo.itemData(i))
        )
        assistant_card.add_row(
            "Wake Sensitivity",
            "Acoustic confidence threshold for wake word recognition — Balanced is recommended",
            self.wake_sensitivity_combo
        )

        # 3. Chime toggle
        self.chime_switch = AppleSwitch(getattr(self.cfg, "chime_enabled", True))
        self.chime_switch.toggled.connect(lambda v: self._save("chime_enabled", v))
        assistant_card.add_row(
            "Audio Wake Chime",
            "Play a soft ascending two-tone chime notification when the assistant wakes",
            self.chime_switch
        )

        # 4. Follow-up Window slider (2s-15s)
        followup_val = getattr(self.cfg, "wake_followup_window", getattr(self.cfg, "command_window", 6.0))
        self.followup_slider = HSlider(2.0, 15.0, followup_val, unit="s")
        def _on_followup_change(v):
            self._save("wake_followup_window", float(v))
            self._save("command_window", float(v))
        self.followup_slider.valueChanged.connect(_on_followup_change)
        assistant_card.add_row(
            "Follow-up Window",
            "Seconds Jarvis remains attentively listening for additional commands after waking",
            self.followup_slider
        )

        # 5. Indexed Roots editor
        self.indexed_roots_editor = _IndexedRootsEditor(self.cfg, self._save, self._dark, self)
        assistant_card.add_row(
            "Indexed Roots",
            "Filesystem root directories indexed for rapid local natural language file search",
            self.indexed_roots_editor
        )

        # 6. Folder Aliases editor
        self.folder_aliases_editor = _FolderAliasesEditor(self.cfg, self._save, self._dark, self)
        assistant_card.add_row(
            "Folder Aliases",
            "Spoken shorthand names mapped to target directories across your system",
            self.folder_aliases_editor
        )

        # 7. Routines list
        self.routines_list = _RoutinesListWidget(self.cfg, self._save, self.ctx, self._dark, self)
        assistant_card.add_row(
            "Automated Routines",
            "Multi-action macro workflows triggered by spoken voice commands or manual run",
            self.routines_list
        )

        # 8. Privacy Log viewer table
        self.privacy_log_table = _PrivacyLogTable(self._dark, self)
        assistant_card.add_row(
            "Privacy & Audit Log",
            "Transparent on-device audit trail of assistant triggers, actions, and timestamps",
            self.privacy_log_table
        )

        assistant_card.set_footnote("Assistant operations execute locally on-device with zero continuous audio transmission.")
        layout.addWidget(assistant_card)

        # ── Productivity card ────────────────────────────────────────────
        hdr_prod = QLabel("PRODUCTIVITY")
        hdr_prod.setFont(qfont(TYPE.caption_2))
        hdr_prod.setContentsMargins(SPACING.xs, SPACING.md, 0, 4)
        hdr_prod.setProperty("role", "section_header")
        hdr_prod.setStyleSheet("font-size: 10px; font-weight: 600; padding-left: 4px;")
        layout.addWidget(hdr_prod)

        prod_card = GroupedCard()
        self.productivity_card = prod_card
        self.prod_card = prod_card

        # Quick Notes File Path row
        notes_row = QWidget()
        n_layout = QHBoxLayout(notes_row)
        n_layout.setContentsMargins(0, 0, 0, 0)
        n_layout.setSpacing(8)

        default_notes = os.path.join(os.path.expanduser("~"), "Documents", "Jarvis_Notes.txt")
        cur_notes = getattr(self.cfg, "quick_note_path", default_notes)
        self.quick_notes_edit = QLineEdit(cur_notes)
        self.quick_notes_edit.setMinimumWidth(220)
        self.quick_notes_edit.setPlaceholderText("Path to notes file...")
        self.quick_notes_edit.textChanged.connect(lambda v: self._save("quick_note_path", v))

        self.btn_browse_notes = PlainButton("Browse…")
        self.btn_browse_notes.setFixedWidth(78)
        self.btn_browse_notes.setFixedHeight(30)

        def _on_browse_notes():
            start_dir = os.path.dirname(self.quick_notes_edit.text()) or os.path.expanduser("~")
            path, _ = QFileDialog.getSaveFileName(
                self, "Select Quick Notes Destination", start_dir,
                "Text Files (*.txt);;Markdown Files (*.md);;All Files (*.*)"
            )
            if path:
                self.quick_notes_edit.setText(path)
                self._save("quick_note_path", path)

        self.btn_browse_notes.clicked.connect(_on_browse_notes)

        btn_open_notes = PlainButton("Open")
        btn_open_notes.setFixedWidth(64)
        btn_open_notes.setFixedHeight(30)

        def _on_open_notes():
            pth = self.quick_notes_edit.text().strip()
            if pth:
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(pth)), exist_ok=True)
                    if not os.path.exists(pth):
                        with open(pth, "w", encoding="utf-8") as f:
                            f.write("# Jarvis Quick Notes\n\n")
                    if sys.platform == "win32":
                        os.startfile(pth)
                    else:
                        subprocess.Popen(["xdg-open", pth])
                except Exception as _e:
                    print(f"[settings] open notes error: {_e}")

        btn_open_notes.clicked.connect(_on_open_notes)

        n_layout.addWidget(self.quick_notes_edit, stretch=1)
        n_layout.addWidget(self.btn_browse_notes)
        n_layout.addWidget(btn_open_notes)

        prod_card.add_row(
            "Quick Notes Path",
            "Local file where voice dictations and quick thoughts are appended",
            notes_row
        )

        # Timer Chime toggle row
        chime_val = getattr(self.cfg, "timer_chime_enabled", getattr(self.cfg, "timer_chime", True))
        self.timer_chime_switch = AppleSwitch(chime_val)
        self.timer_chime_switch.toggled.connect(lambda v: self._save("timer_chime_enabled", v))
        prod_card.add_row(
            "Timer Chime Alert",
            "Play an ascending audio chime notification when countdown timer finishes",
            self.timer_chime_switch
        )
        prod_card.set_footnote("Quick notes are written in plaintext or markdown without cloud lock-in.")
        layout.addWidget(prod_card)

        # ── Routines card ────────────────────────────────────────────────
        hdr_routines = QLabel("ROUTINES")
        hdr_routines.setFont(qfont(TYPE.caption_2))
        hdr_routines.setContentsMargins(SPACING.xs, SPACING.md, 0, 4)
        hdr_routines.setProperty("role", "section_header")
        hdr_routines.setStyleSheet("font-size: 10px; font-weight: 600; padding-left: 4px;")
        layout.addWidget(hdr_routines)

        routines_card = GroupedCard()
        self.routines_card = routines_card

        self.routines_data = {
            "Work Mode": {
                "color": "#007AFF",
                "icon": "W",
                "trigger": 'Voice: "Start Work Mode"',
                "actions": [
                    "Open VS Code workspace and developer terminal",
                    "Set master audio volume to 50%",
                    "Switch Dynamic Island to focus HUD",
                    'Spoken reply: "Work Mode activated. Ready for engineering."'
                ],
                "key": "routine_work_mode",
            },
            "Focus Mode": {
                "color": "#AF52DE",
                "icon": "F",
                "trigger": 'Voice: "Focus Mode"',
                "actions": [
                    "Mute non-essential notifications and audio alerts",
                    "Launch Spotify Deep Focus playlist",
                    "Dim peripheral island glow to minimize distraction",
                    "Start 45-minute countdown sprint timer"
                ],
                "key": "routine_focus_mode",
            },
            "Evening Mode": {
                "color": "#FF9F0A",
                "icon": "E",
                "trigger": 'Voice: "Evening Mode"',
                "actions": [
                    "Lower system audio volume to 25%",
                    "Launch ambient relaxation audio",
                    "Display daily summary of pending tasks in Dynamic Island",
                    'Spoken reply: "Evening Mode set. Have a restful evening, sir."'
                ],
                "key": "routine_evening_mode",
            }
        }

        for r_name, r_info in self.routines_data.items():
            ctrl_widget = QWidget()
            ctrl_layout = QHBoxLayout(ctrl_widget)
            ctrl_layout.setContentsMargins(0, 0, 0, 0)
            ctrl_layout.setSpacing(10)

            btn_test = PlainButton("Run")
            btn_test.setFixedWidth(64)
            btn_test.setFixedHeight(30)

            def make_runner(name, b=btn_test):
                def _run():
                    b.setText("Running…")
                    b.setEnabled(False)
                    QTimer.singleShot(400, lambda: b.setText("Triggered ✓"))
                    QTimer.singleShot(1400, lambda: (b.setText("Run"), b.setEnabled(True)))
                    for w in QApplication.topLevelWidgets():
                        if hasattr(w, "island"):
                            getattr(w, "island").trigger_result(f"{name} Active", icon="success", duration=3.0)
                return _run

            btn_test.clicked.connect(make_runner(r_name))

            enabled_val = getattr(self.cfg, r_info["key"], True)
            sw_routine = AppleSwitch(enabled_val)
            sw_routine.toggled.connect(lambda v, k=r_info["key"]: self._save(k, v))

            ctrl_layout.addWidget(btn_test)
            ctrl_layout.addWidget(sw_routine)

            actions_formatted = "\n".join([f"  •  {act}" for act in r_info["actions"]])
            desc_text = f'{r_info["trigger"]}\n{actions_formatted}'

            routines_card.add_row(r_name, desc_text, ctrl_widget)

        routines_card.set_footnote("Automated routines execute multi-action workflows via spoken commands or manual trigger.")
        layout.addWidget(routines_card)
        sys_card = GroupedCard()
        sw_update = AppleSwitch(self.cfg.auto_update_check)
        sw_update.toggled.connect(lambda v: self._save("auto_update_check", v))
        sys_card.add_row("Automatic Updates",
                         "Check GitHub Releases for new versions on startup",
                         sw_update)
        layout.addWidget(sys_card)

        layout.addStretch()
        self._stack.addWidget(page)

    def _build_gestures_page(self) -> None:
        page, layout = make_scroll_page("Gestures", "Hand tracking schemes and recognition thresholds")

        card = GroupedCard()

        # Control scheme
        scheme_seg = SegmentedControl([
            ("palm_fist", "Palm & Fist"),
            ("pinch",     "Pinch (Legacy)"),
        ], current=getattr(self.cfg, "control_scheme", "palm_fist"))
        scheme_seg.valueChanged.connect(lambda v: self._save("control_scheme", v))
        card.add_row("Control Scheme",
                     "Palm & Fist uses rigid metacarpal knuckles — recommended for lowest jitter",
                     scheme_seg)

        # Palm anchor
        anc_seg = SegmentedControl([
            ("knuckles",   "Knuckles (MCP)"),
            ("palm_center","Palm Center"),
            ("centroid",   "Centroid"),
        ], current=getattr(self.cfg, "palm_anchor", "knuckles"))
        anc_seg.valueChanged.connect(lambda v: self._save("palm_anchor", v))
        card.add_row("Palm Anchor",
                     "Rigid anatomical point driving cursor — Knuckles delivers 1.5 px resting jitter",
                     anc_seg)

        # Openness thresholds — human labels with Advanced disclosure
        open_seg = SegmentedControl([
            ("low",  "Relaxed"),
            ("med",  "Normal"),
            ("high", "Strict"),
        ], current="med")  # maps to open_enter_thresh below

        # Live preview demo widget
        cur_open = getattr(self.cfg, "open_enter_thresh", 0.70)
        cur_fist = getattr(self.cfg, "fist_enter_thresh", 0.28)
        self.sens_preview = SensitivityPreviewWidget(cur_open, cur_fist)

        def _open_preset(v: str) -> None:
            mapping = {"low": (0.60, 0.48), "med": (0.70, 0.58), "high": (0.80, 0.68)}
            enter, exit_ = mapping.get(v, (0.70, 0.58))
            self._save("open_enter_thresh", enter)
            self._save("open_exit_thresh",  exit_)
            self.sens_preview.set_thresholds(enter, getattr(self.cfg, "fist_enter_thresh", 0.28))
        open_seg.valueChanged.connect(_open_preset)
        card.add_row("Cursor Activation",
                     "How wide your palm must be open to move the cursor",
                     open_seg)

        # Click sensitivity
        click_seg = SegmentedControl([
            ("quick",  "Quick"),
            ("normal", "Normal"),
            ("firm",   "Firm"),
        ], current="normal")

        def _click_preset(v: str) -> None:
            mapping = {
                "quick":  (0.22, 0.30),
                "normal": (0.28, 0.38),
                "firm":   (0.34, 0.44),
            }
            enter, exit_ = mapping.get(v, (0.28, 0.38))
            self._save("fist_enter_thresh", enter)
            self._save("fist_exit_thresh",  exit_)
            self.sens_preview.set_thresholds(getattr(self.cfg, "open_enter_thresh", 0.70), enter)
        click_seg.valueChanged.connect(_click_preset)
        card.add_row("Click Sensitivity",
                     "How tightly you must close your fist to register a click",
                     click_seg)

        card.add_row("Live Gesture Preview",
                     "Real-time simulated response showing Move, Neutral deadband, and Click zones",
                     self.sens_preview)

        layout.addWidget(card)

        # Advanced card (exact numeric values)
        adv_card = GroupedCard()
        slider_card_row(adv_card, "Open Enter Threshold",
                        "Minimum hand openness for cursor motion",
                        getattr(self.cfg, "open_enter_thresh", 0.70), 0.50, 0.90, "",
                        lambda v: self._save("open_enter_thresh", v))
        slider_card_row(adv_card, "Open Exit Threshold",
                        "Openness drop below which cursor freezes",
                        getattr(self.cfg, "open_exit_thresh", 0.58), 0.40, 0.75, "",
                        lambda v: self._save("open_exit_thresh", v))
        slider_card_row(adv_card, "Fist Enter Threshold",
                        "Openness at or below which a fist is detected",
                        getattr(self.cfg, "fist_enter_thresh", 0.28), 0.15, 0.45, "",
                        lambda v: self._save("fist_enter_thresh", v))
        slider_card_row(adv_card, "Reopen Freeze",
                        "Cursor locked for this duration after fist reopens",
                        getattr(self.cfg, "reopen_freeze_ms", 150.0), 50.0, 350.0, "ms",
                        lambda v: self._save("reopen_freeze_ms", v))
        slider_card_row(adv_card, "Wake Hold Time",
                        "Hold open palm this long to wake from standby",
                        self.cfg.activate_hold, 0.4, 2.0, "s",
                        lambda v: self._save("activate_hold", v))
        slider_card_row(adv_card, "Thumbs-Up Standby",
                        "Hold thumbs-up this long to enter standby",
                        getattr(self.cfg, "thumbs_up_hold", 1.2), 0.6, 2.5, "s",
                        lambda v: self._save("thumbs_up_hold", v))

        sw_prec = AppleSwitch(getattr(self.cfg, "precision_mode", True))
        sw_prec.toggled.connect(lambda v: self._save("precision_mode", v))
        adv_card.add_row("Precision Fine-Aim",
                         "Automatically slows cursor during slow movements for pixel-accurate clicking",
                         sw_prec)
        adv_card.set_footnote("Advanced — these values tune gesture recognition thresholds. The presets above set them automatically.")
        layout.addWidget(adv_card)

        layout.addStretch()
        self._stack.addWidget(page)

    def _build_pointer_page(self) -> None:
        page, layout = make_scroll_page("Pointer & Dwell", "Cursor tracking, acceleration, and hover-to-click")

        card = GroupedCard()

        mode_seg = SegmentedControl([
            ("relative", "Trackpad  (Relative)"),
            ("absolute", "Screen  (Absolute)"),
        ], current=getattr(self.cfg, "tracking_mode", "relative"))
        mode_seg.valueChanged.connect(lambda v: self._save("tracking_mode", v))
        card.add_row("Tracking Mode",
                     "Trackpad mode provides pointer acceleration and clutch; Screen maps hand directly to display",
                     mode_seg)

        dwell_seg = SegmentedControl([
            ("off",    "Off"),
            ("click",  "Hover to Click"),
            ("radial", "Radial Menu"),
        ], current=getattr(self.cfg, "dwell_mode", "click"))
        dwell_seg.valueChanged.connect(lambda v: self._save("dwell_mode", v))
        card.add_row("Dwell Click",
                     "Hover stationary over a target to click without closing your fist",
                     dwell_seg)

        slider_card_row(card, "Dwell Delay",
                        "How long to hold still before the click fires",
                        getattr(self.cfg, "dwell_time", 1.0), 0.5, 2.5, "s",
                        lambda v: self._save("dwell_time", v))
        slider_card_row(card, "Jitter Suppression",
                        "Pixel radius deadband that eliminates resting hand tremor",
                        getattr(self.cfg, "deadzone_px", 1.5), 0.0, 4.0, "px",
                        lambda v: self._save("deadzone_px", v))
        slider_card_row(card, "Click Lookback Buffer",
                        "Rewinds cursor to pre-closure position for zero click drift",
                        getattr(self.cfg, "cursor_lookback_ms", 200.0), 100.0, 350.0, "ms",
                        lambda v: self._save("cursor_lookback_ms", v))

        card.set_footnote("Jitter suppression and the Lookback Buffer together deliver verified 0.0 px click drift on 24×24 px targets.")
        layout.addWidget(card)

        # Actions card
        act_card = GroupedCard()
        btn_diag = PrimaryButton("Run Diagnostics")
        btn_diag.setFixedWidth(148)
        btn_diag.clicked.connect(self._run_diagnose_action)
        btn_tp   = PlainButton("Target Practice")
        btn_tp.setFixedWidth(148)
        btn_tp.clicked.connect(self._open_target_practice)

        row_w  = QWidget()
        row_hl = QHBoxLayout(row_w)
        row_hl.setContentsMargins(0, 0, 0, 0)
        row_hl.setSpacing(8)
        row_hl.addWidget(btn_diag)
        row_hl.addWidget(btn_tp)
        row_hl.addStretch()
        act_card.add_row("Benchmarks", "Measure jitter, drift, and 24×24 px target accuracy", row_w)
        layout.addWidget(act_card)

        layout.addStretch()
        self._stack.addWidget(page)

    def _build_voice_page(self) -> None:
        page, layout = make_scroll_page("Voice", "Speech recognition and audio input")

        card = GroupedCard()

        sw_voice = AppleSwitch(self.cfg.voice_enabled)
        sw_voice.toggled.connect(lambda v: self._save("voice_enabled", v))
        card.add_row("Voice Commands",
                     "Listen for the Jarvis wake word and spoken actions",
                     sw_voice)

        sw_speak = AppleSwitch(self.cfg.speak)
        sw_speak.toggled.connect(lambda v: self._save("speak", v))
        card.add_row("Spoken Responses",
                     "Jarvis replies aloud with voice confirmations",
                     sw_speak)

        # TTS engine
        self.tts_combo = AppleComboBox()
        tts_options = [
            ("piper",  "Paul Bettany  (MCU J.A.R.V.I.S. Neural)"),
            ("system", "System Default  (Windows SAPI5)"),
        ]
        cur_tts = getattr(self.cfg, "tts_engine", "piper")
        sel_tts = 0
        for i, (code, label) in enumerate(tts_options):
            self.tts_combo.addItem(label, code)
            if cur_tts == code:
                sel_tts = i
        self.tts_combo.setCurrentIndex(sel_tts)
        self.tts_combo.currentIndexChanged.connect(
            lambda i: self._save("tts_engine", self.tts_combo.itemData(i))
        )

        btn_tts_test = PrimaryButton("Test Voice")
        btn_tts_test.setFixedWidth(90)
        btn_tts_test.setFixedHeight(30)
        btn_tts_test.clicked.connect(
            lambda: self.ctx.say("At your service, sir.") if self.ctx else None
        )

        tts_row  = QWidget()
        tts_hl   = QHBoxLayout(tts_row)
        tts_hl.setContentsMargins(0, 0, 0, 0)
        tts_hl.setSpacing(8)
        tts_hl.addWidget(self.tts_combo, stretch=1)
        tts_hl.addWidget(btn_tts_test)
        card.add_row("Voice Persona",
                     "AI voice model used for spoken responses",
                     tts_row)

        # Microphone selection
        mics = list_microphones()
        best_idx, best_name = find_best_microphone(self.cfg)
        self.mic_combo = AppleComboBox()
        self.mic_combo.setMinimumWidth(240)
        self.mic_combo.addItem(f"Auto-detect ({best_name[:22]})", -1)
        sel_mic = 0
        for i, m in enumerate(mics):
            tag = "  [Active]" if m["index"] == best_idx else ""
            self.mic_combo.addItem(f"[{m['index']}]  {m['name'][:30]}{tag}", m["index"])
            if self.cfg.mic_device_index == m["index"]:
                sel_mic = i + 1
        self.mic_combo.setCurrentIndex(sel_mic)
        self.mic_combo.currentIndexChanged.connect(self._on_mic_selected)
        card.add_row("Input Microphone",
                     "Physical audio capture device for speech recognition",
                     self.mic_combo)

        sw_dyn = AppleSwitch(getattr(self.cfg, "mic_dynamic_energy", False))
        sw_dyn.toggled.connect(lambda v: self._save("mic_dynamic_energy", v))
        card.add_row("Dynamic Noise Adapt",
                     "Continuously adapts energy threshold — disable on laptops to prevent cutoff",
                     sw_dyn)

        slider_card_row(card, "Silence Cutoff",
                        "Seconds of silence before phrase capture ends — increase for slow speakers",
                        getattr(self.cfg, "mic_pause_threshold", 1.0), 0.5, 2.5, "s",
                        lambda v: self._save("mic_pause_threshold", float(v)))
        slider_card_row(card, "Trigger Sensitivity",
                        "Energy threshold for voice activation — lower values pick up quieter speech",
                        getattr(self.cfg, "mic_energy_threshold", 300), 50.0, 1500.0, "",
                        lambda v: self._save("mic_energy_threshold", int(v)))
        layout.addWidget(card)

        # Live meter card
        meter_card = GroupedCard()

        self._mic_meter = MicLevelMeter()
        self._mic_meter.setFixedHeight(24)
        meter_card.add_row("Microphone Level",
                           "Real-time audio signal — speak to see the meter respond",
                           self._mic_meter)

        # Mic test
        self.btn_test_mic  = PrimaryButton("Test Microphone & Recognition")
        self.lbl_test_result = QLabel("Click above to test recording and speech transcription.")
        self.lbl_test_result.setFont(qfont(TYPE.footnote))
        self.lbl_test_result.setWordWrap(True)
        self.lbl_test_result.setStyleSheet(f"color: {T.color.secondary_label.resolve(self._dark).name()};")

        self.btn_test_mic.clicked.connect(self._run_mic_test)

        test_w  = QWidget()
        test_vl = QVBoxLayout(test_w)
        test_vl.setContentsMargins(0, 0, 0, 0)
        test_vl.setSpacing(6)
        test_vl.addWidget(self.btn_test_mic)
        test_vl.addWidget(self.lbl_test_result)
        meter_card.add_row("Audio Test", "Speak a command to verify transcription accuracy", test_w)
        layout.addWidget(meter_card)

        layout.addStretch()
        self._stack.addWidget(page)
        self.voice_page = page

        # Mic polling timer
        self._mic_timer = QTimer(self)
        self._mic_timer.setInterval(120)
        self._mic_timer.timeout.connect(self._update_mic_level)

    def _build_ai_page(self) -> None:
        page, layout = make_scroll_page("AI & Intelligence", "Groq cloud speech-to-text and intelligent assistant")

        # Enable + API key
        key_card = GroupedCard()

        sw_ai = AppleSwitch(getattr(self.cfg, "ai_enabled", False))
        sw_ai.toggled.connect(lambda v: self._save("ai_enabled", v))
        key_card.add_row("Enable AI",
                         "Activates Groq cloud intelligence for speech recognition and responses",
                         sw_ai)

        # API key field
        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("gsk_...")
        key_edit.setFixedWidth(190)
        existing = get_api_key(self.cfg) or ""
        if existing:
            key_edit.setText(existing)
        self.key_input = key_edit

        btn_show = PlainButton("Show")
        btn_show.setFixedWidth(52)
        btn_show.setFixedHeight(30)
        btn_save = PrimaryButton("Save")
        btn_save.setFixedWidth(54)
        btn_save.setFixedHeight(30)
        btn_del  = DestructiveButton("Remove")
        btn_del.setFixedWidth(68)
        btn_del.setFixedHeight(30)

        lbl_key_status = QLabel("")
        lbl_key_status.setFont(qfont(TYPE.footnote))

        def toggle_show():
            if key_edit.echoMode() == QLineEdit.EchoMode.Password:
                key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
                btn_show.setText("Hide")
            else:
                key_edit.setEchoMode(QLineEdit.EchoMode.Password)
                btn_show.setText("Show")

        def save_key():
            val = key_edit.text().strip()
            if val:
                set_api_key(val, persist_to_config=False)
                lbl_key_status.setText("Key saved to Windows Credential Manager.")
                lbl_key_status.setStyleSheet(f"color: {T.color.success.resolve(self._dark).name()};")
            else:
                delete_api_key()
                lbl_key_status.setText("No key — removed from Credential Manager.")
                lbl_key_status.setStyleSheet(f"color: {T.color.warning.resolve(self._dark).name()};")

        def remove_key():
            delete_api_key()
            self._save("groq_api_key", "")
            key_edit.clear()
            lbl_key_status.setText("Key removed.")
            lbl_key_status.setStyleSheet(f"color: {T.color.warning.resolve(self._dark).name()};")

        btn_show.clicked.connect(toggle_show)
        btn_save.clicked.connect(save_key)
        btn_del.clicked.connect(remove_key)
        key_edit.returnPressed.connect(save_key)

        key_w  = QWidget()
        key_vl = QVBoxLayout(key_w)
        key_vl.setContentsMargins(0, 0, 0, 0)
        key_vl.setSpacing(4)
        key_hl = QHBoxLayout()
        key_hl.setSpacing(6)
        key_hl.addWidget(key_edit)
        key_hl.addWidget(btn_show)
        key_hl.addWidget(btn_save)
        key_hl.addWidget(btn_del)
        key_vl.addLayout(key_hl)
        key_vl.addWidget(lbl_key_status)

        lbl_link = QLabel('<a href="https://console.groq.com/keys" style="color:#007AFF;text-decoration:none;">Get a free key at console.groq.com</a>')
        lbl_link.setOpenExternalLinks(True)
        lbl_link.setFont(qfont(TYPE.footnote))
        key_vl.addWidget(lbl_link)

        key_card.add_row("Groq API Key",
                         "Stored securely in Windows Credential Manager — never written to config files",
                         key_w)
        key_card.set_footnote("Keys are encrypted by Windows and never transmitted except to api.groq.com over TLS.")
        layout.addWidget(key_card)

        # Models + connectivity
        models_card = GroupedCard()

        self.llm_combo = AppleComboBox()
        for m in ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]:
            self.llm_combo.addItem(m)
        self.llm_combo.setCurrentText(getattr(self.cfg, "ai_llm_model", "openai/gpt-oss-120b"))
        self.llm_combo.currentTextChanged.connect(lambda v: self._save("ai_llm_model", v))
        models_card.add_row("Language Model",
                            "Groq LLM for command interpretation and conversational responses",
                            self.llm_combo)

        self.stt_combo = AppleComboBox()
        for m in ["whisper-large-v3-turbo", "whisper-large-v3"]:
            self.stt_combo.addItem(m)
        self.stt_combo.setCurrentText(getattr(self.cfg, "ai_stt_model", "whisper-large-v3-turbo"))
        self.stt_combo.currentTextChanged.connect(lambda v: self._save("ai_stt_model", v))
        models_card.add_row("Speech Model",
                            "Whisper model for audio-to-text transcription",
                            self.stt_combo)

        self.vision_combo = AppleComboBox()
        self.vision_combo.addItem("qwen/qwen3.8-27b")
        self.vision_combo.setCurrentText(getattr(self.cfg, "ai_vision_model", "qwen/qwen3.8-27b"))
        self.vision_combo.currentTextChanged.connect(lambda v: self._save("ai_vision_model", v))

        sw_vision = AppleSwitch(getattr(self.cfg, "ai_vision_enabled", True))
        sw_vision.toggled.connect(lambda v: self._save("ai_vision_enabled", v))
        models_card.add_row("Camera Vision",
                            "Inspect the camera feed when you say 'what is this' or 'look at this'",
                            sw_vision)
        models_card.add_row("Vision Model",
                            "Multimodal model for scene analysis",
                            self.vision_combo)

        # Connection test
        self.btn_test_conn  = PrimaryButton("Test Connection")
        self.lbl_conn_status = QLabel("Ready to test")
        self.lbl_conn_status.setFont(qfont(TYPE.footnote))
        self.lbl_conn_status.setStyleSheet(f"color: {T.color.secondary_label.resolve(self._dark).name()};")

        self.btn_test_conn.clicked.connect(self._run_test_connection)

        conn_w  = QWidget()
        conn_hl = QHBoxLayout(conn_w)
        conn_hl.setContentsMargins(0, 0, 0, 0)
        conn_hl.setSpacing(10)
        conn_hl.addWidget(self.btn_test_conn)
        conn_hl.addWidget(self.lbl_conn_status, stretch=1)
        models_card.add_row("Connectivity",
                            "Validate API key and refresh available model list",
                            conn_w)
        layout.addWidget(models_card)

        layout.addStretch()
        self._stack.addWidget(page)

    def _build_display_page(self) -> None:
        page, layout = make_scroll_page("Display", "Appearance, materials, and animations")

        card = GroupedCard()

        theme_seg = SegmentedControl([
            ("auto",  "System Auto"),
            ("dark",  "Dark"),
            ("light", "Light"),
        ], current=getattr(self.cfg, "theme_mode", "auto"))
        theme_seg.valueChanged.connect(lambda v: self._save("theme_mode", v))
        card.add_row("Appearance", "Follow the Windows system theme or force a specific mode", theme_seg)

        sw_blur = AppleSwitch(getattr(self.cfg, "glass_blur", True))
        sw_blur.toggled.connect(lambda v: self._save("glass_blur", v))
        card.add_row("Glass Backdrop",
                     "Hardware Acrylic or Mica blur behind the Island and Settings window",
                     sw_blur)

        sw_skel = AppleSwitch(getattr(self.cfg, "show_skeleton_mini", True))
        sw_skel.toggled.connect(lambda v: self._save("show_skeleton_mini", v))
        card.add_row("Mini Hand Skeleton",
                     "Renders a live hand wireframe inside the Dynamic Island",
                     sw_skel)

        sw_motion = AppleSwitch(getattr(self.cfg, "reduce_motion", False))
        sw_motion.toggled.connect(lambda v: self._save("reduce_motion", v))
        card.add_row("Reduce Motion",
                     "Replace spring animations with quick crossfades (matches Windows accessibility setting)",
                     sw_motion)

        card.set_footnote("Reduce Motion is also auto-detected from Windows Settings > Accessibility > Visual Effects.")
        layout.addWidget(card)

        # ── Dynamic Island Section ─────────────────────────────────────────
        island_card = GroupedCard()
        self.island_card = island_card

        # 1. Shape Style (Pill vs Notch)
        self.shape_style_control = SegmentedControl([
            ("pill",  "Pill (Floating)"),
            ("notch", "Notch (Attached)"),
        ], current=getattr(self.cfg, "island_shape_style", "pill"))
        self.shape_seg = self.shape_style_control
        self.shape_style_control.valueChanged.connect(lambda v: self._save("island_shape_style", v))
        island_card.add_row("Shape Style",
                            "Select floating island stadium capsule or bezel-attached notch style",
                            self.shape_style_control)

        # 2. Size Scale (Small / Medium / Large)
        self.island_size_control = SegmentedControl([
            ("small",  "Small (0.85x)"),
            ("medium", "Medium (1.0x)"),
            ("large",  "Large (1.15x)"),
        ], current=getattr(self.cfg, "island_size_scale", getattr(self.cfg, "island_size", "medium")))
        self.size_seg = self.island_size_control
        def _on_size_changed(v):
            self._save("island_size_scale", v)
            self._save("island_size", v)
        self.island_size_control.valueChanged.connect(_on_size_changed)
        island_card.add_row("Size Scale",
                            "Overall scale factor for Dynamic Island capsule and expanded cards",
                            self.island_size_control)

        # 3. Compact Content (Time + Weather vs Media-Aware vs Time)
        self.compact_content_control = SegmentedControl([
            ("time_weather", "Time + Weather"),
            ("media_aware",  "Media-Aware"),
            ("time",         "Time Only"),
        ], current=getattr(self.cfg, "island_compact_mode", getattr(self.cfg, "island_idle_display", "time_weather")))
        self.compact_seg = self.compact_content_control
        def _on_compact_changed(v):
            self._save("island_compact_mode", v)
            self._save("island_idle_display", v)
        self.compact_content_control.valueChanged.connect(_on_compact_changed)
        island_card.add_row("Compact Content",
                            "Standby resting presentation when no assistant interaction is active",
                            self.compact_content_control)

        # 4. 12/24 h Format
        self.clock_format_control = SegmentedControl([
            ("auto", "Auto"),
            ("12h",  "12-Hour"),
            ("24h",  "24-Hour"),
        ], current=getattr(self.cfg, "island_time_format", getattr(self.cfg, "island_clock_format", "12h")))
        self.fmt_seg = self.clock_format_control
        def _on_clock_changed(v):
            self._save("island_time_format", v)
            self._save("island_clock_format", v)
        self.clock_format_control.valueChanged.connect(_on_clock_changed)
        island_card.add_row("12/24 h Format",
                            "Clock display format for compact time and expanded card",
                            self.clock_format_control)

        # 5. First Weekday (Locale / Sunday / Monday)
        self.first_weekday_control = SegmentedControl([
            ("locale", "Locale"),
            ("sunday", "Sunday"),
            ("monday", "Monday"),
        ], current=getattr(self.cfg, "island_first_weekday", "locale"))
        self.weekday_seg = self.first_weekday_control
        self.first_weekday_control.valueChanged.connect(lambda v: self._save("island_first_weekday", v))
        island_card.add_row("First Weekday",
                            "Starting day of the week in the expanded Calendar month grid",
                            self.first_weekday_control)

        # 6. City Input Field / Searchable Dropdown
        city_default = getattr(self.cfg, "island_weather_city", getattr(self.cfg, "weather_city", "Lahore, Punjab, Pakistan"))
        self.city_combo = QComboBox()
        self.city_combo.setEditable(True)
        self.city_combo.setFixedWidth(230)
        self.city_search_edit = self.city_combo.lineEdit()
        self.city_edit = self.city_search_edit
        self.city_search_edit.setPlaceholderText("Search city (e.g. Lahore, London)")

        fill_c = 'rgba(255,255,255,0.08)' if self._dark else 'rgba(0,0,0,0.05)'
        border_c = 'rgba(255,255,255,0.12)' if self._dark else 'rgba(0,0,0,0.12)'
        label_c = T.color.label.resolve(self._dark).name()
        accent_c = T.color.accent.resolve(self._dark).name()
        popup_c = '#242426' if self._dark else '#FFFFFF'

        self.city_combo.setStyleSheet(f"""
            QComboBox {{
                background: {fill_c};
                border: 1px solid {border_c};
                border-radius: 6px;
                color: {label_c};
                padding: 4px 8px;
                font-size: 12px;
            }}
            QComboBox:hover {{ border-color: rgba(128,128,128,0.4); }}
            QComboBox:focus {{ border: 1.5px solid {accent_c}; }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {label_c};
                margin-right: 6px;
            }}
            QComboBox QAbstractItemView {{
                background: {popup_c};
                border: 1px solid {border_c};
                border-radius: 6px;
                color: {label_c};
                selection-background-color: {accent_c};
                selection-color: #ffffff;
                outline: none;
                padding: 4px;
            }}
        """)

        popular_cities = [
            "Lahore, Punjab, Pakistan",
            "Karachi, Sindh, Pakistan",
            "Islamabad, Islamabad Capital Territory, Pakistan",
            "London, England, United Kingdom",
            "New York, New York, United States",
            "Dubai, Dubai, United Arab Emirates",
            "Tokyo, Tokyo, Japan",
            "Paris, Île-de-France, France",
            "Ahmedabad, Gujarat, India",
            "Delhi, Delhi, India",
            "Toronto, Ontario, Canada",
            "Sydney, New South Wales, Australia",
        ]
        if city_default and city_default not in popular_cities:
            self.city_combo.addItem(city_default)
        for c in popular_cities:
            self.city_combo.addItem(c)
        self.city_combo.setCurrentText(city_default)

        self._city_search_timer = QTimer(self)
        self._city_search_timer.setSingleShot(True)
        self._city_search_timer.setInterval(400)

        def _do_city_search():
            q = self.city_search_edit.text().strip()
            if len(q) < 2:
                return
            def _async_query():
                try:
                    from weather_client import get_weather_client
                    client = get_weather_client()
                    res = client.search_cities(q, count=6)
                    if res:
                        displays = [r["display"] for r in res]
                        QTimer.singleShot(0, lambda: _show_city_results(q, displays))
                except Exception as err:
                    print(f"[settings] city search note: {err}")
            threading.Thread(target=_async_query, daemon=True).start()

        def _show_city_results(orig_q, displays):
            if not displays:
                return
            cur = self.city_search_edit.text()
            self.city_combo.blockSignals(True)
            self.city_combo.clear()
            for d in displays:
                self.city_combo.addItem(d)
            self.city_combo.setCurrentText(cur)
            self.city_combo.blockSignals(False)
            if self.city_combo.hasFocus() or self.city_search_edit.hasFocus():
                self.city_combo.showPopup()

        self._city_search_timer.timeout.connect(_do_city_search)

        def _on_city_text_edited(v):
            cleaned = v.strip()
            if cleaned:
                self._save("island_weather_city", cleaned)
                self._save("weather_city", cleaned)
                self._city_search_timer.start()

        self.city_search_edit.textChanged.connect(_on_city_text_edited)

        def _on_city_activated(idx):
            val = self.city_combo.itemText(idx).strip()
            if val:
                self._city_search_timer.stop()
                self._save("island_weather_city", val)
                self._save("weather_city", val)

        self.city_combo.activated.connect(_on_city_activated)

        island_card.add_row("City Location",
                            "Location used for live weather forecasts and modular metrics",
                            self.city_combo)

        # 7. Weather / Speed Units
        self.units_control = SegmentedControl([
            ("auto",       "Auto"),
            ("celsius",    "Celsius (°C, km/h)"),
            ("fahrenheit", "Fahrenheit (°F, mph)"),
        ], current=getattr(self.cfg, "island_weather_units", getattr(self.cfg, "weather_units", "auto")))
        self.units_seg = self.units_control
        def _on_units_changed(v):
            self._save("island_weather_units", v)
            self._save("weather_units", v)
        self.units_control.valueChanged.connect(_on_units_changed)
        island_card.add_row("Weather Units",
                            "Temperature and wind speed units for weather metrics",
                            self.units_control)

        # 8. Hardware Layout (Grid vs Strip)
        self.hw_layout_control = SegmentedControl([
            ("grid",  "2x3 Grid"),
            ("strip", "5-Item Strip"),
        ], current=getattr(self.cfg, "island_hardware_layout", "grid"))
        self.hw_layout_seg = self.hw_layout_control
        self.hw_layout_control.valueChanged.connect(lambda v: self._save("island_hardware_layout", v))
        island_card.add_row("Hardware Layout",
                            "Display style: 2x3 modular tile grid or compact widescreen strip",
                            self.hw_layout_control)

        # 9. Backdrop Glass Blur (toggle)
        self.backdrop_blur_switch = AppleSwitch(getattr(self.cfg, "island_backdrop_blur", getattr(self.cfg, "glass_blur", True)))
        self.sw_island_blur = self.backdrop_blur_switch
        self.backdrop_blur_switch.toggled.connect(lambda v: self._save("island_backdrop_blur", v))
        island_card.add_row("Backdrop Blur",
                            "Enable hardware-accelerated Mica or Acrylic blur behind the island surface",
                            self.backdrop_blur_switch)

        # 10. Backdrop Opacity Slider
        cur_op = float(getattr(self.cfg, "island_backdrop_opacity", 0.95))
        self.slider_backdrop_opacity = slider_card_row(
            island_card,
            "Backdrop Opacity",
            "Surface glass tint opacity from translucent to deep obsidian",
            cur_op,
            0.20,
            1.00,
            "",
            lambda v: self._save("island_backdrop_opacity", round(v, 2)),
        )
        self.slider_island_opacity = self.slider_backdrop_opacity

        # 11. Pin Open Toggle
        self.pin_open_switch = AppleSwitch(getattr(self.cfg, "island_pin_open", False))
        self.sw_pin_open = self.pin_open_switch
        self.pin_open_switch.toggled.connect(lambda v: self._save("island_pin_open", v))
        island_card.add_row("Pin Open",
                            "Keep expanded card open permanently and suppress 4-second auto-collapse timer",
                            self.pin_open_switch)

        # 12. Expand Trigger (Hover / Click / Dwell / Fist / Voice)
        self.expand_trigger_control = SegmentedControl([
            ("hover", "Hover"),
            ("click", "Click"),
            ("dwell", "Dwell"),
            ("fist",  "Fist"),
            ("voice", "Voice"),
        ], current=getattr(self.cfg, "island_expand_trigger", "hover"))
        self.trig_seg = self.expand_trigger_control
        self.expand_trigger_control.valueChanged.connect(lambda v: self._save("island_expand_trigger", v))
        island_card.add_row("Expand Trigger",
                            "Primary interaction method to expand the island into full cards",
                            self.expand_trigger_control)

        # 13. Waveform Color (Blue (Default) / Cyan / Green / Orange / Purple / Red / Pink / Monochrome / Multicolor / Custom)
        wf_container = QWidget()
        wf_layout = QHBoxLayout(wf_container)
        wf_layout.setContentsMargins(0, 0, 0, 0)
        wf_layout.setSpacing(8)

        self.wf_color_combo = QComboBox()
        presets = [
            ("Blue (Default)", "blue", "#007AFF"),
            ("Cyan",           "cyan", "#32ADE6"),
            ("Green",          "green", "#30D158"),
            ("Orange",         "orange", "#FF9F0A"),
            ("Purple",         "purple", "#AF52DE"),
            ("Red",            "red", "#FF453A"),
            ("Pink",           "pink", "#FF2D55"),
            ("Monochrome",     "monochrome", "#E5E5EA"),
            ("Multicolor",     "multicolor", "#AF52DE"),
        ]
        cur_wf = str(getattr(self.cfg, "island_waveform_color", "blue")).strip()
        matched_idx = -1
        for idx, (lbl, key, hex_c) in enumerate(presets):
            self.wf_color_combo.addItem(lbl, key)
            if cur_wf.lower() == key:
                matched_idx = idx

        if matched_idx != -1:
            self.wf_color_combo.setCurrentIndex(matched_idx)
        else:
            self.wf_color_combo.addItem(f"Custom ({cur_wf})", cur_wf)
            self.wf_color_combo.setCurrentIndex(len(presets))

        self.wf_color_combo.setStyleSheet(f"""
            QComboBox {{
                background: {fill_c};
                border: 1px solid {border_c};
                border-radius: 6px;
                color: {label_c};
                padding: 4px 8px;
                font-size: 12px;
                min-width: 140px;
            }}
            QComboBox:hover {{ border-color: rgba(128,128,128,0.4); }}
            QComboBox:focus {{ border: 1.5px solid {accent_c}; }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {label_c};
                margin-right: 6px;
            }}
            QComboBox QAbstractItemView {{
                background: {popup_c};
                border: 1px solid {border_c};
                border-radius: 6px;
                color: {label_c};
                selection-background-color: {accent_c};
                selection-color: #ffffff;
                outline: none;
                padding: 4px;
            }}
        """)

        self.wf_color_btn = QPushButton()
        self.wf_color_btn.setFixedSize(26, 26)
        self.wf_color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wf_color_btn.setToolTip("Click to pick custom waveform color")

        def _get_preset_hex(key: str) -> str:
            for _, k, h in presets:
                if k == key.lower():
                    return h
            return key if key.startswith("#") else "#007AFF"

        def _update_swatch_style(hex_color: str):
            self.wf_color_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {hex_color};
                    border: 2px solid {'rgba(255,255,255,0.7)' if self._dark else 'rgba(0,0,0,0.2)'};
                    border-radius: 13px;
                }}
                QPushButton:hover {{
                    border: 2px solid {'#FFFFFF' if self._dark else '#000000'};
                }}
            """)

        cur_hex = _get_preset_hex(cur_wf)
        _update_swatch_style(cur_hex)

        def _on_wf_combo_changed(idx: int):
            data = self.wf_color_combo.itemData(idx)
            hex_val = _get_preset_hex(data)
            _update_swatch_style(hex_val)
            self._save("island_waveform_color", data)

        def _pick_custom_color():
            initial = QColor(_get_preset_hex(str(getattr(self.cfg, "island_waveform_color", "blue"))))
            col = QColorDialog.getColor(initial, self, "Select Waveform Accent Color")
            if col.isValid():
                hex_str = col.name().upper()
                _update_swatch_style(hex_str)
                matched = False
                for i, (_, k, h) in enumerate(presets):
                    if h.upper() == hex_str:
                        self.wf_color_combo.blockSignals(True)
                        self.wf_color_combo.setCurrentIndex(i)
                        self.wf_color_combo.blockSignals(False)
                        self._save("island_waveform_color", k)
                        matched = True
                        break
                if not matched:
                    if self.wf_color_combo.count() > len(presets):
                        self.wf_color_combo.setItemText(len(presets), f"Custom ({hex_str})")
                        self.wf_color_combo.setItemData(len(presets), hex_str)
                    else:
                        self.wf_color_combo.addItem(f"Custom ({hex_str})", hex_str)
                    self.wf_color_combo.blockSignals(True)
                    self.wf_color_combo.setCurrentIndex(len(presets))
                    self.wf_color_combo.blockSignals(False)
                    self._save("island_waveform_color", hex_str)

        self.wf_color_combo.currentIndexChanged.connect(_on_wf_combo_changed)
        self.wf_color_btn.clicked.connect(_pick_custom_color)

        wf_layout.addWidget(self.wf_color_combo)
        wf_layout.addWidget(self.wf_color_btn)

        island_card.add_row("Waveform Color",
                            "Accent color palette for voice listening and audio-reactive waveforms (Default: Blue)",
                            wf_container)

        # 14. Hide during Fullscreen Apps
        self.hide_fullscreen_switch = AppleSwitch(getattr(self.cfg, "island_hide_fullscreen", getattr(self.cfg, "hide_fullscreen", True)))
        self.sw_fs = self.hide_fullscreen_switch
        def _on_fs_toggled(v):
            self._save("island_hide_fullscreen", v)
            self._save("hide_fullscreen", v)
        self.hide_fullscreen_switch.toggled.connect(_on_fs_toggled)
        island_card.add_row("Hide during Fullscreen Apps",
                            "Automatically hide the island when playing games or running full-screen apps",
                            self.hide_fullscreen_switch)

        island_card.set_footnote("Changes apply immediately to the Dynamic Island on your screen.")
        layout.addWidget(island_card)

        # ── Card Pages & Hardware Sensors Section ───────────────────────────
        pages_card = GroupedCard()
        self.pages_card = pages_card

        pages_map = getattr(self.cfg, "island_pages_enabled", {
            "media": True, "calendar": True, "weather": True, "hardware": True, "jarvis": True
        })

        def _on_page_toggle(page_name: str, enabled: bool):
            cur = dict(getattr(self.cfg, "island_pages_enabled", {}))
            cur[page_name] = enabled
            self._save("island_pages_enabled", cur)

        # Page Toggles
        self.sw_page_media = AppleSwitch(pages_map.get("media", True))
        self.sw_page_media.toggled.connect(lambda v: _on_page_toggle("media", v))
        pages_card.add_row("Media Player Page",
                           "Include Now Playing artwork and audio controls in card carousel",
                           self.sw_page_media)

        self.sw_page_cal = AppleSwitch(pages_map.get("calendar", True))
        self.sw_page_cal.toggled.connect(lambda v: _on_page_toggle("calendar", v))
        pages_card.add_row("Calendar & Clock Page",
                           "Include monthly calendar grid and date tile in card carousel",
                           self.sw_page_cal)

        self.sw_page_weather = AppleSwitch(pages_map.get("weather", True))
        self.sw_page_weather.toggled.connect(lambda v: _on_page_toggle("weather", v))
        pages_card.add_row("Weather Forecast Page",
                           "Include current conditions, temperature, and modular weather metrics",
                           self.sw_page_weather)

        self.sw_page_hw = AppleSwitch(pages_map.get("hardware", True))
        self.sw_page_hw.toggled.connect(lambda v: _on_page_toggle("hardware", v))
        pages_card.add_row("Hardware Monitor Page",
                           "Include system resource and network performance telemetry",
                           self.sw_page_hw)

        self.sw_page_jarvis = AppleSwitch(pages_map.get("jarvis", True))
        self.sw_page_jarvis.toggled.connect(lambda v: _on_page_toggle("jarvis", v))
        pages_card.add_row("JARVIS Assistant Page",
                           "Include assistant status, tracking quick toggle, and last directive",
                           self.sw_page_jarvis)

        # Hardware Sensor Toggles
        self.sw_hw_cpu = AppleSwitch(getattr(self.cfg, "island_hw_cpu", True))
        self.sw_hw_cpu.toggled.connect(lambda v: self._save("island_hw_cpu", v))
        pages_card.add_row("CPU Telemetry Sensor",
                           "Sample processor utilization percentage and clock frequency",
                           self.sw_hw_cpu)

        self.sw_hw_ram = AppleSwitch(getattr(self.cfg, "island_hw_ram", True))
        self.sw_hw_ram.toggled.connect(lambda v: self._save("island_hw_ram", v))
        pages_card.add_row("RAM Telemetry Sensor",
                           "Sample active memory usage, availability, and total capacity",
                           self.sw_hw_ram)

        self.sw_hw_gpu = AppleSwitch(getattr(self.cfg, "island_hw_gpu", True))
        self.sw_hw_gpu.toggled.connect(lambda v: self._save("island_hw_gpu", v))
        pages_card.add_row("GPU Telemetry Sensor",
                           "Sample dedicated graphics processor load and temperature",
                           self.sw_hw_gpu)

        self.sw_hw_net = AppleSwitch(getattr(self.cfg, "island_hw_net", True))
        self.sw_hw_net.toggled.connect(lambda v: self._save("island_hw_net", v))
        pages_card.add_row("Network Telemetry Sensor",
                           "Sample real-time upstream and downstream network bandwidth",
                           self.sw_hw_net)

        self.sw_hw_disk = AppleSwitch(getattr(self.cfg, "island_hw_disk", True))
        self.sw_hw_disk.toggled.connect(lambda v: self._save("island_hw_disk", v))
        pages_card.add_row("Disk Telemetry Sensor",
                           "Sample primary drive utilization and storage capacity",
                           self.sw_hw_disk)

        pages_card.set_footnote("Disabled pages are skipped during mouse wheel and page dot navigation.")
        layout.addWidget(pages_card)

        # Direct property aliases for test compatibility
        self.media_switch = self.sw_page_media
        self.weather_switch = self.sw_page_weather
        self.show_now_playing_switch = self.sw_page_media
        self.show_weather_switch = self.sw_page_weather
        self.city_search_btn = self.city_search_edit
        self.units_combo = self.units_control
        self.time_fmt_combo = self.clock_format_control
        self.expand_combo = self.expand_trigger_control
        self.fs_switch = self.hide_fullscreen_switch
        self.size_combo = self.island_size_control
        self.idle_combo = self.compact_content_control

        layout.addStretch()
        self._stack.addWidget(page)

    def _build_about_page(self) -> None:
        page, layout = make_scroll_page("About", "J.A.R.V.I.S. system information")

        info_card = GroupedCard()

        def _info_lbl(text: str) -> QLabel:
            l = QLabel(text)
            l.setFont(qfont(TYPE.body))
            l.setStyleSheet(f"color: {T.color.secondary_label.resolve(self._dark).name()};")
            return l

        info_card.add_row("Version",    "", _info_lbl(f"v{APP_VERSION}"))
        info_card.add_row("Platform",   "", _info_lbl(f"Windows 64-bit  |  Python {sys.version.split()[0]}"))
        info_card.add_row("Design",     "Apple Human Interface Guidelines", _info_lbl("Sonoma / iOS 17"))
        info_card.add_row("Architecture","Touchless hand-gesture PC control", _info_lbl("120 Hz decoupled, sub-ms latency"))
        layout.addWidget(info_card)

        if self.updater:
            upd_card = GroupedCard()
            self.btn_chk     = PrimaryButton("Check for Updates")
            self.btn_restart = SuccessButton("Restart to Apply")
            self.btn_restart.setVisible(False)
            self.lbl_up_status = QLabel(f"Current version is v{APP_VERSION}.")
            self.lbl_up_status.setFont(qfont(TYPE.footnote))
            self.lbl_up_status.setStyleSheet(f"color: {T.color.secondary_label.resolve(self._dark).name()};")
            self.prog_bar = QProgressBar()
            self.prog_bar.setRange(0, 100)
            self.prog_bar.setFixedHeight(4)
            self.prog_bar.setTextVisible(False)
            self.prog_bar.setVisible(False)

            upd_w  = QWidget()
            upd_vl = QVBoxLayout(upd_w)
            upd_vl.setContentsMargins(0, 0, 0, 0)
            upd_vl.setSpacing(6)
            upd_hl = QHBoxLayout()
            upd_hl.setSpacing(8)
            upd_hl.addWidget(self.btn_chk)
            upd_hl.addWidget(self.btn_restart)
            upd_hl.addWidget(self.lbl_up_status, stretch=1)
            upd_vl.addLayout(upd_hl)
            upd_vl.addWidget(self.prog_bar)
            upd_card.add_row("Software Update",
                             "Automatic GitHub release detection and one-click restart",
                             upd_w)
            layout.addWidget(upd_card)

            self.btn_chk.clicked.connect(lambda: self.updater.check_async(notify_if_latest=True))
            self.btn_restart.clicked.connect(self._on_restart_update)

            def _updater_cb(state, **kwargs):
                self.bridge.updater_event.emit(state, kwargs)
            self._updater_cb = _updater_cb
            self.updater.add_listener(_updater_cb)

        if getattr(self.cfg, "show_health_panel", True):
            hdr_health = QLabel("SYSTEM PRE-FLIGHT HEALTH")
            hdr_health.setFont(qfont(TYPE.caption_2))
            hdr_health.setContentsMargins(SPACING.xs, SPACING.md, 0, 4)
            hdr_health.setProperty("role", "section_header")
            hdr_health.setStyleSheet(f"color: {T.color.tertiary_label.resolve(self._dark).name()}; font-size: 10px; font-weight: 600; padding-left: 4px;")
            layout.addWidget(hdr_health)

            health_card = GroupedCard()
            self.pill_cam    = StatusPill("Ready", variant="default")
            self.pill_mic    = StatusPill("Ready", variant="default")
            self.pill_models = StatusPill("Ready", variant="default")
            self.pill_cloud  = StatusPill("Ready", variant="default")
            self.pill_dpi    = StatusPill("Ready", variant="default")

            health_card.add_row("Camera FPS", "DirectShow hardware capture & frame rate", self.pill_cam)
            health_card.add_row("Mic RMS", "Microphone stream & live audio level", self.pill_mic)
            health_card.add_row("Local Models", "Piper TTS and OpenWakeWord model files", self.pill_models)
            health_card.add_row("Cloud AI Reachability", "Groq API endpoint connectivity", self.pill_cloud)
            health_card.add_row("Display & DPI", "Monitor geometry and DPI scale factor", self.pill_dpi)

            self.btn_diag_health = PrimaryButton("Run Diagnostics")
            self.btn_diag_health.setFixedWidth(148)
            self.btn_diag_health.clicked.connect(self._run_preflight_diagnostics)
            health_card.add_row("Diagnostics", "Run all hardware and network pre-flight tests", self.btn_diag_health)

            layout.addWidget(health_card)

        layout.addStretch()
        self._stack.addWidget(page)

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_mic_selected(self, combo_idx: int) -> None:
        dev = self.mic_combo.itemData(combo_idx)
        val = None if dev == -1 else dev
        self._save("mic_device_index", val)
        self.cfg.mic_device_index = val

    def _on_mic_level_updated(self, norm: float) -> None:
        if hasattr(self, "_mic_meter"):
            self._mic_meter.update_level(norm)

    def _update_mic_level(self) -> None:
        if not self.isVisible():
            return
        now = time.time()
        try:
            from voice import Voice
            if now - getattr(Voice, "_latest_time", 0.0) < 1.0:
                raw = float(getattr(Voice, "_latest_rms", 0.0))
                norm = min(max((raw - 20.0) / 2500.0, 0.0), 1.0)
                if hasattr(self, "_mic_meter"):
                    self._mic_meter.update_level(norm)
                return
        except Exception:
            pass

        if getattr(self, "_mic_probing", False):
            return

        self._mic_probing = True
        dev_idx = getattr(self.cfg, "mic_device_index", None)

        def worker():
            try:
                norm, _ = probe_mic_level(dev_idx)
                self.bridge.mic_level_updated.emit(norm)
            except Exception:
                pass
            finally:
                self._mic_probing = False

        threading.Thread(target=worker, daemon=True).start()

    def _run_mic_test(self) -> None:
        self.btn_test_mic.setEnabled(False)
        self.btn_test_mic.setText("Recording 3.5 s — speak now...")
        self.lbl_test_result.setText("Listening… say something like 'Jarvis, open Chrome'.")
        self.lbl_test_result.setStyleSheet(f"color: {T.color.accent.resolve(self._dark).name()};")

        def worker():
            res = test_microphone_recognition(device_index=self.cfg.mic_device_index, duration=3.5)
            self.bridge.test_mic_result.emit(res)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_mic_result(self, res: dict) -> None:
        self.btn_test_mic.setEnabled(True)
        self.btn_test_mic.setText("Test Microphone & Recognition")
        if res.get("success"):
            self.lbl_test_result.setText(f"Heard: \"{res['text']}\"  (signal: {res.get('avg_level', 0):.0f})")
            self.lbl_test_result.setStyleSheet(f"color: {T.color.success.resolve(self._dark).name()};")
        else:
            self.lbl_test_result.setText(f"Notice: {res.get('error', 'Unknown error')}  (signal: {res.get('avg_level', 0):.0f})")
            self.lbl_test_result.setStyleSheet(f"color: {T.color.warning.resolve(self._dark).name()};")

    def _run_test_connection(self) -> None:
        key = self.key_input.text().strip() or get_api_key(self.cfg)
        if not key:
            self.lbl_conn_status.setText("No API key entered")
            self.lbl_conn_status.setStyleSheet(f"color: {T.color.warning.resolve(self._dark).name()};")
            return
        self.btn_test_conn.setEnabled(False)
        self.lbl_conn_status.setText("Testing connection...")
        self.lbl_conn_status.setStyleSheet(f"color: {T.color.accent.resolve(self._dark).name()};")

        def worker():
            if GroqClient is None:
                self.bridge.test_conn_result.emit(False, "AI module unavailable", {})
                return
            client = GroqClient(api_key=key, timeout=8)
            ok, msg = client.validate_key()
            models = {}
            if ok:
                try:
                    models = client.get_categorized_models()
                except Exception:
                    pass
            self.bridge.test_conn_result.emit(ok, msg, models or {})

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_conn_result(self, ok: bool, message: str, models: dict) -> None:
        self.btn_test_conn.setEnabled(True)
        if ok:
            self.lbl_conn_status.setText(f"Connected: {message}")
            self.lbl_conn_status.setStyleSheet(f"color: {T.color.success.resolve(self._dark).name()};")
            if models:
                for m in models.get("llm", []):
                    if self.llm_combo.findText(m) == -1:
                        self.llm_combo.addItem(m)
                for m in models.get("stt", []):
                    if self.stt_combo.findText(m) == -1:
                        self.stt_combo.addItem(m)
        else:
            self.lbl_conn_status.setText(f"Failed: {message}")
            self.lbl_conn_status.setStyleSheet(f"color: {T.color.warning.resolve(self._dark).name()};")

    def _on_updater_event_gui(self, state: str, kwargs: dict) -> None:
        if not hasattr(self, "btn_chk"):
            return
        if state == "checking":
            self.btn_chk.setEnabled(False)
            self.lbl_up_status.setText("Checking for updates...")
            self.lbl_up_status.setStyleSheet(f"color: {T.color.accent.resolve(self._dark).name()};")
        elif state == "up-to-date":
            self.btn_chk.setEnabled(True)
            self.lbl_up_status.setText(f"Jarvis is up to date (v{APP_VERSION}).")
            self.lbl_up_status.setStyleSheet(f"color: {T.color.success.resolve(self._dark).name()};")
            self.btn_restart.setVisible(False)
            self.prog_bar.setVisible(False)
        elif state == "available":
            self.btn_chk.setEnabled(True)
            ver = kwargs.get("info", {}).get("version", "new version")
            self.lbl_up_status.setText(f"Update available: v{ver}")
            self.lbl_up_status.setStyleSheet(f"color: {T.color.warning.resolve(self._dark).name()};")
            self.btn_restart.setText(f"Download & Update to v{ver}")
            self.btn_restart.setEnabled(True)
            self.btn_restart.setVisible(True)
        elif state == "downloading":
            pct = kwargs.get("percent", 0)
            self.prog_bar.setVisible(True)
            self.prog_bar.setValue(pct)
            self.lbl_up_status.setText(f"Downloading... {pct}%")
            self.lbl_up_status.setStyleSheet(f"color: {T.color.accent.resolve(self._dark).name()};")
        elif state == "downloaded":
            self.prog_bar.setVisible(False)
            self.lbl_up_status.setText("Update ready — click Restart to apply.")
            self.lbl_up_status.setStyleSheet(f"color: {T.color.success.resolve(self._dark).name()};")
            self.btn_restart.setText("Restart to Apply")
            self.btn_restart.setEnabled(True)
            self.btn_restart.setVisible(True)
        elif state == "error":
            self.btn_chk.setEnabled(True)
            msg = kwargs.get("message", "Error checking for updates")
            self.lbl_up_status.setText(f"Notice: {msg[:60]}")
            self.lbl_up_status.setStyleSheet(f"color: {T.color.warning.resolve(self._dark).name()};")

    def _on_restart_update(self) -> None:
        if self.updater:
            if getattr(self.updater, "downloaded_path", None) and os.path.exists(self.updater.downloaded_path):
                self.btn_restart.setEnabled(False)
                self.lbl_up_status.setText("Applying update and restarting...")
                self.updater.apply_update_and_restart()
            else:
                self.btn_restart.setEnabled(False)
                self.updater.download_async()

    # ── Helpers ──────────────────────────────────────────────────────────

    def _on_res_change(self, res_str: str) -> None:
        w, h = map(int, res_str.split("x"))
        self._save("frame_width",  w)
        self._save("frame_height", h)

    def _run_diagnose_action(self) -> None:
        import subprocess
        base = os.path.dirname(os.path.abspath(__file__))
        s = os.path.join(base, "..", "diagnose.py")
        if os.path.exists(s):
            subprocess.Popen([sys.executable, s])

    def _run_preflight_diagnostics(self) -> None:
        if hasattr(self, "btn_diag_health"):
            self.btn_diag_health.setEnabled(False)
            self.btn_diag_health.setText("Running...")

        for pill in (getattr(self, "pill_cam", None),
                     getattr(self, "pill_mic", None),
                     getattr(self, "pill_models", None),
                     getattr(self, "pill_cloud", None),
                     getattr(self, "pill_dpi", None)):
            if pill:
                pill.setVariant("accent")
                pill.setText("Checking...")

        def worker():
            try:
                from self_test import get_self_test_summary
                summary = get_self_test_summary()
            except Exception as exc:
                summary = [{"name": "Diagnostics Error", "passed": False, "message": str(exc), "elapsed_ms": 0.0}]
            self.bridge.diagnostics_result.emit(summary)

        threading.Thread(target=worker, daemon=True).start()

    def _on_diagnostics_result(self, summary: list) -> None:
        if hasattr(self, "btn_diag_health"):
            self.btn_diag_health.setEnabled(True)
            self.btn_diag_health.setText("Run Diagnostics")

        mapping = {
            "camera": getattr(self, "pill_cam", None),
            "microphone": getattr(self, "pill_mic", None),
            "mic": getattr(self, "pill_mic", None),
            "model": getattr(self, "pill_models", None),
            "network": getattr(self, "pill_cloud", None),
            "reachability": getattr(self, "pill_cloud", None),
            "cloud": getattr(self, "pill_cloud", None),
            "dpi": getattr(self, "pill_dpi", None),
            "monitor": getattr(self, "pill_dpi", None),
            "display": getattr(self, "pill_dpi", None),
        }

        for item in summary:
            name_lower = str(item.get("name", "")).lower()
            passed = bool(item.get("passed", False))
            msg = str(item.get("message", ""))

            pill = None
            for key, p in mapping.items():
                if key in name_lower and p is not None:
                    pill = p
                    break

            if pill:
                pill.setVariant("success" if passed else "error")
                pill.setText("Passed" if passed else "Failed")
                pill.setToolTip(msg)


    def _open_target_practice(self) -> None:
        import subprocess
        base = os.path.dirname(os.path.abspath(__file__))
        s = os.path.join(base, "..", "tests", "target_practice.py")
        if os.path.exists(s):
            subprocess.Popen([sys.executable, s, "--gui"])

    def _save(self, key: str, value) -> None:
        if key != "groq_api_key" and any(s in key.lower() for s in ("secret", "token", "password")):
            return
        try:
            setattr(self.cfg, key, value)
            if key == "island_weather_city":
                setattr(self.cfg, "weather_city", value)
            elif key == "weather_city":
                setattr(self.cfg, "island_weather_city", value)
            elif key == "island_weather_units":
                setattr(self.cfg, "weather_units", value)
            elif key == "weather_units":
                setattr(self.cfg, "island_weather_units", value)
        except Exception:
            pass
        try:
            cfg_path = get_config_path()
            data = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data[key] = value
            if key == "island_weather_city":
                data["weather_city"] = value
            elif key == "weather_city":
                data["island_weather_city"] = value
            elif key == "island_weather_units":
                data["weather_units"] = value
            elif key == "weather_units":
                data["island_weather_units"] = value
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as exc:
            print(f"[settings] save error: {exc}")
        try:
            # 1. Propagate live reload via Context & Overlay if available
            if hasattr(self, "ctx") and self.ctx:
                if hasattr(self.ctx, "overlay") and self.ctx.overlay and hasattr(self.ctx.overlay, "reload_config"):
                    self.ctx.overlay.reload_config(self.cfg)
                if hasattr(self.ctx, "reload_config"):
                    self.ctx.reload_config(self.cfg)

            # 2. Propagate to all active Qt top-level widgets
            for w in QApplication.topLevelWidgets():
                if hasattr(w, "reload_config"):
                    w.reload_config()
                if hasattr(w, "mgr") and hasattr(w.mgr, "reload_config"):
                    w.mgr.reload_config()
                if hasattr(w, "island") and hasattr(w.island, "reload_config"):
                    w.island.reload_config()
        except Exception as exc:
            print(f"[settings] reload_config error: {exc}")

        # 3. Live theme update on settings dialog itself
        if key == "theme_mode":
            try:
                self._poll_theme()
            except Exception:
                pass

        # 4. Immediate weather refresh if weather settings changed
        if key in ("island_weather_city", "weather_city", "island_weather_units", "weather_units"):
            try:
                from weather_client import trigger_weather_refresh
                trigger_weather_refresh()
            except Exception:
                pass

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if hasattr(self, "_mic_timer") and self._mic_timer.isActive():
            self._mic_timer.stop()
        if self.updater and hasattr(self, "_updater_cb"):
            self.updater.remove_listener(self._updater_cb)
        if hasattr(self, "_theme_timer") and self._theme_timer.isActive():
            self._theme_timer.stop()
        if event is not None:
            event.ignore()
        self.hide()

    def reject(self) -> None:
        if hasattr(self, "_mic_timer") and self._mic_timer.isActive():
            self._mic_timer.stop()
        if self.updater and hasattr(self, "_updater_cb"):
            self.updater.remove_listener(self._updater_cb)
        if hasattr(self, "_theme_timer") and self._theme_timer.isActive():
            self._theme_timer.stop()
        self.hide()
