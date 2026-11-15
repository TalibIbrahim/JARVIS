"""Tray 'Hide Jarvis' toggle; Settings island-style / voice picker / Privacy & Learning (offscreen, fakes)."""
import sys
import types
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from config import Config


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


# ── tray ─────────────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self):
        self.hidden = False
        self.calls = []

    def is_hidden(self):
        return self.hidden

    def set_hidden(self, on):
        self.calls.append(on)
        self.hidden = on


class _Mgr:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg = Config()
        self.updater = None
        self.island = None

    def quit_app(self):
        pass

    def open_settings(self):
        pass


def test_tray_hide_action_toggles_ctx_and_icon(app):
    from ui.tray import JarvisTray
    ctx = _Ctx()
    tray = JarvisTray(_Mgr(ctx), app)
    try:
        labels = [a.text() for a in tray.contextMenu().actions()]
        assert "Hide Jarvis  (Ctrl+Alt+H)" in labels
        assert labels.index("Hide Jarvis  (Ctrl+Alt+H)") > labels.index("Wake / Standby  (Ctrl+Alt+J)")
        act = tray._act_hide
        assert act.isCheckable() and not act.isChecked()
        normal_key = tray.icon().cacheKey()
        act.trigger()
        assert ctx.calls == [True] and act.isChecked()
        assert tray.icon().cacheKey() != normal_key           # dimmed + slashed icon
        act.trigger()
        assert ctx.calls == [True, False] and not act.isChecked()
        assert tray.icon().cacheKey() == normal_key
        ctx.hidden = True                                      # hotkey flipped it elsewhere
        tray._sync_hidden()
        assert act.isChecked()
    finally:
        tray.hide()


def test_hidden_icon_is_dimmer_than_normal(app):
    from ui.tray import _make_hidden_icon, _make_fallback_icon
    base = _make_fallback_icon(True)
    hid = _make_hidden_icon(base, True).pixmap(64, 64).toImage()
    nor = base.pixmap(64, 64).toImage()
    assert not hid.isNull() and hid != nor


# ── settings ─────────────────────────────────────────────────────────────────

class _Personal:
    def __init__(self):
        self.profile = {"name": "Talib", "uses": ["Gaming", "Work"]}
        self.corrections = [{"heard": "jar vis", "meant": "Jarvis"}]
        self.calls = []

    def get_profile(self):
        return dict(self.profile)

    def update_profile(self, **f):
        self.calls.append(("update", f))
        self.profile.update(f)

    def list_corrections(self):
        return list(self.corrections)

    def remove_correction(self, h):
        self.calls.append(("remove", h))
        self.corrections = [c for c in self.corrections if c["heard"] != h]

    def clip_stats(self):
        return {"count": 3, "mb": 1.25}

    def delete_voice_clips(self):
        self.calls.append(("clips",))

    def habits_summary(self):
        return "Opens Spotify at night"

    def clear_habits(self):
        self.calls.append(("habits",))

    def data_summary(self):
        return "1 profile, 1 correction"

    def delete_all(self):
        self.calls.append(("delete_all",))


class _SCtx:
    def __init__(self):
        self.personal = _Personal()
        self.surface = []
        self.onboarded = 0
        self.said = []

    def set_island_surface(self, s):
        self.surface.append(s)

    def start_onboarding(self):
        self.onboarded += 1

    def say(self, t):
        self.said.append(t)


@pytest.fixture
def saves(monkeypatch):
    rec = []
    from ui import settings_window as sw
    monkeypatch.setattr(sw.SettingsWindow, "_save", lambda self, k, v: (rec.append((k, v)), setattr(self.cfg, k, v)))
    return rec


@pytest.fixture
def dlg(app, saves):
    from ui.settings_window import SettingsWindow
    ctx = _SCtx()
    with patch("ui.settings_window.list_cameras", return_value=[{"index": 0, "name": "cam"}]):
        d = SettingsWindow(Config(), ctx)
        d.ensure_all_pages()
        yield d, ctx
        d._theme_timer.stop()
        d.close()
        d.deleteLater()


def test_island_style_calls_ctx(dlg, saves):
    d, ctx = dlg
    assert [o[0] for o in d.island_surface_control.options] == ["gradient", "near_black", "black", "white"]
    d.island_surface_control.setValue("black")
    assert ctx.surface == ["black"] and d.cfg.island_surface == "black"
    assert saves == []                                         # ctx path owns persistence


def test_island_style_falls_back_to_save_without_ctx(app, saves):
    from ui.settings_window import SettingsWindow
    with patch("ui.settings_window.list_cameras", return_value=[]):
        d = SettingsWindow(Config(), None)
        d.ensure_all_pages()
        d.island_surface_control.setValue("white")
        assert ("island_surface", "white") in saves
        d._theme_timer.stop()
        d.close()


