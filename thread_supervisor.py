"""
thread_supervisor.py — Background Thread Supervision & Graceful Degradation for J.A.R.V.I.S.

Supervises background worker threads (Tracker, Voice, AutoUpdater) with:
- Top-level try/except exception guards preventing any thread crash from taking down the process.
- Exponential backoff auto-restart logic (e.g. 0.5s, 1s, 2s, 4s, capped at max_backoff).
- Thread health tracking (is_alive, restart_count, last_error, dead_threads_count).
- Clean graceful degradation:
  * Camera failure -> voice and hotkeys continue running.
  * Microphone failure -> camera gesture tracking continues running.
  * Network/Offline -> System 1 reflex intent matching operates 100% locally.
- Strictly NO Qt imports. 100% unit-testable with mocks.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union

logger = logging.getLogger("jarvis.supervisor")


MAX_BACKOFF_S = 60.0
HEALTHY_RESET_S = 120.0     # restart_count resets after this long without a crash
SETTLE_S = 5.0              # a freshly restarted instance counts as "restarting" for this long
EXTRA_HEARTBEATS = ("Speaker", "CursorLoop")   # unsupervised, but a stale beat still means "down"

# short user-facing labels for a stalled heartbeat name
_STALL_LABEL = {
    "FrameGrabber": "Camera stalled", "Tracker": "Tracking stalled", "Voice": "Voice stalled",
    "WakeWordDetector": "Mic stalled", "STTWorker": "Speech stalled",
    "Speaker": "Speaker stalled", "CursorLoop": "Cursor stalled",
}


def _short(reason: str, limit: int = 28) -> str:
    reason = str(reason or "")
    return reason if len(reason) <= limit else reason[: limit - 1] + "~"


class SupervisedTask:
    """Represents a supervised background thread or worker task."""

    def __init__(
        self,
        name: str,
        factory: Callable[[], Any],
        stop_event: threading.Event,
        max_restarts: int = 5,
        backoff_base: float = 0.5,
        backoff_multiplier: float = 2.0,
        max_backoff: float = MAX_BACKOFF_S,
        on_error: Optional[Callable[[str, Exception], None]] = None,
        heartbeat_names: Optional[List[str]] = None,
        stall_after: float = 10.0,
    ):
        self.name = name
        self.factory = factory
        self.stop_event = stop_event
        self.max_restarts = max_restarts          # -1 = never terminal (keeps retrying at the cap)
        self.backoff_base = max(0.05, float(backoff_base))
        self.backoff_multiplier = max(1.0, float(backoff_multiplier))
        self.max_backoff = min(MAX_BACKOFF_S, max(self.backoff_base, float(max_backoff)))
        self.on_error = on_error
        self.heartbeat_names: List[str] = list(heartbeat_names or [])
        self.stall_after = float(stall_after)

        self.restart_count = 0
        self.last_error: Optional[BaseException] = None
        self.last_exit_reason: str = ""
        self.last_crash_time: float = 0.0
        self.is_terminal = False
        self.is_running = False
        self.in_backoff = False
        self.started_mono: float = 0.0     # monotonic start of the current instance
        self._runner_thread: Optional[threading.Thread] = None
        self._current_instance: Any = None
        self._lock = threading.RLock()

    def get_backoff(self) -> float:
        """base * multiplier^(n-1), capped (60 s)."""
        exp = min(max(0, self.restart_count - 1), 64)
        raw = self.backoff_base * (self.backoff_multiplier ** exp)
        return min(raw, self.max_backoff)

    def is_alive(self) -> bool:
        """Returns True if the supervised worker runner thread is active."""
        with self._lock:
            return bool(self._runner_thread and self._runner_thread.is_alive() and not self.is_terminal)

    def is_restarting(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.is_terminal:
                return False
            if self.in_backoff:
                return True
            return self.restart_count > 0 and self.is_running and (now - self.started_mono) < SETTLE_S

    def stalled_names(self, health: Dict[str, Dict[str, Any]], now_mono: Optional[float] = None) -> List[str]:
        """Heartbeat names of this task that are stale. A name that has not beaten since the current
        instance started is only stale after max(3*stall_after, 30 s) (model loads are slow)."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            started = self.started_mono
            running = self.is_running and not self.in_backoff
        out = []
        for hb in self.heartbeat_names:
            h = health.get(hb)
            if h is None or not running:
                continue
            age = h["age"]
            since_start = now_mono - started
            if age > since_start:        # last beat predates this instance
                if since_start > max(3 * self.stall_after, 30.0):
                    out.append(hb)
            elif age > self.stall_after:
                out.append(hb)
        return out

    def get_status(self) -> Dict[str, Any]:
        """Returns snapshot of task health."""
        with self._lock:
            return {
                "name": self.name,
                "alive": self.is_alive(),
                "running": self.is_running,
                "terminal": self.is_terminal,
                "restarting": self.is_restarting(),
                "restarts": self.restart_count,
                "last_error": str(self.last_error) if self.last_error else None,
                "last_exit_reason": self.last_exit_reason,
                "last_crash_time": self.last_crash_time,
            }

    def start(self) -> None:
        """Spawns the supervisor runner loop for this task."""
        with self._lock:
            if self._runner_thread and self._runner_thread.is_alive():
                return
            self._runner_thread = threading.Thread(target=self._run_loop, name=f"Supervisor-{self.name}", daemon=True)
            self._runner_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stops the task and awaits termination."""
        self.stop_event.set()
        with self._lock:
            inst = self._current_instance
        if inst is not None and hasattr(inst, "join"):
            try:
                inst.join(timeout=timeout)
            except RuntimeError as exc:   # joining a never-started thread
                logger.warning("task '%s' stop: cannot join instance: %s", self.name, exc)
        with self._lock:
            t = self._runner_thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    # -- one supervised lifetime ------------------------------------------------------
    def _run_instance(self, inst: Any) -> Tuple[Optional[BaseException], str]:
        """Runs/watches one instance until it exits. Returns (exception_or_None, how_it_ended)."""
        if isinstance(inst, threading.Thread):
            captured: List[BaseException] = []
            if inst.ident is None:      # not started yet: wrap run() so we can report the exception
                orig_run = inst.run

                def _wrapped_run():
                    try:
                        orig_run()
                    except BaseException as exc:
                        captured.append(exc)
                        raise
                inst.run = _wrapped_run  # type: ignore[method-assign]
                inst.start()
            else:
                logger.info("task '%s' watching already-started thread %s (not starting it)", self.name, inst.name)
            while not self.stop_event.is_set() and inst.is_alive():
                inst.join(timeout=0.2)
            if self.stop_event.is_set():
                return None, "stop"
            err = captured[0] if captured else getattr(inst, "error", None)
            if err:
                return err, "thread raised"
            return None, "thread ended without exception (or exception not observable for a pre-started thread)"
        if callable(inst):
            inst()
            return None, "callable returned"
        if hasattr(inst, "run") and callable(inst.run):
            inst.run()
            return None, "run() returned"
        return None, "unsupervisable"

    def _run_loop(self) -> None:
        """Supervision loop: restarts on ANY unexpected exit (exception or clean) with backoff."""
        while not self.stop_event.is_set() and not self.is_terminal:
            exc: Optional[BaseException] = None
            how = ""
            try:
                with self._lock:
                    self._current_instance = self.factory()
                    inst = self._current_instance
                if inst is None:
                    logger.info("task '%s' factory returned None: nothing to supervise", self.name)
                    break
                with self._lock:
                    self.is_running = True
                    self.in_backoff = False
                    self.started_mono = time.monotonic()
                logger.info("task '%s' start (attempt #%d, instance=%s)",
                            self.name, self.restart_count + 1, type(inst).__name__)
                exc, how = self._run_instance(inst)
                if how == "unsupervisable":
                    logger.warning("task '%s' instance %s is not a thread/callable/runnable; not supervised",
                                   self.name, type(inst).__name__)
                    break
                if how == "stop":
                    logger.info("task '%s' stopping on stop_event", self.name)
                    break
            except Exception as e:      # factory failure or inline callable raising
                exc, how = e, "raised"

            if self.stop_event.is_set():
                break

            ran = time.monotonic() - self.started_mono if self.started_mono else 0.0
            with self._lock:
                self.is_running = False
                if ran >= HEALTHY_RESET_S and self.restart_count:
                    logger.info("task '%s' ran %.0fs healthy before exit; restart_count reset", self.name, ran)
                    self.restart_count = 0
                self.restart_count += 1
                self.last_crash_time = time.time()
                self.last_error = exc
                self.last_exit_reason = (f"{type(exc).__name__}: {exc}" if exc is not None else how)
                self.in_backoff = True

            if exc is not None:
                logger.error("task '%s' died after %.1fs with exception %s: %s", self.name, ran,
                             type(exc).__name__, exc, exc_info=(type(exc), exc, exc.__traceback__))
            else:
                logger.warning("task '%s' exited without exception after %.1fs (%s) and stop_event is not set",
                               self.name, ran, how)

            if self.on_error:
                try:
                    self.on_error(self.name, exc if isinstance(exc, Exception) else RuntimeError(self.last_exit_reason))
                except Exception:
                    logger.exception("task '%s' on_error callback failed", self.name)

            if self.max_restarts >= 0 and self.restart_count > self.max_restarts:
                with self._lock:
                    self.is_terminal = True
                    self.in_backoff = False
                logger.error("task '%s' TERMINAL: reached maximum restart attempts (%d); degrading to offline",
                             self.name, self.max_restarts)
                break

            backoff = self.get_backoff()
            logger.warning("task '%s' restart #%d in %.2fs (max_restarts=%s%s)", self.name, self.restart_count,
                           backoff, self.max_restarts, ", never terminal" if self.max_restarts < 0 else "")
            self.stop_event.wait(timeout=backoff)


def _call_state(obj: Any, method: str) -> Optional[Tuple[str, str]]:
    """Calls obj.camera_state()/mic_state() if it exists. Returns (state, reason) or None if unavailable."""
    fn = getattr(obj, method, None) if obj is not None else None
    if not callable(fn):
        return None
    try:
        res = fn()
    except Exception as exc:
        logger.exception("%s() raised", method)
        return "down", type(exc).__name__
    if isinstance(res, (tuple, list)):
        return str(res[0]), (str(res[1]) if len(res) > 1 else "")
    return str(res), ""


def _compute_health(tasks: List[SupervisedTask], engine=None, tracker=None, voice=None,
                    extra_stall_after: float = 10.0) -> Dict[str, Any]:
    import jarvis_log
    now = time.monotonic()
    health = jarvis_log.get_health(stall_after=1e9)   # we apply our own per-task thresholds
    workers: Dict[str, str] = {}
    reasons: List[str] = []

    cam = _call_state(tracker, "camera_state")
    if cam is not None:
        workers["Camera"] = cam[0]
        # "opening" is a normal startup state (DSHOW open takes ~3-4 s); a hung open is caught by the
        # FrameGrabber heartbeat never starting (stalled_names: 30 s grace), so it is not "down" here.
        if cam[0] not in ("ok", "opening"):
            reasons.append(f"Camera {cam[0]}")
    mic = _call_state(voice, "mic_state")
    if mic is not None:
        workers["Mic"] = mic[0]
        if mic[0] not in ("ok", "opening"):
            reasons.append(f"Mic {mic[0]}")

    task_hbs = set()
    for task in tasks:
        task_hbs.update(task.heartbeat_names)
        if task.is_terminal:
            workers[task.name] = "terminal"
            reasons.append(f"{task.name} offline")
        elif task.is_restarting(now):
            workers[task.name] = "restarting"
            reasons.append(f"{task.name} restarting ({task.restart_count})")
        else:
            stalled = task.stalled_names(health, now)
            if stalled:
                workers[task.name] = "stalled"
                reasons.append(_STALL_LABEL.get(stalled[0], f"{stalled[0]} stalled"))
            else:
                workers[task.name] = "ok"

    for name, h in health.items():
        if name in task_hbs:
            continue
        exited = h["state"] == "exited"
        stale = h["age"] > extra_stall_after
        if stale and (not exited or name in EXTRA_HEARTBEATS):
            workers[name] = "exited" if exited else "stalled"
            reasons.append(_STALL_LABEL.get(name, f"{name} stalled"))
        elif name in EXTRA_HEARTBEATS:
            workers[name] = "ok"

    if reasons:
        return {"state": "down", "reason": _short(reasons[0]), "workers": workers}
    if engine is not None and getattr(engine, "active", True) is False:
        return {"state": "paused", "reason": "Tracking paused", "workers": workers}
    return {"state": "ok", "reason": "", "workers": workers}


def health_summary(engine=None, tracker=None, voice=None, supervisor: Optional["ThreadSupervisor"] = None) -> Dict[str, Any]:
    """Module-level convenience: heartbeat-only health when no supervisor is given."""
    if supervisor is not None:
        return supervisor.health_summary(engine=engine, tracker=tracker, voice=voice)
    return _compute_health([], engine=engine, tracker=tracker, voice=voice)


class ThreadSupervisor:
    """
    Central supervisor managing all background threads with top-level try/except
    guards, auto-restart backoff, and graceful degradation.
    """

    def __init__(self, stop_event: Optional[threading.Event] = None):
        self.stop_event = stop_event or threading.Event()
        self._tasks: Dict[str, SupervisedTask] = {}
        self._lock = threading.RLock()
        self._monitor_thread: Optional[threading.Thread] = None

    def register(
        self,
        name: str,
        factory: Callable[[], Any],
        max_restarts: int = 5,
        backoff_base: float = 0.5,
        backoff_multiplier: float = 2.0,
        max_backoff: float = MAX_BACKOFF_S,
        on_error: Optional[Callable[[str, Exception], None]] = None,
        heartbeat_names: Optional[List[str]] = None,
        stall_after: float = 10.0,
    ) -> SupervisedTask:
        """Registers a named worker factory under supervision.

        The factory may return an unstarted Thread (the supervisor starts it), an already-started
        Thread (the supervisor only watches it), a callable (run inline), or None (nothing to supervise).
        max_restarts=-1 means never terminal: keep retrying at the backoff cap."""
        with self._lock:
            task = SupervisedTask(
                name=name,
                factory=factory,
                stop_event=self.stop_event,
                max_restarts=max_restarts,
                backoff_base=backoff_base,
                backoff_multiplier=backoff_multiplier,
                max_backoff=max_backoff,
                on_error=on_error,
                heartbeat_names=heartbeat_names,
                stall_after=stall_after,
            )
            self._tasks[name] = task
            return task

    def start(self, name: Optional[str] = None) -> None:
        """Starts one or all registered tasks."""
        with self._lock:
            if name:
                if name in self._tasks:
                    self._tasks[name].start()
            else:
                for task in self._tasks.values():
                    task.start()

    def stop(self, name: Optional[str] = None, timeout: float = 2.0) -> None:
        """Stops one or all registered tasks."""
        with self._lock:
            tasks = [self._tasks[name]] if name and name in self._tasks else list(self._tasks.values())
        for task in tasks:
            task.stop(timeout=timeout)

    def get_task(self, name: str) -> Optional[SupervisedTask]:
        """Returns the task by name."""
        with self._lock:
            return self._tasks.get(name)

    def get_task_instance(self, name: str) -> Any:
        """Returns the current worker instance of a task."""
        with self._lock:
            task = self._tasks.get(name)
            return task._current_instance if task else None

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Returns status of all supervised tasks."""
        with self._lock:
            return {name: t.get_status() for name, t in self._tasks.items()}

    def is_all_alive(self) -> bool:
        """Returns True if all registered tasks are alive."""
        with self._lock:
            if not self._tasks:
                return True
            return all(t.is_alive() for t in self._tasks.values())

    def dead_threads_count(self) -> int:
        """Returns the number of tasks that have crashed and reached terminal state."""
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.is_terminal or (not t.is_alive() and t.restart_count > 0))

    def health_summary(self, engine=None, tracker=None, voice=None) -> Dict[str, Any]:
        """dict(state="ok"|"paused"|"down", reason=str(<=28), workers={name: state})."""
        with self._lock:
            tasks = list(self._tasks.values())
        return _compute_health(tasks, engine=engine, tracker=tracker, voice=voice)

    def start_health_monitor(self, on_health: Callable[[Dict[str, Any]], None],
                             context: Optional[Callable[[], Dict[str, Any]]] = None,
                             interval: float = 1.0) -> threading.Thread:
        """One monitor thread: every `interval` s resets restart counts of long-healthy tasks, computes
        health_summary(**context()) and hands it to on_health. Never kills worker threads."""
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return self._monitor_thread

            def _loop():
                while not self.stop_event.wait(interval):
                    try:
                        now = time.monotonic()
                        with self._lock:
                            tasks = list(self._tasks.values())
                        for t in tasks:
                            if (t.restart_count and t.is_running and not t.in_backoff
                                    and now - t.started_mono >= HEALTHY_RESET_S):
                                logger.info("task '%s' healthy for %.0fs; restart_count %d -> 0",
                                            t.name, now - t.started_mono, t.restart_count)
                                t.restart_count = 0
                        on_health(self.health_summary(**(context() if context else {})))
                    except Exception:
                        logger.exception("health monitor tick failed")

            self._monitor_thread = threading.Thread(target=_loop, name="SupervisorHealth", daemon=True)
            self._monitor_thread.start()
            return self._monitor_thread


