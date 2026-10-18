"""
tests/test_phase2_voice_intents.py — Unit tests for Phase 2 Dynamic Island System 1 Voice Intents.

Verifies:
  - "what's the weather", "weather forecast" -> fetches weather & displays card.
  - "what time is it", "what's the time" -> displays time card.
  - "what's playing", "what song is this" -> displays media card.
  - "play", "pause", "skip", "next track", "previous track" -> controls media.
  - "show media", "show weather" -> expands widget.
  - "hide the island widgets", "hide island" -> toggles widgets.
  - Semantic Decision Engine (System 1) fast classification for all new intents.
  - Zero Qt imports; 100% unit-testable with mock contexts.
"""

import unittest
from unittest.mock import MagicMock, patch

import commands
from config import Config
from decision_engine import get_decision_engine, evaluate_decision
from media_adapter import MediaMetadata, set_media_adapter, MediaAdapter
from weather_client import WeatherData, set_weather_client, WeatherClient


class TestPhase2VoiceIntents(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.ctx = MagicMock()
        self.ctx.cfg = self.cfg
        self.ctx.engine.cfg = self.cfg

    def tearDown(self):
        set_media_adapter(None)
        set_weather_client(None)

    def test_01_weather_intent_fetch_and_card(self):
        """'what's the weather' and 'weather forecast' fetch weather and post card to overlay."""
        mock_weather = WeatherData(
            temperature=22.0,
            apparent_temperature=21.0,
            relative_humidity=50,
            precipitation=0.0,
            wind_speed=10.0,
            weather_code=0,
            condition="sunny",
            description="Clear sky",
            city="New York",
            latitude=40.7128,
            longitude=-74.0060,
            is_day=True,
            unit_symbol="°C",
            timestamp=1000.0,
        )
        mock_client = MagicMock(spec=WeatherClient)
        mock_client.get_weather.return_value = mock_weather
        mock_client.get_weather_for_city.return_value = mock_weather
        set_weather_client(mock_client)

        reply = commands.run_command("what's the weather", self.ctx)
        self.assertIn("22 degrees", reply)
        self.assertIn("clear sky", reply.lower())
        self.ctx.overlay.post.assert_any_call("result", text="New York: 22°C, Clear sky", icon="weather", duration=4.0)

        # Also test "weather forecast"
        reply2 = commands.run_command("weather forecast", self.ctx)
        self.assertIn("22 degrees", reply2)

    def test_02_time_intent_and_card(self):
        """'what time is it' and 'what's the time' speak current time and post time card."""
        reply = commands.run_command("what time is it", self.ctx)
        self.assertIn("It is", reply)
        self.ctx.overlay.post.assert_any_call("result", text=unittest.mock.ANY, icon="time", duration=3.0)

        reply2 = commands.run_command("what's the time", self.ctx)
        self.assertIn("It is", reply2)

    def test_03_now_playing_media_card(self):
        """'what's playing' and 'what song is this' post now playing card."""
        mock_adapter = MagicMock(spec=MediaAdapter)
        mock_adapter.get_current_media.return_value = MediaMetadata(
            title="Bohemian Rhapsody",
            artist="Queen",
            status="playing",
            is_playing=True,
        )
        set_media_adapter(mock_adapter)

        reply = commands.run_command("what's playing", self.ctx)
        self.assertIn("Bohemian Rhapsody", reply)
        self.assertIn("Queen", reply)
        self.ctx.overlay.post.assert_any_call("result", text="Bohemian Rhapsody — Queen", icon="media", duration=3.5)

        # When no song is playing
        mock_adapter.get_current_media.return_value = MediaMetadata()
        mock_adapter.refresh_sync.return_value = None
        reply_none = commands.run_command("what song is this", self.ctx)
        self.assertIn("No media is currently", reply_none)

    @patch("pyautogui.press")
    def test_04_media_controls_play_pause_skip(self, mock_press):
        """play, pause, skip, next track, and previous track control media."""
        mock_adapter = MagicMock(spec=MediaAdapter)
        set_media_adapter(mock_adapter)

        # Play / Pause
        reply_play = commands.run_command("play", self.ctx)
        self.assertEqual(reply_play, "Done.")
        mock_adapter.play_pause.assert_called_once()

        reply_pause = commands.run_command("pause", self.ctx)
        self.assertEqual(reply_pause, "Done.")
        self.assertEqual(mock_adapter.play_pause.call_count, 2)

        # Skip / Next track
        reply_skip = commands.run_command("skip", self.ctx)
        self.assertEqual(reply_skip, "Skipping.")
        mock_adapter.next_track.assert_called_once()

        reply_next = commands.run_command("next track", self.ctx)
        self.assertEqual(reply_next, "Skipping.")
        self.assertEqual(mock_adapter.next_track.call_count, 2)

        # Previous track
        reply_prev = commands.run_command("previous track", self.ctx)
        self.assertEqual(reply_prev, "Going back.")
        mock_adapter.previous_track.assert_called_once()
        # Only the media adapter acts: no extra raw media-key presses (they would double-toggle/skip).
        for call in mock_press.call_args_list:
            self.assertNotIn(call.args[0], ("playpause", "nexttrack", "prevtrack"))

    def test_05_show_widgets_expansion(self):
        """'show media' and 'show weather' expand the requested widget."""
        reply_m = commands.run_command("show media", self.ctx)
        self.assertIn("media controls", reply_m)
        self.ctx.overlay.post.assert_any_call("expand_widget", widget="media")

        reply_w = commands.run_command("show weather", self.ctx)
        self.assertIn("weather forecast", reply_w)
        self.ctx.overlay.post.assert_any_call("expand_widget", widget="weather")

    def test_06_hide_island_widgets_toggle(self):
        """'hide the island widgets' and 'hide island' toggle widgets."""
        reply = commands.run_command("hide the island widgets", self.ctx)
        self.assertIn("Toggling", reply)
        self.ctx.overlay.post.assert_any_call("toggle_widgets")

        reply2 = commands.run_command("hide island", self.ctx)
        self.assertIn("Toggling", reply2)

    def test_07_decision_engine_classifies_new_intents(self):
        """Semantic Decision Engine (System 1) identifies new intent prototypes in <1ms."""
        engine = get_decision_engine()

        # Weather
        act, _ = engine.decide("what's the weather")
        self.assertEqual(act, "weather")

        act_f, _ = engine.decide("weather forecast")
        self.assertEqual(act_f, "weather")

        # Time
        act_t, _ = engine.decide("what time is it")
        self.assertEqual(act_t, "time")

        # Now playing
        act_np, _ = engine.decide("what's playing")
        self.assertEqual(act_np, "now_playing")

        # Show widget
        act_sw, _ = engine.decide("show media")
        self.assertEqual(act_sw, "show_widget")

        # Toggle widgets
        act_tw, _ = engine.decide("hide the island widgets")
        self.assertEqual(act_tw, "toggle_widgets")


if __name__ == "__main__":
    unittest.main()
