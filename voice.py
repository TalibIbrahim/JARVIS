"""
Voice command subsystem for J.A.R.V.I.S.
Handles:
- Speech recognition via Google Speech API (with local audio capture and decoupling).
- Intelligent microphone selection (filters out dead virtual cables and auto-detects active hardware microphones).
- Live audio input level probing for UI volume meters.
- Standalone microphone diagnostic test tool.
- Non-blocking text-to-speech feedback via pyttsx3.
"""
import collections
import math
import os
import queue
import re
import sys
import threading
import time
import numpy as np

from commands import run_command, handle_island_voice_command
intercept_island_voice = handle_island_voice_command
from voice_profile import get_voice_profile
from ai import (
    get_api_key,
    GroqClient,
    AuthenticationError,
    RateLimitError,
    OfflineError,
    TimeoutError,
    AIError,
)
from chime import (
    play_chime,
    play_wake_chime,
    set_chime_muted,
    is_chime_muted,
    set_chime_volume,
    get_chime_volume,
    set_audio_lock,
)


import jarvis_log
from jarvis_log import redact

_vlog = jarvis_log.get_logger("voice")

_PORTAUDIO_LOCK = jarvis_log.InstrumentedRLock("portaudio", warn_wait_ms=50, warn_hold_ms=250)
set_audio_lock(_PORTAUDIO_LOCK)


def _log_mic_open(p, idx, rate, channels, fpb, result, where):
    """Log a PortAudio input-open result. Cheap, never raises."""
    try:
        name = "?"
        host = "?"
        try:
            info = p.get_default_input_device_info() if idx is None else p.get_device_info_by_index(idx)
            name = info.get("name", "?")
            host = p.get_host_api_info_by_index(info["hostApi"])["name"]
        except Exception:
            pass
        lvl = _vlog.info if result == "OK" else _vlog.warning
        lvl("mic open (%s): idx=%s name=%r hostApi=%r rate=%s ch=%s fpb=%s result=%s",
            where, idx, name, host, rate, channels, fpb, result)
    except Exception:
        pass
_SHARED_PYAUDIO = None

# Sensitivity mapping for openWakeWord detector
SENSITIVITY_THRESHOLDS = {
    "low": 0.70,
    "balanced": 0.50,
    "high": 0.35,
}


def parse_sensitivity(value, default=0.50) -> float:
    """Maps sensitivity setting ('low', 'balanced', 'high' or numeric float) to threshold."""
    if isinstance(value, (int, float)):
        return float(np.clip(value, 0.05, 0.99))
    if isinstance(value, str):
        v = value.strip().lower()
        if v in SENSITIVITY_THRESHOLDS:
            return SENSITIVITY_THRESHOLDS[v]
        try:
            return float(np.clip(float(v), 0.05, 0.99))
        except ValueError:
            pass
    return default


class WakeWordGuard:
    """
    False-trigger guard for openWakeWord detector:
    1. Requires score above sensitivity threshold (or 2 consecutive frames).
    2. Single frame score >= high confidence (min(0.85, threshold + 0.15)) also triggers.
    3. Suppresses wake detection while assistant is speaking (Voice.is_speaking / TTS playing).
    4. Post-trigger debounce window to prevent tail re-triggering.
    """
    def __init__(self, threshold=0.50, debounce_seconds=1.2, require_consecutive=False):
        self.threshold = float(threshold)
        self.debounce_seconds = float(debounce_seconds)
        self.require_consecutive = require_consecutive
        self.consecutive = 0
        self.last_trigger_time = 0.0

    def evaluate(self, score: float, is_speaking: bool = False, now: float = None) -> bool:
        if now is None:
            now = time.time()

        # Suppress wake word while assistant is speaking or within debounce window
        if is_speaking or (now - self.last_trigger_time) < self.debounce_seconds:
            self.consecutive = 0
            return False

        if score >= self.threshold:
            self.consecutive += 1
            high_thresh = min(0.85, self.threshold + 0.15)
            if self.consecutive >= 2 or score >= high_thresh or not self.require_consecutive:
                self.consecutive = 0
                self.last_trigger_time = now
                return True
        else:
            self.consecutive = 0
        return False


# Whisper hallucinations produced on noise / silence. Applied ONLY to follow-up-window speech
# (utterances with no wake word); a normalized transcript equal to one of these is dropped.
WHISPER_NOISE_PHRASES = frozenset({
    "bye", "bye bye", "goodbye", "thank you", "thanks", "thank you for watching",
    "thanks for watching", "you", "okay", "ok", "so", "uh", "um", "hmm", "i'll explain",
    "subtitles by the amara.org community", "",
})

