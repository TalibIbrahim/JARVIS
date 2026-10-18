"""
tests/test_visual_regression.py — J.A.R.V.I.S. Visual Regression Gate & Behavioral Verification.

Comprehensive Visual Regression and Behavior Verification Suite:
  1. Offscreen Visual Rendering (Zero Diff Tolerance) across 100% / 150% DPI and Dark / Light themes:
     - Settings Window: 7 tabs (General, Gestures, Pointer, Voice, AI, Display, About)
     - Dynamic Island: 11 base states (IDLE, WAKE, TRACKING, GESTURE, LISTENING, THINKING,
       PROMPT, SPEAKING, RESULT, NO_SPEECH, ERROR)
     - Dynamic Island Expanded Cards: 7 cards (CARD_MEDIA, CARD_WEATHER, CARD_TIME,
       CARD_DISAMBIGUATE, CARD_TIMER, CARD_SENDING, CARD_RESULT)
     - Dynamic Island HUDs: 2 HUDs (HUD_VOLUME, HUD_WINDOW)
     - Cursor Feedback Overlay: Dwell ring and 5-slice radial menu
     - Onboarding Dialog: Steps 0, 1, 2
     - System Tray Menu: Context menu flyout
  2. Animation Frame Sequences:
     - Spring harmonic oscillator physics for IDLE -> WAKE and IDLE -> LISTENING
     - Captures at fixed timestamps: t = 0.00, 0.05, 0.10, 0.15, 0.25, 0.40s
     - Mathematical verification of monotonic expansion, dynamic overshoot, and duration
  3. Non-Pixel Behaviors:
     - Window flags: FramelessWindowHint, Tool, WindowStaysOnTopHint
     - Win32 click-through: WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
     - Focus safety: WS_EX_NOACTIVATE preserved in idle and interactive modes
     - Taskbar icon suppression: Qt.WindowType.Tool and WS_EX_TOOLWINDOW
"""

from __future__ import annotations

import ctypes
import os
import sys
import unittest
from unittest.mock import patch
import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from config import Config
from ui.island import AppleDynamicIsland, IslandState, _SIZE
from ui.cursor_overlay import CursorFeedbackOverlay
from ui.settings_window import SettingsWindow, _CATEGORIES
from ui.onboarding import OnboardingDialog
from ui.tray import JarvisTray

from tools.visual_gate_helper import (
    get_app, set_theme, injected_clock, compare_images,
    render_settings_tab, render_island_view, render_cursor_view,
    render_onboarding_step, render_tray_menu, render_island_animation_frame,
    render_compact_pill, render_media_page, render_calendar_page,
    render_weather_page, render_hardware_grid_page, render_hardware_strip_page,
    render_jarvis_card, render_assistant_state,
    DummyOverlayMgr, ANIM_TIMESTAMPS, THEMES, DPIS,
    ISLAND_BASE_STATES, ISLAND_CARDS, ISLAND_HUDS, CURSOR_VIEWS, ONBOARDING_STEPS
)


GOLDENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goldens")
DIFFS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diffs")


