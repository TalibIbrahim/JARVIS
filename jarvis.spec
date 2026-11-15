# -*- mode: python ; coding: utf-8 -*-
# Canonical PyInstaller build for J.A.R.V.I.S. (build_exe.py just runs this spec).
#   python -m PyInstaller --noconfirm jarvis.spec      ->  dist/JARVIS/JARVIS.exe  (+ dist/JARVIS/_internal)
# One-folder (onedir) build: nothing is unpacked to %TEMP% on launch, so cold start is much faster than onefile.
# UPX is off (slow to build/launch and a common antivirus false-positive trigger).
# models/ (Piper voice, Whisper weights) is intentionally NOT bundled; the app loads them from %APPDATA%\Jarvis.
import importlib.util
import os

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.hooks import collect_dynamic_libs

_spec = importlib.util.spec_from_file_location('jarvis_version_info', os.path.join(SPECPATH, 'installer', 'version_info.py'))
_vi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vi)

datas = [('assets', 'assets')]
datas += collect_data_files('mediapipe')
datas += collect_data_files('certifi')
datas += collect_data_files('piper')
datas += collect_data_files('openwakeword')
datas += collect_data_files('faster_whisper')   # local STT; the Whisper model itself is NOT bundled (first-run download)
datas += copy_metadata('mediapipe')
datas += copy_metadata('openwakeword')

# Never imported at runtime (verified against a full import of the app's runtime deps):
#  - tkinter: overlay.py only falls back to it when PySide6 is missing, and PySide6 is always bundled.
#  - jax/jaxlib (~240 MB): only mediapipe.tasks.python.genai.converter (LLM weight converter) imports them.
#  - sklearn: only openwakeword.custom_verifier_model (training), which voice.py stubs out.
#  - pytest/pygments/narwhals/IPython/pandas: dev/test tooling pulled in transitively by the analyser.
# matplotlib is NOT excluded: mediapipe.python.solutions.drawing_utils imports matplotlib.pyplot on `import mediapipe`.
excludes = [
    'tkinter', '_tkinter', 'turtle', 'idlelib',
    'jax', 'jaxlib',
    'sklearn',
    'pytest', '_pytest', 'pluggy', 'pygments', 'narwhals', 'IPython', 'pandas',
]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=collect_dynamic_libs('ctranslate2'),
    datas=datas,
    hiddenimports=['PySide6', 'speech_recognition', 'pyttsx3.drivers', 'pyttsx3.drivers.sapi5', 'keyring', 'keyring.backends.Windows', 'win32cred', 'pywintypes', 'certifi', 'requests', 'piper', 'sounddevice', '_sounddevice', 'onnxruntime', 'scipy', 'scipy.signal', 'psutil', 'pynvml', 'PIL', 'PIL.Image', 'pycaw', 'comtypes', 'openwakeword', 'openwakeword.model', 'openwakeword.vad', 'aec_audio_processing', 'faster_whisper', 'ctranslate2', 'local_stt', 'tokenizers', 'huggingface_hub', 'av', 'pyaudiowpatch'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Safety net: never ship anything from the local models/ folder (gitignored voice/STT weights).
_models_prefix = 'models' + os.sep
a.datas = [d for d in a.datas if not (d[0].startswith(_models_prefix) or d[0].startswith('models/'))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='JARVIS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/jarvis.ico'],
    version=_vi.make_version_info(SPECPATH),
    manifest=_vi.MANIFEST_XML,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='JARVIS',
)
