"""
Context Memory & Disambiguation Logic for J.A.R.V.I.S.

Strictly NO Qt imports. 100% unit-testable with pure Python and mocks.

Provides:
- In-memory turn context queue (last ~5 turns in RAM only, never persisted to disk).
- Entity extraction & referential resolution:
  * "open it" -> resolves against the last mentioned entity (app, site, folder, file).
  * "make that louder" / "louder" / "quieter" / "turn it down" -> volume / media actions.
  * "do that again" -> repeats the last executed action with previous arguments.
  * "no, the other one" -> switches to secondary candidate from previous ambiguous turn.
- Disambiguation Engine:
  * Manages active disambiguation sessions for closely scored candidates (margin <= 0.08).
  * Dispatches `overlay.post("disambiguate", candidates=[...])`.
  * Resolves user selection via:
    - Voice numbers ("one" / "first" / "number one", "two" / "second" / "number two", "three" / "third" / "number three").
    - Spoken candidate label.
    - UI click / dwell selection callback.
"""

from collections import deque
from dataclasses import dataclass, field
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union


# Standard application names known across J.A.R.V.I.S.
KNOWN_APPS = [
    "chrome", "google chrome", "edge", "firefox", "notepad", "calculator",
    "paint", "file explorer", "explorer", "files", "terminal", "command prompt",
    "cmd", "code", "vs code", "visual studio code", "settings", "task manager",
    "spotify", "word", "excel", "powerpoint",
]

# Standard website shortcuts
KNOWN_SITES = [
    "youtube", "google", "github", "gmail", "maps", "linkedin",
]

# Standard Windows user folders
KNOWN_FOLDERS = [
    "downloads", "documents", "pictures", "desktop", "music", "videos", "programming",
]

# Common file extensions for entity extraction
FILE_EXTENSIONS = (
    ".txt", ".pdf", ".py", ".png", ".jpg", ".jpeg", ".doc", ".docx",
    ".xls", ".xlsx", ".csv", ".json", ".zip", ".mp3", ".mp4",
)


@dataclass
class Candidate:
    """Represents an actionable candidate in a disambiguation or ranked list."""
    index: int  # 1-based (1, 2, 3)
    action: str
    args: Dict[str, Any] = field(default_factory=dict)
    label: str = ""
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "args": dict(self.args),
            "label": self.label,
            "confidence": round(self.confidence, 4),
        }


@dataclass
class TurnContext:
    """
    Represents a single conversational turn in volatile memory.
    Never persisted to disk.
    """
    user_utterance: str
    resolved_action: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    entities: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    candidates: List[Candidate] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_utterance": self.user_utterance,
            "resolved_action": self.resolved_action,
            "arguments": dict(self.arguments),
            "entities": dict(self.entities),
            "timestamp": self.timestamp,
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass
class ResolutionResult:
    """Result of referential or contextual resolution."""
    action: str
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    label: str = ""
    reply: str = ""
    candidate: Optional[Candidate] = None
    candidates: List[Candidate] = field(default_factory=list)


def extract_entities_from_text(text: str) -> Dict[str, Any]:
    """
    Extracts mentioned apps, sites, folders, files, and media from raw text.
    """
    entities: Dict[str, Any] = {}
    clean = text.lower().strip()

    # 1. Apps
    for app in sorted(KNOWN_APPS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(app)}\b", clean):
            entities["app"] = app
            break

    # 2. Sites
    for site in sorted(KNOWN_SITES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(site)}\b", clean):
            entities["site"] = site
            break

    # 3. Folders
    for folder in KNOWN_FOLDERS:
        if re.search(rf"\b{re.escape(folder)}(?:\s+folder)?\b", clean):
            entities["folder"] = folder
            break

    # 4. Files (e.g. "report.pdf", "main.py")
    file_match = re.search(r"\b([a-zA-Z0-9_\-]+\.(?:txt|pdf|py|png|jpg|jpeg|doc|docx|xls|xlsx|csv|json|zip))\b", clean)
    if file_match:
        entities["file"] = file_match.group(1)

    # 5. Media queries (e.g. "play drake", "listen to the weeknd")
    media_match = re.search(r"(?:play|put on|listen to)\s+(?:songs? by\s+)?(.+?)(?:\s+on\s+(?:spotify|youtube))?$", clean)
    if media_match:
        target = media_match.group(1).strip()
        if target not in ("music", "the music", "track", "song", "audio"):
            entities["media"] = target

    return entities


