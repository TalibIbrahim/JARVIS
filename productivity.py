"""
productivity.py — Quick Notes, Dictation & Clipboard Adapter for J.A.R.V.I.S.

Provides:
- Quick Notes:
    - Appends timestamped entries to a persistent markdown file (default: ~/Documents/Jarvis_Notes.md).
    - Voice commands: "note that <text>", "take a note <text>", "write down <text>", "read last note".
- Dictation:
    - "type <text>" inserts text into active field via pyautogui.
    - Dictation mode toggle: "start dictation" / "dictation mode on", "stop dictation" / "dictation mode off".
- Clipboard:
    - Read clipboard text ("read clipboard", "what's on my clipboard").
    - Offline extractive summarization & translation hooks.
- 100% unit-testable behind interfaces; strictly NO Qt imports.
"""

from __future__ import annotations

import datetime
import os
import re
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

try:
    import pyautogui
except ImportError:
    pyautogui = None


# =====================================================================
# Quick Notes Engine
# =====================================================================

def get_default_notes_path(cfg: Any = None) -> str:
    """Returns path to the markdown notes file from config or default."""
    if cfg and hasattr(cfg, "notes_file_path") and cfg.notes_file_path:
        return os.path.abspath(cfg.notes_file_path)
    try:
        from config import load_config
        c = load_config()
        if hasattr(c, "notes_file_path") and c.notes_file_path:
            return os.path.abspath(c.notes_file_path)
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents", "Jarvis_Notes.md")


def add_note(text: str, notes_path: Optional[str] = None, timestamp: Optional[datetime.datetime] = None) -> Tuple[bool, str]:
    """
    Appends a timestamped note entry to the configured markdown file.
    Returns (success: bool, message: str).
    """
    clean_text = text.strip()
    if not clean_text:
        return False, "Note text cannot be empty, sir."

    path = notes_path or get_default_notes_path()
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)

        dt = timestamp or datetime.datetime.now()
        ts_header = dt.strftime("%Y-%m-%d %H:%M:%S")

        is_new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8") as f:
            if is_new_file:
                f.write("# J.A.R.V.I.S. Quick Notes\n\n")
            f.write(f"### {ts_header}\n- {clean_text}\n\n")

        return True, f"Note recorded: '{clean_text}'"
    except Exception as exc:
        return False, f"Failed to record note: {exc}"


def read_notes(max_entries: int = 3, notes_path: Optional[str] = None) -> Tuple[bool, str, List[str]]:
    """
    Reads recent entries from the notes markdown file.
    Returns (success: bool, spoken_summary: str, list_of_entries: List[str]).
    """
    path = notes_path or get_default_notes_path()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False, "You have no recorded notes, sir.", []

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # Split on headers: ### YYYY-MM-DD
        chunks = re.split(r"(?m)^###\s+", content)
        entries = []
        for c in chunks:
            c = c.strip()
            if not c or c.startswith("# J.A.R.V.I.S."):
                continue
            lines = [line.strip("- ").strip() for line in c.splitlines() if line.strip()]
            if lines:
                ts = lines[0]
                body = " ".join(lines[1:]) if len(lines) > 1 else ""
                entry_str = f"On {ts}: {body}" if body else ts
                entries.append(entry_str)

        if not entries:
            return False, "You have no recorded notes, sir.", []

        recent = entries[-max_entries:]
        spoken = "Here are your recent notes: " + "; ".join(recent) + "."
        return True, spoken, recent
    except Exception as exc:
        return False, f"Failed to read notes: {exc}", []


def get_last_note(notes_path: Optional[str] = None) -> Tuple[bool, str]:
    """Returns the most recently recorded note."""
    ok, _, entries = read_notes(max_entries=1, notes_path=notes_path)
    if ok and entries:
        return True, f"Your last note was: {entries[-1]}, sir."
    return False, "You have no recorded notes, sir."


# =====================================================================
# Dictation Engine
# =====================================================================

_DICTATION_MODE: bool = False
_DICTATION_LOCK = threading.Lock()


