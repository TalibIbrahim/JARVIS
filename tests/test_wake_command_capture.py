""""Hey Jarvis <pause> <command>" capture and the live (Siri-style) transcript.

- The wake model keeps listening inside a passive VAD recording, so "Hey Jarvis" is detected even though
  VAD starts recording on its first syllable; the command is then captured as a normal wake.
- A pause after the wake word does not end the command recording; a pause mid-command shorter than
  COMMAND_MIN_ENDPOINT_S does not split it.
- Command recordings stream audio-so-far to on_partial_audio; the Voice side shows it on the island
  (wake word stripped) and never lets a late partial overwrite the final transcript.
"""
import threading
from unittest.mock import MagicMock

import pytest

import jarvis_log
import voice
from config import Config
from voice_fakes import CHUNK, FakeStream, install_audio, loud, make_detector, silence, stop_and_join, wait_until


class SeqModel:
    """openWakeWord stand-in: the n-th predict() returns scores(n) (1-based)."""

    def __init__(self, scores):
        self.scores = scores
        self.n = 0
        self.resets = 0

    def predict(self, arr):
        self.n += 1
        return {"hey_jarvis": self.scores(self.n)}

    def reset(self):
        self.resets += 1


def _plan_stream(plan):
    """plan: list of (kind, count) with kind 'S' (speech) or '.' (silence); silence after the plan."""
    seq = []
    for kind, count in plan:
        seq += [kind] * count

    def source(n):
        if n <= len(seq) and seq[n - 1] == "S":
            return loud(n)
        return silence()
    return FakeStream(source)


def _detector(monkeypatch, scores=None, **kw):
    cfg = Config()
    cfg.mic_pause_threshold = 0.5
    wd = make_detector(monkeypatch, cfg=cfg, **kw)
    if scores is not None:
        wd.model = SeqModel(scores)
    return wd


def _chunks(raw):
    return len(raw) // (CHUNK * 2)


# ---------------------------------------------------------------- wake word inside a passive recording

def test_wake_inside_passive_recording_abandons_it_and_keeps_tail(monkeypatch):
    wd = _detector(monkeypatch, scores=lambda n: 0.95 if n == 6 else 0.0)
    stream = _plan_stream([("S", 20)])
    raw = wd.record_utterance_with_vad(stream=stream, pre_roll=[loud(99)], trigger="vad", pre_speech_chunks=1)
    assert raw is None
    assert wd._mid_wake_tail is not None and len(wd._mid_wake_tail) == wd.MID_WAKE_TAIL_CHUNKS
    assert stream.reads == 6, "recording must stop on the chunk the wake word fired"


def test_passive_recording_without_wake_is_unchanged(monkeypatch):
    wd = _detector(monkeypatch, scores=lambda n: 0.0)
    raw = wd.record_utterance_with_vad(stream=_plan_stream([("S", 8)]), trigger="vad", pre_speech_chunks=1)
    assert raw is not None and wd._mid_wake_tail is None


def test_capture_and_command_recordings_do_not_watch_for_wake(monkeypatch):
    wd = _detector(monkeypatch, scores=lambda n: 0.95)
    raw = wd.record_utterance_with_vad(stream=_plan_stream([("S", 8)]), trigger="followup", pre_speech_chunks=1)
    assert raw is not None and wd._mid_wake_tail is None
    assert wd.model.n == 0


def test_hey_jarvis_pause_command_dispatches_one_command(monkeypatch):
    """Run loop: 'Hey Jarvis' (wake fires late, inside the VAD recording), 0.8 s pause, then the command."""
    env = install_audio(monkeypatch)
    plan = [("S", 8), (".", 10), ("S", 15)]
    seq = "".join(k * c for k, c in plan)
    env.source = lambda n: loud(n) if n <= len(seq) and seq[n - 1] == "S" else silence()
    dispatched, wakes = [], []
    wd = _detector(monkeypatch, scores=lambda n: 0.0)
    wd.model = SeqModel(lambda n: 0.95 if n == 7 else 0.0)   # 7th chunk the model hears = end of "Jarvis"
    wd.on_wake = lambda: wakes.append(1)
    wd.on_utterance = lambda raw, is_command=True: dispatched.append((raw, is_command))
    wd.start()
    try:
        assert wait_until(lambda: dispatched, timeout=5.0)
    finally:
        stop_and_join(wd)
    assert wakes == [1]
    assert len(dispatched) == 1
    raw, is_command = dispatched[0]
    assert is_command is True
    assert _chunks(raw) >= 15 + 10, "the command after the pause must be in the same recording"
    assert wd.model.resets >= 1, "stale 'Hey Jarvis' features would re-trigger right after the command"


def test_wake_model_reset_after_command_prevents_double_wake(monkeypatch):
    wd = _detector(monkeypatch, scores=lambda n: 0.0)
    wd.on_utterance = lambda raw, is_command=True: None
    wd.on_wake = lambda: None
    wd._on_wake_fired(_plan_stream([("S", 6)]), [], CHUNK, 16000)
    assert wd.model.resets == 1


# ---------------------------------------------------------------- endpointing for commands

def test_pause_after_wake_word_does_not_end_command(monkeypatch):
    wd = _detector(monkeypatch)
    # wake-word tail (2 chunks), 0.88 s pause, command, then silence
    raw = wd.record_utterance_with_vad(stream=_plan_stream([("S", 2), (".", 11), ("S", 10)]),
                                       trigger="wake", timeout_no_speech=3.5)
    assert raw is not None
    assert _chunks(raw) >= 2 + 11 + 10


