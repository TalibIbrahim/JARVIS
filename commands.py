"""Local voice commands. No cloud agent: every command maps to a direct action on this PC."""
import datetime
import os
import re
import subprocess
import time
import urllib.parse
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

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

# Pending destructive confirmation state (e.g. 'shutdown', 'restart', 'close_window')
_PENDING_CONFIRMATION: Optional[str] = None


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


def _post_action(ctx, action: str, success: bool = True, args: Optional[dict] = None, text: str = ""):
    """Notifies overlay that a command or action has completed to trigger RESULT card."""
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


def run_command(text, ctx):
    """Returns the reply Jarvis should speak, or None if nothing matched."""
    # 0. Check chained / compound commands (e.g. 'volume up and maximize window')
    try:
        import chained_commands
        sub_cmds = chained_commands.split_chained_commands(text)
        if len(sub_cmds) > 1:
            ok, combined_reply, _ = chained_commands.execute_chained_commands(
                compound_text=text,
                ctx=ctx,
                execute_single_fn=_run_single_command,
            )
            return combined_reply
    except Exception as exc:
        print(f"[commands] chained commands note: {exc}")

    return _run_single_command(text, ctx)


def _run_single_command(text, ctx):
    """Internal single-command evaluator."""
    import pyautogui
    t = text.lower().strip(" ,.!?")
    # Strip conversational filler prefixes
    clean_t = re.sub(r"^(?:please\s+|can you\s+(?:please\s+)?|could you\s+(?:please\s+)?|would you\s+(?:please\s+)?|hey\s+|ok\s+|okay\s+|just\s+)", "", t).strip()

    # Phase 7 Safety Gate: intercept unsafe or arbitrary shell/PowerShell requests
    from quick_answers import is_unsafe_shell_command
    if is_unsafe_shell_command(clean_t):
        return "I can't do that, sir."

    # Phase 7 Screen Awareness (explicit user queries)
    import screen_awareness
    if screen_awareness.is_screen_query(clean_t):
        return screen_awareness.analyze_screen(text, ctx)

    # Phase 7 World Time Lookups (System 1 Reflex: <1 ms)
    import quick_answers
    if quick_answers.is_timezone_query(clean_t):
        tz_reply = quick_answers.get_world_time(clean_t, ctx)
        if tz_reply:
            return tz_reply

    # Phase 7 Explicit Wikipedia queries
    m_wiki = re.match(r"^(?:wiki|wikipedia)\s+(.+)", clean_t)
    if m_wiki:
        w_ans = quick_answers.get_quick_answer(clean_t, ctx)
        if w_ans:
            return w_ans

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

    # Direct verbal confirmations
    if _any(clean_t, "yes confirm shutdown", "confirm shutdown"):
        _PENDING_CONFIRMATION = None
        import system_control
        ok, msg = system_control.shutdown_system(confirmed=True)
        _post_action(ctx, "shutdown_system", ok, text=text)
        return msg

    if _any(clean_t, "yes confirm restart", "confirm restart"):
        _PENDING_CONFIRMATION = None
        import system_control
        ok, msg = system_control.restart_system(confirmed=True)
        _post_action(ctx, "restart_system", ok, text=text)
        return msg

    if _any(clean_t, "confirm close", "force close", "close anyway"):
        _PENDING_CONFIRMATION = None
        import system_control
        ok, msg = system_control.close_window(confirm=True)
        _post_action(ctx, "close_window", ok, text=text)
        return "Window closed, sir."

    # Destructive actions gates
    if _any(clean_t, "shut down the pc", "shut down pc", "shut down computer", "turn off pc", "turn off the pc", "power off pc", "shutdown pc", "shutdown computer"):
        _PENDING_CONFIRMATION = "shutdown"
        return "Shutdown requires confirmation, sir. Please say 'confirm shutdown' to proceed."
    if clean_t == "shutdown":
        _PENDING_CONFIRMATION = "shutdown"
        return "Shutdown requires confirmation, sir. Please say 'confirm shutdown' to proceed."

    if _any(clean_t, "restart pc", "restart the pc", "restart computer", "reboot pc", "reboot computer", "reboot the computer"):
        _PENDING_CONFIRMATION = "restart"
        return "Restart requires confirmation, sir. Please say 'confirm restart' to proceed."
    if clean_t in ("restart", "reboot"):
        _PENDING_CONFIRMATION = "restart"
        return "Restart requires confirmation, sir. Please say 'confirm restart' to proceed."

    # --- power / mode ---
    if _any(clean_t, "sleep system", "sleep pc", "sleep computer", "put pc to sleep", "put computer to sleep"):
        import system_control
        ok, msg = system_control.sleep_system()
        _post_action(ctx, "sleep_system", ok, text=text)
        return "Suspending system power states, sir."

    if _any(clean_t, "shut down jarvis", "quit jarvis", "exit jarvis", "power down", "goodbye"):
        _post_action(ctx, "power_mode", True)
        ctx.quit()
        return "Powering down. Goodbye, sir."
    if _any(clean_t, "go to sleep", "sleep mode", "sleep", "take a nap", "standby", "stand by", "rest"):
        _post_action(ctx, "power_mode", True)
        ctx.set_active(False)
        reset_ai_history()
        return "Going to standby, sir."
    if _any(clean_t, "wake up", "activate", "come online", "i'm back", "im back", "online"):
        _post_action(ctx, "power_mode", True)
        ctx.set_active(True)
        return "Online. At your service, sir."

    # --- ai setup ---
    if _any(clean_t, "set up ai", "setup ai", "configure ai", "ai settings", "open ai settings"):
        _post_action(ctx, "settings", True)
        if hasattr(ctx, "overlay") and ctx.overlay:
            ctx.overlay.open_settings("AI")
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
        pyautogui.press("volumemute", _pause=False)
        pyautogui.hotkey("win", "down", _pause=False)
        _post_action(ctx, "stealth_mode", True)
        return "Stealth mode engaged, sir. Audio muted and visual signatures minimized."
    if _any(clean_t, "clean slate protocol", "clean slate", "protocol clean slate", "wrap it up"):
        pyautogui.hotkey("win", "d", _pause=False)
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
                ok, msg = vp.enroll([latest])
                if ok:
                    vp.enabled = True
                    return "Voice profile successfully calibrated, sir. I have committed your vocal signature to memory and will listen specifically for you."
            return "Vocal training mode active, sir. Speak a sentence clearly and I will calibrate to your voice."
        except Exception:
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
        return get_system_telemetry(ctx)
    if clean_t in ("hello", "hi", "hey", "good morning", "good evening", "good afternoon"):
        return "Welcome back, sir."
    if _any(clean_t, "status", "report", "how are you"):
        return get_system_telemetry(ctx)
    if _any(clean_t, "what's the weather", "weather forecast"):
        from weather_client import get_weather_client
        cfg = getattr(ctx, "cfg", None) or getattr(getattr(ctx, "engine", None), "cfg", None)
        city = getattr(cfg, "weather_city", "New York") if cfg else "New York"
        lat = getattr(cfg, "weather_lat", 40.7128) if cfg else 40.7128
        lon = getattr(cfg, "weather_lon", -74.0060) if cfg else -74.0060
        units = getattr(cfg, "weather_units", "auto") if cfg else "auto"

        client = get_weather_client()
        weather = client.get_weather(lat, lon, city=city, units=units)
        if not weather and city:
            weather = client.get_weather_for_city(city, units=units)

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
        return "Today is " + datetime.datetime.now().strftime("%A, %B %d") + "."
    if _any(clean_t, "thank you jarvis", "thanks jarvis", "good job jarvis", "well done jarvis", "nice work jarvis"):
        return "Always a pleasure watching you work, sir."
    if _any(clean_t, "who are you", "what are you"):
        return "I am J.A.R.V.I.S., Just A Rather Very Intelligent System. Configured to manage your computing environment and tactical telemetry, sir."
    if _any(clean_t, "are you there", "you there jarvis"):
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

    # --- apps and web ---
    m = re.match(r"(?:open|launch|start|run|bring up|go to)\s+(?:up\s+)?(?:the\s+)?(.+)", clean_t)
    if m:
        name = m.group(1).strip()
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
        webbrowser.open("https://www.google.com/search?q=" + m.group(1).strip().replace(" ", "+"))
        _post_action(ctx, "search", True, args={"query": m.group(1).strip()}, text=text)
        return f"Searching for {m.group(1).strip()}."

    # --- media / volume / display (Phase 5) ---
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
        pyautogui.press("volumeup", presses=5, _pause=False)
        _post_action(ctx, "volume_up", True, args={"steps": 5}, text=text)
        return "Volume up."

    if _any(clean_t, "volume down", "turn down", "turn the volume down", "turn volume down", "decrease volume", "lower the volume", "lower volume", "quieter"):
        try:
            import system_control
            system_control.volume_down(5)
        except Exception:
            pass
        pyautogui.press("volumedown", presses=5, _pause=False)
        _post_action(ctx, "volume_down", True, args={"steps": 5}, text=text)
        return "Volume down."

    if re.search(r"\b(mute|unmute)\b", clean_t):
        try:
            import system_control
            system_control.mute(True)
        except Exception:
            pass
        pyautogui.press("volumemute", _pause=False)
        _post_action(ctx, "mute", True, text=text)
        return "Muted."

    # Brightness & Night Light
    m_bri = re.search(r"\b(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})(?:\s*%)?\b", clean_t)
    if m_bri and not _any(clean_t, "brightness up", "brightness down", "brighter", "dimmer"):
        lvl = int(m_bri.group(1))
        import system_control
        ok, msg = system_control.set_brightness(lvl)
        _post_action(ctx, "set_brightness", ok, args={"level": lvl}, text=text)
        ctx_res.record_turn(user_utterance=text, resolved_action="set_brightness", arguments={"level": lvl})
        return f"Screen brightness set to {lvl} percent, sir."

    if _any(clean_t, "brightness up", "turn brightness up", "increase brightness", "raise brightness", "brighter screen", "make screen brighter"):
        import system_control
        ok, msg = system_control.brightness_up(10)
        _post_action(ctx, "brightness_up", ok, args={"steps": 10}, text=text)
        return "Screen brightness increased, sir."

    if _any(clean_t, "brightness down", "turn brightness down", "decrease brightness", "lower brightness", "dim screen", "dim the screen", "make screen dimmer"):
        import system_control
        ok, msg = system_control.brightness_down(10)
        _post_action(ctx, "brightness_down", ok, args={"steps": 10}, text=text)
        return "Screen brightness lowered, sir."

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

    # 4. Simple Play / Pause toggle and track skipping via SMTC with debouncing
    if clean_t in ("play", "pause", "resume", "unpause", "play music", "pause music", "stop music", "resume music", "toggle playback", "toggle play", "toggle music", "stop playback"):
        try:
            from media_adapter import get_media_adapter
            get_media_adapter().play_pause()
        except Exception:
            pass
        pyautogui.press("playpause", _pause=False)
        _post_action(ctx, "play_pause", True)
        return "Done."
    if _any(clean_t, "next track", "next song", "skip", "play next song"):
        try:
            from media_adapter import get_media_adapter
            get_media_adapter().next_track()
        except Exception:
            pass
        pyautogui.press("nexttrack", _pause=False)
        _post_action(ctx, "next_track", True)
        return "Skipping."
    if _any(clean_t, "previous track", "previous song", "go back a song", "replay track"):
        try:
            from media_adapter import get_media_adapter
            get_media_adapter().previous_track()
        except Exception:
            pass
        pyautogui.press("prevtrack", _pause=False)
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
        pyautogui.hotkey("win", "d", _pause=False)
        _post_action(ctx, "show_desktop", True)
        return "Desktop."
    if re.search(r"\b(minimize|minimise)\b", t):
        pyautogui.hotkey("win", "down", _pause=False)
        _post_action(ctx, "minimize_window", True)
        return "Minimised."
    if re.search(r"\b(maximize|maximise)\b", t):
        pyautogui.hotkey("win", "up", _pause=False)
        _post_action(ctx, "maximize_window", True)
        return "Maximised."
    if _any(t, "switch window", "next window"):
        pyautogui.hotkey("alt", "tab", _pause=False)
        _post_action(ctx, "switch_window", True)
        return "Switching."
    if "scroll down" in t:
        pyautogui.scroll(-600, _pause=False)
        _post_action(ctx, "scroll", True)
        return None
    if "scroll up" in t:
        pyautogui.scroll(600, _pause=False)
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

        raw_reply = stream_ai_query(
            client=client,
            messages=messages,
            model=model,
            temperature=0.5,
            ctx=ctx,
        )
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

