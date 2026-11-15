"""Config v5 migration: 640x480 capture, 0.5 s endpoint, camera off by default, "aggressive" wake repaired."""
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


class TestConfigMigrationV5(unittest.TestCase):
    def test_old_defaults_are_migrated(self):
        cfg, saved = _load({"config_version": 4, "frame_width": 1280, "frame_height": 720,
                            "mic_pause_threshold": 1.0})
        self.assertEqual((cfg.frame_width, cfg.frame_height), (640, 480))
        self.assertEqual(cfg.mic_pause_threshold, 0.5)
        self.assertEqual(saved["config_version"], 6)

    def test_custom_values_are_kept(self):
        cfg, _ = _load({"config_version": 4, "frame_width": 1920, "frame_height": 1080,
                        "mic_pause_threshold": 0.8})
        self.assertEqual((cfg.frame_width, cfg.frame_height), (1920, 1080))
        self.assertEqual(cfg.mic_pause_threshold, 0.8)

    def test_does_not_rerun_on_v5(self):
        cfg, _ = _load({"config_version": 5, "frame_width": 1280, "frame_height": 720,
                        "mic_pause_threshold": 1.0})
        self.assertEqual((cfg.frame_width, cfg.frame_height), (1280, 720))
        self.assertEqual(cfg.mic_pause_threshold, 1.0)

    def test_aggressive_wake_sensitivity_becomes_high(self):
        cfg, _ = _load({"config_version": 5, "wake_sensitivity": "aggressive"})
        self.assertEqual(cfg.wake_sensitivity, "high")

    def test_camera_off_by_default(self):
        self.assertFalse(Config().camera_autostart)
        self.assertEqual(Config().gesture_idle_timeout_s, 120.0)
        self.assertEqual(Config().stt_engine, "local")


if __name__ == "__main__":
    unittest.main()
