"""Microphone identity and selection: one entry per physical mic (deduped by name, MME 31-char truncation),
host API preference (WASAPI, MME, DirectSound, never WDM-KS), stall-proof opening and fallback, saved by name,
migration of the legacy raw index (config_version 6), and the no_audio failure of capture/calibrate.
No audio device is touched: PortAudio and streams are faked."""
import json
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import config as config_mod
import voice
from config import Config, load_config
from voice import Voice, WakeWordDetector
from voice_fakes import FakeStream, install_audio, make_detector, stop_and_join

FULL = "Microphone Array (Intel(R) Smart Sound Technology (Intel(R) SST))"
TRUNC = FULL[:31]                       # what MME reports
assert len(TRUNC) == 31


def _dev(idx, name, host, ch=2, rate=44100, **kw):
    d = {"index": idx, "name": name, "host_api": host, "channels": ch, "rate": rate}
    d.update(kw)
    return d


LAPTOP = [
    _dev(0, "Microsoft Sound Mapper - Input", "MME"),
    _dev(1, TRUNC, "MME"),
    _dev(2, "Line 1 (Virtual Audio Cable)", "MME"),
    _dev(6, "Primary Sound Capture Driver", "Windows DirectSound"),
    _dev(7, FULL, "Windows DirectSound"),
    _dev(8, "Line 1 (Virtual Audio Cable)", "Windows DirectSound"),
    _dev(14, "Line 1 (Virtual Audio Cable)", "Windows WASAPI", ch=2, rate=48000),
    _dev(15, FULL, "Windows WASAPI", ch=4, rate=48000),
    _dev(22, "Microphone (Realtek HD Audio Mic input)", "Windows WDM-KS"),
    _dev(40, FULL, "Windows WDM-KS", ch=2, rate=48000),
    _dev(50, "Loopback of Speakers", "Windows WASAPI", loopback=True),
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    voice.reset_variant_failures()
    monkeypatch.setattr(voice, "LAST_MIC_CHOICE", None)
    monkeypatch.setattr(voice, "MIC_VERIFY_TIMEOUT_S", 0.03)
    yield
    voice.reset_variant_failures()


# ------------------------------------------------------------------ dedupe and preference order
def test_names_match_handles_mme_truncation_but_not_lookalikes():
    assert voice._names_match(TRUNC, FULL)
    assert voice._names_match(FULL, TRUNC)
    assert voice._names_match("  microphone   ARRAY (x) ", "Microphone Array (x)")
    assert not voice._names_match("Microphone (Realtek HD Audio Mic input)", FULL)
    assert not voice._names_match("", "")
    assert not voice._names_match("Mic", "Microphone Array")      # short prefixes are not truncations


def test_one_entry_per_physical_mic_with_full_name():
    mics = voice.group_input_devices(LAPTOP)
    names = [m["name"] for m in mics]
    assert names.count(FULL) == 1 and TRUNC not in names           # MME stub merged; the untruncated name wins
    assert len(names) == len(set(voice._norm_mic_name(n) for n in names))
    assert [m["name"] for m in mics if not m["is_virtual"]] == [FULL]      # realtek mic only on WDM-KS: dropped


def test_host_api_preference_wasapi_then_mme_then_directsound_never_wdmks():
    m = voice.find_mic_by_name(voice.group_input_devices(LAPTOP), FULL)
    assert [(v["host_api"], v["index"]) for v in m["variants"]] == [
        ("Windows WASAPI", 15), ("MME", 1), ("Windows DirectSound", 7)]
    assert m["index"] == 15 and m["host_api"] == "Windows WASAPI"
    assert all("wdm" not in v["host_api"].lower() for g in voice.group_input_devices(LAPTOP) for v in g["variants"])


def test_without_wasapi_mme_then_directsound_and_truncated_lookup():
    devs = [_dev(7, FULL, "Windows DirectSound"), _dev(1, TRUNC, "MME")]
    mics = voice.group_input_devices(devs)
    assert [v["index"] for v in mics[0]["variants"]] == [1, 7]
    assert voice.find_mic_by_name(mics, TRUNC) is mics[0]          # a name saved from the MME stub still resolves
    assert voice.find_mic_by_name(mics, "") is None


def test_virtual_and_loopback_devices_are_hidden_from_the_settings_list():
    from ui.settings_window import mic_combo_entries
    mics = voice.group_input_devices(LAPTOP)
    entries = mic_combo_entries(mics, active_name=FULL)
    assert len(entries) == 1 and entries[0][1] == FULL and "[Active]" in entries[0][0]
    assert not any(m["name"].startswith("Loopback") for m in mics)
    assert [m["name"] for m in mics if m["is_virtual"]]            # still enumerated, flagged virtual


class _FakePA:
    def __init__(self, devs):
        self.devs = devs
        hosts = []
        for d in devs:
            if d["host_api"] not in hosts:
                hosts.append(d["host_api"])
        self.hosts = hosts

    def get_device_count(self):
        return max(d["index"] for d in self.devs) + 1

    def get_device_info_by_index(self, i):
        for d in self.devs:
            if d["index"] == i:
                return {"name": d["name"], "maxInputChannels": d["channels"], "defaultSampleRate": d["rate"],
                        "hostApi": self.hosts.index(d["host_api"]), "isLoopbackDevice": d.get("loopback", False)}
        return {"name": "output only", "maxInputChannels": 0, "hostApi": 0}

    def get_host_api_info_by_index(self, h):
        return {"name": self.hosts[h]}

    def get_default_input_device_info(self):
        return {"index": 0}


def test_list_microphones_enumerates_one_entry_per_mic(monkeypatch):
    monkeypatch.setattr(voice, "get_shared_pyaudio", lambda: _FakePA(LAPTOP))
    mics = voice.list_microphones()
    assert [m["name"] for m in mics if not m["is_virtual"]] == [FULL]
    assert len(mics) == len(set(voice._norm_mic_name(m["name"]) for m in mics))
    assert voice.mic_name_for_index(7) == FULL and voice.mic_name_for_index(1) == FULL
    assert voice.mic_name_for_index(3) is None                      # not an input device
    cfg = Config()
    cfg.mic_device_name = TRUNC
    assert voice.resolve_mic_index(cfg) == 15
    cfg.mic_device_name = ""
    assert voice.resolve_mic_index(cfg) is None


# ------------------------------------------------------------------ stall-proof opening and fallback
class _Stream:
    """Delivers a chunk immediately, or never (a stalled DirectSound-like device)."""

    def __init__(self, stalled):
        self.stalled = stalled
        self.closed = False
        self.frames = 0

    def get_read_available(self):
        return 0 if self.stalled else 1280

    def read(self, n, exception_on_overflow=False):
        assert not self.stalled, "read() on a stalled stream would block forever"
        self.frames += n
        return np.full(n, 50, dtype=np.int16).tobytes()

    def is_stopped(self):
        return False

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


def _fake_world(monkeypatch, devs, stalled_idx):
    opened = []
    streams = []

    def open_input(p, idx, chunk_samples=1280, opener=None):
        opened.append(idx)
        s = _Stream(idx in stalled_idx)
        streams.append(s)
        return s

    monkeypatch.setattr(voice, "get_shared_pyaudio", lambda: _FakePA(devs))
    monkeypatch.setattr(voice, "_open_input", open_input)
    monkeypatch.setattr(voice, "_shared_close", lambda s: s.close())
    return opened, streams


TWO_MICS = LAPTOP + [_dev(3, "USB Audio Device", "MME"), _dev(9, "USB Audio Device", "Windows DirectSound")]


def test_a_variant_counts_as_working_only_if_frames_arrive(monkeypatch):
    opened, streams = _fake_world(monkeypatch, LAPTOP, stalled_idx={15, 7})
    ok, detail, _ = voice.probe_16k_open(object(), 15)
    assert not ok and "no data" in detail and "0 frames" in detail and streams[0].closed
    monkeypatch.setattr(voice, "_open_input", lambda p, idx, chunk_samples=1280, opener=None: _Stream(False))
    ok, _, rms = voice.probe_16k_open(object(), 1)
    assert ok and rms == pytest.approx(50.0)        # silence would also be fine: frames are counted, not level


def test_configured_mic_uses_wasapi_when_it_delivers(monkeypatch):
    opened, _ = _fake_world(monkeypatch, LAPTOP, stalled_idx=set())
    cfg = Config()
    cfg.mic_device_name = FULL
    assert voice.find_best_microphone(cfg, require_16k=True) == (15, FULL)
    assert opened == [15]
    assert voice.LAST_MIC_CHOICE["host_api"] == "Windows WASAPI" and voice.LAST_MIC_CHOICE["reason"] == "configured"


def test_stalled_wasapi_falls_to_mme_then_stalled_mme_to_directsound(monkeypatch):
    cfg = Config()
    cfg.mic_device_name = FULL
    opened, _ = _fake_world(monkeypatch, LAPTOP, stalled_idx={15})
    assert voice.find_best_microphone(cfg, require_16k=True) == (1, FULL)
    assert opened == [15, 1]                         # next host API on the SAME mic
    voice.reset_variant_failures()
    opened, _ = _fake_world(monkeypatch, LAPTOP, stalled_idx={15, 1})
    assert voice.find_best_microphone(cfg, require_16k=True) == (7, FULL)
    assert opened == [15, 1, 7]


def test_every_host_api_of_the_configured_mic_stalled_falls_back_to_auto(monkeypatch):
    cfg = Config()
    cfg.mic_device_name = FULL
    opened, _ = _fake_world(monkeypatch, TWO_MICS, stalled_idx={15, 1, 7})
    idx, name = voice.find_best_microphone(cfg, require_16k=True)
    assert name == "USB Audio Device" and idx == 3
    assert opened[:3] == [15, 1, 7] and voice.LAST_MIC_CHOICE["reason"] == "ranked"


def test_stalled_variant_is_not_reopened_first_and_is_skipped_after_two_failures(monkeypatch):
    cfg = Config()
    cfg.mic_device_name = FULL
    opened, _ = _fake_world(monkeypatch, LAPTOP, stalled_idx=set())
    assert voice.find_best_microphone(cfg, require_16k=True) == (15, FULL)
    # the running WASAPI stream then stalls (WakeWordDetector._read_chunk marks it) -> reconnect_stream asks again
    assert voice.note_variant_failure(FULL, "Windows WASAPI") == 1
    opened.clear()
    assert voice.find_best_microphone(cfg, require_16k=True) == (1, FULL)
    assert opened == [1]                              # MME first; the variant that just stalled is not retried first
    voice.note_variant_failure(FULL, "Windows WASAPI")
    opened.clear()
    voice.find_best_microphone(cfg, require_16k=True)
    assert 15 not in opened                           # failed twice: skipped
    voice.reset_variant_failures()                    # a device change gives it another chance
    assert voice.find_best_microphone(cfg, require_16k=True)[0] == 15


def test_stall_marks_survive_name_truncation_and_success_clears_them(monkeypatch):
    voice.note_variant_failure(TRUNC, "MME")
    assert voice._variant_fails(FULL, "MME") == 1     # same mic whichever name form reported it
    voice._note_variant_ok(FULL, "MME")
    assert voice._variant_fails(TRUNC, "MME") == 0


def test_switch_is_logged_and_notified_exactly_once(monkeypatch):
    wd = make_detector(monkeypatch)
    told = []
    wd.on_mic_switch = told.append
    monkeypatch.setattr(voice, "LAST_MIC_CHOICE", {"index": 15, "name": FULL, "host_api": "Windows WASAPI"})
    wd._adopt_choice(15)
    monkeypatch.setattr(voice, "LAST_MIC_CHOICE", {"index": 1, "name": FULL, "host_api": "MME"})
    wd._announce_switch(1, "stall recovery")
    wd._announce_switch(1, "stall recovery")          # same variant again: no second notice
    assert told == [FULL]


def test_reconnect_stream_moves_off_the_stalled_variant(monkeypatch):
    cfg = Config()
    cfg.mic_device_name = FULL
    opened, _ = _fake_world(monkeypatch, LAPTOP, stalled_idx=set())
    monkeypatch.setattr(voice, "reset_shared_pyaudio", lambda: None)
    monkeypatch.setattr(voice, "_log_mic_open", lambda *a, **k: None)
    wd = make_detector(monkeypatch, cfg)
    told = []
    wd.on_mic_switch = told.append
    voice.find_best_microphone(cfg, require_16k=True)
    wd._adopt_choice(15)
    wd.device_index = 15
    voice.note_variant_failure(FULL, "Windows WASAPI")        # what the stall detection does
    monkeypatch.setattr(wd.stop, "wait", lambda t=None: False)
    stream = wd.reconnect_stream()
    assert stream is not None and wd.device_index == 1 and told == [FULL]
    assert voice.LAST_MIC_CHOICE["host_api"] == "MME"


# ------------------------------------------------------------------ the 16 kHz view of a native-rate WASAPI stream
def test_resampled_stream_is_16k_mono_chunked_and_continuous():
    class Raw:
        def __init__(self):
            self.t = 0

        def read(self, n, exception_on_overflow=False):
            i = np.arange(self.t, self.t + n)
            self.t += n
            tone = 8000 * np.sin(2 * np.pi * 1000.0 * i / 48000.0)
            return np.repeat(tone[:, None], 4, axis=1).astype(np.int16).tobytes()      # 4 identical channels

        def get_read_available(self):
            return 3840

    rs = voice._ResampledStream(Raw(), 48000, 4, 1280)
    assert rs.get_read_available() >= 1280
    out = np.concatenate([np.frombuffer(rs.read(1280), dtype=np.int16) for _ in range(6)]).astype(float)
    assert len(out) == 6 * 1280
    body = out[400:]
    assert 5400 < np.sqrt((body ** 2).mean()) * np.sqrt(2) < 8400        # amplitude kept (~8000)
    spec = np.abs(np.fft.rfft(body * np.hanning(len(body))))
    assert abs(np.argmax(spec) * 16000.0 / len(body) - 1000.0) < 25      # still a 1 kHz tone at 16 kHz
    assert np.abs(np.diff(out[400:])).max() < 6500                       # no clicks at the chunk seams
    odd = voice._ResampledStream(Raw(), 44100, 4, 1280)
    assert all(len(odd.read(1280)) == 2560 for _ in range(5))             # non-integer ratio still yields full chunks


# ------------------------------------------------------------------ saved by name
def test_settings_dropdown_saves_the_name_not_an_index():
    from ui.settings_window import SettingsWindow
    saved = []
    names = ["", FULL]
    stub = SimpleNamespace(mic_combo=SimpleNamespace(itemData=lambda i: names[i]), cfg=Config(),
                           _save=lambda k, v: saved.append((k, v)))
    SettingsWindow._on_mic_selected(stub, 1)
    assert saved == [("mic_device_name", FULL)] and stub.cfg.mic_device_name == FULL
    SettingsWindow._on_mic_selected(stub, 0)                              # Auto-detect
    assert saved[-1] == ("mic_device_name", "") and stub.cfg.mic_device_name == ""
    assert all(k != "mic_device_index" for k, _ in saved)


def test_mic_name_round_trips_through_the_config_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "jarvis_config.json")
        cfg = Config()
        cfg.mic_device_name = FULL
        assert config_mod.save_config(cfg, path=path)
        assert load_config(path).mic_device_name == FULL
        assert Config().mic_device_name == "" and Config().config_version == 6


