"""Phase 1 failing-first regression tests pinning diagnosed defects.

Each BUG test was xfail(strict=True) before the Phase 2 fixes; they are now plain regression tests.
Pure/mocked: no camera, mic, network, and no writes to real APPDATA/LOCALAPPDATA.
"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import commands
import voice


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    import privacy_log
    monkeypatch.setattr(privacy_log, "_PRIVACY_LOG",
                        privacy_log.PrivacyLog(max_events=50, log_file=str(tmp_path / "privacy_log.json")))
    # Never let a test press real keys or launch anything.
    import pyautogui
    for name in ("press", "hotkey", "scroll", "click", "moveTo", "typewrite", "write", "keyDown", "keyUp"):
        if hasattr(pyautogui, name):
            monkeypatch.setattr(pyautogui, name, lambda *a, **k: None)
    monkeypatch.setattr(commands.subprocess, "Popen", MagicMock())
    monkeypatch.setattr(commands.webbrowser, "open", MagicMock(return_value=True))
    yield


def _make_ctx():
    ctx = MagicMock()
    ctx.cfg = SimpleNamespace(ai_enabled=False)
    ctx.engine.cfg = ctx.cfg
    ctx.overlay = None
    return ctx


def _make_cfg():
    from config import Config
    cfg = Config()
    cfg.ai_enabled = False
    cfg.chime_enabled = False
    return cfg


class _Toggles:
    def __init__(self):
        self.keys = []
        self.adapter_calls = []


def _install_media(monkeypatch, mode, method):
    """mode 'smtc': adapter call itself performs the toggle (SMTC success).
    mode 'fallback': SMTC fails, adapter falls back to pressing the media key itself."""
    import media_adapter
    import pyautogui
    rec = _Toggles()
    key = {"play_pause": "playpause", "next_track": "nexttrack", "previous_track": "prevtrack"}[method]

    class FakeAdapter:
        def __getattr__(self, name):
            if name != method:
                raise AttributeError(name)

            def fn():
                rec.adapter_calls.append(name)
                if mode == "fallback":
                    pyautogui.press(key, _pause=False)
                return mode == "smtc"
            return fn

        def get_current_media(self):
            return None

        def refresh_sync(self):
            return None

    monkeypatch.setattr(media_adapter, "get_media_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(pyautogui, "press",
                        lambda k, *a, **kw: rec.keys.append(k))
    return rec


def _count(rec, key):
    # SMTC-mode adapter call is one toggle; any key press of the media key is one toggle.
    smtc = len(rec.adapter_calls)
    presses = len([k for k in rec.keys if k == key])
    return smtc, presses


# --------------------------------------------------------------------------- V1
class _StubVad:
    def predict(self, chunk):
        return 0.01


@pytest.mark.xfail(strict=True, reason="BUG-V1: VAD RMS fallback overrides Silero (rms>=threshold -> speech even when prob~0)")
def test_v1_vad_fallback_ignores_silero(monkeypatch):
    monkeypatch.setattr(voice.VADDetector, "_init_vad", lambda self: None)
    det = voice.VADDetector()
    det._vad = _StubVad()
    rng = np.random.default_rng(1234)
    chunk = (rng.standard_normal(1280) * 400).astype(np.int16)
    rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
    assert 300 < rms < 500
    assert det.is_speech(chunk, rms) is False


@pytest.mark.xfail(strict=True, reason="BUG-V1b: WakeWordDetector builds VADDetector() with default 42.0, ignoring cfg.mic_energy_threshold")
def test_v1b_mic_energy_threshold_ignored(monkeypatch):
    monkeypatch.setattr(voice.WakeWordDetector, "_init_model", lambda self: None)
    monkeypatch.setattr(voice.VADDetector, "_init_vad", lambda self: None)
    cfg = _make_cfg()
    cfg.mic_energy_threshold = 231
    wd = voice.WakeWordDetector(cfg=cfg)
    assert wd.vad.energy_threshold != 42.0
    assert wd.vad.energy_threshold == 231


# --------------------------------------------------------------------------- V2
@pytest.mark.parametrize("mode", ["smtc", "fallback"])
@pytest.mark.parametrize("text", ["play", "pause", "resume"])
def test_v2_play_pause_single_toggle(monkeypatch, mode, text):
    rec = _install_media(monkeypatch, mode, "play_pause")
    commands.run_command(text, _make_ctx())
    smtc, presses = _count(rec, "playpause")
    # smtc mode: one adapter toggle; fallback mode: the adapter's own key press is the toggle.
    total = (smtc if mode == "smtc" else 0) + presses
    assert total == 1, f"toggled {total} times (adapter={rec.adapter_calls}, keys={rec.keys})"


@pytest.mark.parametrize("method,key,text", [("next_track", "nexttrack", "next track"),
                                              ("previous_track", "prevtrack", "previous track")])
def test_v2b_track_skip_single(monkeypatch, method, key, text):
    rec = _install_media(monkeypatch, "smtc", method)
    commands.run_command(text, _make_ctx())
    smtc, presses = _count(rec, key)
    assert smtc + presses == 1, f"skipped {smtc + presses} times (adapter={rec.adapter_calls}, keys={rec.keys})"


# --------------------------------------------------------------------------- V3
def _voice_with_ctx():
    ctx = _make_ctx()
    cfg = _make_cfg()
    v = voice.Voice(cfg, ctx, threading.Event())
    return v, ctx


def _hallucination_case(text):
    v, ctx = _voice_with_ctx()
    v._process(text, is_command=True)
    assert not ctx.quit.called, f"{text!r} quit Jarvis"
    for c in ctx.set_active.call_args_list:
        assert c.args != (False,), f"{text!r} put Jarvis in standby"


def test_v3_hallucination_goodbye():
    _hallucination_case("goodbye.")


@pytest.mark.parametrize("text", ["bye.", "thank you.", "you", "okay."])
def test_v3_hallucination_guard(text):
    # Observed today: these do not quit or set standby; guard against regressions.
    _hallucination_case(text)


@pytest.mark.parametrize("text", ["that's interesting", "i need a rest of the day plan", "the forest is nice"])
def test_v3b_substring_triggers_standby(text):
    ctx = _make_ctx()
    commands.run_command(text, ctx)
    for c in ctx.set_active.call_args_list:
        assert c.args != (False,), f"{text!r} triggered standby"


# --------------------------------------------------------------------------- T3
def test_t3_silent_thread_death_is_restarted():
    from thread_supervisor import ThreadSupervisor

    calls = []

    class Dying(threading.Thread):
        def __init__(self):
            super().__init__(daemon=True)

        def run(self):
            raise RuntimeError("boom")

    def factory():
        calls.append(time.time())
        return Dying()

    sup = ThreadSupervisor()
    sup.register("dying", factory, max_restarts=5, backoff_base=0.05, max_backoff=0.1)
    sup.start()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.05)
    finally:
        sup.stop(timeout=1.0)
    assert len(calls) > 1, f"factory called {len(calls)} time(s); thread death not restarted"


# --------------------------------------------------------------------------- T1
@pytest.mark.xfail(strict=True, reason="BUG-T1: list_cameras(force_refresh=True) opens a device held by an active FrameGrabber")
def test_t1_list_cameras_must_not_probe_active_grabber(monkeypatch):
    import numpy as _np
    import tracker

    opens = []

    class FakeCap:
        def __init__(self, idx, backend=None):
            self.idx = idx
            opens.append(idx)

        def set(self, *a, **k):
            return True

        def isOpened(self):
            return True

        def read(self):
            return True, _np.zeros((4, 4, 3), dtype=_np.uint8)

        def release(self):
            pass

        def get(self, *a, **k):
            return 0.0

    monkeypatch.setattr(tracker.cv2, "VideoCapture", FakeCap)
    monkeypatch.setattr(tracker, "_CAMERA_CACHE", None, raising=False)
    cfg = SimpleNamespace(camera_index=0, frame_width=64, frame_height=48, camera_mjpg=False, camera_fps=30)
    g = tracker.FrameGrabber(cfg, threading.Event())
    assert g.open_camera() is True
    assert opens == [0]
    opens.clear()

    tracker.list_cameras(force_refresh=True)
    assert 0 not in opens, f"list_cameras re-opened camera 0 held by active grabber (opens={opens})"
    g.release()
