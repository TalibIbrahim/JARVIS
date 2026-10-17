"""
tests/test_config_atomic.py - Unit tests for atomic config writes, backup rotation, and corruption recovery.

Strictly NO Qt imports. 100% unit-testable with clean interfaces and temp dirs.
"""
import json
import os
import tempfile
import unittest

from config import Config, load_config, save_config, remember_folder_alias, _atomic_write_json


class TestConfigAtomicAndRecovery(unittest.TestCase):
    """Verifies atomic write guarantees, .bak creation, and corruption recovery."""

    def test_atomic_write_creates_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = os.path.join(tmp_dir, "test_config.json")
            data = {"theme_mode": "dark", "glass_blur": True}
            ok = _atomic_write_json(target, data)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(target))
            self.assertFalse(os.path.exists(f"{target}.tmp"))
            with open(target, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(loaded, data)

    def test_backup_created_on_subsequent_write(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = os.path.join(tmp_dir, "jarvis_config.json")
            bak = f"{target}.bak"

            cfg = Config()
            cfg.pointer_speed = 1.5
            self.assertTrue(save_config(cfg, target))
            self.assertTrue(os.path.exists(target))
            self.assertFalse(os.path.exists(bak))

            # Second save should rotate existing target to .bak
            cfg.pointer_speed = 2.0
            self.assertTrue(save_config(cfg, target))
            self.assertTrue(os.path.exists(bak))

            with open(bak, "r", encoding="utf-8") as f:
                bak_data = json.load(f)
            self.assertEqual(bak_data.get("pointer_speed"), 1.5)

            with open(target, "r", encoding="utf-8") as f:
                target_data = json.load(f)
            self.assertEqual(target_data.get("pointer_speed"), 2.0)

    def test_corrupted_main_file_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = os.path.join(tmp_dir, "jarvis_config.json")

            cfg = Config()
            cfg.time_format = "24h"
            save_config(cfg, target)

            cfg.time_format = "12h"
            save_config(cfg, target)

            # Intentionally corrupt main file with partial/invalid JSON
            with open(target, "w", encoding="utf-8") as f:
                f.write('{"time_format": "truncated_in')

            # Load should catch error, recover from .bak, and repair target
            loaded = load_config(target)
            self.assertEqual(loaded.time_format, "24h")

            # Verify the main file is now healthy valid JSON
            with open(target, "r", encoding="utf-8") as f:
                repaired = json.load(f)
            self.assertEqual(repaired.get("time_format"), "24h")

    def test_missing_main_file_recovers_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = os.path.join(tmp_dir, "jarvis_config.json")
            bak = f"{target}.bak"

            # Create backup file only
            data = {"theme_mode": "light"}
            _atomic_write_json(bak, data)
            self.assertFalse(os.path.exists(target))

            loaded = load_config(target)
            self.assertEqual(loaded.theme_mode, "light")
            self.assertTrue(os.path.exists(target))

    def test_corrupted_without_backup_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = os.path.join(tmp_dir, "jarvis_config.json")
            with open(target, "w", encoding="utf-8") as f:
                f.write("NOT_JSON")

            loaded = load_config(target)
            # Default theme_mode is "auto"
            self.assertEqual(loaded.theme_mode, "auto")

    def test_remember_folder_alias_atomic(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = os.path.join(tmp_dir, "jarvis_config.json")
            self.assertTrue(remember_folder_alias("my_code", tmp_dir, config_path=target))
            loaded = load_config(target)
            self.assertIn("my_code", loaded.folders_whitelist)
            self.assertEqual(loaded.folders_whitelist["my_code"], os.path.abspath(tmp_dir))

    def test_approved_proposals_defaults_and_roundtrip(self):
        cfg = Config()
        self.assertTrue(cfg.ui_status_line_enabled)
        self.assertTrue(cfg.ui_cancel_hint_enabled)
        self.assertTrue(cfg.ui_progressive_shimmer_enabled)
        self.assertTrue(cfg.show_health_panel)

        with tempfile.TemporaryDirectory() as tmp_dir:
            target = os.path.join(tmp_dir, "jarvis_config.json")
            cfg.ui_status_line_enabled = False
            cfg.ui_cancel_hint_enabled = False
            cfg.ui_progressive_shimmer_enabled = False
            cfg.show_health_panel = False
            self.assertTrue(save_config(cfg, target))

            loaded = load_config(target)
            self.assertFalse(loaded.ui_status_line_enabled)
            self.assertFalse(loaded.ui_cancel_hint_enabled)
            self.assertFalse(loaded.ui_progressive_shimmer_enabled)
            self.assertFalse(loaded.show_health_panel)


if __name__ == "__main__":
    unittest.main()
