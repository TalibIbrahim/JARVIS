# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.hooks import collect_dynamic_libs

datas = [('assets', 'assets')]
datas += collect_data_files('mediapipe')
datas += collect_data_files('certifi')
datas += collect_data_files('piper')
datas += collect_data_files('openwakeword')
datas += collect_data_files('faster_whisper')   # local STT; the Whisper model itself is NOT bundled (first-run download)
datas += copy_metadata('mediapipe')
datas += copy_metadata('openwakeword')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=collect_dynamic_libs('ctranslate2'),
    datas=datas,
    hiddenimports=['PySide6', 'speech_recognition', 'pyttsx3.drivers', 'pyttsx3.drivers.sapi5', 'keyring', 'keyring.backends.Windows', 'win32cred', 'pywintypes', 'certifi', 'requests', 'piper', 'sounddevice', '_sounddevice', 'onnxruntime', 'scipy', 'scipy.signal', 'psutil', 'pynvml', 'PIL', 'PIL.Image', 'pycaw', 'comtypes', 'openwakeword', 'openwakeword.model', 'openwakeword.vad', 'aec_audio_processing', 'faster_whisper', 'ctranslate2', 'local_stt', 'tokenizers', 'huggingface_hub', 'av', 'pyaudiowpatch'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='JARVIS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/jarvis.ico'],
)
