"""
Phase 2 supervisor / worker-health spec tests (written ahead of alfred's implementation).
Spec tests for supervisor restart semantics and worker-health reporting.
"""
import inspect
import threading
import time
from types import SimpleNamespace

import pytest

import jarvis_log
import thread_supervisor as ts



def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_raising_thread_restarted_and_backoff_capped_at_60s():
    stop = threading.Event()
    calls = []

    def factory():
        calls.append(time.time())

        def boom():
            raise RuntimeError("x")
        return boom

    sup = ts.ThreadSupervisor(stop_event=stop)
    task = sup.register("Boom", factory, max_restarts=-1, backoff_base=0.05, max_backoff=1000.0)
    try:
        sup.start("Boom")
        assert _wait(lambda: len(calls) >= 2), "raising worker was not restarted"
        task.backoff_base = 0.5
        task.restart_count = 500
        assert task.get_backoff() <= 60.0, "backoff must be capped at 60 s"
    finally:
        sup.stop(timeout=1.0)


def test_clean_exit_without_stop_is_restarted():
    stop = threading.Event()
    calls = []

    def factory():
        calls.append(1)
        return lambda: None  # returns immediately, no error

    sup = ts.ThreadSupervisor(stop_event=stop)
    sup.register("Quick", factory, max_restarts=-1, backoff_base=0.05)
    try:
        sup.start("Quick")
        assert _wait(lambda: len(calls) >= 2), "clean exit without stop was not restarted"
    finally:
        sup.stop(timeout=1.0)


def test_already_started_thread_factory_is_watched_not_started_twice():
    stop = threading.Event()
    errors = []
    release = threading.Event()
    th = threading.Thread(target=release.wait, name="WeatherPollerLike", daemon=True)
    th.start()  # factory returns an already started thread (WeatherPoller style)

    sup = ts.ThreadSupervisor(stop_event=stop)
    sup.register("Weather", lambda: th, max_restarts=2, backoff_base=0.05,
                 on_error=lambda n, e: errors.append(e))
    try:
        sup.start("Weather")
        time.sleep(0.5)
        assert not any(isinstance(e, RuntimeError) for e in errors), errors
        task = sup.get_task("Weather")
        assert task.is_alive()
        assert task.restart_count == 0
    finally:
        release.set()
        sup.stop(timeout=1.0)


def _find_health_summary():
    import importlib
    for mod_name in ("main", "thread_supervisor", "jarvis_log"):
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, "health_summary", None)
        if fn is not None:
            return fn
    pytest.fail("health_summary not found in main / thread_supervisor / jarvis_log")


def _call_summary(fn, engine):
    params = inspect.signature(fn).parameters
    if "engine" in params:
        return fn(engine=engine)
    return fn(engine) if params else fn()


def _state_reason(res):
    if isinstance(res, dict):
        return res["state"], res.get("reason", "")
    if isinstance(res, tuple):
        return res[0], (res[1] if len(res) > 1 else "")
    return res.state, getattr(res, "reason", "")


def test_health_summary_down_on_stale_heartbeat(monkeypatch):
    fn = _find_health_summary()
    jarvis_log.heartbeat("WakeWordDetector")
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 120.0)
    state, reason = _state_reason(_call_summary(fn, SimpleNamespace(active=True)))
    assert state == "down"
    assert 0 < len(reason) <= 28


def test_health_summary_paused_when_engine_inactive():
    fn = _find_health_summary()
    state, reason = _state_reason(_call_summary(fn, SimpleNamespace(active=False)))
    assert state == "paused"
    assert len(reason) <= 28


def test_health_summary_ok():
    fn = _find_health_summary()
    with jarvis_log._hb_lock:
        jarvis_log._hb.clear()
    jarvis_log.heartbeat("Tracker")
    state, reason = _state_reason(_call_summary(fn, SimpleNamespace(active=True)))
    assert state == "ok"
    assert len(reason) <= 28


def test_main_posts_worker_health_only_on_change():
    import main
    assert "worker_health" in inspect.getsource(main), "main.py does not post worker_health"
    poster = getattr(main, "post_worker_health", None) or getattr(main, "_post_worker_health", None)
    assert poster is not None, "no worker-health poster helper found in main"
    posts = []
    overlay = SimpleNamespace(post=lambda kind, **kw: posts.append((kind, kw)))
    poster(overlay, "ok", "")
    poster(overlay, "ok", "")
    assert len([p for p in posts if p[0] == "worker_health"]) == 1
    poster(overlay, "down", "WakeWordDetector stalled")
    assert len([p for p in posts if p[0] == "worker_health"]) == 2