def _normalize_noise_text(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace (used to match WHISPER_NOISE_PHRASES)."""
    return " ".join(re.sub(r"[^\w\s]", "", str(text).lower()).split())


_WHISPER_NOISE_NORMALIZED = frozenset(_normalize_noise_text(p) for p in WHISPER_NOISE_PHRASES)

# A recording is dispatched only if it holds at least this many speech chunks (3 x 80 ms = 240 ms).
MIN_SPEECH_CHUNKS = 3


class VADDetector:
    """
    Voice Activity Detection: Silero VAD (ONNX) AND an adaptive RMS gate.

    With Silero available a chunk is speech only if prob >= vad_threshold AND rms >= gate().
    The energy-only rule (rms >= gate) is used ONLY when Silero failed to load or predict raised
    (a WARNING is logged once).

    gate = max(energy_floor, NOISE_RATIO * noise_floor)
      energy_floor = cfg.mic_energy_threshold (read live; the Settings "Mic sensitivity" slider)
      noise_floor  = rolling 30th percentile of RMS over the last ~5 s of chunks Silero scored < 0.1
    """
    NOISE_RATIO = 1.5
    NOISE_PROB = 0.1
    NOISE_WINDOW_S = 5.0
    NOISE_PERCENTILE = 30.0
    DEFAULT_ENERGY_FLOOR = 300.0

    def __init__(self, energy_threshold=None, vad_threshold=0.35, cfg=None):
        self.cfg = cfg
        self._energy_override = energy_threshold
        self.vad_threshold = vad_threshold
        self._vad = None
        self._warned_fallback = False
        self._noise = collections.deque()  # (monotonic_t, rms) of Silero-confirmed non-speech chunks
        self.noise_floor = 0.0
        self.last_prob = None
        self.n_total = 0
        self.n_speech = 0
        self._init_vad()

    def _init_vad(self):
        try:
            from openwakeword.vad import VAD
            self._vad = VAD()
        except Exception as exc:
            self._vad = None
            self._warn_fallback(f"Silero failed to load: {exc!r}")

    def _warn_fallback(self, why):
        if not self._warned_fallback:
            self._warned_fallback = True
            _vlog.warning("VAD: %s; falling back to the energy-only rule (rms >= gate %.0f)", why, self.gate())

    @property
    def energy_threshold(self):
        """Energy floor. Reflects cfg.mic_energy_threshold live (not a hard-coded constant)."""
        if self._energy_override is not None:
            return self._energy_override
        return float(getattr(self.cfg, "mic_energy_threshold", self.DEFAULT_ENERGY_FLOOR))

    @energy_threshold.setter
    def energy_threshold(self, value):
        self._energy_override = value

    def gate(self) -> float:
        return max(float(self.energy_threshold), self.NOISE_RATIO * self.noise_floor)

    def _note_noise(self, rms: float):
        now = time.monotonic()
        self._noise.append((now, rms))
        cutoff = now - self.NOISE_WINDOW_S
        while self._noise and self._noise[0][0] < cutoff:
            self._noise.popleft()
        self.noise_floor = float(np.percentile([r for _, r in self._noise], self.NOISE_PERCENTILE))

    def is_speech(self, chunk: np.ndarray, rms: float) -> bool:
        prob = None
        if self._vad is not None:
            try:
                prob = float(self._vad.predict(chunk))
            except Exception as exc:
                self._warn_fallback(f"Silero predict raised: {exc!r}")
        self.last_prob = prob

        gate = self.gate()  # decided from history before this chunk is folded in
        if prob is None:
            result = rms >= gate
        else:
            result = prob >= self.vad_threshold and rms >= gate
            if prob < self.NOISE_PROB:
                self._note_noise(rms)

        self.n_total += 1
        if result:
            self.n_speech += 1
        return result

    def take_stats(self):
        """Returns (chunks_evaluated, chunks_flagged_speech) since the last call and resets both."""
        out = (self.n_total, self.n_speech)
        self.n_total = self.n_speech = 0
        return out


def get_shared_pyaudio():
    """Returns a process-wide PyAudio instance protected by _PORTAUDIO_LOCK."""
    global _SHARED_PYAUDIO
    with _PORTAUDIO_LOCK:
        if _SHARED_PYAUDIO is None:
            import pyaudio
            _SHARED_PYAUDIO = pyaudio.PyAudio()
        return _SHARED_PYAUDIO


_SHARED_OPEN_INPUT_STREAMS = 0  # input streams currently open on the SHARED instance (guarded by _PORTAUDIO_LOCK)


def _shared_open(p, **kwargs):
    """p.open(...) on the shared instance, counting the open input stream."""
    global _SHARED_OPEN_INPUT_STREAMS
    with _PORTAUDIO_LOCK:
        stream = p.open(**kwargs)
        _SHARED_OPEN_INPUT_STREAMS += 1
        return stream


def _shared_close(stream):
    """Closes a stream opened with _shared_open and decrements the counter (even if close raises)."""
    global _SHARED_OPEN_INPUT_STREAMS
    with _PORTAUDIO_LOCK:
        try:
            stream.close()
        finally:
            _SHARED_OPEN_INPUT_STREAMS = max(0, _SHARED_OPEN_INPUT_STREAMS - 1)


def reset_shared_pyaudio():
    """Terminates and resets the shared PyAudio instance to detect hotplugged devices.
    Skipped (WARNING) while any input stream is open on the shared instance."""
    global _SHARED_PYAUDIO
    with _PORTAUDIO_LOCK:
        if _SHARED_OPEN_INPUT_STREAMS > 0:
            _vlog.warning("reset_shared_pyaudio: %d input stream(s) still open on the shared instance; "
                          "skipping terminate", _SHARED_OPEN_INPUT_STREAMS)
            return
        if _SHARED_PYAUDIO is not None:
            try:
                _SHARED_PYAUDIO.terminate()
            except Exception:
                pass
            _SHARED_PYAUDIO = None


def list_microphones(probe_rms=False):
    """
    Enumerates all system audio input devices safely using the shared PyAudio instance.
    Returns list of dicts:
        [{'index': int, 'name': str, 'channels': int, 'working': bool, 'rms': float, 'is_virtual': bool}]
    """
    mics = []
    _n_probe_fail = 0
    with _PORTAUDIO_LOCK:
        try:
            import pyaudio
            p = get_shared_pyaudio()
            dev_count = p.get_device_count()
            for i in range(dev_count):
                try:
                    info = p.get_device_info_by_index(i)
                    max_ch = info.get("maxInputChannels", 0)
                    if max_ch <= 0:
                        continue

                    name = info.get("name", f"Microphone {i}")
                    rate = int(info.get("defaultSampleRate", 44100))
                    ch = min(max_ch, 2)
                    is_virtual = any(k in name.lower() for k in ("virtual", "cable", "mapper", "stereo mix", "wave out"))

                    working = True
                    rms = 0.0
                    if probe_rms:
                        try:
                            stream = _shared_open(p, 
                                format=pyaudio.paInt16,
                                channels=ch,
                                rate=rate,
                                input=True,
                                input_device_index=i,
                                frames_per_buffer=1024
                            )
                            data = stream.read(1024, exception_on_overflow=False)
                            stream.stop_stream()
                            _shared_close(stream)
                            arr = np.frombuffer(data, dtype=np.int16)
                            rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                            working = True
                        except Exception:
                            working = False
                            rms = 0.0
                            _n_probe_fail += 1

                    try:
                        host_api = p.get_host_api_info_by_index(info["hostApi"])["name"]
                    except Exception:
                        host_api = "?"
                    mics.append({
                        "index": i,
                        "name": name,
                        "channels": max_ch,
                        "working": working,
                        "rms": rms,
                        "is_virtual": is_virtual,
                        "host_api": host_api,
                    })
                except Exception:
                    pass
        except Exception as exc:
            print(f"[voice] error enumerating microphones: {exc}")
    try:
        if probe_rms:
            _vlog.info("list_microphones probe: inputs=%d probe_failed=%d working=%s", len(mics), _n_probe_fail,
                       [(m["index"], round(m["rms"], 1)) for m in mics if m["working"]])
    except Exception:
        pass
    return mics


def _rank_mic_by_name(m):
    """Name-keyword ranking shared by the legacy and the 16 kHz-tested selection."""
    score = 0.0
    name = m["name"].lower()
    if "array" in name:
        score += 40.0
    if "microphone" in name or "mic" in name:
        score += 30.0
    if "intel" in name or "realtek" in name:
        score += 20.0
    return score


def _host_priority_16k(host_api):
    """MME first, then DirectSound, then anything else. WASAPI and WDM-KS are excluded elsewhere."""
    h = str(host_api).lower()
    if "mme" in h:
        return 0
    if "directsound" in h:
        return 1
    return 2


def _host_excluded_16k(host_api):
    """WASAPI rejects 16 kHz shared-mode opens (-9997); WDM-KS fails with -9999."""
    h = str(host_api).lower()
    return "wasapi" in h or "wdm" in h


def probe_16k_open(p, idx, chunk_samples=1280):
    """
    Opens device idx at 16 kHz mono and reads one chunk, exactly like the Settings test does.
    Lock is held only for open and close, never during the read.
    Returns (ok, detail, rms).
    """
    import pyaudio
    stream = None
    try:
        with _PORTAUDIO_LOCK:
            stream = _shared_open(p, format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                            input_device_index=idx, frames_per_buffer=chunk_samples)
        data = stream.read(chunk_samples, exception_on_overflow=False)
        if not data:
            return False, "read returned no data", 0.0
        arr = np.frombuffer(data, dtype=np.int16)
        return True, "OK", float(np.sqrt(np.mean(arr.astype(float) ** 2)))
    except Exception as exc:
        return False, repr(exc), 0.0
    finally:
        if stream is not None:
            try:
                with _PORTAUDIO_LOCK:
                    stream.stop_stream()
                    _shared_close(stream)
            except Exception as close_exc:
                _vlog.warning("probe_16k_open: closing probe stream idx=%s failed: %r", idx, close_exc)


def _find_best_microphone_16k(cfg):
    """
    Pipeline mic selection. Every candidate must pass a 16 kHz mono open + one-chunk read.
    Order: configured device, the Windows default input (if not virtual), then ranking restricted
    to tested devices on MME, then DirectSound (never WASAPI or WDM-KS). Every result is logged.
    """
    mics = list_microphones()
    if not mics:
        _vlog.warning("find_best_microphone[16k]: no input devices")
        return None, "Default System Microphone"
    p = get_shared_pyaudio()
    cfg_idx = getattr(cfg, "mic_device_index", None) if cfg else None
    tested = {}

    def _test(m):
        if m["index"] not in tested:
            ok, detail, rms = probe_16k_open(p, m["index"])
            tested[m["index"]] = ok
            (_vlog.info if ok else _vlog.warning)(
                "find_best_microphone[16k]: candidate idx=%s hostApi=%s name=%r virtual=%s -> %s%s",
                m["index"], m["host_api"], m["name"], m["is_virtual"], "OK" if ok else "FAIL",
                f" rms={rms:.0f}" if ok else f" ({detail})")
        return tested[m["index"]]

    # 1. Explicit user choice (honored if it opens at 16 kHz)
    if cfg_idx is not None:
        for m in mics:
            if m["index"] == cfg_idx and not m["is_virtual"] and _test(m):
                _vlog.info("find_best_microphone[16k]: reason=configured idx=%s name=%r", m["index"], m["name"])
                return m["index"], m["name"]
        _vlog.warning("find_best_microphone[16k]: configured idx=%s unusable at 16 kHz; ranking instead", cfg_idx)

    # 2. The Windows default input device (what the Settings test uses)
    else:
        dflt = None
        try:
            dflt = p.get_default_input_device_info()
        except Exception as exc:
            _vlog.warning("find_best_microphone[16k]: no default input device: %r", exc)
        if dflt is not None:
            for m in mics:
                if m["index"] == dflt.get("index"):
                    if m["is_virtual"]:
                        _vlog.info("find_best_microphone[16k]: default input idx=%s %r is virtual; skipped",
                                   m["index"], m["name"])
                    elif _test(m):
                        _vlog.info("find_best_microphone[16k]: reason=windows_default idx=%s name=%r",
                                   m["index"], m["name"])
                        return m["index"], m["name"]
                    break

    # 3. Ranking among devices that pass the 16 kHz open test (MME, then DirectSound)
    for allow_virtual in (False, True):
        cands = [m for m in mics if m["is_virtual"] == allow_virtual and not _host_excluded_16k(m["host_api"])]
        cands.sort(key=lambda m: (_host_priority_16k(m["host_api"]), -_rank_mic_by_name(m)))
        for m in cands:
            if _test(m):
                _vlog.info("find_best_microphone[16k]: reason=ranked idx=%s hostApi=%s name=%r",
                           m["index"], m["host_api"], m["name"])
                return m["index"], m["name"]
    excluded = [(m["index"], m["host_api"]) for m in mics if _host_excluded_16k(m["host_api"])]
    _vlog.warning("find_best_microphone[16k]: no candidate opened at 16 kHz (excluded WASAPI/WDM-KS: %s)", excluded)
    return None, mics[0]["name"]


def find_best_microphone(cfg=None, probe_working=False, require_16k=False):
    """
    Finds the most suitable microphone device index and name.
    require_16k=True (pipeline use, not for the GUI thread): only devices that open at 16 kHz mono
    and deliver a chunk are considered; prefers cfg index, the Windows default input, then MME/DirectSound.
    Otherwise:
    1. If cfg.mic_device_index is explicitly specified, verifies and uses it.
    2. Otherwise, auto-probes all devices, filtering out silent virtual audio cables
       and selecting the physical microphone with active audio signal.
    """
    if require_16k:
        return _find_best_microphone_16k(cfg)
    cfg_idx = getattr(cfg, "mic_device_index", None) if cfg else None
    mics = list_microphones(probe_rms=probe_working)
    if not mics:
        _vlog.warning("find_best_microphone: no input devices (probe_working=%s)", probe_working)
        return None, "Default System Microphone"

    # 1. Explicitly requested device in config (if physical and working)
    if cfg_idx is not None:
        for m in mics:
            if m["index"] == cfg_idx and m["working"] and not m["is_virtual"]:
                _vlog.info("find_best_microphone: reason=configured idx=%s name=%r probe_working=%s",
                           m["index"], m["name"], probe_working)
                return m["index"], m["name"]

    # 2. Filter working non-virtual devices
    physical_working = [m for m in mics if m["working"] and not m["is_virtual"]]
    
    # Priority ranking for physical microphone keywords
    def rank_mic(m):
        score = 0.0
        name = m["name"].lower()
        if "array" in name:
            score += 40.0
        if "microphone" in name or "mic" in name:
            score += 30.0
        if "intel" in name or "realtek" in name:
            score += 20.0
        if m["rms"] > 5.0:
            score += 50.0 + min(m["rms"] / 100.0, 30.0)
        return score

    if physical_working:
        physical_working.sort(key=rank_mic, reverse=True)
        best = physical_working[0]
        try:
            top = [(m["index"], m["name"][:30], round(rank_mic(m), 1)) for m in physical_working[:3]]
            _vlog.info("find_best_microphone: reason=ranked idx=%s name=%r probe_working=%s cfg_idx=%s top3=%s",
                       best["index"], best["name"], probe_working, cfg_idx, top)
        except Exception:
            pass
        return best["index"], best["name"]

    # 3. Fallback to any working device
    any_working = [m for m in mics if m["working"]]
    if any_working:
        any_working.sort(key=lambda m: m["rms"], reverse=True)
        _vlog.info("find_best_microphone: reason=fallback idx=%s name=%r probe_working=%s",
                   any_working[0]["index"], any_working[0]["name"], probe_working)
        return any_working[0]["index"], any_working[0]["name"]

    _vlog.warning("find_best_microphone: reason=fallback none working (probe_working=%s)", probe_working)
    return None, mics[0]["name"] if mics else "Default Microphone"


def probe_mic_level(device_index=None, chunk_samples=1024):
    """
    Reads a short frame from the microphone and returns normalized volume (0.0 to 1.0) and raw RMS.
    If Voice thread is active, returns its live RMS reading directly with zero hardware contention,
    eliminating PortAudio stream thrashing and preventing ntdll heap corruption.
    """
    if time.time() - getattr(Voice, "_latest_time", 0.0) < 1.0:
        rms = float(getattr(Voice, "_latest_rms", 0.0))
        norm = min(max((rms - 20.0) / 2500.0, 0.0), 1.0)
        return norm, rms

    acquired = _PORTAUDIO_LOCK.acquire(blocking=False)
    if not acquired:
        return 0.0, 0.0
    stream = None
    own = None  # private short-lived instance: terminate() of the shared one cannot affect it
    try:
        import pyaudio
        own = pyaudio.PyAudio()
        if device_index is None:
            info = own.get_default_input_device_info()
            dev_idx = info["index"]
        else:
            info = own.get_device_info_by_index(device_index)
            dev_idx = device_index
        ch = min(info.get("maxInputChannels", 1), 2)
        rate = int(info.get("defaultSampleRate", 44100))

        stream = own.open(
            format=pyaudio.paInt16,
            channels=ch,
            rate=rate,
            input=True,
            input_device_index=dev_idx,
            frames_per_buffer=chunk_samples
        )
        _log_mic_open(own, dev_idx, rate, ch, chunk_samples, "OK", "probe_mic_level")
    except Exception as exc:
        _vlog.warning("mic open (probe_mic_level) FAILED: idx=%s fpb=%s err=%r", device_index, chunk_samples, exc)
        if own is not None:
            own.terminate()
        return 0.0, 0.0
    finally:
        _PORTAUDIO_LOCK.release()

    try:
        data = stream.read(chunk_samples, exception_on_overflow=False)
        arr = np.frombuffer(data, dtype=np.int16)
        rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
        norm = min(max((rms - 20.0) / 2500.0, 0.0), 1.0)
        return norm, rms
    except Exception as exc:
        _vlog.warning("probe_mic_level read failed: %r", exc)
        return 0.0, 0.0
    finally:
        with _PORTAUDIO_LOCK:
            try:
                stream.stop_stream()
                stream.close()
            except Exception as exc:
                _vlog.warning("probe_mic_level close failed: %r", exc)
            own.terminate()


def test_microphone_recognition(device_index=None, duration=3.5, level_cb=None):
    """
    Records audio for duration seconds from device_index, computes energy levels,
    and attempts Google speech recognition.
    Returns:
        {
            "success": bool,
            "text": str,
            "error": str,
            "avg_level": float,
            "max_level": float,
            "device_name": str
        }
    """
    import speech_recognition as sr
    mics = list_microphones()
    dev_name = "Default Microphone"
    for m in mics:
        if m["index"] == device_index:
            dev_name = m["name"]
            break

    result = {
        "success": False,
        "text": "",
        "error": "",
        "avg_level": 0.0,
        "max_level": 0.0,
        "device_name": dev_name
    }

    try:
        with _PORTAUDIO_LOCK:
            r = sr.Recognizer()
            r.energy_threshold = 300
            r.dynamic_energy_threshold = True

            mic = sr.Microphone(device_index=device_index)
            with mic as source:
                r.adjust_for_ambient_noise(source, duration=0.6)
                # Record phrase
                audio = r.listen(source, timeout=2.0, phrase_time_limit=duration)

        # Compute level from captured audio
        raw_bytes = audio.get_raw_data()
        if raw_bytes:
            arr = np.frombuffer(raw_bytes, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
            result["avg_level"] = rms
            result["max_level"] = float(np.max(np.abs(arr)))

        # Speech recognition
        try:
            recognized_text = r.recognize_google(audio)
            result["success"] = True
            result["text"] = recognized_text
        except sr.UnknownValueError:
            result["error"] = "Audio recorded, but speech could not be understood (try speaking closer)."
        except sr.RequestError as re_err:
            result["error"] = f"Speech service unreachable: {re_err}"

    except Exception as exc:
        result["error"] = str(exc)

    return result


def ensure_piper_model(model_path, config_path):
    """Verifies that the Piper model and config exist, downloading them if missing."""
    if os.path.exists(model_path) and os.path.exists(config_path):
        return True
    try:
        import requests
        os.makedirs(os.path.dirname(model_path), exist_ok=True)
        base_url = "https://huggingface.co/jgkawell/jarvis/resolve/main/en/en_GB/jarvis/medium"
        print("[voice] Paul Bettany J.A.R.V.I.S. voice model not found locally. Downloading (~60MB)...")
        for fname, dest in [("jarvis-medium.onnx.json", config_path), ("jarvis-medium.onnx", model_path)]:
            if not os.path.exists(dest):
                resp = requests.get(f"{base_url}/{fname}", stream=True, timeout=60)
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
        print("[voice] Paul Bettany J.A.R.V.I.S. voice model downloaded successfully.")
        return True
    except Exception as exc:
        print(f"[voice] Could not download Paul Bettany model: {exc}")
        return False


_SHARED_PIPER_VOICE = None
_SHARED_PIPER_LOCK = threading.RLock()
_SHARED_PIPER_PATH = None
_AUDIO_PHRASE_CACHE = {}
_CACHED_OUTPUT_SR = None

PRE_RENDERED_PHRASES = [
    "Yes, sir.",
    "At your service.",
    "Online and ready.",
    "Done.",
    "Muted.",
    "Unmuted.",
    "Right away, sir.",
    "Yes, sir?",
]


def _normalize_phrase_key(text: str) -> str:
    """Normalizes text for phrase cache lookup (lowercase, stripped punctuation)."""
    return re.sub(r"[^\w\s]", "", text).strip().lower()


def _get_output_sample_rate(default: int = 44100) -> int:
    """Safely queries hardware output sample rate holding _PORTAUDIO_LOCK during device query."""
    global _CACHED_OUTPUT_SR
    if _CACHED_OUTPUT_SR is not None:
        return _CACHED_OUTPUT_SR
    try:
        import sounddevice as sd
        with _PORTAUDIO_LOCK:
            out_dev = sd.query_devices(kind="output")
        dev_sr = int(out_dev.get("default_samplerate", default))
        if dev_sr > 0:
            _CACHED_OUTPUT_SR = dev_sr
            return dev_sr
    except Exception:
        pass
    _CACHED_OUTPUT_SR = default
    return default


def split_into_sentences(text: str) -> list[str]:
    """
    Splits text into sentences based on punctuation [.!?].
    Preserves punctuation and filters out empty chunks.
    """
    text = text.strip()
    if not text:
        return []
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return parts if parts else [text]


def _synthesize_sentence_audio(voice, text_sentence: str, target_sr: int = None) -> tuple:
    """
    Synthesizes a single sentence or phrase using PiperVoice, applying:
    1. 5ms micro-fade to eliminate DC offsets and inter-chunk boundary clicks.
    2. 60ms natural pause between sentences.
    3. 0.88x headroom scaling (clipped [-0.95, 0.95]) to eliminate inter-sample clipping crackle.
    4. Resampling to output device native sample rate (polyphase filter / linear fallback).
    Returns (audio_float_array, target_sr).
    """
    if voice is None or not text_sentence.strip():
        return np.array([], dtype=np.float32), (target_sr or 44100)

    with _SHARED_PIPER_LOCK:
        chunks = list(voice.synthesize(text_sentence))

    if not chunks:
        return np.array([], dtype=np.float32), (target_sr or 44100)

    raw_sr = chunks[0].sample_rate

    # 1. Clean DC offsets and prevent inter-chunk boundary clicks
    fade_len = int(raw_sr * 0.005)  # 5ms micro-fade
    silence = np.zeros(int(raw_sr * 0.06), dtype=np.float32)  # 60ms natural pause between sentences
    processed_chunks = []
    for c in chunks:
        arr = c.audio_float_array.copy()
        if len(arr) > fade_len * 2:
            ramp = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
            arr[:fade_len] *= ramp
            arr[-fade_len:] *= ramp[::-1]
        processed_chunks.append(arr)
        processed_chunks.append(silence)

    audio = np.concatenate(processed_chunks)

    # 2. Headroom scaling: Prevent digital inter-sample clipping on DAC / Windows mixer
    # 0.88x provides ~1.2 dB of headroom.
    audio = np.clip(audio * 0.88, -0.95, 0.95).astype(np.float32)

    # 3. Resample to hardware output device's native sample rate
    if target_sr is None:
        target_sr = _get_output_sample_rate(default=raw_sr)

    if raw_sr != target_sr:
        try:
            import scipy.signal as signal
            audio = signal.resample_poly(audio, up=target_sr, down=raw_sr).astype(np.float32)
        except Exception:
            old_indices = np.linspace(0, len(audio) - 1, len(audio))
            new_len = int(len(audio) * target_sr / raw_sr)
            new_indices = np.linspace(0, len(audio) - 1, new_len)
            audio = np.interp(new_indices, old_indices, audio).astype(np.float32)

    return audio, target_sr


def _warm_piper_cache(voice):
    """Pre-renders frequent short phrases in memory so playback begins in < 1 ms."""
    global _AUDIO_PHRASE_CACHE
    if voice is None:
        return
    for phrase in PRE_RENDERED_PHRASES:
        key = _normalize_phrase_key(phrase)
        if key not in _AUDIO_PHRASE_CACHE:
            try:
                audio, sr = _synthesize_sentence_audio(voice, phrase)
                if len(audio) > 0:
                    _AUDIO_PHRASE_CACHE[key] = (audio, sr)
                    _AUDIO_PHRASE_CACHE[phrase.strip().lower()] = (audio, sr)
            except Exception as exc:
                print(f"[voice] Failed to pre-render phrase '{phrase}': {exc}")


_PIPER_INTRA_OP_THREADS = 2  # chosen by measurement: 1 thread is ~1.3x slower first-sentence, MediaPipe p99 equal


def _load_piper_limited(model_path, config_path):
    """
    Build a PiperVoice whose ONNX session is thread-limited and non-spinning.
    PiperVoice.load() uses default SessionOptions (all cores, spin-waiting), which starves the
    MediaPipe hand-tracking loop while JARVIS speaks. PiperVoice accepts a ready session, so we
    construct it directly with the arguments load() would use.
    """
    import json
    from pathlib import Path
    import onnxruntime
    from piper import PiperVoice
    from piper.config import PiperConfig

    with open(config_path, "r", encoding="utf-8") as fh:
        config = PiperConfig.from_dict(json.load(fh))
    so = onnxruntime.SessionOptions()
    so.intra_op_num_threads = _PIPER_INTRA_OP_THREADS
    so.inter_op_num_threads = 1
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    so.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = onnxruntime.InferenceSession(str(model_path), sess_options=so, providers=["CPUExecutionProvider"])
    return PiperVoice(config=config, session=session, download_dir=Path.cwd())


def get_shared_piper(model_path: str, config_path: str):
    """
    Returns the warm, process-wide PiperVoice instance.
    Loaded once and held in memory across all Speaker instances.
    """
    global _SHARED_PIPER_VOICE, _SHARED_PIPER_PATH
    with _SHARED_PIPER_LOCK:
        if _SHARED_PIPER_VOICE is not None and (_SHARED_PIPER_PATH == model_path or _SHARED_PIPER_PATH is None):
            return _SHARED_PIPER_VOICE

        if not (os.path.exists(model_path) and os.path.exists(config_path)):
            ensure_piper_model(model_path, config_path)

        if os.path.exists(model_path) and os.path.exists(config_path):
            try:
                print(f"[voice] Loading Paul Bettany J.A.R.V.I.S. neural voice ({os.path.basename(model_path)})...")
                voice = _load_piper_limited(model_path, config_path)
                _SHARED_PIPER_VOICE = voice
                _SHARED_PIPER_PATH = model_path
                print("[voice] Paul Bettany voice model loaded.")
                _warm_piper_cache(voice)
                return voice
            except Exception as e:
                print(f"[voice] PiperVoice initialization failed: {e}")
        return None


def _play_and_wait_bounded(sd, audio, samplerate):
    """
    sd.play() under _PORTAUDIO_LOCK, then a bounded wait instead of the unbounded sd.wait().
    Polls sd.get_stream().active every 0.05 s (Speaker heartbeat inside the loop so a long reply
    does not look like a stall). Deadline = audio duration + 2.0 s; on expiry sd.stop() and a WARNING.
    Returns True if playback finished, False if it was cut at the deadline.
    """
    with _PORTAUDIO_LOCK:
        sd.play(audio, samplerate=samplerate, latency="high")
    duration = len(audio) / float(samplerate)
    deadline = duration + 2.0
    t0 = time.monotonic()
    while True:
        jarvis_log.heartbeat("Speaker", speaking=True, playing=round(time.monotonic() - t0, 1))
        if not sd.get_stream().active:
            return True
        if time.monotonic() - t0 >= deadline:
            try:
                dev = sd.query_devices(kind="output").get("name", "?")
            except Exception as exc:
                dev = f"? ({exc!r})"
            sd.stop()
            _vlog.warning("playback did not finish within %.1fs, device=%s", deadline, dev)
            return False
        time.sleep(0.05)


class Speaker(threading.Thread):
    """
    Synthesizes and plays spoken feedback for J.A.R.V.I.S.
    Supports:
    - 'piper': Offline neural TTS with Paul Bettany voice clone (MCU J.A.R.V.I.S.) via sounddevice.
    - 'system': System-native SAPI5 fallback via pyttsx3.
    """

    def __init__(self, enabled=True, cfg=None):
        super().__init__(name="Speaker", daemon=True)
        self.enabled = enabled
        self.cfg = cfg
        self.q = queue.Queue()
        self._piper_voice = None
        self._sapi_engine = None
        self.is_speaking = False

    def reload_config(self, cfg=None):
        """Live updates speech settings without restart."""
        if cfg:
            self.cfg = cfg
        self.enabled = getattr(self.cfg, "speak", True)

    def say(self, text):
        is_enabled = getattr(self.cfg, "speak", self.enabled) if self.cfg else self.enabled
        if is_enabled and text:
            self.q.put(text)

    def _init_system_engine(self):
        if self._sapi_engine is not None:
            return self._sapi_engine
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 180)
            for v in engine.getProperty("voices"):
                if any(n in v.name for n in ("David", "Mark", "George", "James")):
                    engine.setProperty("voice", v.id)
                    break
            self._sapi_engine = engine
            return engine
        except Exception as exc:
            print(f"[voice] system text-to-speech unavailable: {exc}")
            return None

    def _say_system(self, text):
        engine = self._init_system_engine()
        if engine:
            self.is_speaking = True
            Voice.is_speaking = True
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as e:
                print(f"[voice] system speech error: {e}")
            finally:
                self.is_speaking = False
                Voice.is_speaking = False

    def _get_piper_paths(self):
        rel_model = getattr(self.cfg, "tts_model_path", "models/voice/jarvis-medium.onnx") if self.cfg else "models/voice/jarvis-medium.onnx"
        if os.path.isabs(rel_model):
            model_path = rel_model
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            candidate = os.path.join(base_dir, rel_model)
            if os.path.exists(candidate):
                model_path = candidate
            else:
                appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
                appdata_model = os.path.join(appdata, "Jarvis", rel_model)
                if os.path.exists(appdata_model):
                    model_path = appdata_model
                else:
                    model_path = candidate if not getattr(sys, "frozen", False) else appdata_model
        config_path = model_path + ".json"
        return model_path, config_path

    def _init_piper(self):
        model_path, config_path = self._get_piper_paths()
        voice = get_shared_piper(model_path, config_path)
        self._piper_voice = voice
        self._loaded_model_path = model_path
        return voice

    def _say_piper(self, text):
        voice = self._init_piper()
        if voice is None:
            self._say_system(text)
            return

        self.is_speaking = True
        Voice.is_speaking = True
        stop_synth = threading.Event()
        try:
            import sounddevice as sd

            sentences = split_into_sentences(text)
            if not sentences:
                return

            def get_sentence_audio(sentence: str):
                key = _normalize_phrase_key(sentence)
                cached = _AUDIO_PHRASE_CACHE.get(key)
                if cached is not None:
                    return cached
                return _synthesize_sentence_audio(voice, sentence)

            # Single sentence optimization
            if len(sentences) == 1:
                audio, target_sr = get_sentence_audio(sentences[0])
                if len(audio) > 0:
                    _play_and_wait_bounded(sd, audio, target_sr)
                return

            # Multi-sentence streaming:
            # 1. Synthesize and start playing the first sentence immediately
            audio0, sr0 = get_sentence_audio(sentences[0])

            # 2. Pipeline remaining sentences in background
            synth_queue = queue.Queue(maxsize=len(sentences))

            def _bg_synthesizer():
                try:
                    for s in sentences[1:]:
                        if stop_synth.is_set():
                            break
                        item = get_sentence_audio(s)
                        synth_queue.put(item)
                except Exception as exc:
                    synth_queue.put(exc)
                finally:
                    synth_queue.put(None)

            bg_thread = threading.Thread(target=_bg_synthesizer, daemon=True)
            bg_thread.start()

            # 3. Play first sentence immediately via sounddevice
            if len(audio0) > 0:
                _play_and_wait_bounded(sd, audio0, sr0)

            # 4. Stream remaining sentences as they complete synthesis
            while True:
                item = synth_queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                audio_next, sr_next = item
                if len(audio_next) > 0:
                    _play_and_wait_bounded(sd, audio_next, sr_next)

        except Exception as exc:
            print(f"[voice] Piper synthesis/playback error: {exc}, falling back to system TTS")
            self._say_system(text)
        finally:
            stop_synth.set()
            self.is_speaking = False
            Voice.is_speaking = False

    def run(self):
        jarvis_log.thread_started("Speaker")
        try:
            self._run_body()
        except BaseException as e:
            jarvis_log.thread_exited("Speaker", e)
            raise
        else:
            jarvis_log.thread_exited("Speaker")

    def _run_body(self):
        # Pre-warm Piper model in background so spoken responses are instantaneous
        engine_type = getattr(self.cfg, "tts_engine", "piper") if self.cfg else "piper"
        if engine_type == "piper":
            try:
                self._init_piper()
            except Exception as e:
                print(f"[voice] Piper pre-warm note: {e}")

        while True:
            jarvis_log.heartbeat("Speaker", speaking=self.is_speaking, q=self.q.qsize())
            try:
                text = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            if text is None:
                return
            t_say = time.monotonic()
            try:
                engine_type = getattr(self.cfg, "tts_engine", "piper") if self.cfg else "piper"
                if engine_type == "piper":
                    self._say_piper(text)
                else:
                    self._say_system(text)
            except Exception as exc:
                print(f"[voice] speech output error: {exc}")
            dt_say = time.monotonic() - t_say
            if dt_say > 30.0:
                _vlog.warning("Speaker: say() playback took %.1fs (>30s) engine=%s text=%s",
                              dt_say, engine_type, redact(text))


class WakeWordDetector(threading.Thread):
    """
    Dedicated 16 kHz wake-word detection and VAD recording thread.
    - Captures from probed physical microphone using shared PyAudio protected by _PORTAUDIO_LOCK.
    - CPU usage strictly under ~3% (openWakeWord inference takes ~2.5 ms per 80 ms chunk).
    - Sensitivity mapping (Low=0.7, Balanced=0.5, High=0.35).
    - Configurable follow-up window (default 6 s).
    - False-trigger guard with 2 consecutive detections or score over threshold, suppressed while speaking.
    - Flow:
        Wake detected -> on_wake callback -> soft two-note chime immediately (<150 ms) ->
        Record with VAD (updating Voice._latest_rms) -> end on ~700 ms silence or 8 s max ->
        on_utterance callback (or if 5 s silence with no speech -> on_no_speech callback).
    """

    def __init__(
        self,
        cfg=None,
        on_wake=None,
        on_utterance=None,
        on_no_speech=None,
        on_wake_idle=None,
        stop_event=None,
        sensitivity="balanced",
        followup_window=6.0,
        device_index=None,
        on_speech_end=None,
    ):
        super().__init__(name="WakeWordDetector", daemon=True)
        self.on_speech_end = on_speech_end
        self._was_speaking = False
        self.cfg = cfg  # property: also keeps self.vad.cfg in sync (live mic_energy_threshold)
        self.on_wake = on_wake
        self.on_utterance = on_utterance
        self.on_no_speech = on_no_speech
        self.on_wake_idle = on_wake_idle
        self.stop = stop_event or threading.Event()
        self.sensitivity_str = sensitivity
        self.threshold = parse_sensitivity(getattr(cfg, "wake_sensitivity", sensitivity))
        self.followup_window = float(getattr(cfg, "wake_followup_window", getattr(cfg, "command_window", followup_window)))
        self.device_index = device_index
        self.guard = WakeWordGuard(threshold=self.threshold)
        self.vad = VADDetector(cfg=cfg)
        self.model = None
        self.listening_until = 0.0
        self.is_recording = False
        # diagnostics state (logging only)
        self._hb_t = 0.0
        self._flat_since = None
        self._flat_flagged = False
        self._st_t = time.monotonic()
        self._st_max_score = 0.0
        self._st_loud = 0
        self._st_chunks = 0
        self._stream_fault = None   # "stream_stall" | "stream_error" | "flatline" set by read paths
        self._mic_state = ("down", "not started")
        self._init_model()

    STALL_TIMEOUT_S = 2.0     # no data for this long => stream stall
    STALL_POLL_S = 0.01

    @property
    def cfg(self):
        return self._cfg

    @cfg.setter
    def cfg(self, value):
        self._cfg = value
        vad = getattr(self, "vad", None)
        if vad is not None:
            vad.cfg = value

    def _set_state(self, state, reason=""):
        self._mic_state = (state, reason)

    def mic_state(self):
        """("ok"|"reconnecting"|"stalled"|"flatline"|"down", reason)."""
        if self.ident is not None and not self.is_alive():
            return "down", "WakeWordDetector thread not running"
        state, reason = self._mic_state
        if state in ("reconnecting", "stalled", "down"):
            return state, reason
        if self._flat_flagged:
            return "flatline", f"all-zero input on device [{self.device_index}]"
        return state, reason

    def _read_chunk(self, stream, chunk_samples):
        """
        One bounded chunk read. Before each blocking read, check stream.get_read_available(); if a full
        chunk is not available, wait in 0.01 s steps and declare a stall after STALL_TIMEOUT_S of no data
        (nothing is aborted from another thread). Returns (data, fault) with fault in
        None | "stop" | "stream_stall" | "stream_error".
        """
        try:
            avail = stream.get_read_available()
            if isinstance(avail, (int, np.integer)) and avail < chunk_samples:
                t0 = time.monotonic()
                while avail < chunk_samples:
                    if self.stop.wait(self.STALL_POLL_S):
                        return None, "stop"
                    if time.monotonic() - t0 >= self.STALL_TIMEOUT_S:
                        _vlog.warning("mic stream stall: no data for %.1fs on device [%s] %s (avail=%s/%s)",
                                      self.STALL_TIMEOUT_S, self.device_index, Voice.current_mic_name,
                                      avail, chunk_samples)
                        return None, "stream_stall"
                    avail = stream.get_read_available()
            return stream.read(chunk_samples, exception_on_overflow=False), None
        except Exception as exc:
            _vlog.warning("mic stream read error on device [%s] %s: %r", self.device_index,
                          Voice.current_mic_name, exc)
            print(f"[voice] Microphone stream read error: {exc}. Unplug / disconnect detected.")
            return None, "stream_error"

    def _diag_chunk(self, rms):
        """Per-chunk bookkeeping: heartbeat (<=1/s), 60 s stats line, RMS flatline detection.
        Returns True exactly once per flatline episode (all-zero input for 5 s straight); the caller
        then takes the stream_error / reconnect path."""
        now = time.monotonic()
        flat_new = False
        if rms < 1.0:
            if self._flat_since is None:
                self._flat_since = now
            elif not self._flat_flagged and now - self._flat_since >= 5.0:
                self._flat_flagged = True
                flat_new = True
                _vlog.warning("mic flatline (all-zero input) on device [%s] %s",
                              self.device_index, Voice.current_mic_name)
        else:
            if self._flat_flagged:
                _vlog.info("mic flatline ended on device [%s] (rms=%.1f)", self.device_index, rms)
            self._flat_since = None
            self._flat_flagged = False
        self._st_chunks += 1
        if rms >= self.vad.gate():
            self._st_loud += 1
        if now - self._hb_t >= 1.0:
            self._hb_t = now
            jarvis_log.heartbeat(
                "WakeWordDetector", rms=int(round(rms)), rec=self.is_recording,
                follow=round(max(0.0, self.listening_until - time.time()), 1),
                speaking=Voice.is_speaking)
        if now - self._st_t >= 60.0:
            v_total, v_speech = self.vad.take_stats()
            _vlog.info("wake stats %.0fs: max_score=%.2f noise_floor=%.0f gate=%.0f (energy_floor=%.0f) "
                       "speech=%d/%d chunks (%.1f%%) rms>=gate=%d/%d",
                       now - self._st_t, self._st_max_score, self.vad.noise_floor, self.vad.gate(),
                       self.vad.energy_threshold, v_speech, v_total, 100.0 * v_speech / max(1, v_total),
                       self._st_loud, self._st_chunks)
            self._st_t = now
            self._st_max_score = 0.0
            self._st_loud = self._st_chunks = 0
        return flat_new

    def _init_model(self):
        try:
            from openwakeword.model import Model
            self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except Exception as e:
            print(f"[voice] openWakeWord model initialization note: {e}")
            self.model = None

    def set_sensitivity(self, sensitivity):
        self.sensitivity_str = sensitivity
        self.threshold = parse_sensitivity(sensitivity)
        self.guard.threshold = self.threshold

    def get_sensitivity(self) -> float:
        return self.threshold

    def set_followup_window(self, seconds: float):
        self.followup_window = max(1.0, float(seconds))

    def get_followup_window(self) -> float:
        return self.followup_window

    def record_utterance_with_vad(
        self,
        stream=None,
        timeout_no_speech: float = 5.0,
        silence_endpoint: float = 0.70,
        max_duration: float = 8.0,
        pre_roll: list = None,
        chunk_samples: int = 1280,
        sample_rate: int = 16000,
        trigger: str = "vad",
        pre_speech_chunks: int = 0,
    ) -> bytes:
        """
        Record with VAD: streams chunks while continuously updating Voice._latest_rms.
        End on ~700 ms silence or 8 s max -> return raw PCM bytes.
        If 5 s silence with no speech -> trigger no-speech callback, return None.
        A recording with fewer than MIN_SPEECH_CHUNKS speech chunks is dropped (insufficient_speech).
        pre_speech_chunks: speech chunks already counted by the caller (the chunk that triggered capture).
        """
        self.is_recording = True
        chunk_duration = chunk_samples / sample_rate
        recorded_frames = list(pre_roll) if pre_roll else []

        speech_detected = False
        consecutive_silence = 0.0
        start_time = time.time()
        # diagnostics only
        reason = "stop"
        uid = None
        speech_chunks = int(pre_speech_chunks)
        total_chunks = 0
        rms_list = []

        try:
            while not self.stop.is_set():
                if Voice.is_speaking:
                    reason = "speaking"
                    break

                data = None
                if stream is not None:
                    data, fault = self._read_chunk(stream, chunk_samples)
                    if fault == "stop":
                        reason = "stop"
                        break
                    if fault:
                        reason = fault
                        self._stream_fault = fault
                        break
                if not data:
                    reason = "stream_error"
                    self._stream_fault = "stream_error"
                    break

                arr = np.frombuffer(data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                Voice._latest_rms = rms
                Voice._latest_time = time.time()
                recorded_frames.append(data)
                if self._diag_chunk(rms):
                    reason = "flatline"
                    self._stream_fault = "flatline"
                    break

                now = time.time()
                elapsed = now - start_time
                chunk_has_speech = self.vad.is_speech(arr, rms)
                total_chunks += 1
                rms_list.append(rms)
                if chunk_has_speech:
                    speech_chunks += 1

                if not speech_detected:
                    if chunk_has_speech:
                        speech_detected = True
                        consecutive_silence = 0.0
                        uid = jarvis_log.new_utterance()
                        jarvis_log.utterance_event(jarvis_log.AUDIO_START, uid, trigger=trigger,
                                                   device=self.device_index)
                    elif elapsed >= timeout_no_speech:
                        # 5 s silence with no speech
                        if self.on_no_speech:
                            try:
                                self.on_no_speech()
                            except Exception as ns_err:
                                print(f"[voice] on_no_speech error: {ns_err}")
                        self.listening_until = 0.0
                        return None
                else:
                    if chunk_has_speech:
                        consecutive_silence = 0.0
                    else:
                        consecutive_silence += chunk_duration
                        if consecutive_silence >= silence_endpoint:
                            reason = "silence"
                            break
                    if elapsed >= max_duration:
                        reason = "max_duration"
                        break

            if speech_detected:
                try:
                    jarvis_log.utterance_event(
                        jarvis_log.ENDPOINT, uid, reason=reason, dur_s=f"{time.time() - start_time:.2f}",
                        speech_chunks=speech_chunks, total_chunks=total_chunks,
                        rms_p50=int(sorted(rms_list)[len(rms_list) // 2]) if rms_list else 0)
                except Exception:
                    pass
            if not speech_detected or len(recorded_frames) < 3:
                return None
            if speech_chunks < MIN_SPEECH_CHUNKS:
                jarvis_log.utterance_event(jarvis_log.DROPPED, uid, reason="insufficient_speech",
                                           speech_chunks=speech_chunks)
                return None

            raw_bytes = b"".join(recorded_frames)
            Voice._last_raw_audio = raw_bytes
            return raw_bytes
        finally:
            self.is_recording = False

    def transcribe(self, raw_bytes: bytes) -> str:
        """Transcribes 16 kHz raw PCM bytes via Groq Whisper or Google fallback."""
        if not raw_bytes:
            return ""
        try:
            import speech_recognition as sr
            audio = sr.AudioData(raw_bytes, sample_rate=16000, sample_width=2)
            key = get_api_key()
            ai_enabled = getattr(self.cfg, "ai_enabled", False) if self.cfg else False
            if ai_enabled and key:
                try:
                    wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
                    stt_model = getattr(self.cfg, "ai_stt_model", "whisper-large-v3-turbo") if self.cfg else "whisper-large-v3-turbo"
                    timeout = getattr(self.cfg, "ai_timeout", 10) if self.cfg else 10
                    client = GroqClient(api_key=key, timeout=timeout)
                    return client.transcribe(wav_bytes, model=stt_model, language="en")
                except Exception as e:
                    print(f"[voice] Groq transcribe fallback note: {e}")
            r = sr.Recognizer()
            return r.recognize_google(audio)
        except Exception as exc:
            print(f"[voice] transcription error: {exc}")
            return ""

    def record_and_transcribe(
        self,
        stream=None,
        timeout_no_speech: float = 5.0,
        silence_endpoint: float = 0.70,
        max_duration: float = 8.0,
    ) -> str:
        """Records with VAD and transcribes into an utterance string."""
        raw_bytes = self.record_utterance_with_vad(
            stream=stream,
            timeout_no_speech=timeout_no_speech,
            silence_endpoint=silence_endpoint,
            max_duration=max_duration,
        )
        if not raw_bytes:
            return ""
        return self.transcribe(raw_bytes)

    def reconnect_stream(self, sample_rate: int = 16000, chunk_samples: int = 1280):
        """
        Auto-recovery backoff loop (1s, 2s, 4s) when audio stream encounters error/unplug/stall.
        The caller has already closed the broken stream, so no stream of ours is open when the shared
        PyAudio instance is reset. Only candidates that pass a 16 kHz mono open + read are considered
        (MME, then DirectSound; never WASAPI or WDM-KS); every candidate result is logged.
        """
        import pyaudio
        delays = [1.0, 2.0, 4.0]
        delay_idx = 0
        print("[voice] Audio stream disconnected. Entering backoff retry loop (1s, 2s, 4s)...")
        _vlog.warning("reconnect_stream: entering backoff loop (device was [%s] %s)", self.device_index,
                      Voice.current_mic_name)
        self._set_state("reconnecting", "audio stream lost; backoff loop")
        attempt = 0

        while not self.stop.is_set():
            delay = delays[delay_idx]
            if delay_idx < len(delays) - 1:
                delay_idx += 1
            if self.stop.wait(delay):
                return None

            attempt += 1
            reset_shared_pyaudio()
            p = get_shared_pyaudio()
            best_idx, best_name = find_best_microphone(self.cfg, require_16k=True)
            if best_idx is None:
                _vlog.warning("reconnect_stream: attempt %d: no microphone opened at 16 kHz; retrying", attempt)
                continue

            try:
                with _PORTAUDIO_LOCK:
                    test_stream = _shared_open(p, 
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=sample_rate,
                        input=True,
                        input_device_index=best_idx,
                        frames_per_buffer=chunk_samples,
                    )
                _log_mic_open(p, best_idx, sample_rate, 1, chunk_samples, "OK", "reconnect_stream")
                # Verify reading at least one frame works without exception
                test_data = test_stream.read(chunk_samples, exception_on_overflow=False)
                if test_data and len(test_data) > 0:
                    self.device_index = best_idx
                    Voice.current_mic_index = best_idx
                    Voice.current_mic_name = best_name
                    self._set_state("ok", "")
                    print(f"[voice] Microphone auto-recovered successfully to [{best_idx}] {best_name}.")
                    _vlog.info("reconnect_stream: recovered on attempt %d: idx=%s name=%r", attempt, best_idx, best_name)
                    return test_stream
                _vlog.warning("reconnect_stream: attempt %d: idx=%s opened but read returned no data", attempt, best_idx)
                with _PORTAUDIO_LOCK:
                    test_stream.stop_stream()
                    _shared_close(test_stream)
            except Exception as exc:
                _log_mic_open(p, best_idx, sample_rate, 1, chunk_samples, repr(exc), "reconnect_stream")
        return None

    def run(self):
        jarvis_log.thread_started("WakeWordDetector")
        try:
            self._run_body()
        except BaseException as e:
            self._set_state("down", f"{type(e).__name__}: {e}")
            jarvis_log.thread_exited("WakeWordDetector", e)
            raise
        else:
            self._set_state("down", "stopped")
            jarvis_log.thread_exited("WakeWordDetector")

    def _run_body(self):
        import pyaudio

        sample_rate = 16000
        chunk_samples = 1280  # 80 ms chunks

        while not self.stop.is_set():
            # Re-fetch every pass: reconnect_stream() resets the shared PyAudio instance.
            p = get_shared_pyaudio()
            cfg_idx = getattr(self.cfg, "mic_device_index", None) if self.cfg else None
            active_idx = cfg_idx if cfg_idx is not None else self.device_index
            if active_idx is None:
                active_idx, _ = find_best_microphone(self.cfg, require_16k=True)
            self.device_index = active_idx

            stream = None
            self._stream_fault = None
            try:
                with _PORTAUDIO_LOCK:
                    stream = _shared_open(p, 
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=sample_rate,
                        input=True,
                        input_device_index=active_idx,
                        frames_per_buffer=chunk_samples,
                    )
                _log_mic_open(p, active_idx, sample_rate, 1, chunk_samples, "OK", "WakeWordDetector.run")
                self._set_state("ok", "")
            except Exception as exc:
                _log_mic_open(p, active_idx, sample_rate, 1, chunk_samples, repr(exc), "WakeWordDetector.run")
                print(f"[voice] WakeWordDetector failed to open mic [{active_idx}]: {exc}")
                stream = self.reconnect_stream(sample_rate, chunk_samples)
                if stream is None:
                    continue

            pre_roll = collections.deque(maxlen=4)
            stream_error = False
            try:
                while not self.stop.is_set():
                    new_cfg_idx = getattr(self.cfg, "mic_device_index", None) if self.cfg else None
                    if new_cfg_idx is not None and new_cfg_idx != self.device_index:
                        break

                    data, fault = self._read_chunk(stream, chunk_samples)
                    if fault == "stop":
                        break
                    if fault:
                        self._stream_fault = fault
                        stream_error = True
                        break

                    if not data:
                        continue

                    arr = np.frombuffer(data, dtype=np.int16)
                    rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                    Voice._latest_rms = rms
                    Voice._latest_time = time.time()
                    if self._diag_chunk(rms):
                        # All-zero input for 5 s straight: stale stream (e.g. after resume). Reconnect once.
                        self._stream_fault = "flatline"
                        stream_error = True
                        break

                    # Suppress wake word if assistant is speaking
                    if Voice.is_speaking:
                        self._was_speaking = True
                        self.guard.evaluate(0.0, is_speaking=True)
                        pre_roll.clear()
                        continue

                    now = time.time()
                    if self._was_speaking:
                        # JARVIS just finished speaking: let Voice restore ducked media if the window lapsed.
                        self._was_speaking = False
                        if self.on_speech_end:
                            self.on_speech_end(now)

                    # Check if follow-up window expired without speech
                    if self.listening_until > 0.0 and now >= self.listening_until:
                        self.listening_until = 0.0
                        if self.on_no_speech:
                            try:
                                self.on_no_speech()
                            except Exception as ns_err:
                                print(f"[voice] on_no_speech error: {ns_err}")

                    # One VAD evaluation per chunk (Silero is stateful; also feeds the noise floor).
                    chunk_speech = self.vad.is_speech(arr, rms)

                    # In follow-up window? Check if user started speaking directly
                    in_followup = now < self.listening_until
                    if in_followup and chunk_speech:
                        pre_roll.append(data)
                        raw = self.record_utterance_with_vad(
                            stream=stream,
                            pre_roll=list(pre_roll),
                            chunk_samples=chunk_samples,
                            sample_rate=sample_rate,
                            trigger="followup",
                            pre_speech_chunks=1,
                        )
                        pre_roll.clear()
                        if raw:
                            self._dispatch_utterance(raw, is_command=True)
                        if self._stream_fault:
                            stream_error = True
                            break
                        continue

                    pre_roll.append(data)

                    # 1. openWakeWord prediction for instant "Hey Jarvis" wake chime
                    wake_triggered = False
                    if getattr(self.cfg, "wake_word_enabled", True) and self.model is not None:
                        pred = self.model.predict(arr)
                        score = pred.get("hey_jarvis", 0.0)
                        if score > self._st_max_score:
                            self._st_max_score = float(score)
                        if self.guard.evaluate(score, is_speaking=Voice.is_speaking, now=now):
                            wake_triggered = True
                            _vlog.info("wake: openwakeword score=%.2f", float(score))
                            # WAKE DETECTED!
                            # 1. Fire wake callback immediately
                            if self.on_wake:
                                try:
                                    self.on_wake()
                                except Exception as cb_err:
                                    print(f"[voice] on_wake error: {cb_err}")
                            elif getattr(self.cfg, "chime_enabled", True):
                                play_chime(non_blocking=True)

                            # 2. Enter follow-up window & record with VAD
                            self.listening_until = time.time() + self.followup_window
                            raw = self.record_utterance_with_vad(
                                stream=stream,
                                pre_roll=list(pre_roll),
                                chunk_samples=chunk_samples,
                                sample_rate=sample_rate,
                                timeout_no_speech=min(self.followup_window, 3.5),
                                trigger="wake",
                                pre_speech_chunks=1 if chunk_speech else 0,
                            )
                            pre_roll.clear()
                            if raw:
                                self._dispatch_utterance(raw, is_command=True)
                            elif not self._stream_fault:
                                if self.on_wake_idle:
                                    try:
                                        self.on_wake_idle()
                                    except Exception as idle_err:
                                        print(f"[voice] on_wake_idle error: {idle_err}")
                                self.listening_until = time.time() + self.followup_window
                            if self._stream_fault:
                                stream_error = True
                                break

                    # 2. Continuous VAD speech capture:
                    # Captures direct voice commands (e.g. "Jarvis open Chrome" or "Jarvis")
                    # even if openWakeWord did not trigger on "hey_jarvis".
                    # _recognition_worker and _process verify wake_aliases in passive standby,
                    # ensuring zero false activations from random background speech.
                    if chunk_speech and not wake_triggered:
                        raw = self.record_utterance_with_vad(
                            stream=stream,
                            pre_roll=list(pre_roll),
                            chunk_samples=chunk_samples,
                            sample_rate=sample_rate,
                            pre_speech_chunks=1,
                        )
                        pre_roll.clear()
                        if raw:
                            self._dispatch_utterance(raw, is_command=in_followup)
                        if self._stream_fault:
                            stream_error = True
                            break
            finally:
                if stream is not None:
                    try:
                        with _PORTAUDIO_LOCK:
                            if hasattr(stream, "is_stopped") and not stream.is_stopped():
                                stream.stop_stream()
                            _shared_close(stream)
                    except Exception as close_exc:
                        _vlog.warning("closing mic stream failed: %r", close_exc)
                    stream = None

            if stream_error and not self.stop.is_set():
                fault = self._stream_fault or "stream_error"
                self._set_state("stalled" if fault == "stream_stall" else "reconnecting", fault)
                _vlog.warning("mic stream fault reason=%s device=[%s] %s: closed, reconnecting",
                              fault, self.device_index, Voice.current_mic_name)
                self._stream_fault = None
                self.device_index = None
                recovered_stream = self.reconnect_stream(sample_rate, chunk_samples)
                if recovered_stream is not None:
                    try:
                        with _PORTAUDIO_LOCK:
                            recovered_stream.stop_stream()
                            _shared_close(recovered_stream)
                    except Exception as close_exc:
                        _vlog.warning("closing recovered mic stream failed: %r", close_exc)

    def listen(self, timeout_no_speech: float = 5.0) -> bytes:
        """
        Actively captures an utterance with VAD and returns raw audio bytes.
        PortAudio lock is held only during stream open and close, never during blocking reads.
        """
        cfg_idx = getattr(self.cfg, "mic_device_index", None) if self.cfg else None
        active_idx = cfg_idx if cfg_idx is not None else self.device_index
        if active_idx is None:
            active_idx, _ = find_best_microphone(self.cfg)

        import pyaudio
        p = get_shared_pyaudio()
        stream = None
        try:
            with _PORTAUDIO_LOCK:
                stream = _shared_open(p, 
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=16000,
                    input=True,
                    input_device_index=active_idx,
                    frames_per_buffer=1280,
                )
            _log_mic_open(p, active_idx, 16000, 1, 1280, "OK", "WakeWordDetector.listen")
            return self.record_utterance_with_vad(stream=stream, timeout_no_speech=timeout_no_speech)
        except Exception as exc:
            if stream is None:
                _log_mic_open(p, active_idx, 16000, 1, 1280, repr(exc), "WakeWordDetector.listen")
            print(f"[voice] listen failed: {exc}")
            return None
        finally:
            if stream is not None:
                try:
                    with _PORTAUDIO_LOCK:
                        stream.stop_stream()
                        _shared_close(stream)
                except Exception:
                    pass

    def _dispatch_utterance(self, raw_bytes: bytes, is_command: bool = True):
        if self.on_utterance:
            try:
                self.on_utterance(raw_bytes, is_command=is_command)
            except TypeError:
                self.on_utterance(raw_bytes)
            except Exception as e:
                print(f"[voice] dispatch utterance error: {e}")


class Voice(threading.Thread):
    """Say 'Jarvis, open Chrome' in one go, or say 'Jarvis' and wait for 'Yes, sir?'."""
    _latest_rms = 0.0
    _latest_time = 0.0
    _last_raw_audio = None
    is_speaking = False
    current_mic_index = None      # class-level so WakeWordDetector diagnostics never hit a missing attribute
    current_mic_name = "Auto"

    def __init__(self, cfg, ctx, stop_event):
        super().__init__(name="Voice", daemon=True)
        self.cfg = cfg
        self.ctx = ctx
        self.stop = stop_event
        # Private per-instance stop: set when this Voice exits (for any reason) so its STT worker and
        # wake detector never outlive it and a restarted Voice never competes with an old one.
        self._own_stop = threading.Event()
        self._groq_cd_logged = 0.0
        self.listening_until = 0.0
        self.audio_q = queue.Queue(maxsize=3)
        self.current_mic_index = None
        self.current_mic_name = "Auto"
        self.groq_cooldown_until = 0.0
        self.sensitivity = getattr(cfg, "wake_sensitivity", "balanced") if cfg else "balanced"
        self.followup_window = float(getattr(cfg, "wake_followup_window", getattr(cfg, "command_window", 6.0))) if cfg else 6.0
        self.wake_detector = None
        self._ducked = False
        self._restore_pending = False

    def _duck_media(self, reason):
        """Lower other apps' volume so the follow-up is heard. Once per interaction."""
        self._restore_pending = False
        if self._ducked or not getattr(self.cfg, "media_duck_on_wake", True):
            return
        import audio_ducker
        if audio_ducker.duck(reason=reason):
            self._ducked = True
            _vlog.info("media duck: %s", reason)

    def _restore_media(self, reason, force=False):
        """Restore volume once the interaction is over; deferred while JARVIS is mid-reply."""
        if not self._ducked:
            return
        if Voice.is_speaking and not force:
            self._restore_pending = True
            return
        import audio_ducker
        self._ducked = False
        self._restore_pending = False
        audio_ducker.restore(reason=reason)
        _vlog.info("media restore: %s", reason)

    def _on_speech_finished(self, now):
        """Speaker went idle. If media is still ducked and the follow-up window has expired, restore now
        instead of waiting for the ducker's 20 s auto-restore."""
        if not self._ducked:
            return
        wd_until = self.wake_detector.listening_until if self.wake_detector else 0.0
        if now >= self.listening_until and now >= wd_until:
            self._restore_media("speech_done")

    def mic_state(self):
        """("ok"|"reconnecting"|"stalled"|"flatline"|"down", reason) for the supervisor health summary."""
        if self.wake_detector is None:
            return "down", "WakeWordDetector not started"
        return self.wake_detector.mic_state()

    def listen(self, timeout: float = None) -> str:
        """
        Actively listens for a speech utterance and returns the transcribed text.
        Lock contention free: PortAudio lock is never held during blocking reads.
        """
        if self.wake_detector:
            return self.wake_detector.record_and_transcribe(timeout_no_speech=timeout or 5.0)
        return ""

    def set_sensitivity(self, sensitivity):
        self.sensitivity = sensitivity
        if self.wake_detector:
            self.wake_detector.set_sensitivity(sensitivity)

    def get_sensitivity(self) -> float:
        if self.wake_detector:
            return self.wake_detector.get_sensitivity()
        return parse_sensitivity(self.sensitivity)

    def set_followup_window(self, seconds: float):
        self.followup_window = float(seconds)
        if self.wake_detector:
            self.wake_detector.set_followup_window(seconds)

    def get_followup_window(self) -> float:
        if self.wake_detector:
            return self.wake_detector.get_followup_window()
        return self.followup_window

    def reload_config(self, cfg=None):
        """Live updates voice & wake settings without restart."""
        if cfg:
            self.cfg = cfg
        sens = getattr(self.cfg, "wake_sensitivity", "balanced") if self.cfg else "balanced"
        self.set_sensitivity(sens)
        win = float(getattr(self.cfg, "wake_followup_window", getattr(self.cfg, "command_window", 6.0))) if self.cfg else 6.0
        self.set_followup_window(win)
        if self.wake_detector:
            self.wake_detector.cfg = self.cfg

    def _on_wake_detected(self):
        self._duck_media("wake_word")
        now = time.time()
        self.listening_until = now + self.followup_window
        if self.wake_detector:
            self.wake_detector.listening_until = self.listening_until
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("wake", duration=0.6)
                self.ctx.overlay.post("wake_detected")
                self.ctx.overlay.post("listening", until=self.listening_until)
            except Exception:
                pass
        if getattr(self.cfg, "chime_enabled", True):
            play_chime(non_blocking=True)
        if hasattr(self.ctx, "chirp"):
            try:
                self.ctx.chirp("wake")
            except Exception:
                pass

    def _on_wake_idle(self):
        """User triggered wake word and paused; prompt confirmation and open follow-up window."""
        now = time.time()
        self.listening_until = now + self.followup_window
        if self.wake_detector:
            self.wake_detector.listening_until = self.listening_until
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("listening", until=self.listening_until)
            except Exception:
                pass
        if hasattr(self.ctx, "say"):
            self.ctx.say("Yes, sir?")

    def _on_no_speech_detected(self):
        self._restore_media("followup_closed")
        self.listening_until = 0.0
        if self.wake_detector:
            self.wake_detector.listening_until = 0.0
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("no_speech")
                self.ctx.overlay.post("listening_done")
            except Exception:
                pass

    def post_partial_speech(self, text: str, is_final: bool = False):
        """Posts streaming partial or live speech transcript to the Dynamic Island."""
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("live_speech", text=text, is_final=is_final)
            except Exception:
                pass

    def _on_utterance_recorded(self, raw_bytes: bytes, is_command: bool = True):
        if not raw_bytes:
            return
        Voice._last_raw_audio = raw_bytes
        uid = jarvis_log.current_utterance()
        try:
            import speech_recognition as sr
            audio = sr.AudioData(raw_bytes, sample_rate=16000, sample_width=2)
            self.audio_q.put_nowait((audio, is_command, time.time(), uid))
        except queue.Full:
            qs = self.audio_q.qsize()
            jarvis_log.utterance_event(jarvis_log.DROPPED, uid, reason="audio_q_full", qsize=qs)
            _vlog.warning("audio_q full (qsize=%d): dropping utterance %s", qs, uid)
        except Exception as exc:
            _vlog.warning("enqueue utterance %s failed: %r", uid, exc)

    def run(self):
        jarvis_log.thread_started("Voice")
        try:
            self._run_body()
        except BaseException as e:
            jarvis_log.thread_exited("Voice", e)
            raise
        else:
            jarvis_log.thread_exited("Voice")
        finally:
            self._own_stop.set()
            self._restore_media("voice_stop", force=True)

    def _run_body(self):
        try:
            import speech_recognition as sr
        except ImportError:
            if hasattr(self.ctx, "say"):
                self.ctx.say("Voice control needs the SpeechRecognition package.")
            return

        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = getattr(self.cfg, "mic_dynamic_energy", False)
        recognizer.pause_threshold = getattr(self.cfg, "mic_pause_threshold", 1.0)
        recognizer.phrase_threshold = 0.3
        recognizer.non_speaking_duration = 0.5

        # Probed microphone
        dev_idx, dev_name = find_best_microphone(self.cfg, require_16k=True)
        self.current_mic_index = dev_idx
        self.current_mic_name = dev_name
        Voice.current_mic_index = dev_idx
        Voice.current_mic_name = dev_name
        print(f"[voice] Connecting to microphone [{dev_idx}]: {dev_name}")
        _vlog.info("Voice: microphone chosen idx=%s name=%r (configured idx=%s)", dev_idx, dev_name,
                   getattr(self.cfg, "mic_device_index", None))

        # STT background worker
        worker = threading.Thread(target=jarvis_log.run_logged,
                                  args=("STTWorker", self._recognition_worker, recognizer, sr),
                                  name="STTWorker", daemon=True)
        worker.start()

        # Dedicated openWakeWord detector at 16 kHz
        self.wake_detector = WakeWordDetector(
            cfg=self.cfg,
            on_wake=self._on_wake_detected,
            on_utterance=self._on_utterance_recorded,
            on_no_speech=self._on_no_speech_detected,
            on_wake_idle=self._on_wake_idle,
            on_speech_end=self._on_speech_finished,
            stop_event=self._own_stop,
            sensitivity=self.sensitivity,
            followup_window=self.followup_window,
            device_index=dev_idx,
        )
        self.wake_detector.start()

        while not self.stop.is_set():
            jarvis_log.heartbeat("Voice")
            if not self.wake_detector.is_alive() and not self.stop.is_set():
                _vlog.error("WakeWordDetector thread died (mic_state=%s); Voice exiting so the supervisor "
                            "restarts it", self.wake_detector.mic_state())
                raise RuntimeError("WakeWordDetector thread died")
            configured_idx = getattr(self.cfg, "mic_device_index", None)
            if configured_idx is not None and configured_idx != self.wake_detector.device_index:
                print(f"[voice] Switching microphone to [{configured_idx}]...")
                self.wake_detector.device_index = configured_idx
                self.current_mic_index = configured_idx

            configured_sens = getattr(self.cfg, "wake_sensitivity", None)
            if configured_sens is not None and configured_sens != self.wake_detector.sensitivity_str:
                self.set_sensitivity(configured_sens)

            configured_followup = getattr(self.cfg, "wake_followup_window", None)
            if configured_followup is not None and float(configured_followup) != self.wake_detector.followup_window:
                self.set_followup_window(float(configured_followup))

            self.stop.wait(timeout=0.5)

    _STT_OFFLINE_NOTICE_INTERVAL = 60.0

    def _notify_stt_offline(self, uid=None):
        """Post a rate-limited 'no internet' error (once per 60 s) when all STT engines failed on the network."""
        now = time.monotonic()
        last = getattr(self, "_stt_offline_notified_at", None)
        if last is not None and now - last < self._STT_OFFLINE_NOTICE_INTERVAL:
            return
        self._stt_offline_notified_at = now
        _vlog.warning("All STT engines failed with network errors; posting offline notice")
        jarvis_log.utterance_event(jarvis_log.RESULT, uid, step="stt_offline", ok=False)
        ov = getattr(self.ctx, "overlay", None)
        if ov:
            try:
                ov.post("error", text="No internet - speech unavailable")
            except Exception:
                _vlog.warning("offline notice post failed", exc_info=True)

    def _fallback_google(self, recognizer, audio, sr):
        """Falls back to Google SpeechRecognition if Groq is unavailable."""
        t0 = time.monotonic()
        try:
            nbytes = len(audio.get_raw_data())
            jarvis_log.utterance_event(jarvis_log.STT_SENT, engine="google", bytes=nbytes,
                                       dur_s=f"{nbytes / 32000.0:.2f}")
        except Exception:
            pass
        try:
            print("[voice] Falling back to Google SpeechRecognition...")
            text = recognizer.recognize_google(audio)
            print(f"[voice] Google Heard: \"{redact(text)}\"")
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, engine="google", text=text, ok=bool(text),
                                       ms=int((time.monotonic() - t0) * 1000))
            return text
        except sr.UnknownValueError as exc:
            print("[voice] (Audio captured, but speech could not be understood)")
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, engine="google", ok=False, err=type(exc).__name__,
                                       ms=int((time.monotonic() - t0) * 1000))
            return None
        except sr.RequestError as re_err:
            print(f"[voice] Google speech service error: {re_err}")
            self._google_net_err = True
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, engine="google", ok=False, err=type(re_err).__name__,
                                       ms=int((time.monotonic() - t0) * 1000))
            self.ctx.say("The speech service is unreachable, sir.")
            time.sleep(4)
            return None
        except Exception as exc:
            print(f"[voice] Google recognition error: {exc}")
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, engine="google", ok=False, err=type(exc).__name__,
                                       ms=int((time.monotonic() - t0) * 1000))
            return None

    def _recognition_worker(self, recognizer, sr):
        vp = get_voice_profile()

        while not self.stop.is_set() and not self._own_stop.is_set():
            jarvis_log.heartbeat("STTWorker", q=self.audio_q.qsize())
            try:
                item = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                continue

            uid = None
            if isinstance(item, tuple):
                if len(item) >= 4:
                    audio, is_command, enqueued_at, uid = item[:4]
                else:
                    audio, is_command, enqueued_at = item
            else:
                audio = item
                is_command = False
                enqueued_at = time.time()

            jarvis_log.set_current_utterance(uid)
            try:
                self._handle_audio_item(recognizer, sr, vp, audio, is_command, enqueued_at, uid)
            finally:
                jarvis_log.set_current_utterance(None)

    def _handle_audio_item(self, recognizer, sr, vp, audio, is_command, enqueued_at, uid):
        # (body of the former _recognition_worker loop; `continue` -> `return`)
        if True:
            # Energy-based VAD validation before sending audio
            raw_bytes = audio.get_raw_data()
            if not raw_bytes or len(raw_bytes) < 3200:
                jarvis_log.utterance_event(jarvis_log.DROPPED, uid, reason="too_short",
                                           bytes=len(raw_bytes) if raw_bytes else 0)
                return
            arr = np.frombuffer(raw_bytes, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))

            now = time.time()
            is_active_session = is_command or (now < self.listening_until) or (self.wake_detector and now < self.wake_detector.listening_until)

            # Noise gate: reject quiet acoustic flutter (< 25.0) in active session, require clear speech (> 40.0) in standby
            min_rms = 25.0 if is_active_session else 40.0
            if rms < min_rms:
                jarvis_log.utterance_event(jarvis_log.DROPPED, uid, reason="rms_gate",
                                           rms=int(rms), min=int(min_rms))
                return

            # Speaker verification gate (if enrolled and enabled in config)
            if vp.is_enrolled and getattr(self.cfg, "voice_speaker_verification", False):
                is_match, sim = vp.verify(raw_bytes)
                if not is_match:
                    print(f"[voice] Speaker verification rejected non-owner audio (similarity: {sim:.2f})")
                    jarvis_log.utterance_event(jarvis_log.DROPPED, uid, reason="speaker_verify", sim=f"{sim:.2f}")
                    return

            # Cache latest valid speech audio for profile enrollment ("train my voice")
            Voice._last_raw_audio = raw_bytes

            # Visual feedback:
            # ONLY display "Processing speech…" on the screen if user is already in an active session (already said Jarvis)
            # In passive standby, keep until=now (0 duration) so background noise does not disrupt the screen,
            # while keeping event compatibility for test harnesses.
            if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                try:
                    visual_until = (now + 4.0) if is_active_session else now
                    self.ctx.overlay.post("listening", until=visual_until, transcribing=True)
                except Exception:
                    pass

            text = None
            groq_net_fail = False
            self._google_net_err = False
            key = get_api_key(self.cfg)
            ai_enabled = getattr(self.cfg, "ai_enabled", False)
            use_groq = bool(ai_enabled and key and now >= self.groq_cooldown_until)
            if ai_enabled and key and not use_groq and self._groq_cd_logged != self.groq_cooldown_until:
                self._groq_cd_logged = self.groq_cooldown_until
                _vlog.warning("Groq cooling down until %s (%.0fs left); using Google",
                              time.strftime("%H:%M:%S", time.localtime(self.groq_cooldown_until)),
                              self.groq_cooldown_until - now)

            if use_groq:
                t_groq = time.monotonic()
                try:
                    # Convert in-memory audio chunk to 16 kHz mono WAV (zero temp files)
                    wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
                    stt_model = getattr(self.cfg, "ai_stt_model", "whisper-large-v3-turbo")
                    timeout = getattr(self.cfg, "ai_timeout", 10)
                    client = GroqClient(api_key=key, timeout=timeout)
                    print(f"[voice] Transcribing complete utterance via Groq Whisper ({stt_model})...")
                    jarvis_log.utterance_event(jarvis_log.STT_SENT, uid, engine="groq", bytes=len(wav_bytes),
                                               dur_s=f"{len(raw_bytes) / 32000.0:.2f}")
                    text = client.transcribe(wav_bytes, model=stt_model, language="en")
                    print(f"[voice] Groq Heard: \"{redact(text)}\"")
                    jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", text=text, ok=bool(text),
                                               ms=int((time.monotonic() - t_groq) * 1000))
                    # Clear any prior cooldown
                    self.groq_cooldown_until = 0.0
                except AuthenticationError as auth_err:
                    jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                               err=type(auth_err).__name__, ms=int((time.monotonic() - t_groq) * 1000))
                    print(f"[voice] Groq key authentication failed: {auth_err}")
                    self.groq_cooldown_until = time.time() + 300.0  # 5 min cooldown on auth failure
                    if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                        try:
                            self.ctx.overlay.post("ai_error", text="Invalid Groq Key")
                        except Exception:
                            pass
                    text = self._fallback_google(recognizer, audio, sr)
                except RateLimitError as rl_err:
                    jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                               err=type(rl_err).__name__, ms=int((time.monotonic() - t_groq) * 1000))
                    print(f"[voice] Groq rate limit: {rl_err}")
                    wait_sec = rl_err.retry_after if rl_err.retry_after else 10.0
                    self.groq_cooldown_until = time.time() + wait_sec
                    if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                        try:
                            self.ctx.overlay.post("ai_error", text="Groq Rate Limited")
                        except Exception:
                            pass
                    text = self._fallback_google(recognizer, audio, sr)
                except (OfflineError, TimeoutError) as net_err:
                    jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                               err=type(net_err).__name__, ms=int((time.monotonic() - t_groq) * 1000))
                    print(f"[voice] Groq network offline/timeout: {net_err}")
                    self.groq_cooldown_until = time.time() + 15.0
                    groq_net_fail = True
                    if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                        try:
                            self.ctx.overlay.post("ai_error", text="Groq Offline")
                        except Exception:
                            pass
                    text = self._fallback_google(recognizer, audio, sr)
                except Exception as exc:
                    jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                               err=type(exc).__name__, ms=int((time.monotonic() - t_groq) * 1000))
                    print(f"[voice] Groq error ({exc}), falling back to Google...")
                    text = self._fallback_google(recognizer, audio, sr)
            else:
                text = self._fallback_google(recognizer, audio, sr)

            # Every STT engine tried failed on the network: tell the user once per interval.
            if not text and self._google_net_err and (groq_net_fail or not use_groq):
                self._notify_stt_offline(uid)

            if text:
                try:
                    self._process(text, is_command=is_command)
                except TypeError:
                    self._process(text)

    def _rearm_followup(self):
        """Keep the follow-up window open. Called only after a command produced a (non-None) reply."""
        followup_sec = float(getattr(self.cfg, "wake_followup_window", getattr(self.cfg, "command_window", 6.0)))
        self.listening_until = time.time() + followup_sec
        if self.wake_detector:
            self.wake_detector.listening_until = self.listening_until

    def _process(self, text, is_command: bool = False):
        t = text.lower().strip()
        now = time.time()
        command = None
        is_active = is_command or (now < self.listening_until) or (self.wake_detector and now < self.wake_detector.listening_until)
        matched_alias = None

        if is_active:
            command = t
            for alias in getattr(self.cfg, "wake_aliases", ["jarvis"]):
                pattern = r"^\s*" + re.escape(alias) + r"\b"
                match = re.search(pattern, command)
                if match:
                    matched_alias = alias
                    stripped = command[match.end():].strip(" ,.!?")
                    if stripped:
                        command = stripped
                    else:
                        command = ""
                    break
            jarvis_log.utterance_event(jarvis_log.WAKE_GATE, active=True, matched=matched_alias, command=command)
            # Follow-up speech without a wake word: Whisper routinely hallucinates these on noise.
            if matched_alias is None and _normalize_noise_text(t) in _WHISPER_NOISE_NORMALIZED:
                jarvis_log.utterance_event(jarvis_log.DROPPED, reason="hallucination")
                if now >= self.listening_until and hasattr(self.ctx, "overlay") and self.ctx.overlay:
                    try:
                        self.ctx.overlay.post("listening_done")
                    except Exception:
                        pass
                return
        else:
            for alias in getattr(self.cfg, "wake_aliases", ["jarvis"]):
                pattern = r"\b" + re.escape(alias) + r"\b"
                match = re.search(pattern, t)
                if match:
                    matched_alias = alias
                    command = t[match.end():].strip(" ,.!?")
                    break
            jarvis_log.utterance_event(jarvis_log.WAKE_GATE, active=False, matched=matched_alias, command=command)
            if command is None:
                jarvis_log.utterance_event(jarvis_log.DROPPED, reason="no_wake_word")
                # No wake word: ignore and clear any listening state on the island
                if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                    try:
                        self.ctx.overlay.post("listening_done")
                    except Exception:
                        pass
                return

        if matched_alias is not None:
            self._duck_media("alias_wake" if command else "alias_only")

        if not command:
            self.listening_until = now + getattr(self.cfg, "wake_followup_window", getattr(self.cfg, "command_window", 6.0))
            if self.wake_detector:
                self.wake_detector.listening_until = self.listening_until
            try:
                self.ctx.overlay.post("wake", duration=0.6)
                self.ctx.overlay.post("wake_detected")
                self.ctx.overlay.post("listening", until=self.listening_until)
            except Exception:
                pass
            if getattr(self.cfg, "chime_enabled", True):
                play_chime(non_blocking=True)
            if hasattr(self.ctx, "chirp"):
                try:
                    self.ctx.chirp("wake")
                except Exception:
                    pass
            self.ctx.say("Yes, sir?")
            jarvis_log.utterance_event(jarvis_log.RESULT, step="wake_ack", ok=True)
            return

        # Continuously adapt voice profile on confirmed commands
        if Voice._last_raw_audio:
            try:
                vp = get_voice_profile()
                if vp.is_enrolled:
                    feat = vp.extract_features(Voice._last_raw_audio)
                    if feat is not None:
                        vp.adapt(feat)
            except Exception:
                pass

        # Post recognized prompt to the screen immediately so the user sees what Jarvis received
        try:
            if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                self.ctx.overlay.post("prompt", text=command, raw=text)
        except Exception:
            pass

        if hasattr(self.ctx, "chirp"):
            try:
                self.ctx.chirp("confirm")
            except Exception:
                pass

        # Hand off for intent resolution/processing: overlay receives 'thinking'
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("thinking", active=True, timeout=8.0)
            except Exception:
                pass

        try:
            # Direct intercept for Dynamic Island voice navigation
            island_reply = handle_island_voice_command(command, self.ctx)
            if island_reply:
                self.ctx.say(island_reply)
                jarvis_log.utterance_event(jarvis_log.RESULT, step="reply", ok=True, via="island")
                self._rearm_followup()
                return

            try:
                reply = run_command(command, self.ctx)
            except Exception as exc:
                _vlog.error("run_command raised %s for utterance %s", type(exc).__name__,
                            jarvis_log.current_utterance(), exc_info=True)
                raise
            if reply:
                self.ctx.say(reply)
            jarvis_log.utterance_event(jarvis_log.RESULT, step="reply", ok=reply is not None)
            if reply is not None:
                self._rearm_followup()
        finally:
            if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                try:
                    self.ctx.overlay.post("thinking", active=False)
                except Exception:
                    pass


