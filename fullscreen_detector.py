"""
fullscreen_detector.py — Windows Fullscreen App & Game Detection.

Queries the Windows Shell Notification State API (SHQueryUserNotificationState)
to detect whether a full-screen Direct3D game, full-screen media player, or presentation
is currently active, allowing the Dynamic Island to hide gracefully and avoid obscuring
critical gameplay or application controls.

Notification States (QUERY_USER_NOTIFICATION_STATE):
  - QUNS_NOT_PRESENT (1): Screen saver, locked workstation, or logon UI active.
  - QUNS_BUSY (2): Full-screen app or presentation mode is active.
  - QUNS_RUNNING_D3D_FULL_SCREEN (3): Full-screen Direct3D application/game active.
  - QUNS_PRESENTATION_MODE (4): User engaged Presentation Mode.
  - QUNS_ACCEPTS_NOTIFICATIONS (5): Normal desktop, accepting notifications.
  - QUNS_QUIET_TIME (6): Quiet hours / first hour after install.
  - QUNS_APP (7): Windows Store application is running.

Pure Python behind interfaces: 100% unit-testable with mocks; strictly NO Qt imports.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

# QUERY_USER_NOTIFICATION_STATE enum constants
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7


def win32_query_user_notification_state() -> int:
    """Queries SHQueryUserNotificationState via ctypes on Windows."""
    if sys.platform != "win32":
        return QUNS_ACCEPTS_NOTIFICATIONS
    try:
        import ctypes
        state = ctypes.c_int()
        res = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if res == 0:  # S_OK
            return int(state.value)
    except Exception as exc:
        print(f"[fullscreen_detector] ctypes error: {exc}")
    return QUNS_ACCEPTS_NOTIFICATIONS


class FullscreenDetector:
    """
    Detects active fullscreen applications or games.
    Accepts an optional query function for mock unit testing.
    """

    def __init__(self, query_fn: Optional[Callable[[], int]] = None):
        self._query_fn = query_fn or win32_query_user_notification_state

    def get_state(self) -> int:
        """Returns the raw notification state code."""
        try:
            return self._query_fn()
        except Exception:
            return QUNS_ACCEPTS_NOTIFICATIONS

    def is_fullscreen(self) -> bool:
        """
        Returns True if a fullscreen app or game is active (QUNS_BUSY=2 or QUNS_RUNNING_D3D_FULL_SCREEN=3).
        Returns False if on normal desktop (QUNS_ACCEPTS_NOTIFICATIONS=5 or other).
        """
        st = self.get_state()
        return st in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN)


# Global singleton detector
_DETECTOR: Optional[FullscreenDetector] = None


def get_fullscreen_detector() -> FullscreenDetector:
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = FullscreenDetector()
    return _DETECTOR


def is_fullscreen_active(query_fn: Optional[Callable[[], int]] = None) -> bool:
    """Convenience function to check whether fullscreen mode is active."""
    if query_fn is not None:
        return FullscreenDetector(query_fn).is_fullscreen()
    return get_fullscreen_detector().is_fullscreen()


# --------------------------------------------------------------------------- foreground window vs monitor

_SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")


def win32_foreground_covers_monitor() -> bool:
    """True when the foreground window (not ours, not the shell) exactly covers the primary monitor,
    i.e. exclusive or borderless fullscreen on the monitor the island lives on. Cheap Win32 calls only."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    import os
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE

    hwnd = user32.GetForegroundWindow()
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in _SHELL_CLASSES:
        return False

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    primary = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
    mon = user32.MonitorFromWindow(hwnd, 0)                     # MONITOR_DEFAULTTONULL
    if not mon or mon != primary:
        return False
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    m = info.rcMonitor
    return (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom)


def is_fullscreen_on_island_monitor(
    query_fn: Optional[Callable[[], int]] = None,
    foreground_fn: Optional[Callable[[], bool]] = None,
) -> bool:
    """Exclusive/D3D/presentation fullscreen (shell notification state) OR a borderless window covering the monitor."""
    if FullscreenDetector(query_fn).is_fullscreen():
        return True
    return bool((foreground_fn or win32_foreground_covers_monitor)())
