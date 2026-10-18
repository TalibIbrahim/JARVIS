from __future__ import annotations
"""
Groq AI integration module for J.A.R.V.I.S.
Provides:
- Lightweight HTTP client for Groq's OpenAI-compatible API using requests.
- Secure key storage via Windows Credential Manager (keyring) with env var fallback.
- Audio transcription via Whisper models (whisper-large-v3-turbo).
- Fast low-latency chat completions (llama-3.1-8b-instant).
- Free-tier client-side rate limiting and Retry-After handling.
- Strict action whitelisting, JSON parsing, and conversation history management.
"""
import datetime
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple, Any, Iterator, Union, Callable


class _LazyRequestsProxy:
    """Lazily loads requests on first network invocation to eliminate startup penalty."""
    def __getattr__(self, name):
        import requests
        return getattr(requests, name)


requests = _LazyRequestsProxy()

_ORIG_POST = None
_ORIG_GET = None

def _get_orig_requests():
    global _ORIG_POST, _ORIG_GET
    if _ORIG_POST is None:
        import requests as real_requests
        _ORIG_POST = real_requests.post
        _ORIG_GET = real_requests.get
    return _ORIG_POST, _ORIG_GET

def _is_mocked(func: Any) -> bool:
    if func is None:
        return False
    return getattr(func, "_mock_name", None) is not None or "mock" in type(func).__name__.lower()

_SESSION: Optional[Any] = None
_SESSION_LOCK = threading.Lock()

def get_persistent_session() -> Any:
    """Returns persistent requests.Session with HTTP Keep-Alive and connection pooling."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                import requests as real_requests
                from requests.adapters import HTTPAdapter
                s = real_requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=10,
                    pool_maxsize=10,
                    max_retries=0,
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _SESSION = s
    return _SESSION
try:
    import certifi
    if getattr(sys, "frozen", False):
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
SERVICE_NAME = "JARVIS"
KEY_USERNAME = "groq_api_key"

# Default models
DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"
DEFAULT_STT_MODEL = "whisper-large-v3-turbo"
DEFAULT_VISION_MODEL = "qwen/qwen3.8-27b"

# Whitelist of actions the LLM is permitted to request
WHITELIST_ACTIONS = {
    "none",
    "diagnostics",
    "house_party",
    "stealth_mode",
    "clean_slate",
    "target_practice",
    "volume_up",
    "volume_down",
    "mute",
    "play_pause",
    "next_track",
    "previous_track",
    "open_app",
    "open_site",
    "search",
    "screenshot",
    "lock_workstation",
    "show_desktop",
    "minimize_window",
    "maximize_window",
    "switch_window",
    "close_window",
    "dwell_toggle",
    "precision_mode",
    "tracking_mode",
    "power_mode",
    "status",
    "play_music",
    "weather",
    "time",
    "now_playing",
    "show_widget",
    "toggle_widgets",
    "open_folder",
    "open_file",
    "open_recent_file",
    "show_last_download",
    "remember_folder",
    # Phase 5 System Control Actions
    "set_volume",
    "get_volume",
    "brightness_up",
    "brightness_down",
    "set_brightness",
    "get_brightness",
    "toggle_night_light",
    "snap_window",
    "minimize_all",
    "switch_to_app",
    "move_to_other_monitor",
    "sleep_system",
    "toggle_wifi",
    "toggle_bluetooth",
    "toggle_do_not_disturb",
    "shutdown_system",
    "restart_system",
    # Phase 6 Productivity, Scheduler & Routines
    "set_timer",
    "cancel_timer",
    "timer_status",
    "set_alarm",
    "set_reminder",
    "calculate",
    "convert_units",
    "quick_note",
    "read_notes",
    "type_text",
    "dictation_mode",
    "read_clipboard",
    "summarize_clipboard",
    "start_routine",
    # Phase 7 Screen Awareness & Quick Answers
    "screen_awareness",
    "analyze_screen",
    "world_time",
    "quick_answer",
    # Phase 8 Window Under Cursor & Gesture Actions
    "open_this",
    "copy_this",
    "close_this",
    # Dynamic Island Actions (Phase 4)
    "island_next_page",
    "island_prev_page",
    "island_show_page",
    "island_toggle_expand",
    "island_toggle_tracking",
    "island_set_pin",
}

# Action argument validation schemas
ACTION_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "none": {},
    "volume_up": {"steps": int},
    "volume_down": {"steps": int},
    "set_volume": {"level": int},
    "get_volume": {},
    "mute": {"toggle": bool},
    "brightness_up": {"steps": int},
    "brightness_down": {"steps": int},
    "set_brightness": {"level": int},
    "get_brightness": {},
    "toggle_night_light": {},
    "snap_window": {"direction": str},
    "minimize_all": {},
    "switch_to_app": {"app": str},
    "move_to_other_monitor": {},
    "close_window": {"confirm": bool},
    "lock_workstation": {},
    "sleep_system": {},
    "screenshot": {"path": str},
    "toggle_wifi": {},
    "toggle_bluetooth": {},
    "toggle_do_not_disturb": {},
    "shutdown_system": {"confirmed": bool},
    "restart_system": {"confirmed": bool},
    "open_app": {"app": str},
    "open_site": {"site": str},
    "search": {"query": str},
    "play_music": {"query": str, "platform": str},
    "open_folder": {"folder": str},
    "open_file": {"file": str},
    "open_recent_file": {"folder": str, "extension": str},
    "show_last_download": {},
    "remember_folder": {"alias": str, "path": str, "confirmed": bool},
    # Phase 6 schemas
    "set_timer": {"duration": (int, float), "label": str},
    "cancel_timer": {"label": str},
    "timer_status": {},
    "set_alarm": {"target_time": (int, float), "label": str},
    "set_reminder": {"target_time": (int, float), "text": str},
    "calculate": {"expression": str},
    "convert_units": {"value": (int, float), "from_unit": str, "to_unit": str},
    "quick_note": {"text": str},
    "read_notes": {"count": int},
    "type_text": {"text": str},
    "dictation_mode": {"enabled": bool},
    "read_clipboard": {},
    "summarize_clipboard": {},
    "start_routine": {"routine": str},
    # Phase 7 schemas
    "screen_awareness": {"query": str},
    "analyze_screen": {"query": str},
    "world_time": {"location": str},
    "quick_answer": {"query": str},
    # Phase 8 schemas
    "open_this": {},
    "copy_this": {},
    "close_this": {"confirm": bool},
    # Dynamic Island Actions (Phase 4)
    "island_next_page": {},
    "island_prev_page": {},
    "island_show_page": {"page": str},
    "island_toggle_expand": {},
    "island_toggle_tracking": {},
    "island_set_pin": {"pinned": bool},
}


def validate_action_arguments(action: str, args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validates arguments against the action schema.
    Returns (valid: bool, message: str, sanitized_args: Dict[str, Any]).
    """
    if action not in WHITELIST_ACTIONS:
        return False, f"Action '{action}' is not in the security whitelist", {}

    schema = ACTION_SCHEMAS.get(action)
    if schema is None:
        return True, "Valid", dict(args)

    sanitized = dict(args)
    if action == "set_volume":
        try:
            lvl = int(args.get("level", 50))
            sanitized["level"] = max(0, min(100, lvl))
        except (ValueError, TypeError):
            return False, "Argument 'level' must be an integer between 0 and 100", {}
    elif action == "set_brightness":
        try:
            lvl = int(args.get("level", 50))
            sanitized["level"] = max(0, min(100, lvl))
        except (ValueError, TypeError):
            return False, "Argument 'level' must be an integer between 0 and 100", {}
    elif action in ("volume_up", "volume_down", "brightness_up", "brightness_down"):
        if "steps" in args:
            try:
                sanitized["steps"] = max(1, min(50, int(args["steps"])))
            except (ValueError, TypeError):
                sanitized["steps"] = 5
    elif action == "snap_window":
        direction = str(args.get("direction", "")).strip().lower()
        if direction not in ("left", "right", "up", "down", "maximize", "restore", "west", "east", "north", "south"):
            return False, f"Invalid snap direction '{direction}'. Must be left, right, up, or down", {}
        sanitized["direction"] = direction
    elif action == "switch_to_app":
        app = str(args.get("app", "")).strip()
        if not app:
            return False, "Argument 'app' must not be empty", {}
        sanitized["app"] = app
    elif action == "set_timer":
        try:
            dur = float(args.get("duration", 60.0))
            if dur <= 0:
                return False, "Timer duration must be greater than zero", {}
            sanitized["duration"] = dur
        except (ValueError, TypeError):
            return False, "Argument 'duration' must be a valid number", {}
        sanitized["label"] = str(args.get("label", "Timer")).strip() or "Timer"
    elif action == "calculate":
        expr = str(args.get("expression", "")).strip()
        if not expr:
            return False, "Expression cannot be empty", {}
        sanitized["expression"] = expr
    elif action == "convert_units":
        try:
            val = float(args.get("value", 0.0))
            sanitized["value"] = val
        except (ValueError, TypeError):
            return False, "Argument 'value' must be a valid number", {}
        from_u = str(args.get("from_unit", "")).strip()
        to_u = str(args.get("to_unit", "")).strip()
        if not from_u or not to_u:
            return False, "Both 'from_unit' and 'to_unit' must be provided", {}
        sanitized["from_unit"] = from_u
        sanitized["to_unit"] = to_u
    elif action == "quick_note":
        txt = str(args.get("text", "")).strip()
        if not txt:
            return False, "Note text cannot be empty", {}
        sanitized["text"] = txt
    elif action == "type_text":
        txt = str(args.get("text", "")).strip()
        if not txt:
            return False, "Text to type cannot be empty", {}
        sanitized["text"] = txt
    elif action == "start_routine":
        rout = str(args.get("routine", "")).strip()
        if not rout:
            return False, "Routine name cannot be empty", {}
        sanitized["routine"] = rout
    elif action in ("screen_awareness", "analyze_screen"):
        sanitized["query"] = str(args.get("query", "what's on my screen")).strip()
    elif action == "world_time":
        loc = str(args.get("location", "")).strip()
        if not loc:
            return False, "Argument 'location' cannot be empty", {}
        sanitized["location"] = loc
    elif action == "quick_answer":
        q = str(args.get("query", "")).strip()
        if not q:
            return False, "Argument 'query' cannot be empty", {}
        sanitized["query"] = q
    elif action == "island_show_page":
        p = str(args.get("page", "")).strip().lower()
        if not p:
            return False, "Argument 'page' must not be empty", {}
        sanitized["page"] = p
    elif action == "island_set_pin":
        pinned = args.get("pinned", True)
        if isinstance(pinned, str):
            pinned = pinned.lower() not in ("false", "0", "no", "off")
        else:
            pinned = bool(pinned)
        sanitized["pinned"] = pinned

    return True, "Valid", sanitized


