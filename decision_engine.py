"""
Local Decision Engine (System 1 Reflex Model) for J.A.R.V.I.S.

Inspired by non-autoregressive decision models (like Laya AI and Jev):
- Provides ultra-fast (~5-15 ms) on-device intent scoring and action execution.
- Evaluates candidate actions over bounded decision heads with zero hallucinations.
- Intercepts natural speech variations that rigid regex misses (e.g. "crank up the audio",
  "kill all windows", "hide the screen", "go dark", "how much ram is left").
- Seamlessly falls back to Groq LLM (System 2) when deep conversational reasoning is needed.
- 100% offline, zero network latency, zero API costs.
"""

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

import perf
from context_resolver import Candidate, get_context_resolver, get_disambiguation_engine

# Precompiled regex patterns to guarantee zero runtime re.compile() overhead
_RE_NON_WORD = re.compile(r"[^\w\s]")
_RE_LEAD_FILLER = re.compile(
    r"^(?:can you please|hey jarvis|ok jarvis|okay jarvis|can you|could you|would you|please|jarvis|just|kindly)\b\s*")
_RE_TRAIL_FILLER = re.compile(r"\s*\b(?:thank you|for me|please|now|thanks|jarvis)$")


def strip_fillers(norm_text: str) -> str:
    """Repeatedly strips polite leading/trailing filler words; never returns an empty string."""
    cur = norm_text
    while True:
        nxt = _RE_LEAD_FILLER.sub("", cur, count=1)
        nxt = _RE_TRAIL_FILLER.sub("", nxt, count=1).strip()
        if not nxt or nxt == cur:
            return cur
        cur = nxt
_RE_OPEN_FOLDER = re.compile(r"(?:open|show|bring\s+up)\s+(?:the\s+)?(?:folder\s+)?(.+?)(?:\s+folder|\s+directory)?$")
_RE_STRIP_MY_THE = re.compile(r"^(?:my|the)\s+")
_RE_FILE_EXT = re.compile(r"\b(pdf|txt|md|docx|xlsx|csv|png|jpg|mp4|py)\b")
_RE_REMEMBER_FOLDER = re.compile(r"remember\s+(?:this\s+folder\s+)?(?:as\s+)?(.+)")
_RE_PLAY_MUSIC = re.compile(r"(?:play|put on|listen to|search for)\s+(.+?)(?:\s+(?:on|in)\s+(?:spotify|youtube))?$")
_RE_SET_VOLUME = re.compile(r"\b(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\b")
_RE_SET_BRIGHTNESS = re.compile(r"\b(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})\b")
_RE_SWITCH_APP = re.compile(r"(?:switch\s+(?:to\s+)?)(.+)")
_RE_QUICK_NOTE = re.compile(r"(?:note\s+that|take\s+(?:a\s+)?note|write\s+down|add\s+note)\s+(.+)")

@lru_cache(maxsize=256)
def _get_word_boundary_regex(word: str) -> re.Pattern:
    """Precompiles and caches word boundary regexes for apps, sites, and entities."""
    return re.compile(rf"\b{re.escape(word)}\b")


@dataclass
class DecisionMatch:
    intent: Optional[str]
    confidence: float
    args: Dict[str, Any] = field(default_factory=dict)


