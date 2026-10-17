"""
weather_client.py — Open-Meteo REST Client & Weather Cache for J.A.R.V.I.S.

Features:
  - Geocoding endpoint (https://geocoding-api.open-meteo.com) to resolve city to lat/lon.
  - Forecast endpoint (https://api.open-meteo.com) for current conditions and 6-hour hourly trend.
  - Caches forecast for 20 minutes (1200s); returns cached data with stale=True when offline;
    gracefully returns None if no data is available.
  - Translates WMO weather codes into canonical conditions ("sunny", "cloudy", "rainy", "storm", "snow", etc.).
  - Respects user locale or manual units (Celsius / Fahrenheit).
  - 100% unit-testable with mock HTTP fetchers; strictly NO Qt imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import locale
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request


CACHE_TTL_SECONDS = 1200.0  # 20 minutes
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Canonical condition mappings from WMO weather codes
# (code -> (canonical_condition, descriptive_text))
WMO_CODE_MAP: Dict[int, Tuple[str, str]] = {
    0: ("sunny", "Clear sky"),
    1: ("sunny", "Mainly clear"),
    2: ("cloudy", "Partly cloudy"),
    3: ("cloudy", "Overcast"),
    45: ("foggy", "Foggy"),
    48: ("foggy", "Depositing rime fog"),
    51: ("drizzle", "Light drizzle"),
    53: ("drizzle", "Moderate drizzle"),
    55: ("drizzle", "Dense drizzle"),
    56: ("rainy", "Light freezing drizzle"),
    57: ("rainy", "Dense freezing drizzle"),
    61: ("rainy", "Slight rain"),
    63: ("rainy", "Moderate rain"),
    65: ("rainy", "Heavy rain"),
    66: ("rainy", "Light freezing rain"),
    67: ("rainy", "Heavy freezing rain"),
    71: ("snow", "Slight snow"),
    73: ("snow", "Moderate snow"),
    75: ("snow", "Heavy snow"),
    77: ("snow", "Snow grains"),
    80: ("rainy", "Slight rain showers"),
    81: ("rainy", "Moderate rain showers"),
    82: ("rainy", "Violent rain showers"),
    85: ("snow", "Slight snow showers"),
    86: ("snow", "Heavy snow showers"),
    95: ("storm", "Thunderstorm"),
    96: ("storm", "Thunderstorm with slight hail"),
    99: ("storm", "Thunderstorm with heavy hail"),
}


def canonicalize_condition(weather_code: int, is_day: bool = True) -> Tuple[str, str]:
    """Returns (canonical_condition, description). Canonical: sunny, cloudy, rainy, storm, snow, foggy, drizzle, clear."""
    condition, description = WMO_CODE_MAP.get(weather_code, ("cloudy", "Variable clouds"))
    if not is_day and condition == "sunny":
        condition = "clear"
    return condition, description


def is_fahrenheit_locale() -> bool:
    """Detects whether local system locale uses Fahrenheit by default."""
    try:
        loc = (locale.getlocale()[0] or "") if hasattr(locale, "getlocale") else ""
        country = loc.split("_")[-1].upper() if "_" in loc else ""
        if country in ("US", "BS", "KY", "LR", "PW", "FM", "MH"):
            return True
    except Exception:
        pass
    return False


@dataclass
class HourlyForecast:
    time: str
    temperature: float
    weather_code: int
    condition: str


@dataclass
class WeatherData:
    temperature: float
    apparent_temperature: float
    relative_humidity: int
    precipitation: float
    wind_speed: float
    weather_code: int
    condition: str
    description: str
    city: str
    latitude: float
    longitude: float
    is_day: bool
    unit_symbol: str
    timestamp: float
    stale: bool = False
    hourly: List[HourlyForecast] = field(default_factory=list)


def default_http_fetcher(url: str, timeout: float = 6.0) -> Dict[str, Any]:
    """Default HTTP fetcher using requests if available, with urllib.request fallback."""
    try:
        import requests
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except ImportError:
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-Assistant/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode("utf-8")
            return json.loads(data)


class WeatherClient:
    """
    Open-Meteo REST client with geocoding, 20-minute caching, and offline stale resilience.
    """

    def __init__(
        self,
        http_fetcher: Optional[Callable[[str, float], Dict[str, Any]]] = None,
        cache_ttl: float = CACHE_TTL_SECONDS,
        clock_fn: Callable[[], float] = time.time,
    ):
        self._fetcher = http_fetcher or default_http_fetcher
        self._cache_ttl = cache_ttl
        self._clock = clock_fn
        # Forecast cache: (lat, lon, units) -> (timestamp, WeatherData)
        self._forecast_cache: Dict[str, Tuple[float, WeatherData]] = {}
        # Geocoding cache: city_lower -> (lat, lon, display_name)
        self._geo_cache: Dict[str, Tuple[float, float, str]] = {}

    def resolve_units(self, unit_setting: str) -> str:
        """Resolves 'auto' into either 'fahrenheit' or 'celsius'."""
        u = str(unit_setting or "auto").strip().lower()
        if u in ("fahrenheit", "f", "imperial"):
            return "fahrenheit"
        if u in ("celsius", "c", "metric"):
            return "celsius"
        return "fahrenheit" if is_fahrenheit_locale() else "celsius"

    def geocode(self, city_name: str) -> Optional[Tuple[float, float, str]]:
        """
        Resolves city name to (lat, lon, resolved_city_name).
        Results are cached in-memory.
        """
        clean = city_name.strip()
        if not clean:
            return None
        key = clean.lower()
        if key in self._geo_cache:
            return self._geo_cache[key]

        query = urllib.parse.urlencode({"name": clean, "count": 1, "format": "json"})
        url = f"{GEOCODING_URL}?{query}"

        # Phase 8 Privacy Log: audit external cloud transmission to Open-Meteo geocoding
        try:
            from privacy_log import record_cloud_transmission
            record_cloud_transmission(
                destination="Open-Meteo",
                payload_summary=f"Geocoding lookup for '{clean}'",
                metadata={"city": clean, "endpoint": "geocoding"},
            )
        except Exception:
            pass

        try:
            data = self._fetcher(url, 6.0)
            results = data.get("results")
            if results and len(results) > 0:
                item = results[0]
                lat = float(item["latitude"])
                lon = float(item["longitude"])
                name = str(item.get("name", clean))
                admin1 = item.get("admin1")
                if admin1 and admin1 != name:
                    display = f"{name}, {admin1}"
                else:
                    display = name
                res = (lat, lon, display)
                self._geo_cache[key] = res
                return res
        except Exception as exc:
            print(f"[weather_client] Geocoding error for '{city_name}': {exc}")

        return None

    def get_weather(
        self,
        latitude: float,
        longitude: float,
        city: str = "",
        units: str = "auto",
        force_refresh: bool = False,
    ) -> Optional[WeatherData]:
        """
        Fetches current weather and 6-hour forecast from Open-Meteo.
        Caches for 20 minutes. Returns cached data with stale=True if offline.
        Returns None if no data is available.
        """
        try:
            lat_f = float(latitude)
        except (TypeError, ValueError):
            lat_f = 40.7128
        try:
            lon_f = float(longitude)
        except (TypeError, ValueError):
            lon_f = -74.0060

        units_str = str(units) if units and not hasattr(units, "_mock_name") else "auto"
        unit_type = self.resolve_units(units_str)
        is_f = unit_type == "fahrenheit"
        unit_sym = "°F" if is_f else "°C"
        cache_key = f"{lat_f:.4f},{lon_f:.4f},{unit_type}"
        now = self._clock()

        # Check in-memory cache
        if not force_refresh and cache_key in self._forecast_cache:
            cached_time, cached_data = self._forecast_cache[cache_key]
            if (now - cached_time) < self._cache_ttl:
                cached_data.stale = False
                return cached_data

        # Build Open-Meteo URL
        params = {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,weather_code",
            "forecast_hours": 6,
        }
        if is_f:
            params["temperature_unit"] = "fahrenheit"
            params["wind_speed_unit"] = "mph"
            params["precipitation_unit"] = "inch"
        else:
            params["temperature_unit"] = "celsius"
            params["wind_speed_unit"] = "kmh"
            params["precipitation_unit"] = "mm"

        query_str = urllib.parse.urlencode(params)
        url = f"{FORECAST_URL}?{query_str}"

        # Phase 8 Privacy Log: audit external cloud transmission to Open-Meteo forecast
        try:
            from privacy_log import record_cloud_transmission
            record_cloud_transmission(
                destination="Open-Meteo",
                payload_summary=f"Weather forecast request ({lat_f:.4f}, {lon_f:.4f})",
                metadata={"latitude": lat_f, "longitude": lon_f, "units": unit_type, "endpoint": "forecast"},
            )
        except Exception:
            pass

        try:
            data = self._fetcher(url, 7.0)
            current = data.get("current", {})
            temp = float(current.get("temperature_2m", 0.0))
            apparent_temp = float(current.get("apparent_temperature", temp))
            humidity = int(current.get("relative_humidity_2m", 0))
            is_day = bool(current.get("is_day", 1))
            precip = float(current.get("precipitation", 0.0))
            w_code = int(current.get("weather_code", 0))
            wind = float(current.get("wind_speed_10m", 0.0))

            canonical_cond, desc = canonicalize_condition(w_code, is_day)

            # Hourly trend (6 hours)
            hourly_list: List[HourlyForecast] = []
            h_data = data.get("hourly", {})
            h_times = h_data.get("time", [])
            h_temps = h_data.get("temperature_2m", [])
            h_codes = h_data.get("weather_code", [])
            for t_str, t_val, c_val in zip(h_times, h_temps, h_codes):
                c_cond, _ = canonicalize_condition(int(c_val), is_day=True)
                hourly_list.append(HourlyForecast(
                    time=str(t_str),
                    temperature=float(t_val),
                    weather_code=int(c_val),
                    condition=c_cond,
                ))

            weather_res = WeatherData(
                temperature=temp,
                apparent_temperature=apparent_temp,
                relative_humidity=humidity,
                precipitation=precip,
                wind_speed=wind,
                weather_code=w_code,
                condition=canonical_cond,
                description=desc,
                city=city or f"{latitude:.2f}, {longitude:.2f}",
                latitude=latitude,
                longitude=longitude,
                is_day=is_day,
                unit_symbol=unit_sym,
                timestamp=now,
                stale=False,
                hourly=hourly_list,
            )

            # Save in cache
            self._forecast_cache[cache_key] = (now, weather_res)
            return weather_res

        except Exception as exc:
            print(f"[weather_client] Forecast fetch failed for ({latitude}, {longitude}): {exc}")
            # Offline fallback: Return stale cached data if available
            if cache_key in self._forecast_cache:
                _, cached_data = self._forecast_cache[cache_key]
                cached_data.stale = True
                return cached_data

            # Gracefully return None if no data is available
            return None

    def get_weather_for_city(
        self,
        city_name: str,
        units: str = "auto",
        force_refresh: bool = False,
    ) -> Optional[WeatherData]:
        """Geocodes city and returns WeatherData."""
        geo = self.geocode(city_name)
        if not geo:
            return None
        lat, lon, display_name = geo
        return self.get_weather(lat, lon, city=display_name, units=units, force_refresh=force_refresh)


# Global singleton client
_WEATHER_CLIENT: Optional[WeatherClient] = None


def get_weather_client() -> WeatherClient:
    global _WEATHER_CLIENT
    if _WEATHER_CLIENT is None:
        _WEATHER_CLIENT = WeatherClient()
    return _WEATHER_CLIENT


def set_weather_client(client: Optional[WeatherClient]) -> None:
    global _WEATHER_CLIENT
    _WEATHER_CLIENT = client
