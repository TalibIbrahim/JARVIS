"""
ui/onboarding_window.py — J.A.R.V.I.S. "Getting to know you" window.

Shown after the island intro.  Same sheet, header, cards, buttons and step bar as the first-run
setup dialog (ui/onboarding.py); only the pages differ:

    hello -> name -> address -> voice sample -> usage + favorite apps -> privacy -> done

Every question can be answered by voice (mic button -> ctx.voice.capture_answer) or typed.  Jarvis reads
each question aloud through ctx.say (when available).  Blocking voice calls run on worker threads and
reach the GUI only through signals.

Everything outside the Qt widgets is reached through optional hooks (ctx.personal, ctx.voice, ctx.say,
ctx.reload_config), so the window still works when a piece is missing.
"""

from __future__ import annotations

import math
import re
import sys
import threading
import time
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics, QPainterPath
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QStackedWidget,
)

from ui.tokens import T, TYPE, SPACING, qfont
from ui.materials import apply_best_backdrop, set_rounded_corners
from ui import glass
from ui.components import (
    GroupedCard, PrimaryButton, PlainButton, SegmentedControl, StatusPill, MicLevelMeter,
)
from ui.onboarding import _StepProgressIndicator

from config import get_config_path, update_key


PAGES = ["hello", "name", "address", "voice", "usage", "privacy", "done"]
_QUESTION_PAGES = 5                     # name .. privacy show the step bar

USES = ["Gaming", "Work", "Study", "Creative"]

VOICE_SAMPLES = [
    "The quick brown fox jumps over the lazy dog, and Jarvis hears every word.",
    "Open my music, set a timer for ten minutes, and tell me the weather today.",
    "Search for my files, turn the volume down, and read me my next meeting.",
]

_SPOKEN = {
    "hello":   "Hello, I'm Jarvis. I'll ask a few quick questions.",
    "name":    "What should I call you?",
    "address": "How should I address you?",
    "voice":   "Read the sentence on screen aloud so I can learn your voice.",
    "usage":   "What do you mostly use this PC for?",
    "privacy": "Here is how I handle your data.",
    "done":    "You're all set.",
}

MAX_SUGGESTIONS = 12


# ──────────────────────────────────────────────────────────────────────────────
# Parts
# ──────────────────────────────────────────────────────────────────────────────

def _mic_path() -> QPainterPath:
    """Lucide-style mic on a 16 grid (same stroke conventions as the island icons)."""
    s = QPainterPath()
    s.addRoundedRect(QRectF(5.9, 1.4, 4.2, 7.6), 2.1, 2.1)
    r = QRectF(3.3, 3.6, 9.4, 9.0)
    s.moveTo(3.3, 8.1)
    s.arcTo(r, 180.0, 180.0)
    s.moveTo(8.0, 12.6)
    s.lineTo(8.0, 14.6)
    s.moveTo(5.6, 14.6)
    s.lineTo(10.4, 14.6)
    return s


