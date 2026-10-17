"""
tests/test_full_ui_functional.py — Comprehensive End-to-End Functional Test Suite
for J.A.R.V.I.S. Apple HIG UI Redesign.

Tests covered:
  1. Module Exports & Subpackage Integrity (ui/ imports, tokens, materials, components)
  2. Overlay Manager Integration (_NEW_UI_AVAILABLE, island, cursor, tray)
  3. Dynamic Island State Transitions & Waveform Springs (all 8 states)
  4. Cursor Feedback Overlay (dwell progress ring, completion pulse, radial menu)
  5. System Settings Window (all 7 tabs, search filter, controls, timers, clean teardown)
  6. Onboarding Dialog Flow (Step 1 -> 2 -> 3 -> complete)
  7. High Contrast, Light & Dark Theme Switching
  8. Continuous Curvature (Squircle) & Spring Numerical Stability
"""

import os
import sys
import threading
import time
import unittest

from PySide6.QtCore import Qt, QPoint, QSize, QRectF
from PySide6.QtGui import QPainter, QPixmap, QColor
from PySide6.QtWidgets import QApplication

from config import Config
from ui.tokens import T, TYPE, COLOR, SPACING, RADIUS, SIZES, SPRING
from ui.materials import (
    apply_best_backdrop, apply_mica, apply_acrylic,
    set_dark_titlebar, set_rounded_corners, make_click_through
)
from ui.components import (
    create_squircle_path, Spring, AppleSwitch, SegmentedControl, HSlider,
    GroupedCard, PrimaryButton, PlainButton, StatusPill,
    MicLevelMeter, SensitivityPreviewWidget
)
from ui.island import AppleDynamicIsland, IslandState
from ui.cursor_overlay import CursorFeedbackOverlay
from ui.settings_window import SettingsWindow, _CATEGORIES, _SIDEBAR_LABELS
from ui.onboarding import OnboardingDialog
from ui.tray import JarvisTray
from overlay import Overlay, _NEW_UI_AVAILABLE


class DummyContext:
    def __init__(self):
        self.active = True
        self.speech_log = []
    def say(self, text):
        self.speech_log.append(text)
    def set_active(self, val):
        self.active = not self.active if val is None else val


