"""
hardware_adapter.py — J.A.R.V.I.S. System Hardware Metrics Adapter.

Monitors system resources (CPU, RAM, GPU, Network I/O, Disk, FPS) using psutil
and pynvml with zero background CPU overhead when paused.

Features:
  - Background thread polling at ~1s intervals.
  - Lifecycle controls (pause, resume, is_paused): strictly pauses all metric collection
    when collapsed or hidden, preventing idle power drain.
  - Pure Python, 100% unit-testable with mock psutil/pynvml backends; strictly NO Qt imports.
  - Formatters: format_speed(), format_gb(), format_percent().
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


def format_speed(bytes_per_sec: float) -> str:
    """Formats bytes per second into human-readable B/s, KB/s, MB/s, or GB/s."""
    val = max(0.0, float(bytes_per_sec))
    if val < 1024.0:
        return f"{val:.0f} B/s"
    elif val < 1024.0 * 1024.0:
        return f"{val / 1024.0:.1f} KB/s"
    elif val < 1024.0 * 1024.0 * 1024.0:
        return f"{val / (1024.0 * 1024.0):.1f} MB/s"
    else:
        return f"{val / (1024.0 * 1024.0 * 1024.0):.2f} GB/s"


def format_gb(val_gb: float) -> str:
    """Formats gigabyte numbers to 1 decimal place."""
    return f"{float(val_gb):.1f} GB"


def format_percent(val: Optional[float]) -> str:
    """Formats percentage or '--' if None."""
    if val is None:
        return "--"
    return f"{round(float(val)):.0f}%"


@dataclass
class HardwareMetrics:
    """Snapshot of system hardware utilization and telemetry."""
    cpu_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_percent: float = 0.0
    gpu_percent: Optional[float] = None
    gpu_name: Optional[str] = None
    net_up_bytes_sec: float = 0.0
    net_down_bytes_sec: float = 0.0
    disk_percent: float = 0.0
    fps: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cpu_percent": self.cpu_percent,
            "ram_used_gb": self.ram_used_gb,
            "ram_total_gb": self.ram_total_gb,
            "ram_percent": self.ram_percent,
            "gpu_percent": self.gpu_percent,
            "gpu_name": self.gpu_name,
            "net_up_bytes_sec": self.net_up_bytes_sec,
            "net_down_bytes_sec": self.net_down_bytes_sec,
            "disk_percent": self.disk_percent,
            "fps": self.fps,
            "timestamp": self.timestamp,
        }


class HardwareAdapter:
    """
    Thread-safe hardware telemetry adapter.
    Pauses metric sampling when the Dynamic Island is collapsed or hidden.
    """

    def __init__(
        self,
        poll_interval: float = 1.0,
        clock_fn: Callable[[], float] = time.time,
    ):
        self._poll_interval = float(poll_interval)
        self._clock = clock_fn
        self._lock = threading.Lock()

        self._is_paused: bool = False
        self._resume_event = threading.Event()
        self._resume_event.set()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._last_metrics = HardwareMetrics()
        self._listeners: List[Callable[[HardwareMetrics], None]] = []

        # Network delta tracking
        self._last_net_counters: Optional[Any] = None
        self._last_net_time: float = 0.0

        # NVML state
        self._nvml_initialized: bool = False
        self._nvml_failed: bool = False

        # External FPS indicator
        self._current_fps: float = 60.0

        # Prime initial net counters if available
        if _PSUTIL_AVAILABLE:
            try:
                self._last_net_counters = psutil.net_io_counters()
                self._last_net_time = self._clock()
            except Exception:
                pass

    def start(self) -> None:
        """Starts background monitoring thread."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="HardwareAdapterWorker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stops background thread and releases resources."""
        self._stop_event.set()
        self._resume_event.set()  # Unblock if waiting on pause
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._cleanup_nvml()

    def pause(self) -> None:
        """Pauses polling immediately. Zero CPU usage when collapsed or hidden."""
        self._is_paused = True
        self._resume_event.clear()

    def resume(self) -> None:
        """Resumes active polling when the hardware page becomes visible."""
        self._is_paused = False
        self._resume_event.set()

    def is_paused(self) -> bool:
        """Returns True if telemetry collection is currently paused."""
        return self._is_paused

    def set_fps(self, fps: float) -> None:
        """Sets the current display or render loop FPS."""
        with self._lock:
            self._current_fps = max(0.0, float(fps))

    def add_listener(self, callback: Callable[[HardwareMetrics], None]) -> None:
        """Registers a listener for updated hardware metrics."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[HardwareMetrics], None]) -> None:
        """Unregisters a hardware metrics listener."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def get_metrics(self) -> HardwareMetrics:
        """Returns the most recent hardware metrics."""
        with self._lock:
            return self._last_metrics

    def sample(self) -> HardwareMetrics:
        """
        Samples hardware metrics directly using psutil and NVML.
        Updates internal cache and notifies listeners.
        """
        now = self._clock()

        cpu_pct = 0.0
        ram_used = 0.0
        ram_total = 0.0
        ram_pct = 0.0
        disk_pct = 0.0
        net_up = 0.0
        net_down = 0.0

        if _PSUTIL_AVAILABLE:
            # 1. CPU
            try:
                cpu_pct = float(psutil.cpu_percent(interval=None))
            except Exception:
                cpu_pct = 0.0

            # 2. RAM
            try:
                mem = psutil.virtual_memory()
                ram_total = float(mem.total) / (1024.0 ** 3)
                ram_used = float(mem.used) / (1024.0 ** 3)
                ram_pct = float(mem.percent)
            except Exception:
                pass

            # 3. Disk Usage (primary drive)
            try:
                drive = "C:\\" if sys.platform == "win32" else "/"
                disk = psutil.disk_usage(drive)
                disk_pct = float(disk.percent)
            except Exception:
                pass

            # 4. Network Rate Delta
            try:
                net = psutil.net_io_counters()
                if self._last_net_counters is not None:
                    dt = now - self._last_net_time
                    if dt > 0.05:
                        sent_bytes = max(0, net.bytes_sent - self._last_net_counters.bytes_sent)
                        recv_bytes = max(0, net.bytes_recv - self._last_net_counters.bytes_recv)
                        net_up = float(sent_bytes) / dt
                        net_down = float(recv_bytes) / dt
                self._last_net_counters = net
                self._last_net_time = now
            except Exception:
                pass

        # 5. GPU Telemetry
        gpu_pct, gpu_name = self._sample_gpu()

        with self._lock:
            fps_val = self._current_fps

        metrics = HardwareMetrics(
            cpu_percent=cpu_pct,
            ram_used_gb=ram_used,
            ram_total_gb=ram_total,
            ram_percent=ram_pct,
            gpu_percent=gpu_pct,
            gpu_name=gpu_name,
            net_up_bytes_sec=net_up,
            net_down_bytes_sec=net_down,
            disk_percent=disk_pct,
            fps=fps_val,
            timestamp=now,
        )

        with self._lock:
            self._last_metrics = metrics
            listeners = list(self._listeners)

        for l in listeners:
            try:
                l(metrics)
            except Exception as exc:
                print(f"[hardware_adapter] Listener error: {exc}")

        return metrics

    def _sample_gpu(self) -> Tuple[Optional[float], Optional[str]]:
        """Samples NVIDIA GPU utilization and name via pynvml with graceful fallback."""
        if self._nvml_failed:
            return None, None

        if not self._nvml_initialized:
            try:
                import pynvml
                pynvml.nvmlInit()
                self._nvml_initialized = True
            except Exception:
                self._nvml_failed = True
                return None, None

        try:
            import pynvml
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            rates = pynvml.nvmlDeviceGetUtilizationRates(handle)
            name_raw = pynvml.nvmlDeviceGetName(handle)
            name = name_raw.decode("utf-8") if isinstance(name_raw, bytes) else str(name_raw)
            return float(rates.gpu), name
        except Exception:
            return None, None

    def _cleanup_nvml(self) -> None:
        """Shuts down NVML safely on exit."""
        if self._nvml_initialized:
            try:
                import pynvml
                pynvml.nvmlShutdown()
            except Exception:
                pass
            finally:
                self._nvml_initialized = False

    def _worker_loop(self) -> None:
        """Background thread executing periodic hardware metric sampling."""
        while not self._stop_event.is_set():
            # Wait if paused (Zero CPU overhead while collapsed or hidden)
            self._resume_event.wait()
            if self._stop_event.is_set():
                break

            try:
                self.sample()
            except Exception as exc:
                print(f"[hardware_adapter] Worker sampling error: {exc}")

            # Sleep poll interval with interruptible wait
            self._stop_event.wait(timeout=self._poll_interval)


# Global singleton hardware adapter
_HARDWARE_ADAPTER: Optional[HardwareAdapter] = None


def get_hardware_adapter() -> HardwareAdapter:
    global _HARDWARE_ADAPTER
    if _HARDWARE_ADAPTER is None:
        _HARDWARE_ADAPTER = HardwareAdapter()
    return _HARDWARE_ADAPTER


def set_hardware_adapter(adapter: Optional[HardwareAdapter]) -> None:
    global _HARDWARE_ADAPTER
    _HARDWARE_ADAPTER = adapter
