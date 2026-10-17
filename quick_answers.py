"""
Quick Answers & Time Zones module for J.A.R.V.I.S.
Provides:
- Ultra-fast (<1 ms, zero API cost) local world time zone resolution via standard library zoneinfo/datetime (System 1).
- Factual quick lookups via Wikipedia REST API summary with fast Groq completion fallback.
- Safety gate intercepting unsafe or arbitrary shell/PowerShell requests with "I can't do that, sir."
- 100% unit-testable architecture behind interfaces with mocks.
Strictly NO Qt imports.
"""

import datetime
import json
import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple

try:
    import zoneinfo
    ZONEINFO_AVAILABLE = True
except ImportError:
    ZONEINFO_AVAILABLE = False

import requests


# =====================================================================
# 1. Safety Gate for Arbitrary Shell / PowerShell Requests
# =====================================================================

UNSAFE_SHELL_PATTERNS = [
    # Explicit commands to run shell/PowerShell/cmd/bash
    r"\b(?:run|execute|launch|invoke|call|eval)\s+(?:powershell|cmd|command\s+prompt|shell|bash|sh|terminal\s+command|script)\b",
    r"\bpowershell(?:\.exe)?\s+(?:-[a-zA-Z]+)",
    r"\bcmd(?:\.exe)?\s+(?:/[a-zA-Z]+)",
    r"\bbash\s+-[a-zA-Z]+",
    r"\bsh\s+-[a-zA-Z]+",
    # Arbitrary code/script execution constructs
    r"\b(?:invoke-expression|iex|downloadstring|downloadfile)\b",
    r"\bcurl\s+.*\|\s*(?:sh|bash|powershell|cmd)\b",
    r"\bwget\s+.*\|\s*(?:sh|bash|powershell|cmd)\b",
    # Dangerous system destruction or file deletion commands
    r"\b(?:rmdir|del)\s+/[s|q]\b",
    r"\bdel\s+(?:/f|/q|\*.*)\b",
    r"\bformat\s+[a-zA-Z]:",
    r"\bformat\s+c:\b",
    r"\b(?:diskpart|mkfs|dd\s+if=)\b",
    r"\breg\s+(?:delete|add)\b",
    r"\brm\s+-(?:r|f|rf|fr)\b",
    r"\bdelete\s+system32\b",
    r"\bdrop\s+(?:database|table)\b",
    r"\b(?:kill\s+-9|taskkill\s+/f)\b",
]


def is_unsafe_shell_command(text: str) -> bool:
    """
    Evaluates whether the user's speech requests arbitrary, raw, or destructive shell/PowerShell execution.
    Safe actions (e.g. 'open terminal', 'open cmd', 'launch command prompt') opening the user GUI application
    are permitted and are not flagged as unsafe execution.
    """
    if not text:
        return False
    t = text.lower().strip()

    # Allow safe GUI launch requests
    if re.search(r"^(?:open|launch|start|bring\s+up)\s+(?:terminal|command\s+prompt|cmd)$", t):
        return False

    return any(re.search(pat, t) for pat in UNSAFE_SHELL_PATTERNS)


# =====================================================================
# 2. Local Time Zone Lookups (System 1 Reflex: <1 ms)
# =====================================================================

# City / Region name -> (IANA Timezone Name, Standard UTC offset in hours)
CITY_TIMEZONE_MAP: Dict[str, Tuple[str, float]] = {
    # Key cities explicitly required by specification
    "tokyo": ("Asia/Tokyo", 9.0),
    "london": ("Europe/London", 0.0),
    "paris": ("Europe/Paris", 1.0),
    "new york": ("America/New_York", -5.0),
    "new york city": ("America/New_York", -5.0),
    "nyc": ("America/New_York", -5.0),
    "sydney": ("Australia/Sydney", 10.0),
    "dubai": ("Asia/Dubai", 4.0),

    # Additional major world cities and regions
    "los angeles": ("America/Los_Angeles", -8.0),
    "la": ("America/Los_Angeles", -8.0),
    "san francisco": ("America/Los_Angeles", -8.0),
    "sf": ("America/Los_Angeles", -8.0),
    "seattle": ("America/Los_Angeles", -8.0),
    "chicago": ("America/Chicago", -6.0),
    "toronto": ("America/Toronto", -5.0),
    "vancouver": ("America/Vancouver", -8.0),
    "berlin": ("Europe/Berlin", 1.0),
    "rome": ("Europe/Rome", 1.0),
    "madrid": ("Europe/Madrid", 1.0),
    "amsterdam": ("Europe/Amsterdam", 1.0),
    "beijing": ("Asia/Shanghai", 8.0),
    "shanghai": ("Asia/Shanghai", 8.0),
    "hong kong": ("Asia/Hong_Kong", 8.0),
    "singapore": ("Asia/Singapore", 8.0),
    "seoul": ("Asia/Seoul", 9.0),
    "mumbai": ("Asia/Kolkata", 5.5),
    "delhi": ("Asia/Kolkata", 5.5),
    "new delhi": ("Asia/Kolkata", 5.5),
    "bangalore": ("Asia/Kolkata", 5.5),
    "india": ("Asia/Kolkata", 5.5),
    "moscow": ("Europe/Moscow", 3.0),
    "cairo": ("Africa/Cairo", 2.0),
    "sao paulo": ("America/Sao_Paulo", -3.0),
    "auckland": ("Pacific/Auckland", 12.0),
    "new zealand": ("Pacific/Auckland", 12.0),
    "honolulu": ("Pacific/Honolulu", -10.0),
    "hawaii": ("Pacific/Honolulu", -10.0),
    "utc": ("UTC", 0.0),
    "gmt": ("GMT", 0.0),
}


