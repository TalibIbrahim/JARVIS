"""
Unit tests for the Laya-style Local Decision Engine (System 1 Reflex)
and Voice Profile Speaker Verification.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from commands import run_command
from decision_engine import get_decision_engine, evaluate_decision, DecisionMatch
from voice_profile import VoiceProfileManager


class TestDecisionEngineCore(unittest.TestCase):
    """Verifies ultra-fast local intent classification and calibration."""

    def setUp(self):
        self.engine = get_decision_engine()

    def test_latency_sub_15ms(self):
        """Decision engine must classify commands in under 15 milliseconds (typically < 1ms)."""
        queries = [
            "crank up the audio",
            "hide all my open windows",
            "take a screenshot right now",
            "pause the tunes",
            "go dark and enter stealth mode",
            "run full diagnostic",
            "what is the distance to the moon",
        ]
        times = []
        for q in queries:
            t0 = time.perf_counter()
            self.engine.classify(q)
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000.0)

        avg_latency = sum(times) / len(times)
        max_latency = max(times)
        # Verify average is well below 15ms
        self.assertLess(avg_latency, 15.0, f"Average latency too high: {avg_latency:.2f}ms")
        self.assertLess(max_latency, 25.0, f"Max latency too high: {max_latency:.2f}ms")

    def test_volume_intents(self):
        """Tests natural variations of volume adjustments."""
        up_phrases = ["turn up the sound", "crank up the audio", "make it louder please", "boost volume"]
        for phrase in up_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "volume_up", f"Failed for phrase: '{phrase}' (got {res.intent})")
            self.assertGreaterEqual(res.confidence, 0.20)

        down_phrases = ["lower the sound", "turn down volume", "make it quieter", "quieter please"]
        for phrase in down_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "volume_down", f"Failed for phrase: '{phrase}' (got {res.intent})")

        mute_phrases = ["mute the sound", "shush jarvis", "silence audio", "shut up audio"]
        for phrase in mute_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "mute", f"Failed for phrase: '{phrase}' (got {res.intent})")

    def test_media_intents(self):
        """Tests media transport controls."""
        pause_phrases = ["pause the tunes", "stop playback", "resume playback", "play music"]
        for phrase in pause_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "play_pause", f"Failed for phrase: '{phrase}'")

        next_phrases = ["skip this song", "next track please", "skip track"]
        for phrase in next_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "next_track", f"Failed for phrase: '{phrase}'")

        prev_phrases = ["previous song", "go back to previous track", "replay track"]
        for phrase in prev_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "previous_track", f"Failed for phrase: '{phrase}'")

    def test_desktop_control_intents(self):
        """Tests desktop actions: show desktop, lock workstation, screenshot."""
        desktop_phrases = ["hide all my windows", "show my desktop", "minimize everything", "clear the screen"]
        for phrase in desktop_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "show_desktop", f"Failed for phrase: '{phrase}'")

        lock_phrases = ["lock my workstation", "secure computer", "lock pc immediately"]
        for phrase in lock_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "lock_workstation", f"Failed for phrase: '{phrase}'")

        snap_phrases = ["capture the screen", "take a screenshot", "grab screenshot"]
        for phrase in snap_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "screenshot", f"Failed for phrase: '{phrase}'")

    def test_ironman_protocol_intents(self):
        """Tests stealth mode, diagnostics, clean slate, sleep/wake modes."""
        stealth_phrases = ["go dark", "enter stealth mode", "cloak system"]
        for phrase in stealth_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "stealth_mode", f"Failed for phrase: '{phrase}'")

        diag_phrases = ["run diagnostic check", "check vitals", "system telemetry status"]
        for phrase in diag_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "diagnostics", f"Failed for phrase: '{phrase}'")

        slate_phrases = ["clean slate protocol", "reset all windows", "clean slate"]
        for phrase in slate_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "clean_slate", f"Failed for phrase: '{phrase}'")

        sleep_phrases = ["standby mode", "go to sleep now", "sleep mode"]
        for phrase in sleep_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "sleep_mode", f"Failed for phrase: '{phrase}'")

        wake_phrases = ["wake up jarvis", "reactivate system", "wake mode"]
        for phrase in wake_phrases:
            res = self.engine.classify(phrase)
            self.assertEqual(res.intent, "wake_mode", f"Failed for phrase: '{phrase}'")

    def test_app_and_site_launch_extraction(self):
        """Tests dynamic argument extraction for opening applications and websites."""
        res_spotify = self.engine.classify("launch spotify for me")
        self.assertEqual(res_spotify.intent, "open_app")
        self.assertEqual(res_spotify.args.get("app"), "spotify")

        res_yt = self.engine.classify("bring up youtube")
        self.assertEqual(res_yt.intent, "open_site")
        self.assertEqual(res_yt.args.get("site"), "youtube")

    def test_conversation_fallback_delegation(self):
        """Knowledge queries must not trigger desktop actions and delegate to System 2."""
        knowledge_queries = [
            "what is the capital of France",
            "explain how stars fuse hydrogen into helium",
            "tell me a funny story about Tony Stark",
            "how do quantum computers work",
        ]
        for q in knowledge_queries:
            ctx = MagicMock()
            reply = evaluate_decision(q, ctx)
            self.assertIsNone(reply, f"Knowledge query '{q}' incorrectly intercepted by local reflex: {reply}")

    def test_dynamic_island_intents(self):
        """Verifies Dynamic Island voice commands and parameter extraction."""
        # Next / Prev
        res_next = self.engine.classify("next page")
        self.assertEqual(res_next.intent, "island_next_page")

        res_prev = self.engine.classify("previous page")
        self.assertEqual(res_prev.intent, "island_prev_page")

        # Show page
        res_hw = self.engine.classify("show hardware")
        self.assertEqual(res_hw.intent, "island_show_page")
        self.assertEqual(res_hw.args.get("page"), "hardware")

        res_cal = self.engine.classify("show calendar")
        self.assertEqual(res_cal.intent, "island_show_page")
        self.assertEqual(res_cal.args.get("page"), "calendar")

        res_media_card = self.engine.classify("show media card")
        self.assertEqual(res_media_card.intent, "island_show_page")
        self.assertEqual(res_media_card.args.get("page"), "media")

        # Show widget retains 'show media'
        res_media = self.engine.classify("show media")
        self.assertEqual(res_media.intent, "show_widget")
        self.assertEqual(res_media.args.get("widget"), "media")

        # Expand / Collapse toggle
        res_exp = self.engine.classify("expand island")
        self.assertEqual(res_exp.intent, "island_toggle_expand")

        # Tracking toggle
        res_track = self.engine.classify("toggle tracking")
        self.assertEqual(res_track.intent, "island_toggle_tracking")

        # Pin / Unpin
        res_pin = self.engine.classify("pin island")
        self.assertEqual(res_pin.intent, "island_set_pin")
        self.assertTrue(res_pin.args.get("pinned"))

        res_unpin = self.engine.classify("unpin island")
        self.assertEqual(res_unpin.intent, "island_set_pin")
        self.assertFalse(res_unpin.args.get("pinned"))


class TestDecisionEngineExecution(unittest.TestCase):
    """Tests executing actions through evaluate_decision and run_command."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.local_decision_engine = True
        self.ctx = MagicMock()
        self.ctx.engine.cfg = self.cfg

    @patch("pyautogui.press")
    def test_run_command_executes_volume_up_locally(self, mock_press):
        """Un-regexed command 'crank up the audio' triggers System 1 in commands.py."""
        reply = run_command("crank up the audio", self.ctx)
        self.assertIn("Adjusting the volume upward", reply)
        mock_press.assert_called_with("volumeup", presses=5, _pause=False)
        self.ctx.overlay.post.assert_any_call("action_executed", action="volume_up", success=True)

    @patch("pyautogui.hotkey")
    def test_run_command_executes_show_desktop(self, mock_hotkey):
        """'hide all my windows' executes show_desktop via Win+d."""
        reply = run_command("hide all my open windows", self.ctx)
        self.assertIn("desktop", reply.lower())
        mock_hotkey.assert_called_with("win", "d", _pause=False)

    @patch("ctypes.windll.user32.LockWorkStation", create=True)
    def test_run_command_executes_lock_workstation(self, mock_lock):
        """'lock my workstation' executes lock_workstation via Win32 API."""
        reply = run_command("lock my workstation", self.ctx)
        self.assertIn("lock", reply.lower())
        mock_lock.assert_called_once()

    def test_disabled_decision_engine_bypasses_reflex(self):
        """When local_decision_engine=False, commands pass directly to AI fallback."""
        self.cfg.local_decision_engine = False
        with patch("commands.get_api_key", return_value=None):
            reply = run_command("crank up the audio", self.ctx)
            # Without API key and with decision engine disabled, returns standard unhandled reply
            self.assertEqual(reply, "I didn't catch a command for that, sir.")

    def test_dynamic_island_execution(self):
        """Tests executing Dynamic Island actions through evaluate_decision."""
        from island_manager import IslandPageManager, IslandPage, set_island_page_manager
        mock_mgr = IslandPageManager()
        set_island_page_manager(mock_mgr)

        # Island next page
        reply = evaluate_decision("next page", self.ctx)
        self.assertIsNotNone(reply)
        self.assertIn("Navigating to the next page", reply)
        self.assertEqual(mock_mgr.active_page, IslandPage.CALENDAR)

        # Island prev page
        reply = evaluate_decision("previous page", self.ctx)
        self.assertIsNotNone(reply)
        self.assertIn("Navigating to the previous page", reply)
        self.assertEqual(mock_mgr.active_page, IslandPage.MEDIA)

        # Island show page
        reply = evaluate_decision("show calendar", self.ctx)
        self.assertIsNotNone(reply)
        self.assertIn("Displaying Calendar page", reply)
        self.assertEqual(mock_mgr.active_page, IslandPage.CALENDAR)

        # Show widget also synchronizes active island page
        reply_sw = evaluate_decision("show media", self.ctx)
        self.assertIsNotNone(reply_sw)
        self.assertIn("Displaying media widget", reply_sw)
        self.assertEqual(mock_mgr.active_page, IslandPage.MEDIA)

        # Island toggle expand
        mock_mgr.collapse()
        self.assertFalse(mock_mgr.is_expanded)
        reply = evaluate_decision("expand island", self.ctx)
        self.assertIsNotNone(reply)
        self.assertTrue(mock_mgr.is_expanded)

        # Island toggle tracking
        self.assertTrue(mock_mgr.tracking_enabled)
        reply = evaluate_decision("toggle tracking", self.ctx)
        self.assertIsNotNone(reply)
        self.assertFalse(mock_mgr.tracking_enabled)

        # Island pin / unpin
        self.assertFalse(mock_mgr.pin_open)
        reply = evaluate_decision("pin island", self.ctx)
        self.assertIsNotNone(reply)
        self.assertTrue(mock_mgr.pin_open)

        reply = evaluate_decision("unpin island", self.ctx)
        self.assertIsNotNone(reply)
        self.assertFalse(mock_mgr.pin_open)

    def test_dynamic_island_whitelisted_actions(self):
        """Tests direct execute_whitelisted_action for Dynamic Island actions."""
        from ai import execute_whitelisted_action
        from island_manager import IslandPageManager, IslandPage, set_island_page_manager
        mock_mgr = IslandPageManager()
        set_island_page_manager(mock_mgr)

        ok, msg = execute_whitelisted_action("island_next_page", {}, self.ctx)
        self.assertTrue(ok)
        self.assertIn("calendar", msg)

        ok, msg = execute_whitelisted_action("island_prev_page", {}, self.ctx)
        self.assertTrue(ok)
        self.assertIn("media", msg)

        ok, msg = execute_whitelisted_action("island_show_page", {"page": "calendar"}, self.ctx)
        self.assertTrue(ok)
        self.assertIn("calendar", msg)
        self.assertEqual(mock_mgr.active_page, IslandPage.CALENDAR)

        ok, msg = execute_whitelisted_action("island_toggle_expand", {}, self.ctx)
        self.assertTrue(ok)
        self.assertTrue(mock_mgr.is_expanded)

        ok, msg = execute_whitelisted_action("island_toggle_tracking", {}, self.ctx)
        self.assertTrue(ok)
        self.assertFalse(mock_mgr.tracking_enabled)

        ok, msg = execute_whitelisted_action("island_set_pin", {"pinned": True}, self.ctx)
        self.assertTrue(ok)
        self.assertTrue(mock_mgr.pin_open)


