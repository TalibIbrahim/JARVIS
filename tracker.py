"""Camera + MediaPipe hand tracking loop. Feeds landmarks to the gesture engine."""
import sys
import threading
import time

import cv2
import mediapipe as mp

from gestures import Hand


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
        self._last_frame_t = time.time()

    def run(self):
        cfg = self.cfg
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else 0
        cap = cv2.VideoCapture(cfg.camera_index, backend)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_height)
        cap.set(cv2.CAP_PROP_FPS, 30)
        if not cap.isOpened():
            self.ctx.say("I can't open the camera, sir.")
            self.overlay.post("error", text="CAMERA NOT FOUND")
            return

        hands = mp.solutions.hands.Hands(
            max_num_hands=2, model_complexity=0,
            min_detection_confidence=0.6, min_tracking_confidence=0.6)
        connections = list(mp.solutions.hands.HAND_CONNECTIONS)
        failures = 0
        win_name = "JARVIS // VISION  (q to quit)"

        while not self.stop.is_set():
            # CPU throttling: drop to ~10 FPS when on standby to save CPU on laptops
            if not self.engine.active and not cfg.show_preview:
                time.sleep(0.06)

            ok, frame = cap.read()
            if not ok:
                failures += 1
                if failures == 30:
                    self.ctx.say("Camera feed lost, sir.")
                    self.overlay.post("error", text="CAMERA FEED LOST")
                time.sleep(0.03)
                continue
            failures = 0

            frame = cv2.flip(frame, 1)                       # mirror, so moving right moves right
            result = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            found = []
            if result.multi_hand_landmarks:
                for lms in result.multi_hand_landmarks:
                    found.append(Hand([(p.x, p.y) for p in lms.landmark]))

            now = time.time()
            dt = max(now - self._last_frame_t, 1e-4)
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
            self.overlay.post("state", **st)

            if cfg.show_preview:
                self._preview(frame, found, connections, st, win_name)
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
        red = (60, 60, 240)
        dim_gray = (120, 120, 120)

        # Draw active tracking boundary box
        m = self.cfg.box_margin
        x1, y1 = int(m * w), int(m * h)
        x2, y2 = int((1 - m) * w), int((1 - m) * h)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (70, 70, 30), 1)
        cv2.putText(frame, f"MARGIN: {m*100:.0f}%", (x1 + 4, y1 + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, dim_gray, 1, cv2.LINE_AA)

        # Draw hand skeletons
        for hand in hands:
            pts = [(int(x * w), int(y * h)) for x, y in hand.lm]
            for a, b in connections:
                cv2.line(frame, pts[a], pts[b], cyan, 1, cv2.LINE_AA)
            for i, p in enumerate(pts):
                color = gold if i in (4, 8, 12) else (255, 255, 255)
                radius = 4 if i in (4, 8, 12) else 2
                cv2.circle(frame, p, radius, color, -1, cv2.LINE_AA)

        # Top Bar: JARVIS Status & FPS
        state_color = cyan if st["active"] else dim_gray
        if st["label"] in ("CLICK", "DOUBLE CLICK", "DRAG", "DROP", "RIGHT CLICK", "ZOOM IN", "ZOOM OUT"):
            state_color = gold
        cv2.putText(frame, f"JARVIS // {st['label']}", (12, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, state_color, 2, cv2.LINE_AA)

        fps_str = f"FPS: {self._fps:.1f} {'[LOW-POWER]' if not st['active'] else '[ONLINE]'}"
        cv2.putText(frame, fps_str, (w - 220, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)

        # Palm progress bar if waking/sleeping
        if st.get("progress", 0.0) > 0.01:
            prog = st["progress"]
            bar_w = int(200 * prog)
            cv2.rectangle(frame, (12, 36), (212, 44), (50, 50, 50), -1)
            cv2.rectangle(frame, (12, 36), (12 + bar_w, 44), gold, -1)
            cv2.putText(frame, f"HOLD: {int(prog*100)}%", (218, 43),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, gold, 1, cv2.LINE_AA)

        # Real-time debug telemetry panel (lower-left)
        dbg = st.get("debug")
        if dbg:
            ti = dbg.get("pinch_ti", 0.0)
            tm = dbg.get("pinch_tm", 0.0)
            ti_col = green if ti < self.cfg.pinch_down else (gold if ti < self.cfg.pinch_freeze else (220, 220, 220))
            tm_col = green if tm < self.cfg.pinch_down else (220, 220, 220)

            # Draw semi-transparent background card for telemetry
            overlay = frame.copy()
            cv2.rectangle(overlay, (8, h - 145), (320, h - 8), (15, 20, 25), -1)
            cv2.addWeighted(overlay, 0.65, frame, 0.35, 0, frame)

            y_pos = h - 128
            cv2.putText(frame, f"Pinch T-Idx : {ti:.3f} (down:{self.cfg.pinch_down} frz:{self.cfg.pinch_freeze})",
                        (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.42, ti_col, 1, cv2.LINE_AA)
            y_pos += 20
            cv2.putText(frame, f"Pinch T-Mid : {tm:.3f} (rp_down:{self.cfg.pinch_down})",
                        (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.42, tm_col, 1, cv2.LINE_AA)
            y_pos += 20
            cv2.putText(frame, f"Finger Ratios: Idx={dbg.get('idx',0):.2f} Mid={dbg.get('mid',0):.2f}",
                        (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 220, 255), 1, cv2.LINE_AA)
            y_pos += 18
            cv2.putText(frame, f"              Ring={dbg.get('ring',0):.2f} Pnk={dbg.get('pinky',0):.2f} Thb={dbg.get('thumb_out',0):.2f}",
                        (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 220, 255), 1, cv2.LINE_AA)
            y_pos += 20
            status_desc = "FROZEN (CLICKING)" if dbg.get("closing") else ("DRAGGING" if dbg.get("dragging") else "TRACKING")
            cv2.putText(frame, f"Status: {status_desc} | Hands: {st.get('hands',0)}",
                        (14, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.42, gold if dbg.get("closing") else cyan, 1, cv2.LINE_AA)

        cv2.imshow(win_name, frame)