def is_dictation_mode() -> bool:
    """Returns True if dictation mode is actively on."""
    with _DICTATION_LOCK:
        return _DICTATION_MODE


def set_dictation_mode(enabled: bool) -> bool:
    """Enables or disables continuous dictation mode."""
    global _DICTATION_MODE
    with _DICTATION_LOCK:
        _DICTATION_MODE = bool(enabled)
        return _DICTATION_MODE


def type_text(text: str, use_paste_fallback: bool = True) -> bool:
    """
    Types text into the active Windows focused field.
    Uses pyautogui.write for ASCII and clipboard paste (Ctrl+V) for unicode/multiline.
    """
    clean_text = text.strip()
    if not clean_text:
        return False

    try:
        if pyautogui is None:
            return False

        # Check if text contains non-ASCII characters or special punctuation
        is_pure_ascii = all(32 <= ord(c) < 127 for c in clean_text)
        if is_pure_ascii and not use_paste_fallback:
            pyautogui.write(clean_text, interval=0.01)
            return True

        # Clipboard paste approach: preserves emojis, formatting, and unicode accurately
        set_clipboard_text(clean_text)
        pyautogui.hotkey("ctrl", "v", _pause=False)
        return True
    except Exception as exc:
        print(f"[productivity] type_text note: {exc}")
        return False


# =====================================================================
# Clipboard Adapter & Summarization / Translation Hooks
# =====================================================================

_CLIPBOARD_BACKEND: Optional[Callable[[], str]] = None


def set_clipboard_backend(backend: Optional[Callable[[], str]]) -> None:
    """Overrides clipboard reader with custom callable for unit tests."""
    global _CLIPBOARD_BACKEND
    _CLIPBOARD_BACKEND = backend


def get_clipboard_text() -> str:
    """
    Retrieves the current text content from the Windows clipboard.
    Thread-safe with zero Qt dependencies.
    """
    if _CLIPBOARD_BACKEND is not None:
        return _CLIPBOARD_BACKEND()

    # Try pyperclip if available
    try:
        import pyperclip
        txt = pyperclip.paste()
        if txt is not None and isinstance(txt, str):
            return txt
    except Exception:
        pass

    # Native Win32 user32 clipboard fallback
    if sys.platform == "win32":
        try:
            import ctypes
            CF_UNICODETEXT = 13
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            if u32.OpenClipboard(None):
                try:
                    handle = u32.GetClipboardData(CF_UNICODETEXT)
                    if handle:
                        ptr = k32.GlobalLock(handle)
                        if ptr:
                            try:
                                return ctypes.c_wchar_p(ptr).value or ""
                            finally:
                                k32.GlobalUnlock(handle)
                finally:
                    u32.CloseClipboard()
        except Exception:
            pass

    return ""


