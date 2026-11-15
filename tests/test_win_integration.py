import threading
import types

import autostart
import eco_qos
import win_events as we


# ---- win_events dispatch ----
def _mk(**kw):
    calls = []
    cbs = {n: (lambda n=n: calls.append(n)) for n in
           ("on_toggle", "on_quit", "on_suspend", "on_resume", "on_lock", "on_unlock", "on_devices_changed")}
    cbs.update(kw)
    return we.WinEvents(debounce=0.05, **cbs), calls


def test_hotkey_mapping():
    w, calls = _mk()
    assert w.handle_message(we.WM_HOTKEY, we.HOTKEY_TOGGLE, 0) == 0
    w.handle_message(we.WM_HOTKEY, we.HOTKEY_QUIT, 0)
    w.handle_message(we.WM_HOTKEY, 99, 0)
    assert calls == ["on_toggle", "on_quit"]


def test_power_resume_fires_once():
    w, calls = _mk()
    w.handle_message(we.WM_POWERBROADCAST, we.PBT_APMRESUMEAUTOMATIC, 0)   # no suspend seen: ignored
    assert calls == []
    assert w.handle_message(we.WM_POWERBROADCAST, we.PBT_APMSUSPEND, 0) == 1
    w.handle_message(we.WM_POWERBROADCAST, we.PBT_APMRESUMEAUTOMATIC, 0)
    w.handle_message(we.WM_POWERBROADCAST, we.PBT_APMRESUMESUSPEND, 0)
    assert calls == ["on_suspend", "on_resume"]


def test_session_lock_unlock():
    w, calls = _mk()
    w.handle_message(we.WM_WTSSESSION_CHANGE, we.WTS_SESSION_LOCK, 0)
    w.handle_message(we.WM_WTSSESSION_CHANGE, we.WTS_SESSION_UNLOCK, 0)
    w.handle_message(we.WM_WTSSESSION_CHANGE, 5, 0)
    assert calls == ["on_lock", "on_unlock"]


def test_device_change_debounced():
    done = threading.Event()
    n = []
    w, _ = _mk(on_devices_changed=lambda: (n.append(1), done.set()))
    for _ in range(5):
        w.handle_message(we.WM_DEVICECHANGE, we.DBT_DEVNODES_CHANGED, 0)
    assert done.wait(2.0)
    done.clear()
    assert not done.wait(0.3)
    assert n == [1]
    w.stop()


def test_unhandled_returns_none_and_callback_errors_swallowed():
    def boom():
        raise RuntimeError("x")
    w, _ = _mk(on_toggle=boom)
    assert w.handle_message(0x0001, 0, 0) is None
    w.handle_message(we.WM_HOTKEY, we.HOTKEY_TOGGLE, 0)   # logged, not raised


# ---- governor ----
class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_governor_state_machine():
    clk, applied = Clock(), []
    g = eco_qos.EfficiencyGovernor(idle_after=10, apply=applied.append, clock=clk, start_thread=False)
    g.tick()
    assert not g.efficient
    clk.t = 9.9
    g.tick()
    assert not g.efficient
    clk.t = 10.0
    g.tick()
    assert g.efficient and applied == [True]
    g.activity()                       # lifts immediately
    assert not g.efficient and applied == [True, False]
    clk.t = 15.0
    g.set_busy("camera", True)
    clk.t = 100.0
    g.tick()
    assert not g.efficient             # busy blocks
    g.set_busy("camera", False)
    clk.t = 109.0
    g.tick()
    assert not g.efficient
    clk.t = 110.0
    g.tick()
    assert g.efficient


def test_set_efficiency_never_raises(monkeypatch):
    monkeypatch.setattr(eco_qos.sys, "platform", "linux")
    assert eco_qos.set_efficiency(True) is False


# ---- autostart ----
def test_autostart_roundtrip(monkeypatch):
    import winreg
    sub = r"Software\JarvisTest_autostart"
    monkeypatch.setattr(autostart, "RUN_KEY", sub)
    try:
        assert not autostart.is_enabled()
        assert autostart.set_enabled(True)
        assert autostart.is_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
            val = winreg.QueryValueEx(k, "JARVIS")[0]
        assert val.endswith("--background") and val.startswith('"')
        assert autostart.set_enabled(False)
        assert not autostart.is_enabled()
        assert autostart.set_enabled(False)
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


def test_autostart_frozen_command(monkeypatch):
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(autostart.sys, "executable", r"C:\Apps\JARVIS.exe")
    assert autostart.build_command() == '"' + "C:\\Apps\\JARVIS.exe" + '" --background'


# ---- main.Context system pause/resume ----
def test_context_pause_resume_remembers_gesture_state():
    import main
    calls = []
    voice = types.SimpleNamespace(set_suspended=lambda v: calls.append(("susp", v)),
                                  reopen_devices=lambda: calls.append("reopen"))
    tracker = types.SimpleNamespace(camera_wanted=True,
                                    set_camera_wanted=lambda v: calls.append(("cam", v)),
                                    reopen_camera=lambda: calls.append("recam"))
    engine = types.SimpleNamespace(request=lambda v: calls.append(("eng", v)))
    overlay = types.SimpleNamespace(post=lambda *a, **k: None)
    ctx = main.Context(engine, overlay, None, threading.Event(), tracker=tracker, voice=voice)
    ctx.system_pause("lock")
    tracker.camera_wanted = False
    ctx.system_pause("suspend")        # nested: no second pause
    ctx.system_resume("lock")          # still suspended
    assert ("susp", False) not in calls
    ctx.system_resume("suspend", reopen_devices=True)
    assert ("susp", False) in calls and "reopen" in calls and ("cam", True) in calls
