"""
tools/soak_runner.py — Accelerated Soak Runner & Memory Leak Diagnostic Harness.

Executes an accelerated soak run:
- 1,000 synthetic vision frames (MediaPipe hand landmarks -> GestureEngine -> Controller pipeline).
- 100 scripted voice intents (LocalDecisionEngine scoring & parameter extraction).
- UI state triggers (Dynamic Island event pipeline simulation).
- Thread supervision validation (asserting 0 dead threads).
- Tracemalloc heap monitoring asserting flat memory growth (< 1 MB delta).

Strictly NO Qt imports. 100% unit-testable.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import gc
import math
import os
import sys
import threading
import time
import tracemalloc
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

# Ensure repository root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from config import Config
from gestures import GestureEngine, Hand
from controller import Controller
from decision_engine import get_decision_engine, evaluate_decision
from thread_supervisor import ThreadSupervisor
from tools.replay_harness import synthesize_hand_landmarks


class DummyOverlay:
    """Mock overlay for headless soak testing with no call-history memory retention."""
    def __init__(self) -> None:
        self.last_state: Dict[str, Any] = {}

    def post(self, event_type: str, **kwargs: Any) -> None:
        self.last_state[event_type] = kwargs


@dataclass
class SoakResult:
    """Telemetry report produced by the soak runner."""
    frames_processed: int
    voice_intents_processed: int
    ui_triggers_processed: int
    memory_start_mb: float
    memory_end_mb: float
    memory_delta_mb: float
    peak_memory_mb: float
    dead_threads: int
    duration_seconds: float
    fps: float
    success: bool
    details: Dict[str, Any] = field(default_factory=dict)


def run_soak(
    frames: int = 1000,
    voice_intents: int = 100,
    max_memory_delta_mb: float = 1.0,
    verbose: bool = False,
) -> SoakResult:
    """
    Executes accelerated soak run and measures memory stability and thread supervision.
    """
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()

    # Initialize components
    cfg = Config()
    cfg.tracking_mode = "relative"
    cfg.control_scheme = "palm_fist"

    mock_overlay = DummyOverlay()
    engine = GestureEngine(cfg)
    engine.set_overlay(mock_overlay)
    controller = Controller(cfg, overlay=mock_overlay)
    engine.controller = controller

    dec_engine = get_decision_engine()

    # Thread Supervisor to test thread supervision health
    stop_event = threading.Event()
    supervisor = ThreadSupervisor(stop_event=stop_event)

    worker_iterations = 0
    def background_worker():
        nonlocal worker_iterations
        while not stop_event.is_set():
            worker_iterations += 1
            time.sleep(0.005)

    supervisor.register("SoakWorker", lambda: background_worker, max_restarts=3)
    supervisor.start("SoakWorker")

    # Scripted voice intent corpus (100 natural utterances)
    intent_prototypes = [
        "crank up the audio", "turn up the sound", "make it louder please", "boost volume",
        "turn down the volume", "make it quieter", "lower the sound", "quieter please",
        "mute the sound", "silence audio", "shush jarvis", "mute everything",
        "pause the music", "resume playback", "stop playback", "toggle play pause",
        "skip this song", "next track", "play next song",
        "previous song", "go back a track", "replay track",
        "hide all windows", "show desktop", "minimize everything", "clear the screen",
        "lock the workstation", "lock screen", "secure computer",
        "take a screenshot", "capture the screen", "screen grab",
        "enter stealth mode", "go dark", "cloak suit",
        "clean slate protocol", "wrap it up for today",
        "run system diagnostics", "check vitals", "suit status report",
        "play bohemian rhapsody on spotify", "play jazz on spotify",
        "open chrome", "launch visual studio", "switch to explorer",
        "maximize window", "snap window to left", "snap right",
    ]
    scripted_utterances = [
        intent_prototypes[i % len(intent_prototypes)] for i in range(voice_intents)
    ]

    # Pre-generate 1,000 synthetic hand landmark frames
    # Simulates smooth trajectories, hand opening, fist curling, and dwell hovering
    synthetic_frames: List[List[Hand]] = []
    for i in range(frames):
        t = i / 60.0
        cx = 0.5 + 0.15 * math.sin(t * 1.5)
        cy = 0.5 + 0.10 * math.cos(t * 2.0)
        # Cycle curl from 0.0 (open palm) to 1.0 (tight fist)
        curl = 0.5 + 0.5 * math.sin(t * 3.0)
        lm = synthesize_hand_landmarks(cx=cx, cy=cy, curl=curl)
        synthetic_frames.append([Hand(lm=lm)])

    ui_trigger_count = 0

    with patch("ai.execute_whitelisted_action", return_value=(True, "Executed")), \
         patch("weather_client.get_weather_client"), \
         patch("media_adapter.get_media_adapter"), \
         patch("system_control.volume_up", return_value=50), \
         patch("system_control.volume_down", return_value=50), \
         patch("system_control.get_foreground_window_title", return_value="Desktop"), \
         patch("pyautogui.moveTo"), \
         patch("pyautogui.click"), \
         patch("pyautogui.doubleClick"), \
         patch("pyautogui.mouseDown"), \
         patch("pyautogui.mouseUp"), \
         patch("pyautogui.scroll"):

        # Warm-up phase: exercise pipelines and trigger lazy imports/JIT caches
        for i in range(min(50, frames)):
            actions = engine.update(synthetic_frames[i], now=i * 0.016)
            for act in actions:
                controller.handle(act)
        for it in intent_prototypes:
            dec_engine.classify(it)
            evaluate_decision(it)
        mock_overlay.post("state", active=True)
        mock_overlay.post("hud_volume", level=50)

        gc.collect()
        snap_start = tracemalloc.take_snapshot()
        start_curr, _ = tracemalloc.get_traced_memory()
        start_time = time.perf_counter()

        # 1. Execute 1,000 synthetic vision frames
        t_sim = 0.0
        for i in range(frames):
            now_ts = t_sim
            hands = synthetic_frames[i]
            actions = engine.update(hands, now=now_ts)
            for act in actions:
                controller.handle(act)

            # Trigger mock UI state every 10 frames
            if i % 10 == 0:
                mock_overlay.post("state", active=engine.active, label="ACTIVE" if engine.active else "STANDBY")
                mock_overlay.post("hud_volume", level=50, muted=False)
                ui_trigger_count += 2

            t_sim += 0.016667  # 60 FPS simulated step

        # 2. Execute 100 scripted voice intents
        for i, phrase in enumerate(scripted_utterances):
            match = dec_engine.classify(phrase)
            eval_res = evaluate_decision(phrase)

            # Trigger mock Island UI cards
            mock_overlay.post("live_speech", text=phrase, is_final=True)
            mock_overlay.post("thinking", active=False)
            mock_overlay.post("result", text=f"Executed: {match.intent}", icon="success")
            ui_trigger_count += 3

    end_time = time.perf_counter()
    duration = max(end_time - start_time, 1e-5)
    fps = frames / duration

    # Shutdown background worker and controller
    stop_event.set()
    supervisor.stop("SoakWorker", timeout=0.5)
    controller.shutdown()

    # Check thread supervision
    dead_threads = supervisor.dead_threads_count()
    supervisor_status = supervisor.get_status()

    # Force garbage collection to measure true heap growth
    gc.collect()
    snap_end = tracemalloc.take_snapshot()

    end_curr, peak_val = tracemalloc.get_traced_memory()
    if not was_tracing:
        tracemalloc.stop()

    memory_start_mb = start_curr / (1024 * 1024)
    memory_end_mb = end_curr / (1024 * 1024)
    memory_delta_mb = max(0.0, (end_curr - start_curr) / (1024 * 1024))
    peak_memory_mb = peak_val / (1024 * 1024)

    success = (memory_delta_mb <= max_memory_delta_mb) and (dead_threads == 0)

    if verbose:
        print("=" * 60)
        print("J.A.R.V.I.S. Accelerated Soak Runner Telemetry")
        print("=" * 60)
        print(f"  Frames processed        : {frames:,} frames")
        print(f"  Voice intents processed : {voice_intents:,} intents")
        print(f"  UI triggers simulated   : {ui_trigger_count:,} triggers")
        print(f"  Elapsed wall duration   : {duration:.3f} s ({fps:.1f} frames/sec)")
        print(f"  Baseline heap memory    : {memory_start_mb:.3f} MB")
        print(f"  Final heap memory       : {memory_end_mb:.3f} MB")
        print(f"  Heap memory growth (delta) : {memory_delta_mb:.3f} MB (Threshold: < {max_memory_delta_mb:.1f} MB)")
        print(f"  Peak memory during run  : {peak_memory_mb:.3f} MB")
        print(f"  Dead background threads : {dead_threads}")
        print(f"  Overall Soak Status     : {'PASSED [FLAT MEMORY]' if success else 'FAILED'}")
        print("=" * 60)
        top_diffs = snap_end.compare_to(snap_start, 'lineno')
        print("  Top 5 Memory Allocations:")
        for stat in top_diffs[:5]:
            print("   ", stat)
        print("=" * 60)

    return SoakResult(
        frames_processed=frames,
        voice_intents_processed=voice_intents,
        ui_triggers_processed=ui_trigger_count,
        memory_start_mb=memory_start_mb,
        memory_end_mb=memory_end_mb,
        memory_delta_mb=memory_delta_mb,
        peak_memory_mb=peak_memory_mb,
        dead_threads=dead_threads,
        duration_seconds=duration,
        fps=fps,
        success=success,
        details={
            "worker_iterations": worker_iterations,
            "supervisor_status": supervisor_status,
        },
    )


def main():
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. Accelerated Soak Runner")
    parser.add_argument("--frames", type=int, default=1000, help="Number of synthetic frames (default 1000)")
    parser.add_argument("--intents", type=int, default=100, help="Number of voice intents (default 100)")
    parser.add_argument("--max-delta", type=float, default=1.0, help="Max allowed memory delta in MB (default 1.0)")
    args = parser.parse_args()

    res = run_soak(
        frames=args.frames,
        voice_intents=args.intents,
        max_memory_delta_mb=args.max_delta,
        verbose=True,
    )
    sys.exit(0 if res.success else 1)


if __name__ == "__main__":
    main()
