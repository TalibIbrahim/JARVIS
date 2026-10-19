"""Evidence tests: ordinary open-palm cursor motion must not be misread as swipe/slide shortcuts.

Synthetic landmark streams at 30 fps drive GestureEngine.update (palm_fist scheme) with
pyautogui / system_control mocked. Desired behaviour: ordinary moves -> 0 shortcuts and
0 dropped cursor frames. A genuine fast flick may trigger.
"""
import math
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from config import Config
from gestures import GestureEngine
from test_gestures import DT, FPS, feed, hand, wake

SHORTCUTS = ("switch_window", "volume_up", "volume_down", "island_page_step")


@pytest.fixture(autouse=True)
def _mock_devices(monkeypatch):
    import pyautogui
    import system_control
    monkeypatch.setattr(pyautogui, "hotkey", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(system_control, "get_foreground_window_title", lambda: "win", raising=False)
    monkeypatch.setattr(system_control, "volume_up", lambda steps=1: 55, raising=False)
    monkeypatch.setattr(system_control, "volume_down", lambda steps=1: 45, raising=False)


def _run(path_fn, duration):
    """path_fn(u in [0,1]) -> (dx, dy) palm offset. Returns metrics over the moving frames."""
    cfg = Config(control_scheme="palm_fist")
    e = GestureEngine(cfg, overlay=SimpleNamespace(post=lambda *a, **k: None))
    t = wake(e, 0.0)
    _, t = feed(e, [[hand("palm")]] * 15, t)  # settle open palm, cursor tracking
    n = max(2, int(round(duration * FPS)))
    shortcuts, dropped, run, longest = 0, 0, 0, 0
    for i in range(1, n + 1):
        dx, dy = path_fn(i / n)
        acts = e.update([hand("palm", dx=dx, dy=dy)], t)
        t += DT
        shortcuts += sum(1 for a in acts if a.kind in SHORTCUTS)
        if any(a.kind == "move" for a in acts):
            run = 0
        else:
            dropped += 1
            run += 1
            longest = max(longest, run)
    return shortcuts, dropped, round(longest * DT * 1000)


def _line(dx, dy):
    return lambda u: (dx * u, dy * u)


def _circle(r):
    return lambda u: (r * math.cos(2 * math.pi * u) - r, r * math.sin(2 * math.pi * u))



ORDINARY = [
    ("slow_h_0.10_in_1.0s", _line(0.10, 0.0), 1.0),
    pytest.param("moderate_h_0.15_in_0.5s", _line(0.15, 0.0), 0.5),
    ("slow_v_0.08_in_0.8s", _line(0.0, 0.08), 0.8),
    pytest.param("circle_r0.05_1.5s", _circle(0.05), 1.5),
]


@pytest.mark.parametrize("name,path,dur", ORDINARY, ids=["slow_h", "moderate_h", "slow_v", "circle"])
def test_ordinary_move_no_shortcuts_no_dropped_frames(name, path, dur):
    shortcuts, dropped, longest_ms = _run(path, dur)
    print(f"[G1] {name}: shortcuts={shortcuts} dropped={dropped} longest_freeze_ms={longest_ms}")
    assert shortcuts == 0, f"{name}: {shortcuts} shortcut(s) fired"
    assert dropped == 0, f"{name}: {dropped} dropped cursor frames, longest {longest_ms} ms"


def test_fast_flick_measured():
    shortcuts, dropped, longest_ms = _run(_line(0.25, 0.0), 0.15)
    print(f"[G1] flick_0.25_in_0.15s: shortcuts={shortcuts} dropped={dropped} longest_freeze_ms={longest_ms}")
    assert shortcuts <= 1  # a flick may trigger, but not repeatedly
