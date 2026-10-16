"""
Voice command subsystem for J.A.R.V.I.S.
Handles:
- Speech recognition via Google Speech API (with local audio capture and decoupling).
- Intelligent microphone selection (filters out dead virtual cables and auto-detects active hardware microphones).
- Live audio input level probing for UI volume meters.
- Standalone microphone diagnostic test tool.
- Non-blocking text-to-speech feedback via pyttsx3.
"""
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


_PORTAUDIO_LOCK = threading.RLock()
_SHARED_PYAUDIO = None


def get_shared_pyaudio():
    """Returns a process-wide PyAudio instance protected by _PORTAUDIO_LOCK."""
    global _SHARED_PYAUDIO
    with _PORTAUDIO_LOCK:
        if _SHARED_PYAUDIO is None:
            import pyaudio
            _SHARED_PYAUDIO = pyaudio.PyAudio()
        return _SHARED_PYAUDIO


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


def find_best_microphone(cfg=None):
    """
    Finds the most suitable microphone device index and name.
    1. If cfg.mic_device_index is explicitly specified, verifies and uses it.
    2. Otherwise, auto-probes all devices, filtering out silent virtual audio cables
       and selecting the physical microphone with active audio signal.
    """
    cfg_idx = getattr(cfg, "mic_device_index", None) if cfg else None
    mics = list_microphones(probe_rms=False)
    if not mics:
        return None, "Default System Microphone"

    # 1. Explicitly requested device in config
    if cfg_idx is not None:
        for m in mics:
            if m["index"] == cfg_idx and m["working"]:
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
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        _PORTAUDIO_LOCK.release()


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
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as e:
                print(f"[voice] system speech error: {e}")

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
        if self._piper_voice is not None and getattr(self, "_loaded_model_path", None) == model_path:
            return self._piper_voice

        if not (os.path.exists(model_path) and os.path.exists(config_path)):
            ensure_piper_model(model_path, config_path)
        if os.path.exists(model_path) and os.path.exists(config_path):
            try:
                from piper import PiperVoice
                print(f"[voice] Loading Paul Bettany J.A.R.V.I.S. neural voice ({os.path.basename(model_path)})...")
                voice = PiperVoice.load(model_path, config_path=config_path)
                self._piper_voice = voice
                self._loaded_model_path = model_path
                print("[voice] Paul Bettany voice model loaded.")
                return voice
            except Exception as e:
                print(f"[voice] PiperVoice initialization failed: {e}")
        return None

    def _say_piper(self, text):
        voice = self._init_piper()
        if voice is None:
            self._say_system(text)
            return

        try:
            import sounddevice as sd
            chunks = list(voice.synthesize(text))
            if not chunks:
                return
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
            # Piper normalizes audio to 1.0 (0 dBFS), which causes clipping/crackling during playback & resampling.
            # 0.88x provides ~1.2 dB of headroom.
            audio = np.clip(audio * 0.88, -0.95, 0.95).astype(np.float32)

            # 3. Resample to hardware output device's native sample rate (e.g. 44100 / 48000 Hz)
            # Modern Windows drivers (Realtek) do not natively render 22050 Hz under MME/DirectSound.
            # Real-time driver resampling causes buffer underruns, jitter, and crackling.
            target_sr = raw_sr
            try:
                out_dev = sd.query_devices(kind="output")
                dev_sr = int(out_dev.get("default_samplerate", 44100))
                if dev_sr > 0:
                    target_sr = dev_sr
            except Exception:
                target_sr = 44100

            if raw_sr != target_sr:
                try:
                    import scipy.signal as signal
                    audio = signal.resample_poly(audio, up=target_sr, down=raw_sr).astype(np.float32)
                except Exception:
                    # Fallback linear interpolation
                    old_indices = np.linspace(0, len(audio) - 1, len(audio))
                    new_len = int(len(audio) * target_sr / raw_sr)
                    new_indices = np.linspace(0, len(audio) - 1, new_len)
                    audio = np.interp(new_indices, old_indices, audio).astype(np.float32)

            # 4. Play with high latency buffer to avoid starvation under CPU load
            with _PORTAUDIO_LOCK:
                sd.play(audio, samplerate=target_sr, latency="high")
            sd.wait()
        except Exception as exc:
            print(f"[voice] Piper synthesis/playback error: {exc}, falling back to system TTS")
            self._say_system(text)

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


