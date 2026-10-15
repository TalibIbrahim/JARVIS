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
        "main.py"
    ]

    print("[build] Command:", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode == 0:
        print("\n[build] SUCCESS! Output generated: dist/JARVIS.exe")
    else:
        print(f"\n[build] FAILED with return code {res.returncode}")


if __name__ == "__main__":
    build()
