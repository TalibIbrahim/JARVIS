"""Tests for the DwellClicker engine. Run: python tests/test_dwell.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config import Config
from dwell import DwellClicker


def test_dwell_click_triggers_after_dwell_time():
    cfg = Config()
    cfg.dwell_mode = "click"
    cfg.dwell_time = 1.0
    cfg.dwell_radius = 12.0
    d = DwellClicker(cfg)

    # First update sets anchor
    r = d.update(500, 500, 0.0)
    assert r["action"] is None
    assert r["progress"] == 0.0

    # Stay within radius for 0.5s -> progress ~0.5
    r = d.update(502, 501, 0.5)
    assert r["action"] is None
    assert 0.45 <= r["progress"] <= 0.55

    # At 1.0s -> trigger click!
    r = d.update(501, 503, 1.0)
    assert r["action"] == "click"
    assert r["latched"] is True

    # Next frame at same location -> no repeated click!
    r = d.update(501, 503, 1.1)
    assert r["action"] is None


def test_dwell_resets_when_cursor_moves_outside_radius():
    cfg = Config()
    cfg.dwell_mode = "click"
    cfg.dwell_time = 1.0
    cfg.dwell_radius = 12.0
    d = DwellClicker(cfg)

    d.update(500, 500, 0.0)
    d.update(502, 502, 0.6)  # 60% progress

    # Move outside radius (dist = 30 px > 12 px)
    r = d.update(530, 500, 0.7)
    assert r["action"] is None
    assert r["progress"] == 0.0

    # At 1.2s (only 0.5s at new spot) -> should not trigger
    r = d.update(531, 501, 1.2)
    assert r["action"] is None
    assert 0.45 <= r["progress"] <= 0.55

    # At 1.7s (1.0s at new spot) -> trigger click
    r = d.update(531, 501, 1.7)
    assert r["action"] == "click"


def test_dwell_unlatches_after_moving_away():
    cfg = Config()
    cfg.dwell_mode = "click"
    cfg.dwell_time = 1.0
    cfg.dwell_radius = 12.0
    d = DwellClicker(cfg)

    d.update(100, 100, 0.0)
    r = d.update(100, 100, 1.0)
    assert r["action"] == "click"
    assert d.latched is True

    # Move far away -> unlatch
    d.update(200, 200, 1.1)
    assert d.latched is False

    # Dwell at new location for 1.0s -> second click triggers
    r = d.update(200, 200, 2.1)
    assert r["action"] == "click"


def test_dwell_pauses_during_pinch_or_drag_or_standby():
    cfg = Config()
    cfg.dwell_mode = "click"
    cfg.dwell_time = 1.0
    d = DwellClicker(cfg)

    d.update(500, 500, 0.0)
    d.update(500, 500, 0.6)

    # Standby
    r = d.update(500, 500, 0.7, active=False)
    assert r["progress"] == 0.0
    assert r["action"] is None

    # Pinch active
    d.update(500, 500, 0.8, active=True)
    r = d.update(500, 500, 1.2, pinching=True)
    assert r["progress"] == 0.0
    assert r["action"] is None

    # Dragging active
    r = d.update(500, 500, 1.5, dragging=True)
    assert r["progress"] == 0.0
    assert r["action"] is None


def test_dwell_radial_mode():
    cfg = Config()
    cfg.dwell_mode = "radial"
    cfg.dwell_time = 1.0
    cfg.dwell_radius = 12.0
    d = DwellClicker(cfg)

    d.update(500, 500, 0.0)
    # Dwell for 1.0s opens radial menu
    r = d.update(500, 500, 1.0)
    assert r["radial_open"] is True
    assert r["action"] is None

    # Move cursor up towards top slice (Click: angle ~0 rad)
    # Target center is (500, 500), move to (500, 460) -> distance 40 px Up
    r = d.update(500, 460, 1.1)
    assert r["radial_hover"] == "click"

    # Hover for 0.45s -> select action!
    r = d.update(500, 460, 1.6)
    assert r["action"] == "click"
    assert d.radial_open is False


import unittest


class TestDwell(unittest.TestCase):
    def test_dwell_click_triggers_after_dwell_time(self):
        test_dwell_click_triggers_after_dwell_time()

    def test_dwell_resets_when_cursor_moves_outside_radius(self):
        test_dwell_resets_when_cursor_moves_outside_radius()

    def test_dwell_unlatches_after_moving_away(self):
        test_dwell_unlatches_after_moving_away()

    def test_dwell_pauses_during_pinch_or_drag_or_standby(self):
        test_dwell_pauses_during_pinch_or_drag_or_standby()

    def test_dwell_radial_mode(self):
        test_dwell_radial_mode()


if __name__ == "__main__":
    unittest.main()

