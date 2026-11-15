"""Local voice commands. No cloud agent: every command maps to a direct action on this PC."""
import datetime
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import webbrowser
from typing import Any, List, Optional, Tuple

import jarvis_log as _jl
import personal
from ai import (
    get_api_key,
    GroqClient,
    DEFAULT_LLM_MODEL,
    DEFAULT_VISION_MODEL,
    get_conversation_manager,
    reset_ai_history,
    build_system_prompt,
    parse_ai_response,
    execute_whitelisted_action,
    stream_ai_query,
    AuthenticationError,
    RateLimitError,
    OfflineError,
    TimeoutError,
    answer_cache_get,
    answer_cache_put,
    rate_limit_reply,
)

# Alias for external modules
reset_history = reset_ai_history

APPS = {
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "firefox": "firefox",
    "notepad": "notepad", "calculator": "calc", "paint": "mspaint",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "terminal": "wt", "command prompt": "cmd", "cmd": "cmd",
    "code": "code", "vs code": "code", "visual studio code": "code",
    "settings": "ms-settings:", "task manager": "taskmgr", "spotify": "spotify:",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
}
SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "github": "https://github.com", "gmail": "https://mail.google.com",
    "maps": "https://maps.google.com", "linkedin": "https://www.linkedin.com",
}

# Pending destructive confirmation state (e.g. 'shutdown', 'restart', 'close_window')
_PENDING_CONFIRMATION: Optional[str] = None


_PHRASE_RE_CACHE: dict = {}


def _phrase_regex(phrases: tuple):
    rx = _PHRASE_RE_CACHE.get(phrases)
    if rx is None:
        alts = "|".join(re.escape(p) for p in sorted(set(phrases), key=len, reverse=True) if p)
        rx = re.compile(r"(?<![\w'])(?:" + alts + r")(?![\w'])") if alts else re.compile(r"(?!)")
        _PHRASE_RE_CACHE[phrases] = rx
    return rx


def _any(t, *phrases):
    """Whole-word / whole-phrase match (PROJECT_CONTEXT 11 forbids substring matching)."""
    return bool(_phrase_regex(phrases).search(t))


# --- exact whole-command matching for power / destructive commands -------------------
_LEAD_FILLER = ("hey jarvis", "okay jarvis", "ok jarvis", "jarvis", "please")
_TRAIL_FILLER = ("please", "now", "jarvis")

POWER_QUIT = frozenset({"shut down jarvis", "quit jarvis", "exit jarvis", "power down", "power down jarvis"})
POWER_STANDBY = frozenset({"go to sleep", "sleep mode", "sleep", "take a nap", "standby", "stand by",
                           "go to standby", "rest", "rest mode", "standby mode"})
POWER_WAKE = frozenset({"wake up", "activate", "come online", "i'm back", "im back", "online"})

# Destructive phrases: like POWER_QUIT/STANDBY they run ONLY as the whole utterance, never inside a chain.
CONFIRM_SHUTDOWN = ("yes confirm shutdown", "confirm shutdown")
CONFIRM_RESTART = ("yes confirm restart", "confirm restart")
CONFIRM_CLOSE = ("confirm close", "force close", "close anyway")
SHUTDOWN_PHRASES = ("shut down the pc", "shut down pc", "shut down computer", "turn off pc", "turn off the pc",
                    "power off pc", "shutdown pc", "shutdown computer")
RESTART_PHRASES = ("restart pc", "restart the pc", "restart computer", "reboot pc", "reboot computer",
                   "reboot the computer")
SLEEP_SYSTEM_PHRASES = ("sleep system", "sleep pc", "sleep computer", "put pc to sleep", "put computer to sleep")
CLEAN_SLATE_PHRASES = ("clean slate", "clean slate protocol", "protocol clean slate", "execute clean slate",
                       "execute clean slate protocol")
_PENDING_CONFIRM_WORDS = ("yes", "confirm", "proceed", "do it", "sure", "affirmative", "yes please", "yes confirm",
                          "confirm this", "yes confirm shutdown", "confirm shutdown", "yes confirm restart",
                          "confirm restart", "confirm close", "yes confirm close", "force close")

MAX_UTTERANCE_CHARS = 500   # voice/command entry cap: no legitimate command is longer
_MAX_FILLER_PEELS = 8       # bounded filler peeling keeps _power_forms linear on hostile input
SKIPPED_IN_CHAIN_NOTE = "I'll only do that on its own, sir."


def _norm_text(text) -> str:
    """The ONE normalization for wake/power matching: NFKC then casefold. Confusable letters that
    survive it (dotless i, ...) are non-ASCII and therefore never match an ASCII wake word."""
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def cap_utterance(text):
    """Truncate an over-long utterance to MAX_UTTERANCE_CHARS (logged)."""
    if isinstance(text, str) and len(text) > MAX_UTTERANCE_CHARS:
        _jl.get_logger("commands").warning("utterance truncated from %d to %d chars", len(text), MAX_UTTERANCE_CHARS)
        return text[:MAX_UTTERANCE_CHARS]
    return text


def _power_forms(text) -> list:
    """Normalized candidate forms of an utterance: normalized lowercase, punctuation stripped, then
    leading please/jarvis and trailing please/now/jarvis peeled off step by step (bounded)."""
    s = _norm_text(cap_utterance(str(text or ""))).replace("\u2019", "'")
    s = " ".join(re.sub(r"[^\w\s']", " ", s).split())
    forms = [s]
    for _ in range(_MAX_FILLER_PEELS):
        if not s:
            break
        changed = False
        for f in _LEAD_FILLER:
            if s == f:
                s = ""
                changed = True
            elif s.startswith(f + " "):
                s = s[len(f) + 1:]
                changed = True
            if changed:
                break
        if not changed:
            for f in _TRAIL_FILLER:
                if s.endswith(" " + f):
                    s = s[:-(len(f) + 1)]
                    changed = True
                    break
        if not changed:
            break
        if s:
            forms.append(s)
    return forms


def _exact(text, *phrases) -> bool:
    """True only if the WHOLE (normalized) command equals one of the phrases."""
    wanted = set(phrases)
    return any(f in wanted for f in _power_forms(text))


def is_power_phrase(text, kind: str) -> bool:
    """kind in {'quit','standby','wake'}; used to gate LLM-chosen power_mode actions."""
    table = {"quit": POWER_QUIT, "standby": POWER_STANDBY, "wake": POWER_WAKE}
    return _exact(text, *table.get(kind, ()))


_WAKE_PREFIX_RE = re.compile(r"^(?:(?:hey|ok|okay)[\s,.:;!?-]+)?jarvis(?!\w)[\s,.:;!?-]*(?=\S)", re.ASCII)
# A greeting left in front of the command ("hey, volume up", or "hey" after the wake word was cut off).
_LEAD_GREETING_RE = re.compile(r"^(?:hey|ok|okay)(?!\w)[\s,.:;!?-]*(?=\S)")
# Whole-utterance speech-to-text confusions seen in real logs ("Jarvis, volume up" -> "what are you up").
_STT_COMMAND_FIXES = {"what are you up": "volume up", "what are you down": "volume down"}

_BRI_WORD = frozenset({"brightness", "brighter", "brighten", "bright", "brightest", "dim", "dimmer", "darker",
                       "darken"})
_BRI_VOCAB = _BRI_WORD | frozenset({
    "set", "turn", "put", "make", "change", "adjust", "bring", "the", "my", "screen", "display", "monitor",
    "laptop", "to", "at", "up", "down", "increase", "raise", "boost", "decrease", "lower", "reduce", "full", "max",
    "maximum", "highest", "min", "minimum", "lowest", "half", "all", "way", "a", "bit", "little", "more", "less",
    "by", "percent", "higher", "it", "what", "what's", "s", "is", "current", "level", "how", "hundred",
    "please", "now", "sir", "jarvis", "can", "you", "could", "would", "just", "hey", "ok", "okay"})
_BRI_UP = frozenset({"up", "increase", "raise", "boost", "higher", "more", "brighter", "brighten"})
_BRI_DOWN = frozenset({"down", "decrease", "lower", "reduce", "dim", "dimmer", "darker", "darken", "less"})


def parse_brightness_intent(text: str) -> Optional[Tuple[str, dict]]:
    """Local brightness intent -> ('set_brightness', {'level'}), ('brightness_up'|'brightness_down',
    {'steps'}), ('get_brightness', {}) or None. Fires only when every word is brightness vocabulary,
    so "dim the lights" or a question about something else never matches."""
    s = re.sub(r"(\d)\s*%", r"\1 percent", _norm_text(text).replace("’", "'"))
    words = re.sub(r"[^\w\s']", " ", s).split()
    if not words or len(words) > 12 or not (_BRI_WORD & set(words)):
        return None
    nums = [w for w in words if w.isdigit()]
    if len(nums) > 1 or any(w not in _BRI_VOCAB and not w.isdigit() for w in words):
        return None
    ws = set(words)
    down = bool(_BRI_DOWN & ws)
    up = bool(_BRI_UP & ws) and not down
    if nums or "hundred" in ws:
        n = int(nums[0]) if nums else 100
        if (up or down) and re.search(r"\bby \d", " ".join(words)):
            return ("brightness_down" if down else "brightness_up"), {"steps": max(1, min(100, n))}
        return "set_brightness", {"level": max(0, min(100, n))}
    if ws & {"full", "max", "maximum", "highest", "brightest"} or {"all", "way", "up"} <= ws:
        return "set_brightness", {"level": 100}
    if ws & {"min", "minimum", "lowest"} or {"all", "way", "down"} <= ws:
        return "set_brightness", {"level": 0}
    if "half" in ws:
        return "set_brightness", {"level": 50}
    if down:
        return "brightness_down", {"steps": 10}
    if up:
        return "brightness_up", {"steps": 10}
    if ("brightness" in ws and ws & {"what", "what's", "current", "how", "level"}) or {"how", "bright"} <= ws:
        return "get_brightness", {}
    return None


def strip_wake_prefix(t: str) -> str:
    """Drop a leading '[hey|ok|okay] jarvis' when a command follows it; else return t unchanged.
    Matches on the same NFKC+casefold normalization as is_power_phrase (ASCII-only wake word), so the
    dispatcher and the power gate always agree."""
    n = _norm_text(cap_utterance(t))
    if not _WAKE_PREFIX_RE.match(n):
        return t.strip() or t
    return _WAKE_PREFIX_RE.sub("", n, count=1).strip() or t


def is_chain_gated(text) -> bool:
    """True if `text` is a power/standby/quit or destructive command (or a confirmation of one).
    Such commands execute only when they are the whole utterance, never as part of a chain."""
    gated = (POWER_QUIT | POWER_STANDBY | frozenset(
        CONFIRM_SHUTDOWN + CONFIRM_RESTART + CONFIRM_CLOSE + SHUTDOWN_PHRASES + RESTART_PHRASES
        + SLEEP_SYSTEM_PHRASES + CLEAN_SLATE_PHRASES + ("shutdown", "restart", "reboot")))
    if _PENDING_CONFIRMATION:
        gated = gated | frozenset(_PENDING_CONFIRM_WORDS)
    for base in (text, strip_wake_prefix(text)):
        for f in _power_forms(base):
            if f in gated:
                return True
            f2 = re.sub(r"^(?:can you\s+(?:please\s+)?|could you\s+(?:please\s+)?|would you\s+(?:please\s+)?|hey\s+|ok\s+|okay\s+|just\s+)", "", f)
            if f2 in gated:
                return True
    n = " ".join(re.sub(r"[^\w\s']", " ", _norm_text(cap_utterance(text))).split())
    return _any(n, "clean slate", "confirm shutdown", "confirm restart", "confirm close")


