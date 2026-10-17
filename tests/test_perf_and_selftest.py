"""
tests/test_perf_and_selftest.py — Unit tests for perf.py and self_test.py.

Verifies:
  - SpanBuffer sample recording, circular eviction, percentile calculations (p50, p95, p99).
  - span() context manager and measure_span() decorator.
  - Zero-overhead when disabled.
  - Hidden debug telemetry flag and output.
  - Trace chains evaluation (hand tracking, wake animation, voice pipeline, startup phases).
  - Latency budget table integrity and print_perf_report() formatting.
  - self_test.py pre-flight diagnostics (config, camera, mic, models, network, media, DPI).
  - main.py CLI integration for --perf-report and --self-test without mutex locking.

Strictly NO Qt imports. 100% unit-testable with clean interfaces and mocks.
"""

import io
import math
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import perf
import self_test


class TestPerfSpanBuffer(unittest.TestCase):
    """Tests SpanBuffer ring buffer operations and statistical calculations."""

    def setUp(self):
        self.buf = perf.SpanBuffer("test_metric", maxlen=5)

    def test_add_and_count(self):
        self.assertEqual(self.buf.count(), 0)
        self.buf.add(1_000_000)  # 1 ms
        self.buf.add(2_000_000)  # 2 ms
        self.assertEqual(self.buf.count(), 2)

    def test_ring_buffer_eviction(self):
        for i in range(10):
            self.buf.add(i * 1_000_000)
        self.assertEqual(self.buf.count(), 5)
        # Should contain the last 5 samples: 5, 6, 7, 8, 9 ms
        self.assertAlmostEqual(self.buf.min_ms(), 5.0, places=3)
        self.assertAlmostEqual(self.buf.max_ms(), 9.0, places=3)

    def test_empty_percentiles(self):
        p50, p95, p99 = self.buf.percentiles()
        self.assertEqual((p50, p95, p99), (0.0, 0.0, 0.0))
        self.assertEqual(self.buf.mean_ms(), 0.0)

    def test_percentile_calculation(self):
        buf = perf.SpanBuffer("dense", maxlen=100)
        # Add values from 1 ms to 100 ms
        for i in range(1, 101):
            buf.add(i * 1_000_000)

        p50, p95, p99 = buf.percentiles()
        self.assertAlmostEqual(p50, 50.5, delta=1.0)
        self.assertAlmostEqual(p95, 95.05, delta=1.5)
        self.assertAlmostEqual(p99, 99.01, delta=1.0)
        self.assertAlmostEqual(buf.mean_ms(), 50.5, delta=0.5)

    def test_clear(self):
        self.buf.add(5_000_000)
        self.buf.clear()
        self.assertEqual(self.buf.count(), 0)


class TestPerfTracing(unittest.TestCase):
    """Tests span() context manager and measure_span() decorator."""

    def setUp(self):
        perf.set_perf_enabled(True)
        perf.set_debug_telemetry(False)
        perf.reset_all_spans()

    def tearDown(self):
        perf.set_perf_enabled(True)
        perf.set_debug_telemetry(False)

    def test_span_context_manager(self):
        with perf.span("custom_operation"):
            time.sleep(0.005)

        buf = perf.get_span_buffer("custom_operation")
        self.assertEqual(buf.count(), 1)
        self.assertGreaterEqual(buf.mean_ms(), 3.0)

    def test_measure_span_decorator_no_args(self):
        @perf.measure_span
        def sample_worker():
            time.sleep(0.003)
            return 42

        result = sample_worker()
        self.assertEqual(result, 42)

        qualname = f"{sample_worker.__module__}.{sample_worker.__qualname__}"
        buf = perf.get_span_buffer(qualname)
        self.assertEqual(buf.count(), 1)
        self.assertGreaterEqual(buf.mean_ms(), 2.0)

    def test_measure_span_decorator_with_name(self):
        @perf.measure_span("named_task")
        def sample_named():
            return "ok"

        res = sample_named()
        self.assertEqual(res, "ok")
        buf = perf.get_span_buffer("named_task")
        self.assertEqual(buf.count(), 1)

    def test_zero_overhead_when_disabled(self):
        perf.set_perf_enabled(False)

        with perf.span("disabled_span"):
            pass

        buf = perf.get_span_buffer("disabled_span")
        self.assertEqual(buf.count(), 0)

        @perf.measure_span("disabled_func")
        def dummy_fn():
            return 99

        self.assertEqual(dummy_fn(), 99)
        buf2 = perf.get_span_buffer("disabled_func")
        self.assertEqual(buf2.count(), 0)

    def test_debug_telemetry(self):
        perf.set_debug_telemetry(True)
        self.assertTrue(perf.is_debug_telemetry())

        err_capture = io.StringIO()
        with patch.object(sys, "stderr", err_capture):
            perf.record("telemetry_test", 1_500_000)

        output = err_capture.getvalue()
        self.assertIn("[PERF-TELEMETRY]", output)
        self.assertIn("telemetry_test: 1.500 ms", output)


