"""voice._import_openwakeword_lean: skips sklearn, still loads and runs the wake model."""
import subprocess
import sys
from pathlib import Path

import pytest

import importlib.util

if importlib.util.find_spec("openwakeword") is None:
    pytest.skip("openwakeword not installed", allow_module_level=True)

_REPO = str(Path(__file__).resolve().parent.parent)


def _run(code, timeout=120):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=timeout, cwd=_REPO)


def test_lean_import_does_not_load_sklearn():
    r = _run("import sys, voice\n"
             "voice._import_openwakeword_lean()\n"
             "import openwakeword.model\n"
             "bad = [m for m in sys.modules if m == 'sklearn' or m.startswith('sklearn.')]\n"
             "print('SKLEARN', bool(bad))\n")
    assert r.returncode == 0, r.stderr[-2000:]
    assert "SKLEARN False" in r.stdout


def test_model_loads_and_predicts_on_zeros():
    r = _run("import numpy as np, voice\n"
             "voice._import_openwakeword_lean()\n"
             "from openwakeword.model import Model\n"
             "m = Model(wakeword_models=['hey_jarvis'], inference_framework='onnx')\n"
             "out = m.predict(np.zeros(1280, dtype=np.int16))\n"
             "assert isinstance(out, dict), type(out)\n"
             "assert any('hey_jarvis' in k for k in out), list(out)\n"
             "print('OK')\n")
    assert r.returncode == 0, r.stderr[-2000:]
    assert "OK" in r.stdout


def test_custom_verifier_raises_clear_error():
    r = _run("import voice\n"
             "voice._import_openwakeword_lean()\n"
             "import openwakeword\n"
             "try:\n"
             "    openwakeword.train_custom_verifier()\n"
             "except NotImplementedError as e:\n"
             "    print('NIE', e)\n")
    assert r.returncode == 0, r.stderr[-2000:]
    assert "NIE" in r.stdout and "custom verifier" in r.stdout
