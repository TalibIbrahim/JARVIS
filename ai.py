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
import re
import sys
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple, Any

import requests
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
}


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
    Lightweight Groq API client using requests.
    Zero heavy SDK dependencies.
    """

    def __init__(self, api_key: Optional[str] = None, timeout: int = 10, base_url: str = GROQ_BASE_URL):
        if api_key is not None:
            self.api_key = api_key.strip()
        else:
            self.api_key = get_api_key()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = SimpleRateLimiter(max_requests_per_minute=25)

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

        try:
            resp = requests.post(url, headers=headers, files=files, data=data, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise TimeoutError(f"Audio transcription timed out after {self.timeout}s") from exc
        except requests.exceptions.ConnectionError as exc:
            raise OfflineError("Network offline: unable to reach Groq audio service") from exc
        except requests.exceptions.RequestException as exc:
            raise AIError(f"Network error during transcription: {exc}") from exc

        if resp.status_code != 200:
            self._handle_response_error(resp)

        try:
            res_json = resp.json()
            return res_json.get("text", "").strip()
        except Exception as exc:
            raise InvalidResponseError("Failed to parse transcription response") from exc

    def chat(self, messages: List[Dict[str, str]], model: str = DEFAULT_LLM_MODEL,
             temperature: float = 0.5, json_mode: bool = False) -> str:
        """
        Generates chat completion.
        Endpoint: POST /chat/completions
        """
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

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise TimeoutError(f"Chat completion timed out after {self.timeout}s") from exc
        except requests.exceptions.ConnectionError as exc:
            raise OfflineError("Network offline: unable to reach Groq chat service") from exc
        except requests.exceptions.RequestException as exc:
            raise AIError(f"Network error during chat completion: {exc}") from exc

        if resp.status_code != 200:
            self._handle_response_error(resp)

        try:
            res_json = resp.json()
            return res_json["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            raise InvalidResponseError("Failed to parse chat completion response") from exc

    def describe_image(self, image_input: Any, prompt: Optional[str] = None,
                       model: str = DEFAULT_VISION_MODEL) -> str:
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
            "max_tokens": 120,
            "temperature": 0.2
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise TimeoutError(f"Vision analysis timed out after {self.timeout}s") from exc
        except requests.exceptions.ConnectionError as exc:
            raise OfflineError("Network offline: unable to reach Groq vision service") from exc
        except requests.exceptions.RequestException as exc:
            raise AIError(f"Network error during vision analysis: {exc}") from exc

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

        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise TimeoutError(f"List models timed out after {self.timeout}s") from exc
        except requests.exceptions.ConnectionError as exc:
            raise OfflineError("Network offline: unable to reach Groq service") from exc
        except requests.exceptions.RequestException as exc:
            raise AIError(f"Network error fetching models: {exc}") from exc

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


def execute_whitelisted_action(action: str, args: Dict[str, Any], ctx: Any) -> Tuple[bool, str]:
    """
    Executes a whitelisted action securely.
    Rejects unknown actions or arbitrary code execution.
    Returns (executed: bool, message: str).
    """
    if action not in WHITELIST_ACTIONS or action == "none":
        return False, f"Action '{action}' is not executable"

    import pyautogui
    from commands import APPS, SITES, _launch

    try:
        if action == "volume_up":
            steps = min(max(int(args.get("steps", 5)), 1), 25)
            pyautogui.press("volumeup", presses=steps, _pause=False)
            return True, f"Volume increased by {steps}"

        elif action == "volume_down":
            steps = min(max(int(args.get("steps", 5)), 1), 25)
            pyautogui.press("volumedown", presses=steps, _pause=False)
            return True, f"Volume decreased by {steps}"

        elif action == "mute":
            pyautogui.press("volumemute", _pause=False)
            return True, "Mute toggled"

        elif action == "play_pause":
            pyautogui.press("playpause", _pause=False)
            return True, "Play/pause toggled"

        elif action == "next_track":
            pyautogui.press("nexttrack", _pause=False)
            return True, "Skipped to next track"

        elif action == "previous_track":
            pyautogui.press("prevtrack", _pause=False)
            return True, "Previous track"

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
            pyautogui.hotkey("alt", "f4", _pause=False)
            return True, "Window closed"

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

    except Exception as exc:
        return False, f"Action execution error: {exc}"

    return False, "Unhandled action"
