"""Deterministic mute / unmute / toggle mute and pause / resume tracking phrases (all mocked)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pyautogui
import pytest

import commands
import system_control
from config import Config


class FakeEndpoint:
    def __init__(self, muted=False):
        self.muted = muted
        self.set_calls = []

    def GetMute(self):
        return int(self.muted)

    def SetMute(self, v, ctx):
        self.muted = bool(v)
        self.set_calls.append(bool(v))


@pytest.fixture
def audio(monkeypatch):
    """Real PycawAudioBackend logic over a fake endpoint; no key presses, no real volume."""
    ep = FakeEndpoint()
    backend = system_control.PycawAudioBackend.__new__(system_control.PycawAudioBackend)
    backend._fallback = system_control.FallbackAudioBackend()
    backend._available = True
    monkeypatch.setattr(backend, "_get_endpoint", lambda: ep, raising=False)
    adapter = system_control.SystemControlAdapter(
        audio_backend=backend, display_backend=MagicMock(), window_backend=MagicMock(),
        power_backend=MagicMock())
    monkeypatch.setattr(system_control, "_SYSTEM_CONTROL", adapter)
    keys = MagicMock()
    monkeypatch.setattr(pyautogui, "press", keys)
    monkeypatch.setattr(pyautogui, "hotkey", keys)
    ep.keys = keys
    return ep


def _ctx(**kw):
    cfg = Config()
    return SimpleNamespace(cfg=cfg, engine=SimpleNamespace(cfg=cfg, active=True), overlay=MagicMock(),
                           set_tracking_paused=MagicMock(), say=MagicMock(), **kw)


@pytest.mark.parametrize("start", [False, True])
def test_mute_forces_muted(audio, start):
    audio.muted = start
    reply = commands.run_command("mute", _ctx())
    assert audio.muted is True
    assert "mute" in reply.lower()
    audio.keys.assert_not_called()


@pytest.mark.parametrize("start", [False, True])
def test_unmute_forces_unmuted(audio, start):
    audio.muted = start
    commands.run_command("unmute", _ctx())
    assert audio.muted is False
    audio.keys.assert_not_called()


def test_mute_twice_stays_muted_and_unmute_twice_stays_unmuted(audio):
    ctx = _ctx()
    commands.run_command("mute", ctx)
    commands.run_command("mute", ctx)
    assert audio.muted is True
    commands.run_command("unmute", ctx)
    commands.run_command("unmute", ctx)
    assert audio.muted is False


def test_toggle_mute_toggles(audio):
    ctx = _ctx()
    commands.run_command("toggle mute", ctx)
    assert audio.muted is True
    commands.run_command("toggle mute", ctx)
    assert audio.muted is False
    audio.keys.assert_not_called()


@pytest.mark.parametrize("phrase,expected", [
    ("pause tracking", True), ("stop tracking", True),
    ("resume tracking", False), ("start tracking", False),
])
def test_tracking_phrases_call_set_tracking_paused(phrase, expected):
    ctx = _ctx()
    reply = commands.run_command(phrase, ctx)
    ctx.set_tracking_paused.assert_called_once_with(expected)
    assert "Tracking" in reply
    # no "did you mean" disambiguation card
    for c in ctx.overlay.post.call_args_list:
        assert "disambig" not in str(c).lower()


def test_tracking_phrase_with_wake_prefix():
    ctx = _ctx()
    commands.run_command(commands.strip_wake_prefix("Jarvis, pause tracking"), ctx)
    ctx.set_tracking_paused.assert_called_once_with(True)
