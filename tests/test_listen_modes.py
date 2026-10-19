"""Listen-for-Jarvis off / push-to-talk behaviour of WakeWordDetector and Voice (all devices mocked)."""
import time
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from voice import Voice, WakeWordDetector
from voice_fakes import install_audio, loud, make_detector, silence, stop_and_join, wait_until


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    prev = Voice.is_speaking
    Voice.is_speaking = False
    monkeypatch.setattr(WakeWordDetector, "LISTEN_OFF_POLL_S", 0.02)
    yield
    Voice.is_speaking = prev


@pytest.fixture
def audio(monkeypatch):
    return install_audio(monkeypatch)


def _ptt_source(n):
    # 12 loud chunks (speech) then silence: the recording ends on the silence endpoint.
    return loud(n) if n <= 12 else silence()


def _started(monkeypatch, cfg, **kw):
    wd = make_detector(monkeypatch, cfg, **kw)
    wd.start()
    return wd


def test_listen_off_stream_closed_zero_reads(monkeypatch, audio):
    wd = _started(monkeypatch, Config(listen_for_jarvis=False))
    try:
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        time.sleep(0.15)
        assert audio.opens == 0 and audio.reads == 0
    finally:
        stop_and_join(wd)
    assert audio.opens == 0 and audio.reads == 0


def test_mic_state_is_ok_listening_off(monkeypatch, audio):
    wd = _started(monkeypatch, Config(listen_for_jarvis=False))
    try:
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        assert wd.mic_state() == ("ok", "Listening off")
    finally:
        stop_and_join(wd)


def test_toggle_on_reopens_within_half_second(monkeypatch, audio):
    cfg = Config(listen_for_jarvis=False)
    wd = _started(monkeypatch, cfg)
    try:
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        assert audio.opens == 0
        t0 = time.monotonic()
        cfg.listen_for_jarvis = True
        assert wait_until(lambda: audio.opens == 1 and audio.reads > 0, timeout=0.5)
        assert time.monotonic() - t0 < 0.5
        assert wd.mic_state() == ("ok", "")
        # off again: the stream is closed and reading stops
        cfg.listen_for_jarvis = False
        assert wait_until(lambda: audio.streams[0].closed, timeout=1.0)
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        reads = audio.reads
        time.sleep(0.1)
        assert audio.reads == reads
    finally:
        stop_and_join(wd)


def test_request_listen_while_off_records_one_utterance_and_closes(monkeypatch, audio):
    audio.source = _ptt_source
    got, started, done = [], [], []
    wd = _started(monkeypatch, Config(listen_for_jarvis=False),
                  on_utterance=lambda raw, is_command=False: got.append((raw, is_command)),
                  on_push_to_talk=started.append, on_push_to_talk_done=done.append)
    try:
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        assert audio.opens == 0
        assert wd.request_listen("gesture") is True
        assert wait_until(lambda: len(got) == 1, timeout=5.0)
        assert wait_until(lambda: audio.opens == 1 and audio.streams[0].closed, timeout=2.0)
        time.sleep(0.1)
        assert len(got) == 1 and got[0][1] is True and len(got[0][0]) > 0
        assert started == ["gesture"] and done == []
        assert audio.opens == 1       # not reopened afterwards
    finally:
        stop_and_join(wd)


def test_request_listen_dropped_while_speaking(monkeypatch, audio):
    wd = _started(monkeypatch, Config(listen_for_jarvis=False))
    try:
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        Voice.is_speaking = True
        assert wd.request_listen("gesture") is False
        assert not wd._ptt_event.is_set()
        time.sleep(0.1)
        assert audio.opens == 0
    finally:
        Voice.is_speaking = False
        stop_and_join(wd)


def test_request_listen_dropped_while_busy(monkeypatch, audio):
    wd = _started(monkeypatch, Config(listen_for_jarvis=False))
    try:
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        wd.is_recording = True
        assert wd.request_listen("gesture") is False
        wd.is_recording = False
        wd._ptt_event.set()                      # a request is already pending
        assert wd.request_listen("gesture") is False
        wd._ptt_event.clear()
    finally:
        stop_and_join(wd)


def test_request_listen_dropped_when_thread_not_running(monkeypatch):
    wd = make_detector(monkeypatch, Config())
    assert wd.request_listen("gesture") is False        # never started


def test_voice_request_listen_guards():
    v = Voice(Config(), MagicMock(), MagicMock())
    assert v.request_listen("gesture") is False         # no detector
    v.wake_detector = MagicMock()
    v.wake_detector.request_listen.return_value = True
    assert v.request_listen("gesture") is True
    v.wake_detector.request_listen.assert_called_once_with("gesture")
    Voice.is_speaking = True
    v.wake_detector.request_listen.reset_mock()
    assert v.request_listen("gesture") is False
    v.wake_detector.request_listen.assert_not_called()


# ---------------------------------------------------------------- ducking
def _voice_with_detector(monkeypatch, cfg):
    v = Voice(cfg, MagicMock(), MagicMock())
    wd = make_detector(
        monkeypatch, cfg,
        on_utterance=lambda raw, is_command=False: v.audio_q.put_nowait((raw, is_command)),
        on_push_to_talk=v._on_push_to_talk_start,
        on_push_to_talk_done=v._on_push_to_talk_done)
    v.wake_detector = wd
    return v, wd


def test_push_to_talk_ducks_and_restores_on_no_speech(monkeypatch, audio):
    monkeypatch.setattr(WakeWordDetector, "PTT_TIMEOUT_NO_SPEECH_S", 0.2)
    audio.source = lambda n: silence()
    with patch("audio_ducker.duck", return_value=True) as duck, \
            patch("audio_ducker.restore", return_value=True) as restore, \
            patch("voice.play_chime"):
        v, wd = _voice_with_detector(monkeypatch, Config(listen_for_jarvis=False))
        wd.start()
        try:
            assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
            assert v.request_listen("gesture") is True
            assert wait_until(lambda: restore.call_count == 1, timeout=5.0)
            assert duck.call_count == 1
            assert not v._ducked
        finally:
            stop_and_join(wd)


def test_push_to_talk_ducks_then_restores_after_command(monkeypatch, audio):
    audio.source = _ptt_source
    with patch("audio_ducker.duck", return_value=True) as duck, \
            patch("audio_ducker.restore", return_value=True) as restore, \
            patch("voice.play_chime"):
        v, wd = _voice_with_detector(monkeypatch, Config(listen_for_jarvis=False))
        wd.start()
        try:
            assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
            assert v.request_listen("gesture") is True
            assert wait_until(lambda: v.audio_q.qsize() == 1, timeout=5.0)
            assert duck.call_count == 1 and v._ducked
            assert restore.call_count == 0          # held while the command is being handled
            v._restore_media("command done")
            assert restore.call_count == 1 and not v._ducked
        finally:
            stop_and_join(wd)


def test_push_to_talk_start_respects_duck_setting():
    cfg = Config(media_duck_on_wake=False)
    with patch("audio_ducker.duck", return_value=True) as duck, \
            patch("audio_ducker.restore", return_value=True), patch("voice.play_chime"):
        v = Voice(cfg, MagicMock(), MagicMock())
        v._on_push_to_talk_start("gesture")
        duck.assert_not_called()
