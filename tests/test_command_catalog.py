"""command_catalog: every voice example routes to its declared action (ported from the alfred self-check).

All side effects (keyboard, subprocess, browser, system_control, network clients, AI) are mocked.
"""
import types
from types import SimpleNamespace as NS
from unittest import mock

import numpy as np
import pytest

from command_catalog import CATEGORIES, get_catalog

_VOICE = [(s, ex) for s in get_catalog() if s.kind == "voice" for ex in s.examples]

ALIAS = {
    "open_this": {"open_this", "copy_this", "close_this"},
    "check_updates": {"check_updates", "install_update"},
    "set_brightness": {"set_brightness", "brightness_up", "brightness_down"},
    "shutdown_system": {"shutdown_system", "restart_system", "__confirm__"},
    "set_volume": {"set_volume"},
    "island_page_step": {"island_page_step", "island_next_page", "island_prev_page"},
    "island_expand": {"island_expand", "island_toggle_expand", "island_collapse"},
    "dwell_toggle": {"dwell_toggle"},
    "snap_window": {"snap_window"},
    "play_music": {"play_music", "__play_media__"},
}
REPLY_CHECK = {  # action -> predicate on the reply (paths that emit no action event)
    "date": lambda r: bool(r) and r.startswith("Today is"),
    "wiki": lambda r: bool(r) and "Wikipedia" in r,
    "chatter": lambda r: bool(r),
    "diagnostics": lambda r: bool(r) and r.startswith("All Mark"),
    "train_voice": lambda r: bool(r) and "oice" in r,
    "world_time": lambda r: bool(r),
    "shutdown_system": lambda r: bool(r) and "confirmation" in r,
}


class _Env:
    pass


@pytest.fixture(scope="module")
def env():
    import pyautogui
    import subprocess
    import webbrowser
    import jarvis_log as jl
    import commands, system_control, window_resolver, file_ops, media_adapter, weather_client
    import quick_answers, productivity, scheduler, routines, screen_awareness, context_resolver
    from config import Config

    mp = pytest.MonkeyPatch()
    e = _Env()
    e.EV, e.CALLS, e.groq_used = [], [], []

    mp.setattr(jl, "utterance_event", lambda stage, uid=None, **f: e.EV.append((stage, f)))
    for n in ("press", "hotkey", "scroll", "write", "typewrite", "click", "keyDown", "keyUp", "mouseDown", "mouseUp"):
        mp.setattr(pyautogui, n, lambda *a, **k: e.CALLS.append("pyautogui"))
    mp.setattr(subprocess, "Popen", lambda *a, **k: e.CALLS.append(("Popen", a)) or NS(pid=0))
    mp.setattr(subprocess, "run", lambda *a, **k: NS(stdout="", returncode=0))
    mp.setattr(webbrowser, "open", lambda *a, **k: e.CALLS.append(("web", a)) or True)

    for mod in (system_control, window_resolver):
        for name in dir(mod):
            obj = getattr(mod, name)
            if (callable(obj) and not name.startswith("_") and isinstance(obj, types.FunctionType)
                    and obj.__module__ == mod.__name__):
                mp.setattr(mod, name, (lambda nm: (lambda *a, **k: e.CALLS.append(("sc", nm)) or (True, "mock ok")))(name))
    for name in ("open_folder", "remember_folder", "show_last_download", "open_recent_file", "open_file"):
        mp.setattr(file_ops, name, (lambda nm: (lambda *a, **k: e.CALLS.append(("fo", nm)) or NS(success=True, message="ok", path="p")))(name))

    # get_system_control was blanket-mocked above; give it a harmless audio stub (never the real endpoint).
    mp.setattr(system_control, "get_system_control", lambda: NS(audio=NS(is_muted=lambda: False)))

    media = mock.MagicMock()
    media.get_current_media.return_value = NS(has_media=True, title="T", artist="A")
    mp.setattr(media_adapter, "get_media_adapter", lambda: media)

    w = NS(temperature=20.0, description="Clear", city="Paris, FR", unit_symbol="C", condition="clear")
    wc = mock.MagicMock()
    wc.get_weather.return_value = w
    mp.setattr(weather_client, "get_weather_client", lambda: wc)

    e.wiki = mock.MagicMock()
    e.wiki.get_summary.return_value = "A scientist."
    mp.setattr(quick_answers, "get_wikipedia_client", lambda: e.wiki)

    mp.setattr(productivity, "add_note", lambda *a, **k: (True, "ok"))
    mp.setattr(productivity, "get_last_note", lambda *a, **k: (True, "last"))
    mp.setattr(productivity, "read_notes", lambda *a, **k: (True, "notes", []))
    mp.setattr(productivity, "type_text", lambda *a, **k: e.CALLS.append("type") or True)
    mp.setattr(productivity, "get_clipboard_text", lambda: "hello. this is a clip. more text here. and more. and yet more.")

    e.sch = mock.MagicMock()
    e.sch.cancel_timer.return_value = True
    e.sch.get_time_left.return_value = 100
    e.sch.get_active_timer.return_value = None
    mp.setattr(scheduler, "get_scheduler", lambda *a, **k: e.sch)
    mp.setattr(routines, "get_routines", lambda cfg=None: dict(routines.DEFAULT_ROUTINES))
    mp.setattr(screen_awareness, "analyze_screen", lambda *a, **k: e.CALLS.append("screen_ai") or "screen reply")

    groq_used = e.groq_used

    class FakeGroq:
        def __init__(self, *a, **k):
            pass

        def describe_image(self, *a, **k):
            groq_used.append("vision")
            return "vision reply"

        def chat(self, *a, **k):
            groq_used.append("chat")
            return "chat reply"

    mp.setattr(commands, "GroqClient", FakeGroq)
    mp.setattr(commands, "get_api_key", lambda: "fake")
    mp.setattr(screen_awareness, "get_api_key", lambda: "fake")

    def fake_stream(**k):
        groq_used.append("system2")
        return '{"action":"none","args":{},"reply":"ok"}'

    mp.setattr(commands, "stream_ai_query", fake_stream)
    mp.setattr(commands, "play_media",
               lambda q, platform="auto": e.CALLS.append(("play_media", platform, q)) or (True, f"Playing {q}"))

    cfg = Config()
    cfg.ai_enabled = True
    cfg.ai_vision_enabled = True
    updater = mock.MagicMock()
    updater.downloaded_path = "x"
    e.ctx = NS(engine=NS(cfg=cfg, active=True), cfg=cfg, overlay=mock.MagicMock(), updater=updater,
               tracker=NS(get_latest_frame=lambda: np.zeros((4, 4, 3), dtype=np.uint8)),
               set_active=lambda v: e.CALLS.append(("set_active", v)), quit=lambda: e.CALLS.append("quit"),
               set_tracking_paused=lambda v: e.CALLS.append(("tracking", v)))
    e.commands, e.productivity, e.context_resolver = commands, productivity, context_resolver
    yield e
    mp.undo()