# =====================================================================
# Typed Exceptions
# =====================================================================

class AIError(Exception):
    """Base class for all AI subsystem errors."""
    pass


class AuthenticationError(AIError):
    """Raised on HTTP 401 Unauthorized (invalid API key)."""
    pass


class RateLimitError(AIError):
    """Raised on HTTP 429 Too Many Requests."""
    def __init__(self, message: str = "Rate limit exceeded", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class TimeoutError(AIError):
    """Raised when an HTTP request times out."""
    pass


class OfflineError(AIError):
    """Raised when network is disconnected or DNS lookup fails."""
    pass


class InvalidResponseError(AIError):
    """Raised when server returns invalid JSON or unexpected schema."""
    pass


# =====================================================================
# Secure Key Storage (Keyring + Windows Credential Manager)
# =====================================================================

def get_api_key(cfg=None) -> Optional[str]:
    """
    Retrieves the Groq API key:
    1. From Windows Credential Manager via keyring
    2. From persistent config (cfg.groq_api_key or jarvis_config.json)
    3. From GROQ_API_KEY environment variable.
    Returns None if no key is configured.
    """
    try:
        import keyring
        key = keyring.get_password(SERVICE_NAME, KEY_USERNAME)
        if key and key.strip():
            return key.strip()
    except Exception as exc:
        print(f"[ai] keyring read note: {exc}")

    if cfg and getattr(cfg, "groq_api_key", None):
        return cfg.groq_api_key.strip()

    try:
        from config import load_config
        c = load_config()
        if getattr(c, "groq_api_key", None):
            return c.groq_api_key.strip()
    except Exception:
        pass

    env_key = os.environ.get("GROQ_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    return None


def set_api_key(api_key: str, persist_to_config: bool = True) -> None:
    """
    Saves the API key to Windows Credential Manager via keyring,
    and optionally persists to jarvis_config.json.
    """
    if not api_key or not api_key.strip():
        delete_api_key()
        return

    clean_key = api_key.strip()
    try:
        import keyring
        keyring.set_password(SERVICE_NAME, KEY_USERNAME, clean_key)
    except Exception as exc:
        print(f"[ai] keyring write note: {exc}")

    if persist_to_config:
        try:
            from config import get_config_path
            cfg_path = get_config_path()
            data = {}
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["groq_api_key"] = clean_key
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as exc:
            print(f"[ai] save key to config note: {exc}")


def delete_api_key() -> None:
    """Removes the API key from Windows Credential Manager and config."""
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, KEY_USERNAME)
    except Exception:
        pass

    try:
        from config import get_config_path
        cfg_path = get_config_path()
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "groq_api_key" in data:
                del data["groq_api_key"]
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
    except Exception:
        pass


# =====================================================================
# Rate Limiter for Free-Tier Quota Protection
# =====================================================================

class SimpleRateLimiter:
    """
    Sliding-window rate limiter ensuring calls stay within free-tier bounds.
    Also honors server 429 Retry-After headers.
    """

    def __init__(self, max_requests_per_minute: int = 25):
        self.max_rpm = max_requests_per_minute
        self.timestamps: List[float] = []
        self.retry_after_until: float = 0.0
        self._lock = threading.Lock()

    def record_retry_after(self, seconds: float) -> None:
        with self._lock:
            self.retry_after_until = max(self.retry_after_until, time.time() + float(seconds))

    def acquire(self) -> None:
        """
        Checks rate limit before making a request.
        Raises RateLimitError if blocked by Retry-After or local RPM window.
        """
        with self._lock:
            now = time.time()
            if now < self.retry_after_until:
                wait_sec = self.retry_after_until - now
                raise RateLimitError(
                    f"Rate limit cooldown active. Retry after {wait_sec:.1f}s",
                    retry_after=wait_sec
                )

            # Purge timestamps older than 60 seconds
            cutoff = now - 60.0
            self.timestamps = [t for t in self.timestamps if t > cutoff]

            if len(self.timestamps) >= self.max_rpm:
                oldest = self.timestamps[0]
                wait_sec = max(0.0, 60.0 - (now - oldest))
                raise RateLimitError(
                    f"Local free-tier rate limit ({self.max_rpm} req/min) reached. Try again in {wait_sec:.1f}s",
                    retry_after=wait_sec
                )

            self.timestamps.append(now)


# =====================================================================
# GroqClient
# =====================================================================

class GroqClient:
    """
    Lightweight, high-performance Groq API client with HTTP Keep-Alive,
    connection pooling, TTL response caching, connection pre-warming,
    and jittered exponential backoff retries.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 10,
        base_url: str = GROQ_BASE_URL,
        session: Optional[Any] = None,
        max_retries: int = 2,
        cache_ttl: float = 60.0,
    ):
        if api_key is not None:
            self.api_key = api_key.strip()
        else:
            self.api_key = get_api_key()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.session = session or get_persistent_session()
        self.max_retries = max_retries
        self.cache_ttl = cache_ttl
        self._response_cache: Dict[Tuple, Tuple[str, float]] = {}
        self._cache_lock = threading.Lock()
        self.rate_limiter = SimpleRateLimiter(max_requests_per_minute=25)

    def _post(self, url: str, **kwargs) -> Any:
        _get_orig_requests()
        curr_post = getattr(requests, "post", None)
        if _is_mocked(curr_post) or curr_post is not _ORIG_POST:
            return curr_post(url, **kwargs)
        return self.session.post(url, **kwargs)

    def _get(self, url: str, **kwargs) -> Any:
        _get_orig_requests()
        curr_get = getattr(requests, "get", None)
        if _is_mocked(curr_get) or curr_get is not _ORIG_GET:
            return curr_get(url, **kwargs)
        return self.session.get(url, **kwargs)

    def _request_with_retry(self, method: str, url: str, **kwargs) -> Any:
        attempts = max(1, self.max_retries + 1)
        initial_backoff = 0.05
        max_backoff = 2.0
        for attempt in range(attempts):
            try:
                if method.upper() == "GET":
                    resp = self._get(url, **kwargs)
                else:
                    resp = self._post(url, **kwargs)

                # Retry on 5xx temporary server errors
                if hasattr(resp, "status_code") and resp.status_code in (500, 502, 503, 504) and attempt < attempts - 1:
                    backoff = min(max_backoff, initial_backoff * (2 ** attempt)) + random.uniform(0.0, 0.02)
                    time.sleep(backoff)
                    continue

                return resp
            except requests.exceptions.Timeout as exc:
                raise TimeoutError(f"Request timed out after {kwargs.get('timeout', self.timeout)}s") from exc
            except requests.exceptions.ConnectionError as exc:
                raise OfflineError("Network offline: unable to reach Groq service") from exc
            except requests.exceptions.RequestException as exc:
                raise AIError(f"Network error: {exc}") from exc

    def prewarm_connection(self, async_prewarm: bool = True) -> bool:
        """
        Pre-connects TCP/TLS socket to Groq API endpoint to eliminate handshake latency on wake.
        If async_prewarm is True, performs the handshake in a background daemon thread.
        """
        def _worker():
            try:
                url = f"{self.base_url}/models"
                headers = self._get_headers() if self.api_key else {"User-Agent": "JARVIS-Assistant/1.1"}
                self._get(url, headers=headers, timeout=2.0)
                return True
            except Exception:
                return False

        if async_prewarm:
            t = threading.Thread(target=_worker, name="GroqPrewarmThread", daemon=True)
            t.start()
            return True
        else:
            return _worker()

    def clear_cache(self) -> None:
        """Clears the short query response TTL cache."""
        with self._cache_lock:
            self._response_cache.clear()

    def _get_headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise AuthenticationError("No Groq API key configured.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "JARVIS-Assistant/1.1",
        }

    def _handle_response_error(self, resp: requests.Response) -> None:
        if resp.status_code == 401:
            raise AuthenticationError("Invalid Groq API key (401 Unauthorized)")
        elif resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait_sec = 5.0
            if retry_after:
                try:
                    wait_sec = float(retry_after)
                except ValueError:
                    pass
            self.rate_limiter.record_retry_after(wait_sec)
            raise RateLimitError("Groq API rate limit reached (429)", retry_after=wait_sec)
        else:
            try:
                err_data = resp.json()
                msg = err_data.get("error", {}).get("message", resp.text)
            except Exception:
                msg = resp.text
            raise AIError(f"Groq API error ({resp.status_code}): {msg}")

    def transcribe(self, wav_bytes: bytes, model: str = DEFAULT_STT_MODEL,
                   language: str = "en", prompt: Optional[str] = None) -> str:
        """
        Transcribes 16 kHz in-memory WAV audio bytes using Groq Whisper.
        Endpoint: POST /audio/transcriptions
        Uses acoustic vocabulary biasing prompt to boost recognition of Jarvis wake words and PC commands.
        """
        self.rate_limiter.acquire()
        headers = self._get_headers()
        url = f"{self.base_url}/audio/transcriptions"
        files = {
            "file": ("audio.wav", wav_bytes, "audio/wav")
        }
        if prompt is None:
            prompt = "Jarvis, open Chrome, YouTube, Spotify, volume up, volume down, mute, screenshot, scroll down, zoom in, sleep, wake up."

        data = {
            "model": model,
            "language": language,
            "response_format": "json",
            "prompt": prompt,
        }

        # Phase 8 Privacy Log: audit external cloud audio transmission
        try:
            from privacy_log import record_cloud_transmission
            record_cloud_transmission(
                destination="Groq STT",
                payload_summary=f"Audio transcription ({len(wav_bytes)} bytes, model={model})",
                metadata={"model": model, "language": language, "bytes": len(wav_bytes)},
            )
        except Exception:
            pass

        resp = self._request_with_retry("POST", url, headers=headers, files=files, data=data, timeout=self.timeout)

        if resp.status_code != 200:
            self._handle_response_error(resp)

        try:
            res_json = resp.json()
            return res_json.get("text", "").strip()
        except Exception as exc:
            raise InvalidResponseError("Failed to parse transcription response") from exc

    def chat_stream(self, messages: List[Dict[str, str]], model: str = DEFAULT_LLM_MODEL,
                    temperature: float = 0.5, json_mode: bool = False,
                    on_chunk: Optional[Callable[[str], None]] = None) -> Iterator[str]:
        """
        Streams chat completion tokens as Server-Sent Events (SSE).
        Yields individual text content chunks in real time as they arrive.
        """
        self.rate_limiter.acquire()
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        url = f"{self.base_url}/chat/completions"

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        # Phase 8 Privacy Log: audit external cloud LLM stream transmission
        try:
            from privacy_log import record_cloud_transmission
            user_msgs = [m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"]
            last_text = str(user_msgs[-1]) if user_msgs else ""
            summary_snippet = (last_text[:50] + "...") if len(last_text) > 50 else last_text
            record_cloud_transmission(
                destination="Groq LLM Stream",
                payload_summary=f"Chat stream (model={model}, prompt='{summary_snippet}')",
                metadata={"model": model, "stream": True, "json_mode": json_mode},
            )
        except Exception:
            pass

        resp = self._request_with_retry("POST", url, headers=headers, json=payload, stream=True, timeout=self.timeout)

        if resp.status_code != 200:
            self._handle_response_error(resp)

        lines_iter = resp.iter_lines() if hasattr(resp, "iter_lines") else (resp.text.splitlines() if hasattr(resp, "text") else [])
        for line in lines_iter:
            if not line:
                continue
            line_str = line.decode("utf-8") if isinstance(line, (bytes, bytearray)) else line
            line_str = line_str.strip()
            if not line_str.startswith("data:"):
                continue
            data_str = line_str[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        if on_chunk:
                            try:
                                on_chunk(content)
                            except Exception:
                                pass
                        yield content
            except Exception:
                continue

    def chat(self, messages: List[Dict[str, str]], model: str = DEFAULT_LLM_MODEL,
             temperature: float = 0.5, json_mode: bool = False, stream: bool = False) -> Union[str, Iterator[str]]:
        """
        Generates chat completion. If stream=True, returns an Iterator[str] yielding tokens.
        Identical short queries are served instantly from the TTL response cache.
        Endpoint: POST /chat/completions
        """
        if stream:
            return self.chat_stream(messages, model=model, temperature=temperature, json_mode=json_mode)

        # Check TTL response cache for identical short queries
        cache_key = None
        if not json_mode:
            user_texts = [m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"]
            total_chars = sum(len(txt) for txt in user_texts)
            if total_chars <= 160:
                cache_key = (model, tuple((m.get("role"), str(m.get("content"))) for m in messages if isinstance(m, dict)))
                with self._cache_lock:
                    if cache_key in self._response_cache:
                        cached_val, ts = self._response_cache[cache_key]
                        if time.time() - ts < self.cache_ttl:
                            return cached_val
                        else:
                            del self._response_cache[cache_key]

        self.rate_limiter.acquire()
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        url = f"{self.base_url}/chat/completions"

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        # Phase 8 Privacy Log: audit external cloud LLM chat transmission
        try:
            from privacy_log import record_cloud_transmission
            user_msgs = [m.get("content", "") for m in messages if isinstance(m, dict) and m.get("role") == "user"]
            last_text = str(user_msgs[-1]) if user_msgs else ""
            summary_snippet = (last_text[:50] + "...") if len(last_text) > 50 else last_text
            record_cloud_transmission(
                destination="Groq LLM",
                payload_summary=f"Chat completion (model={model}, messages={len(messages)}, prompt='{summary_snippet}')",
                metadata={"model": model, "messages_count": len(messages), "json_mode": json_mode},
            )
        except Exception:
            pass

        resp = self._request_with_retry("POST", url, headers=headers, json=payload, timeout=self.timeout)

        if resp.status_code != 200:
            self._handle_response_error(resp)

        try:
            res_json = resp.json()
            content = res_json["choices"][0]["message"]["content"].strip()
            if cache_key:
                with self._cache_lock:
                    if len(self._response_cache) >= 512:
                        oldest = min(self._response_cache.keys(), key=lambda k: self._response_cache[k][1])
                        del self._response_cache[oldest]
                    self._response_cache[cache_key] = (content, time.time())
            return content
        except Exception as exc:
            raise InvalidResponseError("Failed to parse chat completion response") from exc

    def describe_image(self, image_input: Any, prompt: Optional[str] = None,
                       model: str = DEFAULT_VISION_MODEL,
                       system_prompt: Optional[str] = None,
                       max_tokens: int = 150) -> str:
        """
        Submits an image to Groq's multimodal vision endpoint for real-time visual scene analysis.
        Accepts:
        - numpy BGR array (OpenCV webcam frame)
        - raw JPEG bytes
        - base64 data URL string
        """
        self.rate_limiter.acquire()
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        url = f"{self.base_url}/chat/completions"

        # Prepare base64 data URL
        if isinstance(image_input, str) and image_input.startswith("data:image"):
            image_url = image_input
        elif isinstance(image_input, (bytes, bytearray)):
            import base64
            b64 = base64.b64encode(image_input).decode("utf-8")
            image_url = f"data:image/jpeg;base64,{b64}"
        elif hasattr(image_input, "shape"):  # numpy array (OpenCV BGR frame)
            import cv2
            import base64
            # Resize frame to max dimension 720 for low-latency transmission while maintaining crisp clarity
            h, w = image_input.shape[:2]
            max_dim = 720
            if max(h, w) > max_dim:
                scale = max_dim / float(max(h, w))
                new_w, new_h = int(w * scale), int(h * scale)
                frame_to_send = cv2.resize(image_input, (new_w, new_h), interpolation=cv2.INTER_AREA)
            else:
                frame_to_send = image_input

            ok, buf = cv2.imencode(".jpg", frame_to_send, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                raise AIError("Failed to encode camera frame into JPEG")
            b64 = base64.b64encode(buf).decode("utf-8")
            image_url = f"data:image/jpeg;base64,{b64}"
        else:
            raise AIError("Unsupported image input format")

        if system_prompt:
            system_instruction = system_prompt
        else:
            system_instruction = (
                "You are J.A.R.V.I.S., the witty, brilliant AI assistant from Iron Man. "
                "The user is showing an object or their surroundings to your camera. "
                "Accurately identify what you see, answer their question, and address the user as 'sir'. "
                "Be concise, natural, and limit your spoken reply to 1-2 sentences maximum."
            )

        user_content = []
        user_prompt = prompt.strip() if prompt and prompt.strip() else "Identify what is in front of the camera, sir."
        user_content.append({"type": "text", "text": f"{system_instruction}\n\nUser asks: {user_prompt}"})
        user_content.append({"type": "image_url", "image_url": {"url": image_url}})

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": user_content
                }
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2
        }

        # Phase 8 Privacy Log: audit external cloud multimodal vision transmission
        try:
            from privacy_log import record_cloud_transmission
            prompt_str = str(user_prompt)
            prompt_snip = (prompt_str[:50] + "...") if len(prompt_str) > 50 else prompt_str
            record_cloud_transmission(
                destination="Groq Vision",
                payload_summary=f"Vision analysis (model={model}, prompt='{prompt_snip}')",
                metadata={"model": model, "prompt": prompt_str},
            )
        except Exception:
            pass

        resp = self._request_with_retry("POST", url, headers=headers, json=payload, timeout=self.timeout)

        if resp.status_code != 200:
            self._handle_response_error(resp)

        try:
            res_json = resp.json()
            return res_json["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            raise InvalidResponseError("Failed to parse vision response") from exc

    def list_models(self) -> List[str]:
        """
        Queries available models from Groq.
        Endpoint: GET /models
        """
        self.rate_limiter.acquire()
        headers = self._get_headers()
        url = f"{self.base_url}/models"

        resp = self._request_with_retry("GET", url, headers=headers, timeout=self.timeout)

        if resp.status_code != 200:
            self._handle_response_error(resp)

        try:
            data = resp.json()
            model_items = data.get("data", [])
            return [m["id"] for m in model_items if "id" in m]
        except Exception as exc:
            raise InvalidResponseError("Failed to parse models response") from exc

    def get_categorized_models(self) -> Dict[str, List[str]]:
        """Returns models partitioned into 'llm', 'vision', and 'stt'."""
        try:
            models = self.list_models()
        except Exception:
            models = []

        stt_models = [m for m in models if "whisper" in m.lower()]
        vision_models = [m for m in models if any(k in m.lower() for k in ("vision", "qwen"))]
        llm_models = [m for m in models if "whisper" not in m.lower()]

        # Ensure defaults are present even if offline or list is empty
        if DEFAULT_STT_MODEL not in stt_models:
            stt_models.insert(0, DEFAULT_STT_MODEL)
        if "whisper-large-v3" not in stt_models:
            stt_models.append("whisper-large-v3")

        if DEFAULT_VISION_MODEL not in vision_models:
            vision_models.insert(0, DEFAULT_VISION_MODEL)

        if DEFAULT_LLM_MODEL not in llm_models:
            llm_models.insert(0, DEFAULT_LLM_MODEL)
        for fallback in ("openai/gpt-oss-20b", "qwen/qwen3.8-27b", "allam-2-7b"):
            if fallback not in llm_models and fallback in models:
                llm_models.append(fallback)

        return {"llm": sorted(llm_models), "vision": sorted(vision_models), "stt": sorted(stt_models)}

    def validate_key(self) -> Tuple[bool, str]:
        """
        Validates the configured API key against Groq.
        Returns: (ok: bool, message: str)
        """
        if not self.api_key:
            return False, "No API key configured"
        try:
            self.list_models()
            return True, "Valid API key"
        except AuthenticationError:
            return False, "Invalid API key (401)"
        except RateLimitError as e:
            return False, f"Rate limit exceeded: {e}"
        except OfflineError:
            return False, "Offline / connection failed"
        except TimeoutError:
            return False, "Connection timed out"
        except Exception as exc:
            return False, f"Validation failed: {exc}"


# =====================================================================
# Conversation Manager & History
# =====================================================================

class ConversationManager:
    """
    Manages in-memory multi-turn conversation history.
    History is kept in RAM only and cleared on standby or reset.
    """

    def __init__(self, max_turns: int = 6):
        self.max_turns = max_turns
        self.history: List[Dict[str, str]] = []
        self._lock = threading.Lock()

    def add_turn(self, user_text: str, assistant_reply: str) -> None:
        with self._lock:
            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": assistant_reply})
            max_msgs = max(2, self.max_turns * 2)
            if len(self.history) > max_msgs:
                self.history = self.history[-max_msgs:]

    def get_messages(self, system_prompt: str, new_user_text: str) -> List[Dict[str, str]]:
        with self._lock:
            msgs = [{"role": "system", "content": system_prompt}]
            msgs.extend(self.history)
            msgs.append({"role": "user", "content": new_user_text})
            return msgs

    def reset(self) -> None:
        with self._lock:
            self.history.clear()


_CONVERSATION_MGR = ConversationManager(max_turns=6)


def get_conversation_manager() -> ConversationManager:
    return _CONVERSATION_MGR


def reset_ai_history() -> None:
    _CONVERSATION_MGR.reset()


def prewarm_connection(api_key: Optional[str] = None, async_prewarm: bool = True) -> bool:
    """Pre-connects TCP/TLS socket to Groq on wake events off the main UI/audio threads."""
    client = GroqClient(api_key=api_key)
    return client.prewarm_connection(async_prewarm=async_prewarm)


def clear_ai_cache() -> None:
    """Clears persistent session and connection cache."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            try:
                _SESSION.close()
            except Exception:
                pass
            _SESSION = None


# =====================================================================
# System Prompt & Action Dispatch
# =====================================================================

def build_system_prompt(ctx: Any = None) -> str:
    """Builds the dynamic system prompt with current environment context."""
    now_str = datetime.datetime.now().strftime("%I:%M %p, %A, %B %d, %Y")
    state_desc = "unknown"
    tracking_mode = "relative"
    dwell_mode = "click"
    precision_mode = True

    if ctx and hasattr(ctx, "engine"):
        state_desc = "active" if getattr(ctx.engine, "active", False) else "standby"
        cfg = getattr(ctx.engine, "cfg", None)
        if cfg:
            tracking_mode = getattr(cfg, "tracking_mode", "relative")
            dwell_mode = getattr(cfg, "dwell_mode", "click")
            precision_mode = getattr(cfg, "precision_mode", True)

    power_info = "AC connected"
    mem_info = "nominal"
    if sys.platform == "win32":
        try:
            import ctypes
            class SYSTEM_POWER_STATUS(ctypes.Structure):
                _fields_ = [
                    ('ACLineStatus', ctypes.c_ubyte),
                    ('BatteryFlag', ctypes.c_ubyte),
                    ('BatteryLifePercent', ctypes.c_ubyte),
                    ('SystemStatusFlag', ctypes.c_ubyte),
                    ('BatteryLifeTime', ctypes.c_ulong),
                    ('BatteryFullLifeTime', ctypes.c_ulong),
                ]
            pwr = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
                if pwr.BatteryLifePercent != 255 and pwr.BatteryLifePercent <= 100:
                    power_info = f"{'AC mains' if pwr.ACLineStatus == 1 else 'Battery'} ({pwr.BatteryLifePercent}%)"
        except Exception:
            pass

        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                mem_info = f"{mem.dwMemoryLoad}% load ({mem.ullAvailPhys / (1024**3):.1f} GB free)"
        except Exception:
            pass

    prompt = (
        "You are J.A.R.V.I.S., Tony Stark's sophisticated, highly intelligent desktop AI assistant from Iron Man.\n"
        f"Current system state: local time is {now_str}, hand tracking is {state_desc}, "
        f"tracking mode is {tracking_mode}, dwell mode is {dwell_mode}, "
        f"precision fine-aim is {'enabled' if precision_mode else 'disabled'}, "
        f"hardware power is {power_info}, physical memory is {mem_info}.\n\n"
        "Instructions:\n"
        "1. Persona: Speak like Paul Bettany's J.A.R.V.I.S.: impeccably polite, dry British wit, dignified, fiercely loyal, and brilliant.\n"
        "2. Address the user respectfully as 'sir'. Do NOT call the user 'Mr. Stark' or 'Tony'.\n"
        "3. Keep your spoken reply to 1-2 spoken sentences maximum so voice synthesis sounds snappy.\n"
        "4. When answering questions (science, coding, engineering, trivia, general life), give sharp, witty, accurate answers with subtle Stark Industries elegance.\n"
        "5. You must respond ONLY with a valid JSON object matching this exact schema:\n"
        "   {\n"
        '     "action": "<action_name or none>",\n'
        '     "args": { ... },\n'
        '     "reply": "<spoken response text>"\n'
        "   }\n"
        "6. Available actions:\n"
        "   - 'none': General conversation or query with no PC command.\n"
        "   - 'diagnostics': (args: {}) Run full system hardware, memory, power, and tracking diagnostics.\n"
        "   - 'house_party': (args: {}) Bring all primary workstations and applications online.\n"
        "   - 'stealth_mode': (args: {}) Mute audio, minimize active window, and dim telemetry.\n"
        "   - 'clean_slate': (args: {}) Clear desktop, reset history, and enter standby.\n"
        "   - 'target_practice': (args: {}) Open repulsor target practice canvas.\n"
        "   - 'volume_up' (args: {'steps': 5})\n"
        "   - 'volume_down' (args: {'steps': 5})\n"
        "   - 'mute' (args: {})\n"
        "   - 'play_pause' (args: {})\n"
        "   - 'next_track' (args: {})\n"
        "   - 'previous_track' (args: {})\n"
        "   - 'open_app' (args: {'app': '<name>'}) [Allowed: chrome, edge, firefox, notepad, calculator, paint, file explorer, terminal, cmd, vs code, settings, task manager, spotify, word, excel, powerpoint]\n"
        "   - 'open_site' (args: {'site': '<name>'}) [Allowed: youtube, google, github, gmail, maps, linkedin]\n"
        "   - 'search' (args: {'query': '<terms>'})\n"
        "   - 'screenshot' (args: {})\n"
        "   - 'lock_workstation' (args: {})\n"
        "   - 'show_desktop' (args: {})\n"
        "   - 'minimize_window' (args: {})\n"
        "   - 'maximize_window' (args: {})\n"
        "   - 'switch_window' (args: {})\n"
        "   - 'close_window' (args: {})\n"
        "   - 'dwell_toggle' (args: {'mode': 'click' | 'radial' | 'off'})\n"
        "   - 'precision_mode' (args: {'enabled': true | false})\n"
        "   - 'tracking_mode' (args: {'mode': 'relative' | 'absolute'})\n"
        "   - 'power_mode' (args: {'mode': 'standby' | 'wake' | 'quit'})\n"
        "   - 'status' (args: {})\n"
        "   - 'play_music' (args: {'query': '<artist or track>', 'platform': 'spotify' | 'youtube'})\n"
        "   - 'set_volume' (args: {'level': 0-100})\n"
        "   - 'get_volume' (args: {})\n"
        "   - 'brightness_up' (args: {'steps': 10})\n"
        "   - 'brightness_down' (args: {'steps': 10})\n"
        "   - 'set_brightness' (args: {'level': 0-100})\n"
        "   - 'get_brightness' (args: {})\n"
        "   - 'toggle_night_light' (args: {})\n"
        "   - 'snap_window' (args: {'direction': 'left' | 'right' | 'up' | 'down'})\n"
        "   - 'minimize_all' (args: {})\n"
        "   - 'switch_to_app' (args: {'app': '<name>'})\n"
        "   - 'move_to_other_monitor' (args: {})\n"
        "   - 'sleep_system' (args: {})\n"
        "   - 'toggle_wifi' (args: {})\n"
        "   - 'toggle_bluetooth' (args: {})\n"
        "   - 'toggle_do_not_disturb' (args: {})\n"
        "   - 'shutdown_system' (args: {'confirmed': true | false})\n"
        "   - 'restart_system' (args: {'confirmed': true | false})\n"
        "7. Never output markdown fences (do not output ```json). Output raw JSON only."
    )
    return prompt


def parse_ai_response(raw_text: str) -> Tuple[str, Dict[str, Any], str]:
    """
    Parses JSON output from LLM.
    Returns (action, args, reply).
    If JSON parsing fails, returns ('none', {}, raw_text).
    """
    t = raw_text.strip()
    # Strip markdown fences if present
    if t.startswith("```"):
        lines = t.split("\n")
        if len(lines) >= 2 and lines[0].startswith("```"):
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            t = "\n".join(lines).strip()

    try:
        data = json.loads(t)
        if isinstance(data, dict):
            action = str(data.get("action", "none")).strip().lower()
            args = data.get("args", {})
            if not isinstance(args, dict):
                args = {}
            reply = str(data.get("reply", "")).strip()
            if not reply:
                reply = raw_text.strip()
            return action, args, reply
    except Exception:
        # Fallback: attempt regex extraction of JSON object
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict):
                    action = str(data.get("action", "none")).strip().lower()
                    args = data.get("args", {})
                    if not isinstance(args, dict):
                        args = {}
                    reply = str(data.get("reply", "")).strip()
                    if reply:
                        return action, args, reply
            except Exception:
                pass

    return "none", {}, raw_text.strip()


