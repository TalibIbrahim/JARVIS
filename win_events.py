"""Native Windows event sink: hotkeys, power, session lock and device changes.

A hidden message-only window runs on its own daemon thread with a GetMessage loop.
  - RegisterHotKey (Ctrl+Alt+J / Ctrl+Alt+Q, MOD_NOREPEAT): no global keyboard hook.
  - WM_POWERBROADCAST: suspend / resume.
  - WTSRegisterSessionNotification + WM_WTSSESSION_CHANGE: lock / unlock.
  - RegisterDeviceNotification (audio + camera interface classes) + WM_DEVICECHANGE: debounced
    on_devices_changed.
No Qt imports. Callbacks are plain callables invoked on the message thread; keep them non-blocking.
"""
import ctypes
import sys
import threading
from ctypes import wintypes

import jarvis_log as _jlog

_log = _jlog.get_logger("win_events")

# window messages
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_HOTKEY = 0x0312
WM_POWERBROADCAST = 0x0218
WM_DEVICECHANGE = 0x0219
WM_WTSSESSION_CHANGE = 0x02B1

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0

DBT_DEVNODES_CHANGED = 0x0007
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_DEVICEINTERFACE = 5
DEVICE_NOTIFY_WINDOW_HANDLE = 0

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_J = 0x4A
VK_Q = 0x51

HOTKEY_TOGGLE = 1
HOTKEY_QUIT = 2

HWND_MESSAGE = -3

GUID_KSCATEGORY_AUDIO = "{6994AD04-93EF-11D0-A3CC-00A0C9223196}"
GUID_KSCATEGORY_VIDEO_CAMERA = "{E5323777-F976-4F5B-9B55-B94699C46E44}"

LRESULT = ctypes.c_ssize_t


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _DevBroadcastInterface(ctypes.Structure):
    _fields_ = [("dbcc_size", wintypes.DWORD), ("dbcc_devicetype", wintypes.DWORD),
                ("dbcc_reserved", wintypes.DWORD), ("dbcc_classguid", _GUID),
                ("dbcc_name", ctypes.c_wchar * 1)]


