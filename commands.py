"""Local voice commands. No cloud agent: every command maps to a direct action on this PC."""
import datetime
import os
import re
import subprocess
import time
import webbrowser
from typing import Any

from ai import (
    get_api_key,
    GroqClient,
    DEFAULT_LLM_MODEL,
    get_conversation_manager,
    reset_ai_history,
    build_system_prompt,
    parse_ai_response,
    execute_whitelisted_action,
    AuthenticationError,
    RateLimitError,
    OfflineError,
    TimeoutError,
    AIError,
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


def _any(t, *phrases):
    return any(p in t for p in phrases)


def _launch(target):
    subprocess.Popen(f'start "" {target}', shell=True)


def run_command(text, ctx):
    """Returns the reply Jarvis should speak, or None if nothing matched."""
    import pyautogui
    t = text.lower().strip(" ,.!?")

    # --- power / mode ---
    if _any(t, "shut down jarvis", "quit jarvis", "exit jarvis", "power down", "goodbye"):
        ctx.quit()
        return "Powering down. Goodbye, sir."
    if _any(t, "go to sleep", "sleep mode", "standby", "stand by"):
        ctx.set_active(False)
        reset_ai_history()
        return "Going to standby, sir."
    if _any(t, "wake up", "activate", "come online", "i'm back", "im back", "online"):
        ctx.set_active(True)
        return "Online. At your service, sir."

    # --- ai setup ---
    if _any(t, "set up ai", "setup ai", "configure ai", "ai settings", "open ai settings"):
        if hasattr(ctx, "overlay") and ctx.overlay:
            ctx.overlay.open_settings("AI")
            return "Opening AI settings, sir."
        return "Settings overlay is unavailable, sir."

    # --- dwell & pointer modes ---
    if _any(t, "dwell on", "enable dwell", "turn on dwell", "dwell click on"):
        ctx.engine.cfg.dwell_mode = "click"
        return "Dwell click enabled, sir."
    if _any(t, "dwell off", "disable dwell", "turn off dwell", "dwell click off"):
        ctx.engine.cfg.dwell_mode = "off"
        return "Dwell click disabled, sir."
    if _any(t, "dwell menu", "dwell radial", "radial menu on", "radial mode"):
        ctx.engine.cfg.dwell_mode = "radial"
        return "Dwell radial menu enabled, sir."
    if _any(t, "precision mode on", "enable precision", "fine aim on"):
        ctx.engine.cfg.precision_mode = True
        return "Precision fine-aim mode enabled, sir."
    if _any(t, "precision mode off", "disable precision", "fine aim off"):
        ctx.engine.cfg.precision_mode = False
        return "Precision fine-aim mode disabled, sir."
    if _any(t, "relative mode", "trackpad mode"):
        ctx.engine.cfg.tracking_mode = "relative"
        return "Relative trackpad mode active, sir."
    if _any(t, "absolute mode", "screen mode"):
        ctx.engine.cfg.tracking_mode = "absolute"
        return "Absolute screen mapping active, sir."

    # --- chatter ---
    if t in ("hello", "hi", "hey", "good morning", "good evening", "good afternoon"):
        return "Welcome back, sir."
    if _any(t, "status", "report", "how are you"):
        state = "online" if ctx.engine.active else "on standby"
        return f"All systems nominal. Hand tracking is {state}, sir."
    if re.search(r"\b(time|what time|current time)\b", t):
        return "It is " + datetime.datetime.now().strftime("%I:%M %p").lstrip("0") + ", sir."
    if _any(t, "date", "what day"):
        return "Today is " + datetime.datetime.now().strftime("%A, %B %d") + "."

    # --- apps and web ---
    m = re.match(r"(?:open|launch|start)\s+(.+)", t)
    if m:
        name = m.group(1).strip()
        if name in SITES:
            webbrowser.open(SITES[name])
            return f"Opening {name}."
        if name in APPS:
            _launch(APPS[name])
            return f"Opening {name}."
        return f"I don't know how to open {name} yet, sir."
    m = re.match(r"(?:search(?: for)?|google|look up)\s+(.+)", t)
    if m:
        webbrowser.open("https://www.google.com/search?q=" + m.group(1).strip().replace(" ", "+"))
        return f"Searching for {m.group(1).strip()}."

    # --- media / volume ---
    if _any(t, "volume up", "louder"):
        pyautogui.press("volumeup", presses=5, _pause=False)
        return "Volume up."
    if _any(t, "volume down", "quieter", "lower the volume"):
        pyautogui.press("volumedown", presses=5, _pause=False)
        return "Volume down."
    if re.search(r"\b(mute|unmute)\b", t):
        pyautogui.press("volumemute", _pause=False)
        return "Muted."
    if _any(t, "play", "pause", "resume"):
        pyautogui.press("playpause", _pause=False)
        return "Done."
    if _any(t, "next track", "next song", "skip"):
        pyautogui.press("nexttrack", _pause=False)
        return "Skipping."
    if _any(t, "previous track", "previous song", "go back a song"):
        pyautogui.press("prevtrack", _pause=False)
        return "Going back."

    # --- windows ---
    if re.search(r"\b(screenshot|take screenshot|screen capture)\b", t):
        folder = os.path.join(os.path.expanduser("~"), "Pictures", "Jarvis")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("shot-%Y%m%d-%H%M%S.png"))
        pyautogui.screenshot(path)
        return "Screenshot saved to Pictures, Jarvis."
    if re.search(r"\b(lock|lock computer|lock workstation|lock pc)\b", t):
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return "Locking the workstation."
    if _any(t, "show desktop", "minimize everything", "minimise everything"):
        pyautogui.hotkey("win", "d", _pause=False)
        return "Desktop."
    if re.search(r"\b(minimize|minimise)\b", t):
        pyautogui.hotkey("win", "down", _pause=False)
        return "Minimised."
    if re.search(r"\b(maximize|maximise)\b", t):
        pyautogui.hotkey("win", "up", _pause=False)
        return "Maximised."
    if _any(t, "switch window", "next window"):
        pyautogui.hotkey("alt", "tab", _pause=False)
        return "Switching."
    if _any(t, "close window", "close this"):
        pyautogui.hotkey("alt", "f4", _pause=False)
        return "Closing."
    if "scroll down" in t:
        pyautogui.scroll(-600, _pause=False)
        return None
    if "scroll up" in t:
        pyautogui.scroll(600, _pause=False)
        return None

    # --- software updates ---
    if _any(t, "check for updates", "update jarvis", "check updates", "any updates"):
        if hasattr(ctx, "updater") and ctx.updater:
            ctx.updater.check_async(notify_if_latest=True)
            return "Checking GitHub for updates, sir."
        return "Updater is not initialized, sir."
    if _any(t, "install update", "apply update", "restart and update"):
        if hasattr(ctx, "updater") and ctx.updater and ctx.updater.downloaded_path:
            ctx.updater.apply_update_and_restart()
            return "Applying update and restarting Jarvis now, sir."
        return "No downloaded update is ready to install, sir."

    # --- fallback: Groq AI query if enabled ---
    return _handle_ai_query(text, ctx)


