"""Site search, gesture-mode voice commands, shared Groq limiter/caches/usage, and the local-first Q&A route."""
import json
import os
import time
import urllib.parse
from types import SimpleNamespace as NS
from unittest import mock

import numpy as np
import pytest

import ai
import chained_commands
import commands
from config import Config


# ----------------------------------------------------------------------------- fixtures

@pytest.fixture
def web(monkeypatch):
    opened = []
    monkeypatch.setattr(commands.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    monkeypatch.setattr(commands, "is_spotify_available", lambda: False)
    import context_resolver
    context_resolver.reset_context()
    return opened


def _ctx(**extra):
    cfg = Config()
    cfg.ai_enabled = False
    base = dict(engine=NS(cfg=cfg, active=True), cfg=cfg, overlay=None)
    base.update(extra)
    return NS(**base)


def _q(url, param):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)[param][0]


# ----------------------------------------------------------------------------- 1. site search

SITE_CASES = [
    ("search elden ring malenia guide on youtube", "youtube", "elden ring malenia guide"),
    ("search for elden ring malenia guide on youtube", "youtube", "elden ring malenia guide"),
    ("jarvis search elden ring malenia guide on youtube", "youtube", "elden ring malenia guide"),
    ("can you search elden ring malenia guide on youtube please", "youtube", "elden ring malenia guide"),
    ("find elden ring malenia guide on youtube", "youtube", "elden ring malenia guide"),
    ("look up elden ring malenia guide on you tube", "youtube", "elden ring malenia guide"),
    ("show me elden ring malenia guide on yt", "youtube", "elden ring malenia guide"),
    ("pull up elden ring malenia guide on youtube", "youtube", "elden ring malenia guide"),
    ("google elden ring malenia guide on youtube", "youtube", "elden ring malenia guide"),
    ("search in youtube for elden ring malenia guide", "youtube", "elden ring malenia guide"),
    ("search youtube for elden ring malenia guide", "youtube", "elden ring malenia guide"),
    ("youtube search elden ring malenia guide", "youtube", "elden ring malenia guide"),
    ("yt search for elden ring malenia guide", "youtube", "elden ring malenia guide"),
    ("search youtube elden ring malenia guide", "youtube", "elden ring malenia guide"),
    ("watch elden ring boss fights on youtube", "youtube", "elden ring boss fights"),
    ("watch videos of elden ring boss fights on youtube", "youtube", "elden ring boss fights"),
    ("how to beat malenia on youtube", "youtube", "how to beat malenia"),
    ("look up rust lifetimes on stack overflow", "stackoverflow", "rust lifetimes"),
    ("search reddit for budget mechanical keyboards", "reddit", "budget mechanical keyboards"),
    ("find a 4090 on amazon", "amazon", "a 4090"),
    ("search for hollow knight on steam", "steam", "hollow knight"),
    ("search hollow knight in the steam store", "steam", "hollow knight"),
    ("look up the batman on imdb", "imdb", "the batman"),
    ("search nikola tesla on wikipedia", "wikipedia", "nikola tesla"),
    ("search coffee shops on google maps", "maps", "coffee shops"),
    ("search red pandas on google images", "images", "red pandas"),
    ("show me pictures of red pandas", "images", "red pandas"),
    ("search elections on google news", "news", "elections"),
    ("search pytorch on github", "github", "pytorch"),
    ("search speedruns on twitch", "twitch", "speedruns"),
    ("search vintage cameras on ebay", "ebay", "vintage cameras"),
    ("search cats on bing", "bing", "cats"),
    ("search privacy tools on duck duck go", "duckduckgo", "privacy tools"),
    ("search spacex on x", "twitter", "spacex"),
    ("search spacex on twitter", "twitter", "spacex"),
    ("search python developers on linkedin", "linkedin", "python developers"),
    ("search lofi beats on spotify", "spotify", "lofi beats"),
    ("search tom and jerry on youtube", "youtube", "tom and jerry"),
]


@pytest.mark.parametrize("text,site,query", SITE_CASES)
def test_parse_site_search(text, site, query):
    assert commands.parse_site_search(commands.strip_wake_prefix(text.lower())) == (site, query)


