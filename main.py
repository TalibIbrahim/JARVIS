"""J.A.R.V.I.S. — hand-gesture PC control with a notch-style overlay.  Run: python main.py"""
import argparse
import ctypes
import datetime
import sys
import threading
import time

import jarvis_log as _jlog


def _enable_dpi_awareness():
    # Per-Monitor V2 DPI awareness for modern Windows 10/11
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
        # Explicit Windows AppUserModelID so taskbar displays our custom icon instead of generic Python/window icon
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JARVIS.TouchlessPC.Control.1.1")
        except Exception:
            pass


class Context:
    """Small bundle shared by the tracker, voice commands and hotkeys."""

    def __init__(self, engine, overlay, speaker, stop, updater=None, tracker=None, controller=None, voice=None, cfg=None):
        self.engine = engine
        self.overlay = overlay
        self.speaker = speaker
        self.stop = stop
        self.updater = updater
        self.tracker = tracker
        self.controller = controller
        self.voice = voice
        self.cfg = cfg
        # Visible expanded island page (None = collapsed); drives hardware/weather polling.
        self._visible_page = None
        self._page_lock = threading.Lock()
        self._page_listeners = []
        # Assistant status observed from overlay events (see observe_overlay).
        self._assist_lock = threading.Lock()
        self._assist = {"listening_until": 0.0, "thinking_until": 0.0, "speaking_until": 0.0,
                        "last_command": "", "last_result": ""}

    # ---- visible island page (GUI thread calls this) ----
    def set_visible_page(self, page):
        """The GUI calls this whenever the visible expanded page changes, or with None when collapsed."""
        page = str(page) if page else None
        with self._page_lock:
            old = self._visible_page
            if old == page:
                return
            self._visible_page = page
            listeners = list(self._page_listeners)
        _jlog.get_logger("main").info("island visible page: %s -> %s", old, page)
        for cb in listeners:
            try:
                cb(page)
            except Exception:
                _jlog.get_logger("main").exception("visible-page listener failed")

    def visible_page(self):
        return self._visible_page

    def add_page_listener(self, cb):
        with self._page_lock:
            if cb not in self._page_listeners:
                self._page_listeners.append(cb)

    # ---- JARVIS pause tracking (engine standby) ----
    def set_tracking_paused(self, paused):
        """paused=True puts the gesture engine in standby; False wakes it. Applied on the next engine update."""
        _jlog.get_logger("main").info("tracking %s via island", "paused" if paused else "resumed")
        self.engine.request(not bool(paused))

    def is_tracking_paused(self):
        pending = getattr(self.engine, "_req", None)
        if pending is not None and pending != "toggle":
            return not pending
        return not bool(getattr(self.engine, "active", True))

    # ---- assistant status for the JARVIS page ----
    def observe_overlay(self, overlay=None):
        """Wrap overlay.post so assistant state/last action can be derived from the event stream."""
        ov = overlay or self.overlay
        orig = ov.post

        def _post(kind, **data):
            self._note_event(kind, data)
            return orig(kind, **data)

        ov.post = _post

    def _note_event(self, kind, d):
        now = time.monotonic()
        with self._assist_lock:
            a = self._assist
            if kind == "listening":
                a["listening_until"] = now + 5.0
            elif kind == "wake_detected":
                a["listening_until"] = now + float(getattr(self.cfg, "command_window", 7.0))
            elif kind == "live_speech":
                a["listening_until"] = now + 4.0
            elif kind == "listening_done":
                a["listening_until"] = 0.0
            elif kind == "thinking":
                a["thinking_until"] = (now + float(d.get("timeout", 8.0))) if d.get("active", True) else 0.0
            elif kind == "say":
                text = str(d.get("text", ""))
                a["thinking_until"] = 0.0
                if d.get("speak", True) and text:
                    a["speaking_until"] = now + max(3.5, len(text) * 0.08)
            elif kind == "ai_error":
                a["thinking_until"] = 0.0
            elif kind == "action_executed":
                action = str(d.get("action", "") or "")
                a["last_command"] = action.replace("_", " ")[:40]
                a["last_result"] = "ok" if d.get("success", True) else "fail"
                a["thinking_until"] = 0.0

    def assistant_status(self):
        """dict(state=idle|listening|thinking|speaking|standby, last_command=<short action name>, last_result=ok|fail|"")."""
        now = time.monotonic()
        with self._assist_lock:
            a = dict(self._assist)
        if a["speaking_until"] > now:
            state = "speaking"
        elif a["thinking_until"] > now:
            state = "thinking"
        elif a["listening_until"] > now:
            state = "listening"
        elif self.is_tracking_paused():
            state = "standby"
        else:
            state = "idle"
        return {"state": state, "last_command": a["last_command"], "last_result": a["last_result"]}

    def reload_config(self, new_cfg=None):
        if new_cfg is not None:
            self.cfg = new_cfg
        if hasattr(self.engine, "cfg"):
            self.engine.cfg = self.cfg
        if hasattr(self.engine, "reload_config"):
            self.engine.reload_config(self.cfg)
        if hasattr(self.controller, "reload_config"):
            self.controller.reload_config(self.cfg)
        if hasattr(self.tracker, "reload_config"):
            self.tracker.reload_config(self.cfg)
        if hasattr(self.speaker, "reload_config"):
            self.speaker.reload_config(self.cfg)
        if hasattr(self.voice, "reload_config"):
            self.voice.reload_config(self.cfg)

    def say(self, text):
        if not text:
            return
        self.overlay.post("say", text=text, speak=True)

    def set_active(self, want):
        if want is False:
            try:
                from ai import reset_ai_history
                reset_ai_history()
            except Exception:
                pass
        if want is None:
            self.engine.request_toggle()
        else:
            self.engine.request(want)

    def quit(self):
        self.stop.set()

    def chirp(self, kind=True):
        if sys.platform != "win32":
            return
        import winsound
        if kind == "wake":
            notes = [(880, 50), (1320, 70)]
        elif kind == "confirm" or kind is True:
            notes = [(1046, 45), (1318, 65)]
        elif kind == "down" or kind is False:
            notes = [(900, 60), (550, 90)]
        else:
            notes = [(880, 50), (1100, 70)]

        def play():
            try:
                for freq, ms in notes:
                    winsound.Beep(freq, ms)
            except Exception:
                pass

        threading.Thread(target=play, daemon=True).start()