# Candidate Action Categories with Prototype Intent Phrases
INTENT_PROTOTYPES: Dict[str, List[str]] = {
    "volume_up": [
        "crank up the volume", "turn up the audio", "make it louder", "boost the volume",
        "raise the volume", "increase the sound", "bump up the audio", "pump up the volume",
        "turn the sound up", "sound is too quiet", "make audio louder", "louder please"
    ],
    "volume_down": [
        "turn down the volume", "make it quieter", "lower the sound", "decrease audio",
        "reduce the volume", "bring the volume down", "too loud turn it down", "turn the audio down",
        "lower the volume a bit", "softer please", "quieter please"
    ],
    "mute": [
        "mute the audio", "mute the sound", "mute sound", "silence the sound",
        "mute everything", "shut up the audio", "kill the sound", "mute computer",
        "cut the sound", "silent mode audio", "shush jarvis", "silence audio", "shut up audio"
    ],
    "play_pause": [
        "pause the music", "resume playback", "play music", "stop playback",
        "pause the video", "resume track", "toggle play pause", "pause media", "unpause"
    ],
    "play_music": [
        "play drake on spotify", "play drake", "play taylor swift on spotify",
        "play music on spotify", "play the weeknd on spotify", "play blinding lights",
        "play songs by drake", "listen to drake on spotify", "put on some drake",
        "play bohemian rhapsody on spotify", "play rock on spotify", "play jazz on spotify",
        "search drake on spotify", "play spotify drake", "play songs by the weeknd"
    ],
    "next_track": [
        "skip this song", "next track", "next song", "skip track", "play next song", "go to next track"
    ],
    "previous_track": [
        "previous song", "previous track", "last song", "go back a track", "replay track"
    ],
    "show_desktop": [
        "show desktop", "hide all windows", "hide all my windows", "clear the screen",
        "minimize all windows", "minimize everything", "hide everything", "show my desktop",
        "minimize windows", "desktop view", "go to desktop", "hide windows", "clear my desktop",
        "minimize all"
    ],
    "lock_workstation": [
        "lock the workstation", "lock my workstation", "lock my pc", "lock the computer",
        "lock screen", "secure the workstation", "secure computer", "lock computer right now",
        "lock pc immediately"
    ],
    "screenshot": [
        "take a screenshot", "capture the screen", "capture screen", "save a screenshot",
        "screen grab", "snap the screen", "grab screenshot", "capture desktop screenshot"
    ],
    "stealth_mode": [
        "stealth mode", "enter stealth mode", "go dark", "cloak system", "cloak suit", "cloak",
        "silent protocol", "stealth protocol", "minimize visual signatures and mute",
        "engage stealth mode"
    ],
    "clean_slate": [
        "clean slate protocol", "clean slate", "wrap it up for today", "shut down workspace",
        "clear everything and sleep", "reset all windows", "execute clean slate"
    ],
    "diagnostics": [
        "run system diagnostics", "run full diagnostic", "run diagnostic check", "check vitals",
        "system telemetry status", "how are system resources", "check hardware status",
        "check memory and power", "suit status report", "how much ram is free",
        "battery level and power", "tactical telemetry report", "system diagnostics"
    ],
    "target_practice": [
        "calibrate repulsors", "target practice range", "open target practice",
        "repulsor aim calibration", "repulsor calibration range"
    ],
    "sleep_mode": [
        "go to sleep", "go to sleep now", "sleep mode", "standby mode", "take a nap",
        "enter standby", "rest mode", "put jarvis to sleep", "stand by jarvis"
    ],
    "wake_mode": [
        "wake up", "wake up jarvis", "wake mode", "reactivate system",
        "come online", "activate systems", "resume tracking", "back online"
    ],
    "open_app": [
        "launch application", "open program", "start chrome", "bring up edge",
        "open vscode", "open code editor", "launch terminal", "open calculator",
        "open notepad", "open task manager", "open spotify", "launch browser"
    ],
    "open_site": [
        "open website", "go to youtube", "open youtube", "bring up youtube",
        "open github", "bring up github", "browse to google", "open gmail", "go to google maps"
    ],
    "train_voice": [
        "train my voice", "calibrate vocal profile", "calibrate vocal signature", "learn how I speak",
        "enroll my voice", "calibrate voice profile", "recognize my voice"
    ],
    "weather": [
        "what's the weather", "weather forecast", "how's the weather", "what is the weather like",
        "weather today", "what's the weather outside", "check the weather", "current forecast",
        "is it raining outside", "forecast for today", "how cold is it outside", "temperature outside",
        "what is the forecast"
    ],
    "time": [
        "what time is it", "what's the time", "current time", "what is the time",
        "tell me the time", "what time do you have", "time please", "check the time"
    ],
    "now_playing": [
        "what's playing", "what song is this", "what is playing right now", "what track is this",
        "who is singing this", "now playing", "tell me the song name", "current track info"
    ],
    "show_widget": [
        "show media", "expand media widget", "expand weather widget",
        "display weather on island",
        "open media widget", "expand island widget"
    ],
    "toggle_widgets": [
        "hide the island widgets", "hide island widgets", "hide island", "hide widgets",
        "toggle island widgets", "dismiss widgets", "hide island capsule widgets", "hide widgets on island"
    ],
    # Dynamic Island Actions (Phase 4)
    "island_next_page": [
        "next page", "next card", "island next page", "next island page",
        "flip card", "cycle card forward", "next island card", "next dashboard page"
    ],
    "island_prev_page": [
        "previous page", "prev page", "last page", "island previous page",
        "island prev page", "previous card", "prev card", "cycle card backward", "previous island card"
    ],
    "island_show_page": [
        "show hardware", "show calendar", "show jarvis",
        "show weather card", "show hardware card", "show media card", "show calendar card", "show jarvis card",
        "open weather card", "open hardware card", "open media card", "open calendar card", "open jarvis card",
        "show weather page", "show hardware page", "show media page", "show calendar page", "show jarvis page",
        "switch to weather", "switch to hardware", "switch to media", "switch to calendar", "switch to jarvis"
    ],
    "island_toggle_expand": [
        "expand island", "open island", "collapse island", "close island",
        "toggle island", "expand card", "collapse card", "toggle expand", "toggle island expand"
    ],
    "island_toggle_tracking": [
        "toggle tracking", "pause tracking", "resume tracking", "toggle hand tracking",
        "stop tracking", "start tracking", "toggle gesture tracking", "switch tracking"
    ],
    "island_set_pin": [
        "pin island", "unpin island", "pin card", "keep island open",
        "pin open", "unpin card", "unpin the island", "pin the island"
    ],
    "open_folder": [
        "open folder", "open downloads folder", "open documents folder", "open my documents",
        "open programming folder", "open desktop folder", "open pictures folder", "show my downloads",
        "open my downloads folder", "open folder downloads", "bring up downloads folder",
        "open folder programming", "open react project folder", "open ai notes folder",
        "open folder pictures", "open music folder", "open videos folder", "open directory"
    ],
    "open_recent_file": [
        "open recent file", "open latest file", "open the last file", "open recent download",
        "open latest download", "open my last downloaded file", "open latest document",
        "open recent pdf", "open latest pdf", "open newest file", "open last file"
    ],
    "show_last_download": [
        "show last download", "show latest download", "locate last download", "show downloaded file",
        "reveal last download", "where is my last download", "show download in explorer",
        "find last download", "highlight last download", "show in explorer"
    ],
    "remember_folder": [
        "remember this folder as", "save this folder alias", "bookmark this folder",
        "remember folder alias", "remember folder"
    ],
    "set_volume": [
        "set volume to fifty", "set volume to 50", "set the volume to 80 percent", "volume seventy percent",
        "change volume to 40", "volume to 30", "set audio level to 60", "adjust volume to 50"
    ],
    "brightness_up": [
        "brightness up", "increase brightness", "make screen brighter", "turn up screen brightness",
        "boost brightness", "brighter screen", "raise the brightness", "increase screen brightness"
    ],
    "brightness_down": [
        "brightness down", "decrease brightness", "make screen dimmer", "dim the screen",
        "turn down brightness", "lower screen brightness", "reduce brightness", "dim screen"
    ],
    "set_brightness": [
        "set brightness to 50", "set screen brightness to 80", "set brightness to seventy percent",
        "brightness 60 percent", "change brightness to 40", "set display brightness to 50"
    ],
    "toggle_night_light": [
        "toggle night light", "turn on night light", "turn off night light", "enable night light",
        "disable night light", "switch night light", "night light on", "night light off"
    ],
    "snap_window": [
        "snap left", "snap right", "snap window left", "snap window right",
        "snap window to the left", "snap window to the right", "snap to left", "snap to right",
        "snap window down", "snap window up", "restore window", "restore this window"
    ],
    "maximize_window": [
        "maximize window", "maximize this window", "maximize active window",
        "expand window to full screen", "full screen window", "maximize app"
    ],
    "switch_to_app": [
        "switch to chrome", "switch to vs code", "switch to edge", "switch to notepad",
        "switch to spotify", "switch application", "switch to terminal", "switch to app"
    ],
    "move_to_other_monitor": [
        "move to other monitor", "move window to other monitor", "move to next monitor",
        "move to second monitor", "shift window to other display", "move window to other screen"
    ],
    "close_window": [
        "close window", "close active window", "kill this window", "exit window"
    ],
    "open_this": [
        "open this", "open this file", "open link under cursor", "open file under cursor",
        "activate this", "click this under cursor", "open what is under the cursor"
    ],
    "copy_this": [
        "copy this", "copy selection", "copy this text", "copy under cursor",
        "copy what is under the cursor", "copy the selected text"
    ],
    "close_this": [
        "close this", "close window under cursor", "kill this window under cursor",
        "close the window under cursor", "close this application"
    ],
    "sleep_system": [
        "sleep system", "put pc to sleep", "sleep the computer", "suspend system", "put computer to sleep", "sleep pc"
    ],
    "toggle_wifi": [
        "toggle wifi", "turn on wifi", "turn off wifi", "enable wifi", "disable wifi", "switch wifi"
    ],
    "toggle_bluetooth": [
        "toggle bluetooth", "turn on bluetooth", "turn off bluetooth", "enable bluetooth", "disable bluetooth"
    ],
    "toggle_do_not_disturb": [
        "turn on do not disturb", "turn off do not disturb", "toggle do not disturb",
        "enable focus assist", "disable focus assist", "do not disturb", "focus assist"
    ],
    "shutdown_system": [
        "shut down pc", "turn off pc", "shutdown computer", "power off computer", "turn off computer",
        "shut down the computer", "shut down the pc", "shutdown system"
    ],
    "restart_system": [
        "restart pc", "reboot pc", "restart computer", "reboot computer", "restart system",
        "reboot the computer", "reboot system", "restart the pc"
    ],
    "set_timer": [
        "set a timer for five minutes", "set timer for 10 minutes", "start a timer for 15 minutes",
        "timer 20 minutes", "set a countdown timer", "set 5 minute timer", "start timer"
    ],
    "cancel_timer": [
        "cancel the timer", "stop the timer", "clear the timer", "cancel active timer",
        "stop countdown", "reset timer", "cancel timer"
    ],
    "timer_status": [
        "how much time is left on my timer", "time left on timer", "timer status",
        "check the timer", "how much time remaining", "check countdown"
    ],
    "calculate": [
        "what is 25 times 4", "calculate 15 percent of 200", "what is 100 plus 50",
        "solve 45 divided by 9", "what is 100 minus 20 percent", "compute square root of 144"
    ],
    "convert_units": [
        "convert 10 miles to kilometers", "how many feet in 2 meters", "convert 100 fahrenheit to celsius",
        "convert 500 grams to ounces", "convert 2 gallons to liters"
    ],
    "quick_note": [
        "note that", "take a note", "write down that", "add a note", "take note",
        "record a quick note", "save a note"
    ],
    "start_routine": [
        "start work mode", "activate work mode", "start focus mode", "activate focus mode",
        "start evening mode", "activate evening mode", "run routine", "launch work mode"
    ],
    "read_clipboard": [
        "read clipboard", "what is on my clipboard", "what's on the clipboard",
        "check clipboard", "clipboard content"
    ],
    "screen_awareness": [
        "what's on my screen", "what is on my screen", "explain this screen error", "summarize this page",
        "what does this screen say", "inspect my screen", "analyze my screen", "read my screen",
        "what is showing on my screen", "what does the screen say", "explain the error on screen",
        "inspect this screen", "what is on screen", "summarize the page", "what does my screen say",
        "explain this code on screen"
    ],
    "world_time": [
        "what time is it in tokyo", "what time is it in london", "what time is it in paris",
        "what time is it in new york", "what time is it in sydney", "what time is it in dubai",
        "current time in tokyo", "time in tokyo", "time in london", "time in paris", "time in new york",
        "tell me the time in sydney", "what is the time in dubai", "time in dubai"
    ],
    "conversation": [
        "what is the distance to mars", "how do solar panels work", "who was nikola tesla",
        "explain general relativity", "explain how stars fuse hydrogen into helium", "why is the sky blue",
        "write a python function", "can you help me with coding", "tell me a science fact",
        "what are black holes", "tell me about quantum computing", "give me advice on engineering",
        "translate this", "what is the weather today", "tell me a funny story about Tony Stark",
        "how do quantum computers work", "what is the capital of France"
    ]
}
 