@pytest.mark.parametrize("text,site,query", SITE_CASES)
def test_site_search_opens_the_site(web, text, site, query):
    reply = commands.run_command(text, _ctx())
    assert len(web) == 1, web
    assert web[0] == commands.site_search_url(site, query)
    assert reply == f"Searching {commands.SITE_SEARCH_NAMES[site]} for {query}."


def test_youtube_url_is_percent_encoded(web):
    commands.run_command("search elden ring malenia guide on youtube", _ctx())
    assert web == ["https://www.youtube.com/results?search_query=elden%20ring%20malenia%20guide"]
    assert _q(web[0], "search_query") == "elden ring malenia guide"


def test_hostile_query_is_encoded(web):
    commands.run_command("search a&b=c#d <x> on reddit", _ctx())
    assert web and _q(web[0], "q") == "a&b=c#d <x>"
    assert " " not in web[0] and "#" not in web[0] and "&b" not in web[0]


@pytest.mark.parametrize("text,query", [
    ("search python tutorials", "python tutorials"),
    ("search for cats and dogs", "cats and dogs"),
    ("google best pizza near me", "best pizza near me"),
    ("look up hotels in london", "hotels in london"),
    ("search for errors in my code", "errors in my code"),
])
def test_plain_search_still_goes_to_google(web, text, query):
    reply = commands.run_command(text, _ctx())
    assert len(web) == 1 and web[0].startswith("https://www.google.com/search?q=")
    assert _q(web[0], "q") == query
    assert reply.startswith("Searching for")


def test_play_on_youtube_keeps_play_media(web, monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "play_media", lambda q, platform="auto": calls.append((q, platform)) or (True, "ok"))
    commands.run_command("play drake on youtube", _ctx())
    commands.run_command("play drake on spotify", _ctx())
    assert calls == [("drake", "youtube"), ("drake", "spotify")]
    assert not web


def test_spotify_site_search_uses_desktop_app_when_installed(web, monkeypatch):
    popen = []
    monkeypatch.setattr(commands, "is_spotify_available", lambda: True)
    monkeypatch.setattr(commands.subprocess, "Popen", lambda cmd, **k: popen.append(cmd))
    ok, msg = commands.open_site_search("spotify", "lofi beats")
    assert ok and popen == ['start "" "spotify:search:lofi%20beats"'] and not web
    assert msg == "Searching Spotify for lofi beats."


def test_resolve_search_site_aliases():
    assert commands.resolve_search_site("You Tube") == "youtube"
    assert commands.resolve_search_site("the steam store") == "steam"
    assert commands.resolve_search_site("google maps") == "maps"
    assert commands.resolve_search_site("reddit website") == "reddit"
    assert commands.resolve_search_site("myspace") is None


@pytest.mark.parametrize("text", ["search it on youtube", "search on youtube", "play drake on youtube",
                                  "open youtube", "search for cats", "what is on youtube"])
def test_non_site_searches_do_not_parse(text):
    assert commands.parse_site_search(text) is None


@pytest.mark.parametrize("text,parts", [
    ("search elden ring malenia guide on youtube and then open spotify",
     ["search elden ring malenia guide on youtube", "open spotify"]),
    ("search X on youtube and volume up", ["search X on youtube", "volume up"]),
    ("youtube search lofi beats and then open spotify", ["youtube search lofi beats", "open spotify"]),
    ("look up rust lifetimes on stackoverflow, then open vs code", ["look up rust lifetimes on stackoverflow", "open vs code"]),
    ("watch tom and jerry on youtube", ["watch tom and jerry on youtube"]),
    ("search tom and jerry on youtube", ["search tom and jerry on youtube"]),
    ("search for cats and dogs", ["search for cats and dogs"]),
])
def test_chain_split_around_site_search(text, parts):
    assert chained_commands.split_chained_commands(text) == parts


def test_chained_site_search_runs_both(web, monkeypatch):
    launched = []
    monkeypatch.setattr(commands, "_launch", lambda target: launched.append(target))
    reply = commands.run_command("search elden ring malenia guide on youtube and then open spotify", _ctx())
    assert web == [commands.site_search_url("youtube", "elden ring malenia guide")]
    assert launched == ["spotify:"]
    assert "Searching YouTube for elden ring malenia guide." in reply