_INSTANCE_MUTEX = None
_LAST_WORKER_HEALTH: dict = {}


def post_worker_health(overlay, state, reason=""):
    """Post overlay 'worker_health' (keys: state, reason) ONLY when (state, reason) changed; log the transition."""
    key = id(overlay)
    new = (state, reason)
    old = _LAST_WORKER_HEALTH.get(key)
    if old == new:
        return False
    _LAST_WORKER_HEALTH[key] = new
    log = _jlog.get_logger("health")
    msg = "worker health %s -> %s (%s)"
    args = (old[0] if old else "start", state, reason)
    if state == "down":
        log.warning(msg, *args)
    else:
        log.info(msg, *args)
    overlay.post("worker_health", state=state, reason=reason)
    return True


HARDWARE_PAGES = ("hardware", "hardware_grid", "hardware_strip")
FPS_ONLY_PAGES = ("jarvis", "jarvis_card")  # island passes "jarvis"; card alias kept for safety


def tracking_fps(tracker):
    """Tracking FPS (FrameGrabber) or None when tracking is not running."""
    if tracker is None:
        return None
    grabber = getattr(tracker, "_grabber", None)
    fps = getattr(grabber, "fps", None) if grabber is not None else getattr(tracker, "_fps", None)
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        return None
    return fps if fps > 0 else None


def compact_shows_hardware(cfg):
    """True when the collapsed pill is configured to display hardware data. No such compact mode exists yet."""
    return False


def _bits(bytes_per_sec):
    """Bytes/s -> true bits/s (x8); None passes through."""
    return None if bytes_per_sec is None else bytes_per_sec * 8