class TestPerfBudgetsAndReport(unittest.TestCase):
    """Tests latency budget evaluation, trace chains, and report formatting."""

    def setUp(self):
        perf.set_perf_enabled(True)
        perf.reset_all_spans()

    def test_trace_chains_definitions(self):
        chains = [
            "frame->landmarks->gesture->cursor/click",
            "wake->animation",
            "speech->STT->intent->action->TTS",
            "startup phases",
        ]
        for c in chains:
            self.assertIn(c, perf.TRACE_CHAINS)
            chain = perf.TRACE_CHAINS[c]
            self.assertGreater(chain.budget_ms, 0)
            self.assertGreater(len(chain.spans), 0)

    def test_trace_chain_evaluation(self):
        chain = perf.TRACE_CHAINS["wake->animation"]
        # Initially no data
        eval_empty = chain.evaluate()
        self.assertFalse(eval_empty["has_data"])
        self.assertIsNone(eval_empty["passed"])

        # Feed passing sample data
        perf.record("wake_word_detect", 30_000_000)        # 30 ms (budget 50ms)
        perf.record("island_wake_animation", 10_000_000)   # 10 ms (total 40ms <= 50ms)

        eval_pass = chain.evaluate()
        self.assertTrue(eval_pass["has_data"])
        self.assertTrue(eval_pass["passed"])
        self.assertAlmostEqual(eval_pass["p95_ms"], 40.0, places=1)

    def test_print_perf_report_all_pass_or_empty(self):
        out = io.StringIO()
        res = perf.print_perf_report(out=out)
        text = out.getvalue()

        self.assertTrue(res)  # Empty data yields no failures
        self.assertIn("LATENCY BUDGET PERFORMANCE REPORT", text)
        self.assertIn("intent_decision", text)
        self.assertIn("frame->landmarks->gesture->cursor/click", text)
        self.assertIn("Summary: Evaluated: 0 | Passed: 0 | Failed: 0", text)

    def test_print_perf_report_with_failing_metric(self):
        # Exceed intent_decision budget (budget is 1.0 ms)
        perf.record("intent_decision", 5_000_000)  # 5 ms

        out = io.StringIO()
        res = perf.print_perf_report(out=out)
        text = out.getvalue()

        self.assertFalse(res)  # Failed metric
        self.assertIn("FAIL", text)
        self.assertIn("Failed: 1", text)


