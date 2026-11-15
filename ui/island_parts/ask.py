"""ui/island_parts/ask.py - "Type or Talk" page: a text field + mic / send button on the existing 390 x 188 card.

* Field: "Ask or tell Jarvis..."; Enter sends (ctx.submit_text), Esc clears / leaves, Ctrl+V pastes.
* Mic button: ctx.listen_now(); becomes the accent Send button as soon as the field has text.
* Listening reuses the island's orb / waveform painters; the reply (say / result events) shows under the field.
* Focus: the island is WS_EX_NOACTIVATE + click-through. While this page is open and the field is clicked (or the page
  is opened with focus=True from the hotkey) the window is made activatable, takes keyboard focus, and on leave /
  Esc / send / timeout NOACTIVATE + click-through are restored and the previously foreground window gets focus back.
  Never grabs focus while a fullscreen app is in front.

All state lives on AppleDynamicIsland; this module holds the painter, the key handling and `FocusController`.
"""

from __future__ import annotations

import math
import sys
import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen

from ui.island_parts.common import IslandState, draw_text, fill_round, paint_icon, with_alpha
from ui.tokens import ISLAND, TYPE, TypeToken, qfont

_C = ISLAND.color
T_FIELD = TypeToken(TYPE.weather_city.family, 14, 400)
T_FIELD_B = TYPE.weather_value
T_HINT = TYPE.tile_label
PLACEHOLDER = "Ask or tell Jarvis…"
MAX_LEN = 400
FOCUS_IDLE_S = 20.0          # focus (and the page) is let go this long after the last keystroke when the field is empty
REPLY_HOLD_S = 6.0           # the page stays open this long after the last reply / listen / send

_FIELD_IDLE = QRectF(24.0, 54.0, 282.0, 46.0)
_MIC_IDLE = (335.0, 77.0, 42.0)
_FIELD_REPLY = QRectF(24.0, 50.0, 282.0, 38.0)
_MIC_REPLY = (335.0, 69.0, 36.0)


# ──────────────────────────────────────────────────────────────────────────────
# Focus controller (Win32 calls go through `api` so tests can mock them)
# ──────────────────────────────────────────────────────────────────────────────

class FocusController:
    """Takes keyboard focus for the island on demand and hands it back to the previous foreground window."""

    def __init__(self, island, api=None):
        self.island = island
        self._api = api
        self.active = False
        self.prev_hwnd = 0

    @property
    def api(self):
        if self._api is not None:
            return self._api
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            return ctypes.windll.user32
        except Exception:
            return None

    def acquire(self) -> bool:
        """Make the island window activatable and foreground. False (nothing changed) when blocked."""
        if self.active:
            return True
        api = self.api
        isl = self.island
        if api is None:
            return False
        try:
            if isl._focus_blocked():
                return False
            hwnd = int(isl.winId())
            fg = int(api.GetForegroundWindow() or 0)
            self.prev_hwnd = fg if fg and fg != hwnd else 0
            self.active = True
            isl._set_focusable(True)
            api.SetForegroundWindow(hwnd)
            if int(api.GetForegroundWindow() or 0) != hwnd:
                self._force_foreground(api, hwnd, fg)
            isl.activateWindow()
            isl.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        except Exception as exc:
            print(f"[island] ask focus error: {exc}")
            self.active = False
            try:
                isl._set_focusable(False)
            except Exception:
                pass
            return False

    @staticmethod
    def _force_foreground(api, hwnd: int, fg: int) -> None:
        """AttachThreadInput dance for when Windows refuses a plain SetForegroundWindow."""
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            cur = kernel32.GetCurrentThreadId()
            fg_tid = api.GetWindowThreadProcessId(fg, None) if fg else 0
            if fg_tid and fg_tid != cur:
                api.AttachThreadInput(cur, fg_tid, True)
                try:
                    api.SetForegroundWindow(hwnd)
                finally:
                    api.AttachThreadInput(cur, fg_tid, False)
            else:
                api.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def release(self, restore: bool = True) -> None:
        """Back to NOACTIVATE + click-through; optionally re-focus the window that had focus before."""
        if not self.active:
            return
        self.active = False
        prev, self.prev_hwnd = self.prev_hwnd, 0
        isl = self.island
        try:
            isl._set_focusable(False)
        except Exception as exc:
            print(f"[island] ask focus restore error: {exc}")
        api = self.api
        if restore and prev and api is not None:
            try:
                own = int(isl.winId())
                if prev != own and api.IsWindow(prev):
                    api.SetForegroundWindow(prev)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Painting helpers (1x logical px, card origin)
