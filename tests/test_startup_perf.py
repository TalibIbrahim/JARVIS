"""Island-first startup helpers and first-use import warm-up (no Qt window, no network, no devices)."""
import sys
import threading
import types

import main
from config import Config


def test_cursor_glide_default_true():
    assert Config().cursor_glide is True


def test_warm_imports_runs_and_logs_missing(monkeypatch):
    mod = types.ModuleType("_warm_probe_ok")
    monkeypatch.setitem(sys.modules, "_warm_probe_ok", mod)
    t = main.warm_first_use_imports(threading.Event(), delay=0, modules=("_warm_probe_ok", "_no_such_mod_xyz"),
                                    lower_priority=False)
    t.join(5)
    assert not t.is_alive()


def test_warm_imports_stops_before_work_when_stop_set():
    stop = threading.Event()
    stop.set()
    t = main.warm_first_use_imports(stop, delay=30, modules=("_no_such_mod_xyz",), lower_priority=False)
    t.join(2)
    assert not t.is_alive()


def test_post_cached_weather_posts_payload(monkeypatch):
    import weather_client
    cached = {"payload": {"temp": 1}, "stale": False, "saved_at": 5.0}
    monkeypatch.setattr(weather_client, "load_weather_cache", lambda c, u: cached)
    posts = []
    ov = types.SimpleNamespace(post=lambda kind, **d: posts.append((kind, d)))
    ctx = types.SimpleNamespace(cfg=Config())
    assert main.post_cached_weather(ctx, ov) is True
    assert posts[0][0] == "weather_state" and posts[0][1]["updated_at"] == 5.0


def test_post_cached_weather_no_cache(monkeypatch):
    import weather_client
    monkeypatch.setattr(weather_client, "load_weather_cache", lambda c, u: None)
    ov = types.SimpleNamespace(post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no post expected")))
    assert main.post_cached_weather(types.SimpleNamespace(cfg=Config()), ov) is False


def test_workers_start_via_thread_without_qt(monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", None)
    ran = threading.Event()
    t = main._start_workers_after_first_frame(ran.set)
    t.join(2)
    assert ran.is_set()