def extract_streaming_reply(accumulated: str) -> str:
    """
    Extracts the current partial reply text from accumulated LLM output
    which may be in-progress JSON (e.g. {"reply": "..."}) or plain text.
    """
    if not accumulated:
        return ""

    s = accumulated.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:].strip()

    if not s.startswith("{"):
        if s.endswith("```"):
            s = s[:-3].strip()
        return s

    match = re.search(r'"reply"\s*:\s*"', s)
    if not match:
        return ""

    start_idx = match.end()
    chars = []
    escaped = False
    for i in range(start_idx, len(s)):
        ch = s[i]
        if escaped:
            if ch == 'n':
                chars.append('\n')
            elif ch == 't':
                chars.append('\t')
            elif ch == '"':
                chars.append('"')
            elif ch == '\\':
                chars.append('\\')
            else:
                chars.append(ch)
            escaped = False
        elif ch == '\\':
            escaped = True
        elif ch == '"':
            break
        else:
            chars.append(ch)

    return "".join(chars).strip()


def extract_first_sentence(text: str) -> str:
    """Extracts the first complete sentence from text, or partial text if no sentence terminator is reached."""
    m = re.search(r"^(.*?[.!?])(?:\s+|$)", text)
    if m:
        return m.group(1).strip()
    return text.strip()


