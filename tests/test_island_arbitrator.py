"""
tests/test_island_arbitrator.py — Unit tests for Dynamic Island Content Arbitration Logic.

Verifies:
  - Strict priority evaluation: assistant states > override > active timer > media playing > time+weather > plain idle pill.
  - 30-second pause rule: media retains priority during first 30s of pause, then falls back to time+weather or idle pill.
  - Fullscreen detection logic: hides island during fullscreen apps/games unless assistant is actively invoked.
  - Idle display configuration: time_weather, time, and off.
  - Widgets hidden toggle: suppresses media, timer, and weather widgets while in idle.
  - Zero Qt imports; 100% pure Python behind interfaces.
"""

import time
import unittest
from unittest.mock import MagicMock

from island_arbitrator import (
    IslandWidgetType,
    IslandArbitrator,
    ArbitratorConfig,
    ArbitrationInputs,
    AssistantInput,
    TimerInput,
    MediaInput,
    WeatherInput,
    arbitrate_content,
    PRIORITY_ASSISTANT,
    PRIORITY_OVERRIDE,
    PRIORITY_TIMER,
    PRIORITY_MEDIA,
    PRIORITY_IDLE_WIDGET,
    PRIORITY_IDLE,
    PRIORITY_HIDDEN,
)


class TestIslandArbitrator(unittest.TestCase):
    def setUp(self):
        self.clock_val = 1000.0
        self.clock = lambda: self.clock_val
        self.config = ArbitratorConfig()
        self.arbitrator = IslandArbitrator(config=self.config, clock_fn=self.clock)

    def test_01_idle_pill_fallback(self):
        """When no content is active and idle_display is 'off', arbitrator returns plain IDLE pill."""
        self.config.idle_display = "off"
        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.IDLE)
        self.assertEqual(result.priority, PRIORITY_IDLE)

    def test_02_time_weather_idle_display(self):
        """When weather is available and idle_display is 'time_weather', returns TIME_WEATHER."""
        self.arbitrator.update_weather(
            temperature=21.5,
            condition="sunny",
            description="Clear sky",
            city="New York",
            is_available=True,
        )
        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.TIME_WEATHER)
        self.assertEqual(result.priority, PRIORITY_IDLE_WIDGET)
        self.assertEqual(result.payload["city"], "New York")
        self.assertAlmostEqual(result.payload["temperature"], 21.5)

    def test_03_time_only_when_weather_unavailable(self):
        """When weather is unavailable, time_weather gracefully falls back to TIME."""
        self.config.idle_display = "time_weather"
        self.arbitrator.update_weather(
            temperature=0.0,
            condition="cloudy",
            is_available=False,
        )
        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.TIME)
        self.assertEqual(result.priority, PRIORITY_IDLE_WIDGET)

    def test_04_media_playing_preempts_weather(self):
        """Active media playback preempts time+weather widget."""
        self.arbitrator.update_weather(temperature=21.5, condition="sunny", is_available=True)
        self.arbitrator.update_media(
            is_playing=True,
            title="Blinding Lights",
            artist="The Weeknd",
            duration=200.0,
            position=45.0,
        )
        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.MEDIA)
        self.assertEqual(result.priority, PRIORITY_MEDIA)
        self.assertEqual(result.payload["title"], "Blinding Lights")
        self.assertTrue(result.payload["is_playing"])

    def test_05_media_pause_within_30s_grace_period(self):
        """Media paused within 30s retains MEDIA priority with is_playing=False."""
        self.clock_val = 1000.0
        self.arbitrator.update_weather(temperature=21.5, condition="sunny", is_available=True)
        # Media starts playing
        self.arbitrator.update_media(is_playing=True, title="Song A", artist="Artist A")
        # Media pauses at t=1010
        self.clock_val = 1010.0
        self.arbitrator.update_media(is_playing=False, title="Song A", artist="Artist A")

        # Check at t=1025 (15 seconds after pause: within 30s)
        self.clock_val = 1025.0
        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.MEDIA)
        self.assertEqual(result.priority, PRIORITY_MEDIA)
        self.assertFalse(result.payload["is_playing"])
        self.assertAlmostEqual(result.payload["time_paused"], 15.0)

    def test_06_media_pause_exceeding_30s_drops_priority(self):
        """When media pause exceeds 30s, priority drops and falls back to time+weather."""
        self.clock_val = 1000.0
        self.arbitrator.update_weather(temperature=21.5, condition="sunny", is_available=True)
        self.arbitrator.update_media(is_playing=True, title="Song A", artist="Artist A")
        # Pause at t=1010
        self.clock_val = 1010.0
        self.arbitrator.update_media(is_playing=False, title="Song A", artist="Artist A")

        # Check at t=1045 (35 seconds after pause: > 30s grace)
        self.clock_val = 1045.0
        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.TIME_WEATHER)
        self.assertEqual(result.priority, PRIORITY_IDLE_WIDGET)

    def test_07_active_timer_preempts_media(self):
        """An active timer preempts media playing."""
        self.arbitrator.update_media(is_playing=True, title="Song A", artist="Artist A")
        self.arbitrator.update_timer(is_active=True, remaining_seconds=45.0, total_seconds=60.0, label="Tea")

        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.TIMER)
        self.assertEqual(result.priority, PRIORITY_TIMER)
        self.assertEqual(result.payload["label"], "Tea")
        self.assertAlmostEqual(result.payload["remaining_seconds"], 45.0)

    def test_08_assistant_state_preempts_everything(self):
        """Assistant interaction (WAKE, LISTENING, THINKING, etc.) preempts timer and media."""
        self.arbitrator.update_media(is_playing=True, title="Song A")
        self.arbitrator.update_timer(is_active=True, remaining_seconds=30.0)
        self.arbitrator.update_assistant("LISTENING", active=True, payload={"rms": 0.42})

        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.ASSISTANT)
        self.assertEqual(result.priority, PRIORITY_ASSISTANT)
        self.assertEqual(result.payload["state"], "LISTENING")

    def test_09_fullscreen_hides_idle_and_widgets(self):
        """When fullscreen is active and hide_fullscreen is True, island is HIDDEN."""
        self.arbitrator.update_weather(temperature=21.5, condition="sunny", is_available=True)
        self.arbitrator.update_media(is_playing=True, title="Song A")
        self.arbitrator.set_fullscreen(True)

        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.HIDDEN)
        self.assertEqual(result.priority, PRIORITY_HIDDEN)

    def test_10_fullscreen_allows_assistant_invocation(self):
        """Assistant invoked while in fullscreen still displays with ASSISTANT priority."""
        self.arbitrator.set_fullscreen(True)
        self.arbitrator.update_assistant("WAKE", active=True, duration=2.0)

        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.ASSISTANT)
        self.assertEqual(result.priority, PRIORITY_ASSISTANT)
        self.assertEqual(result.payload["state"], "WAKE")

    def test_11_widgets_hidden_toggle(self):
        """When widgets are hidden, media and weather are suppressed to plain IDLE capsule."""
        self.arbitrator.update_weather(temperature=21.5, condition="sunny", is_available=True)
        self.arbitrator.update_media(is_playing=True, title="Song A")
        self.arbitrator.set_widgets_hidden(True)

        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.IDLE)
        self.assertEqual(result.priority, PRIORITY_IDLE)

        # Toggle widgets back on
        self.arbitrator.set_widgets_hidden(False)
        result2 = self.arbitrator.evaluate()
        self.assertEqual(result2.widget_type, IslandWidgetType.MEDIA)

    def test_12_manual_user_override(self):
        """Manual user override temporarily forces widget display."""
        self.clock_val = 1000.0
        self.arbitrator.update_weather(temperature=21.5, condition="sunny", is_available=True)
        self.arbitrator.set_manual_override(IslandWidgetType.TIME_WEATHER, duration_sec=5.0)

        result = self.arbitrator.evaluate()
        self.assertEqual(result.widget_type, IslandWidgetType.TIME_WEATHER)
        self.assertEqual(result.priority, PRIORITY_OVERRIDE)

        # After expiration, returns normal priority
        self.clock_val = 1006.0
        result_after = self.arbitrator.evaluate()
        self.assertEqual(result_after.priority, PRIORITY_IDLE_WIDGET)


if __name__ == "__main__":
    unittest.main()
