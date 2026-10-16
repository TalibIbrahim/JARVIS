"""
Voice Profile & Speaker Verification Subsystem for J.A.R.V.I.S.

Provides:
- Acoustic feature extraction: pitch (F0), 8-band formant spectral envelope,
  spectral centroid, rolloff, and zero-crossing rate.
- Voice enrollment and calibration ("train my voice", "enroll voice").
- Real-time speaker verification: filters out background speech and noise from non-owner voices.
- Continuous adaptive learning: gently updates vocal signature on confirmed high-confidence commands.
- Local JSON persistence in %APPDATA%/Jarvis/voice_profile.json.
"""

import json
import math
import os
import sys
import threading
from typing import Dict, List, Optional, Tuple
import numpy as np


def get_voice_profile_path() -> str:
    r"""Returns the persistent path to voice_profile.json."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        d = os.path.join(appdata, "Jarvis")
        try:
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "voice_profile.json")
        except Exception:
            pass
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "voice_profile.json")


class VoiceProfileManager:
    """Manages acoustic enrollment, speaker verification, and adaptive vocal training."""

    def __init__(self, profile_path: Optional[str] = None):
        self.profile_path = profile_path or get_voice_profile_path()
        self._lock = threading.RLock()
        self.is_enrolled = False
        self.enabled = True
        self.similarity_threshold = 0.58
        self.mean_vector: Optional[np.ndarray] = None
        self.pitch_mean: float = 0.0
        self.pitch_std: float = 0.0
        self.sample_count: int = 0
        self.last_score: float = 1.0
        self.load()

    def extract_features(self, audio_bytes: bytes, sample_rate: int = 16000) -> Optional[np.ndarray]:
        """
        Extracts an 11-dimensional normalized acoustic feature vector from raw 16-bit PCM mono audio.
        Returns None if audio is silent or too short (< 0.2s).
        """
        if not audio_bytes or len(audio_bytes) < int(sample_rate * 2 * 0.2):
            return None

        arr = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(arr ** 2)))
        if rms < 35.0:
            return None

        # Normalize signal to [-1.0, 1.0]
        max_val = np.max(np.abs(arr)) + 1e-6
        signal = arr / max_val
        n_samples = len(signal)

        # 1. Pitch (F0) estimation via normalized autocorrelation
        min_lag = int(sample_rate / 350)  # 350 Hz ceiling
        max_lag = int(sample_rate / 75)   # 75 Hz floor
        pitch = 140.0  # fallback nominal pitch
        if n_samples > max_lag * 2:
            seg = signal[:max_lag * 2]
            corr = np.correlate(seg, seg, mode="full")
            corr = corr[len(seg) - 1:]
            if max_lag < len(corr):
                search_region = corr[min_lag:max_lag]
                if len(search_region) > 0 and np.max(search_region) > 0:
                    peak_idx = min_lag + int(np.argmax(search_region))
                    if peak_idx > 0:
                        pitch = float(sample_rate / peak_idx)

        # 2. Magnitude Spectrum via FFT
        # Use center window of ~2048 to 4096 samples for spectral analysis
        fft_len = min(4096, int(2 ** math.floor(math.log2(n_samples))))
        fft_len = max(512, fft_len)
        mid = n_samples // 2
        chunk = signal[max(0, mid - fft_len // 2): min(n_samples, mid + fft_len // 2)]
        if len(chunk) < fft_len:
            chunk = np.pad(chunk, (0, fft_len - len(chunk)))

        window = np.hanning(len(chunk))
        spectrum = np.abs(np.fft.rfft(chunk * window))
        freqs = np.fft.rfftfreq(len(chunk), 1.0 / sample_rate)

        # 3. 8-Band Formant Energy Distribution
        band_ranges = [
            (80, 280),
            (280, 550),
            (550, 1000),
            (1000, 1750),
            (1750, 2500),
            (2500, 3500),
            (3500, 5000),
            (5000, 8000),
        ]
        band_energies = []
        total_spec_energy = np.sum(spectrum ** 2) + 1e-9
        for low, high in band_ranges:
            idx = np.where((freqs >= low) & (freqs < high))[0]
            if len(idx) > 0:
                e = np.sum(spectrum[idx] ** 2) / total_spec_energy
            else:
                e = 0.0
            band_energies.append(float(e))

        # 4. Spectral Centroid & Rolloff
        centroid = float(np.sum(freqs * spectrum) / (np.sum(spectrum) + 1e-9))
        cum_energy = np.cumsum(spectrum ** 2)
        rolloff_idx = np.where(cum_energy >= 0.85 * cum_energy[-1])[0]
        rolloff = float(freqs[rolloff_idx[0]]) if len(rolloff_idx) > 0 else 4000.0

        # Assemble normalized feature vector [pitch, 8 band energies, centroid, rolloff]
        vec = np.array([
            min(max(pitch / 300.0, 0.0), 1.0),
            *band_energies,
            min(max(centroid / 4000.0, 0.0), 1.0),
            min(max(rolloff / 6000.0, 0.0), 1.0),
        ], dtype=np.float32)

        # Normalize vector to unit norm for stable cosine similarity
        norm = np.linalg.norm(vec)
        return (vec / norm) if norm > 1e-6 else vec

    def enroll(self, sample_bytes_list: List[bytes]) -> Tuple[bool, str]:
        """Enrolls a new voice profile from one or more raw audio byte chunks."""
        vectors = []
        pitches = []
        for raw in sample_bytes_list:
            feat = self.extract_features(raw)
            if feat is not None:
                vectors.append(feat)
                # Denormalize pitch for statistics
                pitches.append(feat[0] * 300.0)

        if len(vectors) < 1:
            return False, "Insufficient clear speech captured for voice calibration."

        with self._lock:
            mat = np.stack(vectors, axis=0)
            self.mean_vector = np.mean(mat, axis=0)
            self.mean_vector /= (np.linalg.norm(self.mean_vector) + 1e-9)
            self.pitch_mean = float(np.mean(pitches))
            self.pitch_std = float(np.std(pitches)) if len(pitches) > 1 else 15.0
            self.sample_count = len(vectors)
            self.is_enrolled = True
            self.enabled = True
            self.save()

        return True, f"Voice profile calibrated with {len(vectors)} sample(s). Pitch: ~{self.pitch_mean:.0f} Hz."

    def verify(self, audio_bytes: bytes) -> Tuple[bool, float]:
        """
        Compares incoming audio against the enrolled voice profile.
        Returns (is_match, similarity_score 0.0 to 1.0).
        If not enrolled or disabled, passes unconditionally with score 1.0.
        """
        if not self.is_enrolled or not self.enabled or self.mean_vector is None:
            return True, 1.0

        feat = self.extract_features(audio_bytes)
        if feat is None:
            return False, 0.0

        with self._lock:
            # Cosine similarity between feature vectors
            sim = float(np.dot(self.mean_vector, feat))
            sim = max(0.0, min(1.0, sim))

            # Pitch compatibility check (penalize if pitch is drastically different)
            curr_pitch = feat[0] * 300.0
            pitch_diff = abs(curr_pitch - self.pitch_mean)
            allowed_diff = max(45.0, self.pitch_std * 3.0)
            if pitch_diff > allowed_diff:
                penalty = min((pitch_diff - allowed_diff) / 100.0, 0.25)
                sim = max(0.0, sim - penalty)

            self.last_score = sim
            is_match = sim >= self.similarity_threshold

            # Continuous adaptive learning: gently update vocal signature on high-confidence matches
            if is_match and sim > 0.82:
                self.adapt(feat)

            return is_match, sim

    def adapt(self, feat: np.ndarray, alpha: float = 0.04) -> None:
        """Gently updates profile vector over time to adapt to acoustic variations."""
        with self._lock:
            if self.mean_vector is None:
                return
            updated = (1.0 - alpha) * self.mean_vector + alpha * feat
            updated /= (np.linalg.norm(updated) + 1e-9)
            self.mean_vector = updated
            self.sample_count += 1
            # Save asynchronously or periodically
            if self.sample_count % 10 == 0:
                self.save()

    def reset(self) -> None:
        """Clears the enrolled voice profile."""
        with self._lock:
            self.is_enrolled = False
            self.mean_vector = None
            self.pitch_mean = 0.0
            self.pitch_std = 0.0
            self.sample_count = 0
            if os.path.exists(self.profile_path):
                try:
                    os.remove(self.profile_path)
                except Exception:
                    pass

    def save(self) -> bool:
        """Saves voice profile to disk."""
        if self.mean_vector is None:
            return False
        data = {
            "version": 1,
            "is_enrolled": self.is_enrolled,
            "enabled": self.enabled,
            "similarity_threshold": self.similarity_threshold,
            "mean_vector": self.mean_vector.tolist(),
            "pitch_mean": self.pitch_mean,
            "pitch_std": self.pitch_std,
            "sample_count": self.sample_count,
        }
        try:
            with open(self.profile_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as exc:
            print(f"[voice_profile] failed to save profile: {exc}")
            return False

    def load(self) -> bool:
        """Loads voice profile from disk if present."""
        if not os.path.exists(self.profile_path):
            return False
        try:
            with open(self.profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "mean_vector" in data:
                self.mean_vector = np.array(data["mean_vector"], dtype=np.float32)
                self.is_enrolled = bool(data.get("is_enrolled", True))
                self.enabled = bool(data.get("enabled", True))
                self.similarity_threshold = float(data.get("similarity_threshold", 0.58))
                self.pitch_mean = float(data.get("pitch_mean", 0.0))
                self.pitch_std = float(data.get("pitch_std", 15.0))
                self.sample_count = int(data.get("sample_count", 1))
                return True
        except Exception as exc:
            print(f"[voice_profile] failed to load profile: {exc}")
        return False


# Process-wide singleton
_GLOBAL_VOICE_PROFILE: Optional[VoiceProfileManager] = None


def get_voice_profile() -> VoiceProfileManager:
    """Returns the shared VoiceProfileManager instance."""
    global _GLOBAL_VOICE_PROFILE
    if _GLOBAL_VOICE_PROFILE is None:
        _GLOBAL_VOICE_PROFILE = VoiceProfileManager()
    return _GLOBAL_VOICE_PROFILE
