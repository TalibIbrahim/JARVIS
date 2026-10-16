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
    DEFAULT_VISION_MODEL,
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


def run_command(text, ctx):
    """Returns the reply Jarvis should speak, or None if nothing matched."""
    import pyautogui
    t = text.lower().strip(" ,.!?")
    # Strip conversational filler prefixes
    clean_t = re.sub(r"^(?:please\s+|can you\s+(?:please\s+)?|could you\s+(?:please\s+)?|would you\s+(?:please\s+)?|hey\s+|ok\s+|okay\s+|just\s+)", "", t).strip()

    # --- power / mode ---
    if _any(clean_t, "shut down jarvis", "quit jarvis", "exit jarvis", "power down", "goodbye"):
        ctx.quit()
        return "Powering down. Goodbye, sir."
    if _any(clean_t, "go to sleep", "sleep mode", "sleep", "take a nap", "standby", "stand by", "rest"):
        ctx.set_active(False)
        reset_ai_history()
        return "Going to standby, sir."
    if _any(clean_t, "wake up", "activate", "come online", "i'm back", "im back", "online"):
        ctx.set_active(True)
        return "Online. At your service, sir."

    # --- ai setup ---
    if _any(clean_t, "set up ai", "setup ai", "configure ai", "ai settings", "open ai settings"):
        if hasattr(ctx, "overlay") and ctx.overlay:
            ctx.overlay.open_settings("AI")
            return "Opening AI settings, sir."
        return "Settings overlay is unavailable, sir."

    # --- dwell & pointer modes ---
    if _any(clean_t, "dwell on", "enable dwell", "turn on dwell", "dwell click on"):
        ctx.engine.cfg.dwell_mode = "click"
        return "Dwell click enabled, sir."
    if _any(clean_t, "dwell off", "disable dwell", "turn off dwell", "dwell click off"):
        ctx.engine.cfg.dwell_mode = "off"
        return "Dwell click disabled, sir."
    if _any(clean_t, "dwell menu", "dwell radial", "radial menu on", "radial mode"):
        ctx.engine.cfg.dwell_mode = "radial"
        return "Dwell radial menu enabled, sir."
    if _any(clean_t, "precision mode on", "enable precision", "fine aim on"):
        ctx.engine.cfg.precision_mode = True
        return "Precision fine-aim mode enabled, sir."
    if _any(clean_t, "precision mode off", "disable precision", "fine aim off"):
        ctx.engine.cfg.precision_mode = False
        return "Precision fine-aim mode disabled, sir."
    if _any(clean_t, "relative mode", "trackpad mode"):
        ctx.engine.cfg.tracking_mode = "relative"
        return "Relative trackpad mode active, sir."
    if _any(clean_t, "absolute mode", "screen mode"):
        ctx.engine.cfg.tracking_mode = "absolute"
        return "Absolute screen mapping active, sir."

    # --- Stark protocols ---
    if _any(clean_t, "house party protocol", "house party", "protocol house party"):
        _launch("chrome")
        return "House Party Protocol initiated, sir. Bringing primary workstations online."
    if _any(clean_t, "stealth mode", "protocol stealth", "go dark", "silent mode"):
        pyautogui.press("volumemute", _pause=False)
        pyautogui.hotkey("win", "down", _pause=False)
        return "Stealth mode engaged, sir. Audio muted and visual signatures minimized."
    if _any(clean_t, "clean slate protocol", "clean slate", "protocol clean slate", "wrap it up"):
        pyautogui.hotkey("win", "d", _pause=False)
        reset_ai_history()
        if ctx:
            ctx.set_active(False)
        return "Clean Slate Protocol executed, sir. Have a pleasant evening."
    if _any(clean_t, "target practice", "repulsor practice", "repulsor calibration", "aim test"):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        target_html = os.path.join(base_dir, "tests", "target_practice.html")
        if os.path.exists(target_html):
            webbrowser.open(f"file:///{target_html}")
        else:
            webbrowser.open("https://www.google.com")
        return "Target practice range initialized, sir. Repulsor calibration ready."

    # --- voice training & speaker profile ---
    if _any(clean_t, "train my voice", "train voice", "enroll voice", "learn my voice", "calibrate voice", "voice training", "recognize my voice"):
        try:
            from voice_profile import get_voice_profile
            from voice import Voice
            vp = get_voice_profile()
            latest = getattr(Voice, "_last_raw_audio", None)
            if latest:
                ok, msg = vp.enroll([latest])
                if ok:
                    vp.enabled = True
                    return "Voice profile successfully calibrated, sir. I have committed your vocal signature to memory and will listen specifically for you."
            return "Vocal training mode active, sir. Speak a sentence clearly and I will calibrate to your voice."
        except Exception:
            return "Voice training calibration encountered an issue, sir."

    # --- chatter & diagnostics ---
    if _any(clean_t, "diagnostic", "diagnostics", "run diagnostic", "run diagnostics", "system diagnostic", "suit diagnostic", "suit status", "power level", "power levels", "arc reactor", "telemetry"):
        return get_system_telemetry(ctx)
    if clean_t in ("hello", "hi", "hey", "good morning", "good evening", "good afternoon"):
        return "Welcome back, sir."
    if _any(clean_t, "status", "report", "how are you"):
        return get_system_telemetry(ctx)
    if re.search(r"\b(time|what time|current time)\b", clean_t):
        return "It is " + datetime.datetime.now().strftime("%I:%M %p").lstrip("0") + ", sir."
    if _any(clean_t, "date", "what day"):
        return "Today is " + datetime.datetime.now().strftime("%A, %B %d") + "."
    if _any(clean_t, "thank you jarvis", "thanks jarvis", "good job jarvis", "well done jarvis", "nice work jarvis"):
        return "Always a pleasure watching you work, sir."
    if _any(clean_t, "who are you", "what are you"):
        return "I am J.A.R.V.I.S., Just A Rather Very Intelligent System. Configured to manage your computing environment and tactical telemetry, sir."
    if _any(clean_t, "are you there", "you there jarvis"):
        return "For you, sir, always."

    # --- apps and web ---
    m = re.match(r"(?:open|launch|start|run|bring up|go to)\s+(?:up\s+)?(?:the\s+)?(.+)", clean_t)
    if m:
        name = m.group(1).strip()
        if name in SITES:
            webbrowser.open(SITES[name])
            return f"Opening {name}."
        if name in APPS:
            _launch(APPS[name])
            return f"Opening {name}."
        return f"I don't know how to open {name} yet, sir."
    m = re.match(r"(?:search(?: for)?|google|look up)\s+(.+)", clean_t)
    if m:
        webbrowser.open("https://www.google.com/search?q=" + m.group(1).strip().replace(" ", "+"))
        return f"Searching for {m.group(1).strip()}."

    # --- media / volume ---
    if _any(clean_t, "volume up", "turn up", "turn the volume up", "turn volume up", "increase volume", "raise volume", "louder"):
        pyautogui.press("volumeup", presses=5, _pause=False)
        return "Volume up."
    if _any(clean_t, "volume down", "turn down", "turn the volume down", "turn volume down", "decrease volume", "lower the volume", "lower volume", "quieter"):
        pyautogui.press("volumedown", presses=5, _pause=False)
        return "Volume down."
    if re.search(r"\b(mute|unmute)\b", clean_t):
        pyautogui.press("volumemute", _pause=False)
        return "Muted."
    if _any(clean_t, "play", "pause", "resume"):
        pyautogui.press("playpause", _pause=False)
        return "Done."
    if _any(clean_t, "next track", "next song", "skip"):
        pyautogui.press("nexttrack", _pause=False)
        return "Skipping."
    if _any(clean_t, "previous track", "previous song", "go back a song"):
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

    # Visual queries immediately dispatch to camera vision
    if is_visual_query(text):
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
                return decision_reply
        except Exception as exc:
            print(f"[commands] decision engine note: {exc}")

    # --- fallback: Groq AI query if enabled (System 2 Deep Reasoning) ---
    return _handle_ai_query(text, ctx)


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
            tracker = getattr(ctx, "tracker", None) if ctx else None
            frame = tracker.get_latest_frame() if tracker else None
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
