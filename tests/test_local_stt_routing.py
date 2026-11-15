"""Local STT routing, wake-gated passive capture, queue priority, live endpointing, wake guard + CPU skip."""
import queue
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import local_stt
import voice
from config import Config
from voice_fakes import FakeStream, loud, silence, make_detector


def _pcm(seconds=1.0, amp=3000):
    rng = np.random.default_rng(3)
    return rng.integers(-amp, amp, int(16000 * seconds)).astype(np.int16).tobytes()


class FakeLocal:
    def __init__(self, text="open chrome", conf=0.9, ready=True):
        self.text, self.conf, self.ready = text, conf, ready
        self.calls = 0

    def available(self):
        return self.ready

    def transcribe(self, pcm, prompt=None):
        self.calls += 1
        return self.text, self.conf


def _voice(monkeypatch, local, ai=True, engine="local", cooldown=False):
    cfg = Config()
    cfg.wake_aliases = ["jarvis"]
    cfg.stt_engine = engine
    cfg.ai_enabled = ai
    cfg.local_stt_min_confidence = 0.55
    v = voice.Voice(cfg, MagicMock(), MagicMock())
    monkeypatch.setattr(local_stt, "get_local_transcriber", lambda name="base.en": local)
    monkeypatch.setattr(voice, "get_api_key", lambda *a, **k: "gsk_test" if ai else None)
    if cooldown:
        v.groq_cooldown_until = time.time() + 100
    v._groq = MagicMock(return_value=("groq text", False))
    v._google = MagicMock(return_value="google text")
    v._process = MagicMock()
    monkeypatch.setattr(v, "_stt_groq", v._groq)
    monkeypatch.setattr(v, "_fallback_google", v._google)
    return v


def _run(v, is_command):
    import speech_recognition as sr
    audio = sr.AudioData(_pcm(), 16000, 2)
    v._handle_audio_item(MagicMock(), sr, MagicMock(is_enrolled=False), audio, is_command, time.time(), "u1")


def test_confident_local_never_calls_groq(monkeypatch):
    v = _voice(monkeypatch, FakeLocal(conf=0.9))
    _run(v, True)
    v._groq.assert_not_called()
    v._google.assert_not_called()
    v._process.assert_called_once_with("open chrome", is_command=True)


def test_low_confidence_active_retries_groq(monkeypatch):
    v = _voice(monkeypatch, FakeLocal(conf=0.2))
    _run(v, True)
    v._groq.assert_called_once()
    assert v._groq.call_args.kwargs["google_fallback"] is False
    v._process.assert_called_once_with("groq text", is_command=True)


def test_low_confidence_groq_failure_keeps_local_text(monkeypatch):
    v = _voice(monkeypatch, FakeLocal(conf=0.2))
    v._groq.return_value = (None, False)
    _run(v, True)
    v._process.assert_called_once_with("open chrome", is_command=True)
    v._google.assert_not_called()


@pytest.mark.parametrize("ai,cooldown", [(False, False), (True, True)])
def test_low_confidence_without_groq_accepts_local(monkeypatch, ai, cooldown):
    v = _voice(monkeypatch, FakeLocal(conf=0.2), ai=ai, cooldown=cooldown)
    _run(v, True)
    v._groq.assert_not_called()
    v._process.assert_called_once_with("open chrome", is_command=True)


def test_passive_burst_never_reaches_groq(monkeypatch):
    v = _voice(monkeypatch, FakeLocal(text="jarvis open chrome", conf=0.1))
    _run(v, False)   # wake-less, no follow-up window
    v._groq.assert_not_called()
    v._google.assert_not_called()
    v._process.assert_called_once_with("jarvis open chrome", is_command=False)


def test_engine_groq_keeps_old_path(monkeypatch):
    local = FakeLocal()
    v = _voice(monkeypatch, local, engine="groq")
    _run(v, True)
    assert local.calls == 0
    v._groq.assert_called_once()
    assert v._groq.call_args.kwargs.get("google_fallback", True) is True