# Destructive actions: action -> allowed-utterance phrases. The LLM (System 2) and the System 1 engine
# may NEVER confirm; these run only when the user's own words are one of the exact phrases.
# None = never runnable through execute_whitelisted_action (the local Tier-1 "confirm ..." phrases do it).
DESTRUCTIVE_ACTIONS = {
    "shutdown_system": None,
    "restart_system": None,
    "sleep_system": frozenset(SLEEP_SYSTEM_PHRASES),
    "clean_slate": frozenset(CLEAN_SLATE_PHRASES),
    "close_window": frozenset(CONFIRM_CLOSE),   # only when confirm=True; confirm=False merely asks
}


def destructive_action_gate(action, args, utterance):
    """Returns (allowed, args). shutdown/restart: allowed but `confirmed` forced False (the existing flow
    asks the user to say 'confirm shutdown'/'confirm restart'). Others: allowed only on an exact phrase."""
    if action not in DESTRUCTIVE_ACTIONS:
        return True, args
    args = dict(args or {})
    if action in ("shutdown_system", "restart_system"):
        args["confirmed"] = False
        return True, args
    if action == "close_window" and not args.get("confirm"):
        return True, args
    phrases = DESTRUCTIVE_ACTIONS[action]
    ok = isinstance(utterance, str) and _exact(utterance, *phrases)
    return ok, args


def power_action_allowed(args, utterance) -> bool:
    """An LLM/engine-chosen power_mode standby/quit only runs if the user's own words
    are an exact power phrase. 'wake' is harmless and always allowed."""
    mode = str((args or {}).get("mode", "")).strip().lower()
    if mode not in ("standby", "quit"):
        return True
    if not isinstance(utterance, str):
        return False
    return is_power_phrase(utterance, mode)


def _media_call(method: str) -> bool:
    """Call ONLY the media adapter (it already falls back to the media key itself).
    False means the adapter's debounce suppressed it: a deliberate no-op."""
    try:
        from media_adapter import get_media_adapter
        ok = bool(getattr(get_media_adapter(), method)())
    except Exception:
        _jl.get_logger("media").exception("media adapter %s failed", method)
        return False
    if not ok:
        _jl.get_logger("media").info("media %s suppressed by adapter debounce (deliberate no-op)", method)
    return ok


def _launch(target):
    subprocess.Popen(f'start "" {target}', shell=True)


def get_system_telemetry(ctx=None) -> str:
    """Collects real Windows hardware and J.A.R.V.I.S. runtime telemetry for Iron Man diagnostics."""
    parts = []
    # 1. RAM / Physical Memory via Win32 API
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        mem = MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            load = mem.dwMemoryLoad
            avail_gb = mem.ullAvailPhys / (1024 ** 3)
            parts.append(f"Memory load is at {load}% with {avail_gb:.1f} gigabytes free")
    except Exception:
        pass

    # 2. Power & Battery Status via Win32 API
    try:
        import ctypes
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]
        pwr = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
            if pwr.ACLineStatus == 1:
                if pwr.BatteryLifePercent != 255 and pwr.BatteryLifePercent <= 100:
                    parts.append(f"Power relays connected to AC mains at {pwr.BatteryLifePercent}% charge")
                else:
                    parts.append("Primary power relays connected to AC mains")
            elif pwr.BatteryLifePercent != 255:
                parts.append(f"Running on internal power cells at {pwr.BatteryLifePercent}%")
    except Exception:
        pass

    # 3. Tracking Subsystems
    state = "online"
    mode = "relative trackpad"
    if ctx and hasattr(ctx, "engine"):
        state = "online" if getattr(ctx.engine, "active", False) else "on standby"
        cfg = getattr(ctx.engine, "cfg", None)
        if cfg:
            mode = getattr(cfg, "tracking_mode", "relative")

    parts.append(f"Repulsor tracking is {state} in {mode} mode at 120 hertz")

    if parts:
        telemetry_summary = ". ".join(parts) + "."
        return f"All Mark systems operating nominally, sir. {telemetry_summary}"
    return "All Mark systems operating nominally, sir. Hand tracking telemetry is calibrated."


def is_spotify_available() -> bool:
    """Checks whether Spotify desktop application is installed or running on this PC."""
    appdata = os.environ.get("APPDATA", "")
    spotify_exe = os.path.join(appdata, "Spotify", "Spotify.exe")
    if os.path.exists(spotify_exe):
        return True
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    spotify_winapps = os.path.join(local_appdata, "Microsoft", "WindowsApps", "Spotify.exe")
    if os.path.exists(spotify_winapps):
        return True
    try:
        res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Spotify.exe"], capture_output=True, text=True, timeout=2)
        if "Spotify.exe" in res.stdout:
            return True
    except Exception:
        pass
    return False


def play_media(query: str, platform: str = "auto") -> Tuple[bool, str]:
    """Plays artist or track on Spotify or YouTube."""
    clean_q = query.strip()
    if not clean_q:
        return False, "I didn't catch what you would like to play, sir."

    encoded = urllib.parse.quote(clean_q)
    plat = platform.lower()

    if plat == "spotify" or (plat == "auto" and is_spotify_available()):
        try:
            subprocess.Popen(f'start "" "spotify:search:{encoded}"', shell=True)
            return True, f"Playing {clean_q.title()} on Spotify, sir."
        except Exception:
            webbrowser.open(f"https://open.spotify.com/search/{encoded}")
            return True, f"Playing {clean_q.title()} on Spotify, sir."
    else:
        webbrowser.open(f"https://www.youtube.com/results?search_query={encoded}")
        return True, f"Playing {clean_q.title()} on YouTube, sir."


# --- site search ("search elden ring malenia guide on youtube") -------------------------
# {q} is the percent-encoded query (quote, safe=""): valid in both query strings and path segments.
SITE_SEARCH_URL = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "github": "https://github.com/search?q={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
    "twitch": "https://www.twitch.tv/search?term={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
    "news": "https://news.google.com/search?q={q}",
    "steam": "https://store.steampowered.com/search/?term={q}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "imdb": "https://www.imdb.com/find/?q={q}",
    "twitter": "https://x.com/search?q={q}",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
}
SITE_SEARCH_NAMES = {
    "youtube": "YouTube", "google": "Google", "bing": "Bing", "duckduckgo": "DuckDuckGo",
    "wikipedia": "Wikipedia", "reddit": "Reddit", "amazon": "Amazon", "github": "GitHub",
    "stackoverflow": "Stack Overflow", "twitch": "Twitch", "spotify": "Spotify", "maps": "Google Maps",
    "images": "Google Images", "news": "Google News", "steam": "Steam", "ebay": "eBay", "imdb": "IMDb",
    "twitter": "X", "linkedin": "LinkedIn",
}
# Spoken name -> canonical site. Multi-word names win over their prefixes (sorted by length in the regex).
SITE_SEARCH_ALIASES = {
    **{k: k for k in SITE_SEARCH_URL},
    "yt": "youtube", "you tube": "youtube",
    "duck duck go": "duckduckgo", "ddg": "duckduckgo",
    "wiki": "wikipedia",
    "stack overflow": "stackoverflow",
    "git hub": "github",
    "google maps": "maps", "map": "maps",
    "google images": "images", "image search": "images",
    "google news": "news",
    "steam store": "steam",
    "e bay": "ebay",
    "x": "twitter", "x.com": "twitter",
    "linked in": "linkedin",
    "amazon.com": "amazon",
}
_SITE_ALT = "|".join(re.escape(a) for a in sorted(SITE_SEARCH_ALIASES, key=len, reverse=True))
_SITE_SUFFIX = r"(?:\s+(?:website|site|app|store|dot com))?"
_SITE_VERB = (r"(?:search(?:\s+up)?|find(?:\s+me)?|look\s+up|look\s+for|google|show\s+me|pull\s+up|bring\s+up|"
              r"watch|open|browse)")
_SITE_SEARCH_PATTERNS = (
    # "search [for] X on|in youtube", "look up X on reddit", "watch X on youtube", "show me X on youtube"
    re.compile(r"^" + _SITE_VERB + r"\s+(?:for\s+)?(?P<q>.+?)\s+(?:on|in|at|from|using|via)\s+(?:the\s+)?(?P<site>"
               + _SITE_ALT + r")" + _SITE_SUFFIX + r"$"),
    # "search youtube for X", "look up on reddit for X"
    re.compile(r"^(?:search|look\s+up|look|find|check)\s+(?:on\s+|in\s+)?(?:the\s+)?(?P<site>" + _SITE_ALT + r")"
               + _SITE_SUFFIX + r"\s+for\s+(?P<q>.+)$"),
    # "youtube search X", "reddit search for X"
    re.compile(r"^(?P<site>" + _SITE_ALT + r")\s+search\s+(?:for\s+)?(?P<q>.+)$"),
    # "search youtube X" (YouTube only: "search amazon stock price" is a Google search)
    re.compile(r"^search\s+(?P<site>youtube|you tube|yt)\s+(?P<q>.+)$"),
    # "how to beat malenia on youtube"
    re.compile(r"^(?P<q>how\s+to\s+.+?)\s+(?:on|in)\s+(?P<site>" + _SITE_ALT + r")" + _SITE_SUFFIX + r"$"),
)
_RE_SHOW_PICTURES = re.compile(r"^(?:show\s+me|find(?:\s+me)?|search\s+for|look\s+up|pull\s+up)\s+"
                               r"(?:some\s+)?(?:pictures|pics|images|photos)\s+of\s+(?P<q>.+)$")
_RE_YT_LEAD = re.compile(r"^(?:some\s+|a\s+)?(?:videos?|clips?)\s+(?:of|about|on)\s+")
_SITE_QUERY_JUNK = frozenset({"", "it", "this", "that", "something", "stuff", "things", "a", "the", "for"})


def resolve_search_site(name: str) -> Optional[str]:
    """Canonical SITE_SEARCH_URL key for a spoken site name ('you tube', 'google maps', 'yt'), or None."""
    n = " ".join(str(name or "").lower().split())
    n = re.sub(r"^the\s+", "", n)
    n = re.sub(r"\s+(?:website|site|app|store|dot com)$", "", n) if n not in SITE_SEARCH_ALIASES else n
    return SITE_SEARCH_ALIASES.get(n)


def parse_site_search(text: str) -> Optional[Tuple[str, str]]:
    """'search elden ring malenia guide on youtube' -> ('youtube', 'elden ring malenia guide'); else None.
    Plain 'search X' (no site) returns None so it stays a Google search."""
    t = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).lower().replace("’", "'").split())
    t = t.strip(" ,.!?")
    if not t or len(t) > MAX_UTTERANCE_CHARS:
        return None
    t = re.sub(r"^(?:please\s+|can you\s+(?:please\s+)?|could you\s+(?:please\s+)?|would you\s+(?:please\s+)?|"
               r"hey\s+|ok\s+|okay\s+|just\s+)", "", t).strip()
    t = re.sub(r"\s+(?:please|for me|jarvis)$", "", t).strip()
    site, query = None, None
    for rx in _SITE_SEARCH_PATTERNS:
        m = rx.match(t)
        if m:
            site, query = resolve_search_site(m.group("site")), m.group("q")
            break
    if site is None:
        m = _RE_SHOW_PICTURES.match(t)
        if not m:
            return None
        site, query = "images", m.group("q")
    query = " ".join(query.strip(" ,.!?").split())
    if site == "youtube":
        query = _RE_YT_LEAD.sub("", query).strip()
    if query in _SITE_QUERY_JUNK or not site:
        return None
    return site, query


def site_search_url(site: str, query: str) -> Optional[str]:
    key = resolve_search_site(site)
    if not key:
        return None
    q = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(query or "")).split())
    if not q:
        return None
    enc = urllib.parse.quote_plus(q) if key == "google" else urllib.parse.quote(q, safe="")
    return SITE_SEARCH_URL[key].format(q=enc)