def is_timezone_query(text: str) -> bool:
    """Detects if the user is asking for the current time in a specific city, country, or timezone."""
    if not text:
        return False
    t = text.lower().strip(" ,.!?")
    # Must have a time-related keyword and a location preposition or phrase
    if not any(w in t for w in ("time", "clock", "hour")):
        return False

    time_patterns = [
        r"\b(?:what(?:'s| is) the time|what time is it|current time|tell me the time|time)\s+(?:in|for|at|of)\s+([a-zA-Z\s]+)",
        r"^time\s+(?:in|for|at)\s+([a-zA-Z\s]+)",
        r"^what time is it in\s+([a-zA-Z\s]+)",
        r"^what is the time in\s+([a-zA-Z\s]+)",
        r"^what's the time in\s+([a-zA-Z\s]+)",
    ]
    for pat in time_patterns:
        m = re.search(pat, t)
        if m:
            loc = m.group(1).strip()
            if loc in CITY_TIMEZONE_MAP or any(city in loc for city in CITY_TIMEZONE_MAP):
                return True

    # Check direct phrases like "Tokyo time" or "time Tokyo"
    for city in CITY_TIMEZONE_MAP:
        if f"time in {city}" in t or f"time for {city}" in t or f"{city} time" in t:
            return True

    return False


