"""
Voice replay harness: real Piper speech + realistic fan noise through the real
openWakeWord/Silero VAD in voice.WakeWordDetector.record_utterance_with_vad.

Regression tests for formerly-xfailed defects (BUG-V1: energy gate far below ambient noise so noise
counts as speech; BUG-V1b: cfg.mic_energy_threshold never reaches the VAD).
"""
import os
import threading
import time

import numpy as np
import pytest

import voice_replay_util as U

PHRASES = ["Jarvis, play.", "Jarvis, next track."]


@pytest.fixture(scope="session")
def speech_clips():
    if not (os.path.exists(U.MODEL) and os.path.exists(U.MODEL_JSON)):
        pytest.skip(f"Piper model not found at {U.MODEL}")
    try:
        from piper import PiperVoice
        voice_obj = PiperVoice.load(U.MODEL, config_path=U.MODEL_JSON)  # no phrase-cache warmup
    except Exception as exc:
        pytest.skip(f"Piper unavailable: {exc}")
    return {p: U.synth_16k(voice_obj, p) for p in PHRASES}


@pytest.fixture
def detector():
    import voice
    from config import Config
    voice.Voice.is_speaking = False
    d = voice.WakeWordDetector(cfg=Config(), stop_event=threading.Event())
    yield d
    d.stop.set()


def _speech_flags(det, samples):
    flags = []
    for i in range(0, len(samples) - U.CHUNK + 1, U.CHUNK):
        c = samples[i:i + U.CHUNK]
        flags.append(bool(det.vad.is_speech(c, U.rms(c))))
    return flags


@pytest.mark.parametrize("kind", ["pink", "white"])
def test_noise_only_does_not_start_recording(detector, kind):
    noise = U.make_noise(kind, 10.0)
    flags = _speech_flags(detector, noise)
    assert sum(flags) / len(flags) < 0.05, f"{sum(flags)}/{len(flags)} noise chunks flagged as speech"
    stream = U.FakeStream(noise, on_exhausted="empty")
    assert detector.record_utterance_with_vad(stream=stream, timeout_no_speech=1.0) is None


@pytest.mark.parametrize("phrase", PHRASES)
def test_command_endpoints_on_silence(detector, speech_clips, phrase):
    speech = speech_clips[phrase]
    mix, start, slen = U.compose(speech, "pink")
    stream = U.FakeStream(mix, start_sample=int(1.8 * U.SR), on_exhausted="empty")
    t0 = time.time()
    out = detector.record_utterance_with_vad(stream=stream, timeout_no_speech=5.0)
    assert time.time() - t0 < 10
    assert out is not None
    dur = len(out) / 2 / U.SR
    # Bound = speech + 1.5 s: the endpoint needs 0.70 s of silence, and Silero's probability decay on
    # the TTS tail keeps chunks "speech" for a further ~0.1-0.3 s, plus chunk granularity (80 ms).
    # The strict "is NOT ~8 s (max-duration runaway)" check below is unchanged.
    assert dur <= slen / U.SR + 1.5, f"returned {dur:.2f}s for {slen / U.SR:.2f}s of speech"
    assert abs(dur - 8.0) > 0.5
    assert dur < 4.0, f"returned {dur:.2f}s, endpoint did not fire on silence"


@pytest.mark.parametrize("phrase", PHRASES)
def test_command_audio_contains_speech(detector, speech_clips, phrase):
    speech = speech_clips[phrase]
    mix, start, slen = U.compose(speech, "pink")
    from_s = int(1.8 * U.SR)
    stream = U.FakeStream(mix, start_sample=from_s, on_exhausted="empty")
    out = detector.record_utterance_with_vad(stream=stream, timeout_no_speech=5.0)
    assert out is not None
    pcm = np.frombuffer(out, dtype=np.int16)
    off = start - from_s
    assert len(pcm) >= off + slen, "returned audio ends before the speech does"
    # the recorded speech region must be a faithful copy of the source and louder than the lead-in noise
    assert np.array_equal(pcm[off:off + slen], mix[start:start + slen])
    assert U.rms(pcm[off:off + slen]) > 1.5 * U.rms(pcm[:off])


def test_mic_energy_threshold_applies_live(detector):
    detector.cfg.mic_energy_threshold = 777
    if hasattr(detector, "apply_config"):
        detector.apply_config()
    follows = detector.vad.energy_threshold == 777
    if hasattr(detector.vad, "gate"):
        try:
            follows = follows or bool(detector.vad.gate() == 777)
        except TypeError:
            pass
    assert follows, f"vad.energy_threshold={detector.vad.energy_threshold}, cfg=777"