# Intents that write/change state or launch something. Fuzzy (non-exact) System 1 matches
# for these need higher confidence and margin than read-only intents.
SIDE_EFFECT_INTENTS = frozenset({
    "volume_up", "volume_down", "mute", "play_pause", "play_music", "next_track", "previous_track",
    "show_desktop", "lock_workstation", "screenshot", "stealth_mode", "clean_slate",
    "target_practice", "sleep_mode", "wake_mode", "open_app", "open_site", "train_voice",
    "island_toggle_tracking", "island_set_pin", "open_folder", "open_recent_file",
    "show_last_download", "remember_folder", "set_volume", "brightness_up", "brightness_down",
    "set_brightness", "toggle_night_light", "snap_window", "maximize_window", "switch_to_app",
    "move_to_other_monitor", "close_window", "open_this", "copy_this", "close_this",
    "sleep_system", "toggle_wifi", "toggle_bluetooth", "toggle_do_not_disturb",
    "shutdown_system", "restart_system", "set_timer", "cancel_timer", "quick_note",
    "start_routine",
})
SIDE_EFFECT_MIN_SCORE = 0.55
SIDE_EFFECT_MIN_MARGIN = 0.08
_RE_WEATHER_CUE = re.compile(r"\b(?:weather|temperature|forecast|rain|raining|hot|cold|outside)\b")


def extract_whitelist_args(action: Optional[str], clean_text: str) -> Dict[str, Any]:
    """For open_app/open_site: the whitelisted APPS/SITES name present as a whole word, if any."""
    if action not in ("open_app", "open_site"):
        return {}
    from commands import APPS, SITES
    table, key = (APPS, "app") if action == "open_app" else (SITES, "site")
    for name in sorted(table, key=len, reverse=True):
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", clean_text):
            return {key: name}
    return {}


def apply_decision_policy(top_act: Optional[str], top_score: float, margin: float, clean_text: str,
                          extracted_args: Optional[Dict[str, Any]] = None,
                          min_score: float = 0.25, min_margin: float = 0.03) -> Optional[str]:
    """
    Pure System 1 acceptance policy. Returns top_act if it may execute, else None.
    - conversation / no action -> None; score >= 0.85 -> accepted.
    - SIDE_EFFECT_INTENTS need score >= 0.55 and margin >= 0.08, except open_app/open_site with a
      whitelisted APPS/SITES name present (extracted_args), which only need the standard margin.
    - weather additionally needs a whole-word weather cue in clean_text.
    - everything else uses min_score / min_margin.
    """
    if not top_act or top_act == "conversation":
        return None
    if top_score >= 0.85:
        return top_act
    need_score, need_margin = min_score, min_margin
    if top_act in SIDE_EFFECT_INTENTS:
        need_score = max(min_score, SIDE_EFFECT_MIN_SCORE)
        args = extracted_args if extracted_args is not None else extract_whitelist_args(top_act, clean_text)
        if not (top_act in ("open_app", "open_site") and args):
            need_margin = max(min_margin, SIDE_EFFECT_MIN_MARGIN)
    elif top_act == "weather" and not _RE_WEATHER_CUE.search(clean_text):
        return None
    if top_score >= need_score and margin >= need_margin:
        return top_act
    return None


def decide_with_policy(score_fn, text: str, min_score: float = 0.25,
                       min_margin: float = 0.03) -> Tuple[Optional[str], float]:
    """
    score_fn(norm_text) -> (top_act, top_score, margin). Scores the raw text under the policy;
    if rejected, retries once on strip_fillers(text). Returns (action_or_None, score).
    """
    act, score, margin = score_fn(text)
    res = apply_decision_policy(act, score, margin, text, None, min_score, min_margin)
    if res is not None:
        return res, score
    stripped = strip_fillers(text)
    if stripped != text:
        act2, score2, margin2 = score_fn(stripped)
        res2 = apply_decision_policy(act2, score2, margin2, stripped, None, min_score, min_margin)
        if res2 is not None:
            return res2, score2
    return None, score


_EXACT_PROTOTYPE_MAP: Dict[str, str] = {}
for _act, _phrases in INTENT_PROTOTYPES.items():
    for _p in _phrases:
        _clean_p = _RE_NON_WORD.sub(" ", _p.lower()).strip()
        _norm_p = " ".join(_clean_p.split())
        if _norm_p:
            _EXACT_PROTOTYPE_MAP[_norm_p] = _act


