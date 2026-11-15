"""
Regressions from v3.0.1 user logs:
  1. "full brightness" replied success but nothing changed (WmiSetBrightness called as a .NET method,
     failure returned the level anyway). Now: Invoke-CimMethod, one PowerShell call, verified readback,
     honest failure replies; local brightness intents never need Groq.
  2. "Jarvis volume up" misheard as "what are you up" hit the identity small-talk rule.
  3. The island's green result chip showed for small talk; now only for system actions.
"""
from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import commands
import system_control
from command_catalog import SYSTEM_CHIP_ACTIONS, shows_action_chip
from system_control import (
    BRIGHTNESS_FAILED_REPLY, BRIGHTNESS_UNSUPPORTED_REPLY, BrightnessError, BrightnessUnsupportedError,
    WindowsDisplayBackend,
)


# --------------------------------------------------------------------------- backend (subprocess mocked)
def _proc(stdout="", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr="")


@pytest.fixture
def win():
    with mock.patch.object(sys, "platform", "win32"):
        yield WindowsDisplayBackend()


def test_set_uses_invoke_cimmethod_in_one_call_and_verifies(win):
    with mock.patch("subprocess.run", return_value=_proc("OK 40 100 100 100\n")) as run:
        assert win.set_brightness(100) == 100
    assert run.call_count == 1
    script = run.call_args[0][0][-1]
    assert "Invoke-CimMethod" in script and "-MethodName WmiSetBrightness" in script
    assert ").WmiSetBrightness(" not in script
    assert "CurrentBrightness" in script          # readback happens in the same call


def test_set_clamps_level(win):
    with mock.patch("subprocess.run", return_value=_proc("OK 40 100 100 100")) as run:
        win.set_brightness(250)
    assert "[Math]::Min(100, 100)" in run.call_args[0][0][-1]


@pytest.mark.parametrize("stdout,code", [
    ("ERR Method invocation failed", 1),
    ("", 1),
    ("", 0),                    # the old code's "success": exit 0, nothing read back
    ("garbage", 0),
])
def test_set_failure_raises(win, stdout, code):
    with mock.patch("subprocess.run", return_value=_proc(stdout, code)):
        with pytest.raises(BrightnessError):
            win.set_brightness(100)


def test_set_readback_mismatch_raises(win):
    with mock.patch("subprocess.run", return_value=_proc("OK 40 100 100 40")):
        with pytest.raises(BrightnessError):
            win.set_brightness(100)


def test_snapped_level_is_accepted(win):
    # panel with coarse levels: asked 47, nearest supported 50, display reads 50 -> success
    with mock.patch("subprocess.run", return_value=_proc("OK 30 47 50 50")):
        assert win.set_brightness(47) == 50


def test_no_wmi_brightness_is_unsupported(win):
    with mock.patch("subprocess.run", return_value=_proc("NOWMI", 3)):
        with pytest.raises(BrightnessUnsupportedError):
            win.set_brightness(50)
    with mock.patch("subprocess.run", return_value=_proc("NOWMI", 3)):
        with pytest.raises(BrightnessUnsupportedError):
            win.get_brightness()


def test_timeout_raises(win):
    import subprocess
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("powershell", 6)):
        with pytest.raises(BrightnessError):
            win.brightness_up(10)


@pytest.mark.parametrize("method,expr", [("brightness_up", "$before + 10"), ("brightness_down", "$before - 10")])
def test_relative_steps_one_call(win, method, expr):
    with mock.patch("subprocess.run", return_value=_proc("OK 50 60 60 60" if "+" in expr else "OK 50 40 40 40")) as run:
        getattr(win, method)(10)
    assert run.call_count == 1
    assert expr in run.call_args[0][0][-1]


def test_get_reads_current(win):
    with mock.patch("subprocess.run", return_value=_proc("OK 37\n")) as run:
        assert win.get_brightness() == 37
    assert run.call_count == 1


@pytest.mark.parametrize("stdout,code", [("", 1), ("OK", 0), ("OK x", 0)])
def test_get_failure_raises(win, stdout, code):
    with mock.patch("subprocess.run", return_value=_proc(stdout, code)):
        with pytest.raises(BrightnessError):
            win.get_brightness()


# --------------------------------------------------------------------------- SystemControl replies
class _FailDisplay:
    def __init__(self, exc):
        self.exc = exc

    def _raise(self, *a, **k):
        raise self.exc

    get_brightness = set_brightness = brightness_up = brightness_down = _raise


def _with_display(display):
    sc = system_control.get_system_control()
    return mock.patch.object(sc, "display", display)


@pytest.mark.parametrize("call", [lambda: system_control.set_brightness(100),
                                  lambda: system_control.brightness_up(10),
                                  lambda: system_control.brightness_down(10)])
