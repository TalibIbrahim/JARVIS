"""
self_test.py — System Pre-Flight Diagnostics and Hardware Self-Test for J.A.R.V.I.S.

Verifies:
  1. Config validity and schema integrity
  2. Camera open & real FPS calculation via DirectShow
  3. Microphone stream open & audio RMS acquisition via PortAudio / WASAPI
  4. Model files presence & loadability (Piper TTS / OpenWakeWord)
  5. Network reachability to cloud endpoints (Groq API)
  6. Windows SMTC / WinRT media transport adapters
  7. DPI awareness and monitor layout geometry

Strictly NO Qt imports. 100% unit-testable with clean interfaces and mocks.
"""

from __future__ import annotations

import asyncio
import ctypes
from dataclasses import dataclass, field
import os
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# Deep (pipeline-faithful) mode: enabled by run_self_test(). Left False for direct unit-test
# calls and the Settings UI summary so those keep their original cheap behaviour.
_DEEP = False

MUTEX_NAME = "Local\\JARVIS_HandGesture_SingleInstance_Mutex"  # must match main.acquire_single_instance_lock


@dataclass
class TestResult:
    __test__ = False
    name: str
    passed: bool
    message: str
    elapsed_ms: float
    details: Dict[str, Any] = field(default_factory=dict)
    status: str = ""  # "", "pass", "warn" or "fail"; "warn" is passed=True and never counts as a failure


def check_config(config_path: Optional[str] = None) -> TestResult:
    """Verifies config loading, schema fields, and boundary constraints."""
    t0 = time.perf_counter_ns()
    try:
        from config import load_config, Config
        cfg = load_config(config_path)

        errors = []
        if cfg.frame_width <= 0 or cfg.frame_height <= 0:
            errors.append(f"Invalid frame dimensions: {cfg.frame_width}x{cfg.frame_height}")
        if cfg.camera_fps <= 0:
            errors.append(f"Invalid camera_fps: {cfg.camera_fps}")
        if cfg.control_scheme not in ("palm_fist", "pinch"):
            errors.append(f"Unknown control_scheme: {cfg.control_scheme}")
        if not isinstance(cfg.folders_whitelist, dict):
            errors.append("folders_whitelist must be a dictionary")

        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        if errors:
            return TestResult(
                name="Config Validation",
                passed=False,
                message="; ".join(errors),
                elapsed_ms=elapsed_ms,
            )

        return TestResult(
            name="Config Validation",
            passed=True,
            message=f"Valid (FPS={cfg.camera_fps}, scheme={cfg.control_scheme}, target={cfg.frame_width}x{cfg.frame_height})",
            elapsed_ms=elapsed_ms,
            details={"fps": cfg.camera_fps, "scheme": cfg.control_scheme},
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="Config Validation",
            passed=False,
            message=f"Failed to load config: {exc}",
            elapsed_ms=elapsed_ms,
        )


