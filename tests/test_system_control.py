"""
tests/test_system_control.py — Comprehensive Unit Tests for Phase 5 System Control Adapter.

Covers:
  - Audio: volume_up, volume_down, set_volume, get_volume, mute (pycaw + fallback + mock)
  - Display: brightness_up, brightness_down, set_brightness, get_brightness, toggle_night_light (WMI / CIM + fallback)
  - Window Management: snap_window, minimize_all, switch_to_app, move_to_other_monitor, close_window (with unsaved doc detection)
  - Power & Session: lock_workstation, sleep_system, take_screenshot, toggle_wifi, toggle_bluetooth, toggle_do_not_disturb
  - Destructive Actions Gate: shutdown_system and restart_system requiring confirmation
  - Whitelist Actions & Schema validation in ai.py
  - System 1 Reflex Intents in commands.py and decision_engine.py

Strictly NO Qt imports. 100% unit-tested with mocks.
"""

import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import ai
from ai import (
    ACTION_SCHEMAS,
    WHITELIST_ACTIONS,
    execute_whitelisted_action,
    validate_action_arguments,
)
from commands import run_command
from config import Config
from decision_engine import get_decision_engine, evaluate_decision
import system_control
from system_control import (
    FallbackAudioBackend,
    FallbackDisplayBackend,
    MockAudioBackend,
    MockDisplayBackend,
    MockPowerSessionBackend,
    MockWindowBackend,
    PycawAudioBackend,
    SystemControlAdapter,
    WindowsDisplayBackend,
    WindowsPowerSessionBackend,
    WindowsWindowBackend,
    get_system_control,
    reset_system_control,
    set_system_control,
)


class TestAudioControl(unittest.TestCase):
    """Tests for Audio subsystem backends and operations."""

    def test_mock_audio_volume_operations(self):
        backend = MockAudioBackend(volume=50, muted=False)
        self.assertEqual(backend.get_volume(), 50)

        # Volume up default and custom
        self.assertEqual(backend.volume_up(5), 55)
        self.assertEqual(backend.volume_up(20), 75)
        self.assertEqual(backend.volume_up(50), 100)  # clamped at 100

        # Volume down default and custom
        self.assertEqual(backend.volume_down(10), 90)
        self.assertEqual(backend.volume_down(50), 40)
        self.assertEqual(backend.volume_down(60), 0)   # clamped at 0

        # Set volume with bounds clamping
        self.assertEqual(backend.set_volume(80), 80)
        self.assertEqual(backend.set_volume(150), 100)
        self.assertEqual(backend.set_volume(-20), 0)

    def test_mock_audio_mute(self):
        backend = MockAudioBackend(volume=50, muted=False)
        self.assertFalse(backend.is_muted())

        # Toggle mute
        self.assertTrue(backend.mute(toggle=True))
        self.assertTrue(backend.is_muted())
        self.assertFalse(backend.mute(toggle=True))
        self.assertFalse(backend.is_muted())

        # Explicit mute
        self.assertTrue(backend.mute(toggle=False))
        self.assertTrue(backend.is_muted())

    @patch("pyautogui.press")
    def test_fallback_audio_backend(self, mock_press):
        backend = FallbackAudioBackend(initial_volume=40, initial_muted=False)
        self.assertEqual(backend.get_volume(), 40)

        new_vol = backend.volume_up(10)
        self.assertEqual(new_vol, 50)
        mock_press.assert_called_with("volumeup", presses=5, _pause=False)

        new_vol = backend.volume_down(15)
        self.assertEqual(new_vol, 35)
        mock_press.assert_called_with("volumedown", presses=7, _pause=False)

        muted = backend.mute(toggle=True)
        self.assertTrue(muted)
        mock_press.assert_called_with("volumemute", _pause=False)

    def test_pycaw_audio_backend_with_mocked_endpoint(self):
        backend = PycawAudioBackend()
        mock_endpoint = MagicMock()
        mock_endpoint.GetMasterVolumeLevelScalar.return_value = 0.65
        mock_endpoint.GetMute.return_value = 0

        with patch.object(backend, "_get_endpoint", return_value=mock_endpoint):
            # get_volume converts 0.65 -> 65
            self.assertEqual(backend.get_volume(), 65)

            # set_volume converts 80 -> 0.8
            self.assertEqual(backend.set_volume(80), 80)
            mock_endpoint.SetMasterVolumeLevelScalar.assert_called_with(0.8, None)

            # volume_up
            mock_endpoint.GetMasterVolumeLevelScalar.return_value = 0.8
            self.assertEqual(backend.volume_up(10), 90)

            # mute
            self.assertTrue(backend.mute(toggle=True))
            mock_endpoint.SetMute.assert_called_with(1, None)

    def test_pycaw_audio_backend_fallback_on_exception(self):
        backend = PycawAudioBackend()
        with patch.object(backend, "_get_endpoint", side_effect=Exception("COM Endpoint failure")):
            # Must not crash, should fall back gracefully
            vol = backend.get_volume()
            self.assertIsInstance(vol, int)
            new_vol = backend.set_volume(70)
            self.assertEqual(new_vol, 70)


