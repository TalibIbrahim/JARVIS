"""
tests/test_island_manager.py — Unit Tests for Dynamic Island Page Manager.

Verifies:
  1. Page navigation & cycling (next_page, prev_page, wrap-around).
  2. Page clamping, enabling/disabling, and empty active pages safety.
  3. Inactivity tracking & 4.0s auto-collapse timing with mock clock.
  4. Pin open flag suppressing auto-collapse.
  5. Size scale factors, dimension scaling (small 0.85x, medium 1.0x, large 1.15x),
     hardware grid vs strip layout, shape style (pill vs notch), and triggers.
  6. Assistant preemption arbitration (wake, listening, thinking, speaking, result, error)
     taking precedence over expanded cards and returning to compact pill when done.
  7. Zero background poll waste (should_poll pauses when collapsed or hidden).
  8. Config synchronization and serializable dict representation.

Strictly NO Qt imports. 100% unit-testable.
"""

import unittest
from config import Config
from island_manager import (
    IslandPage,
    IslandPageManager,
    IslandSizeScale,
    IslandHardwareLayout,
    IslandShapeStyle,
    IslandExpandTrigger,
    SCALE_FACTORS,
    DEFAULT_INACTIVITY_TIMEOUT,
    BASE_PAGE_DIMENSIONS,
    BASE_HARDWARE_STRIP_DIMENSIONS,
    BASE_COMPACT_PILL_DIMENSIONS,
)


class MockClock:
    """Deterministic injectable clock."""
    def __init__(self, start_time: float = 1000.0):
        self._time = float(start_time)

    def __call__(self) -> float:
        return self._time

    def advance(self, dt: float) -> None:
        self._time += float(dt)

    def set(self, t: float) -> None:
        self._time = float(t)