class HardwareFeed:
    """Runs the HardwareAdapter only while hardware data is visible and posts overlay 'hardware_state' once per sample.
    Contract: net_up_bps / net_down_bps are BITS per second (adapter bytes/s x 8). On the JARVIS page only fps is posted (1 Hz)."""

    def __init__(self, ctx, overlay, adapter=None):
        from hardware_adapter import get_hardware_adapter
        self.ctx = ctx
        self.overlay = overlay
        self.adapter = adapter or get_hardware_adapter()
        self._log = _jlog.get_logger("hardware")
        self._fps_stop = threading.Event()
        self._fps_thread = None

    def _fps_loop(self):
        """1 Hz hardware_state with ONLY fps while the JARVIS page is visible (no psutil/NVML sampling)."""
        while not self._fps_stop.wait(1.0):
            if self.ctx.visible_page() in FPS_ONLY_PAGES and self.adapter.is_paused():
                self.overlay.post("hardware_state", fps=tracking_fps(self.ctx.tracker))

    def wanted(self):
        return (self.ctx.visible_page() in HARDWARE_PAGES) or compact_shows_hardware(self.ctx.cfg)

    def start(self):
        self.adapter.pause()  # idle until a hardware view is visible
        self.adapter.add_listener(self._on_metrics)
        self.adapter.start()
        self.ctx.add_page_listener(lambda _page: self.update())
        self._fps_thread = threading.Thread(target=self._fps_loop, name="HardwareFpsFeed", daemon=True)
        self._fps_thread.start()
        self.update()

    def update(self):
        want = self.wanted()
        if want and self.adapter.is_paused():
            self._log.info("hardware sampling resumed (page=%s)", self.ctx.visible_page())
            self.adapter.resume()
        elif not want and not self.adapter.is_paused():
            self._log.info("hardware sampling paused (page=%s)", self.ctx.visible_page())
            self.adapter.pause()

    def stop(self):
        self._fps_stop.set()
        self.adapter.remove_listener(self._on_metrics)
        self.adapter.stop(timeout=1.0)

    def _on_metrics(self, m):
        if self.adapter.is_paused():
            return
        self.overlay.post(
            "hardware_state",
            cpu=m.cpu_percent, ram_used_gb=m.ram_used_gb, ram_total_gb=m.ram_total_gb, ram_pct=m.ram_percent,
            gpu=m.gpu_percent, net_up_bps=_bits(m.net_up_bytes_sec), net_down_bps=_bits(m.net_down_bytes_sec),
            disk_pct=m.disk_percent, fps=tracking_fps(self.ctx.tracker),
        )


def start_fullscreen_watcher(ctx, overlay, stop, interval=1.0, probe=None):
    """1 s daemon poll of the foreground window; posts overlay 'fullscreen' (active=bool) on change.
    Skips the Win32 calls entirely while island_hide_fullscreen is off."""
    log = _jlog.get_logger("fullscreen")
    if probe is None:
        from fullscreen_detector import is_fullscreen_on_island_monitor as probe

    def _loop():
        last = False
        logged = set()
        while not stop.wait(interval):
            enabled = bool(getattr(ctx.cfg, "island_hide_fullscreen", True))
            active = last
            if not enabled:
                active = False
            else:
                try:
                    active = bool(probe())
                except Exception as exc:
                    if repr(exc) not in logged:  # log each distinct failure once, keep last state
                        logged.add(repr(exc))
                        log.exception("fullscreen probe failed: %s", exc)
            if active != last:
                last = active
                log.info("fullscreen %s", "entered" if active else "left")
                overlay.post("fullscreen", active=active)

    t = threading.Thread(target=_loop, name="FullscreenWatcher", daemon=True)
    t.start()
    return t


def acquire_single_instance_lock(app_id="Local\\JARVIS_HandGesture_SingleInstance_Mutex"):
    """
    Guarantees only one instance of J.A.R.V.I.S. can run simultaneously.
    If another instance is already running, brings it to focus and exits immediately.
    """
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return True

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    # Create session-local mutex to prevent multi-instance runs
    handle = kernel32.CreateMutexW(None, False, app_id)
    last_error = kernel32.GetLastError()
    _jlog.get_logger("main").info(
        "single-instance mutex: %s (GetLastError=%d) census=%s",
        "already-exists" if last_error == ERROR_ALREADY_EXISTS else "acquired",
        last_error, _jlog.process_census())

    if last_error == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(handle)
        _INSTANCE_MUTEX = None
        print("[JARVIS] Another instance is already running. Exiting.")
        try:
            # Locate existing Dynamic Island window if present
            hwnd = user32.FindWindowW(None, "JARVIS_DYNAMIC_ISLAND")
            if hwnd:
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(hwnd)
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass
        return False

    _INSTANCE_MUTEX = handle
    return True


