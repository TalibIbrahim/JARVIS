"""One-handed pinch-and-hold zoom, driven by synthetic landmarks."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from gestures import GestureEngine
from test_gestures import DT, feed, hand, wake


def _engine(**kw):
    kw.setdefault("control_scheme", "palm_fist")
    e = GestureEngine(Config(**kw))
    t = wake(e, 0.0)
    return e, t


def _pinch_frames(n, dy_per_frame=0.0, start_dy=0.0):
    return [[hand("palm", pinch="index", dy=start_dy + dy_per_frame * i)] for i in range(n)]


def _zoom_n(acts):
    return [a.n for a in acts if a.kind == "zoom"]


def _hold_frames():
    return int(0.45 / DT)   # comfortably beyond the 0.30 s hold


def test_pinch_hold_then_up_zooms_in_with_no_moves():
    e, t = _engine()
    _, t = feed(e, _pinch_frames(_hold_frames()), t)
    assert e.zoom1_on
    acts, t = feed(e, _pinch_frames(15, dy_per_frame=-0.01), t)     # hand moves up (y decreases)
    zooms = _zoom_n(acts)
    assert zooms and all(n == 1 for n in zooms), zooms
    assert not [a for a in acts if a.kind == "move"], "cursor must not move while zooming"


def test_pinch_hold_then_down_zooms_out():
    e, t = _engine()
    _, t = feed(e, _pinch_frames(_hold_frames()), t)
    acts, t = feed(e, _pinch_frames(15, dy_per_frame=0.01), t)
    zooms = _zoom_n(acts)
    assert zooms and all(n == -1 for n in zooms), zooms
    assert not [a for a in acts if a.kind == "move"]


def test_short_pinch_gives_nothing():
    e, t = _engine()
    acts, t = feed(e, _pinch_frames(int(0.15 / DT), dy_per_frame=-0.01), t)
    more, t = feed(e, [[hand("palm")]] * 6, t)
    assert not e.zoom1_on
    assert not _zoom_n(acts + more)
    assert "rewind" not in [a.kind for a in acts + more]


def test_rewind_emitted_on_zoom_entry():
    e, t = _engine()
    acts, t = feed(e, _pinch_frames(_hold_frames()), t)
    rewinds = [a for a in acts if a.kind == "rewind"]
    assert len(rewinds) == 1
    assert rewinds[0].age >= 0.30


def test_release_ends_zoom_and_stops_zooming():
    e, t = _engine()
    _, t = feed(e, _pinch_frames(_hold_frames()), t)
    acts, t = feed(e, [[hand("palm")]] * 6, t)
    assert not e.zoom1_on and not e.zp_down
    assert not _zoom_n(acts)


def test_disabled_by_config():
    e, t = _engine(gesture_one_hand_zoom=False)
    acts1, t = feed(e, _pinch_frames(_hold_frames()), t)
    acts2, t = feed(e, _pinch_frames(15, dy_per_frame=-0.01), t)
    assert not e.zoom1_on
    assert not _zoom_n(acts1 + acts2)
    assert "rewind" not in [a.kind for a in acts1 + acts2]


def test_right_click_pinch_thumb_middle_unaffected():
    e, t = _engine()
    acts, t = feed(e, [[hand("palm", pinch="middle")]] * 4, t)
    more, t = feed(e, [[hand("palm")]] * 6, t)
    ks = [a.kind for a in acts + more]
    assert "right_click" in ks, ks
    assert not e.zoom1_on and "zoom" not in ks


def test_fist_click_still_works():
    e, t = _engine()
    _, t = feed(e, [[hand("palm")]] * 5, t)
    _, t = feed(e, [[hand("fist")]] * 4, t)
    acts, t = feed(e, [[hand("palm")]] * 5, t)
    assert len([a for a in acts if a.kind == "click"]) == 1
    assert not e.zoom1_on


def test_open_palm_still_moves():
    e, t = _engine()
    acts, t = feed(e, [[hand("palm", dx=0.003 * i)] for i in range(1, 10)], t)
    assert [a for a in acts if a.kind == "move"]
    assert not e.zoom1_on