class _DevBroadcastHdr(ctypes.Structure):
    _fields_ = [("dbch_size", wintypes.DWORD), ("dbch_devicetype", wintypes.DWORD),
                ("dbch_reserved", wintypes.DWORD)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def _guid_from_str(s):
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


class WinEvents:
    """Message-only window + hotkeys. Construct, then start(); stop() is idempotent."""

    def __init__(self, on_toggle=None, on_quit=None, on_suspend=None, on_resume=None,
                 on_lock=None, on_unlock=None, on_devices_changed=None, debounce=1.0):
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.on_suspend = on_suspend
        self.on_resume = on_resume
        self.on_lock = on_lock
        self.on_unlock = on_unlock
        self.on_devices_changed = on_devices_changed
        self.debounce = float(debounce)
        self._suspended = False
        self._locked = False
        self._hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._timer = None
        self._timer_lock = threading.Lock()
        self._registered_hotkeys = []
        self._dev_notifs = []
        self._wts_registered = False
        self._wndproc = None   # keep the ctypes callback alive
        self._user32 = None
        self._started_ok = False

    # ---- pure dispatch (unit-testable without a window) ----
    def _fire(self, cb, name):
        if cb is None:
            return
        try:
            cb()
        except Exception:
            _log.exception("callback %s failed", name)

    def handle_message(self, msg, wparam, lparam):
        """Map a window message to callbacks. Returns the LRESULT to hand back, or None for default handling."""
        if msg == WM_HOTKEY:
            if wparam == HOTKEY_TOGGLE:
                self._fire(self.on_toggle, "on_toggle")
            elif wparam == HOTKEY_QUIT:
                self._fire(self.on_quit, "on_quit")
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                if not self._suspended:
                    self._suspended = True
                    _log.info("system suspend")
                    self._fire(self.on_suspend, "on_suspend")
            elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                # Windows sends both resume codes after a wake; fire once.
                if self._suspended:
                    self._suspended = False
                    _log.info("system resume")
                    self._fire(self.on_resume, "on_resume")
            return 1   # TRUE
        if msg == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK:
                self._locked = True
                _log.info("session lock")
                self._fire(self.on_lock, "on_lock")
            elif wparam == WTS_SESSION_UNLOCK:
                self._locked = False
                _log.info("session unlock")
                self._fire(self.on_unlock, "on_unlock")
            return 0
        if msg == WM_DEVICECHANGE:
            if wparam == DBT_DEVNODES_CHANGED or (
                    wparam in (DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE)
                    and self._is_interface_broadcast(lparam)):
                self._schedule_devices_changed()
            return 1   # TRUE
        return None

    @staticmethod
    def _is_interface_broadcast(lparam):
        """Only device-interface broadcasts matter (ignore volume/port arrivals). lparam==0 -> accept."""
        if not lparam:
            return True
        try:
            hdr = _DevBroadcastHdr.from_address(int(lparam))
            return hdr.dbch_devicetype == DBT_DEVTYP_DEVICEINTERFACE
        except Exception:
            _log.exception("could not read device broadcast header")
            return False

    def _schedule_devices_changed(self):
        if self.on_devices_changed is None:
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            t = threading.Timer(self.debounce, self._fire_devices_changed)
            t.daemon = True
            self._timer = t
            t.start()

    def _fire_devices_changed(self):
        _log.info("audio/camera devices changed")
        self._fire(self.on_devices_changed, "on_devices_changed")

    # ---- lifecycle ----
    def start(self, timeout=3.0):
        if sys.platform != "win32":
            _log.info("win_events unavailable: not Windows")
            return False
        if self._thread is not None:
            return self._started_ok
        self._thread = threading.Thread(target=self._run, name="WinEvents", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            _log.error("win_events window was not ready within %.1fs", timeout)
        return self._started_ok

    def stop(self, timeout=2.0):
        t = self._thread
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if t is None:
            return
        if self._hwnd and self._user32 is not None:
            if not self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0):
                _log.error("PostMessage(WM_CLOSE) failed: winerror %s", ctypes.get_last_error())
        t.join(timeout)
        if t.is_alive():
            _log.warning("win_events thread did not exit within %.1fs", timeout)
        self._thread = None

    def _run(self):
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = u32
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.DefWindowProcW.restype = LRESULT
        u32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        u32.RegisterClassW.restype = wintypes.ATOM
        u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.DestroyWindow.argtypes = [wintypes.HWND]
        u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u32.PostQuitMessage.argtypes = [ctypes.c_int]
        u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.RegisterDeviceNotificationW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD]
        u32.RegisterDeviceNotificationW.restype = wintypes.HANDLE
        u32.UnregisterDeviceNotification.argtypes = [wintypes.HANDLE]
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k32.GetModuleHandleW.restype = wintypes.HMODULE

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                res = self.handle_message(msg, wparam, lparam)
                if res is not None:
                    return res
                if msg == WM_CLOSE:
                    u32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    self._cleanup(hwnd)
                    u32.PostQuitMessage(0)
                    return 0
            except Exception:
                _log.exception("window procedure failed (msg=0x%04x)", msg)
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)
        hinst = k32.GetModuleHandleW(None)
        cls = _WNDCLASSW()
        cls.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        cls.hInstance = hinst
        cls.lpszClassName = "JarvisWinEvents"
        if not u32.RegisterClassW(ctypes.byref(cls)):
            _log.error("RegisterClassW failed: winerror %s", ctypes.get_last_error())
            self._ready.set()
            return
        hwnd = u32.CreateWindowExW(0, cls.lpszClassName, "JarvisWinEvents", 0, 0, 0, 0, 0,
                                   wintypes.HWND(HWND_MESSAGE), None, hinst, None)
        if not hwnd:
            _log.error("CreateWindowEx(HWND_MESSAGE) failed: winerror %s", ctypes.get_last_error())
            self._ready.set()
            return
        self._hwnd = hwnd
        self._register_all(u32, hwnd)
        self._started_ok = True
        self._ready.set()
        _log.info("win_events running (hotkeys=%s, devnotif=%d, wts=%s)",
                  self._registered_hotkeys, len(self._dev_notifs), self._wts_registered)

        msg = wintypes.MSG()
        while True:
            r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0:
                break
            if r == -1:
                _log.error("GetMessage failed: winerror %s", ctypes.get_last_error())
                break
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = None
        _log.info("win_events stopped")

    def _register_all(self, u32, hwnd):
        mods = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
        for hid, vk, label in ((HOTKEY_TOGGLE, VK_J, "Ctrl+Alt+J"), (HOTKEY_QUIT, VK_Q, "Ctrl+Alt+Q")):
            if u32.RegisterHotKey(hwnd, hid, mods, vk):
                self._registered_hotkeys.append(hid)
            else:
                _log.warning("hotkey %s not registered (already in use?): winerror %s",
                             label, ctypes.get_last_error())
        try:
            wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
            wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
            wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
            if wts.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION):
                self._wts_registered = True
                self._wts = wts
            else:
                _log.warning("WTSRegisterSessionNotification failed: winerror %s", ctypes.get_last_error())
        except Exception:
            _log.exception("wtsapi32 unavailable; lock/unlock events disabled")
        for name, guid_s in (("audio", GUID_KSCATEGORY_AUDIO), ("camera", GUID_KSCATEGORY_VIDEO_CAMERA)):
            try:
                f = _DevBroadcastInterface()
                f.dbcc_size = ctypes.sizeof(f)
                f.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE
                f.dbcc_classguid = _guid_from_str(guid_s)
                h = u32.RegisterDeviceNotificationW(hwnd, ctypes.byref(f), DEVICE_NOTIFY_WINDOW_HANDLE)
                if h:
                    self._dev_notifs.append(h)
                else:
                    _log.warning("RegisterDeviceNotification(%s) failed: winerror %s", name, ctypes.get_last_error())
            except Exception:
                _log.exception("RegisterDeviceNotification(%s) raised", name)

    def _cleanup(self, hwnd):
        """Runs on the message thread (WM_DESTROY): hotkeys and notifications are per-thread/window."""
        u32 = self._user32
        for hid in self._registered_hotkeys:
            if not u32.UnregisterHotKey(hwnd, hid):
                _log.warning("UnregisterHotKey(%d) failed: winerror %s", hid, ctypes.get_last_error())
        self._registered_hotkeys = []
        for h in self._dev_notifs:
            if not u32.UnregisterDeviceNotification(h):
                _log.warning("UnregisterDeviceNotification failed: winerror %s", ctypes.get_last_error())
        self._dev_notifs = []
        if self._wts_registered:
            if not self._wts.WTSUnRegisterSessionNotification(hwnd):
                _log.warning("WTSUnRegisterSessionNotification failed: winerror %s", ctypes.get_last_error())
            self._wts_registered = False
