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
        # Efficiency governor (eco_qos) and system pause (lock/sleep) bookkeeping.
        self.eco = None
        self.background = False
        import personal
        self.personal = personal            # local profile/learning store (UI calls data_summary etc.)
        self._submit_lock = threading.Lock()   # one typed/spoken-as-text command at a time
        self._sys_lock = threading.Lock()
        self._sys_pause_reasons = set()
        self._gesture_before_pause = False
        # Live voice on/off (set_voice_enabled). _voice_on is the applied state; _voice_user_off marks a
        # user "off" so unlock/resume (system_resume) does not reopen the mic.
        self._voice_lock = threading.Lock()
        self._voice_on = bool(getattr(cfg, "voice_enabled", True)) if cfg is not None else True
        self._voice_user_off = not self._voice_on
        self._voice_starter = None          # main() installs it: registers + starts the Voice task
        self._voice_registered = False

    # ---- voice on/off (Settings switch, onboarding "Turn on voice") ----
    def install_voice_starter(self, starter):
        """main() hands over the function that registers and starts the Voice supervisor task.
        Starts it now when voice is on; otherwise set_voice_enabled(True) starts it later."""
        self._voice_starter = starter
        if self._voice_on:
            self._start_voice_task()

    def _start_voice_task(self):
        """Register + start the Voice task once. False when it already exists or no starter is set."""
        with self._voice_lock:
            if self._voice_registered or self._voice_starter is None:
                return False
            self._voice_registered = True
        self._voice_starter()
        return True

    def voice_should_be_suspended(self):
        """True while the user turned voice off or the PC is locked/asleep (applied to new Voice instances)."""
        with self._sys_lock:
            paused = bool(self._sys_pause_reasons)
        return self._voice_user_off or paused

    def set_voice_enabled(self, on, persist=True):
        """Turn voice commands on/off live. On starts the Voice task if it never ran, else un-suspends it;
        off suspends it (mic closed, no listening) without tearing threads down. Persists cfg.voice_enabled."""
        on = bool(on)
        log = _jlog.get_logger("main")
        log.info("voice commands %s (live)", "on" if on else "off")
        with self._voice_lock:
            self._voice_on = on
            self._voice_user_off = not on
        if persist and self.cfg is not None:
            try:
                import config as _config
                _config.update_key("voice_enabled", on, self.cfg)
                _config.clear_runtime_override(self.cfg, "voice_enabled")
            except Exception:
                log.exception("could not persist voice_enabled")
        if on and self._start_voice_task():
            return
        fn = getattr(self.voice, "set_suspended", None)
        if fn is not None:
            fn(self.voice_should_be_suspended())

    # ---- text input, one-shot listen, hide/show, pages (UI entry points; thread-safe, non-blocking) ----
    def submit_text(self, text):
        """Run the command pipeline as if spoken: no STT, no wake word. Returns False for empty text.
        Replies flow through the normal overlay events (prompt, thinking, say)."""
        text = " ".join(str(text or "").split())
        if not text:
            return False
        threading.Thread(target=self._run_submitted_text, args=(text,), name="SubmitText", daemon=True).start()
        return True

    def _run_submitted_text(self, text):
        log = _jlog.get_logger("main")
        with self._submit_lock:
            self._eco_activity()
            self.overlay.post("prompt", text=text, raw=text)
            self.overlay.post("thinking", active=True, timeout=8.0)
            try:
                from commands import run_command
                reply = run_command(text, self)
                if reply:
                    self.say(reply)
            except Exception:
                log.exception("submitted text command failed")
                self.overlay.post("error", text="That command failed.")
            finally:
                self.overlay.post("thinking", active=False)

    def listen_now(self):
        """Immediate one-shot listen without the wake word. False if voice is unavailable or busy."""
        v = self.voice
        if v is None or not hasattr(v, "request_listen") or self._voice_user_off:
            _jlog.get_logger("main").info("listen_now: voice not available")
            return False
        return bool(v.request_listen("listen_now"))

    def set_hidden(self, on):
        """Hide Jarvis (island appears only while listening/answering). Persisted in cfg.island_hidden."""
        on = bool(on)
        if self.cfg is not None:
            self.cfg.island_hidden = on
            try:
                import config as _config
                _config.save_config(self.cfg)
            except Exception:
                _jlog.get_logger("main").exception("could not persist island_hidden")
        _jlog.get_logger("main").info("island hidden=%s", on)
        self.overlay.post("hidden", on=on)

    def is_hidden(self):
        return bool(getattr(self.cfg, "island_hidden", False))

    def open_ask_page(self):
        self.overlay.post("ask_page", focus=True)

    def start_onboarding(self):
        self.overlay.post("onboarding")

    ISLAND_SURFACES = ("gradient", "near_black", "black", "white")

    def set_island_surface(self, style):
        """Validate, persist cfg.island_surface and tell the UI. Returns False for an unknown style."""
        style = str(style or "").strip().lower()
        if style not in self.ISLAND_SURFACES:
            _jlog.get_logger("main").warning("ignored invalid island surface %r", style)
            return False
        if self.cfg is not None:
            self.cfg.island_surface = style
            try:
                import config as _config
                _config.save_config(self.cfg)
            except Exception:
                _jlog.get_logger("main").exception("could not persist island_surface")
        self.overlay.post("island_surface", style=style)
        return True

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
        if not paused and not self.gesture_mode_on():
            self.set_gesture_mode(True)     # resuming with the camera off must open it
            return
        self.engine.request(not bool(paused))

    # ---- gesture mode (camera on-demand) ----
    def set_gesture_mode(self, on):
        """on=True opens the camera and wakes the gesture engine; on=False releases the camera and
        puts the engine in standby. Safe to call from any thread."""
        on = bool(on)
        _jlog.get_logger("main").info("gesture mode %s", "on" if on else "off")
        self._eco_busy("camera", on)
        tr = self.tracker
        if tr is not None and hasattr(tr, "set_camera_wanted"):
            tr.set_camera_wanted(on)
        self.engine.request(on)
        self.overlay.post("gesture_mode", on=on)
        self.say("Gesture mode on." if on else "Camera off.")

    def gesture_mode_on(self):
        tr = self.tracker
        if tr is not None and hasattr(tr, "camera_wanted"):
            return bool(tr.camera_wanted)
        return bool(getattr(self.cfg, "camera_autostart", False))

    def snapshot_frame(self, timeout=3.0):
        """One BGR frame for vision queries (opens the camera briefly if it is off). None on failure."""
        tr = self.tracker
        if tr is None or not hasattr(tr, "snapshot_frame"):
            return None
        return tr.snapshot_frame(timeout)

    def gesture_mode_auto_off(self):
        """Called by the Tracker when the idle timeout turned the camera off: quiet notice, no speech."""
        _jlog.get_logger("main").info("gesture mode off (idle timeout)")
        self._eco_busy("camera", False)
        self.engine.request(False)
        self.overlay.post("gesture_mode", on=False, auto=True)
        self.overlay.post("say", text="Camera off (idle).", speak=False)

    # ---- efficiency governor feed ----
    def _eco_busy(self, reason, busy):
        eco = self.eco
        if eco is not None:
            eco.set_busy(reason, busy)

    def _eco_activity(self):
        eco = self.eco
        if eco is not None:
            eco.activity()

    # ---- lock / sleep / device events (called from the win_events thread; non-blocking) ----
    def system_pause(self, reason):
        """Lock or suspend: pause voice, camera and cursor control, remembering gesture mode."""
        log = _jlog.get_logger("main")
        with self._sys_lock:
            first = not self._sys_pause_reasons
            self._sys_pause_reasons.add(reason)
            if not first:
                return
            was_on = self.gesture_mode_on()
            self._gesture_before_pause = was_on
        log.info("system pause (%s); gesture mode was %s", reason, "on" if was_on else "off")
        voice = self.voice
        fn = getattr(voice, "set_suspended", None)
        if fn is not None:
            fn(True)
        tr = self.tracker
        if tr is not None and hasattr(tr, "set_camera_wanted"):
            tr.set_camera_wanted(False)
        self.engine.request(False)
        fn = getattr(self.controller, "set_active", None)
        if fn is not None:
            fn(False)
        self._eco_busy("camera", False)

    def system_resume(self, reason, reopen_devices=False):
        """Unlock or resume: undo system_pause once every pause reason is cleared."""
        log = _jlog.get_logger("main")
        with self._sys_lock:
            self._sys_pause_reasons.discard(reason)
            if self._sys_pause_reasons:
                return
            restore = self._gesture_before_pause
            self._gesture_before_pause = False
        log.info("system resume (%s); restoring gesture mode=%s", reason, restore)
        voice = self.voice
        fn = getattr(voice, "set_suspended", None)
        if fn is not None and not self._voice_user_off:     # a user "off" survives unlock/resume
            fn(False)
        if reopen_devices:
            self.reopen_voice_devices()
        fn = getattr(self.controller, "set_active", None)
        if fn is not None:
            fn(True)
        if restore:
            tr = self.tracker
            if tr is not None and hasattr(tr, "set_camera_wanted"):
                tr.set_camera_wanted(True)
            self.engine.request(True)
            self.overlay.post("gesture_mode", on=True, auto=True)
            self._eco_busy("camera", True)
        self._eco_activity()

    def reopen_voice_devices(self):
        fn = getattr(self.voice, "reopen_devices", None)
        if fn is not None:
            fn()

    def devices_changed(self):
        with self._sys_lock:
            if self._sys_pause_reasons:
                return      # resume will reopen devices
        _jlog.get_logger("main").info("audio/camera devices changed; reopening")
        self.reopen_voice_devices()
        if self.gesture_mode_on():
            fn = getattr(self.tracker, "reopen_camera", None)
            if fn is not None:
                fn()

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
        if kind in ("listening", "wake_detected", "live_speech", "prompt", "action_executed") or (
                kind == "thinking" and d.get("active", True)) or (
                kind == "say" and d.get("speak", True)):
            self._eco_activity()
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
        want = getattr(self.cfg, "voice_enabled", None) if self.cfg is not None else None
        if isinstance(want, bool) and want != self._voice_on:
            self.set_voice_enabled(want, persist=False)    # Settings already saved the key
        self.personal.configure(self.cfg)
        lf = getattr(self, "lyrics_feed", None)
        if lf is not None:
            lf.refresh()

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


