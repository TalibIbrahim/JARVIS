"""Manual (opens the mic): ambient VAD statistics. Never saves or prints audio.

Usage: python tools/repro/vad_ambient.py [--seconds 10]
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import voice  # noqa: E402
from config import load_config  # noqa: E402

CHUNK = 1280
RATE = 16000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    a = ap.parse_args()
    cfg = load_config()
    idx, name = voice.find_best_microphone(cfg)
    print(f"mic: [{idx}] {name}")
    vad = voice.VADDetector()
    print(f"energy_threshold={vad.energy_threshold} vad_threshold={vad.vad_threshold} "
          f"silero={'yes' if vad._vad else 'NO'}")
    import pyaudio
    p = voice.get_shared_pyaudio()
    stream = p.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True,
                    input_device_index=idx, frames_per_buffer=CHUNK)
    rmss, probs, speech, fallback_only = [], [], 0, 0
    n = int(a.seconds * RATE / CHUNK)
    try:
        for _ in range(n):
            data = stream.read(CHUNK, exception_on_overflow=False)
            chunk = np.frombuffer(data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
            prob = None
            if vad._vad is not None:
                try:
                    prob = float(vad._vad.predict(chunk))
                except Exception:
                    prob = None
            flagged = vad.is_speech(chunk, rms)
            rmss.append(rms)
            if prob is not None:
                probs.append(prob)
            speech += bool(flagged)
            if flagged and rms >= vad.energy_threshold and (prob is None or prob < vad.vad_threshold):
                fallback_only += 1
    finally:
        stream.stop_stream()
        stream.close()
    r = np.array(rmss)
    print(f"chunks={len(r)}  RMS p10/p50/p90/max = {np.percentile(r, 10):.0f}/"
          f"{np.percentile(r, 50):.0f}/{np.percentile(r, 90):.0f}/{r.max():.0f}")
    if probs:
        pr = np.array(probs)
        print(f"Silero p50/p90 = {np.percentile(pr, 50):.3f}/{np.percentile(pr, 90):.3f}")
    print(f"flagged speech: {100 * speech / len(r):.1f}%   "
          f"flagged ONLY by RMS fallback: {100 * fallback_only / len(r):.1f}%")


if __name__ == "__main__":
    main()