if __name__ == "__main__":
    # Interactive CLI Diagnostic Mode
    print("=" * 65)
    print("J.A.R.V.I.S. // Voice & Microphone Diagnostics")
    print("=" * 65)

    print("\n[1] Probing Audio Input Devices:")
    all_mics = list_microphones()
    best_idx, best_name = find_best_microphone()
    for m in all_mics:
        is_rec = " [RECOMMENDED]" if m["index"] == best_idx else ""
        status = f"WORKING (RMS: {m['rms']:6.1f})" if m["working"] else "FAILED"
        virt = " [VIRTUAL CABLE]" if m["is_virtual"] else ""
        print(f"  Device {m['index']:2d}: {m['name'][:35]:<36} | {status}{virt}{is_rec}")

    print(f"\n[2] Selected Microphone for J.A.R.V.I.S.: [{best_idx}] {best_name}")

    print("\n[3] Testing Live Audio Input (speak now, 3.5s)...")
    res = test_microphone_recognition(device_index=best_idx, duration=3.5)
    print(f"  Average Audio Level : {res['avg_level']:.1f}")
    print(f"  Peak Audio Level    : {res['max_level']:.1f}")
    if res["success"]:
        print(f"  Recognition Result  : SUCCESS -> \"{res['text']}\"")
    else:
        print(f"  Recognition Result  : {res['error']}")
    print("=" * 65)