class LyricsFeed:
    """Fetches synced lyrics for the current track on one background worker (newest request wins) and posts
    overlay 'lyrics_state'(track_key, synced, plain, instrumental, status). Idle cost: none unless enabled."""

    def __init__(self, ctx, overlay, stop, client=None):
        self.ctx = ctx
        self.overlay = overlay
        self._stop = stop
        self._client = client
        self._log = _jlog.get_logger("lyrics")
        self._cv = threading.Condition()
        self._pending = None      # (key, title, artist, album, dur) newest request
        self._key = None          # key of the current track ("" when no media)
        self._meta = None         # (title, artist, album, dur) of the current track
        self._enabled = None
        self._thread = None

    def _enabled_now(self):
        return bool(getattr(self.ctx.cfg, "island_lyrics_enabled", False))

    def _post(self, key, status, synced=None, plain=None, instrumental=False):
        self.overlay.post("lyrics_state", track_key=key,
                          synced=[[t, x] for t, x in (synced or [])],
                          plain=plain, instrumental=instrumental, status=status)

    def on_media(self, meta):
        if meta and meta.has_media and (meta.title or meta.artist):
            title, artist = str(meta.title or ""), str(meta.artist or "")
            key = f"{title}|{artist}"
            self._meta = (title, artist, str(getattr(meta, "album", "") or ""),
                          float(meta.duration) if meta.duration else 0.0)
        else:
            key, self._meta = "", None
        if key == self._key:
            return
        self._key = key
        self._request()

    def refresh(self):
        """Called on config reload: react to the setting being toggled live."""
        en = self._enabled_now()
        if en != self._enabled:
            self._request()

    def _request(self):
        en = self._enabled_now()
        self._enabled = en
        key = self._key or ""
        if not en:
            with self._cv:
                self._pending = None
            self._post(key, "off")
            return
        if not self._meta:
            with self._cv:
                self._pending = None
            return
        self._post(key, "loading")
        with self._cv:
            self._pending = (key,) + self._meta
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="LyricsWorker", daemon=True)
                self._thread.start()
            self._cv.notify()

    def _worker(self):
        while not self._stop.is_set():
            with self._cv:
                while self._pending is None and not self._stop.is_set():
                    self._cv.wait(1.0)
                req, self._pending = self._pending, None
            if req is None:
                continue
            key, title, artist, album, dur = req
            try:
                if self._client is None:
                    from lyrics_client import get_lyrics_client
                    self._client = get_lyrics_client()
                lyr = self._client.get_lyrics(title, artist, album, dur)
                result = ("ok", lyr) if lyr else ("not_found", None)
            except Exception as exc:
                self._log.warning("lyrics lookup failed: %s: %s", type(exc).__name__, exc)
                result = ("error", None)
            if self._key != key or not self._enabled_now():
                continue   # superseded or disabled meanwhile
            status, lyr = result
            if lyr:
                self._post(key, status, lyr.synced, lyr.plain, lyr.instrumental)
            else:
                self._post(key, status)


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
    if last_error == ERROR_ALREADY_EXISTS:
        _jlog.get_logger("main").info(
            "single-instance mutex: already-exists (GetLastError=%d) census=%s", last_error, _jlog.process_census())
    else:   # the process census walks every process (0.2-1.8 s); logged after the first paint by log_census_later()
        _jlog.get_logger("main").info("single-instance mutex: acquired (GetLastError=%d)", last_error)

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


