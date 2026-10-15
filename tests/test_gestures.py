"""Synthetic-landmark tests for the gesture state machine. Run: python tests/test_gestures.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config import Config
from gestures import GestureEngine, Hand

FPS = 30.0
DT = 1.0 / FPS

# open-hand template, fingertips up. Only wrist, knuckles, tips and thumb matter to the engine.
BASE = {
    0: (0.50, 0.80), 5: (0.44, 0.62), 9: (0.50, 0.60), 13: (0.56, 0.62), 17: (0.61, 0.66),
    8: (0.44, 0.35), 12: (0.50, 0.32), 16: (0.56, 0.35), 20: (0.61, 0.42), 4: (0.30, 0.62),
}
CURL = {8: (0.45, 0.70), 12: (0.50, 0.70), 16: (0.55, 0.70), 20: (0.60, 0.72)}


def hand(pose="point", pinch=None, dx=0.0, dy=0.0):
    pts = dict(BASE)
    extended = {"palm": (8, 12, 16, 20), "point": (8,), "peace": (8, 12), "fist": ()}[pose]
    for tip in (8, 12, 16, 20):
        if tip not in extended:
            pts[tip] = CURL[tip]
    if pose in ("point", "peace", "fist"):
        pts[4] = (0.48, 0.66)                        # thumb tucked
    if pinch == "index":
        pts[4] = (pts[8][0] + 0.01, pts[8][1] + 0.02)
    elif pinch == "middle":
        pose_tip = BASE[12]
        pts[12] = pose_tip
        pts[4] = (pose_tip[0] + 0.01, pose_tip[1] + 0.02)
    lm = []
    for i in range(21):
        x, y = pts.get(i, (0.5, 0.6))
        lm.append((x + dx, y + dy))
    return Hand(lm)


def feed(engine, frames, t):
    """frames: list of hand-lists. Returns (actions, new_t)."""
    out = []
    for hl in frames:
        out += engine.update(hl, t)
        t += DT
    return out, t


def kinds(actions):
    return [a.kind for a in actions if a.kind != "move"]


def wake(engine, t):
    acts, t = feed(engine, [[hand("palm")]] * int(1.0 * FPS), t)
    assert "activate" in kinds(acts), kinds(acts)
    acts, t = feed(engine, [[hand("point")]] * 5, t)           # release the palm
    return t


def test_wake_and_standby():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    assert e.active
    acts, t = feed(e, [[hand("palm")]] * int(1.8 * FPS), t)
    assert "deactivate" in kinds(acts) and not e.active
    assert "move" not in [a.kind for a in acts]               # palm never drives the cursor


def test_ignored_while_asleep():
    e = GestureEngine(Config())
    acts, t = feed(e, [[hand("point", pinch="index")]] * 20 + [[hand("point")]] * 20, 0.0)
    assert acts == [], acts


def test_click_and_double_click():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("point")]] * 5 + [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 5, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1 and not clicks[0].double, clicks
    acts, t = feed(e, [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 5, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1 and clicks[0].double, clicks


def test_slow_second_click_is_single():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    _, t = feed(e, [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 3, t)
    _, t = feed(e, [[hand("point")]] * int(1.0 * FPS), t)       # wait past the double-click window
    acts, t = feed(e, [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 3, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1 and not clicks[0].double


def test_drag():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    frames = [[hand("point", pinch="index", dx=0.003 * i)] for i in range(int(0.9 * FPS))]
    acts, t = feed(e, frames, t)
    ks = kinds(acts)
    assert ks.count("drag_start") == 1 and "click" not in ks, ks
    assert any(a.kind == "move" for a in acts), "cursor should follow during drag"
    acts, t = feed(e, [[hand("point")]] * 3, t)
    assert "drag_end" in kinds(acts)


def test_cursor_freezes_when_pinching():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("point", pinch="index")]] * 3, t)
    assert not any(a.kind == "move" for a in acts)


def test_right_click():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("point")]] * 3 + [[hand("point", pinch="middle")]] * 6, t)
    assert kinds(acts).count("right_click") == 1, kinds(acts)


def test_fist_is_not_a_click():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("fist", pinch="index")]] * int(0.8 * FPS) + [[hand("fist")]] * 5, t)
    assert kinds(acts) == [], kinds(acts)


def test_scroll_up_and_down():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    up, t = feed(e, [[hand("peace", dy=-0.01 * i)] for i in range(15)], t)
    assert [a.n for a in up if a.kind == "scroll"] and all(a.n > 0 for a in up if a.kind == "scroll")
    down, t = feed(e, [[hand("peace", dy=-0.15 + 0.01 * i)] for i in range(15)], t)
    assert [a.n for a in down if a.kind == "scroll"] and all(a.n < 0 for a in down if a.kind == "scroll")


def test_zoom_in_and_out():
    e = GestureEngine(Config())
    t = wake(e, 0.0)

    def pair(gap):
        left = hand("point", dx=-gap / 2)
        right = hand("point", dx=gap / 2)
        return [left, right]

    _, t = feed(e, [pair(0.0)] * int(0.4 * FPS), t)                          # enter zoom mode
    out_in, t = feed(e, [pair(0.01 * i) for i in range(1, 20)], t)           # spread
    zooms = [a.n for a in out_in if a.kind == "zoom"]
    assert zooms and all(n == 1 for n in zooms), zooms
    out_out, t = feed(e, [pair(0.19 - 0.01 * i) for i in range(1, 20)], t)   # close
    zooms = [a.n for a in out_out if a.kind == "zoom"]
    assert zooms and all(n == -1 for n in zooms), zooms


def test_hand_lost_releases_drag():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    _, t = feed(e, [[hand("point", pinch="index")]] * int(0.6 * FPS), t)
    assert e.dragging
    acts, t = feed(e, [[]] * int(0.7 * FPS), t)
    ks = kinds(acts)
    assert "drag_end" in ks and "lost" in ks and not e.dragging


def test_external_request():
    e = GestureEngine(Config())
    e.request(True)
    acts, _ = feed(e, [[]], 0.0)
    assert e.active and "activate" in kinds(acts)
    e.request(False)
    acts, _ = feed(e, [[]], 1.0)
    assert not e.active and "deactivate" in kinds(acts)


def test_request_toggle():
    e = GestureEngine(Config())
    assert not e.active
    e.request_toggle()
    acts, _ = feed(e, [[]], 0.0)
    assert e.active and "activate" in kinds(acts)
    e.request_toggle()
    acts, _ = feed(e, [[]], 1.0)
    assert not e.active and "deactivate" in kinds(acts)


def test_palm_hold_tolerates_brief_jitter():
    # 10 palm frames (0.33s), 2 jitter frames (0.066s < 0.15s grace), 16 palm frames (0.53s)
    # Total hold = ~0.86s of palm with brief flicker, should still activate
    e = GestureEngine(Config())
    frames = [[hand("palm")]] * 10 + [[hand("point")]] * 2 + [[hand("palm")]] * 16
    acts, _ = feed(e, frames, 0.0)
    assert "activate" in kinds(acts), f"Expected activate despite flicker, got: {kinds(acts)}"


def test_right_click_does_not_trigger_left_click_on_release():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    # Perform thumb-middle pinch (right click)
    acts, t = feed(e, [[hand("point", pinch="middle")]] * 6, t)
    assert "right_click" in kinds(acts)
    # Transition to normal pointing (release)
    acts, t = feed(e, [[hand("point")]] * 6, t)
    # Should NOT have emitted a left click or drag_start
    assert "click" not in kinds(acts) and "drag_start" not in kinds(acts), kinds(acts)


def test_fist_does_not_move_cursor():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    # When hand is a fist while awake, cursor shouldn't be driven
    acts, _ = feed(e, [[hand("fist")]] * 5, t)
    moves = [a for a in acts if a.kind == "move"]
    assert len(moves) == 0, f"Fist should not generate move actions, got {len(moves)}"


def test_left_and_right_hand_palm_wake():
    # Right hand (thumb on -x side)
    e_right = GestureEngine(Config())
    wake(e_right, 0.0)
    assert e_right.active
    # Left hand: mirror x coordinates across center (thumb on +x side)
    def left_hand_palm():
        h = hand("palm")
        lm = [(1.0 - x, y) for x, y in h.lm]
        return Hand(lm)
    e_left = GestureEngine(Config())
    acts, _ = feed(e_left, [[left_hand_palm()]] * int(1.0 * FPS), 0.0)
    assert "activate" in kinds(acts), f"Left hand palm failed to wake: {kinds(acts)}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")
