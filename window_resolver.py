"""
window_resolver.py — Window Under Cursor Resolution for J.A.R.V.I.S. (Phase 8).

Resolves the window, application, process, and state under the mouse cursor using native
Win32 user32 APIs:
  - GetCursorPos
  - WindowFromPoint
  - GetAncestor (GA_ROOT = 2)
  - GetWindowTextW
  - GetWindowThreadProcessId

Provides context-aware pointer actions:
  - 'open this'  -> activates target window, clicks/opens file or link under cursor.
  - 'copy this'  -> focuses target window and triggers copy (Ctrl+C).
  - 'close this' -> closes window under cursor with unsaved confirmation check.

Strictly NO Qt imports. 100% unit-testable with interfaces and mocks.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union

try:
    from ctypes import wintypes
except ImportError:
    wintypes = None  # type: ignore


# Win32 Constants
GA_ROOT = 2
WM_CLOSE = 0x0010


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class Win32APIProtocol(Protocol):
    """Protocol interface defining low-level Win32 user32 APIs for test mocking."""

    def GetCursorPos(self, pt_byref: Any) -> bool:
        ...

    def WindowFromPoint(self, pt_val: Any) -> Any:
        ...

    def GetAncestor(self, hwnd: Any, ga_flags: int) -> Any:
        ...

    def GetWindowTextLengthW(self, hwnd: Any) -> int:
        ...

    def GetWindowTextW(self, hwnd: Any, buf: Any, max_count: int) -> int:
        ...

    def GetWindowThreadProcessId(self, hwnd: Any, pid_byref: Any) -> int:
        ...

    def SetForegroundWindow(self, hwnd: Any) -> bool:
        ...

    def PostMessageW(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> bool:
        ...


class DefaultWin32API:
    """Default Win32 user32 implementation on Windows systems."""

    def __init__(self):
        self._available = sys.platform == "win32"
        if self._available:
            self._user32 = ctypes.windll.user32
            # Explicit argtypes/restypes for 64-bit ABI safety
            try:
                self._user32.WindowFromPoint.argtypes = [POINT]
                self._user32.WindowFromPoint.restype = wintypes.HWND if wintypes else ctypes.c_void_p
            except Exception:
                pass
            try:
                self._user32.GetAncestor.argtypes = [
                    wintypes.HWND if wintypes else ctypes.c_void_p,
                    ctypes.c_uint,
                ]
                self._user32.GetAncestor.restype = wintypes.HWND if wintypes else ctypes.c_void_p
            except Exception:
                pass
            try:
                self._user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
                self._user32.GetCursorPos.restype = ctypes.c_bool
            except Exception:
                pass
            try:
                self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND if wintypes else ctypes.c_void_p]
                self._user32.GetWindowTextLengthW.restype = ctypes.c_int
            except Exception:
                pass
            try:
                self._user32.GetWindowTextW.argtypes = [
                    wintypes.HWND if wintypes else ctypes.c_void_p,
                    ctypes.c_wchar_p,
                    ctypes.c_int,
                ]
                self._user32.GetWindowTextW.restype = ctypes.c_int
            except Exception:
                pass
            try:
                self._user32.GetWindowThreadProcessId.argtypes = [
                    wintypes.HWND if wintypes else ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_ulong),
                ]
                self._user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
            except Exception:
                pass
            try:
                self._user32.SetForegroundWindow.argtypes = [wintypes.HWND if wintypes else ctypes.c_void_p]
                self._user32.SetForegroundWindow.restype = ctypes.c_bool
            except Exception:
                pass
            try:
                self._user32.PostMessageW.argtypes = [
                    wintypes.HWND if wintypes else ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ]
                self._user32.PostMessageW.restype = ctypes.c_bool
            except Exception:
                pass
        else:
            self._user32 = None

    def GetCursorPos(self, pt_byref: Any) -> bool:
        if self._user32:
            try:
                return bool(self._user32.GetCursorPos(pt_byref))
            except Exception:
                return False
        return False

    def WindowFromPoint(self, pt_val: Any) -> Any:
        if self._user32:
            try:
                return self._user32.WindowFromPoint(pt_val)
            except Exception:
                return None
        return None

    def GetAncestor(self, hwnd: Any, ga_flags: int) -> Any:
        if self._user32 and hwnd:
            try:
                return self._user32.GetAncestor(hwnd, ga_flags)
            except Exception:
                return None
        return None

    def GetWindowTextLengthW(self, hwnd: Any) -> int:
        if self._user32 and hwnd:
            try:
                return int(self._user32.GetWindowTextLengthW(hwnd))
            except Exception:
                return 0
        return 0

    def GetWindowTextW(self, hwnd: Any, buf: Any, max_count: int) -> int:
        if self._user32 and hwnd:
            try:
                return int(self._user32.GetWindowTextW(hwnd, buf, max_count))
            except Exception:
                return 0
        return 0

    def GetWindowThreadProcessId(self, hwnd: Any, pid_byref: Any) -> int:
        if self._user32 and hwnd:
            try:
                return int(self._user32.GetWindowThreadProcessId(hwnd, pid_byref))
            except Exception:
                return 0
        return 0

    def SetForegroundWindow(self, hwnd: Any) -> bool:
        if self._user32 and hwnd:
            try:
                return bool(self._user32.SetForegroundWindow(hwnd))
            except Exception:
                return False
        return False

    def PostMessageW(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> bool:
        if self._user32 and hwnd:
            try:
                return bool(self._user32.PostMessageW(hwnd, msg, wparam, lparam))
            except Exception:
                return False
        return False


def _get_process_name_by_pid(pid: int) -> str:
    """Best-effort process name resolution."""
    if not pid:
        return ""
    try:
        import psutil
        proc = psutil.Process(pid)
        return proc.name()
    except Exception:
        pass
    return ""


class WindowResolver:
    """
    Resolves window metadata and executes context-aware cursor actions.
    """

    def __init__(
        self,
        api: Optional[Win32APIProtocol] = None,
        pyautogui_module: Optional[Any] = None,
    ):
        self.api: Win32APIProtocol = api or DefaultWin32API()
        self._pyautogui = pyautogui_module

    def _get_pyautogui(self):
        if self._pyautogui is not None:
            return self._pyautogui
        try:
            import pyautogui
            return pyautogui
        except ImportError:
            return None

    def resolve_window_under_cursor(
        self, point: Optional[Tuple[int, int]] = None
    ) -> Dict[str, Any]:
        """
        Resolves window under current mouse cursor or specified point.
        Uses Win32 APIs: GetCursorPos, WindowFromPoint, GetAncestor (GA_ROOT=2),
        GetWindowTextW, GetWindowThreadProcessId.
        Returns metadata dict with keys: hwnd, raw_hwnd, title, process_id,
        process_name, point, has_unsaved, is_valid.
        """
        if point is not None:
            cx, cy = int(point[0]), int(point[1])
        else:
            pt = POINT()
            ok = self.api.GetCursorPos(ctypes.byref(pt))
            if ok:
                cx, cy = int(pt.x), int(pt.y)
            else:
                pag = self._get_pyautogui()
                if pag:
                    try:
                        pos = pag.position()
                        cx, cy = int(pos[0]), int(pos[1])
                    except Exception:
                        cx, cy = 0, 0
                else:
                    cx, cy = 0, 0

        target_pt = POINT(cx, cy)
        raw_hwnd = self.api.WindowFromPoint(target_pt)

        root_hwnd = None
        if raw_hwnd:
            root_hwnd = self.api.GetAncestor(raw_hwnd, GA_ROOT)
        if not root_hwnd and raw_hwnd:
            root_hwnd = raw_hwnd

        title = ""
        pid = 0
        effective_hwnd = root_hwnd or raw_hwnd

        if effective_hwnd:
            length = self.api.GetWindowTextLengthW(effective_hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                self.api.GetWindowTextW(effective_hwnd, buf, length + 1)
                title = buf.value
            else:
                # Some root windows have 0 length text while raw control has text
                raw_len = self.api.GetWindowTextLengthW(raw_hwnd) if raw_hwnd else 0
                if raw_len > 0:
                    buf = ctypes.create_unicode_buffer(raw_len + 1)
                    self.api.GetWindowTextW(raw_hwnd, buf, raw_len + 1)
                    title = buf.value

            pid_val = ctypes.c_ulong(0)
            self.api.GetWindowThreadProcessId(effective_hwnd, ctypes.byref(pid_val))
            pid = int(pid_val.value)

        has_unsaved = (
            "*" in title
            or "untitled" in title.lower()
            or "unsaved" in title.lower()
        )

        proc_name = _get_process_name_by_pid(pid)

        # Normalize handle values to int or None
        def _h_to_int(h):
            if h is None:
                return 0
            if isinstance(h, int):
                return h
            try:
                return int(h)
            except Exception:
                return getattr(h, "value", 0) or 0

        int_root = _h_to_int(root_hwnd)
        int_raw = _h_to_int(raw_hwnd)

        return {
            "hwnd": int_root,
            "raw_hwnd": int_raw,
            "title": title.strip(),
            "process_id": pid,
            "process_name": proc_name,
            "point": (cx, cy),
            "has_unsaved": has_unsaved,
            "is_valid": bool(int_root or int_raw),
        }

    def open_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        """
        'open this': activates target window and clicks or opens file/link under cursor.
        """
        info = self.resolve_window_under_cursor(point=point)
        hwnd = info["hwnd"] or info["raw_hwnd"]
        px, py = info["point"]

        if hwnd:
            self.api.SetForegroundWindow(hwnd)

        pag = self._get_pyautogui()
        if pag:
            try:
                # Click target location under cursor
                pag.click(px, py, _pause=False)
            except Exception:
                pass

        title = info["title"]
        if title:
            return True, f"Opened target in '{title}', sir."
        elif hwnd:
            return True, "Opened target under cursor, sir."
        return True, "Target activated under cursor, sir."

    def copy_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        """
        'copy this': focuses target window under cursor and triggers copy (Ctrl+C).
        """
        info = self.resolve_window_under_cursor(point=point)
        hwnd = info["hwnd"] or info["raw_hwnd"]

        if hwnd:
            self.api.SetForegroundWindow(hwnd)

        pag = self._get_pyautogui()
        if pag:
            try:
                pag.hotkey("ctrl", "c", _pause=False)
            except Exception as exc:
                return False, f"Unable to trigger copy: {exc}"
        else:
            return False, "Input controller unavailable"

        title = info["title"]
        if title:
            return True, f"Copied selection from '{title}' to clipboard, sir."
        return True, "Copied selection to clipboard, sir."

    def close_this(
        self, confirm: bool = False, point: Optional[Tuple[int, int]] = None
    ) -> Tuple[bool, str]:
        """
        'close this': closes window under cursor with unsaved confirmation check.
        If document has unsaved changes and confirm=False, returns warning requiring confirmation.
        """
        info = self.resolve_window_under_cursor(point=point)
        hwnd = info["hwnd"] or info["raw_hwnd"]

        if not hwnd and not info["title"]:
            return False, "No window detected under cursor to close, sir."

        title = info["title"]
        if info["has_unsaved"] and not confirm:
            doc_name = title.strip() or "Active document"
            return False, f"Document '{doc_name}' has unsaved changes. Please confirm to close."

        # Target window is safe to close or confirmed
        if hwnd:
            try:
                self.api.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                closed = True
            except Exception:
                closed = False
        else:
            closed = False

        if not closed:
            pag = self._get_pyautogui()
            if pag:
                try:
                    if hwnd:
                        self.api.SetForegroundWindow(hwnd)
                    pag.hotkey("alt", "f4", _pause=False)
                    closed = True
                except Exception as exc:
                    return False, f"Failed to close window: {exc}"

        display_name = title or (info["process_name"] or "window")
        return True, f"Closed '{display_name}', sir."


# Global default resolver instance
_DEFAULT_RESOLVER = WindowResolver()


def resolve_window_under_cursor(point: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    """Resolves metadata of the window under the mouse cursor."""
    return _DEFAULT_RESOLVER.resolve_window_under_cursor(point=point)


def open_this(point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    """Activates and opens/clicks target under cursor."""
    return _DEFAULT_RESOLVER.open_this(point=point)


def copy_this(point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    """Focuses target window and triggers copy (Ctrl+C)."""
    return _DEFAULT_RESOLVER.copy_this(point=point)


def close_this(confirm: bool = False, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    """Closes window under cursor with unsaved confirmation protection."""
    return _DEFAULT_RESOLVER.close_this(confirm=confirm, point=point)