def post_cached_weather(ctx, overlay):
    """Post the on-disk weather cache (no network) so the compact pill has data on the first frame."""
    log = _jlog.get_logger("weather")
    try:
        from weather_client import load_weather_cache
        c = ctx.cfg
        city = getattr(c, "island_weather_city", None) or getattr(c, "weather_city", "auto")
        units = getattr(c, "island_weather_units", None) or getattr(c, "weather_units", "auto")
        cached = load_weather_cache(city, units)
    except Exception:
        log.exception("early cached-weather post failed")
        return False
    if not cached:
        return False
    overlay.post("weather_state", **cached["payload"], stale=cached["stale"], updated_at=cached["saved_at"])
    log.info("posted cached weather before workers (stale=%s)", cached["stale"])
    return True


FIRST_RUN_ONBOARDING_DELAY_S = 1.5


def schedule_first_run_onboarding(ctx, delay=FIRST_RUN_ONBOARDING_DELAY_S):
    """Show onboarding once, shortly after the island first paints (not for --background starts).
    Returns the waiter thread, or None when it is not needed."""
    if getattr(ctx.cfg, "onboarding_done", False) or ctx.background:
        return None

    def _go():
        if not ctx.stop.wait(delay):
            ctx.start_onboarding()

    t = threading.Thread(target=_go, name="OnboardingStart", daemon=True)
    t.start()
    return t