def stream_ai_query(
    client: GroqClient,
    messages: List[Dict[str, str]],
    model: str = DEFAULT_LLM_MODEL,
    temperature: float = 0.5,
    ctx: Any = None,
    on_partial_reply: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Streams chat completion from Groq LLM with low-latency progressive chunk dispatch.
    Extracts partial reply / first sentence in real-time and notifies overlay / callbacks
    as tokens arrive rather than stalling until the entire response is received.
    Returns the full accumulated raw response string.
    """
    chat_result = client.chat(messages, model=model, temperature=temperature, json_mode=True, stream=True)
    if isinstance(chat_result, str):
        # Fallback if client was mocked or returned a complete string directly
        reply_snippet = extract_streaming_reply(chat_result) or chat_result
        first_sent = extract_first_sentence(reply_snippet)
        disp = first_sent or reply_snippet
        if disp and ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("say", text=disp, speak=False)
            except Exception:
                pass
        if on_partial_reply and disp:
            try:
                on_partial_reply(disp)
            except Exception:
                pass
        return chat_result

    accumulated = ""
    last_posted_text = ""
    first_sentence_posted = False

    for token in chat_result:
        accumulated += token
        partial_reply = extract_streaming_reply(accumulated)
        if not partial_reply:
            continue

        first_sent = extract_first_sentence(partial_reply)
        has_full_first_sent = bool(re.search(r"[.!?](?:\s+|$)", partial_reply))

        display_text = partial_reply if has_full_first_sent else first_sent

        if display_text and display_text != last_posted_text:
            last_posted_text = display_text
            if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                try:
                    ctx.overlay.post("say", text=display_text, speak=False)
                except Exception:
                    pass
            if on_partial_reply:
                try:
                    on_partial_reply(display_text)
                except Exception:
                    pass

    return accumulated


def execute_whitelisted_action(action: str, args: Dict[str, Any], ctx: Any) -> Tuple[bool, str]:
    """
    Executes a whitelisted action securely.
    Rejects unknown actions or arbitrary code execution.
    Returns (executed: bool, message: str) and notifies overlay on completion.
    """
    import jarvis_log
    jarvis_log.utterance_event(jarvis_log.DISPATCH, action=action)
    executed, msg = _execute_whitelisted_action_impl(action, args, ctx)
    jarvis_log.utterance_event(jarvis_log.RESULT, action=action, ok=executed, msg=jarvis_log.redact(str(msg)[:60]))
    if action and action != "none" and ctx and hasattr(ctx, "overlay") and ctx.overlay:
        try:
            ctx.overlay.post("action_executed", action=action, success=executed)
        except Exception:
            pass
    if executed and action and action != "none":
        try:
            from context_resolver import get_context_resolver
            get_context_resolver().record_turn(
                user_utterance=str(args.get("_utterance", action)),
                resolved_action=action,
                arguments=args,
            )
        except Exception:
            pass
    return executed, msg


def _execute_whitelisted_action_impl(action: str, args: Dict[str, Any], ctx: Any) -> Tuple[bool, str]:
    if action not in WHITELIST_ACTIONS or action == "none":
        return False, f"Action '{action}' is not executable"

    import pyautogui
    from commands import APPS, SITES, _launch

    try:
        if action == "volume_up":
            steps = min(max(int(args.get("steps", 5)), 1), 25)
            try:
                import system_control
                system_control.volume_up(steps=steps)
            except Exception:
                pass
            pyautogui.press("volumeup", presses=steps, _pause=False)
            return True, f"Volume increased by {steps}"

        elif action == "volume_down":
            steps = min(max(int(args.get("steps", 5)), 1), 25)
            try:
                import system_control
                system_control.volume_down(steps=steps)
            except Exception:
                pass
            pyautogui.press("volumedown", presses=steps, _pause=False)
            return True, f"Volume decreased by {steps}"

        elif action == "mute":
            try:
                import system_control
                system_control.mute(toggle=True)
            except Exception:
                pass
            pyautogui.press("volumemute", _pause=False)
            return True, "Mute toggled"

        elif action == "play_pause":
            from commands import _media_call
            _media_call("play_pause")
            return True, "Play/pause toggled"

        elif action == "next_track":
            from commands import _media_call
            _media_call("next_track")
            return True, "Skipped to next track"

        elif action == "previous_track":
            from commands import _media_call
            _media_call("previous_track")
            return True, "Previous track"

        elif action == "weather":
            from weather_client import get_weather_client
            cfg = getattr(ctx, "cfg", None) or getattr(getattr(ctx, "engine", None), "cfg", None)
            city = str(args.get("city", getattr(cfg, "weather_city", "New York") if cfg else "New York"))
            lat = float(args.get("lat", getattr(cfg, "weather_lat", 40.7128) if cfg else 40.7128))
            lon = float(args.get("lon", getattr(cfg, "weather_lon", -74.0060) if cfg else -74.0060))
            units = getattr(cfg, "weather_units", "auto") if cfg else "auto"
            client = get_weather_client()
            w = client.get_weather(lat, lon, city=city, units=units)
            if not w and city:
                w = client.get_weather_for_city(city, units=units)
            if w:
                temp_int = int(round(w.temperature))
                c_name = w.city.split(",")[0]
                card_text = f"{c_name}: {temp_int}{w.unit_symbol}, {w.description}"
                if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                    try:
                        ctx.overlay.post("result", text=card_text, icon="weather", duration=4.0)
                    except Exception:
                        pass
                return True, f"Weather telemetry retrieved for {c_name}"
            return False, "Weather unavailable"

        elif action == "time":
            import datetime
            now_str = datetime.datetime.now().strftime("%I:%M %p").lstrip("0")
            if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                try:
                    ctx.overlay.post("result", text=f"Time: {now_str}", icon="time", duration=3.0)
                except Exception:
                    pass
            return True, f"Time: {now_str}"

        elif action == "now_playing":
            from media_adapter import get_media_adapter
            adapter = get_media_adapter()
            media = adapter.get_current_media() or adapter.refresh_sync()
            if media and media.has_media:
                card_text = f"{media.title} — {media.artist}" if media.artist else media.title
                if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                    try:
                        ctx.overlay.post("result", text=card_text, icon="media", duration=3.5)
                    except Exception:
                        pass
                return True, f"Now playing: {card_text}"
            return False, "No active media"

        elif action == "show_widget":
            widget = str(args.get("widget", "media")).strip().lower()
            try:
                from island_manager import get_island_page_manager
                mgr = get_island_page_manager()
                mgr.set_active_page(widget)
                mgr.expand()
            except Exception:
                pass
            if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                try:
                    ctx.overlay.post("expand_widget", widget=widget)
                except Exception:
                    pass
            return True, f"Expanded {widget} widget"

        elif action == "toggle_widgets":
            if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                try:
                    ctx.overlay.post("toggle_widgets")
                except Exception:
                    pass
            return True, "Toggled island widgets"

        elif action == "open_app":
            app_req = str(args.get("app", "")).strip().lower()
            if app_req in APPS:
                _launch(APPS[app_req])
                return True, f"Launched {app_req}"
            return False, f"App '{app_req}' not in whitelist"

        elif action == "open_site":
            import webbrowser
            site_req = str(args.get("site", "")).strip().lower()
            if site_req in SITES:
                webbrowser.open(SITES[site_req])
                return True, f"Opened {site_req}"
            return False, f"Site '{site_req}' not in whitelist"

        elif action == "search":
            import webbrowser
            query = str(args.get("query", "")).strip()
            if query:
                webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
                return True, f"Searched for {query}"
            return False, "Empty search query"

        elif action == "play_music":
            from commands import play_media
            query = str(args.get("query", "")).strip()
            platform = str(args.get("platform", "spotify")).strip().lower()
            if not platform or platform not in ("spotify", "youtube"):
                platform = "auto"
            ok, msg = play_media(query, platform=platform)
            return ok, msg

        elif action == "screenshot":
            folder = os.path.join(os.path.expanduser("~"), "Pictures", "Jarvis")
            os.makedirs(folder, exist_ok=True)
            shot_path = os.path.join(folder, time.strftime("shot-%Y%m%d-%H%M%S.png"))
            pyautogui.screenshot(shot_path)
            return True, f"Screenshot saved to {shot_path}"

        elif action == "lock_workstation":
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.LockWorkStation()
                return True, "Workstation locked"
            return False, "Lock not supported on this platform"

        elif action == "show_desktop":
            pyautogui.hotkey("win", "d", _pause=False)
            return True, "Desktop shown"

        elif action == "minimize_window":
            pyautogui.hotkey("win", "down", _pause=False)
            return True, "Window minimized"

        elif action == "maximize_window":
            pyautogui.hotkey("win", "up", _pause=False)
            return True, "Window maximized"

        elif action == "switch_window":
            pyautogui.hotkey("alt", "tab", _pause=False)
            return True, "Window switched"

        elif action == "close_window":
            import system_control
            confirm = bool(args.get("confirm", False))
            return system_control.close_window(confirm=confirm)

        elif action == "dwell_toggle":
            mode = str(args.get("mode", "")).strip().lower()
            if mode in ("click", "radial", "off") and ctx and hasattr(ctx, "engine"):
                ctx.engine.cfg.dwell_mode = mode
                return True, f"Dwell mode set to {mode}"
            return False, "Invalid dwell mode"

        elif action == "precision_mode":
            val = bool(args.get("enabled", True))
            if ctx and hasattr(ctx, "engine"):
                ctx.engine.cfg.precision_mode = val
                return True, f"Precision mode set to {val}"
            return False, "Context unavailable"

        elif action == "tracking_mode":
            mode = str(args.get("mode", "")).strip().lower()
            if mode in ("relative", "absolute") and ctx and hasattr(ctx, "engine"):
                ctx.engine.cfg.tracking_mode = mode
                return True, f"Tracking mode set to {mode}"
            return False, "Invalid tracking mode"

        elif action == "power_mode":
            mode = str(args.get("mode", "")).strip().lower()
            from commands import power_action_allowed
            utt = args.get("_utterance") or getattr(ctx, "_current_utterance", None)
            if not power_action_allowed(args, utt):
                import jarvis_log as _jl
                _jl.utterance_event(_jl.DECISION, tier="system2", action="power_mode(blocked)")
                return False, "Power action blocked: utterance is not an explicit power command"
            if ctx:
                if mode == "standby":
                    ctx.set_active(False)
                    return True, "Set to standby"
                elif mode == "wake":
                    ctx.set_active(True)
                    return True, "Set to active"
                elif mode == "quit":
                    ctx.quit()
                    return True, "Quit requested"
            return False, "Invalid power mode"

        elif action == "status":
            return True, "Status reported"

        elif action == "diagnostics":
            from commands import get_system_telemetry
            telemetry = get_system_telemetry(ctx)
            return True, f"Diagnostics: {telemetry}"

        elif action == "house_party":
            _launch("chrome")
            return True, "House Party Protocol initiated"

        elif action == "stealth_mode":
            pyautogui.press("volumemute", _pause=False)
            pyautogui.hotkey("win", "down", _pause=False)
            return True, "Stealth mode engaged"

        elif action == "clean_slate":
            pyautogui.hotkey("win", "d", _pause=False)
            reset_ai_history()
            if ctx:
                ctx.set_active(False)
            return True, "Clean Slate Protocol executed"

        elif action == "target_practice":
            import webbrowser
            base_dir = os.path.dirname(os.path.abspath(__file__))
            target_html = os.path.join(base_dir, "tests", "target_practice.html")
            if os.path.exists(target_html):
                webbrowser.open(f"file:///{target_html}")
            else:
                webbrowser.open("https://www.google.com")
            return True, "Target practice canvas opened"

        elif action == "open_folder":
            import file_ops
            folder = str(args.get("folder", "")).strip()
            res = file_ops.open_folder(folder)
            return res.success, res.message

        elif action == "open_file":
            import file_ops
            filepath = str(args.get("file", "")).strip()
            res = file_ops.open_file(filepath)
            return res.success, res.message

        elif action == "open_recent_file":
            import file_ops
            folder = str(args.get("folder", "downloads")).strip()
            ext = args.get("extension")
            res = file_ops.open_recent_file(folder=folder, extension=ext)
            return res.success, res.message

        elif action == "show_last_download":
            import file_ops
            res = file_ops.show_last_download()
            return res.success, res.message

        elif action == "remember_folder":
            import file_ops
            alias = str(args.get("alias", "")).strip()
            path = str(args.get("path", "")).strip()
            confirmed = bool(args.get("confirmed", True))
            res = file_ops.remember_folder(alias=alias, path=path, confirmed=confirmed)
            return res.success, res.message

        elif action == "set_volume":
            import system_control
            lvl = int(args.get("level", 50))
            return system_control.set_volume(lvl)

        elif action == "get_volume":
            import system_control
            return system_control.get_volume()

        elif action == "brightness_up":
            import system_control
            steps = int(args.get("steps", 10))
            return system_control.brightness_up(steps)

        elif action == "brightness_down":
            import system_control
            steps = int(args.get("steps", 10))
            return system_control.brightness_down(steps)

        elif action == "set_brightness":
            import system_control
            lvl = int(args.get("level", 50))
            return system_control.set_brightness(lvl)

        elif action == "get_brightness":
            import system_control
            return system_control.get_brightness()

        elif action == "toggle_night_light":
            import system_control
            return system_control.toggle_night_light()

        elif action == "snap_window":
            import system_control
            direction = str(args.get("direction", "left"))
            return system_control.snap_window(direction)

        elif action == "minimize_all":
            import system_control
            return system_control.minimize_all()

        elif action == "switch_to_app":
            import system_control
            app_name = str(args.get("app", ""))
            return system_control.switch_to_app(app_name)

        elif action == "move_to_other_monitor":
            import system_control
            return system_control.move_to_other_monitor()

        elif action == "sleep_system":
            import system_control
            return system_control.sleep_system()

        elif action == "toggle_wifi":
            import system_control
            return system_control.toggle_wifi()

        elif action == "toggle_bluetooth":
            import system_control
            return system_control.toggle_bluetooth()

        elif action == "toggle_do_not_disturb":
            import system_control
            return system_control.toggle_do_not_disturb()

        elif action == "shutdown_system":
            import system_control
            confirmed = bool(args.get("confirmed", False))
            return system_control.shutdown_system(confirmed=confirmed)

        elif action == "restart_system":
            import system_control
            confirmed = bool(args.get("confirmed", False))
            return system_control.restart_system(confirmed=confirmed)

        # Phase 6 Productivity, Scheduler & Routines Actions
        elif action == "set_timer":
            import scheduler
            sched = scheduler.get_scheduler(ctx=ctx)
            duration = float(args.get("duration", 60.0))
            label = str(args.get("label", "Timer"))
            item = sched.set_timer(duration, label=label)
            return True, f"Timer set for {scheduler.format_duration(duration)} ({label})"

        elif action == "cancel_timer":
            import scheduler
            sched = scheduler.get_scheduler(ctx=ctx)
            label = args.get("label")
            ok = sched.cancel_timer(label)
            return ok, "Timer cancelled" if ok else "No active timer found"

        elif action == "timer_status":
            import scheduler
            sched = scheduler.get_scheduler(ctx=ctx)
            left = sched.get_time_left(args.get("label"))
            if left is not None and left > 0:
                return True, f"Time remaining: {scheduler.format_duration(left)}"
            return False, "No active timer running"

        elif action == "set_alarm":
            import scheduler
            sched = scheduler.get_scheduler(ctx=ctx)
            target = float(args.get("target_time", time.time() + 3600))
            label = str(args.get("label", "Alarm"))
            item = sched.set_alarm(target, label=label)
            return True, f"Alarm set for {label}"

        elif action == "set_reminder":
            import scheduler
            sched = scheduler.get_scheduler(ctx=ctx)
            target = float(args.get("target_time", time.time() + 3600))
            text_val = str(args.get("text", "Reminder"))
            item = sched.set_reminder(target, text=text_val)
            return True, f"Reminder set: {text_val}"

        elif action == "calculate":
            import math_parser
            expr = str(args.get("expression", ""))
            ok, res, msg = math_parser.evaluate_expression(expr)
            return ok, msg

        elif action == "convert_units":
            import math_parser
            val = float(args.get("value", 0.0))
            f_u = str(args.get("from_unit", ""))
            t_u = str(args.get("to_unit", ""))
            ok, res, msg = math_parser.convert_units(val, f_u, t_u)
            return ok, msg

        elif action == "quick_note":
            import productivity
            txt = str(args.get("text", ""))
            ok, msg = productivity.add_note(txt)
            return ok, msg

        elif action == "read_notes":
            import productivity
            cnt = int(args.get("count", 3))
            ok, msg, _ = productivity.read_notes(max_entries=cnt)
            return ok, msg

        elif action == "type_text":
            import productivity
            txt = str(args.get("text", ""))
            ok = productivity.type_text(txt)
            return ok, f"Typed text: {txt}" if ok else "Failed to type text"

        elif action == "dictation_mode":
            import productivity
            enabled = bool(args.get("enabled", True))
            res = productivity.set_dictation_mode(enabled)
            return True, f"Dictation mode {'enabled' if res else 'disabled'}"

        elif action == "read_clipboard":
            import productivity
            cb = productivity.get_clipboard_text()
            if not cb:
                return False, "Clipboard is empty"
            return True, f"Clipboard content: {cb[:200]}"

        elif action == "summarize_clipboard":
            import productivity
            cb = productivity.get_clipboard_text()
            if not cb:
                return False, "Clipboard is empty"
            summary = productivity.summarize_text(cb)
            return True, f"Clipboard summary: {summary}"

        elif action == "start_routine":
            import routines
            rout = str(args.get("routine", ""))
            ok, summary, _ = routines.execute_routine(rout, ctx=ctx)
            return ok, summary

        # Phase 7 Screen Awareness & Quick Answers
        elif action in ("screen_awareness", "analyze_screen"):
            import screen_awareness
            query_txt = str(args.get("query", "what's on my screen"))
            reply = screen_awareness.analyze_screen(query_txt, ctx)
            return True, reply

        elif action == "world_time":
            import quick_answers
            loc = str(args.get("location", ""))
            reply = quick_answers.get_world_time(loc, ctx)
            if reply:
                return True, reply
            return False, f"Could not determine time for {loc}"

        elif action == "quick_answer":
            import quick_answers
            query_txt = str(args.get("query", ""))
            reply = quick_answers.get_quick_answer(query_txt, ctx)
            if reply:
                return True, reply
            return False, "Unable to find a quick answer"

        # Phase 8 Window Under Cursor Actions
        elif action == "open_this":
            import window_resolver
            ok, msg = window_resolver.open_this()
            return ok, msg

        elif action == "copy_this":
            import window_resolver
            ok, msg = window_resolver.copy_this()
            return ok, msg

        elif action == "close_this":
            import window_resolver
            confirm = bool(args.get("confirm", False))
            ok, msg = window_resolver.close_this(confirm=confirm)
            return ok, msg

        # Phase 4 Dynamic Island Actions
        elif action == "island_next_page":
            from island_manager import get_island_page_manager
            mgr = get_island_page_manager()
            page = mgr.next_page()
            page_val = page.value if hasattr(page, "value") else str(page)
            return True, f"Dynamic Island flipped to {page_val}"

        elif action == "island_prev_page":
            from island_manager import get_island_page_manager
            mgr = get_island_page_manager()
            page = mgr.previous_page()
            page_val = page.value if hasattr(page, "value") else str(page)
            return True, f"Dynamic Island flipped to {page_val}"

        elif action == "island_show_page":
            from island_manager import get_island_page_manager
            mgr = get_island_page_manager()
            page_name = str(args.get("page", "")).strip().lower()
            ok = mgr.set_active_page(page_name)
            if ok:
                return True, f"Dynamic Island switched to {page_name}"
            return False, f"Unknown page '{page_name}'"

        elif action == "island_toggle_expand":
            from island_manager import get_island_page_manager
            mgr = get_island_page_manager()
            expanded = mgr.toggle_expanded()
            state_str = "expanded" if expanded else "collapsed"
            return True, f"Dynamic Island {state_str}"

        elif action == "island_toggle_tracking":
            from island_manager import get_island_page_manager
            mgr = get_island_page_manager()
            tracking = mgr.toggle_tracking()
            state_str = "enabled" if tracking else "disabled"
            return True, f"Hand tracking {state_str}"

        elif action == "island_set_pin":
            from island_manager import get_island_page_manager
            mgr = get_island_page_manager()
            pinned = bool(args.get("pinned", True))
            mgr.set_pin_open(pinned)
            state_str = "pinned" if pinned else "unpinned"
            return True, f"Dynamic Island {state_str}"

    except Exception as exc:
        return False, f"Action execution error: {exc}"

    return False, "Unhandled action"
