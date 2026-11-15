"""Power gating: cursor loop parked until a camera session, SendInput wrapper, lazy imports,
voice suspend / reopen, and the quiet (game / Focus Assist) rule. All devices mocked."""
import ctypes
import pathlib
import subprocess
import sys
import threading
import time
import unittest.mock as um

import pytest

import controller as controller_mod
import fullscreen_detector as fsd
import voice
import win_input
from config import Config
from controller import Controller
from voice import Voice, WakeWordDetector
from voice_fakes import install_audio, make_detector, stop_and_join, wait_until


# ------------------------------------------------------------------ controller active / inactive

@pytest.fixture
def ctrl():
    with um.patch.object(controller_mod, "win_input", um.MagicMock()) as wi:
        wi.position.return_value = (100, 100)
        c = Controller(Config(control_scheme="palm_fist"))
        c.wi = wi
        yield c
        c.shutdown()


def _fake_windll():
    winmm = um.MagicMock()
    fake = um.MagicMock()
    fake.winmm = winmm
    return fake, winmm


def test_cursor_loop_starts_parked(ctrl):
    assert not ctrl.loop_running
    assert wait_until(lambda: ctrl._parked_evt.is_set())


def test_timer_begin_end_symmetric_and_failsafe(ctrl):
    fake, winmm = _fake_windll()
    with um.patch.object(ctypes, "windll", fake, create=True):
        for _ in range(3):
            ctrl.set_active(True)
            assert wait_until(lambda: winmm.timeBeginPeriod.call_count > 0 and ctrl.loop_running)
            time.sleep(0.03)
            ctrl.wi.mouse_up.reset_mock()
            ctrl.set_active(False)
            assert ctrl._parked_evt.is_set()
            ctrl.wi.mouse_up.assert_called()           # failsafe on deactivate
            ctrl.wi.key_up.assert_any_call("ctrl")
            assert winmm.timeBeginPeriod.call_count == winmm.timeEndPeriod.call_count == 1
            winmm.reset_mock()
        # idempotent while already inactive: no timer activity
        ctrl.set_active(False)
        ctrl.set_active(False)
        assert winmm.timeBeginPeriod.call_count == winmm.timeEndPeriod.call_count == 0


def test_shutdown_while_active_releases_timer(ctrl):
    fake, winmm = _fake_windll()
    with um.patch.object(ctypes, "windll", fake, create=True):
        ctrl.set_active(True)
        assert wait_until(lambda: winmm.timeBeginPeriod.call_count == 1)
        ctrl.shutdown()
        assert not ctrl._thread.is_alive()
        assert winmm.timeEndPeriod.call_count == 1


def test_no_ticks_while_inactive(ctrl):
    ctrl._move = um.MagicMock()
    ctrl.target_x += 200.0
    time.sleep(0.1)
    ctrl._move.assert_not_called()


def test_activate_resyncs_to_real_cursor(ctrl):
    ctrl.wi.position.return_value = (640, 360)
    ctrl.set_active(True)
    try:
        assert (ctrl.cur_x, ctrl.cur_y, ctrl.target_x, ctrl.target_y) == (640.0, 360.0, 640.0, 360.0)
    finally:
        ctrl.set_active(False)


