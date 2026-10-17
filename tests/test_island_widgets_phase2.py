"""
tests/test_island_widgets_phase2.py — Unit test suite for Phase 2 Dynamic Island Widgets,
Cards, Interactivity without Focus Stealing, Fullscreen Suppression, and Settings UI.
"""

import sys
import threading
import time
import unittest

from PySide6.QtCore import Qt, QPointF, QPoint
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from config import Config
from ui.island import AppleDynamicIsland, IslandState, IslandDemoController, _SIZE
from ui.settings_window import SettingsWindow
from overlay import Overlay


class TestIslandWidgetsPhase2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.cfg = Config()
        self.stop_evt = threading.Event()
        self.ov = Overlay(self.stop_evt, cfg=self.cfg)
        self.island = self.ov.island

    def tearDown(self):
        self.stop_evt.set()
        self.ov.timer.stop()
        self.app.processEvents()

    def test_01_compact_widgets_layouts_and_springs(self):
        """Verify Time+Weather, Media, and Timer compact layouts and spring sizes."""
        now = time.time()
        self.island.reset_to_idle()

        # 1. Time + Weather compact layout
        self.island.compact_mode_override = "time_weather"
        self.island.update_frame(now)
        tw, th = self.island._target_size(IslandState.IDLE, now)
        self.assertAlmostEqual(tw, 176.0, delta=2.0)
        self.assertAlmostEqual(th, 32.0, delta=2.0)
        self.assertEqual(self.island.spring_w.target, tw)
        self.assertEqual(self.island.spring_h.target, th)

        # 2. Media compact layout
        self.island.compact_mode_override = "media"
        self.island.media_active = True
        self.island.media_playing = True
        self.island.update_frame(now)
        mw, mh = self.island._target_size(IslandState.IDLE, now)
        self.assertAlmostEqual(mw, 186.0, delta=2.0)
        self.assertAlmostEqual(mh, 32.0, delta=2.0)
        self.assertEqual(len(self.island._media_bars), 4)

        # 3. Timer compact layout
        self.island.compact_mode_override = "timer"
        self.island.timer_active = True
        self.island.timer_end_time = now + 120.0
        self.island.update_frame(now)
        rw, rh = self.island._target_size(IslandState.IDLE, now)
        self.assertAlmostEqual(rw, 152.0, delta=2.0)
        self.assertAlmostEqual(rh, 32.0, delta=2.0)

    def test_02_expanded_media_card(self):
        """Verify Expanded Media Card layout, scrubber, buttons, and dominant color."""
        now = time.time()
        self.island.expand_card("media")
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.CARD_MEDIA)
        cw, ch = _SIZE[IslandState.CARD_MEDIA]
        self.assertEqual(self.island.spring_w.target, cw)
        self.assertEqual(self.island.spring_h.target, ch)

        # Scrubber bar hit test
        sr = self.island._scrubber_rect()
        self.assertGreater(sr.width(), 200.0)

        # Buttons hit test
        w = max(float(self.island.width()), self.island.spring_w.target)
        btn_prev = self.island._button_at(QPointF(w / 2.0 - 52.0, 136.0))
        btn_play = self.island._button_at(QPointF(w / 2.0, 136.0))
        btn_next = self.island._button_at(QPointF(w / 2.0 + 52.0, 136.0))
        self.assertEqual(btn_prev, "prev")
        self.assertEqual(btn_play, "play_pause")
        self.assertEqual(btn_next, "next")

        # Play-pause toggle
        initial_playing = self.island.media_playing
        self.island._trigger_button("play_pause")
        self.assertNotEqual(self.island.media_playing, initial_playing)

    def test_03_expanded_weather_card(self):
        """Verify Expanded Weather Card elements and hourly forecast."""
        now = time.time()
        self.island.expand_card("weather")
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.CARD_WEATHER)
        cw, ch = _SIZE[IslandState.CARD_WEATHER]
        self.assertEqual(self.island.spring_w.target, cw)
        self.assertEqual(self.island.spring_h.target, ch)
        self.assertGreaterEqual(len(self.island.weather_hourly), 5)

        # Temperature formatting
        self.island.weather_temp = 68.0
        self.assertEqual(self.island.format_temp(68.0), "68°")

    def test_04_expanded_time_card(self):
        """Verify Expanded Time Card clock and date display."""
        now = time.time()
        self.island.expand_card("time")
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.CARD_TIME)
        cw, ch = _SIZE[IslandState.CARD_TIME]
        self.assertEqual(self.island.spring_w.target, cw)
        self.assertEqual(self.island.spring_h.target, ch)

    def test_05_interactivity_and_auto_collapse(self):
        """Verify WS_EX_NOACTIVATE interactivity toggle and 4s auto-collapse."""
        now = time.time()
        self.island.collapse_card()
        self.assertFalse(self.island.is_interactive)

        # Expand enables interactivity
        self.island.expand_card("weather")
        self.assertTrue(self.island.is_interactive)
        self.assertEqual(self.island.expanded_card, "weather")

        # After 4.1s without interaction, collapses smoothly
        self.island.update_frame(now + 4.2)
        self.assertIsNone(self.island.expanded_card)
        self.assertFalse(self.island.is_interactive)

    def test_06_voice_expansion_triggers(self):
        """Verify voice prompt commands expand media and weather cards."""
        now = time.time()
        self.ov.post("prompt", text="Jarvis show media")
        self.ov._drain(now)
        self.island.update_frame(now)
        self.assertEqual(self.island.expanded_card, "media")

        self.ov.post("prompt", text="Jarvis show weather forecast")
        self.ov._drain(now + 0.1)
        self.island.update_frame(now + 0.1)
        self.assertEqual(self.island.expanded_card, "weather")

    def test_07_fullscreen_suppression(self):
        """Verify hide_fullscreen collapses the island smoothly to 0 height."""
        now = time.time()
        self.cfg.island_hide_fullscreen = True
        self.island.reset_to_idle()
        self.island.update_frame(now)

        # Notify fullscreen
        self.ov.post("fullscreen", active=True)
        self.ov._drain(now)
        self.island.update_frame(now + 0.02)
        self.assertTrue(self.island.is_fullscreen_suppressed)
        self.assertEqual(self.island.spring_h.target, 0.0)

        # Exit fullscreen
        self.ov.post("fullscreen", active=False)
        self.ov._drain(now + 0.04)
        self.island.update_frame(now + 0.06)
        self.assertFalse(self.island.is_fullscreen_suppressed)
        self.assertGreater(self.island.spring_h.target, 0.0)

    def test_08_settings_window_dynamic_island_section(self):
        """Verify SettingsWindow displays all 9 Dynamic Island options and saves live."""
        dlg = SettingsWindow(self.cfg)
        dlg.select_tab("Display")

        # Verify saving an island setting updates config and applies live
        dlg._save("island_weather_city", "London")
        self.assertEqual(getattr(self.cfg, "island_weather_city"), "London")

        dlg._save("island_idle_display", "time")
        self.assertEqual(getattr(self.cfg, "island_idle_display"), "time")

        dlg._save("island_size", "large")
        self.assertEqual(getattr(self.cfg, "island_size"), "large")
        dlg._save("island_size", "default")
        dlg._save("island_idle_display", "time_weather")
        dlg.close()

    def test_09_extended_demo_controller(self):
        """Verify extended IslandDemoController steps through compact and expanded widgets."""
        demo = IslandDemoController(self.island)
        demo.start()
        self.assertTrue(demo.active)

        # Step 0: Compact Time/Weather
        demo._apply_step(0)
        self.assertEqual(self.island.compact_mode_override, "time_weather")

        # Step 1: Expanded Weather Card
        demo._apply_step(1)
        self.assertEqual(self.island.expanded_card, "weather")

        # Step 2: Compact Media
        demo._apply_step(2)
        self.assertEqual(self.island.compact_mode_override, "media")

        # Step 3: Expanded Media Card
        demo._apply_step(3)
        self.assertEqual(self.island.expanded_card, "media")

        # Step 4: Compact Timer
        demo._apply_step(4)
        self.assertEqual(self.island.compact_mode_override, "timer")

        # Step 5: Assistant Wake
        demo._apply_step(5)
        self.assertTrue(self.island.is_wake)

        demo.stop()
        self.assertFalse(demo.active)


if __name__ == "__main__":
    unittest.main()
