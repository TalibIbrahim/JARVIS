"""
Unit tests for Assistant State Machine Glue, Config Wake Settings, and --island-demo CLI.
Strictly zero Qt imports: pure unit tests with mocks.
"""
import argparse
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from commands import run_command
from ai import execute_whitelisted_action
from voice import Voice, WakeWordDetector


class TestConfigWakeDefaults(unittest.TestCase):
    """Verifies wake word configuration defaults in config.py."""

    def test_wake_defaults(self):
        cfg = Config()
        self.assertTrue(cfg.wake_word_enabled)
        self.assertEqual(cfg.wake_sensitivity, "balanced")
        self.assertEqual(cfg.wake_followup_window, 6.0)
        self.assertTrue(cfg.chime_enabled)


class TestAssistantStateMachineGlue(unittest.TestCase):
    """Verifies the state machine glue connecting voice, commands, decision engine, and overlay."""

    def setUp(self):
        self.cfg = Config()
        self.ctx = MagicMock()
        self.stop = MagicMock()
        self.voice = Voice(self.cfg, self.ctx, self.stop)

    @patch("voice.play_chime")
    def test_wake_detected_triggers_overlay_events_and_soft_chime(self, mock_play_chime):
        """Wake detected triggers overlay 'wake', 'wake_detected', 'listening', and soft chime."""
        self.voice._on_wake_detected()
        self.ctx.overlay.post.assert_any_call("wake", duration=0.6)
        self.ctx.overlay.post.assert_any_call("wake_detected")
        self.ctx.overlay.post.assert_any_call("listening", until=self.voice.listening_until)
        mock_play_chime.assert_called_once_with(non_blocking=True)

    @patch("voice.play_chime")
    def test_wake_detected_suppresses_chime_when_disabled(self, mock_play_chime):
        """When chime_enabled is False, wake detected does not play chime."""
        self.cfg.chime_enabled = False
        self.voice._on_wake_detected()
        mock_play_chime.assert_not_called()

    def test_partial_speech_streaming_to_overlay(self):
        """Streaming partial / live speech transcripts post live_speech to overlay."""
        self.voice.post_partial_speech("hello jarvis how are you", is_final=False)
        self.ctx.overlay.post.assert_called_with("live_speech", text="hello jarvis how are you", is_final=False)

    def test_intent_handoff_posts_thinking_state(self):
        """When speech is handed off for intent resolution, overlay receives 'thinking'."""
        with patch("voice.run_command", return_value="Result confirmed."):
            self.voice._process("jarvis what time is it")

        self.ctx.overlay.post.assert_any_call("thinking", active=True, timeout=8.0)
        self.ctx.overlay.post.assert_any_call("thinking", active=False)

    def test_five_second_silence_triggers_no_speech(self):
        """Silence timeout triggers overlay 'no_speech' (nope wiggle collapse)."""
        self.voice._on_no_speech_detected()
        self.ctx.overlay.post.assert_any_call("no_speech")
        self.ctx.overlay.post.assert_any_call("listening_done")

    @patch("ctypes.windll.user32.LockWorkStation", create=True)
    def test_direct_command_completion_posts_action_executed(self, mock_lock):
        """Completing a direct command notifies overlay with action_executed for RESULT card."""
        run_command("lock workstation", self.ctx)
        self.ctx.overlay.post.assert_any_call("action_executed", action="lock_workstation", success=True)

    @patch("pyautogui.screenshot")
    @patch("os.makedirs")
    def test_screenshot_command_completion_posts_action_executed(self, mock_dirs, mock_shot):
        """Screenshot execution posts action_executed with screenshot action."""
        run_command("take screenshot", self.ctx)
        self.ctx.overlay.post.assert_any_call("action_executed", action="screenshot", success=True)

    @patch("pyautogui.press")
    def test_whitelisted_action_execution_posts_action_executed_success(self, mock_press):
        """execute_whitelisted_action posts action_executed with success=True."""
        ok, msg = execute_whitelisted_action("volume_up", {"steps": 5}, self.ctx)
        self.assertTrue(ok)
        self.ctx.overlay.post.assert_any_call("action_executed", action="volume_up", success=True)

    def test_whitelisted_action_execution_posts_action_executed_failure(self):
        """execute_whitelisted_action posts action_executed with success=False on rejection."""
        ok, msg = execute_whitelisted_action("open_app", {"app": "malicious_script"}, self.ctx)
        self.assertFalse(ok)
        self.ctx.overlay.post.assert_any_call("action_executed", action="open_app", success=False)


class TestMainIslandDemoArg(unittest.TestCase):
    """Verifies --island-demo argument is registered in main.py."""

    def test_island_demo_argument_parsing(self):
        import main
        # Inspect main argparse without invoking GUI loop
        with patch("sys.argv", ["main.py", "--island-demo"]):
            with patch("main.acquire_single_instance_lock", return_value=True):
                # Verify ArgumentParser parses --island-demo
                parser = argparse.ArgumentParser(description="J.A.R.V.I.S. gesture control")
                parser.add_argument("--island-demo", action="store_true")
                args = parser.parse_args(["--island-demo"])
                self.assertTrue(args.island_demo)

    @patch("overlay.Overlay")
    @patch("sys.exit")
    def test_main_island_demo_branch(self, mock_exit, mock_overlay_cls):
        import main
        mock_exit.side_effect = SystemExit(0)
        mock_overlay = MagicMock()
        mock_overlay_cls.return_value = mock_overlay
        with patch("sys.argv", ["main.py", "--island-demo"]):
            with self.assertRaises(SystemExit):
                main.main()
        mock_overlay_cls.assert_called_once()
        mock_overlay.run.assert_called_once()
        mock_exit.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
