"""
Unit tests for Siri-style prompt display and Iron Man AI assistant enhancements:
- Real-time prompt posting to overlay
- Iron Man HUD diagnostics, protocols, and personality
- Action execution and telemetry reporting
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from voice import Voice
from commands import run_command, get_system_telemetry
from ai import (
    WHITELIST_ACTIONS,
    execute_whitelisted_action,
    build_system_prompt,
    parse_ai_response,
)
from overlay import Overlay, AppleDynamicIsland


class TestSiriPromptDisplay(unittest.TestCase):
    """Verifies that user prompts are captured and posted to the overlay like Siri."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.wake_aliases = ["jarvis"]
        self.ctx = MagicMock()
        self.stop = MagicMock()
        self.voice = Voice(self.cfg, self.ctx, self.stop)

    def test_voice_posts_prompt_to_overlay(self):
        """When a user command is recognized, it must post 'prompt' to the overlay."""
        with patch("voice.run_command", return_value="Opening Chrome."):
            self.voice._process("jarvis open chrome")

        # Verify prompt event was posted to overlay with clean command
        self.ctx.overlay.post.assert_any_call("prompt", text="open chrome", raw="jarvis open chrome")
        self.ctx.say.assert_called_with("Opening Chrome.")

    def test_voice_posts_transcribing_visual_feedback(self):
        """When speech audio is captured, overlay is notified of active transcription."""
        recognizer = MagicMock()
        audio = MagicMock()
        audio.get_raw_data.return_value = b"\x00\x10" * 4000
        audio.get_wav_data.return_value = b"RIFF....WAVE"
        self.voice.audio_q.put(audio)
        self.stop.is_set.side_effect = [False, True]

        with patch("voice.get_api_key", return_value=None):
            with patch.object(self.voice, "_fallback_google", return_value="hello"):
                with patch.object(self.voice, "_process"):
                    self.voice._recognition_worker(recognizer, MagicMock())

        # Verify listening with transcribing=True was posted
        post_calls = [call for call in self.ctx.overlay.post.call_args_list if call[0][0] == "listening"]
        self.assertTrue(any(call[1].get("transcribing") is True for call in post_calls))

    def test_wake_word_alone_triggers_listening_and_wake_chirp(self):
        """When user says only 'jarvis', enters listening mode and plays wake tone."""
        self.voice._process("jarvis")
        self.ctx.overlay.post.assert_called_with("listening", until=self.voice.listening_until)
        self.ctx.chirp.assert_called_with("wake")
        self.ctx.say.assert_called_with("Yes, sir?")

    def test_overlay_drains_prompt_into_dynamic_island(self):
        """Dynamic Island updates user_prompt and prompt_until when prompt event arrives."""
        stop = MagicMock()
        stop.is_set.return_value = False
        overlay = Overlay(stop, cfg=self.cfg)
        now = time.time()
        overlay.post("prompt", text="what is the speed of light")
        overlay._drain(now)

        self.assertEqual(overlay.island.user_prompt, "what is the speed of light")
        self.assertGreater(overlay.island.prompt_until, now)
        self.assertFalse(overlay.island.listening)


class TestIronManSystemTelemetryAndCommands(unittest.TestCase):
    """Verifies Iron Man telemetry diagnostics and protocols."""

    def setUp(self):
        self.ctx = MagicMock()
        self.cfg = Config()
        self.ctx.engine.cfg = self.cfg
        self.ctx.engine.active = True

    def test_get_system_telemetry_returns_hardware_report(self):
        telemetry = get_system_telemetry(self.ctx)
        self.assertIsInstance(telemetry, str)
        self.assertIn("Mark systems", telemetry)
        self.assertIn("operating nominally", telemetry)
        self.assertIn("Repulsor tracking is online", telemetry)

    def test_command_run_diagnostics(self):
        reply = run_command("run diagnostics", self.ctx)
        self.assertIn("Mark systems", reply)
        self.assertIn("Repulsor tracking", reply)

    def test_command_suit_status(self):
        reply = run_command("suit status", self.ctx)
        self.assertIn("Mark systems", reply)

    def test_command_power_levels(self):
        reply = run_command("power levels", self.ctx)
        self.assertIn("Mark systems", reply)

    @patch("commands._launch")
    def test_command_house_party_protocol(self, mock_launch):
        reply = run_command("house party protocol", self.ctx)
        self.assertIn("House Party Protocol initiated", reply)
        mock_launch.assert_called_with("chrome")

    @patch("pyautogui.press")
    @patch("pyautogui.hotkey")
    def test_command_stealth_mode(self, mock_hotkey, mock_press):
        reply = run_command("stealth mode", self.ctx)
        self.assertIn("Stealth mode engaged", reply)
        mock_press.assert_called_with("volumemute", _pause=False)
        mock_hotkey.assert_called_with("win", "down", _pause=False)

    @patch("pyautogui.hotkey")
    def test_command_clean_slate_protocol(self, mock_hotkey):
        reply = run_command("clean slate protocol", self.ctx)
        self.assertIn("Clean Slate Protocol executed", reply)
        self.assertNotIn("Mr. Stark", reply)
        self.assertIn("sir", reply)
        mock_hotkey.assert_called_with("win", "d", _pause=False)
        self.ctx.set_active.assert_called_with(False)

    @patch("webbrowser.open")
    def test_command_target_practice(self, mock_web):
        reply = run_command("target practice", self.ctx)
        self.assertIn("Target practice range initialized", reply)
        mock_web.assert_called()

    def test_command_conversational_stark_witticisms(self):
        reply_thanks = run_command("thank you jarvis", self.ctx)
        self.assertIn("pleasure watching you work", reply_thanks)

        reply_who = run_command("who are you", self.ctx)
        self.assertIn("Just A Rather Very Intelligent System", reply_who)

        reply_there = run_command("are you there jarvis", self.ctx)
        self.assertIn("For you, sir, always", reply_there)


