"""
Unit tests for Phase 8: Gesture + Voice Resolution, Window-under-Cursor & Privacy Logging.
Covers:
  1. Window Under Cursor Resolution (window_resolver.py & system_control.py)
  2. Gesture Shortcuts & HUD Events in gestures.py (vertical slide volume, horizontal swipe switch)
  3. Privacy Log in privacy_log.py (ring buffer, JSON sync, cloud audit wiring into ai.py & weather_client.py)
  4. Configuration Settings in config.py
  5. Voice Command & Decision Engine integration for cursor actions
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config import Config, load_config, save_config
from gestures import Action, GestureEngine, Hand
import privacy_log
from privacy_log import PrivacyLog, PrivacyEvent, get_privacy_log, record_cloud_transmission, reset_privacy_log
import window_resolver
from window_resolver import WindowResolver, POINT


FPS = 30.0
DT = 1.0 / FPS

# Open-hand synthetic template
BASE = {
    0: (0.50, 0.80), 5: (0.44, 0.62), 9: (0.50, 0.60), 13: (0.56, 0.62), 17: (0.61, 0.66),
    8: (0.44, 0.35), 12: (0.50, 0.32), 16: (0.56, 0.35), 20: (0.61, 0.42), 4: (0.30, 0.62),
}
CURL = {8: (0.45, 0.70), 12: (0.50, 0.70), 16: (0.55, 0.70), 20: (0.60, 0.72)}


def make_hand(pose="palm", dx=0.0, dy=0.0):
    """Creates synthetic Hand landmark model."""
    pts = dict(BASE)
    if pose == "palm":
        extended = (8, 12, 16, 20)
    elif pose == "point":
        extended = (8,)
    elif pose == "peace":
        extended = (8, 12)
    elif pose in ("fist", "thumbs_up"):
        extended = ()
    else:
        extended = (8, 12, 16, 20)

    for tip in (8, 12, 16, 20):
        if tip not in extended:
            pts[tip] = CURL[tip]

    if pose == "thumbs_up":
        pts[4] = (0.32, 0.44)
    else:
        pts[4] = (0.30, 0.62)

    lm = []
    for i in range(21):
        x, y = pts.get(i, (0.5, 0.6))
        lm.append((x + dx, y + dy))
    return Hand(lm)


def feed_engine(engine, frames, start_t):
    """Feeds frames into engine sequentially."""
    actions = []
    t = start_t
    for hl in frames:
        actions += engine.update(hl, t)
        t += DT
    return actions, t


def wake_engine(engine, t=0.0):
    """Wakes gesture engine with open palm hold."""
    acts, t = feed_engine(engine, [[make_hand("palm")]] * int(1.0 * FPS), t)
    assert engine.active, "Engine must be active after wake"
    return t


class MockWin32API:
    """Mock Win32 user32 provider for 100% deterministic window resolution testing."""

    def __init__(self):
        self.cursor_pos = (150, 250)
        self.raw_hwnd = 1001
        self.root_hwnd = 2002
        self.window_title = "Document.txt - Notepad"
        self.process_id = 9999
        self.foreground_hwnd = None
        self.messages_posted = []

    def GetCursorPos(self, pt_byref):
        pt_byref._obj.x = self.cursor_pos[0]
        pt_byref._obj.y = self.cursor_pos[1]
        return True

    def WindowFromPoint(self, pt_val):
        return self.raw_hwnd

    def GetAncestor(self, hwnd, ga_flags):
        if ga_flags == 2:  # GA_ROOT
            return self.root_hwnd
        return hwnd

    def GetWindowTextLengthW(self, hwnd):
        return len(self.window_title)

    def GetWindowTextW(self, hwnd, buf, max_count):
        buf.value = self.window_title
        return len(self.window_title)

    def GetWindowThreadProcessId(self, hwnd, pid_byref):
        pid_byref._obj.value = self.process_id
        return 1

    def SetForegroundWindow(self, hwnd):
        self.foreground_hwnd = hwnd
        return True

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.messages_posted.append((hwnd, msg, wparam, lparam))
        return True


class TestWindowResolver(unittest.TestCase):
    """Tests for Window Under Cursor Resolution (Phase 8 Task 1)."""

    def setUp(self):
        self.mock_api = MockWin32API()
        self.mock_pag = MagicMock()
        self.resolver = WindowResolver(api=self.mock_api, pyautogui_module=self.mock_pag)

    def test_resolve_window_under_cursor_normal_window(self):
        """Verifies accurate extraction of window handle, title, process ID, and coords."""
        res = self.resolver.resolve_window_under_cursor()
        self.assertEqual(res["hwnd"], 2002)
        self.assertEqual(res["raw_hwnd"], 1001)
        self.assertEqual(res["title"], "Document.txt - Notepad")
        self.assertEqual(res["process_id"], 9999)
        self.assertEqual(res["point"], (150, 250))
        self.assertFalse(res["has_unsaved"])
        self.assertTrue(res["is_valid"])

    def test_resolve_window_under_cursor_detects_unsaved_changes(self):
        """Detects unsaved documents via asterisk, untitled, or unsaved indicators in title."""
        for unsaved_title in ["*Document.txt - Notepad", "Untitled - Visual Studio Code", "Report (unsaved)"]:
            self.mock_api.window_title = unsaved_title
            res = self.resolver.resolve_window_under_cursor()
            self.assertTrue(res["has_unsaved"], f"Failed to detect unsaved changes in: {unsaved_title}")

    def test_open_this_activates_and_clicks(self):
        """'open this' brings window to foreground and clicks at cursor position."""
        ok, msg = self.resolver.open_this()
        self.assertTrue(ok)
        self.assertEqual(self.mock_api.foreground_hwnd, 2002)
        self.mock_pag.click.assert_called_with(150, 250, _pause=False)
        self.assertIn("Document.txt - Notepad", msg)

    def test_copy_this_focuses_and_sends_ctrl_c(self):
        """'copy this' focuses window and emits Ctrl+C hotkey."""
        ok, msg = self.resolver.copy_this()
        self.assertTrue(ok)
        self.assertEqual(self.mock_api.foreground_hwnd, 2002)
        self.mock_pag.hotkey.assert_called_with("ctrl", "c", _pause=False)
        self.assertIn("Copied selection", msg)

    def test_close_this_blocks_unsaved_without_confirmation(self):
        """'close this' rejects closing an unsaved document unless explicitly confirmed."""
        self.mock_api.window_title = "*Budget.xlsx - Excel"
        ok, msg = self.resolver.close_this(confirm=False)
        self.assertFalse(ok)
        self.assertIn("unsaved changes", msg)
        self.assertEqual(len(self.mock_api.messages_posted), 0)

    def test_close_this_proceeds_with_confirmation(self):
        """'close this' proceeds to close unsaved document when confirm=True."""
        self.mock_api.window_title = "*Budget.xlsx - Excel"
        ok, msg = self.resolver.close_this(confirm=True)
        self.assertTrue(ok)
        self.assertIn("Closed", msg)
        self.assertEqual(len(self.mock_api.messages_posted), 1)
        self.assertEqual(self.mock_api.messages_posted[0][1], window_resolver.WM_CLOSE)

    def test_close_this_closes_clean_document_immediately(self):
        """'close this' closes saved/clean documents immediately without confirmation."""
        self.mock_api.window_title = "CleanDocument.txt - Notepad"
        ok, msg = self.resolver.close_this(confirm=False)
        self.assertTrue(ok)
        self.assertEqual(len(self.mock_api.messages_posted), 1)
        self.assertEqual(self.mock_api.messages_posted[0][1], window_resolver.WM_CLOSE)

    def test_system_control_delegation_window_under_cursor(self):
        """Verifies system_control module delegates to window_resolver cleanly."""
        import system_control
        info = system_control.resolve_window_under_cursor()
        self.assertIsInstance(info, dict)
        self.assertIn("point", info)
        self.assertIn("has_unsaved", info)


class TestGestureShortcutsAndHUD(unittest.TestCase):
    """Tests for Gesture Shortcuts & HUD Events in gestures.py (Phase 8 Task 2)."""

    def test_vertical_slide_up_increases_volume_and_emits_hud_event(self):
        """Upward hand slide increases volume by +5% and posts 'hud_volume' event to overlay."""
        cfg = Config(gesture_shortcuts_enabled=True, gesture_slide_volume=True)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        # Slide upward (dy decreases: -0.04 per frame over 3 frames -> dy = -0.08 <= -0.06)
        frames = [[make_hand("palm", dy=-0.04 * i)] for i in range(4)]
        with patch("system_control.volume_up", return_value=65) as mock_vol_up:
            acts, t = feed_engine(engine, frames, t)

        mock_vol_up.assert_called_with(steps=5)
        mock_overlay.post.assert_any_call("hud_volume", level=65, delta=5)
        kinds = [a.kind for a in acts]
        self.assertIn("volume_up", kinds)

    def test_vertical_slide_down_decreases_volume_and_emits_hud_event(self):
        """Downward hand slide decreases volume by -5% and posts 'hud_volume' event to overlay."""
        cfg = Config(gesture_shortcuts_enabled=True, gesture_slide_volume=True)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        # Slide downward (dy increases: +0.04 per frame over 3 frames -> dy = 0.08 >= 0.06)
        frames = [[make_hand("palm", dy=0.04 * i)] for i in range(4)]
        with patch("system_control.volume_down", return_value=40) as mock_vol_down:
            acts, t = feed_engine(engine, frames, t)

        mock_vol_down.assert_called_with(steps=5)
        mock_overlay.post.assert_any_call("hud_volume", level=40, delta=-5)
        kinds = [a.kind for a in acts]
        self.assertIn("volume_down", kinds)

    def test_horizontal_swipe_switches_window_and_emits_hud_event(self):
        """Horizontal hand swipe triggers Alt+Tab window switch and posts 'hud_switch_window'."""
        cfg = Config(gesture_shortcuts_enabled=True, gesture_swipe_switch=True)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        # Swipe horizontally (dx increases: +0.04 per frame over 3 frames -> dx = 0.08 >= 0.08)
        frames = [[make_hand("palm", dx=0.04 * i)] for i in range(4)]
        with patch("pyautogui.hotkey") as mock_hotkey, \
             patch("system_control.get_foreground_window_title", return_value="Visual Studio Code"):
            acts, t = feed_engine(engine, frames, t)

        mock_hotkey.assert_called_with("alt", "tab", _pause=False)
        mock_overlay.post.assert_any_call("hud_switch_window", title="Visual Studio Code")
        kinds = [a.kind for a in acts]
        self.assertIn("switch_window", kinds)

    def test_gesture_shortcuts_disabled_setting_suppresses_events(self):
        """When gesture_shortcuts_enabled is False, slides and swipes are ignored."""
        cfg = Config(gesture_shortcuts_enabled=False)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        # Slide upward
        frames = [[make_hand("palm", dy=-0.04 * i)] for i in range(4)]
        acts, t = feed_engine(engine, frames, t)
        mock_overlay.post.assert_not_called()
        self.assertNotIn("volume_up", [a.kind for a in acts])

    def test_slide_volume_disabled_setting_suppresses_volume(self):
        """When gesture_slide_volume is False, vertical slide does not adjust volume."""
        cfg = Config(gesture_shortcuts_enabled=True, gesture_slide_volume=False)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        frames = [[make_hand("palm", dy=-0.04 * i)] for i in range(4)]
        acts, t = feed_engine(engine, frames, t)
        mock_overlay.post.assert_not_called()
        self.assertNotIn("volume_up", [a.kind for a in acts])

    def test_swipe_switch_disabled_setting_suppresses_switch(self):
        """When gesture_swipe_switch is False, horizontal swipe does not switch windows."""
        cfg = Config(gesture_shortcuts_enabled=True, gesture_swipe_switch=False)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        frames = [[make_hand("palm", dx=0.04 * i)] for i in range(4)]
        acts, t = feed_engine(engine, frames, t)
        mock_overlay.post.assert_not_called()
        self.assertNotIn("switch_window", [a.kind for a in acts])


class TestPrivacyLog(unittest.TestCase):
    """Tests for Privacy Log & Cloud Transmission Tracking (Phase 8 Task 3)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.log_file = os.path.join(self.tmp_dir.name, "test_privacy_log.json")
        self.log = PrivacyLog(max_events=50, log_file=self.log_file, auto_sync=True)

    def tearDown(self):
        self.tmp_dir.cleanup()
        reset_privacy_log()

    def test_ring_buffer_bounds_at_50_events(self):
        """PrivacyLog keeps strictly the last 50 events in a FIFO ring buffer."""
        for i in range(65):
            self.log.record(
                destination="Groq LLM",
                payload_summary=f"Query {i}",
                metadata={"index": i},
            )

        self.assertEqual(self.log.count(), 50)
        events = self.log.get_events()
        self.assertEqual(len(events), 50)
        # Oldest kept event should be index 15
        self.assertEqual(events[0]["metadata"]["index"], 15)
        # Most recent event should be index 64
        self.assertEqual(events[-1]["metadata"]["index"], 64)

    def test_json_persistence_and_reload(self):
        """Verifies disk sync and re-reading events from JSON storage."""
        self.log.record("Groq STT", "Audio 16000Hz")
        self.log.record("Open-Meteo", "Weather forecast for NYC")

        self.assertTrue(os.path.exists(self.log_file))

        # Create fresh reader on the same file
        reader = PrivacyLog(max_events=50, log_file=self.log_file, auto_sync=False)
        self.assertEqual(reader.count(), 2)
        evs = reader.get_events()
        self.assertEqual(evs[0]["destination"], "Groq STT")
        self.assertEqual(evs[1]["destination"], "Open-Meteo")

    def test_clear_wipes_memory_and_file(self):
        """clear() flushes in-memory events and empties JSON file."""
        self.log.record("Groq LLM", "Hello Jarvis")
        self.assertEqual(self.log.count(), 1)
        self.log.clear()
        self.assertEqual(self.log.count(), 0)

        reader = PrivacyLog(max_events=50, log_file=self.log_file, auto_sync=False)
        self.assertEqual(reader.count(), 0)

    def test_thread_safety_concurrent_records(self):
        """Validates thread-safe recording across multiple concurrent threads."""
        threads = []
        def worker(tid):
            for j in range(10):
                self.log.record(
                    destination=f"Dest_{tid}",
                    payload_summary=f"Thread {tid} payload {j}",
                )

        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(self.log.count(), 50)

    def test_ai_module_wires_into_privacy_log(self):
        """Verifies that Groq AI calls record external transmissions to privacy log."""
        import ai
        test_log_file = os.path.join(self.tmp_dir.name, "ai_privacy.json")
        p_log = get_privacy_log(test_log_file)
        p_log.clear()

        client = ai.GroqClient(api_key="gsk_dummy_test_key_for_privacy_audit")

        # 1. Groq STT
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"text": "Hello Jarvis"}
            client.transcribe(b"dummy_wav_bytes")

        # 2. Groq LLM
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "Yes, sir."}}]
            }
            client.chat([{"role": "user", "content": "Status report, Jarvis."}])

        # 3. Groq Vision
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "I see a keyboard."}}]
            }
            client.describe_image(b"dummy_jpeg_bytes", prompt="What is this?")

        events = p_log.get_events()
        destinations = [e["destination"] for e in events]
        self.assertIn("Groq STT", destinations)
        self.assertIn("Groq LLM", destinations)
        self.assertIn("Groq Vision", destinations)

    def test_weather_client_wires_into_privacy_log(self):
        """Verifies that Open-Meteo weather calls record transmissions to privacy log."""
        from weather_client import WeatherClient
        test_log_file = os.path.join(self.tmp_dir.name, "weather_privacy.json")
        p_log = get_privacy_log(test_log_file)
        p_log.clear()

        mock_fetcher = MagicMock(return_value={
            "results": [{"latitude": 40.71, "longitude": -74.01, "name": "New York"}],
            "current": {"temperature_2m": 22.0, "relative_humidity_2m": 50, "apparent_temperature": 22.0, "is_day": 1, "precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 5.0},
            "hourly": {"time": [], "temperature_2m": [], "weather_code": []}
        })

        client = WeatherClient(http_fetcher=mock_fetcher)
        client.geocode("New York")
        client.get_weather(40.71, -74.01, city="New York")

        events = p_log.get_events()
        destinations = [e["destination"] for e in events]
        self.assertIn("Open-Meteo", destinations)
        self.assertGreaterEqual(destinations.count("Open-Meteo"), 2)


class TestConfigSettings(unittest.TestCase):
    """Tests for Configuration Settings updates (Phase 8 Task 4)."""

    def test_gesture_settings_defaults(self):
        """Verifies default values for gesture settings."""
        cfg = Config()
        self.assertTrue(cfg.gesture_shortcuts_enabled)
        self.assertTrue(cfg.gesture_slide_volume)
        self.assertTrue(cfg.gesture_swipe_switch)

    def test_gesture_settings_persistence(self):
        """Verifies gesture shortcut flags persist to and load from JSON."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "jarvis_config.json")
            cfg = Config()
            cfg.gesture_shortcuts_enabled = False
            cfg.gesture_slide_volume = False
            cfg.gesture_swipe_switch = True
            save_config(cfg, cfg_path)

            loaded = load_config(cfg_path)
            self.assertFalse(loaded.gesture_shortcuts_enabled)
            self.assertFalse(loaded.gesture_slide_volume)
            self.assertTrue(loaded.gesture_swipe_switch)


class TestVoiceAndDecisionIntegration(unittest.TestCase):
    """Tests for Voice Commands & Decision Engine integration (Phase 8 Task 5)."""

    def test_voice_commands_open_this_and_copy_this(self):
        """Verifies 'open this' and 'copy this' trigger window_resolver actions."""
        import commands
        ctx = MagicMock()
        with patch("window_resolver.open_this", return_value=(True, "Opened target under cursor, sir.")) as mock_open, \
             patch("window_resolver.copy_this", return_value=(True, "Copied selection to clipboard, sir.")) as mock_copy:
            res_open = commands.run_command("open this", ctx)
            self.assertIn("Opened target", res_open)
            mock_open.assert_called_once()

            res_copy = commands.run_command("copy this", ctx)
            self.assertIn("Copied selection", res_copy)
            mock_copy.assert_called_once()

    def test_voice_command_close_this_confirmation_flow(self):
        """'close this' on unsaved document triggers confirmation prompt and verifies yes."""
        import commands
        commands._PENDING_CONFIRMATION = None
        ctx = MagicMock()

        with patch("window_resolver.close_this") as mock_close:
            # First attempt: unsaved document warns and sets pending state
            mock_close.return_value = (False, "Document 'Notes.txt' has unsaved changes. Please confirm to close.")
            warn_reply = commands.run_command("close this", ctx)
            self.assertIn("unsaved changes", warn_reply)
            self.assertEqual(commands._PENDING_CONFIRMATION, "close_this")

            # User confirms
            mock_close.return_value = (True, "Closed 'Notes.txt', sir.")
            confirm_reply = commands.run_command("yes confirm", ctx)
            self.assertIn("Closed", confirm_reply)
            self.assertIsNone(commands._PENDING_CONFIRMATION)

    def test_decision_engine_recognizes_phase8_intents(self):
        """Verifies DecisionEngine resolves 'open this', 'copy this', and 'close this'."""
        import decision_engine
        engine = decision_engine.get_decision_engine()

        act, score = engine.decide("open link under cursor")
        self.assertEqual(act, "open_this")
        self.assertGreater(score, 0.40)

        act, score = engine.decide("copy text under cursor")
        self.assertEqual(act, "copy_this")
        self.assertGreater(score, 0.40)

        act, score = engine.decide("close window under cursor")
        self.assertEqual(act, "close_this")
        self.assertGreater(score, 0.40)

    def test_ai_whitelist_actions_phase8(self):
        """Verifies open_this, copy_this, and close_this are secured in WHITELIST_ACTIONS."""
        import ai
        self.assertIn("open_this", ai.WHITELIST_ACTIONS)
        self.assertIn("copy_this", ai.WHITELIST_ACTIONS)
        self.assertIn("close_this", ai.WHITELIST_ACTIONS)

        ctx = MagicMock()
        with patch("window_resolver.open_this", return_value=(True, "Opened target")) as m_open, \
             patch("window_resolver.copy_this", return_value=(True, "Copied text")) as m_copy, \
             patch("window_resolver.close_this", return_value=(True, "Closed window")) as m_close:

            ok, msg = ai.execute_whitelisted_action("open_this", {}, ctx)
            self.assertTrue(ok)
            m_open.assert_called_once()

            ok, msg = ai.execute_whitelisted_action("copy_this", {}, ctx)
            self.assertTrue(ok)
            m_copy.assert_called_once()

            ok, msg = ai.execute_whitelisted_action("close_this", {"confirm": True}, ctx)
            self.assertTrue(ok)
            m_close.assert_called_with(confirm=True)


if __name__ == "__main__":
    unittest.main()
