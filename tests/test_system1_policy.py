"""Regression tests for the System 1 acceptance policy (decision_engine)."""
import pytest

from decision_engine import apply_decision_policy, get_decision_engine


@pytest.fixture(scope="module")
def engine():
    return get_decision_engine()


@pytest.mark.parametrize("text", ["that's interesting", "the forest is nice"])
def test_chatter_gives_no_match(engine, text):
    assert engine.classify(text).intent is None


def test_please_lock_my_pc(engine):
    assert engine.classify("please lock my pc").intent == "lock_workstation"


def test_launch_spotify_for_me(engine):
    m = engine.classify("launch spotify for me")
    assert m.intent == "open_app"
    assert m.args.get("app") == "spotify"


def test_weather_phrase(engine):
    assert engine.classify("what's the weather like").intent == "weather"


def test_side_effect_at_half_score_rejected():
    assert apply_decision_policy("mute", 0.5, 0.5, "mute it") is None


def test_read_only_at_half_score_small_margin_accepted():
    assert apply_decision_policy("time", 0.5, 0.05, "what time") == "time"
