"""
tests/test_media_adapter.py — Unit tests for SMTC Media Session Adapter.

Verifies:
  - Metadata parsing (title, artist, album, timeline, thumbnail bytes, status).
  - Background async polling and thread safety.
  - Command debouncing (250 ms threshold prevents command floods).
  - PyAutoGUI fallback execution when backend fails or is unsupported.
  - Listener callbacks on metadata change.
  - Zero Qt imports; 100% testable with mock backends.
"""

import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from media_adapter import (
    MediaAdapter,
    MediaMetadata,
    SMTCBackendProtocol,
    get_media_adapter,
    set_media_adapter,
)


class MockSMTCBackend:
    """Mock SMTC backend simulating Windows SMTC sessions."""

    def __init__(self):
        self.metadata = MediaMetadata(
            title="Starboy",
            artist="The Weeknd",
            album="Starboy",
            position=30.0,
            duration=230.0,
            status="playing",
            is_playing=True,
            source_app_id="Spotify.exe",
            thumbnail_bytes=b"\x89PNG\r\n\x1a\nfakeimagebytes",
        )
        self.play_pause_calls = 0
        self.next_calls = 0
        self.prev_calls = 0
        self.play_pause_succeeds = True
        self.next_succeeds = True
        self.prev_succeeds = True
        self.available = True

    def is_available(self) -> bool:
        return self.available

    async def get_current_metadata(self):
        return self.metadata

    async def play_pause(self):
        self.play_pause_calls += 1
        if self.play_pause_succeeds:
            self.metadata.is_playing = not self.metadata.is_playing
            self.metadata.status = "playing" if self.metadata.is_playing else "paused"
            return True
        return False

    async def next_track(self):
        self.next_calls += 1
        return self.next_succeeds

    async def previous_track(self):
        self.prev_calls += 1
        return self.prev_succeeds

    async def play(self):
        self.metadata.is_playing = True
        return True

    async def pause(self):
        self.metadata.is_playing = False
        return True

    def close(self):
        pass


class TestMediaAdapter(unittest.TestCase):
    def setUp(self):
        self.mock_backend = MockSMTCBackend()
        self.adapter = MediaAdapter(
            backend=self.mock_backend,
            poll_interval=0.05,
            debounce_sec=0.20,
            use_pyautogui_fallback=True,
        )

    def tearDown(self):
        self.adapter.stop()

    def test_01_metadata_properties(self):
        """Verifies metadata extraction and has_media helper."""
        meta = self.mock_backend.metadata
        self.assertTrue(meta.has_media)
        self.assertEqual(meta.title, "Starboy")
        self.assertEqual(meta.artist, "The Weeknd")
        self.assertEqual(meta.album, "Starboy")
        self.assertEqual(meta.position, 30.0)
        self.assertEqual(meta.duration, 230.0)
        self.assertTrue(meta.is_playing)
        self.assertIsNotNone(meta.thumbnail_bytes)

        empty_meta = MediaMetadata()
        self.assertFalse(empty_meta.has_media)

    def test_02_sync_refresh_and_state(self):
        """Synchronous refresh updates and returns cached metadata."""
        meta = self.adapter.refresh_sync()
        self.assertIsNotNone(meta)
        self.assertEqual(meta.title, "Starboy")
        self.assertEqual(self.adapter.get_current_media().title, "Starboy")

    def test_03_listener_notification(self):
        """Listeners are called when metadata is updated."""
        received = []
        self.adapter.add_listener(lambda m: received.append(m))
        self.adapter.refresh_sync()

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].title, "Starboy")

    def test_04_command_debouncing(self):
        """Commands called within debounce window are suppressed."""
        # First call succeeds
        res1 = self.adapter.play_pause()
        self.assertTrue(res1)
        self.assertEqual(self.mock_backend.play_pause_calls, 1)

        # Immediate second call is suppressed by debounce
        res2 = self.adapter.play_pause()
        self.assertFalse(res2)
        self.assertEqual(self.mock_backend.play_pause_calls, 1)

        # After debounce expires, next command executes
        time.sleep(0.25)
        res3 = self.adapter.play_pause()
        self.assertTrue(res3)
        self.assertEqual(self.mock_backend.play_pause_calls, 2)

    @patch("pyautogui.press")
    def test_05_pyautogui_fallback_when_backend_fails(self, mock_press):
        """When SMTC backend returns False, falls back to pyautogui hardware media key."""
        self.mock_backend.play_pause_succeeds = False

        res = self.adapter.play_pause()
        self.assertTrue(res)
        mock_press.assert_called_with("playpause", _pause=False)

    @patch("pyautogui.press")
    def test_06_skip_commands_with_fallback(self, mock_press):
        """next_track and previous_track execute backend or fallback."""
        # Next track backend success
        self.adapter.next_track()
        self.assertEqual(self.mock_backend.next_calls, 1)

        time.sleep(0.25)
        # Previous track with backend failure triggers fallback
        self.mock_backend.prev_succeeds = False
        self.adapter.previous_track()
        mock_press.assert_called_with("prevtrack", _pause=False)

    def test_07_background_polling_thread(self):
        """Background thread polls and pushes updates to listeners."""
        self.mock_backend.metadata = MediaMetadata(title="Initial", artist="Artist")
        updates = []
        self.adapter.add_listener(lambda m: updates.append(m.title if m else ""))

        self.adapter.start()
        time.sleep(0.12)

        self.mock_backend.metadata = MediaMetadata(title="Second Song", artist="Artist 2")
        time.sleep(0.12)

        self.adapter.stop()
        self.assertIn("Initial", updates)
        self.assertIn("Second Song", updates)


if __name__ == "__main__":
    unittest.main()