def _start_workers_after_first_frame(fn):
    """Run fn on a background thread once the Qt loop has started (island painted); no Qt -> start immediately."""
    t = threading.Thread(target=fn, name="WorkerStartup", daemon=True)
    try:
        from PySide6.QtCore import QTimer
    except ImportError:
        _jlog.get_logger("main").warning("PySide6 unavailable; starting workers immediately")
        t.start()
        return t
    QTimer.singleShot(0, t.start)
    return t


WARM_DELAY_S = 5.0


def log_census_later(stop, delay=WARM_DELAY_S):
    """Diagnostic only: log the JARVIS process census off the startup path (it iterates every process)."""
    def _run():
        if stop.wait(delay):
            return
        log = _jlog.get_logger("main")
        t0 = time.perf_counter()
        log.info("process census (deferred): %s (%.0f ms)", _jlog.process_census(), (time.perf_counter() - t0) * 1000)

    t = threading.Thread(target=_run, name="Census", daemon=True)
    t.start()
    return t
# Modules the first voice command would otherwise import lazily (60-115 ms each). Import only: no devices, no network.
WARM_MODULES = ("comtypes", "pycaw.pycaw", "winrt.windows.media.control", "winrt.windows.storage.streams",
                "winreg", "window_resolver", "requests")


def warm_first_use_imports(stop, delay=WARM_DELAY_S, modules=WARM_MODULES, lower_priority=True):
    """Daemon thread: after `delay` s of settled startup, import the lazily-imported command dependencies and build an
    (unused, offline) requests.Session so the first voice command is fast. Returns the thread."""
    log = _jlog.get_logger("warm")

    def _run():
        if stop.wait(delay):
            return
        if lower_priority and sys.platform == "win32":
            try:  # THREAD_PRIORITY_BELOW_NORMAL
                k32 = ctypes.windll.kernel32
                k32.GetCurrentThread.restype = ctypes.c_void_p
                k32.SetThreadPriority(ctypes.c_void_p(k32.GetCurrentThread()), -1)
            except Exception as exc:
                log.warning("could not lower warm-up thread priority: %s", exc)
        import importlib
        t_all = time.perf_counter()
        for name in modules:
            if stop.is_set():
                return
            t0 = time.perf_counter()
            try:
                importlib.import_module(name)
            except Exception as exc:
                log.warning("warm import %s skipped: %s: %s", name, type(exc).__name__, exc)
                continue
            log.debug("warm import %s %.0f ms", name, (time.perf_counter() - t0) * 1000)
        if "requests" in sys.modules:
            try:
                sys.modules["requests"].Session().close()   # builds adapters/ssl context; opens no connection
            except Exception as exc:
                log.warning("requests session warm failed: %s: %s", type(exc).__name__, exc)
        if stop.is_set():
            return
        t0 = time.perf_counter()
        try:   # first-ever decision-engine call builds/loads its index; classify() on a dummy phrase, no action runs
            from decision_engine import get_decision_engine
            get_decision_engine().classify("warm up")
        except Exception as exc:
            log.warning("decision engine warm failed: %s: %s", type(exc).__name__, exc)
        log.debug("decision engine warm %.0f ms", (time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        try:   # the command pipeline's lazy imports and regex compiles; pure predicates only (no ctx, no actions)
            import chained_commands, commands, context_resolver, math_parser, quick_answers, screen_awareness
            screen_awareness.is_screen_query("warm up")
            quick_answers.is_unsafe_shell_command("warm up")
            quick_answers.is_timezone_query("warm up")
            chained_commands.split_chained_commands("warm up")
            math_parser.parse_math_or_unit_query("warm up")
            context_resolver.extract_entities_from_text("warm up")
            commands.strip_wake_prefix("warm up")
        except Exception as exc:
            log.warning("command pipeline warm failed: %s: %s", type(exc).__name__, exc)
        log.debug("command pipeline warm %.0f ms", (time.perf_counter() - t0) * 1000)
        if sys.platform == "win32" and "comtypes" in sys.modules:
            t0 = time.perf_counter()
            try:   # first COM init in the process; no device, endpoint or volume call
                com = sys.modules["comtypes"]
                com.CoInitialize()
                com.CoUninitialize()
            except Exception as exc:
                log.warning("COM warm failed: %s: %s", type(exc).__name__, exc)
            log.debug("COM warm %.0f ms", (time.perf_counter() - t0) * 1000)
        log.info("first-use imports warmed in %.0f ms", (time.perf_counter() - t_all) * 1000)

    t = threading.Thread(target=_run, name="WarmImports", daemon=True)
    t.start()
    return t


def main():
    _jlog.setup()
    if any(arg in sys.argv for arg in ("-h", "--help", "--island-demo", "--perf-report", "--self-test")):
        pass
    elif not acquire_single_instance_lock():
        sys.exit(0)

    _enable_dpi_awareness()
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. gesture control")
    parser.add_argument("--preview", action="store_true", help="show the camera HUD window")
    parser.add_argument("--background", action="store_true",
                        help="started at login: no welcome speech or popups; island as normal")
    parser.add_argument("--no-voice", action="store_true", help="disable voice commands")
    parser.add_argument("--camera", type=int, default=None, help="camera index (default 0)")
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

    from config import load_config, mark_runtime_override
    from gestures import GestureEngine
    from overlay import Overlay

    cfg = load_config()
    import personal
    personal.configure(cfg)
    _jlog.set_debug_transcripts(getattr(cfg, "debug_log_transcripts", False))
    # CLI flags are runtime-only: mark them so a later whole-file save_config(cfg) never persists them.
    if args.preview:
        cfg.show_preview = True
        mark_runtime_override(cfg, "show_preview")
    if args.no_voice:
        cfg.voice_enabled = False
        mark_runtime_override(cfg, "voice_enabled")
    if args.camera is not None:
        cfg.camera_index = args.camera
        mark_runtime_override(cfg, "camera_index")

    # ---- island first: QApplication + Overlay are created and shown before any heavy worker ----
    stop = threading.Event()
    engine = GestureEngine(cfg)
    overlay = Overlay(stop, cfg=cfg)
    engine.set_overlay(overlay)
    ctx = Context(engine, overlay, None, stop, cfg=cfg)   # speaker/controller/tracker/voice are None until workers start
    overlay.ctx = ctx
    ctx.observe_overlay(overlay)
    ctx.background = bool(args.background)

    from thread_supervisor import ThreadSupervisor, explain_graceful_degradation
    supervisor = ThreadSupervisor(stop_event=stop)
    ctx.supervisor = supervisor
    post_cached_weather(ctx, overlay)

    controller = speaker = media_adapter = hardware_feed = None

    def _start_workers():
        """Heavy startup (controller, Speaker/Piper, Tracker, Voice, media, feeds); runs off the GUI thread."""
        nonlocal controller, speaker, media_adapter, hardware_feed
        try:
            _start_workers_impl()
        except BaseException:
            _jlog.get_logger("main").exception("worker startup failed")
            stop.set()
            raise

    def _start_workers_impl():
        nonlocal controller, speaker, media_adapter, hardware_feed
        try:
            import system_control
            system_control.get_system_control().set_overlay(overlay)
        except Exception:
            pass
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

        from controller import Controller   # deliberately after the island is up
        from tracker import Tracker
        from voice import Speaker, Voice
        controller = Controller(cfg)
        ctx.controller = controller
        controller.set_overlay(overlay)
        speaker = Speaker(enabled=cfg.speak, cfg=cfg)
        ctx.speaker = speaker
        speaker.start()

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

        def _on_voice_error(name, exc):
            explain_graceful_degradation("voice", overlay=overlay, speaker=speaker)

        def _make_voice():
            v = Voice(ctx.cfg or cfg, ctx, stop)
            if ctx.voice_should_be_suspended():     # user turned it off / PC locked before a (re)start
                v.set_suspended(True)
            ctx.voice = v
            return v

        def _register_voice():
            """Startup (voice on) and ctx.set_voice_enabled(True) both start voice through this."""
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

        ctx.install_voice_starter(_register_voice)   # starts now if cfg.voice_enabled

        # Media Session Manager (Windows SMTC / Media Controls)
        from media_adapter import get_media_adapter
        media_adapter = get_media_adapter()

        ctx.lyrics_feed = LyricsFeed(ctx, overlay, stop)

        def _on_media_changed(meta):
            ctx.lyrics_feed.on_media(meta)
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
        _jlog.start_thread_cpu_sampler(interval=10.0)

        # 1 s worker-health poll (supervisor monitor thread); posts to the overlay only on change.
        supervisor.start_health_monitor(
            on_health=lambda h: post_worker_health(overlay, h["state"], h["reason"]),
            context=lambda: {"engine": engine, "tracker": ctx.tracker, "voice": ctx.voice},
            interval=1.0,
        )

        def _hotkey_toggle_hidden():
            ctx.set_hidden(not ctx.is_hidden())

        def _hotkey_gesture_toggle():
            if not ctx.gesture_mode_on():
                ctx.set_gesture_mode(True)
                return
            # Camera already on: the toggle flips wake/standby; going to standby also releases the camera.
            pending = getattr(engine, "_req", None)
            active = (pending if pending not in (None, "toggle") else bool(getattr(engine, "active", True)))
            if active:
                ctx.set_gesture_mode(False)
            else:
                engine.request(True)

        # Efficiency mode: governor is fed by overlay events (voice/commands) and gesture mode.
        from eco_qos import EfficiencyGovernor
        ctx.eco = EfficiencyGovernor(idle_after=10.0)
        ctx.eco.set_busy("camera", ctx.gesture_mode_on())

        # Native Windows events: hotkeys (no keyboard hook), lock/sleep, device changes.
        from win_events import WinEvents
        win_events = WinEvents(
            on_toggle=_hotkey_gesture_toggle,
            on_quit=stop.set,
            on_hide=_hotkey_toggle_hidden,
            on_ask=ctx.open_ask_page,
            on_suspend=lambda: ctx.system_pause("suspend"),
            on_resume=lambda: ctx.system_resume("suspend", reopen_devices=True),
            on_lock=lambda: ctx.system_pause("lock"),
            on_unlock=lambda: ctx.system_resume("lock"),
            on_devices_changed=ctx.devices_changed,
        )
        ctx.win_events = win_events
        win_events.start()

        hour = datetime.datetime.now().hour
        part = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        if not ctx.background:
            ctx.say(f"{part}, {personal.address_word()}. Jarvis is standing by. Raise your palm to wake me.")
        schedule_first_run_onboarding(ctx)

    _start_workers_after_first_frame(_start_workers)
    warm_first_use_imports(stop)
    log_census_later(stop)

    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for _name in ("win_events", "eco"):
            _obj = getattr(ctx, _name, None)
            try:
                if _obj is not None:
                    _obj.stop()
            except Exception:
                _jlog.get_logger("main").exception("%s stop failed", _name)
        supervisor.stop(timeout=2)
        try:
            if hardware_feed is not None:
                hardware_feed.stop()
        except Exception as exc:
            _jlog.get_logger("main").warning("hardware feed stop failed: %s", exc)
        try:
            if media_adapter is not None:
                media_adapter.stop(timeout=1.0)
        except Exception:
            pass
        if controller is not None:
            controller.shutdown()
        if speaker is not None:
            speaker.q.put(None)
        if _INSTANCE_MUTEX and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
            except Exception:
                pass


if __name__ == "__main__":
    main()