def _assert_image_matches_golden(test_case: unittest.TestCase, actual_img: QImage, golden_filename: str):
    """
    Compares actual_img against tests/goldens/<golden_filename>.
    If GENERATE_GOLDENS=1 or --generate-goldens was set, generates/updates the golden.
    Otherwise asserts zero diffs and writes a diff image if mismatched.
    """
    golden_path = os.path.join(GOLDENS_DIR, golden_filename)
    should_gen = os.environ.get("GENERATE_GOLDENS") == "1" or "--generate-goldens" in sys.argv

    if should_gen or not os.path.exists(golden_path):
        os.makedirs(GOLDENS_DIR, exist_ok=True)
        actual_img.save(golden_path, "PNG")
        return

    golden_img = QImage(golden_path)
    diff_path = os.path.join(DIFFS_DIR, f"diff_{golden_filename}")
    identical, diff_count, diff_pct = compare_images(actual_img, golden_img, diff_save_path=diff_path)

    if not identical:
        test_case.fail(
            f"Visual regression detected in {golden_filename}!\n"
            f"  Diff pixels: {diff_count} ({diff_pct:.3f}%)\n"
            f"  Diff mask written to: {diff_path}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 1. Settings Window Visual Regression (7 tabs x 2 DPIs x 2 themes)
# ──────────────────────────────────────────────────────────────────────────────

class TestSettingsWindowVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_settings_tabs_visual(self):
        """Verify pixel-exact rendering for all 7 Settings tabs across themes and DPIs."""
        # The camera list is populated asynchronously (SettingsBridge.cameras_result); pin the probe
        # result and let render_settings_tab wait (bounded) for the placeholder to be replaced.
        fixed = [{"index": 0, "name": "HP HD Camera"}]
        with patch("ui.settings_window.list_cameras", return_value=fixed):
            for theme in THEMES:
                for dpi in DPIS:
                    dpi_str = f"dpi{int(dpi * 100)}"
                    for tab in _CATEGORIES:
                        fname = f"settings_{tab.lower()}_{theme}_{dpi_str}.png"
                        img = render_settings_tab(tab, dpi, theme)
                        _assert_image_matches_golden(self, img, fname)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Dynamic Island States Visual Regression (11 base states x 2 DPIs x 2 themes)
# ──────────────────────────────────────────────────────────────────────────────

class TestDynamicIslandStatesVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_dynamic_island_states_visual(self):
        """Verify all 11 base Island states offscreen with zero diff tolerance."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                for st in ISLAND_BASE_STATES:
                    fname = f"island_{st.lower()}_{theme}_{dpi_str}.png"
                    img = render_island_view(st, dpi, theme)
                    _assert_image_matches_golden(self, img, fname)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Dynamic Island Expanded Cards Visual Regression (7 cards x 2 DPIs x 2 themes)
# ──────────────────────────────────────────────────────────────────────────────

class TestDynamicIslandCardsVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_dynamic_island_expanded_cards_visual(self):
        """Verify all 7 expanded Island cards offscreen with zero diff tolerance."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                for card in ISLAND_CARDS:
                    fname = f"island_{card.lower()}_{theme}_{dpi_str}.png"
                    img = render_island_view(card, dpi, theme)
                    _assert_image_matches_golden(self, img, fname)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Dynamic Island HUDs Visual Regression (2 HUDs x 2 DPIs x 2 themes)
# ──────────────────────────────────────────────────────────────────────────────

class TestDynamicIslandHUDsVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_dynamic_island_huds_visual(self):
        """Verify Volume and Window gesture HUDs offscreen with zero diff tolerance."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                for hud in ISLAND_HUDS:
                    fname = f"island_{hud.lower()}_{theme}_{dpi_str}.png"
                    img = render_island_view(hud, dpi, theme)
                    _assert_image_matches_golden(self, img, fname)


# ──────────────────────────────────────────────────────────────────────────────
# 5. Cursor Feedback Overlay Visual Regression (2 views x 2 DPIs x 2 themes)
# ──────────────────────────────────────────────────────────────────────────────

class TestCursorOverlayVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_cursor_overlay_visual(self):
        """Verify Dwell Progress Ring and 5-slice Radial Menu offscreen."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                for cur in CURSOR_VIEWS:
                    fname = f"cursor_{cur}_{theme}_{dpi_str}.png"
                    img = render_cursor_view(cur, dpi, theme)
                    _assert_image_matches_golden(self, img, fname)


# ──────────────────────────────────────────────────────────────────────────────
# 6. Onboarding Dialog Visual Regression (3 steps x 2 DPIs x 2 themes)
# ──────────────────────────────────────────────────────────────────────────────

class TestOnboardingDialogVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_onboarding_dialog_visual(self):
        """Verify Onboarding Dialog Steps 0, 1, and 2 offscreen."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                for step in ONBOARDING_STEPS:
                    fname = f"onboarding_step_{step}_{theme}_{dpi_str}.png"
                    img = render_onboarding_step(step, dpi, theme)
                    _assert_image_matches_golden(self, img, fname)


# ──────────────────────────────────────────────────────────────────────────────
# 7. Tray Menu Visual Regression (1 menu x 2 DPIs x 2 themes)
# ──────────────────────────────────────────────────────────────────────────────

class TestTrayMenuVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_tray_menu_visual(self):
        """Verify System Tray Context Menu offscreen."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                fname = f"tray_menu_{theme}_{dpi_str}.png"
                img = render_tray_menu(dpi, theme)
                _assert_image_matches_golden(self, img, fname)


# ──────────────────────────────────────────────────────────────────────────────
# 8. Animation Frame Sequences & Spring Verification
# ──────────────────────────────────────────────────────────────────────────────

class TestIslandAnimationSequencesVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_animation_frame_sequences_visual(self):
        """Capture and verify island spring animations at fixed timestamps."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                for seq in ["idle_wake", "idle_listening"]:
                    for ts in ANIM_TIMESTAMPS:
                        ts_str = f"t{int(round(ts * 100)):03d}"
                        fname = f"anim_{seq}_{ts_str}_{theme}_{dpi_str}.png"
                        img = render_island_animation_frame(seq, ts, dpi, theme)
                        _assert_image_matches_golden(self, img, fname)

    def test_spring_curves_overshoot_and_duration(self):
        """Verify harmonic oscillator physics: progressive rise, overshoot, and settling."""
        t0 = 1000.0
        mgr = DummyOverlayMgr()

        def advance_to(island, target_t):
            curr = t0
            step = 0.01
            while curr < t0 + target_t - 1e-6:
                next_t = min(curr + step, t0 + target_t)
                island.update_frame(next_t)
                curr = next_t

        # 1. IDLE -> WAKE (target: 280 x 44)
        wake_island = AppleDynamicIsland(mgr)
        wake_island.t0 = t0
        wake_island.last_frame_t = t0
        wake_island.trigger_wake(duration=2.0)

        widths_wake = []
        for dt in ANIM_TIMESTAMPS:
            isl = AppleDynamicIsland(mgr)
            isl.t0 = t0
            isl.last_frame_t = t0
            isl.trigger_wake(duration=2.0)
            advance_to(isl, dt)
            widths_wake.append(isl.spring_w.value)

        # Monotonically increasing during initial rise up to peak
        self.assertLess(widths_wake[0], widths_wake[1])
        self.assertLess(widths_wake[1], widths_wake[2])
        self.assertLess(widths_wake[2], widths_wake[3])
        self.assertLess(widths_wake[3], widths_wake[4])

        # Overshoot peak at t=0.25s (> 280.0)
        self.assertGreater(widths_wake[4], 280.0, "Spring peak must overshoot target width 280.0")
        self.assertLess(widths_wake[4], 300.0, "Overshoot peak must remain bounded (<300)")

        # Settling back towards 280.0 at t=0.40s
        self.assertLess(widths_wake[5], widths_wake[4], "Spring must settle towards target after overshoot peak")
        self.assertGreater(widths_wake[5], 280.0)

        # 2. IDLE -> LISTENING (target: 390 x 54)
        widths_listening = []
        for dt in ANIM_TIMESTAMPS:
            isl = AppleDynamicIsland(mgr)
            isl.t0 = t0
            isl.last_frame_t = t0
            isl.listening = True
            isl.listening_until = t0 + 10.0
            advance_to(isl, dt)
            widths_listening.append(isl.spring_w.value)

        self.assertLess(widths_listening[0], widths_listening[1])
        self.assertLess(widths_listening[1], widths_listening[2])
        self.assertLess(widths_listening[2], widths_listening[3])
        self.assertLess(widths_listening[3], widths_listening[4])

        # Overshoot peak at t=0.25s (> 390.0)
        self.assertGreater(widths_listening[4], 390.0, "Spring peak must overshoot target width 390.0")
        self.assertLess(widths_listening[4], 415.0, "Overshoot peak must remain bounded (<415)")

        # Settling back towards 390.0 at t=0.40s
        self.assertLess(widths_listening[5], widths_listening[4], "Spring must settle towards target after overshoot peak")
        self.assertGreater(widths_listening[5], 390.0)


# ──────────────────────────────────────────────────────────────────────────────
# 9. Non-Pixel Behaviors Verification
# ──────────────────────────────────────────────────────────────────────────────

class TestNonPixelBehaviors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_window_flags(self):
        """Verify FramelessWindowHint, Tool, WindowStaysOnTopHint on overlay windows."""
        mgr = DummyOverlayMgr()
        island = AppleDynamicIsland(mgr)
        flags = island.windowFlags()

        self.assertTrue(bool(flags & Qt.WindowType.FramelessWindowHint))
        self.assertTrue(bool(flags & Qt.WindowType.Tool))
        self.assertTrue(bool(flags & Qt.WindowType.WindowStaysOnTopHint))
        self.assertTrue(bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus))
        self.assertTrue(island.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(island.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))

        cur = CursorFeedbackOverlay()
        cur_flags = cur.windowFlags()
        self.assertTrue(bool(cur_flags & Qt.WindowType.FramelessWindowHint))
        self.assertTrue(bool(cur_flags & Qt.WindowType.Tool))
        self.assertTrue(bool(cur_flags & Qt.WindowType.WindowStaysOnTopHint))
        self.assertTrue(bool(cur_flags & Qt.WindowType.WindowDoesNotAcceptFocus))

    @unittest.skipUnless(sys.platform == "win32", "Win32 extended style flags test")
    def test_win32_click_through_and_focus_safety(self):
        """Verify WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, and WS_EX_TOOLWINDOW styles."""
        mgr = DummyOverlayMgr()
        island = AppleDynamicIsland(mgr)
        island.make_click_through()

        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE  = 0x08000000
        WS_EX_TOOLWINDOW  = 0x00000080

        hwnd_island = int(island.winId())
        style_idle = user32.GetWindowLongW(hwnd_island, GWL_EXSTYLE)

        # In idle: must be click-through, focus-safe, and hidden from taskbar
        self.assertTrue(bool(style_idle & WS_EX_TRANSPARENT), "Island idle must have WS_EX_TRANSPARENT")
        self.assertTrue(bool(style_idle & WS_EX_NOACTIVATE), "Island idle must have WS_EX_NOACTIVATE")
        self.assertTrue(bool(style_idle & WS_EX_TOOLWINDOW), "Island idle must have WS_EX_TOOLWINDOW")

        # In interactive mode: removes transparency but strictly keeps NOACTIVATE
        island.set_interactive(True)
        style_interactive = user32.GetWindowLongW(hwnd_island, GWL_EXSTYLE)
        self.assertFalse(bool(style_interactive & WS_EX_TRANSPARENT), "Interactive island must not be click-through")
        self.assertTrue(bool(style_interactive & WS_EX_NOACTIVATE), "Interactive island MUST keep WS_EX_NOACTIVATE")
        self.assertTrue(bool(style_interactive & WS_EX_TOOLWINDOW), "Interactive island MUST keep WS_EX_TOOLWINDOW")

        # Cursor feedback overlay
        cur = CursorFeedbackOverlay()
        hwnd_cur = int(cur.winId())
        style_cur = user32.GetWindowLongW(hwnd_cur, GWL_EXSTYLE)
        self.assertTrue(bool(style_cur & WS_EX_TRANSPARENT), "Cursor overlay must have WS_EX_TRANSPARENT")
        self.assertTrue(bool(style_cur & WS_EX_NOACTIVATE), "Cursor overlay must have WS_EX_NOACTIVATE")
        self.assertTrue(bool(style_cur & WS_EX_TOOLWINDOW), "Cursor overlay must have WS_EX_TOOLWINDOW")

    def test_no_taskbar_icon_created(self):
        """Verify no taskbar icon created for overlays via Qt.WindowType.Tool."""
        mgr = DummyOverlayMgr()
        island = AppleDynamicIsland(mgr)
        cur = CursorFeedbackOverlay()

        self.assertTrue(bool(island.windowFlags() & Qt.WindowType.Tool))
        self.assertTrue(bool(cur.windowFlags() & Qt.WindowType.Tool))


# ──────────────────────────────────────────────────────────────────────────────
# 10. Dynamic Island Pages and Assistant States (Phase 0 Requirements)
# ──────────────────────────────────────────────────────────────────────────────

class TestDynamicIslandPagesAndAssistantStatesVisual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_compact_pill_rendering_and_visual(self):
        """Verify Compact pill (idle) renders at 100% and 150% DPI with zero diffs."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                img = render_compact_pill(dpi, theme)
                self.assertFalse(img.isNull())
                self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                _assert_image_matches_golden(self, img, f"island_idle_{theme}_{dpi_str}.png")

    def test_media_page_rendering_and_visual(self):
        """Verify Media page renders at 100% and 150% DPI with zero diffs."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                img = render_media_page(dpi, theme)
                self.assertFalse(img.isNull())
                self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                _assert_image_matches_golden(self, img, f"island_card_media_{theme}_{dpi_str}.png")

    def test_calendar_page_rendering_and_visual(self):
        """Verify Calendar page renders at 100% and 150% DPI with zero diffs."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                img = render_calendar_page(dpi, theme)
                self.assertFalse(img.isNull())
                self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                _assert_image_matches_golden(self, img, f"island_card_time_{theme}_{dpi_str}.png")

    def test_weather_page_rendering_and_visual(self):
        """Verify Weather page renders at 100% and 150% DPI with zero diffs."""
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                img = render_weather_page(dpi, theme)
                self.assertFalse(img.isNull())
                self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                _assert_image_matches_golden(self, img, f"island_card_weather_{theme}_{dpi_str}.png")

    def test_hardware_grid_page_rendering(self):
        """Verify Hardware Grid (2x3) page renders offscreen at 100% and 150% DPI."""
        for theme in THEMES:
            for dpi in DPIS:
                img = render_hardware_grid_page(dpi, theme)
                self.assertFalse(img.isNull())
                self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                expected_w = int(round(390.0 * dpi))
                expected_h = int(round(188.0 * dpi))
                self.assertEqual(img.width(), expected_w)
                self.assertEqual(img.height(), expected_h)

    def test_hardware_strip_page_rendering(self):
        """Verify Hardware Strip page renders offscreen at 100% and 150% DPI."""
        for theme in THEMES:
            for dpi in DPIS:
                img = render_hardware_strip_page(dpi, theme)
                self.assertFalse(img.isNull())
                self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                expected_w = int(round(446.0 * dpi))
                expected_h = int(round(69.0 * dpi))
                self.assertEqual(img.width(), expected_w)
                self.assertEqual(img.height(), expected_h)

    def test_jarvis_card_rendering(self):
        """Verify JARVIS card renders offscreen at 100% and 150% DPI."""
        for theme in THEMES:
            for dpi in DPIS:
                img = render_jarvis_card(dpi, theme)
                self.assertFalse(img.isNull())
                self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                expected_w = int(round(390.0 * dpi))
                expected_h = int(round(188.0 * dpi))
                self.assertEqual(img.width(), expected_w)
                self.assertEqual(img.height(), expected_h)

    def test_assistant_states_rendering_and_visual(self):
        """Verify assistant states (wake, listening, thinking, prompt, speaking, result, error) across themes/DPIs."""
        states = ["wake", "listening", "thinking", "prompt", "speaking", "result", "error"]
        for theme in THEMES:
            for dpi in DPIS:
                dpi_str = f"dpi{int(dpi * 100)}"
                for st in states:
                    img = render_assistant_state(st, dpi, theme)
                    self.assertFalse(img.isNull())
                    self.assertEqual(img.format(), QImage.Format.Format_ARGB32)
                    _assert_image_matches_golden(self, img, f"island_{st}_{theme}_{dpi_str}.png")


if __name__ == "__main__":
    unittest.main()
