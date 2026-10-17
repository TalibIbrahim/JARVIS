"""
tests/test_weather_client.py — Unit tests for Open-Meteo Weather Client.

Verifies:
  - Geocoding parsing and caching.
  - Forecast endpoint parsing (current temperature, apparent temp, humidity, wind, precipitation).
  - WMO canonical condition mapping ("sunny", "cloudy", "rainy", "storm", "snow", etc.).
  - 20-minute forecast caching and cache hits.
  - Offline fallback: returns cached forecast with stale=True when network fails.
  - Graceful degradation: returns None when network fails and no cache exists.
  - Manual and auto unit resolution (Celsius / Fahrenheit).
  - Zero Qt imports; 100% unit-testable with mock HTTP fetcher.
"""

import time
import unittest

from weather_client import (
    WeatherClient,
    WeatherData,
    canonicalize_condition,
    WMO_CODE_MAP,
)


class MockFetcher:
    """Mock HTTP fetcher returning preconfigured JSON responses."""

    def __init__(self):
        self.calls = []
        self.should_fail = False

        self.geocode_resp = {
            "results": [
                {
                    "name": "London",
                    "latitude": 51.5085,
                    "longitude": -0.1257,
                    "admin1": "England",
                    "country": "United Kingdom",
                }
            ]
        }
        self.forecast_resp = {
            "latitude": 51.5085,
            "longitude": -0.1257,
            "current": {
                "time": "2026-10-17T12:00",
                "temperature_2m": 15.2,
                "apparent_temperature": 14.1,
                "relative_humidity_2m": 72,
                "is_day": 1,
                "precipitation": 0.0,
                "weather_code": 2,
                "wind_speed_10m": 12.5,
            },
            "hourly": {
                "time": ["2026-10-17T12:00", "2026-10-17T13:00"],
                "temperature_2m": [15.2, 15.8],
                "weather_code": [2, 3],
            },
        }

    def __call__(self, url: str, timeout: float = 6.0):
        self.calls.append(url)
        if self.should_fail:
            raise ConnectionError("Simulated network offline")
        if "geocoding-api" in url:
            return self.geocode_resp
        return self.forecast_resp


