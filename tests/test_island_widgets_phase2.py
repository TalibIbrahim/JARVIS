"""
tests/test_island_widgets_phase2.py — Unit test suite for Phase 2 Dynamic Island Widgets,
Cards, Interactivity without Focus Stealing, Fullscreen Suppression, and Settings UI.
"""

import sys
import threading
import time
import unittest

from PySide6.QtCore import Qt, QPointF, QPoint
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from config import Config
from ui.island import AppleDynamicIsland, IslandState, IslandDemoController, _SIZE
from ui.settings_window import SettingsWindow
from overlay import Overlay


class TestIslandWidgetsPhase2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.cfg = Config()
        self.stop_evt = threading.Event()
        self.ov = Overlay(self.stop_evt, cfg=self.cfg)
        self.island = self.ov.island

    def tearDown(self):
        self.stop_evt.set()
        self.ov.timer.stop()
        self.app.processEvents()

    def test_01_compact_widgets_layouts_and_springs(self):
        """Verify Time+Weather, Media, and Timer compact layouts and spring sizes."""
        now = time.time()
        self.island.reset_to_idle()

        # 1. Time + Weather compact layout
        self.island.compact_mode_override = "time_weather"
        self.island.update_frame(now)
        tw, th = self.island._target_size(IslandState.IDLE, now)
        self.assertAlmostEqual(tw, 176.0, delta=2.0)
        self.assertAlmostEqual(th, 32.0, delta=2.0)
        self.assertEqual(self.island.spring_w.target, tw)
        self.assertEqual(self.island.spring_h.target, th)

        # 2. Media compact layout
        self.island.compact_mode_override = "media"
        self.island.media_active = True
        self.island.media_playing = True
        self.island.update_frame(now)
        mw, mh = self.island._target_size(IslandState.IDLE, now)
        self.assertAlmostEqual(mw, 186.0, delta=2.0)
        self.assertAlmostEqual(mh, 32.0, delta=2.0)
        self.assertEqual(len(self.island._media_bars), 4)

        # 3. Timer compact layout
        self.island.compact_mode_override = "timer"
        self.island.timer_active = True
        self.island.timer_end_time = now + 120.0
        self.island.update_frame(now)
        rw, rh = self.island._target_size(IslandState.IDLE, now)
        self.assertAlmostEqual(rw, 152.0, delta=2.0)
        self.assertAlmostEqual(rh, 32.0, delta=2.0)

    def test_02_expanded_media_card(self):
        """Verify Expanded Media Card layout, scrubber, buttons, and dominant color."""
        now = time.time()
        self.island.expand_card("media")
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.CARD_MEDIA)
        cw, ch = _SIZE[IslandState.CARD_MEDIA]
        self.assertEqual(self.island.spring_w.target, cw)
        self.assertEqual(self.island.spring_h.target, ch)

        # Scrubber bar hit test
        sr = self.island._scrubber_rect()
        self.assertGreater(sr.width(), 200.0)

        # Buttons hit test
        w = max(float(self.island.width()), self.island.spring_w.target)
        btn_prev = self.island._button_at(QPointF(w / 2.0 - 52.0, 136.0))
        btn_play = self.island._button_at(QPointF(w / 2.0, 136.0))
        btn_next = self.island._button_at(QPointF(w / 2.0 + 52.0, 136.0))
        self.assertEqual(btn_prev, "prev")
        self.assertEqual(btn_play, "play_pause")
        self.assertEqual(btn_next, "next")

        # Play-pause toggle
        initial_playing = self.island.media_playing
        self.island._trigger_button("play_pause")
        self.assertNotEqual(self.island.media_playing, initial_playing)

    def test_03_expanded_weather_card(self):
        """Verify Expanded Weather Card elements and hourly forecast."""
        now = time.time()
        self.island.expand_card("weather")
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.CARD_WEATHER)
        cw, ch = _SIZE[IslandState.CARD_WEATHER]
        self.assertEqual(self.island.spring_w.target, cw)
        self.assertEqual(self.island.spring_h.target, ch)
        self.assertGreaterEqual(len(self.island.weather_hourly), 5)

        # Temperature formatting
        self.island.weather_temp = 68.0
        self.assertEqual(self.island.format_temp(68.0), "68°")

    def test_04_expanded_time_card(self):
        """Verify Expanded Time Card clock and date display."""
        now = time.time()
        self.island.expand_card("time")
        self.island.update_frame(now)

        self.assertEqual(self.island._resolve_state(now), IslandState.CARD_TIME)
        cw, ch = _SIZE[IslandState.CARD_TIME]
        self.assertEqual(self.island.spring_w.target, cw)
        self.assertEqual(self.island.spring_h.target, ch)

    def test_05_interactivity_and_auto_collapse(self):
        """Verify WS_EX_NOACTIVATE interactivity toggle and 4s auto-collapse."""
        now = time.time()
        self.island.collapse_card()
        self.assertFalse(self.island.is_interactive)

        # Expand enables interactivity
        self.island.expand_card("weather")
        self.assertTrue(self.island.is_interactive)
        self.assertEqual(self.island.expanded_card, "weather")

        # After 4.1s without interaction, collapses smoothly
        self.island.update_frame(now + 4.2)
        self.assertIsNone(self.island.expanded_card)
        self.assertFalse(self.island.is_interactive)

    def test_06_voice_expansion_triggers(self):
        """Verify voice prompt commands expand media and weather cards."""
        now = time.time()
        self.ov.post("prompt", text="Jarvis show media")
        self.ov._drain(now)
        self.island.update_frame(now)
        self.assertEqual(self.island.expanded_card, "media")

        self.ov.post("prompt", text="Jarvis show weather forecast")
        self.ov._drain(now + 0.1)
        self.island.update_frame(now + 0.1)
        self.assertEqual(self.island.expanded_card, "weather")

    def test_07_fullscreen_suppression(self):
        """Verify hide_fullscreen collapses the island smoothly to 0 height."""
        now = time.time()
        self.cfg.island_hide_fullscreen = True
        self.island.reset_to_idle()
        self.island.update_frame(now)

        # Notify fullscreen
        self.ov.post("fullscreen", active=True)
        self.ov._drain(now)
        self.island.update_frame(now + 0.02)
        self.assertTrue(self.island.is_fullscreen_suppressed)
        self.assertEqual(self.island.spring_h.target, 0.0)

        # Exit fullscreen
        self.ov.post("fullscreen", active=False)
        self.ov._drain(now + 0.04)
        self.island.update_frame(now + 0.06)
        self.assertFalse(self.island.is_fullscreen_suppressed)
        self.assertGreater(self.island.spring_h.target, 0.0)

    def test_08_settings_window_dynamic_island_section(self):
        """Verify SettingsWindow displays all 9 Dynamic Island options and saves live."""
        dlg = SettingsWindow(self.cfg)
        dlg.select_tab("Display")

        # Verify saving an island setting updates config and applies live
        dlg._save("island_weather_city", "London")
        self.assertEqual(getattr(self.cfg, "island_weather_city"), "London")

        dlg._save("island_idle_display", "time")
        self.assertEqual(getattr(self.cfg, "island_idle_display"), "time")

        dlg._save("island_size", "large")
        self.assertEqual(getattr(self.cfg, "island_size"), "large")
        dlg._save("island_size", "default")
        dlg._save("island_idle_display", "time_weather")
        dlg.close()

    def test_09_extended_demo_controller(self):
        """Verify extended IslandDemoController steps through compact and expanded widgets."""
        demo = IslandDemoController(self.island)
        demo.start()
        self.assertTrue(demo.active)

        # Step 0: Compact Time/Weather
        demo._apply_step(0)
        self.assertEqual(self.island.compact_mode_override, "time_weather")

        # Step 1: Expanded Weather Card
        demo._apply_step(1)
        self.assertEqual(self.island.expanded_card, "weather")

        # Step 2: Compact Media
        demo._apply_step(2)
        self.assertEqual(self.island.compact_mode_override, "media")

        # Step 3: Expanded Media Card
        demo._apply_step(3)
        self.assertEqual(self.island.expanded_card, "media")

        # Step 4: Compact Timer
        demo._apply_step(4)
        self.assertEqual(self.island.compact_mode_override, "timer")

        # Step 5: Assistant Wake
        demo._apply_step(5)
        self.assertTrue(self.island.is_wake)

        demo.stop()
        self.assertFalse(demo.active)

    def test_10_shape_style_pill_and_notch(self):
        """Verify switching between Pill (floating) and Notch (attached) updates geometry and rendering."""
        screen = QApplication.primaryScreen()
        geom_y = screen.geometry().y() if screen else 0

        # Set to pill
        self.island.set_shape_style("pill")
        self.island._center_top()
        self.assertEqual(self.island.shape_style, "pill")
        self.assertEqual(self.island.geometry().y(), geom_y + 10)

        # Set to notch
        self.island.set_shape_style("notch")
        self.island._center_top()
        self.assertEqual(self.island.shape_style, "notch")
        self.assertEqual(self.island.geometry().y(), geom_y)

        # Live switch via SettingsWindow _save
        dlg = SettingsWindow(self.cfg)
        dlg._save("island_shape_style", "pill")
        self.assertEqual(self.cfg.island_shape_style, "pill")
        self.assertEqual(self.island.shape_style, "pill")
        self.assertEqual(self.island.geometry().y(), geom_y + 10)

        dlg._save("island_shape_style", "notch")
        self.assertEqual(self.cfg.island_shape_style, "notch")
        self.assertEqual(self.island.shape_style, "notch")
        self.assertEqual(self.island.geometry().y(), geom_y)
        dlg.close()

    def test_11_weather_no_negative_double_conversion(self):
        """Verify Celsius temperatures are not double-converted into impossible negatives (e.g. Lahore 3°C -> -16°C)."""
        self.island.weather_unit_symbol = "°C"
        self.island.weather_temp = 24.6
        self.cfg.island_weather_units = "celsius"

        # 24.6°C must format as 25°, NOT -4°
        self.assertEqual(self.island.format_temp(24.6), "25°")

        # 3.0°C in winter Lahore must format as 3°, NOT -16°
        self.assertEqual(self.island.format_temp(3.0), "3°")

        # When feed sends Fahrenheit 68°F and user wants Celsius, correctly converts to 20°
        self.island.weather_unit_symbol = "°F"
        self.assertEqual(self.island.format_temp(68.0), "20°")

        # When feed sends Celsius 20°C and user wants Fahrenheit, correctly converts to 68°
        self.cfg.island_weather_units = "fahrenheit"
        self.island.weather_unit_symbol = "°C"
        self.assertEqual(self.island.format_temp(20.0), "68°")

    def test_12_city_dropdown_in_settings_window(self):
        """Verify city selection uses a searchable QComboBox dropdown."""
        dlg = SettingsWindow(self.cfg)
        self.assertTrue(hasattr(dlg, "city_combo"))
        self.assertTrue(dlg.city_combo.isEditable())
        self.assertGreater(dlg.city_combo.count(), 5)

        # Check default city list contains Lahore and London
        all_cities = [dlg.city_combo.itemText(i) for i in range(dlg.city_combo.count())]
        self.assertTrue(any("Lahore" in c for c in all_cities))
        self.assertTrue(any("London" in c for c in all_cities))

        # Test selecting a city saves both island_weather_city and weather_city
        dlg._save("island_weather_city", "Lahore, Punjab, Pakistan")
        self.assertEqual(getattr(self.cfg, "island_weather_city"), "Lahore, Punjab, Pakistan")
        self.assertEqual(getattr(self.cfg, "weather_city"), "Lahore, Punjab, Pakistan")
    def test_13_media_dynamic_gradient_and_dominant_color(self):
        """Verify media player card color dynamically samples album cover gradient/dominant color instead of fixed purple."""
        import io
        from PIL import Image

        # Create a bright emerald green album cover image
        img = Image.new("RGB", (64, 64), (16, 185, 129))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        green_bytes = buf.getvalue()

        # Initially, dominant color is default
        self.island.set_thumbnail_bytes(green_bytes)
        self.assertIsNotNone(self.island.media_album_art)
        self.assertFalse(self.island.media_album_art.isNull())

        # Dominant and average colors must reflect the green album art, NOT fixed purple (175, 82, 222)
        dom = self.island.media_dominant_color
        self.assertNotEqual((dom.red(), dom.green(), dom.blue()), (175, 82, 222))
        self.assertGreater(dom.green(), dom.red())
        self.assertGreater(dom.green(), dom.blue())

        # Verify average color is set
        avg = self.island.media_average_color
        self.assertIsNotNone(avg)
        self.assertGreater(avg.green(), avg.red())

        # Now test overlay.py media_state event handling
        mock_stop = threading.Event()
        try:
            from overlay import Overlay
            # Test queue processing with thumbnail_bytes and dominant_color
            overlay = Overlay(mock_stop, cfg=self.cfg)
            overlay.island = self.island

            # Create an amber/gold album cover image
            img_amber = Image.new("RGB", (64, 64), (245, 158, 11))
            buf_amber = io.BytesIO()
            img_amber.save(buf_amber, format="PNG")
            amber_bytes = buf_amber.getvalue()

            overlay.post(
                "media_state",
                title="Song A",
                artist="Artist B",
                album="Album C",
                playing=True,
                active=True,
                position=10.0,
                duration=100.0,
                progress=0.1,
                source="Spotify",
                thumbnail_bytes=amber_bytes,
                dominant_color="#F59E0B",
            )
            overlay._drain(time.time())

            self.assertEqual(self.island.media_title, "Song A")
            self.assertEqual(self.island.media_artist, "Artist B")
            self.assertEqual(self.island.media_album, "Album C")
            amber_dom = self.island.media_dominant_color
            self.assertEqual(amber_dom.name().upper(), "#F59E0B")
            self.assertNotEqual((amber_dom.red(), amber_dom.green(), amber_dom.blue()), (175, 82, 222))
        finally:
            mock_stop.set()

    def test_14_live_settings_reload(self):
        """Verify settings changes take effect live without restart."""
        dlg = SettingsWindow(self.cfg)
        try:
            # 1. Size Scale Live Reload
            dlg._save("island_size_scale", "large")
            self.assertEqual(self.cfg.island_size_scale, "large")
            self.assertEqual(self.island._get_size_scale(), 1.15)

            dlg._save("island_size_scale", "compact")
            self.assertEqual(self.cfg.island_size_scale, "compact")
            self.assertEqual(self.island._get_size_scale(), 0.85)

            # Reset back to default/medium
            dlg._save("island_size_scale", "medium")
            self.assertEqual(self.island._get_size_scale(), 1.0)

            # 2. Hardware Layout Live Reload
            self.island.expanded_card = "hardware_grid"
            dlg._save("island_hardware_layout", "strip")
            self.assertEqual(self.island.expanded_card, "hardware_strip")

            dlg._save("island_hardware_layout", "grid")
            self.assertEqual(self.island.expanded_card, "hardware_grid")

            # 3. Pin Open Live Reload
            dlg._save("island_pin_open", True)
            self.assertTrue(self.island.pin_open)
            dlg._save("island_pin_open", False)
            self.assertFalse(self.island.pin_open)

            # 4. First Weekday Live Reload (cache invalidation)
            self.island._cached_calendar_data = "cached_data"
            self.island._cached_calendar_key = (2026, 10, 18, "locale")
            dlg._save("island_first_weekday", "monday")
            self.assertIsNone(self.island._cached_calendar_data)
            self.assertIsNone(self.island._cached_calendar_key)

            # 5. Theme Mode Live Reload
            dlg._save("theme_mode", "light")
            self.assertFalse(self.island.dark_mode)

            dlg._save("theme_mode", "dark")
            self.assertTrue(self.island.dark_mode)

            # 6. Context & Subsystem Live Reload
            class MockSubsystem:
                def __init__(self):
                    self.reloaded = False
                    self.cfg = None
                def reload_config(self, cfg=None):
                    self.reloaded = True
                    self.cfg = cfg

            mock_tracker = MockSubsystem()
            mock_voice = MockSubsystem()
            mock_controller = MockSubsystem()

            from main import Context
            mock_ctx = Context(
                engine=MockSubsystem(),
                overlay=self.island.mgr,
                speaker=MockSubsystem(),
                stop=threading.Event(),
                tracker=mock_tracker,
                controller=mock_controller,
                voice=mock_voice,
                cfg=self.cfg,
            )
            dlg.ctx = mock_ctx

            dlg._save("wake_sensitivity", "aggressive")
            self.assertTrue(mock_tracker.reloaded)
            self.assertTrue(mock_voice.reloaded)
            self.assertTrue(mock_controller.reloaded)
        finally:
            dlg.close()

    def test_15_media_peek_hover_and_click(self):
        """Verify peeking at playing media on hover and expanding via click."""
        from unittest.mock import patch
        now = time.time()
        self.island.reset_to_idle()
        self.island.media_playing = True
        self.island.media_title = "Starboy"
        self.island.compact_mode_override = "time_weather"

        # 1. Hover Peek
        self.assertIsNone(self.island.expanded_card)
        self.assertFalse(self.island._opened_via_hover)

        self.island.show()
        self.island.setGeometry(100, 0, 180, 32)

        # Simulate cursor hovering
        with patch("PySide6.QtGui.QCursor.pos", return_value=QPoint(150, 10)):
            self.island._update_interaction(now, 0.016)
            self.assertEqual(self.island._hover_enter_t, now)

            # Advance time past hover threshold (0.25s)
            self.island._update_interaction(now + 0.3, 0.016)
            # Island expands into media card!
            self.assertEqual(self.island.expanded_card, "media")
            self.assertTrue(self.island._opened_via_hover)

        # Cursor leaves the island
        with patch("PySide6.QtGui.QCursor.pos", return_value=QPoint(150, 200)):
            # First frame cursor left
            self.island._update_interaction(now + 0.4, 0.016)
            self.assertEqual(self.island._hover_leave_t, now + 0.4)
            self.assertEqual(self.island.expanded_card, "media")

            # After 0.35s grace period, hover peek auto-collapses
            self.island._update_interaction(now + 0.8, 0.016)
            self.assertIsNone(self.island.expanded_card)
            self.assertFalse(self.island._opened_via_hover)

        # 2. Direct Click Expansion while collapsed
        self.island.reset_to_idle()
        self.island.media_playing = True
        self.assertIsNone(self.island.expanded_card)

        # Click event on the collapsed island
        click_evt = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(50.0, 16.0),
            QPointF(150.0, 16.0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.island.mousePressEvent(click_evt)
        self.assertEqual(self.island.expanded_card, "media")
        self.assertFalse(self.island._opened_via_hover)

        # Mouse leaves after click-expansion -> should NOT auto-collapse after 0.35s
        with patch("PySide6.QtGui.QCursor.pos", return_value=QPoint(150, 200)):
            self.island._update_interaction(now + 1.0, 0.016)
            self.island._update_interaction(now + 1.5, 0.016)
            self.assertEqual(self.island.expanded_card, "media")

        # 3. Clicking while hover-peeking promotes peek to full card expansion
        self.island.reset_to_idle()
        self.island.media_playing = True
        self.island._opened_via_hover = True
        self.island.expanded_card = "media"

        self.island.mousePressEvent(click_evt)
        self.assertEqual(self.island.expanded_card, "media")
        self.assertFalse(self.island._opened_via_hover)

    def test_16_waveform_color_selection_and_live_reload(self):
        """Verify waveform color presets, custom color selection, and live reload."""
        # 1. Default should be blue only
        self.assertEqual(self.cfg.island_waveform_color, "blue")
        self.island.waveform_color = "blue"
        blue_bars = self.island._get_waveform_colors()
        self.assertEqual(len(blue_bars), 7)
        for i, c in enumerate(blue_bars):
            # All bars have blue as primary channel
            self.assertGreater(c.blue(), 180, f"Bar {i} must have strong blue component")
            if i != 3:  # non-crest bars have no red (no purple!)
                self.assertEqual(c.red(), 0, f"Bar {i} must have 0 red (pure blue, no purple)")

        # 2. Preset color generation (green, red, custom hex)
        self.island.waveform_color = "green"
        green_bars = self.island._get_waveform_colors()
        self.assertEqual(len(green_bars), 7)
        for i, c in enumerate(green_bars):
            if i != 3:
                self.assertGreater(c.green(), c.red())
                self.assertGreater(c.green(), c.blue())

        self.island.waveform_color = "red"
        red_bars = self.island._get_waveform_colors()
        self.assertEqual(len(red_bars), 7)
        for i, c in enumerate(red_bars):
            if i != 3:
                self.assertGreater(c.red(), c.green())
                self.assertGreater(c.red(), c.blue())

        self.island.waveform_color = "#FF1493"
        custom_bars = self.island._get_waveform_colors()
        self.assertEqual(len(custom_bars), 7)
        self.assertEqual(custom_bars[2].red(), 255)

        # 3. Live settings reload via SettingsWindow
        dlg = SettingsWindow(cfg=self.cfg)
        try:
            dlg.sidebar.setCurrentRow(4)  # Island tab
            self.app.processEvents()

            self.assertIsNotNone(dlg.wf_color_combo)
            self.assertIsNotNone(dlg.wf_color_btn)

            # Live change to green
            dlg._save("island_waveform_color", "green")
            self.assertEqual(self.cfg.island_waveform_color, "green")
            self.assertEqual(self.island.waveform_color, "green")

            # Live change to custom hex
            dlg._save("island_waveform_color", "#00FFCC")
            self.assertEqual(self.cfg.island_waveform_color, "#00FFCC")
            self.assertEqual(self.island.waveform_color, "#00FFCC")

            # Live revert to default blue
            dlg._save("island_waveform_color", "blue")
            self.assertEqual(self.cfg.island_waveform_color, "blue")
            self.assertEqual(self.island.waveform_color, "blue")
        finally:
            dlg.close()


if __name__ == "__main__":
    unittest.main()
