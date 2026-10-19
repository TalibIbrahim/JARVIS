"""Duck other applications' audio while JARVIS listens (no Qt).

Per-application volumes are scaled through the Windows audio-session API (pycaw).
JARVIS's own process and the system-sounds session are never touched. All COM work
runs on short-lived worker threads (each does CoInitialize), so callers never block.

API: duck(level, reason) -> bool, restore(reason) -> bool, is_ducked() -> bool, shutdown().
Return values mean "request accepted"; failures are logged, never raised.
"""
from __future__ import annotations

import atexit
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import jarvis_log

_log = jarvis_log.get_logger("audio_ducker")

DUCK_FADE_S = 0.12
RESTORE_FADE_S = 0.25
FADE_STEP_S = 0.02
AUTO_RESTORE_S = 20.0
USER_CHANGE_TOLERANCE = 0.05


class _Session:
    """One app audio session: key, pid, is_system, get(), set()."""

    def __init__(self, key: str, pid: int, is_system: bool, vol: Any):
        self.key, self.pid, self.is_system, self._vol = key, pid, is_system, vol

    def get(self) -> float:
        return float(self._vol.GetMasterVolume())

    def set(self, v: float) -> None:
        self._vol.SetMasterVolume(max(0.0, min(1.0, float(v))), None)


class _PycawLayer:
    """Real session layer. Must be called from a COM-initialized thread."""

    def init_thread(self) -> None:
        import comtypes
        comtypes.CoInitialize()

    def uninit_thread(self) -> None:
        import comtypes
        comtypes.CoUninitialize()

    def sessions(self) -> List[_Session]:
        from pycaw.pycaw import AudioUtilities
        out: List[_Session] = []
        for i, s in enumerate(AudioUtilities.GetAllSessions()):
            try:
                pid = int(s.ProcessId or 0)
                ident = getattr(s, "Identifier", None) or f"{pid}:{i}"
                out.append(_Session(str(ident), pid, pid == 0, s.SimpleAudioVolume))
            except Exception as e:
                _log.warning("session inspect failed: %s", e)
        return out