def _draw_mic(p: QPainter, cx: float, cy: float, size: float, color: QColor, stroke: float = 1.5) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(cx, cy)
    k = size / 16.0
    p.scale(k, k)
    p.translate(-8, -8)
    p.setPen(QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(_mic_path())
    p.restore()


class MicButton(QWidget):
    """Round mic button.  Idle = glass disc; active = accent disc with a ring that follows the input level."""
    clicked = Signal()

    def __init__(self, active: bool = False, d: int = 40, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.active = active
        self.d = d
        self.level = 0.0
        self.setFixedSize(d + 12, d + 12)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_active(self, on: bool) -> None:
        self.active = bool(on)
        if not on:
            self.level = 0.0
        self.update()

    def set_level(self, v: float) -> None:
        self.level = max(0.0, min(1.0, float(v)))
        if self.active:
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        c = QPointF(self.width() / 2, self.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        if self.active:
            acc = T.color.accent.resolve(dark)
            ring = self.d / 2 + 3 + 3 * self.level
            p.setBrush(QColor(acc.red(), acc.green(), acc.blue(), 50))
            p.drawEllipse(c, ring, ring)
            p.setBrush(acc)
            p.drawEllipse(c, self.d / 2, self.d / 2)
            _draw_mic(p, c.x(), c.y(), 19, QColor("#FFFFFF"), 1.8)
        else:
            p.setBrush(QColor(120, 120, 128, 50 if dark else 40))
            p.drawEllipse(c, self.d / 2, self.d / 2)
            _draw_mic(p, c.x(), c.y(), 19, T.color.label.resolve(dark), 1.6)


class Orb(QWidget):
    """The island's Siri orb, painted with the island's own painter (animated while visible)."""

    def __init__(self, scale: float = 3.2, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.k = scale
        self._t = 1.1
        self._t0 = time.monotonic() - 1.1
        self.setFixedSize(int(60 * scale), int(60 * scale))
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._t = time.monotonic() - self._t0
        self.update()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not T.reduce_motion():
            self._timer.start()

    def hideEvent(self, e) -> None:
        self._timer.stop()
        super().hideEvent(e)

    def paintEvent(self, e) -> None:
        try:
            from ui.island import AppleDynamicIsland
            painter = AppleDynamicIsland._draw_siri_orb
        except Exception:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(self.k, self.k)
        try:
            painter(None, p, 0.0, 0.0, self._t, 1.0)          # the painter never touches self
        except Exception:
            pass


class Chip(QWidget):
    """Pill chip: glass when off, accent when on.  Click toggles; `toggled(bool)` fires."""
    toggled = Signal(bool)

    def __init__(self, text: str, selected: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.text = text
        self.sel = selected
        f = qfont(TYPE.callout)
        f.setWeight(QFont.Weight.DemiBold)
        self.f = f
        self.setFixedSize(QFontMetrics(f).horizontalAdvance(text) + 2 * SPACING.md + 6, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, on: bool) -> None:
        self.sel = bool(on)
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.sel = not self.sel
            self.update()
            self.toggled.emit(self.sel)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = T.is_dark()
        r = self.height() / 2
        rect = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.PenStyle.NoPen)
        if self.sel:
            p.setBrush(glass.solid_fill_for_white(QColor(T.color.accent.resolve(dark))))
            tc = QColor("#FFFFFF")
        else:
            p.setBrush(QColor(120, 120, 128, 40 if dark else 30))
            tc = T.color.label.resolve(dark)
        p.drawRoundedRect(rect, r, r)
        p.setPen(tc)
        p.setFont(self.f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text)


def _flow_layout():
    from ui.settings_window import _Flow
    return _Flow(hspace=8, vspace=8)


# ──────────────────────────────────────────────────────────────────────────────
# Window
# ──────────────────────────────────────────────────────────────────────────────

class OnboardingWindow(QDialog):
    """Name, address, voice sample, usage, privacy.  Skippable; closing it counts as skipping."""

    _sig_level = Signal(float)
    _sig_answer = Signal(str, int, str)          # which, token, text
    _sig_sample = Signal(int, int, bool, str)    # token, index, ok, error
    _sig_apps = Signal(list)

    def __init__(self, ctx=None, cfg=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.cfg = cfg
        self._dark = T.is_dark()
        self._idx = 0
        self._finished = False
        self.completed = False
        self.was_skipped = False
        self._token = 0                      # bumped to invalidate in-flight voice work
        self._busy: Optional[str] = None     # "name" | "apps" | "voice" while a worker is running
        self._threads: List[threading.Thread] = []

        # collected answers
        self.name = ""
        self.address = "sir"
        self.uses: List[str] = []
        self.favorites: List[str] = []
        self._suggestions: List[str] = []
        self._apps_scanned = False
        self._sample_done = 0
        self._sample_ok = False

        self.setWindowTitle("Welcome to J.A.R.V.I.S.")
        self.setFixedSize(580, 520)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.CustomizeWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._apply_style()

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACING.xl, SPACING.lg, SPACING.xl, SPACING.lg)
        root.setSpacing(SPACING.md)

        self._step_bar = _StepProgressIndicator(total_steps=_QUESTION_PAGES, current_step=0)
        root.addWidget(self._step_bar)

        self._stack = QStackedWidget(self)
        root.addWidget(self._stack, stretch=1)
        self._build_hello()
        self._build_name()
        self._build_address()
        self._build_voice()
        self._build_usage()
        self._build_privacy()
        self._build_done()

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(SPACING.sm)
        self._btn_back = PlainButton("Back")
        self._btn_back.setFixedWidth(80)
        self._btn_back.clicked.connect(self._prev)
        self._btn_skip = PlainButton("Skip")
        self._btn_skip.setFixedWidth(80)
        self._btn_skip.clicked.connect(self.skip)
        self._btn_next = PrimaryButton("Let’s begin")
        self._btn_next.clicked.connect(self._next)
        nav.addWidget(self._btn_back)
        nav.addStretch()
        nav.addWidget(self._btn_skip)
        nav.addWidget(self._btn_next, stretch=1)
        root.addLayout(nav)

        self._sig_level.connect(self._on_level)
        self._sig_answer.connect(self._on_answer)
        self._sig_sample.connect(self._on_sample)
        self._sig_apps.connect(self._on_apps)

        self._go(0, speak=False)

    # ── look ────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        dark = self._dark
        label_c = T.color.label.resolve(dark).name()
        fill_c = T.color.elevated_background.resolve(dark).name()
        accent_c = T.color.accent.resolve(dark).name()
        self.setStyleSheet(f"""
            QDialog {{ background: transparent; color: {label_c}; }}
            QWidget {{ color: {label_c}; font-family: "Inter", "Segoe UI Variable", "Segoe UI", sans-serif; }}
            QLabel {{ background: transparent; color: {label_c}; }}
            QLineEdit {{
                background: {fill_c}; border: 1px solid rgba(128,128,128,0.2);
                border-radius: 9px; color: {label_c}; padding: 8px 12px; font-size: 13px;
                selection-background-color: {accent_c};
            }}
            QLineEdit:focus {{ border: 1.5px solid {accent_c}; }}
        """)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "win32":
            hwnd = int(self.winId())
            self._backdrop_type = apply_best_backdrop(hwnd, self._dark, prefer_mica=True)
            set_rounded_corners(hwnd)
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        dark = T.is_dark()
        bg = T.color.grouped_background.resolve(dark)
        if getattr(self, "_backdrop_type", None) == "mica":
            bg = QColor(bg.red(), bg.green(), bg.blue(), 220)
        p.fillRect(self.rect(), bg)

    # ── small builders ──────────────────────────────────────────────────

    def _sec_label(self, text: str, size: int = 11) -> QLabel:
        l = QLabel(text)
        l.setWordWrap(True)
        l.setStyleSheet(f"color: {T.color.secondary_label.resolve(self._dark).name()}; font-size: {size}px;")
        return l

    def _page(self, top: int = 0) -> tuple:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, top, 0, 0)
        v.setSpacing(SPACING.md)
        self._stack.addWidget(page)
        return page, v

    def _header(self, layout: QVBoxLayout, title: str, desc: str) -> None:
        v = QVBoxLayout()
        v.setSpacing(4)
        t = QLabel(title)
        t.setFont(qfont(TYPE.title))
        d = QLabel(desc)
        d.setFont(qfont(TYPE.callout))
        d.setWordWrap(True)
        d.setStyleSheet(f"color: {T.color.secondary_label.resolve(self._dark).name()};")
        v.addWidget(t)
        v.addWidget(d)
        layout.addLayout(v)

    def _field_with_mic(self, placeholder: str, on_mic: Callable) -> tuple:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        e = QLineEdit()
        e.setPlaceholderText(placeholder)
        e.setMinimumHeight(44)
        mic = MicButton(False)
        mic.clicked.connect(on_mic)
        h.addWidget(e, 1)
        h.addWidget(mic)
        if not self._voice_ok():
            mic.setVisible(False)
        return row, e, mic

    @staticmethod
    def _row_labels(ctrl: QWidget) -> list:
        row = ctrl.parentWidget()
        return row.findChildren(QLabel) if row is not None else []

    # ── pages ───────────────────────────────────────────────────────────

    def _build_hello(self) -> None:
        page, v = self._page(24)
        v.setSpacing(6)
        v.addWidget(Orb(2.6), 0, Qt.AlignmentFlag.AlignHCenter)
        t = QLabel("Hello, I’m J.A.R.V.I.S.")
        t.setFont(qfont(TYPE.large_title))
        t.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(t)
        d = self._sec_label("I’ll ask a few quick questions so I can work the way you do. "
                            "It takes about a minute, and you can skip any of it.", 13)
        d.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(d)
        v.addStretch()

    def _build_name(self) -> None:
        page, v = self._page()
        self._header(v, "What should I call you?", "Say it or type it. I’ll remember it for next time.")
        self.name_card = GroupedCard()
        row, self.name_edit, self.name_mic = self._field_with_mic("Your name", lambda: self._listen("name"))
        self.name_edit.returnPressed.connect(self._next)
        self.name_card.add_widget(row, divider=False)
        self._name_hint = ("Tap the mic to answer by voice. Press Enter to continue."
                           if self._voice_ok() else "Type your name and press Enter to continue.")
        self.name_card.set_footnote(self._name_hint)
        v.addWidget(self.name_card)
        v.addStretch()

    def _build_address(self) -> None:
        page, v = self._page()
        self._header(v, "How should I address you?", "This is how I’ll greet you and reply.")
        card = GroupedCard()
        self.address_seg = SegmentedControl([("sir", "Sir"), ("maam", "Ma’am"), ("name", "By name")], "sir")
        self.address_seg.valueChanged.connect(self._on_address)
        card.add_row("Address me as", "Pick one, change it later in Settings", self.address_seg)
        self.address_preview = QLabel("")
        self.address_preview.setFont(qfont(TYPE.body))
        self.address_preview.setWordWrap(True)
        play = PlainButton("Hear it")
        play.setFixedWidth(84)
        play.clicked.connect(self._hear_preview)
        self.btn_hear = play
        card.add_row("Preview", "How I’ll sound", play)
        card.add_widget(self.address_preview)
        card.set_footnote("“By name” uses the name you gave me.")
        v.addWidget(card)
        v.addStretch()
        self._refresh_preview()

    def _build_voice(self) -> None:
        page, v = self._page()
        self._header(v, "Let me learn your voice",
                     "Read this sentence aloud so I can tune to your voice and accent.")
        card = GroupedCard()
        self.sample_label = QLabel(f"“{VOICE_SAMPLES[0]}”")
        self.sample_label.setWordWrap(True)
        self.sample_label.setFont(qfont(TYPE.title_3))
        self.sample_label.setContentsMargins(0, 12, 0, 12)
        card.add_widget(self.sample_label, divider=False)
        self.voice_meter = MicLevelMeter()
        self.voice_meter.setMinimumWidth(190)
        card.add_row("Input level", "Aim for the green zone", self.voice_meter)
        self.sample_progress = _StepProgressIndicator(total_steps=len(VOICE_SAMPLES), current_step=-1)
        self.sample_progress.setFixedWidth(120)
        card.add_row(f"Sample 1 of {len(VOICE_SAMPLES)}", "Tap the mic and read the sentence", self.sample_progress)
        self._sample_title_lbl, self._sample_desc_lbl = (self._row_labels(self.sample_progress) + [None, None])[:2]
        card.set_footnote("Used only to tune recognition to you. Stays on this PC.")
        self.voice_card = card
        v.addWidget(card)
        row = QHBoxLayout()
        row.addStretch()
        self.voice_mic = MicButton(False, 46)
        self.voice_mic.clicked.connect(self._start_samples)
        row.addWidget(self.voice_mic)
        row.addStretch()
        v.addLayout(row)
        if not self._voice_ok():
            self.voice_mic.setVisible(False)
            card.set_footnote("Voice isn’t available right now, so you can skip this step.")
        v.addStretch()

    def _build_usage(self) -> None:
        page, v = self._page()
        self._header(v, "What do you mostly use this PC for?",
                     "Pick any that fit. This helps me suggest the right things.")
        card = GroupedCard()
        uses = QWidget()
        hl = QHBoxLayout(uses)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        self.use_chips = {}
        for name in USES:
            c = Chip(name, False)
            c.toggled.connect(lambda on, n=name: self._on_use(n, on))
            self.use_chips[name] = c
            hl.addWidget(c)
        hl.addStretch()
        card.add_widget(uses, divider=False)
        card.add_row("Games and apps you use most", "Type or say a few names", QWidget())
        row, self.apps_edit, self.apps_mic = self._field_with_mic("Elden Ring, Chrome, Premiere…",
                                                                  lambda: self._listen("apps"))
        self.apps_edit.returnPressed.connect(self._add_typed_apps)
        card.add_widget(row)
        self.chip_host = QWidget()
        self.chip_flow = _flow_layout()
        self.chip_host.setLayout(self.chip_flow)
        card.add_widget(self.chip_host)
        self.usage_card = card
        self._set_usage_footnote("Looking for apps on this PC…")
        v.addWidget(card)
        v.addStretch()

    def _build_privacy(self) -> None:
        page, v = self._page()
        self._header(v, "Your data stays with you",
                     "Everything I learn stays on this PC. The one exception is a question I can’t "
                     "answer locally: it is sent to Groq (cloud) so it can be answered.")
        card = GroupedCard()
        for title, desc in (("Name and how to address you", "Used for greetings and replies"),
                            ("Voice sample", "Tunes recognition to your voice"),
                            ("Apps and habits", "Used for suggestions only")):
            card.add_row(title, desc, StatusPill("On this PC", "success"))
        card.add_row("Questions I can’t answer here", "Sent to Groq (cloud) to be answered",
                     StatusPill("Groq cloud", "warning"))
        card.set_footnote("Settings › Privacy & Learning shows everything I know and lets you erase it any time.")
        v.addWidget(card)
        v.addStretch()

    def _build_done(self) -> None:
        page, v = self._page(18)
        v.setSpacing(6)
        v.addWidget(Orb(2.2), 0, Qt.AlignmentFlag.AlignHCenter)
        self.done_title = QLabel("You’re all set.")
        self.done_title.setFont(qfont(TYPE.large_title))
        self.done_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.done_title)
        card = GroupedCard()
        card.add_row("Say “Jarvis”", "Then speak your request", QWidget())
        card.add_row("Hover the island", "Scroll to Type or Talk and write to me", QWidget())
        card.add_row("Change anything", "Tray icon › Settings", QWidget())
        v.addWidget(card)
        v.addStretch()

    # ── navigation ──────────────────────────────────────────────────────

    def page_id(self) -> str:
        return PAGES[self._idx]

    def _go(self, idx: int, speak: bool = True) -> None:
        self._idx = max(0, min(len(PAGES) - 1, idx))
        pid = PAGES[self._idx]
        self._stack.setCurrentIndex(self._idx)
        in_questions = 1 <= self._idx <= _QUESTION_PAGES
        self._step_bar.setVisible(in_questions)
        if in_questions:
            self._step_bar.set_step(self._idx - 1)
        self._btn_back.setVisible(2 <= self._idx <= _QUESTION_PAGES)
        self._btn_skip.setVisible(pid != "done")
        self._btn_next.setText({"hello": "Let’s begin", "privacy": "Looks good",
                                "done": "Start using Jarvis"}.get(pid, "Continue"))
        if pid == "address":
            self._refresh_preview()
        elif pid == "usage":
            self._ensure_app_scan()
        elif pid == "done":
            nm = self.name.strip()
            self.done_title.setText(f"You’re all set, {nm}." if nm else "You’re all set.")
        if speak:
            self._speak(_SPOKEN.get(pid, ""))

    def _next(self) -> None:
        pid = PAGES[self._idx]
        if self._busy and pid in ("name", "usage", "voice"):
            return                                    # let the voice answer land first
        self._collect(pid)
        if pid == "done":
            self.finish()
        else:
            self._go(self._idx + 1)

    def _prev(self) -> None:
        if self._idx > 1 and not self._busy:
            self._collect(PAGES[self._idx])
            self._go(self._idx - 1)

    def skip(self) -> None:
        """Skip the rest.  Whatever was already answered is kept."""
        self._collect(PAGES[self._idx])
        self.finish(skipped=True)

    def _collect(self, pid: str) -> None:
        if pid == "name":
            self.name = self.name_edit.text().strip()
        elif pid == "address":
            self.address = self.address_seg.currentValue()
        elif pid == "usage":
            self._add_typed_apps()
        self._save_profile()

    # ── saving ──────────────────────────────────────────────────────────

    def _profile_fields(self) -> dict:
        f = {}
        if self.name:
            f["name"] = self.name
        if self._idx >= 2 or self.address != "sir":
            f["address_as"] = {"maam": "ma'am"}.get(self.address, self.address)
        if self.uses:
            f["uses"] = list(self.uses)
        if self.favorites:
            f["favorite_apps"] = list(self.favorites)
        return f

    def _save_profile(self) -> None:
        pers = getattr(self.ctx, "personal", None) if self.ctx is not None else None
        fields = self._profile_fields()
        if pers is None or not fields:
            return
        try:
            pers.update_profile(**fields)
        except Exception as exc:
            print(f"[onboarding] update_profile failed: {exc}")

    def _save_cfg(self, key: str, value) -> None:
        """Same persistence path as SettingsWindow._save: set on cfg, atomic single-key write, reload hook."""
        cfg = self.cfg
        update_key(key, value, cfg, get_config_path())
        try:
            fn = getattr(self.ctx, "reload_config", None)
            if callable(fn) and cfg is not None:
                fn(cfg)
        except Exception as exc:
            print(f"[onboarding] reload_config error: {exc}")

    def finish(self, skipped: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        self._token += 1                              # drop any in-flight voice result
        self._save_profile()
        self._save_cfg("onboarding_done", True)
        self.completed = True
        self.was_skipped = skipped
        QDialog.accept(self)

    def reject(self) -> None:
        if not self._finished:
            self.finish(skipped=True)
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if not self._finished:
            self._token += 1
            self.finish(skipped=True)
        super().closeEvent(event)

    # ── speech out ──────────────────────────────────────────────────────

    def _speak(self, text: str) -> None:
        if not text or self.ctx is None:
            return
        if self.cfg is not None and getattr(self.cfg, "speak", True) is False:
            return
        fn = getattr(self.ctx, "say", None)
        if callable(fn):
            try:
                fn(text)
            except Exception as exc:
                print(f"[onboarding] say failed: {exc}")

    # ── voice in ────────────────────────────────────────────────────────

    def _voice(self):
        return getattr(self.ctx, "voice", None) if self.ctx is not None else None

    def _voice_ok(self) -> bool:
        v = self._voice()
        return v is not None and callable(getattr(v, "capture_answer", None))

    def _level_cb(self, token: int) -> Callable:
        def _cb(level=0.0, *_a):
            if token == self._token:
                try:
                    self._sig_level.emit(float(level))
                except Exception:
                    pass
        return _cb

    def _spawn(self, target: Callable, name: str) -> threading.Thread:
        t = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def _listen(self, which: str) -> None:
        if self._busy or not self._voice_ok():
            return
        voice = self._voice()
        self._token += 1
        token = self._token
        self._busy = which
        mic = self.name_mic if which == "name" else self.apps_mic
        mic.set_active(True)
        if which == "name":
            self.name_card.set_footnote("Listening… just say your name.")
        else:
            self._set_usage_footnote("Listening… say a few names.")
        cb = self._level_cb(token)

        def _work():
            text = ""
            try:
                text = voice.capture_answer(timeout=8.0, on_level=cb) or ""
            except Exception as exc:
                print(f"[onboarding] capture_answer failed: {exc}")
            self._sig_answer.emit(which, token, str(text))

        self._spawn(_work, "onboarding-listen")

    def _on_level(self, level: float) -> None:
        if self._busy == "voice":
            self.voice_meter.update_level(level)
            self.voice_mic.set_level(level)
        elif self._busy == "name":
            self.name_mic.set_level(level)
        elif self._busy == "apps":
            self.apps_mic.set_level(level)

    def _on_answer(self, which: str, token: int, text: str) -> None:
        if token != self._token or self._finished:
            return
        self._busy = None
        text = (text or "").strip()
        if which == "name":
            self.name_mic.set_active(False)
            cleaned = self._clean_name(text)
            if cleaned:
                self.name_edit.setText(cleaned)
                self.name_card.set_footnote(f"Got it: {cleaned}. Press Continue, or type to change it.")
            else:
                self.name_card.set_footnote("I didn’t catch that. Tap the mic to try again, or type it.")
        else:
            self.apps_mic.set_active(False)
            names = self._split_names(text)
            for n in names:
                self._add_favorite(n)
            self._set_usage_footnote(f"Added {', '.join(names)}." if names else
                                     "I didn’t catch that. Tap the mic to try again, or type it.")

    @staticmethod
    def _clean_name(text: str) -> str:
        t = re.sub(r"^\s*(?:hi|hello|hey)?[\s,]*(?:my name is|my name’s|my name's|i am|i’m|i'm|call me|it is|it’s|it's|this is)\s+",
                   "", text, flags=re.I)
        t = t.strip(" .,!?\"'")
        return " ".join(w[:1].upper() + w[1:] for w in t.split()) if t else ""

    @staticmethod
    def _split_names(text: str) -> List[str]:
        t = re.sub(r"^\s*(?:i (?:mostly )?(?:use|play)|mostly|it’s|it's)\s+", "", text, flags=re.I)
        parts = re.split(r",|\band\b|;", t, flags=re.I)
        out = []
        for p in parts:
            p = p.strip(" .!?\"'")
            if p and p.lower() not in [o.lower() for o in out]:
                out.append(p)
        return out

    # ── address page ────────────────────────────────────────────────────

    def _on_address(self, value: str) -> None:
        self.address = value
        self._refresh_preview()

    def _preview_text(self) -> str:
        h = time.localtime().tm_hour
        greet = "Good morning" if h < 12 else ("Good afternoon" if h < 18 else "Good evening")
        who = {"sir": "sir", "maam": "ma’am"}.get(self.address)
        if who is None:
            who = self.name_edit.text().strip() or self.name or "sir"
        return f"{greet}, {who}. All systems are online."

    def _refresh_preview(self) -> None:
        self.address_preview.setText(f"“{self._preview_text()}”")

    def _hear_preview(self) -> None:
        self._speak(self._preview_text())

    # ── voice sample page ───────────────────────────────────────────────

    def _start_samples(self) -> None:
        if self._busy or not self._voice_ok():
            return
        voice = self._voice()
        calibrate = getattr(voice, "calibrate_from_sample", None)
        if not callable(calibrate):
            self.voice_card.set_footnote("Voice tuning isn’t available right now, so you can skip this step.")
            return
        self._token += 1
        token = self._token
        self._busy = "voice"
        self.voice_mic.set_active(True)
        start = 0 if self._sample_done >= len(VOICE_SAMPLES) else self._sample_done
        self._sample_done = start
        cb = self._level_cb(token)

        def _work():
            for i in range(start, len(VOICE_SAMPLES)):
                if token != self._token:
                    return
                ok, err = False, ""
                try:
                    res = calibrate(VOICE_SAMPLES[i], timeout=12.0, on_level=cb)
                    if isinstance(res, dict):
                        ok = bool(res.get("ok", res.get("success", True)))
                        err = str(res.get("error", "") or "")
                    else:
                        ok = bool(res)
                except Exception as exc:
                    err = str(exc)
                self._sig_sample.emit(token, i, ok, err)
                if not ok:
                    return

        self._show_sample(start, reading=True)
        self._spawn(_work, "onboarding-calibrate")

    def _show_sample(self, i: int, reading: bool) -> None:
        n = len(VOICE_SAMPLES)
        i = max(0, min(n - 1, i))
        self.sample_label.setText(f"“{VOICE_SAMPLES[i]}”")
        if self._sample_title_lbl is not None:
            self._sample_title_lbl.setText(f"Sample {i + 1} of {n}")
        if self._sample_desc_lbl is not None:
            self._sample_desc_lbl.setText("Reading… speak naturally" if reading
                                          else "Tap the mic and read the sentence")
        self.sample_progress.set_step(self._sample_done - 1)

    def _on_sample(self, token: int, i: int, ok: bool, err: str) -> None:
        if token != self._token or self._finished:
            return
        n = len(VOICE_SAMPLES)
        if ok:
            self._sample_done = i + 1
            self._sample_ok = True
            if self._sample_done >= n:
                self._busy = None
                self.voice_mic.set_active(False)
                self.voice_meter.update_level(0.0)
                self.sample_progress.set_step(n - 1)
                if self._sample_title_lbl is not None:
                    self._sample_title_lbl.setText(f"Sample {n} of {n}")
                if self._sample_desc_lbl is not None:
                    self._sample_desc_lbl.setText("All done. I’ve tuned to your voice")
                self.voice_card.set_footnote("Used only to tune recognition to you. Stays on this PC.")
            else:
                self._show_sample(self._sample_done, reading=True)
        else:
            self._busy = None
            self.voice_mic.set_active(False)
            self.voice_meter.update_level(0.0)
            self._show_sample(self._sample_done, reading=False)
            self.voice_card.set_footnote("I couldn’t hear that clearly. Tap the mic to try again, or continue.")

    # ── usage page ──────────────────────────────────────────────────────

    def _set_usage_footnote(self, text: str) -> None:
        self.usage_card.set_footnote(text)

    def _on_use(self, name: str, on: bool) -> None:
        if on and name not in self.uses:
            self.uses.append(name)
        elif not on and name in self.uses:
            self.uses.remove(name)

    def _ensure_app_scan(self) -> None:
        if self._apps_scanned:
            return
        self._apps_scanned = True
        pers = getattr(self.ctx, "personal", None) if self.ctx is not None else None
        fn = getattr(pers, "installed_apps", None)
        if not callable(fn):
            self._set_usage_footnote("Type or say a few names.")
            return

        def _work():
            names: list = []
            try:
                for it in (fn(40) or []):
                    if isinstance(it, dict):
                        it = it.get("name") or it.get("title") or ""
                    elif isinstance(it, (list, tuple)):
                        it = it[0] if it else ""
                    it = str(it).strip()
                    if it:
                        names.append(it)
            except Exception as exc:
                print(f"[onboarding] installed_apps failed: {exc}")
            self._sig_apps.emit(names)

        self._spawn(_work, "onboarding-apps")

    def _on_apps(self, names: list) -> None:
        if self._finished:
            return
        seen, out = set(), []
        for n in names:
            if n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        self._suggestions = out[:MAX_SUGGESTIONS]
        self._rebuild_chips()
        if self._suggestions:
            self._set_usage_footnote("Found on this PC. Tap to add.")
        else:
            self._set_usage_footnote("Type or say a few names.")

    def _add_typed_apps(self) -> None:
        text = self.apps_edit.text().strip() if hasattr(self, "apps_edit") else ""
        if not text:
            return
        for n in [p.strip() for p in text.split(",") if p.strip()]:
            self._add_favorite(n)
        self.apps_edit.clear()

    def _add_favorite(self, name: str) -> None:
        name = name.strip()
        if name and name.lower() not in [f.lower() for f in self.favorites]:
            self.favorites.append(name)
            self._rebuild_chips()

    def _toggle_favorite(self, name: str, on: bool) -> None:
        if on:
            self._add_favorite(name)
        else:
            self.favorites = [f for f in self.favorites if f.lower() != name.lower()]
            self._rebuild_chips()

    def _rebuild_chips(self) -> None:
        lay = self.chip_flow
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for fav in self.favorites:
            c = Chip(f"{fav} ×", True)
            c.toggled.connect(lambda on, n=fav: self._toggle_favorite(n, False))
            lay.addWidget(c)
        have = {f.lower() for f in self.favorites}
        for s in self._suggestions:
            if s.lower() in have:
                continue
            c = Chip(s, False)
            c.toggled.connect(lambda on, n=s: self._toggle_favorite(n, True))
            lay.addWidget(c)
        self.chip_host.updateGeometry()
        self.chip_host.setVisible(lay.count() > 0)

    # ── test / external helpers ─────────────────────────────────────────

    def join_workers(self, timeout: float = 5.0) -> None:
        for t in list(self._threads):
            t.join(timeout)
