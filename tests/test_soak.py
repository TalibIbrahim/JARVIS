"""
tests/test_soak.py — Soak Testing and Memory Leak Harness Suite.

Verifies:
1. 1,000 synthetic vision frames + 100 scripted voice intents + 500 UI events.
2. Tracemalloc memory delta is flat (< 1.0 MB growth).
3. Background thread supervisor maintains 0 dead threads under load.
4. Correct telemetry counting for frames and voice intents.

Strictly NO Qt imports. 100% unit-testable.
"""

from __future__ import annotations

import os
import sys
import unittest

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.soak_runner import run_soak, SoakResult


class TestSoakHarness(unittest.TestCase):
    """Automated soak tests asserting memory stability and thread supervision."""

    def test_accelerated_soak_1000_frames_flat_memory(self):
        """Executes 1,000 frames + 100 intents and asserts flat heap memory growth."""
        result: SoakResult = run_soak(
            frames=1000,
            voice_intents=100,
            max_memory_delta_mb=1.0,
            verbose=False,
        )

        self.assertTrue(result.success, f"Soak run failed telemetry check: {result}")
        self.assertLess(
            result.memory_delta_mb,
            1.0,
            f"Heap memory growth exceeded 1.0 MB: {result.memory_delta_mb:.3f} MB",
        )
        self.assertEqual(
            result.dead_threads,
            0,
            f"Expected 0 dead threads during soak, got {result.dead_threads}",
        )
        self.assertEqual(
            result.frames_processed,
            1000,
            f"Expected 1000 frames, processed {result.frames_processed}",
        )
        self.assertEqual(
            result.voice_intents_processed,
            100,
            f"Expected 100 voice intents, processed {result.voice_intents_processed}",
        )
        self.assertGreater(result.fps, 100.0, f"Expected > 100 fps, got {result.fps:.1f}")

    def test_short_burst_soak_stability(self):
        """Verifies rapid micro-soak execution with 200 frames and 20 intents."""
        result: SoakResult = run_soak(
            frames=200,
            voice_intents=20,
            max_memory_delta_mb=0.5,
            verbose=False,
        )

        self.assertTrue(result.success)
        self.assertLess(result.memory_delta_mb, 0.5)
        self.assertEqual(result.dead_threads, 0)
        self.assertEqual(result.frames_processed, 200)
        self.assertEqual(result.voice_intents_processed, 20)


if __name__ == "__main__":
    unittest.main()