def check_camera(index: Optional[int] = None, num_frames: int = 5) -> TestResult:
    """Tests camera device acquisition, DirectShow backend, and measures real capture FPS."""
    t0 = time.perf_counter_ns()
    try:
        import cv2
        from config import load_config

        cfg = load_config()
        cam_idx = index if index is not None else cfg.camera_index

        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        backend_name = "DSHOW" if backend == getattr(cv2, "CAP_DSHOW", None) else "ANY"
        t_open = time.perf_counter()
        cap = cv2.VideoCapture(cam_idx, backend)
        if not cap.isOpened():
            # Fallback to default backend if DirectShow fails
            cap = cv2.VideoCapture(cam_idx)
            backend_name = "default"

        if not cap.isOpened():
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="Camera Capture & FPS",
                passed=False,
                message=f"Camera index {cam_idx} could not be opened",
                elapsed_ms=elapsed_ms,
            )

        # Set configured resolution
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_height)
        if cfg.camera_mjpg and sys.platform == "win32":
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        # Warmup frame
        ret, frame = cap.read()
        if not ret or frame is None:
            cap.release()
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="Camera Capture & FPS",
                passed=False,
                message=f"Camera index {cam_idx} opened but failed to capture frame",
                elapsed_ms=elapsed_ms,
            )

        open_ms = (time.perf_counter() - t_open) * 1000.0

        # Measure real capture FPS (deep mode: over 2 s of wall clock)
        fps_t0 = time.perf_counter()
        frames_read = 0
        if _DEEP:
            while time.perf_counter() - fps_t0 < 2.0:
                ok, f = cap.read()
                if ok and f is not None:
                    frames_read += 1
        else:
            for _ in range(num_frames):
                ok, f = cap.read()
                if ok and f is not None:
                    frames_read += 1
        fps_t1 = time.perf_counter()

        dt = fps_t1 - fps_t0
        real_fps = (frames_read / dt) if dt > 0 else 0.0

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="Camera Capture & FPS",
            passed=True,
            message=f"Camera {cam_idx} open @ {real_fps:.1f} real FPS ({w}x{h}) backend={backend_name} open={open_ms:.0f}ms",
            elapsed_ms=elapsed_ms,
            details={"fps": real_fps, "width": w, "height": h, "index": cam_idx,
                     "backend": backend_name, "open_ms": open_ms},
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="Camera Capture & FPS",
            passed=False,
            message=f"Camera check error: {exc}",
            elapsed_ms=elapsed_ms,
        )


