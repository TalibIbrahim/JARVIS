"""Camera + MediaPipe hand tracking loop. Feeds landmarks to the gesture engine."""
import sys
import threading
import time

import cv2
import numpy as np

from gestures import Hand, Action

mp = None


def _get_mp():
    """Lazily imports and caches MediaPipe to avoid 1.5s+ startup penalty."""
    global mp
    if mp is None:
        import mediapipe as _mp
        mp = _mp
    return mp


_CAMERA_LOCK = threading.Lock()
_CAMERA_CACHE = None
_CAMERA_CACHE_TIME = 0.0
_CAMERA_CACHE_TTL = 30.0


def clear_camera_cache():
    """Clears cached camera enumeration on exit."""
    global _CAMERA_CACHE, _CAMERA_CACHE_TIME
    with _CAMERA_LOCK:
        _CAMERA_CACHE = None
        _CAMERA_CACHE_TIME = 0.0


def list_cameras(force_refresh=False):
    """
    Enumerates available physical and virtual camera capture devices.
    Filters out non-functioning devices, Windows Hello IR sensors, and ghost indices.
    Returns list of dicts:
        [{'index': int, 'name': str, 'working': bool}]
    """
    global _CAMERA_CACHE, _CAMERA_CACHE_TIME
    with _CAMERA_LOCK:
        now = time.time()
        if not force_refresh and _CAMERA_CACHE is not None and (now - _CAMERA_CACHE_TIME) < _CAMERA_CACHE_TTL:
            return list(_CAMERA_CACHE)

        qt_cams = []
        try:
            from PySide6.QtMultimedia import QMediaDevices
            for c in QMediaDevices.videoInputs():
                qt_cams.append({"name": c.description(), "default": c.isDefault()})
        except Exception:
            pass

        backend = cv2.CAP_DSHOW if sys.platform == "win32" else 0
        cams = []
        limit = max(2, len(qt_cams))

        for idx in range(limit):
            name = qt_cams[idx]["name"] if idx < len(qt_cams) else f"Camera {idx}"
            # Skip pure IR / Windows Hello sensors
            if any(w in name.lower() for w in (" ir ", "ir camera", "infrared", "windows hello")):
                continue

            cap = cv2.VideoCapture(idx, backend)
            opened = cap.isOpened()
            working = False
            if opened:
                ret, frame = cap.read()
                working = bool(ret and frame is not None and frame.size > 0)
                try:
                    cap.release()
                except Exception:
                    pass
            else:
                try:
                    cap.release()
                except Exception:
                    pass

            if working:
                cams.append({"index": idx, "name": name, "working": True})

        if not cams:
            # Fallback if probing was blocked or in headless environment
            default_name = qt_cams[0]["name"] if qt_cams else "Default Camera (Camera 0)"
            cams.append({"index": 0, "name": default_name, "working": True})

        _CAMERA_CACHE = cams
        _CAMERA_CACHE_TIME = now
        return list(cams)