class TestIronManAIWhitelistedActions(unittest.TestCase):
    """Verifies new Iron Man actions in ai.py."""

    def setUp(self):
        self.ctx = MagicMock()
        self.cfg = Config()
        self.ctx.engine.cfg = self.cfg

    def test_actions_present_in_whitelist(self):
        for act in ("diagnostics", "house_party", "stealth_mode", "clean_slate", "target_practice"):
            self.assertIn(act, WHITELIST_ACTIONS)

    def test_execute_diagnostics_action(self):
        ok, msg = execute_whitelisted_action("diagnostics", {}, self.ctx)
        self.assertTrue(ok)
        self.assertIn("Diagnostics:", msg)

    @patch("commands._launch")
    def test_execute_house_party_action(self, mock_launch):
        ok, msg = execute_whitelisted_action("house_party", {}, self.ctx)
        self.assertTrue(ok)
        mock_launch.assert_called_with("chrome")

    @patch("pyautogui.press")
    @patch("pyautogui.hotkey")
    def test_execute_stealth_mode_action(self, mock_hotkey, mock_press):
        ok, msg = execute_whitelisted_action("stealth_mode", {}, self.ctx)
        self.assertTrue(ok)
        mock_press.assert_called_with("volumemute", _pause=False)
        mock_hotkey.assert_called_with("win", "down", _pause=False)

    @patch("pyautogui.hotkey")
    def test_execute_clean_slate_action(self, mock_hotkey):
        ok, msg = execute_whitelisted_action("clean_slate", {}, self.ctx)
        self.assertTrue(ok)
        mock_hotkey.assert_called_with("win", "d", _pause=False)
        self.ctx.set_active.assert_called_with(False)

    @patch("webbrowser.open")
    def test_execute_target_practice_action(self, mock_web):
        ok, msg = execute_whitelisted_action("target_practice", {}, self.ctx)
        self.assertTrue(ok)
        mock_web.assert_called()

    def test_build_system_prompt_contains_stark_persona(self):
        prompt = build_system_prompt(self.ctx)
        self.assertIn("Tony Stark's sophisticated", prompt)
        self.assertIn("Paul Bettany's J.A.R.V.I.S.", prompt)
        self.assertIn("Do NOT call the user 'Mr. Stark'", prompt)
        self.assertNotIn("occasionally 'Mr. Stark'", prompt)
        self.assertIn("diagnostics", prompt)
        self.assertIn("house_party", prompt)
        self.assertIn("stealth_mode", prompt)
        self.assertIn("clean_slate", prompt)


class TestVisualQueryTimeout(unittest.TestCase):
    """Verifies that visual queries like 'what is this' have strict timeouts and never hang in thinking state."""

    def setUp(self):
        self.ctx = MagicMock()
        self.cfg = Config()
        self.cfg.ai_enabled = True
        self.ctx.engine.cfg = self.cfg

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    def test_visual_query_no_frame_clears_thinking_state(self, mock_key):
        """When camera frame is missing, thinking state must be cleared immediately."""
        self.ctx.tracker.get_latest_frame.return_value = None
        reply = run_command("what is this", self.ctx)
        self.assertIn("optical sensor is currently unavailable", reply)
        # Verify thinking active=True then active=False was called
        self.ctx.overlay.post.assert_any_call("thinking", active=True)
        self.ctx.overlay.post.assert_any_call("thinking", active=False)

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    @patch("ai.GroqClient.describe_image")
    def test_visual_query_success_clears_thinking_state(self, mock_describe, mock_key):
        """When vision succeeds, thinking state must be cleared."""
        import numpy as np
        dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.ctx.tracker.get_latest_frame.return_value = dummy_frame
        mock_describe.return_value = "That is a Mark VII repulsor emitter, sir."

        reply = run_command("what is this", self.ctx)
        self.assertEqual(reply, "That is a Mark VII repulsor emitter, sir.")
        self.ctx.overlay.post.assert_any_call("thinking", active=True)
        self.ctx.overlay.post.assert_any_call("thinking", active=False)

    @patch("commands.get_api_key", return_value="gsk_valid_key")
    @patch("ai.GroqClient.describe_image")
    def test_visual_query_timeout_clears_thinking_state(self, mock_describe, mock_key):
        """When vision times out, thinking state must be cleared and error shown."""
        import numpy as np
        from ai import TimeoutError
        dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.ctx.tracker.get_latest_frame.return_value = dummy_frame
        mock_describe.side_effect = TimeoutError("Vision timed out")

        reply = run_command("what is this", self.ctx)
        self.assertIn("timed out", reply.lower())
        self.ctx.overlay.post.assert_any_call("ai_error", text="Vision Timed Out")
        self.ctx.overlay.post.assert_any_call("thinking", active=False)

    def test_overlay_thinking_watchdog_timeout(self):
        """Dynamic Island thinking flag must automatically expire after thinking_until."""
        stop = MagicMock()
        stop.is_set.return_value = False
        overlay = Overlay(stop, cfg=self.cfg)
        now = time.time()
        # Post thinking with 0.1s timeout
        overlay.post("thinking", active=True, timeout=0.1)
        overlay._drain(now)

        self.assertTrue(overlay.island.thinking)
        self.assertAlmostEqual(overlay.island.thinking_until, now + 0.1, delta=0.05)

        # After timeout expires, update_frame must set is_thinking to False
        future = now + 0.2
        overlay.island.update_frame(future)
        self.assertFalse(overlay.island.thinking and (future < overlay.island.thinking_until))


if __name__ == "__main__":
    unittest.main()
