"""
test_productivity_and_routines.py — Unit Tests for Phase 6 Productivity, Math/Unit Engine, Scheduler & Routines.
"""

import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

import ai
import chained_commands
import commands
import math_parser
import productivity
import routines
import scheduler


# =====================================================================
# 1. Math & Unit Parser Tests
# =====================================================================

class TestMathParser:
    def test_basic_arithmetic(self):
        ok, res, msg = math_parser.evaluate_expression("15 + 27")
        assert ok is True
        assert res == 42
        assert msg == "42"

        ok, res, msg = math_parser.evaluate_expression("100 - 45")
        assert ok is True
        assert res == 55

        ok, res, msg = math_parser.evaluate_expression("12 * 8")
        assert ok is True
        assert res == 96

        ok, res, msg = math_parser.evaluate_expression("144 / 12")
        assert ok is True
        assert res == 12

    def test_powers_and_modulo(self):
        ok, res, _ = math_parser.evaluate_expression("2 ^ 8")
        assert ok is True
        assert res == 256

        ok, res, _ = math_parser.evaluate_expression("2 ** 5")
        assert ok is True
        assert res == 32

        ok, res, _ = math_parser.evaluate_expression("29 % 5")
        assert ok is True
        assert res == 4

    def test_functions_and_constants(self):
        ok, res, msg = math_parser.evaluate_expression("sqrt(144)")
        assert ok is True
        assert res == 12.0

        ok, res, _ = math_parser.evaluate_expression("abs(-42)")
        assert ok is True
        assert res == 42

        ok, res, _ = math_parser.evaluate_expression("round(3.14159, 2)")
        assert ok is True
        assert res == 3.14

        ok, res, _ = math_parser.evaluate_expression("pi")
        assert ok is True
        assert abs(res - 3.14159265) < 0.0001

    def test_percentages(self):
        # 15% of 200 -> 30
        ok, res, _ = math_parser.evaluate_expression("15% of 200")
        assert ok is True
        assert res == 30.0

        # 15 percent of 200 -> 30
        ok, res, _ = math_parser.evaluate_expression("15 percent of 200")
        assert ok is True
        assert res == 30.0

        # 100 + 15% -> 115
        ok, res, _ = math_parser.evaluate_expression("100 + 15%")
        assert ok is True
        assert res == 115.0

        # 100 - 20% -> 80
        ok, res, _ = math_parser.evaluate_expression("100 - 20%")
        assert ok is True
        assert res == 80.0

    def test_division_by_zero(self):
        ok, res, msg = math_parser.evaluate_expression("10 / 0")
        assert ok is False
        assert "zero" in msg.lower()

    def test_security_rejection_of_disallowed_code(self):
        # Must safely reject non-whitelisted functions and arbitrary syntax
        ok, _, msg = math_parser.evaluate_expression("open('secret.txt')")
        assert ok is False
        assert "not in safe function whitelist" in msg

        ok, _, msg = math_parser.evaluate_expression("eval('2+2')")
        assert ok is False

    def test_spoken_math_query_parsing(self):
        raw, reply = math_parser.parse_math_or_unit_query("what is 25 times 4")
        assert raw == "100"
        assert "100" in reply

        raw, reply = math_parser.parse_math_or_unit_query("calculate 15 percent of 200")
        assert raw == "30"
        assert "30" in reply

        raw, reply = math_parser.parse_math_or_unit_query("square root of 144")
        assert raw == "12"


