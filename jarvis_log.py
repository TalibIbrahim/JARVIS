"""
jarvis_log.py - durable, privacy-safe diagnostics for J.A.R.V.I.S.

Rotating file log, crash capture (faulthandler), stdout/stderr tee (the exe is
built --windowed so the std streams are None), thread lifecycle events,
heartbeats + stall monitor, an instrumented RLock and per-utterance stage
tracking.  No Qt imports.  Importing this module has no side effects; nothing
is written anywhere until setup() is called.

Privacy: audio, frames, API keys and transcripts are never logged.  Free text
passes through redact() which yields "<len=N>" unless debug transcripts are on.
"""

from __future__ import annotations

import faulthandler
import itertools
import logging
import logging.handlers
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

_ROOT = "jarvis"
_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(name)s: %(message)s"

# Avoid logging.lastResort printing WARNINGs when setup() was never called.
logging.getLogger(_ROOT).addHandler(logging.NullHandler())

_setup_lock = threading.Lock()
_log_path: Optional[str] = None
_crash_file = None  # kept open for the process lifetime (faulthandler)
_debug_transcripts = False

# Canonical utterance stages
AUDIO_START = "AUDIO_START"
ENDPOINT = "ENDPOINT"
STT_SENT = "STT_SENT"
TRANSCRIPT = "TRANSCRIPT"
WAKE_GATE = "WAKE_GATE"
DECISION = "DECISION"
DISPATCH = "DISPATCH"
RESULT = "RESULT"
DROPPED = "DROPPED"


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_ROOT}.{name}")


_log = get_logger("core")


# --------------------------------------------------------------------------- privacy

def set_debug_transcripts(flag: bool) -> None:
    global _debug_transcripts
    _debug_transcripts = bool(flag)


def debug_transcripts() -> bool:
    return _debug_transcripts


def redact(text: Any) -> str:
    s = "" if text is None else (text if isinstance(text, str) else str(text))
    if _debug_transcripts:
        return s
    return f"<len={len(s)}>"


# --------------------------------------------------------------------------- stream tee

