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

    def test_update_announces_only_once_and_debounces(self):
        """Verifies that an available update is announced verbally exactly once without duplicates."""
        from unittest.mock import MagicMock, patch
        import json
        import io

        mock_payload = {
            "tag_name": "v99.0.0",
            "name": "v99.0.0",
            "body": "Test release",
            "assets": [{"name": "JARVIS.exe", "browser_download_url": "https://example.com/JARVIS.exe", "size": 1024}]
        }
        mock_resp = io.BytesIO(json.dumps(mock_payload).encode("utf-8"))

        overlay = MagicMock()
        ctx = MagicMock()
        updater = AutoUpdater(ctx=ctx, overlay=overlay, auto_check=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            updater._check_worker(notify_if_latest=False)

        # Spoken announcement should be posted to overlay exactly once
        say_calls = [c for c in overlay.post.call_args_list if c[0][0] == "say"]
        self.assertEqual(len(say_calls), 1)
        self.assertEqual(say_calls[0][1]["text"], "Update v99.0.0 is available, sir.")
        # ctx.say must NOT be called as a duplicate
        ctx.say.assert_not_called()

        # Second background check for the same version should NOT announce verbally again
        mock_resp2 = io.BytesIO(json.dumps(mock_payload).encode("utf-8"))
        with patch("urllib.request.urlopen", return_value=mock_resp2):
            updater._check_worker(notify_if_latest=False)

        say_calls_after = [c for c in overlay.post.call_args_list if c[0][0] == "say"]
        self.assertEqual(len(say_calls_after), 1, "Spoken update notification repeated across background checks")


if __name__ == "__main__":
    unittest.main()