class TestIslandPageManager(unittest.TestCase):
    """Unit tests for IslandPageManager logic and arbitration."""

    def setUp(self):
        self.clock = MockClock(1000.0)
        self.mgr = IslandPageManager(clock_fn=self.clock)

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Page Cycling & Navigation
    # ──────────────────────────────────────────────────────────────────────────

    def test_default_pages_and_order(self):
        """Default pages list should have 5 pages in specified order."""
        expected = [
            IslandPage.MEDIA,
            IslandPage.CALENDAR,
            IslandPage.WEATHER,
            IslandPage.HARDWARE,
            IslandPage.JARVIS,
        ]
        self.assertEqual(self.mgr.pages, expected)
        self.assertEqual(self.mgr.active_pages, expected)
        self.assertEqual(self.mgr.current_page, IslandPage.MEDIA)
        self.assertEqual(self.mgr.page_index, 0)

    def test_next_page_forward_cycling_and_wraparound(self):
        """next_page() advances sequentially and wraps from last to first."""
        self.assertEqual(self.mgr.current_page, IslandPage.MEDIA)

        self.assertEqual(self.mgr.next_page(), IslandPage.CALENDAR)
        self.assertEqual(self.mgr.page_index, 1)

        self.assertEqual(self.mgr.next_page(), IslandPage.WEATHER)
        self.assertEqual(self.mgr.page_index, 2)

        self.assertEqual(self.mgr.next_page(), IslandPage.HARDWARE)
        self.assertEqual(self.mgr.page_index, 3)

        self.assertEqual(self.mgr.next_page(), IslandPage.JARVIS)
        self.assertEqual(self.mgr.page_index, 4)

        # Wrap around to beginning
        self.assertEqual(self.mgr.next_page(), IslandPage.MEDIA)
        self.assertEqual(self.mgr.page_index, 0)

    def test_prev_page_backward_cycling_and_wraparound(self):
        """prev_page() cycles backwards and wraps from first to last."""
        self.assertEqual(self.mgr.current_page, IslandPage.MEDIA)

        # Wrap around backwards from 0 to last
        self.assertEqual(self.mgr.prev_page(), IslandPage.JARVIS)
        self.assertEqual(self.mgr.page_index, 4)

        self.assertEqual(self.mgr.prev_page(), IslandPage.HARDWARE)
        self.assertEqual(self.mgr.page_index, 3)

        self.assertEqual(self.mgr.prev_page(), IslandPage.WEATHER)
        self.assertEqual(self.mgr.page_index, 2)

        self.assertEqual(self.mgr.prev_page(), IslandPage.CALENDAR)
        self.assertEqual(self.mgr.page_index, 1)

        self.assertEqual(self.mgr.prev_page(), IslandPage.MEDIA)
        self.assertEqual(self.mgr.page_index, 0)

    def test_set_page_by_enum_and_string(self):
        """set_page accepts both enum members and case-insensitive strings."""
        self.assertTrue(self.mgr.set_page(IslandPage.WEATHER))
        self.assertEqual(self.mgr.current_page, IslandPage.WEATHER)
        self.assertEqual(self.mgr.page_index, 2)

        self.assertTrue(self.mgr.set_page("calendar"))
        self.assertEqual(self.mgr.current_page, IslandPage.CALENDAR)
        self.assertEqual(self.mgr.page_index, 1)

        self.assertTrue(self.mgr.set_page("HARDWARE"))
        self.assertEqual(self.mgr.current_page, IslandPage.HARDWARE)
        self.assertEqual(self.mgr.page_index, 3)

        # Unknown string returns False
        self.assertFalse(self.mgr.set_page("nonexistent_page"))
        self.assertEqual(self.mgr.current_page, IslandPage.HARDWARE)

    def test_page_index_setter_clamps_correctly(self):
        """Setting page_index clamps within valid range."""
        self.mgr.page_index = 3
        self.assertEqual(self.mgr.current_page, IslandPage.HARDWARE)

        # Over-range clamped to max
        self.mgr.page_index = 999
        self.assertEqual(self.mgr.current_page, IslandPage.JARVIS)
        self.assertEqual(self.mgr.page_index, 4)

        # Under-range clamped to 0
        self.mgr.page_index = -5
        self.assertEqual(self.mgr.current_page, IslandPage.MEDIA)
        self.assertEqual(self.mgr.page_index, 0)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Page Enabling / Disabling & Clamping
    # ──────────────────────────────────────────────────────────────────────────

    def test_disable_single_page_skips_during_cycling(self):
        """Disabled pages are excluded from active_pages and skipped during cycling."""
        self.mgr.set_page_enabled(IslandPage.CALENDAR, False)
        self.assertNotIn(IslandPage.CALENDAR, self.mgr.active_pages)
        self.assertEqual(len(self.mgr.active_pages), 4)

        # media -> weather (skipping calendar)
        self.assertEqual(self.mgr.current_page, IslandPage.MEDIA)
        self.assertEqual(self.mgr.next_page(), IslandPage.WEATHER)
        self.assertEqual(self.mgr.prev_page(), IslandPage.MEDIA)

    def test_disabling_current_page_clamps_to_next_active(self):
        """If the currently active page is disabled, it clamps to valid active page."""
        self.mgr.set_page(IslandPage.CALENDAR)
        self.assertEqual(self.mgr.current_page, IslandPage.CALENDAR)

        self.mgr.set_page_enabled("calendar", False)
        self.assertNotEqual(self.mgr.current_page, IslandPage.CALENDAR)
        self.assertIn(self.mgr.current_page, self.mgr.active_pages)

    def test_cannot_set_disabled_page(self):
        """set_page() returns False when attempting to select a disabled page."""
        self.mgr.set_page_enabled(IslandPage.WEATHER, False)
        self.assertFalse(self.mgr.set_page(IslandPage.WEATHER))
        self.assertNotEqual(self.mgr.current_page, IslandPage.WEATHER)

    def test_all_pages_disabled_safety(self):
        """When all pages are disabled, manager behaves safely without exceptions."""
        for p in self.mgr.pages:
            self.mgr.set_page_enabled(p, False)

        self.assertEqual(self.mgr.active_pages, [])
        self.assertIsNone(self.mgr.current_page)
        self.assertEqual(self.mgr.page_index, -1)
        self.assertIsNone(self.mgr.next_page())
        self.assertIsNone(self.mgr.prev_page())

        # Re-enabling restores active selection
        self.mgr.set_page_enabled(IslandPage.JARVIS, True)
        self.assertEqual(self.mgr.active_pages, [IslandPage.JARVIS])
        self.assertEqual(self.mgr.current_page, IslandPage.JARVIS)
        self.assertEqual(self.mgr.page_index, 0)

    def test_custom_page_list_configuration(self):
        """Setting custom pages reconfigures page list."""
        custom = [IslandPage.WEATHER, IslandPage.MEDIA]
        self.mgr.set_pages(custom)
        self.assertEqual(self.mgr.pages, custom)
        self.assertEqual(self.mgr.active_pages, custom)
        self.assertEqual(self.mgr.current_page, IslandPage.WEATHER)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Inactivity Tracking & Auto-Collapse Timing
    # ──────────────────────────────────────────────────────────────────────────

    def test_auto_collapse_after_4_seconds(self):
        """Expanded card should auto-collapse after default 4.0s inactivity."""
        self.mgr.expand()
        self.assertTrue(self.mgr.is_expanded)
        self.assertEqual(self.mgr.inactivity_timeout, 4.0)

        # At t = 1000.0, just expanded
        self.assertFalse(self.mgr.is_expired(1000.0))
        self.assertEqual(self.mgr.remaining_inactivity_time(1000.0), 4.0)

        # Advance 3.9 seconds (not yet expired)
        self.clock.advance(3.9)
        self.assertFalse(self.mgr.is_expired())
        self.assertAlmostEqual(self.mgr.remaining_inactivity_time(), 0.1, places=3)
        self.assertFalse(self.mgr.check_inactivity())
        self.assertTrue(self.mgr.is_expanded)

        # Advance another 0.1s to reach exactly 4.0s
        self.clock.advance(0.1)
        self.assertTrue(self.mgr.is_expired())
        self.assertEqual(self.mgr.remaining_inactivity_time(), 0.0)

        # check_inactivity triggers collapse
        collapsed = self.mgr.check_inactivity()
        self.assertTrue(collapsed)
        self.assertFalse(self.mgr.is_expanded)

    def test_touch_activity_resets_timer(self):
        """User activity resets inactivity clock."""
        self.mgr.expand()
        self.clock.advance(3.5)
        self.assertFalse(self.mgr.is_expired())

        # Activity occurs (e.g. user hovers, scrolls, touches)
        self.mgr.touch_activity()
        self.assertAlmostEqual(self.mgr.remaining_inactivity_time(), 4.0, places=3)

        # Advance 2.0s from touch: total elapsed since start is 5.5s, but only 2.0s since touch
        self.clock.advance(2.0)
        self.assertFalse(self.mgr.is_expired())

    def test_collapsed_card_does_not_expire(self):
        """When island is already collapsed (compact pill), is_expired returns False."""
        self.mgr.collapse()
        self.assertFalse(self.mgr.is_expanded)
        self.clock.advance(100.0)
        self.assertFalse(self.mgr.is_expired())
        self.assertFalse(self.mgr.check_inactivity())

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Pin Open Flag
    # ──────────────────────────────────────────────────────────────────────────

    def test_pin_open_suppresses_auto_collapse(self):
        """When pin_open is True, auto-collapse is completely suppressed."""
        self.mgr.expand()
        self.mgr.pin_open = True
        self.assertTrue(self.mgr.pin_open)

        # Advance 60 seconds
        self.clock.advance(60.0)
        self.assertFalse(self.mgr.is_expired())
        self.assertEqual(self.mgr.remaining_inactivity_time(), float("inf"))
        self.assertFalse(self.mgr.check_inactivity())
        self.assertTrue(self.mgr.is_expanded)

        # Unpinning re-enables expiration
        self.mgr.pin_open = False
        self.assertTrue(self.mgr.is_expired())
        self.assertTrue(self.mgr.check_inactivity())
        self.assertFalse(self.mgr.is_expanded)

    def test_toggle_pin(self):
        """toggle_pin flips flag state and touches activity."""
        self.assertFalse(self.mgr.pin_open)
        self.assertTrue(self.mgr.toggle_pin())
        self.assertTrue(self.mgr.pin_open)
        self.assertFalse(self.mgr.toggle_pin())
        self.assertFalse(self.mgr.pin_open)

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Size Scale Dimensions & Layout Configurations
    # ──────────────────────────────────────────────────────────────────────────

    def test_size_scale_factors(self):
        """Scale factors must be: small=0.85x, medium=1.0x, large=1.15x."""
        self.mgr.set_size_scale(IslandSizeScale.SMALL)
        self.assertEqual(self.mgr.scale_factor, 0.85)

        self.mgr.set_size_scale("medium")
        self.assertEqual(self.mgr.scale_factor, 1.0)

        self.mgr.set_size_scale(IslandSizeScale.LARGE)
        self.assertEqual(self.mgr.scale_factor, 1.15)

    def test_scale_dimensions_calculation(self):
        """scale_dimensions scales width and height and rounds to integers."""
        # Medium 1.0x
        self.mgr.set_size_scale("medium")
        self.assertEqual(self.mgr.scale_dimensions(400, 180), (400, 180))

        # Small 0.85x: 400 * 0.85 = 340, 180 * 0.85 = 153
        self.mgr.set_size_scale("small")
        self.assertEqual(self.mgr.scale_dimensions(400, 180), (340, 153))

        # Large 1.15x: 400 * 1.15 = 460, 180 * 1.15 = 207
        self.mgr.set_size_scale("large")
        self.assertEqual(self.mgr.scale_dimensions(400, 180), (460, 207))

    def test_get_page_dimensions_for_cards(self):
        """get_page_dimensions returns scaled dimensions for each card."""
        self.mgr.set_size_scale("medium")
        # Media (400, 168)
        self.assertEqual(self.mgr.get_page_dimensions(IslandPage.MEDIA), (400, 168))
        # Calendar (370, 142)
        self.assertEqual(self.mgr.get_page_dimensions(IslandPage.CALENDAR), (370, 142))
        # Weather (400, 180)
        self.assertEqual(self.mgr.get_page_dimensions(IslandPage.WEATHER), (400, 180))

    def test_hardware_layout_grid_vs_strip(self):
        """Hardware page adapts dimensions based on grid vs strip layout."""
        self.mgr.set_size_scale("medium")
        self.mgr.hardware_layout = IslandHardwareLayout.GRID
        self.assertEqual(self.mgr.get_page_dimensions(IslandPage.HARDWARE), (400, 180))

        self.mgr.hardware_layout = IslandHardwareLayout.STRIP
        self.assertEqual(self.mgr.get_page_dimensions(IslandPage.HARDWARE), (420, 72))

    def test_shape_style_and_expand_triggers(self):
        """Shape style and expand trigger enums can be set and parsed."""
        self.mgr.shape_style = IslandShapeStyle.from_str("notch")
        self.assertEqual(self.mgr.shape_style, IslandShapeStyle.NOTCH)

        self.mgr.shape_style = IslandShapeStyle.from_str("pill")
        self.assertEqual(self.mgr.shape_style, IslandShapeStyle.PILL)

        for mode in ("hover", "click", "dwell", "fist", "voice"):
            self.mgr.expand_trigger = IslandExpandTrigger.from_str(mode)
            self.assertEqual(self.mgr.expand_trigger.value, mode)

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Assistant Preemption Arbitration
    # ──────────────────────────────────────────────────────────────────────────

    def test_assistant_states_preempt_expanded_card(self):
        """
        Assistant states (wake, listening, thinking, speaking, result, error)
        take precedence over expanded card pages and return to compact pill when done.
        """
        self.mgr.expand(IslandPage.WEATHER)
        self.assertTrue(self.mgr.is_expanded)
        self.assertEqual(self.mgr.current_page, IslandPage.WEATHER)
        self.assertEqual(self.mgr.effective_card_page, IslandPage.WEATHER)
        self.assertEqual(self.mgr.effective_view_mode, "card:weather")

        # Assistant activates into WAKE
        self.mgr.update_assistant_state("WAKE", active=True)
        self.assertTrue(self.mgr.is_assistant_active)
        self.assertTrue(self.mgr.is_preempted)
        self.assertEqual(self.mgr.assistant_state, "WAKE")
        # Card page is preempted (not visible)
        self.assertIsNone(self.mgr.effective_card_page)
        self.assertEqual(self.mgr.effective_view_mode, "assistant:wake")

        # Assistant transitions to LISTENING -> THINKING -> SPEAKING -> RESULT
        for st in ("LISTENING", "THINKING", "SPEAKING", "RESULT", "ERROR"):
            self.mgr.update_assistant_state(st, active=True)
            self.assertTrue(self.mgr.is_assistant_active)
            self.assertEqual(self.mgr.assistant_state, st)
            self.assertEqual(self.mgr.effective_view_mode, f"assistant:{st.lower()}")

        # Assistant finishes and returns to IDLE
        self.mgr.update_assistant_state("IDLE", active=False)
        self.assertFalse(self.mgr.is_assistant_active)
        self.assertFalse(self.mgr.is_preempted)
        # MUST return to compact pill when done!
        self.assertFalse(self.mgr.is_expanded)
        self.assertEqual(self.mgr.effective_view_mode, "compact_pill")

    def test_clear_assistant_returns_to_compact_pill(self):
        """clear_assistant() resets preemption and collapses to compact pill."""
        self.mgr.expand(IslandPage.MEDIA)
        self.mgr.update_assistant_state("THINKING", active=True)
        self.assertTrue(self.mgr.is_assistant_active)

        self.mgr.clear_assistant()
        self.assertFalse(self.mgr.is_assistant_active)
        self.assertFalse(self.mgr.is_expanded)
        self.assertEqual(self.mgr.effective_view_mode, "compact_pill")

    def test_cannot_expand_while_assistant_preempting(self):
        """expand() is suppressed while an assistant state has precedence."""
        self.mgr.update_assistant_state("LISTENING", active=True)
        self.assertTrue(self.mgr.is_assistant_active)

        # Attempting expand while assistant is talking/listening fails
        result = self.mgr.expand(IslandPage.HARDWARE)
        self.assertFalse(result)
        self.assertFalse(self.mgr.is_expanded)

    def test_assistant_expiration_with_duration(self):
        """Assistant state with duration expires cleanly."""
        self.mgr.update_assistant_state("RESULT", active=True, duration=2.0)
        self.assertTrue(self.mgr.is_assistant_active)

        self.clock.advance(1.9)
        self.assertTrue(self.mgr.is_assistant_active)

        self.clock.advance(0.2)  # total 2.1s
        self.assertFalse(self.mgr.is_assistant_active)

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Energy Optimization & Polling Pauses (Zero Poll Waste)
    # ──────────────────────────────────────────────────────────────────────────

    def test_should_poll_pauses_when_collapsed(self):
        """Background polling pauses when collapsed to compact pill."""
        self.mgr.collapse()
        self.assertFalse(self.mgr.is_expanded)
        self.assertFalse(self.mgr.should_poll)

        self.mgr.expand()
        self.assertTrue(self.mgr.should_poll)

        self.mgr.collapse()
        self.assertFalse(self.mgr.should_poll)

    def test_should_poll_pauses_when_hidden(self):
        """Polling pauses if island is explicitly hidden."""
        self.mgr.expand()
        self.assertTrue(self.mgr.should_poll)

        self.mgr.set_hidden(True)
        self.assertFalse(self.mgr.should_poll)
        self.assertFalse(self.mgr.is_visible)

        self.mgr.set_hidden(False)
        self.assertTrue(self.mgr.should_poll)

    def test_should_poll_pauses_during_fullscreen(self):
        """Fullscreen app suppresses polling unless assistant is active."""
        self.mgr.expand()
        self.mgr.hide_fullscreen = True
        self.mgr.set_fullscreen(True)

        self.assertFalse(self.mgr.should_poll)

        # Assistant active in fullscreen wakes polling
        self.mgr.update_assistant_state("WAKE", active=True)
        self.assertTrue(self.mgr.should_poll)

    # ──────────────────────────────────────────────────────────────────────────
    # 8. Config Schema & Serialization
    # ──────────────────────────────────────────────────────────────────────────

    def test_config_integration(self):
        """IslandPageManager initializes and synchronizes from Config instance."""
        cfg = Config()
        cfg.island_pages = ["weather", "media"]
        cfg.island_pages_enabled = {"weather": True, "media": False}
        cfg.island_size_scale = "large"
        cfg.island_hardware_layout = "strip"
        cfg.island_shape_style = "notch"
        cfg.island_expand_trigger = "fist"
        cfg.island_pin_open = True
        cfg.island_waveform_color = "cyan"

        mgr = IslandPageManager(config=cfg, clock_fn=self.clock)
        self.assertEqual(mgr.pages, [IslandPage.WEATHER, IslandPage.MEDIA])
        self.assertEqual(mgr.active_pages, [IslandPage.WEATHER])
        self.assertEqual(mgr.size_scale, IslandSizeScale.LARGE)
        self.assertEqual(mgr.scale_factor, 1.15)
        self.assertEqual(mgr.hardware_layout, IslandHardwareLayout.STRIP)
        self.assertEqual(mgr.shape_style, IslandShapeStyle.NOTCH)
        self.assertEqual(mgr.expand_trigger, IslandExpandTrigger.FIST)
        self.assertTrue(mgr.pin_open)
        # island is solid black: backdrop settings were removed
        self.assertFalse(hasattr(mgr, "backdrop_blur"))
        self.assertFalse(hasattr(mgr, "backdrop_opacity"))
        self.assertEqual(mgr.waveform_color, "cyan")

    def test_config_serializable_dict(self):
        """Config.to_dict() returns valid serializable dictionary with all island keys."""
        cfg = Config()
        d = cfg.to_dict()
        self.assertIsInstance(d, dict)
        self.assertIn("island_pages", d)
        self.assertIn("island_pages_enabled", d)
        self.assertIn("island_compact_mode", d)
        self.assertIn("island_time_format", d)
        self.assertIn("island_first_weekday", d)
        self.assertIn("island_hardware_layout", d)
        self.assertIn("island_size_scale", d)
        self.assertIn("island_shape_style", d)
        self.assertNotIn("island_backdrop_blur", d)
        self.assertNotIn("island_backdrop_opacity", d)
        self.assertIn("island_pin_open", d)
        self.assertIn("island_expand_trigger", d)
        self.assertIn("island_hide_fullscreen", d)
        self.assertIn("island_waveform_color", d)

    def test_old_config_with_removed_backdrop_keys_loads_and_drops_them(self):
        """An old config file containing the removed backdrop keys loads cleanly and the keys are dropped."""
        import json, os, tempfile
        from config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jarvis_config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"config_version": 2, "island_backdrop_blur": True,
                           "island_backdrop_opacity": 0.5, "island_size_scale": "large"}, f)
            cfg = load_config(path)
            self.assertEqual(cfg.island_size_scale, "large")
            self.assertNotIn("island_backdrop_blur", cfg.to_dict())
            self.assertNotIn("island_backdrop_opacity", cfg.to_dict())
            self.assertFalse(hasattr(cfg, "island_backdrop_blur"))

    def test_tracking_toggle_and_setter(self):
        """IslandPageManager supports tracking state querying, toggling, and setting."""
        self.assertTrue(self.mgr.tracking_enabled)
        # Toggle tracking
        res = self.mgr.toggle_tracking()
        self.assertFalse(res)
        self.assertFalse(self.mgr.tracking_enabled)
        # Set explicitly
        self.mgr.set_tracking(True)
        self.assertTrue(self.mgr.tracking_enabled)
        self.mgr.set_tracking(False)
        self.assertFalse(self.mgr.tracking_enabled)

    def test_assistant_status_tracking(self):
        """IslandPageManager records and reports mic and camera states."""
        status = self.mgr.get_assistant_status()
        self.assertEqual(status["mic"], "active")
        self.assertEqual(status["camera"], "active")

        self.mgr.set_assistant_status(mic="muted", camera="off")
        status2 = self.mgr.get_assistant_status()
        self.assertEqual(status2["mic"], "muted")
        self.assertEqual(status2["camera"], "off")

    def test_last_command_tracking(self):
        """IslandPageManager tracks the last command and its execution result."""
        cmd = self.mgr.get_last_command()
        self.assertEqual(cmd["text"], "")
        self.assertEqual(cmd["result"], "")
        self.assertEqual(cmd["timestamp"], 0.0)

        self.mgr.set_last_command("open spotify", "Spotify opened")
        cmd2 = self.mgr.get_last_command()
        self.assertEqual(cmd2["text"], "open spotify")
        self.assertEqual(cmd2["result"], "Spotify opened")
        self.assertGreater(cmd2["timestamp"], 0.0)

    def test_set_pin_open(self):
        """set_pin_open sets pinned state and returns new value."""
        self.assertFalse(self.mgr.pin_open)
        res = self.mgr.set_pin_open(True)
        self.assertTrue(res)
        self.assertTrue(self.mgr.pin_open)

        res2 = self.mgr.set_pin_open(False)
        self.assertFalse(res2)
        self.assertFalse(self.mgr.pin_open)

    def test_global_island_page_manager_singleton(self):
        """get_island_page_manager returns singleton and set_island_page_manager overrides it."""
        from island_manager import get_island_page_manager, set_island_page_manager
        orig = get_island_page_manager()
        self.assertIsNotNone(orig)

        custom_mgr = IslandPageManager()
        set_island_page_manager(custom_mgr)
        self.assertIs(get_island_page_manager(), custom_mgr)

        # Restore original
        set_island_page_manager(orig)
        self.assertIs(get_island_page_manager(), orig)

    def test_update_island_settings(self):
        """update_island_settings dynamically updates Config and active IslandPageManager."""
        import os
        import tempfile
        from config import update_island_settings, Config
        from island_manager import get_island_page_manager, set_island_page_manager

        test_mgr = IslandPageManager()
        set_island_page_manager(test_mgr)
        test_cfg = Config()

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            temp_path = tf.name

        try:
            ok = update_island_settings(
                cfg=test_cfg,
                config_path=temp_path,
                island_size_scale="large",
                island_pin_open=True,
                island_hardware_layout="strip",
            )
            self.assertTrue(ok)
            self.assertEqual(test_cfg.island_size_scale, "large")
            self.assertTrue(test_cfg.island_pin_open)
            self.assertEqual(test_cfg.island_hardware_layout, "strip")
            self.assertEqual(test_mgr.scale_factor, 1.15)
            self.assertTrue(test_mgr.pin_open)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


if __name__ == "__main__":
    unittest.main()
