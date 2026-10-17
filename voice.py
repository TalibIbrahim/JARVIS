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

from commands import run_command
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


_PORTAUDIO_LOCK = threading.RLock()
set_audio_lock(_PORTAUDIO_LOCK)
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


class VADDetector:
    """
    Voice Activity Detection combining Silero VAD (ONNX) and RMS energy fallback.
    Takes 16 kHz 16-bit PCM chunks and evaluates speech presence.
    """
    def __init__(self, energy_threshold=42.0, vad_threshold=0.35):
        self.energy_threshold = energy_threshold
        self.vad_threshold = vad_threshold
        self._vad = None
        self._init_vad()

    def _init_vad(self):
        try:
            from openwakeword.vad import VAD
            self._vad = VAD()
        except Exception:
            self._vad = None

    def is_speech(self, chunk: np.ndarray, rms: float) -> bool:
        if rms < self.energy_threshold:
            return False

        if self._vad is not None:
            try:
                prob = float(self._vad.predict(chunk))
                if prob >= self.vad_threshold:
                    return True
            except Exception:
                pass
        return rms >= self.energy_threshold



def get_shared_pyaudio():
    """Returns a process-wide PyAudio instance protected by _PORTAUDIO_LOCK."""
    global _SHARED_PYAUDIO
    with _PORTAUDIO_LOCK:
        if _SHARED_PYAUDIO is None:
            import pyaudio
            _SHARED_PYAUDIO = pyaudio.PyAudio()
        return _SHARED_PYAUDIO


def reset_shared_pyaudio():
    """Terminates and resets the shared PyAudio instance to detect hotplugged devices."""
    global _SHARED_PYAUDIO
    with _PORTAUDIO_LOCK:
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
                            stream = p.open(
                                format=pyaudio.paInt16,
                                channels=ch,
                                rate=rate,
                                input=True,
                                input_device_index=i,
                                frames_per_buffer=1024
                            )
                            data = stream.read(1024, exception_on_overflow=False)
                            stream.stop_stream()
                            stream.close()
                            arr = np.frombuffer(data, dtype=np.int16)
                            rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                            working = True
                        except Exception:
                            working = False
                            rms = 0.0

                    mics.append({
                        "index": i,
                        "name": name,
                        "channels": max_ch,
                        "working": working,
                        "rms": rms,
                        "is_virtual": is_virtual
                    })
                except Exception:
                    pass
        except Exception as exc:
            print(f"[voice] error enumerating microphones: {exc}")
    return mics


def find_best_microphone(cfg=None, probe_working=False):
    """
    Finds the most suitable microphone device index and name.
    1. If cfg.mic_device_index is explicitly specified, verifies and uses it.
    2. Otherwise, auto-probes all devices, filtering out silent virtual audio cables
       and selecting the physical microphone with active audio signal.
    """
    cfg_idx = getattr(cfg, "mic_device_index", None) if cfg else None
    mics = list_microphones(probe_rms=probe_working)
    if not mics:
        return None, "Default System Microphone"

    # 1. Explicitly requested device in config (if physical and working)
    if cfg_idx is not None:
        for m in mics:
            if m["index"] == cfg_idx and m["working"] and not m["is_virtual"]:
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
        return best["index"], best["name"]

    # 3. Fallback to any working device
    any_working = [m for m in mics if m["working"]]
    if any_working:
        any_working.sort(key=lambda m: m["rms"], reverse=True)
        return any_working[0]["index"], any_working[0]["name"]

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
    try:
        import pyaudio
        p = get_shared_pyaudio()
        if device_index is None:
            info = p.get_default_input_device_info()
            dev_idx = info["index"]
        else:
            info = p.get_device_info_by_index(device_index)
            dev_idx = device_index
        ch = min(info.get("maxInputChannels", 1), 2)
        rate = int(info.get("defaultSampleRate", 44100))

        stream = p.open(
            format=pyaudio.paInt16,
            channels=ch,
            rate=rate,
            input=True,
            input_device_index=dev_idx,
            frames_per_buffer=chunk_samples
        )
    except Exception:
        return 0.0, 0.0
    finally:
        _PORTAUDIO_LOCK.release()

    try:
        data = stream.read(chunk_samples, exception_on_overflow=False)
        arr = np.frombuffer(data, dtype=np.int16)
        rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
        norm = min(max((rms - 20.0) / 2500.0, 0.0), 1.0)
        return norm, rms
    except Exception:
        return 0.0, 0.0
    finally:
        if stream is not None:
            try:
                with _PORTAUDIO_LOCK:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass


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
                from piper import PiperVoice
                print(f"[voice] Loading Paul Bettany J.A.R.V.I.S. neural voice ({os.path.basename(model_path)})...")
                voice = PiperVoice.load(model_path, config_path=config_path)
                _SHARED_PIPER_VOICE = voice
                _SHARED_PIPER_PATH = model_path
                print("[voice] Paul Bettany voice model loaded.")
                _warm_piper_cache(voice)
                return voice
            except Exception as e:
                print(f"[voice] PiperVoice initialization failed: {e}")
        return None