def main():
    _jlog.setup()
    if any(arg in sys.argv for arg in ("-h", "--help", "--island-demo", "--perf-report", "--self-test")):
        pass
    elif not acquire_single_instance_lock():
        sys.exit(0)

    _enable_dpi_awareness()
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. gesture control")
    parser.add_argument("--preview", action="store_true", help="show the camera HUD window")
    parser.add_argument("--no-voice", action="store_true", help="disable voice commands")
    parser.add_argument("--camera", type=int, default=None, help="camera index (default 0)")
    parser.add_argument("--diagnose", action="store_true", help="run diagnostic jitter, latency, and precision measurement")
    parser.add_argument("--test-mic", action="store_true", help="test microphone input levels and speech recognition")
    parser.add_argument("--island-demo", action="store_true", help="run Dynamic Island demo mode without requiring camera/gestures")
    parser.add_argument("--perf-report", action="store_true", help="print latency budget performance report and exit")
    parser.add_argument("--self-test", action="store_true", help="run hardware, models, and network self-test diagnostic")
    args = parser.parse_args()
    _jlog.get_logger("main").info("args: %s", vars(args))

    if args.perf_report:
        from perf import print_perf_report
        code = 0 if print_perf_report() else 1
        sys.exit(code)

    if args.self_test:
        from self_test import run_self_test
        code = run_self_test()
        sys.exit(code)

    if args.diagnose:
        from diagnose import run_diagnose
        run_diagnose()
        sys.exit(0)

    if args.test_mic:
        import subprocess
        subprocess.run([sys.executable, "voice.py"])
        sys.exit(0)

    if args.island_demo:
        from config import load_config
        from overlay import Overlay
        cfg = load_config()
        stop = threading.Event()
        overlay = Overlay(stop, cfg=cfg)
        if hasattr(overlay, "island") and not getattr(overlay, "_island_demo", None):
            try:
                from ui.island import IslandDemoController
                demo = IslandDemoController(overlay.island)
                demo.start()
                overlay._island_demo = demo
            except Exception as _demo_err:
                print(f"[main] demo start error: {_demo_err}")
        print("[JARVIS] Dynamic Island running in --island-demo mode at 60 FPS.")
        try:
            overlay.run()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            if _INSTANCE_MUTEX and sys.platform == "win32":
                try:
                    ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
                except Exception:
                    pass
        sys.exit(0)

    from config import load_config
    from gestures import GestureEngine
    from controller import Controller
    from overlay import Overlay
    from tracker import Tracker
    from voice import Speaker, Voice

    cfg = load_config()
    _jlog.set_debug_transcripts(getattr(cfg, "debug_log_transcripts", False))
    # CLI flags are runtime-only: mark them so a later whole-file save_config(cfg) never persists them.
    from config import mark_runtime_override
    if args.preview:
        cfg.show_preview = True
        mark_runtime_override(cfg, "show_preview")
    if args.no_voice:
        cfg.voice_enabled = False
        mark_runtime_override(cfg, "voice_enabled")
    if args.camera is not None:
        cfg.camera_index = args.camera
        mark_runtime_override(cfg, "camera_index")

    stop = threading.Event()
    engine = GestureEngine(cfg)
    controller = Controller(cfg)
    speaker = Speaker(enabled=cfg.speak, cfg=cfg)
    speaker.start()

    overlay = Overlay(stop, cfg=cfg)
    controller.set_overlay(overlay)
    engine.set_overlay(overlay)
    ctx = Context(engine, overlay, speaker, stop, controller=controller, cfg=cfg)
    overlay.ctx = ctx
    ctx.observe_overlay(overlay)

    try:
        import system_control
        system_control.get_system_control().set_overlay(overlay)
    except Exception:
        pass

    from thread_supervisor import ThreadSupervisor, explain_graceful_degradation
    supervisor = ThreadSupervisor(stop_event=stop)
    ctx.supervisor = supervisor

    from updater import AutoUpdater
    updater = AutoUpdater(ctx=ctx, overlay=overlay, auto_check=cfg.auto_update_check)
    ctx.updater = updater
    overlay.updater = updater
    supervisor.register(
        name="AutoUpdater",
        factory=lambda: updater.start_periodic_check(interval_hours=4),
        max_restarts=3,
        backoff_base=2.0,
    )
    supervisor.start("AutoUpdater")

    def _make_tracker():
        t = Tracker(cfg, engine, controller, overlay, ctx, stop)
        ctx.tracker = t
        return t

    def _on_tracker_error(name, exc):
        explain_graceful_degradation("camera", overlay=overlay, speaker=speaker)

    supervisor.register(
        name="Tracker",
        factory=_make_tracker,
        max_restarts=-1,            # never terminal: keep retrying at the 60 s cap
        backoff_base=1.0,
        on_error=_on_tracker_error,
        heartbeat_names=["Tracker", "FrameGrabber"],
        stall_after=10.0,
    )
    supervisor.start("Tracker")

    if cfg.voice_enabled:
        def _on_voice_error(name, exc):
            explain_graceful_degradation("voice", overlay=overlay, speaker=speaker)

        def _make_voice():
            v = Voice(cfg, ctx, stop)
            ctx.voice = v
            return v

        supervisor.register(
            name="Voice",
            factory=_make_voice,
            max_restarts=-1,        # never terminal: keep retrying at the 60 s cap
            backoff_base=1.0,
            on_error=_on_voice_error,
            heartbeat_names=["Voice", "WakeWordDetector", "STTWorker"],
            stall_after=10.0,
        )
        supervisor.start("Voice")

    # Media Session Manager (Windows SMTC / Media Controls)
    from media_adapter import get_media_adapter
    media_adapter = get_media_adapter()

    def _on_media_changed(meta):
        if meta and meta.has_media:
            dur = float(meta.duration) if meta.duration else 0.0
            pos = float(meta.position) if meta.position else 0.0
            prog = (pos / dur) if dur > 0 else 0.0
            thumb = meta.get_artwork_bytes(fallback=True) if hasattr(meta, "get_artwork_bytes") else getattr(meta, "thumbnail_bytes", None)
            dom_col = meta.get_dominant_color() if hasattr(meta, "get_dominant_color") else getattr(meta, "dominant_color", None)
            overlay.post(
                "media_state",
                title=meta.title,
                artist=meta.artist,
                album=getattr(meta, "album", ""),
                playing=meta.is_playing,
                active=meta.has_media,
                position=pos,
                duration=dur,
                progress=prog,
                source=meta.source_app_id,
                thumbnail_bytes=thumb,
                dominant_color=dom_col,
            )
        else:
            overlay.post(
                "media_state",
                title="",
                artist="",
                album="",
                playing=False,
                active=False,
                position=0.0,
                duration=0.0,
                progress=0.0,
                source="",
                thumbnail_bytes=None,
                dominant_color=None,
            )

    media_adapter.add_listener(_on_media_changed)
    supervisor.register(
        name="MediaAdapter",
        factory=lambda: media_adapter.start(),   # returns its worker thread; the supervisor watches it
        max_restarts=3,
        backoff_base=2.0,
    )
    supervisor.start("MediaAdapter")

    # Weather: cached value posted immediately, 15 min background poll, refresh when the weather
    # page / collapsed pill becomes visible and the last success is older than 10 min.
    def _run_weather_poller():
        import time as _time
        from weather_client import (get_weather_client, weather_state_payload, save_weather_cache,
                                    load_weather_cache)
        wlog = _jlog.get_logger("weather")
        client = get_weather_client()
        wake = threading.Event()
        state = {"last_ok": 0.0, "last_try": 0.0, "failing": False}

        def _city():
            c = ctx.cfg
            return getattr(c, "island_weather_city", None) or getattr(c, "weather_city", "auto")

        def _units():
            c = ctx.cfg
            return getattr(c, "island_weather_units", None) or getattr(c, "weather_units", "auto")

        def _post_cached():
            cached = load_weather_cache(_city(), _units())
            if cached:
                overlay.post("weather_state", **cached["payload"], stale=cached["stale"], updated_at=cached["saved_at"])
                wlog.info("posted cached weather (stale=%s)", cached["stale"])
                if not cached["stale"]:
                    state["last_ok"] = _time.monotonic() - (_time.time() - cached["saved_at"])

        def _fetch_and_post(force=True):
            state["last_try"] = _time.monotonic()
            w = client.get_weather_for_city(_city(), units=_units(), force_refresh=force)
            if w is None:
                if not state["failing"]:
                    state["failing"] = True
                    wlog.warning("weather fetch failed; keeping last value")
                return
            payload = weather_state_payload(w)
            overlay.post("weather_state", **payload, stale=bool(w.stale), updated_at=w.timestamp)
            if w.stale:
                if not state["failing"]:
                    state["failing"] = True
                    wlog.warning("weather offline; showing stale value")
                return
            state["failing"] = False
            state["last_ok"] = _time.monotonic()
            save_weather_cache(payload, _city(), _units())

        def _on_page(page):
            # weather page, or collapsed (compact pill shows weather): refresh only if stale
            if page == "weather" or (page is None and getattr(ctx.cfg, "island_show_weather", True)
                                     and getattr(ctx.cfg, "island_idle_display", "time_weather") == "time_weather"):
                wake.set()

        ctx.add_page_listener(_on_page)
        try:
            from weather_client import register_weather_refresh_callback
            register_weather_refresh_callback(lambda: _fetch_and_post(force=True))
        except Exception:
            wlog.exception("could not register weather refresh callback")

        def _poller_loop():
            _post_cached()
            if not stop.wait(2.0):
                _fetch_and_post()
            next_bg = _time.monotonic() + 900.0
            while not stop.is_set():
                wake.wait(max(1.0, next_bg - _time.monotonic()))
                if stop.is_set():
                    break
                now = _time.monotonic()
                if wake.is_set():
                    wake.clear()
                    if now - state["last_ok"] < 600.0 or now - state["last_try"] < 60.0:
                        continue
                elif now < next_bg:
                    continue
                _fetch_and_post()
                next_bg = _time.monotonic() + 900.0

        t = threading.Thread(target=_poller_loop, name="WeatherPoller", daemon=True)
        t.start()
        return t

    supervisor.register(
        name="WeatherPoller",
        factory=_run_weather_poller,
        max_restarts=3,
        backoff_base=5.0,
    )
    supervisor.start("WeatherPoller")

    hardware_feed = HardwareFeed(ctx, overlay)
    ctx.hardware_feed = hardware_feed
    hardware_feed.start()
    start_fullscreen_watcher(ctx, overlay, stop)
    _jlog.start_heartbeat_monitor()

    # 1 s worker-health poll (supervisor monitor thread); posts to the overlay only on change.
    supervisor.start_health_monitor(
        on_health=lambda h: post_worker_health(overlay, h["state"], h["reason"]),
        context=lambda: {"engine": engine, "tracker": ctx.tracker, "voice": ctx.voice},
        interval=1.0,
    )

    # Global hotkeys: Ctrl+Alt+J toggles wake/standby, Ctrl+Alt+Q quits.
    try:
        from pynput import keyboard
        hotkeys = keyboard.GlobalHotKeys({
            "<ctrl>+<alt>+j": engine.request_toggle,
            "<ctrl>+<alt>+q": stop.set,
        })
        hotkeys.daemon = True
        hotkeys.start()
    except Exception as exc:
        print(f"[hotkeys] unavailable: {exc}")

    hour = datetime.datetime.now().hour
    part = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    ctx.say(f"{part}, sir. Jarvis is standing by. Raise your palm to wake me.")

    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        supervisor.stop(timeout=2)
        try:
            hardware_feed.stop()
        except Exception as exc:
            _jlog.get_logger("main").warning("hardware feed stop failed: %s", exc)
        try:
            media_adapter.stop(timeout=1.0)
        except Exception:
            pass
        controller.shutdown()
        speaker.q.put(None)
        if _INSTANCE_MUTEX and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
            except Exception:
                pass


if __name__ == "__main__":
    main()