class _StreamTee:
    """Line-buffered writer: logs each complete line, forwards to the original stream."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, logger: logging.Logger, level: int, original):
        self._logger = logger
        self._level = level
        self._orig = original
        self._tls = threading.local()  # per-thread partial-line buffer + reentrancy guard
        self._orig_failed = False

    def write(self, s) -> int:
        try:
            if isinstance(s, (bytes, bytearray)):
                s = bytes(s).decode("utf-8", "replace")
            elif not isinstance(s, str):
                s = str(s)
            n = len(s)
            self._forward(s)
            tls = self._tls
            if getattr(tls, "busy", False):
                return n  # logging itself wrote to us (handler error); do not recurse
            tls.busy = True
            try:
                buf = getattr(tls, "buf", "") + s
                if "\n" in buf:
                    *lines, rest = buf.split("\n")
                    for line in lines:
                        line = line.rstrip("\r")
                        if line:
                            self._logger.log(self._level, line)
                    buf = rest
                tls.buf = buf
            finally:
                tls.busy = False
            return n
        except Exception as exc:  # never raise from a std stream
            self._forward(f"[jarvis_log] tee write failed: {exc!r}\n")
            return len(s) if isinstance(s, str) else 0

    def _forward(self, s: str) -> None:
        o = self._orig
        if o is None or self._orig_failed:
            return
        try:
            o.write(s)
        except Exception as exc:
            self._orig_failed = True
            if not getattr(self._tls, "busy", False):
                self._logger.warning("original stream write failed, forwarding disabled: %r", exc)

    def writelines(self, lines) -> None:
        for ln in lines:
            self.write(ln)

    def flush(self) -> None:
        o = self._orig
        if o is not None and not self._orig_failed:
            try:
                o.flush()
            except Exception:
                self._orig_failed = True

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._orig is not None:
            return self._orig.fileno()
        import io
        raise io.UnsupportedOperation("fileno")


# --------------------------------------------------------------------------- hooks

def _install_excepthooks() -> None:
    prev_sys = sys.excepthook
    prev_thr = threading.excepthook

    def _sys_hook(tp, val, tb):
        try:
            get_logger("excepthook").error("Uncaught exception in main thread", exc_info=(tp, val, tb))
        finally:
            prev_sys(tp, val, tb)

    def _thr_hook(args):
        try:
            if args.exc_type is not SystemExit:
                name = args.thread.name if args.thread else "?"
                if getattr(args.exc_value, "_jarvis_logged", False):
                    get_logger("excepthook").error("Thread %s died (traceback logged above)", name)
                else:
                    get_logger("excepthook").error(
                        "Uncaught exception in thread %s", name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        finally:
            prev_thr(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thr_hook


def _resolve_dir(log_dir: Optional[str]) -> str:
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        return log_dir
    base = os.environ.get("LOCALAPPDATA")
    candidates = []
    if base:
        candidates.append(os.path.join(base, "JARVIS", "logs"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".jarvis", "logs"))
    last: Optional[Exception] = None
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError as exc:
            last = exc
            if sys.stderr is not None:
                sys.stderr.write(f"[jarvis_log] cannot use log dir {d}: {exc}\n")
    raise OSError(f"no writable log directory: {last}")


def _version() -> str:
    try:
        from version import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return "unknown"


def setup(debug_transcripts: bool = False, log_dir: Optional[str] = None) -> str:
    """Configure logging once. Returns the log file path. Idempotent."""
    global _log_path, _crash_file, _debug_transcripts
    with _setup_lock:
        if _log_path is not None:
            return _log_path
        _debug_transcripts = bool(debug_transcripts)
        directory = _resolve_dir(log_dir)
        path = os.path.join(directory, "jarvis.log")

        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FORMAT))
        root = logging.getLogger(_ROOT)
        root.setLevel(logging.INFO)
        root.addHandler(handler)

        # Native crash capture (e.g. 0xC0000374 heap corruption)
        crash_path = os.path.join(directory, "crash.log")
        _crash_file = open(crash_path, "a", encoding="utf-8", buffering=1)
        _crash_file.write(f"=== process start pid={os.getpid()} ===\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)

        _install_excepthooks()
        sys.stdout = _StreamTee(get_logger("stdout"), logging.INFO, sys.stdout)
        sys.stderr = _StreamTee(get_logger("stderr"), logging.WARNING, sys.stderr)

        _log_path = path
        _log.info("startup version=%s frozen=%s python=%s pid=%d argv=%s log=%s",
                  _version(), bool(getattr(sys, "frozen", False)), sys.version.split()[0],
                  os.getpid(), sys.argv, path)
        return path


# --------------------------------------------------------------------------- heartbeats

_hb_lock = threading.Lock()
_hb: Dict[str, tuple] = {}
_exited: Dict[str, float] = {}
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()
_monitor_lock = threading.Lock()


def heartbeat(name: str, **fields) -> None:
    """Cheap liveness marker (no I/O). Safe at 120 Hz."""
    now = time.monotonic()
    with _hb_lock:
        _hb[name] = (now, fields)


def mark_exited(name: str) -> None:
    with _hb_lock:
        _exited[name] = time.monotonic()


def get_health(stall_after: float = 10.0) -> Dict[str, Dict[str, Any]]:
    now = time.monotonic()
    wall = time.time()
    with _hb_lock:
        items = list(_hb.items())
        exited = dict(_exited)
    out: Dict[str, Dict[str, Any]] = {}
    for name, (t, fields) in items:
        age = now - t
        if name in exited and exited[name] >= t:
            state = "exited"
        elif age > stall_after:
            state = "stalled"
        else:
            state = "alive"
        out[name] = {"last": wall - age, "age": age, "state": state, "fields": dict(fields)}
    return out


def _fmt_fields(fields: Dict[str, Any]) -> str:
    return "(" + ",".join(f"{k}={v}" for k, v in fields.items()) + ")" if fields else ""


def _monitor_loop(interval: float, stall_after: float) -> None:
    log = get_logger("heartbeat")
    stalled: set = set()
    while not _monitor_stop.wait(interval):
        health = get_health(stall_after)
        if not health:
            continue
        parts = []
        for name, h in health.items():
            tag = "" if h["state"] == "alive" else f"[{h['state']}]"
            parts.append(f"{name}={h['age']:.1f}s{tag}{_fmt_fields(h['fields'])}")
            if h["state"] == "stalled" and name not in stalled:
                stalled.add(name)
                log.warning("STALL %s no heartbeat for %.1fs (>%.1fs)", name, h["age"], stall_after)
            elif h["state"] != "stalled" and name in stalled:
                stalled.discard(name)
                log.info("recovered %s (state=%s)", name, h["state"])
        log.info("hb %s", " ".join(parts))


def start_heartbeat_monitor(interval: float = 5.0, stall_after: float = 10.0) -> None:
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_monitor_loop, args=(interval, stall_after), name="HeartbeatMonitor", daemon=True)
        _monitor_thread.start()


def stop_heartbeat_monitor(timeout: float = 2.0) -> None:
    global _monitor_thread
    with _monitor_lock:
        t = _monitor_thread
        _monitor_stop.set()
        _monitor_thread = None
    if t is not None:
        t.join(timeout)


# --------------------------------------------------------------------------- per-thread CPU sampler

_cpu_thread: Optional[threading.Thread] = None
_cpu_lock = threading.Lock()
_cpu_stop = threading.Event()


def _native_thread_description(tid: int) -> str:
    """OS-level thread name (GetThreadDescription, Win10 1607+); '' when unavailable."""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenThread.restype = wintypes.HANDLE
    k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenThread(0x0400, False, tid)  # THREAD_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        fn = k32.GetThreadDescription
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        fn.restype = ctypes.c_long
        out = ctypes.c_wchar_p()
        if fn(h, ctypes.byref(out)) < 0 or not out.value:
            return ""
        val = out.value
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
        return val
    finally:
        k32.CloseHandle(h)


def _cpu_sampler_loop(interval: float, top: int) -> None:
    import psutil
    log = get_logger("cpu")
    proc = psutil.Process()
    ncpu = psutil.cpu_count() or 1
    prev: Dict[int, float] = {}
    first_seen: Dict[int, int] = {}
    desc: Dict[int, str] = {}
    order = itertools.count(1)
    prev_t = time.perf_counter()
    for t in proc.threads():
        prev[t.id] = t.user_time + t.system_time
        first_seen[t.id] = next(order)
    proc.cpu_percent(None)
    psutil.cpu_percent(None)
    while not _cpu_stop.wait(interval):
        try:
            now = time.perf_counter()
            dt = max(now - prev_t, 1e-6)
            prev_t = now
            names = {t.native_id: t.name for t in threading.enumerate()}
            cur: Dict[int, float] = {}
            rows = []
            for t in proc.threads():
                cpu = t.user_time + t.system_time
                cur[t.id] = cpu
                if t.id not in first_seen:
                    first_seen[t.id] = next(order)
                pct = (cpu - prev.get(t.id, cpu)) / dt * 100.0
                if pct <= 1.0:
                    continue
                name = names.get(t.id)
                if name is None:
                    if t.id not in desc:
                        try:
                            desc[t.id] = _native_thread_description(t.id)
                        except Exception as exc:
                            desc[t.id] = ""
                            log.warning("GetThreadDescription failed tid=%s: %s", t.id, exc)
                    d = desc[t.id]
                    name = f"native:{t.id}#{first_seen[t.id]}" + (f"({d})" if d else "")
                rows.append((pct, name))
            prev = cur
            rows.sort(reverse=True)
            body = " ".join(f"{n}={p:.0f}%" for p, n in rows[:top])
            log.info("cpu total=%.0f%% (%d cores, 100%%=1 core) system=%.0f%% threads=%d top: %s",
                     proc.cpu_percent(None), ncpu, psutil.cpu_percent(None), len(cur), body or "-")
        except Exception as exc:
            log.warning("cpu sampler error: %s: %s", type(exc).__name__, exc)


def start_thread_cpu_sampler(interval: float = 10.0, top: int = 10) -> None:
    global _cpu_thread
    with _cpu_lock:
        if _cpu_thread is not None and _cpu_thread.is_alive():
            return
        _cpu_stop.clear()
        _cpu_thread = threading.Thread(
            target=_cpu_sampler_loop, args=(interval, top), name="CpuSampler", daemon=True)
        _cpu_thread.start()


# --------------------------------------------------------------------------- thread lifecycle

def thread_started(name: str, **fields) -> None:
    extra = (" " + " ".join(f"{k}={v}" for k, v in fields.items())) if fields else ""
    get_logger("thread").info("%s started%s", name, extra)


def thread_exited(name: str, exc: Optional[BaseException] = None) -> None:
    mark_exited(name)
    log = get_logger("thread")
    if exc is not None:
        log.error("%s exited with %s: %s", name, type(exc).__name__, exc,
                  exc_info=(type(exc), exc, exc.__traceback__))
    else:
        log.info("%s exited cleanly", name)


def run_logged(name: str, fn: Callable, *args, **kwargs):
    """Thread-target wrapper: logs start/exit, re-raises so behavior is unchanged."""
    thread_started(name)
    try:
        result = fn(*args, **kwargs)
    except BaseException as exc:
        try:
            exc._jarvis_logged = True  # lets threading.excepthook avoid a duplicate traceback
        except Exception:
            pass
        thread_exited(name, exc)
        raise
    thread_exited(name)
    return result


# --------------------------------------------------------------------------- instrumented lock

_perf = time.perf_counter
_get_ident = threading.get_ident


class InstrumentedRLock:
    """Drop-in RLock that records owner, wait and hold times and warns on slow ones."""

    def __init__(self, name: str, warn_wait_ms: float = 50, warn_hold_ms: float = 250):
        self._lock = threading.RLock()
        self.name = name
        self._warn_wait = warn_wait_ms / 1000.0
        self._warn_hold = warn_hold_ms / 1000.0
        self._owner: Optional[int] = None
        self._owner_name = ""
        self._depth = 0
        self._t_acq = 0.0
        self._site: Any = None
        self.acquisitions = 0
        self.max_wait_ms = 0.0
        self.max_hold_ms = 0.0
        self._log = get_logger("lock")

    # -- helpers
    @staticmethod
    def _site_of(level: int):
        try:
            f = sys._getframe(level)
            return (f.f_code.co_filename, f.f_lineno)
        except ValueError:
            return None

    def _fmt_site(self) -> str:
        s = self._site
        return f"{os.path.basename(s[0])}:{s[1]}" if s else "?"

    def _acquire(self, blocking: bool, timeout: float, level: int) -> bool:
        me = _get_ident()
        if self._owner == me:  # re-entrant
            if not self._lock.acquire(blocking, timeout):
                return False
            self._depth += 1
            return True
        t0 = _perf()
        prev_name = ""
        if not self._lock.acquire(False):
            if not blocking:
                return False
            prev_name = self._owner_name
            prev_site = self._fmt_site()
            if not self._lock.acquire(True, timeout):
                return False
            wait = _perf() - t0
            if wait > self._warn_wait:
                self._log.warning("lock %s: waited %.0f ms; owner was %s at %s",
                                  self.name, wait * 1000.0, prev_name or "?", prev_site)
        else:
            wait = _perf() - t0
        self._owner = me
        self._owner_name = threading.current_thread().name
        self._depth = 1
        self._site = self._site_of(level)
        self.acquisitions += 1
        wms = wait * 1000.0
        if wms > self.max_wait_ms:
            self.max_wait_ms = wms
        self._t_acq = _perf()
        return True

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._acquire(blocking, timeout, 3)

    def release(self) -> None:
        if self._owner != _get_ident():
            self._lock.release()  # raises RuntimeError like RLock
            return
        if self._depth > 1:
            self._depth -= 1
            self._lock.release()
            return
        hold = _perf() - self._t_acq
        hms = hold * 1000.0
        if hms > self.max_hold_ms:
            self.max_hold_ms = hms
        warn = hold > self._warn_hold
        holder, site = self._owner_name, self._fmt_site() if warn else ""
        self._owner = None
        self._depth = 0
        self._lock.release()
        if warn:
            self._log.warning("lock %s: held %.0f ms by %s, acquired at %s",
                              self.name, hms, holder, site)

    def __enter__(self):
        self._acquire(True, -1, 3)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

    def _is_owned(self) -> bool:
        return self._owner == _get_ident()

    # threading.Condition support
    def _release_save(self):
        depth = self._depth
        self._owner = None
        self._depth = 0
        for _ in range(depth):
            self._lock.release()
        return depth

    def _acquire_restore(self, depth) -> None:
        for _ in range(depth):
            self._lock.acquire()
        self._owner = _get_ident()
        self._owner_name = threading.current_thread().name
        self._depth = depth
        self._t_acq = _perf()

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "acquisitions": self.acquisitions,
            "max_wait_ms": self.max_wait_ms,
            "max_hold_ms": self.max_hold_ms,
            "owner": self._owner_name if self._owner is not None else None,
        }


# --------------------------------------------------------------------------- utterances

_utt_counter = itertools.count(1)
_utt_tls = threading.local()
_REDACT_KEYS = frozenset(("text", "transcript", "command", "prompt", "reply"))
_ulog = get_logger("utterance")


def new_utterance() -> str:
    uid = f"u{next(_utt_counter):04d}"
    _utt_tls.uid = uid
    return uid


def set_current_utterance(uid: Optional[str]) -> None:
    _utt_tls.uid = uid


def current_utterance() -> Optional[str]:
    return getattr(_utt_tls, "uid", None)


def _fmt_value(k: str, v: Any) -> str:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<bytes={len(v)}>"
    if k in _REDACT_KEYS:
        return redact(v)
    s = str(v)
    return s if s and " " not in s else repr(s)


def utterance_event(stage: str, uid: Optional[str] = None, **fields) -> None:
    uid = uid or current_utterance() or "-"
    extra = "".join(f" {k}={_fmt_value(k, v)}" for k, v in fields.items())
    _ulog.info("%s %s%s", uid, stage, extra)


# --------------------------------------------------------------------------- census

def process_census() -> Dict[str, Any]:
    """Running JARVIS processes (exe or python main.py under a Jarvis path). Caller logs it."""
    pids: List[int] = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            info = p.info
            name = (info.get("name") or "").lower()
            if name == "jarvis.exe":
                pids.append(info["pid"])
            elif name in ("python.exe", "pythonw.exe"):
                cmd = " ".join(info.get("cmdline") or [])
                if "main.py" in cmd and "jarvis" in cmd.lower():
                    pids.append(info["pid"])
    except Exception as exc:
        _log.warning("process_census failed: %r", exc)
        return {"count": -1, "pids": pids, "error": repr(exc)}
    return {"count": len(pids), "pids": pids}
