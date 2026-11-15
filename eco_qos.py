"""Efficiency mode (EcoQoS) for the Jarvis process.

set_efficiency(True) asks Windows to throttle the process (Windows 11 task manager shows the leaf
"Efficiency mode"; Windows 10 ignores the throttling request) and lowers the priority class.
Never raises; failures are logged once per distinct reason.
"""
import ctypes
import sys
import threading
import time
from ctypes import wintypes

import jarvis_log as _jlog

_log = _jlog.get_logger("eco_qos")

ProcessPowerThrottling = 4
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
NORMAL_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


_logged_failures = set()


def _fail(what, err):
    key = (what, err)
    if key not in _logged_failures:
        _logged_failures.add(key)
        _log.warning("%s failed: winerror %s", what, err)


def set_efficiency(on, lower_priority=True):
    """Enable/disable efficiency mode. Returns True if the throttling call succeeded. Never raises."""
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        h = k32.GetCurrentProcess()
        st = _PowerThrottlingState(PROCESS_POWER_THROTTLING_CURRENT_VERSION,
                                   PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                                   PROCESS_POWER_THROTTLING_EXECUTION_SPEED if on else 0)
        k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        ok = bool(k32.SetProcessInformation(h, ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)))
        if not ok:
            _fail("SetProcessInformation(ProcessPowerThrottling)", ctypes.get_last_error())
        if lower_priority:
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            cls = BELOW_NORMAL_PRIORITY_CLASS if on else NORMAL_PRIORITY_CLASS
            if not k32.SetPriorityClass(h, cls):
                _fail("SetPriorityClass", ctypes.get_last_error())
        return ok
    except Exception:
        _log.exception("set_efficiency(%s) raised", on)
        return False


class EfficiencyGovernor:
    """Efficient when no busy reason is set and no activity for `idle_after` seconds.

    Reasons: set_busy("camera", True) while gesture mode is on, "voice", "command", ... Any activity() or
    busy=True lifts efficiency synchronously. A single timer thread wakes only at the next deadline.
    """

    def __init__(self, idle_after=10.0, apply=set_efficiency, clock=time.monotonic, start_thread=True):
        self.idle_after = float(idle_after)
        self._apply = apply
        self._clock = clock
        self._lock = threading.Lock()
        self._busy = set()
        self._last_activity = clock()
        self._efficient = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        if start_thread:
            self._thread = threading.Thread(target=self._loop, name="EcoGovernor", daemon=True)
            self._thread.start()

    @property
    def efficient(self):
        return self._efficient

    def activity(self):
        """Something happened (wake word, speech, command): lift efficiency now and restart the idle clock."""
        with self._lock:
            self._last_activity = self._clock()
        self._reevaluate()

    def set_busy(self, reason, busy):
        with self._lock:
            if busy:
                self._busy.add(reason)
            else:
                self._busy.discard(reason)
            self._last_activity = self._clock()
        self._reevaluate()

    def _want_efficient(self):
        return not self._busy and (self._clock() - self._last_activity) >= self.idle_after

    def _reevaluate(self):
        with self._lock:
            want = self._want_efficient()
            changed = want != self._efficient
            if changed:
                self._efficient = want
        if changed:
            _log.info("efficiency mode %s", "on" if want else "off")
            self._apply(want)
        self._wake.set()

    def tick(self):
        """Evaluate now (the timer thread calls this; tests call it with a fake clock)."""
        self._reevaluate()
        self._wake.clear()

    def _next_wait(self):
        with self._lock:
            if self._efficient or self._busy:
                return None   # nothing to time: wait for a state change
            return max(0.05, self.idle_after - (self._clock() - self._last_activity))

    def _loop(self):
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(self._next_wait())
            if self._stop.is_set():
                break

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._efficient:
            self._efficient = False
            self._apply(False)
