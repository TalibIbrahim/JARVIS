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
        max_backoff: float = 10.0,
        on_error: Optional[Callable[[str, Exception], None]] = None,
    ):
        self.name = name
        self.factory = factory
        self.stop_event = stop_event
        self.max_restarts = max_restarts
        self.backoff_base = max(0.05, float(backoff_base))
        self.backoff_multiplier = max(1.0, float(backoff_multiplier))
        self.max_backoff = max(self.backoff_base, float(max_backoff))
        self.on_error = on_error

        self.restart_count = 0
        self.last_error: Optional[Exception] = None
        self.last_crash_time: float = 0.0
        self.is_terminal = False
        self.is_running = False
        self._runner_thread: Optional[threading.Thread] = None
        self._current_instance: Any = None
        self._lock = threading.RLock()

    def get_backoff(self) -> float:
        """Computes exponential backoff for current restart count."""
        raw = self.backoff_base * (self.backoff_multiplier ** max(0, self.restart_count - 1))
        return min(raw, self.max_backoff)

    def is_alive(self) -> bool:
        """Returns True if the supervised worker runner thread is active."""
        with self._lock:
            return bool(self._runner_thread and self._runner_thread.is_alive() and not self.is_terminal)

    def get_status(self) -> Dict[str, Any]:
        """Returns snapshot of task health."""
        with self._lock:
            return {
                "name": self.name,
                "alive": self.is_alive(),
                "running": self.is_running,
                "terminal": self.is_terminal,
                "restarts": self.restart_count,
                "last_error": str(self.last_error) if self.last_error else None,
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
        if inst and hasattr(inst, "join"):
            try:
                inst.join(timeout=timeout)
            except Exception:
                pass
        with self._lock:
            t = self._runner_thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def _run_loop(self) -> None:
        """Supervision loop: creates instances and restarts on failure with backoff."""
        while not self.stop_event.is_set() and not self.is_terminal:
            try:
                with self._lock:
                    self._current_instance = self.factory()
                    inst = self._current_instance

                if inst is None:
                    break

                self.is_running = True
                logger.info("task '%s' start (attempt #%d, instance=%s)",
                            self.name, self.restart_count + 1, type(inst).__name__)

                # If instance is a threading.Thread
                if isinstance(inst, threading.Thread):
                    inst.start()
                    # Wait for thread completion or stop event
                    while not self.stop_event.is_set() and inst.is_alive():
                        inst.join(timeout=0.2)
                    if self.stop_event.is_set():
                        logger.info("task '%s' stopping on stop_event", self.name)
                        break
                    # If thread finished without stop_event, check if it was clean or abnormal
                    if hasattr(inst, "error") and inst.error:
                        raise inst.error
                    logger.warning("task '%s' worker thread ended without error and without stop_event", self.name)
                elif callable(inst):
                    inst()
                elif hasattr(inst, "run") and callable(inst.run):
                    inst.run()
                else:
                    break

                # Clean exit without stop event
                logger.info("task '%s' exit clean (no restart)", self.name)
                break

            except Exception as exc:
                logger.error("task '%s' crashed: %s: %s", self.name, type(exc).__name__, exc,
                             exc_info=(type(exc), exc, exc.__traceback__))
                self.last_error = exc
                self.last_crash_time = time.time()
                self.restart_count += 1
                self.is_running = False

                if self.on_error:
                    try:
                        self.on_error(self.name, exc)
                    except Exception:
                        pass

                logger.warning("task '%s' restart #%d in %.2fs (max_restarts=%d)",
                               self.name, self.restart_count, self.get_backoff(), self.max_restarts)

                if self.max_restarts >= 0 and self.restart_count > self.max_restarts:
                    self.is_terminal = True
                    logger.error("task '%s' TERMINAL: reached maximum restart attempts (%d); degrading to offline",
                                 self.name, self.max_restarts)
                    break

                backoff = self.get_backoff()
                self.stop_event.wait(timeout=backoff)


class ThreadSupervisor:
    """
    Central supervisor managing all background threads with top-level try/except
    guards, auto-restart backoff, and graceful degradation.
    """

    def __init__(self, stop_event: Optional[threading.Event] = None):
        self.stop_event = stop_event or threading.Event()
        self._tasks: Dict[str, SupervisedTask] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        factory: Callable[[], Any],
        max_restarts: int = 5,
        backoff_base: float = 0.5,
        backoff_multiplier: float = 2.0,
        max_backoff: float = 10.0,
        on_error: Optional[Callable[[str, Exception], None]] = None,
    ) -> SupervisedTask:
        """Registers a named worker factory under supervision."""
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