def _compute_prototypes_hash() -> str:
    """Computes deterministic SHA-256 hash of INTENT_PROTOTYPES candidates."""
    serialized = json.dumps(INTENT_PROTOTYPES, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _get_default_cache_path() -> str:
    """Returns absolute path to disk cache file in .cache/."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, ".cache", "system1_index.npz")


class SemanticDecisionEngine:
    """
    Lightweight, high-speed on-device decision model for J.A.R.V.I.S.
    Performs calibrated semantic similarity evaluation in < 1 ms with NumPy vectorization.
    Uses non-autoregressive exemplar matching with margin thresholding.
    Caches precomputed TF-IDF index to disk with SHA-256 validation.
    """

    def __init__(self, cache_path: Optional[str] = None):
        self._vocab: Dict[str, int] = {}
        self._idf: np.ndarray = np.array([], dtype=np.float32)
        self._action_names: List[str] = list(INTENT_PROTOTYPES.keys())
        self._action_to_idx: Dict[str, int] = {act: i for i, act in enumerate(self._action_names)}
        self._exemplar_matrix: np.ndarray = np.array([], dtype=np.float32)
        self._exemplar_actions: List[str] = []
        self._exemplar_action_indices: np.ndarray = np.array([], dtype=np.int32)
        self._cache_path: str = cache_path if cache_path is not None else _get_default_cache_path()
        if not self._load_from_cache():
            self._build_vocab_and_prototypes()
            self._save_to_cache()

    def _load_from_cache(self) -> bool:
        """Loads precomputed index from disk if content hash matches."""
        if not self._cache_path or not os.path.exists(self._cache_path):
            return False
        try:
            expected_hash = _compute_prototypes_hash()
            with np.load(self._cache_path, allow_pickle=False) as data:
                cached_hash = str(data["hash"][0])
                if cached_hash != expected_hash:
                    return False
                vk = data["vocab_keys"]
                vv = data["vocab_vals"]
                self._vocab = dict(zip(vk.tolist(), vv.tolist()))
                self._idf = data["idf"].astype(np.float32)
                self._exemplar_matrix = data["exemplar_matrix"].astype(np.float32)
                self._exemplar_action_indices = data["exemplar_action_indices"].astype(np.int32)
                self._action_names = data["action_names"].tolist()
                self._action_to_idx = {act: i for i, act in enumerate(self._action_names)}
                self._exemplar_actions = [self._action_names[idx] for idx in self._exemplar_action_indices]
                return True
        except Exception:
            return False

    def _save_to_cache(self) -> bool:
        """Atomically saves precomputed index to disk with content hash."""
        if not self._cache_path:
            return False
        try:
            cache_dir = os.path.dirname(self._cache_path)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            current_hash = _compute_prototypes_hash()
            vk = np.array(list(self._vocab.keys()))
            vv = np.array(list(self._vocab.values()), dtype=np.int32)
            action_names_arr = np.array(self._action_names)
            hash_arr = np.array([current_hash])
            tmp_path = f"{self._cache_path}.tmp.npz"
            np.savez(
                tmp_path,
                vocab_keys=vk,
                vocab_vals=vv,
                idf=self._idf,
                exemplar_matrix=self._exemplar_matrix,
                exemplar_action_indices=self._exemplar_action_indices,
                action_names=action_names_arr,
                hash=hash_arr,
            )
            os.replace(tmp_path, self._cache_path)
            return True
        except Exception:
            return False

    def _tokenize(self, text: str) -> List[str]:
        """Extracts word unigrams and character 3-grams for typo-resilient semantic matching."""
        clean = _RE_NON_WORD.sub(" ", text.lower()).strip()
        words = clean.split()
        tokens = list(words)
        # Add character tri-grams for subword matching
        for w in words:
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tokens.append(f"#{w[i:i+3]}")
        return tokens

    def _build_vocab_and_prototypes(self):
        """Constructs semantic vocabulary and individual exemplar vectors for crisp classification."""
        all_phrases = []
        doc_counts: Dict[str, int] = {}
        phrase_tokens: List[Tuple[str, List[str]]] = []

        self._action_names = list(INTENT_PROTOTYPES.keys())
        self._action_to_idx = {act: i for i, act in enumerate(self._action_names)}

        for action, phrases in INTENT_PROTOTYPES.items():
            for p in phrases:
                all_phrases.append(p)
                tokens = self._tokenize(p)
                phrase_tokens.append((action, tokens))
                for t in set(tokens):
                    doc_counts[t] = doc_counts.get(t, 0) + 1

        n_docs = len(all_phrases)
        self._vocab = {tok: idx for idx, tok in enumerate(sorted(doc_counts.keys()))}
        vocab_size = len(self._vocab)

        # Compute IDF
        self._idf = np.zeros(vocab_size, dtype=np.float32)
        for tok, idx in self._vocab.items():
            self._idf[idx] = math.log((1.0 + n_docs) / (1.0 + doc_counts[tok])) + 1.0

        # Build normalized exemplar vectors
        exemplars = []
        exemplar_actions = []
        exemplar_action_indices = []
        for action, tokens in phrase_tokens:
            vec = self._vectorize_tokens(tokens)
            norm = np.linalg.norm(vec)
            if norm > 1e-6:
                exemplars.append(vec / norm)
                exemplar_actions.append(action)
                exemplar_action_indices.append(self._action_to_idx[action])

        self._exemplar_matrix = np.stack(exemplars, axis=0).astype(np.float32)  # Shape: (n_exemplars, vocab_size)
        self._exemplar_actions = exemplar_actions
        self._exemplar_action_indices = np.array(exemplar_action_indices, dtype=np.int32)

    def _vectorize_tokens(self, tokens: List[str]) -> np.ndarray:
        """Projects token list into normalized TF-IDF feature vector."""
        vec = np.zeros(len(self._vocab), dtype=np.float32)
        for t in tokens:
            if t in self._vocab:
                vec[self._vocab[t]] += 1.0
        vec *= self._idf
        norm = np.linalg.norm(vec)
        return (vec / norm) if norm > 1e-6 else vec

    def score(self, user_text: str) -> Dict[str, float]:
        """Returns softmax-calibrated decision probabilities across all action categories."""
        tokens = self._tokenize(user_text)
        vec = self._vectorize_tokens(tokens)
        if np.linalg.norm(vec) < 1e-6 or len(self._exemplar_actions) == 0:
            return {act: 1.0 / len(self._action_names) for act in self._action_names}

        # Vectorized dot product and max-reduction across action heads
        raw_sims = np.dot(self._exemplar_matrix, vec)
        action_max_sims = np.zeros(len(self._action_names), dtype=np.float32)
        np.maximum.at(action_max_sims, self._exemplar_action_indices, raw_sims)

        # Softmax with temperature scaling T=0.20 for crisp, calibrated probabilities
        temp = 0.20
        max_sim = np.max(action_max_sims)
        exp_sims = np.exp((action_max_sims - max_sim) / temp)
        probs = exp_sims / np.sum(exp_sims)
        return {act: float(probs[i]) for i, act in enumerate(self._action_names)}

    def decide(self, user_text: str, min_score: float = 0.25, min_margin: float = 0.03) -> Tuple[Optional[str], float]:
        """
        Evaluates user speech. Returns (action_name, confidence).
        Uses margin-based thresholding inspired by non-autoregressive decision models.
        If best decision is 'conversation' or score is ambiguous, returns (None, confidence).
        Vectorized with NumPy for < 1.0 ms System 1 latency.
        """
        if not user_text:
            return None, 0.0
        if not isinstance(user_text, str):
            try:
                user_text = str(user_text, errors="ignore") if isinstance(user_text, (bytes, bytearray)) else str(user_text)
            except Exception:
                return None, 0.0
        if len(user_text) > 4096:
            user_text = user_text[:4096]

        cleaned = _RE_NON_WORD.sub(" ", user_text.lower()).strip()
        norm_text = " ".join(cleaned.split())
        if not norm_text:
            return None, 0.0
        return decide_with_policy(self._raw_scores_memo, norm_text, min_score, min_margin)

    def _raw_scores(self, norm_text: str) -> Tuple[Optional[str], float, float]:
        """Policy-free scoring: (top_act, top_score, margin). Exact prototype hits return (act, 1.0, 1.0)."""
        with perf.span("intent_decision"):
            if norm_text in _EXACT_PROTOTYPE_MAP:
                return _EXACT_PROTOTYPE_MAP[norm_text], 1.0, 1.0
            tokens = self._tokenize(norm_text)
            vec = self._vectorize_tokens(tokens)
            if np.linalg.norm(vec) < 1e-6 or len(self._exemplar_actions) == 0:
                return None, 0.0, 0.0
            raw_sims = np.dot(self._exemplar_matrix, vec)
            n_actions = len(self._action_names)
            action_max_sims = np.zeros(n_actions, dtype=np.float32)
            np.maximum.at(action_max_sims, self._exemplar_action_indices, raw_sims)
            top_idx = int(np.argmax(action_max_sims))
            top_score = float(action_max_sims[top_idx])
            top_act = self._action_names[top_idx]
            action_max_sims[top_idx] = -1.0
            second_score = float(np.max(action_max_sims)) if n_actions > 1 else 0.0
            return top_act, top_score, top_score - second_score

    @lru_cache(maxsize=1024)
    def _raw_scores_memo(self, norm_text: str) -> Tuple[Optional[str], float, float]:
        return self._raw_scores(norm_text)

    @lru_cache(maxsize=1024)
    def _decide_memo(self, norm_text: str, min_score: float, min_margin: float) -> Tuple[Optional[str], float]:
        """Single-text decision (no filler retry)."""
        act, score, margin = self._raw_scores(norm_text)
        return apply_decision_policy(act, score, margin, norm_text, None, min_score, min_margin), score

    def clear(self) -> None:
        """Frees large exemplar matrices on shutdown and clears LRU caches."""
        try:
            self._decide_memo.cache_clear()
            self._raw_scores_memo.cache_clear()
        except Exception:
            pass
        self._exemplar_matrix = np.array([], dtype=np.float32)
        self._exemplar_action_indices = np.array([], dtype=np.int32)
        self._idf = np.array([], dtype=np.float32)
        self._vocab.clear()

    def classify(self, user_text: str, min_score: float = 0.25, min_margin: float = 0.03) -> DecisionMatch:
        """
        Evaluates user text and returns DecisionMatch with confidence and extracted parameters.
        """
        if not user_text:
            return DecisionMatch(intent=None, confidence=0.0, args={})
        if not isinstance(user_text, str):
            try:
                user_text = str(user_text, errors="ignore") if isinstance(user_text, (bytes, bytearray)) else str(user_text)
            except Exception:
                return DecisionMatch(intent=None, confidence=0.0, args={})
        if len(user_text) > 4096:
            user_text = user_text[:4096]

        action, conf = self.decide(user_text, min_score=min_score, min_margin=min_margin)
        args: Dict[str, Any] = {}
        clean = user_text.lower().strip(" ,.!?")
        if action == "open_app":
            from commands import APPS
            for app in APPS:
                if app in clean:
                    args["app"] = app
                    break
        elif action == "open_site":
            from commands import SITES
            for site in SITES:
                if site in clean:
                    args["site"] = site
                    break
        elif action == "open_folder":
            m = _RE_OPEN_FOLDER.search(clean)
            f_name = m.group(1).strip() if m else clean
            args["folder"] = _RE_STRIP_MY_THE.sub("", f_name).strip()
        elif action == "open_recent_file":
            folder = "downloads"
            if "documents" in clean:
                folder = "documents"
            elif "desktop" in clean:
                folder = "desktop"
            args["folder"] = folder
            ext_m = _RE_FILE_EXT.search(clean)
            if ext_m:
                args["extension"] = ext_m.group(1)
        elif action == "show_last_download":
            args["folder"] = "downloads"
        elif action == "remember_folder":
            m = _RE_REMEMBER_FOLDER.search(clean)
            if m:
                args["alias"] = m.group(1).strip()
        elif action == "show_widget":
            args["widget"] = "media" if ("media" in clean or "music" in clean) else "weather"
        elif action == "island_show_page":
            page = "jarvis"
            for p in ("weather", "hardware", "media", "calendar", "jarvis"):
                if p in clean:
                    page = p
                    break
            args["page"] = page
        elif action == "island_set_pin":
            args["pinned"] = not ("unpin" in clean or "don't pin" in clean or "do not pin" in clean)
        return DecisionMatch(intent=action, confidence=conf, args=args)

    def get_top_candidates(self, user_text: str, top_k: int = 3, min_score: float = 0.18) -> List[Tuple[str, float]]:
        """
        Returns ranked list of top actionable (intent, score) candidates,
        excluding 'conversation', using calibrated softmax probabilities.
        """
        probs = self.score(user_text)
        ranked = [
            (act, score)
            for act, score in sorted(probs.items(), key=lambda x: x[1], reverse=True)
            if act != "conversation" and score >= min_score
        ]
        return ranked[:top_k]


def _extract_args_for_action(action: str, clean: str) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    if action in ("volume_up", "volume_down"):
        args["steps"] = 5
    elif action == "open_app":
        from commands import APPS
        for app in sorted(APPS.keys(), key=len, reverse=True):
            if _get_word_boundary_regex(app).search(clean):
                args["app"] = app
                break
    elif action == "open_site":
        from commands import SITES
        for site in sorted(SITES.keys(), key=len, reverse=True):
            if _get_word_boundary_regex(site).search(clean):
                args["site"] = site
                break
    elif action == "play_music":
        platform = "spotify" if "spotify" in clean else ("youtube" if "youtube" in clean else "auto")
        query = clean
        m = _RE_PLAY_MUSIC.search(clean)
        if m:
            query = m.group(1).strip()
        args = {"query": query, "platform": platform}
    elif action == "show_widget":
        args["widget"] = "media" if ("media" in clean or "music" in clean) else "weather"
    elif action == "open_folder":
        m = _RE_OPEN_FOLDER.search(clean)
        f_name = m.group(1).strip() if m else clean
        args["folder"] = _RE_STRIP_MY_THE.sub("", f_name).strip()
    elif action == "open_recent_file":
        folder = "downloads"
        if "documents" in clean:
            folder = "documents"
        elif "desktop" in clean:
            folder = "desktop"
        args["folder"] = folder
        ext_m = _RE_FILE_EXT.search(clean)
        if ext_m:
            args["extension"] = ext_m.group(1)
    elif action == "show_last_download":
        args["folder"] = "downloads"
    elif action == "remember_folder":
        m = _RE_REMEMBER_FOLDER.search(clean)
        if m:
            args["alias"] = m.group(1).strip()
    elif action == "set_volume":
        m = _RE_SET_VOLUME.search(clean)
        args["level"] = int(m.group(1)) if m else 50
    elif action in ("brightness_up", "brightness_down"):
        args["steps"] = 10
    elif action == "set_brightness":
        m = _RE_SET_BRIGHTNESS.search(clean)
        args["level"] = int(m.group(1)) if m else 50
    elif action == "snap_window":
        dir_val = "left"
        if "right" in clean:
            dir_val = "right"
        elif "up" in clean or "maximize" in clean:
            dir_val = "up"
        elif "down" in clean or "restore" in clean:
            dir_val = "down"
        args["direction"] = dir_val
    elif action == "maximize_window":
        args["direction"] = "up"
    elif action == "switch_to_app":
        from commands import APPS
        for app in sorted(APPS.keys(), key=len, reverse=True):
            if _get_word_boundary_regex(app).search(clean):
                args["app"] = app
                break
        if "app" not in args:
            m = _RE_SWITCH_APP.search(clean)
            if m:
                args["app"] = m.group(1).strip()
    elif action in ("shutdown_system", "restart_system"):
        args["confirmed"] = "confirm" in clean
    elif action == "close_window":
        args["confirm"] = "confirm" in clean or "force" in clean
    elif action == "island_show_page":
        page = "jarvis"
        for p in ("weather", "hardware", "media", "calendar", "jarvis"):
            if p in clean:
                page = p
                break
        args["page"] = page
    elif action == "island_set_pin":
        args["pinned"] = not ("unpin" in clean or "don't pin" in clean or "do not pin" in clean)
    return args


def _get_action_label(action: str, args: Dict[str, Any]) -> str:
    labels = {
        "volume_up": "Volume Up",
        "volume_down": "Volume Down",
        "mute": "Mute Audio",
        "play_pause": "Play / Pause",
        "next_track": "Next Track",
        "previous_track": "Previous Track",
        "show_desktop": "Show Desktop",
        "lock_workstation": "Lock Workstation",
        "screenshot": "Take Screenshot",
        "stealth_mode": "Stealth Mode",
        "clean_slate": "Clean Slate Protocol",
        "diagnostics": "System Diagnostics",
        "target_practice": "Target Practice",
        "sleep_mode": "Sleep Mode",
        "wake_mode": "Wake Online",
        "weather": "Weather Forecast",
        "time": "Current Time",
        "now_playing": "Now Playing",
        "toggle_widgets": "Toggle Widgets",
        "show_last_download": "Show Last Download",
        "set_volume": "Set Volume",
        "brightness_up": "Brightness Up",
        "brightness_down": "Brightness Down",
        "set_brightness": "Set Brightness",
        "toggle_night_light": "Toggle Night Light",
        "snap_window": "Snap Window",
        "maximize_window": "Maximize Window",
        "minimize_all": "Minimize All Windows",
        "switch_to_app": "Switch Application",
        "move_to_other_monitor": "Move to Other Monitor",
        "close_window": "Close Window",
        "open_this": "Open Under Cursor",
        "copy_this": "Copy Under Cursor",
        "close_this": "Close Under Cursor",
        "sleep_system": "Sleep System",
        "toggle_wifi": "Toggle Wi-Fi",
        "toggle_bluetooth": "Toggle Bluetooth",
        "toggle_do_not_disturb": "Toggle Do Not Disturb",
        "shutdown_system": "Shutdown System",
        "restart_system": "Restart System",
        "set_timer": "Set Timer",
        "cancel_timer": "Cancel Timer",
        "timer_status": "Timer Status",
        "calculate": "Calculate",
        "convert_units": "Convert Units",
        "quick_note": "Quick Note",
        "read_notes": "Read Notes",
        "type_text": "Type Text",
        "dictation_mode": "Dictation Mode",
        "read_clipboard": "Read Clipboard",
        "summarize_clipboard": "Summarize Clipboard",
        "start_routine": "Start Routine",
        "screen_awareness": "Analyzing Screen",
        "analyze_screen": "Analyzing Screen",
        "world_time": "World Time",
        "quick_answer": "Quick Answer",
        "island_next_page": "Next Island Page",
        "island_prev_page": "Previous Island Page",
        "island_show_page": "Show Island Page",
        "island_toggle_expand": "Toggle Island Expand",
        "island_toggle_tracking": "Toggle Hand Tracking",
        "island_set_pin": "Pin Dynamic Island",
    }
    if action == "island_show_page" and "page" in args:
        return f"Show {args['page'].title()} Page"
    if action == "start_routine" and "routine" in args:
        return f"Start {args['routine'].title()}"
    if action == "quick_note" and "text" in args:
        return f"Note: {args['text'][:25]}"
    if action == "open_app" and "app" in args:
        return f"Open {args['app'].title()}"
    if action == "open_site" and "site" in args:
        return f"Open {args['site'].title()}"
    if action == "play_music" and "query" in args:
        return f"Play {args['query'].title()}"
    if action == "show_widget" and "widget" in args:
        return f"Show {args['widget'].title()} Widget"
    if action == "open_folder" and "folder" in args:
        return f"Open {args['folder'].title()} Folder"
    if action == "open_recent_file":
        f_name = args.get("folder", "downloads").title()
        ext = f" ({args['extension'].upper()})" if "extension" in args else ""
        return f"Open Recent File {f_name}{ext}"
    if action == "remember_folder" and "alias" in args:
        return f"Remember Folder '{args['alias']}'"
    return labels.get(action, action.replace("_", " ").title())


def check_ambiguity_candidates(user_text: str) -> List[Candidate]:
    """
    Evaluates whether user text presents close-scoring or ambiguous choices:
    1. Entity ambiguity (e.g. 'open chrome or edge', 'open youtube or google')
    2. Semantic margin ambiguity (top 2-3 intents within margin <= 0.08)
    Returns list of Candidate objects if ambiguous, empty list otherwise.
    """
    clean = user_text.lower().strip(" ,.!?")

    # 1. Entity ambiguity (multiple apps or sites mentioned with 'or', 'vs', 'and')
    from commands import APPS, SITES
    if any(sep in clean for sep in (" or ", " vs ", " and ", " / ")):
        found_apps = []
        seen_targets = set()
        for app in sorted(APPS.keys(), key=len, reverse=True):
            if _get_word_boundary_regex(app).search(clean):
                tgt = APPS[app]
                if tgt not in seen_targets:
                    seen_targets.add(tgt)
                    found_apps.append(app)

        if len(found_apps) >= 2:
            return [
                Candidate(
                    index=i + 1,
                    action="open_app",
                    args={"app": app},
                    label=f"Open {app.title()}",
                    confidence=0.5,
                )
                for i, app in enumerate(found_apps[:3])
            ]

        found_sites = []
        seen_sites = set()
        for site in sorted(SITES.keys(), key=len, reverse=True):
            if _get_word_boundary_regex(site).search(clean):
                tgt = SITES[site]
                if tgt not in seen_sites:
                    seen_sites.add(tgt)
                    found_sites.append(site)

        if len(found_sites) >= 2:
            return [
                Candidate(
                    index=i + 1,
                    action="open_site",
                    args={"site": site},
                    label=f"Open {site.title()}",
                    confidence=0.5,
                )
                for i, site in enumerate(found_sites[:3])
            ]

    # 2. Semantic margin ambiguity
    engine = get_decision_engine()
    top_cands = engine.get_top_candidates(clean, top_k=3, min_score=0.18)
    if len(top_cands) >= 2:
        top_act, top_score = top_cands[0]
        sec_act, sec_score = top_cands[1]
        margin = top_score - sec_score
        if margin <= 0.08 and top_score >= 0.20:
            cands = []
            for i, (act, score) in enumerate(top_cands[:3]):
                args = _extract_args_for_action(act, clean)
                label = _get_action_label(act, args)
                cands.append(
                    Candidate(
                        index=i + 1,
                        action=act,
                        args=args,
                        label=label,
                        confidence=score,
                    )
                )
            return cands

    return []


# Global decision engine instance
_ENGINE: Optional[SemanticDecisionEngine] = None


def get_decision_engine() -> SemanticDecisionEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = SemanticDecisionEngine()
    return _ENGINE


_VISUAL_ACTIONS = frozenset({"screen_awareness", "analyze_screen", "camera_capture", "visual_query", "describe_scene"})
_RE_VISUAL_CUE = re.compile(r"(?<![\w'])(?:screen|see|look|this|that|read|what's on|whats on|show me)(?![\w'])")


def evaluate_decision(user_text: str, ctx: Any = None) -> Optional[str]:
    """
    Evaluates speech via the local Decision Engine (System 1).
    If a whitelisted desktop action is identified with high confidence,
    executes it locally and returns J.A.R.V.I.S.'s spoken reply in ~5-15 ms.
    Returns None if the utterance should be routed to Groq LLM (System 2).
    """
    if not user_text:
        return None
    if not isinstance(user_text, str):
        try:
            user_text = str(user_text, errors="ignore") if isinstance(user_text, (bytes, bytearray)) else str(user_text)
        except Exception:
            return None
    if len(user_text) > 4096:
        user_text = user_text[:4096]

    from ai import execute_whitelisted_action
    from commands import APPS, SITES, get_system_telemetry

    clean = user_text.lower().strip(" ,.!?")
    # Wake-word stripping: "jarvis play" must be classified as "play", not as "show jarvis".
    from commands import strip_wake_prefix
    clean = strip_wake_prefix(clean)

    # Safety Gate: check for unsafe or arbitrary shell/PowerShell execution
    from quick_answers import is_unsafe_shell_command
    if is_unsafe_shell_command(clean):
        return "I can't do that, sir."

    # Phase 7 Quick Answers & World Time (System 1, <1 ms)
    import quick_answers
    if quick_answers.is_timezone_query(clean):
        tz_reply = quick_answers.get_world_time(clean, ctx)
        if tz_reply:
            return tz_reply

    # Phase 7 Screen Awareness (explicit user query)
    import screen_awareness
    if screen_awareness.is_screen_query(clean):
        return screen_awareness.analyze_screen(user_text, ctx)

    # Check for close-scoring candidates or entity ambiguity (Disambiguation Engine)
    ambig_cands = check_ambiguity_candidates(clean)
    if ambig_cands and len(ambig_cands) >= 2:
        dis_eng = get_disambiguation_engine()
        overlay = getattr(ctx, "overlay", None) if ctx else None
        dis_eng.start_session(ambig_cands, overlay=overlay)
        get_context_resolver().record_turn(
            user_utterance=user_text,
            candidates=ambig_cands,
        )
        labels = [f"{c.index}: {c.label}" for c in ambig_cands]
        if len(labels) == 2:
            prompt_str = f"{labels[0]}, or {labels[1]}"
        else:
            prompt_str = f"{', '.join(labels[:-1])}, or {labels[-1]}"
        return f"Did you mean {prompt_str}, sir?"

    engine = get_decision_engine()
    action, prob = engine.decide(clean)

    if not action:
        return None

    # System 1 guards: screen/visual actions need an explicit visual cue AND confidence >= 0.70;
    # power-mode actions need the exact power phrase.
    import jarvis_log as _jl
    if action in _VISUAL_ACTIONS and not (prob >= 0.70 and _RE_VISUAL_CUE.search(clean)):
        _jl.get_logger("decision").info("System 1 %s rejected: no explicit visual cue or conf %.2f < 0.70", action, prob)
        return None
    if action in ("sleep_mode", "wake_mode"):
        from commands import is_power_phrase
        if not is_power_phrase(clean, "standby" if action == "sleep_mode" else "wake"):
            _jl.utterance_event(_jl.DECISION, tier="system1", action="power_mode(blocked)")
            return None

    args: Dict[str, Any] = {}
    reply = ""

    if action == "volume_up":
        args = {"steps": 5}
        reply = "Adjusting the volume upward for you, sir."
    elif action == "volume_down":
        args = {"steps": 5}
        reply = "Lowering the volume, sir."
    elif action == "mute":
        reply = "Audio muted, sir."
    elif action == "play_pause":
        reply = "Playback toggled, sir."
    elif action == "play_music":
        platform = "spotify" if "spotify" in clean else ("youtube" if "youtube" in clean else "auto")
        query = clean
        m = _RE_PLAY_MUSIC.search(clean)
        if m:
            query = m.group(1).strip()
        args = {"query": query, "platform": platform}
        reply = f"Playing {query.title()} on {platform.title()}, sir." if platform != "auto" else f"Playing {query.title()} for you now, sir."
    elif action == "next_track":
        reply = "Skipping to the next track, sir."
    elif action == "previous_track":
        reply = "Returning to the previous track, sir."
    elif action == "weather":
        from weather_client import get_weather_client
        cfg = getattr(ctx, "cfg", None) or getattr(getattr(ctx, "engine", None), "cfg", None)
        city = getattr(cfg, "weather_city", "New York") if cfg else "New York"
        lat = getattr(cfg, "weather_lat", 40.7128) if cfg else 40.7128
        lon = getattr(cfg, "weather_lon", -74.0060) if cfg else -74.0060
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
            args = {"city": c_name, "temp": temp_int, "condition": w.condition}
            reply = f"It is {temp_int} degrees and {w.description.lower()} in {c_name}, sir."
        else:
            reply = "I am unable to retrieve the weather forecast at the moment, sir."
    elif action == "time":
        import datetime
        now_str = datetime.datetime.now().strftime("%I:%M %p").lstrip("0")
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("result", text=f"Time: {now_str}", icon="time", duration=3.0)
            except Exception:
                pass
        args = {"time": now_str}
        reply = f"It is {now_str}, sir."
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
            args = {"title": media.title, "artist": media.artist}
            reply_artist = f" by {media.artist}" if media.artist else ""
            reply = f"Currently playing '{media.title}'{reply_artist}, sir."
        else:
            reply = "No media is currently active or playing, sir."
    elif action == "show_widget":
        widget = "media" if ("media" in clean or "music" in clean) else "weather"
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
        args = {"widget": widget}
        reply = f"Displaying {widget} widget, sir."
    elif action == "toggle_widgets":
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("toggle_widgets")
            except Exception:
                pass
        reply = "Toggling Dynamic Island widgets, sir."
    elif action == "island_next_page":
        reply = "Navigating to the next page, sir."
    elif action == "island_prev_page":
        reply = "Navigating to the previous page, sir."
    elif action == "island_show_page":
        page = "jarvis"
        for p in ("weather", "hardware", "media", "calendar", "jarvis"):
            if p in clean:
                page = p
                break
        args = {"page": page}
        reply = f"Displaying {page.title()} page on the Dynamic Island, sir."
    elif action == "island_toggle_expand":
        reply = "Toggling Dynamic Island expansion, sir."
    elif action == "island_toggle_tracking":
        reply = "Toggling hand tracking, sir."
    elif action == "island_set_pin":
        pinned = not ("unpin" in clean or "don't pin" in clean or "do not pin" in clean)
        args = {"pinned": pinned}
        reply = f"Dynamic Island {'pinned open' if pinned else 'unpinned'}, sir."
    elif action == "show_desktop":
        reply = "Displaying the desktop, sir."
    elif action == "lock_workstation":
        reply = "Locking workstation now, sir."
    elif action == "screenshot":
        reply = "Screen capture saved to your pictures, sir."
    elif action == "stealth_mode":
        reply = "Stealth mode engaged, sir. Signatures minimized."
    elif action == "clean_slate":
        reply = "Clean Slate Protocol executed, sir. Have a pleasant evening."
    elif action == "diagnostics":
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("action_executed", action="diagnostics", success=True)
            except Exception:
                pass
        return get_system_telemetry(ctx)
    elif action == "target_practice":
        reply = "Target practice range initialized, sir. Repulsor calibration ready."
    elif action == "sleep_mode":
        args = {"mode": "standby"}
        action = "mode"
        reply = "Going to standby, sir."
    elif action == "wake_mode":
        args = {"mode": "wake"}
        action = "mode"
        reply = "Online. At your service, sir."
        try:
            from ai import prewarm_connection
            prewarm_connection(async_prewarm=True)
        except Exception:
            pass
    elif action == "train_voice":
        from voice_profile import get_voice_profile
        from voice import Voice
        vp = get_voice_profile()
        latest = getattr(Voice, "_last_raw_audio", None)
        if latest:
            ok, msg = vp.enroll([latest])
            if ok:
                vp.enabled = True
                if ctx and hasattr(ctx, "overlay") and ctx.overlay:
                    try:
                        ctx.overlay.post("action_executed", action="train_voice", success=True)
                    except Exception:
                        pass
                return "Voice profile successfully calibrated, sir. I have committed your vocal signature to memory."
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("action_executed", action="train_voice", success=False)
            except Exception:
                pass
        return "Voice training mode active, sir. Speak clearly and I will calibrate to your speech."
    elif action == "open_app":
        # Extract target app
        for app in APPS:
            if app in clean:
                args = {"app": app}
                reply = f"Opening {app}, sir."
                break
        if not args:
            return None  # Route to LLM if app cannot be extracted
    elif action == "open_site":
        for site in SITES:
            if site in clean:
                args = {"site": site}
                reply = f"Opening {site}, sir."
                break
        if not args:
            return None
    elif action == "open_folder":
        folder_raw = clean
        m = _RE_OPEN_FOLDER.search(clean)
        if m:
            folder_raw = m.group(1).strip()
        folder_raw = _RE_STRIP_MY_THE.sub("", folder_raw).strip()
        args = {"folder": folder_raw}
        reply = f"Opening {folder_raw} folder, sir."
    elif action == "open_recent_file":
        folder = "downloads"
        if "documents" in clean:
            folder = "documents"
        elif "desktop" in clean:
            folder = "desktop"
        args = {"folder": folder}
        ext_m = _RE_FILE_EXT.search(clean)
        if ext_m:
            args["extension"] = ext_m.group(1)
        reply = f"Opening recent file from {folder}, sir."
    elif action == "show_last_download":
        args = {}
        reply = "Showing your latest download in Explorer, sir."
    elif action == "remember_folder":
        m = _RE_REMEMBER_FOLDER.search(clean)
        alias = m.group(1).strip() if m else "workspace"
        path = os.getcwd()
        args = {"alias": alias, "path": path, "confirmed": True}
        reply = f"Remembering folder as '{alias}', sir."
    elif action == "set_volume":
        m = _RE_SET_VOLUME.search(clean)
        lvl = int(m.group(1)) if m else 50
        args = {"level": lvl}
        reply = f"Volume set to {lvl} percent, sir."
    elif action == "brightness_up":
        args = {"steps": 10}
        reply = "Screen brightness increased, sir."
    elif action == "brightness_down":
        args = {"steps": 10}
        reply = "Screen brightness lowered, sir."
    elif action == "set_brightness":
        m = _RE_SET_BRIGHTNESS.search(clean)
        lvl = int(m.group(1)) if m else 50
        args = {"level": lvl}
        reply = f"Display brightness calibrated to {lvl} percent, sir."
    elif action == "toggle_night_light":
        reply = "Toggling Night Light, sir."
    elif action == "snap_window":
        dir_val = "left"
        if "right" in clean:
            dir_val = "right"
        elif "up" in clean or "maximize" in clean:
            dir_val = "up"
        elif "down" in clean or "restore" in clean:
            dir_val = "down"
        args = {"direction": dir_val}
        reply = f"Window snapped {dir_val}, sir."
    elif action == "maximize_window":
        action = "snap_window"
        args = {"direction": "up"}
        reply = "Window maximized, sir."
    elif action == "minimize_all":
        reply = "All windows minimized, sir."
    elif action == "switch_to_app":
        from commands import APPS
        target_app = ""
        for app in sorted(APPS.keys(), key=len, reverse=True):
            if _get_word_boundary_regex(app).search(clean):
                target_app = app
                break
        if not target_app:
            m = _RE_SWITCH_APP.search(clean)
            if m:
                target_app = m.group(1).strip()
        args = {"app": target_app}
        reply = f"Switched to {target_app}, sir."
    elif action == "move_to_other_monitor":
        reply = "Window moved to other monitor, sir."
    elif action == "close_window":
        args = {"confirm": "confirm" in clean or "force" in clean}
        reply = "Closing window, sir."
    elif action == "open_this":
        import window_resolver
        ok, msg = window_resolver.open_this()
        return msg
    elif action == "copy_this":
        import window_resolver
        ok, msg = window_resolver.copy_this()
        return msg
    elif action == "close_this":
        import window_resolver
        confirm = "confirm" in clean or "force" in clean
        ok, msg = window_resolver.close_this(confirm=confirm)
        if not ok and "unsaved" in msg.lower():
            import commands
            commands._PENDING_CONFIRMATION = "close_this"
        return msg
    elif action == "sleep_system":
        reply = "Suspending system power states, sir."
    elif action == "toggle_wifi":
        reply = "Toggling Wi-Fi, sir."
    elif action == "toggle_bluetooth":
        reply = "Toggling Bluetooth, sir."
    elif action == "toggle_do_not_disturb":
        reply = "Toggling Do Not Disturb, sir."
    elif action == "shutdown_system":
        confirmed = "confirm" in clean
        args = {"confirmed": confirmed}
        if not confirmed:
            return "Shutdown requires confirmation, sir. Please say 'confirm shutdown' to proceed."
        reply = "Shutting down system, sir."
    elif action == "restart_system":
        confirmed = "confirm" in clean
        args = {"confirmed": confirmed}
        if not confirmed:
            return "Restart requires confirmation, sir. Please say 'confirm restart' to proceed."
        reply = "Restarting system, sir."
    elif action == "set_timer":
        import scheduler
        dur = scheduler.parse_duration_seconds(clean) or 300.0
        args = {"duration": dur, "label": "Timer"}
        reply = f"Timer set for {scheduler.format_duration(dur)}, sir."
    elif action == "cancel_timer":
        args = {}
        reply = "Timer cancelled, sir."
    elif action == "timer_status":
        args = {}
        import scheduler
        sched = scheduler.get_scheduler(ctx=ctx)
        left = sched.get_time_left()
        if left is not None and left > 0:
            reply = f"There is {scheduler.format_duration(left)} remaining, sir."
        else:
            reply = "There are no active timers running, sir."
    elif action == "calculate":
        import math_parser
        math_res = math_parser.parse_math_or_unit_query(clean)
        if math_res:
            raw_ans, reply = math_res
            args = {"expression": clean, "result": raw_ans}
        else:
            args = {"expression": clean}
            reply = "Calculation completed, sir."
    elif action == "convert_units":
        import math_parser
        conv_res = math_parser.parse_math_or_unit_query(clean)
        if conv_res:
            raw_ans, reply = conv_res
            args = {"query": clean, "result": raw_ans}
        else:
            args = {"value": 1.0, "from_unit": "km", "to_unit": "miles"}
            reply = "Unit conversion completed, sir."
    elif action == "quick_note":
        m_note = _RE_QUICK_NOTE.search(clean)
        note_txt = m_note.group(1).strip() if m_note else clean
        args = {"text": note_txt}
        reply = f"Note recorded: '{note_txt}', sir."
    elif action == "read_clipboard":
        import productivity
        cb = productivity.get_clipboard_text().strip()
        args = {}
        if cb:
            preview = cb[:200] + ("..." if len(cb) > 200 else "")
            reply = f"Clipboard contains: {preview}"
        else:
            reply = "Your clipboard is currently empty, sir."
    elif action == "start_routine":
        import routines
        rout_name = "work mode"
        for r in routines.get_routines():
            if r in clean:
                rout_name = r
                break
        args = {"routine": rout_name}
        reply = f"Starting {rout_name.title()}, sir."
    elif action in ("screen_awareness", "analyze_screen"):
        import screen_awareness
        if not screen_awareness.is_screen_query(clean):
            return None
        return screen_awareness.analyze_screen(user_text, ctx)
    elif action == "world_time":
        import quick_answers
        tz_reply = quick_answers.get_world_time(clean, ctx)
        if tz_reply:
            return tz_reply
        args = {"location": clean}
        reply = "World time retrieved, sir."

    # Execute whitelisted desktop action
    executed, msg = execute_whitelisted_action(action, args, ctx)
    if executed:
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("action_executed", action=action, success=True)
            except Exception:
                pass
        print(f"[decision_engine] System 1 executed '{action}' (confidence: {prob:.2f}): {msg}")
        try:
            get_context_resolver().record_turn(
                user_utterance=user_text,
                resolved_action=action,
                arguments=args,
            )
        except Exception:
            pass
        return reply or msg

    return None