# ──────────────────────────────────────────────────────────────────────────────

def _mic_path() -> QPainterPath:
    s = QPainterPath()
    s.addRoundedRect(QRectF(5.9, 1.4, 4.2, 7.6), 2.1, 2.1)
    s.moveTo(3.3, 8.1)
    s.arcTo(QRectF(3.3, 3.6, 9.4, 9.0), 180.0, 180.0)
    s.moveTo(8.0, 12.6)
    s.lineTo(8.0, 14.6)
    s.moveTo(5.6, 14.6)
    s.lineTo(10.4, 14.6)
    return s


_MIC_PATH = None


def draw_mic(p: QPainter, cx: float, cy: float, size: float, color: QColor, stroke: float = 1.5) -> None:
    global _MIC_PATH
    if _MIC_PATH is None:
        _MIC_PATH = _mic_path()
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(cx, cy)
    k = size / 16.0
    p.scale(k, k)
    p.translate(-8.0, -8.0)
    p.setPen(QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(_MIC_PATH)
    p.restore()


def _wrap(text: str, token, width: float, max_lines: int) -> list:
    fm = QFontMetricsF(qfont(token))
    lines, cur = [], ""
    for word in text.split():
        t = (cur + " " + word).strip()
        if fm.horizontalAdvance(t) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)]
    return lines


def _key_chip(p: QPainter, x: float, baseline: float, label: str, tail: str) -> float:
    font = qfont(T_HINT)
    fm = QFontMetricsF(font)
    tw = fm.horizontalAdvance(label)
    rect = QRectF(x, baseline - 12.5, tw + 12.0, 17.0)
    fill_round(p, rect, 5.0, _C.tile_fill)
    draw_text(p, label, x + 6.0, baseline, T_HINT, _C.secondary)
    adv = draw_text(p, tail, rect.right() + 6.0, baseline, T_HINT, _C.label)
    return rect.right() + 6.0 + adv