def open_site_search(site: str, query: str) -> Tuple[bool, str]:
    """Opens the site's search results for query. Returns (ok, spoken reply)."""
    key = resolve_search_site(site)
    url = site_search_url(site, query)
    if not key or not url:
        return False, "I didn't catch what to search for, sir."
    q = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(query)).split())
    name = SITE_SEARCH_NAMES.get(key, key.title())
    if key == "spotify" and is_spotify_available():
        try:
            subprocess.Popen(f'start "" "spotify:search:{urllib.parse.quote(q, safe="")}"', shell=True)
            return True, f"Searching {name} for {q}."
        except Exception:
            pass
    webbrowser.open(url)
    return True, f"Searching {name} for {q}."


# --- gesture mode (camera hand control on/off) ------------------------------------------
GESTURE_ON_PHRASES = (
    "gesture mode on", "gesture mode", "gestures on", "enable gesture mode", "enable gestures",
    "enable gesture control", "turn on gestures", "turn gestures on", "turn on gesture mode",
    "turn on gesture control", "start gesture control", "start gesture mode", "start gestures",
    "activate gestures", "activate gesture mode", "activate gesture control", "use gestures",
    "i want to use gestures", "let me use gestures", "turn on the camera", "turn the camera on",
    "turn on camera", "camera on", "enable the camera", "enable camera",
)
GESTURE_OFF_PHRASES = (
    "gesture mode off", "gestures off", "disable gesture mode", "disable gestures", "disable gesture control",
    "turn off gestures", "turn gestures off", "turn off gesture mode", "turn off gesture control",
    "stop gesture control", "stop gesture mode", "stop gestures", "deactivate gestures",
    "deactivate gesture mode", "exit gesture mode", "turn off the camera", "turn the camera off",
    "turn off camera", "camera off", "disable the camera", "disable camera", "release the camera",
)
GESTURE_TOGGLE_PHRASES = (
    "toggle tracking", "toggle hand tracking", "toggle gesture tracking", "toggle gestures",
    "toggle gesture mode", "toggle gesture control", "toggle camera", "toggle the camera",
)


def gesture_mode_state(ctx) -> Optional[bool]:
    """ctx.gesture_mode_on() as a bool, or None when the host has no gesture-mode support."""
    fn = getattr(ctx, "gesture_mode_on", None) if ctx is not None else None
    if not callable(fn):
        return None
    try:
        v = fn()
    except Exception:
        return None
    return v if isinstance(v, bool) else None


def set_gesture_mode(ctx, on: bool) -> Tuple[bool, str]:
    """Turns gesture mode (camera + hand tracking) on/off via ctx.set_gesture_mode; falls back to
    pausing/resuming tracking on hosts without it. Returns (ok, spoken reply)."""
    fn = getattr(ctx, "set_gesture_mode", None) if ctx is not None else None
    if callable(fn):
        try:
            fn(bool(on))
        except Exception:
            _jl.get_logger("commands").exception("set_gesture_mode failed")
            return False, "I couldn't change gesture mode, sir."
        return True, "Gesture mode on." if on else "Gesture mode off, camera released."
    pause = getattr(ctx, "set_tracking_paused", None) if ctx is not None else None
    if callable(pause):
        try:
            pause(not on)
        except Exception:
            return False, "I couldn't change gesture mode, sir."
        return True, "Gesture mode on." if on else "Gesture mode off."
    return False, "Gesture mode isn't available right now, sir."


def _is_image_frame(frame) -> bool:
    try:
        import numpy as np
        return isinstance(frame, np.ndarray) and frame.ndim == 3 and frame.size > 0
    except Exception:
        return False


def _camera_frame(ctx):
    """Latest tracker frame, else a one-off ctx.snapshot_frame() (gesture mode off: camera not running)."""
    tracker = getattr(ctx, "tracker", None) if ctx else None
    get_latest = getattr(tracker, "get_latest_frame", None) if tracker is not None else None
    frame = None
    if callable(get_latest):
        try:
            frame = get_latest()
        except Exception:
            frame = None
    if frame is not None:
        return frame
    snap = getattr(ctx, "snapshot_frame", None) if ctx else None
    if callable(snap):
        try:
            shot = snap(timeout=3.0)
        except Exception:
            _jl.get_logger("commands").exception("snapshot_frame failed")
            return None
        if _is_image_frame(shot):
            return shot
    return None


def _log_habit(ctx, action: str, text: str = "") -> None:
    """Feed the local habits log (no-op unless learn_habits is on). Never raises into the command path."""
    utt = text or getattr(ctx, "_current_utterance", "")
    if not isinstance(utt, str) or not utt:
        utt = action.replace("_", " ")
    try:
        personal.log_command(utt, action)
    except Exception:
        _jl.get_logger("commands").exception("habit logging failed")


def _post_action(ctx, action: str, success: bool = True, args: Optional[dict] = None, text: str = ""):
    """Notifies overlay that a command or action has completed to trigger RESULT card."""
    _caller = sys._getframe(1).f_code.co_name  # stage logging: which tier resolved this action
    if _caller == "handle_island_voice_command":
        _jl.utterance_event(_jl.DECISION, tier="island", intent=action + "".join(f":{v}" for v in (args or {}).values()))
    elif _caller == "_run_single_command":
        _jl.utterance_event(_jl.DECISION, tier="regex", intent=action)
    _jl.utterance_event(_jl.RESULT, action=action, ok=success)
    if success and action and action != "none":
        _log_habit(ctx, action, text)
    if ctx and hasattr(ctx, "overlay") and ctx.overlay:
        try:
            ctx.overlay.post("action_executed", action=action, success=success)
        except Exception:
            pass
    if success and action and action != "none":
        try:
            from context_resolver import get_context_resolver
            get_context_resolver().record_turn(
                user_utterance=text or getattr(ctx, "_current_utterance", action),
                resolved_action=action,
                arguments=args or {},
            )
        except Exception:
            pass


def _execute_candidate(cand, ctx) -> str:
    """Executes a disambiguated Candidate and returns spoken reply."""
    from ai import execute_whitelisted_action
    action = cand.action
    args = dict(cand.args)

    executed, msg = execute_whitelisted_action(action, args, ctx)
    if executed:
        _post_action(ctx, action, True, args=args)
        if action == "volume_up":
            return "Adjusting volume upward, sir."
        elif action == "volume_down":
            return "Lowering volume, sir."
        elif action == "open_app":
            app = args.get("app", "application")
            return f"Opening {app}, sir."
        elif action == "open_site":
            site = args.get("site", "website")
            return f"Opening {site}, sir."
        elif action == "play_music":
            q = args.get("query", "music")
            return f"Playing {q.title()}, sir."
        elif action == "open_folder":
            f_name = args.get("folder", "folder")
            return msg or f"Opening {f_name} folder, sir."
        elif action == "open_file":
            return msg or f"Opening {args.get('file', 'file')}, sir."
        elif action == "open_recent_file":
            return msg or "Opening recent file, sir."
        elif action == "show_last_download":
            return msg or "Showing latest download in Explorer, sir."
        elif action == "remember_folder":
            return msg or "Folder alias remembered, sir."
        return f"{cand.label or action.replace('_', ' ').title()} executed, sir."
    return f"Unable to execute {cand.label or action}, sir."


def _execute_resolution(ref_res, ctx) -> Optional[str]:
    """Executes a referential resolution result and returns spoken reply."""
    from ai import execute_whitelisted_action
    action = ref_res.action
    args = dict(ref_res.args)

    executed, msg = execute_whitelisted_action(action, args, ctx)
    if executed:
        _post_action(ctx, action, True, args=args)
        return ref_res.reply or f"Executing {ref_res.label or action}, sir."
    return None


def handle_island_voice_command(text: str, ctx: Any = None) -> Optional[str]:
    """
    Direct intercept for Dynamic Island voice navigation and card display intents:
      - 'next page' / 'next card' -> overlay.post('island_page_step', direction=1)
      - 'previous page' / 'prev page' / 'last page' -> overlay.post('island_page_step', direction=-1)
      - 'show weather' / 'weather page' -> overlay.post('island_show_page', page='weather')
      - 'show hardware' / 'hardware page' / 'show specs' -> overlay.post('island_show_page', page='hardware')
      - 'show media' / 'media page' -> overlay.post('island_show_page', page='media')
      - 'show calendar' / 'calendar page' -> overlay.post('island_show_page', page='calendar')
      - 'show jarvis' / 'jarvis page' -> overlay.post('island_show_page', page='jarvis')
      - 'expand island' / 'open island' -> overlay.post('island_expand')
      - 'collapse island' / 'close island' -> overlay.post('island_collapse')
    """
    if not text:
        return None

    clean_t = text.lower().strip(" ,.!?")
    clean_t = re.sub(r"^(?:please\s+|can you\s+(?:please\s+)?|could you\s+(?:please\s+)?|would you\s+(?:please\s+)?|hey\s+|ok\s+|okay\s+|just\s+)", "", clean_t).strip()
    if not (clean_t.startswith("jarvis page") or clean_t.startswith("jarvis card")):
        clean_t = re.sub(r"^(?:jarvis\s+|hey\s+jarvis\s+|ok\s+jarvis\s+)", "", clean_t).strip()

    def _post(kind: str, **kwargs):
        # Thread-safety: this runs on the voice worker thread. Never touch Qt widgets here;
        # the only channel to the island is overlay.post (drained on the GUI thread).
        if not ctx:
            return
        ov = getattr(ctx, "overlay", None)
        if ov is not None and hasattr(ov, "post"):
            ov.post(kind, **kwargs)
        elif hasattr(ctx, "post"):
            ctx.post(kind, **kwargs)

    # 1. Page Step (Next)
    if clean_t in ("next page", "next card") or _any(clean_t, "next page", "next card"):
        _post("island_page_step", direction=1)
        _post_action(ctx, "island_page_step", True, args={"direction": 1}, text=text)
        return "Next card, sir."

    # 2. Page Step (Previous)
    if clean_t in ("previous page", "prev page", "last page", "previous card", "prev card", "last card") or _any(clean_t, "previous page", "prev page", "last page", "previous card", "prev card", "last card"):
        _post("island_page_step", direction=-1)
        _post_action(ctx, "island_page_step", True, args={"direction": -1}, text=text)
        return "Previous card, sir."

    # 3. Show weather / weather page
    if clean_t in ("show weather", "weather page", "weather card") or _any(clean_t, "show weather", "weather page", "weather card"):
        _post("island_show_page", page="weather")
        _post("expand_widget", widget="weather")  # legacy no-op kind, kept for compatibility
        _post_action(ctx, "island_show_page", True, args={"page": "weather"}, text=text)
        return "Displaying weather forecast, sir."

    # 4. Show hardware / hardware page / show specs
    if clean_t in ("show hardware", "hardware page", "show specs", "specs page", "hardware card") or _any(clean_t, "show hardware", "hardware page", "show specs", "specs page", "hardware card"):
        _post("island_show_page", page="hardware")
        _post_action(ctx, "island_show_page", True, args={"page": "hardware"}, text=text)
        return "Displaying hardware telemetry, sir."

    # 5. Show media / media page
    if clean_t in ("show media", "media page", "media card") or _any(clean_t, "show media", "media page", "media card"):
        _post("island_show_page", page="media")
        _post("expand_widget", widget="media")  # legacy no-op kind, kept for compatibility
        _post_action(ctx, "island_show_page", True, args={"page": "media"}, text=text)
        return "Displaying media controls, sir."

    # 6. Show calendar / calendar page
    if clean_t in ("show calendar", "calendar page", "calendar card") or _any(clean_t, "show calendar", "calendar page", "calendar card"):
        _post("island_show_page", page="calendar")
        _post_action(ctx, "island_show_page", True, args={"page": "calendar"}, text=text)
        return "Displaying calendar schedule, sir."

    # 7. Show jarvis / jarvis page
    if clean_t in ("show jarvis", "jarvis page", "jarvis card") or _any(clean_t, "show jarvis", "jarvis page", "jarvis card"):
        if clean_t.startswith("show jarvis") and getattr(ctx, "is_hidden", None) and ctx.is_hidden() is True:
            _set_hidden(ctx, False)
        _post("island_show_page", page="jarvis")
        _post_action(ctx, "island_show_page", True, args={"page": "jarvis"}, text=text)
        return "Displaying Jarvis assistant card, sir."

    # 8. Expand island / open island
    if clean_t in ("expand island", "open island", "expand the island", "open the island") or _any(clean_t, "expand island", "open island", "expand the island", "open the island"):
        _post("island_expand")
        _post_action(ctx, "island_expand", True, text=text)
        return "Expanding Dynamic Island, sir."

    # 9. Collapse island / close island
    if clean_t in ("collapse island", "close island", "collapse the island", "close the island") or _any(clean_t, "collapse island", "close island", "collapse the island", "close the island"):
        _post("island_collapse")
        _post_action(ctx, "island_collapse", True, text=text)
        return "Collapsing Dynamic Island, sir."

    return None