def test_wrapper_reports_failure_not_success(call):
    with _with_display(_FailDisplay(BrightnessError("readback 0 != 100"))):
        assert call() == (False, BRIGHTNESS_FAILED_REPLY)


def test_wrapper_reports_unsupported_display():
    with _with_display(_FailDisplay(BrightnessUnsupportedError("no wmi"))):
        ok, msg = system_control.set_brightness(100)
    assert ok is False and msg == BRIGHTNESS_UNSUPPORTED_REPLY


# --------------------------------------------------------------------------- local brightness intents
@pytest.mark.parametrize("text,expected", [
    ("full brightness", ("set_brightness", {"level": 100})),
    ("max brightness", ("set_brightness", {"level": 100})),
    ("maximum brightness please", ("set_brightness", {"level": 100})),
    ("brightness to full", ("set_brightness", {"level": 100})),
    ("brightness 100", ("set_brightness", {"level": 100})),
    ("brightness to 50%", ("set_brightness", {"level": 50})),
    ("set brightness to 70 percent", ("set_brightness", {"level": 70})),
    ("set the screen brightness to 30", ("set_brightness", {"level": 30})),
    ("half brightness", ("set_brightness", {"level": 50})),
    ("minimum brightness", ("set_brightness", {"level": 0})),
    ("dim the screen", ("brightness_down", {"steps": 10})),
    ("dimmer", ("brightness_down", {"steps": 10})),
    ("lower brightness", ("brightness_down", {"steps": 10})),
    ("lower the brightness", ("brightness_down", {"steps": 10})),
    ("turn down the brightness", ("brightness_down", {"steps": 10})),
    ("brighter", ("brightness_up", {"steps": 10})),
    ("make the screen brighter", ("brightness_up", {"steps": 10})),
    ("turn up the brightness", ("brightness_up", {"steps": 10})),
    ("increase brightness", ("brightness_up", {"steps": 10})),
    ("brightness up by 20", ("brightness_up", {"steps": 20})),
    ("what's the brightness", ("get_brightness", {})),
])
def test_parse_brightness_intent(text, expected):
    assert commands.parse_brightness_intent(text) == expected


@pytest.mark.parametrize("text", [
    "dim the lights", "how do i change brightness on my mac", "volume up", "the future looks bright",
    "brightness 50 and volume 30", "turn up", "",
])
def test_parse_brightness_intent_negatives(text):
    assert commands.parse_brightness_intent(text) is None


# --------------------------------------------------------------------------- full routing
class _Ctx:
    overlay = None
    cfg = None

    def say(self, *a, **k):
        pass


@pytest.fixture
def routed():
    """Runs commands.run_command with every side effect mocked; returns (posted actions, reply)."""
    posted = []

    def _spy(ctx, action, success=True, args=None, text=""):
        posted.append((action, success, dict(args or {})))

    calls = []

    def _sc(name, result=(True, "ok")):
        def _f(*a, **k):
            calls.append((name, a, k))
            return result
        return _f

    import win_input
    patches = [
        mock.patch.object(commands, "_post_action", _spy),
        mock.patch.object(win_input, "press", lambda *a, **k: None),
        mock.patch.object(commands, "_handle_ai_query", lambda *a, **k: "LLM"),
    ]
    for n in ("volume_up", "volume_down", "set_volume"):
        patches.append(mock.patch.object(system_control, n, _sc(n)))
    for n in ("set_brightness", "brightness_up", "brightness_down", "get_brightness"):
        patches.append(mock.patch.object(system_control, n, _sc(n, (True, f"{n} verified"))))
    for p in patches:
        p.start()

    from context_resolver import get_context_resolver
    try:
        get_context_resolver().clear()
    except Exception:
        pass

    def run(text):
        posted.clear()
        calls.clear()
        reply = commands.run_command(text, _Ctx())
        return [a for a, _, _ in posted], reply, list(calls)

    yield run
    for p in reversed(patches):
        p.stop()


def _voice_form(raw: str) -> str:
    """What voice._process hands run_command: lowercased; in passive mode, the text after 'jarvis'."""
    t = raw.lower().strip()
    i = t.find("jarvis")
    return t[i + len("jarvis"):].strip(" ,.!?") if i >= 0 else t


VOLUME_UP_PHRASES = [
    "Volume up.", "volume up", "Volume up, please.", "Jarvis, volume up.", "Hey Jarvis, volume up.",
    "Hey, Jarvis. Volume up.", "volume up jarvis", "turn the volume up", "increase volume", "louder",
    "hey, volume up", "Hey Jarvis, what are you up?", "what are you up",
]