def test_local_not_ready_falls_back_to_groq(monkeypatch):
    v = _voice(monkeypatch, FakeLocal(ready=False))
    _run(v, True)
    v._groq.assert_called_once()


def test_local_exception_falls_back(monkeypatch):
    local = FakeLocal()
    local.transcribe = MagicMock(side_effect=RuntimeError("boom"))
    v = _voice(monkeypatch, local)
    _run(v, True)
    v._groq.assert_called_once()


# ---- passive pre-gate when local STT is unavailable
def _gate_voice(monkeypatch, peak, local_ready):
    v = _voice(monkeypatch, FakeLocal(ready=local_ready))
    v.wake_detector = SimpleNamespace(threshold=0.5, last_burst_wake_peak=peak, listening_until=0.0)
    monkeypatch.setattr(voice, "jarvis_log", voice.jarvis_log)
    return v


def test_pregate_drops_quiet_passive_burst_without_local(monkeypatch):
    v = _gate_voice(monkeypatch, peak=0.05, local_ready=False)
    v._on_utterance_recorded(_pcm(), is_command=False)
    assert v.audio_q.qsize() == 0


def test_pregate_passes_when_wake_model_stirred(monkeypatch):
    v = _gate_voice(monkeypatch, peak=0.3, local_ready=False)   # >= 0.5 * 0.5
    v._on_utterance_recorded(_pcm(), is_command=False)
    assert v.audio_q.qsize() == 1


def test_pregate_skipped_with_local_and_for_commands(monkeypatch):
    v = _gate_voice(monkeypatch, peak=0.0, local_ready=True)
    v._on_utterance_recorded(_pcm(), is_command=False)
    assert v.audio_q.qsize() == 1
    v2 = _gate_voice(monkeypatch, peak=0.0, local_ready=False)
    v2._on_utterance_recorded(_pcm(), is_command=True)
    assert v2.audio_q.qsize() == 1


# ---- queue priority
def _fill_passive(v, n=3):
    for i in range(n):
        v.audio_q.put_nowait((f"p{i}", False, 0.0, f"u{i}"))


def test_command_evicts_oldest_passive_when_full():
    v = voice.Voice(Config(), MagicMock(), MagicMock())
    _fill_passive(v)
    assert v._enqueue_audio(("cmd", True, 0.0, "c"), True) is True
    items = list(v.audio_q.queue)
    assert [i[0] for i in items] == ["p1", "p2", "cmd"]


def test_passive_dropped_when_full_and_commands_never_evicted():
    v = voice.Voice(Config(), MagicMock(), MagicMock())
    for i in range(3):
        v.audio_q.put_nowait((f"c{i}", True, 0.0, f"u{i}"))
    assert v._enqueue_audio(("p", False, 0.0, "x"), False) is False
    assert v._enqueue_audio(("c3", True, 0.0, "y"), True) is False
    assert [i[0] for i in v.audio_q.queue] == ["c0", "c1", "c2"]


# ---- endpointing
def _record_reads(pause, loud_chunks=4):
    cfg = Config()
    cfg.mic_pause_threshold = pause
    cfg.mic_phrase_time_limit = 15.0
    det = make_detector_nomonkey(cfg)
    stream = FakeStream(lambda n: loud(n) if n <= loud_chunks else silence())
    raw = det.record_utterance_with_vad(stream=stream, timeout_no_speech=5.0)
    assert raw
    return stream.reads


def make_detector_nomonkey(cfg):
    with patch.object(voice.VADDetector, "_init_vad", lambda self: None), \
         patch.object(voice.WakeWordDetector, "_init_model", lambda self: None):
        cfg.wake_aliases = ["jarvis"]
        return voice.WakeWordDetector(cfg=cfg)


