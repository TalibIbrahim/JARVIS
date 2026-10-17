"""
tests/test_island_demo_verification.py — Comprehensive verification of --island-demo execution,
full 24-stage (0-23) lifecycle, UI rendering, HUD volume/window switch capsules, and interruptibility.
"""

import math
import sys
import threading
import time
import unittest

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtWidgets import QApplication

from config import Config
from ui.island import AppleDynamicIsland, IslandState, IslandDemoController, _SIZE
from overlay import Overlay


class TestIslandDemoVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.cfg = Config()
        self.stop_evt = threading.Event()
        self.overlay = Overlay(self.stop_evt, cfg=self.cfg)
        self.island = self.overlay.island
        self.demo = IslandDemoController(self.island)

    def tearDown(self):
        self.demo.stop()
        self.stop_evt.set()
        self.overlay.timer.stop()
        self.app.processEvents()

    def test_all_24_stages_apply_and_render_cleanly(self):
        """Verify each of all 24 stages (0 through 23) applies and renders to pixmap without exception."""
        self.demo.start()
        self.assertTrue(self.demo.active)
        self.assertTrue(self.island._demo_mode)

        now = time.time()
        stage_names = [
            "Compact Time + Weather",
            "Expanded Weather Card",
            "Compact Media",
            "Expanded Media Card",
            "Compact Timer",
            "WAKE",
            "LISTENING",
            "THINKING",
            "DISAMBIGUATION",
            "RESULT",
            "Expanded Timer Card",
            "Routine: Work Mode",
            "Routine: Focus Mode",
            "Routine: Evening Mode",
            "COLLAPSE",
            "WAKE mid-collapse",
            "NO_SPEECH",
            "Screen Awareness Sending Indicator",
            "Quick Answer Card",
            "Gesture HUD: Volume 65%",
            "Gesture HUD: Volume 90%",
            "Gesture HUD: Window Switch VSCode",
            "Gesture HUD: Window Switch Chrome",
            "Auto-collapse back to Idle",
        ]

        self.assertEqual(len(stage_names), 24)

        for s in range(24):
            self.demo.step = s
            self.demo.step_start = now
            self.demo._apply_step(s)

            # Advance simulation frame
            self.island.update_frame(now + 0.05 * s)

            # Render to pixmap to exercise full paintEvent and painter paths
            w = int(max(10.0, max(float(self.island.width()), self.island.spring_w.target)))
            h = int(max(10.0, max(float(self.island.height()), self.island.spring_h.target)))
            self.island.resize(w, h)
            pix = QPixmap(w, h)
            self.island.render(pix)
            self.assertFalse(pix.isNull(), f"Pixmap rendering failed on stage {s}: {stage_names[s]}")

    def test_listening_audio_and_transcript_feed(self):
        """Verify step 6 (LISTENING) feeds audio RMS and partial transcript cleanly."""
        self.demo.start()
        self.demo._apply_step(6)
        self.demo.step = 6
        self.demo.step_start = time.time() - 1.5

        self.demo._feed_listening(1.5)
        self.assertGreater(len(self.island._transcript_words), 0)
        self.assertTrue(self.island.listening)

        # Render frame
        pix = QPixmap(int(max(10, self.island.width())), int(max(10, self.island.height())))
        self.island.render(pix)
        self.assertFalse(pix.isNull())

    def test_disambiguation_dwell_and_selection(self):
        """Verify step 8 (DISAMBIGUATION) exercises chip hovering and dwell selection."""
        self.demo.start()
        self.demo._apply_step(8)
        self.demo.step = 8
        self.demo.step_start = time.time()

        self.assertTrue(self.island.disambiguate_active)
        self.assertEqual(len(self.island.disambiguate_candidates), 3)

        # Simulate dwell ticks
        self.demo._step_tick()
        # Verify candidate chips can render
        pix = QPixmap(int(max(10, self.island.width())), int(max(10, self.island.height())))
        self.island.render(pix)
        self.assertFalse(pix.isNull())

    def test_hud_volume_capsule_lifecycle_and_render(self):
        """Verify Island HUD volume capsule triggers, spring dynamics, and rendering."""
        now = time.time()
        self.island.reset_to_idle()

        # Trigger 65% volume
        self.island.trigger_hud_volume(level=65, muted=False, duration=1.8)
        self.assertEqual(self.island.hud_type, "volume")
        self.assertEqual(self.island.hud_volume_level, 65)
        self.assertFalse(self.island.hud_volume_muted)
        self.assertEqual(self.island._resolve_state(now), IslandState.HUD_VOLUME)

        # Target sizing check
        tw, th = self.island._target_size(IslandState.HUD_VOLUME, now)
        self.assertAlmostEqual(tw, 260.0, delta=2.0)
        self.assertAlmostEqual(th, 38.0, delta=2.0)

        # Update frame & render
        self.island.update_frame(now + 0.1)
        pix = QPixmap(int(max(10, self.island.width())), int(max(10, self.island.height())))
        self.island.render(pix)
        self.assertFalse(pix.isNull())

        # Dynamic volume change to 90%
        self.island.trigger_hud_volume(level=90, muted=False, duration=1.8)
        self.assertEqual(self.island.hud_volume_level, 90)
        self.assertAlmostEqual(self.island.spring_hud_volume.target, 0.90, delta=0.01)

        # Muted volume
        self.island.trigger_hud_volume(level=0, muted=True, duration=1.8)
        self.assertTrue(self.island.hud_volume_muted)
        self.island.update_frame(now + 0.2)
        pix_muted = QPixmap(int(max(10, self.island.width())), int(max(10, self.island.height())))
        self.island.render(pix_muted)
        self.assertFalse(pix_muted.isNull())

    def test_hud_window_switch_capsule_lifecycle_and_render(self):
        """Verify Island HUD window switch capsule triggers, dynamic width, and rendering."""
        now = time.time()
        self.island.reset_to_idle()

        # Trigger VS Code window title
        title = "Visual Studio Code - island.py"
        self.island.trigger_hud_window(title=title, duration=2.0)
        self.assertEqual(self.island.hud_type, "window")
        self.assertEqual(self.island.hud_window_title, title)
        self.assertEqual(self.island._resolve_state(now), IslandState.HUD_WINDOW)

        # Target sizing check (scales with window title length, bounded between 240 and 360)
        tw, th = self.island._target_size(IslandState.HUD_WINDOW, now)
        self.assertGreaterEqual(tw, 240.0)
        self.assertLessEqual(tw, 360.0)
        self.assertAlmostEqual(th, 38.0, delta=2.0)

        # Update frame & render
        self.island.update_frame(now + 0.1)
        pix = QPixmap(int(max(10, self.island.width())), int(max(10, self.island.height())))
        self.island.render(pix)
        self.assertFalse(pix.isNull())

        # Long window title with ellipsis
        long_title = "Google Chrome - Apple Human Interface Guidelines Design Tokens Specification Overview"
        self.island.trigger_hud_window(title=long_title, duration=2.0)
        tw_long, _ = self.island._target_size(IslandState.HUD_WINDOW, now)
        self.assertEqual(tw_long, 360.0)  # Capped at maximum width
        self.island.update_frame(now + 0.2)
        pix_long = QPixmap(int(max(10, self.island.width())), int(max(10, self.island.height())))
        self.island.render(pix_long)
        self.assertFalse(pix_long.isNull())

    def test_overlay_post_dispatches_hud_events(self):
        """Verify Overlay post events route correctly to Island HUD methods."""
        now = time.time()
        # 1. hud_volume event
        self.overlay.hud_volume(level=70, muted=False, duration=1.5)
        # Drain overlay queue
        self.overlay._drain(now)
        self.assertEqual(self.island.hud_type, "volume")
        self.assertEqual(self.island.hud_volume_level, 70)

        # 2. hud_switch_window event
        self.overlay.hud_switch_window(title="Calculator", duration=1.5)
        self.overlay._drain(now)
        self.assertEqual(self.island.hud_type, "window")
        self.assertEqual(self.island.hud_window_title, "Calculator")

    def test_timer_ticks_cycle_through_stages(self):
        """Verify _step_tick naturally advances steps based on stage durations."""
        self.demo.start()
        self.assertEqual(self.demo.step, 0)

        # Advance step_start to simulate elapsed time > durations[0] (2.5s)
        self.demo.step_start = time.time() - 3.0
        self.demo._step_tick()
        self.assertEqual(self.demo.step, 1)

        # Advance to step 2
        self.demo.step_start = time.time() - 4.0
        self.demo._step_tick()
        self.assertEqual(self.demo.step, 2)


if __name__ == "__main__":
    unittest.main()