class TestWeatherClient(unittest.TestCase):
    def setUp(self):
        self.clock_val = 1000.0
        self.clock = lambda: self.clock_val
        self.fetcher = MockFetcher()
        self.client = WeatherClient(
            http_fetcher=self.fetcher,
            cache_ttl=1200.0,
            clock_fn=self.clock,
        )

    def test_01_canonical_condition_mapping(self):
        """Verifies canonical condition translation across all major WMO code bands."""
        cond_0, desc_0 = canonicalize_condition(0, is_day=True)
        self.assertEqual(cond_0, "sunny")
        self.assertEqual(desc_0, "Clear sky")

        # Nighttime clear sky becomes "clear"
        cond_night, _ = canonicalize_condition(0, is_day=False)
        self.assertEqual(cond_night, "clear")

        cond_2, _ = canonicalize_condition(2, is_day=True)
        self.assertEqual(cond_2, "cloudy")

        cond_61, _ = canonicalize_condition(61, is_day=True)
        self.assertEqual(cond_61, "rainy")

        cond_71, _ = canonicalize_condition(71, is_day=True)
        self.assertEqual(cond_71, "snow")

        cond_95, _ = canonicalize_condition(95, is_day=True)
        self.assertEqual(cond_95, "storm")

        cond_45, _ = canonicalize_condition(45, is_day=True)
        self.assertEqual(cond_45, "foggy")

    def test_02_geocoding_and_caching(self):
        """Resolves city to lat/lon and caches the result."""
        res = self.client.geocode("London")
        self.assertIsNotNone(res)
        lat, lon, name = res
        self.assertAlmostEqual(lat, 51.5085, places=3)
        self.assertAlmostEqual(lon, -0.1257, places=3)
        self.assertIn("London", name)
        self.assertEqual(len(self.fetcher.calls), 1)

        # Second call uses in-memory cache without hitting fetcher
        res2 = self.client.geocode("London")
        self.assertEqual(res, res2)
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_03_forecast_parsing_celsius(self):
        """Fetches and parses current conditions in Celsius."""
        data = self.client.get_weather(51.5085, -0.1257, city="London", units="celsius")
        self.assertIsNotNone(data)
        self.assertAlmostEqual(data.temperature, 15.2)
        self.assertAlmostEqual(data.apparent_temperature, 14.1)
        self.assertEqual(data.relative_humidity, 72)
        self.assertEqual(data.condition, "cloudy")
        self.assertEqual(data.unit_symbol, "°C")
        self.assertFalse(data.stale)
        self.assertEqual(len(data.hourly), 2)

    def test_04_forecast_parsing_fahrenheit(self):
        """Units fahrenheit requests imperial parameters and uses °F symbol."""
        data = self.client.get_weather(40.7128, -74.0060, city="New York", units="fahrenheit")
        self.assertIsNotNone(data)
        self.assertEqual(data.unit_symbol, "°F")
        self.assertIn("temperature_unit=fahrenheit", self.fetcher.calls[-1])

    def test_05_cache_hit_within_20_minutes(self):
        """Requests within 20 minutes reuse cached forecast."""
        self.clock_val = 1000.0
        self.client.get_weather(51.5085, -0.1257, units="celsius")
        self.assertEqual(len(self.fetcher.calls), 1)

        # 10 minutes later (t=1600s): cache hit
        self.clock_val = 1600.0
        data2 = self.client.get_weather(51.5085, -0.1257, units="celsius")
        self.assertIsNotNone(data2)
        self.assertFalse(data2.stale)
        self.assertEqual(len(self.fetcher.calls), 1)

        # 25 minutes later (t=2501s): cache expired, fetches again
        self.clock_val = 2501.0
        data3 = self.client.get_weather(51.5085, -0.1257, units="celsius")
        self.assertIsNotNone(data3)
        self.assertEqual(len(self.fetcher.calls), 2)

    def test_06_offline_fallback_returns_stale_cache(self):
        """When network is offline but cache exists, returns cached data with stale=True."""
        self.clock_val = 1000.0
        # Warm cache
        self.client.get_weather(51.5085, -0.1257, units="celsius")

        # Network goes offline and cache expires
        self.clock_val = 3000.0
        self.fetcher.should_fail = True

        data = self.client.get_weather(51.5085, -0.1257, units="celsius")
        self.assertIsNotNone(data)
        self.assertTrue(data.stale)
        self.assertAlmostEqual(data.temperature, 15.2)

    def test_07_offline_without_cache_gracefully_returns_none(self):
        """When network fails and no cached data exists, gracefully returns None."""
        self.fetcher.should_fail = True
        data = self.client.get_weather(12.34, 56.78, units="celsius")
        self.assertIsNone(data)

    def test_08_auto_detect_location_and_auto_city(self):
        """Auto geolocation resolves 'auto' or 'current location' to detected coordinates."""
        # Mock fetcher responses for ip-api.com
        orig_fetch = self.fetcher.__call__

        def custom_fetch(url, timeout=6.0):
            if "ip-api.com" in url or "ipapi.co" in url:
                return {
                    "status": "success",
                    "city": "Lahore",
                    "country": "Pakistan",
                    "lat": 31.5826,
                    "lon": 74.3276,
                }
            return orig_fetch(url, timeout)

        self.client._fetcher = custom_fetch
        geo = self.client.geocode("auto")
        self.assertIsNotNone(geo)
        lat, lon, display = geo
        self.assertAlmostEqual(lat, 31.5826)
        self.assertAlmostEqual(lon, 74.3276)
        self.assertIn("Lahore", display)

        # get_weather_for_city("auto") should fetch weather for Lahore
        w = self.client.get_weather_for_city("auto")
        self.assertIsNotNone(w)
        self.assertIn("Lahore", w.city)


if __name__ == "__main__":
    unittest.main()
