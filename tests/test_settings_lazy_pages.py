"""SettingsWindow lazy page building (offscreen, no devices, no network)."""
import sys
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from config import Config
from ui import settings_window as sw
from ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def dlg(app):
    with patch("ui.settings_window.list_cameras", return_value=[{"index": 0, "name": "cam"}]):
        d = SettingsWindow(Config(), MagicMock())
        yield d
        d._theme_timer.stop()
        d.close()
        d.deleteLater()


def _built(d):
    return [c for c in sw._CATEGORIES if c not in d._pending_pages]


def test_only_general_built_after_construction(dlg):
    assert _built(dlg) == ["General"]
    assert len(dlg._pending_pages) == len(sw._CATEGORIES) - 1


def test_show_and_backdrop_build_nothing_more(dlg, app):
    dlg.show()
    dlg._apply_backdrop()
    app.processEvents()
    assert _built(dlg) == ["General"]


def test_select_tab_builds_exactly_one_more(dlg):
    dlg.select_tab("display")
    assert sorted(_built(dlg)) == sorted(["General", "Display"])


def test_ensure_all_pages_builds_all(dlg):
    dlg.ensure_all_pages()
    assert dlg._pending_pages == []
    assert dlg.build_next_pending_page() is False


def test_build_next_pending_page_builds_one_at_a_time(dlg):
    before = len(dlg._pending_pages)
    assert dlg.build_next_pending_page() is True
    assert len(dlg._pending_pages) == before - 1


def test_sidebar_search_then_navigation_builds_page(dlg):
    dlg._sidebar._search._edit.setText("voice")
    assert any(r["id"] == "Voice" and r["visible"] for r in dlg._sidebar._rows)
    assert "Voice" in dlg._pending_pages
    dlg._sidebar.select("Voice")
    assert "Voice" not in dlg._pending_pages
    assert len(_built(dlg)) == 2


def test_command_filter_on_unbuilt_page_builds_it(dlg):
    assert "Commands" in dlg._pending_pages
    dlg._on_cmd_filter("All")
    assert "Commands" not in dlg._pending_pages
    assert dlg._cmd_rows


def test_no_getattr_or_setattr_override():
    for name in ("__getattr__", "__setattr__", "__getattribute__"):
        assert name not in SettingsWindow.__dict__, name
        for base in SettingsWindow.__mro__:
            if base.__module__.startswith("ui."):
                assert name not in base.__dict__, (base, name)
