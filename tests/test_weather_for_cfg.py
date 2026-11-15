"""Voice weather answers use the island's city from Settings, geocoded (not the default coordinates)."""
from types import SimpleNamespace
from unittest import mock

import weather_client


def _client():
    c = mock.Mock()
    c.get_weather_for_city.return_value = "city-weather"
    c.get_weather.return_value = "coord-weather"
    return c


def test_uses_island_city_from_settings():
    cfg = SimpleNamespace(island_weather_city="Lahore", weather_city="New York", island_weather_units="celsius")
    c = _client()
    with mock.patch.object(weather_client, "get_weather_client", return_value=c):
        assert weather_client.weather_for_cfg(cfg) == "city-weather"
    c.get_weather_for_city.assert_called_once_with("Lahore", units="celsius")
    c.get_weather.assert_not_called()


def test_asked_city_wins():
    cfg = SimpleNamespace(island_weather_city="Lahore")
    c = _client()
    with mock.patch.object(weather_client, "get_weather_client", return_value=c):
        weather_client.weather_for_cfg(cfg, city="Tokyo")
    assert c.get_weather_for_city.call_args[0][0] == "Tokyo"


def test_auto_uses_coordinates():
    cfg = SimpleNamespace(island_weather_city="auto", weather_lat=31.5, weather_lon=74.3)
    c = _client()
    with mock.patch.object(weather_client, "get_weather_client", return_value=c):
        assert weather_client.weather_for_cfg(cfg) == "coord-weather"
    c.get_weather_for_city.assert_not_called()
    assert c.get_weather.call_args[0][:2] == (31.5, 74.3)