def test_llm_site_search_action_is_whitelisted_and_validated(web):
    ok, msg, args = ai.validate_action_arguments("site_search", {"site": "You Tube", "query": "  lofi  beats "})
    assert ok and args == {"site": "youtube", "query": "lofi beats"}
    ok, _, _ = ai.validate_action_arguments("site_search", {"site": "evil.example", "query": "x"})
    assert not ok
    executed, msg = ai.execute_whitelisted_action("site_search", {"site": "reddit", "query": "keyboards"}, None)
    assert executed and web == ["https://www.reddit.com/search/?q=keyboards"]
    assert "site_search" in ai.build_system_prompt(None)


def test_decision_engine_site_search(web):
    from decision_engine import evaluate_decision
    reply = evaluate_decision("search elden ring on youtube", _ctx())
    assert reply == "Searching YouTube for elden ring."
    assert web == [commands.site_search_url("youtube", "elden ring")]


def test_decision_engine_play_music_regex_accepts_plain_search():
    from decision_engine import _RE_PLAY_MUSIC
    assert _RE_PLAY_MUSIC.search("search drake on spotify").group(1) == "drake"
    assert _RE_PLAY_MUSIC.search("search for drake on spotify").group(1) == "drake"


# ----------------------------------------------------------------------------- 2. gesture mode

class _GestureCtx:
    def __init__(self, on=False, frame=None):
        self.on = on
        self.calls = []
        self.frame = frame
        cfg = Config()
        self.cfg = cfg
        self.engine = NS(cfg=cfg, active=True)
        self.overlay = None
        self.tracker = NS(get_latest_frame=lambda: None)

    def set_gesture_mode(self, on):
        self.calls.append(on)
        self.on = on

    def gesture_mode_on(self):
        return self.on

    def snapshot_frame(self, timeout=3.0):
        self.calls.append(("snapshot", timeout))
        return self.frame


@pytest.mark.parametrize("text", ["gesture mode on", "enable gestures", "turn on gestures", "start gesture control",
                                  "turn on the camera", "camera on", "i want to use gestures", "jarvis gesture mode on",
                                  "please turn on the camera"])
def test_gesture_on_phrases(text):
    ctx = _GestureCtx(on=False)
    assert commands.run_command(text, ctx) == "Gesture mode on."
    assert ctx.calls == [True]


@pytest.mark.parametrize("text", ["gesture mode off", "disable gestures", "turn off gestures", "stop gesture control",
                                  "turn off the camera", "camera off", "release the camera"])
def test_gesture_off_phrases(text):
    ctx = _GestureCtx(on=True)
    assert commands.run_command(text, ctx) == "Gesture mode off, camera released."
    assert ctx.calls == [False]


def test_toggle_tracking_toggles_gesture_mode():
    ctx = _GestureCtx(on=True)
    assert commands.run_command("toggle tracking", ctx) == "Gesture mode off, camera released."
    assert commands.run_command("toggle tracking", ctx) == "Gesture mode on."
    assert ctx.calls == [False, True]


def test_toggle_tracking_whitelisted_and_engine_paths_use_gesture_mode():
    from decision_engine import evaluate_decision
    ctx = _GestureCtx(on=False)
    ok, msg = ai.execute_whitelisted_action("island_toggle_tracking", {}, ctx)
    assert ok and msg == "Gesture mode on." and ctx.calls == [True]
    assert evaluate_decision("toggle hand tracking", ctx) == "Gesture mode off, camera released."
    assert ctx.calls == [True, False]
    ok, msg = ai.execute_whitelisted_action("gesture_mode", {"enabled": True}, ctx)
    assert ok and ctx.calls[-1] is True


def test_gesture_mode_falls_back_to_tracking_pause():
    paused = []
    ctx = _ctx(set_tracking_paused=lambda v: paused.append(v))
    assert commands.run_command("gesture mode off", ctx) == "Gesture mode off."
    assert commands.run_command("gesture mode on", ctx) == "Gesture mode on."
    assert paused == [True, False]
    assert commands.run_command("camera on", _ctx()) == "Gesture mode isn't available right now, sir."


