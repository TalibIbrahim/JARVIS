"""
Unit tests for SettingsWindow stability, Spring physics numerical stability,
and background audio probing safety.
"""
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from overlay import Spring, PYSIDE6_AVAILABLE
import voice


class TestSpringPhysicsStability(unittest.TestCase):
    """Verifies that Spring physics never explodes or overflows even during severe UI lag."""

    def test_spring_lag_explosion_prevention(self):
        s = Spring(120.0, 190.0, 22.0)
        s.set_target(210.0)
        # Simulate 10 frames of 0.5s lag
        for _ in range(10):
            val = s.update(0.5)
            self.assertGreaterEqual(val, 10.0)
            self.assertLessEqual(val, 3840.0)
            self.assertTrue(abs(val) < 5000.0)

    def test_spring_convergence(self):
        s = Spring(100.0, 180.0, 20.0)
        s.set_target(300.0)
        for _ in range(100):
            s.update(0.016)
        self.assertAlmostEqual(s.value, 300.0, delta=1.0)


class TestAudioProbingSafety(unittest.TestCase):
    """Verifies that audio probing is non-blocking and safe."""

    def test_probe_mic_level_non_blocking_when_locked(self):
        # Acquire the lock in a thread
        lock_held = threading.Event()
        stop_thread = threading.Event()

        def lock_holder():
            with voice._PORTAUDIO_LOCK:
                lock_held.set()
                stop_thread.wait(timeout=2.0)

        t = threading.Thread(target=lock_holder)
        t.start()
        lock_held.wait(timeout=1.0)

        try:
            # probe_mic_level should return immediately with (0.0, 0.0) and NOT block
            norm, rms = voice.probe_mic_level()
            self.assertEqual(norm, 0.0)
            self.assertEqual(rms, 0.0)
        finally:
            stop_thread.set()
            t.join(timeout=1.0)

    def test_list_microphones_without_rms_probe(self):
        # list_microphones(probe_rms=False) should not open audio streams
        mics = voice.list_microphones(probe_rms=False)
        self.assertIsInstance(mics, list)


@unittest.skipUnless(PYSIDE6_AVAILABLE, "PySide6 not installed")
class TestSettingsWindowInteractions(unittest.TestCase):
    """Verifies Settings dialog initialization and interaction stability."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        from overlay import Overlay, SettingsWindow
        self.cfg = Config()
        self.stop = threading.Event()
        self.ctx = MagicMock()
        self.overlay = Overlay(self.stop, cfg=self.cfg, ctx=self.ctx)

    def tearDown(self):
        self.stop.set()
        if self.overlay.settings_dlg:
            self.overlay.settings_dlg.close()

    def test_quit_on_last_window_closed_is_false(self):
        self.assertFalse(self.app.quitOnLastWindowClosed())

    def test_open_settings_and_tab_navigation(self):
        self.overlay.open_settings()
        dlg = self.overlay.settings_dlg
        self.assertIsNotNone(dlg)

        for i in range(dlg.sidebar.count()):
            dlg.sidebar.setCurrentRow(i)
            self.app.processEvents()
            self.assertEqual(dlg.stack.currentIndex(), i)

    def test_dialog_close_does_not_set_stop_event(self):
        self.overlay.open_settings()
        dlg = self.overlay.settings_dlg
        dlg.close()
        self.app.processEvents()
        self.assertFalse(self.stop.is_set())


if __name__ == "__main__":
    unittest.main()