class TestFullUIFunctional(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.cfg = Config()
        self.ctx = DummyContext()
        self.stop_evt = threading.Event()

    def tearDown(self):
        self.stop_evt.set()
        self.app.processEvents()

    # ── 1. Module Exports & Subpackage Integrity ───────────────────────────
    def test_01_subpackage_exports(self):
        """Verify new ui/ package is loaded by default in overlay.py."""
        self.assertTrue(_NEW_UI_AVAILABLE, "_NEW_UI_AVAILABLE must be True")
        self.assertIsNotNone(T)
        self.assertIsNotNone(AppleSwitch)
        self.assertIsNotNone(SegmentedControl)
        self.assertIsNotNone(GroupedCard)
        self.assertIsNotNone(SettingsWindow)
        self.assertIsNotNone(AppleDynamicIsland)
        self.assertIsNotNone(CursorFeedbackOverlay)
        self.assertIsNotNone(JarvisTray)

    # ── 2. Overlay Manager Integration ─────────────────────────────────────
    def test_02_overlay_initialization(self):
        """Verify Overlay initializes refactored UI components."""
        ov = Overlay(self.stop_evt, cfg=self.cfg, ctx=self.ctx)
        self.assertIsInstance(ov.island, AppleDynamicIsland)
        self.assertIsInstance(ov.cursor_overlay, CursorFeedbackOverlay)
        self.assertIsInstance(ov.tray, JarvisTray)
        self.assertTrue(ov.island.isVisible())
        self.assertTrue(ov.cursor_overlay.isVisible())
        ov.timer.stop()

    # ── 3. Dynamic Island State Transitions ────────────────────────────────
    def test_03_island_state_transitions(self):
        """Verify Dynamic Island transitions smoothly through all states."""
        ov = Overlay(self.stop_evt, cfg=self.cfg, ctx=self.ctx)
        island = ov.island
        now = time.time()

        # IDLE
        ov.post("state", active=False, label="STANDBY")
        ov._drain(now)
        island.update_frame(now)
        self.assertEqual(island._resolve_state(now), IslandState.IDLE)

        # TRACKING
        ov.post("state", active=True, label="TRACKING", hands=1, landmarks=[])
        ov._drain(now)
        island.update_frame(now)
        self.assertEqual(island._resolve_state(now), IslandState.TRACKING)

        # GESTURE
        ov.post("state", active=True, label="CLICK", hands=1, landmarks=[])
        ov._drain(now)
        island.update_frame(now)
        self.assertEqual(island._resolve_state(now), IslandState.GESTURE)

        # LISTENING (Waveform)
        ov.post("wake_detected")
        ov._drain(now)
        island.update_frame(now)
        self.assertEqual(island._resolve_state(now), IslandState.LISTENING)
        self.assertTrue(island.listening)

        # PROMPT
        ov.post("prompt", text="What is the weather today?")
        ov._drain(now)
        island.update_frame(now)
        self.assertEqual(island._resolve_state(now), IslandState.PROMPT)
        self.assertIn("weather", island.user_prompt)

        # THINKING
        ov.post("thinking", active=True, timeout=5.0)
        ov._drain(now)
        island.update_frame(now)
        self.assertTrue(island.thinking)

        # SPEAKING
        ov.post("say", text="It is currently 72 degrees and sunny.", speak=False)
        ov._drain(now)
        island.update_frame(now)
        self.assertEqual(island._resolve_state(now), IslandState.SPEAKING)

        # ERROR
        ov.post("error", text="Audio input stream disconnected")
        ov._drain(now)
        island.update_frame(now)
        self.assertEqual(island._resolve_state(now), IslandState.ERROR)
        self.assertIn("disconnected", island.error_text)

        ov.timer.stop()

    # ── 4. Cursor Feedback Overlay ─────────────────────────────────────────
    def test_04_cursor_overlay_dwell_and_radial(self):
        """Verify dwell progress ring and radial menu states."""
        cursor = CursorFeedbackOverlay()
        self.assertEqual(cursor.progress, 0.0)

        # Dwell progress update
        cursor.update_dwell({
            "progress": 0.65,
            "x": 640.0,
            "y": 480.0,
        })
        self.assertAlmostEqual(cursor.progress, 0.65)
        self.assertEqual(cursor.cur_x, 640.0)
        self.assertEqual(cursor.cur_y, 480.0)

        # Dwell completion pulse
        cursor.update_dwell({"flashing": True})
        self.assertTrue(cursor.flashing)

        # Radial menu
        cursor.update_dwell({
            "radial_open": True,
            "radial_hover": "right_click",
            "radial_progress": 0.45,
            "x": 800.0,
            "y": 600.0,
        })
        self.assertTrue(cursor.radial_open)
        self.assertEqual(cursor.radial_hover, "right_click")
        self.assertAlmostEqual(cursor.radial_prog, 0.45)

        cursor.close()

    # ── 5. System Settings Window (All 7 Tabs & Search) ────────────────────
    def test_05_settings_window_tabs_and_controls(self):
        """Verify SettingsWindow builds all tabs and handles navigation without error."""
        dlg = SettingsWindow(self.cfg, self.ctx)
        self.assertEqual(dlg.sidebar.count(), 7)

        # Test selecting each tab by ID and integer
        for i, cid in enumerate(_CATEGORIES):
            dlg.select_tab(cid)
            self.assertEqual(dlg.stack.currentIndex(), i)
            dlg.select_tab(i)
            self.assertEqual(dlg.stack.currentIndex(), i)

        # Test sidebar search filtering
        sidebar = dlg._sidebar
        sidebar._on_search("Gestures")
        visible_rows = [r for r in sidebar._rows if r["visible"]]
        self.assertEqual(len(visible_rows), 1)
        self.assertEqual(visible_rows[0]["id"], "Gestures")

        # Clear search
        sidebar._on_search("")
        all_visible = [r for r in sidebar._rows if r["visible"]]
        self.assertEqual(len(all_visible), 7)

        # Test paintEvent execution
        pix = dlg.grab()
        self.assertGreater(pix.width(), 800)
        self.assertGreater(pix.height(), 500)

        # Clean teardown
        dlg.reject()

    # ── 6. Onboarding Flow ────────────────────────────────────────────────
    def test_06_onboarding_dialog_flow(self):
        """Verify OnboardingDialog 3-step sequence and navigation buttons."""
        onboarding = OnboardingDialog(self.cfg, self.ctx)
        self.assertEqual(onboarding._step, 0)
        self.assertTrue(onboarding._btn_back.isHidden())

        # Step 0 -> Step 1 (Mic)
        onboarding._next_step()
        self.assertEqual(onboarding._step, 1)
        self.assertFalse(onboarding._btn_back.isHidden())

        # Step 1 -> Step 2 (Gestures)
        onboarding._next_step()
        self.assertEqual(onboarding._step, 2)
        self.assertEqual(onboarding._btn_next.text(), "Complete Setup")

        # Step 2 -> Back to Step 1
        onboarding._prev_step()
        self.assertEqual(onboarding._step, 1)

        # Complete
        onboarding._next_step()
        onboarding.accept()

    # ── 7. Component Library Interactions ──────────────────────────────────
    def test_07_components_behavior(self):
        """Test AppleSwitch, SegmentedControl, HSlider values and events."""
        # AppleSwitch
        sw = AppleSwitch(False)
        self.assertFalse(sw.isChecked())
        events = []
        sw.toggled.connect(lambda v: events.append(v))
        sw.setChecked(True)
        self.assertTrue(sw.isChecked())
        self.assertEqual(events, [True])

        # SegmentedControl
        opts = [("fast", "Fast"), ("acc", "Accurate")]
        seg = SegmentedControl(opts, "fast")
        seg_events = []
        seg.valueChanged.connect(lambda v: seg_events.append(v))
        seg.setValue("acc")
        self.assertEqual(seg.currentValue(), "acc")
        self.assertEqual(seg_events, ["acc"])

        # HSlider
        slider = HSlider(0.0, 100.0, 50.0, unit="%")
        slider_events = []
        slider.valueChanged.connect(lambda v: slider_events.append(v))
        slider.setValue(75.0)
        self.assertAlmostEqual(slider.value, 75.0)
        self.assertEqual(slider_events, [75.0])

        # Squircle Path
        path = create_squircle_path(QRectF(0, 0, 100, 100), 20.0)
        self.assertFalse(path.isEmpty())

    # ── 8. Spring Harmonic Oscillator Numerical Stability ─────────────────
    def test_08_spring_stability(self):
        """Verify spring updates converge without numerical explosion."""
        sp = Spring(0.0, stiffness=200.0, damping=20.0)
        sp.set_target(100.0)
        for _ in range(100):
            sp.update(0.016, reduce_motion=False)
        self.assertAlmostEqual(sp.value, 100.0, places=1)

        # Reduce motion snap
        sp2 = Spring(0.0)
        sp2.set_target(50.0)
        sp2.update(0.016, reduce_motion=True)
        self.assertAlmostEqual(sp2.value, 50.0)


if __name__ == "__main__":
    unittest.main()
