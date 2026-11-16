"""
Voice command subsystem for J.A.R.V.I.S.
Handles:
- Speech recognition via local faster-whisper, then Groq Whisper (with local audio capture and decoupling).
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
)
from chime import (
    play_chime,
    set_audio_lock,
)


import jarvis_log
from jarvis_log import redact
import fullscreen_detector

_vlog = jarvis_log.get_logger("voice")

# ---------------------------------------------------------------------------------------------------
# QUIET RULE (games / presentations / Focus Assist). fullscreen_detector.is_quiet() is True for
# QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE and QUNS_QUIET_TIME. While quiet:
#   * the wake chime / wake earcon is OFF (including the push-to-talk chime);
#   * spoken (TTS) replies are OFF, EXCEPT
#       - replies within EXPLICIT_COMMAND_WINDOW_S (10 s) of a command the user explicitly issued
#         ("Jarvis ..." / follow-up / push-to-talk) -- see note_explicit_command();
#       - urgent speech: timers, alarms and reminders (say(..., urgent=True) or a scheduler phrase).
#   * proactive / unsolicited speech (gesture mode announcements, briefings, tips, ...) is OFF.
# Text is still posted to the island; only audio is suppressed. The island hiding behavior is unchanged.
# ---------------------------------------------------------------------------------------------------
EXPLICIT_COMMAND_WINDOW_S = 10.0
_URGENT_SPEECH_RE = re.compile(r"^\s*sir,\s+(your\s+(timer|alarm)\s+for\s|reminder:)", re.IGNORECASE)
_last_explicit_command_t = -1e9


def _quiet() -> bool:
    return fullscreen_detector.is_quiet()


def note_explicit_command(now=None):
    """Called when the user explicitly addressed JARVIS (accepted command / push-to-talk)."""
    global _last_explicit_command_t
    _last_explicit_command_t = time.monotonic() if now is None else now


def speech_allowed(text, urgent=False, now=None) -> bool:
    """True if `text` may be spoken now under the quiet rule above."""
    if not _quiet():
        return True
    if urgent or (text and _URGENT_SPEECH_RE.match(text)):
        return True
    t = time.monotonic() if now is None else now
    return (t - _last_explicit_command_t) <= EXPLICIT_COMMAND_WINDOW_S


def _wake_chime_allowed() -> bool:
    return not _quiet()

_PORTAUDIO_LOCK = jarvis_log.InstrumentedRLock("portaudio", warn_wait_ms=50, warn_hold_ms=250)


def _import_openwakeword_lean():
    """Import openwakeword without its unused training module.

    openwakeword/__init__.py imports openwakeword.custom_verifier_model, which
    pulls in sklearn and scipy.stats (~3 s and ~69 MB RSS) only to train custom
    verifiers, which JARVIS never does. Pre-register a minimal stub so that
    import is skipped. openwakeword.model does not reference the module.
    """
    name = "openwakeword.custom_verifier_model"
    if name not in sys.modules:
        import types

        def train_custom_verifier(*_a, **_k):
            raise NotImplementedError("custom verifier models are not supported in JARVIS")

        stub = types.ModuleType(name)
        stub.train_custom_verifier = train_custom_verifier
        sys.modules[name] = stub
    import openwakeword
    return openwakeword

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
    "aggressive": 0.35,   # legacy name for "high"
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
# Optional greeting before a wake alias at the start of a command ("Hey Jarvis, ...", "OK Jarvis ...").
_WAKE_PREFIX_RE = r"(?:(?:hey|hi|ok|okay)[\s,]+)?"


def _get_personal():
    """The personal-learning module (clips, corrections, vocabulary), or None when it is not available."""
    try:
        import personal
        return personal
    except Exception:
        return None


def _level_01(rms: float) -> float:
    """Mic RMS (int16 units) -> 0..1 for level meters: -60 dBFS maps to 0, 0 dBFS to 1."""
    if rms <= 0:
        return 0.0
    db = 20.0 * math.log10(max(rms, 1e-9) / 32768.0)
    return float(min(1.0, max(0.0, (db + 60.0) / 60.0)))


def analyze_levels(pcm16: bytes, chunk: int = 1280) -> dict:
    """Level metrics of a 16 kHz int16 recording: speech level (RMS of the louder half of 80 ms chunks),
    noise floor (10th percentile chunk RMS), both in dBFS, and the SNR between them."""
    arr = np.frombuffer(pcm16, dtype=np.int16).astype(np.float64)
    n = len(arr) // chunk
    if n < 2:
        return {"level_db": -120.0, "noise_db": -120.0, "snr_db": 0.0, "speech_rms": 0.0, "noise_rms": 0.0}
    rms = np.sqrt(np.mean(arr[:n * chunk].reshape(n, chunk) ** 2, axis=1))
    noise = float(np.percentile(rms, 10))
    loud = rms[rms >= np.median(rms)]
    speech = float(np.sqrt(np.mean(loud ** 2)))

    def db(x):
        return 20.0 * math.log10(max(x, 1.0) / 32768.0)
    return {"level_db": round(db(speech), 1), "noise_db": round(db(noise), 1),
            "snr_db": round(db(speech) - db(noise), 1), "speech_rms": speech, "noise_rms": noise}


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
            _import_openwakeword_lean()
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

    def is_speech(self, chunk: np.ndarray, rms: float, gate_override: float = None) -> bool:
        prob = None
        if self._vad is not None:
            try:
                prob = float(self._vad.predict(chunk))
            except Exception as exc:
                self._warn_fallback(f"Silero predict raised: {exc!r}")
        self.last_prob = prob

        gate = self.gate() if gate_override is None else float(gate_override)  # from history, before this chunk
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


# ---------------------------------------------------------------------------------------------------------
# Microphone identity. PortAudio lists every physical mic once per host API (MME, DirectSound, WASAPI, WDM-KS)
# and its raw indices change with devices and builds, so a mic is identified by NAME. One entry per mic,
# with its usable host-API "variants" ordered WASAPI (native rate, resampled), MME, DirectSound; never WDM-KS.
# ---------------------------------------------------------------------------------------------------------
MIC_VERIFY_TIMEOUT_S = 1.0    # a variant only counts as working when a full chunk arrives within this long
VARIANT_MAX_FAILS = 2         # stalls/no-data opens after which a variant is skipped until a device change
_MME_NAME_MAX = 31            # MME truncates device names to 31 characters
_VIRTUAL_NAME_KEYS = ("virtual", "cable", "mapper", "stereo mix", "wave out", "loopback", "primary sound capture")

_VARIANT_FAILS = {}           # (normalized name prefix, host rank) -> consecutive failures (guarded by _PORTAUDIO_LOCK)
LAST_MIC_CHOICE = None        # {"index", "name", "host_api", "reason"} of the last find_best_microphone(require_16k=True)


def _norm_mic_name(name):
    return " ".join(str(name or "").split()).casefold()


def _names_match(a, b):
    """Same physical mic: equal normalized names, or one is an MME-truncated (~31 char) prefix of the other."""
    na, nb = _norm_mic_name(a), _norm_mic_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= _MME_NAME_MAX - 3 and long_.startswith(short)


def _host_rank(host_api):
    """WASAPI 0, MME 1, DirectSound 2, anything else 3; None = never used (WDM-KS)."""
    h = str(host_api).lower()
    if "wdm" in h:
        return None
    if "wasapi" in h:
        return 0
    if "mme" in h:
        return 1
    if "directsound" in h or "direct sound" in h:
        return 2
    return 3


def _variant_key(name, host_api):
    return (_norm_mic_name(name)[:_MME_NAME_MAX - 3], _host_rank(host_api))


def note_variant_failure(name, host_api):
    """A mic variant opened but delivered no data (or failed to open). Returns its consecutive failure count."""
    with _PORTAUDIO_LOCK:
        k = _variant_key(name, host_api)
        _VARIANT_FAILS[k] = _VARIANT_FAILS.get(k, 0) + 1
        return _VARIANT_FAILS[k]


def _note_variant_ok(name, host_api):
    with _PORTAUDIO_LOCK:
        _VARIANT_FAILS.pop(_variant_key(name, host_api), None)


def _variant_fails(name, host_api):
    with _PORTAUDIO_LOCK:
        return _VARIANT_FAILS.get(_variant_key(name, host_api), 0)


def reset_variant_failures():
    with _PORTAUDIO_LOCK:
        _VARIANT_FAILS.clear()


def group_input_devices(devs):
    """
    devs: [{"index", "name", "host_api", "channels", "rate", "loopback"(opt)}] (raw PortAudio inputs).
    Returns one dict per physical mic, in first-seen order:
        {"index", "name", "channels", "working", "rms", "is_virtual", "host_api", "variants": [...]}
    where index/host_api are those of the preferred variant, name is the longest (untruncated) name seen,
    and variants are ordered by host API preference (WASAPI, MME, DirectSound; WDM-KS and loopback dropped).
    """
    groups = []
    for d in devs:
        if d.get("loopback"):
            continue
        rank = _host_rank(d["host_api"])
        if rank is None:
            continue
        var = {"index": d["index"], "host_api": d["host_api"], "rank": rank,
               "channels": d.get("channels", 1), "rate": d.get("rate", 44100)}
        for g in groups:
            if _names_match(g["name"], d["name"]):
                g["variants"].append(var)
                if len(_norm_mic_name(d["name"])) > len(_norm_mic_name(g["name"])):
                    g["name"] = d["name"]
                break
        else:
            groups.append({"name": d["name"], "variants": [var]})
    mics = []
    for g in groups:
        g["variants"].sort(key=lambda v: v["rank"])
        best = g["variants"][0]
        mics.append({
            "index": best["index"],
            "name": g["name"],
            "channels": best["channels"],
            "working": True,
            "rms": 0.0,
            "is_virtual": any(k in g["name"].lower() for k in _VIRTUAL_NAME_KEYS),
            "host_api": best["host_api"],
            "variants": g["variants"],
        })
    return mics


def find_mic_by_name(mics, name):
    """The grouped mic whose name matches `name` (see _names_match), or None. "" never matches."""
    if not name:
        return None
    for m in mics:
        if _names_match(m["name"], name):
            return m
    return None


def _clean_device_name(name):
    """PyAudio hands back UTF-8 device names decoded as cp1252 ("IntelÂ®"); undo that so names display and
    compare correctly. Names that are not such mojibake are returned unchanged."""
    name = str(name)
    if "Â" in name or "Ã" in name:
        try:
            return name.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name
    return name


def _enumerate_input_devices(p):
    devs = []
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) <= 0:
                continue
            try:
                host_api = p.get_host_api_info_by_index(info["hostApi"])["name"]
            except Exception:
                host_api = "?"
            devs.append({"index": i, "name": _clean_device_name(info.get("name", f"Microphone {i}")),
                         "host_api": host_api,
                         "channels": int(info.get("maxInputChannels", 1)),
                         "rate": int(info.get("defaultSampleRate", 44100)),
                         "loopback": bool(info.get("isLoopbackDevice", False))})
        except Exception as exc:
            _vlog.warning("list_microphones: device %s skipped: %r", i, exc)
    return devs


def mic_name_for_index(idx):
    """Full name of the input device at raw index `idx` (None if it is not an input device). Used by the
    one-time config migration from mic_device_index to mic_device_name."""
    with _PORTAUDIO_LOCK:
        devs = _enumerate_input_devices(get_shared_pyaudio())
    for d in devs:
        if d["index"] == idx:
            return max((x["name"] for x in devs if _names_match(x["name"], d["name"])), key=len)
    return None


def list_microphones(probe_rms=False):
    """
    One entry per physical input device (see group_input_devices), using the shared PyAudio instance.
    Returns list of dicts:
        [{'index': int, 'name': str, 'channels': int, 'working': bool, 'rms': float, 'is_virtual': bool,
          'host_api': str, 'variants': [{'index','host_api','rank','channels','rate'}, ...]}]
    probe_rms=True also opens the preferred variant to confirm it delivers frames (working) and read its level.
    """
    mics = []
    try:
        with _PORTAUDIO_LOCK:
            mics = group_input_devices(_enumerate_input_devices(get_shared_pyaudio()))
    except Exception as exc:
        _vlog.warning("list_microphones: enumeration failed: %r", exc)
        print(f"[voice] error enumerating microphones: {exc}")
    if probe_rms and mics:
        p = get_shared_pyaudio()
        for m in mics:
            ok, _detail, rms = probe_16k_open(p, m["index"])
            m["working"], m["rms"] = ok, rms
        _vlog.info("list_microphones probe: mics=%d working=%s", len(mics),
                   [(m["index"], round(m["rms"], 1)) for m in mics if m["working"]])
    return mics


def resolve_mic_index(cfg):
    """Raw index of the preferred host-API variant of the configured mic (cfg.mic_device_name), or None for
    auto-detect / not currently present. No device is opened."""
    m = find_mic_by_name(list_microphones(), getattr(cfg, "mic_device_name", "") if cfg else "")
    return m["index"] if m else None


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


class _ResampledStream:
    """16 kHz mono view of a native-rate (WASAPI shared-mode) input stream: channels are averaged and the audio
    is band-limited (windowed-sinc FIR) and interpolated down with state carried across reads, so successive
    80 ms chunks join seamlessly. Quacks like a PyAudio stream for the pipeline (read / get_read_available /
    stop_stream / close / is_stopped)."""
    TARGET = 16000
    TAPS = 193

    def __init__(self, raw, rate, channels, chunk_samples=1280):
        self.raw, self.rate, self.channels = raw, int(rate), max(1, int(channels))
        self._native_chunk = int(round(chunk_samples * self.rate / self.TARGET))
        self._step = self.rate / float(self.TARGET)
        fc = 0.45 * self.TARGET / self.rate                      # cycles/sample; keeps the band below 7.2 kHz
        t = np.arange(self.TAPS) - (self.TAPS - 1) / 2.0
        h = 2.0 * fc * np.sinc(2.0 * fc * t) * np.blackman(self.TAPS)
        self._h = (h / h.sum()).astype(np.float64)
        self._tail = np.zeros(self.TAPS - 1)
        self._last = 0.0
        self._pos = 1.0
        self._fifo = np.zeros(0, dtype=np.float64)

    def _process(self, x):
        buf = np.concatenate([self._tail, x])
        filt = np.convolve(buf, self._h, mode="valid")           # len(x) samples, state-continuous
        self._tail = buf[-(self.TAPS - 1):]
        ext = np.concatenate([[self._last], filt])
        n = max(int(np.floor((len(ext) - 1 - self._pos) / self._step)) + 1, 0)
        pos = self._pos + self._step * np.arange(n)
        out = np.interp(pos, np.arange(len(ext)), ext)
        self._pos = self._pos + self._step * n - (len(ext) - 1)
        self._last = float(filt[-1])
        return out

    def read(self, n, exception_on_overflow=False):
        while len(self._fifo) < n:
            data = self.raw.read(self._native_chunk, exception_on_overflow=exception_on_overflow)
            x = np.frombuffer(data, dtype=np.int16).astype(np.float64)
            if self.channels > 1:
                x = x[: len(x) // self.channels * self.channels].reshape(-1, self.channels).mean(axis=1)
            self._fifo = np.concatenate([self._fifo, self._process(x)])
        out, self._fifo = self._fifo[:n], self._fifo[n:]
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16).tobytes()

    def get_read_available(self):
        return len(self._fifo) + int(self.raw.get_read_available() * self.TARGET / self.rate)

    def is_stopped(self):
        return self.raw.is_stopped()

    def stop_stream(self):
        return self.raw.stop_stream()

    def close(self):
        return self.raw.close()


def _open_input(p, idx, chunk_samples=1280, opener=None):
    """
    Opens raw device `idx` as a 16 kHz mono stream. WASAPI shared mode only opens at the device's native rate
    and channel count (-9997 / -9998 otherwise), so it is opened that way and wrapped in _ResampledStream;
    MME / DirectSound open at 16 kHz mono directly. idx None = the Windows default input.
    """
    import pyaudio
    opener = opener or _shared_open
    if idx is None:
        return opener(p, format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                      input_device_index=None, frames_per_buffer=chunk_samples)
    info = p.get_device_info_by_index(idx)
    host = str(p.get_host_api_info_by_index(info["hostApi"])["name"]).lower()
    if "wasapi" in host:
        rate = int(info.get("defaultSampleRate", 48000))
        ch = max(1, int(info.get("maxInputChannels", 1)))
        native_chunk = int(round(chunk_samples * rate / 16000.0))
        raw = opener(p, format=pyaudio.paInt16, channels=ch, rate=rate, input=True, input_device_index=idx,
                     frames_per_buffer=native_chunk)
        return _ResampledStream(raw, rate, ch, chunk_samples)
    return opener(p, format=pyaudio.paInt16, channels=1, rate=16000, input=True, input_device_index=idx,
                  frames_per_buffer=chunk_samples)


def _stream_delivers(stream, chunk_samples, timeout_s=None):
    """(ok, data): ok when a full chunk arrives within timeout_s. Frames are counted, not their level:
    silence is fine, NO DATA is not. The blocking read only happens once the chunk is available."""
    deadline = time.monotonic() + (MIC_VERIFY_TIMEOUT_S if timeout_s is None else timeout_s)
    while True:
        avail = stream.get_read_available()
        if not isinstance(avail, (int, np.integer)) or avail >= chunk_samples:
            break
        if time.monotonic() >= deadline:
            return False, b""
        time.sleep(0.01)
    data = stream.read(chunk_samples, exception_on_overflow=False)
    return bool(data), data


def probe_16k_open(p, idx, chunk_samples=1280):
    """
    Opens raw device idx as 16 kHz mono (see _open_input) and requires a full chunk within
    MIC_VERIFY_TIMEOUT_S. Lock is held only for open and close, never during the wait.
    Returns (ok, detail, rms).
    """
    stream = None
    try:
        stream = _open_input(p, idx, chunk_samples)
        ok, data = _stream_delivers(stream, chunk_samples)
        if not ok:
            return False, f"no data within {MIC_VERIFY_TIMEOUT_S:.1f}s (0 frames)", 0.0
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


def _verified_variant(p, m):
    """First variant of grouped mic `m` that delivers frames: fewest recent failures first, then WASAPI, MME,
    DirectSound. Variants already failed VARIANT_MAX_FAILS times are skipped. Returns the variant dict or None."""
    order = sorted(m["variants"], key=lambda v: (_variant_fails(m["name"], v["host_api"]), v["rank"]))
    for v in order:
        fails = _variant_fails(m["name"], v["host_api"])
        if fails >= VARIANT_MAX_FAILS:
            _vlog.info("find_best_microphone[16k]: skip idx=%s hostApi=%s name=%r (failed %d times)",
                       v["index"], v["host_api"], m["name"], fails)
            continue
        ok, detail, rms = probe_16k_open(p, v["index"])
        (_vlog.info if ok else _vlog.warning)(
            "find_best_microphone[16k]: candidate idx=%s hostApi=%s name=%r -> %s%s", v["index"], v["host_api"],
            m["name"], "OK frames" if ok else "FAIL", f" rms={rms:.0f}" if ok else f" ({detail})")
        if ok:
            _note_variant_ok(m["name"], v["host_api"])
            return v
        note_variant_failure(m["name"], v["host_api"])
    return None


def _find_best_microphone_16k(cfg):
    """
    Pipeline mic selection. A variant must deliver frames within ~1 s of being opened (no data = not working).
    Order: the configured mic by name (its host-API variants in preference order), the Windows default input
    (if not virtual), then name-ranked mics. Never WDM-KS. Every result is logged; the winner is also left in
    LAST_MIC_CHOICE. Returns (raw index of the working variant, mic name).
    """
    global LAST_MIC_CHOICE
    LAST_MIC_CHOICE = None
    mics = list_microphones()
    if not mics:
        _vlog.warning("find_best_microphone[16k]: no input devices")
        return None, "Default System Microphone"
    p = get_shared_pyaudio()
    cfg_name = getattr(cfg, "mic_device_name", "") if cfg else ""
    tried = set()

    def _attempt(m, reason):
        global LAST_MIC_CHOICE
        tried.add(id(m))
        v = _verified_variant(p, m)
        if v is None:
            return None
        LAST_MIC_CHOICE = {"index": v["index"], "name": m["name"], "host_api": v["host_api"], "reason": reason}
        _vlog.info("find_best_microphone[16k]: reason=%s idx=%s hostApi=%s name=%r", reason, v["index"],
                   v["host_api"], m["name"])
        return v["index"], m["name"]

    for attempt_round in (0, 1):
        # 1. Explicit user choice, by name
        if cfg_name:
            m = find_mic_by_name(mics, cfg_name)
            if m is None:
                _vlog.warning("find_best_microphone[16k]: configured mic %r not present; auto-detecting", cfg_name)
            elif m["is_virtual"]:
                _vlog.warning("find_best_microphone[16k]: configured mic %r is virtual; auto-detecting", cfg_name)
            else:
                res = _attempt(m, "configured")
                if res:
                    return res
                _vlog.warning("find_best_microphone[16k]: configured mic %r delivered no data on any host API; "
                              "auto-detecting", cfg_name)
        else:
            # 2. The Windows default input device
            try:
                dflt = p.get_default_input_device_info()
            except Exception as exc:
                dflt = None
                _vlog.warning("find_best_microphone[16k]: no default input device: %r", exc)
            if dflt is not None:
                for m in mics:
                    if any(v["index"] == dflt.get("index") for v in m["variants"]):
                        if m["is_virtual"]:
                            _vlog.info("find_best_microphone[16k]: default input %r is virtual; skipped", m["name"])
                        else:
                            res = _attempt(m, "windows_default")
                            if res:
                                return res
                        break
        # 3. Ranking: physical mics first, then virtual ones
        for allow_virtual in (False, True):
            cands = [m for m in mics if m["is_virtual"] == allow_virtual and id(m) not in tried]
            cands.sort(key=lambda m: -_rank_mic_by_name(m))
            for m in cands:
                res = _attempt(m, "ranked")
                if res:
                    return res
        if attempt_round == 0 and _VARIANT_FAILS:
            _vlog.warning("find_best_microphone[16k]: nothing delivered frames; clearing failure marks, retrying once")
            reset_variant_failures()
            tried.clear()
        else:
            break
    _vlog.warning("find_best_microphone[16k]: no candidate delivered audio frames")
    return None, mics[0]["name"]


def find_best_microphone(cfg=None, probe_working=False, require_16k=False):
    """
    Finds the most suitable microphone and returns (raw device index of its preferred host-API variant, name).
    require_16k=True (pipeline use, not for the GUI thread): only a variant that actually delivers frames is
    returned (see _find_best_microphone_16k).
    Otherwise (cheap, nothing is opened unless probe_working):
    1. If cfg.mic_device_name is set and present, uses that mic.
    2. Otherwise ranks the physical, non-virtual microphones by name keywords (and level when probed).
    """
    if require_16k:
        return _find_best_microphone_16k(cfg)
    cfg_name = getattr(cfg, "mic_device_name", "") if cfg else ""
    mics = list_microphones(probe_rms=probe_working)
    if not mics:
        _vlog.warning("find_best_microphone: no input devices (probe_working=%s)", probe_working)
        return None, "Default System Microphone"

    m = find_mic_by_name(mics, cfg_name)
    if m is not None and m["working"] and not m["is_virtual"]:
        _vlog.info("find_best_microphone: reason=configured idx=%s name=%r", m["index"], m["name"])
        return m["index"], m["name"]

    def rank_mic(m):
        score = _rank_mic_by_name(m)
        if m["rms"] > 5.0:
            score += 50.0 + min(m["rms"] / 100.0, 30.0)
        return score

    physical_working = [m for m in mics if m["working"] and not m["is_virtual"]]
    if physical_working:
        physical_working.sort(key=rank_mic, reverse=True)
        best = physical_working[0]
        _vlog.info("find_best_microphone: reason=ranked idx=%s name=%r cfg_name=%r", best["index"], best["name"],
                   cfg_name)
        return best["index"], best["name"]

    any_working = [m for m in mics if m["working"]]
    if any_working:
        any_working.sort(key=lambda m: m["rms"], reverse=True)
        _vlog.info("find_best_microphone: reason=fallback idx=%s name=%r", any_working[0]["index"],
                   any_working[0]["name"])
        return any_working[0]["index"], any_working[0]["name"]

    _vlog.warning("find_best_microphone: reason=fallback none working (probe_working=%s)", probe_working)
    return None, mics[0]["name"]


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
            dev_idx = own.get_default_input_device_info()["index"]
        else:
            dev_idx = device_index
        stream = _open_input(own, dev_idx, chunk_samples, opener=lambda pa, **kw: pa.open(**kw))
        rate, ch = 16000, 1
        _log_mic_open(own, dev_idx, rate, ch, chunk_samples, "OK", "probe_mic_level")
    except Exception as exc:
        _vlog.warning("mic open (probe_mic_level) FAILED: idx=%s fpb=%s err=%r", device_index, chunk_samples, exc)
        if own is not None:
            own.terminate()
        return 0.0, 0.0
    finally:
        _PORTAUDIO_LOCK.release()

    try:
        ok, data = _stream_delivers(stream, chunk_samples)
        if not ok:
            _vlog.warning("probe_mic_level: no data from idx=%s within %.1fs", device_index, MIC_VERIFY_TIMEOUT_S)
            return 0.0, 0.0
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


def _pcm16_wav(raw_bytes, rate=16000):
    """16-bit mono PCM -> WAV file bytes (the container Groq Whisper uploads expect)."""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(raw_bytes)
    return buf.getvalue()


def _local_stt_for(cfg):
    """The local transcriber when cfg.stt_engine == "local" and its model is loaded, else None."""
    if str(getattr(cfg, "stt_engine", "local")).lower() != "local":
        return None
    import local_stt
    lt = local_stt.get_local_transcriber(getattr(cfg, "local_stt_model", "base.en"))
    return lt if lt.available() else None


def _record_pcm16(device_index, duration, level_cb=None, chunk=1024):
    """Record `duration` seconds of 16 kHz mono PCM16 from a private PyAudio instance (WASAPI is opened at its
    native rate and resampled by _open_input). level_cb(0..1) is called once per chunk. Raises RuntimeError
    ("no_audio") when the device delivers no frames within MIC_VERIFY_TIMEOUT_S."""
    import pyaudio
    rate = 16000
    with _PORTAUDIO_LOCK:
        own = pyaudio.PyAudio()
        try:
            dev = device_index if device_index is not None else own.get_default_input_device_info()["index"]
            stream = _open_input(own, dev, chunk, opener=lambda pa, **kw: pa.open(**kw))
            _log_mic_open(own, dev, rate, 1, chunk, "OK", "test_microphone_recognition")
        except Exception:
            own.terminate()
            raise
    frames = []
    try:
        ok, first = _stream_delivers(stream, chunk)
        if not ok:
            raise RuntimeError("no_audio")
        frames.append(first)
        for _ in range(max(0, int(duration * rate / chunk) - 1)):
            data = stream.read(chunk, exception_on_overflow=False)
            frames.append(data)
            if level_cb is not None:
                a = np.frombuffer(data, dtype=np.int16).astype(float)
                level_cb(min(max((float(np.sqrt(np.mean(a ** 2))) - 20.0) / 2500.0, 0.0), 1.0))
    finally:
        with _PORTAUDIO_LOCK:
            try:
                stream.stop_stream()
                stream.close()
            except Exception as exc:
                _vlog.warning("test_microphone_recognition close failed: %r", exc)
            own.terminate()
    return b"".join(frames)


def test_microphone_recognition(device_index=None, duration=3.5, level_cb=None):
    """
    Records audio for duration seconds from device_index, computes energy levels,
    and transcribes it with the local speech model (the audio never leaves the PC).
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
        raw_bytes = _record_pcm16(device_index, duration, level_cb)
        if raw_bytes:
            arr = np.frombuffer(raw_bytes, dtype=np.int16)
            rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
            result["avg_level"] = rms
            result["max_level"] = float(np.max(np.abs(arr)))

        import local_stt
        try:
            from config import load_config
            model = getattr(load_config(), "local_stt_model", "base.en")
        except Exception:
            model = "base.en"
        lt = local_stt.get_local_transcriber(model)
        lt.start_loading()
        deadline = time.monotonic() + 20.0
        while lt.state == local_stt.LOADING and time.monotonic() < deadline:
            time.sleep(0.2)
        if not lt.available():
            result["error"] = "Audio recorded, but the local speech model is not ready yet."
            return result
        recognized_text, _conf = lt.transcribe(raw_bytes)
        if recognized_text:
            result["success"] = True
            result["text"] = recognized_text
        else:
            result["error"] = "Audio recorded, but speech could not be understood (try speaking closer)."

    except Exception as exc:
        result["error"] = ("No audio from the microphone. Pick another one in Input Microphone."
                           if str(exc) == "no_audio" else str(exc))

    return result


