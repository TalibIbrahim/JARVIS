"""ui/island_parts/media.py - Media card renderer (MEASUREMENT_SHEET_v2), scrubber geometry and transport actions.

Layout, colours and type come from ui.tokens.ISLAND / TYPE. The card is tinted from the art dominant colour
(background glow, scrubber, glyphs, waveform) exactly as sheet section 7 describes; button fills stay untinted.
Transport calls (adapter play/pause/next/prev, which can block up to 0.6 s) run on ONE background worker thread
and never touch Qt; the play state is flipped optimistically and reconciled by the next media_state event.
"""

from __future__ import annotations

import ctypes
import math
import queue
import sys
import threading
import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient, QTransform,
)

from ui.tokens import ISLAND, TYPE, COLOR, qfont
from ui.island_parts.common import (
    create_cached_squircle_path, ensure_contrast, lerp_color, lighten, with_alpha,
)
from ui.components import continuous_path_cached

_L = ISLAND.media
_C = ISLAND.color
_TINT = ISLAND.tint


# ──────────────────────────────────────────────────────────────────────────────
# Transport worker: one reusable daemon thread (no thread per click, no Qt access)
# ──────────────────────────────────────────────────────────────────────────────

_TRANSPORT_Q: "queue.Queue" = queue.Queue()
_TRANSPORT_THREAD: Optional[threading.Thread] = None
_TRANSPORT_LOCK = threading.Lock()


def _transport_loop() -> None:
    while True:
        job = _TRANSPORT_Q.get()
        try:
            job()
        except Exception as exc:  # never let the worker die
            print(f"[island] media transport error: {exc}")
        finally:
            _TRANSPORT_Q.task_done()


def run_transport(job) -> None:
    """Queue `job` (a no-arg callable) on the single media transport thread."""
    global _TRANSPORT_THREAD
    with _TRANSPORT_LOCK:
        if _TRANSPORT_THREAD is None or not _TRANSPORT_THREAD.is_alive():
            _TRANSPORT_THREAD = threading.Thread(target=_transport_loop, name="island-media-transport", daemon=True)
            _TRANSPORT_THREAD.start()
    _TRANSPORT_Q.put(job)


