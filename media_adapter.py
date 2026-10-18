"""
media_adapter.py — Windows System Media Transport Controls (SMTC) Adapter.

Connects to Windows SMTC using winrt-Windows.Media.Control to track and control
active media sessions (Spotify, YouTube in Chrome/Edge, Apple Music, VLC, etc.).

Features:
  - Multi-session handling (prioritizes active playing session, handles switching).
  - Retrieves rich metadata: Title, Artist, Album, Position, Duration, Playback Status,
    Source App ID, and Thumbnail stream bytes.
  - Dominant vibrant color extraction from album artwork using Pillow.
  - Missing artwork fallback generator.
  - Seeking with app-without-seek fallback.
  - Playback commands with debouncing: play_pause(), next_track(), previous_track(), seek().
  - Media-key fallback (pyautogui & Win32 keybd_event) when SMTC commands fail.
  - 100% unit-testable with mock backends; strictly NO Qt imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import io
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

# Try to import PIL for dominant color extraction and artwork generation
try:
    from PIL import Image, ImageDraw
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# SMTC Playback Status constants (from Windows.Media.Control.GlobalSystemMediaTransportControlsSessionPlaybackStatus)
STATUS_CLOSED = 0
STATUS_OPENED = 1
STATUS_CHANGING = 2
STATUS_STOPPED = 3
STATUS_PLAYING = 4
STATUS_PAUSED = 5

DEFAULT_DEBOUNCE_SEC = 0.25
DEFAULT_POLL_INTERVAL_SEC = 1.0
DEFAULT_ACCENT_COLOR = "#45C1EE"

# Minimal 1x1 PNG fallback byte string
_MINIMAL_FALLBACK_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00"
    b"\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xaf\xa4q"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def extract_dominant_color(
    thumbnail_bytes: Optional[bytes],
    default_hex: str = DEFAULT_ACCENT_COLOR,
) -> str:
    """
    Pure Python / Pillow dominant vibrant color extractor returning a hex string.
    Filters out near-black (lum < 28) and near-white (lum > 235 or max rgb > 230).
    Favors vibrant, saturated colors; falls back to default_hex if no candidates exist.
    """
    if not thumbnail_bytes or not _PIL_AVAILABLE:
        return default_hex

    try:
        im = Image.open(io.BytesIO(thumbnail_bytes)).convert("RGB")
        im.thumbnail((48, 48))
        colors = im.getcolors(maxcolors=48 * 48)
        if not colors:
            return default_hex

        best_color: Optional[Tuple[int, int, int]] = None
        best_score = -1.0

        for count, (r, g, b) in colors:
            # Luminance calculation (ITU-R BT.601)
            lum = 0.299 * r + 0.587 * g + 0.114 * b

            # Exclude near-black
            if lum < 28.0:
                continue

            # Exclude near-white
            if lum > 235.0 or (r > 230 and g > 230 and b > 230):
                continue

            max_c = max(r, g, b)
            min_c = min(r, g, b)
            if max_c == 0:
                continue

            # Saturation in [0.0, 1.0]
            sat = (max_c - min_c) / max_c

            # Favor saturated colors with balanced luminance
            lum_weight = 1.0 - abs(lum - 128.0) / 128.0
            score = count * (sat ** 1.3) * lum_weight

            if score > best_score:
                best_score = score
                best_color = (r, g, b)

        if best_color is None:
            # Fallback to most frequent non-extreme color
            sorted_colors = sorted(colors, key=lambda c: c[0], reverse=True)
            for count, (r, g, b) in sorted_colors:
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                if 25.0 <= lum <= 235.0:
                    best_color = (r, g, b)
                    break

        if best_color is not None:
            return f"#{best_color[0]:02X}{best_color[1]:02X}{best_color[2]:02X}"

    except Exception:
        pass

    return default_hex


def generate_fallback_artwork(
    title: str = "",
    artist: str = "",
    size: int = 128,
) -> bytes:
    """
    Generates a minimalist placeholder album art PNG bytes when official thumbnail is missing.
    """
    if not _PIL_AVAILABLE:
        return _MINIMAL_FALLBACK_PNG

    try:
        im = Image.new("RGBA", (size, size), (26, 28, 34, 255))
        draw = ImageDraw.Draw(im)

        # Subtle disc
        margin = size // 6
        draw.ellipse([margin, margin, size - margin, size - margin], fill=(45, 50, 62, 255))

        # Musical note glyph
        center_x = size // 2
        center_y = size // 2
        r = size // 9

        # Note heads and stems
        draw.ellipse([center_x - r * 2, center_y + r // 2, center_x - r, center_y + r * 2], fill=(120, 135, 165, 255))
        draw.ellipse([center_x, center_y + r // 2, center_x + r, center_y + r * 2], fill=(120, 135, 165, 255))
        draw.rectangle([center_x - r, center_y - r * 2, center_x - r + 3, center_y + r], fill=(120, 135, 165, 255))
        draw.rectangle([center_x + r, center_y - r * 2, center_x + r + 3, center_y + r], fill=(120, 135, 165, 255))
        draw.rectangle([center_x - r, center_y - r * 2, center_x + r + 3, center_y - r * 2 + 5], fill=(120, 135, 165, 255))

        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return _MINIMAL_FALLBACK_PNG


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
    dominant_color: Optional[str] = None
    last_updated: float = 0.0
    last_playback_change: float = 0.0

    @property
    def has_media(self) -> bool:
        return bool(self.title or self.artist)

    @property
    def has_thumbnail(self) -> bool:
        return bool(self.thumbnail_bytes)

    def get_dominant_color(self, default_hex: str = DEFAULT_ACCENT_COLOR) -> str:
        """Returns cached or extracted vibrant dominant color hex string."""
        if self.dominant_color:
            return self.dominant_color
        color = extract_dominant_color(self.thumbnail_bytes, default_hex)
        self.dominant_color = color
        return color

    def get_artwork_bytes(self, fallback: bool = True) -> Optional[bytes]:
        """Returns album artwork thumbnail bytes, or generated fallback if missing."""
        if self.thumbnail_bytes:
            return self.thumbnail_bytes
        if fallback:
            return generate_fallback_artwork(self.title, self.artist)
        return None


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

    async def seek(self, position_seconds: float) -> bool:
        ...

    def is_available(self) -> bool:
        ...

    def close(self) -> None:
        ...


class WinRTSMTCBackend:
    """
    Native Windows SMTC implementation using WinRT.
    Supports multi-session arbitration (prioritizes active playing sessions).
    """

    def __init__(self):
        self._manager = None
        self._available = False
        self._active_session = None
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

    def _select_active_session(self, mgr) -> Any:
        """
        Multi-session arbitration:
        1. Query all active media sessions from the manager.
        2. Prioritize sessions that are actively PLAYING.
        3. If none playing, prioritize PAUSED sessions.
        4. Fall back to mgr.get_current_session().
        """
        if not mgr:
            return None

        current_session = None
        try:
            current_session = mgr.get_current_session()
        except Exception:
            pass

        sessions: List[Any] = []
        try:
            raw_sessions = mgr.get_sessions()
            if raw_sessions:
                sessions = list(raw_sessions)
        except Exception:
            sessions = []

        if not sessions:
            self._active_session = current_session
            return current_session

        playing_sessions: List[Any] = []
        paused_sessions: List[Any] = []

        for s in sessions:
            try:
                info = s.get_playback_info()
                if info:
                    st = int(getattr(info, "playback_status", STATUS_STOPPED))
                    if st == STATUS_PLAYING:
                        playing_sessions.append(s)
                    elif st == STATUS_PAUSED:
                        paused_sessions.append(s)
            except Exception:
                continue

        # 1. Prioritize actively playing session
        if playing_sessions:
            if current_session in playing_sessions:
                chosen = current_session
            else:
                chosen = playing_sessions[0]
            self._active_session = chosen
            return chosen

        # 2. Prioritize paused session
        if paused_sessions:
            if current_session in paused_sessions:
                chosen = current_session
            else:
                chosen = paused_sessions[0]
            self._active_session = chosen
            return chosen

        self._active_session = current_session
        return current_session

    async def get_current_metadata(self) -> Optional[MediaMetadata]:
        mgr = await self._ensure_manager()
        if not mgr:
            return None

        try:
            session = self._select_active_session(mgr)
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
            meta = MediaMetadata(
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
            # Pre-extract dominant color if thumbnail is present
            if thumbnail_bytes:
                meta.get_dominant_color()

            return meta

        except Exception as exc:
            print(f"[media_adapter] SMTC fetch error: {exc}")
            return None

    async def play_pause(self) -> bool:
        mgr = await self._ensure_manager()
        if not mgr:
            return False
        try:
            session = self._active_session or self._select_active_session(mgr)
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
            session = self._active_session or self._select_active_session(mgr)
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
            session = self._active_session or self._select_active_session(mgr)
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
            session = self._active_session or self._select_active_session(mgr)
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
            session = self._active_session or self._select_active_session(mgr)
            if session:
                res = await session.try_pause_async()
                return bool(res)
        except Exception:
            pass
        return False

    async def seek(self, position_seconds: float) -> bool:
        mgr = await self._ensure_manager()
        if not mgr:
            return False
        try:
            session = self._active_session or self._select_active_session(mgr)
            if session:
                # Check if position change is supported
                info = session.get_playback_info()
                if info and hasattr(info, "controls") and info.controls:
                    if not getattr(info.controls, "is_playback_position_enabled", True):
                        return False
                # Convert seconds to 100-nanosecond units (1s = 10,000,000 hns)
                hns = int(max(0.0, position_seconds) * 10_000_000)
                res = await session.try_change_playback_position_async(hns)
                return bool(res)
        except Exception as exc:
            print(f"[media_adapter] SMTC seek error: {exc}")
        return False

    def close(self):
        self._manager = None
        self._active_session = None


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

    def _fallback_key(self, key_name: str) -> bool:
        """
        Sends simulated hardware media key when SMTC command fails.
        Tries pyautogui first, then Win32 user32 keybd_event.
        """
        if not self._use_fallback:
            return False

        sent = False
        try:
            import pyautogui
            pyautogui.press(key_name, _pause=False)
            sent = True
        except Exception as exc:
            print(f"[media_adapter] PyAutoGUI fallback '{key_name}' failed: {exc}")

        if not sent and sys.platform == "win32":
            try:
                import ctypes
                VK_MAP = {
                    "playpause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
                    "nexttrack": 0xB0,  # VK_MEDIA_NEXT_TRACK
                    "prevtrack": 0xB1,  # VK_MEDIA_PREV_TRACK
                    "stop": 0xB2,       # VK_MEDIA_STOP
                }
                vk = VK_MAP.get(key_name)
                if vk:
                    KEYEVENTF_EXTENDEDKEY = 0x0001
                    KEYEVENTF_KEYUP = 0x0002
                    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
                    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
                    sent = True
            except Exception as exc:
                print(f"[media_adapter] Win32 keybd_event fallback failed: {exc}")

        return sent

    def play_pause(self) -> bool:
        """Toggles play/pause with debouncing and media-key fallback."""
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
        """Skips to next track with debouncing and media-key fallback."""
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
        """Returns to previous track with debouncing and media-key fallback."""
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

    def seek(self, position_seconds: float) -> bool:
        """
        Seeks to position_seconds in the active track.
        If SMTC backend does not support seeking or fails, applies optimistic
        local position update as app-without-seek fallback.
        """
        if position_seconds < 0:
            position_seconds = 0.0

        with self._lock:
            if self._metadata and self._metadata.duration > 0:
                position_seconds = min(position_seconds, self._metadata.duration)

        success = False
        if self._backend.is_available():
            try:
                if self._loop and self._loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(
                        self._backend.seek(position_seconds), self._loop
                    )
                    success = fut.result(timeout=0.8)
                else:
                    success = asyncio.run(self._backend.seek(position_seconds))
            except Exception:
                success = False

        # App-without-seek fallback: Always maintain local timeline continuity
        with self._lock:
            if self._metadata:
                self._metadata.position = position_seconds
                self._metadata.last_updated = time.time()

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