_SHARED_PIPER_VOICE = None
_SHARED_PIPER_LOCK = threading.RLock()
_SHARED_PIPER_PATH = None
_SHARED_PIPER_SYN = None      # synthesis overrides for the loaded voice (None -> PIPER_SYN_PARAMS)
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


_RE_SPELLED_NAME = re.compile(r"\bJ[.\s]{1,2}A[.\s]{1,2}R[.\s]{1,2}V[.\s]{1,2}I[.\s]{1,2}S\b(\.)?", re.I)


def _speech_text(text: str) -> str:
    """Speech-only rewrite: TTS spells out the dotted "J.A.R.V.I.S.", so it is said as "Jarvis". Plain "Jarvis"
    or "JARVIS" is untouched; the acronym's dot is kept only where a sentence really ends."""
    def repl(m):
        rest = m.string[m.end():]
        ends = m.group(1) and (not rest.strip() or (rest[:1].isspace() and rest.lstrip()[:1] in "\"'(" + "0123456789"
                                                    or rest[:1].isspace() and rest.lstrip()[:1].isupper()))
        return "Jarvis." if ends else "Jarvis"
    return _RE_SPELLED_NAME.sub(repl, text)


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


# Piper settings picked by listening tests on the J.A.R.V.I.S. voice ("piper_fixed A"): lower noise scales
# remove the choppy, warbling delivery of its defaults; a slightly longer length_scale is a touch calmer.
# Other registry voices synthesize with their own .json inference defaults, which rhasspy tuned per voice.
PIPER_SYN_PARAMS = {"noise_scale": 0.333, "noise_w_scale": 0.333, "length_scale": 1.05}
TUNED_SYN_VOICES = ("jarvis",)