class TestSelfTestDiagnostics(unittest.TestCase):
    """Unit tests for self_test.py diagnostic checks using clean mocks."""

    def test_check_config_valid(self):
        res = self_test.check_config()
        self.assertTrue(res.passed)
        self.assertIn("Valid", res.message)
        self.assertGreater(res.elapsed_ms, 0.0)

    def test_check_config_invalid(self):
        mock_cfg = MagicMock()
        mock_cfg.frame_width = -1
        mock_cfg.frame_height = 0
        mock_cfg.camera_fps = -30
        mock_cfg.control_scheme = "invalid_scheme"
        mock_cfg.folders_whitelist = "not_a_dict"

        with patch("config.load_config", return_value=mock_cfg):
            res = self_test.check_config()
            self.assertFalse(res.passed)
            self.assertIn("Invalid frame dimensions", res.message)

    def test_check_camera_mocked_success(self):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, MagicMock())
        mock_cap.get.side_effect = lambda prop: 1280 if prop == 3 else 720

        with patch("cv2.VideoCapture", return_value=mock_cap):
            res = self_test.check_camera(index=99, num_frames=3)
            self.assertTrue(res.passed)
            self.assertIn("Camera 99 open", res.message)
            self.assertTrue(mock_cap.release.called)

    def test_check_camera_mocked_failure(self):
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False

        with patch("cv2.VideoCapture", return_value=mock_cap):
            res = self_test.check_camera(index=99)
            self.assertFalse(res.passed)
            self.assertIn("could not be opened", res.message)

    def test_check_microphone_mocked_success(self):
        import numpy as np

        mock_stream = MagicMock()
        fake_data = np.zeros((1600, 1), dtype=np.int16)
        mock_stream.read.return_value = (fake_data, False)

        with patch("sounddevice.query_devices", return_value={"name": "Mock Array"}), \
             patch("sounddevice.InputStream", return_value=mock_stream):
            res = self_test.check_microphone()
            self.assertTrue(res.passed)
            self.assertIn("Mock Array", res.message)
            self.assertTrue(mock_stream.stop.called)

    def test_check_microphone_mocked_no_devices(self):
        with patch("sounddevice.query_devices", side_effect=RuntimeError("No devices found")):
            res = self_test.check_microphone()
            self.assertFalse(res.passed)
            self.assertIn("No default microphone detected", res.message)

    def test_check_models_present(self):
        res = self_test.check_models()
        self.assertTrue(res.passed)
        self.assertIn("jarvis-medium.onnx present", res.message)

    def test_check_models_missing(self):
        res = self_test.check_models(model_path="non_existent_model.onnx")
        self.assertFalse(res.passed)
        self.assertIn("Model file missing", res.message)

    def test_check_network_mocked_success(self):
        mock_sock = MagicMock()
        with patch("socket.create_connection", return_value=mock_sock):
            res = self_test.check_network(host="test.api", port=443)
            self.assertTrue(res.passed)
            self.assertIn("Connected to test.api:443", res.message)
            self.assertTrue(mock_sock.close.called)

    def test_check_network_mocked_failure(self):
        with patch("socket.create_connection", side_effect=OSError("Network unreachable")):
            res = self_test.check_network(host="test.api", port=443)
            self.assertFalse(res.passed)
            self.assertIn("unreachable", res.message)

    def test_check_media_adapter_mocked(self):
        mock_backend = MagicMock()
        mock_backend.is_available.return_value = True

        mock_session = MagicMock()
        mock_props = MagicMock()
        mock_props.title = "Mock Song"
        mock_props.artist = "Mock Artist"

        async def _fake_props():
            return mock_props

        mock_session.try_get_media_properties_async.return_value = _fake_props()
        mock_info = MagicMock()
        mock_info.playback_status = 4  # playing
        mock_session.get_playback_info.return_value = mock_info

        mock_mgr = MagicMock()
        mock_mgr.get_current_session.return_value = mock_session

        async def _fake_mgr():
            return mock_mgr

        mock_backend._ensure_manager.return_value = _fake_mgr()

        with patch("media_adapter.WinRTSMTCBackend", return_value=mock_backend):
            res = self_test.check_media_adapter()
            self.assertTrue(res.passed)
            self.assertIn("Mock Song", res.message)

    def test_check_dpi_and_monitors(self):
        res = self_test.check_dpi_and_monitors()
        self.assertTrue(res.passed)
        self.assertIn("display(s)", res.message)

    def test_run_self_test_all_pass(self):
        pass_chk = lambda: self_test.TestResult("MockCheck", True, "All OK", 1.0)
        with patch.object(self_test, "check_config", pass_chk), \
             patch.object(self_test, "check_camera", pass_chk), \
             patch.object(self_test, "check_microphone", pass_chk), \
             patch.object(self_test, "check_models", pass_chk), \
             patch.object(self_test, "check_network", pass_chk), \
             patch.object(self_test, "check_media_adapter", pass_chk), \
             patch.object(self_test, "check_dpi_and_monitors", pass_chk):
            out = io.StringIO()
            code = self_test.run_self_test(verbose=True, out=out)
            self.assertEqual(code, 0)
            self.assertIn("7 passed, 0 failed. All systems nominal.", out.getvalue())

    def test_run_self_test_with_failure(self):
        pass_chk = lambda: self_test.TestResult("PassCheck", True, "OK", 1.0)
        fail_chk = lambda: self_test.TestResult("FailCheck", False, "Error occurred", 1.0)

        with patch.object(self_test, "check_config", pass_chk), \
             patch.object(self_test, "check_camera", fail_chk), \
             patch.object(self_test, "check_microphone", pass_chk), \
             patch.object(self_test, "check_models", pass_chk), \
             patch.object(self_test, "check_network", pass_chk), \
             patch.object(self_test, "check_media_adapter", pass_chk), \
             patch.object(self_test, "check_dpi_and_monitors", pass_chk):
            out = io.StringIO()
            code = self_test.run_self_test(verbose=True, out=out)
            self.assertEqual(code, 1)
            self.assertIn("6 passed, 1 failed. Action required.", out.getvalue())

    def test_get_self_test_summary_success(self):
        pass_chk = lambda: self_test.TestResult("MockCheck", True, "All OK", 1.25)
        with patch.object(self_test, "check_config", pass_chk), \
             patch.object(self_test, "check_camera", pass_chk), \
             patch.object(self_test, "check_microphone", pass_chk), \
             patch.object(self_test, "check_models", pass_chk), \
             patch.object(self_test, "check_network", pass_chk), \
             patch.object(self_test, "check_media_adapter", pass_chk), \
             patch.object(self_test, "check_dpi_and_monitors", pass_chk):
            summary = self_test.get_self_test_summary()
            self.assertEqual(len(summary), 7)
            for item in summary:
                self.assertIn("name", item)
                self.assertIn("passed", item)
                self.assertIn("message", item)
                self.assertIn("elapsed_ms", item)
                self.assertIsInstance(item["name"], str)
                self.assertIsInstance(item["passed"], bool)
                self.assertIsInstance(item["message"], str)
                self.assertIsInstance(item["elapsed_ms"], float)
                self.assertTrue(item["passed"])

    def test_get_self_test_summary_forwards_config_path(self):
        called_with = []
        def mock_check_config(config_path=None):
            called_with.append(config_path)
            return self_test.TestResult("Config Validation", True, "OK", 0.5)

        pass_chk = lambda: self_test.TestResult("MockCheck", True, "OK", 1.0)
        with patch.object(self_test, "check_config", mock_check_config), \
             patch.object(self_test, "check_camera", pass_chk), \
             patch.object(self_test, "check_microphone", pass_chk), \
             patch.object(self_test, "check_models", pass_chk), \
             patch.object(self_test, "check_network", pass_chk), \
             patch.object(self_test, "check_media_adapter", pass_chk), \
             patch.object(self_test, "check_dpi_and_monitors", pass_chk):
            summary = self_test.get_self_test_summary(config_path="custom/path.json")
            self.assertEqual(called_with, ["custom/path.json"])
            self.assertEqual(summary[0]["name"], "Config Validation")

    def test_get_self_test_summary_handles_exception(self):
        def crashing_chk():
            raise RuntimeError("Hardware disconnect")

        pass_chk = lambda: self_test.TestResult("MockCheck", True, "OK", 1.0)
        with patch.object(self_test, "check_config", pass_chk), \
             patch.object(self_test, "check_camera", crashing_chk), \
             patch.object(self_test, "check_microphone", pass_chk), \
             patch.object(self_test, "check_models", pass_chk), \
             patch.object(self_test, "check_network", pass_chk), \
             patch.object(self_test, "check_media_adapter", pass_chk), \
             patch.object(self_test, "check_dpi_and_monitors", pass_chk):
            summary = self_test.get_self_test_summary()
            self.assertEqual(len(summary), 7)
            cam_entry = summary[1]
            self.assertEqual(cam_entry["name"], "Camera Capture & FPS")
            self.assertFalse(cam_entry["passed"])
            self.assertIn("Hardware disconnect", cam_entry["message"])


class TestMainCliIntegration(unittest.TestCase):
    """Verifies main.py CLI integration for --perf-report and --self-test."""

    def test_perf_report_cli(self):
        import main

        with patch.object(sys, "argv", ["main.py", "--perf-report"]), \
             patch("perf.print_perf_report", return_value=True) as mock_report, \
             patch("main.acquire_single_instance_lock") as mock_lock, \
             self.assertRaises(SystemExit) as cm:
            main.main()

        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(mock_report.called)
        # Verify single instance lock was NOT called
        self.assertFalse(mock_lock.called)

    def test_self_test_cli(self):
        import main

        with patch.object(sys, "argv", ["main.py", "--self-test"]), \
             patch("self_test.run_self_test", return_value=0) as mock_selftest, \
             patch("main.acquire_single_instance_lock") as mock_lock, \
             self.assertRaises(SystemExit) as cm:
            main.main()

        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(mock_selftest.called)
        # Verify single instance lock was NOT called
        self.assertFalse(mock_lock.called)


if __name__ == "__main__":
    unittest.main()