# ------------------------------------------------------------------ config migration 5 -> 6
def _load(d, resolver=None, monkeypatch=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "jarvis_config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        if resolver is not None:
            monkeypatch.setattr(config_mod, "_resolve_mic_name", resolver)
        cfg = load_config(path)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    return cfg, saved


def test_migration_maps_index_to_name_and_clears_the_index(monkeypatch):
    seen = []
    cfg, saved = _load({"config_version": 5, "mic_device_index": 7},
                       lambda idx: seen.append(idx) or FULL, monkeypatch)
    assert seen == [7]
    assert cfg.mic_device_name == FULL and cfg.mic_device_index is None
    assert saved["mic_device_name"] == FULL and saved["mic_device_index"] is None and saved["config_version"] == 6


def test_migration_unresolvable_index_falls_back_to_auto(monkeypatch):
    cfg, saved = _load({"config_version": 5, "mic_device_index": 99}, lambda idx: None, monkeypatch)
    assert cfg.mic_device_name == "" and cfg.mic_device_index is None
    assert saved["mic_device_index"] is None and saved["config_version"] == 6


def test_migration_without_an_index_does_not_query_audio(monkeypatch):
    def boom(idx):
        raise AssertionError("PortAudio must not be queried when no index is configured")
    cfg, saved = _load({"config_version": 5}, boom, monkeypatch)
    assert cfg.mic_device_name == "" and saved["config_version"] == 6


def test_migration_runs_once(monkeypatch):
    cfg, _ = _load({"config_version": 6, "mic_device_index": 4, "mic_device_name": ""},
                   lambda idx: pytest.fail("v6 configs are not migrated again"), monkeypatch)
    assert cfg.mic_device_index == 4


def test_migration_keeps_an_existing_name(monkeypatch):
    cfg, _ = _load({"config_version": 5, "mic_device_index": 7, "mic_device_name": "My Mic"},
                   lambda idx: FULL, monkeypatch)
    assert cfg.mic_device_name == "My Mic" and cfg.mic_device_index is None


def test_resolver_failure_means_auto_detect(monkeypatch):
    monkeypatch.setattr(voice, "mic_name_for_index", lambda idx: (_ for _ in ()).throw(OSError("no portaudio")))
    cfg, _ = _load({"config_version": 5, "mic_device_index": 7})
    assert cfg.mic_device_name == "" and cfg.mic_device_index is None


# ------------------------------------------------------------------ no_audio in capture / calibrate
class _NoDataStream(FakeStream):
    def get_read_available(self):
        return 0


@pytest.fixture
def silent_mic(monkeypatch):
    audio = install_audio(monkeypatch)
    audio.open = lambda p, **kw: _NoDataStream()
    monkeypatch.setattr(WakeWordDetector, "STALL_TIMEOUT_S", 0.2)
    monkeypatch.setattr(WakeWordDetector, "CAPTURE_SPEAKING_WAIT_S", 0.5)
    monkeypatch.setattr(Voice, "CAPTURE_NO_AUDIO_S", 0.5)
    prev = Voice.is_speaking
    Voice.is_speaking = False
    wd = make_detector(monkeypatch, Config())
    v = Voice(Config(), MagicMock(), MagicMock())
    v.wake_detector = wd
    wd.start()
    yield v, wd
    Voice.is_speaking = prev
    stop_and_join(wd)


def test_capture_answer_without_audio_fails_promptly_with_no_audio(silent_mic):
    v, wd = silent_mic
    v._transcribe_blocking = MagicMock()
    t0 = time.monotonic()
    assert v.capture_answer(timeout=8.0) == ""
    assert v.last_capture_reason == "no_audio"
    assert time.monotonic() - t0 < 4.0                 # not the 8 s timeout (+15 s grace)
    v._transcribe_blocking.assert_not_called()


def test_calibrate_without_audio_returns_ok_false_reason_no_audio(silent_mic):
    v, wd = silent_mic
    t0 = time.monotonic()
    out = v.calibrate_from_sample("hello there", timeout=12.0)
    assert out["ok"] is False and out["reason"] == "no_audio" and out["transcript"] == ""
    assert time.monotonic() - t0 < 4.0


def test_silence_that_does_arrive_is_not_no_audio(monkeypatch):
    audio = install_audio(monkeypatch)
    monkeypatch.setattr(Voice, "CAPTURE_NO_AUDIO_S", 0.5)
    prev = Voice.is_speaking
    Voice.is_speaking = False
    wd = make_detector(monkeypatch, Config())
    v = Voice(Config(), MagicMock(), MagicMock())
    v.wake_detector = wd
    wd.start()
    try:
        assert v.capture_answer(timeout=0.3) == ""
        assert v.last_capture_reason == "no_speech"
        assert v.calibrate_from_sample("hello", timeout=0.3).get("reason") is None
    finally:
        Voice.is_speaking = prev
        stop_and_join(wd)


def test_onboarding_footnote_text_for_no_audio():
    from ui.onboarding_window import NO_AUDIO_FOOTNOTE
    assert NO_AUDIO_FOOTNOTE == ("I can't hear your microphone. Pick it in Settings > Voice > Input Microphone, "
                                 "or skip this step.")


def test_mojibake_device_names_are_repaired():
    assert voice._clean_device_name("Microphone Array (Intel\u00c2\u00ae SST)") == "Microphone Array (Intel\u00ae SST)"
    assert voice._clean_device_name("Mikrofon \u00c3\u00a4") == "Mikrofon \u00e4"
    assert voice._clean_device_name("Plain Mic") == "Plain Mic"
    assert voice._clean_device_name("\u00c2 not utf8 \u0152") == "\u00c2 not utf8 \u0152"
