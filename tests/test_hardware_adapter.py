"""
tests/test_hardware_adapter.py — Unit Tests for Hardware Metrics Adapter.

Verifies:
  1. Human-readable formatters (format_speed, format_gb, format_percent).
  2. HardwareMetrics dataclass and dictionary serialization.
  3. Sampling CPU, RAM, Disk, and Network I/O deltas with mocked psutil.
  4. GPU telemetry extraction with mocked pynvml and graceful fallback when absent.
  5. Lifecycle controls (pause, resume, is_paused) ensuring zero polling when paused.
  6. Listener registration and notification.
  7. Thread-safe background worker loop operation.

Strictly NO Qt imports. 100% unit-testable.
"""

from collections import namedtuple
import time
import unittest
from unittest.mock import MagicMock, patch

from hardware_adapter import (
    HardwareAdapter,
    HardwareMetrics,
    format_speed,
    format_gb,
    format_percent,
)

MockVirtualMem = namedtuple("MockVirtualMem", ["total", "used", "percent"])
MockDiskUsage = namedtuple("MockDiskUsage", ["percent"])
MockNetCounters = namedtuple("MockNetCounters", ["bytes_sent", "bytes_recv"])


class TestHardwareAdapter(unittest.TestCase):
    """Unit tests for HardwareAdapter and metrics formatters."""

    def setUp(self):
        self.clock_val = 1000.0
        self.clock = lambda: self.clock_val
        self.adapter = HardwareAdapter(poll_interval=0.05, clock_fn=self.clock)

    def tearDown(self):
        self.adapter.stop()

    def test_01_formatters(self):
        """Tests format_speed, format_gb, and format_percent across magnitude ranges."""
        # Speed formatter
        self.assertEqual(format_speed(500), "500 B/s")
        self.assertEqual(format_speed(2048), "2.0 KB/s")
        self.assertEqual(format_speed(5.5 * 1024 * 1024), "5.5 MB/s")
        self.assertEqual(format_speed(2.34 * 1024 * 1024 * 1024), "2.34 GB/s")

        # GB formatter
        self.assertEqual(format_gb(16.0), "16.0 GB")
        self.assertEqual(format_gb(7.854), "7.9 GB")

        # Percent formatter
        self.assertEqual(format_percent(45.2), "45%")
        self.assertEqual(format_percent(99.6), "100%")
        self.assertEqual(format_percent(None), "--")

    def test_02_metrics_to_dict(self):
        """HardwareMetrics serializes cleanly to JSON-compatible dictionary."""
        metrics = HardwareMetrics(
            cpu_percent=25.0,
            ram_used_gb=8.0,
            ram_total_gb=16.0,
            ram_percent=50.0,
            gpu_percent=42.0,
            gpu_name="NVIDIA RTX 4080",
            net_up_bytes_sec=1024.0,
            net_down_bytes_sec=4096.0,
            disk_percent=60.0,
            fps=120.0,
            timestamp=1000.0,
        )
        d = metrics.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["cpu_percent"], 25.0)
        self.assertEqual(d["gpu_percent"], 42.0)
        self.assertEqual(d["gpu_name"], "NVIDIA RTX 4080")
        self.assertEqual(d["fps"], 120.0)

    @patch("psutil.cpu_percent")
    @patch("psutil.virtual_memory")
    @patch("psutil.disk_usage")
    @patch("psutil.net_io_counters")
    def test_03_psutil_sampling(self, mock_net, mock_disk, mock_mem, mock_cpu):
        """Verifies accurate metric extraction from psutil."""
        mock_cpu.return_value = 18.5
        mock_mem.return_value = MockVirtualMem(
            total=16 * (1024 ** 3),
            used=8 * (1024 ** 3),
            percent=50.0,
        )
        mock_disk.return_value = MockDiskUsage(percent=65.2)

        # First sample primes net counters
        mock_net.return_value = MockNetCounters(bytes_sent=1000000, bytes_recv=5000000)
        self.adapter._last_net_counters = None
        self.adapter.sample()

        # Advance clock by 2 seconds and update net counters
        self.clock_val = 1002.0
        mock_net.return_value = MockNetCounters(bytes_sent=1020000, bytes_recv=5100000)

        metrics = self.adapter.sample()

        self.assertAlmostEqual(metrics.cpu_percent, 18.5)
        self.assertAlmostEqual(metrics.ram_total_gb, 16.0)
        self.assertAlmostEqual(metrics.ram_used_gb, 8.0)
        self.assertAlmostEqual(metrics.ram_percent, 50.0)
        self.assertAlmostEqual(metrics.disk_percent, 65.2)

        # Net deltas: (1020000 - 1000000) / 2s = 10000 B/s; (5100000 - 5000000) / 2s = 50000 B/s
        self.assertAlmostEqual(metrics.net_up_bytes_sec, 10000.0)
        self.assertAlmostEqual(metrics.net_down_bytes_sec, 50000.0)

    def test_04_pynvml_sampling_success(self):
        """Verifies NVIDIA GPU telemetry when pynvml is available."""
        mock_nvml = MagicMock()
        mock_handle = MagicMock()
        mock_rates = MagicMock()
        mock_rates.gpu = 68.0

        mock_nvml.nvmlDeviceGetHandleByIndex.return_value = mock_handle
        mock_nvml.nvmlDeviceGetUtilizationRates.return_value = mock_rates
        mock_nvml.nvmlDeviceGetName.return_value = b"NVIDIA GeForce RTX 3080"

        with patch.dict("sys.modules", {"pynvml": mock_nvml}):
            adapter = HardwareAdapter(clock_fn=self.clock)
            pct, name = adapter._sample_gpu()
            self.assertEqual(pct, 68.0)
            self.assertEqual(name, "NVIDIA GeForce RTX 3080")

    def test_05_pynvml_fallback_when_absent(self):
        """When NVML is unavailable or fails, returns (None, None) gracefully."""
        self.adapter._nvml_failed = True
        pct, name = self.adapter._sample_gpu()
        self.assertIsNone(pct)
        self.assertIsNone(name)

    def test_06_pause_resume_lifecycle(self):
        """pause() stops polling immediately, resume() re-enables polling."""
        self.assertFalse(self.adapter.is_paused())

        self.adapter.pause()
        self.assertTrue(self.adapter.is_paused())
        self.assertFalse(self.adapter._resume_event.is_set())

        self.adapter.resume()
        self.assertFalse(self.adapter.is_paused())
        self.assertTrue(self.adapter._resume_event.is_set())

    def test_07_listener_notifications(self):
        """Listeners receive updated metrics on sample()."""
        received = []
        self.adapter.add_listener(lambda m: received.append(m))

        self.adapter.sample()
        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], HardwareMetrics)

        # Remove listener
        self.adapter.remove_listener(received.append)
        self.adapter.sample()
        self.assertEqual(len(received), 2)  # lambda still registered

    def test_08_set_fps(self):
        """External render or tracking engine can set FPS."""
        self.adapter.set_fps(144.0)
        metrics = self.adapter.sample()
        self.assertEqual(metrics.fps, 144.0)

    def test_09_background_worker_pauses_without_cpu_waste(self):
        """Worker thread pauses on resume event when adapter is paused."""
        sample_count = [0]
        orig_sample = self.adapter.sample

        def counting_sample():
            sample_count[0] += 1
            return orig_sample()

        self.adapter.sample = counting_sample
        self.adapter.start()

        # Let it run briefly
        time.sleep(0.12)
        count_before_pause = sample_count[0]
        self.assertGreater(count_before_pause, 0)

        # Pause adapter (simulating collapsed/hidden island)
        self.adapter.pause()
        time.sleep(0.15)
        count_during_pause = sample_count[0]

        # No new samples should be taken while paused
        self.assertEqual(count_during_pause, count_before_pause)

        # Resume adapter
        self.adapter.resume()
        time.sleep(0.12)
        self.assertGreater(sample_count[0], count_during_pause)


if __name__ == "__main__":
    unittest.main()