def test_endpoint_follows_mic_pause_threshold():
    short = _record_reads(0.3)    # 4 silent chunks
    long_ = _record_reads(1.5)    # 19 silent chunks
    assert short == 4 + 4
    assert long_ == 4 + 19


def test_endpoint_is_clamped_and_live():
    cfg = Config()
    det = make_detector_nomonkey(cfg)
    cfg.mic_pause_threshold = 0.01
    assert det.endpoint_silence_s() == 0.3
    cfg.mic_pause_threshold = 99
    assert det.endpoint_silence_s() == 2.0
    cfg.mic_phrase_time_limit = 1
    assert det.max_phrase_s() == 3.0
    cfg.mic_phrase_time_limit = 99
    assert det.max_phrase_s() == 15.0


# ---- wake guard / sensitivity
def test_guard_needs_two_frames_or_high_confidence():
    g = voice.WakeWordGuard(threshold=0.5, debounce_seconds=0.0, require_consecutive=True)
    assert g.evaluate(0.55, now=1.0) is False       # single marginal frame
    assert g.evaluate(0.1, now=2.0) is False        # run broken
    assert g.evaluate(0.55, now=3.0) is False
    assert g.evaluate(0.55, now=4.0) is True        # 2 consecutive
    assert g.evaluate(0.9, now=5.0) is True         # single high-confidence frame


def test_detector_guard_requires_consecutive(monkeypatch):
    det = make_detector(monkeypatch)
    assert det.guard.require_consecutive is True


def test_parse_sensitivity_aggressive_alias():
    assert voice.parse_sensitivity("aggressive") == voice.parse_sensitivity("high") == 0.35


# ---- wake-model CPU skip
class _CountModel:
    def __init__(self):
        self.n = 0

    def predict(self, arr):
        self.n += 1
        return {"hey_jarvis": 0.0}


def test_wake_skip_after_sustained_silence_and_replays_ring(monkeypatch):
    det = make_detector(monkeypatch)
    det.model = _CountModel()
    arr = np.zeros(1280, dtype=np.int16)
    for _ in range(det.WAKE_SKIP_AFTER_QUIET):
        det._wake_predict(arr, True)
    assert det.model.n == det.WAKE_SKIP_AFTER_QUIET          # not skipped yet
    for _ in range(10):
        det._wake_predict(arr, True)
    assert det.model.n == det.WAKE_SKIP_AFTER_QUIET          # skipped
    det._wake_predict(arr, False)                            # onset: replay 2 skipped + current
    assert det.model.n == det.WAKE_SKIP_AFTER_QUIET + 3


def test_not_quiet_when_aec_active_or_speechlike(monkeypatch):
    det = make_detector(monkeypatch)
    det.vad.last_prob = 0.0
    assert det._is_quiet(10.0, None) is True
    assert det._is_quiet(10.0, np.zeros(4)) is False
    assert det._is_quiet(10_000.0, None) is False
    det.vad.last_prob = 0.5
    assert det._is_quiet(10.0, None) is False


# ---- confidence summary
def test_summarize_segments():
    seg = lambda t, lp, ns=0.0: SimpleNamespace(text=t, avg_logprob=lp, no_speech_prob=ns)
    text, conf = local_stt.summarize_segments([seg(" open chrome", -0.1), seg("", -5.0), seg("noise", -3, 0.9)])
    assert text == "open chrome"
    assert conf == pytest.approx(np.exp(-0.1))
    assert local_stt.summarize_segments([]) == ("", 0.0)


def test_groq_client_is_reused(monkeypatch):
    made = []

    class G:
        def __init__(self, api_key=None, timeout=10):
            made.append(self)

    monkeypatch.setattr(voice, "GroqClient", G)
    monkeypatch.setattr(voice, "_GROQ_STT_CLIENT", None)
    a = voice._get_groq_stt_client("k", 10)
    b = voice._get_groq_stt_client("k", 10)
    c = voice._get_groq_stt_client("k2", 10)
    assert a is b and a is not c and len(made) == 2