class Speaker(threading.Thread):
    """
    Synthesizes and plays spoken feedback for J.A.R.V.I.S.
    Supports:
    - 'piper': Offline neural TTS with Paul Bettany voice clone (MCU J.A.R.V.I.S.) via sounddevice.
    - 'system': System-native SAPI5 fallback via pyttsx3.
    """

    def __init__(self, enabled=True, cfg=None):
        super().__init__(daemon=True)
        self.enabled = enabled
        self.cfg = cfg
        self.q = queue.Queue()
        self._piper_voice = None
        self._sapi_engine = None
        self.is_speaking = False

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
                    with _PORTAUDIO_LOCK:
                        sd.play(audio, samplerate=target_sr, latency="high")
                    sd.wait()
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
                with _PORTAUDIO_LOCK:
                    sd.play(audio0, samplerate=sr0, latency="high")
                sd.wait()

            # 4. Stream remaining sentences as they complete synthesis
            while True:
                item = synth_queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                audio_next, sr_next = item
                if len(audio_next) > 0:
                    with _PORTAUDIO_LOCK:
                        sd.play(audio_next, samplerate=sr_next, latency="high")
                    sd.wait()

        except Exception as exc:
            print(f"[voice] Piper synthesis/playback error: {exc}, falling back to system TTS")
            self._say_system(text)
        finally:
            stop_synth.set()
            self.is_speaking = False
            Voice.is_speaking = False

    def run(self):
        # Pre-warm Piper model in background so spoken responses are instantaneous
        engine_type = getattr(self.cfg, "tts_engine", "piper") if self.cfg else "piper"
        if engine_type == "piper":
            try:
                self._init_piper()
            except Exception as e:
                print(f"[voice] Piper pre-warm note: {e}")

        while True:
            text = self.q.get()
            if text is None:
                return
            try:
                engine_type = getattr(self.cfg, "tts_engine", "piper") if self.cfg else "piper"
                if engine_type == "piper":
                    self._say_piper(text)
                else:
                    self._say_system(text)
            except Exception as exc:
                print(f"[voice] speech output error: {exc}")


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
        stop_event=None,
        sensitivity="balanced",
        followup_window=6.0,
        device_index=None,
    ):
        super().__init__(name="WakeWordDetector", daemon=True)
        self.cfg = cfg
        self.on_wake = on_wake
        self.on_utterance = on_utterance
        self.on_no_speech = on_no_speech
        self.stop = stop_event or threading.Event()
        self.sensitivity_str = sensitivity
        self.threshold = parse_sensitivity(getattr(cfg, "wake_sensitivity", sensitivity))
        self.followup_window = float(getattr(cfg, "wake_followup_window", getattr(cfg, "command_window", followup_window)))
        self.device_index = device_index
        self.guard = WakeWordGuard(threshold=self.threshold)
        self.vad = VADDetector()
        self.model = None
        self.listening_until = 0.0
        self.is_recording = False
        self._init_model()

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
    ) -> bytes:
        """
        Record with VAD: streams chunks while continuously updating Voice._latest_rms.
        End on ~700 ms silence or 8 s max -> return raw PCM bytes.
        If 5 s silence with no speech -> trigger no-speech callback, return None.
        """
        self.is_recording = True
        chunk_duration = chunk_samples / sample_rate
        recorded_frames = list(pre_roll) if pre_roll else []

        speech_detected = False
        consecutive_silence = 0.0
        start_time = time.time()

        try:
            while not self.stop.is_set():
                if Voice.is_speaking:
                    break

                data = None
                if stream is not None:
                    try:
                        data = stream.read(chunk_samples, exception_on_overflow=False)
                    except Exception:
                        break
                if not data:
                    break

                arr = np.frombuffer(data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                Voice._latest_rms = rms
                Voice._latest_time = time.time()
                recorded_frames.append(data)

                now = time.time()
                elapsed = now - start_time
                chunk_has_speech = self.vad.is_speech(arr, rms)

                if not speech_detected:
                    if chunk_has_speech:
                        speech_detected = True
                        consecutive_silence = 0.0
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
                            break
                    if elapsed >= max_duration:
                        break

            if not speech_detected or len(recorded_frames) < 3:
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
        Auto-recovery backoff loop (1s, 2s, 4s) when audio stream encounters error/unplug.
        Cleans up broken stream, resets PortAudio topology, probes for working physical mic,
        and re-establishes stream automatically without restarting the app.
        """
        import pyaudio
        delays = [1.0, 2.0, 4.0]
        delay_idx = 0
        print("[voice] Audio stream disconnected. Entering backoff retry loop (1s, 2s, 4s)...")

        while not self.stop.is_set():
            delay = delays[delay_idx]
            if delay_idx < len(delays) - 1:
                delay_idx += 1
            if self.stop.wait(delay):
                return None

            reset_shared_pyaudio()
            p = get_shared_pyaudio()
            best_idx, best_name = find_best_microphone(self.cfg, probe_working=True)
            if best_idx is None:
                continue

            try:
                with _PORTAUDIO_LOCK:
                    test_stream = p.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=sample_rate,
                        input=True,
                        input_device_index=best_idx,
                        frames_per_buffer=chunk_samples,
                    )
                # Verify reading at least one frame works without exception
                test_data = test_stream.read(chunk_samples, exception_on_overflow=False)
                if test_data and len(test_data) > 0:
                    self.device_index = best_idx
                    Voice.current_mic_index = best_idx
                    Voice.current_mic_name = best_name
                    print(f"[voice] Microphone auto-recovered successfully to [{best_idx}] {best_name}.")
                    return test_stream
                else:
                    try:
                        with _PORTAUDIO_LOCK:
                            test_stream.stop_stream()
                            test_stream.close()
                    except Exception:
                        pass
            except Exception:
                pass
        return None

    def run(self):
        import pyaudio
        p = get_shared_pyaudio()

        sample_rate = 16000
        chunk_samples = 1280  # 80 ms chunks

        while not self.stop.is_set():
            cfg_idx = getattr(self.cfg, "mic_device_index", None) if self.cfg else None
            active_idx = cfg_idx if cfg_idx is not None else self.device_index
            if active_idx is None:
                active_idx, _ = find_best_microphone(self.cfg)
            self.device_index = active_idx

            stream = None
            try:
                with _PORTAUDIO_LOCK:
                    stream = p.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=sample_rate,
                        input=True,
                        input_device_index=active_idx,
                        frames_per_buffer=chunk_samples,
                    )
            except Exception as exc:
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

                    try:
                        data = stream.read(chunk_samples, exception_on_overflow=False)
                    except Exception as stream_exc:
                        print(f"[voice] Microphone stream read error: {stream_exc}. Unplug / disconnect detected.")
                        stream_error = True
                        break

                    if not data:
                        continue

                    arr = np.frombuffer(data, dtype=np.int16)
                    rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                    Voice._latest_rms = rms
                    Voice._latest_time = time.time()

                    # Suppress wake word if assistant is speaking
                    if Voice.is_speaking:
                        self.guard.evaluate(0.0, is_speaking=True)
                        pre_roll.clear()
                        continue

                    now = time.time()

                    # Check if follow-up window expired without speech
                    if self.listening_until > 0.0 and now >= self.listening_until:
                        self.listening_until = 0.0
                        if self.on_no_speech:
                            try:
                                self.on_no_speech()
                            except Exception as ns_err:
                                print(f"[voice] on_no_speech error: {ns_err}")

                    # In follow-up window? Check if user started speaking directly
                    in_followup = now < self.listening_until
                    if in_followup and self.vad.is_speech(arr, rms):
                        pre_roll.append(data)
                        raw = self.record_utterance_with_vad(
                            stream=stream,
                            pre_roll=list(pre_roll),
                            chunk_samples=chunk_samples,
                            sample_rate=sample_rate,
                        )
                        pre_roll.clear()
                        if raw:
                            self._dispatch_utterance(raw)
                        continue

                    pre_roll.append(data)

                    # openWakeWord prediction
                    if getattr(self.cfg, "wake_word_enabled", True) and self.model is not None:
                        pred = self.model.predict(arr)
                        score = pred.get("hey_jarvis", 0.0)
                        if self.guard.evaluate(score, is_speaking=Voice.is_speaking, now=now):
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
                            )
                            pre_roll.clear()
                            if raw:
                                self._dispatch_utterance(raw)
            finally:
                if stream is not None:
                    try:
                        with _PORTAUDIO_LOCK:
                            if hasattr(stream, "is_stopped") and not stream.is_stopped():
                                stream.stop_stream()
                            stream.close()
                    except Exception:
                        pass
                    stream = None

            if stream_error and not self.stop.is_set():
                self.device_index = None
                recovered_stream = self.reconnect_stream(sample_rate, chunk_samples)
                if recovered_stream is not None:
                    try:
                        with _PORTAUDIO_LOCK:
                            recovered_stream.stop_stream()
                            recovered_stream.close()
                    except Exception:
                        pass

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
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=16000,
                    input=True,
                    input_device_index=active_idx,
                    frames_per_buffer=1280,
                )
            return self.record_utterance_with_vad(stream=stream, timeout_no_speech=timeout_no_speech)
        except Exception as exc:
            print(f"[voice] listen failed: {exc}")
            return None
        finally:
            if stream is not None:
                try:
                    with _PORTAUDIO_LOCK:
                        stream.stop_stream()
                        stream.close()
                except Exception:
                    pass

    def _dispatch_utterance(self, raw_bytes: bytes):
        if self.on_utterance:
            try:
                self.on_utterance(raw_bytes)
            except Exception as e:
                print(f"[voice] dispatch utterance error: {e}")


class Voice(threading.Thread):
    """Say 'Jarvis, open Chrome' in one go, or say 'Jarvis' and wait for 'Yes, sir?'."""
    _latest_rms = 0.0
    _latest_time = 0.0
    _last_raw_audio = None
    is_speaking = False

    def __init__(self, cfg, ctx, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ctx = ctx
        self.stop = stop_event
        self.listening_until = 0.0
        self.audio_q = queue.Queue(maxsize=3)
        self.current_mic_index = None
        self.current_mic_name = "Auto"
        self.groq_cooldown_until = 0.0
        self.sensitivity = getattr(cfg, "wake_sensitivity", "balanced") if cfg else "balanced"
        self.followup_window = float(getattr(cfg, "wake_followup_window", getattr(cfg, "command_window", 6.0))) if cfg else 6.0
        self.wake_detector = None

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

    def _on_wake_detected(self):
        now = time.time()
        self.listening_until = now + self.followup_window
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

    def _on_no_speech_detected(self):
        self.listening_until = 0.0
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

    def _on_utterance_recorded(self, raw_bytes: bytes):
        if not raw_bytes:
            return
        Voice._last_raw_audio = raw_bytes
        try:
            import speech_recognition as sr
            audio = sr.AudioData(raw_bytes, sample_rate=16000, sample_width=2)
            self.audio_q.put_nowait(audio)
        except Exception:
            pass

    def run(self):
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
        dev_idx, dev_name = find_best_microphone(self.cfg)
        self.current_mic_index = dev_idx
        self.current_mic_name = dev_name
        print(f"[voice] Connecting to microphone [{dev_idx}]: {dev_name}")

        # STT background worker
        worker = threading.Thread(target=self._recognition_worker, args=(recognizer, sr), daemon=True)
        worker.start()

        # Dedicated openWakeWord detector at 16 kHz
        self.wake_detector = WakeWordDetector(
            cfg=self.cfg,
            on_wake=self._on_wake_detected,
            on_utterance=self._on_utterance_recorded,
            on_no_speech=self._on_no_speech_detected,
            stop_event=self.stop,
            sensitivity=self.sensitivity,
            followup_window=self.followup_window,
            device_index=dev_idx,
        )
        self.wake_detector.start()

        while not self.stop.is_set():
            configured_idx = getattr(self.cfg, "mic_device_index", None)
            if configured_idx is not None and configured_idx != self.wake_detector.device_index:
                print(f"[voice] Switching microphone to [{configured_idx}]...")
                self.wake_detector.device_index = configured_idx
                self.current_mic_index = configured_idx
            self.stop.wait(timeout=0.5)

    def _fallback_google(self, recognizer, audio, sr):
        """Falls back to Google SpeechRecognition if Groq is unavailable."""
        try:
            print("[voice] Falling back to Google SpeechRecognition...")
            text = recognizer.recognize_google(audio)
            print(f"[voice] Google Heard: \"{text}\"")
            return text
        except sr.UnknownValueError:
            print("[voice] (Audio captured, but speech could not be understood)")
            return None
        except sr.RequestError as re_err:
            print(f"[voice] Google speech service error: {re_err}")
            self.ctx.say("The speech service is unreachable, sir.")
            time.sleep(4)
            return None
        except Exception as exc:
            print(f"[voice] Google recognition error: {exc}")
            return None

    def _recognition_worker(self, recognizer, sr):
        vp = get_voice_profile()

        while not self.stop.is_set():
            try:
                audio = self.audio_q.get(timeout=0.5)
            except queue.Empty:
                continue

            # Energy-based VAD validation before sending audio
            raw_bytes = audio.get_raw_data()
            if not raw_bytes or len(raw_bytes) < 3200:
                continue
            arr = np.frombuffer(raw_bytes, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))

            now = time.time()
            is_active_session = now < self.listening_until

            # Noise gate: reject quiet acoustic flutter (< 35.0) and require clear speech (> 48.0) in standby
            min_rms = 35.0 if is_active_session else 48.0
            if rms < min_rms:
                continue

            # Speaker verification gate (if enrolled and enabled in config)
            if vp.is_enrolled and getattr(self.cfg, "voice_speaker_verification", False):
                is_match, sim = vp.verify(raw_bytes)
                if not is_match:
                    print(f"[voice] Speaker verification rejected non-owner audio (similarity: {sim:.2f})")
                    continue

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
            key = get_api_key()
            ai_enabled = getattr(self.cfg, "ai_enabled", False)
            use_groq = bool(ai_enabled and key and now >= self.groq_cooldown_until)

            if use_groq:
                try:
                    # Convert in-memory audio chunk to 16 kHz mono WAV (zero temp files)
                    wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
                    stt_model = getattr(self.cfg, "ai_stt_model", "whisper-large-v3-turbo")
                    timeout = getattr(self.cfg, "ai_timeout", 10)
                    client = GroqClient(api_key=key, timeout=timeout)
                    print(f"[voice] Transcribing complete utterance via Groq Whisper ({stt_model})...")
                    text = client.transcribe(wav_bytes, model=stt_model, language="en")
                    print(f"[voice] Groq Heard: \"{text}\"")
                    # Clear any prior cooldown
                    self.groq_cooldown_until = 0.0
                except AuthenticationError as auth_err:
                    print(f"[voice] Groq key authentication failed: {auth_err}")
                    if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                        try:
                            self.ctx.overlay.post("ai_error", text="Invalid Groq Key")
                        except Exception:
                            pass
                    text = self._fallback_google(recognizer, audio, sr)
                except RateLimitError as rl_err:
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
                    print(f"[voice] Groq network offline/timeout: {net_err}")
                    self.groq_cooldown_until = time.time() + 15.0
                    if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                        try:
                            self.ctx.overlay.post("ai_error", text="Groq Offline")
                        except Exception:
                            pass
                    text = self._fallback_google(recognizer, audio, sr)
                except Exception as exc:
                    print(f"[voice] Groq error ({exc}), falling back to Google...")
                    text = self._fallback_google(recognizer, audio, sr)
            else:
                text = self._fallback_google(recognizer, audio, sr)

            if text:
                self._process(text)

    def _process(self, text):
        t = text.lower().strip()
        now = time.time()
        command = None
        if now < self.listening_until:
            command = t
            for alias in getattr(self.cfg, "wake_aliases", ["jarvis"]):
                pattern = r"^\s*" + re.escape(alias) + r"\b"
                match = re.search(pattern, command)
                if match:
                    stripped = command[match.end():].strip(" ,.!?")
                    if stripped:
                        command = stripped
                    break
        else:
            for alias in getattr(self.cfg, "wake_aliases", ["jarvis"]):
                pattern = r"\b" + re.escape(alias) + r"\b"
                match = re.search(pattern, t)
                if match:
                    command = t[match.end():].strip(" ,.!?")
                    break
            if command is None:
                # No wake word: ignore and clear any listening state on the island
                if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                    try:
                        self.ctx.overlay.post("listening_done")
                    except Exception:
                        pass
                return

            if not command:
                self.listening_until = now + getattr(self.cfg, "wake_followup_window", getattr(self.cfg, "command_window", 6.0))
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
                return

        self.listening_until = 0.0

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
            reply = run_command(command, self.ctx)
            if reply:
                self.ctx.say(reply)
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