def test_gesture_state_ignores_non_bool():
    assert commands.gesture_mode_state(mock.MagicMock()) is None
    assert commands.gesture_mode_state(None) is None


def test_gesture_fuzzy_intent_needs_camera_or_gesture_word():
    from decision_engine import apply_decision_policy
    assert apply_decision_policy("gesture_mode_on", 0.9, 0.5, "turn on wifi") is None
    assert apply_decision_policy("gesture_mode_on", 0.9, 0.5, "switch the camera on") == "gesture_mode_on"


def _ai_ctx(ctx):
    ctx.cfg.ai_enabled = True
    ctx.cfg.ai_vision_enabled = True
    return ctx


def test_vision_uses_snapshot_when_tracker_has_no_frame(monkeypatch):
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    ctx = _ai_ctx(_GestureCtx(on=False, frame=frame))
    seen = []

    class FakeGroq:
        def __init__(self, *a, **k):
            pass

        def describe_image(self, img, **k):
            seen.append(img)
            return "A coffee mug, sir."

    monkeypatch.setattr(commands, "GroqClient", FakeGroq)
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")
    import screen_awareness
    monkeypatch.setattr(screen_awareness, "vision_blocked_reply", lambda *a, **k: None)
    assert commands.run_command("what am i holding", ctx) == "A coffee mug, sir."
    assert seen and seen[0] is frame
    assert ("snapshot", 3.0) in ctx.calls


def test_vision_without_any_frame_reports_sensor(monkeypatch):
    ctx = _ai_ctx(_GestureCtx(on=False, frame=None))
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")
    import screen_awareness
    monkeypatch.setattr(screen_awareness, "vision_blocked_reply", lambda *a, **k: None)
    assert "optical sensor is currently unavailable" in commands.run_command("what am i holding", ctx)


# ----------------------------------------------------------------------------- 3. shared Groq state

class _Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code = status
        self._data = data or {}
        self.headers = headers or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def test_limiter_is_shared_across_clients():
    a, b = ai.GroqClient(api_key="k"), ai.GroqClient(api_key="k")
    assert a.rate_limiter is b.rate_limiter is ai.get_shared_rate_limiter("llm")
    assert a.stt_rate_limiter is b.stt_rate_limiter is ai.get_shared_rate_limiter("stt")
    a.rate_limiter.record_retry_after(20)
    with pytest.raises(ai.RateLimitError) as ei:
        b.chat([{"role": "user", "content": "x"}], json_mode=True)
    assert 19 <= ei.value.retry_after <= 20
    # a separate kind (speech-to-text) is not blocked by the LLM cooldown
    with mock.patch.object(ai.GroqClient, "_request_with_retry", return_value=_Resp(200, {"text": "hi"})):
        assert b.transcribe(b"wav") == "hi"


def test_server_429_retry_after_blocks_every_client():
    a, b = ai.GroqClient(api_key="k"), ai.GroqClient(api_key="k")
    with mock.patch.object(ai.GroqClient, "_request_with_retry",
                           return_value=_Resp(429, headers={"Retry-After": "30"})):
        with pytest.raises(ai.RateLimitError):
            a.describe_image(b"\xff\xd8jpeg")
    with pytest.raises(ai.RateLimitError) as ei:
        b.describe_image(b"\xff\xd8jpeg")
    assert ei.value.retry_after > 25


def test_local_rpm_window_is_shared():
    lim = ai.get_shared_rate_limiter("llm")
    for _ in range(lim.max_rpm):
        ai.GroqClient(api_key="k").rate_limiter.acquire()
    with pytest.raises(ai.RateLimitError):
        ai.GroqClient(api_key="k").rate_limiter.acquire()


def test_chat_cache_shared_and_bounded():
    resp = _Resp(200, {"choices": [{"message": {"content": "Paris, sir."}}]})
    with mock.patch.object(ai.GroqClient, "_request_with_retry", return_value=resp) as req:
        msgs = [{"role": "user", "content": "capital of france"}]
        assert ai.GroqClient(api_key="k").chat(msgs) == "Paris, sir."
        assert ai.GroqClient(api_key="k").chat(msgs) == "Paris, sir."
        assert req.call_count == 1
        for i in range(ai._CHAT_CACHE_MAX + 10):
            ai.GroqClient(api_key="k", cache_ttl=60).chat([{"role": "user", "content": f"q{i}"}])
            ai.get_shared_rate_limiter("llm").reset()
    assert len(ai._CHAT_CACHE) == ai._CHAT_CACHE_MAX


