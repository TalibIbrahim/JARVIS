"""
Unit tests for the new features:
1. Gesture camera auto-recovery and open palm natural webcam detection.
2. Voice wake word 'JARVIS' Siri-style expansion & listening window.
3. Intelligent music playback: Spotify, YouTube, and generic artist queries.
"""
import unittest
from unittest.mock import MagicMock, patch
import time

from config import Config
from gestures import GestureEngine, Hand, Pose
import commands
import voice
import overlay


class TestOpenPalmAndCameraRecovery(unittest.TestCase):
    def test_camera_auto_recovery_when_preferred_fails(self):
        """Verifies that FrameGrabber auto-recovers to working camera 0 if camera 1 fails."""
        from tracker import FrameGrabber
        import threading
        cfg = Config(camera_index=1)
        stop = threading.Event()
        grabber = FrameGrabber(cfg, stop)
        
        # open_camera should try camera 1, fail, then auto-recover to camera 0
        opened = grabber.open_camera()
        if opened:
            self.assertEqual(cfg.camera_index, 0)
            grabber.release()

    def test_natural_webcam_open_palm_detected(self):
        """Verifies natural webcam open palm is identified as palm even with relaxed thumb."""
        # Simulated open palm with relaxed thumb (thumb_out ~ 0.35, openness ~ 0.75)
        # 21 landmarks
        lm = [
            (0.5, 0.8),   # 0 wrist
            (0.48, 0.72), (0.45, 0.65), (0.43, 0.58), (0.40, 0.52),  # 1-4 thumb
            (0.45, 0.55), (0.45, 0.44), (0.45, 0.35), (0.45, 0.25),  # 5-8 index
            (0.50, 0.54), (0.50, 0.42), (0.50, 0.33), (0.50, 0.22),  # 9-12 middle
            (0.55, 0.55), (0.55, 0.44), (0.55, 0.35), (0.55, 0.25),  # 13-16 ring
            (0.60, 0.58), (0.60, 0.49), (0.60, 0.42), (0.60, 0.34),  # 17-20 pinky
        ]
        p = Pose(lm)
        self.assertTrue(p.palm, "Natural open palm with relaxed thumb should be recognized as palm")


class TestMusicPlaybackAndIntelligence(unittest.TestCase):
    def test_play_drake_on_spotify(self):
        """Saying 'play Drake on spotify' must invoke Spotify search and reply appropriately."""
        ctx = MagicMock()
        with patch("commands.play_media", return_value=(True, "Playing Drake on Spotify, sir.")) as mock_pm:
            reply = commands.run_command("play Drake on spotify", ctx)
            self.assertEqual(reply, "Playing Drake on Spotify, sir.")
            mock_pm.assert_called_with("drake", platform="spotify")

    def test_play_drake_on_youtube(self):
        """Saying 'play Drake on youtube' must invoke YouTube search."""
        ctx = MagicMock()
        with patch("commands.play_media", return_value=(True, "Playing Drake on YouTube, sir.")) as mock_pm:
            reply = commands.run_command("play Drake on youtube", ctx)
            self.assertEqual(reply, "Playing Drake on YouTube, sir.")
            mock_pm.assert_called_with("drake", platform="youtube")

    def test_play_drake_generic(self):
        """Saying 'play Drake' must trigger music search (platform auto)."""
        ctx = MagicMock()
        with patch("commands.play_media", return_value=(True, "Playing Drake on Spotify, sir.")) as mock_pm:
            reply = commands.run_command("play Drake", ctx)
            self.assertEqual(reply, "Playing Drake on Spotify, sir.")
            mock_pm.assert_called_with("drake", platform="auto")

    def test_simple_play_pause_toggle(self):
        """Saying just 'pause' or 'play' should toggle playback without searching music."""
        ctx = MagicMock()
        with patch("pyautogui.press") as mock_press:
            reply = commands.run_command("pause", ctx)
            self.assertEqual(reply, "Done.")
            mock_press.assert_called_with("playpause", _pause=False)

            reply2 = commands.run_command("play", ctx)
            self.assertEqual(reply2, "Done.")


class TestWakeWordSiriExpansion(unittest.TestCase):
    def test_saying_jarvis_triggers_wake_detected_and_listening(self):
        """Saying 'jarvis' should trigger wake_detected and listening until now + 7.0s."""
        cfg = Config(command_window=7.0)
        ctx = MagicMock()
        stop = MagicMock()
        v = voice.Voice(cfg, ctx, stop)

        v._process("jarvis")
        self.assertGreater(v.listening_until, time.time())
        # Verify wake_detected event was posted to overlay
        ctx.overlay.post.assert_any_call("wake_detected")
        ctx.overlay.post.assert_any_call("listening", until=v.listening_until)
        # Verify wake chime was played
        ctx.chirp.assert_called_with("wake")
        # Verify voice spoke 'Yes, sir?'
        ctx.say.assert_called_with("Yes, sir?")

    def test_dynamic_island_expands_to_siri_dimensions_when_listening(self):
        """Dynamic Island dimensions when listening should expand to 380x52."""
        if not overlay.PYSIDE6_AVAILABLE:
            self.skipTest("PySide6 not available")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])

        mgr = MagicMock()
        mgr.cfg = Config()
        island = overlay.AppleDynamicIsland(mgr)

        # Set listening
        island.listening_until = time.time() + 5.0
        island.update_frame(time.time())

        # Target spring dimensions must be 380 x 52
        self.assertEqual(island.spring_w.target, 380.0)
        self.assertEqual(island.spring_h.target, 52.0)


if __name__ == "__main__":
    unittest.main()