@pytest.mark.parametrize("phrase", VOLUME_UP_PHRASES)
def test_volume_up_phrasings(routed, phrase):
    # Both the raw transcript (follow-up window) and the post-wake-gate tail must route to volume_up.
    for text in {phrase, phrase.lower().strip(" ,.!?"), _voice_form(phrase) or phrase}:
        actions, _reply, _ = routed(text)
        assert actions == ["volume_up"], (text, actions, _reply)


def test_identity_small_talk_still_works(routed):
    for text in ("who are you", "Who are you, Jarvis?", "what are you"):
        actions, reply, _ = routed(text)
        assert actions == ["small_talk"], text
        assert "J.A.R.V.I.S." in reply


def test_what_are_you_doing_is_not_identity(routed):
    actions, _reply, _ = routed("what are you doing")
    assert "small_talk" not in actions


def test_leftover_hey_is_stripped():
    assert commands.strip_wake_prefix("hey, jarvis. volume up") == "volume up"
    assert commands._LEAD_GREETING_RE.sub("", "hey, volume up", count=1) == "volume up"
    assert commands._LEAD_GREETING_RE.sub("", "hey", count=1) == "hey"      # a bare greeting stays


BRIGHTNESS_PHRASES = [
    ("full brightness", "set_brightness", {"level": 100}),
    ("max brightness", "set_brightness", {"level": 100}),
    ("brightness 100", "set_brightness", {"level": 100}),
    ("brightness to 50%", "set_brightness", {"level": 50}),
    ("dim the screen", "brightness_down", {"steps": 10}),
    ("brighter", "brightness_up", {"steps": 10}),
    ("lower brightness", "brightness_down", {"steps": 10}),
]


@pytest.mark.parametrize("prefix", ["", "Hey Jarvis, ", "Jarvis ", "hey, jarvis. ", "hey "])
@pytest.mark.parametrize("phrase,action,args", BRIGHTNESS_PHRASES)
def test_brightness_phrasings_local(routed, prefix, phrase, action, args):
    actions, reply, calls = routed(prefix + phrase)
    assert actions == [action], (prefix + phrase, actions)
    assert calls == [(action, (), args)]
    assert reply == f"{action} verified"        # the backend's verified reply, never Groq ("LLM")


def test_brightness_failure_reply_is_honest(routed):
    with mock.patch.object(system_control, "set_brightness", lambda **k: (False, BRIGHTNESS_FAILED_REPLY)):
        actions, reply, _ = routed("full brightness")
    assert reply == BRIGHTNESS_FAILED_REPLY


def test_system2_brightness_failure_overrides_llm_reply():
    ctx = SimpleNamespace(cfg=SimpleNamespace(ai_enabled=True), overlay=None, _current_utterance="full brightness")
    with mock.patch.object(commands, "get_api_key", return_value="k"), \
            mock.patch.object(commands, "GroqClient", mock.MagicMock()), \
            mock.patch.object(commands, "stream_ai_query", return_value="{}"), \
            mock.patch.object(commands, "build_system_prompt", return_value="sys"), \
            mock.patch.object(commands, "parse_ai_response",
                              return_value=("set_brightness", {"level": 100}, "Full maximum, sir.")), \
            mock.patch.object(system_control, "set_brightness", lambda lvl: (False, BRIGHTNESS_FAILED_REPLY)):
        assert commands._handle_ai_query("full brightness", ctx) == BRIGHTNESS_FAILED_REPLY


# --------------------------------------------------------------------------- island chip whitelist
def test_chip_whitelist():
    for a in ("volume_up", "set_brightness", "play_pause", "open_app", "open_site", "snap_window",
              "lock_workstation", "open_folder"):
        assert shows_action_chip(a), a
    for a in ("small_talk", "time", "date", "weather", "diagnostics", "wiki", "world_time", "quick_answer",
              "calculate", "set_name", "remember", "correction", "none", "", None):
        assert not shows_action_chip(a), a
    assert "small_talk" not in SYSTEM_CHIP_ACTIONS


@pytest.fixture(scope="module")
def overlay():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from config import Config
    from overlay import Overlay
    stop = threading.Event()
    ov = Overlay(stop, cfg=Config())
    yield ov
    stop.set()
    ov.timer.stop()
    app.processEvents()


@pytest.mark.parametrize("action,shown", [
    ("volume_up", True), ("set_brightness", True), ("open_app", True),
    ("small_talk", False), ("time", False), ("weather", False), ("diagnostics", False), ("wiki", False),
])
def test_overlay_chip_only_for_system_actions(overlay, action, shown):
    with mock.patch.object(overlay.island, "trigger_result") as tr:
        overlay.post("action_executed", action=action, success=True)
        overlay._drain(time.time())
    assert tr.called is shown, action
    if shown:
        assert tr.call_args.kwargs["icon"] == "success"
        assert tr.call_args.kwargs["text"] == action.replace("_", " ").title()   # chip look unchanged
