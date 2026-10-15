"""Synthetic-landmark tests for the gesture state machine. Run: python tests/test_gestures.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config import Config
from gestures import GestureEngine, Hand

FPS = 30.0
DT = 1.0 / FPS

# Open-hand template, fingertips up.
BASE = {
    0: (0.50, 0.80), 5: (0.44, 0.62), 9: (0.50, 0.60), 13: (0.56, 0.62), 17: (0.61, 0.66),
    8: (0.44, 0.35), 12: (0.50, 0.32), 16: (0.56, 0.35), 20: (0.61, 0.42), 4: (0.30, 0.62),
}
CURL = {8: (0.45, 0.70), 12: (0.50, 0.70), 16: (0.55, 0.70), 20: (0.60, 0.72)}


def hand(pose="point", pinch=None, dx=0.0, dy=0.0):
    pts = dict(BASE)
    if pose == "palm":
        extended = (8, 12, 16, 20)
    elif pose == "point":
        extended = (8,)
    elif pose == "peace":
        extended = (8, 12)
    elif pose in ("fist", "thumbs_up"):
        extended = ()
    elif pose in ("neutral", "relaxed"):
        # Halfway between open base and curled
        for tip in (8, 12, 16, 20):
            pts[tip] = ((BASE[tip][0] + CURL[tip][0]) / 2.0, (BASE[tip][1] + CURL[tip][1]) / 2.0)
        pts[4] = (0.45, 0.64)
        extended = None

    if extended is not None:
        for tip in (8, 12, 16, 20):
            if tip not in extended:
                pts[tip] = CURL[tip]

    if pose in ("point", "peace", "fist"):
        pts[4] = (0.48, 0.66)                        # thumb tucked
    elif pose == "thumbs_up":
        pts[4] = (0.32, 0.44)                        # thumb extended upward/outward

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
    acts, t = feed(engine, [[hand("neutral" if engine.cfg.control_scheme == "palm_fist" else "point")]] * 5, t)
    return t


# =====================================================================
# Palm & Fist Control Scheme Tests (New Default)
# =====================================================================

def test_palm_fist_open_moves_cursor():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)
    assert e.active

    # Open palm moving horizontally by dx
    frames = [[hand("palm", dx=0.003 * i)] for i in range(1, 10)]
    acts, t = feed(e, frames, t)
    moves = [a for a in acts if a.kind == "move"]
    assert len(moves) > 0, "Open palm must drive cursor in Palm & Fist scheme"


def test_palm_fist_quick_fist_clicks():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # 1. Open palm
    _, t = feed(e, [[hand("palm")]] * 5, t)
    # 2. Close to fist for 4 frames (~130ms < 400ms max hold)
    _, t = feed(e, [[hand("fist")]] * 4, t)
    # 3. Reopen to open palm
    acts, t = feed(e, [[hand("palm")]] * 5, t)

    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1, f"Expected 1 click, got: {len(clicks)}"
    assert not clicks[0].double, "First click should not be double click"


def test_palm_fist_quick_double_fist_double_clicks():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # First fist click
    _, t = feed(e, [[hand("palm")]] * 3, t)
    _, t = feed(e, [[hand("fist")]] * 4, t)
    acts1, t = feed(e, [[hand("palm")]] * 4, t)
    assert len([a for a in acts1 if a.kind == "click"]) == 1

    # Second fist click within 0.65s double click window
    _, t = feed(e, [[hand("fist")]] * 4, t)
    acts2, t = feed(e, [[hand("palm")]] * 4, t)
    clicks2 = [a for a in acts2 if a.kind == "click"]
    assert len(clicks2) == 1
    assert clicks2[0].double, "Second quick fist click must be a double click"


def test_palm_fist_held_fist_drags_and_drops():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # Close into fist and hold for 15 frames (~500ms > 350ms drag threshold)
    frames = [[hand("fist", dx=0.002 * i)] for i in range(15)]
    acts, t = feed(e, frames, t)
    ks = kinds(acts)
    assert "drag_start" in ks, f"Expected drag_start, got: {ks}"
    assert "click" not in ks, "Held fist must not trigger click"
    assert e.dragging, "Engine must be in dragging state"
    assert any(a.kind == "move" for a in acts), "Cursor must follow hand while dragging"

    # Reopen hand -> drop
    acts, t = feed(e, [[hand("palm")]] * 5, t)
    ks = kinds(acts)
    assert "drag_end" in ks, f"Expected drag_end, got: {ks}"
    assert not e.dragging


def test_palm_fist_neutral_zone_does_nothing():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # Relaxed / neutral hand (openness ~ 0.52, within [0.38, 0.58])
    acts, t = feed(e, [[hand("neutral")]] * 15, t)
    moves = [a for a in acts if a.kind == "move"]
    clicks = [a for a in acts if a.kind == "click"]
    assert len(moves) == 0, f"Neutral relaxed hand must freeze cursor, got {len(moves)} moves"
    assert len(clicks) == 0, "Neutral relaxed hand must never click"


def test_palm_fist_relaxed_hand_does_not_click():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # Transition from palm to relaxed neutral and back to palm
    _, t = feed(e, [[hand("palm")]] * 5, t)
    _, t = feed(e, [[hand("neutral")]] * 10, t)
    acts, t = feed(e, [[hand("palm")]] * 5, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 0, "Relaxed hand dip without full fist must never click"


def test_palm_fist_standby_palm_wakes():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    assert not e.active
    acts, t = feed(e, [[hand("palm")]] * int(1.0 * FPS), 0.0)
    assert "activate" in kinds(acts)
    assert e.active


def test_palm_fist_awake_palm_never_sleeps():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)
    assert e.active

    # Continuous open palm while awake for 3 full seconds (90 frames)
    acts, t = feed(e, [[hand("palm")]] * int(3.0 * FPS), t)
    assert "deactivate" not in kinds(acts), "Open palm while awake must NEVER trigger sleep!"
    assert e.active, "Engine must stay awake"


def test_palm_fist_thumbs_up_sleeps():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)
    assert e.active

    # Hold thumbs up for 1.5s (> 1.2s threshold)
    acts, t = feed(e, [[hand("thumbs_up")]] * int(1.5 * FPS), t)
    assert "deactivate" in kinds(acts), f"Thumbs up must trigger deactivate, got: {kinds(acts)}"
    assert not e.active, "Engine must be on standby"


def test_palm_fist_fist_during_wake_never_sleeps_or_wakes():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    assert not e.active

    # Fist while on standby should NEVER wake
    acts, t = feed(e, [[hand("fist")]] * int(1.5 * FPS), 0.0)
    assert "activate" not in kinds(acts)
    assert not e.active

    # Wake cleanly with palm
    t = wake(e, t)
    assert e.active

    # Fist while awake should NEVER sleep
    acts, t = feed(e, [[hand("fist")]] * int(2.0 * FPS), t)
    assert "deactivate" not in kinds(acts)
    assert e.active


def test_palm_fist_click_lookback_accuracy():
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # Hand moves to position (0.50, 0.50)
    _, t = feed(e, [[hand("palm", dx=0.0)]] * 10, t)
    # Fingers curl into fist: in the closing phase, the anchor barely shifts
    _, t = feed(e, [[hand("fist")]] * 4, t)
    # Reopen to click
    acts, t = feed(e, [[hand("palm")]] * 4, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1


# =====================================================================
# Legacy Pinch Control Scheme Tests (Backward Compatibility)
# =====================================================================

def test_pinch_wake_and_standby():
    e = GestureEngine(Config(control_scheme="pinch"))
    t = wake(e, 0.0)
    assert e.active
    acts, t = feed(e, [[hand("palm")]] * int(1.8 * FPS), t)
    assert "deactivate" in kinds(acts) and not e.active
    assert "move" not in [a.kind for a in acts]


def test_pinch_click_and_double_click():
    e = GestureEngine(Config(control_scheme="pinch"))
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("point")]] * 5 + [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 5, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1 and not clicks[0].double, clicks
    acts, t = feed(e, [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 5, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1 and clicks[0].double, clicks


def test_pinch_slow_second_click_is_single():
    e = GestureEngine(Config(control_scheme="pinch"))
    t = wake(e, 0.0)
    _, t = feed(e, [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 3, t)
    _, t = feed(e, [[hand("point")]] * int(1.0 * FPS), t)
    acts, t = feed(e, [[hand("point", pinch="index")]] * 4 + [[hand("point")]] * 3, t)
    clicks = [a for a in acts if a.kind == "click"]
    assert len(clicks) == 1 and not clicks[0].double


def test_pinch_drag():
    e = GestureEngine(Config(control_scheme="pinch"))
    t = wake(e, 0.0)
    frames = [[hand("point", pinch="index", dx=0.003 * i)] for i in range(int(0.9 * FPS))]
    acts, t = feed(e, frames, t)
    ks = kinds(acts)
    assert ks.count("drag_start") == 1 and "click" not in ks, ks
    assert any(a.kind == "move" for a in acts), "cursor should follow during drag"
    acts, t = feed(e, [[hand("point")]] * 3, t)
    assert "drag_end" in kinds(acts)


def test_pinch_cursor_freezes_when_pinching():
    e = GestureEngine(Config(control_scheme="pinch"))
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("point", pinch="index")]] * 3, t)
    assert not any(a.kind == "move" for a in acts)


def test_pinch_fist_is_not_a_click():
    e = GestureEngine(Config(control_scheme="pinch"))
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("fist", pinch="index")]] * int(0.8 * FPS) + [[hand("fist")]] * 5, t)
    assert kinds(acts) == [], kinds(acts)


# =====================================================================
# Shared Navigation & System Behavior Tests
# =====================================================================

def test_ignored_while_asleep():
    e = GestureEngine(Config())
    acts, t = feed(e, [[hand("point", pinch="index")]] * 20 + [[hand("point")]] * 20, 0.0)
    assert acts == [], acts


def test_right_click():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("point")]] * 3 + [[hand("point", pinch="middle")]] * 6, t)
    assert kinds(acts).count("right_click") == 1, kinds(acts)


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

    _, t = feed(e, [pair(0.0)] * int(0.4 * FPS), t)
    out_in, t = feed(e, [pair(0.01 * i) for i in range(1, 20)], t)
    zooms = [a.n for a in out_in if a.kind == "zoom"]
    assert zooms and all(n == 1 for n in zooms), zooms
    out_out, t = feed(e, [pair(0.19 - 0.01 * i) for i in range(1, 20)], t)
    zooms = [a.n for a in out_out if a.kind == "zoom"]
    assert zooms and all(n == -1 for n in zooms), zooms


def test_hand_lost_releases_drag():
    e = GestureEngine(Config(control_scheme="palm_fist"))
    t = wake(e, 0.0)
    _, t = feed(e, [[hand("fist")]] * int(0.6 * FPS), t)
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
    e = GestureEngine(Config(control_scheme="palm_fist"))
    frames = [[hand("palm")]] * 10 + [[hand("neutral")]] * 2 + [[hand("palm")]] * 16
    acts, _ = feed(e, frames, 0.0)
    assert "activate" in kinds(acts), f"Expected activate despite flicker, got: {kinds(acts)}"


def test_right_click_does_not_trigger_left_click_on_release():
    e = GestureEngine(Config())
    t = wake(e, 0.0)
    acts, t = feed(e, [[hand("point", pinch="middle")]] * 6, t)
    assert "right_click" in kinds(acts)
    acts, t = feed(e, [[hand("point")]] * 6, t)
    assert "click" not in kinds(acts) and "drag_start" not in kinds(acts), kinds(acts)


def test_anchor_points_selection():
    cfg = Config()
    for mode in ["knuckles", "palm_center", "centroid", "index_mcp"]:
        cfg.palm_anchor = mode
        e = GestureEngine(cfg)
        h = hand("palm")
        anc = e._compute_palm_anchor(h.lm)
        assert len(anc) == 2
        assert 0.0 <= anc[0] <= 1.0 and 0.0 <= anc[1] <= 1.0


def test_clutch_stops_cursor_outside_box():
    cfg = Config(control_scheme="palm_fist")
    cfg.clutch_enabled = True
    cfg.box_margin = 0.20
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # Inside box: should emit move
    acts, t = feed(e, [[hand("palm", dx=0.0)]] * 2, t)
    moves = [a for a in acts if a.kind == "move"]
    assert len(moves) > 0
    assert not e.clutched

    # Move outside margin (dx = -0.35)
    acts, t = feed(e, [[hand("palm", dx=-0.35)]] * 3, t)
    moves = [a for a in acts if a.kind == "move"]
    assert len(moves) == 0
    assert e.clutched


def test_precision_mode_and_relative_deltas():
    cfg = Config(control_scheme="palm_fist")
    cfg.precision_threshold = 0.02
    e = GestureEngine(cfg)
    t = wake(e, 0.0)

    # Move slowly (fine-aim)
    acts, t = feed(e, [[hand("palm", dx=0.0002 * i)] for i in range(5)], t)
    moves = [a for a in acts if a.kind == "move"]
    assert len(moves) > 0
    assert moves[-1].precision is True

    # Move quickly (fast sweep)
    acts, t = feed(e, [[hand("palm", dx=0.05 * i)] for i in range(1, 5)], t)
    moves = [a for a in acts if a.kind == "move"]
    assert len(moves) > 0
    assert moves[-1].precision is False
    assert moves[-1].dx != 0.0
    assert moves[-1].speed > 0.0


# =====================================================================
# Unittest Discovery Wrapper
# =====================================================================

class TestGestures(unittest.TestCase):
    pass


def _make_test(fn):
    return lambda self: fn()


for _k, _fn in list(globals().items()):
    if _k.startswith("test_") and callable(_fn):
        setattr(TestGestures, _k, _make_test(_fn))


if __name__ == "__main__":
    unittest.main()
