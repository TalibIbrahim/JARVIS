"""
Overlay manager for J.A.R.V.I.S.: owns the Qt application, the Dynamic Island, the cursor
feedback overlay, the tray icon and the Settings window, and marshals thread-safe
``post(kind, **data)`` events onto the GUI thread.

The widgets themselves live in the ui/ subpackage:
  ui/island.py          — AppleDynamicIsland
  ui/cursor_overlay.py  — CursorFeedbackOverlay
  ui/settings_window.py — SettingsWindow
  ui/tray.py            — JarvisTray
"""
import os
import queue
import sys
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication

from ui.island import AppleDynamicIsland
from ui.cursor_overlay import CursorFeedbackOverlay
from ui.settings_window import SettingsWindow
from ui.tray import JarvisTray
from ui import glass as _glass          # Liquid Glass material (radial menu, tooltips, Settings)
from command_catalog import shows_action_chip


def _qt_excepthook(exc_type, exc_val, exc_tb):
    import traceback
    traceback.print_exception(exc_type, exc_val, exc_tb)


sys.excepthook = _qt_excepthook


def get_app_icon():
    """Finds and returns the high-resolution J.A.R.V.I.S. QIcon."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("jarvis.ico", "jarvis.png", "jarvis_tray.png"):
        p = os.path.join(base_dir, "assets", name)
        if os.path.exists(p):
            return QIcon(p)
    return None


class Overlay:
    """Public interface for the Apple HIG Dynamic Island & Cursor Overlays."""

    def __init__(self, stop_event, cfg=None, ctx=None):
        self.stop = stop_event
        self.cfg = cfg
        self.ctx = ctx
        # Optional hook wired by main.py: called as on_gesture_mode_toggle(new_state: bool) from the
        # tray "Gesture mode" item (UI thread). Falls back to ctx.set_gesture_mode(on) when unset.
        self.on_gesture_mode_toggle = None
        self.updater = None
        self.q = queue.Queue()
        self.settings_dlg = None

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app_icon = get_app_icon()
        if self.app_icon and not self.app_icon.isNull():
            self.app.setWindowIcon(self.app_icon)

        self.island         = AppleDynamicIsland(self)
        self.cursor_overlay = CursorFeedbackOverlay()
        self.tray           = JarvisTray(self, self.app)

        try:
            _glass.configure(self.cfg)                       # glass_mode / glass_transparency
        except Exception as _glass_err:
            print(f"[overlay] glass init error: {_glass_err}")

        self.island.hide_empty_quote = True       # SPEAKING without a prompt: no empty "" line
        self.island.show()
        self.island.make_click_through()
        self.cursor_overlay.show()
        if getattr(self.cfg, "island_hidden", False) and hasattr(self.island, "set_hidden"):
            self.island.set_hidden(True)          # Hide mode at startup: absent until voice activity

        if "--island-demo" in sys.argv:
            try:
                from ui.island import IslandDemoController
                self._island_demo = IslandDemoController(self.island)
                self._island_demo.start()
            except Exception as _demo_err:
                print(f"[overlay] demo start error: {_demo_err}")

        # Adaptive frame timer: 16 ms (60 FPS) only while something animates, otherwise a slow
        # housekeeping poll (queue drain, hover detection, minute clock) at IDLE_TICK_MS.
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(self.FAST_TICK_MS)

        # Pre-create SettingsWindow hidden once the GUI has been idle for a while (never in the
        # first 5 s of startup); opening Settings earlier simply builds it on demand.
        self._prewarm_started = time.monotonic()
        QTimer.singleShot(5000, self._prewarm_poll)

    FAST_TICK_MS = 16
    IDLE_TICK_MS = 50
    _PREWARM_IDLE_S = 3.0
    _PREWARM_FORCE_S = 40.0

    def _gui_idle_seconds(self):
        """Seconds since the last user input or island repaint request (0.0 if unknown/busy)."""
        idle = time.monotonic() - getattr(self.island, "_last_activity_t", 0.0)
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class _LII(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
                lii = _LII()
                lii.cbSize = ctypes.sizeof(_LII)
                if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                    in_idle = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
                    idle = min(idle, max(0.0, in_idle))
            except Exception:
                pass
        return idle

    def _prewarm_poll(self):
        """Idle-gated prewarm: build Settings (then its remaining tabs, one per idle poll)."""
        if not self.cfg or not self.ctx or self.stop.is_set():
            return
        waited = time.monotonic() - getattr(self, "_prewarm_started", time.monotonic())
        idle_ok = self._gui_idle_seconds() >= self._PREWARM_IDLE_S or waited >= self._PREWARM_FORCE_S
        dlg = self.settings_dlg
        if dlg is None:
            if idle_ok:
                self._prewarm_settings()
        elif idle_ok and not dlg.isVisible() and hasattr(dlg, "build_next_pending_page"):
            try:
                if not dlg.build_next_pending_page():
                    return                              # everything built
            except Exception as exc:
                print(f"[overlay] prewarm settings page failed: {exc}")
                return
        elif not hasattr(dlg, "build_next_pending_page"):
            return
        QTimer.singleShot(1000, self._prewarm_poll)

    def _prewarm_settings(self):
        """Pre-creates the SettingsWindow hidden when idle to eliminate opening latency."""
        if not self.cfg or not self.ctx:
            return
        if self.stop.is_set() or self.settings_dlg is not None:
            return
        try:
            self.settings_dlg = SettingsWindow(self.cfg, self.ctx, updater=self.updater)
            if hasattr(self.settings_dlg, "winId"):
                _ = self.settings_dlg.winId()
            self.settings_dlg.hide()
        except Exception as exc:
            print(f"[overlay] prewarm settings failed: {exc}")

    def post(self, kind, **data):
        """Thread-safe state update."""
        self.q.put((kind, data))

    def run(self):
        """Run the main GUI loop."""
        self.app.exec()

    def reload_config(self, new_cfg=None):
        """Live updates settings across Dynamic Island, overlays, and background subsystems."""
        if new_cfg is not None:
            self.cfg = new_cfg
        try:
            _glass.configure(self.cfg)                           # live Clear/Tinted + Transparency
        except Exception:
            pass
        if hasattr(self, "island") and hasattr(self.island, "reload_config"):
            self.island.reload_config()
        if hasattr(self, "cursor_overlay") and hasattr(self.cursor_overlay, "reload_config"):
            self.cursor_overlay.reload_config()
        if hasattr(self, "tray") and hasattr(self.tray, "reload_config"):
            self.tray.reload_config()
        if hasattr(self, "ctx") and self.ctx and hasattr(self.ctx, "reload_config"):
            self.ctx.reload_config(self.cfg)

    def open_settings(self, initial_tab=0):
        if not self.cfg or not self.ctx:
            return
        # Thread-safe marshalling: if invoked from background voice/hotkey thread, queue to GUI loop
        if threading.current_thread() is not threading.main_thread():
            self.post("open_settings", initial_tab=initial_tab)
            return

        if self.settings_dlg is None:
            self.settings_dlg = SettingsWindow(self.cfg, self.ctx, updater=self.updater)

        self.settings_dlg.select_tab(initial_tab)
        self.settings_dlg.show()
        self.settings_dlg.raise_()
        self.settings_dlg.activateWindow()

    def quit_app(self):
        self.stop.set()
        self.app.quit()

    def set_sending_indicator(self, active=True, label="Analyzing Screen", timeout=8.0):
        self.post("sending_indicator", active=active, label=label, timeout=timeout)

    def hud_volume(self, level=50, muted=False, duration=1.5):
        self.post("hud_volume", level=level, muted=muted, duration=duration)

    def hud_switch_window(self, title="", duration=1.5):
        self.post("hud_switch_window", title=title, duration=duration)

    def _drain(self, now):
        try:
            while True:
                kind, d = self.q.get_nowait()
                if kind == "state":
                    self.island.active = d["active"]
                    self.island.label = d["label"]
                    self.island.progress = d.get("progress", 0.0)
                    self.island.hands = d.get("hands", 0)
                    self.island.landmarks = d.get("landmarks", [])
                    self.island.precision_active = d.get("precision", False)
                    self.tray.update_status(d["active"])
                    self.cursor_overlay.update_state(d)
                elif kind == "prompt":
                    prompt_txt = str(d.get("text", "")).strip()
                    if prompt_txt:
                        low = prompt_txt.lower()
                        if any(w in low for w in ("show media", "show music", "now playing", "play music")):
                            if hasattr(self.island, "expand_card"):
                                self.island.expand_card("media")
                        elif any(w in low for w in ("show weather", "what's the weather", "weather forecast")):
                            if hasattr(self.island, "expand_card"):
                                self.island.expand_card("weather")
                        elif any(w in low for w in ("show time", "show clock")):
                            if hasattr(self.island, "expand_card"):
                                self.island.expand_card("time")
                        elif any(w in low for w in ("hide card", "collapse card", "close card")):
                            if hasattr(self.island, "collapse_card"):
                                self.island.collapse_card()
                        self.island.target_prompt = prompt_txt
                        self.island.prompt_type_index = len(prompt_txt)
                        self.island.last_type_time = now
                        self.island.is_live_speech = False
                        self.island.user_prompt = prompt_txt
                        self.island.prompt_until = now + max(4.0, len(prompt_txt) * 0.09)
                        self.island.listening = False
                        self.island.listening_until = 0.0
                        self.island.is_transcribing = False
                elif kind == "live_speech":
                    live_txt = str(d.get("text", "")).strip()
                    if live_txt:
                        if hasattr(self.island, "set_partial_transcript"):
                            self.island.set_partial_transcript(live_txt, now)
                        else:
                            self.island.target_prompt = live_txt
                            self.island.user_prompt = live_txt
                            self.island.prompt_type_index = len(live_txt)
                        self.island.is_live_speech = not d.get("is_final", False)
                        self.island.prompt_until = now + 4.0
                        self.island.listening = True
                        self.island.listening_until = now + 4.0
                        self.island.is_transcribing = False
                elif kind == "listening_done":
                    self.island.listening = False
                    self.island.listening_until = 0.0
                    self.island.is_transcribing = False
                elif kind == "say":
                    text = d.get("text", "")
                    self.island.say_text = text
                    self.island.say_until = now + max(3.5, len(text) * 0.08)
                    self.island.thinking = False
                    self.island.thinking_until = 0.0
                    if self.island.user_prompt:
                        self.island.prompt_until = max(self.island.prompt_until, self.island.say_until)
                    if d.get("speak", True) and text and self.ctx and hasattr(self.ctx, "speaker"):
                        self.ctx.speaker.say(text)
                elif kind == "wake":
                    if hasattr(self.island, "trigger_wake"):
                        self.island.trigger_wake(duration=d.get("duration", 0.5))
                    else:
                        self.island.spring_w.vel = 140.0
                elif kind == "wake_detected":
                    self.island.listening = True
                    self.island.listening_until = now + getattr(self.cfg, "command_window", 7.0)
                    self.island.is_transcribing = False
                    if hasattr(self.island, "trigger_wake_pulse"):
                        self.island.trigger_wake_pulse()
                    else:
                        self.island.spring_w.vel = 140.0
                    if d.get("show_wake", False) and hasattr(self.island, "trigger_wake"):
                        self.island.trigger_wake(duration=d.get("duration", 0.5))
                elif kind == "result":
                    text = d.get("text", "Directive Confirmed")
                    icon = d.get("icon", "success")
                    duration = float(d.get("duration", 2.5))
                    if str(icon).lower() in ("globe", "world", "clock", "time", "book", "fact", "wiki", "timezone") and hasattr(self.island, "trigger_quick_answer"):
                        self.island.trigger_quick_answer(text=text, icon=icon, duration=duration)
                    elif hasattr(self.island, "trigger_result"):
                        self.island.trigger_result(text=text, icon=icon, duration=duration)
                elif kind == "sending_indicator":
                    active = bool(d.get("active", True))
                    label = d.get("label", "Analyzing Screen")
                    timeout = float(d.get("timeout", 8.0))
                    if hasattr(self.island, "set_sending_indicator"):
                        self.island.set_sending_indicator(active=active, label=label, timeout=timeout)
                elif kind == "quick_answer":
                    text = d.get("text") or d.get("answer") or d.get("result") or ""
                    icon = d.get("icon")
                    if not icon:
                        cat = str(d.get("category") or d.get("type") or "").lower()
                        if any(w in cat for w in ("timezone", "time", "clock", "zone")):
                            icon = "globe"
                        elif any(w in cat for w in ("fact", "wiki", "book", "knowledge", "definition")):
                            icon = "book"
                        else:
                            icon = "globe" if any(k in text.lower() for k in ("utc", "gmt", "pm", "am", "time in", "time zone")) else "book"
                    duration = float(d.get("duration", 4.0))
                    if hasattr(self.island, "trigger_quick_answer"):
                        self.island.trigger_quick_answer(text=text, icon=icon, duration=duration)
                elif kind == "hud_volume":
                    level = int(d.get("level", 50))
                    muted = bool(d.get("muted", False))
                    duration = float(d.get("duration", 1.5))
                    if hasattr(self.island, "trigger_hud_volume"):
                        self.island.trigger_hud_volume(level=level, muted=muted, duration=duration)
                elif kind in ("hud_switch_window", "hud_window"):
                    title = str(d.get("title", ""))
                    duration = float(d.get("duration", 1.5))
                    if hasattr(self.island, "trigger_hud_window"):
                        self.island.trigger_hud_window(title=title, duration=duration)
                elif kind == "action_executed":
                    action = d.get("action", "")
                    success = d.get("success", True)
                    if action in ("volume_up", "volume_down", "set_volume") and "level" in d:
                        lvl = int(d["level"])
                        if hasattr(self.island, "trigger_hud_volume"):
                            self.island.trigger_hud_volume(level=lvl, muted=bool(d.get("muted", False)), duration=1.5)
                    elif action == "switch_window":
                        win_title = str(d.get("title") or d.get("window") or "Switch Window")
                        if hasattr(self.island, "trigger_hud_window"):
                            self.island.trigger_hud_window(title=win_title, duration=1.5)
                    elif shows_action_chip(action):
                        # System actions only; small talk, time, weather, Q&A keep their plain reply.
                        label = action.replace("_", " ").title()
                        if hasattr(self.island, "trigger_result"):
                            self.island.trigger_result(text=label, icon="success" if success else "error", duration=2.5)
                elif kind == "no_speech":
                    if hasattr(self.island, "trigger_no_speech"):
                        self.island.trigger_no_speech()
                elif kind == "listening":
                    self.island.listening = True
                    self.island.listening_until = d.get("until", now + 5.0)
                    self.island.is_transcribing = d.get("transcribing", False)
                    if d.get("wake", False):
                        if hasattr(self.island, "trigger_wake_pulse"):
                            self.island.trigger_wake_pulse()
                        else:
                            self.island.spring_w.vel = 140.0
                elif kind == "thinking":
                    active = bool(d.get("active", True))
                    self.island.thinking = active
                    if active:
                        timeout_sec = float(d.get("timeout", 8.0))
                        self.island.thinking_until = now + timeout_sec
                        if self.island.user_prompt:
                            self.island.prompt_until = max(self.island.prompt_until, self.island.thinking_until)
                    else:
                        self.island.thinking_until = 0.0
                elif kind == "ai_error":
                    self.island.thinking = False
                    self.island.thinking_until = 0.0
                    err_text = d.get("text", "AI Error")
                    self.island.error_text = err_text
                    self.island.error_until = now + 4.5
                    self.island.is_ai_error = True
                    if d.get("speak", True) and err_text and self.ctx and hasattr(self.ctx, "speaker"):
                        self.ctx.speaker.say(f"Notice: {err_text}")
                elif kind == "error":
                    self.island.thinking = False
                    self.island.thinking_until = 0.0
                    err_text = d.get("text", "Error")
                    self.island.error_text = err_text
                    self.island.error_until = now + 5.0
                    self.island.is_ai_error = False
                    if d.get("speak", False) and err_text and self.ctx and hasattr(self.ctx, "speaker"):
                        self.ctx.speaker.say(f"Alert: {err_text}")
                elif kind == "cursor_dwell":
                    self.cursor_overlay.update_dwell(d)
                elif kind == "open_settings":
                    self.open_settings(d.get("initial_tab", 0))
                elif kind == "disambiguate":
                    candidates = d.get("candidates", [])
                    callback = d.get("callback") or d.get("on_select")
                    header = d.get("header") or d.get("prompt") or "Which one, sir?"
                    duration = float(d.get("duration", 12.0))

                    def _on_select(selected_cand):
                        if callable(callback):
                            try:
                                callback(selected_cand)
                            except TypeError:
                                callback(selected_cand)
                            except Exception as exc:
                                print(f"[overlay] disambiguate callback error: {exc}")
                        elif isinstance(selected_cand, dict):
                            act = selected_cand.get("action") or selected_cand.get("callback")
                            if callable(act):
                                try:
                                    act()
                                except Exception as exc:
                                    print(f"[overlay] candidate action error: {exc}")
                            elif isinstance(act, str) and self.ctx and hasattr(self.ctx, "execute_action"):
                                try:
                                    self.ctx.execute_action(act)
                                except Exception as exc:
                                    print(f"[overlay] ctx execute_action error: {exc}")

                    if hasattr(self.island, "trigger_disambiguate"):
                        self.island.trigger_disambiguate(
                            candidates, callback=_on_select, header=header, duration=duration
                        )
                elif kind == "disambiguate_selected":
                    cand = d.get("candidate")
                    title = d.get("title", "")
                    idx = d.get("index", 0)
                    if isinstance(cand, dict):
                        act = cand.get("action") or cand.get("callback")
                        if callable(act):
                            try:
                                act()
                            except Exception as exc:
                                print(f"[overlay] candidate action error: {exc}")
                        elif isinstance(act, str) and self.ctx and hasattr(self.ctx, "execute_action"):
                            try:
                                self.ctx.execute_action(act)
                            except Exception as exc:
                                print(f"[overlay] ctx execute_action error: {exc}")
                    if self.ctx and hasattr(self.ctx, "on_disambiguate_selected"):
                        try:
                            self.ctx.on_disambiguate_selected(cand)
                        except Exception:
                            pass
                elif kind == "reload_config":
                    self.reload_config(d.get("cfg"))
                elif kind == "expand_card":
                    card = d.get("card", "weather")
                    if hasattr(self.island, "expand_card"):
                        self.island.expand_card(card)
                elif kind == "worker_health":
                    if hasattr(self.island, "set_worker_health"):
                        self.island.set_worker_health(str(d.get("state", "")), str(d.get("reason", "")))
                elif kind == "collapse_card":
                    if hasattr(self.island, "collapse_card"):
                        self.island.collapse_card()
                elif kind == "fullscreen":
                    if hasattr(self.island, "set_fullscreen"):
                        self.island.set_fullscreen(d.get("active", d.get("is_fullscreen", True)))
                elif kind == "media_state":
                    if hasattr(self.island, "media_title"):
                        if "title" in d: self.island.media_title = str(d["title"])
                        if "artist" in d: self.island.media_artist = str(d["artist"])
                        if "album" in d: self.island.media_album = str(d["album"])
                        if "playing" in d: self.island.media_playing = bool(d["playing"])
                        if "active" in d: self.island.media_active = bool(d["active"])
                        dragging = getattr(self.island, "_dragging_scrubber", False)   # the user owns the knob mid-drag
                        if "progress" in d and not dragging: self.island.media_progress = float(d["progress"])
                        if "duration" in d: self.island.media_duration_sec = float(d["duration"])
                        if "position" in d and not dragging: self.island.media_position_sec = float(d["position"])
                        if "source" in d: self.island.media_source = str(d["source"])
                        if "thumbnail_bytes" in d:
                            t_bytes = d["thumbnail_bytes"]
                            if t_bytes and hasattr(self.island, "set_thumbnail_bytes"):
                                self.island.set_thumbnail_bytes(t_bytes)
                            elif t_bytes is None:
                                self.island.media_album_art = None
                                if hasattr(self.island, "_icon_cache"):
                                    self.island._icon_cache.clear()
                        if "dominant_color" in d:
                            c_val = d["dominant_color"]
                            if c_val and hasattr(self.island, "media_dominant_color"):
                                self.island.media_dominant_color = QColor(c_val) if isinstance(c_val, str) else c_val
                elif kind == "lyrics_state":
                    if hasattr(self.island, "set_lyrics_state"):
                        self.island.set_lyrics_state(**d)
                elif kind == "weather_state":
                    if hasattr(self.island, "weather_temp"):
                        if "temp" in d: self.island.weather_temp = float(d["temp"])
                        if "unit_symbol" in d: self.island.weather_unit_symbol = str(d["unit_symbol"])
                        if "condition" in d: self.island.weather_condition = str(d["condition"])
                        if "icon" in d: self.island.weather_icon = str(d["icon"])
                        if "high" in d: self.island.weather_high = float(d["high"])
                        if "low" in d: self.island.weather_low = float(d["low"])
                        if "feels_like" in d: self.island.weather_feels_like = float(d["feels_like"])
                        if "city" in d: self.island.weather_city = str(d["city"])
                        if "hourly" in d: self.island.weather_hourly = list(d["hourly"])
                        if d.get("wind_kmh") is not None: self.island.weather_wind_kmh = float(d["wind_kmh"])
                        if d.get("wind_dir_deg") is not None: self.island.weather_wind_dir_deg = float(d["wind_dir_deg"])
                        if d.get("humidity") is not None: self.island.weather_humidity = int(round(float(d["humidity"])))
                        if "stale" in d: self.island.weather_stale = bool(d["stale"])
                        if d.get("updated_at") is not None: self.island.weather_updated_at = float(d["updated_at"])
                elif kind == "hardware_state":
                    # None values mean "sensor unavailable"; the island renders them as "--"
                    self.island.hardware_state = {
                        k: d.get(k) for k in ("cpu", "ram_used_gb", "ram_total_gb", "ram_pct", "gpu",
                                              "net_up_bps", "net_down_bps", "disk_pct", "fps")
                    }
                    self.island.hardware_state_t = time.time()
                    if hasattr(self.island, "update"):
                        self.island.update()
                elif kind in ("island_page_step", "island_show_page", "island_expand", "island_collapse", "island_action"):
                    isl = self.island
                    if kind == "island_page_step" and hasattr(isl, "page_step"):
                        isl.page_step(int(d.get("direction", 1) or 1))
                    elif kind == "island_show_page" and hasattr(isl, "show_page"):
                        isl.show_page(d.get("page", "weather"))
                    elif kind == "island_expand" and hasattr(isl, "expand"):
                        isl.expand(d.get("page"))
                    elif kind == "island_collapse" and hasattr(isl, "collapse_card"):
                        isl.collapse_card()
                    elif kind == "island_action" and hasattr(isl, "perform_action"):
                        isl.perform_action(d.get("action", ""), **{k: v for k, v in d.items() if k != "action"})
                elif kind == "island_surface":
                    if hasattr(self.island, "set_surface_style"):
                        self.island.set_surface_style(d.get("style", "gradient"))
                elif kind == "hidden":
                    if hasattr(self.island, "set_hidden"):
                        self.island.set_hidden(bool(d.get("on", d.get("hidden", True))))
                elif kind == "ask_page":
                    if hasattr(self.island, "open_ask_page"):
                        self.island.open_ask_page(focus=bool(d.get("focus", False)), toggle=bool(d.get("toggle", False)))
                elif kind == "onboarding":
                    self._start_onboarding()
                elif kind == "gesture_mode":
                    if hasattr(self.island, "set_gesture_mode"):
                        self.island.set_gesture_mode(bool(d.get("on", False)))
                elif kind == "timer_state":
                    if hasattr(self.island, "timer_active"):
                        if "active" in d: self.island.timer_active = bool(d["active"])
                        if "duration" in d: self.island.timer_duration_sec = float(d["duration"])
                        if "end_time" in d: self.island.timer_end_time = float(d["end_time"])
        except queue.Empty:
            pass

    def _start_onboarding(self):
        """overlay.post("onboarding"): boot key frame + intro line on the island (spoken through ctx.say when
        available), then the onboarding window (ui.onboarding_window.OnboardingWindow(ctx, cfg), imported lazily)."""
        line = None
        try:
            if hasattr(self.island, "play_intro"):
                line = self.island.play_intro()
        except Exception as exc:
            print(f"[overlay] onboarding intro error: {exc}")
        say = getattr(self.ctx, "say", None) if self.ctx is not None else None
        if callable(say) and line:
            try:
                say(line)
            except Exception as exc:
                print(f"[overlay] onboarding say error: {exc}")

        def _open():
            try:
                from ui.onboarding_window import OnboardingWindow
            except Exception as exc:
                print(f"[overlay] onboarding window unavailable: {exc}")
                return
            try:
                self._onboarding_win = OnboardingWindow(self.ctx, self.cfg)
                self._onboarding_win.show()
            except Exception as exc:
                print(f"[overlay] onboarding window failed: {exc}")

        QTimer.singleShot(1600, _open)

    def _sync_gesture_mode(self):
        """Mirror ctx.gesture_mode_on() (if main.py provides it) into the island's indicator."""
        fn = getattr(self.ctx, "gesture_mode_on", None) if self.ctx is not None else None
        if callable(fn) and hasattr(self.island, "set_gesture_mode"):
            try:
                v = fn()
                if isinstance(v, bool):
                    self.island.set_gesture_mode(v)
            except Exception:
                pass

    def _tick(self):
        if self.stop.is_set():
            self.app.quit()
            return
        try:
            now = time.time()
            self._drain(now)
            self._sync_gesture_mode()
            self.island.update_frame(now)
            want = self.FAST_TICK_MS if getattr(self.island, "animating", True) else self.IDLE_TICK_MS
            if self.timer.interval() != want:
                self.timer.setInterval(want)
        except Exception as exc:
            print(f"[overlay] tick error: {exc}")


if __name__ == "__main__":
    stop_evt = threading.Event()
    cfg = None
    try:
        from config import load_config
        cfg = load_config()
    except Exception:
        pass
    ov = Overlay(stop_evt, cfg=cfg)
    try:
        ov.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()

