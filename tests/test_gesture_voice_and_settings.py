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

    @staticmethod
    def _flick(axis, sign, base=0.0, n=6, step=0.05):
        """Genuine flick: 0.25 displacement over ~0.17 s at 30 fps."""
        k = "dx" if axis == "x" else "dy"
        return [[make_hand("palm", **{k: base + sign * step * i})] for i in range(n)]

    @staticmethod
    def _slow(axis, sign, base=0.0):
        """Old-style slow motion: 0.08 over 3 frames. No longer a shortcut."""
        k = "dx" if axis == "x" else "dy"
        return [[make_hand("palm", **{k: base + sign * 0.04 * i})] for i in range(4)]

    def _assert_flick_extras(self, acts, nframes):
        kinds = [a.kind for a in acts]
        self.assertIn("rewind", kinds)
        self.assertGreaterEqual(kinds.count("move"), nframes)

    def _assert_nothing_fired(self, acts, mock_overlay):
        kinds = [a.kind for a in acts]
        for k in ("volume_up", "volume_down", "switch_window", "island_page_step", "rewind"):
            self.assertNotIn(k, kinds)
        mock_overlay.post.assert_not_called()

    def _engine(self, **kw):
        mock_overlay = MagicMock()
        engine = GestureEngine(Config(gesture_shortcuts_enabled=True, **kw), overlay=mock_overlay)
        return engine, mock_overlay, wake_engine(engine, 0.0)

    def test_vertical_flick_up_increases_volume_and_emits_hud_event(self):
        """Upward flick increases volume by +5%, posts 'hud_volume', emits rewind and a move per frame."""
        engine, mock_overlay, t = self._engine(gesture_slide_volume=True)
        frames = self._flick("y", -1)
        with patch("system_control.volume_up", return_value=65) as mock_vol_up:
            acts, t = feed_engine(engine, frames, t)
        mock_vol_up.assert_called_with(steps=5)
        mock_overlay.post.assert_any_call("hud_volume", level=65, delta=5)
        self.assertIn("volume_up", [a.kind for a in acts])
        self._assert_flick_extras(acts, len(frames))

    def test_vertical_flick_down_decreases_volume_and_emits_hud_event(self):
        """Downward flick decreases volume by -5%, posts 'hud_volume', emits rewind and a move per frame."""
        engine, mock_overlay, t = self._engine(gesture_slide_volume=True)
        frames = self._flick("y", +1)
        with patch("system_control.volume_down", return_value=40) as mock_vol_down:
            acts, t = feed_engine(engine, frames, t)
        mock_vol_down.assert_called_with(steps=5)
        mock_overlay.post.assert_any_call("hud_volume", level=40, delta=-5)
        self.assertIn("volume_down", [a.kind for a in acts])
        self._assert_flick_extras(acts, len(frames))

    def test_slow_vertical_slide_no_longer_changes_volume(self):
        """Old slow 0.08-over-3-frames slide (up or down) triggers nothing."""
        for sign in (-1, +1):
            engine, mock_overlay, t = self._engine(gesture_slide_volume=True)
            with patch("system_control.volume_up") as up, patch("system_control.volume_down") as down:
                acts, t = feed_engine(engine, self._slow("y", sign), t)
            up.assert_not_called()
            down.assert_not_called()
            self._assert_nothing_fired(acts, mock_overlay)
            self.assertGreaterEqual([a.kind for a in acts].count("move"), 4)

    def test_horizontal_flick_right_switches_window_and_emits_hud_event(self):
        """Right flick sends Alt+Tab, posts 'hud_switch_window', emits rewind and a move per frame."""
        engine, mock_overlay, t = self._engine(gesture_swipe_switch=True)
        frames = self._flick("x", +1)
        with patch("pyautogui.hotkey") as mock_hotkey, \
             patch("system_control.get_foreground_window_title", return_value="Visual Studio Code"):
            acts, t = feed_engine(engine, frames, t)
        mock_hotkey.assert_called_with("alt", "tab", _pause=False)
        mock_overlay.post.assert_any_call("hud_switch_window", title="Visual Studio Code")
        self.assertIn("switch_window", [a.kind for a in acts])
        self._assert_flick_extras(acts, len(frames))

    def test_horizontal_flick_left_sends_alt_shift_tab(self):
        """Left flick sends Alt+Shift+Tab."""
        engine, mock_overlay, t = self._engine(gesture_swipe_switch=True)
        frames = self._flick("x", -1)
        with patch("pyautogui.hotkey") as mock_hotkey, \
             patch("system_control.get_foreground_window_title", return_value="Visual Studio Code"):
            acts, t = feed_engine(engine, frames, t)
        mock_hotkey.assert_called_with("alt", "shift", "tab", _pause=False)
        mock_overlay.post.assert_any_call("hud_switch_window", title="Visual Studio Code")
        self.assertIn("switch_window", [a.kind for a in acts])
        self._assert_flick_extras(acts, len(frames))

    def test_slow_horizontal_swipe_no_longer_switches_window(self):
        """Old slow 0.08-over-3-frames swipe (either direction) triggers nothing."""
        for sign in (-1, +1):
            engine, mock_overlay, t = self._engine(gesture_swipe_switch=True)
            with patch("pyautogui.hotkey") as mock_hotkey:
                acts, t = feed_engine(engine, self._slow("x", sign), t)
            mock_hotkey.assert_not_called()
            self._assert_nothing_fired(acts, mock_overlay)
            self.assertGreaterEqual([a.kind for a in acts].count("move"), 4)

    def test_gesture_shortcuts_disabled_setting_suppresses_events(self):
        """When gesture_shortcuts_enabled is False, slides and swipes are ignored."""
        cfg = Config(gesture_shortcuts_enabled=False)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        # Slide upward
        frames = [[make_hand("palm", dy=-0.05 * i)] for i in range(6)]
        acts, t = feed_engine(engine, frames, t)
        mock_overlay.post.assert_not_called()
        self.assertNotIn("volume_up", [a.kind for a in acts])

    def test_slide_volume_disabled_setting_suppresses_volume(self):
        """When gesture_slide_volume is False, vertical slide does not adjust volume."""
        cfg = Config(gesture_shortcuts_enabled=True, gesture_slide_volume=False)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        frames = [[make_hand("palm", dy=-0.05 * i)] for i in range(6)]
        acts, t = feed_engine(engine, frames, t)
        mock_overlay.post.assert_not_called()
        self.assertNotIn("volume_up", [a.kind for a in acts])

    def test_swipe_switch_disabled_setting_suppresses_switch(self):
        """When gesture_swipe_switch is False, horizontal swipe does not switch windows."""
        cfg = Config(gesture_shortcuts_enabled=True, gesture_swipe_switch=False)
        mock_overlay = MagicMock()
        engine = GestureEngine(cfg, overlay=mock_overlay)
        t = wake_engine(engine, 0.0)

        frames = [[make_hand("palm", dx=0.05 * i)] for i in range(6)]
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


