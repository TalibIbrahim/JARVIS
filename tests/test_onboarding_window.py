"""OnboardingWindow flow, saving, skip, and voice answers (offscreen; fake ctx / voice / personal)."""
import os
import sys
import threading

import pytest
from PySide6.QtWidgets import QApplication

from config import Config


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


class FakePersonal:
    def __init__(self):
        self.updates = []

    def update_profile(self, **f):
        self.updates.append(dict(f))

    def installed_apps(self, limit=40):
        return ["Steam", {"name": "Discord"}, "Spotify", "steam"]


class FakeVoice:
    def __init__(self, answers=None, ok=True):
        self.answers = list(answers or [])
        self.ok = ok
        self.samples = []
        self.threads = []

    def capture_answer(self, timeout=8.0, on_level=None):
        self.threads.append(threading.current_thread())
        if on_level:
            on_level(0.4)
        return self.answers.pop(0) if self.answers else ""

    def calibrate_from_sample(self, expected_text, timeout=12.0, on_level=None):
        self.threads.append(threading.current_thread())
        self.samples.append(expected_text)
        if on_level:
            on_level(0.6)
        return {"ok": self.ok}


class FakeCtx:
    def __init__(self, voice=None):
        self.personal = FakePersonal()
        self.voice = voice
        self.said = []
        self.reloaded = []

    def say(self, t):
        self.said.append(t)

    def reload_config(self, cfg):
        self.reloaded.append(cfg)


@pytest.fixture
def make(app, tmp_path, monkeypatch):
    cfg_path = tmp_path / "jarvis_config.json"
    monkeypatch.setattr("ui.onboarding_window.get_config_path", lambda: str(cfg_path))
    made = []

    def _make(voice=None):
        ctx = FakeCtx(voice)
        cfg = Config()
        cfg.onboarding_done = False
        win = __import__("ui.onboarding_window", fromlist=["OnboardingWindow"]).OnboardingWindow(ctx, cfg)
        made.append(win)
        return win, ctx, cfg, cfg_path

    yield _make
    for w in made:
        w._finished = True
        w.join_workers()
        w.deleteLater()


def _settle(win, app):
    win.join_workers()
    app.processEvents()


def test_flow_navigation_and_save(make, app):
    win, ctx, cfg, cfg_path = make(FakeVoice())
    assert win.page_id() == "hello"
    win._next()
    assert win.page_id() == "name"
    assert ctx.said[-1] == "What should I call you?"
    win.name_edit.setText("Talib")
    win._next()
    assert win.page_id() == "address"
    win.address_seg.setValue("name")
    assert "Talib" in win.address_preview.text()
    win._next()
    assert win.page_id() == "voice"
    win._next()                                   # voice step is optional
    assert win.page_id() == "usage"
    win.use_chips["Gaming"].toggled.emit(True)
    win.apps_edit.setText("Elden Ring, Chrome")
    _settle(win, app)
    win._next()
    assert win.page_id() == "privacy"
    win._prev()
    assert win.page_id() == "usage"
    win._next()
    win._next()
    assert win.page_id() == "done"
    assert "Talib" in win.done_title.text()
    win._next()                                   # finish
    assert win._finished and win.completed and not win.was_skipped
    last = {}
    for u in ctx.personal.updates:
        last.update(u)
    assert last["name"] == "Talib"
    assert last["address_as"] == "name"
    assert last["uses"] == ["Gaming"]
    assert last["favorite_apps"] == ["Elden Ring", "Chrome"]
    assert cfg.onboarding_done is True
    import json
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["onboarding_done"] is True
    assert ctx.reloaded


def test_skip_sets_done_and_keeps_answers(make, app):
    win, ctx, cfg, cfg_path = make(FakeVoice())
    win._next()
    win.name_edit.setText("Ana")
    win.skip()
    assert win.completed and win.was_skipped
    assert cfg.onboarding_done is True
    assert any(u.get("name") == "Ana" for u in ctx.personal.updates)


def test_close_counts_as_skip(make, app):
    win, ctx, cfg, _ = make(FakeVoice())
    win.show()
    win.reject()
    assert cfg.onboarding_done is True and win.was_skipped


def test_voice_name_answer_on_worker_thread(make, app):
    v = FakeVoice(answers=["my name is talib"])
    win, ctx, cfg, _ = make(v)
    win._go(1, speak=False)
    win._listen("name")
    assert win._busy == "name"
    _settle(win, app)
    assert win.name_edit.text() == "Talib"
    assert win._busy is None
    assert v.threads and v.threads[0] is not threading.main_thread()


def test_voice_apps_answer_adds_chips(make, app):
    v = FakeVoice(answers=["Elden Ring and Chrome, Blender"])
    win, ctx, cfg, _ = make(v)
    win._go(4, speak=False)
    _settle(win, app)
    win._listen("apps")
    _settle(win, app)
    assert win.favorites == ["Elden Ring", "Chrome", "Blender"]


def test_installed_apps_become_suggestions(make, app):
    win, ctx, cfg, _ = make(FakeVoice())
    win._go(4, speak=False)
    _settle(win, app)
    assert win._suggestions == ["Steam", "Discord", "Spotify"]
    win._toggle_favorite("Steam", True)
    assert win.favorites == ["Steam"]
    win._toggle_favorite("Steam", False)
    assert win.favorites == []


def test_voice_samples_run_all_and_report(make, app):
    v = FakeVoice()
    win, ctx, cfg, _ = make(v)
    win._go(3, speak=False)
    win._start_samples()
    _settle(win, app)
    assert len(v.samples) == 3
    assert win._busy is None and win._sample_done == 3 and win._sample_ok
    assert all(t is not threading.main_thread() for t in v.threads)


def test_voice_sample_failure_stops_and_allows_retry(make, app):
    v = FakeVoice(ok=False)
    win, ctx, cfg, _ = make(v)
    win._go(3, speak=False)
    win._start_samples()
    _settle(win, app)
    assert len(v.samples) == 1 and win._busy is None and win._sample_done == 0
    v.ok = True
    win._start_samples()
    _settle(win, app)
    assert win._sample_done == 3


def test_no_voice_hides_mics_and_still_works(make, app):
    win, ctx, cfg, _ = make(None)
    assert not win._voice_ok()
    win._listen("name")                           # no-op, no crash
    assert win._busy is None
    win._go(1, speak=False)
    win.name_edit.setText("Sam")
    win.skip()
    assert cfg.onboarding_done is True


def test_speak_respects_cfg_speak(make, app):
    win, ctx, cfg, _ = make(FakeVoice())
    cfg.speak = False
    win._go(1)
    assert ctx.said == []
    cfg.speak = True
    win._go(2)
    assert ctx.said == ["How should I address you?"]


def test_clean_name():
    from ui.onboarding_window import OnboardingWindow as W
    assert W._clean_name("My name is talib.") == "Talib"
    assert W._clean_name("call me Big Al") == "Big Al"
    assert W._clean_name("") == ""