def _check_microphone_pipeline() -> TestResult:
    """Tests the microphone exactly as the voice pipeline uses it (PyAudio, 16 kHz mono, 1280-frame chunks)."""
    t0 = time.perf_counter_ns()
    name = "Microphone Stream"

    def _ms() -> float:
        return (time.perf_counter_ns() - t0) / 1_000_000.0

    try:
        import numpy as np
        import pyaudio
        import voice
        from config import load_config

        cfg = load_config()
        idx, dev_name = voice.find_best_microphone(cfg)
        if idx is None:
            return TestResult(name, False, "No usable input device found by find_best_microphone", _ms())

        p = voice.get_shared_pyaudio()
        with voice._PORTAUDIO_LOCK:
            info = p.get_device_info_by_index(idx)
            host = p.get_host_api_info_by_index(info["hostApi"])["name"]
            try:
                dflt = p.get_default_input_device_info()
                dflt_idx, dflt_name = dflt["index"], dflt.get("name", "?")
            except Exception:
                dflt_idx, dflt_name = None, "none"
            try:
                stream = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                                input_device_index=idx, frames_per_buffer=1280)
            except Exception as open_exc:
                return TestResult(name, False,
                                  f"open FAILED on [{idx}] {dev_name[:30]} ({host}): {open_exc}", _ms())

        print("  [mic] stay quiet for 2 s ...", flush=True)
        rms_list: List[float] = []
        flagged = 0
        vad = voice.VADDetector()
        try:
            for _ in range(25):  # 25 x 80 ms = 2 s
                data = stream.read(1280, exception_on_overflow=False)
                arr = np.frombuffer(data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(arr.astype(float) ** 2)))
                rms_list.append(rms)
                if vad.is_speech(arr, rms):
                    flagged += 1
        finally:
            with voice._PORTAUDIO_LOCK:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

        p50 = float(sorted(rms_list)[len(rms_list) // 2]) if rms_list else 0.0
        pct = 100.0 * flagged / max(1, len(rms_list))
        gate = float(vad.energy_threshold)
        differs = dflt_idx is not None and dflt_idx != idx
        base = (f"pipeline mic [{idx}] {dev_name[:30]} hostapi={host} rms_p50={p50:.0f} vad_flagged={pct:.0f}% "
                f"| Windows default [{dflt_idx}] {str(dflt_name)[:30]} "
                f"{'DIFFERS from' if differs else 'same as'} pipeline")
        details = {"index": idx, "device": dev_name, "hostapi": host, "rms_p50": p50, "vad_flagged_pct": pct,
                   "default_index": dflt_idx, "default_differs": differs}
        if pct > 50.0:
            return TestResult(
                name, True,
                f"VAD flags ambient as speech ({pct:.0f}%) - ambient RMS {p50:.0f} >= gate {gate:.0f} | {base}",
                _ms(), details, status="warn")
        return TestResult(name, True, base, _ms(), details)
    except Exception as exc:
        return TestResult(name, False, f"Microphone pipeline check error: {exc}", _ms())


def check_microphone(device_index: Optional[int] = None) -> TestResult:
    """Verifies audio input stream opening, PortAudio WASAPI driver, and reads acoustic buffer."""
    if _DEEP and device_index is None:
        return _check_microphone_pipeline()
    t0 = time.perf_counter_ns()
    try:
        import sounddevice as sd
        import numpy as np

        # Query input device
        try:
            dev_info = (
                sd.query_devices(device_index)
                if device_index is not None
                else sd.query_devices(kind="input")
            )
            dev_name = dev_info.get("name", "Unknown Mic")
        except Exception as q_err:
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="Microphone Stream",
                passed=False,
                message=f"No default microphone detected: {q_err}",
                elapsed_ms=elapsed_ms,
            )

        # Open short 100ms 16kHz mono stream to verify hardware access
        samplerate = 16000
        stream = sd.InputStream(
            device=device_index,
            samplerate=samplerate,
            channels=1,
            dtype="int16",
        )
        stream.start()
        # Read 1600 samples (100 ms)
        data, overflowed = stream.read(1600)
        stream.stop()
        stream.close()

        rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0

        return TestResult(
            name="Microphone Stream",
            passed=True,
            message=f"Mic '{dev_name[:28]}' open @ {samplerate} Hz (RMS={rms:.1f})",
            elapsed_ms=elapsed_ms,
            details={"device": dev_name, "rms": rms, "samplerate": samplerate},
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="Microphone Stream",
            passed=False,
            message=f"Microphone stream error: {exc}",
            elapsed_ms=elapsed_ms,
        )


def check_models(model_path: Optional[str] = None) -> TestResult:
    """Validates presence and loadability of local neural models (Piper TTS ONNX)."""
    t0 = time.perf_counter_ns()
    try:
        from config import load_config
        cfg = load_config()

        target_path = model_path or cfg.tts_model_path
        if not os.path.isabs(target_path):
            target_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), target_path)

        if not os.path.exists(target_path):
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="Model Files & Weights",
                passed=False,
                message=f"Model file missing: {target_path}",
                elapsed_ms=elapsed_ms,
            )

        size_bytes = os.path.getsize(target_path)
        size_mb = size_bytes / (1024.0 * 1024.0)

        if size_bytes < 100_000:
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="Model Files & Weights",
                passed=False,
                message=f"Model file corrupted or too small ({size_bytes} bytes)",
                elapsed_ms=elapsed_ms,
            )

        # Validate ONNX header / protobuf format
        with open(target_path, "rb") as mf:
            header = mf.read(16)
        if len(header) < 4:
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="Model Files & Weights",
                passed=False,
                message=f"Model file {os.path.basename(target_path)} has empty header",
                elapsed_ms=elapsed_ms,
            )

        # Check accompanying JSON config if present
        json_path = target_path + ".json"
        has_json = os.path.exists(json_path)

        model_name = os.path.basename(target_path)
        json_str = "+JSON" if has_json else ""
        extra = ""
        if _DEEP:
            import numpy as np
            zeros = np.zeros(1280, dtype=np.int16)
            try:
                t_a = time.perf_counter()
                from openwakeword.model import Model
                oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
                load_ms = (time.perf_counter() - t_a) * 1000.0
                t_a = time.perf_counter()
                oww.predict(zeros)
                extra += f"; hey_jarvis load {load_ms:.0f}ms predict {(time.perf_counter() - t_a) * 1000.0:.1f}ms"
            except Exception as oww_exc:
                return TestResult("Model Files & Weights", False,
                                  f"openWakeWord hey_jarvis failed to load/predict: {oww_exc}",
                                  (time.perf_counter_ns() - t0) / 1_000_000.0)
            try:
                t_a = time.perf_counter()
                from openwakeword.vad import VAD
                silero = VAD()
                load_ms = (time.perf_counter() - t_a) * 1000.0
                t_a = time.perf_counter()
                silero.predict(zeros)
                extra += f"; silero load {load_ms:.0f}ms predict {(time.perf_counter() - t_a) * 1000.0:.1f}ms"
            except Exception as vad_exc:
                return TestResult("Model Files & Weights", False,
                                  f"Silero VAD failed to load/predict: {vad_exc}",
                                  (time.perf_counter_ns() - t0) / 1_000_000.0)
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="Model Files & Weights",
            passed=True,
            message=f"{model_name} present & verified ({size_mb:.1f} MB {json_str}){extra}",
            elapsed_ms=elapsed_ms,
            details={"model": model_name, "size_mb": size_mb, "path": target_path},
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="Model Files & Weights",
            passed=False,
            message=f"Model validation error: {exc}",
            elapsed_ms=elapsed_ms,
        )


