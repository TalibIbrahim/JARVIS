"""
media_adapter.py — Windows System Media Transport Controls (SMTC) Adapter.

Connects to Windows SMTC using winrt-Windows.Media.Control to track and control
active media sessions (Spotify, YouTube in Chrome/Edge, Apple Music, VLC, etc.).

Features:
  - Thread-safe background polling and listener.
  - Retrieves rich metadata: Title, Artist, Album, Position, Duration, Playback Status,
    Source App ID, and Thumbnail stream bytes.
  - Playback commands with debouncing: play_pause(), next_track(), previous_track().
  - Graceful fallback to hardware media key events (pyautogui) when SMTC commands
    are unsupported or unavailable.
  - 100% unit-testable with mock backends; strictly NO Qt imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
import threading
import time
from typing import Any, Callable, List, Optional, Protocol


# SMTC Playback Status constants (from Windows.Media.Control.GlobalSystemMediaTransportControlsSessionPlaybackStatus)
STATUS_CLOSED = 0
STATUS_OPENED = 1
STATUS_CHANGING = 2
STATUS_STOPPED = 3
STATUS_PLAYING = 4
STATUS_PAUSED = 5

DEFAULT_DEBOUNCE_SEC = 0.25
DEFAULT_POLL_INTERVAL_SEC = 1.0


@dataclass
class MediaMetadata:
    title: str = ""
    artist: str = ""
    album: str = ""
    position: float = 0.0          # seconds
    duration: float = 0.0          # seconds
    status: str = "stopped"        # "playing", "paused", "stopped", "changing"
    is_playing: bool = False
    source_app_id: str = ""
    thumbnail_bytes: Optional[bytes] = None
    last_updated: float = 0.0
    last_playback_change: float = 0.0

    @property
    def has_media(self) -> bool:
        return bool(self.title or self.artist)


class SMTCBackendProtocol(Protocol):
    """Protocol defining the interface for SMTC session operations."""

    async def get_current_metadata(self) -> Optional[MediaMetadata]:
        ...

    async def play_pause(self) -> bool:
        ...

    async def next_track(self) -> bool:
        ...

    async def previous_track(self) -> bool:
        ...

    async def play(self) -> bool:
        ...

    async def pause(self) -> bool:
        ...

    def is_available(self) -> bool:
        ...

    def close(self) -> None:
        ...


class WinRTSMTCBackend:
    """
    Native Windows SMTC implementation using WinRT.
    """

    def __init__(self):
        self._manager = None
        self._available = False
        self._check_availability()

    def _check_availability(self):
        if sys.platform != "win32":
            self._available = False
            return
        try:
            import winrt.windows.media.control as wmc
            self._available = True
        except Exception:
            self._available = False

    def is_available(self) -> bool:
        return self._available

    async def _ensure_manager(self):
        if not self._available:
            return None
        if self._manager is None:
            try:
                import winrt.windows.media.control as wmc
                self._manager = await wmc.GlobalSystemMediaTransportControlsSessionManager.request_async()
            except Exception as exc:
                print(f"[media_adapter] Failed to request SMTC manager: {exc}")
                self._available = False
                return None
        return self._manager

    async def get_current_metadata(self) -> Optional[MediaMetadata]:
        mgr = await self._ensure_manager()
        if not mgr:
            return None

        try:
            session = mgr.get_current_session()
            if not session:
                return None

            source_app_id = str(getattr(session, "source_app_user_model_id", "") or "")

            # 1. Media Properties
            title = ""
            artist = ""
            album = ""
            thumbnail_bytes: Optional[bytes] = None
            try:
                props = await session.try_get_media_properties_async()
                if props:
                    title = str(getattr(props, "title", "") or "")
                    artist = str(getattr(props, "artist", "") or "")
                    album = str(getattr(props, "album_title", "") or "")
                    thumb_ref = getattr(props, "thumbnail", None)
                    if thumb_ref:
                        try:
                            stream = await thumb_ref.open_read_async()
                            import winrt.windows.storage.streams as wss
                            reader = wss.DataReader(stream)
                            await reader.load_async(stream.size)
                            buf = bytearray(stream.size)
                            reader.read_bytes(buf)
                            thumbnail_bytes = bytes(buf)
                        except Exception:
                            thumbnail_bytes = None
            except Exception as exc:
                print(f"[media_adapter] Error reading media properties: {exc}")

            # 2. Playback Info
            status_str = "stopped"
            is_playing = False
            try:
                info = session.get_playback_info()
                if info:
                    st_val = int(getattr(info, "playback_status", STATUS_STOPPED))
                    if st_val == STATUS_PLAYING:
                        status_str = "playing"
                        is_playing = True
                    elif st_val == STATUS_PAUSED:
                        status_str = "paused"
                        is_playing = False
                    elif st_val == STATUS_CHANGING:
                        status_str = "changing"
                        is_playing = False
                    else:
                        status_str = "stopped"
                        is_playing = False
            except Exception:
                pass

            # 3. Timeline Properties
            position = 0.0
            duration = 0.0
            try:
                tl = session.get_timeline_properties()
                if tl:
                    if hasattr(tl, "position") and tl.position:
                        position = float(tl.position.total_seconds())
                    if hasattr(tl, "end_time") and tl.end_time:
                        duration = float(tl.end_time.total_seconds())
            except Exception:
                pass

            now = time.time()
            return MediaMetadata(
                title=title,
                artist=artist,
                album=album,
                position=position,
                duration=duration,
                status=status_str,
                is_playing=is_playing,
                source_app_id=source_app_id,
                thumbnail_bytes=thumbnail_bytes,
                last_updated=now,
                last_playback_change=now,
            )

        except Exception as exc:
            print(f"[media_adapter] SMTC fetch error: {exc}")
            return None

    async def play_pause(self) -> bool:
        mgr = await self._ensure_manager()
        if not mgr:
            return False
        try:
            session = mgr.get_current_session()
            if session:
                res = await session.try_toggle_play_pause_async()
                return bool(res)
        except Exception as exc:
            print(f"[media_adapter] SMTC play_pause error: {exc}")
        return False

    async def next_track(self) -> bool:
        mgr = await self._ensure_manager()
        if not mgr:
            return False
        try:
            session = mgr.get_current_session()
            if session:
                res = await session.try_skip_next_async()
                return bool(res)
        except Exception as exc:
            print(f"[media_adapter] SMTC next_track error: {exc}")
        return False

    async def previous_track(self) -> bool:
        mgr = await self._ensure_manager()
        if not mgr:
            return False
        try:
            session = mgr.get_current_session()
            if session:
                res = await session.try_skip_previous_async()
                return bool(res)
        except Exception as exc:
            print(f"[media_adapter] SMTC previous_track error: {exc}")
        return False

    async def play(self) -> bool:
        mgr = await self._ensure_manager()
        if not mgr:
            return False
        try:
            session = mgr.get_current_session()
            if session:
                res = await session.try_play_async()
                return bool(res)
        except Exception:
            pass
        return False

    async def pause(self) -> bool:
        mgr = await self._ensure_manager()
        if not mgr:
            return False
        try:
            session = mgr.get_current_session()
            if session:
                res = await session.try_pause_async()
                return bool(res)
        except Exception:
            pass
        return False

    def close(self):
        self._manager = None


class MediaAdapter:
    """
    Thread-safe media session manager with background polling and debounced controls.
    """

    def __init__(
        self,
        backend: Optional[SMTCBackendProtocol] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
        debounce_sec: float = DEFAULT_DEBOUNCE_SEC,
        use_pyautogui_fallback: bool = True,
    ):
        self._backend: SMTCBackendProtocol = backend or WinRTSMTCBackend()
        self._poll_interval = poll_interval
        self._debounce_sec = debounce_sec
        self._use_fallback = use_pyautogui_fallback

        self._lock = threading.Lock()
        self._metadata: Optional[MediaMetadata] = None
        self._last_command_time: float = 0.0

        self._listeners: List[Callable[[Optional[MediaMetadata]], None]] = []

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        """Starts background async polling thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, name="MediaAdapterWorker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0):
        """Stops background async polling thread."""
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._backend.close()

    def add_listener(self, callback: Callable[[Optional[MediaMetadata]], None]):
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Optional[MediaMetadata]], None]):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def get_current_media(self) -> Optional[MediaMetadata]:
        with self._lock:
            return self._metadata

    def refresh_sync(self) -> Optional[MediaMetadata]:
        """Synchronously queries metadata from the backend."""
        try:
            coro = self._backend.get_current_metadata()
            # If called from a thread without loop, run directly
            meta = asyncio.run(coro)
            self._update_metadata(meta)
            return meta
        except Exception:
            return self.get_current_media()

    def _update_metadata(self, new_meta: Optional[MediaMetadata]):
        with self._lock:
            old = self._metadata
            # Track playback status transitions
            if new_meta and old:
                if new_meta.is_playing != old.is_playing:
                    new_meta.last_playback_change = time.time()
                else:
                    new_meta.last_playback_change = old.last_playback_change
            self._metadata = new_meta
            listeners = list(self._listeners)

        for l in listeners:
            try:
                l(new_meta)
            except Exception as exc:
                print(f"[media_adapter] Listener callback error: {exc}")

    def _worker_loop(self):
        """Background thread executing the asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def poll_coro():
            while not self._stop_event.is_set():
                try:
                    meta = await self._backend.get_current_metadata()
                    self._update_metadata(meta)
                except Exception as exc:
                    print(f"[media_adapter] Polling error: {exc}")
                await asyncio.sleep(self._poll_interval)

        try:
            self._loop.run_until_complete(poll_coro())
        except Exception:
            pass
        finally:
            self._loop.close()

    def _debounce_check(self) -> bool:
        """Returns True if command is allowed, False if suppressed by debounce."""
        now = time.time()
        if now - self._last_command_time < self._debounce_sec:
            return False
        self._last_command_time = now
        return True

    def _fallback_key(self, key_name: str):
        if not self._use_fallback:
            return
        try:
            import pyautogui
            pyautogui.press(key_name, _pause=False)
        except Exception as exc:
            print(f"[media_adapter] PyAutoGUI fallback '{key_name}' failed: {exc}")

    def play_pause(self) -> bool:
        """Toggles play/pause with debouncing and pyautogui fallback."""
        if not self._debounce_check():
            return False

        success = False
        if self._backend.is_available():
            try:
                if self._loop and self._loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(self._backend.play_pause(), self._loop)
                    success = fut.result(timeout=0.6)
                else:
                    success = asyncio.run(self._backend.play_pause())
            except Exception:
                success = False

        if not success:
            self._fallback_key("playpause")
            # Optimistically flip playback state if known
            with self._lock:
                if self._metadata:
                    self._metadata.is_playing = not self._metadata.is_playing
                    self._metadata.status = "playing" if self._metadata.is_playing else "paused"
                    self._metadata.last_playback_change = time.time()
            return True

        return success

    def next_track(self) -> bool:
        """Skips to next track with debouncing and pyautogui fallback."""
        if not self._debounce_check():
            return False

        success = False
        if self._backend.is_available():
            try:
                if self._loop and self._loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(self._backend.next_track(), self._loop)
                    success = fut.result(timeout=0.6)
                else:
                    success = asyncio.run(self._backend.next_track())
            except Exception:
                success = False

        if not success:
            self._fallback_key("nexttrack")
            return True

        return success

    def previous_track(self) -> bool:
        """Returns to previous track with debouncing and pyautogui fallback."""
        if not self._debounce_check():
            return False

        success = False
        if self._backend.is_available():
            try:
                if self._loop and self._loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(self._backend.previous_track(), self._loop)
                    success = fut.result(timeout=0.6)
                else:
                    success = asyncio.run(self._backend.previous_track())
            except Exception:
                success = False

        if not success:
            self._fallback_key("prevtrack")
            return True

        return success


# Global singleton adapter
_MEDIA_ADAPTER: Optional[MediaAdapter] = None


def get_media_adapter() -> MediaAdapter:
    global _MEDIA_ADAPTER
    if _MEDIA_ADAPTER is None:
        _MEDIA_ADAPTER = MediaAdapter()
    return _MEDIA_ADAPTER


def set_media_adapter(adapter: Optional[MediaAdapter]) -> None:
    global _MEDIA_ADAPTER
    _MEDIA_ADAPTER = adapter