def test_rate_limit_reply_says_how_long():
    assert ai.rate_limit_reply(ai.RateLimitError("x", retry_after=19.2)) == \
        "Groq rate limit reached, sir. Try again in 20 seconds."
    assert ai.rate_limit_reply(ai.RateLimitError("x", retry_after=1)) == \
        "Groq rate limit reached, sir. Try again in 1 second."
    assert "3 minutes" in ai.rate_limit_reply(ai.RateLimitError("x", retry_after=150))
    assert "a moment" in ai.rate_limit_reply(ai.RateLimitError("x"))


def test_llm_path_reports_wait_time(monkeypatch):
    ctx = _ctx()
    ctx.cfg.ai_enabled = True
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")

    def limited(**k):
        raise ai.RateLimitError("limited", retry_after=20)

    monkeypatch.setattr(commands, "stream_ai_query", limited)
    assert commands.run_command("tell me a joke", ctx) == "Groq rate limit reached, sir. Try again in 20 seconds."


def test_usage_counter_by_kind():
    with mock.patch.object(ai.GroqClient, "_request_with_retry",
                           return_value=_Resp(200, {"text": "hi", "choices": [{"message": {"content": "ok"}}]})):
        c = ai.GroqClient(api_key="k")
        c.transcribe(b"wav")
        c.transcribe(b"wav")
        c.chat([{"role": "user", "content": "a question here"}])
        c.describe_image(b"\xff\xd8jpeg")
    u = ai.groq_usage_today()
    assert (u["stt"], u["llm"], u["vision"], u["total"]) == (2, 1, 1, 4)
    assert u["date"] == time.strftime("%Y-%m-%d")
    with open(ai._usage_path(), encoding="utf-8") as f:
        assert json.load(f)["stt"] == 2


# ----------------------------------------------------------------------------- answer cache

@pytest.mark.parametrize("a,b", [
    ("Jarvis, what is the capital of France?", "what is the capital of france"),
    ("hey jarvis can you tell me why is the sky blue please", "why is the sky blue"),
    ("Who was Nikola Tesla?!", "who was nikola tesla"),
])
def test_normalize_question(a, b):
    assert ai.normalize_question(a) == b


@pytest.mark.parametrize("q,ok", [
    ("what is the capital of france", True),
    ("why is the sky blue", True),
    ("how far is the moon from earth", True),
    ("what time is it in tokyo", False),
    ("what's the weather today", False),
    ("what is the news right now", False),
    ("what did i just say", False),
    ("what about him", False),
    ("tell me more", False),
    ("tell me a joke", False),
    ("how are you", False),
    ("explain that again", False),
    ("hi", False),
])
def test_cacheable_questions(q, ok):
    assert ai.is_cacheable_question(q) is ok


def test_answer_cache_roundtrip_ttl_and_lru(monkeypatch):
    assert ai.answer_cache_put("What is the capital of France?", "Paris, sir.")
    assert ai.answer_cache_get("jarvis what is the capital of france") == "Paris, sir."
    assert not ai.answer_cache_put("what time is it", "noon")
    key = ai.normalize_question("what is the capital of france")
    assert ai._ANSWER_CACHE[key][2] == ai.ANSWER_TTL_FACTUAL
    real_time = time.time
    monkeypatch.setattr(ai.time, "time", lambda: real_time() + ai.ANSWER_TTL_FACTUAL + 1)
    assert ai.answer_cache_get("what is the capital of france") is None
    monkeypatch.setattr(ai.time, "time", real_time)
    for i in range(ai.ANSWER_CACHE_MAX + 5):
        ai.answer_cache_put(f"what is element number {i} called", f"e{i}")
    assert len(ai._ANSWER_CACHE) == ai.ANSWER_CACHE_MAX
    assert ai.answer_cache_get("what is element number 0 called") is None
    assert ai.answer_cache_get(f"what is element number {ai.ANSWER_CACHE_MAX + 4} called") == f"e{ai.ANSWER_CACHE_MAX + 4}"


