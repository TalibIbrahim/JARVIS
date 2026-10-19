"""
tests/test_island_data_feeds.py - data-side island feeds: hardware/weather/fullscreen feeds, config
migration v3, island voice commands (post-only) and assistant status. No Qt, no network, no hardware.
"""
import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import commands
import config as config_mod
import hardware_adapter
import main as jmain
import weather_client
from config import Config, load_config
from weather_client import WeatherClient


class FakeOverlay:
    def __init__(self):
        self.posts = []
        self._lock = threading.Lock()

    def post(self, kind, **kw):
        with self._lock:
            self.posts.append((kind, kw))

    def kinds(self, kind):
        with self._lock:
            return [kw for k, kw in self.posts if k == kind]


class StrictOverlay:
    """Raises on any attribute access except .post."""

    def __init__(self):
        object.__setattr__(self, "posts", [])

    def __getattribute__(self, name):
        if name == "post":
            posts = object.__getattribute__(self, "posts")
            return lambda kind, **kw: posts.append((kind, kw))
        if name == "posts":
            return object.__getattribute__(self, "posts")
        raise AssertionError(f"direct overlay attribute access: {name}")


class FakeEngine:
    def __init__(self, active=True):
        self.active = active
        self._req = None
        self.requests = []

    def request(self, want):
        self.requests.append(want)
        self._req = bool(want)


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def make_ctx(overlay=None, cfg=None, engine=None):
    return jmain.Context(engine or FakeEngine(), overlay or FakeOverlay(), None, threading.Event(), cfg=cfg or Config())


# --------------------------------------------------------------------------- hardware
class TestHardwareFeed(unittest.TestCase):
    def _feed(self):
        ov = FakeOverlay()
        ctx = make_ctx(ov)
        adapter = hardware_adapter.HardwareAdapter(poll_interval=0.02)
        feed = jmain.HardwareFeed(ctx, ov, adapter=adapter)
        return ctx, ov, adapter, feed

    def test_hardware_grid_posts_and_none_pauses(self):
        ctx, ov, adapter, feed = self._feed()
        with mock.patch.object(adapter, "_sample_gpu", return_value=(None, None)):
            feed.start()
            try:
                self.assertTrue(adapter.is_paused())
                time.sleep(0.15)
                self.assertEqual(ov.kinds("hardware_state"), [])
                ctx.set_visible_page("hardware_grid")
                self.assertFalse(adapter.is_paused())
                self.assertTrue(wait_for(lambda: len(ov.kinds("hardware_state")) >= 2))
                ctx.set_visible_page(None)
                self.assertTrue(adapter.is_paused())
                time.sleep(0.1)  # let any in-flight sample finish
                n = len(ov.kinds("hardware_state"))
                time.sleep(0.2)
                self.assertEqual(len(ov.kinds("hardware_state")), n)
            finally:
                feed.stop()

    def test_state_keys_match_contract(self):
        ctx, ov, adapter, feed = self._feed()
        with mock.patch.object(adapter, "_sample_gpu", return_value=(None, None)):
            adapter.add_listener(feed._on_metrics)
            adapter.resume()
            adapter.sample()
        posted = ov.kinds("hardware_state")
        self.assertEqual(len(posted), 1)
        self.assertEqual(
            set(posted[0]),
            {"cpu", "ram_used_gb", "ram_total_gb", "ram_pct", "gpu", "net_up_bps", "net_down_bps", "disk_pct", "fps"},
        )

    def test_gpu_none_when_nvml_and_pdh_fail(self):
        ctx, ov, adapter, feed = self._feed()
        fake_pdh = mock.Mock(side_effect=RuntimeError("no pdh"))
        with mock.patch.object(hardware_adapter, "_import_pynvml", side_effect=ImportError("no nvml")), \
                mock.patch.object(hardware_adapter, "_PdhGpuCounter", fake_pdh):
            adapter.add_listener(feed._on_metrics)
            adapter.resume()
            adapter.sample()
            adapter.sample()
        posted = ov.kinds("hardware_state")
        self.assertEqual(len(posted), 2)
        for p in posted:
            self.assertIsNone(p["gpu"])
            self.assertIsNone(p["fps"])  # no tracker -> unavailable, not 0
        self.assertIsNotNone(posted[0]["ram_total_gb"])  # psutil values are real