# --- personal intents (learning from the user) ------------------------------------------
_PREV_UTTERANCE = {"text": "", "at": 0.0}
_CORRECTION_WINDOW_S = 120.0
_RE_WAKE_LEAD = re.compile(r"^(?:(?:hey|ok|okay)[\s,.:;!?-]+)?jarvis\b[\s,.:;!?-]*", re.I)
_RE_CORRECTION = re.compile(
    r"^(?:(?:no|nope|nah|wrong|not that|that'?s wrong|that is wrong|that'?s not right|that is not right|"
    r"that'?s not what i said)\b[\s,.:;!?-]*)?"
    r"(?:i said|i meant|i mean|i was saying|what i said was|it'?s|it is)\s+(.+)$", re.I)
_RE_CALL_ME = re.compile(r"^(?:please\s+)?(?:call me|my name is|my name'?s)\s+(.+)$", re.I)
_RE_REMEMBER = re.compile(
    r"^(?:please\s+)?(?:remember|note|keep in mind)(?:\s+that)?\s+(?:i|my)\s+"
    r"(like|love|enjoy|use|play|prefer|favou?rite browser is|browser is|search engine is|favou?rite search engine is)\s+(.+)$",
    re.I)
_RE_HIDE = re.compile(r"^(?:please\s+)?(?:hide|conceal)\s+(?:yourself|jarvis)$", re.I)
_RE_SHOW = re.compile(r"^(?:please\s+)?(?:show\s+yourself|come\s+back|unhide(?:\s+yourself|\s+jarvis)?|un-hide)$", re.I)
_BROWSERS = {"chrome": "chrome", "google chrome": "chrome", "edge": "edge", "microsoft edge": "edge",
             "firefox": "firefox", "brave": "brave", "opera": "opera"}
_SEARCH_ENGINES = {"google": "google", "bing": "bing", "duckduckgo": "duckduckgo", "duck duck go": "duckduckgo"}


def _remember_utterance(text: str) -> None:
    _PREV_UTTERANCE["text"], _PREV_UTTERANCE["at"] = str(text or ""), time.time()


def correction_pairs(prev: str, meant: str, max_words: int = 3) -> List[Tuple[str, str]]:
    """Diff the previous utterance against what the user says they meant; returns [(heard, meant), ...] for
    each replaced span (at most max_words words on either side). Pure."""
    import difflib
    a = re.findall(r"[\w']+", str(prev or "").lower())
    b = re.findall(r"[\w']+", str(meant or "").lower())
    if not a or not b:
        return []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < 0.3:
        return []
    pairs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace" or (j2 - j1) > max_words:
            continue
        if (i2 - i1) > (j2 - j1):
            # The user restated only part of the phrase: keep the heard words that best match what they said.
            n = j2 - j1
            def _score(k):
                return sum(difflib.SequenceMatcher(None, a[k + x], b[j1 + x]).ratio() for x in range(n))
            k = max(range(i1, i2 - n + 1), key=_score)
            i1, i2 = k, k + n
        if (i2 - i1) <= max_words:
            pairs.append((" ".join(a[i1:i2]), " ".join(b[j1:j2])))
    return pairs


def _apply_pairs(prev: str, pairs) -> str:
    out = prev
    for heard, meant in pairs:
        out = re.sub(r"(?<!\w)" + re.escape(heard).replace(r"\ ", r"\s+") + r"(?!\w)",
                     lambda _m, _x=meant: _x, out, flags=re.I)
    return out


def _set_hidden(ctx, on: bool) -> None:
    if hasattr(ctx, "set_hidden"):
        ctx.set_hidden(on)
    elif ctx is not None and getattr(ctx, "overlay", None) is not None:
        ctx.overlay.post("hidden", on=on)


def _handle_correction(meant: str, ctx) -> str:
    prev, at = _PREV_UTTERANCE["text"], _PREV_UTTERANCE["at"]
    meant = meant.strip(" ,.!?")
    if not prev or time.time() - at > _CORRECTION_WINDOW_S or not meant:
        _post_action(ctx, "correction", False, text=meant)
        return "I don't have anything to correct, sir."
    pairs = correction_pairs(prev, meant)
    _PREV_UTTERANCE["text"] = ""          # a correction never chains into another correction
    learned = sum(1 for heard, fixed in pairs if personal.add_correction(heard, fixed))
    _post_action(ctx, "correction", True, args={"pairs": len(pairs)}, text=meant)
    redo = _apply_pairs(prev, pairs) if pairs else meant
    reply = run_command(redo, ctx)
    if reply:
        return reply
    return "Noted, sir." if learned else "Understood, sir."


def _handle_call_me(name: str, ctx) -> str:
    name = " ".join(name.strip(" ,.!?").split())
    low = name.lower()
    if low in ("sir", "ma'am", "madam", "maam"):
        said = "sir" if low == "sir" else "ma'am"
        fields = {"address_as": said}
    elif low in ("by my name", "by name"):
        fields, said = {"address_as": "name"}, "by name"
    elif 0 < len(name) <= 30 and len(name.split()) <= 3:
        said = name.title() if name.islower() else name
        fields = {"name": said}
    else:
        _post_action(ctx, "set_name", False, text=name)
        return "I didn't catch the name, sir."
    ok = personal.update_profile(**fields)
    _post_action(ctx, "set_name", ok, text=name)
    if not ok:
        return "Learning your profile is switched off in Settings, sir."
    return f"Very well, I'll call you {said}."


def _handle_remember(verb: str, what: str, ctx) -> str:
    verb, what = verb.lower(), what.strip(" ,.!?")
    if not what or len(what) > 60:
        _post_action(ctx, "remember", False, text=what)
        return "I didn't catch that, sir."
    if "browser" in verb:
        b = _BROWSERS.get(what.lower())
        ok = bool(b) and personal.update_profile(browser=b)
    elif "search engine" in verb:
        e = _SEARCH_ENGINES.get(what.lower())
        ok = bool(e) and personal.update_profile(search_engine=e)
    elif verb == "play":
        ok = personal.add_to_profile("games", what)
    elif verb == "use":
        ok = personal.add_to_profile("favorite_apps", what)
    else:
        ok = personal.add_to_profile("uses", what)
    _post_action(ctx, "remember", bool(ok), text=what)
    if not ok:
        return "I couldn't save that, sir. Learning may be off in Settings."
    return f"Noted, sir. I'll remember {what}."


def handle_personal_intent(text: str, ctx) -> Optional[str]:
    """Corrections, profile statements and hide/show. Returns the reply, or None if the text is none of those."""
    t = _RE_WAKE_LEAD.sub("", " ".join(str(text or "").split()), count=1).strip(" ,.!?")
    if not t or len(t) > 200:
        return None
    if _RE_HIDE.match(t):
        _set_hidden(ctx, True)
        _post_action(ctx, "hide", True, text=t)
        return "Hiding, sir. Say show yourself to bring me back."
    if _RE_SHOW.match(t):
        _set_hidden(ctx, False)
        _post_action(ctx, "show", True, text=t)
        return "Back on screen, sir."
    m = _RE_CALL_ME.match(t)
    if m:
        return _handle_call_me(m.group(1), ctx)
    m = _RE_REMEMBER.match(t)
    if m:
        return _handle_remember(m.group(1), m.group(2), ctx)
    m = _RE_CORRECTION.match(t)
    if m:
        # Bare "it's X" / "it is X" is ordinary speech; only treat it as a correction with an explicit "no".
        if re.match(r"^(?:it'?s|it is)\b", t, re.I):
            return None
        return _handle_correction(m.group(1), ctx)
    return None


def web_search_url(query: str) -> str:
    """Search URL on the user's preferred engine (profile), Google by default."""
    q = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(query)).split())
    engine = ""
    try:
        engine = personal.get_profile().get("search_engine", "") if personal.flag("profile") else ""
    except Exception:
        _jl.get_logger("commands").exception("profile read failed")
    tmpl = SITE_SEARCH_URL.get(engine) or SITE_SEARCH_URL["google"]
    return tmpl.format(q=urllib.parse.quote_plus(q))


_BROWSER_EXE = {"chrome": "chrome", "edge": "msedge", "firefox": "firefox", "brave": "brave", "opera": "opera"}


def open_web_url(url: str) -> None:
    """Open a URL in the user's favorite browser (profile) or the system default."""
    exe = ""
    if personal.flag("profile"):
        exe = _BROWSER_EXE.get(personal.get_profile().get("browser", ""), "")
    if exe and sys.platform == "win32":
        subprocess.Popen(["cmd.exe", "/c", "start", "", exe, url])
        return
    webbrowser.open(url)


def run_command(text, ctx):

    """Returns the reply Jarvis should speak, or None if nothing matched."""
    text = cap_utterance(text)
    # Personal intents: corrections ("no, I said ..."), "call me ...", "remember that I ...", hide/show.
    personal_reply = handle_personal_intent(text, ctx)
    if personal_reply is not None:
        return personal_reply
    _remember_utterance(text)
    text = personal.apply_corrections(text)
    # 0. Check chained / compound commands (e.g. 'volume up and maximize window')
    try:
        import chained_commands
        sub_cmds = chained_commands.split_chained_commands(text)
        if len(sub_cmds) > 1:
            ok, combined_reply, _ = chained_commands.execute_chained_commands(
                compound_text=text,
                ctx=ctx,
                execute_single_fn=_run_single_command,
                is_gated_fn=is_chain_gated,
            )
            return combined_reply
    except Exception as exc:
        print(f"[commands] chained commands note: {exc}")

    return _run_single_command(text, ctx)


