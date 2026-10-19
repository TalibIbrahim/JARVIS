"""Voice <-> audio_ducker integration with audio_ducker mocked (no COM, no volumes touched)."""
import time
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from voice import Voice


@pytest.fixture
def ducker():
    with patch("audio_ducker.duck", return_value=True) as d,             patch("audio_ducker.restore", return_value=True) as r,             patch("voice.play_chime"):
        yield d, r


@pytest.fixture(autouse=True)
def _not_speaking():
    prev = Voice.is_speaking
    Voice.is_speaking = False
    yield
    Voice.is_speaking = prev


def _voice(**cfg_kw):
    cfg = Config(**cfg_kw)
    cfg.wake_aliases = ["jarvis"]
    v = Voice(cfg, MagicMock(), MagicMock())
    v.wake_detector = MagicMock()
    v.wake_detector.listening_until = 0.0
    return v


def test_wake_ducks_once(ducker):
    duck, restore = ducker
    v = _voice()
    v._on_wake_detected()
    v._on_wake_detected()   # a second wake inside the same interaction must not re-duck
    assert duck.call_count == 1
    assert restore.call_count == 0
    assert v._ducked


def test_followup_expiry_restores(ducker):
    duck, restore = ducker
    v = _voice()
    v._on_wake_detected()
    v._on_no_speech_detected()
    assert duck.call_count == 1 and restore.call_count == 1
    assert not v._ducked


def test_wake_command_speech_lapse_is_one_duck_one_restore(ducker):
    duck, restore = ducker
    v = _voice()
    v._on_wake_detected()
    with patch("voice.run_command", return_value="Opening Chrome."):
        v._process("jarvis open chrome")
    assert duck.call_count == 1
    Voice.is_speaking = True                 # JARVIS is replying
    v._on_no_speech_detected()               # follow-up lapses mid-reply: deferred
    assert restore.call_count == 0
    assert v._restore_pending
    Voice.is_speaking = False
    v._on_speech_finished(time.time())       # reply ends after the window closed
    assert duck.call_count == 1
    assert restore.call_count == 1
    v._on_speech_finished(time.time())       # nothing further to restore
    v._on_no_speech_detected()
    assert restore.call_count == 1


def test_speech_ending_after_expiry_restores(ducker):
    duck, restore = ducker
    v = _voice()
    v._on_wake_detected()
    v.listening_until = 0.0
    v.wake_detector.listening_until = 0.0
    v._on_speech_finished(time.time())
    assert restore.call_count == 1
    assert not v._ducked


def test_speech_ending_inside_followup_window_keeps_duck(ducker):
    duck, restore = ducker
    v = _voice()
    v._on_wake_detected()                    # window open for followup_window seconds
    v._on_speech_finished(time.time())
    assert restore.call_count == 0
    assert v._ducked


def test_wake_detector_accepts_on_speech_end_hook():
    """WakeWordDetector accepts and stores the on_speech_end hook that Voice wires to _on_speech_finished."""
    from voice import WakeWordDetector
    import inspect
    assert "on_speech_end" in inspect.signature(WakeWordDetector.__init__).parameters


def test_disabled_never_ducks(ducker):
    duck, restore = ducker
    v = _voice(media_duck_on_wake=False)
    v._on_wake_detected()
    with patch("voice.run_command", return_value="ok"):
        v._process("jarvis open chrome")
    v._on_no_speech_detected()
    v._on_speech_finished(time.time())
    assert duck.call_count == 0
    assert restore.call_count == 0


def test_voice_stop_force_restores_even_while_speaking(ducker):
    duck, restore = ducker
    v = _voice()
    v._on_wake_detected()
    Voice.is_speaking = True
    v._restore_media("voice_stop", force=True)
    assert restore.call_count == 1
