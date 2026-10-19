"""ui/island_parts/paging.py - Page model (page_step/show_page), page-dot column, thread-safe hit-test snapshot,
island_action dispatch and visible-page notification. See the ui/island.py docstring for the contract."""

from __future__ import annotations

import math
import time
from typing import Optional
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtCore import QPoint, QPointF, Qt

from ui.tokens import ISLAND


# Voice names, config names and renderer ids -> logical page key (an entry of card_pages).
_PAGE_ALIASES = {
    "media": "media", "music": "media",
    "time": "time", "calendar": "time", "clock": "time",
    "weather": "weather",
    "hardware": "hardware", "hardware_grid": "hardware", "hardware_strip": "hardware",
    "jarvis": "jarvis", "jarvis_card": "jarvis",
}


class PagingMixin:
    """Page model, page-dot column, hit-test snapshot and island_action dispatch.
    Methods only; all state lives on the AppleDynamicIsland instance."""

    # ── page model: page_step / show_page are the ONLY page-change entry points ──

    @staticmethod
    def _logical_page(name) -> Optional[str]:
        return _PAGE_ALIASES.get(str(name or "").strip().lower())

    def _current_logical_page(self) -> Optional[str]:
        return self._logical_page(self.expanded_card) if self.expanded_card else None

    def _default_card(self) -> str:
        """Card to open from the collapsed pill: media while media is present, timer while running, else first enabled page."""
        if self.media_playing or self.media_active or getattr(self, "media_title", ""):
            if "media" in self.card_pages:
                return "media"
        if self._get_current_compact_mode() == "timer" or self.timer_active:
            return "timer"
        if "weather" in self.card_pages:
            return "weather"
        return self.card_pages[0] if self.card_pages else "weather"

    def page_step(self, direction: int = 1) -> Optional[str]:
        """Step to the next/previous enabled page (wraps). From the collapsed pill the first (or last) page opens.
        Returns the logical page shown."""
        pages = list(self.card_pages)
        if not pages:
            return None
        step = -1 if int(direction or 1) < 0 else 1
        cur = self._current_logical_page()
        if cur in pages:
            target = pages[(pages.index(cur) + step) % len(pages)]
        else:
            target = pages[0] if step > 0 else pages[-1]
        self.show_page(target)
        return target

    def show_page(self, name) -> bool:
        """Open the named page (any alias). Returns False if unknown or disabled in island_pages_enabled."""
        logical = self._logical_page(name)
        if logical is None or logical not in self.card_pages:
            return False
        self._opened_via_hover = False
        self.expand_card(logical)
        return True

    def expand(self, page=None) -> None:
        """Expand to `page`, or to the context default; just refreshes the timeout when already expanded."""
        if page is not None and self.show_page(page):
            return
        if self.expanded_card:
            self.expanded_until = time.time() + 4.0
            return
        self._opened_via_hover = False
        self.expand_card(self._default_card())

    def media_seek(self, ratio: float) -> None:
        ratio = min(1.0, max(0.0, float(ratio)))
        self.media_progress = ratio
        self.media_position_sec = ratio * self.media_duration_sec
        target = float(self.media_position_sec)

        def _seek_job():
            try:
                from media_adapter import get_media_adapter
                get_media_adapter().seek(target)
            except Exception:
                pass

        from ui.island_parts.media import run_transport   # seek can block up to 0.8 s: never on the GUI thread
        run_transport(_seek_job)
        self.update()

    def toggle_tracking_paused(self) -> None:
        """JARVIS-page pause toggle: engine standby via ctx.set_tracking_paused (state read back from ctx)."""
        ctx = self._ctx()
        if ctx is not None and hasattr(ctx, "set_tracking_paused") and hasattr(ctx, "is_tracking_paused"):
            ctx.set_tracking_paused(not ctx.is_tracking_paused())
        else:
            self.tracking_paused = not getattr(self, "tracking_paused", False)
        self.update()

    def _tracking_is_paused(self) -> bool:
        ctx = self._ctx()
        if ctx is not None and hasattr(ctx, "is_tracking_paused"):
            try:
                return bool(ctx.is_tracking_paused())
            except Exception:
                pass
        return bool(getattr(self, "tracking_paused", False))

    def _ctx(self):
        return getattr(self.mgr, "ctx", None)

    def _notify_visible_page(self) -> None:
        """Report the visible expanded page (None when collapsed) to ctx.set_visible_page whenever it changes."""
        page = self.expanded_card or None
        if page == self._notified_page:
            return
        self._notified_page = page
        ctx = self._ctx()
        if ctx is not None and hasattr(ctx, "set_visible_page"):
            try:
                ctx.set_visible_page(page)
            except Exception as exc:
                print(f"[island] set_visible_page error: {exc}")

    def perform_action(self, action: str, **args) -> bool:
        """GUI-thread handler for overlay.post("island_action", action=..., **args)."""
        a = str(action or "")
        if a != "collapse":
            self.expanded_until = max(self.expanded_until, time.time() + 4.0)
        if a == "media_prev":
            self.media_prev()
        elif a == "media_toggle":
            self.media_toggle_play()
        elif a == "media_next":
            self.media_next()
        elif a == "page_step":
            self.page_step(int(args.get("direction", 1) or 1))
        elif a == "show_page":
            return self.show_page(args.get("page", "weather"))
        elif a == "expand":
            self.expand(args.get("page"))
        elif a == "collapse":
            self.collapse_card()
        elif a == "button":
            btn = str(args.get("button", args.get("target", "")))
            if btn.startswith("dot:"):
                return self.show_page(btn[4:])
            if btn == "card":
                return True
            self._trigger_button(btn)
            self._btn_flash_until = time.time() + 0.25
        elif a == "seek":
            self.media_seek(float(args.get("ratio", 0.0)))
        else:
            return False
        return True

    # ── thread-safe hit testing ──

    def hit_test(self, gx: float, gy: float) -> Optional[str]:
        """Target id under GLOBAL screen point (gx, gy) or None. Reads only the immutable paint snapshot."""
        snap = self._hit_snapshot  # single atomic attribute read
        for (x, y, w, h), target in snap[1]:
            if x <= gx < x + w and y <= gy < y + h:
                return target
        return None

    def _publish_hit_snapshot(self, w: float, h: float, state) -> None:
        """Called from paint (GUI thread): rebuild the snapshot in global coordinates; bump generation on change."""
        from ui.island_parts.common import IslandState
        targets = []
        card_states = (
            IslandState.CARD_MEDIA, IslandState.CARD_WEATHER, IslandState.CARD_TIME, IslandState.CARD_HARDWARE_GRID,
            IslandState.CARD_HARDWARE_STRIP, IslandState.CARD_JARVIS, IslandState.CARD_TIMER,
        )
        if state in card_states and self.expanded_card:
            ir = self.island_rect()     # global rect of the ISLAND (not of the margin window)
            ox, oy = float(ir.x()), float(ir.y())
            scale = self._get_size_scale()

            def add(x, y, rw, rh, tid):
                targets.append(((ox + x, oy + y, rw, rh), tid))

            if state == IslandState.CARD_MEDIA:
                for _bid, tid, bcx, bcy, hit_r in self._media_button_geometry():
                    add(bcx - hit_r, bcy - hit_r, 2 * hit_r, 2 * hit_r, tid)
            elif state == IslandState.CARD_TIMER:
                cy_btn, bw, bh = 126.0 * scale, 70.0 * scale, 22.0 * scale
                add(w / 2.0 - 44.0 * scale - bw / 2.0, cy_btn - bh / 2.0, bw, bh, "timer_cancel")
                add(w / 2.0 + 44.0 * scale - bw / 2.0, cy_btn - bh / 2.0, bw, bh, "timer_pause_resume")

            if state != IslandState.CARD_TIMER:
                dot_x, ys, pitch = self._dots_geometry(w, h)
                half_w = ISLAND.dots.hit_half_w * scale
                x0 = dot_x - half_w
                for page, dy in zip(self.card_pages, ys):
                    add(x0, dy - pitch / 2.0, min(2 * half_w, w - x0), pitch, f"dot:{page}")
            add(0.0, 0.0, w, h, "card")
        new = tuple(targets)
        old_gen, old = self._hit_snapshot
        if new != old:
            self._hit_snapshot = (old_gen + 1, new)

    # ── page-dot column ──

    def _dots_geometry(self, w: float, h: float):
        """(centre x, [centre y per page], pitch) of the dot column: 5 dia, pitch 8.2, centre 9 from the right
        edge, vertically centred on the card."""
        scale = self._get_size_scale()
        n = len(self.card_pages)
        pitch = ISLAND.dots.pitch * scale
        dot_x = w - ISLAND.dots.right_inset * scale
        start_y = (h - (n - 1) * pitch) / 2.0
        return dot_x, [start_y + i * pitch for i in range(n)], pitch

    def _dot_strengths(self, active_idx: int, now: float, n: int) -> list:
        """Per-dot active strength 0..1 with a smooth (smoothstep) hand-over from the previous active dot."""
        st = getattr(self, "_dots_anim", None)
        if st is None or st["n"] != n:
            st = {"n": n, "target": active_idx, "prev": active_idx, "t0": -1e9}
            self._dots_anim = st
        if active_idx != st["target"]:
            st["prev"], st["target"], st["t0"] = st["target"], active_idx, now
        dur = ISLAND.dots.anim_s
        k = 1.0 if (self._reduce_motion() or dur <= 0.0) else min(1.0, max(0.0, (now - st["t0"]) / dur))
        e = k * k * (3.0 - 2.0 * k)
        out = [0.0] * n
        if 0 <= st["target"] < n:
            out[st["target"]] = e
        if k < 1.0 and 0 <= st["prev"] < n and st["prev"] != st["target"]:
            out[st["prev"]] = 1.0 - e
        if k < 1.0:
            self.update()
        return out

    def _draw_page_dots(self, p: QPainter, w: float, h: float, dark: bool) -> None:
        """One dot per enabled page along the right edge: inactive white 25%, active white 95% (5 -> 5.6 dia)."""
        D = ISLAND.dots
        scale = self._get_size_scale()
        num = len(self.card_pages)
        if num <= 0:
            return
        dot_x, ys, _pitch = self._dots_geometry(w, h)
        curr_page = self._current_logical_page()
        active_idx = self.card_pages.index(curr_page) if curr_page in self.card_pages else -1
        strengths = self._dot_strengths(active_idx, time.time(), num)

        lo, hi = ISLAND.color.dot_inactive.alphaF(), ISLAND.color.dot_active.alphaF()
        col = QColor(255, 255, 255)
        p.setPen(Qt.PenStyle.NoPen)
        for i, dy in enumerate(ys):
            k = strengths[i]
            col.setAlphaF(lo + (hi - lo) * k)
            p.setBrush(col)
            r = (D.d + (D.d_active - D.d) * k) / 2.0 * scale
            p.drawEllipse(QPointF(dot_x, dy), r, r)

    def _dot_at(self, pos: QPointF | QPoint) -> Optional[int]:
        """Hit test for the page dots on expanded cards (local coordinates)."""
        if not self.expanded_card:
            return None
        iw, ih = self.island_size()
        w = max(iw, self.spring_w.target)
        h = max(ih, self.spring_h.target)
        scale = self._get_size_scale()
        dot_x, ys, pitch = self._dots_geometry(w, h)
        half_w = ISLAND.dots.hit_half_w * scale
        if abs(pos.x() - dot_x) > half_w:
            return None
        for i, dy in enumerate(ys):
            if abs(pos.y() - dy) <= pitch / 2.0:
                return i
        return None

    def wheelEvent(self, event) -> None:
        """Mouse wheel scrolling to switch between expanded card pages."""
        if self.expanded_card:
            delta = event.angleDelta().y()
            if delta == 0:
                return
            self.page_step(1 if delta < 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)
