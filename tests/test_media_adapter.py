"""
tests/test_media_adapter.py — Unit tests for SMTC Media Session Adapter.

Verifies:
  - Metadata parsing (title, artist, album, timeline, thumbnail bytes, status).
  - Dominant vibrant color extraction from artwork (excluding near-black/near-white).
  - Missing artwork fallback generation.
  - Timeline seeking with app-without-seek local fallback.
  - Multi-session arbitration (prioritizing actively playing session over paused).
  - Command debouncing (250 ms threshold prevents command floods).
  - Hardware media key fallback execution when backend fails or is unsupported.
  - Background async polling and thread safety.
  - Zero Qt imports; 100% testable with mock backends.
"""

import asyncio
import io
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from media_adapter import (
    MediaAdapter,
    MediaMetadata,
    SMTCBackendProtocol,
    WinRTSMTCBackend,
    extract_dominant_color,
    generate_fallback_artwork,
    get_media_adapter,
    set_media_adapter,
    STATUS_PLAYING,
    STATUS_PAUSED,
    STATUS_STOPPED,
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
        self.seek_calls = 0
        self.last_seek_pos = 0.0
        self.play_pause_succeeds = True
        self.next_succeeds = True
        self.prev_succeeds = True
        self.seek_succeeds = True
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

    async def seek(self, position_seconds: float) -> bool:
        self.seek_calls += 1
        self.last_seek_pos = position_seconds
        if self.seek_succeeds:
            self.metadata.position = position_seconds
            return True
        return False

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
        """Verifies metadata extraction and has_media / has_thumbnail helpers."""
        meta = self.mock_backend.metadata
        self.assertTrue(meta.has_media)
        self.assertTrue(meta.has_thumbnail)
        self.assertEqual(meta.title, "Starboy")
        self.assertEqual(meta.artist, "The Weeknd")
        self.assertEqual(meta.album, "Starboy")
        self.assertEqual(meta.position, 30.0)
        self.assertEqual(meta.duration, 230.0)
        self.assertTrue(meta.is_playing)
        self.assertIsNotNone(meta.thumbnail_bytes)

        empty_meta = MediaMetadata()
        self.assertFalse(empty_meta.has_media)
        self.assertFalse(empty_meta.has_thumbnail)

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
        """When SMTC backend returns False, falls back to hardware media key."""
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

    # ──────────────────────────────────────────────────────────────────────────
    # New Phase 2 Tests: Seeking, Dominant Color, Artwork, Multi-Session
    # ──────────────────────────────────────────────────────────────────────────

    def test_08_seek_success(self):
        """Timeline seeking invokes backend and updates position."""
        self.adapter.refresh_sync()
        success = self.adapter.seek(45.5)

        self.assertTrue(success)
        self.assertEqual(self.mock_backend.seek_calls, 1)
        self.assertEqual(self.mock_backend.last_seek_pos, 45.5)
        self.assertEqual(self.adapter.get_current_media().position, 45.5)

    def test_09_seek_app_without_seek_fallback(self):
        """When SMTC seek returns False, applies optimistic local position fallback."""
        self.adapter.refresh_sync()
        self.mock_backend.seek_succeeds = False

        success = self.adapter.seek(120.0)
        self.assertFalse(success)
        self.assertEqual(self.mock_backend.seek_calls, 1)
        # Position is still updated locally as app-without-seek fallback
        self.assertEqual(self.adapter.get_current_media().position, 120.0)

    def test_10_seek_clamps_to_duration_and_zero(self):
        """Seeking beyond track bounds clamps to 0 and duration."""
        self.adapter.refresh_sync()

        # Negative position clamped to 0.0
        self.adapter.seek(-10.0)
        self.assertEqual(self.mock_backend.last_seek_pos, 0.0)

        # Over-duration clamped to duration (230.0)
        self.adapter.seek(9999.0)
        self.assertEqual(self.mock_backend.last_seek_pos, 230.0)

    def test_11_extract_dominant_color_vibrant(self):
        """Dominant color extraction extracts vibrant color from album artwork."""
        # Create an in-memory image with vibrant red (#E61919)
        img = Image.new("RGB", (64, 64), color=(230, 25, 25))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()

        color = extract_dominant_color(raw_bytes)
        self.assertEqual(color.upper(), "#E61919")

    def test_12_extract_dominant_color_fallback_and_edge_cases(self):
        """None, empty, pure black, and pure white fall back cleanly."""
        default = "#45C1EE"
        self.assertEqual(extract_dominant_color(None, default), default)
        self.assertEqual(extract_dominant_color(b"", default), default)
        self.assertEqual(extract_dominant_color(b"not_an_image", default), default)

        # Pure black (0, 0, 0) is excluded as near-black
        img_black = Image.new("RGB", (32, 32), color=(0, 0, 0))
        buf_black = io.BytesIO()
        img_black.save(buf_black, format="PNG")
        self.assertEqual(extract_dominant_color(buf_black.getvalue(), default), default)

        # Pure white (255, 255, 255) is excluded as near-white
        img_white = Image.new("RGB", (32, 32), color=(255, 255, 255))
        buf_white = io.BytesIO()
        img_white.save(buf_white, format="PNG")
        self.assertEqual(extract_dominant_color(buf_white.getvalue(), default), default)

    def test_13_missing_artwork_fallback_generation(self):
        """generate_fallback_artwork returns valid PNG bytes when artwork is missing."""
        png_bytes = generate_fallback_artwork("Blinding Lights", "The Weeknd")
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png_bytes), 100)

        # MediaMetadata get_artwork_bytes fallback integration
        meta_no_art = MediaMetadata(title="Song", artist="Artist", thumbnail_bytes=None)
        self.assertIsNone(meta_no_art.get_artwork_bytes(fallback=False))
        art = meta_no_art.get_artwork_bytes(fallback=True)
        self.assertIsNotNone(art)
        self.assertTrue(art.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_14_multi_session_arbitration(self):
        """_select_active_session prioritizes actively playing session over paused ones."""
        backend = WinRTSMTCBackend()

        # Mock Session 1: Paused Spotify
        s1 = MagicMock()
        info1 = MagicMock()
        info1.playback_status = STATUS_PAUSED
        s1.get_playback_info.return_value = info1

        # Mock Session 2: Actively playing Edge / YouTube
        s2 = MagicMock()
        info2 = MagicMock()
        info2.playback_status = STATUS_PLAYING
        s2.get_playback_info.return_value = info2

        # Mock Manager with both sessions
        mock_mgr = MagicMock()
        mock_mgr.get_sessions.return_value = [s1, s2]
        mock_mgr.get_current_session.return_value = s1  # Windows OS default might still point to s1

        # Multi-session arbitration should pick s2 because it is PLAYING!
        chosen = backend._select_active_session(mock_mgr)
        self.assertEqual(chosen, s2)

        # If s2 pauses and s1 stays paused, picks current session
        info2.playback_status = STATUS_PAUSED
        chosen_paused = backend._select_active_session(mock_mgr)
        self.assertIn(chosen_paused, [s1, s2])

    def test_15_long_paused_session_retained_when_stopped_and_no_current(self):
        """When a video has been paused for a long time (stopped, no active OS session), session is retained."""
        backend = WinRTSMTCBackend()

        s_video = MagicMock()
        info_video = MagicMock()
        info_video.playback_status = STATUS_STOPPED
        s_video.get_playback_info.return_value = info_video

        # Simulate s_video being previously active before being paused for a long time
        backend._active_session = s_video

        mock_mgr = MagicMock()
        mock_mgr.get_sessions.return_value = [s_video]
        mock_mgr.get_current_session.return_value = None  # Windows drops current_session after long pause

        chosen = backend._select_active_session(mock_mgr)
        self.assertEqual(chosen, s_video)

    def test_16_play_pause_falls_back_to_try_play_when_toggle_fails(self):
        """When try_toggle_play_pause_async returns False on long-paused session, calls try_play_async."""
        backend = WinRTSMTCBackend()
        s = MagicMock()

        async def mock_toggle():
            return False

        async def mock_play():
            return True

        s.try_toggle_play_pause_async = mock_toggle
        s.try_play_async = mock_play
        backend._active_session = s

        mock_mgr = MagicMock()
        backend._manager = mock_mgr

        res = asyncio.run(backend.play_pause())
        self.assertTrue(res)

    def test_17_adapter_play_and_pause_methods(self):
        """MediaAdapter provides play() and pause() methods that update playback state."""
        adapter = MediaAdapter(self.mock_backend, debounce_sec=0.0)
        self.mock_backend.metadata.is_playing = False
        adapter.refresh_sync()

        self.assertTrue(adapter.play())
        self.assertTrue(adapter.get_current_media().is_playing)

        self.assertTrue(adapter.pause())
        self.assertFalse(adapter.get_current_media().is_playing)


if __name__ == "__main__":
    unittest.main()