# --------------------------------------------------------------------------- weather
OPEN_METEO = {
    "current": {
        "temperature_2m": 21.5, "relative_humidity_2m": 63, "apparent_temperature": 20.1, "is_day": 1,
        "precipitation": 0.0, "weather_code": 2, "wind_speed_10m": 14.4, "wind_direction_10m": 225.0,
    },
    "hourly": {"time": ["2026-01-01T10:00", "2026-01-01T11:00"], "temperature_2m": [22.0, 23.0], "weather_code": [0, 3]},
}
PRE_EXISTING = {"temp", "condition", "icon", "high", "low", "feels_like", "city", "hourly", "unit_symbol"}


def _client():
    def fetch(url, timeout=6.0):
        return json.loads(json.dumps(OPEN_METEO))
    return WeatherClient(http_fetcher=fetch)


class TestWeather(unittest.TestCase):
    def test_payload_has_new_and_old_keys(self):
        w = _client().get_weather(51.5, -0.12, city="London, UK", units="celsius")
        p = weather_client.weather_state_payload(w)
        self.assertTrue(PRE_EXISTING <= set(p))
        for k in ("wind_kmh", "wind_dir_deg", "humidity", "feels_like"):
            self.assertIn(k, p)
        self.assertEqual(p["wind_kmh"], 14.4)
        self.assertEqual(p["wind_dir_deg"], 225.0)
        self.assertEqual(p["humidity"], 63.0)
        self.assertEqual(p["feels_like"], 20.1)
        self.assertEqual(p["city"], "London")

    def test_imperial_wind_converted_to_kmh(self):
        w = _client().get_weather(40.7, -74.0, city="NYC", units="fahrenheit")
        p = weather_client.weather_state_payload(w)
        self.assertAlmostEqual(p["wind_kmh"], round(14.4 * 1.609344, 1))

    def test_cache_roundtrip_atomic_stale_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"APPDATA": tmp}):
                path = weather_client.weather_cache_path()
                self.assertTrue(path.startswith(tmp))
                payload = weather_client.weather_state_payload(_client().get_weather(1, 2, city="Paris", units="celsius"))
                t0 = 1_000_000.0
                with mock.patch("os.replace", wraps=os.replace) as rep:
                    self.assertTrue(weather_client.save_weather_cache(payload, "Paris", "celsius", now=t0))
                    rep.assert_called_once()
                self.assertFalse(os.path.exists(path + ".tmp"))

                fresh = weather_client.load_weather_cache("Paris", "celsius", now=t0 + 60)
                self.assertFalse(fresh["stale"])
                self.assertEqual(fresh["payload"]["wind_kmh"], payload["wind_kmh"])
                self.assertEqual(fresh["saved_at"], t0)
                # case-insensitive city match
                self.assertIsNotNone(weather_client.load_weather_cache("paris", "celsius", now=t0 + 60))
                self.assertFalse(weather_client.load_weather_cache("Paris", "celsius", now=t0 + 3600)["stale"])
                self.assertTrue(weather_client.load_weather_cache("Paris", "celsius", now=t0 + 3601)["stale"])
                self.assertIsNone(weather_client.load_weather_cache("Berlin", "celsius", now=t0))
                self.assertIsNone(weather_client.load_weather_cache("Paris", "fahrenheit", now=t0))

    def test_corrupt_or_missing_cache_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "w.json")
            self.assertIsNone(weather_client.load_weather_cache("X", "auto", path=path))
            with open(path, "w") as f:
                f.write("{not json")
            self.assertIsNone(weather_client.load_weather_cache("X", "auto", path=path))

    def test_failed_write_leaves_previous_cache_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "w.json")
            weather_client.save_weather_cache({"temp": 1}, "X", "auto", now=5.0, path=path)
            with mock.patch("os.replace", side_effect=OSError("boom")):
                self.assertFalse(weather_client.save_weather_cache({"temp": 2}, "X", "auto", now=6.0, path=path))
            self.assertEqual(weather_client.load_weather_cache("X", "auto", now=6.0, path=path)["payload"], {"temp": 1})


