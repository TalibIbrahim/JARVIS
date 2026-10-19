"""Destructive-action gate: unit tests for commands.destructive_action_gate, the System 2 (LLM) path under the
side-effect guard, and the exact local phrases that must still work. Nothing here touches the real machine."""
from __future__ import annotations

import contextlib
import io
from unittest import mock

import pytest

import commands
from commands import DESTRUCTIVE_ACTIONS, destructive_action_gate
from side_effect_guard import SideEffectGuard, make_ctx

NOISE = ["hello", "bye", "tell me a joke", "volume up and shut down", ""]

ALLOWED = {
    "sleep_system": ["sleep system", "sleep pc", "sleep computer", "put pc to sleep", "Sleep PC.", "please sleep pc"],
    "clean_slate": ["clean slate", "execute clean slate", "clean slate protocol", "Execute Clean Slate!"],
    "close_window": ["confirm close", "force close", "close anyway", "Force close."],
}
ARGS = {"sleep_system": {}, "clean_slate": {}, "close_window": {"confirm": True}}
NEAR_MISS = {
    "sleep_system": ["sleep", "sleep mode", "go to sleep", "sleep pcs", "sleeppc", "sleep p c",
                     "do not sleep pc", "sleep pc and volume up", "volume up and sleep pc",
                     "volume up; sleep pc", "sleep pc then quit", "sleеp pc", "sleep pс"],
    "clean_slate": ["execute", "execute everything", "clean", "slate", "clean slate please explain",
                    "what is a clean slate", "clean slate and volume up", "volume up and clean slate",
                    "clean slate; sleep pc", "wrap it up", "clеan slate", "clean slаte"],
    "close_window": ["close", "close window", "confirm", "confirm shutdown", "force", "force close and volume up",
                     "volume up and force close", "confirm close; quit", "fоrce close", "force clоse"],
}


def _quiet():
    return contextlib.redirect_stdout(io.StringIO())


@pytest.fixture
def guard():
    with SideEffectGuard() as g, _quiet():
        yield g


def _confirmed(call):
    _, a, k = call
    return k.get("confirmed", a[0] if a else False)


def test_every_destructive_action_is_covered_by_these_tests():
    assert set(DESTRUCTIVE_ACTIONS) == {"shutdown_system", "restart_system", "sleep_system", "clean_slate",
                                        "close_window"}
    assert set(ALLOWED) | {"shutdown_system", "restart_system"} == set(DESTRUCTIVE_ACTIONS)


# ------------------------------------------------------------------------------------------ gate unit tests
@pytest.mark.parametrize("action", sorted(ALLOWED))
def test_gate_allows_exact_phrases(action):
    for utt in ALLOWED[action]:
        ok, _ = destructive_action_gate(action, dict(ARGS[action]), utt)
        assert ok, (action, utt)


@pytest.mark.parametrize("action", sorted(ALLOWED))
def test_gate_blocks_benign_near_miss_chained_and_homoglyph(action):
    for utt in NOISE + NEAR_MISS[action]:
        ok, _ = destructive_action_gate(action, dict(ARGS[action]), utt)
        assert not ok, (action, utt)


@pytest.mark.parametrize("action", sorted(ALLOWED))
def test_gate_blocks_other_actions_phrases(action):
    for other, phrases in ALLOWED.items():
        if other == action:
            continue
        for utt in phrases:
            ok, _ = destructive_action_gate(action, dict(ARGS[action]), utt)
            assert not ok, (action, utt)


@pytest.mark.parametrize("action", sorted(ALLOWED))
@pytest.mark.parametrize("utt", [None, 0, 3.5, b"sleep pc", ["sleep pc"], {"a": 1}, object()])
def test_gate_fails_closed_when_no_utterance_is_known(action, utt):
    ok, _ = destructive_action_gate(action, dict(ARGS[action]), utt)
    assert not ok


@pytest.mark.parametrize("action", ["shutdown_system", "restart_system"])
@pytest.mark.parametrize("utt", NOISE + [None, "confirm shutdown", "confirm restart"])
def test_gate_never_lets_the_model_confirm_shutdown_or_restart(action, utt):
    for given in ({"confirmed": True}, {"confirmed": "yes"}, {"confirmed": 1}, {}, None):
        ok, args = destructive_action_gate(action, given, utt)
        assert ok and args["confirmed"] is False, (action, utt, given, args)


def test_gate_close_window_without_confirm_only_asks():
    ok, args = destructive_action_gate("close_window", {"confirm": False}, None)
    assert ok and not args.get("confirm")
    ok, args = destructive_action_gate("close_window", {}, "bye")
    assert ok and not args.get("confirm")


def test_gate_does_not_mutate_caller_args_and_passes_other_actions_through():
    given = {"confirmed": True}
    destructive_action_gate("shutdown_system", given, "bye")
    assert given == {"confirmed": True}
    assert destructive_action_gate("volume_up", {"steps": 3}, None) == (True, {"steps": 3})


