"""ui/island_parts/media.py - Media card renderer, scrubber geometry and transport actions (verbatim move from island.py)."""

from __future__ import annotations

import ctypes
import sys
from ui.tokens import COLOR, TYPE, qfont
from typing import Optional
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient
)
from PySide6.QtCore import QPointF, QRectF, Qt
from ui.island_parts.common import create_cached_squircle_path


class MediaPageMixin:
    """Media card renderer, scrubber geometry and transport actions (verbatim move from island.py).
    Methods only; all state lives on the AppleDynamicIsland instance."""

    def _scrubber_rect(self) -> QRectF:
        w = max(float(self.width()), self.spring_w.target)
        scale = self._get_size_scale()
        sx = 24.0 * scale
        sy = 96.0 * scale
        sw = w - 48.0 * scale
        sh = 4.0 * scale
        return QRectF(sx, sy, sw, sh)

    def _scrubber_hit_rect(self) -> QRectF:
        sr = self._scrubber_rect()
        scale = self._get_size_scale()
        return QRectF(sr.x() - 4.0 * scale, sr.y() - 10.0 * scale, sr.width() + 8.0 * scale, sr.height() + 20.0 * scale)

    def media_toggle_play(self) -> None:
        self.media_playing = not self.media_playing
        success = False
        try:
            from media_adapter import get_media_adapter
            adapter = get_media_adapter()
            if self.media_playing:
                success = adapter.play()
            else:
                success = adapter.pause()
        except Exception as exc:
            print(f"[island] media_toggle_play error: {exc}")

        if not success and sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
            except Exception:
                pass
        self.update()

    def media_prev(self) -> None:
        success = False
        try:
            from media_adapter import get_media_adapter
            success = get_media_adapter().previous_track()
        except Exception as exc:
            print(f"[island] media_prev error: {exc}")

        if not success and sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB1, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB1, 0, 2, 0)
            except Exception:
                pass
        self.update()

    def media_next(self) -> None:
        success = False
        try:
            from media_adapter import get_media_adapter
            success = get_media_adapter().next_track()
        except Exception as exc:
            print(f"[island] media_next error: {exc}")

        if not success and sys.platform == "win32":
            try:
                ctypes.windll.user32.keybd_event(0xB0, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB0, 0, 2, 0)
            except Exception:
                pass
        self.update()

    def _draw_source_badge(self, p: QPainter, bx: float, by: float, size: float, source: str, dark: bool, scale: float) -> None:
        """Renders small source-app badge in corner of album art (Apple HIG)."""
        r = size / 2.0
        cx = bx + r
        cy = by + r

        # Dark circular background
        p.setPen(QPen(QColor(255, 255, 255, 30 if dark else 60), 1.0))
        p.setBrush(QBrush(QColor(16, 18, 20, 245) if dark else QColor(245, 245, 250, 245)))
        p.drawEllipse(QPointF(cx, cy), r, r)

        src = (source or "Spotify").lower()
        if "spotify" in src:
            # Green spotify soundwave arcs
            p.setPen(QPen(QColor("#1ED760"), 1.2 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for off, rad in [(-2.0, 4.6), (0.0, 3.4), (2.0, 2.2)]:
                arc_rect = QRectF(cx - rad * scale, cy + off * scale - rad * scale, rad * 2.0 * scale, rad * 2.0 * scale)
                p.drawArc(arc_rect, 40 * 16, 100 * 16)
        else:
            # Musical note glyph
            p.setPen(QColor("#F5F5F7" if dark else "#1C1C1E"))
            p.setFont(QFont("Segoe UI Variable Text", int(8 * scale), QFont.Weight.Bold))
            p.drawText(QRectF(bx, by - 0.5 * scale, size, size), Qt.AlignmentFlag.AlignCenter, "♪")

    def _draw_media_card(self, p: QPainter, w: float, h: float,
                         now: float, t: float, dark: bool,
                         primary: QColor, secondary: QColor, accent: QColor, CYAN: QColor) -> None:
        """
        Expanded Media Card (390x188 px):
        - Left: 56-60 px rounded album art (radius ~14px) with small source-app badge in corner.
        - Title (semibold), artist and album in secondary gray (#8E9094).
        - Tiny accent equalizer bars top right (animated when playing).
        - Scrubber: 4px track, accent-colored fill, white thumb, elapsed time on left
          and remaining time (-X:XX) on right in tabular numerals. Interactive click/drag seek hit target.
        - Three round controls: previous track, large play/pause in a darker disc, next track.
        - Dominant color tinting: controls, progress fill, and equalizer bars tinted from album art dominant color
          (fallback to sky blue #45C1EE).
        - Mouse click handling for previous, play/pause, next, and scrubber seek.
        """
        scale = self._get_size_scale()

        # Dominant color tinting with sky blue (#45C1EE) fallback
        if hasattr(self, "media_dominant_color") and self.media_dominant_color and self.media_dominant_color.isValid():
            dom_accent = self.media_dominant_color
        else:
            dom_accent = QColor("#45C1EE")

        # 1. 58 px Album Art Thumbnail with squircle radius 14 px
        art_size = 58.0 * scale
        ax = 22.0 * scale
        ay = 22.0 * scale
        art_rect = QRectF(ax, ay, art_size, art_size)
        art_path = create_cached_squircle_path(art_rect, 14.0 * scale)

        # Soft shadow & ambient tint
        for off, a in [(2, 18), (5, 32)]:
            s_rect = QRectF(ax, ay + off, art_size, art_size)
            s_path = create_cached_squircle_path(s_rect, 14.0 * scale)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, a)))
            p.drawPath(s_path)

        # Dominant glow behind artwork
        glow = QRadialGradient(ax + art_size / 2.0, ay + art_size / 2.0, art_size * 0.85)
        glow.setColorAt(0.0, QColor(dom_accent.red(), dom_accent.green(), dom_accent.blue(), 90))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(ax + art_size / 2.0, ay + art_size / 2.0), art_size * 0.75, art_size * 0.75)

        # Draw Artwork Pixmap
        art_pix = self._get_album_art(art_size)
        p.save()
        p.setClipPath(art_path)
        p.drawPixmap(QPointF(ax, ay), art_pix)
        p.restore()
        p.strokePath(art_path, QPen(QColor(255, 255, 255, 60 if dark else 90), 1.0))

        # Small source-app badge in corner of album art
        badge_size = 18.0 * scale
        bx = ax + art_size - badge_size + 2.0 * scale
        by = ay + art_size - badge_size + 2.0 * scale
        self._draw_source_badge(p, bx, by, badge_size, self.media_source, dark, scale)

        # 2. Track Title & Artist / Album
        tx = ax + art_size + 14.0 * scale
        eq_x = w - 42.0 * scale
        tw = eq_x - tx - 8.0 * scale

        title_font = qfont(TYPE.headline)
        title_font.setPointSize(int(14 * scale))
        title_font.setWeight(QFont.Weight.DemiBold)
        title_rect = QRectF(tx, ay + 2.0 * scale, tw, 22.0 * scale)
        self._draw_marquee_text(p, self.media_title, title_font, title_rect, primary, now - self._card_expand_t)

        album_str = getattr(self, "media_album", "")
        if album_str:
            artist_album_txt = f"{self.media_artist} — {album_str}"
        else:
            artist_album_txt = self.media_artist

        artist_font = qfont(TYPE.subheadline)
        artist_font.setPointSize(int(12 * scale))
        artist_font.setWeight(QFont.Weight.Normal)
        artist_rect = QRectF(tx, ay + 24.0 * scale, tw, 20.0 * scale)
        self._draw_marquee_text(p, artist_album_txt, artist_font, artist_rect, QColor("#8E9094"), now - self._card_expand_t)

        # 3. Tiny accent equalizer bars top right (animated when playing)
        eq_y = ay + 4.0 * scale
        bar_w = 2.4 * scale
        bar_gap = 1.6 * scale
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(dom_accent))
        for i in range(4):
            bx_i = eq_x + i * (bar_w + bar_gap)
            if self.media_playing:
                b_val = self._media_bars[i % len(self._media_bars)]
                bh_i = max(3.0 * scale, b_val * 14.0 * scale)
            else:
                bh_i = 3.0 * scale
            by_i = eq_y + 14.0 * scale - bh_i
            p.drawRoundedRect(QRectF(bx_i, by_i, bar_w, bh_i), 1.0 * scale, 1.0 * scale)

        # 4. Scrubber Bar with Tabular Numerals
        sr = self._scrubber_rect()
        # Track background: 4px track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 28 if dark else 50)))
        p.drawRoundedRect(sr, 2.0 * scale, 2.0 * scale)

        # Accent-colored fill
        prog_w = sr.width() * min(1.0, max(0.0, self.media_progress))
        if prog_w > 0:
            fill_rect = QRectF(sr.x(), sr.y(), prog_w, sr.height())
            p.setBrush(QBrush(dom_accent))
            p.drawRoundedRect(fill_rect, 2.0 * scale, 2.0 * scale)

        # White thumb
        hx = sr.x() + prog_w
        hy = sr.y() + sr.height() / 2.0
        p.setBrush(QBrush(QColor(255, 255, 255, 255)))
        p.drawEllipse(QPointF(hx, hy), 4.8 * scale, 4.8 * scale)

        # Numerals in tabular format
        num_font = qfont(TYPE.caption_2)
        num_font.setPointSize(int(10 * scale))
        num_font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
        p.setFont(num_font)
        p.setPen(QColor("#8E9094"))

        time_text_y = sr.y() + sr.height() + 3.0 * scale
        el_m = int(self.media_position_sec // 60)
        el_s = int(self.media_position_sec % 60)
        el_str = f"{el_m}:{el_s:02d}"
        p.drawText(QRectF(sr.x(), time_text_y, 60.0 * scale, 16.0 * scale), Qt.AlignmentFlag.AlignLeft, el_str)

        rem_sec = max(0.0, self.media_duration_sec - self.media_position_sec)
        rem_m = int(rem_sec // 60)
        rem_s = int(rem_sec % 60)
        rem_str = f"-{rem_m}:{rem_s:02d}"
        p.drawText(QRectF(sr.right() - 60.0 * scale, time_text_y, 60.0 * scale, 16.0 * scale), Qt.AlignmentFlag.AlignRight, rem_str)

        # 5. Playback Controls (Prev, Play-Pause in a darker disc, Next)
        cy_btn = 138.0 * scale
        prev_cx = (w / 2.0 - 52.0 * scale)
        play_cx = (w / 2.0)
        next_cx = (w / 2.0 + 52.0 * scale)

        self._draw_playback_button(p, "prev", prev_cx, cy_btn, 16.0 * scale, primary, scale, now, dom_accent)
        self._draw_playback_button(p, "play_pause", play_cx, cy_btn, 20.0 * scale, primary, scale, now, dom_accent)
        self._draw_playback_button(p, "next", next_cx, cy_btn, 16.0 * scale, primary, scale, now, dom_accent)

        # Footnote source app text
        foot_font = qfont(TYPE.caption_2)
        p.setFont(foot_font)
        p.setPen(QColor("#8E9094"))
        src_txt = f"♪ {self.media_source}"
        p.drawText(QRectF(w - 110.0 * scale, cy_btn - 8.0 * scale, 84.0 * scale, 16.0 * scale),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, src_txt)

    def _draw_playback_button(self, p: QPainter, btn_id: str, cx: float, cy: float,
                              r: float, color: QColor, scale: float, now: float,
                              dom_accent: Optional[QColor] = None) -> None:
        is_hover = (self._hovered_btn == btn_id)
        is_flash = (now < self._btn_flash_until and self._hovered_btn == btn_id)
        accent_col = dom_accent if dom_accent else COLOR.HUD_ONLY.neon_cyan

        if btn_id == "play_pause":
            # Large play/pause in a darker disc
            disc_r = r + 2.0 * scale
            p.setPen(QPen(QColor(255, 255, 255, 20), 1.0))
            if is_hover or is_flash:
                p.setBrush(QBrush(QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 55 if is_flash else 35)))
            else:
                p.setBrush(QBrush(QColor(255, 255, 255, 24)))
            p.drawEllipse(QPointF(cx, cy), disc_r, disc_r)
        else:
            # Hover circle highlight for prev / next
            if is_hover or is_flash:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(255, 255, 255, 55 if is_flash else 22)))
                p.drawEllipse(QPointF(cx, cy), r + 3.0 * scale, r + 3.0 * scale)

        # Hand-tracking dwell ring (fills over 1.0s)
        if is_hover and self._btn_dwell_elapsed > 0.05:
            dwell_prog = min(1.0, self._btn_dwell_elapsed / 1.0)
            dwell_pen = QPen(accent_col, 2.2 * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(dwell_pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            d_rect = QRectF(cx - r - 4.0 * scale, cy - r - 4.0 * scale, (r + 4.0 * scale) * 2.0, (r + 4.0 * scale) * 2.0)
            p.drawArc(d_rect, 90 * 16, -int(dwell_prog * 360 * 16))

        # Vector glyphs
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))

        if btn_id == "play_pause":
            if not self.media_playing:
                # Play triangle
                tri = QPolygonF([
                    QPointF(cx - 5.0 * scale, cy - 7.5 * scale),
                    QPointF(cx + 7.0 * scale, cy),
                    QPointF(cx - 5.0 * scale, cy + 7.5 * scale)
                ])
                p.drawPolygon(tri)
            else:
                # Pause bars
                bw = 3.5 * scale
                bh = 14.0 * scale
                p.drawRoundedRect(QRectF(cx - 5.5 * scale, cy - bh / 2.0, bw, bh), 1.5 * scale, 1.5 * scale)
                p.drawRoundedRect(QRectF(cx + 2.0 * scale, cy - bh / 2.0, bw, bh), 1.5 * scale, 1.5 * scale)

        elif btn_id == "prev":
            # Bar left
            p.drawRoundedRect(QRectF(cx - 8.0 * scale, cy - 7.0 * scale, 2.0 * scale, 14.0 * scale), 1.0, 1.0)
            # Double triangle left
            tri1 = QPolygonF([
                QPointF(cx + 6.0 * scale, cy - 7.0 * scale),
                QPointF(cx, cy),
                QPointF(cx + 6.0 * scale, cy + 7.0 * scale)
            ])
            tri2 = QPolygonF([
                QPointF(cx, cy - 7.0 * scale),
                QPointF(cx - 6.0 * scale, cy),
                QPointF(cx, cy + 7.0 * scale)
            ])
            p.drawPolygon(tri1)
            p.drawPolygon(tri2)

        elif btn_id == "next":
            # Double triangle right
            tri1 = QPolygonF([
                QPointF(cx - 6.0 * scale, cy - 7.0 * scale),
                QPointF(cx, cy),
                QPointF(cx - 6.0 * scale, cy + 7.0 * scale)
            ])
            tri2 = QPolygonF([
                QPointF(cx, cy - 7.0 * scale),
                QPointF(cx + 6.0 * scale, cy),
                QPointF(cx, cy + 7.0 * scale)
            ])
            p.drawPolygon(tri1)
            p.drawPolygon(tri2)
            # Bar right
            p.drawRoundedRect(QRectF(cx + 6.0 * scale, cy - 7.0 * scale, 2.0 * scale, 14.0 * scale), 1.0, 1.0)

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

    def _get_album_art(self, size: float) -> QPixmap:
        """Returns crisp DPI-safe cached album art pixmap."""
        dpr = self.devicePixelRatio()
        key = ("album_art", self.dark_mode, dpr, int(size))
        if key in self._icon_cache:
            return self._icon_cache[key]

        px = int(size * dpr)

        if self.media_album_art and not self.media_album_art.isNull():
            scaled = self.media_album_art.scaled(
                px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )
            self._icon_cache[key] = scaled
            return scaled

        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)
        pix.setDevicePixelRatio(dpr)

        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Procedural Apple Music vinyl & gradient
        grad = QLinearGradient(0, 0, size, size)
        grad.setColorAt(0.0, QColor("#8A2387"))
        grad.setColorAt(0.5, QColor("#E94057"))
        grad.setColorAt(1.0, QColor("#F27121"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(0, 0, size, size), 6.0, 6.0)

        # Subtle vinyl rings
        p.setPen(QPen(QColor(255, 255, 255, 30), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = size / 2.0, size / 2.0
        p.drawEllipse(QPointF(cx, cy), size * 0.35, size * 0.35)
        p.drawEllipse(QPointF(cx, cy), size * 0.22, size * 0.22)

        # Music note
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 230)))
        note_r = size * 0.10
        p.drawEllipse(QPointF(cx - size * 0.06, cy + size * 0.08), note_r, note_r * 0.75)
        p.drawRect(QRectF(cx + size * 0.02, cy - size * 0.14, size * 0.05, size * 0.22))
        p.end()

        self._icon_cache[key] = pix
        return pix

    def _draw_marquee_text(self, p: QPainter, text: str, font: QFont, rect: QRectF,
                           color: QColor, elapsed: float) -> None:
        """Draws marquee scrolling text only if truncated, perfectly stationary otherwise."""
        p.setFont(font)
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(text)
        avail_w = rect.width()

        p.save()
        p.setClipRect(rect)

        if text_w <= avail_w:
            p.setPen(color)
            p.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        else:
            lead_pause = 1.5
            scroll_spd = 28.0
            gap = 40.0
            cycle = (text_w + gap) / scroll_spd + lead_pause
            phase = elapsed % cycle
            offset = 0.0 if phase < lead_pause else (phase - lead_pause) * scroll_spd

            p.setPen(color)
            y = rect.y() + (rect.height() + fm.ascent() - fm.descent()) / 2.0
            p.drawText(int(rect.x() - offset), int(y), text)
            p.drawText(int(rect.x() - offset + text_w + gap), int(y), text)

        p.restore()