class ContextResolver:
    """
    Manages the short-term working context memory (last ~5 turns)
    in RAM only, providing fast referential resolution.
    """

    def __init__(self, max_turns: int = 5):
        self._max_turns = max_turns
        self._turns: deque[TurnContext] = deque(maxlen=max_turns)
        self._lock = threading.RLock()

    @property
    def max_turns(self) -> int:
        return self._max_turns

    def record_turn(
        self,
        user_utterance: str,
        resolved_action: Optional[str] = None,
        arguments: Optional[Dict[str, Any]] = None,
        entities: Optional[Dict[str, Any]] = None,
        candidates: Optional[List[Union[Candidate, Dict[str, Any]]]] = None,
        timestamp: Optional[float] = None,
    ) -> TurnContext:
        """
        Records a new turn into volatile in-memory deque. Never touches disk.
        """
        with self._lock:
            args = dict(arguments) if arguments else {}

            # Build entities dictionary: auto-extract if not explicitly given or merge
            extracted = extract_entities_from_text(user_utterance)
            if resolved_action == "open_app" and "app" in args:
                extracted["app"] = args["app"]
            elif resolved_action == "open_site" and "site" in args:
                extracted["site"] = args["site"]
            elif resolved_action == "open_folder":
                folder_val = args.get("folder") or args.get("path")
                if folder_val:
                    extracted["folder"] = folder_val
            elif resolved_action in ("open_file", "open_recent_file", "show_last_download"):
                file_val = args.get("file") or args.get("path")
                if file_val:
                    extracted["file"] = file_val
            elif resolved_action in ("play_music", "media") and "query" in args:
                extracted["media"] = args["query"]

            if entities:
                extracted.update(entities)

            # Normalize candidates list
            norm_cands: List[Candidate] = []
            if candidates:
                for i, c in enumerate(candidates):
                    if isinstance(c, Candidate):
                        norm_cands.append(c)
                    elif isinstance(c, dict):
                        norm_cands.append(
                            Candidate(
                                index=c.get("index", i + 1),
                                action=c.get("action", ""),
                                args=c.get("args", {}),
                                label=c.get("label", ""),
                                confidence=c.get("confidence", 0.0),
                            )
                        )

            turn = TurnContext(
                user_utterance=user_utterance,
                resolved_action=resolved_action,
                arguments=args,
                entities=extracted,
                timestamp=timestamp or time.time(),
                candidates=norm_cands,
            )
            self._turns.append(turn)
            return turn

    def get_history(self) -> List[TurnContext]:
        """Returns a shallow copy of active turns in memory."""
        with self._lock:
            return list(self._turns)

    def get_last_turn(self, require_action: bool = False) -> Optional[TurnContext]:
        """Returns the most recent turn, optionally requiring resolved_action."""
        with self._lock:
            for turn in reversed(self._turns):
                if require_action:
                    if turn.resolved_action and turn.resolved_action not in ("none", "conversation"):
                        return turn
                else:
                    return turn
            return None

    def get_last_entity(
        self,
        entity_types: Tuple[str, ...] = ("app", "site", "folder", "file"),
    ) -> Optional[Tuple[str, Any]]:
        """
        Scans backwards through turns to find the most recently mentioned entity
        matching any of the requested entity_types.
        Returns (entity_type, entity_value) or None.
        """
        with self._lock:
            for turn in reversed(self._turns):
                for etype in entity_types:
                    if etype in turn.entities and turn.entities[etype]:
                        return etype, turn.entities[etype]
            return None

    def resolve_referential(self, utterance: str) -> Optional[ResolutionResult]:
        """
        Attempts to resolve anaphoric or referential expressions against context:
        - "open it" -> resolves against last mentioned entity (app, site, folder, file).
        - "make that louder" / "louder" / "quieter" / "turn it down" -> volume actions.
        - "do that again" -> repeats last executed action with previous args.
        - "no, the other one" -> switches to secondary candidate from previous ambiguous turn.
        """
        clean = utterance.lower().strip(" ,.!?")

        # 1. "open it" / "launch it" / "open that"
        if re.search(r"^(?:please\s+)?(?:open|launch|start|run|bring\s+up)\s+(?:it|that|this)(?:\s+please)?$", clean):
            last_ent = self.get_last_entity(("app", "site", "folder", "file"))
            if last_ent:
                etype, val = last_ent
                if etype == "app":
                    return ResolutionResult(
                        action="open_app",
                        args={"app": val},
                        reason="referential_open_app",
                        label=f"Open {val.title()}",
                        reply=f"Opening {val}, sir.",
                    )
                elif etype == "site":
                    return ResolutionResult(
                        action="open_site",
                        args={"site": val},
                        reason="referential_open_site",
                        label=f"Open {val.title()}",
                        reply=f"Opening {val}, sir.",
                    )
                elif etype == "folder":
                    return ResolutionResult(
                        action="open_folder",
                        args={"folder": val},
                        reason="referential_open_folder",
                        label=f"Open {val.title()} Folder",
                        reply=f"Opening {val} folder, sir.",
                    )
                elif etype == "file":
                    return ResolutionResult(
                        action="open_file",
                        args={"file": val},
                        reason="referential_open_file",
                        label=f"Open {val}",
                        reply=f"Opening {val}, sir.",
                    )

        # 2. Volume adjustments: "make that louder" / "louder"
        if re.search(r"^(?:make\s+(?:that|it)\s+louder|turn\s+(?:that|it)\s+up|a\s+bit\s+louder|louder(?:\s+please)?|crank\s+(?:it|that)\s+up|bump\s+it\s+up)$", clean):
            return ResolutionResult(
                action="volume_up",
                args={"steps": 5},
                reason="referential_volume_up",
                label="Volume Up",
                reply="Adjusting the volume upward for you, sir.",
            )

        # Volume adjustments: "quieter" / "turn it down" / "make that quieter"
        if re.search(r"^(?:make\s+(?:that|it)\s+quieter|turn\s+(?:that|it)\s+down|a\s+bit\s+quieter|quieter(?:\s+please)?|lower\s+(?:it|that)|bring\s+it\s+down|softer(?:\s+please)?)$", clean):
            return ResolutionResult(
                action="volume_down",
                args={"steps": 5},
                reason="referential_volume_down",
                label="Volume Down",
                reply="Lowering the volume, sir.",
            )

        # 3. "do that again" / "repeat that" / "once more"
        if re.search(r"^(?:do\s+(?:that|it)\s+again|repeat\s+that|repeat(?:\s+last)?|once\s+more|one\s+more\s+time|run\s+that\s+again|again|repeat\s+action)$", clean):
            last_turn = self.get_last_turn(require_action=True)
            if last_turn and last_turn.resolved_action:
                act = last_turn.resolved_action
                args = dict(last_turn.arguments)
                friendly_name = act.replace("_", " ")
                return ResolutionResult(
                    action=act,
                    args=args,
                    reason="repeat_action",
                    label=f"Repeat {friendly_name.title()}",
                    reply=f"Repeating {friendly_name}, sir.",
                    candidates=last_turn.candidates,
                )

        # 4. "no, the other one" / "the other one" / "the other option"
        if re.search(r"^(?:no[,\s]+)?(?:the\s+other\s+one|the\s+other\s+option|the\s+second\s+one|try\s+the\s+other\s+one|switch\s+to\s+the\s+other\s+one|the\s+other|other\s+one)$", clean):
            with self._lock:
                for turn in reversed(self._turns):
                    if turn.candidates and len(turn.candidates) >= 2:
                        # Find the secondary candidate
                        cand0 = turn.candidates[0]
                        cand1 = turn.candidates[1]
                        # If candidate 0 was the one resolved or tried, pick candidate 1
                        alt = cand1 if turn.resolved_action == cand0.action else (cand0 if turn.resolved_action == cand1.action else cand1)
                        return ResolutionResult(
                            action=alt.action,
                            args=dict(alt.args),
                            reason="alternative_candidate",
                            label=alt.label or alt.action,
                            reply=f"Switching to {alt.label or alt.action.replace('_', ' ')}, sir.",
                            candidate=alt,
                            candidates=turn.candidates,
                        )

        return None

    def clear(self) -> None:
        """Flushes context memory queue."""
        with self._lock:
            self._turns.clear()