def _run_single_command(text, ctx):
    """Internal single-command evaluator."""
    import win_input
    text = cap_utterance(text)
    t = text.lower().strip(" ,.!?")
    # Strip conversational filler prefixes
    clean_t = re.sub(r"^(?:please\s+|can you\s+(?:please\s+)?|could you\s+(?:please\s+)?|would you\s+(?:please\s+)?|hey\s+|ok\s+|okay\s+|just\s+)", "", t).strip()

    # Dynamic Island Voice Navigation Intercept (Phase 4)
    island_reply = handle_island_voice_command(clean_t, ctx)
    if island_reply:
        return island_reply

    # Strip a leading wake word ("jarvis play" -> "play") now that island navigation
    # ("jarvis page") has had its chance; keeps downstream matching on the command itself.
    clean_t = strip_wake_prefix(clean_t)
    clean_t = _LEAD_GREETING_RE.sub("", clean_t, count=1).strip(" ,.!?") or clean_t
    clean_t = _STT_COMMAND_FIXES.get(" ".join(re.sub(r"[^\w\s']", " ", clean_t).split()), clean_t)

    # Phase 7 Safety Gate: intercept unsafe or arbitrary shell/PowerShell requests
    from quick_answers import is_unsafe_shell_command
    if is_unsafe_shell_command(clean_t):
        return "I can't do that, sir."

    # Phase 7 Screen Awareness (explicit user queries)
    import screen_awareness
    if screen_awareness.is_screen_query(clean_t):
        _screen_reply = screen_awareness.analyze_screen(text, ctx)
        _post_action(ctx, "screen_query", bool(_screen_reply), text=text)
        return _screen_reply

    # Phase 7 World Time Lookups (System 1 Reflex: <1 ms)
    import quick_answers
    if quick_answers.is_timezone_query(clean_t):
        tz_reply = quick_answers.get_world_time(clean_t, ctx)
        if tz_reply:
            _post_action(ctx, "world_time", True, text=text)
            return tz_reply

    # Phase 7 Explicit Wikipedia queries
    m_wiki = re.match(r"^(?:wiki|wikipedia)\s+(.+)", clean_t)
    if m_wiki:
        w_ans = quick_answers.get_quick_answer(clean_t, ctx)
        if w_ans:
            _post_action(ctx, "wiki", True, text=text)
            return w_ans
        if not quick_answers._ai_allowed(ctx):
            _post_action(ctx, "wiki", False, text=text)
            return "I couldn't find that on Wikipedia, sir, and AI is off in Settings."

    if ctx:
        try:
            ctx._current_utterance = text
        except Exception:
            pass

    # Dictation mode interceptor
    try:
        import productivity
        if productivity.is_dictation_mode():
            if clean_t in ("stop dictation", "end dictation", "dictation mode off", "disable dictation", "exit dictation"):
                productivity.set_dictation_mode(False)
                return "Dictation mode deactivated, sir."
            productivity.type_text(text)
            return f"Typed: '{text}'"
    except Exception as exc:
        print(f"[commands] dictation interceptor note: {exc}")

    from context_resolver import get_context_resolver, get_disambiguation_engine
    dis_eng = get_disambiguation_engine()
    ctx_res = get_context_resolver()

    # 1. Handle active disambiguation session if present
    if dis_eng.is_active():
        if clean_t in ("cancel", "never mind", "nevermind", "abort", "dismiss", "stop", "neither", "none of them"):
            dis_eng.cancel("cancelled")
            return "Disambiguation cancelled, sir."

        cand = dis_eng.resolve_selection(clean_t)
        if cand:
            reply = _execute_candidate(cand, ctx)
            ctx_res.record_turn(
                user_utterance=text,
                resolved_action=cand.action,
                arguments=cand.args,
                candidates=[cand],
            )
            return reply
        else:
            dis_eng.cancel("new_command")

    # 2. Check context resolver for referential phrases (open it, louder, do that again, the other one)
    ref_res = ctx_res.resolve_referential(clean_t)
    if ref_res:
        reply = _execute_resolution(ref_res, ctx)
        if reply:
            ctx_res.record_turn(
                user_utterance=text,
                resolved_action=ref_res.action,
                arguments=ref_res.args,
                candidates=ref_res.candidates,
            )
            return reply

    # --- Pending confirmation for destructive actions ---
    global _PENDING_CONFIRMATION
    if _PENDING_CONFIRMATION:
        if clean_t in ("cancel", "never mind", "nevermind", "abort", "dismiss", "stop", "no"):
            pend = _PENDING_CONFIRMATION
            _PENDING_CONFIRMATION = None
            return f"{pend.replace('_', ' ').title()} cancelled, sir."
        elif clean_t in ("yes", "confirm", "proceed", "do it", "sure", "affirmative", "yes please", "yes confirm", "confirm this", "yes confirm shutdown", "confirm shutdown", "yes confirm restart", "confirm restart", "confirm close", "yes confirm close", "force close"):
            pend = _PENDING_CONFIRMATION
            _PENDING_CONFIRMATION = None
            import system_control
            if pend == "shutdown":
                ok, msg = system_control.shutdown_system(confirmed=True)
                _post_action(ctx, "shutdown_system", ok, text=text)
                return msg
            elif pend == "restart":
                ok, msg = system_control.restart_system(confirmed=True)
                _post_action(ctx, "restart_system", ok, text=text)
                return msg
            elif pend == "close_window":
                ok, msg = system_control.close_window(confirm=True)
                _post_action(ctx, "close_window", ok, text=text)
                return "Window closed, sir."
            elif pend in ("close_this", "close_under_cursor"):
                import window_resolver
                ok, msg = window_resolver.close_this(confirm=True)
                _post_action(ctx, "close_this", ok, text=text)
                return msg

    # Tier-1 exact phrases: explicit tracking pause/resume (no "did you mean" disambiguation)
    _trk = None
    if _exact(clean_t, "resume tracking", "start tracking"):
        _trk = False
    elif _exact(clean_t, "pause tracking", "stop tracking"):
        _trk = True
    if _trk is not None and ctx is not None and hasattr(ctx, "set_tracking_paused"):
        ctx.set_tracking_paused(_trk)
        _post_action(ctx, "tracking_paused" if _trk else "tracking_resumed", True, text=text)
        return "Tracking paused, sir." if _trk else "Tracking resumed, sir."

    # Tier-1 exact phrases: gesture mode (camera + hand control) on / off / toggle
    _gest = None
    if _exact(clean_t, *GESTURE_ON_PHRASES):
        _gest = True
    elif _exact(clean_t, *GESTURE_OFF_PHRASES):
        _gest = False
    elif _exact(clean_t, *GESTURE_TOGGLE_PHRASES):
        _state = gesture_mode_state(ctx)
        if _state is not None:   # hosts without gesture mode keep the island tracking toggle below
            _gest = not _state
    if _gest is not None:
        _ok, _msg = set_gesture_mode(ctx, _gest)
        _post_action(ctx, "gesture_mode", _ok, args={"enabled": _gest}, text=text)
        return _msg

    # Direct verbal confirmations
    if _exact(clean_t, *CONFIRM_SHUTDOWN):
        _PENDING_CONFIRMATION = None
        import system_control
        ok, msg = system_control.shutdown_system(confirmed=True)
        _post_action(ctx, "shutdown_system", ok, text=text)
        return msg

    if _exact(clean_t, *CONFIRM_RESTART):
        _PENDING_CONFIRMATION = None
        import system_control
        ok, msg = system_control.restart_system(confirmed=True)
        _post_action(ctx, "restart_system", ok, text=text)
        return msg

    if _exact(clean_t, *CONFIRM_CLOSE):
        _PENDING_CONFIRMATION = None
        import system_control
        ok, msg = system_control.close_window(confirm=True)
        _post_action(ctx, "close_window", ok, text=text)
        return "Window closed, sir."

    # Destructive actions gates
    if _exact(clean_t, *SHUTDOWN_PHRASES):
        _PENDING_CONFIRMATION = "shutdown"
        return "Shutdown requires confirmation, sir. Please say 'confirm shutdown' to proceed."
    if clean_t == "shutdown":
        _PENDING_CONFIRMATION = "shutdown"
        return "Shutdown requires confirmation, sir. Please say 'confirm shutdown' to proceed."

    if _exact(clean_t, *RESTART_PHRASES):
        _PENDING_CONFIRMATION = "restart"
        return "Restart requires confirmation, sir. Please say 'confirm restart' to proceed."
    if clean_t in ("restart", "reboot"):
        _PENDING_CONFIRMATION = "restart"
        return "Restart requires confirmation, sir. Please say 'confirm restart' to proceed."

    # --- power / mode ---
    if _exact(clean_t, *SLEEP_SYSTEM_PHRASES):
        import system_control
        ok, msg = system_control.sleep_system()
        _post_action(ctx, "sleep_system", ok, text=text)
        return "Suspending system power states, sir."

    if _exact(clean_t, *POWER_QUIT):
        _post_action(ctx, "power_mode", True)
        ctx.quit()
        return "Powering down. Goodbye, sir."
    if _exact(clean_t, *POWER_STANDBY):
        _post_action(ctx, "power_mode", True)
        ctx.set_active(False)
        reset_ai_history()
        return "Going to standby, sir."
    if _exact(clean_t, *POWER_WAKE):
        _post_action(ctx, "power_mode", True)
        ctx.set_active(True)
        return "Online. At your service, sir."

    # --- ai setup ---
    if _any(clean_t, "set up ai", "setup ai", "configure ai", "ai settings", "open ai settings"):
        _post_action(ctx, "settings", True)
        if hasattr(ctx, "overlay") and ctx.overlay:
            ctx.overlay.open_settings("AI")  # self-marshals to the GUI thread when off-thread
            return "Opening AI settings, sir."
        return "Settings overlay is unavailable, sir."

    # --- dwell & pointer modes ---
    if _any(clean_t, "dwell on", "enable dwell", "turn on dwell", "dwell click on"):
        ctx.engine.cfg.dwell_mode = "click"
        _post_action(ctx, "dwell_toggle", True)
        return "Dwell click enabled, sir."
    if _any(clean_t, "dwell off", "disable dwell", "turn off dwell", "dwell click off"):
        ctx.engine.cfg.dwell_mode = "off"
        _post_action(ctx, "dwell_toggle", True)
        return "Dwell click disabled, sir."
    if _any(clean_t, "dwell menu", "dwell radial", "radial menu on", "radial mode"):
        ctx.engine.cfg.dwell_mode = "radial"
        _post_action(ctx, "dwell_toggle", True)
        return "Dwell radial menu enabled, sir."
    if _any(clean_t, "precision mode on", "enable precision", "fine aim on"):
        ctx.engine.cfg.precision_mode = True
        _post_action(ctx, "precision_mode", True)
        return "Precision fine-aim mode enabled, sir."
    if _any(clean_t, "precision mode off", "disable precision", "fine aim off"):
        ctx.engine.cfg.precision_mode = False
        _post_action(ctx, "precision_mode", True)
        return "Precision fine-aim mode disabled, sir."
    if _any(clean_t, "relative mode", "trackpad mode"):
        ctx.engine.cfg.tracking_mode = "relative"
        _post_action(ctx, "tracking_mode", True)
        return "Relative trackpad mode active, sir."
    if _any(clean_t, "absolute mode", "screen mode"):
        ctx.engine.cfg.tracking_mode = "absolute"
        _post_action(ctx, "tracking_mode", True)
        return "Absolute screen mapping active, sir."

    # --- Stark protocols ---
    if _any(clean_t, "house party protocol", "house party", "protocol house party"):
        _launch("chrome")
        _post_action(ctx, "house_party", True)
        return "House Party Protocol initiated, sir. Bringing primary workstations online."
    if _any(clean_t, "stealth mode", "protocol stealth", "go dark", "silent mode"):
        win_input.press("volumemute")
        win_input.hotkey("win", "down")
        _post_action(ctx, "stealth_mode", True)
        return "Stealth mode engaged, sir. Audio muted and visual signatures minimized."
    if _exact(clean_t, *CLEAN_SLATE_PHRASES):
        win_input.hotkey("win", "d")
        reset_ai_history()
        if ctx:
            ctx.set_active(False)
        _post_action(ctx, "clean_slate", True)
        return "Clean Slate Protocol executed, sir. Have a pleasant evening."
    if _any(clean_t, "target practice", "repulsor practice", "repulsor calibration", "aim test"):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        target_html = os.path.join(base_dir, "tests", "target_practice.html")
        if os.path.exists(target_html):
            webbrowser.open(f"file:///{target_html}")
        else:
            webbrowser.open("https://www.google.com")
        _post_action(ctx, "target_practice", True)
        return "Target practice range initialized, sir. Repulsor calibration ready."

    # --- voice training & speaker profile ---
    if _any(clean_t, "train my voice", "train voice", "enroll voice", "learn my voice", "calibrate voice", "voice training", "recognize my voice"):
        try:
            from voice_profile import get_voice_profile
            from voice import Voice
            vp = get_voice_profile()
            latest = getattr(Voice, "_last_raw_audio", None)
            if latest:
                feat = vp.extract_features(latest)
                bands = None if feat is None else [float(x) for x in feat[1:9]]
                total = sum(bands) if bands else 0.0
                # Speech has energy above 550 Hz (bands 3-8); hum/rumble does not.
                if not bands or total <= 0.0 or sum(bands[2:]) / total < 0.01:
                    _post_action(ctx, "train_voice", False, text=text)
                    return "I couldn't hear your voice clearly, sir. Please say a full sentence after asking me to learn your voice."
                ok, msg = vp.enroll([latest])
                if ok:
                    vp.enabled = True
                    cfg = getattr(getattr(ctx, "engine", None), "cfg", None)
                    if getattr(cfg, "voice_speaker_verification", False):
                        _post_action(ctx, "train_voice", True, text=text)
                        return "Voice sample saved, sir. I'll now respond only to your voice."
                    _post_action(ctx, "train_voice", True, text=text)
                    return ("I've saved a sample of your voice, sir. Speaker verification is off, so I'll "
                            "still respond to anyone; you can enable it in Settings, Voice.")
                _post_action(ctx, "train_voice", False, text=text)
                return "I couldn't hear your voice clearly, sir. Please say a full sentence after asking me to learn your voice."
            _post_action(ctx, "train_voice", True, text=text)
            return "Vocal training mode active, sir. Speak a sentence clearly and I will calibrate to your voice."
        except Exception as exc:
            print(f"[commands] voice training note: {exc}")
            _post_action(ctx, "train_voice", False, text=text)
            return "Voice training calibration encountered an issue, sir."

    # --- routines (Phase 6) ---
    try:
        import routines
        rout_reply = routines.handle_routine_command(clean_t, ctx=ctx)
        if rout_reply:
            _post_action(ctx, "start_routine", True, args={"text": clean_t}, text=text)
            return rout_reply
    except Exception as exc:
        print(f"[commands] routines handler note: {exc}")

    # --- local scheduler: timers, alarms, reminders (Phase 6) ---
    try:
        import scheduler
        sched_reply = scheduler.handle_scheduler_command(clean_t, ctx=ctx)
        if sched_reply:
            _post_action(ctx, "set_timer", True, args={"text": clean_t}, text=text)
            return sched_reply
    except Exception as exc:
        print(f"[commands] scheduler handler note: {exc}")

    # --- local deterministic math & unit parser (Phase 6) ---
    try:
        import math_parser
        math_res = math_parser.parse_math_or_unit_query(clean_t)
        if math_res:
            raw_ans, spoken_ans = math_res
            if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                try:
                    ctx.overlay.post("result", text=f"Math: {raw_ans}", icon="calc", duration=3.5)
                except Exception:
                    pass
            _post_action(ctx, "calculate", True, args={"expression": clean_t, "result": raw_ans}, text=text)
            return spoken_ans
    except Exception as exc:
        print(f"[commands] math handler note: {exc}")

    # --- quick notes, dictation, clipboard (Phase 6) ---
    try:
        import productivity
        prod_reply = productivity.handle_productivity_command(text, ctx=ctx)
        if prod_reply:
            _post_action(ctx, "quick_note", True, args={"text": text}, text=text)
            return prod_reply
    except Exception as exc:
        print(f"[commands] productivity handler note: {exc}")

    # --- chatter & diagnostics ---
    if _any(clean_t, "diagnostic", "diagnostics", "run diagnostic", "run diagnostics", "system diagnostic", "suit diagnostic", "suit status", "power level", "power levels", "arc reactor", "telemetry"):
        _post_action(ctx, "diagnostics", True, text=text)
        return get_system_telemetry(ctx)
    if clean_t in ("hello", "hi", "hey", "good morning", "good evening", "good afternoon"):
        _post_action(ctx, "small_talk", True, text=text)
        return "Welcome back, sir."
    if _any(clean_t, "status", "report", "how are you"):
        _post_action(ctx, "diagnostics", True, text=text)
        return get_system_telemetry(ctx)
    if _any(clean_t, "what's the weather", "weather forecast"):
        from weather_client import weather_for_cfg
        cfg = getattr(ctx, "cfg", None) or getattr(getattr(ctx, "engine", None), "cfg", None)
        weather = weather_for_cfg(cfg)

        if weather:
            temp_int = int(round(weather.temperature))
            desc = weather.description
            c_name = weather.city.split(",")[0]
            card_text = f"{c_name}: {temp_int}{weather.unit_symbol}, {desc}"
            if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                try:
                    ctx.overlay.post("result", text=card_text, icon="weather", duration=4.0)
                except Exception:
                    pass
            _post_action(ctx, "weather", True)
            return f"It is {temp_int} degrees and {desc.lower()} in {c_name}, sir."
        else:
            _post_action(ctx, "weather", False)
            return "I am unable to retrieve the weather telemetry right now, sir."

    if re.search(r"\b(what time is it|what's the time|what is the time|current time|tell me the time|time)\b", clean_t):
        now_str = datetime.datetime.now().strftime("%I:%M %p").lstrip("0")
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("result", text=f"Time: {now_str}", icon="time", duration=3.0)
            except Exception:
                pass
        _post_action(ctx, "time", True)
        return f"It is {now_str}, sir."
    if _any(clean_t, "date", "what day"):
        _post_action(ctx, "date", True, text=text)
        return "Today is " + datetime.datetime.now().strftime("%A, %B %d") + "."
    if _any(clean_t, "thank you jarvis", "thanks jarvis", "good job jarvis", "well done jarvis", "nice work jarvis"):
        _post_action(ctx, "small_talk", True, text=text)
        return "Always a pleasure watching you work, sir."
    # Whole utterance only: inside a phrase it caught "what are you up" (misheard "volume up").
    if re.fullmatch(r"(?:who|what) (?:are|r) you(?: (?:exactly|again|jarvis))?",
                    " ".join(re.sub(r"[^\w\s']", " ", clean_t).split())):
        _post_action(ctx, "small_talk", True, text=text)
        return "I am J.A.R.V.I.S., Just A Rather Very Intelligent System. Configured to manage your computing environment and tactical telemetry, sir."
    if _any(clean_t, "are you there", "you there jarvis"):
        _post_action(ctx, "small_talk", True, text=text)
        return "For you, sir, always."

    # --- files and folders (Phase 4) ---
    m_rem = re.match(r"^(?:remember|save)\s+(?:this\s+folder\s+)?(?:as\s+)?(.+)", clean_t)
    if ("remember" in clean_t or "save folder" in clean_t) and m_rem:
        alias_raw = m_rem.group(1).strip()
        import file_ops
        res = file_ops.remember_folder(alias=alias_raw, path=os.getcwd(), confirmed=True)
        _post_action(ctx, "remember_folder", res.success, args={"alias": alias_raw, "path": res.path}, text=text)
        if res.success:
            ctx_res.record_turn(user_utterance=text, resolved_action="remember_folder", arguments={"alias": alias_raw, "path": res.path})
        return res.message

    if _any(clean_t, "show last download", "show latest download", "reveal last download", "locate last download", "show download in explorer", "find last download", "highlight last download"):
        import file_ops
        res = file_ops.show_last_download()
        _post_action(ctx, "show_last_download", res.success, args={"file": res.path}, text=text)
        if res.success:
            ctx_res.record_turn(user_utterance=text, resolved_action="show_last_download", arguments={"file": res.path})
        return res.message

    if _any(clean_t, "open recent file", "open latest file", "open last file", "open recent download", "open latest download", "open last download", "open recent pdf", "open latest pdf"):
        folder = "downloads"
        if "documents" in clean_t:
            folder = "documents"
        elif "desktop" in clean_t:
            folder = "desktop"
        ext_m = re.search(r"\b(pdf|txt|md|docx|xlsx|csv|png|jpg|mp4|py)\b", clean_t)
        ext = ext_m.group(1) if ext_m else None
        import file_ops
        res = file_ops.open_recent_file(folder=folder, extension=ext)
        _post_action(ctx, "open_recent_file", res.success, args={"file": res.path, "folder": folder}, text=text)
        if res.success:
            ctx_res.record_turn(user_utterance=text, resolved_action="open_recent_file", arguments={"file": res.path})
        return res.message

    # --- site search: "search X on youtube", "youtube search X", "look up X on reddit" ---
    # Must run before the generic open/search handlers, which would Google the whole phrase.
    _site = parse_site_search(clean_t)
    if _site:
        _s_name, _s_query = _site
        _ok, _msg = open_site_search(_s_name, _s_query)
        _post_action(ctx, "site_search", _ok, args={"site": _s_name, "query": _s_query}, text=text)
        return _msg

    # --- apps and web ---
    m = re.match(r"(?:open|launch|start|run|bring up|go to)\s+(?:up\s+)?(?:the\s+)?(.+)", clean_t)
    if m:
        from decision_engine import strip_fillers
        name = strip_fillers(m.group(1).strip())
        if name in SITES:
            webbrowser.open(SITES[name])
            _post_action(ctx, "open_site", True, args={"site": name}, text=text)
            return f"Opening {name}."
        elif name in APPS:
            _launch(APPS[name])
            _post_action(ctx, "open_app", True, args={"app": name}, text=text)
            return f"Opening {name}."
        else:
            folder_query = re.sub(r"\s+(?:folder|directory)$", "", name).strip()
            folder_query = re.sub(r"^(?:my|the)\s+", "", folder_query).strip()
            import file_ops
            aliases = file_ops.get_file_ops_adapter()._get_alias_map()
            if folder_query in aliases or "folder" in clean_t or "directory" in clean_t:
                res = file_ops.open_folder(folder_query)
                if res.success:
                    _post_action(ctx, "open_folder", True, args={"folder": folder_query, "path": res.path}, text=text)
                    ctx_res.record_turn(user_utterance=text, resolved_action="open_folder", arguments={"folder": folder_query, "path": res.path})
                    return res.message
    m = re.match(r"(?:search(?: for)?|google|look up)\s+(.+)", clean_t)
    if m:
        _q = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", m.group(1)).split())
        open_web_url(web_search_url(_q))
        _post_action(ctx, "search", True, args={"query": m.group(1).strip()}, text=text)
        return f"Searching for {m.group(1).strip()}."

    # --- media / volume / display (Phase 5) ---
    # Brightness first: "turn up the brightness" must not hit the volume "turn up" rule.
    bri = parse_brightness_intent(clean_t)
    if bri:
        import system_control
        b_action, b_args = bri
        ok, msg = getattr(system_control, b_action)(**b_args)
        _post_action(ctx, b_action, ok, args=b_args, text=text)
        return msg

    m_vol = re.search(r"\b(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})(?:\s*%)?\b", clean_t)
    if m_vol and not _any(clean_t, "volume up", "volume down", "turn up", "turn down"):
        lvl = int(m_vol.group(1))
        import system_control
        ok, msg = system_control.set_volume(lvl)
        _post_action(ctx, "set_volume", ok, args={"level": lvl}, text=text)
        ctx_res.record_turn(user_utterance=text, resolved_action="set_volume", arguments={"level": lvl})
        return f"Volume set to {lvl} percent, sir."

    if _any(clean_t, "volume up", "turn up", "turn the volume up", "turn volume up", "increase volume", "raise volume", "louder"):
        try:
            import system_control
            system_control.volume_up(5)
        except Exception:
            pass
        win_input.press("volumeup", presses=5)
        _post_action(ctx, "volume_up", True, args={"steps": 5}, text=text)
        return "Volume up."

    if _any(clean_t, "volume down", "turn down", "turn the volume down", "turn volume down", "decrease volume", "lower the volume", "lower volume", "quieter"):
        try:
            import system_control
            system_control.volume_down(5)
        except Exception:
            pass
        win_input.press("volumedown", presses=5)
        _post_action(ctx, "volume_down", True, args={"steps": 5}, text=text)
        return "Volume down."

    if re.search(r"\b(mute|unmute)\b", clean_t):
        # Explicit state, never the volumemute key (it toggles). "toggle mute" toggles.
        import system_control
        if re.search(r"\btoggle\b", clean_t):
            _ok, _msg = system_control.mute(True)
            _post_action(ctx, "mute", _ok, args={"mode": "toggle"}, text=text)
            return _msg
        if re.search(r"\bunmute\b", clean_t):
            if system_control.get_system_control().audio.is_muted():
                system_control.mute(True)  # currently muted: one toggle -> unmuted
            _post_action(ctx, "mute", True, args={"mode": "unmute"}, text=text)
            return "Unmuted."
        system_control.mute(False)  # toggle=False -> force muted
        _post_action(ctx, "mute", True, args={"mode": "mute"}, text=text)
        return "Muted."

    # Brightness phrasings outside parse_brightness_intent's vocabulary, and Night Light
    m_bri = re.search(r"\b(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})(?:\s*%)?\b", clean_t)
    if m_bri and not _any(clean_t, "brightness up", "brightness down", "brighter", "dimmer"):
        lvl = int(m_bri.group(1))
        import system_control
        ok, msg = system_control.set_brightness(lvl)
        _post_action(ctx, "set_brightness", ok, args={"level": lvl}, text=text)
        return msg

    if _any(clean_t, "brightness up", "turn brightness up", "increase brightness", "raise brightness", "brighter screen", "make screen brighter"):
        import system_control
        ok, msg = system_control.brightness_up(10)
        _post_action(ctx, "brightness_up", ok, args={"steps": 10}, text=text)
        return msg

    if _any(clean_t, "brightness down", "turn brightness down", "decrease brightness", "lower brightness", "dim screen", "dim the screen", "make screen dimmer"):
        import system_control
        ok, msg = system_control.brightness_down(10)
        _post_action(ctx, "brightness_down", ok, args={"steps": 10}, text=text)
        return msg

    if _any(clean_t, "toggle night light", "turn on night light", "turn off night light", "night light"):
        import system_control
        ok, msg = system_control.toggle_night_light()
        _post_action(ctx, "toggle_night_light", ok, text=text)
        return msg
    # 1. Dedicated Music Search & Playback (Spotify / YouTube)
    m_spot = re.match(r"(?:play|put on|listen to|search for)\s+(.+?)\s+(?:on|in)\s+spotify\b", clean_t)
    if not m_spot:
        m_spot = re.match(r"(?:play|put on|listen to)\s+spotify\s+(.+)", clean_t)
    if not m_spot:
        m_spot = re.match(r"^spotify\s+(?:play\s+)?(.+)", clean_t)
    if m_spot:
        query = m_spot.group(1).strip()
        ok, reply = play_media(query, platform="spotify")
        _post_action(ctx, "play_music", ok)
        return reply

    m_yt = re.match(r"(?:play|put on|listen to|search for)\s+(.+?)\s+(?:on|in)\s+youtube\b", clean_t)
    if not m_yt:
        m_yt = re.match(r"(?:play|put on|listen to)\s+youtube\s+(.+)", clean_t)
    if not m_yt:
        m_yt = re.match(r"^youtube\s+(?:play\s+)?(.+)", clean_t)
    if m_yt:
        query = m_yt.group(1).strip()
        ok, reply = play_media(query, platform="youtube")
        _post_action(ctx, "play_music", ok)
        return reply

    # 2. General "play <query>" (e.g. "play Drake", "play some Drake", "play Bohemian Rhapsody")
    m_play = re.match(r"(?:play|put on|listen to)\s+(?:some\s+)?(.+)", clean_t)
    if m_play:
        target = m_play.group(1).strip()
        if target not in ("music", "the music", "track", "song", "audio", "video", "media", "pause", "resume", "playback"):
            ok, reply = play_media(target, platform="auto")
            _post_action(ctx, "play_music", ok)
            return reply

    # 3. What's Playing / Current Track Query
    if _any(clean_t, "what's playing", "what song is this", "what is playing", "currently playing", "current track", "now playing", "who is singing"):
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
            _post_action(ctx, "now_playing", True)
            reply_artist = f" by {media.artist}" if media.artist else ""
            return f"Currently playing '{media.title}'{reply_artist}, sir."
        else:
            _post_action(ctx, "now_playing", False)
            return "No media is currently active or playing, sir."

    # 4. Simple Play / Pause toggle and track skipping: adapter only (it owns the key fallback + debounce)
    if clean_t in ("play", "pause", "resume", "unpause", "play music", "pause music", "stop music", "resume music", "toggle playback", "toggle play", "toggle music", "stop playback"):
        _media_call("play_pause")
        _post_action(ctx, "play_pause", True)
        return "Done."
    if _any(clean_t, "next track", "next song", "skip", "play next song"):
        _media_call("next_track")
        _post_action(ctx, "next_track", True)
        return "Skipping."
    if _any(clean_t, "previous track", "previous song", "go back a song", "replay track"):
        _media_call("previous_track")
        _post_action(ctx, "previous_track", True)
        return "Going back."

    # 5. Show / Hide Dynamic Island Widgets
    if _any(clean_t, "show media", "show music", "expand media", "expand music", "show the media widget"):
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("expand_widget", widget="media")
            except Exception:
                pass
        _post_action(ctx, "show_media", True)
        return "Displaying media controls, sir."

    if _any(clean_t, "show weather", "expand weather", "show the weather widget", "show weather card"):
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("expand_widget", widget="weather")
            except Exception:
                pass
        _post_action(ctx, "show_weather", True)
        return "Displaying weather forecast, sir."

    if _any(clean_t, "hide the island widgets", "hide island widgets", "hide island", "hide widgets", "toggle island widgets", "dismiss widgets"):
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("toggle_widgets")
            except Exception:
                pass
        _post_action(ctx, "toggle_widgets", True)
        return "Toggling Dynamic Island widgets, sir."

    # --- windows & power (Phase 5) ---
    if _any(clean_t, "snap left", "snap window left", "snap to the left", "snap window to the left"):
        import system_control
        ok, msg = system_control.snap_window("left")
        _post_action(ctx, "snap_window", ok, args={"direction": "left"}, text=text)
        return "Window snapped left, sir."

    if _any(clean_t, "snap right", "snap window right", "snap to the right", "snap window to the right"):
        import system_control
        ok, msg = system_control.snap_window("right")
        _post_action(ctx, "snap_window", ok, args={"direction": "right"}, text=text)
        return "Window snapped right, sir."

    if _any(clean_t, "snap up", "maximize window", "maximize active window", "maximize this window"):
        import system_control
        ok, msg = system_control.snap_window("up")
        _post_action(ctx, "maximize_window", ok, text=text)
        return "Window maximized, sir."

    if _any(clean_t, "snap down", "restore window", "restore this window"):
        import system_control
        ok, msg = system_control.snap_window("down")
        _post_action(ctx, "snap_window", ok, args={"direction": "down"}, text=text)
        return "Window restored, sir."



    m_sw = re.match(r"^(?:switch|switch app|switch to)\s+(?:app\s+)?(.+)", clean_t)
    if m_sw:
        target_app = m_sw.group(1).strip()
        if target_app not in ("window", "next window", "other window", "tab"):
            import system_control
            ok, msg = system_control.switch_to_app(target_app)
            _post_action(ctx, "switch_to_app", ok, args={"app": target_app}, text=text)
            return f"Switched to {target_app}, sir."

    if _any(clean_t, "move to other monitor", "move window to other monitor", "move to next monitor", "other monitor", "other screen", "next monitor"):
        import system_control
        ok, msg = system_control.move_to_other_monitor()
        _post_action(ctx, "move_to_other_monitor", ok, text=text)
        return "Window moved to other monitor, sir."

    # Phase 8 Window Under Cursor Actions
    if clean_t in ("open this", "open that", "open link under cursor", "open file under cursor", "open link", "open this file", "click this"):
        import window_resolver
        ok, msg = window_resolver.open_this()
        _post_action(ctx, "open_this", ok, text=text)
        return msg

    if clean_t in ("copy this", "copy that", "copy selection", "copy this text", "copy text under cursor", "copy under cursor"):
        import window_resolver
        ok, msg = window_resolver.copy_this()
        _post_action(ctx, "copy_this", ok, text=text)
        return msg

    if clean_t in ("close this", "close window under cursor", "kill this window"):
        import window_resolver
        if "confirm" in clean_t or "force" in clean_t:
            _PENDING_CONFIRMATION = None
            ok, msg = window_resolver.close_this(confirm=True)
            _post_action(ctx, "close_this", ok, text=text)
            return msg
        ok, msg = window_resolver.close_this(confirm=False)
        if not ok and "unsaved" in msg.lower():
            _PENDING_CONFIRMATION = "close_this"
            return msg
        _post_action(ctx, "close_this", ok, text=text)
        return msg

    if _any(clean_t, "close window", "close this window", "close active window"):
        import system_control
        if "confirm" in clean_t or "force" in clean_t:
            _PENDING_CONFIRMATION = None
            ok, msg = system_control.close_window(confirm=True)
            _post_action(ctx, "close_window", ok, text=text)
            return "Window closed, sir."
        ok, msg = system_control.close_window(confirm=False)
        if not ok:
            _PENDING_CONFIRMATION = "close_window"
            return msg
        _post_action(ctx, "close_window", True, text=text)
        return "Window closed, sir."

    if _any(clean_t, "lock pc", "lock computer", "lock workstation", "lock the pc", "lock the computer"):
        import system_control
        ok, msg = system_control.lock_workstation()
        _post_action(ctx, "lock_workstation", ok, text=text)
        return "Workstation locked securely, sir."

    if re.search(r"\b(screenshot|take screenshot|screen capture)\b", t):
        import system_control
        ok, msg = system_control.take_screenshot()
        _post_action(ctx, "screenshot", ok, text=text)
        return "Screenshot saved to Pictures, Jarvis."

    if _any(clean_t, "toggle wifi", "turn on wifi", "turn off wifi", "enable wifi", "disable wifi"):
        import system_control
        ok, msg = system_control.toggle_wifi()
        _post_action(ctx, "toggle_wifi", ok, text=text)
        return msg

    if _any(clean_t, "toggle bluetooth", "turn on bluetooth", "turn off bluetooth", "enable bluetooth", "disable bluetooth"):
        import system_control
        ok, msg = system_control.toggle_bluetooth()
        _post_action(ctx, "toggle_bluetooth", ok, text=text)
        return msg

    if _any(clean_t, "toggle do not disturb", "turn on do not disturb", "turn off do not disturb", "do not disturb", "enable focus assist", "disable focus assist"):
        import system_control
        ok, msg = system_control.toggle_do_not_disturb()
        _post_action(ctx, "toggle_do_not_disturb", ok, text=text)
        return msg

    if _any(t, "show desktop", "minimize everything", "minimise everything", "minimize all", "minimize all windows"):
        try:
            import system_control
            system_control.minimize_all()
        except Exception:
            pass
        win_input.hotkey("win", "d")
        _post_action(ctx, "show_desktop", True)
        return "Desktop."
    if re.search(r"\b(minimize|minimise)\b", t):
        win_input.hotkey("win", "down")
        _post_action(ctx, "minimize_window", True)
        return "Minimised."
    if re.search(r"\b(maximize|maximise)\b", t):
        win_input.hotkey("win", "up")
        _post_action(ctx, "maximize_window", True)
        return "Maximised."
    if _any(t, "switch window", "next window"):
        win_input.hotkey("alt", "tab")
        _post_action(ctx, "switch_window", True)
        return "Switching."
    if "scroll down" in t:
        win_input.scroll(-600)
        _post_action(ctx, "scroll", True)
        return None
    if "scroll up" in t:
        win_input.scroll(600)
        _post_action(ctx, "scroll", True)
        return None

    # --- software updates ---
    if _any(t, "check for updates", "update jarvis", "check updates", "any updates"):
        if hasattr(ctx, "updater") and ctx.updater:
            ctx.updater.check_async(notify_if_latest=True)
            _post_action(ctx, "check_updates", True)
            return "Checking GitHub for updates, sir."
        _post_action(ctx, "check_updates", False)
        return "Updater is not initialized, sir."
    if _any(t, "install update", "apply update", "restart and update"):
        if hasattr(ctx, "updater") and ctx.updater and ctx.updater.downloaded_path:
            ctx.updater.apply_update_and_restart()
            _post_action(ctx, "install_update", True)
            return "Applying update and restarting Jarvis now, sir."
        _post_action(ctx, "install_update", False)
        return "No downloaded update is ready to install, sir."

    # Visual queries immediately dispatch to camera vision
    if is_visual_query(text):
        _jl.utterance_event(_jl.DECISION, tier="vision", intent="visual_query")
        return _handle_ai_query(text, ctx)

    # --- local decision engine (System 1 Reflex: ~1-15ms) ---
    cfg = None
    if ctx:
        if hasattr(ctx, "engine") and hasattr(ctx.engine, "cfg"):
            cfg = ctx.engine.cfg
        elif hasattr(ctx, "cfg"):
            cfg = ctx.cfg

    if cfg is None or getattr(cfg, "local_decision_engine", True):
        try:
            from decision_engine import evaluate_decision
            decision_reply = evaluate_decision(text, ctx)
            if decision_reply:
                _jl.utterance_event(_jl.DECISION, tier="system1", intent="decision_engine")
                return decision_reply
        except Exception as exc:
            print(f"[commands] decision engine note: {exc}")

    # Check context resolver before fallback (System 1 context memory)
    try:
        from context_resolver import get_context_resolver
        ctx_res = get_context_resolver()
        ref_res = ctx_res.resolve_referential(clean_t)
        if ref_res:
            reply = _execute_resolution(ref_res, ctx)
            if reply:
                ctx_res.record_turn(
                    user_utterance=text,
                    resolved_action=ref_res.action,
                    arguments=ref_res.args,
                    candidates=ref_res.candidates,
                )
                return reply
    except Exception as exc:
        print(f"[commands] context resolver check note: {exc}")

    # --- local-first answers (Wikipedia for simple "who is / define" lookups) before any Groq call ---
    local_reply = _local_answer_first(text, clean_t, ctx)
    if local_reply:
        return local_reply

    # --- fallback: Groq AI query if enabled (System 2 Deep Reasoning) ---
    return _handle_ai_query(text, ctx)


