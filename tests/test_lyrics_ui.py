"""Lyrics strip / pill / toggle on the Dynamic Island (offscreen; config writes and transport mocked)."""
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from config import Config
from overlay import Overlay
from ui.island import IslandState
from ui.tokens import ISLAND

LY = ISLAND.lyrics
TITLE, ARTIST = "Song", "Band"
KEY = f"{TITLE}|{ARTIST}"
SYNCED = [(10.0, "line A"), (20.0, "line B"), (30.0, "line C")]
NORMAL_PILL_W = 186.0


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.cfg = Config()
        self.stop_evt = threading.Event()
        self.ov = Overlay(self.stop_evt, cfg=self.cfg)
        self.island = self.ov.island
        self.island.reset_to_idle()
        self.island.media_title, self.island.media_artist = TITLE, ARTIST
        self.island.media_active = True
        self.island.media_playing = True
        self.island.compact_mode_override = "media"

    def tearDown(self):
        self.stop_evt.set()
        self.ov.timer.stop()
        self.app.processEvents()

    def _lyrics_cfg(self, enabled, in_pill=True):
        self.cfg.island_lyrics_enabled = enabled
        self.cfg.island_lyrics_in_pill = in_pill
        self.island.reload_config()


class TestLyricsLineSelection(_Base):
    def _line_at(self, position):
        self.island.set_lyrics_state(track_key=KEY, synced=SYNCED, status="ok")
        self.island.media_position_sec = position
        st = self.island._lyrics
        idx = self.island._lyrics_tick(st, time.time())
        return idx, self.island._ly_line(st, idx), self.island._ly_line(st, idx + 1)

    def test_current_and_next_line(self):
        idx, cur, nxt = self._line_at(25.0)
        self.assertEqual((idx, cur, nxt), (1, "line B", "line C"))

    def test_before_first_line_is_music_note(self):
        idx, cur, nxt = self._line_at(2.0)
        self.assertEqual(idx, -1)
        self.assertEqual(cur, "♪")
        self.assertEqual(nxt, "line A")

    def test_last_line_has_empty_next(self):
        idx, cur, nxt = self._line_at(45.0)
        self.assertEqual((idx, cur, nxt), (2, "line C", ""))

    def test_quarter_second_lead(self):
        self.assertEqual(LY.lead_s, 0.25)
        self.assertEqual(self._line_at(19.70)[0], 0)      # 19.95 < 20.0: still line A
        self.assertEqual(self._line_at(19.76)[0], 1)      # 20.01 >= 20.0: line B shows early
        self.assertEqual(self._line_at(20.0)[0], 1)

    def test_unsorted_input_is_sorted(self):
        self.island.set_lyrics_state(track_key=KEY, synced=list(reversed(SYNCED)), status="ok")
        self.assertEqual(self.island._lyrics.times, [10.0, 20.0, 30.0])
        self.assertEqual(self.island._lyrics.texts, ["line A", "line B", "line C"])

    def test_stale_track_lyrics_are_not_used(self):
        self.island.set_lyrics_state(track_key="Other|Band", synced=SYNCED, status="ok")
        self.assertIsNone(self.island._lyrics_for_track())

    def test_overlay_drain_routes_lyrics_state(self):
        self.ov.post("lyrics_state", track_key=KEY, synced=SYNCED, status="ok")
        self.ov._drain(time.time())
        self.assertIsNotNone(self.island._lyrics_for_track())


class TestLyricsStatusRender(_Base):
    def _render(self, **state):
        self._lyrics_cfg(True)
        self.island.set_lyrics_state(track_key=KEY, **state)
        self.island.expand_card("media")
        now = time.time()
        self.island.update_frame(now)
        pix = self.island.grab()
        self.assertFalse(pix.isNull())
        return self.island._lyrics_strip_mode(now)

    def test_loading(self):
        self.assertEqual(self._render(status="loading"), "loading")

    def test_not_found(self):
        self.assertEqual(self._render(status="not_found"), "not_found")

    def test_instrumental(self):
        self.assertEqual(self._render(instrumental=True, status="ok"), "instrumental")

    def test_synced_and_plain(self):
        self.island.media_position_sec = 25.0
        self.assertEqual(self._render(synced=SYNCED, status="ok"), "synced")
        self.assertEqual(self._render(plain="first line\nsecond", status="ok"), "plain")

    def test_lyrics_off_hides_strip(self):
        self.island.set_lyrics_state(track_key=KEY, synced=SYNCED, status="ok")
        self._lyrics_cfg(False)
        self.assertIsNone(self.island._lyrics_strip_mode(time.time()))


