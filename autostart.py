"""Start-with-Windows via HKCU Run. The registry is the single source of truth (no config field)."""
import os
import sys

import jarvis_log as _jlog

_log = _jlog.get_logger("autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "JARVIS"
BACKGROUND_FLAG = "--background"

try:
    import winreg
except ImportError:   # non-Windows
    winreg = None


def build_command():
    """Quoted command line for the Run value."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {BACKGROUND_FLAG}'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
    return f'"{exe}" "{script}" {BACKGROUND_FLAG}'


def _read():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
        return winreg.QueryValueEx(k, VALUE_NAME)[0]


def is_enabled():
    """True when a JARVIS Run entry exists. Never raises."""
    if winreg is None:
        return False
    try:
        return bool(_read())
    except FileNotFoundError:
        return False
    except OSError as exc:
        _log.warning("could not read autostart: %s", exc)
        return False


def set_enabled(enabled):
    """Create/remove the Run entry. Returns True on success. Never raises."""
    if winreg is None:
        return False
    try:
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, build_command())
            _log.info("autostart enabled")
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, VALUE_NAME)
            except FileNotFoundError:
                pass
            _log.info("autostart disabled")
        return True
    except OSError as exc:
        _log.error("could not %s autostart: %s", "enable" if enabled else "disable", exc)
        return False
