"""
Build script to package J.A.R.V.I.S. into a standalone Windows executable.
Run: python build_exe.py
"""
import os
import subprocess
import sys


def build():
    print("[build] Packaging J.A.R.V.I.S. into standalone JARVIS.exe...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=JARVIS",
        "--onefile",  # standalone single-file executable for direct download & run
        "--windowed",  # suppresses standard console window
        "--noconfirm",
        "--collect-data", "mediapipe",
        "--copy-metadata", "mediapipe",
        "--icon=assets/jarvis.ico",
        "--add-data=assets;assets",
        "--hidden-import", "PySide6",
        "--hidden-import", "speech_recognition",
        "--hidden-import", "pyttsx3.drivers",
        "--hidden-import", "pyttsx3.drivers.sapi5",
        "--hidden-import", "keyring",
        "--hidden-import", "keyring.backends.Windows",
        "--hidden-import", "win32cred",
        "--hidden-import", "pywintypes",
        "--hidden-import", "certifi",
        "--hidden-import", "requests",
        "--hidden-import", "piper",
        "--hidden-import", "sounddevice",
        "--hidden-import", "_sounddevice",
        "--hidden-import", "onnxruntime",
        "--hidden-import", "scipy",
        "--hidden-import", "scipy.signal",
        "--hidden-import", "psutil",
        "--hidden-import", "pynvml",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "pycaw",
        "--hidden-import", "comtypes",
        "--collect-data", "certifi",
        "--collect-data", "piper",
        "--collect-data", "openwakeword",
        "--copy-metadata", "openwakeword",
        "--hidden-import", "openwakeword",
        "--hidden-import", "openwakeword.model",
        "--hidden-import", "openwakeword.vad",
        "main.py"
    ]

    print("[build] Command:", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode == 0:
        print("\n[build] SUCCESS! Output generated: dist/JARVIS.exe")
    else:
        print(f"\n[build] FAILED with return code {res.returncode}")
        sys.exit(res.returncode)


if __name__ == "__main__":
    build()