class FrameGrabber(threading.Thread):
    """Dedicated background thread for non-blocking camera frame capture at 30+ FPS."""
    def __init__(self, cfg, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.stop = stop_event
        self.cap = None
        self.lock = threading.Lock()
        self.latest_t = 0.0
        self.fps = 0.0
        self.failures = 0
        self.opened = False

        self.reconnecting = False

        # Double-buffer preallocated frames to eliminate per-frame allocations
        h = int(getattr(cfg, "frame_height", 720))
        w = int(getattr(cfg, "frame_width", 1280))
        self._buffers = [
            np.zeros((h, w, 3), dtype=np.uint8),
            np.zeros((h, w, 3), dtype=np.uint8),
        ]
        self._write_idx = 0
        self._read_idx = 1
        self._has_frame = False

    def open_camera(self):
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else 0
        preferred = getattr(self.cfg, "camera_index", 0)
        candidates = [preferred] + [i for i in (0, 1, 2) if i != preferred]
        w = int(getattr(self.cfg, "frame_width", 1280))
        h = int(getattr(self.cfg, "frame_height", 720))

        for idx in candidates:
            cap = cv2.VideoCapture(idx, backend)
            if getattr(self.cfg, "camera_mjpg", True):
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                except Exception:
                    pass
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, getattr(self.cfg, "camera_fps", 30))
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            if cap.isOpened():
                ret, test_frame = cap.read()
                is_valid = False
                if ret and test_frame is not None:
                    size = getattr(test_frame, "size", 1)
                    is_valid = bool(size > 0 if isinstance(size, (int, float)) else True)

                if is_valid:
                    if idx != preferred:
                        print(f"[tracker] Camera [{preferred}] unavailable. Auto-recovered to working camera [{idx}].")
                        self.cfg.camera_index = idx
                    self.cap = cap
                    self.opened = True
                    # Re-sync double-buffer dimensions if camera resolution differs
                    if hasattr(test_frame, "shape") and isinstance(test_frame.shape, tuple) and len(test_frame.shape) >= 2:
                        actual_h, actual_w = int(test_frame.shape[0]), int(test_frame.shape[1])
                    else:
                        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or w
                        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or h

                    with self.lock:
                        if self._buffers[0].shape != (actual_h, actual_w, 3):
                            self._buffers = [
                                np.zeros((actual_h, actual_w, 3), dtype=np.uint8),
                                np.zeros((actual_h, actual_w, 3), dtype=np.uint8),
                            ]
                        if isinstance(test_frame, np.ndarray) and test_frame.shape == self._buffers[self._write_idx].shape:
                            np.copyto(self._buffers[self._write_idx], test_frame)
                        self._read_idx = self._write_idx
                        self._write_idx = 1 - self._write_idx
                        self.latest_t = time.perf_counter()
                        self._has_frame = True
                    return True
                try:
                    cap.release()
                except Exception:
                    pass
            else:
                try:
                    cap.release()
                except Exception:
                    pass

        self.cap = None
        self.opened = False
        return False

    def reconnect_loop(self):
        """Auto-reconnect backoff loop (1s, 2s, 4s) when camera is disconnected."""
        self.reconnecting = True
        self.opened = False
        self.release()
        delays = [1.0, 2.0, 4.0]
        delay_idx = 0
        print("[tracker] Camera disconnected. Entering auto-reconnect backoff loop (1s, 2s, 4s)...")
        while not self.stop.is_set():
            delay = delays[delay_idx]
            if delay_idx < len(delays) - 1:
                delay_idx += 1
            if self.stop.wait(delay):
                break

            clear_camera_cache()
            if self.open_camera():
                self.failures = 0
                self.reconnecting = False
                print("[tracker] Camera reconnected: DirectShow device re-opened and buffers re-initialized.")
                return True
        self.reconnecting = False
        return False

    def run(self):
        if not self.cap or not self.opened:
            if not self.open_camera():
                if not self.reconnect_loop():
                    return

        last_t = time.perf_counter()
        while not self.stop.is_set():
            if not self.cap or not self.opened:
                if not self.reconnect_loop():
                    continue
                last_t = time.perf_counter()

            ok, frame = self.cap.read()
            now = time.perf_counter()
            if not ok or frame is None or getattr(frame, "size", 0) == 0:
                self.failures += 1
                if self.failures >= 30:
                    print(f"[tracker] Camera read failure threshold reached ({self.failures}). Reconnecting...")
                    self.release()
                    self.reconnect_loop()
                    last_t = time.perf_counter()
                    continue
                time.sleep(0.02)
                continue

            self.failures = 0
            dt = max(min(now - last_t, 0.05), 1e-4)
            last_t = now
            inst_fps = 1.0 / dt
            self.fps = 0.9 * self.fps + 0.1 * inst_fps if self.fps > 0 else inst_fps

            # Double-buffer write into preallocated array (zero-allocation)
            write_buf = self._buffers[self._write_idx]
            if hasattr(frame, "shape") and write_buf.shape != frame.shape:
                self._buffers = [
                    np.zeros_like(frame),
                    np.zeros_like(frame),
                ]
                write_buf = self._buffers[self._write_idx]
            if isinstance(frame, np.ndarray):
                np.copyto(write_buf, frame)

            with self.lock:
                self._read_idx = self._write_idx
                self._write_idx = 1 - self._write_idx
                self.latest_t = now
                self._has_frame = True

    def get_latest(self):
        with self.lock:
            if not self._has_frame:
                return None, 0.0
            return self._buffers[self._read_idx], self.latest_t

    def release(self):
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None
        self.opened = False