@dataclass
class DisambiguationSession:
    """Represents an active disambiguation prompt waiting for user selection."""
    session_id: str
    candidates: List[Candidate]
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    is_active: bool = True
    selected_candidate: Optional[Candidate] = None
    overlay: Any = None


class DisambiguationEngine:
    """
    Manages close-candidate disambiguation states.
    Dispatches to overlay and resolves selections via voice numbers, labels, or UI callbacks.
    """

    def __init__(self, timeout: float = 30.0):
        self._default_timeout = timeout
        self._current_session: Optional[DisambiguationSession] = None
        self._lock = threading.RLock()

    def start_session(
        self,
        candidates: List[Union[Candidate, Dict[str, Any]]],
        overlay: Any = None,
        timeout: Optional[float] = None,
    ) -> DisambiguationSession:
        """
        Starts a disambiguation session with top 2-3 candidates.
        Notifies overlay via overlay.post("disambiguate", candidates=[...]).
        """
        with self._lock:
            norm_cands: List[Candidate] = []
            for i, c in enumerate(candidates[:3]):  # Limit to top 2-3
                idx = i + 1
                if isinstance(c, Candidate):
                    c.index = idx
                    norm_cands.append(c)
                elif isinstance(c, dict):
                    norm_cands.append(
                        Candidate(
                            index=idx,
                            action=c.get("action", ""),
                            args=c.get("args", {}),
                            label=c.get("label", c.get("action", f"Option {idx}")),
                            confidence=c.get("confidence", 0.0),
                        )
                    )

            now = time.time()
            ttl = timeout or self._default_timeout
            session = DisambiguationSession(
                session_id=f"dis_{int(now * 1000)}",
                candidates=norm_cands,
                created_at=now,
                expires_at=now + ttl,
                is_active=True,
                overlay=overlay,
            )
            self._current_session = session

            # Notify overlay if available
            if overlay and hasattr(overlay, "post"):
                try:
                    payload = [c.to_dict() for c in norm_cands]
                    overlay.post("disambiguate", candidates=payload)
                except Exception as exc:
                    print(f"[disambiguation] overlay post note: {exc}")

            return session

    def is_active(self) -> bool:
        """Returns True if there is an unexpired active disambiguation session."""
        with self._lock:
            if not self._current_session or not self._current_session.is_active:
                return False
            if time.time() > self._current_session.expires_at:
                self._current_session.is_active = False
                return False
            return True

    def get_active_session(self) -> Optional[DisambiguationSession]:
        with self._lock:
            if self.is_active():
                return self._current_session
            return None

    def cancel(self, reason: str = "cancelled") -> None:
        """Cancels active disambiguation session."""
        with self._lock:
            if self._current_session and self._current_session.is_active:
                self._current_session.is_active = False
                if self._current_session.overlay and hasattr(self._current_session.overlay, "post"):
                    try:
                        self._current_session.overlay.post("disambiguate_closed", reason=reason)
                    except Exception:
                        pass

    def complete(self, candidate: Candidate) -> Candidate:
        """Marks active session completed with the chosen candidate."""
        with self._lock:
            if self._current_session:
                self._current_session.selected_candidate = candidate
                self._current_session.is_active = False
                if self._current_session.overlay and hasattr(self._current_session.overlay, "post"):
                    try:
                        self._current_session.overlay.post(
                            "disambiguate_resolved", selected=candidate.to_dict()
                        )
                    except Exception:
                        pass
            return candidate

    def resolve_selection(self, utterance: str) -> Optional[Candidate]:
        """
        Resolves user utterance against active candidates:
        - Voice numbers: "one" / "first" / "number one", "two" / "second" / "number two", "three" / "third" / "number three".
        - Spoken candidate label.
        - Cancellation commands: "cancel", "never mind", "neither".
        """
        with self._lock:
            if not self.is_active() or not self._current_session:
                return None

            clean = utterance.lower().strip(" ,.!?")
            candidates = self._current_session.candidates

            # Explicit cancellation
            if clean in ("cancel", "never mind", "nevermind", "abort", "neither", "stop", "dismiss", "none of them"):
                self.cancel("user_cancelled")
                return None

            # 1. Voice numbers
            # Number 1
            if re.search(r"^(?:#?1|one|first|number\s+one|option\s+one|the\s+first(?:\s+one)?|first\s+one)$", clean):
                for c in candidates:
                    if c.index == 1:
                        return self.complete(c)
                if candidates:
                    return self.complete(candidates[0])

            # Number 2
            if re.search(r"^(?:#?2|two|to|too|second|number\s+two|option\s+two|the\s+second(?:\s+one)?|second\s+one|the\s+other\s+one|other\s+one)$", clean):
                for c in candidates:
                    if c.index == 2:
                        return self.complete(c)
                if len(candidates) >= 2:
                    return self.complete(candidates[1])

            # Number 3
            if re.search(r"^(?:#?3|three|third|number\s+three|option\s+three|the\s+third(?:\s+one)?|third\s+one)$", clean):
                for c in candidates:
                    if c.index == 3:
                        return self.complete(c)
                if len(candidates) >= 3:
                    return self.complete(candidates[2])

            # 2. Spoken candidate label / entity matching
            for c in candidates:
                label_lower = c.label.lower()
                action_lower = c.action.lower()

                # Exact or substring match on label
                if clean == label_lower or clean in label_lower or label_lower in clean:
                    return self.complete(c)

                # Match app or site argument (e.g. user says "chrome" or "google")
                for key in ("app", "site", "folder", "file", "query"):
                    val = str(c.args.get(key, "")).lower()
                    if val and (clean == val or val in clean or clean in val):
                        return self.complete(c)

                # Match action name directly (e.g. "volume up")
                if clean == action_lower.replace("_", " "):
                    return self.complete(c)

            return None

    def resolve_click(self, index: int) -> Optional[Candidate]:
        """
        UI click / dwell selection callback:
        Supports 1-based (1, 2, 3) or 0-based index.
        """
        with self._lock:
            if not self.is_active() or not self._current_session:
                return None

            candidates = self._current_session.candidates

            # Match 1-based index first
            for c in candidates:
                if c.index == index:
                    return self.complete(c)

            # Match 0-based index fallback
            if 0 <= index < len(candidates):
                return self.complete(candidates[index])

            return None


