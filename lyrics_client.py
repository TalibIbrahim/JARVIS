"""Synced-lyrics client (LRCLIB, free, no API key). No Qt.

get_lyrics(title, artist, album, duration_s) -> Lyrics | None
Results (including "not found") are cached on disk under %APPDATA%\\JARVIS\\lyrics_cache.
Lyrics text is never logged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

import jarvis_log as _jlog

try:
    from version import APP_VERSION  # single source of truth for the app version
except Exception:  # pragma: no cover - updater import is optional in stripped test environments
    APP_VERSION = "unknown"
USER_AGENT = f"JARVIS/{APP_VERSION} (https://github.com/TalibIbrahim/JARVIS)"
BASE_URL = "https://lrclib.net/api"
TIMEOUT_S = 5.0
NEGATIVE_TTL_S = 7 * 24 * 3600.0
MAX_CACHE_FILES = 500
DURATION_TOLERANCE_S = 3.0

_log = _jlog.get_logger("lyrics")


class LyricsError(Exception):
    """Network/parse failure (distinct from 'not found')."""


@dataclass
class Lyrics:
    synced: List[Tuple[float, str]] = field(default_factory=list)
    plain: Optional[str] = None
    instrumental: bool = False


# ---- parsing ---------------------------------------------------------------

_TS_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META_RE = re.compile(r"^\s*\[[A-Za-z]+:[^\]]*\]\s*$")


def parse_lrc(text: str) -> List[Tuple[float, str]]:
    out: List[Tuple[float, str]] = []
    for raw in (text or "").splitlines():
        if _META_RE.match(raw) and not _TS_RE.match(raw.strip()):
            continue
        stamps = []
        pos = 0
        s = raw.strip()
        while True:
            m = _TS_RE.match(s, pos)
            if not m:
                break
            frac = m.group(3)
            f = float("0." + frac) if frac else 0.0
            stamps.append(int(m.group(1)) * 60 + int(m.group(2)) + f)
            pos = m.end()
        if not stamps:
            continue
        line = s[pos:].strip()
        for t in stamps:
            out.append((t, line))
    out.sort(key=lambda x: x[0])
    return out


# ---- normalization ---------------------------------------------------------

_BRACKET_RE = re.compile(
    r"\s*[\(\[][^\)\]]*(remaster|feat|ft\.|featuring|live|version|edit|mix|explicit|"
    r"slowed|reverb|sped|bonus|deluxe|mono|stereo|official|audio|lyrics?)[^\)\]]*[\)\]]",
    re.I)
_DASH_RE = re.compile(
    r"\s+-\s+(?:\d{4}\s+)?(remaster(ed)?|slowed|sped up|reverb|live|radio edit|"
    r"single version|album version|mono|stereo|bonus track|feat\.?.*|ft\.?.*)\b.*$",
    re.I)


def normalize_title(title: str) -> str:
    t = title or ""
    prev = None
    while prev != t:
        prev = t
        t = _BRACKET_RE.sub("", t)
        t = _DASH_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _key_norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def cache_key(title: str, artist: str, duration_s: float) -> str:
    raw = f"{_key_norm(normalize_title(title))}|{_key_norm(artist)}|{int(round(duration_s or 0))}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def default_cache_dir() -> str:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "JARVIS", "lyrics_cache")


# ---- client ----------------------------------------------------------------

class LyricsClient:
    def __init__(self, session: Any = None, cache_dir: Optional[str] = None,
                 clock: Callable[[], float] = time.time, max_files: int = MAX_CACHE_FILES):
        self._session = session
        self._cache_dir = cache_dir or default_cache_dir()
        self._clock = clock
        self._max_files = max_files
        self._lock = threading.Lock()

    # session (single, reused)
    def _sess(self):
        if self._session is None:
            import requests
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT})
            self._session = s
        return self._session

    def _http_get(self, path: str, params: dict):
        url = f"{BASE_URL}/{path}"
        assert url.startswith("https://")
        try:
            resp = self._sess().get(url, params=params, timeout=TIMEOUT_S,
                                    headers={"User-Agent": USER_AGENT})
        except Exception as exc:
            raise LyricsError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise LyricsError(f"HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LyricsError(f"bad JSON: {exc}") from exc

    # cache
    def _path(self, key: str) -> str:
        return os.path.join(self._cache_dir, key + ".json")

    def _cache_read(self, key: str):
        """Returns (hit, Lyrics|None)."""
        p = self._path(key)
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return False, None
        except (OSError, ValueError) as exc:
            _log.warning("lyrics cache unreadable (%s): %s", key[:8], exc)
            return False, None
        if d.get("not_found"):
            if self._clock() - float(d.get("saved_at", 0)) > NEGATIVE_TTL_S:
                return False, None
            self._touch(p)
            return True, None
        self._touch(p)
        return True, Lyrics(
            synced=[(float(t), str(x)) for t, x in d.get("synced", [])],
            plain=d.get("plain"),
            instrumental=bool(d.get("instrumental", False)),
        )

    def _touch(self, p: str) -> None:
        try:
            os.utime(p, None)
        except OSError as exc:
            _log.warning("lyrics cache touch failed: %s", exc)

    def _cache_write(self, key: str, lyr: Optional[Lyrics]) -> None:
        d = {"saved_at": self._clock()}
        if lyr is None:
            d["not_found"] = True
        else:
            d.update(synced=[[t, x] for t, x in lyr.synced], plain=lyr.plain,
                     instrumental=lyr.instrumental)
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._cache_dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self._path(key))
        except OSError as exc:
            _log.warning("lyrics cache write failed: %s", exc)
            return
        self._prune()

    def _prune(self) -> None:
        try:
            files = [os.path.join(self._cache_dir, n) for n in os.listdir(self._cache_dir)
                     if n.endswith(".json")]
            if len(files) <= self._max_files:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[: len(files) - self._max_files]:
                os.remove(p)
        except OSError as exc:
            _log.warning("lyrics cache prune failed: %s", exc)

    # lookup
    def _record(self, title: str, artist: str) -> None:
        try:
            from privacy_log import record_cloud_transmission
            record_cloud_transmission(
                destination="LRCLIB",
                payload_summary=f"Lyrics lookup for '{title}' by '{artist}'",
                metadata={"endpoint": "lrclib"},
            )
        except Exception:
            _log.exception("privacy log record failed")

    @staticmethod
    def _to_lyrics(d: dict) -> Optional[Lyrics]:
        if not isinstance(d, dict):
            return None
        synced = parse_lrc(d.get("syncedLyrics") or "")
        plain = d.get("plainLyrics") or None
        inst = bool(d.get("instrumental"))
        if not synced and not plain and not inst:
            return None
        return Lyrics(synced=synced, plain=plain, instrumental=inst)

    def _fetch_once(self, title: str, artist: str, album: str, duration_s: float) -> Optional[Lyrics]:
        self._record(title, artist)
        params = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if duration_s:
            params["duration"] = int(round(duration_s))
        data = self._http_get("get", params)
        if data is not None:
            return self._to_lyrics(data)
        results = self._http_get("search", {"track_name": title, "artist_name": artist})
        if not isinstance(results, list):
            return None
        best = None
        for r in results:
            if not isinstance(r, dict):
                continue
            if duration_s:
                try:
                    diff = abs(float(r.get("duration", 0)) - duration_s)
                except (TypeError, ValueError):
                    continue
                if diff > DURATION_TOLERANCE_S:
                    continue
                if best is None or diff < best[0]:
                    best = (diff, r)
            elif best is None:
                best = (0.0, r)
        return self._to_lyrics(best[1]) if best else None

    def get_lyrics(self, title: str, artist: str, album: str = "",
                   duration_s: float = 0.0) -> Optional[Lyrics]:
        """Lyrics or None (not found). Raises LyricsError on network/parse failure."""
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title:
            return None
        key = cache_key(title, artist, duration_s)
        with self._lock:
            hit, lyr = self._cache_read(key)
        if hit:
            return lyr
        lyr = self._fetch_once(title, artist, album, duration_s)
        if lyr is None:
            norm = normalize_title(title)
            if norm and norm != title:
                lyr = self._fetch_once(norm, artist, album, duration_s)
        with self._lock:
            self._cache_write(key, lyr)
        return lyr


_client: Optional[LyricsClient] = None
_client_lock = threading.Lock()


def get_lyrics_client() -> LyricsClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = LyricsClient()
        return _client


def get_lyrics(title: str, artist: str, album: str = "", duration_s: float = 0.0) -> Optional[Lyrics]:
    return get_lyrics_client().get_lyrics(title, artist, album, duration_s)
