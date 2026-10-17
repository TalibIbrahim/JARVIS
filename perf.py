"""
perf.py — Low-Overhead Performance Instrumentation, Ring-Buffered Tracing & Latency Budgets.

Provides:
  - Named spans context manager (span(name)) and decorator (measure_span(name)).
  - Ring buffer of recent duration samples (nanoseconds via time.perf_counter_ns).
  - Near-zero overhead when disabled (~20 ns per span check).
  - Trace chains definitions for end-to-end pipelines:
      1. frame->landmarks->gesture->cursor/click
      2. wake->animation
      3. speech->STT->intent->action->TTS
      4. startup phases
  - Percentile calculations (p50, p95, p99 in ms).
  - Latency budget definitions matching J.A.R.V.I.S. performance targets.
  - print_perf_report() outputting formatted budget table with pass/fail status.
  - Hidden debug telemetry flag (default False).

Strictly NO Qt imports. 100% unit-testable with clean interfaces.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import functools
import math
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


# --- Hidden Debug Telemetry Flag ---
DEBUG_TELEMETRY: bool = os.environ.get("JARVIS_DEBUG_TELEMETRY", "0").lower() in ("1", "true", "yes")

# Global profiling enabled switch
_PERF_ENABLED: bool = True
_PERF_LOCK = threading.Lock()


def set_perf_enabled(enabled: bool) -> None:
    """Enables or disables performance span collection."""
    global _PERF_ENABLED
    _PERF_ENABLED = bool(enabled)


def is_perf_enabled() -> bool:
    """Returns True if span collection is active."""
    return _PERF_ENABLED


def set_debug_telemetry(enabled: bool) -> None:
    """Toggles verbose debug telemetry logging for span durations."""
    global DEBUG_TELEMETRY
    DEBUG_TELEMETRY = bool(enabled)


def is_debug_telemetry() -> bool:
    """Returns True if debug telemetry is active."""
    return DEBUG_TELEMETRY


# --- Ring Buffer for Nanosecond Samples ---
class SpanBuffer:
    """Thread-safe ring buffer storing recent duration samples in nanoseconds."""

    def __init__(self, name: str, maxlen: int = 1000):
        self.name = name
        self.maxlen = maxlen
        self.samples: deque[int] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, duration_ns: int) -> None:
        """Appends a duration sample in nanoseconds."""
        with self._lock:
            self.samples.append(duration_ns)

    def count(self) -> int:
        """Returns total number of recorded samples in buffer."""
        with self._lock:
            return len(self.samples)

    def clear(self) -> None:
        """Clears all recorded samples."""
        with self._lock:
            self.samples.clear()

    def percentiles(self) -> Tuple[float, float, float]:
        """
        Calculates (p50, p95, p99) latencies in milliseconds.
        Returns (0.0, 0.0, 0.0) if no samples exist.
        """
        with self._lock:
            if not self.samples:
                return (0.0, 0.0, 0.0)
            sorted_ns = sorted(self.samples)
            n = len(sorted_ns)

        def _calc_pct(p: float) -> float:
            if n == 1:
                return sorted_ns[0] / 1_000_000.0
            idx = (p / 100.0) * (n - 1)
            lower = int(math.floor(idx))
            upper = int(math.ceil(idx))
            weight = idx - lower
            val_ns = sorted_ns[lower] * (1.0 - weight) + sorted_ns[upper] * weight
            return val_ns / 1_000_000.0

        return (_calc_pct(50.0), _calc_pct(95.0), _calc_pct(99.0))

    def min_ms(self) -> float:
        with self._lock:
            return (min(self.samples) / 1_000_000.0) if self.samples else 0.0

    def max_ms(self) -> float:
        with self._lock:
            return (max(self.samples) / 1_000_000.0) if self.samples else 0.0

    def mean_ms(self) -> float:
        with self._lock:
            if not self.samples:
                return 0.0
            return (sum(self.samples) / len(self.samples)) / 1_000_000.0


# Registry of span buffers by name
_SPANS: Dict[str, SpanBuffer] = {}


def get_span_buffer(name: str, maxlen: int = 1000) -> SpanBuffer:
    """Returns or creates a SpanBuffer for the given span name."""
    if name not in _SPANS:
        with _PERF_LOCK:
            if name not in _SPANS:
                _SPANS[name] = SpanBuffer(name, maxlen=maxlen)
    return _SPANS[name]


def record(name: str, duration_ns: int) -> None:
    """Directly records a duration sample in nanoseconds to the named span buffer."""
    buf = get_span_buffer(name)
    buf.add(duration_ns)
    if DEBUG_TELEMETRY:
        dur_ms = duration_ns / 1_000_000.0
        sys.stderr.write(f"[PERF-TELEMETRY] {name}: {dur_ms:.3f} ms\n")


def get_all_spans() -> Dict[str, SpanBuffer]:
    """Returns a snapshot dictionary of all registered span buffers."""
    with _PERF_LOCK:
        return dict(_SPANS)


def reset_all_spans() -> None:
    """Clears samples across all registered span buffers."""
    with _PERF_LOCK:
        for buf in _SPANS.values():
            buf.clear()


# --- No-Op Span for Zero Overhead When Disabled ---
class _NoOpSpan:
    __slots__ = ()

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False


_NOOP = _NoOpSpan()


# --- Active Span Context Manager ---
class SpanContext:
    __slots__ = ("name", "start_ns")

    def __init__(self, name: str):
        self.name = name
        self.start_ns: int = 0

    def __enter__(self) -> SpanContext:
        self.start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        duration_ns = time.perf_counter_ns() - self.start_ns
        record(self.name, duration_ns)
        return False


def span(name: str) -> Union[SpanContext, _NoOpSpan]:
    """
    Context manager for measuring execution time of a named code block.
    When disabled, returns a singleton no-op context manager with ~20ns overhead.
    """
    if not _PERF_ENABLED:
        return _NOOP
    return SpanContext(name)


def measure_span(name_or_func: Optional[Union[str, Callable]] = None) -> Callable:
    """
    Decorator for measuring function latency.
    Can be used as:
      @measure_span
      def my_func(): ...
    or:
      @measure_span("custom_name")
      def my_func(): ...
    """
    def _decorator(func: Callable) -> Callable:
        span_name = (
            name_or_func
            if isinstance(name_or_func, str) and name_or_func
            else f"{func.__module__}.{func.__qualname__}"
        )

        @functools.wraps(func)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _PERF_ENABLED:
                return func(*args, **kwargs)
            t0 = time.perf_counter_ns()
            try:
                return func(*args, **kwargs)
            finally:
                t1 = time.perf_counter_ns()
                record(span_name, t1 - t0)

        return _wrapper

    if callable(name_or_func):
        # Used as @measure_span without arguments
        func = name_or_func
        name_or_func = None
        return _decorator(func)
    return _decorator


# --- Trace Chains Definitions ---
@dataclass
class TraceChain:
    """Defines an end-to-end pipeline consisting of sequential spans."""
    name: str
    spans: List[str]
    budget_ms: float
    description: str = ""

    def evaluate(self, percentile: str = "p95") -> Dict[str, Any]:
        """
        Evaluates the trace chain latency across constituent spans.
        Returns aggregate latencies and pass/fail status against budget.
        """
        all_spans = get_all_spans()
        span_stats: Dict[str, Tuple[float, float, float]] = {}
        total_p50 = 0.0
        total_p95 = 0.0
        total_p99 = 0.0
        has_any_data = False

        for s_name in self.spans:
            buf = all_spans.get(s_name)
            if buf and buf.count() > 0:
                p50, p95, p99 = buf.percentiles()
                span_stats[s_name] = (p50, p95, p99)
                total_p50 += p50
                total_p95 += p95
                total_p99 += p99
                has_any_data = True
            else:
                span_stats[s_name] = (0.0, 0.0, 0.0)

        target_val = total_p95 if percentile == "p95" else (total_p50 if percentile == "p50" else total_p99)
        passed = (target_val <= self.budget_ms) if has_any_data else None

        return {
            "name": self.name,
            "budget_ms": self.budget_ms,
            "p50_ms": total_p50,
            "p95_ms": total_p95,
            "p99_ms": total_p99,
            "has_data": has_any_data,
            "passed": passed,
            "span_stats": span_stats,
        }


TRACE_CHAINS: Dict[str, TraceChain] = {
    "frame->landmarks->gesture->cursor/click": TraceChain(
        name="frame->landmarks->gesture->cursor/click",
        spans=["camera_capture", "landmarks_inference", "gesture_classification", "cursor_update"],
        budget_ms=35.0,
        description="Vision tracking & gesture control loop: capture -> MediaPipe -> pose -> click",
    ),
    "wake->animation": TraceChain(
        name="wake->animation",
        spans=["wake_word_detect", "island_wake_animation"],
        budget_ms=50.0,
        description="Acoustic wake word trigger to Dynamic Island wake state expansion",
    ),
    "speech->STT->intent->action->TTS": TraceChain(
        name="speech->STT->intent->action->TTS",
        spans=["audio_capture_vad", "stt_transcription", "intent_decision", "action_execution", "tts_synthesis"],
        budget_ms=1500.0,
        description="End-to-end voice loop: speech utterance -> Whisper STT -> System 1 reflex -> action -> TTS",
    ),
    "startup phases": TraceChain(
        name="startup phases",
        spans=["startup_dpi", "startup_config", "startup_models", "startup_ui"],
        budget_ms=2500.0,
        description="Application cold-start initialization phases",
    ),
}

# Alias for code lookup convenience
TRACE_CHAINS["startup_phases"] = TRACE_CHAINS["startup phases"]


# --- Latency Budgets Table ---
@dataclass
class BudgetTarget:
    name: str
    budget_ms: float
    percentile: str = "p95"
    description: str = ""
    is_chain: bool = False


LATENCY_BUDGETS: Dict[str, BudgetTarget] = {
    # System 1 Decision Engine
    "intent_decision": BudgetTarget(
        name="intent_decision",
        budget_ms=1.0,
        percentile="p95",
        description="System 1 Reflex intent classification",
    ),
    # Real-Time Pipelines: Vision & Cursor
    "camera_capture": BudgetTarget(
        name="camera_capture",
        budget_ms=33.3,
        percentile="p95",
        description="DirectShow 30 FPS camera frame capture",
    ),
    "landmarks_inference": BudgetTarget(
        name="landmarks_inference",
        budget_ms=25.0,
        percentile="p95",
        description="MediaPipe hand landmark inference",
    ),
    "gesture_classification": BudgetTarget(
        name="gesture_classification",
        budget_ms=1.0,
        percentile="p95",
        description="Metacarpal anchor & openness pose classification",
    ),
    "cursor_update": BudgetTarget(
        name="cursor_update",
        budget_ms=1.0,
        percentile="p95",
        description="Decoupled cursor positioning & SendInput",
    ),
    "cursor_refresh_interval": BudgetTarget(
        name="cursor_refresh_interval",
        budget_ms=8.33,
        percentile="p95",
        description="120+ Hz decoupled cursor tick interval",
    ),
    # Real-Time Pipelines: Audio & Voice
    "wake_word_detect": BudgetTarget(
        name="wake_word_detect",
        budget_ms=50.0,
        percentile="p95",
        description="OpenWakeWord acoustic streaming trigger",
    ),
    "audio_capture_vad": BudgetTarget(
        name="audio_capture_vad",
        budget_ms=200.0,
        percentile="p95",
        description="Energy-based VAD speech endpointing",
    ),
    "stt_transcription": BudgetTarget(
        name="stt_transcription",
        budget_ms=1200.0,
        percentile="p95",
        description="Whisper cloud/local speech-to-text",
    ),
    "action_execution": BudgetTarget(
        name="action_execution",
        budget_ms=10.0,
        percentile="p95",
        description="Whitelisted action execution & dispatch",
    ),
    "tts_synthesis": BudgetTarget(
        name="tts_synthesis",
        budget_ms=200.0,
        percentile="p95",
        description="Paul Bettany neural TTS synthesis",
    ),
    # UI Animations & Startup
    "island_wake_animation": BudgetTarget(
        name="island_wake_animation",
        budget_ms=16.7,
        percentile="p95",
        description="Dynamic Island wake animation frame (60 FPS)",
    ),
    "startup_dpi": BudgetTarget(
        name="startup_dpi",
        budget_ms=10.0,
        percentile="p95",
        description="Windows DPI & AppUserModelID setup",
    ),
    "startup_config": BudgetTarget(
        name="startup_config",
        budget_ms=5.0,
        percentile="p95",
        description="Config file load and validation",
    ),
    "startup_models": BudgetTarget(
        name="startup_models",
        budget_ms=1500.0,
        percentile="p95",
        description="Neural models warm-up (ONNX / Piper / WakeWord)",
    ),
    "startup_ui": BudgetTarget(
        name="startup_ui",
        budget_ms=500.0,
        percentile="p95",
        description="Dynamic Island overlay UI init",
    ),
    # End-to-End Trace Chains
    "frame->landmarks->gesture->cursor/click": BudgetTarget(
        name="frame->landmarks->gesture->cursor/click",
        budget_ms=35.0,
        percentile="p95",
        description="Pipeline: Frame -> Landmarks -> Gesture -> Cursor",
        is_chain=True,
    ),
    "wake->animation": BudgetTarget(
        name="wake->animation",
        budget_ms=50.0,
        percentile="p95",
        description="Pipeline: Wake detection -> Dynamic Island wake",
        is_chain=True,
    ),
    "speech->STT->intent->action->TTS": BudgetTarget(
        name="speech->STT->intent->action->TTS",
        budget_ms=1500.0,
        percentile="p95",
        description="Pipeline: Speech -> STT -> Intent -> Action -> TTS",
        is_chain=True,
    ),
    "startup phases": BudgetTarget(
        name="startup phases",
        budget_ms=2500.0,
        percentile="p95",
        description="Pipeline: Cold-start initialization sequence",
        is_chain=True,
    ),
}


# --- Formatted Perf Report ---
def print_perf_report(out: Optional[Any] = None) -> bool:
    """
    Prints a formatted ASCII latency budget report table with p50, p95, p99
    and pass/fail status for all recorded spans and trace chains.

    Returns:
      True if all recorded spans and trace chains pass their latency budget.
      False if any recorded metric violates its budget.
    """
    output = out or sys.stdout
    all_spans = get_all_spans()

    header = (
        f"{'Metric / Pipeline Span':<42} | {'Budget':<10} | {'p50 (ms)':<9} | "
        f"{'p95 (ms)':<9} | {'p99 (ms)':<9} | {'Samples':<8} | {'Status':<8}"
    )
    divider = "-" * len(header)

    lines = []
    lines.append("\n" + "=" * len(header))
    lines.append("                  J.A.R.V.I.S. LATENCY BUDGET PERFORMANCE REPORT")
    lines.append("=" * len(header))
    lines.append(header)
    lines.append(divider)

    total_evaluated = 0
    total_passed = 0
    total_failed = 0
    total_no_data = 0

    # 1. Individual Latency Budgets
    lines.append(" [Individual Subsystem Spans]")
    for name, target in LATENCY_BUDGETS.items():
        if target.is_chain:
            continue
        buf = all_spans.get(name)
        if buf and buf.count() > 0:
            p50, p95, p99 = buf.percentiles()
            check_val = p95 if target.percentile == "p95" else (p50 if target.percentile == "p50" else p99)
            passed = check_val <= target.budget_ms
            status = "PASS" if passed else "FAIL"
            if passed:
                total_passed += 1
            else:
                total_failed += 1
            total_evaluated += 1
            samples_str = str(buf.count())
            p50_str = f"{p50:8.3f}"
            p95_str = f"{p95:8.3f}"
            p99_str = f"{p99:8.3f}"
        else:
            status = "NO DATA"
            total_no_data += 1
            samples_str = "0"
            p50_str = "   -    "
            p95_str = "   -    "
            p99_str = "   -    "

        lines.append(
            f"  {name:<40} | {target.budget_ms:>7.2f} ms | {p50_str} | "
            f"{p95_str} | {p99_str} | {samples_str:>8} | {status:<8}"
        )

    lines.append(divider)
    # 2. End-to-End Trace Chains
    lines.append(" [End-to-End Trace Chains]")
    for chain_key in [
        "frame->landmarks->gesture->cursor/click",
        "wake->animation",
        "speech->STT->intent->action->TTS",
        "startup phases",
    ]:
        chain = TRACE_CHAINS[chain_key]
        eval_res = chain.evaluate(percentile="p95")
        if eval_res["has_data"]:
            passed = eval_res["passed"]
            status = "PASS" if passed else "FAIL"
            if passed:
                total_passed += 1
            else:
                total_failed += 1
            total_evaluated += 1
            p50_str = f"{eval_res['p50_ms']:8.3f}"
            p95_str = f"{eval_res['p95_ms']:8.3f}"
            p99_str = f"{eval_res['p99_ms']:8.3f}"
            samples_str = "chain"
        else:
            status = "NO DATA"
            total_no_data += 1
            samples_str = "chain"
            p50_str = "   -    "
            p95_str = "   -    "
            p99_str = "   -    "

        lines.append(
            f"  {chain.name:<40} | {chain.budget_ms:>7.2f} ms | {p50_str} | "
            f"{p95_str} | {p99_str} | {samples_str:>8} | {status:<8}"
        )

    lines.append(divider)
    summary = (
        f"Summary: Evaluated: {total_evaluated} | Passed: {total_passed} | "
        f"Failed: {total_failed} | No Data: {total_no_data}"
    )
    lines.append(summary)
    lines.append("=" * len(header) + "\n")

    report_text = "\n".join(lines)
    output.write(report_text + "\n")
    if hasattr(output, "flush"):
        output.flush()

    return total_failed == 0