class TestDisplayControl(unittest.TestCase):
    """Tests for Display subsystem backends and operations."""

    def test_mock_display_brightness_operations(self):
        backend = MockDisplayBackend(brightness=70, night_light=False)
        self.assertEqual(backend.get_brightness(), 70)

        # Brightness up with clamping
        self.assertEqual(backend.brightness_up(10), 80)
        self.assertEqual(backend.brightness_up(30), 100)

        # Brightness down with clamping
        self.assertEqual(backend.brightness_down(20), 80)
        self.assertEqual(backend.brightness_down(90), 0)

        # Set brightness
        self.assertEqual(backend.set_brightness(65), 65)
        self.assertEqual(backend.set_brightness(150), 100)
        self.assertEqual(backend.set_brightness(-10), 0)

        # Night light toggle
        self.assertTrue(backend.toggle_night_light())
        self.assertFalse(backend.toggle_night_light())

    def test_fallback_display_backend(self):
        backend = FallbackDisplayBackend(initial_brightness=50, initial_night_light=False)
        self.assertEqual(backend.get_brightness(), 50)
        self.assertEqual(backend.brightness_up(15), 65)
        self.assertEqual(backend.brightness_down(25), 40)
        self.assertEqual(backend.set_brightness(85), 85)
        self.assertTrue(backend.toggle_night_light())
        self.assertFalse(backend.toggle_night_light())

    @patch("subprocess.run")
    def test_windows_display_backend_powershell_cim_read(self, mock_run):
        backend = WindowsDisplayBackend()
        mock_run.return_value = MagicMock(returncode=0, stdout="85\n")

        with patch("sys.platform", "win32"):
            b = backend.get_brightness()
            self.assertEqual(b, 85)

    @patch("subprocess.run")
    def test_windows_display_backend_powershell_cim_write(self, mock_run):
        backend = WindowsDisplayBackend()
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        with patch("sys.platform", "win32"):
            res = backend.set_brightness(90)
            self.assertEqual(res, 90)
            self.assertTrue(mock_run.called)


class TestWindowManagement(unittest.TestCase):
    """Tests for Window management, snapping, and unsaved changes safety."""

    def test_mock_window_operations(self):
        backend = MockWindowBackend(foreground_title="Code.py - Visual Studio Code")
        self.assertTrue(backend.snap_window("left"))
        self.assertEqual(backend.last_snap, "left")

        self.assertTrue(backend.snap_window("right"))
        self.assertEqual(backend.last_snap, "right")

        self.assertTrue(backend.snap_window("up"))
        self.assertTrue(backend.snap_window("down"))

        self.assertTrue(backend.minimize_all())
        self.assertTrue(backend.all_minimized)

        self.assertTrue(backend.switch_to_app("chrome"))
        self.assertIn("chrome", backend.switched_apps)

        self.assertTrue(backend.move_to_other_monitor())
        self.assertTrue(backend.moved_monitor)

    def test_close_window_clean_document_closes_without_confirm(self):
        backend = MockWindowBackend(foreground_title="Document.txt - Notepad")
        ok, msg = backend.close_window(confirm=False)
        self.assertTrue(ok)
        self.assertEqual(msg, "Window closed.")
        self.assertTrue(backend.closed)

    def test_close_window_unsaved_asterisk_requires_confirmation(self):
        backend = MockWindowBackend(foreground_title="*Document.txt - Notepad")
        ok, msg = backend.close_window(confirm=False)
        self.assertFalse(ok)
        self.assertIn("unsaved changes", msg)
        self.assertFalse(backend.closed)

        # With confirm=True, it succeeds
        ok2, msg2 = backend.close_window(confirm=True)
        self.assertTrue(ok2)
        self.assertEqual(msg2, "Window closed.")
        self.assertTrue(backend.closed)

    def test_close_window_unsaved_untitled_requires_confirmation(self):
        backend = MockWindowBackend(foreground_title="Untitled - Notepad")
        ok, msg = backend.close_window(confirm=False)
        self.assertFalse(ok)
        self.assertIn("unsaved changes", msg)
        self.assertFalse(backend.closed)

    @patch("pyautogui.hotkey")
    def test_windows_window_backend_snap(self, mock_hotkey):
        backend = WindowsWindowBackend()
        self.assertTrue(backend.snap_window("left"))
        mock_hotkey.assert_called_with("win", "left", _pause=False)

        self.assertTrue(backend.snap_window("right"))
        mock_hotkey.assert_called_with("win", "right", _pause=False)

        self.assertTrue(backend.snap_window("up"))
        mock_hotkey.assert_called_with("win", "up", _pause=False)

        self.assertTrue(backend.snap_window("down"))
        mock_hotkey.assert_called_with("win", "down", _pause=False)

        self.assertFalse(backend.snap_window("invalid_direction"))