def test_categories_are_nonempty_and_known():
    by_cat = {}
    for s in get_catalog():
        assert s.category in CATEGORIES, s
        by_cat.setdefault(s.category, []).append(s)
    for c in CATEGORIES:
        assert by_cat.get(c), f"category {c!r} has no entries"


def test_no_duplicate_titles():
    """Within a category (what a filter capsule shows) no two entries may share a title."""
    keys = [(s.category, s.title.strip().lower()) for s in get_catalog()]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicate titles: {dupes}"


def test_specs_are_well_formed():
    for s in get_catalog():
        assert s.title and s.description and s.action
        assert s.examples, s.title
        assert s.kind in ("voice", "gesture")


@pytest.mark.parametrize("spec,example", _VOICE, ids=[f"{s.category}:{ex}" for s, ex in _VOICE])
def test_voice_example_routes_to_declared_action(env, spec, example):
    e = env
    e.EV.clear()
    e.CALLS.clear()
    e.groq_used.clear()
    e.commands._PENDING_CONFIRMATION = None
    e.context_resolver.reset_context()
    e.productivity.set_dictation_mode(False)
    e.sch.reset_mock()
    text = e.commands.strip_wake_prefix(example)
    reply = e.commands.run_command(text, e.ctx)
    acts = {f.get("action") for s, f in e.EV if f.get("action")}
    acts |= {f.get("intent", "").split(":")[0] for s, f in e.EV if f.get("intent")}
    if any(c[0] == "play_media" for c in e.CALLS if isinstance(c, tuple)):
        acts.add("__play_media__")
    if reply and "confirmation" in reply:
        acts.add("__confirm__")
    if spec.requires_ai:
        assert e.groq_used or "screen_ai" in e.CALLS, f"{example!r} did not reach the AI"
        return
    want = ALIAS.get(spec.action, {spec.action})
    ok = bool(acts & want) or (spec.action in REPLY_CHECK and REPLY_CHECK[spec.action](reply))
    if spec.action == "wiki":
        ok = ok and e.wiki.get_summary.called
    assert ok, f"{example!r}: want {spec.action}, got actions {sorted(a for a in acts if a)}, reply {reply!r}"
    assert not e.groq_used, f"{example!r} reached the AI ({e.groq_used}) but should be local"
