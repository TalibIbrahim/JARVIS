"""Acoustic echo canceller for the optional "voice over music" setting (no Qt).

Captures the default speaker output via WASAPI loopback (PyAudioWPatch, on its
OWN PyAudio instance, never the shared one in voice.py) and feeds it as the
far-end reference to the WebRTC audio processing module.

DUAL-PATH: process() returns the AEC output, which suppresses the user's voice
during double-talk. Callers must keep the raw mic for recording and run
wake/VAD on both raw and AEC audio. Never replace the raw mic with this.

Any failure degrades to returning the input unchanged (one warning logged).
"""
from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

SR = 16000
FRAME = 160                    # 10 ms, the APM frame size
RING_SECONDS = 1.0
DEFAULT_DELAY_MS = 80
SILENCE_PEAK = 8               # int16 peak below this counts as "no playback"
HANGOVER_S = 1.5               # keep cancelling this long after playback stops (room tail)
MAX_BACKLOG = SR * 3 // 10     # reference older than 300 ms is dropped


class EchoCanceller:
    def __init__(self, delay_ms: int = DEFAULT_DELAY_MS):
        self._delay_ms = int(delay_ms)
        self._lock = threading.Lock()          # guards the ring + counters below
        self._ring = np.zeros(int(SR * RING_SECONDS), dtype=np.int16)
        self._wpos = 0                         # total samples written
        self._rpos = 0                         # total samples consumed
        self._pa = None
        self._stream = None
        self._device = ""
        self._apm = None                       # created lazily on the process() thread
        self._enabled = False
        self._failed = False
        self._warned = False
        self._fir = None
        self._fir_zi = None
        self._factor = 1
        self._rate = SR
        self._channels = 1
        self._active_until = 0.0
        self._frames = 0
        self._cpu_s = 0.0
        self._e_in = 1e-9
        self._e_out = 1e-9

    def _warn(self, msg: str) -> None:
        self._failed = True
        if not self._warned:
            self._warned = True
            log.warning("EchoCanceller disabled: %s", msg)

    # ----------------------------------------------------------- loopback in
    def start(self) -> bool:
        """Open the default WASAPI loopback. Returns False (and logs once) on any failure."""
        if self._stream is not None:
            return True
        try:
            import pyaudiowpatch as pyaudio
            self._pa = pyaudio.PyAudio()
            info = self._pa.get_default_wasapi_loopback()
            self._rate = int(info["defaultSampleRate"])
            self._channels = max(1, int(info["maxInputChannels"]))
            self._device = str(info["name"])
            self._setup_resampler()
            self._stream = self._pa.open(
                format=pyaudio.paInt16, channels=self._channels, rate=self._rate,
                input=True, input_device_index=int(info["index"]),
                frames_per_buffer=int(self._rate * 0.02),
                stream_callback=self._callback)
            self._enabled = True
            return True
        except Exception as e:  # no loopback device, driver error, missing module
            self._warn(f"loopback unavailable ({type(e).__name__}: {e})")
            self.close()
            return False

    def _setup_resampler(self) -> None:
        from scipy.signal import firwin
        if self._rate % SR == 0:
            self._factor = self._rate // SR
            self._fir = firwin(63, 0.45 * SR, fs=self._rate).astype(np.float64)
            self._fir_zi = np.zeros(len(self._fir) - 1)
        else:
            self._factor = 0   # rational resample_poly per block

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        try:
            x = np.frombuffer(in_data, dtype=np.int16).reshape(-1, self._channels)
            self._push(x.astype(np.float64).mean(axis=1), self._rate)
            return (None, pyaudio.paContinue)
        except Exception as e:
            self._warn(f"loopback callback failed ({type(e).__name__}: {e})")
            return (None, pyaudio.paAbort)

    def _push(self, mono: np.ndarray, rate: int) -> None:
        """Resample a block of mono samples at `rate` to 16 kHz and append to the ring."""
        if rate == SR:
            y = mono
        elif self._factor:
            from scipy.signal import lfilter
            f, self._fir_zi = lfilter(self._fir, 1.0, mono, zi=self._fir_zi)
            y = f[::self._factor]
        else:
            from scipy.signal import resample_poly
            g = math.gcd(SR, rate)
            y = resample_poly(mono, SR // g, rate // g)
        self.feed_reference(np.clip(np.round(y), -32768, 32767).astype(np.int16))

    def feed_reference(self, ref16: np.ndarray) -> None:
        """Append 16 kHz mono int16 reference samples (loopback callback and test hook)."""
        n = len(ref16)
        if n == 0:
            return
        size = len(self._ring)
        if n >= size:
            ref16 = ref16[-size:]
            n = size
        with self._lock:
            i = self._wpos % size
            first = min(n, size - i)
            self._ring[i:i + first] = ref16[:first]
            if first < n:
                self._ring[:n - first] = ref16[first:]
            self._wpos += n
            if self._wpos - self._rpos > size:      # overwritten before being read
                self._rpos = self._wpos - size

    def _take_reference(self, n: int) -> np.ndarray:
        """Oldest n unread reference samples; zero-padded if the loopback delivered less."""
        size = len(self._ring)
        with self._lock:
            if self._wpos - self._rpos > MAX_BACKLOG:
                self._rpos = self._wpos - MAX_BACKLOG
            avail = min(n, self._wpos - self._rpos)
            idx = (self._rpos + np.arange(avail)) % size
            out = np.zeros(n, dtype=np.int16)
            out[:avail] = self._ring[idx]
            self._rpos += avail
        return out

    # --------------------------------------------------------------- process
    def process(self, mic_chunk_int16: np.ndarray) -> np.ndarray:
        """Return the AEC output for a mic chunk (same length). Never raises."""
        try:
            if not self._enabled or self._failed:
                return mic_chunk_int16
            n = len(mic_chunk_int16)
            ref = self._take_reference(n)
            now = time.monotonic()
            playing = int(np.abs(ref.astype(np.int32)).max(initial=0)) >= SILENCE_PEAK
            if playing:
                self._active_until = now + HANGOVER_S
            elif now > self._active_until:
                return mic_chunk_int16
            if self._apm is None:
                self._init_apm()
            mic = np.ascontiguousarray(mic_chunk_int16, dtype=np.int16)
            out = mic.copy()
            t0 = time.process_time()
            for i in range(0, n - FRAME + 1, FRAME):
                self._apm.process_reverse_stream(ref[i:i + FRAME].tobytes())
                out[i:i + FRAME] = np.frombuffer(
                    self._apm.process_stream(mic[i:i + FRAME].tobytes()), dtype=np.int16)
                self._frames += 1
            self._cpu_s += time.process_time() - t0
            if playing:
                self._e_in = 0.9 * self._e_in + 0.1 * float(np.mean(mic.astype(np.float64) ** 2) + 1e-9)
                self._e_out = 0.9 * self._e_out + 0.1 * float(np.mean(out.astype(np.float64) ** 2) + 1e-9)
            return out
        except Exception as e:
            self._warn(f"process failed ({type(e).__name__}: {e})")
            return mic_chunk_int16

    def _init_apm(self) -> None:
        from aec_audio_processing import AudioProcessor
        ap = AudioProcessor(enable_aec=True, enable_ns=False, ns_level=1,
                            enable_agc=False, enable_vad=False)
        ap.set_stream_format(SR, 1)
        ap.set_reverse_stream_format(SR, 1)
        ap.set_stream_delay(self._delay_ms)
        self._apm = ap

    # ------------------------------------------------------------- lifecycle
    def enable_for_test(self) -> None:
        """Test hook: skip the loopback device; reference comes from feed_reference()."""
        self._enabled = True
        self._device = "test-hook"

    def stop(self) -> None:
        self.close()

    def close(self) -> None:
        stream, pa = self._stream, self._pa
        self._stream = None
        self._pa = None
        self._enabled = False
        try:
            if stream is not None:
                try:
                    if stream.is_active():
                        stream.stop_stream()
                finally:
                    stream.close()
        finally:
            if pa is not None:
                pa.terminate()

    def stats(self) -> dict:
        audio_s = self._frames * FRAME / SR
        return {
            "enabled": bool(self._enabled and not self._failed),
            "loopback_device": self._device,
            "erle_db_estimate": round(10 * math.log10(self._e_in / self._e_out), 1),
            "cpu_ms_per_s": round(1000 * self._cpu_s / audio_s, 2) if audio_s else 0.0,
            "frames": self._frames,
        }
