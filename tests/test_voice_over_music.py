"""Voice Over Music (dual raw/AEC path) in WakeWordDetector, with a fake EchoCanceller. No loopback."""
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

import voice
from config import Config
from voice import VADDetector, Voice, WakeWordDetector
from voice_fakes import (FakeAEC, FakeStream, install_audio, install_fake_aec, install_openwakeword,
                         loud, make_detector, silence, stop_and_join, wait_until)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    prev = Voice.is_speaking
    Voice.is_speaking = False
    monkeypatch.setattr(WakeWordDetector, "LISTEN_OFF_POLL_S", 0.02)
    yield
    Voice.is_speaking = prev


@pytest.fixture
def audio(monkeypatch):
    return install_audio(monkeypatch)


@pytest.fixture
def aec(monkeypatch):
    return install_fake_aec(monkeypatch)


def _arr(amp=0, n=1280):
    return np.full(n, amp, dtype=np.int16)


def _rms(a):
    return float(np.sqrt(np.mean(a.astype(float) ** 2)))


# ------------------------------------------------------------------ setting off
def test_setting_off_no_aec_and_identical_vad(monkeypatch, aec):
    wd = make_detector(monkeypatch, Config(voice_over_music=False))
    wd._sync_aec()
    assert wd._aec is None and wd._aec_vad is None and wd._aec_model is None
    assert aec.instances == []
    assert wd._aec_hb() == {}                      # no aec field in the heartbeat
    assert wd._aec_chunk(_arr(1000)) is None
    ref = VADDetector(cfg=wd.cfg)
    for amp in (0, 50, 400, 3000, 90, 5000, 10, 800):
        a = _arr(amp)
        r = _rms(a)
        assert wd._vad_dual(a, r, wd._aec_chunk(a)) == ref.is_speech(a, r)


def test_setting_off_heartbeat_has_no_aec_field(monkeypatch, audio, aec):
    beats = []
    monkeypatch.setattr(voice.jarvis_log, "heartbeat", lambda name, **f: beats.append((name, f)))
    wd = make_detector(monkeypatch, Config(voice_over_music=False))
    audio.on_read = lambda n: wd.stop.set() if n >= 5 else None
    wd.start()
    wd.join(5)
    assert not wd.is_alive()
    det = [f for n, f in beats if n == "WakeWordDetector" and "rms" in f]
    assert det and all("aec" not in f for f in det)
    assert aec.instances == []


def test_setting_on_heartbeat_reports_aec(monkeypatch, audio, aec):
    beats = []
    monkeypatch.setattr(voice.jarvis_log, "heartbeat", lambda name, **f: beats.append((name, f)))
    wd = make_detector(monkeypatch, Config(voice_over_music=True))
    audio.on_read = lambda n: wd.stop.set() if n >= 5 else None
    wd.start()
    wd.join(5)
    assert not wd.is_alive()
    det = [f for n, f in beats if n == "WakeWordDetector" and "rms" in f]
    assert det and det[0].get("aec") in ("on", "idle", "off")


# ------------------------------------------------------------------ lifecycle
def test_toggle_creates_and_closes_canceller(monkeypatch, audio, aec):
    cfg = Config(voice_over_music=False)
    wd = make_detector(monkeypatch, cfg)
    wd.start()
    try:
        assert wait_until(lambda: audio.reads > 3)
        assert aec.instances == []
        cfg.voice_over_music = True
        assert wait_until(lambda: len(aec.instances) == 1 and wd._aec is not None)
        assert aec.instances[0].started == 1 and aec.instances[0].closed == 0
        cfg.voice_over_music = False
        assert wait_until(lambda: aec.instances[0].closed == 1 and wd._aec is None)
        n = audio.reads
        assert wait_until(lambda: audio.reads > n + 3)
        assert len(aec.instances) == 1 and aec.instances[0].closed == 1   # closed exactly once
        cfg.voice_over_music = True
        assert wait_until(lambda: len(aec.instances) == 2)
    finally:
        stop_and_join(wd)
    assert aec.instances[1].closed == 1            # closed when the stream closed on stop
    assert wd._aec is None