def syn_params_for(voice_id) -> dict:
    """Synthesis overrides for a registry voice: the tuned set for J.A.R.V.I.S. (and custom model files),
    {} for every other voice so Piper falls back to that model's own inference defaults."""
    return dict(PIPER_SYN_PARAMS) if voice_id is None or voice_id in TUNED_SYN_VOICES else {}
REPLY_PAUSE_S = 0.18        # silence between sentences of one reply
REPLY_FADE_IN_S = 0.005     # fade only at the very start ...
REPLY_FADE_OUT_S = 0.012    # ... and the very end of a reply


def _piper_syn_config():
    try:
        from piper import SynthesisConfig
        return SynthesisConfig(**(PIPER_SYN_PARAMS if _SHARED_PIPER_SYN is None else _SHARED_PIPER_SYN))
    except Exception as exc:
        _vlog.warning("piper SynthesisConfig unavailable (%r); synthesizing with library defaults", exc)
        return None


def _iter_reply_audio(voice, text: str, target_sr: int = None):
    """Synthesize a whole reply in ONE Piper call and yield (float32 audio, sample_rate) pieces to be
    played back-to-back on a single stream. Piper's synthesize() is a generator that yields one chunk per
    sentence, so the first piece is ready after the first sentence while later ones are still being made.
    Pieces: 0.88x headroom (clipped to +/-0.95), resampled to the device rate, REPLY_PAUSE_S of silence
    between sentences, a fade only at the start of the first and the end of the last sentence."""
    if voice is None or not text.strip():
        return
    gen = iter(voice.synthesize(text, syn_config=_piper_syn_config()))
    held = None      # tail of the previous sentence; faded only if it turns out to be the last
    sr = target_sr
    first = True
    while True:
        with _SHARED_PIPER_LOCK:
            chunk = next(gen, None)
        if chunk is None:
            break
        raw = chunk.audio_float_array
        if len(raw) == 0:
            continue
        raw_sr = chunk.sample_rate
        if sr is None:
            sr = _get_output_sample_rate(default=raw_sr)
        arr = np.clip(raw.astype(np.float32) * 0.88, -0.95, 0.95).astype(np.float32)
        if raw_sr != sr:
            arr = _resample_fft(arr, raw_sr, sr)
        if first:
            n = min(int(sr * REPLY_FADE_IN_S), len(arr))
            if n > 1:
                arr[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)
            first = False
        if held is not None:
            yield held, sr
            yield np.zeros(int(sr * REPLY_PAUSE_S), dtype=np.float32), sr
        n_tail = min(int(sr * REPLY_FADE_OUT_S), len(arr))
        if len(arr) > n_tail:
            yield arr[:-n_tail], sr
        held = arr[len(arr) - n_tail:].copy()
    if held is not None and len(held):
        held *= np.linspace(1.0, 0.0, len(held), dtype=np.float32)
        yield held, sr


def _synthesize_sentence_audio(voice, text_sentence: str, target_sr: int = None) -> tuple:
    """Whole-text synthesis joined into ONE continuous buffer (see _iter_reply_audio).
    Returns (audio_float_array, sample_rate)."""
    pieces = []
    sr = target_sr
    for audio, sr in _iter_reply_audio(voice, text_sentence, target_sr):
        pieces.append(audio)
    if not pieces:
        return np.array([], dtype=np.float32), (target_sr or 44100)
    return np.concatenate(pieces).astype(np.float32), sr


