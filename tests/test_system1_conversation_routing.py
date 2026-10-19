"""Conversation utterances must not be grabbed by System 1 read-only intents; real queries still are."""
from unittest.mock import MagicMock, patch

import pytest

import commands
from decision_engine import get_decision_engine


@pytest.fixture(scope="module")
def engine():
    return get_decision_engine()


@pytest.mark.parametrize("text", [
    "tell me a joke", "tell me something", "tell me a story", "tell me a fun fact",
    "how are you", "what can you do",
])
def test_conversation_gives_no_system1_action(engine, text):
    assert engine.classify(text).intent is None


@pytest.mark.parametrize("text,intent", [
    ("what time is it", "time"),
    ("tell me the time", "time"),
    ("what's playing", "now_playing"),
    ("what's the weather like", "weather"),
    ("what is 5 plus 3", "calculate"),
    ("what time is it in tokyo", "world_time"),
])
def test_real_queries_still_match(engine, text, intent):
    assert engine.classify(text).intent == intent


def test_run_command_joke_goes_to_system2_not_time():
    ctx = MagicMock()
    ctx.cfg.local_decision_engine = True
    ctx.engine.cfg.local_decision_engine = True
    with patch.object(commands, "_handle_ai_query", return_value="Why did the chicken...") as ai, \
         patch.object(commands, "_post_action") as post:
        reply = commands.run_command("tell me a joke", ctx)
    assert reply == "Why did the chicken..."
    ai.assert_called_once()
    assert not any(c.args[1] == "time" for c in post.call_args_list)
