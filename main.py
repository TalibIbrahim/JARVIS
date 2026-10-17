"""J.A.R.V.I.S. — hand-gesture PC control with a notch-style overlay.  Run: python main.py"""
import argparse
import ctypes
import datetime
import sys
import threading


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

    def __init__(self, engine, overlay, speaker, stop, updater=None, tracker=None):
        self.engine = engine
        self.overlay = overlay
        self.speaker = speaker
        self.stop = stop
        self.updater = updater
        self.tracker = tracker

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
    if args.preview:
        cfg.show_preview = True
    if args.no_voice:
        cfg.voice_enabled = False
    if args.camera is not None:
        cfg.camera_index = args.camera

    stop = threading.Event()
    engine = GestureEngine(cfg)
    controller = Controller(cfg)
    speaker = Speaker(enabled=cfg.speak, cfg=cfg)
    speaker.start()

    overlay = Overlay(stop, cfg=cfg)
    controller.set_overlay(overlay)
    engine.set_overlay(overlay)
    ctx = Context(engine, overlay, speaker, stop)
    overlay.ctx = ctx

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
        max_restarts=5,
        backoff_base=1.0,
        on_error=_on_tracker_error,
    )
    supervisor.start("Tracker")

    if cfg.voice_enabled:
        def _on_voice_error(name, exc):
            explain_graceful_degradation("voice", overlay=overlay, speaker=speaker)

        supervisor.register(
            name="Voice",
            factory=lambda: Voice(cfg, ctx, stop),
            max_restarts=5,
            backoff_base=1.0,
            on_error=_on_voice_error,
        )
        supervisor.start("Voice")

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
        controller.shutdown()
        speaker.q.put(None)
        if _INSTANCE_MUTEX and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(_INSTANCE_MUTEX)
            except Exception:
                pass


if __name__ == "__main__":
    main()