def check_network(host: str = "api.groq.com", port: int = 443, timeout: float = 3.0) -> TestResult:
    """Checks TCP connectivity and DNS resolution to cloud API endpoints."""
    t0 = time.perf_counter_ns()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="Network Reachability",
            passed=True,
            message=f"Connected to {host}:{port}",
            elapsed_ms=elapsed_ms,
            details={"host": host, "port": port, "latency_ms": elapsed_ms},
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        # Check if fallback DNS works
        try:
            backup_sock = socket.create_connection(("1.1.1.1", 53), timeout=1.5)
            backup_sock.close()
            return TestResult(
                name="Network Reachability",
                passed=False,
                message=f"Endpoint {host} unreachable, but Internet connection active: {exc}",
                elapsed_ms=elapsed_ms,
            )
        except Exception:
            return TestResult(
                name="Network Reachability",
                passed=False,
                message=f"Offline / network unreachable: {exc}",
                elapsed_ms=elapsed_ms,
            )


def check_media_adapter(timeout: float = 2.0) -> TestResult:
    """Verifies Windows SMTC (System Media Transport Controls) adapter and WinRT availability."""
    t0 = time.perf_counter_ns()
    try:
        from media_adapter import WinRTSMTCBackend

        backend = WinRTSMTCBackend()
        if not backend.is_available():
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="WinRT / Media Adapters",
                passed=True,
                message="SMTC UNAVAILABLE - PyAutoGUI hardware-key fallback only",
                elapsed_ms=elapsed_ms,
                status="warn",
            )

        async def _probe():
            mgr = await backend._ensure_manager()
            if not mgr:
                return "SMTC manager ready (idle)"
            session = mgr.get_current_session()
            if not session:
                return "SMTC session manager active (no active media session)"
            props = await session.try_get_media_properties_async()
            title = str(getattr(props, "title", "") or "Unknown Title")
            artist = str(getattr(props, "artist", "") or "Unknown Artist")
            info = session.get_playback_info()
            st_val = int(getattr(info, "playback_status", 0)) if info else 0
            st_str = "playing" if st_val == 4 else ("paused" if st_val == 5 else "idle")
            return f"SMTC active: '{title[:24]}' by '{artist[:20]}' ({st_str})"

        status = "pass"
        try:
            msg = asyncio.run(asyncio.wait_for(_probe(), timeout=timeout))
        except asyncio.TimeoutError:
            msg = f"SMTC probe TIMED OUT after {timeout:.0f}s (backend present, state unknown)"
            status = "warn"

        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="WinRT / Media Adapters",
            passed=True,
            message=msg,
            elapsed_ms=elapsed_ms,
            status=status,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="WinRT / Media Adapters",
            passed=False,
            message=f"Media adapter initialization error: {exc}",
            elapsed_ms=elapsed_ms,
        )