def _handle_ai_query(user_text: str, ctx: Any) -> str:
    """Dispatches unhandled user utterances to the Groq LLM if enabled."""
    cfg = None
    if ctx:
        if hasattr(ctx, "engine") and hasattr(ctx.engine, "cfg"):
            cfg = ctx.engine.cfg
        elif hasattr(ctx, "cfg"):
            cfg = ctx.cfg

    ai_enabled = getattr(cfg, "ai_enabled", False) if cfg else False
    api_key = get_api_key()

    if not ai_enabled or not api_key:
        return "I didn't catch a command for that, sir."

    # Notify overlay of thinking state
    if ctx and hasattr(ctx, "overlay") and ctx.overlay:
        try:
            ctx.overlay.post("thinking", active=True)
        except Exception:
            pass

    try:
        timeout = getattr(cfg, "ai_timeout", 10) if cfg else 10
        model = getattr(cfg, "ai_llm_model", DEFAULT_LLM_MODEL) if cfg else DEFAULT_LLM_MODEL
        max_history = getattr(cfg, "ai_max_history", 6) if cfg else 6

        client = GroqClient(api_key=api_key, timeout=timeout)
        conv_mgr = get_conversation_manager()
        conv_mgr.max_turns = max_history

        system_prompt = build_system_prompt(ctx)
        messages = conv_mgr.get_messages(system_prompt, user_text)

        raw_reply = client.chat(messages, model=model, temperature=0.5, json_mode=True)
        action, args, reply = parse_ai_response(raw_reply)

        if action and action != "none":
            executed, act_msg = execute_whitelisted_action(action, args, ctx)
            if not executed:
                print(f"[ai] Action '{action}' note: {act_msg}")

        conv_mgr.add_turn(user_text, reply)
        return reply

    except AuthenticationError:
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("ai_error", text="Invalid Groq Key")
            except Exception:
                pass
        return "My Groq API key appears to be invalid, sir. Please check your settings."
    except RateLimitError:
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("ai_error", text="Groq Rate Limited")
            except Exception:
                pass
        return "Groq rate limit reached, sir. Please stand by a moment."
    except (OfflineError, TimeoutError):
        if ctx and hasattr(ctx, "overlay") and ctx.overlay:
            try:
                ctx.overlay.post("ai_error", text="Groq Offline")
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
