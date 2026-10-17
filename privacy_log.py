"""
privacy_log.py — Privacy Audit Log & Cloud Transmission Tracker for J.A.R.V.I.S. (Phase 8).

Features:
- Thread-safe ring buffer keeping the last 50 cloud transmission events in memory.
- Automatic or explicit JSON synchronization to disk (privacy_log.json).
- Audits and records every external cloud transmission:
    * Groq STT (Whisper Audio Transcription)
    * Groq LLM (Text Chat Completions)
    * Groq Vision (Multimodal Screen/Scene Analysis)
    * Open-Meteo (Geocoding & Forecast APIs)
- Strictly NO Qt imports. 100% unit-testable with interfaces and mocks.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import datetime
import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union


import re

MAX_LOG_BYTES: int = 5 * 1024 * 1024  # Capped at 5 MB

_API_KEY_PATTERN = re.compile(r"gsk_[A-Za-z0-9_-]{16,}")
_SENSITIVE_KEYS = {"api_key", "groq_api_key", "key", "token", "secret", "authorization", "password"}


def _scrub_string(s: str) -> str:
    """Masks API keys and secrets in strings."""
    if not isinstance(s, str):
        return str(s)
    return _API_KEY_PATTERN.sub("[REDACTED_API_KEY]", s)


def _sanitize_privacy_data(obj: Any, key_name: str = "") -> Any:
    """
    Sanitizes privacy-sensitive payloads:
    - Never logs frames (numpy arrays, cv2 frames, image structures)
    - Never logs raw audio (bytes, audio buffers, PCM data)
    - Never logs API keys or tokens
    """
    low_key = key_name.lower()
    if low_key in _SENSITIVE_KEYS:
        return "[REDACTED_API_KEY]"

    # Check for image/frame objects (e.g. numpy ndarray)
    if hasattr(obj, "shape") and hasattr(obj, "ndim"):
        return f"[FRAME DATA REDACTED shape={list(obj.shape)}]"
    if "frame" in low_key or "image" in low_key:
        if isinstance(obj, (bytes, bytearray)):
            return f"[FRAME DATA REDACTED ({len(obj)} bytes)]"

    # Check for raw audio bytes or buffers
    if "audio" in low_key or "wav" in low_key or "pcm" in low_key:
        if isinstance(obj, (bytes, bytearray)):
            return f"[AUDIO DATA REDACTED ({len(obj)} bytes)]"
        if hasattr(obj, "get_raw_data"):
            return "[AUDIO DATA REDACTED]"

    if isinstance(obj, (bytes, bytearray)):
        if len(obj) > 64:
            return f"[BINARY DATA REDACTED ({len(obj)} bytes)]"
        return "[BINARY DATA REDACTED]"

    if isinstance(obj, str):
        return _scrub_string(obj)
    if isinstance(obj, dict):
        return {str(k): _sanitize_privacy_data(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, deque)):
        return [_sanitize_privacy_data(v, key_name) for v in obj]
    return obj


def get_default_privacy_log_path(explicit_path: Optional[str] = None) -> str:
    """
    Returns persistent privacy log JSON path.
    Uses %APPDATA%\\Jarvis\\privacy_log.json if APPDATA exists,
    falling back to local privacy_log.json next to source files.
    """
    if explicit_path:
        return explicit_path

    appdata = os.environ.get("APPDATA")
    if appdata:
        jarvis_dir = os.path.join(appdata, "Jarvis")
        try:
            os.makedirs(jarvis_dir, exist_ok=True)
            return os.path.join(jarvis_dir, "privacy_log.json")
        except Exception:
            pass

    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "privacy_log.json")


def _json_safe(obj: Any) -> Any:
    """Recursively converts objects to JSON-serializable primitives."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, deque)):
        return [_json_safe(v) for v in obj]
    return str(obj)


@dataclass
class PrivacyEvent:
    """Represents a single external cloud transmission record."""
    timestamp: float = field(default_factory=time.time)
    iso_timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    destination: str = ""
    payload_summary: str = ""
    status: str = "success"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "iso_timestamp": self.iso_timestamp,
            "destination": _scrub_string(self.destination),
            "payload_summary": _scrub_string(self.payload_summary),
            "status": self.status,
            "metadata": _json_safe(_sanitize_privacy_data(self.metadata)),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PrivacyEvent:
        return cls(
            timestamp=float(data.get("timestamp", time.time())),
            iso_timestamp=str(data.get("iso_timestamp", "")),
            destination=_scrub_string(str(data.get("destination", ""))),
            payload_summary=_scrub_string(str(data.get("payload_summary", ""))),
            status=str(data.get("status", "success")),
            metadata=_sanitize_privacy_data(dict(data.get("metadata", {}))),
        )


class PrivacyLogProtocol(Protocol):
    """Protocol interface for Privacy Log implementations."""

    def record(
        self,
        destination: str,
        payload_summary: str,
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "success",
    ) -> PrivacyEvent:
        ...

    def get_events(self, limit: Optional[int] = None, reverse: bool = False) -> List[Dict[str, Any]]:
        ...

    def clear(self) -> None:
        ...

    def sync_to_disk(self) -> bool:
        ...


