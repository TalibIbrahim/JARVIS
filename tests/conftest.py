"""
tests/conftest.py — Pytest Configuration for J.A.R.V.I.S. Test Suites.

Test isolation: APPDATA / LOCALAPPDATA are redirected to a temp tree for the whole
session (applied at import time so collection-time imports see it too), the privacy
log singleton is pointed at a temp file, and jarvis_log.setup() side effects
(stdout/stderr tee, excepthooks, file handler, crash file, heartbeat monitor) are
undone after every test.
"""

import faulthandler
import logging
import os
import sys
import tempfile
import threading

import pytest

# --- session-wide redirect of user data dirs (before any test module is imported) ---
_REAL_APPDATA = os.environ.get("APPDATA")
_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")
_ISO_ROOT = tempfile.mkdtemp(prefix="jarvis_tests_")
_ISO_APPDATA = os.path.join(_ISO_ROOT, "Roaming")
_ISO_LOCALAPPDATA = os.path.join(_ISO_ROOT, "Local")
os.makedirs(_ISO_APPDATA, exist_ok=True)
os.makedirs(_ISO_LOCALAPPDATA, exist_ok=True)
os.environ["APPDATA"] = _ISO_APPDATA
os.environ["LOCALAPPDATA"] = _ISO_LOCALAPPDATA


def pytest_addoption(parser):
    parser.addoption(
        "--generate-goldens",
        action="store_true",
        default=False,
        help="Generate or update visual regression golden images in tests/goldens/",
    )


@pytest.fixture(scope="session")
def generate_goldens_flag(pytestconfig):
    """Returns True if --generate-goldens was passed or GENERATE_GOLDENS=1 is in env."""
    return pytestconfig.getoption("--generate-goldens") or os.environ.get("GENERATE_GOLDENS") == "1"


def _reset_jarvis_log(jl) -> None:
    """Undo jarvis_log.setup() state (no public reset helper exists)."""
    try:
        jl.stop_heartbeat_monitor(timeout=1.0)
    except Exception:
        pass
    root = logging.getLogger(getattr(jl, "_ROOT", "jarvis"))
    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)
    cf = getattr(jl, "_crash_file", None)
    if cf is not None:
        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            cf.close()
        except Exception:
            pass
        jl._crash_file = None
    jl._log_path = None
    jl._debug_transcripts = False
    try:
        with jl._hb_lock:
            jl._hb.clear()
            jl._exited.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path_factory):
    """Per-test: temp privacy log + restore process-global hooks/streams touched by jarvis_log.setup()."""
    saved = (sys.stdout, sys.stderr, sys.excepthook, threading.excepthook)

    pl = None
    saved_pl = None
    try:
        import privacy_log as pl
        saved_pl = pl._PRIVACY_LOG
        pl._PRIVACY_LOG = pl.PrivacyLog(
            max_events=50, log_file=os.path.join(_ISO_APPDATA, "JARVIS", "privacy_log.json"))
    except Exception:
        pl = None

    try:
        yield
    finally:
        jl = sys.modules.get("jarvis_log")
        if jl is not None:
            was_setup = getattr(jl, "_log_path", None) is not None
            _reset_jarvis_log(jl)
            if was_setup:
                try:
                    faulthandler.enable()
                except Exception:
                    pass
        sys.stdout, sys.stderr, sys.excepthook, threading.excepthook = saved
        if pl is not None:
            pl._PRIVACY_LOG = saved_pl
