"""Helpers for the voice replay harness: Piper synthesis, ambient noise, fake mic stream."""
import os
import numpy as np

SR = 16000
CHUNK = 1280  # 80 ms, matches WakeWordDetector
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(ROOT, "models", "voice", "jarvis-medium.onnx")
MODEL_JSON = MODEL + ".json"


def rms(x) -> float:
    x = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(x ** 2))) if x.size else 0.0


def synth_16k(voice, text: str) -> np.ndarray:
    """Synthesize `text` with Piper and return 16 kHz int16 mono."""
    import voice as voice_mod
    audio, sr = voice_mod._synthesize_sentence_audio(voice, text, target_sr=SR)
    if sr != SR:  # defensive
        from scipy.signal import resample_poly
        audio = resample_poly(audio, SR, sr)
    return np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)


def make_noise(kind: str, seconds: float, target_rms: float = 400.0, seed: int = 1234) -> np.ndarray:
    """Seeded noise at `target_rms` (int16). kind: 'pink' | 'brown' | 'white'."""
    n = int(seconds * SR)
    rng = np.random.default_rng(seed)
    w = rng.standard_normal(n)
    if kind == "white":
        x = w
    else:
        spec = np.fft.rfft(w)
        f = np.fft.rfftfreq(n, 1.0 / SR)
        f[0] = f[1]
        spec = spec / (f ** (0.5 if kind == "pink" else 1.0))
        x = np.fft.irfft(spec, n)
    x = x / (rms(x) or 1.0) * target_rms
    return np.clip(x, -32768, 32767).astype(np.int16)


def compose(speech: np.ndarray, noise_kind: str = "pink", lead_s: float = 2.0, tail_s: float = 3.0,
            noise_rms: float = 400.0, speech_ratio: float = 2.5, seed: int = 1234):
    """noise(lead) + speech*scale + noise (over noise), then noise(tail).
    Returns (stream_int16, speech_start_sample, speech_len_samples)."""
    total = lead_s + len(speech) / SR + tail_s
    noise = make_noise(noise_kind, total, noise_rms, seed).astype(np.float64)
    sp = speech.astype(np.float64)
    sp = sp / (rms(sp[np.abs(sp) > 0.02 * np.abs(sp).max()]) or 1.0) * noise_rms * speech_ratio
    start = int(lead_s * SR)
    mix = noise.copy()
    mix[start:start + len(sp)] += sp
    return np.clip(mix, -32768, 32767).astype(np.int16), start, len(sp)


class FakeStream:
    """PyAudio-like input stream over a fixed int16 buffer."""

    def __init__(self, samples: np.ndarray, start_sample: int = 0, on_exhausted: str = "empty",
                 filler: np.ndarray = None):
        self.samples = np.asarray(samples, dtype=np.int16)
        self.pos = int(start_sample)
        self.on_exhausted = on_exhausted  # "empty" -> b"" ; "noise" -> repeat filler
        self.filler = filler if filler is not None else make_noise("pink", 5.0)
        self._fpos = 0

    def read(self, n, exception_on_overflow=False):
        if self.pos < len(self.samples):
            out = self.samples[self.pos:self.pos + n]
            self.pos += n
            if len(out) < n:
                out = np.concatenate([out, np.zeros(n - len(out), dtype=np.int16)])
            return out.tobytes()
        if self.on_exhausted == "noise":
            idx = (self._fpos + np.arange(n)) % len(self.filler)
            self._fpos += n
            return self.filler[idx].tobytes()
        return b""

    def get_read_available(self):
        # Once the buffer is exhausted keep reporting a full chunk so the stall-wait is not hit;
        # read() then returns b"" (empty mode) or filler noise.
        return max(CHUNK, len(self.samples) - self.pos)