class TestPowerAndSession(unittest.TestCase):
    """Tests for Power, Lock, Radio toggles, and Destructive Confirmation Gate."""

    def test_mock_power_operations(self):
        backend = MockPowerSessionBackend()
        self.assertTrue(backend.lock_workstation())
        self.assertTrue(backend.locked)

        self.assertTrue(backend.sleep_system())
        self.assertTrue(backend.slept)

        p = backend.take_screenshot("custom_shot.png")
        self.assertEqual(p, "custom_shot.png")
        self.assertIn("custom_shot.png", backend.screenshots)

        ok_wifi, _ = backend.toggle_wifi()
        self.assertTrue(ok_wifi)
        self.assertTrue(backend.wifi_toggled)

        ok_bt, _ = backend.toggle_bluetooth()
        self.assertTrue(ok_bt)
        self.assertTrue(backend.bluetooth_toggled)

        ok_dnd, _ = backend.toggle_do_not_disturb()
        self.assertTrue(ok_dnd)
        self.assertTrue(backend.dnd_toggled)

    def test_destructive_gate_shutdown_requires_confirmation(self):
        backend = MockPowerSessionBackend()
        # Unconfirmed MUST fail and return confirmation prompt
        ok, msg = backend.shutdown_system(confirmed=False)
        self.assertFalse(ok)
        self.assertIn("requires confirmation", msg)
        self.assertFalse(backend.shutdown_executed)

        # Confirmed succeeds
        ok2, msg2 = backend.shutdown_system(confirmed=True)
        self.assertTrue(ok2)
        self.assertTrue(backend.shutdown_executed)
        self.assertIn("initiated", msg2)

    def test_destructive_gate_restart_requires_confirmation(self):
        backend = MockPowerSessionBackend()
        # Unconfirmed MUST fail and return confirmation prompt
        ok, msg = backend.restart_system(confirmed=False)
        self.assertFalse(ok)
        self.assertIn("requires confirmation", msg)
        self.assertFalse(backend.restart_executed)

        # Confirmed succeeds
        ok2, msg2 = backend.restart_system(confirmed=True)
        self.assertTrue(ok2)
        self.assertTrue(backend.restart_executed)
        self.assertIn("initiated", msg2)