# Global singletons
_GLOBAL_CONTEXT_RESOLVER: Optional[ContextResolver] = None
_GLOBAL_DISAMBIGUATION_ENGINE: Optional[DisambiguationEngine] = None


def get_context_resolver() -> ContextResolver:
    """Returns the singleton ContextResolver instance."""
    global _GLOBAL_CONTEXT_RESOLVER
    if _GLOBAL_CONTEXT_RESOLVER is None:
        _GLOBAL_CONTEXT_RESOLVER = ContextResolver(max_turns=5)
    return _GLOBAL_CONTEXT_RESOLVER


def get_disambiguation_engine() -> DisambiguationEngine:
    """Returns the singleton DisambiguationEngine instance."""
    global _GLOBAL_DISAMBIGUATION_ENGINE
    if _GLOBAL_DISAMBIGUATION_ENGINE is None:
        _GLOBAL_DISAMBIGUATION_ENGINE = DisambiguationEngine()
    return _GLOBAL_DISAMBIGUATION_ENGINE


def reset_context() -> None:
    """Flushes context memory and cancels active disambiguation (useful for testing or standby)."""
    if _GLOBAL_CONTEXT_RESOLVER is not None:
        _GLOBAL_CONTEXT_RESOLVER.clear()
    if _GLOBAL_DISAMBIGUATION_ENGINE is not None:
        _GLOBAL_DISAMBIGUATION_ENGINE.cancel("reset")