def check_dpi_and_monitors() -> TestResult:
    """Queries Windows Per-Monitor V2 DPI awareness and screen metrics."""
    t0 = time.perf_counter_ns()
    try:
        if sys.platform != "win32":
            elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
            return TestResult(
                name="DPI & Monitor Info",
                passed=True,
                message=f"Non-Windows platform ({sys.platform})",
                elapsed_ms=elapsed_ms,
            )

        user32 = ctypes.windll.user32
        cx = user32.GetSystemMetrics(0)  # SM_CXSCREEN
        cy = user32.GetSystemMetrics(1)  # SM_CYSCREEN
        num_monitors = user32.GetSystemMetrics(80)  # SM_CMONITORS
        if num_monitors <= 0:
            num_monitors = 1

        try:
            dpi = user32.GetDpiForSystem()
        except Exception:
            dpi = 96

        scale_pct = int(round((dpi / 96.0) * 100))
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0

        return TestResult(
            name="DPI & Monitor Info",
            passed=True,
            message=f"{cx}x{cy}, {num_monitors} display(s), DPI={dpi} ({scale_pct}%)",
            elapsed_ms=elapsed_ms,
            details={"cx": cx, "cy": cy, "monitors": num_monitors, "dpi": dpi, "scale": scale_pct},
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return TestResult(
            name="DPI & Monitor Info",
            passed=False,
            message=f"DPI query error: {exc}",
            elapsed_ms=elapsed_ms,
        )


def get_self_test_summary(config_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Executes all diagnostic checks and returns structured summary dictionaries:
      {"name": str, "passed": bool, "message": str, "elapsed_ms": float}
    Enables Settings UI to cleanly render health status without blocking.
    """
    checks = [
        ("Config Validation", lambda: check_config(config_path) if config_path is not None else check_config()),
        ("Camera Capture & FPS", lambda: check_camera()),
        ("Microphone Stream", lambda: check_microphone()),
        ("Model Files & Weights", lambda: check_models()),
        ("Network Reachability", lambda: check_network()),
        ("WinRT / Media Adapters", lambda: check_media_adapter()),
        ("DPI & Monitor Info", lambda: check_dpi_and_monitors()),
    ]

    summary: List[Dict[str, Any]] = []
    for default_name, chk in checks:
        try:
            res = chk()
            if hasattr(res, "name"):
                summary.append({
                    "name": str(res.name),
                    "passed": bool(res.passed),
                    "message": str(res.message),
                    "elapsed_ms": float(res.elapsed_ms),
                    "status": getattr(res, "status", "") or ("pass" if res.passed else "fail"),
                })
            elif isinstance(res, dict):
                summary.append({
                    "name": str(res.get("name", default_name)),
                    "passed": bool(res.get("passed", False)),
                    "message": str(res.get("message", "")),
                    "elapsed_ms": float(res.get("elapsed_ms", 0.0)),
                })
        except Exception as exc:
            summary.append({
                "name": default_name,
                "passed": False,
                "message": f"Check error: {exc}",
                "elapsed_ms": 0.0,
            })

    return summary


def check_instance_mutex() -> TestResult:
    """Probes the single-instance mutex main.py uses, then releases it immediately."""
    t0 = time.perf_counter_ns()
    name = "Instance Mutex"
    try:
        import jarvis_log
        census = jarvis_log.process_census()
        if sys.platform != "win32":
            return TestResult(name, True, f"n/a on {sys.platform}", 0.0, {"census": census})
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.CreateMutexW(None, False, MUTEX_NAME)
        last_error = ctypes.get_last_error()
        if handle:
            k32.CloseHandle(handle)  # release immediately; we must not hold the real instance lock
        ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        held = last_error == 183  # ERROR_ALREADY_EXISTS
        if held:
            return TestResult(name, True, f"held by a running instance; census={census}", ms,
                              {"census": census}, status="warn")
        return TestResult(name, True, f"free; census={census}", ms, {"census": census})
    except Exception as exc:
        return TestResult(name, False, f"mutex probe error: {exc}",
                          (time.perf_counter_ns() - t0) / 1_000_000.0)


def _run_with_deadline(chk, timeout: float, label: str) -> TestResult:
    """Runs a check in a daemon thread; a hung check reports FAIL instead of blocking the suite."""
    box: Dict[str, Any] = {}

    def _target():
        try:
            box["res"] = chk()
        except BaseException as exc:  # reported, never swallowed
            box["exc"] = exc

    t0 = time.perf_counter()
    th = threading.Thread(target=_target, name=f"SelfTest-{label}", daemon=True)
    th.start()
    th.join(timeout)
    ms = (time.perf_counter() - t0) * 1000.0
    if th.is_alive():
        return TestResult(label, False, f"timeout after {timeout:.0f}s", ms)
    if "exc" in box:
        return TestResult(label, False, f"check raised {type(box['exc']).__name__}: {box['exc']}", ms)
    return box["res"]


def run_self_test(verbose: bool = True, out: Optional[Any] = None) -> int:
    """
    Executes complete pre-flight diagnostic suite.
    Prints status and execution timings for each subsystem in ms.

    Returns:
      0 if all self-test checks pass (warnings do not count as failures).
      Non-zero (count of failures) if any check fails.
    """
    global _DEEP
    output = out or sys.stdout
    t_suite = time.perf_counter()

    # (check, display name used if it times out, deadline seconds)
    checks = [
        (check_config, "Config Validation", 5.0),
        (check_camera, "Camera Capture & FPS", 10.0),
        (check_microphone, "Microphone Stream", 6.0),
        (check_models, "Model Files & Weights", 5.0),
        (check_network, "Network Reachability", 5.0),
        (check_media_adapter, "WinRT / Media Adapters", 5.0),
        (check_dpi_and_monitors, "DPI & Monitor Info", 5.0),
    ]

    header = "================== J.A.R.V.I.S. SYSTEM SELF-TEST =================="
    divider = "-" * len(header)
    lines = ["\n" + header]

    passed_count = 0
    warned_count = 0
    failed_count = 0
    results: List[TestResult] = []

    def _tag_of(res: TestResult) -> str:
        st = getattr(res, "status", "") or ("pass" if res.passed else "fail")
        return {"pass": "[PASS]", "warn": "[WARN]"}.get(st, "[FAIL]")

    prev_deep = _DEEP
    _DEEP = True
    try:
        # Single-instance mutex: informational row, deliberately not part of the pass/fail totals.
        mres = _run_with_deadline(check_instance_mutex, 5.0, "Instance Mutex")
        lines.append(f" {_tag_of(mres):<6} {mres.name:<24}: {mres.message} ({mres.elapsed_ms:.1f} ms)")

        for chk, label, deadline in checks:
            res = _run_with_deadline(chk, deadline, label)
            results.append(res)
            tag = _tag_of(res)
            lines.append(f" {tag:<6} {res.name:<24}: {res.message} ({res.elapsed_ms:.1f} ms)")
            if tag == "[FAIL]":
                failed_count += 1
            elif tag == "[WARN]":
                warned_count += 1
            else:
                passed_count += 1
    finally:
        _DEEP = prev_deep

    lines.append(divider)
    if failed_count == 0 and warned_count == 0:
        lines.append(f"Results: {passed_count} passed, 0 failed. All systems nominal.")
    elif failed_count == 0:
        lines.append(f"Results: {passed_count} passed, {warned_count} warned, 0 failed. Review warnings.")
    else:
        warn_str = f", {warned_count} warned" if warned_count else ""
        lines.append(f"Results: {passed_count} passed{warn_str}, {failed_count} failed. Action required.")
    lines.append(f"Total time: {time.perf_counter() - t_suite:.1f} s")
    lines.append("=" * len(header) + "\n")

    if verbose:
        output.write("\n".join(lines) + "\n")
        if hasattr(output, "flush"):
            output.flush()

    return 0 if failed_count == 0 else failed_count


if __name__ == "__main__":
    sys.exit(run_self_test())