def wait_transport_idle(timeout: float = 2.0) -> bool:
    """Block until queued transport jobs finished (tests / shutdown). Returns True when idle."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _TRANSPORT_Q.unfinished_tasks == 0:
            return True
        time.sleep(0.005)
    return _TRANSPORT_Q.unfinished_tasks == 0


def _press_media_key(vk: int) -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
    except Exception:
        pass


def _adapter_call(method: str, *args, vk: Optional[int] = None) -> None:
    """Worker-thread body: adapter method, then the media-key fallback when the adapter reports failure."""
    ok = False
    try:
        from media_adapter import get_media_adapter
        ok = bool(getattr(get_media_adapter(), method)(*args))
    except Exception as exc:
        print(f"[island] media {method} error: {exc}")
    if not ok and vk is not None:
        _press_media_key(vk)


# ──────────────────────────────────────────────────────────────────────────────
# Tint palette (sheet section 7)
# ──────────────────────────────────────────────────────────────────────────────

class MediaPalette:
    """All tint-derived colours for one art colour, computed once per colour."""
    __slots__ = ("tint", "has_art", "bg_stops", "bg_glow", "scrub_start", "scrub_end", "knob_ring",
                 "knob_core", "pause", "side", "wave", "title", "artist", "album", "time", "_bg_points")

    def __init__(self, base: QColor, has_art: bool):
        self.has_art = has_art
        t = QColor(base) if has_art else QColor(_C.media_neutral)
        h, s, v, _a = t.getHsvF()
        h = max(0.0, h)
        t = QColor.fromHsvF(h, s, max(v, 0.55))              # clamp lightness: never a glyph colour that vanishes
        self.tint = t
        neutral = _C.bg_neutral
        top = QColor.fromHsvF(h, min(1.0, s * _TINT.bg_saturation), _TINT.bg_value)
        stops = [(pos, lerp_color(top, neutral, mix)) for pos, mix in _TINT.bg_stops]
        stops.append((1.0, QColor(neutral)))
        self.bg_stops = stops
        self.bg_glow = with_alpha(t, _TINT.glow_alpha)
        self.scrub_start = lighten(t, _TINT.scrub_start_lift)
        self.scrub_end = QColor(t)
        self.knob_ring = lighten(t, _TINT.knob_ring_lift)
        self.knob_core = lerp_color(_C.white, t, 0.03)
        self.pause = with_alpha(t, _TINT.pause_alpha)
        self.side = with_alpha(t, _TINT.side_glyph_alpha)
        self.wave = with_alpha(t, _TINT.wave_alpha)
        self._bg_points = stops
        # Text: white / grey with a faint tint cast, lifted to >= 4.5:1 on the (dark) background at its position
        title = lerp_color(_C.white, t, _TINT.title_mix)
        sec = lerp_color(_C.secondary, t, _TINT.text_mix)
        dim = lerp_color(_C.dim, t, _TINT.text_mix)
        self.title = ensure_contrast(title, self.bg_at(_L.title_baseline / _L.h))
        self.artist = ensure_contrast(sec, self.bg_at(_L.artist_baseline / _L.h))
        self.album = ensure_contrast(dim, self.bg_at(_L.album_baseline / _L.h))
        self.time = ensure_contrast(sec, self.bg_at(_L.time_baseline / _L.h))

    def bg_at(self, frac: float) -> QColor:
        """Opaque background colour of the tinted surface at height fraction `frac` (for contrast checks)."""
        pts = self._bg_points
        frac = min(1.0, max(0.0, frac))
        for (p0, c0), (p1, c1) in zip(pts, pts[1:]):
            if frac <= p1:
                span = max(1e-6, p1 - p0)
                return lerp_color(c0, c1, (frac - p0) / span)
        return pts[-1][1]


_PALETTE_CACHE: dict = {}


def media_palette(color: Optional[QColor], has_art: bool) -> MediaPalette:
    if not has_art or color is None or not color.isValid():
        key = ("neutral",)
        has_art = False
    else:
        key = (color.rgb(),)
    pal = _PALETTE_CACHE.get(key)
    if pal is None:
        if len(_PALETTE_CACHE) > 32:
            _PALETTE_CACHE.clear()
        pal = MediaPalette(color if has_art else QColor(_C.media_neutral), has_art)
        _PALETTE_CACHE[key] = pal
    return pal


# ──────────────────────────────────────────────────────────────────────────────
# Glyph paths (own drawings, rounded corners via quadratic corner cuts)
# ──────────────────────────────────────────────────────────────────────────────

def rounded_polygon(points, radius: float) -> QPainterPath:
    """Polygon with every corner rounded by `radius` (quadratic corner cut)."""
    n = len(points)
    path = QPainterPath()
    for i in range(n):
        p0, p1, p2 = points[i - 1], points[i], points[(i + 1) % n]

        def toward(a, b, d):
            vx, vy = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(vx, vy) or 1.0
            d = min(d, ln / 2.0)
            return (a[0] + vx / ln * d, a[1] + vy / ln * d)

        a = toward(p1, p0, radius)
        b = toward(p1, p2, radius)
        if i == 0:
            path.moveTo(a[0], a[1])
        else:
            path.lineTo(a[0], a[1])
        path.quadTo(p1[0], p1[1], b[0], b[1])
    path.closeSubpath()
    return path


def _skip_path(forward: bool) -> QPainterPath:
    """Skip glyph in an 11 x 13 box centred on the origin: bar + rounded triangle."""
    w, h = _L.side_glyph_w, _L.side_glyph_h
    x0, y0 = -w / 2.0, -h / 2.0
    tri = rounded_polygon([(x0 + 2.9, 0.0), (x0 + w, y0), (x0 + w, y0 + h)], 1.1)
    bar = QPainterPath()
    bar.addRoundedRect(QRectF(x0, y0, 1.9, h), 0.95, 0.95)
    path = tri.united(bar)
    if forward:
        path = QTransform().scale(-1.0, 1.0).map(path)
    return path


_GLYPH_CACHE: dict = {}


def _glyph(kind: str) -> QPainterPath:
    g = _GLYPH_CACHE.get(kind)
    if g is None:
        if kind == "prev":
            g = _skip_path(False)
        elif kind == "next":
            g = _skip_path(True)
        elif kind == "pause":
            bw, gap, h = _L.pause_bar_w, _L.pause_gap, _L.pause_h
            g = QPainterPath()
            x0 = -(2 * bw + gap) / 2.0
            g.addRoundedRect(QRectF(x0, -h / 2.0, bw, h), 1.4, 1.4)
            g.addRoundedRect(QRectF(x0 + bw + gap, -h / 2.0, bw, h), 1.4, 1.4)
        else:   # play
            g = rounded_polygon([(-5.2, -8.0), (-5.2, 8.0), (7.4, 0.0)], 1.6)
        _GLYPH_CACHE[kind] = g
    return g


# ──────────────────────────────────────────────────────────────────────────────

class MediaPageMixin:
    """Media card renderer, scrubber geometry and transport actions.
    Methods only; all state lives on the AppleDynamicIsland instance."""

    # ── geometry ─────────────────────────────────────────────────────────────

    def _scrubber_rect(self) -> QRectF:
        s = self._get_size_scale()
        return QRectF(_L.scrub_x * s, (_L.scrub_cy - _L.scrub_h / 2.0) * s, _L.scrub_w * s, _L.scrub_h * s)

    def _scrubber_hit_rect(self) -> QRectF:
        sr = self._scrubber_rect()
        scale = self._get_size_scale()
        return QRectF(sr.x() - 4.0 * scale, sr.y() - 10.0 * scale, sr.width() + 8.0 * scale, sr.height() + 20.0 * scale)

    def _media_button_geometry(self):
        """[(button id, snapshot id, cx, cy, hit radius)] in widget coordinates; the single source for drawing,
        mouse hit-testing and the thread-safe hit snapshot."""
        s = self._get_size_scale()
        cy = _L.ctrl_cy * s
        return [
            ("prev", "media_prev", _L.prev_cx * s, cy, _L.hit_side_r * s),
            ("play_pause", "media_toggle", _L.play_cx * s, cy, _L.hit_play_r * s),
            ("next", "media_next", _L.next_cx * s, cy, _L.hit_side_r * s),
        ]

    # ── transport (GUI thread: flip state + queue; worker: adapter call) ────

    def media_toggle_play(self) -> None:
        self.media_playing = not self.media_playing          # optimistic; the next media_state event reconciles
        want_play = bool(self.media_playing)
        run_transport(lambda: _adapter_call("play" if want_play else "pause", vk=0xB3))
        self.update()

    def media_prev(self) -> None:
        run_transport(lambda: _adapter_call("previous_track", vk=0xB1))
        self.update()

    def media_next(self) -> None:
        run_transport(lambda: _adapter_call("next_track", vk=0xB0))
        self.update()

    # ── tint ─────────────────────────────────────────────────────────────────

    def _has_media_art(self) -> bool:
        art = getattr(self, "media_album_art", None)
        return bool(art is not None and not art.isNull())

    def _media_palette(self) -> MediaPalette:
        return media_palette(getattr(self, "media_dominant_color", None), self._has_media_art())

    def _media_shell_tint(self):
        """(cache key, gradient stops, glow colour) for the tinted card surface, or (None, None, None) when
        there is no art (neutral surface)."""
        pal = self._media_palette()
        if not pal.has_art:
            return (None, None, None)
        return (pal.tint.rgb(), pal.bg_stops, pal.bg_glow)

    # ── drawing ──────────────────────────────────────────────────────────────

    def _text_baseline_font(self, token) -> tuple:
        font = qfont(token)
        return font, QFontMetricsF(font)

    def _wave_heights(self, now: float) -> list:
        """Sheet heights; while playing each bar breathes between 55% and 100% on the shared clock."""
        hs = _L.wave_heights
        if not self.media_playing or self._reduce_motion():
            return list(hs)
        out = []
        for i, hv in enumerate(hs):
            ph = now * (5.2 + 0.55 * i) + i * 1.7
            out.append(hv * (0.55 + 0.45 * (0.5 + 0.5 * math.sin(ph))))
        return out

    def _draw_media_card(self, p: QPainter, w: float, h: float,
                         now: float, t: float, dark: bool,
                         primary: QColor, secondary: QColor, accent: QColor, CYAN: QColor) -> None:
        """Expanded Media Card (390 x 188, MEASUREMENT_SHEET_v2)."""
        scale = self._get_size_scale()
        pal = self._media_palette()
        dpr = getattr(self, "_paint_dpr", 1.0)

        p.save()
        p.scale(scale, scale)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Album art (pre-masked pixmap) + source badge
        ax, ay, asz = _L.art_x, _L.art_y, _L.art_size
        p.drawPixmap(QPointF(ax, ay), self._get_masked_art(asz, dpr * scale))
        bd = _L.badge_d
        bcx = ax + asz - _L.badge_inset - bd / 2.0
        bcy = ay + asz - _L.badge_inset - bd / 2.0
        p.drawPixmap(QPointF(bcx - bd / 2.0 - 0.5, bcy - bd / 2.0 - 0.5), self._get_badge_pixmap(dpr * scale, pal))

        # 2. Text: title (marquee when too long), artist, album
        tx = _L.text_x
        avail = _L.wave_x - _L.text_wave_gap - tx
        elapsed = now - self._card_expand_t
        tfont, _ = self._text_baseline_font(TYPE.media_title)
        self._draw_marquee_text(p, self.media_title, tfont, tx, _L.title_baseline, avail, pal.title, elapsed)
        afont, _ = self._text_baseline_font(TYPE.media_artist)
        if self.media_artist:
            self._draw_marquee_text(p, self.media_artist, afont, tx, _L.artist_baseline, avail, pal.artist, elapsed)
        album_str = getattr(self, "media_album", "")
        if album_str:
            self._draw_marquee_text(p, album_str, afont, tx, _L.album_baseline, avail, pal.album, elapsed)

        # 3. Waveform: 7 bars, tint at 0.75 alpha, animated only while playing
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.wave)
        bw = _L.wave_bar_w
        for i, bh in enumerate(self._wave_heights(now)):
            p.drawRoundedRect(QRectF(_L.wave_x + i * _L.wave_pitch, _L.wave_cy - bh / 2.0, bw, bh), bw / 2.0, bw / 2.0)

        # 4. Scrubber: track, tint gradient fill, knob
        ratio = min(1.0, max(0.0, float(self.media_progress)))
        sx, sw, sth = _L.scrub_x, _L.scrub_w, _L.scrub_h
        sy = _L.scrub_cy - sth / 2.0
        p.setBrush(_C.track)
        p.drawRoundedRect(QRectF(sx, sy, sw, sth), sth / 2.0, sth / 2.0)
        fill_w = sw * ratio
        if fill_w > 0.5:
            grad = QLinearGradient(sx, 0.0, sx + max(fill_w, 1.0), 0.0)
            grad.setColorAt(0.0, pal.scrub_start)
            grad.setColorAt(1.0, pal.scrub_end)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(sx, sy, fill_w, sth), sth / 2.0, sth / 2.0)
        kx, ky = sx + fill_w, _L.scrub_cy
        glow = QRadialGradient(kx, ky, _L.knob_d * 1.1)
        glow.setColorAt(0.0, with_alpha(pal.knob_ring, 0.28))
        glow.setColorAt(1.0, with_alpha(pal.knob_ring, 0.0))
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(kx, ky), _L.knob_d * 1.1, _L.knob_d * 1.1)
        p.setBrush(pal.knob_ring)
        p.drawEllipse(QPointF(kx, ky), _L.knob_d / 2.0, _L.knob_d / 2.0)
        p.setBrush(pal.knob_core)
        p.drawEllipse(QPointF(kx, ky), _L.knob_core_d / 2.0, _L.knob_core_d / 2.0)

        # 5. Times: elapsed left, remaining right ("-2:19"), tabular 11 px
        nfont, nfm = self._text_baseline_font(TYPE.media_time)
        p.setFont(nfont)
        p.setPen(pal.time)
        pos = max(0.0, float(self.media_position_sec))
        el = f"{int(pos // 60)}:{int(pos % 60):02d}"
        rem = max(0.0, float(self.media_duration_sec) - pos)
        rm = f"-{int(rem // 60)}:{int(rem % 60):02d}"
        p.drawText(QPointF(_L.time_left_x, _L.time_baseline), el)
        p.drawText(QPointF(_L.time_right_edge - nfm.horizontalAdvance(rm), _L.time_baseline), rm)
        p.restore()

        # 6. Controls (own scale handling inside)
        now_f = now
        for btn_id, _snap, cx, cy, _hit in self._media_button_geometry():
            self._draw_playback_button(p, btn_id, cx, cy, scale, now_f, pal)

    def _draw_playback_button(self, p: QPainter, btn_id: str, cx: float, cy: float,
                              scale: float, now: float, pal: MediaPalette) -> None:
        is_hover = (self._hovered_btn == btn_id)
        is_flash = (now < self._btn_flash_until and self._hovered_btn == btn_id)
        play = (btn_id == "play_pause")
        d = (_L.play_d if play else _L.side_d) * scale
        r = d / 2.0

        base = QColor(_C.btn_play_fill if play else _C.btn_side_fill)      # never tinted
        if is_flash:
            base.setAlpha(min(255, base.alpha() + 60))
        elif is_hover:
            base.setAlpha(min(255, base.alpha() + 30))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawEllipse(QPointF(cx, cy), r, r)

        # Hand-tracking dwell ring (fills over 1.0 s)
        if is_hover and self._btn_dwell_elapsed > 0.05:
            dwell_prog = min(1.0, self._btn_dwell_elapsed / 1.0)
            p.setPen(QPen(pal.tint, 2.2 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            ring = QRectF(cx - r - 3.0 * scale, cy - r - 3.0 * scale, (r + 3.0 * scale) * 2.0, (r + 3.0 * scale) * 2.0)
            p.drawArc(ring, 90 * 16, -int(dwell_prog * 360 * 16))

        # Glyph (tint at 88% for play/pause, 62% for prev/next)
        kind = ("pause" if self.media_playing else "play") if play else btn_id
        p.save()
        p.translate(cx, cy)
        p.scale(scale, scale)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.pause if play else pal.side)
        p.drawPath(_glyph(kind))
        p.restore()

    # ── badge, art ───────────────────────────────────────────────────────────

    @staticmethod
    def _badge_kind(source: str) -> str:
        src = (source or "").lower()
        if "spotify" in src:
            return "spotify"
        if "chrome" in src:
            return "chrome"
        return "generic"

    def _get_badge_pixmap(self, dpr: float, pal: MediaPalette) -> QPixmap:
        kind = self._badge_kind(self.media_source)
        key = ("media_badge", kind, round(dpr, 3), pal.tint.rgb() if kind == "generic" else 0)
        pix = self._icon_cache.get(key)
        if pix is not None:
            return pix
        d = _L.badge_d + 1.0
        pix = QPixmap(max(1, int(math.ceil(d * dpr))), max(1, int(math.ceil(d * dpr))))
        pix.setDevicePixelRatio(dpr)
        pix.fill(Qt.GlobalColor.transparent)
        bp = QPainter(pix)
        bp.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = d / 2.0
        r = _L.badge_d / 2.0
        bp.setPen(Qt.PenStyle.NoPen)
        if kind == "chrome":
            wedge = QRectF(c - r, c - r, 2 * r, 2 * r)
            for start, col in ((30, _C.chrome_red), (150, _C.chrome_green), (270, _C.chrome_yellow)):
                bp.setBrush(col)
                bp.drawPie(wedge, start * 16, 120 * 16)
            bp.setBrush(_C.white)
            bp.drawEllipse(QPointF(c, c), r * 0.47, r * 0.47)
            bp.setBrush(_C.chrome_blue)
            bp.drawEllipse(QPointF(c, c), r * 0.37, r * 0.37)
        else:
            bp.setBrush(_C.badge_fill)
            bp.drawEllipse(QPointF(c, c), r, r)
            if kind == "spotify":
                bp.setBrush(Qt.BrushStyle.NoBrush)
                bp.setPen(QPen(_C.spotify_green, 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                for off, rad in ((-2.1, 4.9), (0.0, 3.6), (2.1, 2.3)):
                    bp.drawArc(QRectF(c - rad, c + off - rad, rad * 2.0, rad * 2.0), 40 * 16, 100 * 16)
            else:
                tri = rounded_polygon([(c - 2.4, c - 3.3), (c - 2.4, c + 3.3), (c + 3.4, c)], 0.7)
                bp.setPen(Qt.PenStyle.NoPen)
                bp.setBrush(pal.tint)
                bp.drawPath(tri)
        bp.end()
        self._icon_cache[key] = pix
        return pix

    def _get_masked_art(self, size: float, dpr: float) -> QPixmap:
        """Album art scaled to `size`, centre-cropped and masked by the continuous r15.4 corner (cached)."""
        key = ("media_art_masked", round(dpr, 3), round(size, 2))
        pix = self._icon_cache.get(key)
        if pix is not None:
            return pix
        px = max(1, int(round(size * dpr)))
        src = self._get_album_art_source(px)
        out = QPixmap(px, px)
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.GlobalColor.transparent)
        lsz = px / dpr
        op = QPainter(out)
        op.setRenderHint(QPainter.RenderHint.Antialiasing)
        op.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = continuous_path_cached(lsz, lsz, _L.art_radius * (lsz / _L.art_size), ISLAND.corner_smoothing)
        op.fillPath(path, QColor(255, 255, 255, 255))
        op.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        sx = max(0, (src.width() - px) // 2)
        sy = max(0, (src.height() - px) // 2)
        op.drawPixmap(QRectF(0.0, 0.0, lsz, lsz), src, QRectF(float(sx), float(sy), float(px), float(px)))
        op.end()
        self._icon_cache[key] = out
        return out

    def _get_album_art_source(self, px: int) -> QPixmap:
        """Unmasked art covering a px x px square: the real art (expanded fit) or the procedural placeholder."""
        art = getattr(self, "media_album_art", None)
        if art is not None and not art.isNull():
            pix = art.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
            pix.setDevicePixelRatio(1.0)
            return pix
        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = float(px)
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0.0, QColor("#8A2387"))
        grad.setColorAt(0.5, QColor("#E94057"))
        grad.setColorAt(1.0, QColor("#F27121"))
        p.fillRect(QRectF(0, 0, size, size), QBrush(grad))
        p.setPen(QPen(QColor(255, 255, 255, 30), max(1.0, size / 65.0)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx = cy = size / 2.0
        p.drawEllipse(QPointF(cx, cy), size * 0.35, size * 0.35)
        p.drawEllipse(QPointF(cx, cy), size * 0.22, size * 0.22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 230)))
        note_r = size * 0.10
        p.drawEllipse(QPointF(cx - size * 0.06, cy + size * 0.08), note_r, note_r * 0.75)
        p.drawRect(QRectF(cx + size * 0.02, cy - size * 0.14, size * 0.05, size * 0.22))
        p.end()
        return pix

    def _get_album_art(self, size: float) -> QPixmap:
        """Crisp DPI-safe cached (unmasked) album art; used by the compact media pill thumbnail."""
        dpr = getattr(self, "_paint_dpr", None) or self.devicePixelRatio()
        key = ("album_art", self.dark_mode, dpr, int(size))
        if key in self._icon_cache:
            return self._icon_cache[key]
        px = max(1, int(size * dpr))
        pix = self._get_album_art_source(px)
        pix.setDevicePixelRatio(dpr)
        self._icon_cache[key] = pix
        return pix

    # ── marquee ──────────────────────────────────────────────────────────────

    def _draw_marquee_text(self, p: QPainter, text: str, font: QFont, x: float, baseline: float,
                           avail_w: float, color: QColor, elapsed: float) -> None:
        """Text at (x, baseline); scrolls only when wider than `avail_w`, perfectly still otherwise."""
        p.setFont(font)
        fm = QFontMetricsF(font)
        text_w = fm.horizontalAdvance(text)
        p.setPen(color)
        if text_w <= avail_w:
            p.drawText(QPointF(x, baseline), text)
            return
        p.save()
        p.setClipRect(QRectF(x, baseline - fm.ascent() - 2.0, avail_w, fm.height() + 4.0))
        lead_pause, scroll_spd, gap = 1.5, 28.0, 40.0
        cycle = (text_w + gap) / scroll_spd + lead_pause
        phase = max(0.0, elapsed) % cycle
        offset = 0.0 if phase < lead_pause else (phase - lead_pause) * scroll_spd
        p.drawText(QPointF(x - offset, baseline), text)
        p.drawText(QPointF(x - offset + text_w + gap, baseline), text)
        p.restore()

    # ── thumbnail intake (GUI thread) ───────────────────────────────────────

    def set_thumbnail_bytes(self, data: bytes) -> None:
        """Sets thumbnail from raw bytes and samples dominant & average gradient colors."""
        pix = QPixmap()
        if pix.loadFromData(data):
            self.media_album_art = pix
            img = pix.toImage().scaled(32, 32)
            r, g, b = 0, 0, 0
            n = img.width() * img.height()
            r_top, g_top, b_top, n_top = 0, 0, 0, 0
            r_bot, g_bot, b_bot, n_bot = 0, 0, 0, 0
            half_h = img.height() // 2
            for y in range(img.height()):
                for x in range(img.width()):
                    c = img.pixelColor(x, y)
                    r += c.red()
                    g += c.green()
                    b += c.blue()
                    if y < half_h:
                        r_top += c.red()
                        g_top += c.green()
                        b_top += c.blue()
                        n_top += 1
                    else:
                        r_bot += c.red()
                        g_bot += c.green()
                        b_bot += c.blue()
                        n_bot += 1
            if n > 0:
                avg_col = QColor(int(r / n), int(g / n), int(b / n))
                top_col = QColor(int(r_top / n_top), int(g_top / n_top), int(b_top / n_top)) if n_top > 0 else avg_col
                bot_col = QColor(int(r_bot / n_bot), int(g_bot / n_bot), int(b_bot / n_bot)) if n_bot > 0 else avg_col
                try:
                    from media_adapter import extract_dominant_color
                    hex_c = extract_dominant_color(data)
                    if hex_c and hex_c.startswith("#"):
                        self.media_dominant_color = QColor(hex_c)
                    else:
                        self.media_dominant_color = top_col
                except Exception:
                    self.media_dominant_color = top_col

                self.media_average_color = avg_col
                self.media_secondary_color = bot_col
            self._icon_cache.clear()
            self.update()
