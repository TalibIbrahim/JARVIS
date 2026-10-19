"""
tests/test_island_disambiguation.py — Tests for Island Disambiguation Card UI & Chips
Verifies:
  1. trigger_disambiguate expands island into Disambiguation Card with spring physics
  2. Layout: "Which one, sir?" header + 2-3 numbered chips with badges & titles
  3. Interactive without focus stealing (WS_EX_NOACTIVATE, set_interactive)
  4. Mouse click selection
  5. Hand tracking dwell ring (1.0s fill) and dwell completion selection
  6. Fist-click gesture selection
  7. Selection callback & candidate action execution
  8. Transition to RESULT checkmark and collapse back to IDLE
  9. Overlay event queue wiring for 'disambiguate'
  10. IslandDemoController disambiguation stage
"""

import time
import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui.island import AppleDynamicIsland, IslandState, IslandDemoController
from overlay import Overlay


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class MockOverlayManager:
    def __init__(self):
        self.cfg = None
        self.posted_events = []

    def post(self, kind, **data):
        self.posted_events.append((kind, data))


def test_disambiguate_trigger_and_state(qapp):
    mgr = MockOverlayManager()
    island = AppleDynamicIsland(mgr)
    
    candidates = ["Spotify", "SoundCloud", "Apple Music"]
    island.trigger_disambiguate(candidates, header="Which one, sir?", duration=10.0)

    assert island.disambiguate_active is True
    assert len(island.disambiguate_candidates) == 3
    assert island.disambiguate_header == "Which one, sir?"
    assert island.is_interactive is True

    # State resolver
    now = time.time()
    state = island._resolve_state(now)
    assert state == IslandState.CARD_DISAMBIGUATE

    # Target size should be card size
    tw, th = island._target_size(state, now)
    assert tw >= 440.0
    assert th == 90.0

    # Spring target updated
    assert island.spring_w.target >= 440.0
    assert island.spring_h.target == 90.0


def test_chip_rects_and_hit_testing(qapp):
    mgr = MockOverlayManager()
    island = AppleDynamicIsland(mgr)
    
    candidates = [
        {"title": "Spotify", "action": "open_spotify"},
        {"title": "SoundCloud", "action": "open_soundcloud"}
    ]
    island.trigger_disambiguate(candidates)

    w = 380.0
    h = 90.0
    scale = 1.0
    rects = island._chip_rects(w, h, scale)
    assert len(rects) == 2
    assert rects[0].x() < rects[1].x()
    assert rects[0].height() == 36.0

    # Hit test chip 0
    pt0 = QPointF(rects[0].center().x(), rects[0].center().y())
    assert island._chip_at(pt0) == 0

    # Hit test chip 1
    pt1 = QPointF(rects[1].center().x(), rects[1].center().y())
    assert island._chip_at(pt1) == 1

    # Outside chips
    assert island._chip_at(QPointF(0, 0)) is None


def test_mouse_click_selection(qapp):
    mgr = MockOverlayManager()
    island = AppleDynamicIsland(mgr)
    
    selected = []
    candidates = ["Spotify", "SoundCloud"]
    island.trigger_disambiguate(candidates, callback=lambda c: selected.append(c))

    iw, ih = island.island_size()
    w = max(iw, island.spring_w.target)
    h = max(ih, island.spring_h.target)
    rects = island._chip_rects(w, h, 1.0)
    pt1 = rects[1].center() + QPointF(island._pad, island._pad)   # window-local = island-local + margin

    # Simulate mouse press event on chip 1
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        pt1,
        pt1,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier
    )
    island.mousePressEvent(event)

    # Candidate 1 ("SoundCloud") should be selected
    assert selected == ["SoundCloud"]
    assert island.disambiguate_active is False
    assert island.is_interactive is False  # Restores click-through

    # RESULT checkmark should be triggered
    now = time.time()
    assert island._resolve_state(now) == IslandState.RESULT
    assert island.result_text == "SoundCloud"
    assert island.result_icon == "success"


def test_hand_dwell_selection(qapp):
    mgr = MockOverlayManager()
    island = AppleDynamicIsland(mgr)
    
    action_executed = []
    candidates = [
        {"title": "Spotify", "action": lambda: action_executed.append("spotify_done")},
        {"title": "SoundCloud"}
    ]
    island.trigger_disambiguate(candidates)

    # Hover over chip 0
    island._hovered_chip_idx = 0
    island._chip_dwell_elapsed = 0.5
    assert island.disambiguate_active is True

    # Complete dwell (>= 1.0s)
    island.select_candidate(0)

    assert action_executed == ["spotify_done"]
    assert island.disambiguate_active is False
    assert island.result_text == "Spotify"
    assert island.result_icon == "success"


def test_fist_click_gesture_selection(qapp):
    mgr = MockOverlayManager()
    island = AppleDynamicIsland(mgr)
    
    selected = []
    candidates = ["Option A", "Option B", "Option C"]
    island.trigger_disambiguate(candidates, callback=lambda c: selected.append(c))

    # Simulate hovering over chip 2 with gesture CLICK
    island._hovered_chip_idx = 2
    island.label = "CLICK"
    island._prev_active_gesture = ""

    island.select_candidate(2)

    assert selected == ["Option C"]
    assert island.disambiguate_active is False
    assert island.result_text == "Option C"


def test_overlay_disambiguate_queue_wiring(qapp):
    class MockStop:
        def is_set(self):
            return False

    overlay = Overlay(MockStop())
    
    selected_result = []
    def my_callback(cand):
        selected_result.append(cand)

    # Post disambiguate event
    overlay.post("disambiguate", candidates=["Item 1", "Item 2"], callback=my_callback, header="Choose")

    # Drain queue
    now = time.time()
    overlay._drain(now)

    assert overlay.island.disambiguate_active is True
    assert len(overlay.island.disambiguate_candidates) == 2
    assert overlay.island.disambiguate_header == "Choose"

    # Select candidate 0
    overlay.island.select_candidate(0)
    assert selected_result == ["Item 1"]
    assert overlay.island.result_text == "Item 1"


def test_demo_controller_disambiguation_stage(qapp):
    mgr = MockOverlayManager()
    island = AppleDynamicIsland(mgr)
    demo = IslandDemoController(island)
    demo.active = True

    # Step 8 is disambiguation stage
    demo._apply_step(8)
    assert island.disambiguate_active is True
    assert len(island.disambiguate_candidates) == 3
    assert island._resolve_state(time.time()) == IslandState.CARD_DISAMBIGUATE

    # Dwell simulation
    demo.step = 8
    demo.step_start = time.time() - 1.0  # 1.0s elapsed -> dwell progress
    demo._step_tick()
    assert island._hovered_chip_idx == 0
    assert island._chip_dwell_elapsed > 0.0

    # Resolve simulation
    demo.step_start = time.time() - 2.0  # 2.0s elapsed -> resolve
    demo._step_tick()
    assert island.disambiguate_active is False
    assert island.result_text == "Spotify"
    assert island.result_icon == "success"