def _fake_voice_models(installed, downloads):
    m = types.ModuleType("voice_models")
    state = {"installed": installed}
    m.available_voices = lambda: [
        {"id": "system", "name": "System", "size_mb": 0, "installed": True, "engine": "system"},
        {"id": "jarvis", "name": "J.A.R.V.I.S.", "size_mb": 63, "installed": state["installed"], "engine": "piper"},
    ]

    def _dl(vid, cb):
        cb(0.5)
        downloads.append(vid)
        state["installed"] = True
        return True
    m.download = _dl
    m.remove = lambda vid: state.update(installed=False)
    return m


def test_voice_picker_sets_tts_engine(dlg, saves):
    d, ctx = dlg
    d.tts_seg.setValue("system")
    d.tts_seg.setValue("piper")
    assert ("tts_engine", "system") in saves and ("tts_engine", "piper") in saves


def test_voice_download_runs_on_worker_and_updates_ui(app, saves, monkeypatch):
    from ui.settings_window import SettingsWindow
    downloads = []
    monkeypatch.setitem(sys.modules, "voice_models", _fake_voice_models(False, downloads))
    with patch("ui.settings_window.list_cameras", return_value=[]):
        d = SettingsWindow(Config(), _SCtx())
        d.ensure_all_pages()
        assert (not d.btn_voice_dl.isHidden()) and not (not d.btn_voice_rm.isHidden())
        d._on_voice_dl_clicked()
        d._voice_dl_thread.join(5)
        app.processEvents()
        assert downloads == ["jarvis"]
        assert (not d.btn_voice_rm.isHidden()) and not (not d.btn_voice_dl.isHidden())
        d._on_voice_remove()
        assert (not d.btn_voice_dl.isHidden())
        d._theme_timer.stop()
        d.close()


def test_privacy_page_toggles_save(dlg, saves):
    d, ctx = dlg
    d.priv_toggles["learn_habits"].setChecked(False)
    d.priv_toggles["learn_voice_clips"].setChecked(False)
    assert ("learn_habits", False) in saves and ("learn_voice_clips", False) in saves
    assert set(d.priv_toggles) == {"learn_profile", "learn_corrections", "learn_voice_clips", "learn_habits"}


def test_privacy_profile_edit_corrections_clips_habits(dlg):
    d, ctx = dlg
    p = ctx.personal
    assert set(d.priv_profile_edits) == {"name", "uses"}
    d.priv_profile_edits["name"].setText("Tal")
    d.priv_profile_edits["name"].editingFinished.emit()
    d.priv_profile_edits["uses"].setText("Gaming, Study")
    d.priv_profile_edits["uses"].editingFinished.emit()
    assert ("update", {"name": "Tal"}) in p.calls
    assert ("update", {"uses": ["Gaming", "Study"]}) in p.calls
    assert len(d.priv_correction_buttons) == 1
    d.priv_correction_buttons[0].clicked.emit()
    assert ("remove", "jar vis") in p.calls and d.priv_correction_buttons == []
    d.btn_priv_clips.clicked.emit()
    d.btn_priv_habits.clicked.emit()
    assert ("clips",) in p.calls and ("habits",) in p.calls
    assert d._fmt_clip_stats({"count": 3, "mb": 1.5}) == "3 clips · 1.5 MB"
    assert d._fmt_clip_stats({"count": 1, "bytes": 0}) == "1 clip · 0.0 MB"


def test_redo_onboarding_and_delete_needs_confirmation(dlg):
    d, ctx = dlg
    d.btn_priv_redo.clicked.emit()
    assert ctx.onboarded == 1
    d.btn_priv_delete.clicked.emit()                           # first click only asks
    assert ("delete_all",) not in ctx.personal.calls
    assert d.btn_priv_delete_confirm is not None
    d.btn_priv_delete_cancel.clicked.emit()
    assert ("delete_all",) not in ctx.personal.calls and d.btn_priv_delete is not None
    d.btn_priv_delete.clicked.emit()
    d.btn_priv_delete_confirm.clicked.emit()
    assert ("delete_all",) in ctx.personal.calls and d.btn_priv_delete is not None


def test_privacy_page_without_personal_module(app, saves):
    from ui.settings_window import SettingsWindow
    with patch("ui.settings_window.list_cameras", return_value=[]):
        d = SettingsWindow(Config(), None)
        d.select_tab("Privacy")
        assert d.priv_profile_edits == {}
        d._theme_timer.stop()
        d.close()