def test_closed_when_mic_stream_closes(monkeypatch, audio, aec):
    cfg = Config(voice_over_music=True)
    wd = make_detector(monkeypatch, cfg)
    wd.start()
    try:
        assert wait_until(lambda: len(aec.instances) == 1 and wd._aec is not None)
        cfg.listen_for_jarvis = False                  # stream closes
        assert wait_until(lambda: audio.streams[0].closed)
        assert wait_until(lambda: aec.instances[0].closed == 1 and wd._aec is None)
    finally:
        stop_and_join(wd)


def test_closed_on_thread_exit(monkeypatch, aec):
    wd = make_detector(monkeypatch, Config(voice_over_music=True))
    f = FakeAEC()
    wd._aec = f
    monkeypatch.setattr(wd, "_run_body", lambda: None)
    wd.run()
    assert f.closed == 1 and wd._aec is None

    wd2 = make_detector(monkeypatch, Config(voice_over_music=True))
    f2 = FakeAEC()
    wd2._aec = f2

    def boom():
        raise RuntimeError("detector crashed")
    monkeypatch.setattr(wd2, "_run_body", boom)
    with pytest.raises(RuntimeError):
        wd2.run()
    assert f2.closed == 1 and wd2._aec is None


def test_failed_start_not_retried_until_toggled(monkeypatch, audio):
    fake = install_fake_aec(monkeypatch, start_ok=False)
    cfg = Config(voice_over_music=True)
    wd = make_detector(monkeypatch, cfg)
    wd.start()
    try:
        assert wait_until(lambda: len(fake.instances) == 1)
        n = audio.reads
        assert wait_until(lambda: audio.reads > n + 20)
        assert len(fake.instances) == 1 and fake.instances[0].started == 1
        assert wd._aec is None and wd._aec_failed
        cfg.voice_over_music = False
        assert wait_until(lambda: not wd._aec_failed)
        cfg.voice_over_music = True
        assert wait_until(lambda: len(fake.instances) == 2)
    finally:
        stop_and_join(wd)


def test_start_raising_is_contained(monkeypatch, audio):
    import echo_canceller

    class Bad:
        def __init__(self):
            raise OSError("no device")
    monkeypatch.setattr(echo_canceller, "EchoCanceller", Bad)
    wd = make_detector(monkeypatch, Config(voice_over_music=True))
    wd._sync_aec()
    wd._sync_aec()
    assert wd._aec is None and wd._aec_failed


# ------------------------------------------------------------------ _aec_chunk
def test_aec_chunk_none_when_process_returns_input(monkeypatch, aec):
    wd = make_detector(monkeypatch, Config(voice_over_music=True))
    f = FakeAEC()
    f.mode = "idle"
    wd._aec = f
    a = _arr(500)
    assert wd._aec_chunk(a) is None and wd._aec_active is False
    f.mode = "echo"
    out = wd._aec_chunk(a)
    assert out is not None and out is not a and wd._aec_active is True
    f.mode = "idle"
    assert wd._aec_chunk(a) is None and wd._aec_active is False


def test_aec_chunk_resets_model_on_activation(monkeypatch, aec):
    wd = make_detector(monkeypatch, Config(voice_over_music=True))
    f = FakeAEC()
    wd._aec = f
    wd._aec_model = MagicMock()
    a = _arr(500)
    wd._aec_chunk(a)
    wd._aec_chunk(a)
    assert wd._aec_model.reset.call_count == 1       # only on the idle -> active edge


# ------------------------------------------------------------------ wake = max(raw, aec)
def _run_scores(monkeypatch, audio, raw, aec_score, mode):
    install_openwakeword(monkeypatch, [raw, aec_score])
    fake = install_fake_aec(monkeypatch)
    wd = make_detector(monkeypatch, Config(voice_over_music=True), with_model=True)
    seen = []
    monkeypatch.setattr(wd.guard, "evaluate", lambda score, is_speaking=False, now=None: seen.append(score) or False)
    orig = FakeAEC.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.mode = mode
    monkeypatch.setattr(FakeAEC, "__init__", init)
    audio.on_read = lambda n: wd.stop.set() if n >= 6 else None
    wd.start()
    wd.join(5)
    assert not wd.is_alive()
    return seen