class TestLyricsPillWidth(_Base):
    def _width(self):
        return self.island._target_size(IslandState.IDLE, time.time())[0]

    def test_lyrics_off_is_normal_pill(self):
        self._lyrics_cfg(False)
        self.island.set_lyrics_state(track_key=KEY, synced=SYNCED, status="ok")
        self.island.media_position_sec = 25.0
        self.assertAlmostEqual(self._width(), NORMAL_PILL_W * self.island._get_size_scale(), delta=2.0)

    def test_lyrics_on_playing_with_line_is_240(self):
        self._lyrics_cfg(True)
        self.island.set_lyrics_state(track_key=KEY, synced=SYNCED, status="ok")
        self.island.media_position_sec = 25.0
        self.assertEqual(LY.pill_w, 240.0)
        self.assertAlmostEqual(self._width(), 240.0 * self.island._get_size_scale(), delta=0.01)

    def test_lyrics_on_but_paused_or_no_line_or_not_in_pill_is_normal(self):
        scale = self.island._get_size_scale()
        self._lyrics_cfg(True)
        self.island.set_lyrics_state(track_key=KEY, synced=SYNCED, status="ok")
        self.island.media_position_sec = 25.0
        self.island.media_playing = False
        self.assertAlmostEqual(self._width(), NORMAL_PILL_W * scale, delta=2.0)
        self.island.media_playing = True
        self.island.media_position_sec = 1.0               # before the first line
        self.assertAlmostEqual(self._width(), NORMAL_PILL_W * scale, delta=2.0)
        self.island.media_position_sec = 25.0
        self._lyrics_cfg(True, in_pill=False)
        self.assertAlmostEqual(self._width(), NORMAL_PILL_W * scale, delta=2.0)

    def test_pill_with_lyrics_paints(self):
        self._lyrics_cfg(True)
        self.island.set_lyrics_state(track_key=KEY, synced=SYNCED, status="ok")
        self.island.media_position_sec = 25.0
        self.island.update_frame(time.time())
        self.assertFalse(self.island.grab().isNull())


class TestLyricsToggle(_Base):
    def test_hit_test_at_glyph_centre(self):
        self.island.expand_card("media")
        self.island.update_frame(time.time())
        self.island.grab()                                  # paint publishes the hit snapshot
        ir = self.island.island_rect()
        s = self.island._get_size_scale()
        gx = ir.x() + LY.toggle_cx * s
        gy = ir.y() + LY.toggle_cy * s
        self.assertEqual(self.island.hit_test(gx, gy), "lyrics_toggle")

    def test_button_at_glyph_centre(self):
        from PySide6.QtCore import QPointF
        self.island.expand_card("media")
        self.island.update_frame(time.time())
        s = self.island._get_size_scale()
        self.assertEqual(self.island._button_at(QPointF(LY.toggle_cx * s, LY.toggle_cy * s)), "lyrics_toggle")

    def test_trigger_button_flips_setting_and_writes_config(self):
        self._lyrics_cfg(False)
        self.island.mgr.ctx = None
        with patch("ui.island_parts.media.run_transport") as rt:
            self.island._trigger_button("lyrics_toggle")
            self.assertTrue(self.cfg.island_lyrics_enabled)
            self.assertTrue(self.island.lyrics_enabled())
            self.assertEqual(rt.call_count, 1)             # persistence queued, never run for real
            self.island._trigger_button("lyrics_toggle")
            self.assertFalse(self.cfg.island_lyrics_enabled)
            self.assertFalse(self.island.lyrics_enabled())

    def test_trigger_button_uses_ctx_update_setting_when_available(self):
        self._lyrics_cfg(False)
        ctx = MagicMock()
        self.island.mgr.ctx = ctx
        with patch("ui.island_parts.media.run_transport") as rt:
            self.island._trigger_button("lyrics_toggle")
        ctx.update_setting.assert_called_once_with("island_lyrics_enabled", True)
        rt.assert_not_called()
        self.assertTrue(self.cfg.island_lyrics_enabled)

    def test_persist_setting_writes_expected_key(self):
        import ui.island_parts.media as media
        with patch("config._atomic_write_json") as w, patch("config.get_config_path", return_value="nonexistent.json"):
            media._persist_setting("island_lyrics_enabled", True)
        self.assertEqual(w.call_args[0][1].get("island_lyrics_enabled"), True)


if __name__ == "__main__":
    unittest.main()