def extract_timezone_location(text: str) -> Optional[Tuple[str, str, float]]:
    """
    Extracts matched location details from query.
    Returns (display_name, iana_tz_name, fallback_offset_hours) or None.
    """
    clean = text.lower().strip(" ,.!?")
    # Check for direct city mentions sorted by length descending (e.g. "new york city" before "new york")
    for city, (iana, offset) in sorted(CITY_TIMEZONE_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if re.search(rf"\b{re.escape(city)}\b", clean):
            return city.title(), iana, offset

    # Try regex extraction
    m = re.search(r"(?:in|for|at|of)\s+([a-zA-Z\s]+)$", clean)
    if m:
        candidate = m.group(1).strip()
        if candidate in CITY_TIMEZONE_MAP:
            iana, offset = CITY_TIMEZONE_MAP[candidate]
            return candidate.title(), iana, offset

    return None


def get_world_time(query_or_location: str, ctx: Any = None) -> Optional[str]:
    """
    Resolves the current time for a world city or region in <1 ms via zoneinfo/datetime.
    Updates the overlay card if an overlay instance is available in ctx.
    Returns spoken response string or None if location not recognized.
    """
    loc_match = extract_timezone_location(query_or_location)
    if not loc_match:
        return None

    display_name, iana_tz, fallback_offset = loc_match

    tz_obj = None
    if ZONEINFO_AVAILABLE:
        try:
            tz_obj = zoneinfo.ZoneInfo(iana_tz)
        except Exception:
            tz_obj = None

    if tz_obj is None:
        # Fallback using fixed UTC offset timedelta (ensures 100% offline stability on bare Windows systems)
        tz_obj = datetime.timezone(datetime.timedelta(hours=fallback_offset))

    now_tz = datetime.datetime.now(tz_obj)
    time_str = now_tz.strftime("%I:%M %p").lstrip("0")

    # Update Dynamic Island / Overlay if present
    overlay = getattr(ctx, "overlay", None) if ctx else None
    if overlay and hasattr(overlay, "post"):
        try:
            overlay.post("result", text=f"{display_name}: {time_str}", icon="time", duration=3.0)
        except Exception:
            pass

    return f"It is {time_str} in {display_name}, sir."


# =====================================================================
# 3. Factual Quick Lookups (Wikipedia REST API + Fast Groq Completion)
# =====================================================================

WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
WIKI_USER_AGENT = "JARVIS-Assistant/1.1 (voice-assistant-desktop; https://github.com/)"


class WikipediaClient:
    """Lightweight REST client for Wikipedia's official page summary endpoint."""

    def __init__(self, timeout: float = 2.5):
        self.timeout = timeout

    def get_summary(self, query: str) -> Optional[str]:
        clean_q = query.strip()
        if not clean_q:
            return None

        clean_slug = urllib.parse.quote(clean_q.replace(" ", "_"))
        url = WIKI_SUMMARY_URL.format(clean_slug)
        headers = {"User-Agent": WIKI_USER_AGENT, "Accept": "application/json"}

        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                extract = data.get("extract", "").strip()
                if extract:
                    # Clean up and extract first 1-2 sentences for crisp vocal presentation
                    sentences = re.split(r"(?<=[.!?])\s+", extract)
                    summary = " ".join(sentences[:2]) if len(sentences) >= 2 else extract
                    return summary
            return None
        except Exception as exc:
            print(f"[quick_answers] Wikipedia error: {exc}")
            return None


# Global Wikipedia client (mockable for unit tests)
_WIKI_CLIENT: WikipediaClient = WikipediaClient()


def get_wikipedia_client() -> WikipediaClient:
    global _WIKI_CLIENT
    return _WIKI_CLIENT


def set_wikipedia_client(client: WikipediaClient) -> None:
    global _WIKI_CLIENT
    _WIKI_CLIENT = client


def is_factual_query(text: str) -> bool:
    """Detects if user is asking a factual question suitable for Wikipedia or quick lookup."""
    if not text:
        return False
    t = text.lower().strip(" ,.!?")
    patterns = [
        r"^who (?:was|is)\s+(.+)",
        r"^what (?:is|was|are)\s+(.+)",
        r"^tell me about\s+(.+)",
        r"^define\s+(.+)",
        r"^summarize\s+(.+)",
        r"^(?:wiki|wikipedia)\s+(.+)",
    ]
    return any(re.search(p, t) for p in patterns)


def extract_factual_topic(text: str) -> Optional[str]:
    """Extracts search term / topic from a factual question."""
    t = text.lower().strip(" ,.!?")
    patterns = [
        r"^who (?:was|is)\s+(.+)",
        r"^what (?:is|was|are)\s+(.+)",
        r"^tell me about\s+(.+)",
        r"^define\s+(.+)",
        r"^summarize\s+(.+)",
        r"^(?:wiki|wikipedia)\s+(.+)",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            topic = m.group(1).strip()
            # Clean common trailing punctuation
            return re.sub(r"[?.!]$", "", topic).strip()
    return None


def get_quick_answer(query: str, ctx: Any = None) -> Optional[str]:
    """
    Evaluates factual queries:
    1. Checks if it's a world time zone query (<1 ms, System 1).
    2. Queries Wikipedia REST API summary.
    3. If Wikipedia misses or is unavailable, falls back to fast Groq chat completion.
    """
    # 1. World Time Check
    if is_timezone_query(query):
        return get_world_time(query, ctx)

    topic = extract_factual_topic(query)
    search_term = topic if topic else query

    # 2. Wikipedia REST API Summary Lookup
    wiki = get_wikipedia_client()
    summary = wiki.get_summary(search_term)
    if summary:
        overlay = getattr(ctx, "overlay", None) if ctx else None
        if overlay and hasattr(overlay, "post"):
            try:
                overlay.post("result", text=f"Wiki: {search_term.title()}", icon="search", duration=3.5)
            except Exception:
                pass
        return f"According to Wikipedia, {summary}"

    # 3. Fast Groq LLM Completion Fallback
    try:
        from ai import get_api_key, GroqClient, DEFAULT_LLM_MODEL
        api_key = get_api_key()
        if api_key:
            client = GroqClient(api_key=api_key, timeout=5)
            prompt = (
                f"You are J.A.R.V.I.S., the witty and brilliant AI assistant from Iron Man. "
                f"Answer the user's factual question concisely in 1-2 clear, accurate sentences. "
                f"Address the user as 'sir'.\n\nQuestion: {query}"
            )
            messages = [{"role": "user", "content": prompt}]
            reply = client.chat(messages, model=DEFAULT_LLM_MODEL, temperature=0.3, json_mode=False)
            if reply:
                return reply
    except Exception as exc:
        print(f"[quick_answers] Groq fallback note: {exc}")

    return None