@pytest.mark.parametrize("raw,aec_score,expected", [(0.2, 0.8, 0.8), (0.9, 0.1, 0.9), (0.5, 0.5, 0.5)])
def test_wake_score_is_max_of_raw_and_aec(monkeypatch, audio, raw, aec_score, expected):
    seen = _run_scores(monkeypatch, audio, raw, aec_score, "echo")
    assert seen and max(seen) == pytest.approx(expected)
    assert min(seen) == pytest.approx(expected)       # every chunk past the AEC start uses max()


def test_wake_score_raw_only_when_aec_idle(monkeypatch, audio):
    seen = _run_scores(monkeypatch, audio, 0.2, 0.8, "idle")
    assert seen and all(s == pytest.approx(0.2) for s in seen)


# ------------------------------------------------------------------ VAD OR
def _dual_detector(monkeypatch):
    cfg = Config(voice_over_music=True, mic_energy_threshold=100)
    wd = make_detector(monkeypatch, cfg)
    wd._aec_vad = VADDetector(cfg=cfg)
    wd.vad.noise_floor = 1000.0          # music inflated the raw gate: 1.5 * 1000 = 1500
    return wd


def test_vad_or_and_raw_uses_aec_gate(monkeypatch):
    wd = _dual_detector(monkeypatch)
    assert wd.vad.gate() == 1500.0 and wd._aec_vad.gate() == 100.0
    spy = MagicMock(wraps=wd.vad.is_speech)
    monkeypatch.setattr(wd.vad, "is_speech", spy)

    quiet, mid = _arr(0), _arr(500)
    # AEC inactive: raw gate only, 500 < 1500
    assert wd._vad_dual(mid, _rms(mid), None) is False
    assert spy.call_args.kwargs.get("gate_override") is None
    # raw speech passes via the AEC gate while the AEC path itself hears nothing
    assert wd._vad_dual(mid, _rms(mid), quiet) is True
    assert spy.call_args.kwargs["gate_override"] == 100.0
    # AEC path hears speech while raw is under the gate
    low = _arr(50)
    assert wd._vad_dual(low, _rms(low), mid) is True
    # neither
    assert wd._vad_dual(low, _rms(low), quiet) is False


# ------------------------------------------------------------------ recording stays raw
def test_recorded_bytes_are_raw_frames(monkeypatch, aec):
    wd = make_detector(monkeypatch, Config(voice_over_music=True))
    f = FakeAEC()                         # returns arr // 2, i.e. clearly different from the input
    wd._aec = f
    wd._aec_vad = VADDetector(cfg=wd.cfg)
    source = lambda n: loud(n) if n <= 12 else silence()
    stream = FakeStream(source)
    raw = wd.record_utterance_with_vad(stream=stream)
    assert raw
    expected = b"".join(source(i) for i in range(1, stream.reads + 1))
    assert raw == expected
    assert f.process_calls == stream.reads
    assert Voice._last_raw_audio == raw


# ------------------------------------------------------------------ stats line
def test_stats_line_includes_aec(monkeypatch, aec):
    log = MagicMock()
    monkeypatch.setattr(voice, "_vlog", log)
    wd = make_detector(monkeypatch, Config(voice_over_music=True))
    wd._aec = FakeAEC()
    wd._aec_vad = VADDetector(cfg=wd.cfg)
    wd._st_t = time.monotonic() - 61.0
    wd._diag_chunk(100.0)
    lines = [c.args[0] % c.args[1:] for c in log.info.call_args_list if c.args and "wake stats" in c.args[0]]
    assert len(lines) == 1 and " aec={" in lines[0] and "erle_db_estimate" in lines[0]

    log.reset_mock()
    wd2 = make_detector(monkeypatch, Config(voice_over_music=False))
    wd2._st_t = time.monotonic() - 61.0
    wd2._diag_chunk(100.0)
    lines = [c.args[0] % c.args[1:] for c in log.info.call_args_list if c.args and "wake stats" in c.args[0]]
    assert len(lines) == 1 and "aec=" not in lines[0]