# Module-level graceful degradation notification helper
def explain_graceful_degradation(
    subsystem: str,
    overlay: Optional[Any] = None,
    speaker: Optional[Any] = None,
) -> str:
    """
    Explains degraded functionality to the user cleanly via Dynamic Island and voice.
    """
    sub = subsystem.lower()
    if sub in ("camera", "vision", "tracker"):
        msg = "CAMERA OFFLINE - VOICE ACTIVE"
        speech = "Camera feed is currently offline, sir. Voice commands and hotkeys remain fully operational."
    elif sub in ("voice", "mic", "microphone"):
        msg = "MIC OFFLINE - GESTURES ACTIVE"
        speech = "Microphone input is currently unavailable, sir. Hand gestures and mouse control remain active."
    elif sub in ("network", "offline", "groq", "ai"):
        msg = "OFFLINE - SYSTEM 1 REFLEX ACTIVE"
        speech = "Operating in offline mode with local reflex commands only, sir."
    else:
        msg = f"{subsystem.upper()} OFFLINE"
        speech = f"{subsystem} is currently offline, sir."

    if overlay:
        try:
            overlay.post("error", text=msg)
            overlay.post("say", text=msg, speak=False)
        except Exception:
            pass

    if speaker and hasattr(speaker, "say"):
        try:
            speaker.say(speech)
        except Exception:
            pass

    return msg
