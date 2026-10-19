"""Config v4 migration: deprecated glass_blur -> glass_mode / glass_transparency."""
import json
import os
import tempfile
import unittest

from config import Config, load_config


def _load(d):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "jarvis_config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        cfg = load_config(path)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    return cfg, saved


class TestConfigMigrationV4(unittest.TestCase):
    def test_blur_false_becomes_tinted_zero(self):
        cfg, _ = _load({"config_version": 3, "glass_blur": False})
        self.assertEqual(cfg.glass_mode, "tinted")
        self.assertEqual(cfg.glass_transparency, 0.0)

    def test_blur_true_gives_defaults(self):
        cfg, _ = _load({"config_version": 3, "glass_blur": True})
        self.assertEqual(cfg.glass_mode, "tinted")
        self.assertEqual(cfg.glass_transparency, 0.6)

    def test_blur_missing_gives_defaults(self):
        cfg, _ = _load({"config_version": 3})
        self.assertEqual(cfg.glass_mode, "tinted")
        self.assertEqual(cfg.glass_transparency, 0.6)

    def test_version_bumped_and_saved(self):
        cfg, saved = _load({"config_version": 3, "glass_blur": False})
        self.assertEqual(cfg.config_version, 4)
        self.assertEqual(saved["config_version"], 4)

    def test_does_not_rerun_on_saved_v4(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jarvis_config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"config_version": 3, "glass_blur": False}, f)
            load_config(path)
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            saved["glass_transparency"] = 0.8
            with open(path, "w", encoding="utf-8") as f:
                json.dump(saved, f)
            cfg = load_config(path)
        self.assertEqual(cfg.glass_transparency, 0.8)
        self.assertEqual(cfg.config_version, 4)

    def test_v4_file_with_blur_false_untouched(self):
        cfg, _ = _load({"config_version": 4, "glass_blur": False,
                        "glass_mode": "clear", "glass_transparency": 0.4})
        self.assertEqual(cfg.glass_mode, "clear")
        self.assertEqual(cfg.glass_transparency, 0.4)

    def test_invalid_mode_becomes_tinted(self):
        cfg, _ = _load({"config_version": 4, "glass_mode": "frosted"})
        self.assertEqual(cfg.glass_mode, "tinted")

    def test_transparency_coerced_and_clamped(self):
        for raw, want in ((-1, 0.0), (5, 1.0), ("abc", 0.6), ("0.25", 0.25)):
            with self.subTest(raw=raw):
                cfg, _ = _load({"config_version": 4, "glass_transparency": raw})
                self.assertIsInstance(cfg.glass_transparency, float)
                self.assertEqual(cfg.glass_transparency, want)

    def test_fresh_config_is_v4(self):
        self.assertEqual(Config().config_version, 4)


if __name__ == "__main__":
    unittest.main()