def test_answer_cache_persists_atomically():
    ai.answer_cache_put("why is the sky blue", "Rayleigh scattering, sir.")
    path = ai._answer_cache_path()
    assert path.startswith(os.environ["LOCALAPPDATA"])
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    assert rows[-1][:2] == ["why is the sky blue", "Rayleigh scattering, sir."]
    with ai._ANSWER_LOCK:   # simulate a fresh process: memory empty, load from disk
        ai._ANSWER_CACHE.clear()
        ai._ANSWER_LOADED = False
    assert ai.answer_cache_get("Why is the sky blue?") == "Rayleigh scattering, sir."


def test_llm_answer_cached_so_second_ask_skips_groq(monkeypatch):
    ctx = _ctx()
    ctx.cfg.ai_enabled = True
    calls = []
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")

    def fake_stream(**k):
        calls.append(k["messages"][-1]["content"])
        return '{"action": "none", "args": {}, "reply": "Rayleigh scattering, sir."}'

    monkeypatch.setattr(commands, "stream_ai_query", fake_stream)
    assert commands.run_command("why is the sky blue", ctx) == "Rayleigh scattering, sir."
    assert commands.run_command("Jarvis, why is the sky blue?", ctx) == "Rayleigh scattering, sir."
    assert len(calls) == 1
    assert ai.groq_usage_today()["cache_hits"] == 1
    # context-dependent follow-ups are never cached
    commands.run_command("why is that", ctx)
    commands.run_command("why is that", ctx)
    assert len(calls) == 3


def test_llm_actions_are_never_cached(monkeypatch):
    ctx = _ctx()
    ctx.cfg.ai_enabled = True
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")
    monkeypatch.setattr(commands, "execute_whitelisted_action", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(commands, "stream_ai_query",
                        lambda **k: '{"action": "volume_up", "args": {}, "reply": "Louder, sir."}')
    commands.run_command("could you crank the noise way up for the drums", ctx)
    assert not ai._ANSWER_CACHE


# ----------------------------------------------------------------------------- 4. local-first route

def test_who_is_goes_to_wikipedia_before_groq(monkeypatch):
    import quick_answers
    wiki = mock.MagicMock()
    wiki.get_summary.return_value = "Nikola Tesla was an inventor."
    monkeypatch.setattr(quick_answers, "get_wikipedia_client", lambda: wiki)
    monkeypatch.setattr(commands, "stream_ai_query", mock.MagicMock(side_effect=AssertionError("Groq used")))
    ctx = _ctx()
    ctx.cfg.ai_enabled = True
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")
    assert commands.run_command("who was nikola tesla", ctx) == "According to Wikipedia, Nikola Tesla was an inventor."
    wiki.get_summary.assert_called_with("nikola tesla")


def test_wikipedia_miss_falls_through_to_groq(monkeypatch):
    import quick_answers
    wiki = mock.MagicMock()
    wiki.get_summary.return_value = None
    monkeypatch.setattr(quick_answers, "get_wikipedia_client", lambda: wiki)
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")
    monkeypatch.setattr(commands, "stream_ai_query",
                        lambda **k: '{"action": "none", "args": {}, "reply": "He was an inventor, sir."}')
    ctx = _ctx()
    ctx.cfg.ai_enabled = True
    assert commands.run_command("who is the inventor of the telephone", ctx) == "He was an inventor, sir."


def test_math_and_time_never_reach_groq(monkeypatch):
    monkeypatch.setattr(commands, "stream_ai_query", mock.MagicMock(side_effect=AssertionError("Groq used")))
    monkeypatch.setattr(commands, "get_api_key", lambda: "k")
    ctx = _ctx()
    ctx.cfg.ai_enabled = True
    assert "100" in commands.run_command("what is 25 times 4", ctx)
    assert commands.run_command("what time is it", ctx).startswith("It is")
    assert "kilometers" in commands.run_command("convert 10 miles to kilometers", ctx).lower()