class TestUnitConverter:
    def test_length_conversions(self):
        # 1 mile to kilometers
        ok, val, msg = math_parser.convert_units(1.0, "mi", "km")
        assert ok is True
        assert abs(val - 1.609344) < 0.001

        # 1 meter to centimeters
        ok, val, _ = math_parser.convert_units(2.5, "meters", "centimeters")
        assert ok is True
        assert val == 250.0

    def test_mass_conversions(self):
        # 1 kilogram to grams
        ok, val, _ = math_parser.convert_units(2.0, "kg", "g")
        assert ok is True
        assert val == 2000.0

        # 1 pound to ounces
        ok, val, _ = math_parser.convert_units(1.0, "lb", "oz")
        assert ok is True
        assert abs(val - 16.0) < 0.01

    def test_temperature_conversions(self):
        # 100 Celsius to Fahrenheit
        ok, val, msg = math_parser.convert_units(100.0, "celsius", "fahrenheit")
        assert ok is True
        assert val == 212.0
        assert "212" in msg

        # 32 Fahrenheit to Celsius
        ok, val, _ = math_parser.convert_units(32.0, "fahrenheit", "celsius")
        assert ok is True
        assert val == 0.0

        # 0 Celsius to Kelvin
        ok, val, _ = math_parser.convert_units(0.0, "celsius", "kelvin")
        assert ok is True
        assert val == 273.15

    def test_volume_conversions(self):
        # 1 gallon to liters
        ok, val, _ = math_parser.convert_units(1.0, "gal", "l")
        assert ok is True
        assert abs(val - 3.78541) < 0.01

        # 1 liter to milliliters
        ok, val, _ = math_parser.convert_units(1.5, "l", "ml")
        assert ok is True
        assert val == 1500.0

    def test_time_conversions(self):
        # 2 hours to minutes
        ok, val, _ = math_parser.convert_units(2.0, "hours", "minutes")
        assert ok is True
        assert val == 120.0

        # 1 day to hours
        ok, val, _ = math_parser.convert_units(1.0, "days", "hours")
        assert ok is True
        assert val == 24.0

    def test_incompatible_units(self):
        ok, val, msg = math_parser.convert_units(10.0, "miles", "celsius")
        assert ok is False
        assert "incompatible" in msg.lower()

    def test_spoken_unit_query(self):
        raw, reply = math_parser.parse_math_or_unit_query("convert 10 miles to kilometers")
        assert "16.0934" in raw
        assert "10 miles is approximately" in reply


# =====================================================================
# 2. Local Scheduler Tests
# =====================================================================

class TestScheduler:
    def test_duration_parsing(self):
        assert scheduler.parse_duration_seconds("5 minutes") == 300.0
        assert scheduler.parse_duration_seconds("10 minutes and 30 seconds") == 630.0
        assert scheduler.parse_duration_seconds("1 hour") == 3600.0
        assert scheduler.parse_duration_seconds("half an hour") == 1800.0
        assert scheduler.parse_duration_seconds("quarter hour") == 900.0
        assert scheduler.parse_duration_seconds("45 seconds") == 45.0
        assert scheduler.parse_duration_seconds("two minutes") == 120.0

    def test_format_duration(self):
        assert scheduler.format_duration(300) == "5 minutes"
        assert scheduler.format_duration(3660) == "1 hour and 1 minute"
        assert scheduler.format_duration(45) == "45 seconds"

    def test_persistence_and_restart(self, tmp_path):
        sched_file = str(tmp_path / "scheduler.json")
        current_time = 1000.0

        # Create scheduler, add timer and alarm
        sched1 = scheduler.LocalScheduler(storage_path=sched_file, clock_fn=lambda: current_time)
        t = sched1.set_timer(300.0, label="Pasta")
        a = sched1.set_alarm(current_time + 3600.0, label="Meeting")
        assert os.path.exists(sched_file)

        # Reload in a new scheduler instance (simulating app restart)
        sched2 = scheduler.LocalScheduler(storage_path=sched_file, clock_fn=lambda: current_time)
        active = sched2.list_items(active_only=True)
        assert len(active) == 2

        active_timer = sched2.get_active_timer()
        assert active_timer is not None
        assert active_timer.label == "Pasta"
        assert active_timer.remaining_seconds(current_time) == 300.0

    def test_timer_expiration_and_chime(self, tmp_path):
        sched_file = str(tmp_path / "scheduler.json")
        sim_time = 1000.0
        chime_mock = MagicMock()
        notify_mock = MagicMock()

        sched = scheduler.LocalScheduler(
            storage_path=sched_file,
            clock_fn=lambda: sim_time,
            chime_fn=chime_mock,
            notify_fn=notify_mock,
        )
        item = sched.set_timer(60.0, label="Tea")

        # Advance time by 30s: not expired
        sim_time = 1030.0
        expired = sched.tick(now=sim_time)
        assert len(expired) == 0
        assert chime_mock.call_count == 0

        # Advance time past 60s: should expire and trigger chime & notify
        sim_time = 1061.0
        expired = sched.tick(now=sim_time)
        assert len(expired) == 1
        assert expired[0].id == item.id
        assert chime_mock.call_count == 1
        assert notify_mock.call_count == 1
        assert item.has_finished is True

    def test_cancel_timer(self, tmp_path):
        sched_file = str(tmp_path / "scheduler.json")
        sched = scheduler.LocalScheduler(storage_path=sched_file, clock_fn=lambda: 1000.0)
        sched.set_timer(300.0, label="Oven")

        assert sched.get_active_timer() is not None
        ok = sched.cancel_timer("Oven")
        assert ok is True
        assert sched.get_active_timer() is None

    def test_voice_command_handling(self, tmp_path):
        sched_file = str(tmp_path / "scheduler.json")
        sched = scheduler.LocalScheduler(storage_path=sched_file, clock_fn=lambda: 1000.0)

        # Set timer
        reply = scheduler.handle_scheduler_command("set a timer for 10 minutes", scheduler=sched)
        assert "Timer set for 10 minutes" in reply

        # Check time left
        reply = scheduler.handle_scheduler_command("time left on timer", scheduler=sched)
        assert "10 minutes remaining" in reply

        # Cancel timer
        reply = scheduler.handle_scheduler_command("cancel timer", scheduler=sched)
        assert "Timer cancelled" in reply