def set_clipboard_text(text: str) -> bool:
    """Writes text to the Windows clipboard."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    return False


def summarize_text(text: str, max_sentences: int = 3) -> str:
    """
    Offline extractive summarization algorithm.
    Splits text into sentences, scores by word frequencies, and returns top sentences in order.
    """
    clean = text.strip()
    if not clean:
        return "Clipboard is empty, sir."

    sentences = re.split(r"(?<=[.!?])\s+", clean)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
    if len(sentences) <= max_sentences:
        return clean

    # Token frequency scoring
    words = re.findall(r"\b[a-zA-Z]{3,}\b", clean.lower())
    stop_words = {
        "the", "and", "that", "have", "for", "not", "with", "you", "this", "but", "his",
        "from", "they", "say", "her", "she", "will", "one", "all", "would", "there", "their",
    }
    freq: Dict[str, int] = {}
    for w in words:
        if w not in stop_words:
            freq[w] = freq.get(w, 0) + 1

    scored = []
    for idx, s in enumerate(sentences):
        score = sum(freq.get(w, 0) for w in re.findall(r"\b[a-zA-Z]{3,}\b", s.lower()))
        scored.append((score, idx, s))

    # Pick top N sentences by score
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_sentences]
    # Re-sort into original appearance order
    top.sort(key=lambda x: x[1])

    summary = " ".join(item[2] for item in top)
    return summary


class TranslatorProtocol(Protocol):
    def translate(self, text: str, target_language: str) -> str:
        ...


_TRANSLATOR_HOOK: Optional[Callable[[str, str], str]] = None


def set_translator_hook(hook: Optional[Callable[[str, str], str]]) -> None:
    """Sets pluggable translation backend."""
    global _TRANSLATOR_HOOK
    _TRANSLATOR_HOOK = hook


def translate_text(text: str, target_language: str) -> str:
    """
    Translates text to target language using registered translator hook or returns placeholder.
    """
    if _TRANSLATOR_HOOK:
        return _TRANSLATOR_HOOK(text, target_language)
    return f"Translation of clipboard to {target_language.capitalize()} requires active translation service."


# =====================================================================
# Command Dispatcher
# =====================================================================

def handle_productivity_command(text: str, ctx: Any = None, cfg: Any = None) -> Optional[str]:
    """
    Evaluates productivity utterances:
      - "note that <text>", "take a note <text>", "write down <text>"
      - "read last note", "read my notes", "show my notes"
      - "type <text>"
      - "start dictation", "stop dictation"
      - "read clipboard", "what's on my clipboard"
      - "summarize clipboard", "translate clipboard to <lang>"
    Returns spoken reply if matched, or None.
    """
    clean = text.lower().strip(" ,.!?")

    # 1. Quick Notes: Take Note
    m_note = re.match(r"^(?:take\s+a\s+note|take\s+note|note\s+that|write\s+down|add\s+note)\s+(?:that\s+)?(.+)", text, re.IGNORECASE)
    if m_note:
        note_body = m_note.group(1).strip()
        ok, msg = add_note(note_body)
        if ok:
            return f"Note recorded, sir: '{note_body}'"
        return f"Unable to record note: {msg}"

    # 2. Read Notes
    if any(q in clean for q in ("read last note", "what was my last note", "tell me my last note")):
        ok, reply = get_last_note()
        return reply

    if any(q in clean for q in ("read my notes", "read notes", "show my notes", "show notes", "what are my notes")):
        ok, reply, _ = read_notes(max_entries=3)
        return reply

    # 3. Dictation Mode Toggle
    if any(q in clean for q in ("start dictation", "begin dictation", "dictation mode on", "enable dictation")):
        set_dictation_mode(True)
        return "Dictation mode activated, sir. Speak and I will type for you. Say 'stop dictation' when finished."

    if any(q in clean for q in ("stop dictation", "end dictation", "dictation mode off", "disable dictation", "exit dictation")):
        set_dictation_mode(False)
        return "Dictation mode deactivated, sir."

    # 4. Dictation: "type <text>"
    m_type = re.match(r"^type\s+(.+)", text, re.IGNORECASE)
    if m_type:
        to_type = m_type.group(1).strip()
        if to_type:
            type_text(to_type)
            return f"Typed: '{to_type}'"

    # 5. Clipboard: Read
    if any(q in clean for q in ("read clipboard", "what's on my clipboard", "what is on my clipboard", "read what is on the clipboard", "clipboard content")):
        cb = get_clipboard_text().strip()
        if not cb:
            return "Your clipboard is currently empty, sir."
        preview = cb[:200] + ("..." if len(cb) > 200 else "")
        return f"Clipboard contains: {preview}"

    # 6. Clipboard: Summarize
    if any(q in clean for q in ("summarize clipboard", "summarize my clipboard", "summarize copied text", "summarize the clipboard")):
        cb = get_clipboard_text().strip()
        if not cb:
            return "Your clipboard is empty, sir."
        summary = summarize_text(cb)
        return f"Clipboard summary: {summary}"

    # 7. Clipboard: Translate
    m_trans = re.search(r"translate\s+(?:my\s+)?clipboard\s+to\s+([a-zA-Z]+)", clean)
    if m_trans:
        target_lang = m_trans.group(1).strip()
        cb = get_clipboard_text().strip()
        if not cb:
            return "Your clipboard is empty, sir."
        res = translate_text(cb, target_lang)
        return res

    return None
