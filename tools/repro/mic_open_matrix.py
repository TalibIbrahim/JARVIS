"""Manual (opens each input device briefly): pyaudio open matrix at 16 kHz mono, 1280 frames.

Usage: python tools/repro/mic_open_matrix.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import voice  # noqa: E402
from config import load_config  # noqa: E402


def main():
    import pyaudio
    p = voice.get_shared_pyaudio()
    print(f"{'idx':>3} {'hostapi':<22} {'result':<6} name / error")
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) <= 0:
            continue
        host = p.get_host_api_info_by_index(info["hostApi"])["name"]
        try:
            s = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                       input_device_index=i, frames_per_buffer=1280)
            try:
                s.read(1280, exception_on_overflow=False)
            finally:
                s.stop_stream()
                s.close()
            print(f"{i:>3} {host:<22} OK     {info['name']}")
        except Exception as e:
            print(f"{i:>3} {host:<22} FAIL   {info['name']}  -> {e}")
    cfg = load_config()
    print("find_best_microphone(cfg)                      ->", voice.find_best_microphone(cfg))
    print("find_best_microphone(cfg, probe_working=True)  ->",
          voice.find_best_microphone(cfg, probe_working=True))
    print("find_best_microphone(cfg, require_16k=True)    ->",
          voice.find_best_microphone(cfg, require_16k=True), " (what the pipeline uses)")


if __name__ == "__main__":
    main()
