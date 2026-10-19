"""Call-me (thumb + pinky) listen gesture: synthetic landmarks, no camera."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config import Config
from gestures import GestureEngine, Hand
from test_gestures import BASE, CURL, DT, FPS, feed, hand, kinds, wake


def call_me():
    pts = dict(BASE)
    for tip in (8, 12, 16):
        pts[tip] = CURL[tip]
    pts[20] = BASE[20]            # pinky straight
    pts[4] = (0.30, 0.62)         # thumb out to the side
    return Hand([pts.get(i, (0.5, 0.6)) for i in range(21)])


def _engine(**kw):
    e = GestureEngine(Config(control_scheme="palm_fist", **kw))
    t = wake(e, 0.0)
    assert e.active
    return e, t


def _count(acts, kind):
    return sum(1 for a in acts if a.kind == kind)


def test_hold_fires_one_listen_one_rewind_rearms_after_release():
    e, t = _engine()
    acts, t = feed(e, [[call_me()]] * int(1.5 * FPS), t)
    assert _count(acts, "listen") == 1, kinds(acts)
    assert _count(acts, "rewind") == 1, kinds(acts)
    assert _count(acts, "click") == 0
    # still held: no repeat
    acts, t = feed(e, [[call_me()]] * int(1.0 * FPS), t)
    assert _count(acts, "listen") == 0
    # release, then hold again: fires again exactly once
    _, t = feed(e, [[hand("palm")]] * 10, t)
    acts, t = feed(e, [[call_me()]] * int(1.0 * FPS), t)
    assert _count(acts, "listen") == 1, kinds(acts)


def test_short_pose_gives_nothing_and_no_click():
    e, t = _engine()
    acts, t = feed(e, [[call_me()]] * int(0.3 * FPS), t)
    more, t = feed(e, [[hand("palm")]] * 10, t)
    acts += more
    assert _count(acts, "listen") == 0
    assert _count(acts, "click") == 0, kinds(acts)


@pytest.mark.parametrize("pose", ["thumbs_up", "fist", "palm", "peace", "point"])
def test_other_poses_never_fire(pose):
    e, t = _engine()
    acts, t = feed(e, [[hand(pose)]] * int(1.5 * FPS), t)
    assert _count(acts, "listen") == 0, (pose, kinds(acts))


def test_standby_never_fires():
    e = GestureEngine(Config(control_scheme="palm_fist"))
    assert not e.active
    acts, _ = feed(e, [[call_me()]] * int(2.0 * FPS), 0.0)
    assert _count(acts, "listen") == 0 and not e.active


def test_disabled_by_config():
    e, t = _engine(listen_gesture_enabled=False)
    acts, _ = feed(e, [[call_me()]] * int(2.0 * FPS), t)
    assert _count(acts, "listen") == 0


def _tracker_dispatch_loop():
    """Compile the REAL `for action in self.engine.update(...)` loop out of Tracker._run_body (the loop
    needs a camera to run in place, so it is extracted by AST and executed against fakes)."""
    import ast
    import inspect
    import textwrap
    import tracker
    tree = ast.parse(textwrap.dedent(inspect.getsource(tracker.Tracker._run_body)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.iter, ast.Call)
                and ast.unparse(node.iter.func) == "self.engine.update"):
            return compile(ast.Module(body=[node], type_ignores=[]), "tracker_loop", "exec")
    raise AssertionError("dispatch loop not found in Tracker._run_body")


def _run_loop(actions, voice):
    from gestures import Action
    me = SimpleNamespace(
        ctx=SimpleNamespace(voice=voice, chirp=MagicMock(), say=MagicMock()),
        controller=MagicMock(),
        engine=SimpleNamespace(update=lambda found, now: actions),
    )
    import tracker
    ns = {"self": me, "found": [], "now": 0.0, "gesture_toggled": False, "_tlog": tracker._tlog}
    exec(_tracker_dispatch_loop(), ns)
    return me


def test_tracker_routes_listen_to_voice():
    from gestures import Action
    voice = MagicMock()
    me = _run_loop([Action("listen")], voice)
    voice.request_listen.assert_called_once_with("gesture")
    me.controller.handle.assert_called_once()
    me.ctx.say.assert_not_called()


def test_tracker_listen_without_voice_is_harmless():
    from gestures import Action
    _run_loop([Action("listen")], None)
    _run_loop([Action("listen")], object())   # voice without request_listen


def test_tracker_other_actions_do_not_listen():
    from gestures import Action
    voice = MagicMock()
    _run_loop([Action("click"), Action("activate")], voice)
    voice.request_listen.assert_not_called()