class PrivacyLog:
    """
    Thread-safe ring buffer (last 50 events) with persistent JSON synchronization
    and 5 MB capped rotation. Never logs frames, raw audio, or API keys.
    """

    def __init__(
        self,
        max_events: int = 50,
        log_file: Optional[str] = None,
        auto_sync: bool = True,
        max_file_bytes: int = MAX_LOG_BYTES,
    ):
        self.max_events = max(1, int(max_events))
        self.log_file = log_file or get_default_privacy_log_path()
        self.auto_sync = auto_sync
        self.max_file_bytes = max(1024, int(max_file_bytes))
        self._lock = threading.Lock()
        self._events: deque[PrivacyEvent] = deque(maxlen=self.max_events)
        self.load_from_disk()

    def record(
        self,
        destination: str,
        payload_summary: str,
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "success",
    ) -> PrivacyEvent:
        """
        Appends a sanitized event to the ring buffer and syncs to JSON storage.
        """
        now = time.time()
        iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        safe_meta = _sanitize_privacy_data(metadata or {})
        safe_summary = _scrub_string(str(payload_summary).strip())
        safe_dest = _scrub_string(str(destination).strip())

        evt = PrivacyEvent(
            timestamp=now,
            iso_timestamp=iso,
            destination=safe_dest,
            payload_summary=safe_summary,
            status=str(status).strip(),
            metadata=dict(safe_meta),
        )

        with self._lock:
            self._events.append(evt)

        if self.auto_sync:
            self.sync_to_disk()

        return evt

    def get_events(self, limit: Optional[int] = None, reverse: bool = False) -> List[Dict[str, Any]]:
        """
        Returns snapshot of recorded events as dicts.
        If reverse is True, returns most recent first.
        """
        with self._lock:
            events = [e.to_dict() for e in self._events]

        if reverse:
            events.reverse()

        if limit is not None and limit > 0:
            return events[:limit]
        return events

    def get_event_objects(self) -> List[PrivacyEvent]:
        """Returns shallow copy of PrivacyEvent objects."""
        with self._lock:
            return list(self._events)

    def count(self) -> int:
        """Returns total number of events currently in the buffer."""
        with self._lock:
            return len(self._events)

    def clear(self) -> None:
        """Clears all events and updates disk store."""
        with self._lock:
            self._events.clear()

        if self.auto_sync:
            self.sync_to_disk()

    def rotate_if_needed(self) -> bool:
        """Rotates the log file if its size reaches or exceeds max_file_bytes (5 MB)."""
        if not self.log_file or not os.path.exists(self.log_file):
            return False
        try:
            size = os.path.getsize(self.log_file)
            if size >= self.max_file_bytes:
                backup = f"{self.log_file}.1"
                if os.path.exists(backup):
                    try:
                        os.remove(backup)
                    except Exception:
                        pass
                os.replace(self.log_file, backup)
                return True
        except Exception as exc:
            print(f"[privacy_log] rotation error: {exc}", file=sys.stderr)
        return False

    def sync_to_disk(self) -> bool:
        """Writes current ring buffer events to JSON file, rotating if file exceeds 5 MB."""
        if not self.log_file:
            return False

        with self._lock:
            raw_data = [e.to_dict() for e in self._events]

        self.rotate_if_needed()

        try:
            parent = os.path.dirname(os.path.abspath(self.log_file))
            if parent:
                os.makedirs(parent, exist_ok=True)

            content = json.dumps(raw_data, indent=2, default=str)
            while len(content.encode("utf-8")) > self.max_file_bytes and len(raw_data) > 1:
                raw_data.pop(0)
                content = json.dumps(raw_data, indent=2, default=str)

            tmp_file = f"{self.log_file}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.log_file)
            return True
        except Exception as exc:
            # Must not crash main application loop on disk IO failure
            print(f"[privacy_log] Warning: Failed to sync log to {self.log_file}: {exc}", file=sys.stderr)
            return False

    def load_from_disk(self) -> int:
        """Loads events from JSON file into ring buffer. Returns loaded count."""
        if not self.log_file or not os.path.exists(self.log_file):
            return 0

        try:
            with open(self.log_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                return 0

            loaded = []
            for item in raw:
                if isinstance(item, dict):
                    loaded.append(PrivacyEvent.from_dict(item))

            with self._lock:
                self._events.clear()
                # Keep up to max_events
                for ev in loaded[-self.max_events:]:
                    self._events.append(ev)
                return len(self._events)
        except Exception as exc:
            print(f"[privacy_log] Note: Could not load existing log from {self.log_file}: {exc}", file=sys.stderr)
            return 0


# Global singleton instance
_PRIVACY_LOG: Optional[PrivacyLog] = None
_INIT_LOCK = threading.Lock()


def get_privacy_log(log_file: Optional[str] = None) -> PrivacyLog:
    """Retrieves or initializes the global PrivacyLog singleton."""
    global _PRIVACY_LOG
    with _INIT_LOCK:
        if _PRIVACY_LOG is None:
            _PRIVACY_LOG = PrivacyLog(max_events=50, log_file=log_file)
        elif log_file and _PRIVACY_LOG.log_file != log_file:
            _PRIVACY_LOG = PrivacyLog(max_events=50, log_file=log_file)
        return _PRIVACY_LOG


def record_cloud_transmission(
    destination: str,
    payload_summary: str,
    metadata: Optional[Dict[str, Any]] = None,
    status: str = "success",
) -> PrivacyEvent:
    """Convenience helper to record a cloud transmission event to the singleton."""
    return get_privacy_log().record(
        destination=destination,
        payload_summary=payload_summary,
        metadata=metadata,
        status=status,
    )


def reset_privacy_log() -> None:
    """Resets the singleton instance (useful for unit tests)."""
    global _PRIVACY_LOG
    with _INIT_LOCK:
        if _PRIVACY_LOG is not None:
            _PRIVACY_LOG.clear()
        _PRIVACY_LOG = None