class TestVoiceProfileVerification(unittest.TestCase):
    """Tests acoustic speaker feature extraction and profile matching."""

    def test_feature_extraction(self):
        # 0.5s of 200 Hz sine wave at 16000 Hz sample rate
        import math
        sr = 16000
        duration = 0.5
        samples = [int(10000 * math.sin(2 * math.pi * 200 * i / sr)) for i in range(int(sr * duration))]
        raw_pcm = b"".join(s.to_bytes(2, byteorder="little", signed=True) for s in samples)

        mgr = VoiceProfileManager(profile_path="temp_voice_profile_test.json")
        try:
            feat = mgr.extract_features(raw_pcm, sample_rate=sr)
            self.assertIsNotNone(feat)
            self.assertEqual(len(feat), 11)
            # Normalized pitch dimension in unit-norm vector
            self.assertGreater(feat[0], 0.3)
            self.assertLess(feat[0], 0.9)
        finally:
            import os
            if os.path.exists("temp_voice_profile_test.json"):
                os.remove("temp_voice_profile_test.json")

    def test_speaker_verification_profile(self):
        profile_file = "temp_voice_profile_test2.json"
        mgr = VoiceProfileManager(profile_path=profile_file)
        try:
            import math
            sr = 16000
            # Speaker A (200 Hz fundamental)
            samples_a = [int(10000 * math.sin(2 * math.pi * 200 * i / sr)) for i in range(int(sr * 0.5))]
            raw_a = b"".join(s.to_bytes(2, byteorder="little", signed=True) for s in samples_a)

            ok, msg = mgr.enroll([raw_a])
            self.assertTrue(ok, msg)
            self.assertTrue(mgr.is_enrolled)

            # Same speaker sample should verify with high score
            is_match_same, score_same = mgr.verify(raw_a)
            self.assertTrue(is_match_same)
            self.assertGreater(score_same, 0.70)

            # Different speaker (380 Hz fundamental) should produce lower score
            samples_b = [int(10000 * math.sin(2 * math.pi * 380 * i / sr)) for i in range(int(sr * 0.5))]
            raw_b = b"".join(s.to_bytes(2, byteorder="little", signed=True) for s in samples_b)
            is_match_diff, score_diff = mgr.verify(raw_b)
            self.assertLess(score_diff, score_same)
        finally:
            import os
            if os.path.exists(profile_file):
                os.remove(profile_file)


if __name__ == "__main__":
    unittest.main()