class TestSystemControlAdapterAndRegistry(unittest.TestCase):
    """Tests the unified SystemControlAdapter and singleton lifecycle."""

    def setUp(self):
        reset_system_control()
        self.mock_audio = MockAudioBackend(volume=60, muted=False)
        self.mock_display = MockDisplayBackend(brightness=75, night_light=False)
        self.mock_window = MockWindowBackend(foreground_title="Main Workstation")
        self.mock_power = MockPowerSessionBackend()
        self.adapter = SystemControlAdapter(
            audio_backend=self.mock_audio,
            display_backend=self.mock_display,
            window_backend=self.mock_window,
            power_backend=self.mock_power,
        )
        set_system_control(self.adapter)

    def tearDown(self):
        reset_system_control()

    def test_audio_delegation(self):
        ok, msg = system_control.volume_up(5)
        self.assertTrue(ok)
        self.assertIn("65 percent", msg)

        ok, msg = system_control.volume_down(10)
        self.assertTrue(ok)
        self.assertIn("55 percent", msg)

        ok, msg = system_control.set_volume(45)
        self.assertTrue(ok)
        self.assertIn("45 percent", msg)

        ok, msg = system_control.get_volume()
        self.assertTrue(ok)
        self.assertIn("45 percent", msg)

        ok, msg = system_control.mute(toggle=True)
        self.assertTrue(ok)
        self.assertIn("muted", msg)

    def test_display_delegation(self):
        ok, msg = system_control.brightness_up(10)
        self.assertTrue(ok)
        self.assertIn("85 percent", msg)

        ok, msg = system_control.brightness_down(15)
        self.assertTrue(ok)
        self.assertIn("70 percent", msg)

        ok, msg = system_control.set_brightness(55)
        self.assertTrue(ok)
        self.assertIn("55 percent", msg)

        ok, msg = system_control.get_brightness()
        self.assertTrue(ok)
        self.assertIn("55 percent", msg)

        ok, msg = system_control.toggle_night_light()
        self.assertTrue(ok)
        self.assertIn("engaged", msg)

    def test_window_delegation(self):
        ok, msg = system_control.snap_window("left")
        self.assertTrue(ok)
        self.assertEqual(self.mock_window.last_snap, "left")

        ok, msg = system_control.minimize_all()
        self.assertTrue(ok)
        self.assertTrue(self.mock_window.all_minimized)

        ok, msg = system_control.switch_to_app("notepad")
        self.assertTrue(ok)
        self.assertIn("notepad", self.mock_window.switched_apps)

        ok, msg = system_control.move_to_other_monitor()
        self.assertTrue(ok)
        self.assertTrue(self.mock_window.moved_monitor)

        ok, msg = system_control.close_window(confirm=False)
        self.assertTrue(ok)
        self.assertTrue(self.mock_window.closed)

    def test_power_delegation_and_destructive_gates(self):
        ok, _ = system_control.lock_workstation()
        self.assertTrue(ok)
        self.assertTrue(self.mock_power.locked)

        ok, _ = system_control.sleep_system()
        self.assertTrue(ok)
        self.assertTrue(self.mock_power.slept)

        ok, _ = system_control.take_screenshot()
        self.assertTrue(ok)

        ok, _ = system_control.toggle_wifi()
        self.assertTrue(ok)

        ok, _ = system_control.toggle_bluetooth()
        self.assertTrue(ok)

        ok, _ = system_control.toggle_do_not_disturb()
        self.assertTrue(ok)

        # Shutdown gate
        ok_sd_unconfirmed, msg_sd = system_control.shutdown_system(confirmed=False)
        self.assertFalse(ok_sd_unconfirmed)
        self.assertIn("requires confirmation", msg_sd)

        ok_sd_confirmed, _ = system_control.shutdown_system(confirmed=True)
        self.assertTrue(ok_sd_confirmed)

        # Restart gate
        ok_re_unconfirmed, msg_re = system_control.restart_system(confirmed=False)
        self.assertFalse(ok_re_unconfirmed)
        self.assertIn("requires confirmation", msg_re)

        ok_re_confirmed, _ = system_control.restart_system(confirmed=True)
        self.assertTrue(ok_re_confirmed)


