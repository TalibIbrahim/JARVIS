"""
Unit tests for the new features:
1. Gesture camera auto-recovery and open palm natural webcam detection.
2. Voice wake word 'JARVIS' Siri-style expansion & listening window.
3. Intelligent music playback: Spotify, YouTube, and generic artist queries.
"""
import unittest
from unittest.mock import MagicMock, patch
import time
import numpy as np

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
        from media_adapter import MediaAdapter, set_media_adapter
        ctx = MagicMock()
        adapter = MagicMock(spec=MediaAdapter)
        set_media_adapter(adapter)
        try:
            with patch("pyautogui.press") as mock_press:
                reply = commands.run_command("pause", ctx)
                self.assertEqual(reply, "Done.")
                adapter.play_pause.assert_called_once()

                reply2 = commands.run_command("play", ctx)
                self.assertEqual(reply2, "Done.")
                self.assertEqual(adapter.play_pause.call_count, 2)
                # The adapter is the only actor: no extra raw "playpause" key press (would double-toggle).
                for call in mock_press.call_args_list:
                    self.assertNotEqual(call.args[0], "playpause")
        finally:
            set_media_adapter(None)


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


class TestPhase5ReliabilityAndRecovery(unittest.TestCase):
    """Verifies camera hotplug backoff, mic stream recovery, and sleep dt clamping/failsafes."""

    def test_camera_hotplug_reconnect_backoff_loop(self):
        """FrameGrabber enters reconnect_loop on read failure and re-initializes on replug."""
        from tracker import FrameGrabber
        import threading
        cfg = Config(camera_index=0)
        stop = threading.Event()
        grabber = FrameGrabber(cfg, stop)

        mock_bad_cap = MagicMock()
        mock_bad_cap.isOpened.return_value = False

        mock_good_cap = MagicMock()
        mock_good_cap.isOpened.return_value = True
        fake_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        mock_good_cap.read.return_value = (True, fake_frame)
        mock_good_cap.get.side_effect = lambda prop: 1280 if prop == 3 else 720

        # Simulate 1 failure then success
        caps = [mock_bad_cap, mock_good_cap]
        def cap_factory(*args, **kwargs):
            return caps.pop(0) if caps else mock_good_cap

        with patch("tracker.cv2.VideoCapture", side_effect=cap_factory), \
             patch("tracker.time.sleep", return_value=None), \
             patch.object(stop, "wait", return_value=False):
            recovered = grabber.reconnect_loop()
            self.assertTrue(recovered)
            self.assertTrue(grabber.opened)
            self.assertEqual(grabber.failures, 0)
            grabber.release()

    def test_microphone_stream_error_recovery(self):
        """WakeWordDetector cleans up broken stream, probes working mic, and recovers stream."""
        import voice
        from voice import WakeWordDetector
        import threading

        cfg = Config()
        stop = threading.Event()
        detector = WakeWordDetector(cfg=cfg, stop_event=stop)

        mock_pyaudio = MagicMock()
        mock_stream = MagicMock()
        fake_audio = np.zeros(1280, dtype=np.int16).tobytes()
        mock_stream.read.return_value = fake_audio
        mock_pyaudio.open.return_value = mock_stream

        with patch.object(voice, "get_shared_pyaudio", return_value=mock_pyaudio), \
             patch.object(voice, "reset_shared_pyaudio"), \
             patch.object(voice, "find_best_microphone", return_value=(2, "USB Audio Device")), \
             patch.object(stop, "wait", return_value=False):
            rec_stream = detector.reconnect_stream()
            self.assertIsNotNone(rec_stream)
            self.assertEqual(detector.device_index, 2)
            self.assertEqual(voice.Voice.current_mic_index, 2)
            self.assertEqual(voice.Voice.current_mic_name, "USB Audio Device")

    def test_controller_sleep_dt_clamping_and_failsafe_release(self):
        """Controller clamps dt, detects sleep gap (>0.10s), and unconditionally releases inputs."""
        from controller import Controller
        from gestures import Action
        cfg = Config(tracking_mode="relative")
        ctrl = Controller(cfg)

        with patch("controller.pyautogui.mouseUp") as mock_mouse_up, \
             patch("controller.pyautogui.keyUp") as mock_key_up:

            # 1. Simulate active dragging
            ctrl.button_down = True
            ctrl.ctrl_down = True
            ctrl.dragging = True

            # 2. Trigger unconditional failsafe release
            ctrl.failsafe_release()
            mock_mouse_up.assert_called_with(_pause=False)
            mock_key_up.assert_called_with("ctrl", _pause=False)
            self.assertFalse(ctrl.button_down)
            self.assertFalse(ctrl.ctrl_down)
            self.assertFalse(ctrl.dragging)

            # 3. Test sleep gap > 0.10s in relative mode
            mock_mouse_up.reset_mock()
            mock_key_up.reset_mock()
            ctrl.cur_x, ctrl.cur_y = 500.0, 500.0
            ctrl.target_x, ctrl.target_y = 500.0, 500.0
            ctrl.last_landmark_x = 0.5
            ctrl.last_landmark_y = 0.5
            ctrl.last_move_t = 100.0  # long ago (e.g. sleep)

            with patch("controller.time.perf_counter", return_value=105.0):  # 5s sleep gap
                act = Action("move", x=0.6, y=0.6)
                ctrl.handle(act)
                # Must have triggered failsafe release and NOT jumped cursor
                mock_mouse_up.assert_called_with(_pause=False)
                mock_key_up.assert_called_with("ctrl", _pause=False)
                self.assertEqual(ctrl.target_x, 500.0)
                self.assertEqual(ctrl.target_y, 500.0)

            # 4. Test lost / sleep action triggers failsafe release
            mock_mouse_up.reset_mock()
            mock_key_up.reset_mock()
            ctrl.handle(Action("lost"))
            mock_mouse_up.assert_called_with(_pause=False)
            mock_key_up.assert_called_with("ctrl", _pause=False)

            # 5. Test on_sleep and on_resume hooks
            mock_mouse_up.reset_mock()
            mock_key_up.reset_mock()
            ctrl.on_sleep()
            mock_mouse_up.assert_called_with(_pause=False)
            mock_key_up.assert_called_with("ctrl", _pause=False)

        ctrl.shutdown()


if __name__ == "__main__":
    unittest.main()