# =====================================================================
# 3. Productivity Tests (Notes, Dictation & Clipboard)
# =====================================================================

class TestProductivity:
    def test_quick_notes_create_and_read(self, tmp_path):
        notes_file = str(tmp_path / "Jarvis_Notes.md")

        # Add notes
        ok, msg = productivity.add_note("Buy coffee beans", notes_path=notes_file)
        assert ok is True
        assert os.path.exists(notes_file)

        ok, msg = productivity.add_note("Schedule dentist appointment", notes_path=notes_file)
        assert ok is True

        # Read notes
        ok, summary, entries = productivity.read_notes(max_entries=2, notes_path=notes_file)
        assert ok is True
        assert len(entries) == 2
        assert "Buy coffee beans" in entries[0]
        assert "Schedule dentist appointment" in entries[1]

        # Get last note
        ok, last = productivity.get_last_note(notes_path=notes_file)
        assert ok is True
        assert "Schedule dentist appointment" in last

    def test_dictation_mode_toggle(self):
        assert productivity.is_dictation_mode() is False
        productivity.set_dictation_mode(True)
        assert productivity.is_dictation_mode() is True
        productivity.set_dictation_mode(False)
        assert productivity.is_dictation_mode() is False

    @patch("productivity.pyautogui")
    def test_type_text(self, mock_pyautogui):
        mock_pyautogui.write = MagicMock()
        mock_pyautogui.hotkey = MagicMock()

        ok = productivity.type_text("Hello Jarvis world")
        assert ok is True

    def test_clipboard_reader_and_summarizer(self):
        sample_text = (
            "Artificial intelligence is transforming computing. "
            "Systems can process language and optimize tasks with high performance. "
            "Local edge computing ensures total user privacy and offline reliability. "
            "J.A.R.V.I.S. integrates non-autoregressive decision reflex models. "
            "Future interfaces rely on natural gestures and spoken interaction."
        )
        productivity.set_clipboard_backend(lambda: sample_text)
        assert productivity.get_clipboard_text() == sample_text

        summary = productivity.summarize_text(sample_text, max_sentences=2)
        assert len(summary) > 0
        assert len(summary) < len(sample_text)

        # Reset backend
        productivity.set_clipboard_backend(None)

    def test_translator_hook(self):
        productivity.set_translator_hook(lambda text, lang: f"Translated to {lang}: {text}")
        res = productivity.translate_text("Hello", "spanish")
        assert res == "Translated to spanish: Hello"
        productivity.set_translator_hook(None)


# =====================================================================
# 4. Routines Engine Tests
# =====================================================================

