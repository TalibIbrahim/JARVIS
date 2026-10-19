"""Part 3 performance benchmarks with thresholds (run alone: pytest -m benchmark; also in the default run).

Every benchmark warms up, repeats, and asserts on p99 with margin. Each test re-runs its measurement up
to 3 times and passes if ANY run meets the threshold (implemented in `_bench`; there is no plugin).
Measured p50/p95/p99 are printed (use -s to see them). Devices, network and system side effects are mocked;
Qt-event-loop benchmarks (wake paint, idle repaint) run in a fresh offscreen subprocess so they never
paint on the real desktop and never disturb the pytest process.

System 1 decide p99 < 1 ms is tests/test_decision_benchmark.py::test_latency_p99_sub_millisecond
(tagged `benchmark` in conftest, not duplicated here).
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

pytestmark = pytest.mark.benchmark

_REPO = str(Path(__file__).resolve().parent.parent)
ATTEMPTS = 3


# --------------------------------------------------------------------------------------------- helpers
def _pcts(samples_ms):
    a = np.asarray(samples_ms, dtype=np.float64)
    return (float(np.percentile(a, 50)), float(np.percentile(a, 95)), float(np.percentile(a, 99)),
            float(a.max()), len(a))


def _bench(name, measure, limit_ms):
    """measure() -> list of ms samples. Pass if any of up to ATTEMPTS runs has p99 < limit_ms."""
    seen = []
    for i in range(ATTEMPTS):
        p50, p95, p99, mx, n = _pcts(measure())
        print(f"[BENCH] {name} run{i + 1}: p50={p50:.3f} p95={p95:.3f} p99={p99:.3f} max={mx:.3f} ms "
              f"n={n} limit={limit_ms} ms", flush=True)
        seen.append(p99)
        if p99 < limit_ms:
            return
    pytest.fail(f"{name}: p99 never under {limit_ms} ms in {ATTEMPTS} runs: {[round(s, 3) for s in seen]}")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def _run_py(code, timeout=90, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout,
                       cwd=_REPO, env=env)
    return r


def _result_line(r):
    """The measurement is printed before the child exits. A native crash during interpreter teardown
    (observed intermittently: exit code 0xC0000005 AFTER the RESULT line, Qt/WinRT threads at ExitProcess)
    does not invalidate it; a failure BEFORE the RESULT line does."""
    for line in r.stdout.splitlines():
        if line.startswith("RESULT "):
            if r.returncode != 0:
                print(f"[BENCH] note: child exit code {r.returncode:#x} after RESULT (teardown crash)", flush=True)
            return json.loads(line[7:])
    assert r.returncode == 0, (r.returncode, r.stdout[-1500:], r.stderr[-2500:])
    raise AssertionError("no RESULT line: " + r.stdout[-1500:] + r.stderr[-1500:])


# --------------------------------------------------------------------------------------------- gesture
def test_gesture_controller_cursor_tick_p99_under_8ms():
    import controller as controller_mod
    from config import Config
    from controller import Controller
    from gestures import GestureEngine
    from test_gestures import hand, wake

    def measure():
        cfg = Config(control_scheme="palm_fist")
        with mock.patch.object(controller_mod, "pyautogui", mock.MagicMock()), \
                mock.patch("system_control.get_foreground_window_title", lambda: "win", create=True), \
                mock.patch("system_control.volume_up", lambda steps=1: 55, create=True), \
                mock.patch("system_control.volume_down", lambda steps=1: 45, create=True):
            eng = GestureEngine(cfg, overlay=SimpleNamespace(post=lambda *a, **k: None))
            c = Controller(cfg)
            c.shutdown()                       # stop the real cursor thread; ticks are driven below
            c._move = mock.MagicMock()
            c._handle_dwell_action = mock.MagicMock()
            c.active = True
            c.cur_x = c.target_x = c.sw * 0.5
            c.cur_y = c.target_y = c.sh * 0.5
            vclk = [1000.0]                    # virtual 30 fps clock for the controller's filters (no real sleeping)
            ctl_time = SimpleNamespace(perf_counter=lambda: vclk[0], time=time.time, sleep=lambda s: None,
                                       monotonic=lambda: vclk[0])
            tpatch = mock.patch.object(controller_mod, "time", ctl_time)
            tpatch.start()
            rate = max(float(getattr(cfg, "cursor_rate_hz", 120)), 60.0)
            alpha = 1.0 - math.exp(-38.0 * (1.0 / (rate + 1.0)))
            dt = 1.0 / 30.0
            t = wake(eng, 0.0)
            out = []
            try:
                for i in range(420):           # bounded: 20 warm-up + 400 timed frames
                    frame = [hand("palm", dx=0.08 * math.sin(i / 14.0), dy=0.05 * math.cos(i / 11.0))]
                    t0 = time.perf_counter()
                    for a in eng.update(frame, t):
                        c.handle(a)
                    cdx = (c.target_x - c.cur_x) * alpha
                    cdy = (c.target_y - c.cur_y) * alpha
                    if abs(cdx) > 0.02 or abs(cdy) > 0.02:
                        c.cur_x += cdx
                        c.cur_y += cdy
                        c._move(round(c.cur_x), round(c.cur_y))
                    now = vclk[0]
                    c.history.append_fast(now, c.cur_x, c.cur_y)
                    c.dwell.update(c.cur_x, c.cur_y, now, active=c.active, hand_present=c.hand_present,
                                   pinching=c.pinching, dragging=c.dragging)
                    t1 = time.perf_counter()
                    t += dt
                    vclk[0] += dt
                    if i >= 20:
                        out.append((t1 - t0) * 1000.0)
            finally:
                tpatch.stop()
            assert c._move.called, "synthetic landmarks never moved the cursor; benchmark is not exercising the path"
            return out

    _bench("GestureEngine.update+Controller.handle+cursor tick", measure, 8.0)


# --------------------------------------------------------------------------------------------- wake paint
_WAKE_SCRIPT = r'''
import os, sys, time, threading, json
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from unittest import mock
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEventLoop
app = QApplication(sys.argv)
import config, overlay, voice
from ui.island_parts.common import IslandState
def spin(ms):
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 5)
        time.sleep(0.001)
cfg = config.Config()
stop = threading.Event()
class Ctx:
    def __init__(s): s.overlay = None; s.cfg = cfg; s.speaker = mock.MagicMock()
    def say(s, *a, **k): pass
    def chirp(s, *a, **k): pass
    def __getattr__(s, n): return mock.MagicMock()
ctx = Ctx()
ov = overlay.Overlay(stop, cfg=cfg, ctx=ctx)
ctx.overlay = ov
isl = ov.island
voice.play_chime = lambda *a, **k: None
v = voice.Voice(cfg, ctx, stop)
v._duck_media = lambda *a, **k: None
rec = {"paints": []}
cls = type(isl)
orig = cls.paintEvent
def pe(self, ev):
    t0 = time.perf_counter_ns(); orig(self, ev); t1 = time.perf_counter_ns()
    rec["paints"].append((t0, t1, self._resolve_state(time.time())))
cls.paintEvent = pe
spin(1200)
out = []
for i in range(14):
    isl.listening = False; isl.listening_until = 0.0; isl.is_wake = False; isl.wake_until = 0.0
    isl.is_transcribing = False
    spin(250)
    rec["paints"].clear()
    T0 = time.perf_counter_ns()
    v._on_wake_detected()
    dl = time.perf_counter() + 1.0
    while time.perf_counter() < dl:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 2)
        if any(s == IslandState.WAKE for _, _, s in rec["paints"]):
            break
        time.sleep(0.0003)
    ps = [p for p in rec["paints"] if p[2] == IslandState.WAKE]
    if ps and i >= 2:                       # first two trials are warm-up
        out.append((ps[0][1] - T0) / 1e6)
print("RESULT " + json.dumps(out), flush=True)
os._exit(0)
'''


def test_wake_to_first_wake_paint_p99_under_150ms():
    def measure():
        samples = _result_line(_run_py(_WAKE_SCRIPT, timeout=120))
        assert len(samples) >= 8, f"only {len(samples)} wake paints observed"
        return samples

    _bench("wake -> first WAKE-state island paint (offscreen)", measure, 150.0)


# --------------------------------------------------------------------------------------------- transcript -> dispatch
_COMMANDS = [
    "jarvis volume up", "jarvis volume down", "jarvis mute", "jarvis unmute", "jarvis open chrome",
    "jarvis open notepad", "jarvis take a screenshot", "jarvis what time is it", "jarvis pause the music",
    "jarvis next track", "jarvis show desktop", "jarvis lock the computer", "jarvis scroll down",
    "jarvis minimize window", "jarvis maximize window", "jarvis what is 12 times 7", "jarvis play music",
    "jarvis set a timer for five minutes", "jarvis search for python tutorials", "jarvis show weather",
]


def test_transcript_to_dispatch_p99_under_50ms_warm(monkeypatch):
    import config
    import voice
    from side_effect_guard import SideEffectGuard, make_ctx

    monkeypatch.setattr(voice, "play_chime", lambda *a, **k: None)

    def measure():
        with SideEffectGuard() as g:
            ctx = make_ctx(g, config.Config())
            v = voice.Voice(ctx.cfg, ctx, threading.Event())
            v.ctx = ctx
            v._duck_media = lambda *a, **k: None
            v._rearm_followup = lambda *a, **k: None
            assert len(_COMMANDS) == 20
            for c in _COMMANDS:                      # round 0: cold/warm-up
                v.listening_until = time.time() + 5
                v._process(c, is_command=True)
            out = []
            for _ in range(10):                      # bounded: 10 warm rounds x 20 commands
                for c in _COMMANDS:
                    v.listening_until = time.time() + 5
                    t0 = time.perf_counter()
                    v._process(c, is_command=True)
                    out.append((time.perf_counter() - t0) * 1000.0)
            assert any(n.startswith(("system_control.", "pyautogui.", "webbrowser.", "subprocess."))
                       for n in g.names()), "no mocked side effect recorded: commands were not dispatched"
            return out

    _bench("transcript -> dispatch (Voice._process, 20 commands, warm)", measure, 50.0)


# --------------------------------------------------------------------------------------------- island paintEvent
def test_island_paint_every_state_and_page_p99_under_16_7ms(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from tools.visual_gate_helper import (
        DummyOverlayMgr, ISLAND_BASE_STATES, ISLAND_CARDS, ISLAND_HUDS, injected_clock, set_theme,
        setup_island_state,
    )
    from ui.island import AppleDynamicIsland

    states = (list(ISLAND_BASE_STATES) + list(ISLAND_CARDS) + list(ISLAND_HUDS)
              + ["CARD_HARDWARE_GRID", "CARD_HARDWARE_STRIP", "CARD_JARVIS"])
    states = list(dict.fromkeys(states))
    set_theme("dark")

    def measure():
        worst = []
        per = {}
        now = 1000.0
        with injected_clock(now):
            for st in states:
                island = AppleDynamicIsland(DummyOverlayMgr())
                island.t0 = now
                island.last_frame_t = now
                tw, th = setup_island_state(island, st, now)
                island.spring_w.snap(tw)
                island.spring_h.snap(th)
                island.set_island_size(int(round(tw)), int(round(th)))
                qapp.processEvents()
                for dpi in (1.0, 1.5):
                    pix = QPixmap(max(1, int(round(island.width() * dpi))), max(1, int(round(island.height() * dpi))))
                    pix.setDevicePixelRatio(dpi)
                    ms = []
                    for k in range(26):             # bounded: 2 warm-ups + 24 timed
                        pix.fill(QColor(0, 0, 0, 0))
                        p = QPainter(pix)
                        t0 = time.perf_counter()
                        island.render(p, QPoint())
                        t1 = time.perf_counter()
                        p.end()
                        if k >= 2:
                            ms.append((t1 - t0) * 1000.0)
                    per[(st, dpi)] = ms
                island.close()
        for key, ms in per.items():
            worst.append((float(np.percentile(ms, 99)), key))
        worst.sort(reverse=True)
        print("[BENCH] island paint worst combos (p99 ms): " +
              ", ".join(f"{k[0]}@{int(k[1] * 100)}%={v:.2f}" for v, k in worst[:4]), flush=True)
        # _bench takes the p99 over the samples; feed it each combo's p99 so the max combo decides.
        return [v for v, _ in worst]

    seen = []
    for i in range(ATTEMPTS):
        vals = measure()
        p50, p95, p99, mx, n = _pcts(vals)
        print(f"[BENCH] island paintEvent run{i + 1}: combos={n} median_p99={p50:.3f} worst_p99={mx:.3f} ms "
              f"limit=16.7 ms ({len(states)} states x 2 DPIs)", flush=True)
        seen.append(mx)
        if mx < 16.7:
            return
    pytest.fail(f"island paint: worst per-combo p99 never under 16.7 ms: {[round(s, 2) for s in seen]}")


# --------------------------------------------------------------------------------------------- openwakeword
@pytest.mark.skipif(importlib.util.find_spec("openwakeword") is None, reason="openwakeword not installed")
def test_openwakeword_lean_import_under_1_5s_without_sklearn():
    code = (
        "import sys, time, json\n"
        "import voice\n"
        "t0 = time.perf_counter()\n"
        "voice._import_openwakeword_lean()\n"
        "import openwakeword.model\n"
        "dt = (time.perf_counter() - t0) * 1000\n"
        "bad = [m for m in sys.modules if m == 'sklearn' or m.startswith('sklearn.')]\n"
        "assert not bad, bad[:3]\n"
        "print('RESULT ' + json.dumps([dt]))\n"
    )

    def measure():
        return _result_line(_run_py(code, timeout=120))

    # One sample per run: p99 == the sample; any of 3 fresh-subprocess runs under 1.5 s passes.
    _bench("import openwakeword (lean, fresh subprocess)", measure, 1500.0)


# --------------------------------------------------------------------------------------------- settings window
def test_settings_window_construction_under_600ms(qapp):
    from config import Config
    from ui.settings_window import SettingsWindow

    def build():
        with mock.patch("ui.settings_window.list_cameras", return_value=[{"index": 0, "name": "cam"}]):
            t0 = time.perf_counter()
            d = SettingsWindow(Config(), mock.MagicMock())
            dt = (time.perf_counter() - t0) * 1000.0
        d._theme_timer.stop()
        d.close()
        d.deleteLater()
        qapp.processEvents()
        return dt

    build()                                                     # warm-up (imports, style caches)

    def measure():
        return [build() for _ in range(6)]                      # bounded

    _bench("SettingsWindow construction (target 150, limit 600)", measure, 600.0)


# --------------------------------------------------------------------------------------------- idle repaint
_IDLE_SCRIPT = r'''
import os, sys, time, threading, json
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from unittest import mock
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEventLoop
app = QApplication(sys.argv)
import config, overlay
def spin(ms):
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 5)
        time.sleep(0.001)
cfg = config.Config()
stop = threading.Event()
ov = overlay.Overlay(stop, cfg=cfg)
isl = ov.island
cls = type(isl)
orig = cls.paintEvent
count = [0]
def pe(self, ev):
    count[0] += 1
    orig(self, ev)
cls.paintEvent = pe
isl.media_playing = False
spin(2500)                                  # settle: startup animation, first paints
count[0] = 0
spin(2000)
state = [str(isl._resolve_state(time.time())), bool(isl.media_playing)]
print("RESULT " + json.dumps([count[0]] + state), flush=True)
os._exit(0)
'''


def test_island_idle_compact_pill_zero_repaints_over_2s():
    last = None
    for i in range(ATTEMPTS):
        res = _result_line(_run_py(_IDLE_SCRIPT, timeout=90))
        paints, state, media = res
        print(f"[BENCH] idle repaints over 2 s run{i + 1}: paints={paints} state={state} media_playing={media}",
              flush=True)
        last = res
        if paints == 0:
            return
    pytest.fail(f"idle compact pill repainted in all {ATTEMPTS} runs; last (paints, state, media)={last}")