class TestWhitelistActionsAndSchemas(unittest.TestCase):
    """Tests Phase 5 security schemas and execute_whitelisted_action integration."""

    def test_phase5_actions_in_whitelist(self):
        expected_phase5_actions = [
            "set_volume", "get_volume", "brightness_up", "brightness_down",
            "set_brightness", "get_brightness", "toggle_night_light",
            "snap_window", "minimize_all", "switch_to_app", "move_to_other_monitor",
            "sleep_system", "toggle_wifi", "toggle_bluetooth", "toggle_do_not_disturb",
            "shutdown_system", "restart_system"
        ]
        for act in expected_phase5_actions:
            self.assertIn(act, WHITELIST_ACTIONS, f"Action '{act}' missing from WHITELIST_ACTIONS")

    def test_schema_validation(self):
        # Valid set_volume
        ok, msg, sanitized = validate_action_arguments("set_volume", {"level": 75})
        self.assertTrue(ok)
        self.assertEqual(sanitized.get("level"), 75)

        # Clamped set_volume
        ok, msg, sanitized = validate_action_arguments("set_volume", {"level": 150})
        self.assertTrue(ok)
        self.assertEqual(sanitized.get("level"), 100)

        # Invalid type set_volume
        ok, msg, _ = validate_action_arguments("set_volume", {"level": "invalid"})
        self.assertFalse(ok)

        # Valid snap_window
        ok, msg, sanitized = validate_action_arguments("snap_window", {"direction": "left"})
        self.assertTrue(ok)
        self.assertEqual(sanitized.get("direction"), "left")

        # Invalid snap_window direction
        ok, msg, _ = validate_action_arguments("snap_window", {"direction": "diagonal"})
        self.assertFalse(ok)

        # Empty switch_to_app
        ok, msg, _ = validate_action_arguments("switch_to_app", {"app": ""})
        self.assertFalse(ok)

    def test_execute_whitelisted_action_phase5(self):
        mock_audio = MockAudioBackend(volume=50)
        mock_display = MockDisplayBackend(brightness=60)
        mock_window = MockWindowBackend(foreground_title="Document.txt")
        mock_power = MockPowerSessionBackend()
        adapter = SystemControlAdapter(
            audio_backend=mock_audio,
            display_backend=mock_display,
            window_backend=mock_window,
            power_backend=mock_power,
        )
        set_system_control(adapter)

        try:
            ctx = MagicMock()
            # set_volume
            ok, msg = execute_whitelisted_action("set_volume", {"level": 80}, ctx)
            self.assertTrue(ok)
            self.assertEqual(mock_audio.volume, 80)

            # set_brightness
            ok, msg = execute_whitelisted_action("set_brightness", {"level": 90}, ctx)
            self.assertTrue(ok)
            self.assertEqual(mock_display.brightness, 90)

            # snap_window
            ok, msg = execute_whitelisted_action("snap_window", {"direction": "left"}, ctx)
            self.assertTrue(ok)
            self.assertEqual(mock_window.last_snap, "left")

            # sleep_system
            ok, msg = execute_whitelisted_action("sleep_system", {}, ctx)
            self.assertTrue(ok)
            self.assertTrue(mock_power.slept)

            # shutdown_system unconfirmed
            ok, msg = execute_whitelisted_action("shutdown_system", {"confirmed": False}, ctx)
            self.assertFalse(ok)
            self.assertIn("requires confirmation", msg)

            # shutdown_system confirmed
            ok, msg = execute_whitelisted_action("shutdown_system", {"confirmed": True}, ctx)
            self.assertTrue(ok)
            self.assertTrue(mock_power.shutdown_executed)
        finally:
            reset_system_control()