# --------------------------------------------------------------------------- config migration v3
def _write(tmp, d):
    path = os.path.join(tmp, "jarvis_config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    return path


class TestConfigMigrationV3(unittest.TestCase):
    def test_aliases_map_to_canonical(self):
        old = {
            "config_version": 2,
            "island_time_format": "24h",
            "idle_display": "time",
            "island_size": "large",
            "expand_trigger": "click",
            "hide_fullscreen": False,
            "weather_city": "Paris",
            "weather_units": "celsius",
            "show_weather": False,
            "show_now_playing": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(_write(tmp, old))
        self.assertEqual(cfg.island_clock_format, "24h")
        self.assertEqual(cfg.island_idle_display, "time")
        self.assertEqual(cfg.island_size_scale, "large")
        self.assertEqual(cfg.island_expand_trigger, "click")
        self.assertFalse(cfg.island_hide_fullscreen)
        self.assertEqual(cfg.island_weather_city, "Paris")
        self.assertEqual(cfg.island_weather_units, "celsius")
        self.assertFalse(cfg.island_show_weather)
        self.assertFalse(cfg.island_show_now_playing)
        self.assertGreaterEqual(cfg.config_version, 3)

    def test_size_alias_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(_write(tmp, {"config_version": 2, "island_size": "compact"}))
        self.assertEqual(cfg.island_size_scale, "small")

    def test_user_set_canonical_not_overwritten(self):
        old = {"config_version": 2, "island_time_format": "12h", "island_clock_format": "24h",
               "weather_city": "Paris", "island_weather_city": "Tokyo"}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(_write(tmp, old))
        self.assertEqual(cfg.island_clock_format, "24h")
        self.assertEqual(cfg.island_weather_city, "Tokyo")

    def test_media_aware_becomes_time_weather(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(_write(tmp, {"config_version": 2, "island_compact_mode": "media_aware"}))
            self.assertEqual(cfg.island_idle_display, "time_weather")
            cfg2 = load_config(_write(tmp, {"config_version": 3, "island_idle_display": "media_aware"}))
            self.assertEqual(cfg2.island_idle_display, "time_weather")

    def test_migration_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"config_version": 2, "island_time_format": "24h"})
            cfg = load_config(path)
            self.assertEqual(cfg.island_clock_format, "24h")
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertGreaterEqual(saved["config_version"], 3)
            # user later changes the canonical key; the alias is still on disk but must not win again
            saved["island_clock_format"] = "12h"
            saved["island_time_format"] = "24h"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(saved, f)
            with mock.patch.object(config_mod, "_migrate_island_keys", wraps=config_mod._migrate_island_keys) as spy:
                cfg2 = load_config(path)
                spy.assert_not_called()
            self.assertEqual(cfg2.island_clock_format, "12h")


# --------------------------------------------------------------------------- voice commands
class TestIslandVoicePostsOnly(unittest.TestCase):
    CASES = [
        ("next page", "island_page_step", {"direction": 1}),
        ("previous page", "island_page_step", {"direction": -1}),
        ("show weather", "island_show_page", {"page": "weather"}),
        ("expand island", "island_expand", {}),
        ("collapse island", "island_collapse", {}),
    ]

    def test_only_contract_posts(self):
        for text, kind, kw in self.CASES:
            with self.subTest(text=text):
                ov = StrictOverlay()
                ctx = types.SimpleNamespace(overlay=ov)
                reply = commands.handle_island_voice_command(text, ctx)
                self.assertTrue(reply)
                posts = ov.posts
                self.assertIn((kind, kw), posts)
                for k, _ in posts:
                    self.assertTrue(k.startswith("island_") or k in ("expand_widget", "action_executed"), k)


# --------------------------------------------------------------------------- fullscreen watcher
class TestFullscreenWatcher(unittest.TestCase):
    def _run(self, cfg, probe, duration):
        ov = FakeOverlay()
        ctx = make_ctx(ov, cfg)
        stop = threading.Event()
        t = jmain.start_fullscreen_watcher(ctx, ov, stop, interval=0.01, probe=probe)
        return ov, stop, t

    def test_posts_only_on_change(self):
        seq = [False, False, True, True, True, False, False]
        calls = []

        def probe():
            calls.append(1)
            return seq[min(len(calls) - 1, len(seq) - 1)]

        ov, stop, t = self._run(Config(), probe, 0)
        try:
            self.assertTrue(wait_for(lambda: len(calls) >= len(seq) + 3))
        finally:
            stop.set()
            t.join(2)
        self.assertEqual([kw["active"] for kw in ov.kinds("fullscreen")], [True, False])

    def test_skipped_when_hide_fullscreen_false(self):
        cfg = Config()
        cfg.island_hide_fullscreen = False
        probe = mock.Mock(return_value=True)
        ov, stop, t = self._run(cfg, probe, 0)
        time.sleep(0.2)
        stop.set()
        t.join(2)
        probe.assert_not_called()
        self.assertEqual(ov.kinds("fullscreen"), [])

    def test_disabling_clears_active_state(self):
        cfg = Config()
        probe = mock.Mock(return_value=True)
        ov, stop, t = self._run(cfg, probe, 0)
        try:
            self.assertTrue(wait_for(lambda: ov.kinds("fullscreen") == [{"active": True}]))
            cfg.island_hide_fullscreen = False
            self.assertTrue(wait_for(lambda: ov.kinds("fullscreen") == [{"active": True}, {"active": False}]))
        finally:
            stop.set()
            t.join(2)


# --------------------------------------------------------------------------- tracking + assistant status
class TestTrackingAndAssistantStatus(unittest.TestCase):
    def test_tracking_paused_maps_to_engine_request(self):
        eng = FakeEngine(active=True)
        ctx = make_ctx(engine=eng)
        self.assertFalse(ctx.is_tracking_paused())
        ctx.set_tracking_paused(True)
        self.assertEqual(eng.requests, [False])
        self.assertTrue(ctx.is_tracking_paused())
        ctx.set_tracking_paused(False)
        self.assertEqual(eng.requests, [False, True])
        self.assertFalse(ctx.is_tracking_paused())

    def test_is_paused_follows_engine_active_without_pending_request(self):
        ctx = make_ctx(engine=FakeEngine(active=False))
        self.assertTrue(ctx.is_tracking_paused())

    def test_assistant_status_never_echoes_transcript(self):
        ov = FakeOverlay()
        ctx = make_ctx(ov)
        ctx.observe_overlay(ov)
        secret = "my secret transcript " * 20
        ctx.overlay.post("live_speech", text=secret)
        ctx.overlay.post("say", text=secret, speak=False)
        ctx.overlay.post("action_executed", action="island_show_page", success=True, text=secret)
        ctx.overlay.post("action_executed", action="a_very_long_action_name_" * 5, success=False, text=secret)
        st = ctx.assistant_status()
        self.assertEqual(set(st), {"state", "last_command", "last_result"})
        self.assertLessEqual(len(st["last_command"]), 40)
        self.assertEqual(st["last_result"], "fail")
        self.assertNotIn("secret", json.dumps(st))
        self.assertNotIn("transcript", json.dumps(st))

    def test_state_listening_and_standby(self):
        ov = FakeOverlay()
        eng = FakeEngine(active=True)
        ctx = make_ctx(ov, engine=eng)
        ctx.observe_overlay(ov)
        self.assertEqual(ctx.assistant_status()["state"], "idle")
        ctx.overlay.post("live_speech", text="hello")
        self.assertEqual(ctx.assistant_status()["state"], "listening")
        ctx.overlay.post("listening_done")
        ctx.set_tracking_paused(True)
        self.assertEqual(ctx.assistant_status()["state"], "standby")


if __name__ == "__main__":
    unittest.main()