class TestDynamicIslandVoiceIntents(unittest.TestCase):
    """Phase 4: Unit tests for Dynamic Island voice intents in commands.py and voice.py."""

    def setUp(self):
        self.ctx = MagicMock()
        self.cfg = Config()
        self.ctx.cfg = self.cfg
        self.ctx.engine.cfg = self.cfg

    def test_next_page_and_next_card(self):
        """'next page' and 'next card' trigger next island page (direction=1)."""
        import commands
        r1 = commands.run_command("next page", self.ctx)
        self.assertIn("Next", r1)
        self.ctx.overlay.post.assert_any_call("island_page_step", direction=1)

        self.ctx.overlay.reset_mock()
        r2 = commands.run_command("next card", self.ctx)
        self.assertIn("Next", r2)
        self.ctx.overlay.post.assert_any_call("island_page_step", direction=1)

    def test_prev_page_variants(self):
        """'previous page', 'prev page', and 'last page' trigger previous island page (direction=-1)."""
        import commands
        for phrase in ("previous page", "prev page", "last page", "previous card", "prev card"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("Previous", reply)
            self.ctx.overlay.post.assert_any_call("island_page_step", direction=-1)

    def test_show_weather_page(self):
        """'show weather' and 'weather page' trigger island_show_page(page='weather')."""
        import commands
        for phrase in ("show weather", "weather page", "weather card"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("weather", reply.lower())
            self.ctx.overlay.post.assert_any_call("island_show_page", page="weather")

    def test_show_hardware_page(self):
        """'show hardware', 'hardware page', and 'show specs' trigger island_show_page(page='hardware')."""
        import commands
        for phrase in ("show hardware", "hardware page", "show specs"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("hardware", reply.lower())
            self.ctx.overlay.post.assert_any_call("island_show_page", page="hardware")

    def test_show_media_page(self):
        """'show media' and 'media page' trigger island_show_page(page='media')."""
        import commands
        for phrase in ("show media", "media page"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("media", reply.lower())
            self.ctx.overlay.post.assert_any_call("island_show_page", page="media")

    def test_show_calendar_page(self):
        """'show calendar' and 'calendar page' trigger island_show_page(page='calendar')."""
        import commands
        for phrase in ("show calendar", "calendar page"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("calendar", reply.lower())
            self.ctx.overlay.post.assert_any_call("island_show_page", page="calendar")

    def test_show_jarvis_page(self):
        """'show jarvis' and 'jarvis page' trigger island_show_page(page='jarvis')."""
        import commands
        for phrase in ("show jarvis", "jarvis page"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("jarvis", reply.lower())
            self.ctx.overlay.post.assert_any_call("island_show_page", page="jarvis")

    def test_expand_and_collapse_island(self):
        """'expand island' / 'open island' expands; 'collapse island' / 'close island' collapses."""
        import commands
        # Expand
        for phrase in ("expand island", "open island"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("Expanding", reply)
            self.ctx.overlay.post.assert_any_call("island_expand")

        # Collapse
        for phrase in ("collapse island", "close island"):
            self.ctx.overlay.reset_mock()
            reply = commands.run_command(phrase, self.ctx)
            self.assertIn("Collapsing", reply)
            self.ctx.overlay.post.assert_any_call("island_collapse")

    def test_voice_process_direct_intercept(self):
        """Voice._process direct intercept for Dynamic Island voice navigation."""
        from voice import Voice, intercept_island_voice
        stop_evt = threading.Event()
        v = Voice(self.cfg, self.ctx, stop_evt)
        v.listening_until = time.time() + 10.0

        v._process("next page")
        self.ctx.overlay.post.assert_any_call("island_page_step", direction=1)
        self.ctx.say.assert_called_with("Next card, sir.")

        self.ctx.overlay.reset_mock()
        self.ctx.say.reset_mock()
        v.listening_until = time.time() + 10.0
        v._process("show calendar")
        self.ctx.overlay.post.assert_any_call("island_show_page", page="calendar")
        self.ctx.say.assert_called_with("Displaying calendar schedule, sir.")


class TestDynamicIslandGestureHooks(unittest.TestCase):
    """Phase 4: Unit tests for Dynamic Island gesture hooks in gestures.py and controller.py."""

    def _island_run(self, base, sign, n=6, step=0.05):
        mock_overlay = MagicMock()
        engine = GestureEngine(Config(gesture_shortcuts_enabled=True), overlay=mock_overlay)
        t = wake_engine(engine, 0.0)
        frames = [[make_hand("palm", dy=base + sign * step * i)] for i in range(n)]
        acts, t = feed_engine(engine, frames, t + 1.0)
        return acts, mock_overlay, len(frames)

    def test_vertical_flick_down_in_island_area_steps_next_page(self):
        """Down flick while hand is in top-screen area (hy <= 0.38) triggers island_page_step(1)."""
        acts, mock_overlay, n = self._island_run(-0.45, +1)
        mock_overlay.post.assert_any_call("island_page_step", direction=1)
        kinds = [a.kind for a in acts]
        self.assertIn("island_page_step", kinds)
        self.assertIn("rewind", kinds)
        self.assertGreaterEqual(kinds.count("move"), n)
        self.assertEqual(next(a for a in acts if a.kind == "island_page_step").n, 1)

    def test_vertical_flick_up_in_island_area_steps_prev_page(self):
        """Up flick while hand is in top-screen area (hy <= 0.38) triggers island_page_step(-1)."""
        acts, mock_overlay, n = self._island_run(-0.35, -1)
        mock_overlay.post.assert_any_call("island_page_step", direction=-1)
        kinds = [a.kind for a in acts]
        self.assertIn("island_page_step", kinds)
        self.assertIn("rewind", kinds)
        self.assertGreaterEqual(kinds.count("move"), n)
        self.assertEqual(next(a for a in acts if a.kind == "island_page_step").n, -1)

    def test_slow_vertical_swipe_in_island_area_does_not_step_page(self):
        """Old slow 0.08-over-3-frames swipe in the island area (either direction) no longer steps."""
        for base, sign in ((-0.45, +1), (-0.35, -1)):
            acts, mock_overlay, n = self._island_run(base, sign, n=4, step=0.04)
            kinds = [a.kind for a in acts]
            self.assertNotIn("island_page_step", kinds)
            self.assertNotIn("rewind", kinds)
            mock_overlay.post.assert_not_called()
            self.assertGreaterEqual(kinds.count("move"), n)

    def test_controller_island_page_step_handling(self):
        """Controller (worker thread) only posts island_page_step; the GUI-side drain moves the island page."""
        import queue
        from types import SimpleNamespace
        from controller import Controller
        from overlay import Overlay
        cfg = Config()
        q = queue.Queue()
        mock_overlay = MagicMock()
        mock_overlay.post.side_effect = lambda kind, **d: q.put((kind, d))
        island_mock = MagicMock()
        island_mock.expanded_card = "media"
        island_mock.card_pages = ["media", "time", "weather", "hardware", "jarvis"]
        mock_overlay.island = island_mock

        ctrl = Controller(cfg, overlay=mock_overlay)
        try:
            ctrl.handle(Action("island_page_step", n=1))
            mock_overlay.post.assert_any_call("island_page_step", direction=1)
            ctrl.handle(Action("island_page_step", n=-1))
            mock_overlay.post.assert_any_call("island_page_step", direction=-1)
            # Worker thread must not touch island widget methods directly.
            island_mock.expand_card.assert_not_called()
            island_mock.page_step.assert_not_called()
            island_mock.show_page.assert_not_called()
        finally:
            ctrl.shutdown()

        # GUI side: Overlay._drain routes the queued events to island.page_step.
        pages = ["media", "time", "weather", "hardware", "jarvis"]
        state = {"cur": "media"}

        def page_step(direction=1):
            state["cur"] = pages[(pages.index(state["cur"]) + direction) % len(pages)]
            return state["cur"]

        gui_island = MagicMock()
        gui_island.page_step.side_effect = page_step
        gui = SimpleNamespace(q=q, island=gui_island)
        steps = [k for k, _ in list(q.queue) if k == "island_page_step"]
        self.assertGreaterEqual(len(steps), 2)
        # Drain only the first (direction=+1) event first.
        first = q.get_nowait()
        q2 = queue.Queue()
        q2.put(first)
        gui.q = q2
        Overlay._drain(gui, time.time())
        gui_island.page_step.assert_called_once_with(1)
        self.assertEqual(state["cur"], "time")
        # Then the direction=-1 event steps back.
        gui.q = q
        Overlay._drain(gui, time.time())
        gui_island.page_step.assert_called_with(-1)
        self.assertEqual(state["cur"], "media")

    def test_dwell_over_island_buttons_triggers_click(self):
        """Dwell over interactive island buttons (prev, play_pause, next, page dots, pause tracking) triggers click."""
        from controller import Controller
        cfg = Config()
        mock_overlay = MagicMock()
        ctrl = Controller(cfg, overlay=mock_overlay)
        try:
            # Register island button hit zones
            ctrl.island_buttons["prev"] = (100, 100, 140, 140)
            ctrl.island_buttons["play_pause"] = (150, 100, 190, 140)
            ctrl.island_buttons["next"] = (200, 100, 240, 140)
            ctrl.island_buttons["dot_2"] = (300, 100, 320, 120)
            ctrl.island_buttons["pause_tracking_toggle"] = (50, 20, 90, 40)

            buttons_tested = [
                ((120, 120), "prev"),
                ((170, 120), "play_pause"),
                ((220, 120), "next"),
                ((310, 110), "dot_2"),
                ((70, 30), "pause_tracking_toggle"),
            ]

            for (bx, by), btn_name in buttons_tested:
                ctrl.cur_x, ctrl.cur_y = float(bx), float(by)
                mock_overlay.post.reset_mock()
                with patch("pyautogui.click") as mock_click:
                    ctrl._handle_dwell_action("click")
                    mock_click.assert_called_once()
                    mock_overlay.post.assert_any_call("island_button_click", button=btn_name)
        finally:
            ctrl.shutdown()

    def test_fist_click_lookback_buffer_and_0px_drift_invariant(self):
        """Fist-click over island button snaps to 200ms lookback buffer position with 0 px drift."""
        from controller import Controller
        cfg = Config()
        cfg.cursor_lookback_ms = 200.0
        mock_overlay = MagicMock()
        ctrl = Controller(cfg, overlay=mock_overlay)
        try:
            ctrl.island_buttons["play_pause"] = (200, 200, 260, 260)

            # 200 ms ago, cursor was steadily aiming at (230, 230)
            t0 = time.perf_counter()
            ctrl.history.append(t0 - 0.20, 230.0, 230.0)
            # Drift occurs during fist curling -> moves off-button to (290, 290)
            ctrl.cur_x, ctrl.cur_y = 290.0, 290.0
            ctrl.target_x, ctrl.target_y = 290.0, 290.0
            ctrl.history.append(t0, 290.0, 290.0)

            with patch("pyautogui.click") as mock_click, patch.object(ctrl, "_move") as mock_move:
                ctrl.handle(Action("click"))
                # Precision invariant verification: exactly 0 px pre-click drift
                self.assertEqual(ctrl.cur_x, 230.0)
                self.assertEqual(ctrl.cur_y, 230.0)
                mock_move.assert_called_with(230, 230)
                mock_click.assert_called_once()
                mock_overlay.post.assert_any_call("island_button_click", button="play_pause")
        finally:
            ctrl.shutdown()


if __name__ == "__main__":
    unittest.main()