class AskPageMixin:
    """Type-or-Talk page. Methods only; state lives on the AppleDynamicIsland instance (see _ask_init)."""

    def _ask_init(self) -> None:
        self.ask_text: str = ""
        self.ask_cursor: int = 0
        self.ask_sent_text: str = ""
        self.ask_reply: str = ""
        self._ask_focus = FocusController(self)
        self._ask_input_t: float = 0.0
        self._ask_activity_t: float = 0.0          # last send / listen / reply: keeps the page open for REPLY_HOLD_S
        self._ask_listen_until: float = 0.0        # optimistic "listening" after the mic press
        self._ask_wait_until: float = 0.0          # waiting for the reply after a send
        self._ask_seen: tuple = ("", 0.0)
        self._ask_focus_active: bool = False       # read by _apply_exstyle (drop NOACTIVATE while True)

    # ── ctx plumbing ──────────────────────────────────────────────────────

    def _ask_ctx_call(self, name: str, *args) -> bool:
        fn = getattr(self._ctx(), name, None)
        if not callable(fn):
            print(f"[island] ask: ctx.{name} unavailable")
            return False
        try:
            fn(*args)
            return True
        except Exception as exc:
            print(f"[island] ask: ctx.{name} failed: {exc}")
            return False

    # ── focus / window plumbing ───────────────────────────────────────────

    def _focus_blocked(self) -> bool:
        """True while a fullscreen app (game) is in front: never take focus from it."""
        if self.is_fullscreen_suppressed:
            return True
        try:
            return bool(self._check_fullscreen_foreground())
        except Exception:
            return False

    def _set_focusable(self, on: bool) -> None:
        """Toggle activatable <-> WS_EX_NOACTIVATE (+ click-through rules) without recreating the window."""
        self._ask_focus_active = bool(on)
        try:
            wh = self.windowHandle()
            if wh is not None:
                wh.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, not on)
        except Exception:
            pass
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, bool(on))
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if on else Qt.FocusPolicy.NoFocus)
        except Exception:
            pass
        self._apply_exstyle()
        self._dirty = True

    def ask_has_focus(self) -> bool:
        return bool(self._ask_focus.active)

    def _ask_open(self) -> bool:
        return self.expanded_card == "ask"

    def open_ask_page(self, focus: bool = False, toggle: bool = False) -> None:
        """GUI thread. Show the page (revealing a hidden island); take keyboard focus when `focus`."""
        if toggle and self._ask_open() and (self._ask_focus.active or not focus):
            self.collapse_card()
            return
        now = time.time()
        if not self._ask_open():
            self._opened_via_hover = False
            self.expand_card("ask")
        self.expanded_until = now + 4.0
        self._ask_input_t = now
        self._ask_activity_t = now
        self._dirty = True
        if focus:
            self._ask_focus.acquire()
        if getattr(self, "hidden", False):
            self._hidden_reveal(now)

    def _ask_on_leave(self, restore: bool = True) -> None:
        """Hook from collapse_card / switching page: drop focus, reset transient reply state."""
        self._ask_focus.release(restore)
        self.ask_text = ""
        self.ask_cursor = 0
        self.ask_reply = ""
        self.ask_sent_text = ""
        self._ask_listen_until = 0.0
        self._ask_wait_until = 0.0
        self._dirty = True

    def ask_escape(self) -> None:
        """Esc: clear the text; with nothing to clear (or while listening) stop listening / leave the page."""
        now = time.time()
        if self._ask_listening(now):
            for name in ("stop_listening", "cancel_listening", "cancel_listen"):
                if callable(getattr(self._ctx(), name, None)):
                    self._ask_ctx_call(name)
                    break
            self.listening = False
            self.listening_until = 0.0
            self._ask_listen_until = 0.0
            return
        if self.ask_text:
            self.ask_text, self.ask_cursor = "", 0
            self._dirty = True
            return
        self.collapse_card()

    def ask_send(self) -> bool:
        text = self.ask_text.strip()
        if not text:
            return False
        now = time.time()
        self._ask_ctx_call("submit_text", text)
        self.ask_sent_text = text
        self.ask_text, self.ask_cursor = "", 0
        self.ask_reply = ""
        self._ask_wait_until = now + 12.0
        self._ask_activity_t = now
        self.expanded_until = max(self.expanded_until, now + REPLY_HOLD_S)
        self._ask_focus.release(True)              # send complete: focus goes back to where the user was
        self._dirty = True
        return True

    def ask_listen(self) -> None:
        now = time.time()
        if self._ask_listening(now):
            self.ask_escape()
            return
        self._ask_ctx_call("listen_now")
        self._ask_listen_until = now + 1.5
        self._ask_activity_t = now
        self.ask_reply = ""
        self._dirty = True

    def ask_mic_action(self) -> None:
        """The round button: Send when there is text, else the mic."""
        if self.ask_text.strip():
            self.ask_send()
        else:
            self.ask_listen()

    # ── editing ───────────────────────────────────────────────────────────

    def _ask_insert(self, s: str) -> None:
        s = "".join(ch if ch >= " " else " " for ch in s.replace("\r", " ").replace("\n", " "))
        if not s:
            return
        room = MAX_LEN - len(self.ask_text)
        if room <= 0:
            return
        s = s[:room]
        self.ask_text = self.ask_text[:self.ask_cursor] + s + self.ask_text[self.ask_cursor:]
        self.ask_cursor += len(s)
        self._ask_typed()

    def _ask_typed(self) -> None:
        now = time.time()
        self._ask_input_t = now
        self.expanded_until = max(self.expanded_until, now + 4.0)
        if self.ask_text:
            self.ask_reply = ""
            self._ask_wait_until = 0.0
        self._dirty = True

    def ask_key_press(self, key: int, text: str, mods) -> bool:
        """Handle a key for the field. True when consumed."""
        K = Qt.Key
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if key in (K.Key_Return, K.Key_Enter):
            self.ask_send()
        elif key == K.Key_Escape:
            self.ask_escape()
        elif key == K.Key_Backspace:
            if ctrl:
                i = self.ask_cursor
                while i > 0 and self.ask_text[i - 1] == " ":
                    i -= 1
                while i > 0 and self.ask_text[i - 1] != " ":
                    i -= 1
                self.ask_text = self.ask_text[:i] + self.ask_text[self.ask_cursor:]
                self.ask_cursor = i
            elif self.ask_cursor > 0:
                self.ask_text = self.ask_text[:self.ask_cursor - 1] + self.ask_text[self.ask_cursor:]
                self.ask_cursor -= 1
            self._ask_typed()
        elif key == K.Key_Delete:
            self.ask_text = self.ask_text[:self.ask_cursor] + self.ask_text[self.ask_cursor + 1:]
            self._ask_typed()
        elif key == K.Key_Left:
            self.ask_cursor = max(0, self.ask_cursor - 1)
            self._ask_typed()
        elif key == K.Key_Right:
            self.ask_cursor = min(len(self.ask_text), self.ask_cursor + 1)
            self._ask_typed()
        elif key == K.Key_Home:
            self.ask_cursor = 0
            self._ask_typed()
        elif key == K.Key_End:
            self.ask_cursor = len(self.ask_text)
            self._ask_typed()
        elif ctrl and key == K.Key_V:
            try:
                self._ask_insert(QGuiApplication.clipboard().text())
            except Exception:
                pass
        elif ctrl and key == K.Key_A:
            self.ask_cursor = len(self.ask_text)
            self._ask_typed()
        elif text and not ctrl and text >= " " and text != "\x7f":
            self._ask_insert(text)
        else:
            return False
        return True

    def keyPressEvent(self, event) -> None:
        if self._ask_open() and self._ask_focus.active:
            if self.ask_key_press(int(event.key()), event.text(), event.modifiers()):
                event.accept()
                self.update()
                return
        super().keyPressEvent(event)

    def inputMethodEvent(self, event) -> None:
        if self._ask_open() and self._ask_focus.active:
            commit = event.commitString()
            if commit:
                self._ask_insert(commit)
                self.update()
            event.accept()
            return
        super().inputMethodEvent(event)

    def changeEvent(self, event) -> None:
        try:
            from PySide6.QtCore import QEvent
            if (event.type() == QEvent.Type.ActivationChange and self._ask_focus.active
                    and not self.isActiveWindow()):
                self._ask_focus.release(False)       # the user clicked elsewhere: nothing to give back
                self._dirty = True
        except Exception:
            pass
        super().changeEvent(event)

    # ── state / frame hook ────────────────────────────────────────────────

    def _ask_listening(self, now: float) -> bool:
        return bool(self.listening or now < self.listening_until or now < self._ask_listen_until)

    def _ask_hold(self, now: float) -> bool:
        """True while the page must not auto-collapse: text typed, focus held (recently used), listening, a reply
        or an answer being awaited."""
        if self.ask_text:
            return True
        if self._ask_listening(now) or now < self._ask_wait_until:
            return True
        if self._ask_focus.active and now - self._ask_input_t < FOCUS_IDLE_S:
            return True
        return now - self._ask_activity_t < REPLY_HOLD_S

    def _ask_tick(self, now: float) -> None:
        """Per frame while the page is open: pick up replies, time focus out."""
        if not self._ask_open():
            return
        cand = None
        if self.say_text and now < self.say_until:
            cand = (self.say_text, self.say_until)
        elif self.quick_answer_active and self.quick_answer_text and now < self.quick_answer_until:
            cand = (self.quick_answer_text, self.quick_answer_until)
        elif self.result_text and now < self.result_until:
            cand = (self.result_text, self.result_until)
        if cand is not None and cand != self._ask_seen and not self.ask_text:
            self._ask_seen = cand
            self.ask_reply = cand[0]
            self._ask_wait_until = 0.0
            self._ask_activity_t = now
        if self._ask_listening(now):
            self._ask_activity_t = now
        if self._ask_focus.active and not self.ask_text and now - self._ask_input_t >= FOCUS_IDLE_S:
            self._ask_focus.release(True)            # idle timeout: give the keyboard back
        if self._ask_hold(now):
            self.expanded_until = max(self.expanded_until, now + 1.0)
            self._opened_via_hover = False

    def _ask_owns_card(self, now: float) -> bool:
        is_pinned = bool(getattr(self, "pin_open", False))
        return self._ask_open() and (is_pinned or now < self.expanded_until)

    # ── geometry (1x card px) ─────────────────────────────────────────────

    def _ask_variant(self, now: float) -> str:
        if self._ask_listening(now):
            return "listen"
        if self.ask_text:
            return "typing"
        if self.ask_reply or now < self._ask_wait_until:
            return "reply"
        return "idle"

    def _ask_geometry(self, variant: str):
        if variant == "reply":
            return _FIELD_REPLY, _MIC_REPLY
        return _FIELD_IDLE, _MIC_IDLE

    def _ask_hit(self, pos) -> Optional[str]:
        """'ask_mic' | 'ask_field' | None for an island-local point."""
        s = self._get_size_scale()
        field, mic = self._ask_geometry(self._ask_variant(time.time()))
        if math.hypot(pos.x() - mic[0] * s, pos.y() - mic[1] * s) <= (mic[2] / 2.0 + 6.0) * s:
            return "ask_mic"
        f = QRectF(field.x() * s, field.y() * s, field.width() * s, field.height() * s)
        if f.contains(pos):
            return "ask_field"
        return None

    def _ask_button_at(self, pos) -> Optional[str]:
        return "ask_mic" if self._ask_hit(pos) == "ask_mic" else None

    def _ask_mouse_press(self, pos) -> bool:
        hit = self._ask_hit(pos)
        if hit == "ask_field":
            self._ask_input_t = time.time()
            self._ask_focus.acquire()
            return True
        if hit == "ask_mic":
            self.ask_mic_action()
            self._btn_flash_until = time.time() + 0.25
            return True
        return False

    def _publish_hit_snapshot(self, w: float, h: float, state) -> None:
        if state != IslandState.CARD_ASK or not self.expanded_card:
            return super()._publish_hit_snapshot(w, h, state)
        ir = self.island_rect()
        ox, oy = float(ir.x()), float(ir.y())
        targets = []
        s = self._get_size_scale()
        field, mic = self._ask_geometry(self._ask_variant(time.time()))
        r = (mic[2] / 2.0 + 6.0) * s
        targets.append(((ox + mic[0] * s - r, oy + mic[1] * s - r, 2 * r, 2 * r), "ask_mic"))
        targets.append(((ox + field.x() * s, oy + field.y() * s, field.width() * s, field.height() * s), "ask_field"))
        dot_x, ys, pitch = self._dots_geometry(w, h)
        half_w = ISLAND.dots.hit_half_w * s
        x0 = dot_x - half_w
        for page, dy in zip(self.card_pages, ys):
            targets.append(((ox + x0, oy + dy - pitch / 2.0, min(2 * half_w, w - x0), pitch), f"dot:{page}"))
        targets.append(((ox, oy, w, h), "card"))
        new = tuple(targets)
        old_gen, old = self._hit_snapshot
        if new != old:
            self._hit_snapshot = (old_gen + 1, new)

    # ── painting ──────────────────────────────────────────────────────────

    def _ask_round_button(self, p: QPainter, cx: float, cy: float, d: float, kind: str,
                          active: bool, t: float, hover: bool) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        on_accent = QColor("#0C0C0C")
        if active:
            pulse = 0.5 + 0.5 * math.sin(t * 5.0)
            p.setBrush(with_alpha(_C.accent, 0.14 + 0.10 * pulse))
            p.drawEllipse(QPointF(cx, cy), d / 2.0 + 5.0, d / 2.0 + 5.0)
            p.setBrush(_C.accent)
            p.drawEllipse(QPointF(cx, cy), d / 2.0, d / 2.0)
            draw_mic(p, cx, cy, 17.0, on_accent, 1.7)
        elif kind == "send":
            p.setBrush(_C.accent)
            p.drawEllipse(QPointF(cx, cy), d / 2.0, d / 2.0)
            paint_icon(p, "arrow_up", cx, cy, 15.0, on_accent, stroke=1.9)
        else:
            fill = QColor(_C.tile_fill)
            if hover:
                fill.setAlpha(min(255, fill.alpha() + 26))
            p.setBrush(fill)
            p.drawEllipse(QPointF(cx, cy), d / 2.0, d / 2.0)
            draw_mic(p, cx, cy, 17.0, _C.value, 1.5)
        if hover and self._btn_dwell_elapsed > 0.05:
            prog = min(1.0, self._btn_dwell_elapsed / 1.0)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_C.accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            rr = d / 2.0 + 3.0
            p.drawArc(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), 90 * 16, -int(prog * 360 * 16))
        p.restore()

    def _ask_field_content(self, p: QPainter, rect: QRectF, variant: str, now: float, t: float) -> None:
        cy = rect.center().y()
        focused = self._ask_focus.active
        if variant == "listen":
            self._draw_siri_orb(p, orb_cx=rect.x() + 24.0, orb_cy=cy, t=t, speed_mult=1.0)
            x = rect.x() + 46.0
            words = self.user_prompt.split() if self.user_prompt else [w for w, _t in self._transcript_words]
            if not words:
                draw_text(p, "Listening…", x, cy + 5.0, T_FIELD, _C.label)
            else:
                adv = 0.0
                for i, wd in enumerate(words):
                    col = _C.value if i < len(words) - 1 else with_alpha(_C.value, 0.55)
                    adv += draw_text(p, wd + " ", x + adv, cy + 5.0, T_FIELD, col)
                    if adv > rect.right() - 12.0 - x:
                        break
            self._draw_waveform_bars(p, rect.right() + 4.0, cy)
            return
        x = rect.x() + 16.0
        avail = rect.width() - 32.0
        if variant == "typing":
            font = qfont(T_FIELD)
            fm = QFontMetricsF(font)
            cur_adv = fm.horizontalAdvance(self.ask_text[:self.ask_cursor])
            off = max(0.0, cur_adv - avail + 2.0)
            p.save()
            p.setClipRect(QRectF(x - 2.0, rect.y(), avail + 4.0, rect.height()))
            draw_text(p, self.ask_text, x - off, cy + 5.0, T_FIELD, _C.value)
            p.restore()
            if focused and int(now * 1.9) % 2 == 0:
                cx = x + cur_adv - off + 1.0
                p.setPen(QPen(_C.accent, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(cx, cy - 9.0), QPointF(cx, cy + 9.0))
        elif variant == "reply":
            draw_text(p, self.ask_sent_text or "", x, cy + 5.0, T_FIELD, _C.secondary, max_w=avail)
        else:
            caret = focused and int(now * 1.9) % 2 == 0
            if focused:
                if caret:
                    p.setPen(QPen(_C.accent, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    p.drawLine(QPointF(x, cy - 9.0), QPointF(x, cy + 9.0))
                draw_text(p, PLACEHOLDER, x + 7.0, cy + 5.0, T_FIELD, _C.label)
            else:
                draw_text(p, PLACEHOLDER, x, cy + 5.0, T_FIELD, _C.label)

    def _draw_ask_card(self, p: QPainter, w: float, h: float, now: float, t: float, dark: bool,
                       primary: QColor, secondary: QColor, accent: QColor) -> None:
        """Expanded Type-or-Talk card (390 x 188, same shell / dots as the sibling pages)."""
        scale = self._get_size_scale()
        variant = self._ask_variant(now)
        field, mic = self._ask_geometry(variant)
        hover = self._hovered_btn == "ask_mic"
        p.save()
        p.scale(scale, scale)
        title = "Listening" if variant == "listen" else "Type or Talk"
        draw_text(p, title, 26.7, 39.1, TYPE.weather_city, _C.label)
        fill_round(p, field, 12.0 if variant != "reply" else 11.0, _C.tile_fill)
        self._ask_field_content(p, field, variant, now, t)
        if variant == "listen":
            self._ask_round_button(p, mic[0], mic[1], mic[2], "mic", True, t, hover)
            draw_text(p, "Tap to stop", mic[0], 116.0, T_HINT, _C.label, align="center")
            draw_text(p, "Speak now. Jarvis is listening without a wake word.", 26.0, 134.0, T_HINT, _C.secondary)
            _key_chip(p, 26.0, 164.0, "Esc", "to cancel")
        elif variant == "typing":
            self._ask_round_button(p, mic[0], mic[1], mic[2], "send", False, t, hover)
            draw_text(p, "Send", mic[0], 116.0, T_HINT, _C.label, align="center")
            x = _key_chip(p, 26.0, 134.0, "Enter", "to send")
            _key_chip(p, x + 14.0, 134.0, "Esc", "to clear")
            draw_text(p, "Runs like a spoken command, no wake word needed", 26.0, 164.0, T_HINT, _C.dim)
        elif variant == "reply":
            self._ask_round_button(p, mic[0], mic[1], mic[2], "mic", False, t, hover)
            if self.ask_reply:
                lines = _wrap(self.ask_reply, T_FIELD_B, 320.0, 3)
                y = 114.0
                for ln in lines:
                    draw_text(p, ln, 26.0, y, T_FIELD_B, _C.value)
                    y += 20.0
                if len(lines) <= 2:
                    draw_text(p, "Start typing to ask again", 26.0, 164.0, T_HINT, _C.dim)
            else:
                dots = "." * (1 + int(now * 2.5) % 3)
                draw_text(p, "Thinking" + dots, 26.0, 114.0, T_FIELD_B, _C.label)
        else:
            self._ask_round_button(p, mic[0], mic[1], mic[2], "mic", False, t, hover)
            draw_text(p, "Tap to listen", mic[0], 116.0, T_HINT, _C.label, align="center")
            x = _key_chip(p, 26.0, 134.0, "Enter", "to send")
            _key_chip(p, x + 14.0, 134.0, "Esc", "to close")
            draw_text(p, "Tap the mic and speak, no need to say “Jarvis”", 26.0, 164.0, T_HINT, _C.dim)
        p.restore()