class Tracker(threading.Thread):
    def __init__(self, cfg, engine, controller, overlay, ctx, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.engine = engine
        self.controller = controller
        self.overlay = overlay
        self.ctx = ctx
        self.stop = stop_event
        self._fps = 0.0
        self._grabber = None
        self._inference_ms = 0.0
        self._frame_lock = threading.Lock()
        self._latest_raw_frame = None
        self._last_frame_t = 0.0
        # Preallocated buffers for zero-allocation hot path
        self._flip_buf = None
        self._rgb_buf = None
        self._preview_overlay_buf = None

    def get_latest_frame(self, unmirrored: bool = True):
        """Returns the latest captured camera frame (BGR) for vision inspection."""
        if self._grabber:
            frame, _ = self._grabber.get_latest()
            if frame is not None:
                return frame.copy() if unmirrored else cv2.flip(frame, 1)
        with self._frame_lock:
            if self._latest_raw_frame is not None:
                return self._latest_raw_frame.copy() if unmirrored else cv2.flip(self._latest_raw_frame, 1)
        return None

    def run(self):
        cfg = self.cfg
        use_threaded = getattr(cfg, "threaded_capture", True)

        if use_threaded:
            self._grabber = FrameGrabber(cfg, self.stop)
            if not self._grabber.open_camera():
                self.ctx.say("I can't open the camera, sir.")
                self.overlay.post("error", text="CAMERA NOT FOUND")
            self._grabber.start()
        else:
            backend = cv2.CAP_DSHOW if sys.platform == "win32" else 0
            preferred = getattr(cfg, "camera_index", 0)
            candidates = [preferred] + [i for i in (0, 1, 2) if i != preferred]
            cap = None
            for idx in candidates:
                cand_cap = cv2.VideoCapture(idx, backend)
                if getattr(cfg, "camera_mjpg", True):
                    try:
                        cand_cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                    except Exception:
                        pass
                cand_cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_width)
                cand_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_height)
                cand_cap.set(cv2.CAP_PROP_FPS, getattr(cfg, "camera_fps", 30))
                if cand_cap.isOpened():
                    cap = cand_cap
                    if idx != preferred:
                        print(f"[tracker] Camera [{preferred}] unavailable. Auto-recovered to working camera [{idx}].")
                        cfg.camera_index = idx
                    break
                try:
                    cand_cap.release()
                except Exception:
                    pass

            if not cap or not cap.isOpened():
                self.ctx.say("I can't open the camera, sir.")
                self.overlay.post("error", text="CAMERA NOT FOUND")
                return

        comp = getattr(cfg, "model_complexity", 1)
        det_conf = getattr(cfg, "min_detection_confidence", 0.6)
        track_conf = getattr(cfg, "min_tracking_confidence", 0.6)

        mp_mod = _get_mp()
        hands = mp_mod.solutions.hands.Hands(
            max_num_hands=2,
            model_complexity=comp,
            min_detection_confidence=det_conf,
            min_tracking_confidence=track_conf
        )
        connections = list(mp_mod.solutions.hands.HAND_CONNECTIONS)
        failures = 0
        last_processed_t = 0.0
        win_name = "JARVIS // VISION  (q to quit)"

        hand_visible = False
        last_hand_seen_t = 0.0
        while not self.stop.is_set():
            now_loop = time.perf_counter()
            # Adaptive rate: low-power scanning when no hand is in view;
            # jump to full 30 FPS rate instantly the moment a hand is detected,
            # with zero missed first gestures.
            hand_recent = (now_loop - last_hand_seen_t) < 0.5
            if not self.engine.active and not cfg.show_preview and not hand_recent:
                time.sleep(0.06)

            if use_threaded:
                raw_frame, frame_t = self._grabber.get_latest()
                if raw_frame is None or frame_t <= last_processed_t:
                    if (self._grabber.failures >= 30 or not self._grabber.opened) and failures < 30:
                        failures = 30
                        self.ctx.say("Camera feed lost, sir.")
                        self.overlay.post("error", text="CAMERA FEED LOST")
                        self.controller.handle(Action("lost"))
                    time.sleep(0.002)
                    continue

                if failures >= 30:
                    failures = 0
                    print("[tracker] Camera feed recovered. Resuming tracking.")
                    self.controller._reset_filters()
                    self._flip_buf = None
                    self._rgb_buf = None
                    self._preview_overlay_buf = None

                failures = 0
                last_processed_t = frame_t
            else:
                ok, frame = cap.read()
                if not ok:
                    failures += 1
                    if failures == 30:
                        self.ctx.say("Camera feed lost, sir.")
                        self.overlay.post("error", text="CAMERA FEED LOST")
                        self.controller.handle(Action("lost"))
                    if failures >= 30:
                        delays = [1.0, 2.0, 4.0]
                        delay_idx = min((failures - 30) // 10, len(delays) - 1)
                        if self.stop.wait(delays[delay_idx]):
                            break
                        clear_camera_cache()
                        try:
                            cap.release()
                        except Exception:
                            pass
                        for c_idx in candidates:
                            cand_cap = cv2.VideoCapture(c_idx, backend)
                            if cand_cap.isOpened():
                                ret, tf = cand_cap.read()
                                if ret and tf is not None and getattr(tf, "size", 0) > 0:
                                    cap = cand_cap
                                    failures = 0
                                    self._flip_buf = None
                                    self._rgb_buf = None
                                    self._preview_overlay_buf = None
                                    self.controller._reset_filters()
                                    break
                                try:
                                    cand_cap.release()
                                except Exception:
                                    pass
                        continue
                failures = 0
                raw_frame = frame
                with self._frame_lock:
                    self._latest_raw_frame = raw_frame

            h, w = raw_frame.shape[:2]
            if self._flip_buf is None or self._flip_buf.shape != (h, w, 3):
                self._flip_buf = np.empty((h, w, 3), dtype=np.uint8)
                self._rgb_buf = np.empty((h, w, 3), dtype=np.uint8)

            # In-place destination flip (mirror, so moving right moves right)
            cv2.flip(raw_frame, 1, dst=self._flip_buf)

            # MediaPipe Inference with in-place destination color conversion
            t_inf_0 = time.perf_counter()
            cv2.cvtColor(self._flip_buf, cv2.COLOR_BGR2RGB, dst=self._rgb_buf)
            result = hands.process(self._rgb_buf)
            self._inference_ms = (time.perf_counter() - t_inf_0) * 1000.0

            found = []
            if result.multi_hand_landmarks:
                for lms in result.multi_hand_landmarks:
                    found.append(Hand([(p.x, p.y) for p in lms.landmark]))

            now = time.time()
            if found:
                last_hand_seen_t = time.perf_counter()
                hand_visible = True
            else:
                hand_visible = False

            dt = max(min(now - self._last_frame_t, 0.05), 1e-4)
            self._last_frame_t = now
            inst_fps = 1.0 / dt
            self._fps = 0.9 * self._fps + 0.1 * inst_fps if self._fps > 0 else inst_fps

            for action in self.engine.update(found, now):
                self.controller.handle(action)
                if action.kind == "activate":
                    self.ctx.chirp(True)
                    self.ctx.say("Online. At your service, sir.")
                elif action.kind == "deactivate":
                    self.ctx.chirp(False)
                    self.ctx.say("Standing by.")

            st = self.engine.status(now)
            st["landmarks"] = [h.lm for h in found]
            st["fps"] = self._grabber.fps if (use_threaded and self._grabber) else self._fps
            st["inference_ms"] = self._inference_ms
            self.overlay.post("state", **st)

            if cfg.show_preview:
                self._preview(self._flip_buf, found, connections, st, win_name)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q") or key == 27:             # q or Esc
                    self.stop.set()
                try:
                    # Closing window via 'X' button triggers clean shutdown
                    if cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1:
                        self.stop.set()
                except Exception:
                    pass

        hands.close()
        if use_threaded and self._grabber:
            self._grabber.release()
        elif not use_threaded:
            cap.release()

        if cfg.show_preview:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    def _preview(self, frame, hands, connections, st, win_name):
        h, w = frame.shape[:2]
        cyan = (255, 229, 0)       # BGR (0x00E5FF)
        gold = (77, 201, 255)      # BGR (0xFFC94D)
        green = (80, 230, 80)
        dim_gray = (120, 120, 120)

        # Draw active tracking boundary box
        m = self.cfg.box_margin
        if m > 0.005:
            x1, y1 = int(m * w), int(m * h)
            x2, y2 = int((1 - m) * w), int((1 - m) * h)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (70, 70, 30), 1)
            cv2.putText(frame, f"TRACKING ZONE ({100 - int(2*m*100)}%)", (x1 + 4, y1 + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, dim_gray, 1, cv2.LINE_AA)
        else:
            cv2.putText(frame, "TRACKING ZONE: FULL (100%)", (12, 46),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, green, 1, cv2.LINE_AA)

        # Draw hand skeletons (2 px strokes with round joints)
        for hand in hands:
            pts = [(int(x * w), int(y * h)) for x, y in hand.lm]
            for a, b in connections:
                cv2.line(frame, pts[a], pts[b], cyan, 2, cv2.LINE_AA)
            for i, p in enumerate(pts):
                color = gold if i in (4, 8, 12) else (255, 255, 255)
                radius = 4 if i in (4, 8, 12) else 3
                cv2.circle(frame, p, radius, color, -1, cv2.LINE_AA)

        # Top Bar: Status & Telemetry
        state_color = cyan if st["active"] else dim_gray
        if st["label"] in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT"):
            state_color = gold
        scheme_label = "PALM & FIST" if st.get("scheme") == "palm_fist" else "PINCH"
        cv2.putText(frame, f"JARVIS // {st['label']}", (12, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, state_color, 2, cv2.LINE_AA)
        cv2.putText(frame, f"[{scheme_label}]", (260, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 220, 255), 1, cv2.LINE_AA)

        fps_val = st.get("fps", self._fps)
        inf_val = st.get("inference_ms", self._inference_ms)
        fps_str = f"CAM: {fps_val:.1f} FPS | INF: {inf_val:.1f}ms {'[STANDBY]' if not st['active'] else '[ACTIVE]'}"
        cv2.putText(frame, fps_str, (w - 320, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1, cv2.LINE_AA)

        # Palm progress bar if waking/sleeping
        if st.get("progress", 0.0) > 0.01:
            prog = st["progress"]
            bar_w = int(200 * prog)
            cv2.rectangle(frame, (12, 36), (212, 44), (50, 50, 50), -1)
            cv2.rectangle(frame, (12, 36), (12 + bar_w, 44), gold, -1)
            cv2.putText(frame, f"HOLD: {int(prog*100)}%", (218, 43),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, gold, 1, cv2.LINE_AA)

        # Real-time debug telemetry panel (lower-left)
        scheme = st.get("scheme", getattr(self.cfg, "control_scheme", "palm_fist"))
        dbg = st.get("debug")
        
        panel_h = 160 if scheme == "palm_fist" else 145
        panel_w = 380 if scheme == "palm_fist" else 340
        if self._preview_overlay_buf is None or self._preview_overlay_buf.shape != frame.shape:
            self._preview_overlay_buf = np.empty_like(frame)
        np.copyto(self._preview_overlay_buf, frame)
        cv2.rectangle(self._preview_overlay_buf, (8, h - panel_h), (panel_w, h - 8), (15, 20, 25), -1)
        cv2.addWeighted(self._preview_overlay_buf, 0.70, frame, 0.30, 0, frame)

        if scheme == "palm_fist":
            pose = st.get("pose", "OPEN")
            openness = st.get("openness", 0.0)

            if pose == "OPEN":
                pose_color = green
                pose_desc = "OPEN PALM (MOVING)"
            elif pose == "NEUTRAL":
                pose_color = (50, 160, 255)  # Orange
                pose_desc = "NEUTRAL (FROZEN)"
            else:
                pose_color = gold
                pose_desc = "FIST (CLICK / DRAG)"

            y_pos = h - panel_h + 20
            # 1. Pose Badge
            cv2.putText(frame, "Pose: ", (14, y_pos),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (200, 200, 200), 1, cv2.LINE_AA)
            cv2.putText(frame, pose_desc, (60, y_pos),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, pose_color, 2, cv2.LINE_AA)

            # 2. Openness Score Bar with Threshold Markers
            y_pos += 22
            bar_x, bar_y = 14, y_pos
            bar_len, bar_thickness = 220, 10
            cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_len, bar_y + bar_thickness), (45, 45, 50), -1)
            fill_len = int(bar_len * min(max(openness, 0.0), 1.0))
            if fill_len > 0:
                cv2.rectangle(frame, (bar_x, bar_y), (bar_x + fill_len, bar_y + bar_thickness), pose_color, -1)

            # Draw threshold tick marks: Fist enter (0.28), Fist exit (0.38), Open exit (0.58), Open enter (0.70)
            f_enter = getattr(self.cfg, "fist_enter_thresh", 0.28)
            f_exit = getattr(self.cfg, "fist_exit_thresh", 0.38)
            o_exit = getattr(self.cfg, "open_exit_thresh", 0.58)
            o_enter = getattr(self.cfg, "open_enter_thresh", 0.70)
            for thresh, col in [(f_enter, (0, 0, 220)), (f_exit, gold), (o_exit, (50, 160, 255)), (o_enter, green)]:
                tx = int(bar_x + bar_len * thresh)
                cv2.line(frame, (tx, bar_y - 2), (tx, bar_y + bar_thickness + 2), col, 1, cv2.LINE_AA)

            cv2.putText(frame, f"Open: {openness:.2f}", (bar_x + bar_len + 8, bar_y + 9),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, (220, 220, 220), 1, cv2.LINE_AA)

            # 3. Finger Ratios
            y_pos += 24
            if dbg:
                f_str = f"Idx:{dbg.get('idx',0):.2f} Mid:{dbg.get('mid',0):.2f} Ring:{dbg.get('ring',0):.2f} Pnk:{dbg.get('pinky',0):.2f}"
            else:
                f_str = "Ratios: Standby"
            cv2.putText(frame, f_str, (14, y_pos),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 220, 255), 1, cv2.LINE_AA)

            # 4. Mode & Anchor
            y_pos += 20
            p_anchor = getattr(self.cfg, "palm_anchor", "knuckles").upper()
            t_mode = getattr(self.cfg, "tracking_mode", "relative").upper()
            cv2.putText(frame, f"Mode: {t_mode} | Anchor: {p_anchor}", (14, y_pos),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 220, 255), 1, cv2.LINE_AA)

            # 5. Status & Precision/Clutch
            y_pos += 20
            prec_str = "FINE AIM" if st.get("precision") else "NORMAL"
            clutch_str = " [CLUTCHED]" if st.get("clutched") else ""
            status_desc = "DRAGGING" if st.get("dragging") else ("FROZEN" if pose != "OPEN" else "TRACKING")
            cv2.putText(frame, f"Status: {status_desc}{clutch_str} | Aim: {prec_str}", (14, y_pos),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, pose_color, 1, cv2.LINE_AA)

        else:
            # Legacy Pinch Scheme HUD
            if dbg:
                ti = dbg.get("pinch_ti", 0.0)
                tm = dbg.get("pinch_tm", 0.0)
                ti_col = green if ti < self.cfg.pinch_down else (gold if ti < self.cfg.pinch_freeze else (220, 220, 220))
                tm_col = green if tm < self.cfg.pinch_down else (220, 220, 220)

                y_pos = h - panel_h + 20
                cv2.putText(frame, f"Pinch T-Idx : {ti:.3f} (down:{self.cfg.pinch_down} frz:{self.cfg.pinch_freeze})",
                            (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.42, ti_col, 1, cv2.LINE_AA)
                y_pos += 20
                cv2.putText(frame, f"Pinch T-Mid : {tm:.3f} (rp_down:{self.cfg.pinch_down})",
                            (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.42, tm_col, 1, cv2.LINE_AA)
                y_pos += 20
                cv2.putText(frame, f"Finger Ratios: Idx={dbg.get('idx',0):.2f} Mid={dbg.get('mid',0):.2f}",
                            (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 220, 255), 1, cv2.LINE_AA)
                y_pos += 18
                cv2.putText(frame, f"Tracking Mode: {getattr(self.cfg, 'tracking_mode', 'relative').upper()} | Anchor: {getattr(self.cfg, 'anchor_point', 'blend').upper()}",
                            (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 220, 255), 1, cv2.LINE_AA)
                y_pos += 20
                status_desc = "FROZEN (CLICKING)" if dbg.get("closing") else ("DRAGGING" if dbg.get("dragging") else "TRACKING")
                cv2.putText(frame, f"Status: {status_desc} | Hands: {st.get('hands',0)}",
                            (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.42, gold if dbg.get("closing") else cyan, 1, cv2.LINE_AA)

        cv2.imshow(win_name, frame)