def test_long_silence_after_wake_word_still_ends(monkeypatch):
    wd = _detector(monkeypatch)
    stream = _plan_stream([("S", 2)])
    raw = wd.record_utterance_with_vad(stream=stream, trigger="wake", timeout_no_speech=3.5)
    assert raw is None                       # too little speech: the follow-up window takes over
    grace_chunks = int(wd.WAKE_PAUSE_GRACE_S / (CHUNK / 16000)) + 1
    assert stream.reads <= 2 + grace_chunks + 1


def test_mid_command_pause_shorter_than_min_endpoint_does_not_split(monkeypatch):
    wd = _detector(monkeypatch)
    raw = wd.record_utterance_with_vad(stream=_plan_stream([("S", 8), (".", 8), ("S", 4)]), trigger="wake")
    assert raw is not None and _chunks(raw) >= 8 + 8 + 4


def test_explicit_silence_endpoint_is_respected(monkeypatch):
    wd = _detector(monkeypatch)
    raw = wd.record_utterance_with_vad(stream=_plan_stream([("S", 8), (".", 8), ("S", 4)]), trigger="wake",
                                       silence_endpoint=0.5)
    assert raw is not None and _chunks(raw) < 8 + 8 + 4


# ---------------------------------------------------------------- partial audio stream

def test_command_recording_streams_partials_then_none(monkeypatch):
    got = []
    wd = _detector(monkeypatch, on_partial_audio=got.append)
    raw = wd.record_utterance_with_vad(stream=_plan_stream([("S", 30)]), trigger="wake")
    assert raw is not None
    assert got[-1] is None
    clips = got[:-1]
    assert clips, "no partial audio during a 2.4 s command"
    assert all(isinstance(c, bytes) for c in clips)
    assert [len(c) for c in clips] == sorted(len(c) for c in clips)


def test_passive_recording_streams_no_partials(monkeypatch):
    got = []
    wd = _detector(monkeypatch, on_partial_audio=got.append)
    wd.record_utterance_with_vad(stream=_plan_stream([("S", 30)]), trigger="vad", pre_speech_chunks=1)
    assert got == []


# ---------------------------------------------------------------- Voice side

class BlockingLocal:
    def __init__(self, text):
        self.text = text
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    def available(self):
        return True

    def transcribe(self, pcm, prompt=None, hotwords=None):
        self.started.set()
        self.release.wait(3.0)
        return self.text, 0.9


def _voice(monkeypatch, local):
    cfg = Config()
    cfg.wake_aliases = ["jarvis"]
    v = voice.Voice(cfg, MagicMock(), threading.Event())
    monkeypatch.setattr(v, "_local_stt_ready", lambda: local)
    posts = []
    monkeypatch.setattr(v, "post_partial_speech", lambda text, is_final=False: posts.append(text))
    return v, posts


@pytest.mark.parametrize("heard,shown", [
    ("Hey Jarvis, what's the", "What's the"),
    ("Jarvis what's the time", "What's the time"),
    ("okay jarvis open chrome", "Open chrome"),
    ("what's the time", "What's the time"),
    ("Thank you.", ""),
    ("Hey Jarvis.", ""),
])
def test_partial_display_text(monkeypatch, heard, shown):
    v, _ = _voice(monkeypatch, None)
    assert v._partial_display_text(heard) == shown


def test_partial_worker_posts_live_text(monkeypatch):
    local = BlockingLocal("Hey Jarvis what's the")
    v, posts = _voice(monkeypatch, local)
    t = threading.Thread(target=v._partial_worker, daemon=True)
    t.start()
    try:
        v._on_partial_audio(b"\x01\x00" * 16000)
        assert wait_until(lambda: posts, timeout=3.0)
        assert posts == ["What's the"]
    finally:
        v._own_stop.set()
        t.join(2.0)


def test_late_partial_never_overwrites_final(monkeypatch):
    local = BlockingLocal("what's the time")
    local.release.clear()
    v, posts = _voice(monkeypatch, local)
    t = threading.Thread(target=v._partial_worker, daemon=True)
    t.start()
    try:
        v._on_partial_audio(b"\x01\x00" * 16000)
        assert local.started.wait(2.0)
        v._on_partial_audio(None)            # recording ended while the partial was transcribing
        local.release.set()
        assert not wait_until(lambda: posts, timeout=0.5)
    finally:
        v._own_stop.set()
        t.join(2.0)


def test_no_partials_without_local_model(monkeypatch):
    v, _ = _voice(monkeypatch, None)
    v._on_partial_audio(b"\x01\x00" * 16000)
    assert v._partial_pending is None


class _Stop(Exception):
    pass


@pytest.mark.parametrize("heard,command", [
    ("hey jarvis what's the time", "what's the time"),
    ("Hey, Jarvis, what's the time", "what's the time"),
    ("jarvis what's the time", "what's the time"),
    ("hey jarvis", ""),
])
def test_active_command_strips_hey_jarvis(monkeypatch, heard, command):
    v, _ = _voice(monkeypatch, None)
    seen = {}

    def event(kind, *a, **kw):
        if kind == jarvis_log.WAKE_GATE:
            seen.update(kw)
            raise _Stop()
    monkeypatch.setattr(voice.jarvis_log, "utterance_event", event)
    with pytest.raises(_Stop):
        v._process(heard, is_command=True)
    assert seen["command"] == command
