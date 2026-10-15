import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from updater import parse_version, is_newer, AutoUpdater, APP_VERSION


class TestAutoUpdater(unittest.TestCase):

    def test_version_parsing(self):
        self.assertEqual(parse_version("v1.0.0"), (1, 0, 0))
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("v2.1"), (2, 1, 0))
        self.assertEqual(parse_version("v0.0.1-alpha"), (0, 0, 1))
        self.assertEqual(parse_version("invalid"), (0, 0, 0))

    def test_version_comparison(self):
        self.assertTrue(is_newer("v1.0.1", "1.0.0"))
        self.assertTrue(is_newer("v1.1.0", "1.0.9"))
        self.assertTrue(is_newer("v2.0.0", "1.9.9"))
        self.assertFalse(is_newer("v1.0.0", "1.0.0"))
        self.assertFalse(is_newer("v0.9.9", "1.0.0"))
        self.assertFalse(is_newer("v1.0.0", "1.0.1"))

    def test_updater_listeners(self):
        updater = AutoUpdater(auto_check=False)
        received = []
        updater.add_listener(lambda state, **kw: received.append((state, kw)))
        updater._notify("testing_state", value=42)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "testing_state")
        self.assertEqual(received[0][1]["value"], 42)
        self.assertEqual(updater.status, "testing_state")


if __name__ == "__main__":
    unittest.main()
