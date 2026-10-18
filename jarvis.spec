# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import copy_metadata

datas = [('assets', 'assets')]
datas += collect_data_files('mediapipe')
datas += collect_data_files('certifi')
datas += collect_data_files('piper')
datas += copy_metadata('mediapipe')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['PySide6', 'speech_recognition', 'pyttsx3.drivers', 'pyttsx3.drivers.sapi5', 'keyring', 'keyring.backends.Windows', 'win32cred', 'pywintypes', 'certifi', 'requests', 'piper', 'sounddevice', '_sounddevice', 'onnxruntime', 'scipy', 'scipy.signal', 'psutil', 'pynvml', 'PIL', 'PIL.Image'],
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