class TestReflexIntentsAndCommands(unittest.TestCase):
    """Tests System 1 reflex command recognition and execution in commands.py."""

    def setUp(self):
        self.cfg = Config()
        self.cfg.local_decision_engine = True
        self.ctx = MagicMock()
        self.ctx.engine.cfg = self.cfg
        self.mock_audio = MockAudioBackend(volume=50)
        self.mock_display = MockDisplayBackend(brightness=70)
        self.mock_window = MockWindowBackend(foreground_title="Report.txt")
        self.mock_power = MockPowerSessionBackend()
        self.adapter = SystemControlAdapter(
            audio_backend=self.mock_audio,
            display_backend=self.mock_display,
            window_backend=self.mock_window,
            power_backend=self.mock_power,
        )
        set_system_control(self.adapter)
        # Reset pending confirmation in commands module
        import commands
        commands._PENDING_CONFIRMATION = None

    def tearDown(self):
        reset_system_control()
        import commands
        commands._PENDING_CONFIRMATION = None

    @patch("pyautogui.press")
    def test_audio_reflexes(self, mock_press):
        reply = run_command("volume up", self.ctx)
        self.assertIn("Volume up", reply)

        reply = run_command("volume down", self.ctx)
        self.assertIn("Volume down", reply)

        reply = run_command("set volume to 75", self.ctx)
        self.assertIn("75 percent", reply)
        self.assertEqual(self.mock_audio.volume, 75)

        reply = run_command("mute", self.ctx)
        self.assertIn("Muted", reply)

    def test_display_reflexes(self):
        reply = run_command("brightness up", self.ctx)
        self.assertIn("brightness increased", reply.lower())

        reply = run_command("brightness down", self.ctx)
        self.assertIn("brightness lowered", reply.lower())

        reply = run_command("set brightness to 60", self.ctx)
        self.assertIn("60 percent", reply)
        self.assertEqual(self.mock_display.brightness, 60)

        reply = run_command("toggle night light", self.ctx)
        self.assertIn("night light", reply.lower())

    def test_window_reflexes(self):
        reply = run_command("snap left", self.ctx)
        self.assertIn("snapped left", reply.lower())
        self.assertEqual(self.mock_window.last_snap, "left")

        reply = run_command("snap right", self.ctx)
        self.assertIn("snapped right", reply.lower())
        self.assertEqual(self.mock_window.last_snap, "right")

        reply = run_command("maximize window", self.ctx)
        self.assertIn("maximized", reply.lower())

        reply = run_command("switch to chrome", self.ctx)
        self.assertIn("chrome", reply.lower())

        reply = run_command("move to other monitor", self.ctx)
        self.assertIn("other monitor", reply.lower())

    def test_power_and_radio_reflexes(self):
        reply = run_command("lock pc", self.ctx)
        self.assertIn("locked", reply.lower())
        self.assertTrue(self.mock_power.locked)

        reply = run_command("sleep pc", self.ctx)
        self.assertIn("suspending system power", reply.lower())
        self.assertTrue(self.mock_power.slept)

        reply = run_command("toggle wifi", self.ctx)
        self.assertIn("wi-fi", reply.lower())

        reply = run_command("toggle bluetooth", self.ctx)
        self.assertIn("bluetooth", reply.lower())

        reply = run_command("turn on do not disturb", self.ctx)
        self.assertIn("disturb", reply.lower())

    def test_shutdown_confirmation_flow(self):
        # 1. Unconfirmed prompt
        reply1 = run_command("shut down pc", self.ctx)
        self.assertIn("confirmation", reply1.lower())
        self.assertFalse(self.mock_power.shutdown_executed)

        # 2. Spoken confirmation
        reply2 = run_command("confirm shutdown", self.ctx)
        self.assertIn("shutdown initiated", reply2.lower())
        self.assertTrue(self.mock_power.shutdown_executed)

    def test_direct_confirm_shutdown(self):
        reply = run_command("yes confirm shutdown", self.ctx)
        self.assertIn("shutdown initiated", reply.lower())
        self.assertTrue(self.mock_power.shutdown_executed)

    def test_restart_confirmation_flow(self):
        # 1. Unconfirmed prompt
        reply1 = run_command("restart pc", self.ctx)
        self.assertIn("confirmation", reply1.lower())
        self.assertFalse(self.mock_power.restart_executed)

        # 2. Spoken confirmation
        reply2 = run_command("confirm restart", self.ctx)
        self.assertTrue("restart initiated" in reply2.lower() or "reboot" in reply2.lower())
        self.assertTrue(self.mock_power.restart_executed)

    def test_cancel_pending_confirmation(self):
        run_command("shutdown", self.ctx)
        reply = run_command("cancel", self.ctx)
        self.assertIn("cancelled", reply.lower())
        self.assertFalse(self.mock_power.shutdown_executed)

    def test_close_window_with_unsaved_doc_flow(self):
        # Window title with '*'
        self.mock_window.foreground_title = "*Notes.txt - Notepad"
        reply1 = run_command("close window", self.ctx)
        self.assertIn("unsaved changes", reply1.lower())
        self.assertFalse(self.mock_window.closed)

        # Follow up with confirmation
        reply2 = run_command("confirm close", self.ctx)
        self.assertIn("window closed", reply2.lower())
        self.assertTrue(self.mock_window.closed)


class TestDecisionEnginePhase5Intents(unittest.TestCase):
    """Tests SemanticDecisionEngine classification of Phase 5 actions."""

    def setUp(self):
        self.engine = get_decision_engine()

    def test_classification_of_phase5_phrases(self):
        test_cases = [
            ("increase screen brightness", "brightness_up"),
            ("make screen dimmer", "brightness_down"),
            ("toggle night light", "toggle_night_light"),
            ("snap window left", "snap_window"),
            ("snap window to the right", "snap_window"),
            ("move window to other screen", "move_to_other_monitor"),
            ("put pc to sleep", "sleep_system"),
            ("turn on bluetooth", "toggle_bluetooth"),
            ("enable focus assist", "toggle_do_not_disturb"),
        ]
        for phrase, expected_intent in test_cases:
            match = self.engine.classify(phrase)
            self.assertEqual(
                match.intent,
                expected_intent,
                f"Phrase '{phrase}' classified as {match.intent}, expected {expected_intent}"
            )


if __name__ == "__main__":
    unittest.main()
