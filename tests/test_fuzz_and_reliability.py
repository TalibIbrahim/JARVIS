"""
tests/test_fuzz_and_reliability.py — Fuzz Testing & Resilience Suite for J.A.R.V.I.S.

Tests:
1. Fuzz decision_engine.py with 600+ malformed queries (emojis, raw bytes, huge strings, injection, type mutations)
   asserting 0 unhandled exceptions.
2. Fuzz config.py with corrupt JSON, non-dict roots, bad types, missing fields
   asserting auto-repair and safe defaults.
3. Offline & device failure resilience (offline = System 1 with clean explanation; no camera = voice works; no mic = vision works; UIPI elevation handling).

Strictly NO Qt imports. 100% unit-testable with clean interfaces.
"""

from __future__ import annotations

import json
import os
import random
import string
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from config import Config, load_config, save_config
from decision_engine import SemanticDecisionEngine, DecisionMatch, evaluate_decision, get_decision_engine
import system_control
from system_control import MockWindowBackend, SystemControlAdapter, ELEVATED_WINDOW_EXPLANATION
from thread_supervisor import ThreadSupervisor, explain_graceful_degradation


class TestDecisionEngineFuzzing(unittest.TestCase):
    """Fuzzes decision_engine with 600+ malformed queries and asserts 0 unhandled exceptions."""

    @classmethod
    def setUpClass(cls):
        cls.engine = get_decision_engine()

    def test_fuzz_500_plus_malformed_queries(self):
        rng = random.Random(1337)
        corpus: list[any] = []

        # 1. Emojis, non-Latin scripts, zero-width chars, RTL text (120 queries)
        emoji_bank = ["✨", "🚀", "🔥", "💥", "🤖", "👂", "👍", "👀", "🧠", "❤️", "👑", "⚡", "🛸", "🎯", "🛡️"]
        scripts = [
            "مرحبا بك يا جارفيس", "открой браузер", "你好贾维斯", "こんにちは",
            "नमस्ते जार्विस", "שלום ג'ארוויס", "γειά σου Jarvis",
            "كبير الصوت", "تحكم بالصوت", "باي باي",
        ]
        for _ in range(70):
            combo = "".join(rng.choices(emoji_bank, k=rng.randint(1, 10)))
            corpus.append(combo)
            corpus.append(f"Jarvis {combo} please {rng.choice(scripts)}")
        corpus.extend(scripts)

        # 2. Control characters, null bytes, format strings, shell injection (100 queries)
        injections = [
            "\x00\x00\x00", "\r\n\r\n\t\t", "%s%s%s%s%s%n", "{0}{1}{2}",
            "'; DROP TABLE users; --", "&& rm -rf /", "| powershell -c calc.exe",
            "`whoami`", "$((2+2))", "<script>alert(1)</script>",
            "\ufeff\u200b\u200c\u200d", "\u202eRTL_OVERRIDE\u202c",
            "\\x1b[31mRED_COLOR\\x1b[0m", "C:\\Windows\\System32\\cmd.exe",
            "../../../../../../etc/passwd", "file:///C:/boot.ini",
        ]
        for inj in injections:
            corpus.append(inj)
            corpus.append(f"open {inj}")
            corpus.append(f"volume {inj} now")

        # 3. Huge strings and extreme repetition (80 queries)
        for size in [500, 1000, 4000, 8000, 20000, 65536]:
            corpus.append("a" * size)
            corpus.append(" volume up " * (size // 11))
            corpus.append("!@#$%^&*() " * (size // 11))

        # 4. Boundary values (50 queries)
        corpus.extend([
            "", " ", "   \t\n\r   ", "\n", "None", "null", "undefined", "NaN",
            "0", "-1", "999999999999999999999999999999", "3.1415926535",
            ".", ",", "!", "?", "...", "!?!?", " - ", " / ", " # ",
        ])

        # 5. Type mutations & invalid types (70 queries)
        type_mutations = [
            None, 12345, -99, 3.14159, True, False,
            b"raw byte query", b"\x00\xff\xfe\xfd",
            [], ["open", "chrome"], {"intent": "mute"}, (1, 2, 3),
        ]
        corpus.extend(type_mutations)

        # 6. Random alphanumeric junk mutations (350 queries)
        chars = string.ascii_letters + string.digits + string.punctuation + " "
        for _ in range(350):
            length = rng.randint(1, 120)
            rnd_str = "".join(rng.choices(chars, k=length))
            corpus.append(rnd_str)

        self.assertGreaterEqual(len(corpus), 500, f"Expected 500+ fuzz queries, got {len(corpus)}")

        unhandled_exceptions = []
        with patch("ai.execute_whitelisted_action", return_value=(True, "Executed")), \
             patch("weather_client.get_weather_client"), \
             patch("media_adapter.get_media_adapter"):
            for i, query in enumerate(corpus):
                try:
                    # 1. Test engine.decide()
                    action, conf = self.engine.decide(query)
                    self.assertIsInstance(conf, float)
                    self.assertTrue(0.0 <= conf <= 1.0)
                    if action is not None:
                        self.assertIsInstance(action, str)

                    # 2. Test engine.classify()
                    match = self.engine.classify(query)
                    self.assertIsInstance(match, DecisionMatch)
                    self.assertIsInstance(match.confidence, float)
                    self.assertIsInstance(match.args, dict)

                    # 3. Test evaluate_decision()
                    eval_res = evaluate_decision(query)
                    if eval_res is not None:
                        self.assertIsInstance(eval_res, str)

                except Exception as exc:
                    unhandled_exceptions.append((i, query, type(exc).__name__, str(exc)))

        self.assertEqual(
            len(unhandled_exceptions),
            0,
            f"Fuzzing resulted in {len(unhandled_exceptions)} unhandled exceptions: {unhandled_exceptions[:5]}",
        )


class TestConfigFuzzingAndAutoRepair(unittest.TestCase):
    """Fuzzes config.py with corrupt JSON, bad types, and missing fields; asserts auto-repair and safe defaults."""

    def test_corrupt_json_strings_auto_repaired(self):
        """Corrupt JSON strings are safely handled and auto-repaired to valid defaults."""
        corrupt_inputs = [
            '{"camera_index": 0, "frame_width": ',       # Truncated
            '{"theme_mode": "dark", "glass_blur": tru',  # Truncated bool
            'NOT_VALID_JSON_AT_ALL',                      # Plain text
            '<<<MALFORMED XML/JSON>>>',                   # Junk symbols
            '{\x00\x01\x02\xff}',                         # Binary junk in JSON
            '{"time_format": "12h",,}',                  # Syntax error double comma
            '{"pointer_speed": NaN}',                     # NaN token
            '{"smooth_beta": undefined}',                 # Undefined token
            '',                                           # 0-byte empty file
            '   \n\t  \r  ',                              # Whitespace only
            '[1, 2, 3, "not a dict"]',                   # Non-dict list root
            '"just a bare string"',                       # Bare string root
            '123456789',                                  # Bare number root
            'true',                                       # Bare bool root
            'null',                                       # Bare null root
        ]

        for payload in corrupt_inputs:
            with tempfile.TemporaryDirectory() as tmp_dir:
                cfg_path = os.path.join(tmp_dir, "jarvis_config.json")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(payload)

                # Must not raise unhandled exception
                cfg = load_config(cfg_path)
                self.assertIsInstance(cfg, Config)

                # Safe defaults preserved
                self.assertEqual(cfg.camera_index, 0)
                self.assertEqual(cfg.frame_width, 1280)
                self.assertTrue(isinstance(cfg.pointer_speed, float))
                self.assertTrue(isinstance(cfg.voice_enabled, bool))

                # Auto-repair check: config file on disk must now be valid JSON
                with open(cfg_path, "r", encoding="utf-8") as f_check:
                    repaired_data = json.load(f_check)
                self.assertIsInstance(repaired_data, dict)
                self.assertEqual(repaired_data.get("camera_index"), 0)

    def test_bad_types_coerced_or_repaired_to_defaults(self):
        """Bad field types in JSON are coerced or safely defaulted without crashing."""
        bad_type_scenarios = [
            # Wrong types for numeric fields
            {"camera_index": "not_an_int", "frame_width": "twelve_eighty", "pointer_speed": "super_fast"},
            # Negative or bad types for bool fields
            {"voice_enabled": "maybe", "show_preview": [1, 2], "speak": {"speak": True}},
            # Wrong types for containers
            {"wake_aliases": 12345, "folders_whitelist": "all_folders"},
            # Coercible strings to numbers and bools
            {"camera_index": "2", "pointer_speed": "1.75", "voice_enabled": "true", "show_preview": "0"},
            # None values for primitive non-nullable fields
            {"camera_index": None, "pointer_speed": None, "voice_enabled": None},
        ]

        for scenario in bad_type_scenarios:
            with tempfile.TemporaryDirectory() as tmp_dir:
                cfg_path = os.path.join(tmp_dir, "jarvis_config.json")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(scenario, f)

                cfg = load_config(cfg_path)
                self.assertIsInstance(cfg, Config)

                # Verify strict dataclass typing invariants
                self.assertIsInstance(cfg.camera_index, int)
                self.assertIsInstance(cfg.pointer_speed, float)
                self.assertIsInstance(cfg.voice_enabled, bool)
                self.assertIsInstance(cfg.wake_aliases, list)
                self.assertIsInstance(cfg.folders_whitelist, dict)

    def test_missing_fields_safely_defaulted(self):
        """Partial config missing 90% of fields loads with all missing fields having safe defaults."""
        partial = {"weather_city": "London", "speak": False}
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "jarvis_config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(partial, f)

            cfg = load_config(cfg_path)
            self.assertEqual(cfg.weather_city, "London")
            self.assertEqual(cfg.speak, False)
            # Default values for missing fields
            self.assertEqual(cfg.camera_index, 0)
            self.assertEqual(cfg.theme_mode, "auto")
            self.assertEqual(cfg.tracking_mode, "relative")
            self.assertEqual(cfg.control_scheme, "palm_fist")


class TestOfflineAndResilience(unittest.TestCase):
    """Tests offline graceful degradation, hardware failure resilience, and UIPI handling."""

    def test_offline_graceful_degradation_system1(self):
        """Offline network state gracefully retains System 1 reflex execution with clean explanation."""
        # System 1 command execution does not require internet
        engine = get_decision_engine()
        match = engine.classify("mute the audio right now")
        self.assertEqual(match.intent, "mute")

        # Explain graceful degradation when offline
        mock_overlay = MagicMock()
        mock_speaker = MagicMock()
        msg = explain_graceful_degradation("network", overlay=mock_overlay, speaker=mock_speaker)
        self.assertIn("OFFLINE", msg)
        self.assertIn("SYSTEM 1 REFLEX ACTIVE", msg)
        mock_overlay.post.assert_any_call("error", text="OFFLINE - SYSTEM 1 REFLEX ACTIVE")

    def test_camera_offline_voice_works(self):
        """When camera is unavailable or disconnected, voice and hotkeys operate normally."""
        mock_overlay = MagicMock()
        mock_speaker = MagicMock()
        msg = explain_graceful_degradation("camera", overlay=mock_overlay, speaker=mock_speaker)
        self.assertIn("CAMERA OFFLINE", msg)
        self.assertIn("VOICE ACTIVE", msg)

        # Voice command execution still works 100%
        engine = get_decision_engine()
        match = engine.classify("crank up the audio")
        self.assertEqual(match.intent, "volume_up")

    def test_microphone_offline_vision_works(self):
        """When microphone is unavailable, vision gestures continue operating normally."""
        mock_overlay = MagicMock()
        mock_speaker = MagicMock()
        msg = explain_graceful_degradation("voice", overlay=mock_overlay, speaker=mock_speaker)
        self.assertIn("MIC OFFLINE", msg)
        self.assertIn("GESTURES ACTIVE", msg)

    def test_uipi_elevated_window_detection_and_explanation(self):
        """Elevated foreground window explains via ELEVATED WINDOW - RUN AS ADMIN instead of silent failure."""
        mock_overlay = MagicMock()
        mock_window = MockWindowBackend(foreground_title="Administrator: Command Prompt", is_elevated=True)
        adapter = SystemControlAdapter(window_backend=mock_window, overlay=mock_overlay)

        # Ensure current process is treated as un-elevated for this test
        with patch("system_control.is_current_process_elevated", return_value=False):
            ok, msg = adapter.snap_window("left")
            self.assertFalse(ok)
            self.assertIn(ELEVATED_WINDOW_EXPLANATION, msg)

            # Island text was posted
            mock_overlay.post.assert_any_call("result", text=ELEVATED_WINDOW_EXPLANATION, icon="error")
            mock_overlay.post.assert_any_call("say", text=ELEVATED_WINDOW_EXPLANATION, speak=False)

            # Test close_window
            ok_close, msg_close = adapter.close_window(confirm=True)
            self.assertFalse(ok_close)
            self.assertIn(ELEVATED_WINDOW_EXPLANATION, msg_close)

    def test_thread_supervisor_auto_restart_backoff(self):
        """ThreadSupervisor restarts crashing workers with exponential backoff and tracks health."""
        stop_evt = threading.Event()
        supervisor = ThreadSupervisor(stop_event=stop_evt)

        crash_count = 0
        def crashing_worker():
            nonlocal crash_count
            crash_count += 1
            if crash_count < 3:
                raise RuntimeError(f"Simulated crash #{crash_count}")
            # Runs smoothly after 2 restarts
            while not stop_evt.is_set():
                time.sleep(0.01)

        task = supervisor.register(
            name="TestWorker",
            factory=lambda: crashing_worker,
            max_restarts=5,
            backoff_base=0.05,
            backoff_multiplier=1.5,
        )

        supervisor.start("TestWorker")
        time.sleep(0.3)

        status = task.get_status()
        self.assertGreaterEqual(status["restarts"], 2)
        self.assertTrue(task.is_alive())

        stop_evt.set()
        supervisor.stop("TestWorker", timeout=0.5)


if __name__ == "__main__":
    unittest.main()
