"""Settings > Commands page: built from the catalog, filter capsules, search, and empty state (offscreen)."""
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from command_catalog import CATEGORIES, get_catalog
from config import Config
from ui.settings_window import SettingsWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


def _window(app):
    ctx = MagicMock()
    ctx.update_setting = MagicMock()
    return SettingsWindow(Config(), ctx)


def _visible_rows(dlg):
    return [r for r in dlg._cmd_rows if not r.isHidden()]


def test_page_builds_with_every_catalog_entry(app):
    dlg = _window(app)
    specs = get_catalog()
    assert len(dlg._cmd_rows) == len(specs)
    assert [r.spec.title for r in dlg._cmd_rows if r.category == "Media"] == \
        [s.title for s in specs if s.category == "Media"]
    assert set(dlg._cmd_caps) == {"All", *[c for c in CATEGORIES if any(s.category == c for s in specs)]}
    assert dlg._cmd_empty.isHidden()
    assert len(_visible_rows(dlg)) == len(specs)


def test_media_capsule_shows_only_media_rows(app):
    dlg = _window(app)
    dlg._on_cmd_filter("Media")
    vis = _visible_rows(dlg)
    assert vis and all(r.category == "Media" for r in vis)
    assert len(vis) == sum(1 for s in get_catalog() if s.category == "Media")
    assert dlg._cmd_empty.isHidden()
    # other category cards are hidden
    for cat, (hdr, card, rows) in dlg._cmd_groups.items():
        assert card.isHidden() == (cat != "Media")
    dlg._on_cmd_filter("All")
    assert len(_visible_rows(dlg)) == len(get_catalog())


def test_search_volume_filters_rows(app):
    dlg = _window(app)
    dlg._cmd_search._edit.setText("volume")
    vis = _visible_rows(dlg)
    assert 0 < len(vis) < len(get_catalog())
    assert all("volume" in r.haystack for r in vis)
    # hidden rows are exactly the non-matches
    assert all("volume" not in r.haystack for r in dlg._cmd_rows if r.isHidden())


def test_search_with_no_match_shows_empty_label(app):
    dlg = _window(app)
    dlg._cmd_search._edit.setText("zzzz-no-such-command")
    assert not _visible_rows(dlg)
    assert not dlg._cmd_empty.isHidden()
    assert dlg._cmd_empty.text() == "No matching commands"


def test_search_and_capsule_combine(app):
    dlg = _window(app)
    dlg._on_cmd_filter("Media")
    dlg._cmd_search._edit.setText("volume")
    vis = _visible_rows(dlg)
    assert vis and all(r.category == "Media" and "volume" in r.haystack for r in vis)


def test_catalog_import_failure_shows_empty_state(app):
    with patch.dict(sys.modules, {"command_catalog": None}):     # makes `from command_catalog import ...` raise
        dlg = _window(app)
    assert dlg._cmd_rows == []
    assert dlg._cmd_search is None
    from PySide6.QtWidgets import QLabel
    texts = [l.text() for l in dlg._cmd_page.findChildren(QLabel)]
    assert "Command list unavailable" in texts
    # filtering with no groups is a safe no-op
    dlg._apply_cmd_filter()
