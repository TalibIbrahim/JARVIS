"""
chained_commands.py — Compound Utterance Parser & Sequential Executor for J.A.R.V.I.S.

Provides:
- Splits compound speech utterances (e.g. "open my programming folder and launch VS Code",
  "volume up and maximize window") using conjunctions ("and", "then", "and then", "after that")
  and executes each command sequentially.
- Protects conjunctions within queries, notes, dictation, and time durations:
    - "search for cats and dogs" -> NOT split
    - "note that buy bread and butter" -> NOT split
    - "type yes and enter" -> NOT split
    - "timer for 2 minutes and 30 seconds" -> NOT split
- Pure Python; unit-testable behind interfaces; strictly NO Qt imports.
"""

from __future__ import annotations

import re
from typing import Any, Callable, List, Optional, Tuple

import jarvis_log as _jl


MAX_CHAIN_COMMANDS = 5
SKIPPED_NOTE = "I'll only do that on its own, sir."

# Command prefixes whose arguments must never be split by conjunctions
_UNSPLITTABLE_PREFIXES = (
    "search for ",
    "search ",
    "google ",
    "look up ",
    "note that ",
    "take a note ",
    "take note ",
    "write down ",
    "add note ",
    "type ",
    "remind me to ",
)

# Strong conjunction regexes in descending order of precedence
_CONJUNCTION_PATTERNS = [
    r"\s+and\s+then\s+",
    r"\s*,\s*then\s+",
    r"\s+then\s+",
    r"\s+after\s+that\s+",
    r"\s*;\s*",
    r"\s*,\s*and\s+",
    r"\s+also\s+",
    r"\s+and\s+",
]


def _protect_tokens(text: str) -> Tuple[str, List[str]]:
    """Protects internal conjunctions (e.g. time '2 minutes and 30 seconds') with tokens."""
    protected = text
    tokens: List[str] = []

    # Protect time conjunctions: "X minutes and Y seconds"
    def _time_repl(match):
        token = f"__TIME_TOKEN_{len(tokens)}__"
        tokens.append(match.group(0))
        return token

    protected = re.sub(
        r"(\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?))\s+and\s+(\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?))",
        _time_repl,
        protected,
        flags=re.IGNORECASE,
    )

    # Protect "between X and Y"
    def _between_repl(match):
        token = f"__TIME_TOKEN_{len(tokens)}__"
        tokens.append(match.group(0))
        return token

    protected = re.sub(
        r"\bbetween\s+\S+\s+and\s+\S+\b",
        _between_repl,
        protected,
        flags=re.IGNORECASE,
    )

    return protected, tokens


def _restore_tokens(text: str, tokens: List[str]) -> str:
    """Restores protected conjunction tokens."""
    res = text
    for i, tok in enumerate(tokens):
        res = res.replace(f"__TIME_TOKEN_{i}__", tok)
    return res


_SITE_SEARCH_TAIL_RE: Optional["re.Pattern[str]"] = None


def _split_after_site_search(text: str) -> Tuple[Optional[str], Optional[str]]:
    """('search X on youtube', 'open spotify') for 'search X on youtube and then open spotify', else (None, None).
    The head must parse as a site search on its own; the conjunction must directly follow the site name."""
    global _SITE_SEARCH_TAIL_RE
    try:
        from commands import SITE_SEARCH_ALIASES, parse_site_search
    except Exception:
        return None, None
    if _SITE_SEARCH_TAIL_RE is None:
        alt = "|".join(re.escape(a) for a in sorted(SITE_SEARCH_ALIASES, key=len, reverse=True))
        _SITE_SEARCH_TAIL_RE = re.compile(
            r"\s+(?:on|in|at|from|using|via)\s+(?:the\s+)?(?:" + alt + r")(?:\s+(?:website|site|app|store))?"
            r"(?=\s*(?:,\s*and\s+then|,\s*then|,\s*and|and\s+then|then|after\s+that|also|and|;)\s+\S)",
            re.IGNORECASE)
    for m in _SITE_SEARCH_TAIL_RE.finditer(text):
        head = text[:m.end()].strip(" ,.!?")
        rest = re.sub(r"^\s*(?:,\s*and\s+then|,\s*then|,\s*and|and\s+then|then|after\s+that|also|and|;)\s+", "",
                      text[m.end():], flags=re.IGNORECASE).strip(" ,.!?")
        if rest and parse_site_search(head):
            return head, rest
    # "youtube search lofi and then open spotify": a strong conjunction ends the query of any site search form.
    for m in _STRONG_CONJ_RE.finditer(text):
        head, rest = text[:m.start()].strip(" ,.!?"), text[m.end():].strip(" ,.!?")
        if rest and parse_site_search(head):
            return head, rest
    return None, None