def test_tracker_session_toggles_controller_active():
    import tracker
    t = tracker.Tracker.__new__(tracker.Tracker)
    t.controller = um.MagicMock()
    with um.patch.object(tracker.Tracker, "_session_body", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            t._session()
    assert [c.args for c in t.controller.set_active.call_args_list] == [(True,), (False,)]


def test_tracker_reopen_camera_only_when_wanted():
    import tracker
    t = tracker.Tracker(Config(), um.MagicMock(), um.MagicMock(), um.MagicMock(), um.MagicMock(),
                        threading.Event())
    t._want.clear()
    t.reopen_camera()
    assert not t._reopen.is_set()          # camera off: no-op
    t.set_camera_wanted(True)
    t.reopen_camera()
    t.reopen_camera()
    assert t._reopen.is_set()


# ------------------------------------------------------------------ win_input structs

@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(n, arr, size):
        assert size == ctypes.sizeof(win_input.INPUT)
        calls.append([(arr[i].type, arr[i]) for i in range(n)])
        return n

    fake = um.MagicMock()
    fake.SendInput.side_effect = fake_send
    monkeypatch.setattr(win_input, "_user32", fake)
    return calls


def _mouse(calls):
    return [(i.mi.dwFlags, i.mi.mouseData) for batch in calls for (t, i) in batch if t == win_input.INPUT_MOUSE]


def _keys(calls):
    return [(i.ki.wVk, i.ki.dwFlags) for batch in calls for (t, i) in batch if t == win_input.INPUT_KEYBOARD]


def test_win_input_mouse(sent):
    win_input.click()
    win_input.double_click()
    win_input.right_click()
    win_input.mouse_down()
    win_input.mouse_up()
    ld, lu = win_input.MOUSEEVENTF_LEFTDOWN, win_input.MOUSEEVENTF_LEFTUP
    rd, ru = win_input.MOUSEEVENTF_RIGHTDOWN, win_input.MOUSEEVENTF_RIGHTUP
    flags = [f for f, _ in _mouse(sent)]
    assert flags == [ld, lu, ld, lu, ld, lu, rd, ru, ld, lu]
    assert len(sent[1]) == 4          # double click is one SendInput of 4 events


def test_win_input_scroll_negative_wraps_to_dword(sent):
    win_input.scroll(120)
    win_input.scroll(-240)
    assert _mouse(sent) == [(win_input.MOUSEEVENTF_WHEEL, 120),
                            (win_input.MOUSEEVENTF_WHEEL, (-240) & 0xFFFFFFFF)]


def test_win_input_keys_and_hotkey(sent):
    win_input.key_down("ctrl")
    win_input.key_up("ctrl")
    win_input.hotkey("alt", "shift", "tab")
    up = win_input.KEYEVENTF_KEYUP
    assert _keys(sent) == [(0x11, 0), (0x11, up),
                           (0x12, 0), (0x10, 0), (0x09, 0), (0x09, up), (0x10, up), (0x12, up)]
    with pytest.raises(ValueError):
        win_input.key_down("nonsense-key")


def test_win_input_struct_size():
    assert ctypes.sizeof(win_input.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


# ------------------------------------------------------------------ lazy imports

def test_import_does_not_load_cv2_or_pyautogui():
    code = ("import sys, controller, tracker, productivity, fullscreen_detector, win_input\n"
            "bad = [m for m in ('cv2', 'pyautogui', 'scipy', 'scipy.signal') if m in sys.modules]\n"
            "print('BAD', bad)\n"
            "sys.exit(1 if bad else 0)\n")
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_tracker_lazy_module_forwards_setattr():
    import cv2
    import tracker
    original = cv2.VideoCapture
    with um.patch("tracker.cv2.VideoCapture", "sentinel"):
        assert cv2.VideoCapture == "sentinel"
    assert cv2.VideoCapture is original


# ------------------------------------------------------------------ voice suspend / reopen

@pytest.fixture
def audio(monkeypatch):
    env = install_audio(monkeypatch)
    resets = []
    monkeypatch.setattr(voice, "reset_shared_pyaudio", lambda: resets.append(1))
    env.resets = resets
    prev = Voice.is_speaking
    Voice.is_speaking = False
    monkeypatch.setattr(WakeWordDetector, "LISTEN_OFF_POLL_S", 0.02)
    yield env
    Voice.is_speaking = prev


def test_suspend_closes_stream_and_resume_reopens(monkeypatch, audio):
    wd = make_detector(monkeypatch, Config())
    wd.start()
    try:
        assert wait_until(lambda: audio.opens == 1 and audio.reads > 0)
        wd.set_suspended(True)
        assert wait_until(lambda: audio.streams[0].closed and wd.mic_state() == ("ok", "Suspended"))
        reads = audio.reads
        time.sleep(0.15)
        assert audio.reads == reads and audio.opens == 1
        wd.set_suspended(False)
        assert wait_until(lambda: audio.opens == 2 and audio.streams[1].reads > 0, timeout=3.0)
        assert audio.resets, "resume must re-enumerate PortAudio devices"
    finally:
        stop_and_join(wd)


def test_reopen_is_debounced_and_reopens_once(monkeypatch, audio):
    monkeypatch.setattr(WakeWordDetector, "REOPEN_SETTLE_S", 0.1)
    wd = make_detector(monkeypatch, Config())
    wd.start()
    try:
        assert wait_until(lambda: audio.opens == 1)
        for _ in range(5):
            wd.request_reopen()
        assert wait_until(lambda: audio.opens == 2, timeout=3.0)
        time.sleep(0.4)
        assert audio.opens == 2
        assert audio.streams[0].closed and len(audio.resets) == 1
    finally:
        stop_and_join(wd)


def test_voice_set_suspended_forwards_and_blocks_ptt():
    v = Voice(Config(), um.MagicMock(), threading.Event())
    v.wake_detector = um.MagicMock()
    v.set_suspended(True)
    v.wake_detector.set_suspended.assert_called_with(True)
    assert v.request_listen("gesture") is False
    v.reopen_devices()
    v.wake_detector.request_reopen.assert_called_once()
    v.set_suspended(False)
    v.wake_detector.set_suspended.assert_called_with(False)


# ------------------------------------------------------------------ quiet rule

@pytest.mark.parametrize("state,quiet", [
    (fsd.QUNS_NOT_PRESENT, False), (fsd.QUNS_BUSY, True), (fsd.QUNS_RUNNING_D3D_FULL_SCREEN, True),
    (fsd.QUNS_PRESENTATION_MODE, True), (fsd.QUNS_ACCEPTS_NOTIFICATIONS, False),
    (fsd.QUNS_QUIET_TIME, True), (fsd.QUNS_APP, False)])
def test_is_quiet_states(state, quiet):
    assert fsd.is_quiet(query_fn=lambda: state) is quiet


def test_is_quiet_cached_one_second(monkeypatch):
    n = []
    monkeypatch.setattr(fsd, "_quiet_cache", (-1e9, False))
    monkeypatch.setattr(fsd, "_DETECTOR", fsd.FullscreenDetector(lambda: n.append(1) or fsd.QUNS_BUSY))
    assert fsd.is_quiet(now=100.0) is True
    assert fsd.is_quiet(now=100.5) is True
    assert len(n) == 1
    assert fsd.is_quiet(now=101.2) is True
    assert len(n) == 2


def test_quiet_rule_speech_and_chime(monkeypatch):
    monkeypatch.setattr(voice, "_quiet", lambda: True)
    monkeypatch.setattr(voice, "_last_explicit_command_t", -1e9)
    sp = voice.Speaker(enabled=True, cfg=None)
    sp.say("Online. At your service, sir.")                       # proactive: suppressed
    assert sp.q.empty()
    sp.say("Sir, your timer for tea has completed.")             # timers/reminders still speak
    sp.say("whatever", urgent=True)
    assert sp.q.qsize() == 2
    while not sp.q.empty():
        sp.q.get_nowait()
    voice.note_explicit_command()
    sp.say("Opening Chrome, sir.")                                # reply to an explicit command: spoken
    assert sp.q.qsize() == 1
    monkeypatch.setattr(voice, "_last_explicit_command_t", time.monotonic() - 11.0)
    sp.say("Late chatter")
    assert sp.q.qsize() == 1                                      # 10 s window elapsed: suppressed
    assert voice._wake_chime_allowed() is False


def test_not_quiet_speaks_and_chimes(monkeypatch):
    monkeypatch.setattr(voice, "_quiet", lambda: False)
    sp = voice.Speaker(enabled=True, cfg=None)
    sp.say("Anything")
    assert sp.q.qsize() == 1 and voice._wake_chime_allowed() is True


def test_wake_detected_no_chime_when_quiet(monkeypatch):
    chimes = []
    monkeypatch.setattr(voice, "play_chime", lambda **k: chimes.append(1))
    ctx = um.MagicMock()
    v = Voice(Config(), ctx, threading.Event())
    monkeypatch.setattr(voice, "_quiet", lambda: True)
    v._on_wake_detected()
    assert chimes == [] and not ctx.chirp.called
    monkeypatch.setattr(voice, "_quiet", lambda: False)
    v._on_wake_detected()
    assert chimes == [1] and ctx.chirp.called


def test_resample_fft_length_and_accuracy():
    import numpy as np
    sr = 22050
    x = (np.sin(2 * np.pi * 440 * np.arange(sr) / sr) * 0.5).astype(np.float32)
    x[:400] = 0
    x[-400:] = 0
    y = voice._resample_fft(x, sr, 48000)
    assert len(y) == 48000 and y.dtype == np.float32
    ref = 0.5 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)
    assert np.abs(y[2000:-2000] - ref[2000:-2000]).max() < 1e-3
