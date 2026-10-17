"""
Audio chime helper module for J.A.R.V.I.S.
Synthesizes and plays a soft ascending two-note activation chime (<150 ms)
with a raised cosine envelope. Supports non-blocking playback, mutable volume,
and thread-safe PortAudio interaction.
"""
import sys
import threading
import time
import numpy as np

# Global mutable volume and mute configuration
_CHIME_MUTED = False
_CHIME_VOLUME = 0.10
_CHIME_LOCK = threading.Lock()
_AUDIO_LOCK = None


def set_audio_lock(lock):
    """Sets the shared PortAudio lock for thread-safe output streaming."""
    global _AUDIO_LOCK
    _AUDIO_LOCK = lock


def get_audio_lock():
    global _AUDIO_LOCK
    if _AUDIO_LOCK is None:
        try:
            from voice import _PORTAUDIO_LOCK
            _AUDIO_LOCK = _PORTAUDIO_LOCK
        except (ImportError, AttributeError):
            _AUDIO_LOCK = threading.RLock()
    return _AUDIO_LOCK


def synthesize_chime(
    sample_rate: int = 44100,
    f1: float = 659.25,     # E5
    f2: float = 1046.50,    # C6
    d1: float = 0.060,      # 60 ms note 1
    d2: float = 0.070,      # 70 ms note 2
    gap: float = 0.005,     # 5 ms inter-note silence
    volume: float = 0.10,
) -> np.ndarray:
    """
    Synthesizes a soft, ascending two-note chime with a smooth raised cosine (Hann) envelope.
    Total duration: ~135 ms (strictly < 150 ms target).
    Zero DC offset, zero clicks/pops, soft and pleasant.
    """
    # Note 1: E5 with raised cosine envelope
    n_samples_1 = int(sample_rate * d1)
    t1 = np.linspace(0, d1, n_samples_1, endpoint=False, dtype=np.float32)
    env1 = (0.5 * (1.0 - np.cos(2.0 * np.pi * np.linspace(0, 1, n_samples_1, endpoint=False)))).astype(np.float32)
    note1 = np.sin(2.0 * np.pi * f1 * t1) * env1

    # Note 2: C6 with raised cosine envelope
    n_samples_2 = int(sample_rate * d2)
    t2 = np.linspace(0, d2, n_samples_2, endpoint=False, dtype=np.float32)
    env2 = (0.5 * (1.0 - np.cos(2.0 * np.pi * np.linspace(0, 1, n_samples_2, endpoint=False)))).astype(np.float32)
    note2 = np.sin(2.0 * np.pi * f2 * t2) * env2

    # Natural micro-gap
    silence = np.zeros(int(sample_rate * gap), dtype=np.float32)

    # Concatenate and apply scaling
    chime = np.concatenate([note1, silence, note2]) * float(volume)
    return chime.astype(np.float32)


# Precompute standard waveform at import time for zero runtime latency
_DEFAULT_SR = 44100
_PRECOMPUTED_CHIME = synthesize_chime(sample_rate=_DEFAULT_SR, volume=_CHIME_VOLUME)


def set_chime_muted(muted: bool):
    """Mutes or unmutes wake chime feedback."""
    global _CHIME_MUTED
    with _CHIME_LOCK:
        _CHIME_MUTED = bool(muted)


def is_chime_muted() -> bool:
    """Returns True if wake chime is muted."""
    with _CHIME_LOCK:
        return _CHIME_MUTED


def set_chime_volume(volume: float):
    """Sets chime output volume (0.0 to 1.0) and updates precomputed buffer."""
    global _CHIME_VOLUME, _PRECOMPUTED_CHIME
    with _CHIME_LOCK:
        _CHIME_VOLUME = float(np.clip(volume, 0.0, 1.0))
        _PRECOMPUTED_CHIME = synthesize_chime(sample_rate=_DEFAULT_SR, volume=_CHIME_VOLUME)


def get_chime_volume() -> float:
    """Returns current chime volume."""
    with _CHIME_LOCK:
        return _CHIME_VOLUME


def _play_raw(buffer: np.ndarray, sample_rate: int):
    """Internal playback worker."""
    try:
        import sounddevice as sd
        lock = get_audio_lock()
        if lock is not None:
            with lock:
                sd.play(buffer, samplerate=sample_rate)
        else:
            sd.play(buffer, samplerate=sample_rate)
        sd.wait()
    except Exception as exc:
        # Non-critical: failure to render chime (e.g. headless CI or disabled output) should never crash
        pass


def play_chime(non_blocking: bool = True, muted: bool = None, volume: float = None):
    """
    Plays the soft ascending two-note activation chime.
    If non_blocking is True (default), starts playback in a daemon thread and returns immediately (<1 ms).
    Target playback duration is <150 ms.
    """
    if muted is True or (muted is None and is_chime_muted()):
        return

    vol = _CHIME_VOLUME if volume is None else float(np.clip(volume, 0.0, 1.0))
    if vol <= 0.001:
        return

    with _CHIME_LOCK:
        if volume is not None and abs(volume - _CHIME_VOLUME) > 0.001:
            buffer = synthesize_chime(sample_rate=_DEFAULT_SR, volume=vol)
        else:
            buffer = _PRECOMPUTED_CHIME

    if non_blocking:
        th = threading.Thread(target=_play_raw, args=(buffer, _DEFAULT_SR), daemon=True)
        th.start()
    else:
        _play_raw(buffer, _DEFAULT_SR)


play_wake_chime = play_chime
