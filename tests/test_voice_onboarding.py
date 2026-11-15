"""Onboarding voice API (capture_answer / calibrate_from_sample), voice learning hooks, Piper single-stream
playback and the opt-in voice model registry. All devices, models and network are mocked."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import voice
import voice_models
from config import Config
from voice import Speaker, Voice, WakeWordDetector
from voice_fakes import install_audio, loud, make_detector, silence, stop_and_join, wait_until


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    prev = Voice.is_speaking
    Voice.is_speaking = False
    monkeypatch.setattr(WakeWordDetector, "LISTEN_OFF_POLL_S", 0.02)
    yield
    Voice.is_speaking = prev


class FakePersonal:
    def __init__(self, vocab=("Malenia", "Elden Ring")):
        self.clips = []
        self.vocab = list(vocab)

    def save_voice_clip(self, pcm16, sample_rate, transcript, meta):
        self.clips.append((pcm16, sample_rate, transcript, meta))

    def vocabulary(self, limit=50):
        return self.vocab

    def apply_corrections(self, text):
        return text.replace("mallenia", "Malenia")


def _running_voice(monkeypatch, cfg, source=None, personal=None):
    audio = install_audio(monkeypatch)
    box = {}
    spoken = [0]

    def speech_during_capture(n):
        # the user speaks (12 loud chunks) only once a capture is under way, then goes quiet
        if box["wd"]._capture_req is not None and spoken[0] < 12:
            spoken[0] += 1
            return loud(spoken[0])
        return silence()
    audio.source = source or speech_during_capture
    dispatched = []
    wd = make_detector(monkeypatch, cfg,
                       on_utterance=lambda raw, is_command=False: dispatched.append(raw))
    box["wd"] = wd
    v = Voice(cfg, MagicMock(), MagicMock())
    v.wake_detector = wd
    v._process = MagicMock()
    monkeypatch.setattr(voice, "_get_personal", lambda: personal)
    wd.start()
    # start from "listening is on and quiet": the capture request is picked up by the main loop
    return v, wd, dispatched


# ------------------------------------------------------------------ capture_answer
def test_capture_answer_returns_transcript_and_is_not_dispatched(monkeypatch):
    v, wd, dispatched = _running_voice(monkeypatch, Config(), personal=FakePersonal())
    v._transcribe_blocking = MagicMock(return_value=("my name is Bob", 0.9, "local"))
    levels = []
    try:
        text = v.capture_answer(timeout=5.0, on_level=levels.append)
        assert text == "my name is Bob"
        assert dispatched == [] and v._process.call_count == 0 and v.audio_q.empty()
        assert levels and all(0.0 <= x <= 1.0 for x in levels) and max(levels) > 0.3
        assert wd._capture_req is None and not wd._capture_active
    finally:
        stop_and_join(wd)


def test_capture_answer_silence_returns_empty(monkeypatch):
    v, wd, dispatched = _running_voice(monkeypatch, Config(), source=lambda n: silence())
    v._transcribe_blocking = MagicMock()
    try:
        assert v.capture_answer(timeout=0.3) == ""
        v._transcribe_blocking.assert_not_called()
        assert dispatched == []
        v.ctx.overlay.post.assert_not_called()     # no no_speech / listening_done side effects
    finally:
        stop_and_join(wd)


def test_capture_works_while_listen_for_jarvis_off(monkeypatch):
    v, wd, dispatched = _running_voice(monkeypatch, Config(listen_for_jarvis=False))
    v._transcribe_blocking = MagicMock(return_value=("hello", None, "google"))
    try:
        assert wait_until(lambda: wd.mic_state() == ("ok", "Listening off"))
        assert v.capture_answer(timeout=5.0) == "hello"
        assert dispatched == []
    finally:
        stop_and_join(wd)


def test_capture_without_detector_or_when_suspended_is_empty():
    v = Voice(Config(), MagicMock(), MagicMock())
    assert v.capture_answer(timeout=0.1) == ""
    v.wake_detector = MagicMock(suspended=True)
    assert v.capture_answer(timeout=0.1) == ""
    v.wake_detector.request_capture.assert_not_called()


# ------------------------------------------------------------------ calibrate_from_sample
def test_calibrate_returns_metrics_applies_floor_and_saves_clip(monkeypatch):
    p = FakePersonal()
    saved = []
    import config
    monkeypatch.setattr(config, "save_config", lambda cfg, path=None: saved.append(cfg.mic_energy_threshold) or True)
    cfg = Config()
    v, wd, dispatched = _running_voice(monkeypatch, cfg, personal=p)
    v._transcribe_blocking = MagicMock(return_value=("the quick brown fox", 0.9, "local"))
    try:
        out = v.calibrate_from_sample("the quick brown fox", timeout=5.0)
        assert out["ok"] is True and out["transcript"] == "the quick brown fox"
        assert out["level_db"] > -40 and out["snr_db"] >= 6.0
        assert 100 <= cfg.mic_energy_threshold <= 2400 and saved == [cfg.mic_energy_threshold]
        assert dispatched == []
        assert wait_until(lambda: len(p.clips) == 1)
        pcm, sr, transcript, meta = p.clips[0]
        assert sr == 16000 and len(pcm) > 0 and transcript == "the quick brown fox"
        assert meta["kind"] == "calibration" and meta["expected"] == "the quick brown fox"
    finally:
        stop_and_join(wd)


def test_calibrate_nothing_heard_is_not_ok(monkeypatch):
    v, wd, _ = _running_voice(monkeypatch, Config(), source=lambda n: silence())
    try:
        out = v.calibrate_from_sample("hello there", timeout=0.3)
        assert out["ok"] is False and out["transcript"] == ""
        assert {"ok", "transcript", "level_db", "snr_db"} <= set(out)
    finally:
        stop_and_join(wd)


def test_analyze_levels_snr():
    rng = np.random.default_rng(1)
    quiet = rng.integers(-30, 30, 1280 * 10).astype(np.int16)
    talk = rng.integers(-6000, 6000, 1280 * 10).astype(np.int16)
    m = voice.analyze_levels(np.concatenate([quiet, talk]).tobytes())
    assert m["snr_db"] > 30 and -25 < m["level_db"] < -10


# ------------------------------------------------------------------ learning hooks
def _item_voice(monkeypatch, cfg, personal, text="open mallenia guide"):
    import local_stt
    seen = {}

    class Local:
        def available(self):
            return True

        def transcribe(self, pcm, prompt=None, hotwords=None):
            seen["hotwords"] = hotwords
            return text, 0.9

    v = Voice(cfg, MagicMock(), MagicMock())
    monkeypatch.setattr(local_stt, "get_local_transcriber", lambda name="base.en": Local())
    monkeypatch.setattr(voice, "_get_personal", lambda: personal)
    v._process = MagicMock()
    return v, seen


def _run_item(v):
    import speech_recognition as sr
    rng = np.random.default_rng(3)
    raw = rng.integers(-3000, 3000, 16000).astype(np.int16).tobytes()
    v._handle_audio_item(MagicMock(), sr, MagicMock(is_enrolled=False), sr.AudioData(raw, 16000, 2),
                         True, time.time(), "u1")


def test_corrections_applied_before_dispatch_only_when_enabled(monkeypatch):
    v, _ = _item_voice(monkeypatch, Config(learn_corrections=True), FakePersonal())
    _run_item(v)
    assert v._process.call_args[0][0] == "open Malenia guide"
    v, _ = _item_voice(monkeypatch, Config(learn_corrections=False), FakePersonal())
    _run_item(v)
    assert v._process.call_args[0][0] == "open mallenia guide"


def test_hotwords_use_vocabulary_without_wake_aliases(monkeypatch):
    p = FakePersonal(vocab=["Malenia", "Jarvis Prime", "Elden Ring"])
    v, seen = _item_voice(monkeypatch, Config(), p)
    _run_item(v)
    assert seen["hotwords"] == "Malenia, Elden Ring"
    v, seen = _item_voice(monkeypatch, Config(learn_corrections=False), p)
    _run_item(v)
    assert not seen["hotwords"]


def test_no_personal_module_is_harmless(monkeypatch):
    v, seen = _item_voice(monkeypatch, Config(), None)
    _run_item(v)
    assert v._process.call_args[0][0] == "open mallenia guide" and not seen["hotwords"]


@pytest.mark.parametrize("flag,expected", [(True, 1), (False, 0)])
def test_command_clip_saved_only_when_flag_on(monkeypatch, flag, expected):
    p = FakePersonal()
    cfg = Config(learn_voice_clips=flag)
    cfg.wake_aliases = ["jarvis"]
    v = Voice(cfg, MagicMock(), MagicMock())
    monkeypatch.setattr(voice, "_get_personal", lambda: p)
    monkeypatch.setattr(voice, "handle_island_voice_command", lambda c, ctx: None)
    monkeypatch.setattr(voice, "run_command", lambda c, ctx: "Done.")
    v._utt_ctx = {"raw": b"\x01\x00" * 4000, "engine": "local", "conf": 0.8, "dur_s": 0.25}
    v._process("jarvis open chrome", is_command=True)
    time.sleep(0.2)
    assert len(p.clips) == expected
    if expected:
        pcm, sr, transcript, meta = p.clips[0]
        assert transcript == "open chrome" and sr == 16000
        assert meta["engine"] == "local" and meta["confidence"] == 0.8 and meta["duration"] == 0.25


# ------------------------------------------------------------------ TTS: no auto-download, one stream
def test_no_auto_download_when_voice_missing(monkeypatch, tmp_path):
    assert not hasattr(voice, "ensure_piper_model")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(voice_models, "get_paths", lambda vid: None)
    monkeypatch.setattr(voice_models, "download", MagicMock(side_effect=AssertionError("must not download")))
    import requests
    monkeypatch.setattr(requests, "get", MagicMock(side_effect=AssertionError("network")))
    sp = Speaker(enabled=True, cfg=Config())
    sp._say_system = MagicMock()
    sp._say_piper("hello there")
    sp._say_system.assert_called_once_with("hello there")
    assert sp._get_piper_paths() == (None, None)


class _Chunk:
    def __init__(self, n=11025, sr=22050, val=0.5):
        self.sample_rate = sr
        self.audio_float_array = np.full(n, val, dtype=np.float32)


class _FakeSD:
    """sounddevice stand-in: one pump thread per OutputStream drives the callback like PortAudio would."""
    class CallbackStop(Exception):
        pass

    def __init__(self):
        self.streams = []
        self.play = MagicMock()
        outer = self

        class OutputStream:
            def __init__(s, samplerate, channels, dtype, latency, callback, finished_callback):
                s.samplerate, s.callback, s.finished_callback = samplerate, callback, finished_callback
                s.out = []
                s.t = None
                s.closed = False
                outer.streams.append(s)

            def start(s):
                s.t = threading.Thread(target=s._pump, daemon=True)
                s.t.start()

            def _pump(s):
                while True:
                    buf = np.zeros((1024, 1), dtype=np.float32)
                    try:
                        s.callback(buf, 1024, None, None)
                    except outer.CallbackStop:
                        s.out.append(buf[:, 0].copy())
                        s.finished_callback()
                        return
                    s.out.append(buf[:, 0].copy())
                    time.sleep(0.0005)

            def abort(s):
                pass

            def close(s):
                s.closed = True
        self.OutputStream = OutputStream


def test_reply_plays_on_one_continuous_stream(monkeypatch):
    import sys
    fake_sd = _FakeSD()
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(voice, "_AUDIO_PHRASE_CACHE", {})
    monkeypatch.setattr(voice, "_get_output_sample_rate", lambda default=0: 22050)
    mv = MagicMock()
    mv.synthesize.return_value = [_Chunk(), _Chunk(), _Chunk()]
    sp = Speaker(enabled=True, cfg=Config())
    monkeypatch.setattr(sp, "_init_piper", lambda: mv)
    sp._say_piper("First sentence. Second sentence! Third sentence.")

    assert len(fake_sd.streams) == 1 and fake_sd.streams[0].closed
    fake_sd.play.assert_not_called()
    assert mv.synthesize.call_count == 1                       # whole reply in ONE synthesize call
    syn_cfg = mv.synthesize.call_args.kwargs.get("syn_config")
    if syn_cfg is not None:
        assert (syn_cfg.noise_scale, syn_cfg.noise_w_scale, syn_cfg.length_scale) == (0.333, 0.333, 1.05)
    out = np.concatenate(fake_sd.streams[0].out)
    # 3 sentences of 0.5 s + 2 pauses of 180 ms, joined without per-chunk fades or extra silence
    sig = np.flatnonzero(np.abs(out) > 1e-6)
    sig_len = sig[-1] - sig[0] + 1
    assert abs(sig_len - (3 * 11025 + 2 * int(22050 * voice.REPLY_PAUSE_S))) < 30
    zero_runs = np.flatnonzero(np.abs(out[sig[0]:sig[-1]]) < 1e-6)
    assert len(zero_runs) == 2 * int(22050 * voice.REPLY_PAUSE_S)
    assert out[sig[0]] < 0.01 and abs(out[sig[-1]]) < 0.05      # fades at the very start and end only
    assert np.isclose(out[5000], 0.44, atol=1e-3)               # untouched mid-sentence (0.5 * 0.88)
    assert not Voice.is_speaking


def test_phrase_cache_plays_without_synthesis(monkeypatch):
    import sys
    fake_sd = _FakeSD()
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(voice, "_AUDIO_PHRASE_CACHE", {"yes sir": (np.full(2000, 0.1, np.float32), 22050)})
    mv = MagicMock()
    sp = Speaker(enabled=True, cfg=Config())
    monkeypatch.setattr(sp, "_init_piper", lambda: mv)
    sp._say_piper("Yes, sir.")
    mv.synthesize.assert_not_called()
    assert len(fake_sd.streams) == 1


# ------------------------------------------------------------------ voice registry
@pytest.fixture
def vm_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(voice_models, "_app_dir", lambda: str(tmp_path / "app"))
    return tmp_path


def test_registry_lists_jarvis_not_installed(vm_env):
    voices = voice_models.available_voices()
    j = next(v for v in voices if v["id"] == "jarvis")
    assert set(j) == {"id", "name", "size_mb", "installed", "engine"}
    assert j["engine"] == "piper" and 55 < j["size_mb"] < 70 and j["installed"] is False
    assert voice_models.get_paths("jarvis") is None and voice_models.get_paths("nope") is None


def test_existing_install_counts_as_installed(vm_env):
    d = vm_env / "roaming" / "Jarvis" / "models" / "voice"
    d.mkdir(parents=True)
    entry = voice_models.REGISTRY["jarvis"]
    for fname, _u, size in entry["files"]:
        (d / fname).write_bytes(b"\0" * size)
    paths = voice_models.get_paths("jarvis")
    assert paths["model"].endswith("jarvis-medium.onnx") and paths["config"].endswith(".onnx.json")
    assert next(v for v in voice_models.available_voices() if v["id"] == "jarvis")["installed"] is True
    assert voice_models.remove("jarvis") is False          # legacy copy is left alone
    assert (d / "jarvis-medium.onnx").exists()


def test_download_resume_verify_and_remove(vm_env, monkeypatch):
    payload = {"a.onnx.json": b"{}" * 5, "a.onnx": bytes(range(256)) * 4}
    monkeypatch.setitem(voice_models.REGISTRY, "tiny", {
        "name": "Tiny", "engine": "piper", "model": "a.onnx", "legacy": (),
        "files": [(n, "http://x/" + n, len(b)) for n, b in payload.items()]})
    # a partial .part file from an interrupted download must be resumed with a Range request
    part_dir = vm_env / "local" / "JARVIS" / "voices" / "tiny"
    part_dir.mkdir(parents=True)
    (part_dir / "a.onnx.part").write_bytes(payload["a.onnx"][:100])
    ranges = []

    class Resp:
        def __init__(self, body, status):
            self.body, self.status_code = body, status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self.body

    def fake_get(url, headers=None, stream=True, timeout=0):
        name = url.rsplit("/", 1)[1]
        rng = (headers or {}).get("Range")
        ranges.append((name, rng))
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return Resp(payload[name][start:], 206)
        return Resp(payload[name], 200)

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    progress = []
    assert voice_models.download("tiny", progress_cb=lambda d, t: progress.append((d, t))) is True
    assert ("a.onnx", "bytes=100-") in ranges
    assert (part_dir / "a.onnx").read_bytes() == payload["a.onnx"]
    assert progress and progress[-1][0] == progress[-1][1]
    assert voice_models.get_paths("tiny")["model"] == str(part_dir / "a.onnx")
    assert voice_models.remove("tiny") is True and voice_models.get_paths("tiny") is None


def test_download_size_mismatch_fails(vm_env, monkeypatch):
    monkeypatch.setitem(voice_models.REGISTRY, "bad", {
        "name": "Bad", "engine": "piper", "model": "b.onnx", "legacy": (),
        "files": [("b.onnx", "http://x/b.onnx", 50)]})

    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"short"

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    assert voice_models.download("bad") is False
    assert voice_models.get_paths("bad") is None
    assert voice_models.download("unknown-voice") is False
