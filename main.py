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


class Context:
    """Small bundle shared by the tracker, voice commands and hotkeys."""

    def __init__(self, engine, overlay, speaker, stop, updater=None):
        self.engine = engine
        self.overlay = overlay
        self.speaker = speaker
        self.stop = stop
        self.updater = updater

    def say(self, text):
        self.overlay.post("say", text=text)
        self.speaker.say(text)

    def set_active(self, want):
        if want is None:
            self.engine.request_toggle()
        else:
            self.engine.request(want)

    def quit(self):
        self.stop.set()

    def chirp(self, up):
        if sys.platform != "win32":
            return
        import winsound
        notes = [(740, 70), (1100, 90)] if up else [(900, 70), (500, 110)]

        def play():
            for freq, ms in notes:
                winsound.Beep(freq, ms)

        threading.Thread(target=play, daemon=True).start()


def main():
    _enable_dpi_awareness()
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. gesture control")
    parser.add_argument("--preview", action="store_true", help="show the camera HUD window")
    parser.add_argument("--no-voice", action="store_true", help="disable voice commands")
    parser.add_argument("--camera", type=int, default=None, help="camera index (default 0)")
    args = parser.parse_args()

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
    speaker = Speaker(cfg.speak)
    speaker.start()

    overlay = Overlay(stop, cfg=cfg)
    ctx = Context(engine, overlay, speaker, stop)
    overlay.ctx = ctx

    from updater import AutoUpdater
    updater = AutoUpdater(ctx=ctx, overlay=overlay, auto_check=cfg.auto_update_check)
    ctx.updater = updater
    overlay.updater = updater
    updater.start_periodic_check(interval_hours=4)

    tracker = Tracker(cfg, engine, controller, overlay, ctx, stop)
    tracker.start()
    if cfg.voice_enabled:
        Voice(cfg, ctx, stop).start()

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
        controller.shutdown()
        speaker.q.put(None)
        tracker.join(timeout=2)


if __name__ == "__main__":
    main()