_STRONG_CONJ_RE = re.compile(r"\s*,?\s+(?:and\s+then|then|after\s+that)\s+|\s*;\s*", re.IGNORECASE)


def split_chained_commands(utterance: str) -> List[str]:
    """
    Splits a compound utterance into a sequence of individual command strings.
    Returns [utterance] if it represents a single atomic command.
    """
    clean = utterance.strip()
    if not clean:
        return []

    lower = clean.lower()

    # 1. A site search's query ends at "on <site>": "search X on youtube and then open spotify" is two
    #    commands, while "watch tom and jerry on youtube" (site at the very end) is one.
    head, rest = _split_after_site_search(clean) if len(clean) <= 1000 else (None, None)
    if head and rest:
        return [head] + split_chained_commands(rest)[:MAX_CHAIN_COMMANDS - 1]
    try:
        from commands import parse_site_search
        if parse_site_search(clean):
            return [clean]
    except Exception:
        pass

    # 2. Do not split queries, dictations, or notes that naturally contain "and"
    for prefix in _UNSPLITTABLE_PREFIXES:
        if lower.startswith(prefix):
            return [clean]

    # 3. Protect valid non-splitting conjunctions (times, ranges)
    protected, tokens = _protect_tokens(clean)

    # 4. Try splitting across conjunction patterns
    for pattern in _CONJUNCTION_PATTERNS:
        parts = re.split(pattern, protected, flags=re.IGNORECASE)
        # Check if parts look like plausible commands
        cleaned_parts = [_restore_tokens(p.strip(" ,.!?"), tokens) for p in parts if p.strip(" ,.!?")]
        if len(cleaned_parts) >= 2:
            # Validate that each piece has substance (at least 2 words or a known 1-word command)
            valid = True
            for part in cleaned_parts:
                words = part.split()
                if not words:
                    valid = False
                    break
                # Single word must be an actionable command like 'mute', 'skip', 'unmute', 'status'
                if len(words) == 1 and words[0].lower() not in (
                    "mute", "unmute", "skip", "pause", "resume", "play",
                    "status", "diagnostics", "lock", "screenshot", "restart",
                ):
                    valid = False
                    break

            if valid:
                return cleaned_parts

    return [clean]


def execute_chained_commands(
    compound_text: str,
    ctx: Any,
    execute_single_fn: Callable[[str, Any], Optional[str]],
    is_gated_fn: Optional[Callable[[str], bool]] = None,
) -> Tuple[bool, str, List[Tuple[str, str]]]:
    """
    Splits compound text and executes each sub-command sequentially via execute_single_fn.
    Power/standby/quit and destructive sub-commands (is_gated_fn True) are NEVER executed inside a
    chain: they are skipped with a short spoken note. At most MAX_CHAIN_COMMANDS run.
    Returns (success: bool, combined_reply: str, execution_log: List[(command, reply)]).
    """
    commands = split_chained_commands(compound_text)
    if len(commands) <= 1:
        reply = execute_single_fn(compound_text, ctx)
        return bool(reply), reply or "", [(compound_text, reply or "")]

    if is_gated_fn is None:
        from commands import is_chain_gated as is_gated_fn

    if len(commands) > MAX_CHAIN_COMMANDS:
        _jl.get_logger("commands").warning(
            "chain of %d sub-commands capped at %d", len(commands), MAX_CHAIN_COMMANDS)
        commands = commands[:MAX_CHAIN_COMMANDS]

    replies: List[str] = []
    log: List[Tuple[str, str]] = []
    skipped_noted = False

    for cmd in commands:
        if is_gated_fn(cmd):
            _jl.get_logger("commands").warning("power/destructive sub-command skipped in chain")
            log.append((cmd, SKIPPED_NOTE))
            if not skipped_noted:
                replies.append(SKIPPED_NOTE)
                skipped_noted = True
            continue
        try:
            cmd_reply = execute_single_fn(cmd, ctx)
            if cmd_reply:
                replies.append(cmd_reply.strip())
                log.append((cmd, cmd_reply.strip()))
            else:
                log.append((cmd, "No reply"))
        except Exception as exc:
            err = f"Failed to execute '{cmd}': {exc}"
            replies.append(err)
            log.append((cmd, err))

    combined = " ".join(replies) if replies else "Executed chained commands, sir."
    return True, combined, log
