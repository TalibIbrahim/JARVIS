"""audio_ducker with a fake pycaw session layer (no COM, no real volumes touched)."""
import threading

import pytest

import audio_ducker
from audio_ducker import AudioDucker, _Session

OWN_PID = 4242


class FakeVol:
    def __init__(self, v):
        self.v = v
        self.sets = 0

    def GetMasterVolume(self):
        return self.v

    def SetMasterVolume(self, v, ctx):
        self.v = v
        self.sets += 1


class FakeLayer:
    def __init__(self, specs):
        # specs: key -> (pid, is_system, volume)
        self.vols = {k: FakeVol(v) for k, (pid, sysf, v) in specs.items()}
        self._sessions = [_Session(k, pid, sysf, self.vols[k]) for k, (pid, sysf, v) in specs.items()]
        self.init_calls = 0
        self.uninit_calls = 0

    def init_thread(self):
        self.init_calls += 1

    def uninit_thread(self):
        self.uninit_calls += 1

    def sessions(self):
        return list(self._sessions)


class FakeTimer:
    instances = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.cancelled, self.daemon = interval, fn, False, False
        FakeTimer.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def _settle(d):
    for t in list(d._threads):
        t.join(timeout=5.0)
        assert not t.is_alive()


@pytest.fixture
def layer():
    return FakeLayer({
        "own": (OWN_PID, False, 0.9),
        "sys": (0, True, 0.7),
        "music": (100, False, 0.8),
        "browser": (200, False, 0.5),
    })


@pytest.fixture
def ducker(layer, monkeypatch):
    FakeTimer.instances = []
    monkeypatch.setattr(audio_ducker.threading, "Timer", FakeTimer)
    return AudioDucker(layer=layer, own_pid=OWN_PID, fade_scale=0.0)


def test_duck_scales_others_to_20_percent_and_skips_own_and_system(ducker, layer):
    assert ducker.duck(0.2, "t") is True
    _settle(ducker)
    assert ducker.is_ducked()
    assert layer.vols["music"].v == pytest.approx(0.16)
    assert layer.vols["browser"].v == pytest.approx(0.10)
    assert layer.vols["own"].v == 0.9 and layer.vols["own"].sets == 0
    assert layer.vols["sys"].v == 0.7 and layer.vols["sys"].sets == 0


def test_restore_returns_originals(ducker, layer):
    ducker.duck(0.2, "t")
    _settle(ducker)
    assert ducker.restore("t") is True
    _settle(ducker)
    assert not ducker.is_ducked()
    assert layer.vols["music"].v == pytest.approx(0.8)
    assert layer.vols["browser"].v == pytest.approx(0.5)
    assert layer.vols["own"].v == 0.9 and layer.vols["sys"].v == 0.7


def test_restore_without_duck_is_noop(ducker, layer):
    assert ducker.restore("t") is False
    assert all(v.sets == 0 for v in layer.vols.values())


def test_user_volume_change_during_duck_is_preserved(ducker, layer):
    ducker.duck(0.2, "t")
    _settle(ducker)
    layer.vols["music"].v = 0.95        # user dragged the slider while ducked
    ducker.restore("t")
    _settle(ducker)
    assert layer.vols["music"].v == pytest.approx(0.95)
    assert layer.vols["browser"].v == pytest.approx(0.5)   # untouched session still restored


def test_duck_is_idempotent(ducker, layer):
    ducker.duck(0.2, "a")
    _settle(ducker)
    sets_after_first = {k: v.sets for k, v in layer.vols.items()}
    ducker.duck(0.2, "b")
    _settle(ducker)
    assert {k: v.sets for k, v in layer.vols.items()} == sets_after_first
    assert layer.vols["music"].v == pytest.approx(0.16)    # not 0.032
    ducker.restore("t")
    _settle(ducker)
    assert layer.vols["music"].v == pytest.approx(0.8)


def test_auto_restore_fires_after_timeout(layer, monkeypatch):
    FakeTimer.instances = []
    monkeypatch.setattr(audio_ducker.threading, "Timer", FakeTimer)
    d = AudioDucker(layer=layer, own_pid=OWN_PID, auto_restore_s=20.0, fade_scale=0.0)
    d.duck(0.2, "t")
    _settle(d)
    timer = FakeTimer.instances[-1]
    assert timer.interval == 20.0 and not timer.cancelled
    assert layer.vols["music"].v == pytest.approx(0.16)
    timer.fn()                                   # time passes: timer fires
    _settle(d)
    assert not d.is_ducked()
    assert layer.vols["music"].v == pytest.approx(0.8)


def test_restore_cancels_pending_timer(ducker):
    ducker.duck(0.2, "t")
    timer = FakeTimer.instances[-1]
    ducker.restore("t")
    _settle(ducker)
    assert timer.cancelled


def test_duck_refresh_rearms_timer(ducker):
    ducker.duck(0.2, "t")
    first = FakeTimer.instances[-1]
    ducker.duck(0.2, "t2")
    assert first.cancelled
    assert FakeTimer.instances[-1] is not first


def test_shutdown_restores(ducker, layer):
    ducker.duck(0.2, "t")
    _settle(ducker)
    ducker.shutdown()
    assert not ducker.is_ducked()
    assert layer.vols["music"].v == pytest.approx(0.8)
    assert layer.vols["browser"].v == pytest.approx(0.5)


def test_shutdown_when_not_ducked_is_safe(ducker, layer):
    ducker.shutdown()
    assert all(v.sets == 0 for v in layer.vols.values())


def test_com_init_and_uninit_balanced(ducker, layer):
    ducker.duck(0.2, "t")
    _settle(ducker)
    ducker.restore("t")
    _settle(ducker)
    assert layer.init_calls == layer.uninit_calls == 2


def test_failing_layer_never_raises(monkeypatch):
    class Boom(FakeLayer):
        def sessions(self):
            raise RuntimeError("no audio device")

    FakeTimer.instances = []
    monkeypatch.setattr(audio_ducker.threading, "Timer", FakeTimer)
    d = AudioDucker(layer=Boom({}), own_pid=OWN_PID, fade_scale=0.0)
    assert d.duck(0.2, "t") is True
    _settle(d)
    assert d.restore("t") is True
    _settle(d)