def _resample_fft(audio, src_sr, dst_sr):
    """Band-limited (Fourier) resample of a whole clip with numpy only (scipy.signal costs ~75 MB RSS and
    ~2 s of import at startup). Equivalent to scipy.signal.resample for finite TTS clips, which start and
    end in silence so the periodic-extension edge effect is inaudible."""
    n_in = len(audio)
    n_out = int(round(n_in * float(dst_sr) / float(src_sr)))
    if n_in == 0 or n_out == 0 or n_out == n_in:
        return audio.astype(np.float32)
    spec = np.fft.rfft(audio.astype(np.float64))
    n_keep = min(len(spec), n_out // 2 + 1)
    out_spec = np.zeros(n_out // 2 + 1, dtype=np.complex128)
    out_spec[:n_keep] = spec[:n_keep]
    if n_out < n_in and n_out % 2 == 0:
        out_spec[n_keep - 1] *= 0.5     # fold the Nyquist bin when downsampling to an even length
    return (np.fft.irfft(out_spec, n_out) * (n_out / float(n_in))).astype(np.float32)


def _warm_piper_cache(voice):
    """Pre-renders frequent short phrases in memory so playback begins in < 1 ms."""
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


def get_shared_piper(model_path: str, config_path: str, syn_params=None):
    """
    Returns the warm, process-wide PiperVoice instance for `model_path`.
    One voice is held in memory across all Speaker instances, keyed by its model path: asking for a
    different path (the user picked another voice) loads that one, swaps it in and re-renders the phrase
    cache with it. `syn_params` are that voice's synthesis overrides (None -> PIPER_SYN_PARAMS).
    """
    global _SHARED_PIPER_VOICE, _SHARED_PIPER_PATH, _SHARED_PIPER_SYN
    with _SHARED_PIPER_LOCK:
        if _SHARED_PIPER_VOICE is not None and (_SHARED_PIPER_PATH == model_path or _SHARED_PIPER_PATH is None):
            return _SHARED_PIPER_VOICE

        if os.path.exists(model_path) and os.path.exists(config_path):
            try:
                print(f"[voice] Loading Piper voice ({os.path.basename(model_path)})...")
                voice = _load_piper_limited(model_path, config_path)
                _SHARED_PIPER_VOICE = voice
                _SHARED_PIPER_PATH = model_path
                _SHARED_PIPER_SYN = None if syn_params is None else dict(syn_params)
                _AUDIO_PHRASE_CACHE.clear()          # pre-rendered phrases belong to the previous voice
                print("[voice] Piper voice loaded.")
                _warm_piper_cache(voice)
                return voice
            except Exception as e:
                print(f"[voice] PiperVoice initialization failed: {e}")
        return None


class _ReplyPlayer:
    """One sounddevice OutputStream for a whole reply. Audio pieces are queued with push(); the stream's
    callback drains them (silence if synthesis ever lags), so there is no per-sentence device restart.
    Opened/closed under _PORTAUDIO_LOCK; the wait for the tail is bounded (audio length + 2 s)."""

    def __init__(self, sd, samplerate):
        self.sd = sd
        self.samplerate = int(samplerate)
        self._chunks = collections.deque()
        self._cur = None
        self._pos = 0
        self._frames_left = 0
        self._lock = threading.Lock()
        self._done = False
        self.finished = threading.Event()
        with _PORTAUDIO_LOCK:
            self.stream = sd.OutputStream(samplerate=self.samplerate, channels=1, dtype="float32",
                                          latency="high", callback=self._callback,
                                          finished_callback=self.finished.set)
            self.stream.start()

    def push(self, audio):
        if self.finished.is_set():
            raise RuntimeError("output stream ended before the reply was queued")
        with self._lock:
            self._chunks.append(audio)
            self._frames_left += len(audio)

    def _callback(self, outdata, frames, time_info, status):
        out = outdata[:, 0]
        filled = 0
        with self._lock:
            while filled < frames:
                if self._cur is None:
                    if not self._chunks:
                        break
                    self._cur, self._pos = self._chunks.popleft(), 0
                take = min(frames - filled, len(self._cur) - self._pos)
                out[filled:filled + take] = self._cur[self._pos:self._pos + take]
                filled += take
                self._pos += take
                self._frames_left -= take
                if self._pos >= len(self._cur):
                    self._cur = None
            stop = self._done and self._cur is None and not self._chunks
        if filled < frames:
            out[filled:] = 0.0
        if stop:
            raise self.sd.CallbackStop

    def finish_and_wait(self) -> bool:
        """No more audio: wait (bounded) for the buffered tail to play. False if cut at the deadline."""
        with self._lock:
            self._done = True
            remaining = self._frames_left / float(self.samplerate)
        deadline = time.monotonic() + remaining + 2.0
        t0 = time.monotonic()
        while not self.finished.wait(0.05):
            jarvis_log.heartbeat("Speaker", speaking=True, playing=round(time.monotonic() - t0, 1))
            if time.monotonic() >= deadline:
                _vlog.warning("playback did not finish within %.1fs", remaining + 2.0)
                return False
        return True

    def close(self):
        try:
            with _PORTAUDIO_LOCK:
                if not self.finished.is_set():
                    self.stream.abort()
                self.stream.close()
        except Exception as exc:
            _vlog.warning("closing output stream failed: %r", exc)


class Speaker(threading.Thread):
    """
    Synthesizes and plays spoken feedback for J.A.R.V.I.S.
    Supports:
    - 'piper': Offline neural TTS with the cfg.tts_voice registry voice (optional download) via sounddevice.
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
        """Live updates speech settings without restart. A newly picked Piper voice is loaded (and its
        phrase cache rendered) in the background so the next reply does not wait for it."""
        if cfg:
            self.cfg = cfg
        self.enabled = getattr(self.cfg, "speak", True)
        if str(getattr(self.cfg, "tts_engine", "system")).lower() != "piper":
            return
        try:
            model_path = self._get_piper_paths()[0]
        except Exception:
            return
        if model_path and model_path != getattr(self, "_loaded_model_path", None):
            threading.Thread(target=self._init_piper, name="piper-prewarm", daemon=True).start()

    def say(self, text, urgent=False):
        """Queue `text` for speech. `urgent` (timers/alarms/reminders) bypasses the quiet rule."""
        is_enabled = getattr(self.cfg, "speak", self.enabled) if self.cfg else self.enabled
        if is_enabled and text:
            if not speech_allowed(text, urgent=urgent):
                _vlog.info("Speaker: speech suppressed (quiet: game/presentation/focus), %d chars", len(text))
                return
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

    DEFAULT_MODEL_REL = "models/voice/jarvis-medium.onnx"

    def _selected_piper(self):
        """(model, config, voice id) of the selected Piper voice, or (None, None, id) when it is not
        installed. The voice id is None for an explicit custom tts_model_path.
        Never downloads: voices are installed from Settings via voice_models.download()."""
        import voice_models
        rel_model = getattr(self.cfg, "tts_model_path", self.DEFAULT_MODEL_REL) if self.cfg else self.DEFAULT_MODEL_REL
        if rel_model and rel_model != self.DEFAULT_MODEL_REL:      # explicit custom model file
            custom = rel_model if os.path.isabs(rel_model) else os.path.join(
                os.path.dirname(os.path.abspath(__file__)), rel_model)
            if os.path.exists(custom) and os.path.exists(custom + ".json"):
                return custom, custom + ".json", None
        voice_id = (getattr(self.cfg, "tts_voice", "jarvis") if self.cfg else "jarvis") or "jarvis"
        paths = voice_models.get_paths(voice_id)
        if paths and paths.get("engine") == "piper":
            return paths["model"], paths["config"], voice_id
        return None, None, voice_id

    def _get_piper_paths(self):
        """(model, config) of the selected Piper voice, or (None, None) when it is not installed."""
        model_path, config_path, _vid = self._selected_piper()
        return model_path, config_path

    def _init_piper(self):
        model_path, config_path, voice_id = self._selected_piper()
        if model_path is None:
            if not getattr(self, "_piper_missing_logged", False):
                self._piper_missing_logged = True
                _vlog.warning("Piper voice not installed; speaking with the system voice "
                              "(install one from Settings > Voice)")
            self._piper_voice = None
            return None
        self._piper_missing_logged = False
        voice = get_shared_piper(model_path, config_path, syn_params_for(voice_id))
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
        player = None
        try:
            import sounddevice as sd

            if not split_into_sentences(text):
                return
            cached = _AUDIO_PHRASE_CACHE.get(_normalize_phrase_key(text))
            pieces = iter([cached]) if cached is not None else _iter_reply_audio(voice, text)
            for audio, sr in pieces:
                if player is None:
                    player = _ReplyPlayer(sd, sr)
                player.push(audio)
            if player is not None:
                player.finish_and_wait()
        except Exception as exc:
            if player is None:
                print(f"[voice] Piper synthesis/playback error: {exc}, falling back to system TTS")
                self._say_system(text)
            else:
                _vlog.warning("Piper playback error mid-reply: %r", exc)
        finally:
            if player is not None:
                player.close()
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
        engine_type = getattr(self.cfg, "tts_engine", "system") if self.cfg else "system"
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
            text = _speech_text(text)    # single synthesis entry (Piper, its phrase cache, and system TTS)
            t_say = time.monotonic()
            try:
                engine_type = getattr(self.cfg, "tts_engine", "system") if self.cfg else "system"
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
        on_push_to_talk=None,
        on_push_to_talk_done=None,
        on_partial_audio=None,
    ):
        super().__init__(name="WakeWordDetector", daemon=True)
        self.on_speech_end = on_speech_end
        # callable(pcm16 bytes so far) during a command recording, then callable(None) when it ends
        self.on_partial_audio = on_partial_audio
        self._mid_wake_tail = None   # set when the wake word fires inside a passive recording
        # Personal wake verifier (wake_trainer): learned from the user's voice, reloaded when retrained.
        # on_wake_sample(pcm16 of the ~2 s up to the wake) is called after a wake that led to a command.
        import wake_trainer
        self._verifier = wake_trainer.VerifierHandle()
        self._verify_floor = wake_trainer.VERIFY_FLOOR
        self._wake_ring = collections.deque(maxlen=int(wake_trainer.CLIP_S * 16000 / 1280) + 1)
        self.on_wake_sample = None
        self.on_push_to_talk = on_push_to_talk
        self.on_push_to_talk_done = on_push_to_talk_done
        self._ptt_event = threading.Event()   # set by request_listen(); consumed on this thread only
        self._ptt_source = ""
        self._capture_req = None              # set by request_capture(); consumed on this thread only
        self._capture_lock = threading.Lock()
        self._level_cb = None                 # per-chunk level meter callback while a capture runs
        self._capture_active = False
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
        self.guard = WakeWordGuard(threshold=self.threshold, require_consecutive=True)
        self.vad = VADDetector(cfg=cfg)
        self.model = None
        # Voice Over Music (experimental): echo canceller + a second model/VAD for the AEC path.
        # Owned and touched only by this thread (created/closed in _sync_aec).
        self._aec = None
        self._aec_model = None
        self._aec_vad = None
        self._aec_failed = False      # start() failed this enable cycle; not retried until the setting toggles
        self._aec_active = False      # last chunk was actually echo-cancelled (playback present)
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
        self._cfg_name_seen = self._cfg_mic_name()   # cfg.mic_device_name last acted on (a change reopens the mic)
        self._active_choice = None  # (mic name, host API) of the stream in use; set from LAST_MIC_CHOICE
        self.on_mic_switch = None   # callable(name) -> None: told once when stall recovery moves to another mic/host API
        self._last_rec_chunks = 0   # chunks read by the last record_utterance_with_vad (0 + fault = no audio at all)
        # Wake-model CPU saver + passive-burst pre-gate (see _wake_predict / Voice._on_utterance_recorded)
        self._recent_scores = collections.deque(maxlen=6)
        self.last_burst_wake_peak = 0.0
        self._quiet_run = 0
        self._skip_ring = collections.deque(maxlen=2)
        self.wake_predicts = 0
        self.wake_skipped = 0
        self._mic_state = ("down", "not started")
        # Suspend (PC locked/asleep) and device re-enumeration (hot-plug / resume), see set_suspended().
        self._suspended = threading.Event()
        self._resume_evt = threading.Event()
        self._resume_evt.set()
        self._reopen_at = 0.0           # monotonic time at/after which to close+reopen (0 = none); debounced
        self._reset_pa_pending = False  # re-enumerate PortAudio devices before the next open
        self._init_model()

    def _cfg_mic_name(self):
        return str(getattr(self.cfg, "mic_device_name", "") or "") if self.cfg else ""

    def _adopt_choice(self, idx):
        """Remember which (mic, host API) find_best_microphone verified for raw device idx."""
        ch = LAST_MIC_CHOICE
        if ch and ch.get("index") == idx:
            self._active_choice = (ch["name"], ch["host_api"])

    def _announce_switch(self, idx, reason):
        """Stall recovery landed on a different mic/host API than before: log it and notify once."""
        ch = LAST_MIC_CHOICE
        if not (ch and ch.get("index") == idx):
            return
        new = (ch["name"], ch["host_api"])
        old = self._active_choice
        self._active_choice = new
        if old is None or old == new:
            return
        _vlog.warning("microphone switched (%s): %r [%s] -> %r [%s]", reason, old[0], old[1], new[0], new[1])
        cb = self.on_mic_switch
        if cb is not None:
            try:
                cb(new[0])
            except Exception as exc:
                _vlog.warning("on_mic_switch callback failed: %r", exc)

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
        avad = getattr(self, "_aec_vad", None)
        if avad is not None:
            avad.cfg = value

    def _set_state(self, state, reason=""):
        self._mic_state = (state, reason)

    PTT_TIMEOUT_NO_SPEECH_S = 5.0
    LISTEN_OFF_POLL_S = 0.25
    REOPEN_SETTLE_S = 0.75    # trailing-edge debounce: repeated reopen requests inside this window coalesce
    # Command endpointing. After "Hey Jarvis" people pause before the command: until the command itself has
    # COMMAND_GRACE_CHUNKS speech chunks, silence must last WAKE_PAUSE_GRACE_S to end the recording. Once the
    # command is under way the user's pause setting applies, but never shorter than COMMAND_MIN_ENDPOINT_S.
    WAKE_PAUSE_GRACE_S = 1.2
    COMMAND_GRACE_CHUNKS = 5
    COMMAND_MIN_ENDPOINT_S = 0.8
    PARTIAL_INTERVAL_S = 0.6  # live transcript: audio-so-far handed to on_partial_audio this often
    MID_WAKE_TAIL_CHUNKS = 2  # chunks kept from a passive recording when the wake word fires inside it

    def set_suspended(self, on: bool):
        """True: the loop closes the mic stream and does no wake/VAD/STT work until resumed.
        False: resume; PortAudio is re-enumerated before the stream is reopened. Thread-safe."""
        if on:
            self._resume_evt.clear()
            self._suspended.set()
            self.listening_until = 0.0
        else:
            if self._suspended.is_set():
                self._reset_pa_pending = True
            self._suspended.clear()
            self._resume_evt.set()

    @property
    def suspended(self) -> bool:
        return self._suspended.is_set()

    def request_reopen(self):
        """Debounced (trailing edge): re-enumerate devices and reopen the input stream shortly after the
        last call. Safe to call repeatedly from any thread."""
        self._reset_pa_pending = True
        self._reopen_at = time.monotonic() + self.REOPEN_SETTLE_S

    def _listen_off(self):
        """Live 'Listen for Jarvis' setting (cfg is reloaded in place, so poll every pass)."""
        return not getattr(self.cfg, "listen_for_jarvis", True) if self.cfg else False

    def _sync_aec(self):
        """Live 'Voice Over Music' setting: create/start or close the canceller on this thread."""
        want = bool(getattr(self.cfg, "voice_over_music", False)) if self.cfg else False
        if not want:
            self._aec_failed = False
            if self._aec is not None:
                self._close_aec("setting off")
            return
        if self._aec is not None or self._aec_failed:
            return
        try:
            from echo_canceller import EchoCanceller
            aec = EchoCanceller()
            if not aec.start():
                self._aec_failed = True
                return
            self._aec = aec
            self._aec_vad = VADDetector(cfg=self.cfg)
            if self.model is not None:
                _import_openwakeword_lean()
                from openwakeword.model import Model
                self._aec_model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            _vlog.info("voice over music: echo canceller started")
        except Exception as exc:
            self._aec_failed = True
            _vlog.warning("voice over music: could not start (%r); running without echo cancellation", exc)
            self._close_aec("start failed")

    def _close_aec(self, why):
        aec, self._aec = self._aec, None
        self._aec_model = None
        self._aec_vad = None
        self._aec_active = False
        if aec is not None:
            try:
                aec.close()
            except Exception as exc:
                _vlog.warning("echo canceller close failed: %r", exc)
            _vlog.info("voice over music: echo canceller closed (%s)", why)

    def _aec_chunk(self, arr):
        """AEC output for this mic chunk, or None when AEC is off/idle (playback silent). The raw mic
        is never replaced: callers use the result only as a second opinion for wake/VAD."""
        if self._aec is None:
            self._aec_active = False
            return None
        out = self._aec.process(arr)
        active = out is not arr
        if active and not self._aec_active and self._aec_model is not None \
                and hasattr(self._aec_model, "reset"):
            self._aec_model.reset()   # stale feature buffer from before the idle gap
        self._aec_active = active
        return out if active else None

    def _vad_dual(self, arr, rms, aec_arr):
        """VAD on raw OR aec. While AEC is active both use the aec path's gate (its noise floor is
        music-free, so music does not inflate the raw gate)."""
        if aec_arr is None:
            return self.vad.is_speech(arr, rms)
        a_rms = float(np.sqrt(np.mean(aec_arr.astype(float) ** 2)))
        a_speech = self._aec_vad.is_speech(aec_arr, a_rms)
        r_speech = self.vad.is_speech(arr, rms, gate_override=self._aec_vad.gate())
        return r_speech or a_speech

    def request_listen(self, source: str = "") -> bool:
        """Thread-safe push-to-talk request (gesture / hotkey). The detector thread does the work.
        Dropped (False) while a listen is in progress or pending, or while JARVIS is speaking."""
        if self._ptt_event.is_set() or self.is_recording or Voice.is_speaking:
            _vlog.info("push_to_talk dropped (source=%s): busy", source)
            return False
        if not self.is_alive() or self.stop.is_set():
            return False
        self._ptt_source = str(source)
        self._ptt_event.set()
        return True

    def _do_push_to_talk(self, stream, chunk_samples, sample_rate):
        """Record ONE utterance on the open stream and dispatch it as a command (no wake word needed)."""
        source = self._ptt_source
        self._ptt_event.clear()
        _vlog.info("push_to_talk start (source=%s)", source)
        if self.on_push_to_talk:
            try:
                self.on_push_to_talk(source)
            except Exception as cb_err:
                print(f"[voice] on_push_to_talk error: {cb_err}")
        raw = self.record_utterance_with_vad(
            stream=stream,
            chunk_samples=chunk_samples,
            sample_rate=sample_rate,
            timeout_no_speech=self.PTT_TIMEOUT_NO_SPEECH_S,
            trigger="push_to_talk",
        )
        if raw:
            self._dispatch_utterance(raw, is_command=True)
        elif self.on_push_to_talk_done:
            try:
                self.on_push_to_talk_done(source)
            except Exception as cb_err:
                print(f"[voice] on_push_to_talk_done error: {cb_err}")

    CAPTURE_SPEAKING_WAIT_S = 10.0

    class CaptureRequest:
        def __init__(self, timeout, on_level, max_duration):
            self.timeout = float(timeout)
            self.on_level = on_level
            self.max_duration = max_duration
            self.raw = None
            self.done = threading.Event()
            self.started = threading.Event()   # the detector thread picked the request up
            self.no_audio = False              # the mic delivered no frames at all (stalled / not reading)
            self.cancelled = False

    def request_capture(self, timeout: float, on_level=None, max_duration: float = None):
        """Record ONE utterance on the already-open stream without a wake word and WITHOUT dispatching it
        (onboarding / calibration). Returns a CaptureRequest (wait on .done, result in .raw) or None when
        busy/unavailable. Thread-safe; the detector thread does the work."""
        if not self.is_alive() or self.stop.is_set() or self._suspended.is_set():
            return None
        with self._capture_lock:
            if self._capture_req is not None:
                return None
            req = WakeWordDetector.CaptureRequest(timeout, on_level, max_duration)
            self._capture_req = req
            return req

    def cancel_capture(self, req):
        with self._capture_lock:
            req.cancelled = True
            if self._capture_req is req:
                self._capture_req = None

    def _do_capture(self, stream, chunk_samples, sample_rate):
        req = self._capture_req
        if req is None:
            return
        try:
            waited = 0.0
            while Voice.is_speaking and waited < self.CAPTURE_SPEAKING_WAIT_S and not req.cancelled:
                if self.stop.wait(0.05):
                    break
                waited += 0.05
            if req.cancelled or self.stop.is_set():
                return
            self._level_cb = req.on_level
            self._capture_active = True
            req.started.set()
            req.raw = self.record_utterance_with_vad(
                stream=stream, chunk_samples=chunk_samples, sample_rate=sample_rate,
                timeout_no_speech=req.timeout,
                max_duration=req.max_duration if req.max_duration else req.timeout,
                trigger="capture",
            )
            req.no_audio = req.raw is None and self._last_rec_chunks == 0 and self._stream_fault is not None
        finally:
            self._level_cb = None
            self._capture_active = False
            with self._capture_lock:
                if self._capture_req is req:
                    self._capture_req = None
            req.done.set()

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
                        if self._active_choice is not None:
                            note_variant_failure(*self._active_choice)
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
        gate_vad = self._aec_vad if (self._aec_active and self._aec_vad is not None) else self.vad
        if rms >= gate_vad.gate():
            self._st_loud += 1
        if now - self._hb_t >= 1.0:
            self._hb_t = now
            jarvis_log.heartbeat(
                "WakeWordDetector", rms=int(round(rms)), rec=self.is_recording,
                follow=round(max(0.0, self.listening_until - time.time()), 1),
                speaking=Voice.is_speaking, **self._aec_hb())
        if now - self._st_t >= 60.0:
            v_total, v_speech = self.vad.take_stats()
            aec_txt = ""
            if self._aec is not None:
                if self._aec_vad is not None:
                    self._aec_vad.take_stats()
                aec_txt = f" aec={self._aec.stats()}"
            _vlog.info("wake stats %.0fs: max_score=%.2f noise_floor=%.0f gate=%.0f (energy_floor=%.0f) "
                       "speech=%d/%d chunks (%.1f%%) rms>=gate=%d/%d%s",
                       now - self._st_t, self._st_max_score, self.vad.noise_floor, self.vad.gate(),
                       self.vad.energy_threshold, v_speech, v_total, 100.0 * v_speech / max(1, v_total),
                       self._st_loud, self._st_chunks, aec_txt)
            self._st_t = now
            self._st_max_score = 0.0
            self._st_loud = self._st_chunks = 0
        return flat_new

    def _aec_hb(self):
        if not (getattr(self.cfg, "voice_over_music", False) if self.cfg else False):
            return {}
        if self._aec is None:
            return {"aec": "off"}
        return {"aec": "on" if self._aec_active else "idle"}

    # Skip the wake model only after this many consecutive clearly-silent chunks (~0.64 s), so the model has
    # already seen the trailing context of any word; the last 2 skipped chunks are replayed on the first
    # non-silent chunk so its streaming mel/feature buffers see contiguous audio at the onset.
    WAKE_SKIP_AFTER_QUIET = 8

    def _is_quiet(self, rms, aec_arr):
        """Clearly silent: Silero < 0.1 AND rms well below the VAD gate. Never while the AEC path is active."""
        if aec_arr is not None:
            return False
        prob = self.vad.last_prob
        return prob is not None and prob < VADDetector.NOISE_PROB and rms < 0.5 * self.vad.gate()

    def _wake_predict(self, arr, quiet):
        """openWakeWord score for this chunk (0.0 when the chunk was skipped as clearly silent)."""
        if quiet:
            self._quiet_run += 1
            if self._quiet_run > self.WAKE_SKIP_AFTER_QUIET:
                self._skip_ring.append(arr)
                self.wake_skipped += 1
                return 0.0
        else:
            self._quiet_run = 0
            if self._skip_ring:
                for old in self._skip_ring:
                    self.model.predict(old)
                self._skip_ring.clear()
        self.wake_predicts += 1
        return self.model.predict(arr).get("hey_jarvis", 0.0)

    def _wake_score(self, arr, rms, aec_arr):
        """"Hey Jarvis" score for this chunk (main + AEC path, personal verifier applied), also tracked for
        diagnostics."""
        self._wake_ring.append(arr.tobytes())
        score = self._wake_predict(arr, self._is_quiet(rms, aec_arr))
        if score >= self._verify_floor:
            v = self._verifier.get()
            if v is not None:
                try:
                    pre = self.model.preprocessor
                    score = v.prob(pre.get_features(self.model.model_inputs["hey_jarvis"]))
                except Exception as v_err:
                    _vlog.warning("personal wake verifier failed (%r); using the stock score", v_err)
        if aec_arr is not None and self._aec_model is not None:
            # Dual path: the AEC path only ever adds a chance to hear the wake word.
            score = max(score, self._aec_model.predict(aec_arr).get("hey_jarvis", 0.0))
        self._recent_scores.append(float(score))
        if score > self._st_max_score:
            self._st_max_score = float(score)
        return score

    def _wake_watch_enabled(self) -> bool:
        return bool(getattr(self.cfg, "wake_word_enabled", True)) and self.model is not None

    def _init_model(self):
        try:
            _import_openwakeword_lean()
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

    def endpoint_silence_s(self) -> float:
        """Silence that ends an utterance: cfg.mic_pause_threshold read live (Settings slider), clamped."""
        try:
            v = float(getattr(self.cfg, "mic_pause_threshold", 0.5))
        except (TypeError, ValueError):
            v = 0.5
        return min(2.0, max(0.3, v))

    def max_phrase_s(self) -> float:
        try:
            v = float(getattr(self.cfg, "mic_phrase_time_limit", 10.0))
        except (TypeError, ValueError):
            v = 10.0
        return min(15.0, max(3.0, v))

    def record_utterance_with_vad(
        self,
        stream=None,
        timeout_no_speech: float = 5.0,
        silence_endpoint: float = None,
        max_duration: float = None,
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
        Passive ("vad") recordings keep feeding the wake model; if "Hey Jarvis" fires the recording is
        abandoned (returns None) and self._mid_wake_tail holds its last chunks for the wake capture.
        Command recordings (wake / follow-up / push-to-talk) stream audio-so-far to on_partial_audio.
        """
        self.is_recording = True
        watch_wake = trigger == "vad" and not self._capture_active and self._wake_watch_enabled()
        if not watch_wake:
            # The model hears nothing while recording: its next chunks are not contiguous with these.
            self._quiet_run = 0
            self._skip_ring.clear()
            self._recent_scores.clear()
        self._mid_wake_tail = None
        is_command_rec = trigger in ("wake", "followup", "push_to_talk")
        adaptive_endpoint = silence_endpoint is None and is_command_rec
        partial_cb = self.on_partial_audio if is_command_rec else None
        last_partial_t = 0.0
        if silence_endpoint is None:
            silence_endpoint = self.endpoint_silence_s()
        if max_duration is None:
            max_duration = self.max_phrase_s()
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
                lvl_cb = self._level_cb
                if lvl_cb is not None:
                    try:
                        lvl_cb(_level_01(rms))
                    except Exception as lv_err:
                        _vlog.warning("level callback failed (%r); disabling it", lv_err)
                        self._level_cb = None
                recorded_frames.append(data)
                if self._diag_chunk(rms):
                    reason = "flatline"
                    self._stream_fault = "flatline"
                    break

                now = time.time()
                elapsed = now - start_time
                aec_arr = self._aec_chunk(arr)
                chunk_has_speech = self._vad_dual(arr, rms, aec_arr)
                total_chunks += 1
                rms_list.append(rms)
                if chunk_has_speech:
                    speech_chunks += 1

                if watch_wake and self.guard.evaluate(self._wake_score(arr, rms, aec_arr),
                                                      is_speaking=Voice.is_speaking, now=now):
                    _vlog.info("wake: openwakeword score=%.2f (inside speech)", self._recent_scores[-1])
                    self._mid_wake_tail = recorded_frames[-self.MID_WAKE_TAIL_CHUNKS:]
                    reason = "wake"
                    break

                if not speech_detected:
                    if chunk_has_speech:
                        speech_detected = True
                        consecutive_silence = 0.0
                        uid = jarvis_log.new_utterance()
                        jarvis_log.utterance_event(jarvis_log.AUDIO_START, uid, trigger=trigger,
                                                   device=self.device_index)
                    elif elapsed >= timeout_no_speech:
                        # 5 s silence with no speech
                        if self._capture_active:
                            return None     # onboarding capture: no overlay/media side effects
                        if self.on_no_speech:
                            try:
                                self.on_no_speech()
                            except Exception as ns_err:
                                print(f"[voice] on_no_speech error: {ns_err}")
                        self.listening_until = 0.0
                        return None
                else:
                    endpoint = silence_endpoint
                    if adaptive_endpoint:
                        if trigger == "wake" and speech_chunks - pre_speech_chunks < self.COMMAND_GRACE_CHUNKS:
                            endpoint = max(endpoint, self.WAKE_PAUSE_GRACE_S)
                        else:
                            endpoint = max(endpoint, self.COMMAND_MIN_ENDPOINT_S)
                    if chunk_has_speech:
                        consecutive_silence = 0.0
                    else:
                        consecutive_silence += chunk_duration
                        if consecutive_silence >= endpoint:
                            reason = "silence"
                            break
                    if elapsed >= max_duration:
                        reason = "max_duration"
                        break
                    if partial_cb is not None and now - last_partial_t >= self.PARTIAL_INTERVAL_S:
                        last_partial_t = now
                        try:
                            partial_cb(b"".join(recorded_frames))
                        except Exception as p_err:
                            _vlog.warning("partial audio callback failed (%r); disabling it", p_err)
                            partial_cb = None

            if speech_detected:
                try:
                    jarvis_log.utterance_event(
                        jarvis_log.ENDPOINT, uid, reason=reason, dur_s=f"{time.time() - start_time:.2f}",
                        speech_chunks=speech_chunks, total_chunks=total_chunks,
                        rms_p50=int(sorted(rms_list)[len(rms_list) // 2]) if rms_list else 0)
                except Exception:
                    pass
            if self._mid_wake_tail is not None:
                return None
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
            self._last_rec_chunks = total_chunks
            self.is_recording = False
            if partial_cb is not None:
                try:
                    partial_cb(None)   # recording over: any partial still in flight is stale
                except Exception:
                    pass

    def _on_wake_fired(self, stream, pre_roll, chunk_samples, sample_rate, pre_speech_chunks=0):
        """"Hey Jarvis" was heard: wake callback (chime / island), then record the command and dispatch it.
        No command within the window -> on_wake_idle (follow-up window stays open)."""
        wake_audio = b"".join(self._wake_ring)   # the ~2 s up to the wake: "Hey Jarvis" in the user's voice
        self._wake_ring.clear()
        if self.on_wake:
            try:
                self.on_wake()
            except Exception as cb_err:
                print(f"[voice] on_wake error: {cb_err}")
        elif getattr(self.cfg, "chime_enabled", True) and _wake_chime_allowed():
            play_chime(non_blocking=True)

        self.listening_until = time.time() + self.followup_window
        raw = self.record_utterance_with_vad(
            stream=stream,
            pre_roll=pre_roll,
            chunk_samples=chunk_samples,
            sample_rate=sample_rate,
            timeout_no_speech=min(self.followup_window, 3.5),
            trigger="wake",
            pre_speech_chunks=pre_speech_chunks,
        )
        # The model was not fed during the command, so its feature buffer still ends in "Hey Jarvis":
        # without a reset it fires again on the first chunks after the command.
        for m in (self.model, self._aec_model):
            if m is not None and hasattr(m, "reset"):
                try:
                    m.reset()
                except Exception as r_err:
                    _vlog.warning("wake model reset failed: %r", r_err)
        if raw:
            if self.on_wake_sample is not None and wake_audio:
                try:
                    self.on_wake_sample(wake_audio)
                except Exception as s_err:
                    _vlog.warning("wake sample callback failed: %r", s_err)
            self._dispatch_utterance(raw, is_command=True)
        elif not self._stream_fault:
            if self.on_wake_idle:
                try:
                    self.on_wake_idle()
                except Exception as idle_err:
                    print(f"[voice] on_wake_idle error: {idle_err}")
            self.listening_until = time.time() + self.followup_window

    def transcribe(self, raw_bytes: bytes) -> str:
        """Transcribes 16 kHz raw PCM bytes: local model first, then Groq Whisper when AI is on, else ""."""
        if not raw_bytes:
            return ""
        try:
            lt = _local_stt_for(self.cfg)
            if lt is not None:
                try:
                    return lt.transcribe(raw_bytes)[0]
                except Exception as e:
                    print(f"[voice] local transcribe failed: {e}")
            key = get_api_key()
            ai_enabled = getattr(self.cfg, "ai_enabled", False) if self.cfg else False
            if ai_enabled and key:
                stt_model = getattr(self.cfg, "ai_stt_model", "whisper-large-v3-turbo") if self.cfg else "whisper-large-v3-turbo"
                timeout = getattr(self.cfg, "ai_timeout", 10) if self.cfg else 10
                client = GroqClient(api_key=key, timeout=timeout)
                return client.transcribe(_pcm16_wav(raw_bytes), model=stt_model, language="en")
            return ""
        except Exception as exc:
            print(f"[voice] transcription error: {exc}")
            return ""

    def record_and_transcribe(
        self,
        stream=None,
        timeout_no_speech: float = 5.0,
        silence_endpoint: float = None,
        max_duration: float = None,
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
        PyAudio instance is reset. Selection goes through find_best_microphone(require_16k=True): the
        configured mic first on its host-API variants (the variant that just stalled has been marked, so it is
        not reopened ahead of the others and is skipped after two failures), then auto-detect; only a variant
        that delivers frames within ~1 s is returned. A switch to another mic/host API is logged and announced.
        """
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
                _vlog.warning("reconnect_stream: attempt %d: no microphone delivered audio; retrying", attempt)
                continue

            test_stream = None
            try:
                test_stream = _open_input(p, best_idx, chunk_samples)
                _log_mic_open(p, best_idx, sample_rate, 1, chunk_samples, "OK", "reconnect_stream")
                ok, _data = _stream_delivers(test_stream, chunk_samples)
                if ok:
                    self.device_index = best_idx
                    Voice.current_mic_index = best_idx
                    Voice.current_mic_name = best_name
                    self._announce_switch(best_idx, "stall recovery")
                    self._set_state("ok", "")
                    print(f"[voice] Microphone auto-recovered successfully to [{best_idx}] {best_name}.")
                    _vlog.info("reconnect_stream: recovered on attempt %d: idx=%s name=%r", attempt, best_idx, best_name)
                    return test_stream
                _vlog.warning("reconnect_stream: attempt %d: idx=%s opened but delivered no frames", attempt, best_idx)
                ch = LAST_MIC_CHOICE
                if ch and ch.get("index") == best_idx:
                    note_variant_failure(ch["name"], ch["host_api"])
            except Exception as exc:
                _log_mic_open(p, best_idx, sample_rate, 1, chunk_samples, repr(exc), "reconnect_stream")
            if test_stream is not None:
                try:
                    with _PORTAUDIO_LOCK:
                        test_stream.stop_stream()
                        _shared_close(test_stream)
                except Exception as close_exc:
                    _vlog.warning("reconnect_stream: closing unusable stream failed: %r", close_exc)
        return None

    def run(self):
        jarvis_log.thread_started("WakeWordDetector")
        try:
            self._run_body()
        except BaseException as e:
            self._close_aec("thread exit")
            self._set_state("down", f"{type(e).__name__}: {e}")
            jarvis_log.thread_exited("WakeWordDetector", e)
            raise
        else:
            self._close_aec("thread exit")
            self._set_state("down", "stopped")
            jarvis_log.thread_exited("WakeWordDetector")

    def _run_body(self):
        import pyaudio

        sample_rate = 16000
        chunk_samples = 1280  # 80 ms chunks

        while not self.stop.is_set():
            if self._suspended.is_set():
                # PC locked / asleep: no stream open (mic indicator off), no reads, no wake model, no VAD.
                self._set_state("ok", "Suspended")
                self._reopen_at = 0.0     # resume re-enumerates anyway
                self._flat_since = None
                self._flat_flagged = False
                self.listening_until = 0.0
                self._ptt_event.clear()
                jarvis_log.heartbeat("WakeWordDetector", listening="suspended")
                self._resume_evt.wait(1.0)
                continue
            if self._listen_off() and not self._ptt_event.is_set() and self._capture_req is None:
                # Listen for Jarvis is OFF: no stream is open (mic-in-use indicator off), no reads, no
                # wake model, no VAD, no STT. Idle until the setting flips or a push-to-talk arrives.
                self._set_state("ok", "Listening off")
                self._reopen_at = 0.0     # no stream to reopen; _reset_pa_pending still applies on next open
                self._flat_since = None
                self._flat_flagged = False
                self.listening_until = 0.0
                jarvis_log.heartbeat("WakeWordDetector", listening="off")
                self._ptt_event.wait(self.LISTEN_OFF_POLL_S)
                continue

            if self._reset_pa_pending:
                # Device change / resume: no stream of ours is open here, so re-enumeration is safe.
                self._reset_pa_pending = False
                reset_shared_pyaudio()
                reset_variant_failures()
                self.device_index = None          # raw indices are not stable: re-pick by name from the fresh list
            # Re-fetch every pass: reconnect_stream() resets the shared PyAudio instance.
            p = get_shared_pyaudio()
            cfg_name = self._cfg_mic_name()
            if cfg_name != self._cfg_name_seen:   # the user picked another microphone
                self._cfg_name_seen = cfg_name
                self.device_index = None
            active_idx = self.device_index
            if active_idx is None:
                active_idx, _ = find_best_microphone(self.cfg, require_16k=True)
                self._adopt_choice(active_idx)
            self.device_index = active_idx

            stream = None
            self._stream_fault = None
            try:
                stream = _open_input(p, active_idx, chunk_samples)
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
                    if self._cfg_mic_name() != self._cfg_name_seen:
                        break   # finally: closes the stream; the outer loop reopens the newly chosen mic

                    if self._suspended.is_set():
                        break   # finally: closes the stream; the outer loop then parks
                    if self._reopen_at and time.monotonic() >= self._reopen_at:
                        self._reopen_at = 0.0
                        break   # finally: closes the stream; the outer loop re-enumerates and reopens
                    if self._capture_req is not None:
                        pre_roll.clear()
                        self._quiet_run = 0
                        self._skip_ring.clear()
                        self._do_capture(stream, chunk_samples, sample_rate)
                        if self._stream_fault:
                            stream_error = True
                            break
                        continue
                    if self._ptt_event.is_set():
                        pre_roll.clear()
                        self._quiet_run = 0
                        self._skip_ring.clear()
                        self._do_push_to_talk(stream, chunk_samples, sample_rate)
                        if self._stream_fault:
                            stream_error = True
                            break
                        continue
                    if self._listen_off():
                        break   # finally: closes the stream; the outer loop then idles

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
                    self._sync_aec()
                    aec_arr = self._aec_chunk(arr)
                    chunk_speech = self._vad_dual(arr, rms, aec_arr)

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
                    if self._wake_watch_enabled():
                        score = self._wake_score(arr, rms, aec_arr)
                        if self.guard.evaluate(score, is_speaking=Voice.is_speaking, now=now):
                            wake_triggered = True
                            _vlog.info("wake: openwakeword score=%.2f", float(score))
                            self._on_wake_fired(stream, list(pre_roll), chunk_samples, sample_rate,
                                                pre_speech_chunks=1 if chunk_speech else 0)
                            pre_roll.clear()
                            if self._stream_fault:
                                stream_error = True
                                break

                    # 2. Continuous VAD speech capture:
                    # Captures direct voice commands (e.g. "Jarvis open Chrome" or "Jarvis")
                    # even if openWakeWord has not (yet) triggered on "hey_jarvis". The wake model keeps
                    # listening inside this recording; if "Hey Jarvis" fires there, the recording is
                    # abandoned and the command is captured as a normal wake (below).
                    # _recognition_worker and _process verify wake_aliases in passive standby,
                    # ensuring zero false activations from random background speech.
                    if chunk_speech and not wake_triggered:
                        self.last_burst_wake_peak = max(self._recent_scores, default=0.0)
                        raw = self.record_utterance_with_vad(
                            stream=stream,
                            pre_roll=list(pre_roll),
                            chunk_samples=chunk_samples,
                            sample_rate=sample_rate,
                            pre_speech_chunks=1,
                        )
                        pre_roll.clear()
                        tail, self._mid_wake_tail = self._mid_wake_tail, None
                        if tail is not None and not self._stream_fault:
                            self._on_wake_fired(stream, tail, chunk_samples, sample_rate,
                                                pre_speech_chunks=1)
                        elif raw:
                            self._dispatch_utterance(raw, is_command=in_followup)
                        if self._stream_fault:
                            stream_error = True
                            break
            finally:
                self._close_aec("mic stream closed")
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
        active_idx = self.device_index
        if active_idx is None:
            active_idx, _ = find_best_microphone(self.cfg)

        p = get_shared_pyaudio()
        stream = None
        try:
            stream = _open_input(p, active_idx, 1280)
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


_GROQ_STT_CLIENT = None   # (GroqClient class, key, timeout, instance)
_GROQ_STT_LOCK = threading.Lock()


def _get_groq_stt_client(key, timeout):
    """One GroqClient for STT (keeps its HTTP session and its 25 req/min limiter) instead of one per call."""
    global _GROQ_STT_CLIENT
    with _GROQ_STT_LOCK:
        c = _GROQ_STT_CLIENT
        if c is not None and c[0] is GroqClient and c[1] == key and c[2] == timeout:
            return c[3]
        client = GroqClient(api_key=key, timeout=timeout)
        _GROQ_STT_CLIENT = (GroqClient, key, timeout, client)
        return client


class Voice(threading.Thread):
    """Say 'Jarvis, open Chrome' in one go, or say 'Jarvis' and wait for 'Yes, sir?'."""
    _latest_rms = 0.0
    _latest_time = 0.0
    _last_raw_audio = None
    is_speaking = False
    current_mic_index = None      # class-level so WakeWordDetector diagnostics never hit a missing attribute
    current_mic_name = "Auto"
    last_capture_reason = ""      # why the last capture_answer returned nothing: no_speech | unavailable | no_audio

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
        # Live transcript: newest (generation, audio-so-far) waiting for the partial worker; the generation
        # is bumped when a recording ends so a late partial never overwrites the final transcript.
        self._partial_lock = threading.Lock()
        self._partial_evt = threading.Event()
        self._partial_pending = None
        self._partial_gen = 0
        # Personal wake verifier training (wake_trainer): examples only while voice clips are allowed.
        import wake_trainer
        self._wake_trainer = wake_trainer.WakeTrainer(enabled=lambda: _get_personal() is not None
                                                      and _get_personal().flag("voice_clips"))
        self.current_mic_index = None
        self.current_mic_name = "Auto"
        self.groq_cooldown_until = 0.0
        self.sensitivity = getattr(cfg, "wake_sensitivity", "balanced") if cfg else "balanced"
        self.followup_window = float(getattr(cfg, "wake_followup_window", getattr(cfg, "command_window", 6.0))) if cfg else 6.0
        self.wake_detector = None
        self._suspended = False
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
            # Voice.run is still probing the microphone (~1 s at startup): a normal startup state, not a
            # failure (the island dot would otherwise flash red). A Voice thread that died is still "down".
            if self.is_alive():
                return "opening", "Mic starting"
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
        if _wake_chime_allowed():
            if getattr(self.cfg, "chime_enabled", True):
                play_chime(non_blocking=True)
            if hasattr(self.ctx, "chirp"):
                try:
                    self.ctx.chirp("wake")
                except Exception:
                    pass

    def set_suspended(self, on: bool):
        """PC locked / asleep: close the mic stream and stop wake/VAD processing; resume reopens it
        (re-enumerating devices). Thread-safe; safe to call before the detector exists."""
        self._suspended = bool(on)
        if on:
            self.listening_until = 0.0
        wd = self.wake_detector
        if wd is not None:
            wd.set_suspended(self._suspended)

    def reopen_devices(self):
        """Re-enumerate audio devices and reopen the input stream (device change / resume). Debounced and
        safe to call repeatedly; while suspended it only schedules re-enumeration for resume."""
        wd = self.wake_detector
        if wd is not None:
            wd.request_reopen()

    def request_listen(self, source: str = "") -> bool:
        """Push-to-talk (e.g. the call-me gesture). Thread-safe. Returns False if dropped (no detector,
        already listening, or JARVIS speaking). At most one request is held; extras are dropped."""
        wd = self.wake_detector
        if wd is None or Voice.is_speaking or self._suspended:
            return False
        return wd.request_listen(source)

    def _on_push_to_talk_start(self, source):
        self._duck_media("push_to_talk")
        until = time.time() + WakeWordDetector.PTT_TIMEOUT_NO_SPEECH_S
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("wake", duration=0.6)
                self.ctx.overlay.post("wake_detected")
                self.ctx.overlay.post("listening", until=until)
            except Exception:
                pass
        note_explicit_command()
        if getattr(self.cfg, "chime_enabled", True) and _wake_chime_allowed():
            play_chime(non_blocking=True)

    def _on_push_to_talk_done(self, source):
        """No usable utterance was captured: restore media and close the listening pill."""
        self._restore_media("push_to_talk_done")
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("listening_done")
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

    # ------------------------------------------------------------------ live transcript (Siri-style)
    PARTIAL_MAX_AUDIO_S = 12.0   # only the most recent audio is re-transcribed for the live line

    def _on_partial_audio(self, raw):
        """Detector thread: audio-so-far of a command recording, or None when it ended. Never blocks:
        the newest clip replaces any clip the partial worker has not started yet."""
        with self._partial_lock:
            if raw is None:
                self._partial_gen += 1
                self._partial_pending = None
                return
            if self._local_stt_ready() is None:
                return    # live text only from the local model: never a Groq call per partial
            self._partial_pending = (self._partial_gen, raw)
        self._partial_evt.set()

    def _partial_worker(self):
        while not self.stop.is_set() and not self._own_stop.is_set():
            if not self._partial_evt.wait(0.5):
                continue
            self._partial_evt.clear()
            with self._partial_lock:
                item, self._partial_pending = self._partial_pending, None
            if item is None:
                continue
            gen, raw = item
            if self.audio_q.qsize() > 0:
                continue    # a finished utterance is waiting: the final transcript matters more
            lt = self._local_stt_ready()
            if lt is None:
                continue
            raw = raw[-int(self.PARTIAL_MAX_AUDIO_S * 32000):]
            try:
                text, _conf = lt.transcribe(raw)
            except Exception as exc:
                _vlog.warning("partial transcribe failed: %r", exc)
                continue
            text = self._partial_display_text(text)
            with self._partial_lock:
                if gen != self._partial_gen or not text:
                    continue    # the recording ended meanwhile: the final transcript takes over
                self.post_partial_speech(text, is_final=False)

    def _partial_display_text(self, text):
        """Live line without the wake word ("Hey Jarvis, what's the" -> "What's the"); "" for noise."""
        t = (text or "").strip()
        if _normalize_noise_text(t) in _WHISPER_NOISE_NORMALIZED:
            return ""
        for alias in list(getattr(self.cfg, "wake_aliases", ["jarvis"])) + ["jarvis"]:
            m = re.match(_WAKE_PREFIX_RE + re.escape(str(alias)) + r"\b[\s,.!?]*", t, re.IGNORECASE)
            if m:
                t = t[m.end():]
                break
        t = t.strip()
        return t[:1].upper() + t[1:] if t else ""

    WAKE_LIKE_RATIO = 0.5      # a word this similar to an alias ("Joffas") may be a misheard wake word

    def _learn_wake_example(self, text, matched_alias):
        """Passive (no wake model) utterance -> wake_trainer example. Starts with the wake word: a wake the
        model missed (positive). No wake word and nothing wake-like near the start: negative."""
        ctx = getattr(self, "_utt_ctx", None)
        raw = ctx.get("raw") if ctx else None
        if not raw:
            return
        t = (text or "").lower().strip()
        aliases = [str(a).lower() for a in getattr(self.cfg, "wake_aliases", ["jarvis"])] + ["jarvis"]
        if matched_alias is not None:
            if any(re.match(r"^\s*" + _WAKE_PREFIX_RE + re.escape(a) + r"\b", t) for a in aliases):
                self._wake_trainer.add("pos", raw)
            return
        words = re.findall(r"[a-z']+", t)[:3]
        if not words or words[0] in ("hey", "hi", "ok", "okay"):
            return
        from difflib import SequenceMatcher
        if any(SequenceMatcher(None, a, w).ratio() >= self.WAKE_LIKE_RATIO for a in aliases for w in words):
            return
        self._wake_trainer.add("neg", raw)

    PASSIVE_PREGATE_FRACTION = 0.5

    def _on_utterance_recorded(self, raw_bytes: bytes, is_command: bool = True):
        if not raw_bytes:
            return
        Voice._last_raw_audio = raw_bytes
        uid = jarvis_log.current_utterance()
        now = time.time()
        wd = self.wake_detector
        if not is_command and now >= self.listening_until and not (wd and now < wd.listening_until):
            # Wake-less speech in standby. Without a local transcriber it would cost a Groq call
            # only to be dropped by the wake-word check, so require the wake model to have stirred first.
            if self._local_stt_ready() is None and wd is not None:
                peak = getattr(wd, "last_burst_wake_peak", None)
                thr = getattr(wd, "threshold", None)
                if isinstance(peak, (int, float)) and isinstance(thr, (int, float)) \
                        and peak < self.PASSIVE_PREGATE_FRACTION * thr:
                    jarvis_log.utterance_event(jarvis_log.DROPPED, uid, reason="passive_pregate",
                                               peak=f"{peak:.2f}", thr=f"{thr:.2f}")
                    return
        try:
            if not self._enqueue_audio((raw_bytes, is_command, time.time(), uid), is_command):
                qs = self.audio_q.qsize()
                jarvis_log.utterance_event(jarvis_log.DROPPED, uid, reason="audio_q_full", qsize=qs)
                _vlog.warning("audio_q full (qsize=%d): dropping utterance %s", qs, uid)
        except Exception as exc:
            _vlog.warning("enqueue utterance %s failed: %r", uid, exc)

    def _enqueue_audio(self, item, is_command) -> bool:
        """Queue an utterance. When full, a command/follow-up evicts the OLDEST passive (wake-less) item so
        background noise can never starve it; a passive item is simply dropped. Returns False if dropped."""
        q = self.audio_q
        try:
            q.put_nowait(item)
            return True
        except queue.Full:
            pass
        if not is_command:
            return False
        with q.mutex:
            for i, it in enumerate(q.queue):
                if isinstance(it, tuple) and len(it) > 1 and not it[1]:
                    del q.queue[i]
                    q.unfinished_tasks = max(0, q.unfinished_tasks - 1)
                    _vlog.info("audio_q full: evicted oldest passive utterance for a command")
                    break
            else:
                return False
        try:
            q.put_nowait(item)
            return True
        except queue.Full:
            return False

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
        self._start_local_stt()

        # Probed microphone
        dev_idx, dev_name = find_best_microphone(self.cfg, require_16k=True)
        self.current_mic_index = dev_idx
        self.current_mic_name = dev_name
        Voice.current_mic_index = dev_idx
        Voice.current_mic_name = dev_name
        print(f"[voice] Connecting to microphone [{dev_idx}]: {dev_name}")
        _vlog.info("Voice: microphone chosen idx=%s name=%r (configured name=%r)", dev_idx, dev_name,
                   getattr(self.cfg, "mic_device_name", ""))

        # STT background worker
        worker = threading.Thread(target=jarvis_log.run_logged,
                                  args=("STTWorker", self._recognition_worker),
                                  name="STTWorker", daemon=True)
        worker.start()
        threading.Thread(target=jarvis_log.run_logged, args=("PartialSTT", self._partial_worker),
                         name="PartialSTT", daemon=True).start()

        # Dedicated openWakeWord detector at 16 kHz
        self.wake_detector = WakeWordDetector(
            cfg=self.cfg,
            on_wake=self._on_wake_detected,
            on_utterance=self._on_utterance_recorded,
            on_no_speech=self._on_no_speech_detected,
            on_wake_idle=self._on_wake_idle,
            on_speech_end=self._on_speech_finished,
            on_push_to_talk=self._on_push_to_talk_start,
            on_push_to_talk_done=self._on_push_to_talk_done,
            on_partial_audio=self._on_partial_audio,
            stop_event=self._own_stop,
            sensitivity=self.sensitivity,
            followup_window=self.followup_window,
            device_index=dev_idx,
        )
        self.wake_detector._adopt_choice(dev_idx)
        self.wake_detector.on_mic_switch = self._on_mic_switched
        self.wake_detector.on_wake_sample = lambda pcm: self._wake_trainer.add("pos", pcm, at_end=True)
        if self._suspended:
            self.wake_detector.set_suspended(True)
        self.wake_detector.start()

        while not self.stop.is_set():
            jarvis_log.heartbeat("Voice")
            if not self.wake_detector.is_alive() and not self.stop.is_set():
                _vlog.error("WakeWordDetector thread died (mic_state=%s); Voice exiting so the supervisor "
                            "restarts it", self.wake_detector.mic_state())
                raise RuntimeError("WakeWordDetector thread died")
            configured_sens = getattr(self.cfg, "wake_sensitivity", None)
            if configured_sens is not None and configured_sens != self.wake_detector.sensitivity_str:
                self.set_sensitivity(configured_sens)

            configured_followup = getattr(self.cfg, "wake_followup_window", None)
            if configured_followup is not None and float(configured_followup) != self.wake_detector.followup_window:
                self.set_followup_window(float(configured_followup))

            self.stop.wait(timeout=0.5)

    def _on_mic_switched(self, name):
        """Stall recovery moved the detector to another microphone / host API: one island notice."""
        Voice.current_mic_name = self.current_mic_name = name
        ov = getattr(self.ctx, "overlay", None)
        if ov:
            try:
                ov.post("say", text=f"Microphone switched to {name}", speak=False)
            except Exception:
                _vlog.warning("mic switch notice post failed", exc_info=True)

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

    def _recognition_worker(self):
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
                self._handle_audio_item(vp, audio, is_command, enqueued_at, uid)
            finally:
                jarvis_log.set_current_utterance(None)

    def _local_stt_ready(self):
        """The local transcriber when cfg.stt_engine == "local" and its model is loaded, else None."""
        return _local_stt_for(self.cfg)

    def _start_local_stt(self):
        """Load the local model in the background at voice start (first run downloads it)."""
        if str(getattr(self.cfg, "stt_engine", "local")).lower() != "local":
            return
        import local_stt
        local_stt.get_local_transcriber(getattr(self.cfg, "local_stt_model", "base.en")).start_loading()

    HOTWORDS_TTL_S = 30.0

    def _hotwords(self):
        """The user's own vocabulary (names, games, apps) as a faster-whisper `hotwords` string, or "".
        Wake aliases are filtered out: any hotword containing "Jarvis" makes Whisper drop a leading
        "Jarvis". Only used while learn_corrections is on."""
        if not getattr(self.cfg, "learn_corrections", True):
            return ""
        now = time.monotonic()
        cached = getattr(self, "_hotwords_cache", None)
        if cached is not None and now - cached[0] < self.HOTWORDS_TTL_S:
            return cached[1]
        text = ""
        p = _get_personal()
        if p is not None:
            try:
                aliases = [str(a).lower() for a in getattr(self.cfg, "wake_aliases", ["jarvis"])] + ["jarvis"]
                terms = [t for t in (p.vocabulary(limit=50) or [])
                         if t and not any(a in str(t).lower() for a in aliases)]
                text = ", ".join(terms)
            except Exception as exc:
                _vlog.warning("personal.vocabulary failed: %r", exc)
        self._hotwords_cache = (now, text)
        return text

    def _stt_local(self, lt, raw_bytes, uid, queue_ms):
        t0 = time.monotonic()
        jarvis_log.utterance_event(jarvis_log.STT_SENT, uid, engine="local", bytes=len(raw_bytes),
                                   dur_s=f"{len(raw_bytes) / 32000.0:.2f}")
        hw = self._hotwords()
        text, conf = lt.transcribe(raw_bytes, hotwords=hw) if hw else lt.transcribe(raw_bytes)
        ms = int((time.monotonic() - t0) * 1000)
        print(f"[voice] Local Heard: \"{redact(text)}\"")
        jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="local", text=text, ok=bool(text),
                                   conf=f"{conf:.2f}", ms=ms, queue_ms=queue_ms)
        return text, conf

    def _stt_groq(self, raw_bytes, uid, key):
        """Groq Whisper with the shared cooldown handling. Returns (text, net_fail); text is None on failure."""
        text = None
        groq_net_fail = False
        t_groq = time.monotonic()

        def ms():
            return int((time.monotonic() - t_groq) * 1000)

        try:
            # Convert in-memory audio chunk to 16 kHz mono WAV (zero temp files)
            wav_bytes = _pcm16_wav(raw_bytes)
            stt_model = getattr(self.cfg, "ai_stt_model", "whisper-large-v3-turbo")
            timeout = getattr(self.cfg, "ai_timeout", 10)
            client = _get_groq_stt_client(key, timeout)
            print(f"[voice] Transcribing complete utterance via Groq Whisper ({stt_model})...")
            jarvis_log.utterance_event(jarvis_log.STT_SENT, uid, engine="groq", bytes=len(wav_bytes),
                                       dur_s=f"{len(raw_bytes) / 32000.0:.2f}")
            text = client.transcribe(wav_bytes, model=stt_model, language="en")
            print(f"[voice] Groq Heard: \"{redact(text)}\"")
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", text=text, ok=bool(text),
                                       ms=ms())
            self.groq_cooldown_until = 0.0   # clear any prior cooldown
        except AuthenticationError as auth_err:
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                       err=type(auth_err).__name__, ms=ms())
            print(f"[voice] Groq key authentication failed: {auth_err}")
            self.groq_cooldown_until = time.time() + 300.0  # 5 min cooldown on auth failure
            self._post_ai_error("Invalid Groq Key")
        except RateLimitError as rl_err:
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                       err=type(rl_err).__name__, ms=ms())
            print(f"[voice] Groq rate limit: {rl_err}")
            wait_sec = rl_err.retry_after if rl_err.retry_after else 10.0
            self.groq_cooldown_until = time.time() + wait_sec
            self._post_ai_error("Groq Rate Limited")
        except (OfflineError, TimeoutError) as net_err:
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                       err=type(net_err).__name__, ms=ms())
            print(f"[voice] Groq network offline/timeout: {net_err}")
            self.groq_cooldown_until = time.time() + 15.0
            groq_net_fail = True
            self._post_ai_error("Groq Offline")
        except Exception as exc:
            jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="groq", ok=False,
                                       err=type(exc).__name__, ms=ms())
            print(f"[voice] Groq error ({exc})")
        return text, groq_net_fail

    def _post_ai_error(self, text):
        if hasattr(self.ctx, "overlay") and self.ctx.overlay:
            try:
                self.ctx.overlay.post("ai_error", text=text)
            except Exception:
                pass

    def _handle_audio_item(self, vp, raw_bytes, is_command, enqueued_at, uid):
        # (body of the former _recognition_worker loop; `continue` -> `return`)
        if True:
            # Energy-based VAD validation before sending audio
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
            passive = not is_active_session
            key = get_api_key(self.cfg)
            ai_enabled = getattr(self.cfg, "ai_enabled", False)
            use_groq = bool(ai_enabled and key and now >= self.groq_cooldown_until)
            if ai_enabled and key and not use_groq and self._groq_cd_logged != self.groq_cooldown_until:
                self._groq_cd_logged = self.groq_cooldown_until
                _vlog.warning("Groq cooling down until %s (%.0fs left); local STT only",
                              time.strftime("%H:%M:%S", time.localtime(self.groq_cooldown_until)),
                              self.groq_cooldown_until - now)

            queue_ms = max(0, int((time.time() - enqueued_at) * 1000)) if enqueued_at else 0
            done = False
            engine, conf = None, None
            lt = self._local_stt_ready()
            if lt is not None:
                try:
                    text, conf = self._stt_local(lt, raw_bytes, uid, queue_ms)
                    engine = "local"
                    done = True
                    min_conf = float(getattr(self.cfg, "local_stt_min_confidence", 0.55))
                    if conf < min_conf and not passive and use_groq:
                        # Unsure local result in an active session: let Groq have a go; keep local text if it fails.
                        _vlog.info("local STT conf=%.2f < %.2f: retrying with Groq", conf, min_conf)
                        gtext, groq_net_fail = self._stt_groq(raw_bytes, uid, key)
                        if gtext:
                            text, engine = gtext, "groq"
                except Exception as exc:
                    _vlog.warning("local STT failed (%r); falling back", exc)
                    jarvis_log.utterance_event(jarvis_log.TRANSCRIPT, uid, engine="local", ok=False,
                                               err=type(exc).__name__)
                    text, done = None, False

            if not done and use_groq:
                text, groq_net_fail = self._stt_groq(raw_bytes, uid, key)
                engine = "groq"

            # No engine could transcribe (text None, not merely empty): offline notice, or a short spoken
            # "didn't catch that" in an active session. Passive standby noise stays silent.
            if text is None:
                if groq_net_fail:
                    self._notify_stt_offline(uid)
                elif is_active_session and hasattr(self.ctx, "say"):
                    self.ctx.say("I didn't catch that, sir.")

            if text:
                text = self._apply_corrections(text)
                self._utt_ctx = {"raw": raw_bytes, "engine": engine, "conf": conf,
                                 "dur_s": len(raw_bytes) / 32000.0}
                try:
                    self._process(text, is_command=is_command)
                except TypeError:
                    self._process(text)
                finally:
                    self._utt_ctx = None

    def _apply_corrections(self, text):
        """personal.apply_corrections on the transcript (learned "no, I said X" fixes), if enabled."""
        if not getattr(self.cfg, "learn_corrections", True):
            return text
        p = _get_personal()
        if p is None:
            return text
        try:
            return p.apply_corrections(text) or text
        except Exception as exc:
            _vlog.warning("personal.apply_corrections failed: %r", exc)
            return text

    def _save_clip(self, raw_bytes, transcript, meta):
        """Save audio + transcript through personal.save_voice_clip when learn_voice_clips is on.
        Runs on a short-lived thread so a disk write never delays the command."""
        if not getattr(self.cfg, "learn_voice_clips", True) or not raw_bytes or not transcript:
            return False
        p = _get_personal()
        if p is None:
            return False

        def _work():
            try:
                p.save_voice_clip(raw_bytes, 16000, transcript, meta)
            except Exception as exc:
                _vlog.warning("save_voice_clip failed: %r", exc)
        threading.Thread(target=_work, name="VoiceClipSave", daemon=True).start()
        return True

    def _learn_from_command(self, command):
        ctx = getattr(self, "_utt_ctx", None)
        if not ctx:
            return
        conf = ctx.get("conf")
        self._save_clip(ctx["raw"], command, {
            "kind": "command", "engine": ctx.get("engine"),
            "confidence": None if conf is None else round(float(conf), 3),
            "duration": round(ctx.get("dur_s", 0.0), 2)})

    # ------------------------------------------------------------------ onboarding / calibration API
    CAPTURE_GRACE_S = 5.0

    CAPTURE_NO_AUDIO_S = 2.0

    def _capture_raw(self, timeout, on_level):
        """One no-wake-word utterance from the live mic stream: (PCM16 bytes or None, reason). reason is ""
        on success, "no_audio" when the mic delivers no frames at all (the capture returns within ~2 s of
        that being evident instead of waiting out the whole timeout), else "no_speech" / "unavailable".
        Blocking, bounded. The detector thread records it on its own stream, so the main listen loop cannot
        collide."""
        wd = self.wake_detector
        if wd is None or self._suspended or wd.suspended:
            return None, "unavailable"
        req = wd.request_capture(timeout, on_level=on_level)
        if req is None:
            _vlog.info("capture dropped: detector busy or unavailable")
            return None, "unavailable"
        limit = float(timeout) + WakeWordDetector.CAPTURE_SPEAKING_WAIT_S + self.CAPTURE_GRACE_S
        t0 = time.monotonic()
        while not req.done.wait(0.1):
            waited = time.monotonic() - t0
            if (waited >= self.CAPTURE_NO_AUDIO_S and not req.started.is_set()
                    and wd.mic_state()[0] in ("stalled", "reconnecting", "down")):
                wd.cancel_capture(req)
                _vlog.warning("capture: no audio (mic_state=%s); giving up after %.1fs", wd.mic_state(), waited)
                return None, "no_audio"
            if waited >= limit:
                wd.cancel_capture(req)
                _vlog.warning("capture timed out after %.1fs", limit)
                return None, "unavailable"
        if req.no_audio:
            _vlog.warning("capture: no audio frames from the microphone")
            return None, "no_audio"
        return req.raw, ("" if req.raw else "no_speech")

    def _transcribe_blocking(self, raw_bytes):
        """(text, confidence|None, engine) for a PCM16 clip on the calling thread, same engine order as
        the command path: local, then Groq. Text is "" when nothing was recognised."""
        lt = self._local_stt_ready()
        if lt is not None:
            try:
                text, conf = self._stt_local(lt, raw_bytes, None, 0)
                return text or "", conf, "local"
            except Exception as exc:
                _vlog.warning("local STT failed in capture (%r); falling back", exc)
        key = get_api_key(self.cfg)
        if getattr(self.cfg, "ai_enabled", False) and key and time.time() >= self.groq_cooldown_until:
            text, _ = self._stt_groq(raw_bytes, None, key)
            return text or "", None, "groq"
        return "", None, None

    def capture_answer(self, timeout: float = 8.0, on_level=None) -> str:
        """Record ONE utterance without a wake word and return its transcript ("" on silence/timeout/
        error). It is NOT dispatched as a command. Blocking; call from a non-GUI thread.
        on_level(float 0..1) is called from the audio thread once per 80 ms chunk.
        self.last_capture_reason afterwards is "" (heard speech), "no_speech", "unavailable" or "no_audio"
        (the microphone delivered no audio: the answer failed because nothing could be heard)."""
        raw, self.last_capture_reason = self._capture_raw(timeout, on_level)
        if not raw:
            return ""
        text, conf, engine = self._transcribe_blocking(raw)
        text = self._apply_corrections(text) if text else ""
        if text:
            self._save_clip(raw, text, {"kind": "answer", "engine": engine,
                                        "confidence": None if conf is None else round(float(conf), 3),
                                        "duration": round(len(raw) / 32000.0, 2)})
        return text

    def calibrate_from_sample(self, expected_text: str, timeout: float = 12.0, on_level=None) -> dict:
        """The user reads `expected_text`. Measures speech level, noise floor and SNR, retunes the mic
        energy floor (cfg.mic_energy_threshold, persisted) when the sample was usable, saves the clip as
        a calibration sample (if learn_voice_clips) and returns
        {"ok", "transcript", "level_db", "snr_db", ...}. Blocking; call from a non-GUI thread."""
        out = {"ok": False, "transcript": "", "level_db": -120.0, "snr_db": 0.0}
        raw, reason = self._capture_raw(timeout, on_level)
        if not raw:
            if reason == "no_audio":
                out["reason"] = "no_audio"
            return out
        m = analyze_levels(raw)
        text, conf, engine = self._transcribe_blocking(raw)
        out.update(transcript=text, level_db=m["level_db"], snr_db=m["snr_db"], noise_db=m["noise_db"])
        out["ok"] = bool(text) and m["snr_db"] >= 6.0
        if out["ok"]:
            # Gate: above ~2x the room noise but never above 40% of the user's speech level.
            floor = int(min(max(m["noise_rms"] * 2.0, 100.0), max(100.0, m["speech_rms"] * 0.4)))
            out["energy_threshold"] = floor
            self._apply_energy_floor(floor)
        self._save_clip(raw, text, {"kind": "calibration", "expected": expected_text, "engine": engine,
                                    "level_db": m["level_db"], "snr_db": m["snr_db"]})
        return out

    def _apply_energy_floor(self, floor: int):
        try:
            self.cfg.mic_energy_threshold = int(floor)
            import config
            config.save_config(self.cfg)
        except Exception as exc:
            _vlog.warning("could not persist calibrated energy floor: %r", exc)

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
                pattern = r"^\s*" + _WAKE_PREFIX_RE + re.escape(alias) + r"\b"
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
            try:
                self._learn_wake_example(t, matched_alias)
            except Exception as exc:
                _vlog.warning("wake example failed: %r", exc)
            if command is None:
                jarvis_log.utterance_event(jarvis_log.DROPPED, reason="no_wake_word")
                # No wake word: ignore and clear any listening state on the island
                if hasattr(self.ctx, "overlay") and self.ctx.overlay:
                    try:
                        self.ctx.overlay.post("listening_done")
                    except Exception:
                        pass
                return

        note_explicit_command()     # accepted wake/command: spoken replies stay on for 10 s even when quiet
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
            if _wake_chime_allowed():
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

        self._learn_from_command(command)

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