class TestRoutines:
    def test_default_routines_present(self):
        rout = routines.get_routines()
        assert "work mode" in rout
        assert "focus mode" in rout
        assert "evening mode" in rout

    def test_get_routine_case_insensitive(self):
        steps = routines.get_routine("Work Mode")
        assert steps is not None
        assert len(steps) >= 2
        assert steps[0]["action"] == "open_app"

    def test_save_routine_whitelisting(self):
        # Valid actions
        valid_steps = [
            {"action": "set_volume", "args": {"level": 25}},
            {"action": "snap_window", "args": {"direction": "left"}},
        ]
        ok, msg = routines.save_routine("test_valid", valid_steps)
        assert ok is True

        # Invalid non-whitelisted action
        invalid_steps = [
            {"action": "rmdir_everything", "args": {}},
        ]
        ok, msg = routines.save_routine("test_invalid", invalid_steps)
        assert ok is False
        assert "security whitelist" in msg.lower()

        # Clean up
        routines.delete_routine("test_valid")

    def test_execute_routine_sequential(self):
        mock_executor = MagicMock(return_value=(True, "Success"))
        ok, summary, steps = routines.execute_routine(
            "work mode",
            executor_fn=mock_executor,
        )
        assert ok is True
        assert "Work Mode" in summary
        assert mock_executor.call_count >= 2
        assert steps[0]["success"] is True


# =====================================================================
# 5. Chained Commands Tests
# =====================================================================

class TestChainedCommands:
    def test_splitting_compound_commands(self):
        parts = chained_commands.split_chained_commands("volume up and maximize window")
        assert parts == ["volume up", "maximize window"]

        parts = chained_commands.split_chained_commands("open my programming folder and launch VS Code")
        assert parts == ["open my programming folder", "launch VS Code"]

        parts = chained_commands.split_chained_commands("turn down brightness, then mute audio")
        assert parts == ["turn down brightness", "mute audio"]

    def test_protecting_non_splittable_phrases(self):
        # Queries with "and"
        parts = chained_commands.split_chained_commands("search for cats and dogs")
        assert parts == ["search for cats and dogs"]

        # Quick notes with "and"
        parts = chained_commands.split_chained_commands("note that buy bread and butter")
        assert parts == ["note that buy bread and butter"]

        # Dictation with "and"
        parts = chained_commands.split_chained_commands("type hello and welcome")
        assert parts == ["type hello and welcome"]

        # Durations with "and"
        parts = chained_commands.split_chained_commands("set timer for 2 minutes and 30 seconds")
        assert parts == ["set timer for 2 minutes and 30 seconds"]

    def test_execute_chained_commands(self):
        log = []

        def mock_exec(cmd, ctx):
            log.append(cmd)
            return f"Done {cmd}."

        ok, reply, results = chained_commands.execute_chained_commands(
            "volume up and maximize window",
            ctx=None,
            execute_single_fn=mock_exec,
        )
        assert ok is True
        assert log == ["volume up", "maximize window"]
        assert "Done volume up. Done maximize window." in reply


# =====================================================================
# 6. Integration Tests (ai.py WHITELIST_ACTIONS & commands.py Dispatch)
# =====================================================================

class TestIntegration:
    def test_phase6_actions_in_ai_whitelist(self):
        expected_actions = [
            "set_timer", "cancel_timer", "timer_status", "set_alarm", "set_reminder",
            "calculate", "convert_units", "quick_note", "read_notes", "type_text",
            "dictation_mode", "read_clipboard", "summarize_clipboard", "start_routine",
        ]
        for act in expected_actions:
            assert act in ai.WHITELIST_ACTIONS
            assert act in ai.ACTION_SCHEMAS

    def test_validate_action_arguments(self):
        ok, msg, sanitized = ai.validate_action_arguments("set_timer", {"duration": 120, "label": "Tea"})
        assert ok is True
        assert sanitized["duration"] == 120.0
        assert sanitized["label"] == "Tea"

        ok, msg, sanitized = ai.validate_action_arguments("calculate", {"expression": "15 * 4"})
        assert ok is True
        assert sanitized["expression"] == "15 * 4"

        ok, msg, sanitized = ai.validate_action_arguments("convert_units", {"value": 5, "from_unit": "km", "to_unit": "m"})
        assert ok is True
        assert sanitized["value"] == 5.0

        # Reject invalid timer duration
        ok, msg, _ = ai.validate_action_arguments("set_timer", {"duration": -10})
        assert ok is False

    def test_commands_run_command_math_integration(self):
        reply = commands.run_command("what is 50 plus 25", ctx=None)
        assert reply is not None
        assert "75" in reply

    def test_commands_run_command_chained_integration(self):
        reply = commands.run_command("what is 10 plus 10 and then what is 20 plus 20", ctx=None)
        assert reply is not None
        assert "20" in reply
        assert "40" in reply
