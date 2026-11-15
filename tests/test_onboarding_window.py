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


def test_skip_skips_one_step_and_keeps_answers(make, app):
    win, ctx, cfg, cfg_path = make(FakeVoice())
    win._next()
    win.name_edit.setText("Ana")
    win.skip()
    assert win.page_id() == "address"             # only this step was skipped
    assert not win._finished and not win.completed
    assert cfg.onboarding_done is False
    assert any(u.get("name") == "Ana" for u in ctx.personal.updates)


def test_skip_on_voice_step_goes_to_next_page_not_done(make, app):
    win, ctx, cfg, cfg_path = make(None)          # voice unavailable: the user report
    win._go(3, speak=False)
    assert win.page_id() == "voice"
    win.skip()
    assert win.page_id() == "usage"
    assert cfg.onboarding_done is False and not win._finished
    assert not cfg_path.exists()
    win.skip()
    win.skip()
    assert win.page_id() == "done"
    assert not win._btn_skip.isVisibleTo(win)
    assert cfg.onboarding_done is False
    win._next()
    assert cfg.onboarding_done is True and win.completed and not win.was_skipped


def test_skip_while_calibrating_drops_work_and_advances(make, app):
    win, ctx, cfg, _ = make(FakeVoice())
    win._go(3, speak=False)
    win._busy = "voice"                           # as if a sample were being read
    tok = win._token
    win.skip()
    assert win.page_id() == "usage" and win._busy is None and win._token > tok


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
    assert win.page_id() == "address" and cfg.onboarding_done is False
    win.reject()                                  # closing still skips the rest
    assert cfg.onboarding_done is True and win.was_skipped


VOICE_OFF = "Voice commands are off. Turn them on in Settings > Voice, or skip this step."


def test_voice_off_by_cfg_hides_mics_and_shows_footnote(make, app):
    from ui.onboarding_window import OnboardingWindow
    ctx = FakeCtx(FakeVoice())
    cfg = Config()
    cfg.voice_enabled = False
    win = OnboardingWindow(ctx, cfg)
    try:
        assert not win._voice_ok()
        assert win.name_mic.isHidden() and win.apps_mic.isHidden()
        assert win.voice_mic.isHidden()
        assert _footnote(win.voice_card) == VOICE_OFF
        assert win.btn_voice_on.isHidden()   # FakeCtx has no set_voice_enabled
    finally:
        win._finished = True
        win.deleteLater()


def test_no_voice_footnote_text(make, app):
    win, ctx, cfg, _ = make(None)
    assert _footnote(win.voice_card) == VOICE_OFF


class VoiceCtx(FakeCtx):
    """Has set_voice_enabled; ctx.cfg is the live config, as on main.Context."""

    def __init__(self, cfg):
        super().__init__(None)
        self.cfg = cfg
        self.calls = []

    def set_voice_enabled(self, on):
        self.calls.append(on)
        self.cfg.voice_enabled = bool(on)
        self.voice = FakeVoice()


def test_turn_on_voice_button_calls_ctx_and_enables_mics(app):
    from ui.onboarding_window import OnboardingWindow
    cfg = Config()
    cfg.voice_enabled = False
    ctx = VoiceCtx(cfg)
    win = OnboardingWindow(ctx, cfg)
    try:
        win._go(3, speak=False)
        assert win.btn_voice_on.isVisibleTo(win) and not win.voice_mic.isVisibleTo(win)
        assert _footnote(win.voice_card) == VOICE_OFF
        win.btn_voice_on.clicked.emit()
        assert ctx.calls == [True]
        assert not win.voice_mic.isHidden() and not win.name_mic.isHidden() and not win.apps_mic.isHidden()
        assert not win.btn_voice_on.isVisibleTo(win)
        assert _footnote(win.voice_card) != VOICE_OFF
        assert win._voice_ok()
    finally:
        win._finished = True
        win.deleteLater()


def test_turn_on_voice_polls_until_voice_ready(app):
    from ui.onboarding_window import OnboardingWindow
    cfg = Config()
    cfg.voice_enabled = False
    ctx = VoiceCtx(cfg)
    ctx.set_voice_enabled = lambda on: (ctx.calls.append(on), setattr(cfg, "voice_enabled", on))
    win = OnboardingWindow(ctx, cfg)
    try:
        win._go(3, speak=False)
        win._turn_on_voice()
        assert win._voice_poll.isActive() and not win.btn_voice_on.isEnabled()
        ctx.voice = FakeVoice()                   # supervisor started Voice
        win._poll_voice_ready()
        assert not win._voice_poll.isActive() and win.voice_mic.isVisibleTo(win)
    finally:
        win._finished = True
        win._voice_poll.stop()
        win.deleteLater()


def _footnote(card):
    return card._footnote_label.text()


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