class AudioDucker:
    def __init__(self, layer: Any = None, own_pid: Optional[int] = None,
                 auto_restore_s: float = AUTO_RESTORE_S, fade_scale: float = 1.0):
        self._layer = layer or _PycawLayer()
        self._pid = os.getpid() if own_pid is None else own_pid
        self._auto_s = auto_restore_s
        self._fade_scale = fade_scale
        self._lock = threading.Lock()      # guards state below
        self._op_lock = threading.Lock()   # serialises COM work
        self._ducked = False
        self._gen = 0
        self._saved: Dict[str, Tuple[float, float]] = {}  # key -> (original, target)
        self._timer: Optional[threading.Timer] = None
        self._threads: List[threading.Thread] = []

    # ------------------------------------------------------------------ public
    def is_ducked(self) -> bool:
        with self._lock:
            return self._ducked

    def duck(self, level: float = 0.2, reason: str = "") -> bool:
        try:
            level = max(0.0, min(1.0, float(level)))
            with self._lock:
                self._gen += 1
                gen = self._gen
                already = self._ducked
                self._ducked = True
                self._arm_timer_locked()
            if already:
                _log.debug("duck refresh (already ducked) reason=%s", reason)
                return True
            _log.info("duck level=%.2f reason=%s", level, reason)
            self._spawn(self._do_duck, gen, level)
            return True
        except Exception as e:
            _log.error("duck failed: %s", e)
            return False

    def restore(self, reason: str = "") -> bool:
        try:
            with self._lock:
                if not self._ducked:
                    return False
                self._gen += 1
                gen = self._gen
                self._ducked = False
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
            _log.info("restore reason=%s", reason)
            self._spawn(self._do_restore, gen)
            return True
        except Exception as e:
            _log.error("restore failed: %s", e)
            return False

    def shutdown(self) -> None:
        try:
            if self.restore("shutdown"):
                for t in list(self._threads):
                    t.join(timeout=2.0)
        except Exception as e:
            _log.error("shutdown failed: %s", e)

    # ------------------------------------------------------------------ internals
    def _arm_timer_locked(self) -> None:
        if self._timer:
            self._timer.cancel()
        t = threading.Timer(self._auto_s, self._auto_restore)
        t.daemon = True
        t.start()
        self._timer = t

    def _auto_restore(self) -> None:
        _log.warning("auto-restore after %.0fs without restore()", self._auto_s)
        self.restore("auto-timeout")

    def _spawn(self, fn, *args) -> None:
        t = threading.Thread(target=self._run, args=(fn, *args), name="audio-ducker", daemon=True)
        self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()

    def _run(self, fn, *args) -> None:
        try:
            self._layer.init_thread()
        except Exception as e:
            _log.error("COM init failed: %s", e)
            return
        try:
            with self._op_lock:
                fn(*args)
        except Exception as e:
            _log.error("%s failed: %s", fn.__name__, e)
        finally:
            try:
                self._layer.uninit_thread()
            except Exception as e:
                _log.warning("COM uninit failed: %s", e)

    def _current(self, gen: int) -> bool:
        with self._lock:
            return self._gen == gen

    def _fade(self, gen: int, moves: List[Tuple[_Session, float, float]], duration: float) -> None:
        duration *= self._fade_scale
        steps = max(1, int(duration / FADE_STEP_S)) if duration > 0 else 1
        for n in range(1, steps + 1):
            if not self._current(gen):
                return
            f = n / steps
            for sess, a, b in moves:
                try:
                    sess.set(a + (b - a) * f)
                except Exception as e:
                    _log.warning("set volume failed pid=%s: %s", sess.pid, e)
            if n < steps:
                time.sleep(FADE_STEP_S * self._fade_scale)

    def _do_duck(self, gen: int, level: float) -> None:
        moves = []
        saved: Dict[str, Tuple[float, float]] = {}
        sessions = self._layer.sessions()
        for s in sessions:
            if s.is_system or s.pid == self._pid:
                continue
            try:
                orig = s.get()
            except Exception as e:
                _log.warning("get volume failed pid=%s: %s", s.pid, e)
                continue
            saved[s.key] = (orig, orig * level)
            moves.append((s, orig, orig * level))
        with self._lock:
            if self._gen != gen:
                return  # restore() already superseded us; nothing was changed
            self._saved = saved
            self._sessions = {s.key: s for s, _, _ in moves}
        _log.debug("ducking %d session(s)", len(moves))
        self._fade(gen, moves, DUCK_FADE_S)

    def _do_restore(self, gen: int) -> None:
        with self._lock:
            saved, self._saved = self._saved, {}
            sessions = getattr(self, "_sessions", {})
            self._sessions = {}
        live = {s.key: s for s in self._layer.sessions()}
        moves = []
        for key, (orig, target) in saved.items():
            s = live.get(key) or sessions.get(key)
            if s is None:
                continue
            try:
                cur = s.get()
            except Exception as e:
                _log.warning("session gone, skip restore pid=%s: %s", s.pid, e)
                continue
            if abs(cur - target) > USER_CHANGE_TOLERANCE:
                _log.info("user changed volume while ducked (pid=%s), leaving it", s.pid)
                continue
            moves.append((s, cur, orig))
        self._fade(gen, moves, RESTORE_FADE_S)


_ducker: Optional[AudioDucker] = None
_ducker_lock = threading.Lock()


def _get() -> AudioDucker:
    global _ducker
    with _ducker_lock:
        if _ducker is None:
            _ducker = AudioDucker()
            atexit.register(shutdown)
        return _ducker


def duck(level: float = 0.2, reason: str = "") -> bool:
    return _get().duck(level, reason)


def restore(reason: str = "") -> bool:
    return _get().restore(reason)


def is_ducked() -> bool:
    return _ducker.is_ducked() if _ducker else False


def shutdown() -> None:
    if _ducker:
        _ducker.shutdown()
