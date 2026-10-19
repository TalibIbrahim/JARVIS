"""Defaults and round-trip of the lyrics / ducking / one-hand-zoom config fields."""
import dataclasses

import pytest

from config import Config

DEFAULTS = {
    "island_lyrics_enabled": False,
    "island_lyrics_in_pill": True,
    "gesture_one_hand_zoom": True,
    "media_duck_on_wake": True,
}


@pytest.mark.parametrize("name,expected", sorted(DEFAULTS.items()))
def test_default_value(name, expected):
    val = getattr(Config(), name)
    assert val is expected


@pytest.mark.parametrize("name", sorted(DEFAULTS))
def test_field_is_a_bool_dataclass_field(name):
    fields = {f.name: f for f in dataclasses.fields(Config)}
    assert name in fields
    assert fields[name].type in (bool, "bool")


@pytest.mark.parametrize("name,expected", sorted(DEFAULTS.items()))
def test_override_via_constructor(name, expected):
    assert getattr(Config(**{name: not expected}), name) is (not expected)