# "who is X", "tell me about X", "define X", "what is a/an X": encyclopedia lookups Wikipedia answers
# for free. Anything that is not a self-contained topic (pronouns, time words, "my") goes to the LLM.
_RE_LOCAL_LOOKUP = re.compile(r"^(?:who (?:is|was)|tell me about|define|what (?:is|are) (?:an?|the meaning of))\s+(.+)$")


def _local_answer_first(text: str, clean_t: str, ctx: Any) -> Optional[str]:
    """Local answers tried before Groq: the answer cache, then Wikipedia for plain lookups.
    Math, units, time/date, world time and 'wiki X' are already handled earlier in _run_single_command."""
    if is_visual_query(text):
        return None
    try:
        cached = answer_cache_get(clean_t)
    except Exception:
        cached = None
    if cached:
        _jl.utterance_event(_jl.DECISION, tier="answer_cache", intent="conversation")
        try:
            get_conversation_manager().add_turn(text, cached)
        except Exception:
            pass
        return cached
    m = _RE_LOCAL_LOOKUP.match(clean_t.strip(" ,.!?"))
    if not m:
        return None
    from ai import is_cacheable_question
    topic = m.group(1).strip()
    if len(topic.split()) > 6 or not is_cacheable_question(clean_t):
        return None
    try:
        import quick_answers
        summary = quick_answers.get_wikipedia_client().get_summary(topic)
    except Exception:
        summary = None
    if not summary:
        return None
    _jl.utterance_event(_jl.DECISION, tier="wiki_first", intent="quick_answer")
    _post_action(ctx, "wiki", True, text=text)
    return f"According to Wikipedia, {summary}"