# ---------------------------------------------------------------------------------------- System 2 (LLM)
S2_REQUESTS = {
    "shutdown_system": {"confirmed": True},
    "restart_system": {"confirmed": True},
    "sleep_system": {},
    "clean_slate": {},
    "close_window": {"confirm": True},
    "power_mode:quit": {"mode": "quit"},
    "power_mode:standby": {"mode": "standby"},
}
S2_UTTERANCES = ["bye", "tell me a joke", "volume up and shut down"]
REPLY = "Certainly, sir. Spoken reply kept."


def _cfg_ai():
    import config
    cfg = config.Config()
    cfg.ai_enabled = True
    return cfg


def _system2(guard, key, args, utterance):
    ctx = make_ctx(guard, _cfg_ai())
    action = key.split(":")[0]
    ctx._current_utterance = utterance  # run_command sets this before falling through to the LLM
    with mock.patch.object(commands, "GroqClient", mock.MagicMock()), \
            mock.patch.object(commands, "stream_ai_query", return_value="{}"), \
            mock.patch.object(commands, "build_system_prompt", return_value="sys"), \
            mock.patch.object(commands, "parse_ai_response", return_value=(action, dict(args), REPLY)):
        return commands._handle_ai_query(utterance, ctx)


def _destructive_calls(guard):
    bad = []
    for call in guard.calls:
        name, a, k = call
        if name in ("system_control.sleep_system", "ctx.quit", "ctx.set_active", "pyautogui.hotkey"):
            bad.append(call)
        elif name in ("system_control.shutdown_system", "system_control.restart_system"):
            if _confirmed(call):
                bad.append(call)
        elif name == "system_control.close_window":
            if k.get("confirm", a[0] if a else False):
                bad.append(call)
        elif name.startswith(("subprocess.", "os.system", "os.popen")):
            bad.append(call)
    return bad


@pytest.mark.parametrize("utterance", S2_UTTERANCES)
@pytest.mark.parametrize("key", sorted(S2_REQUESTS))
def test_system2_destructive_request_is_blocked_and_reply_kept(guard, key, utterance):
    reply = _system2(guard, key, S2_REQUESTS[key], utterance)
    assert reply == REPLY
    assert _destructive_calls(guard) == [], guard.calls


@pytest.mark.parametrize("utterance", S2_UTTERANCES)
@pytest.mark.parametrize("key", ["shutdown_system", "restart_system"])
def test_system2_shutdown_restart_only_reach_system_control_unconfirmed(guard, key, utterance):
    _system2(guard, key, S2_REQUESTS[key], utterance)
    for call in guard.of("system_control." + key):
        assert _confirmed(call) is False, guard.calls
    assert not guard.of("subprocess")


def test_system2_exact_local_phrase_still_runs_the_action(guard):
    assert _system2(guard, "sleep_system", {}, "sleep pc") == REPLY
    assert guard.of("system_control.sleep_system")
    guard.calls.clear()
    _system2(guard, "clean_slate", {}, "execute clean slate")
    assert any(c[0] == "pyautogui.hotkey" for c in guard.calls)


def test_system2_unknown_utterance_fails_closed_through_execute_whitelisted_action(guard):
    import ai
    ctx = make_ctx(guard)
    ctx._current_utterance = None
    for action, args in (("sleep_system", {}), ("clean_slate", {}), ("close_window", {"confirm": True})):
        ok, msg = ai.execute_whitelisted_action(action, args, ctx)
        assert not ok and "blocked" in msg, (action, msg)
    assert _destructive_calls(guard) == []


# ------------------------------------------------------------------------- exact local phrases still work
def test_confirm_shutdown_after_prompt_executes(guard):
    ctx = make_ctx(guard)
    r = commands.run_command("shut down the pc", ctx)
    assert r and "confirm shutdown" in r.lower()
    assert not [c for c in guard.of("system_control.shutdown_system") if _confirmed(c)]
    commands.run_command("confirm shutdown", ctx)
    assert [c for c in guard.of("system_control.shutdown_system") if _confirmed(c)]


def test_confirm_restart_executes(guard):
    ctx = make_ctx(guard)
    commands.run_command("restart pc", ctx)
    commands.run_command("confirm restart", ctx)
    assert [c for c in guard.of("system_control.restart_system") if _confirmed(c)]


def test_sleep_pc_executes(guard):
    commands.run_command("sleep pc", make_ctx(guard))
    assert guard.of("system_control.sleep_system")


def test_execute_clean_slate_executes(guard):
    ctx = make_ctx(guard)
    commands.run_command("execute clean slate", ctx)
    assert ("pyautogui.hotkey", ("win", "d"), {"_pause": False}) in guard.calls
    assert ("ctx.set_active", (False,), {}) in guard.calls


def test_force_close_executes(guard):
    commands.run_command("force close", make_ctx(guard))
    calls = guard.of("system_control.close_window")
    assert calls and any(k.get("confirm", a[0] if a else False) for _, a, k in calls)


@pytest.mark.parametrize("text", ["volume up and sleep pc", "volume up and execute clean slate",
                                  "volume up and force close", "hello; confirm shutdown"])
def test_destructive_phrases_do_not_run_inside_a_chain(guard, text):
    commands.run_command(text, make_ctx(guard))
    assert _destructive_calls(guard) == [], guard.calls
