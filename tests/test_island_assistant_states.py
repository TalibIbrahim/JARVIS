"""
tests/test_island_assistant_states.py — Unit tests for Dynamic Island Assistant Animation States (Part 2).
"""

import sys
import threading
import time
import unittest

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from config import Config
from ui.island import AppleDynamicIsland, IslandState, IslandDemoController, _SIZE
from overlay import Overlay


class TestIslandAssistantStates(unittest.TestCase):
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

    def test_01_wake_state_and_springs(self):
        """Verify WAKE state sets overshooting spring targets and edge glow pulse."""
        now = time.time()
        self.ov.post("wake", duration=0.8)
        self.ov._drain(now)
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.WAKE)
        tw, th = _SIZE[IslandState.WAKE]
        self.assertEqual(self.island.spring_w.target, tw)
        self.assertEqual(self.island.spring_h.target, th)
        self.assertAlmostEqual(self.island.spring_w.k, 246.74, places=1)
        self.assertAlmostEqual(self.island.spring_w.c, 22.0, places=1)
        self.assertGreater(self.island.spring_glow.value, 0.0)

    def test_02_listening_state_and_word_fade(self):
        """Verify LISTENING with 7 bars and word-by-word fading partial transcript."""
        now = time.time()
        self.ov.post("live_speech", text="hello jarvis how are you", is_final=False)
        self.ov._drain(now)
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.LISTENING)
        self.assertEqual(len(self.island._wave_bars), 7)
        self.assertEqual(len(self.island._transcript_words), 5)
        self.assertEqual(self.island._transcript_words[0][0], "hello")
        self.assertEqual(self.island._transcript_words[-1][0], "you")

    def test_03_thinking_state(self):
        """Verify THINKING state speeds up orb and sweeps shimmer."""
        now = time.time()
        self.island.user_prompt = ""
        self.ov.post("thinking", active=True, timeout=3.0)
        self.ov._drain(now)
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.THINKING)
        tw, th = _SIZE[IslandState.THINKING]
        self.assertEqual(self.island.spring_w.target, tw)
        self.assertEqual(self.island.spring_h.target, th)

    def test_04_result_state_and_13pt_card(self):
        """Verify RESULT morphs to taller card with 13 pt text and vector icon."""
        now = time.time()
        self.ov.post("result", text="Volume Set to 50%", icon="success", duration=2.5)
        self.ov._drain(now)
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.RESULT)
        self.assertEqual(self.island.result_text, "Volume Set to 50%")
        self.assertEqual(self.island.result_icon, "success")
        self.assertEqual(self.island.spring_h.target, 58.0)

        # Check icon cache produces valid pixmap
        dpr = self.island.devicePixelRatio()
        icon = self.island._get_icon_pixmap("success", True, dpr)
        self.assertFalse(icon.isNull())
        self.assertGreater(icon.width(), 0)

    def test_05_no_speech_nope_nudge(self):
        """Verify NO_SPEECH triggers horizontal 'nope' wiggle without error text."""
        now = time.time()
        self.ov.post("no_speech")
        self.ov._drain(now)
        self.island.update_frame(now + 0.05)

        self.assertTrue(self.island.no_speech_active)
        self.assertEqual(self.island.spring_w.target, _SIZE[IslandState.IDLE][0])
        self.assertEqual(self.island.spring_h.target, _SIZE[IslandState.IDLE][1])
        # Verify horizontal offset oscillation
        self.assertNotEqual(self.island._nope_offset_x, 0.0)
        self.assertEqual(self.island.error_text, "")

    def test_06_mid_collapse_interruptibility(self):
        """Verify mid-collapse re-expansion smoothly preserves spring velocity."""
        now = time.time()
        # Start collapse to idle
        self.island.reset_to_idle()
        self.island.spring_w.snap(300.0)
        self.island.spring_w.set_target(110.0)
        self.island.update_frame(now + 0.016)

        # Mid-collapse wake
        vel_before = self.island.spring_w.vel
        self.ov.post("wake", duration=0.8)
        self.ov._drain(now + 0.02)
        self.island.update_frame(now + 0.032)

        self.assertEqual(self.island.spring_w.target, _SIZE[IslandState.WAKE][0])
        self.assertGreater(self.island.spring_w.value, 110.0)
        self.assertGreater(self.island.spring_w.vel, vel_before)

    def test_07_paint_event_smoke_test(self):
        """Verify paintEvent executes cleanly for all states without exceptions."""
        now = time.time()
        for state in [
            IslandState.IDLE, IslandState.WAKE, IslandState.LISTENING,
            IslandState.THINKING, IslandState.RESULT, IslandState.NO_SPEECH,
            IslandState.ERROR
        ]:
            if state == IslandState.WAKE:
                self.island.trigger_wake(1.0)
            elif state == IslandState.LISTENING:
                self.island.wake_until = 0.0
                self.island.listening = True
                self.island.listening_until = now + 5.0
            elif state == IslandState.THINKING:
                self.island.listening = False
                self.island.thinking = True
                self.island.thinking_until = now + 5.0
            elif state == IslandState.RESULT:
                self.island.thinking = False
                self.island.trigger_result("Test Result", "success", 2.0)
            elif state == IslandState.NO_SPEECH:
                self.island.trigger_no_speech()
            elif state == IslandState.ERROR:
                self.island.error_until = now + 5.0
                self.island.error_text = "Test Error"
            else:
                self.island.reset_to_idle()

            self.island.update_frame(now)
            pix = QPixmap(int(self.island.width()), int(self.island.height()))
            self.island.render(pix)
            self.assertFalse(pix.isNull())

    def test_08_demo_controller_cycle(self):
        """Verify IslandDemoController steps through all 17 stages of the extended Phase 6 demo."""
        demo = IslandDemoController(self.island)
        demo.start()
        self.assertTrue(demo.active)
        self.assertEqual(demo.step, 0)
        now = time.time()
        for s in range(17):
            demo._apply_step(s)
            self.island.update_frame(now + s * 0.1)
            pix = QPixmap(int(self.island.width()), int(self.island.height()))
            self.island.render(pix)
            self.assertFalse(pix.isNull())
        demo._step_tick()
        demo.stop()
        self.assertFalse(demo.active)


if __name__ == "__main__":
    unittest.main()