class Voice(threading.Thread):
    """Say 'Jarvis, open Chrome' in one go, or say 'Jarvis' and wait for 'Yes, sir?'."""
    _latest_rms = 0.0
    _latest_time = 0.0
    _last_raw_audio = None

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

    def run(self):
        try:
            import speech_recognition as sr
        except ImportError:
            self.ctx.say("Voice control needs the SpeechRecognition package.")
            return

        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = getattr(self.cfg, "mic_dynamic_energy", False)
        recognizer.pause_threshold = getattr(self.cfg, "mic_pause_threshold", 1.0)
        recognizer.phrase_threshold = 0.3
        recognizer.non_speaking_duration = 0.5

        # Auto-detect best physical microphone
        dev_idx, dev_name = find_best_microphone(self.cfg)
        self.current_mic_index = dev_idx
        self.current_mic_name = dev_name
        print(f"[voice] Connecting to microphone [{dev_idx}]: {dev_name}")

        # Background worker for network recognition so microphone listening never stalls
        worker = threading.Thread(target=self._recognition_worker, args=(recognizer, sr), daemon=True)
        worker.start()

        while not self.stop.is_set():
            configured_idx = getattr(self.cfg, "mic_device_index", None)
            active_idx = configured_idx if configured_idx is not None else self.current_mic_index

            try:
                with _PORTAUDIO_LOCK:
                    mic = sr.Microphone(device_index=active_idx)
                self.current_mic_index = active_idx
            except Exception as exc:
                print(f"[voice] failed to initialize microphone [{active_idx}]: {exc}")
                time.sleep(1.0)
                continue

            try:
                with mic as source:
                    # Initial ambient noise calibration with strict ceiling clamp
                    try:
                        recognizer.adjust_for_ambient_noise(source, duration=0.8)
                        if recognizer.energy_threshold > 750:
                            recognizer.energy_threshold = 600
                        elif recognizer.energy_threshold < 150:
                            recognizer.energy_threshold = 250
                    except Exception as e:
                        print(f"[voice] ambient noise adjustment note: {e}")

                    # If dynamic energy is disabled, ensure configured threshold is respected
                    if not getattr(self.cfg, "mic_dynamic_energy", False):
                        recognizer.energy_threshold = getattr(self.cfg, "mic_energy_threshold", 300)

                    print(f"[voice] Listening online on device [{active_idx}] (trigger threshold: {recognizer.energy_threshold:.0f}, pause: {recognizer.pause_threshold:.1f}s)")

                    # Hook source.stream.read to monitor audio levels live without opening extra streams
                    orig_read = source.stream.read
                    def hooked_read(size):
                        chunk = orig_read(size)
                        if chunk:
                            try:
                                arr = np.frombuffer(chunk, dtype=np.int16)
                                if len(arr) > 0:
                                    Voice._latest_rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                                    Voice._latest_time = time.time()
                            except Exception:
                                pass
                        return chunk
                    source.stream.read = hooked_read

                    while not self.stop.is_set():
                        # Check if device changed in settings
                        new_cfg_idx = getattr(self.cfg, "mic_device_index", None)
                        if new_cfg_idx is not None and new_cfg_idx != self.current_mic_index:
                            print(f"[voice] Switching microphone to [{new_cfg_idx}]...")
                            break

                        # Keep thresholds synced with config
                        recognizer.dynamic_energy_threshold = getattr(self.cfg, "mic_dynamic_energy", False)
                        recognizer.pause_threshold = getattr(self.cfg, "mic_pause_threshold", 1.0)
                        if not recognizer.dynamic_energy_threshold:
                            recognizer.energy_threshold = getattr(self.cfg, "mic_energy_threshold", 300)

                        phrase_time_limit = getattr(self.cfg, "mic_phrase_time_limit", 10.0)
                        try:
                            audio = recognizer.listen(source, timeout=1, phrase_time_limit=phrase_time_limit)
                            try:
                                self.audio_q.put_nowait(audio)
                            except queue.Full:
                                pass
                        except sr.WaitTimeoutError:
                            continue
                        except Exception as loop_err:
                            print(f"[voice] listen cycle note: {loop_err}")
                            time.sleep(0.3)
                            break
            except Exception as stream_err:
                print(f"[voice] stream error: {stream_err}")
                time.sleep(0.5)

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
        else:
            for alias in self.cfg.wake_aliases:
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
                self.listening_until = now + self.cfg.command_window
                try:
                    self.ctx.overlay.post("listening", until=self.listening_until)
                except Exception:
                    pass
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

        reply = run_command(command, self.ctx)
        if reply:
            self.ctx.say(reply)


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