def is_visual_query(text: str) -> bool:
    """Detects if the user's query asks to identify or inspect something in front of the camera."""
    t = text.lower().strip()
    visual_patterns = [
        r"\bwhat(?:'s| is) this\b",
        r"\bwhat(?:'s| is) that\b",
        r"\bwhat am i holding\b",
        r"\bwhat(?:'s| is) in my hand\b",
        r"\bwhat am i showing\b",
        r"\blook at this\b",
        r"\blook here\b",
        r"\bcan you see this\b",
        r"\bdo you see this\b",
        r"\bwhat do you see\b",
        r"\bdescribe what you see\b",
        r"\bwhat(?:'s| is) in front of (?:the|your) camera\b",
        r"\bidentify this\b",
        r"\btell me what this is\b",
        r"\bwhat object is this\b",
        r"\bwhat does this say\b",
        r"\bread this\b",
        r"\binspect this\b",
    ]
    return any(re.search(pat, t) for pat in visual_patterns)


def _handle_ai_query(user_text: str, ctx: Any) -> str:
    """Dispatches unhandled user utterances to the Groq LLM or Vision endpoint if enabled."""
    from quick_answers import is_unsafe_shell_command
    if is_unsafe_shell_command(user_text):
        return "I can't do that, sir."

    import screen_awareness
    if screen_awareness.is_screen_query(user_text):
        return screen_awareness.analyze_screen(user_text, ctx)

    cfg = None
    if ctx:
        if hasattr(ctx, "engine") and hasattr(ctx.engine, "cfg"):
            cfg = ctx.engine.cfg
        elif hasattr(ctx, "cfg"):
            cfg = ctx.cfg

    ai_enabled = getattr(cfg, "ai_enabled", False) if cfg else False
    api_key = get_api_key()

    if not ai_enabled or not api_key:
        if is_visual_query(user_text):
            # Camera frame must never be fetched or uploaded without the AI switch and a key.
            return screen_awareness.vision_blocked_reply(ctx, "Visual analysis")
        return "I didn't catch a command for that, sir."

    timeout = min(max(int(getattr(cfg, "ai_timeout", 8)), 3), 15) if cfg else 8

    # Notify overlay of thinking state with watchdog timeout
    if ctx and hasattr(ctx, "overlay") and ctx.overlay:
        try:
            ctx.overlay.post("thinking", active=True)
        except Exception:
            pass

    try:
        # Visual scene query inspection
        if is_visual_query(user_text) and getattr(cfg, "ai_vision_enabled", True):
            blocked = screen_awareness.vision_blocked_reply(ctx, "Visual analysis")
            if blocked:
                return blocked
            frame = _camera_frame(ctx)
            if frame is not None:
                try:
                    vision_model = getattr(cfg, "ai_vision_model", DEFAULT_VISION_MODEL) if cfg else DEFAULT_VISION_MODEL
                    # Enforce strict 6-8s timeout on vision queries so user is never stalled
                    vision_timeout = min(timeout, 8)
                    client = GroqClient(api_key=api_key, timeout=vision_timeout)
                    reply = client.describe_image(frame, prompt=user_text, model=vision_model)
                    conv_mgr = get_conversation_manager()
                    conv_mgr.add_turn(user_text, reply)
                    return reply
                except AuthenticationError:
                    if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                        try:
                            ctx.overlay.post("ai_error", text="Invalid Groq Key")
                        except Exception:
                            pass
                    return "My Groq API key appears to be invalid, sir. Please check your settings."
                except RateLimitError as rl_exc:
                    if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                        try:
                            ctx.overlay.post("ai_error", text="Groq Rate Limited")
                        except Exception:
                            pass
                    return rate_limit_reply(rl_exc)
                except (OfflineError, TimeoutError):
                    if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                        try:
                            ctx.overlay.post("ai_error", text="Vision Timed Out")
                        except Exception:
                            pass
                    return "Visual analysis timed out, sir. Please try again."
                except Exception as exc:
                    print(f"[ai] vision query exception: {exc}")
                    return "I was unable to analyze the camera feed, sir."
            else:
                return "My optical sensor is currently unavailable, sir. I cannot view what you are holding."

        # Text LLM query
        model = getattr(cfg, "ai_llm_model", DEFAULT_LLM_MODEL) if cfg else DEFAULT_LLM_MODEL
        max_history = getattr(cfg, "ai_max_history", 6) if cfg else 6

        client = GroqClient(api_key=api_key, timeout=timeout)
        conv_mgr = get_conversation_manager()
        conv_mgr.max_turns = max_history

        # Same self-contained question asked before: answer locally, no Groq request.
        cached = answer_cache_get(user_text)
        if cached:
            _jl.utterance_event(_jl.DECISION, tier="answer_cache", intent="conversation")
            conv_mgr.add_turn(user_text, cached)
            return cached

        system_prompt = build_system_prompt(ctx)
        messages = conv_mgr.get_messages(system_prompt, user_text)

        raw_reply = stream_ai_query(
            client=client,
            messages=messages,
            model=model,
            temperature=0.5,
            ctx=ctx,
        )
        action, args, reply = parse_ai_response(raw_reply)

        if action == "power_mode" and not power_action_allowed(args, user_text):
            _jl.utterance_event(_jl.DECISION, tier="system2", action="power_mode(blocked)")
            action = "none"
        else:
            _jl.utterance_event(_jl.DECISION, tier="system2", action=action or "none")
        if action and action != "none":
            executed, act_msg = execute_whitelisted_action(action, args, ctx)
            if not executed:
                print(f"[ai] Action '{action}' note: {act_msg}")
            if action in ("set_brightness", "brightness_up", "brightness_down", "get_brightness") and act_msg:
                # The model wrote its reply before the action ran ("full maximum, sir"); speak what the
                # display actually did instead, success or failure.
                reply = act_msg
        elif reply:
            answer_cache_put(user_text, reply)   # pure Q&A only; refuses time/personal/follow-up questions

        conv_mgr.add_turn(user_text, reply)
        return reply

    except AuthenticationError:
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("ai_error", text="Invalid Groq Key")
            except Exception:
                pass
        return "My Groq API key appears to be invalid, sir. Please check your settings."
    except RateLimitError as rl_exc:
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("ai_error", text="Groq Rate Limited")
            except Exception:
                pass
        return rate_limit_reply(rl_exc)
    except (OfflineError, TimeoutError) as exc:
        err_msg = "Request Timed Out" if isinstance(exc, TimeoutError) else "Groq Offline"
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("ai_error", text=err_msg)
            except Exception:
                pass
        return "I am unable to reach the AI service right now, sir."
    except Exception as exc:
        print(f"[ai] query exception: {exc}")
        return "I didn't catch a command for that, sir."
    finally:
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("thinking", active=False)
            except Exception:
                pass


# Alias for compatibility
handle_command = run_command

