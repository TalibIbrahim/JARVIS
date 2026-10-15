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
from ai import (
    get_api_key,
    GroqClient,
    AuthenticationError,
    RateLimitError,
    OfflineError,
    TimeoutError,
    AIError,
)


def list_microphones():
    """
    Enumerates all system audio input devices and probes each for functionality and RMS level.
    Returns list of dicts:
        [{'index': int, 'name': str, 'channels': int, 'working': bool, 'rms': float, 'is_virtual': bool}]
    """
    mics = []
    try:
        import pyaudio
        p = pyaudio.PyAudio()
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

                # Quick probe to test if stream can be opened and measure signal
                working = False
                rms = 0.0
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
        p.terminate()
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
    mics = list_microphones()
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
        # Add audio signal presence (devices with actual acoustic energy get priority)
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
    Safe and lightweight for non-blocking UI polling timers.
    """
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        try:
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
            stream.stop_stream()
            stream.close()
            p.terminate()

            arr = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
            # Logarithmic/perceptual normalization (0 dB to 90 dB range)
            norm = min(max((rms - 20.0) / 2500.0, 0.0), 1.0)
            return norm, rms
        except Exception:
            p.terminate()
            return 0.0, 0.0
    except Exception:
        return 0.0, 0.0


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
        r = sr.Recognizer()
        r.energy_threshold = 300
        r.dynamic_energy_threshold = True

        mic = sr.Microphone(device_index=device_index)
        levels = []
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


class Speaker(threading.Thread):
    """Runs pyttsx3 on its own thread (the engine is not thread-safe)."""

    def __init__(self, enabled=True):
        super().__init__(daemon=True)
        self.enabled = enabled
        self.q = queue.Queue()

    def say(self, text):
        if self.enabled:
            self.q.put(text)

    def run(self):
        if not self.enabled:
            return
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 180)
            for v in engine.getProperty("voices"):
                if any(n in v.name for n in ("David", "Mark", "George", "James")):
                    engine.setProperty("voice", v.id)
                    break
        except Exception as exc:
            print(f"[voice] text-to-speech unavailable: {exc}")
            return
        while True:
            text = self.q.get()
            if text is None:
                return
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass


class Voice(threading.Thread):
    """Say 'Jarvis, open Chrome' in one go, or say 'Jarvis' and wait for 'Yes, sir?'."""

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
        recognizer.dynamic_energy_threshold = getattr(self.cfg, "mic_dynamic_energy", True)
        recognizer.pause_threshold = 0.6

        # Auto-detect best physical microphone
        dev_idx, dev_name = find_best_microphone(self.cfg)
        self.current_mic_index = dev_idx
        self.current_mic_name = dev_name
        print(f"[voice] Connecting to microphone [{dev_idx}]: {dev_name}")

        try:
            mic = sr.Microphone(device_index=dev_idx)
            initial_thresh = getattr(self.cfg, "mic_energy_threshold", 400)
            recognizer.energy_threshold = initial_thresh

            with mic as source:
                try:
                    recognizer.adjust_for_ambient_noise(source, duration=0.8)
                    # Safety clamp: avoid floating into unreachable thresholds in background noise
                    if recognizer.energy_threshold > 2500:
                        recognizer.energy_threshold = 2000
                    elif recognizer.energy_threshold < 150:
                        recognizer.energy_threshold = 250
                except Exception as e:
                    print(f"[voice] ambient noise adjustment note: {e}")

            print(f"[voice] Listening online on '{dev_name}' (trigger threshold: {recognizer.energy_threshold:.0f})")
        except Exception as exc:
            print(f"[voice] failed to initialize microphone [{dev_idx}]: {exc}")
            self.ctx.say("I can't access the microphone, sir.")
            return

        # Background worker for network recognition so microphone listening never stalls
        worker = threading.Thread(target=self._recognition_worker, args=(recognizer, sr), daemon=True)
        worker.start()

        while not self.stop.is_set():
            # Check if user switched microphone device in settings
            configured_idx = getattr(self.cfg, "mic_device_index", None)
            if configured_idx is not None and configured_idx != self.current_mic_index:
                print(f"[voice] Switching microphone from [{self.current_mic_index}] to [{configured_idx}]...")
                try:
                    mic = sr.Microphone(device_index=configured_idx)
                    self.current_mic_index = configured_idx
                except Exception as switch_err:
                    print(f"[voice] error switching microphone: {switch_err}")

            try:
                with mic as source:
                    audio = recognizer.listen(source, timeout=1, phrase_time_limit=6)
                try:
                    self.audio_q.put_nowait(audio)
                except queue.Full:
                    pass  # drop older buffers if network lags to keep responses fresh
            except sr.WaitTimeoutError:
                continue
            except Exception:
                time.sleep(0.5)
                continue

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
            if rms < 30.0:
                continue  # Discard silent acoustic flutter to protect quota

            text = None
            key = get_api_key()
            ai_enabled = getattr(self.cfg, "ai_enabled", False)
            now = time.time()
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
                return  # no wake word: ignore
            if not command:
                self.listening_until = now + self.cfg.command_window
                try:
                    self.ctx.overlay.post("listening", until=self.listening_until)
                except Exception:
                    pass
                self.ctx.say("Yes, sir?")
                return

        self.listening_until = 0.0
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
