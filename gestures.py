"""
Gesture recognition. Pure Python (no camera, no OS calls) so it can be tested with fake landmarks.

Input : list of Hand (21 landmarks each, x already mirrored, normalised 0..1) + a timestamp.
Output: list of Action for the controller / overlay.

Control Schemes:
1. Palm & Fist (Default):
   - Open palm = move cursor (anchored on wrist + 4 MCP knuckles, landmarks 0, 5, 9, 13, 17).
   - Fist closed & reopened quickly (< 400 ms) = click.
   - Two quick fists (window ~0.65 s) = double click.
   - Fist held (> 350 ms) = drag: mouse down, hand moves, opening hand drops.
   - Thumb + middle pinch = right click (or via dwell radial menu).
   - Peace sign moving up/down = scroll.
   - Two open hands spreading/closing = zoom.
   - Wake: hold open palm ~0.8 s ONLY while in standby.
   - Standby: hold thumbs-up ~1.2 s, hotkey, or voice. (Open palm while awake NEVER sleeps).

2. Pinch (Legacy selectable in settings):
   - Index pointing = move cursor (anchored on blended index tip / PIP).
   - Thumb + index pinch quick = click / double click.
   - Thumb + index pinch hold = drag.
   - Open palm hold while awake = standby.
"""
import math
from dataclasses import dataclass

WRIST = 0
THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20

EXTENDED = 1.55   # wrist->tip / wrist->knuckle ratio above this = finger straight
CURLED = 1.40     # below this = finger folded
FOLDED = 1.30     # below this the finger is tucked into the palm


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class Hand:
    lm: list            # 21 (x, y) tuples
    label: str = ""


@dataclass
class Action:
    kind: str           # move rewind click right_click drag_start drag_end scroll zoom activate deactivate lost
    x: float = 0.0
    y: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    n: int = 0
    double: bool = False
    speed: float = 0.0
    precision: bool = False
    clutched: bool = False
    t: float = 0.0      # rewind: flick start time (engine clock)
    age: float = 0.0    # rewind: seconds between flick start and now


class Pose:
    """Finger extension ratios, openness score, and pinch metrics for one hand."""

    def __init__(self, lm):
        self.lm = lm
        self.size = max(_d(lm[WRIST], lm[MIDDLE_MCP]), 1e-6)

        def ratio(mcp, tip):
            return _d(lm[WRIST], lm[tip]) / max(_d(lm[WRIST], lm[mcp]), 1e-6)

        self.idx = ratio(INDEX_MCP, INDEX_TIP)
        self.mid = ratio(MIDDLE_MCP, MIDDLE_TIP)
        self.ring = ratio(RING_MCP, RING_TIP)
        self.pinky = ratio(PINKY_MCP, PINKY_TIP)
        self.thumb_out = _d(lm[THUMB_TIP], lm[PINKY_MCP]) / self.size
        self.pinch_ti = _d(lm[THUMB_TIP], lm[INDEX_TIP]) / self.size
        self.pinch_tm = _d(lm[THUMB_TIP], lm[MIDDLE_TIP]) / self.size

        # Continuous finger openness: 0.0 = curled into fist, 1.0 = extended straight
        def finger_openness(r):
            return min(max((r - 1.25) / (1.65 - 1.25), 0.0), 1.0)

        self.open_idx = finger_openness(self.idx)
        self.open_mid = finger_openness(self.mid)
        self.open_ring = finger_openness(self.ring)
        self.open_pinky = finger_openness(self.pinky)
        self.openness = (self.open_idx + self.open_mid + self.open_ring + self.open_pinky) / 4.0

    @property
    def palm(self):
        # 1. Classical hyper-extended palm check (backward compatibility with test mocks)
        classic_palm = (min(self.idx, self.mid, self.ring, self.pinky) > EXTENDED
                        and self.thumb_out > 0.8 and self.pinch_ti > 0.6)
        if classic_palm:
            return True

        # 2. Natural open palm facing camera (robust on real webcams with perspective distortion)
        fingers_open = (self.openness >= 0.38) and (self.idx > 1.25 and self.mid > 1.25)
        extended_count = sum([self.idx > 1.28, self.mid > 1.28, self.ring > 1.22, self.pinky > 1.20])
        has_extended_fingers = fingers_open or (extended_count >= 3 and self.openness >= 0.35)

        not_pinching = self.pinch_ti > 0.28
        not_peace = not (self.idx > 1.35 and self.mid > 1.35 and self.ring < 1.25 and self.pinky < 1.25)
        thumb_natural = self.thumb_out > 0.28
        return has_extended_fingers and not_pinching and not_peace and thumb_natural

    @property
    def peace(self):
        return (self.idx > EXTENDED and self.mid > EXTENDED
                and self.ring < CURLED and self.pinky < CURLED and self.pinch_ti > 0.6)

    @property
    def thumbs_up(self):
        # 4 fingers curled in fist, thumb extended upward/outward
        four_curled = max(self.open_idx, self.open_mid, self.open_ring, self.open_pinky) < 0.35
        thumb_extended = (self.thumb_out > 0.70) and (self.lm[THUMB_TIP][1] < self.lm[INDEX_MCP][1])
        return four_curled and thumb_extended


class GestureEngine:
    def __init__(self, cfg, overlay=None):
        self.cfg = cfg
        self.overlay = overlay
        self.active = False
        self.nhands = 0
        self.progress = 0.0
        self._req = None
        self._flash = ""
        self._flash_until = 0.0
        self._mode = ""
        self._lost_sent = False
        self._palm_hist = []
        self._shortcut_cooldown_until = 0.0
        self._reset_state()

    def set_overlay(self, overlay):
        """Thread-safe setter for UI overlay event dispatcher."""
        self.overlay = overlay

    # ---------- public ----------
    def request(self, want):
        """Thread-safe: voice / hotkey ask for wake (True) or standby (False)."""
        self._req = bool(want)

    def request_toggle(self):
        """Thread-safe toggle of active state."""
        self._req = "toggle"

    def island_page_step(self, direction: int = 1) -> Action:
        """Triggers island page step (1 = next, -1 = prev) via overlay and returns Action."""
        if self.overlay and hasattr(self.overlay, "post"):
            try:
                self.overlay.post("island_page_step", direction=direction)
            except Exception:
                pass
        return Action("island_page_step", n=direction)

    def status(self, now):
        if now < self._flash_until:
            label = self._flash
        elif not self.active:
            label = "STANDBY"
        elif self.dragging:
            label = "DRAGGING"
        elif self._mode:
            label = self._mode
        else:
            label = "TRACKING"

        scheme = getattr(self.cfg, "control_scheme", "palm_fist")
        res = {
            "active": self.active,
            "label": label,
            "progress": self.progress,
            "hands": self.nhands,
            "scheme": scheme,
            "openness": getattr(self, "last_openness", 0.0),
            "pose": getattr(self, "confirmed_pose", "OPEN"),
            "precision": getattr(self, "precision_active", False),
            "clutched": getattr(self, "clutched", False),
            "dragging": self.dragging
        }
        if hasattr(self, "last_pose") and self.last_pose is not None:
            p = self.last_pose
            res["debug"] = {
                "openness": p.openness,
                "pinch_ti": p.pinch_ti,
                "pinch_tm": p.pinch_tm,
                "idx": p.idx,
                "mid": p.mid,
                "ring": p.ring,
                "pinky": p.pinky,
                "thumb_out": p.thumb_out,
                "closing": getattr(self, "last_closing", False),
                "dragging": self.dragging,
            }
        return res

    def update(self, hands, now):
        cfg = self.cfg
        actions = []
        self._mode = ""
        self.nhands = len(hands)

        if self._req is not None:
            want, self._req = self._req, None
            if want == "toggle":
                want = not self.active
            if want != self.active:
                self._set_active(want, actions)

        # ---- no hand in view ----
        if not hands:
            self.progress = 0.0
            self.palm_since = None
            self.palm_latched = False
            self.thumbs_up_since = None
            self.last_pose = None
            self.last_closing = False
            self.last_openness = 0.0
            if self.lost_since is None:
                self.lost_since = now
            elif now - self.lost_since > cfg.lost_grace:
                self._end_pinch(actions)
                self._clear_motion()
                if not self._lost_sent:
                    actions.append(Action("lost"))
                    self._lost_sent = True
            return actions

        self.lost_since = None
        self._lost_sent = False

        scheme = getattr(cfg, "control_scheme", "palm_fist")
        if scheme == "palm_fist":
            return self._update_palm_fist(hands, now, actions)
        else:
            return self._update_pinch(hands, now, actions)

    # ---------- Palm & Fist Control Scheme ----------
    def _update_palm_fist(self, hands, now, actions):
        cfg = self.cfg

        # ---- two hands: zoom ----
        if self.active and len(hands) >= 2:
            self._end_pinch(actions)
            self.palm_since = None
            self.thumbs_up_since = None
            self.progress = 0.0
            self.last_pose = None
            dist = _d(hands[0].lm[INDEX_TIP], hands[1].lm[INDEX_TIP])
            if self.two_since is None:
                self.two_since = now
            if now - self.two_since >= cfg.zoom_enter_delay:
                self._mode = "ZOOM"
                if self.zoom_prev is None:
                    self.zoom_prev = dist
                else:
                    self.zoom_acc += dist - self.zoom_prev
                    self.zoom_prev = dist
                    while self.zoom_acc >= cfg.zoom_step:
                        actions.append(Action("zoom", n=1))
                        self.zoom_acc -= cfg.zoom_step
                        self._flash_label("ZOOM IN", now)
                    while self.zoom_acc <= -cfg.zoom_step:
                        actions.append(Action("zoom", n=-1))
                        self.zoom_acc += cfg.zoom_step
                        self._flash_label("ZOOM OUT", now)
            return actions

        self.two_since = None
        self.zoom_prev = None
        self.zoom_acc = 0.0

        # ---- one hand ----
        lm = hands[0].lm
        p = Pose(lm)
        self.last_pose = p
        openness = p.openness
        self.last_openness = openness

        # 1. Pose Classification with Hysteresis & Debouncing
        open_enter = getattr(cfg, "open_enter_thresh", 0.70)
        open_exit = getattr(cfg, "open_exit_thresh", 0.58)
        fist_enter = getattr(cfg, "fist_enter_thresh", 0.28)
        fist_exit = getattr(cfg, "fist_exit_thresh", 0.38)

        if self.confirmed_pose == "OPEN":
            if openness < open_exit:
                target_pose = "FIST" if openness <= fist_enter else "NEUTRAL"
            else:
                target_pose = "OPEN"
        elif self.confirmed_pose == "FIST":
            if openness > fist_exit:
                target_pose = "OPEN" if openness >= open_enter else "NEUTRAL"
            else:
                target_pose = "FIST"
        else:  # "NEUTRAL"
            if openness >= open_enter:
                target_pose = "OPEN"
            elif openness <= fist_enter:
                target_pose = "FIST"
            else:
                target_pose = "NEUTRAL"

        debounce = getattr(cfg, "pose_debounce_frames", 2)
        if target_pose != self.confirmed_pose:
            if target_pose == self.pending_pose:
                self.pending_pose_count += 1
                if self.pending_pose_count >= debounce:
                    prev_pose = self.confirmed_pose
                    self.confirmed_pose = target_pose
                    self.pending_pose = None
                    self.pending_pose_count = 0
                    self._on_pose_transition(prev_pose, target_pose, now, actions)
            else:
                self.pending_pose = target_pose
                self.pending_pose_count = 1
        else:
            self.pending_pose = None
            self.pending_pose_count = 0

        cur_pose = self.confirmed_pose

        # 2. Wake & Standby Rules
        if not self.active:
            # ONLY Open Palm wakes from standby! Fist or Neutral must never wake.
            is_wake_palm = bool(p.palm or (cur_pose == "OPEN" and openness >= 0.45))
            if is_wake_palm:
                self.palm_last_seen = now
                if not self.palm_latched:
                    if self.palm_since is None:
                        self.palm_since = now
                    hold = cfg.activate_hold
                    self.progress = min((now - self.palm_since) / hold, 1.0)
                    if now - self.palm_since >= hold:
                        self._set_active(True, actions)
                        self.palm_latched = True
                        self.palm_since = None
                        self.progress = 0.0
            else:
                palm_grace = getattr(cfg, "palm_grace", 0.35)
                if self.palm_since is not None and (now - getattr(self, "palm_last_seen", now)) <= palm_grace:
                    pass
                else:
                    self.palm_latched = False
                    self.palm_since = None
                    self.progress = 0.0
            return actions

        # While active:
        # Open palm must NEVER sleep!
        # Fist must NEVER sleep!
        # Thumbs-up hold puts app to standby
        if p.thumbs_up:
            if self.thumbs_up_since is None:
                self.thumbs_up_since = now
            hold = getattr(cfg, "thumbs_up_hold", 1.2)
            self.progress = min((now - self.thumbs_up_since) / hold, 1.0)
            self._mode = "STANDBY HOLD"
            if now - self.thumbs_up_since >= hold:
                self._set_active(False, actions)
                self.thumbs_up_since = None
                self.progress = 0.0
                return actions
        else:
            self.thumbs_up_since = None
            if not (cur_pose == "OPEN" and self.palm_since is not None):
                self.progress = 0.0

        # Scroll: peace sign
        if p.peace:
            self._mode = "SCROLL"
            y = (lm[INDEX_TIP][1] + lm[MIDDLE_TIP][1]) / 2.0
            if self.scroll_y is not None:
                self.scroll_acc += self.scroll_y - y
                while abs(self.scroll_acc) >= cfg.scroll_step:
                    n = 1 if self.scroll_acc > 0 else -1
                    self.scroll_acc -= n * cfg.scroll_step
                    actions.append(Action("scroll", n=-n if cfg.scroll_invert else n))
            self.scroll_y = y
            return actions
        self.scroll_y = None
        self.scroll_acc = 0.0

        # Right click: thumb + middle pinch
        rp = (p.pinch_tm < (cfg.pinch_up if self.rp_down else cfg.pinch_down)
              and p.pinch_tm < p.pinch_ti and p.mid > FOLDED)
        if rp and not self.rp_down and not self.dragging:
            actions.append(Action("right_click"))
            self._flash_label("RIGHT CLICK", now)
        self.rp_down = rp

        # One-handed zoom: thumb+index pinch with middle/ring/pinky clearly NOT curled.
        zp = False
        if getattr(cfg, "gesture_one_hand_zoom", True) and not self.dragging and not rp:
            others = (p.open_mid + p.open_ring + p.open_pinky) / 3.0
            zp = (p.pinch_ti < (cfg.pinch_up if self.zp_down else cfg.pinch_down)
                  and p.pinch_tm > p.pinch_ti and p.pinch_tm > cfg.pinch_down
                  and others >= (0.45 if self.zp_down else 0.55)
                  and cur_pose != "FIST")
        if zp:
            if not self.zp_down:
                self.zp_down = True
                self.zp_since = now
                self._palm_hist = []
            if not self.zoom1_on and now - self.zp_since >= getattr(cfg, "zoom_pinch_hold", 0.30):
                self.zoom1_on = True
                self.zp_y = None
                self.zp_acc = 0.0
                lb_s = getattr(cfg, "cursor_lookback_ms", 200.0) / 1000.0
                actions.append(Action("rewind", t=self.zp_since - lb_s, age=now - self.zp_since + lb_s))
            if self.zoom1_on:
                self._mode = "ZOOM"
                self._flash_label("ZOOM", now)
                y = self._compute_palm_anchor(lm)[1]
                if self.zp_y is not None:
                    self.zp_acc += self.zp_y - y   # up = positive = zoom in
                    while self.zp_acc >= cfg.zoom_step:
                        actions.append(Action("zoom", n=1))
                        self.zp_acc -= cfg.zoom_step
                    while self.zp_acc <= -cfg.zoom_step:
                        actions.append(Action("zoom", n=-1))
                        self.zp_acc += cfg.zoom_step
                self.zp_y = y
            self.last_anchor = None
            self.last_anchor_t = None
            self.precision_active = False
            self.clutched = False
            return actions
        if self.zp_down:
            self._end_zoom_pinch(now)

        # Fist Hold -> Drag
        drag_hold = getattr(cfg, "fist_drag_min_hold", 0.35)
        if cur_pose == "FIST" and self.fist_active:
            if not self.dragging and (now - self.fist_start_t) >= drag_hold:
                self.dragging = True
                actions.append(Action("drag_start"))
                self._flash_label("DRAG", now)

        # Phase 8: Gesture Shortcuts (Vertical Slide & Horizontal Swipe)
        flick_actions = []
        self._check_gesture_shortcuts(lm, p, now, flick_actions)

        # 3. Cursor Motion (Wrist + 4 MCP Knuckles Anchor)
        anchor = self._compute_palm_anchor(lm)
        m = cfg.box_margin
        in_box = True if m <= 0.001 else ((m <= anchor[0] <= 1.0 - m) and (m <= anchor[1] <= 1.0 - m))
        clutch_enabled = getattr(cfg, "clutch_enabled", True)

        frozen = (now < self.reopen_freeze_until) or (cur_pose != "OPEN" and not self.dragging)
        can_move = (self.dragging or (cur_pose == "OPEN")) and not (frozen or p.thumbs_up or self.rp_down or p.peace)

        if can_move:
            if clutch_enabled and not in_box and not self.dragging:
                self.clutched = True
                self.last_anchor = None
                self.last_anchor_t = None
                self.precision_active = False
            else:
                self.clutched = False
                ax, ay = self._map(*anchor)
                if self.last_anchor is not None and self.last_anchor_t is not None:
                    raw_dt = now - self.last_anchor_t
                    if raw_dt > 0.5:
                        # genuine stall (sleep/resume, camera hiccup): drop this one delta only
                        adx, ady, speed = 0.0, 0.0, 0.0
                    else:
                        dt = max(raw_dt, 1e-3)
                        adx = anchor[0] - self.last_anchor[0]
                        ady = anchor[1] - self.last_anchor[1]
                        speed = math.hypot(adx, ady) / dt
                else:
                    adx, ady, speed = 0.0, 0.0, 0.0
                self.last_anchor = anchor
                self.last_anchor_t = now

                is_precision = speed < getattr(cfg, "precision_threshold", 0.015)
                self.precision_active = is_precision
                actions.append(Action("move", x=ax, y=ay, dx=adx, dy=ady, speed=speed, precision=is_precision))
        else:
            self.clutched = False
            self.last_anchor = None
            self.last_anchor_t = None
            self.precision_active = False

        actions.extend(flick_actions)
        return actions

    def _on_pose_transition(self, prev_pose, new_pose, now, actions):
        cfg = self.cfg

        # 1. Leaving OPEN: openness dropping -> freeze cursor immediately
        if prev_pose == "OPEN" and new_pose in ("NEUTRAL", "FIST"):
            self.last_anchor = None
            self.last_anchor_t = None

        # 2. Entering FIST: record start time
        if new_pose == "FIST":
            self.fist_start_t = now
            self.fist_active = True

        # 3. Exiting FIST: reopened!
        if prev_pose == "FIST" and self.fist_active:
            fist_duration = now - self.fist_start_t
            self.fist_active = False
            reopen_freeze_ms = getattr(cfg, "reopen_freeze_ms", 150.0)

            if self.dragging:
                self.dragging = False
                actions.append(Action("drag_end"))
                self._flash_label("DROP", now)
                self.reopen_freeze_until = now + (reopen_freeze_ms / 1000.0)
            else:
                max_click_hold = getattr(cfg, "fist_click_max_hold", 0.40)
                if fist_duration <= max_click_hold:
                    double_window = getattr(cfg, "fist_double_window", 0.65)
                    double = (now - self.last_click) <= double_window
                    actions.append(Action("click", double=double))
                    self.last_click = -1e9 if double else now
                    self._flash_label("DOUBLE CLICK" if double else "CLICK", now)
                    self.reopen_freeze_until = now + (reopen_freeze_ms / 1000.0)

    def _check_gesture_shortcuts(self, lm, p, now, actions) -> bool:
        """
        Deliberate-flick shortcuts. Never suppresses cursor motion (caller always emits the move).
        - Horizontal flick: right = Alt+Tab, left = Alt+Shift+Tab.
        - Vertical flick: volume +/-5%, or an island page step when the hand is near the top.
        A flick needs displacement >= 0.18 frame widths within <= 0.25 s, a peak speed >= 1.2 frame
        widths/s, and a dominant axis (> 2x the other). When one fires, a "rewind" action is appended so
        the controller puts the cursor back where it was when the flick began.
        Returns True if a shortcut fired.
        """
        cfg = self.cfg
        if not getattr(cfg, "gesture_shortcuts_enabled", True):
            self._palm_hist = []
            return False

        # Exclude other dedicated poses/actions (peace/scroll, thumbs-up, dragging, right-click pinch)
        if p.peace or p.thumbs_up or self.dragging or self.rp_down:
            self._palm_hist = []
            return False

        hx = (lm[WRIST][0] + lm[MIDDLE_MCP][0]) / 2.0
        hy = (lm[WRIST][1] + lm[MIDDLE_MCP][1]) / 2.0

        hist = self._palm_hist
        if hist and now < hist[-1][0]:          # clock jumped backwards
            hist.clear()
        hist.append((now, hx, hy))
        while hist and now - hist[0][0] > 0.30:
            hist.pop(0)
        if len(hist) < 3 or now < self._shortcut_cooldown_until:
            return False

        # Window start candidates: samples within 0.25 s of now
        win = [s for s in hist if now - s[0] <= 0.25]
        if len(win) < 3:
            return False

        # Peak segment speed (frame widths / s); segment dt floored at one 60 fps frame
        peak_x = peak_y = 0.0
        for (t0, x0, y0), (t1, x1, y1) in zip(win, win[1:]):
            sdt = max(t1 - t0, 0.016)
            peak_x = max(peak_x, abs(x1 - x0) / sdt)
            peak_y = max(peak_y, abs(y1 - y0) / sdt)

        t0, x0, y0 = win[0]
        dx = hx - x0
        dy = hy - y0
        min_disp = getattr(cfg, "flick_min_disp", 0.18)
        min_peak = getattr(cfg, "flick_min_peak", 1.2)

        horizontal = (abs(dx) >= min_disp and peak_x >= min_peak and abs(dx) > 2.0 * abs(dy))
        vertical = (abs(dy) >= min_disp and peak_y >= min_peak and abs(dy) > 2.0 * abs(dx))

        if horizontal and getattr(cfg, "gesture_swipe_switch", True):
            self._shortcut_cooldown_until = now + 0.60
            self._palm_hist = []
            self._fire_window_switch(dx > 0, now, actions)
            actions.append(Action("rewind", t=t0, age=now - t0))
            return True

        if vertical:
            is_top_area = (
                hy <= 0.38
                or y0 <= 0.38
                or getattr(self, "in_island_area", False)
                or (hasattr(self, "cursor_y") and self.cursor_y is not None and self.cursor_y < 250)
            )
            if is_top_area:
                self._shortcut_cooldown_until = now + 0.60
                self._palm_hist = []
                direction = 1 if dy > 0 else -1
                if self.overlay and hasattr(self.overlay, "post"):
                    try:
                        self.overlay.post("island_page_step", direction=direction)
                    except Exception:
                        pass
                actions.append(Action("island_page_step", n=direction, dy=dy))
                actions.append(Action("rewind", t=t0, age=now - t0))
                self._flash_label("ISLAND NEXT" if direction > 0 else "ISLAND PREV", now)
                return True

            if getattr(cfg, "gesture_slide_volume", True):
                self._shortcut_cooldown_until = now + 0.60
                self._palm_hist = []
                delta = 5 if dy < 0 else -5
                level = 50
                try:
                    import system_control
                    if delta > 0:
                        level = system_control.volume_up(steps=abs(delta))
                    else:
                        level = system_control.volume_down(steps=abs(delta))
                except Exception:
                    level = max(0, min(100, 50 + delta))

                if self.overlay and hasattr(self.overlay, "post"):
                    try:
                        self.overlay.post("hud_volume", level=level, delta=delta)
                    except Exception:
                        pass
                actions.append(Action("volume_up" if delta > 0 else "volume_down", dy=dy, n=delta))
                actions.append(Action("rewind", t=t0, age=now - t0))
                self._flash_label("VOLUME +5%" if delta > 0 else "VOLUME -5%", now)
                return True

        return False

    def _fire_window_switch(self, right, now, actions):
        title = ""
        try:
            import system_control
            title = system_control.get_foreground_window_title() or ""
        except Exception:
            pass

        try:
            import pyautogui
            if right:
                pyautogui.hotkey("alt", "tab", _pause=False)
            else:
                pyautogui.hotkey("alt", "shift", "tab", _pause=False)
        except Exception:
            pass

        try:
            import system_control
            new_title = system_control.get_foreground_window_title()
            if new_title:
                title = new_title
        except Exception:
            pass

        if not title:
            title = "Switched Window"

        if self.overlay and hasattr(self.overlay, "post"):
            try:
                self.overlay.post("hud_switch_window", title=title)
            except Exception:
                pass

        actions.append(Action("switch_window"))
        self._flash_label("SWITCH WINDOW", now)

    def _compute_palm_anchor(self, lm):
        mode = getattr(self.cfg, "palm_anchor", "knuckles")
        if mode == "knuckles":
            # Knuckles centroid (MCP joints 5, 9, 13, 17)
            ax = (lm[INDEX_MCP][0] + lm[MIDDLE_MCP][0] + lm[RING_MCP][0] + lm[PINKY_MCP][0]) / 4.0
            ay = (lm[INDEX_MCP][1] + lm[MIDDLE_MCP][1] + lm[RING_MCP][1] + lm[PINKY_MCP][1]) / 4.0
            return (ax, ay)
        elif mode == "palm_center":
            # Midpoint of wrist (0) and middle MCP (9)
            ax = (lm[WRIST][0] + lm[MIDDLE_MCP][0]) / 2.0
            ay = (lm[WRIST][1] + lm[MIDDLE_MCP][1]) / 2.0
            return (ax, ay)
        elif mode == "centroid":
            # Metacarpal weighted centroid
            ax = 0.20 * lm[WRIST][0] + 0.20 * lm[INDEX_MCP][0] + 0.30 * lm[MIDDLE_MCP][0] + 0.15 * lm[RING_MCP][0] + 0.15 * lm[PINKY_MCP][0]
            ay = 0.20 * lm[WRIST][1] + 0.20 * lm[INDEX_MCP][1] + 0.30 * lm[MIDDLE_MCP][1] + 0.15 * lm[RING_MCP][1] + 0.15 * lm[PINKY_MCP][1]
            return (ax, ay)
        elif mode == "index_mcp":
            return lm[INDEX_MCP]
        return (lm[MIDDLE_MCP][0], lm[MIDDLE_MCP][1])

    # ---------- Legacy Pinch Control Scheme ----------
    def _update_pinch(self, hands, now, actions):
        cfg = self.cfg

        # ---- two hands: zoom ----
        if self.active and len(hands) >= 2:
            self._end_pinch(actions)
            self.palm_since = None
            self.progress = 0.0
            self.last_pose = None
            dist = _d(hands[0].lm[INDEX_TIP], hands[1].lm[INDEX_TIP])
            if self.two_since is None:
                self.two_since = now
            if now - self.two_since >= cfg.zoom_enter_delay:
                self._mode = "ZOOM"
                if self.zoom_prev is None:
                    self.zoom_prev = dist
                else:
                    self.zoom_acc += dist - self.zoom_prev
                    self.zoom_prev = dist
                    while self.zoom_acc >= cfg.zoom_step:
                        actions.append(Action("zoom", n=1))
                        self.zoom_acc -= cfg.zoom_step
                        self._flash_label("ZOOM IN", now)
                    while self.zoom_acc <= -cfg.zoom_step:
                        actions.append(Action("zoom", n=-1))
                        self.zoom_acc += cfg.zoom_step
                        self._flash_label("ZOOM OUT", now)
            return actions
        self.two_since = None
        self.zoom_prev = None
        self.zoom_acc = 0.0

        # ---- one hand ----
        lm = hands[0].lm
        p = Pose(lm)
        self.last_pose = p
        palm = p.palm

        # palm hold toggles wake / standby in pinch mode
        if palm and not self.pinch_down and not self.dragging:
            self.palm_last_seen = now
            if not self.palm_latched:
                if self.palm_since is None:
                    self.palm_since = now
                hold = cfg.deactivate_hold if self.active else cfg.activate_hold
                self.progress = min((now - self.palm_since) / hold, 1.0)
                if now - self.palm_since >= hold:
                    self._set_active(not self.active, actions)
                    self.palm_latched = True
                    self.palm_since = None
                    self.progress = 0.0
        else:
            palm_grace = getattr(cfg, "palm_grace", 0.35)
            if self.palm_since is not None and (now - getattr(self, "palm_last_seen", now)) <= palm_grace:
                pass
            else:
                if not palm:
                    self.palm_latched = False
                self.palm_since = None
                self.progress = 0.0

        if not self.active:
            return actions
        if palm:
            self._mode = "REPULSOR"
            return actions

        # scroll: peace sign
        if p.peace:
            self._mode = "SCROLL"
            y = (lm[INDEX_TIP][1] + lm[MIDDLE_TIP][1]) / 2.0
            if self.scroll_y is not None:
                self.scroll_acc += self.scroll_y - y
                while abs(self.scroll_acc) >= cfg.scroll_step:
                    n = 1 if self.scroll_acc > 0 else -1
                    self.scroll_acc -= n * cfg.scroll_step
                    actions.append(Action("scroll", n=-n if cfg.scroll_invert else n))
            self.scroll_y = y
            return actions
        self.scroll_y = None
        self.scroll_acc = 0.0

        # right click: thumb + middle pinch
        rp = (p.pinch_tm < (cfg.pinch_up if self.rp_down else cfg.pinch_down)
              and p.pinch_tm < p.pinch_ti and p.mid > FOLDED)
        if rp and not self.rp_down and not self.pinch_down:
            actions.append(Action("right_click"))
            self._flash_label("RIGHT CLICK", now)
        self.rp_down = rp

        # left pinch: click / double click / drag
        lp = p.pinch_ti < (cfg.pinch_up if self.pinch_down else cfg.pinch_down)
        if lp and not self.pinch_down and (p.pinch_ti > p.pinch_tm and p.mid > FOLDED):
            lp = False
        if lp and not self.pinch_down and p.idx < FOLDED:
            lp = False
        if lp and not self.pinch_down and not self.rp_down:
            self.pinch_down = True
            self.pinch_t0 = now
        elif lp and self.pinch_down:
            if not self.dragging and now - self.pinch_t0 >= cfg.drag_hold:
                self.dragging = True
                actions.append(Action("drag_start"))
        elif not lp and self.pinch_down:
            self.pinch_down = False
            if self.dragging:
                self.dragging = False
                actions.append(Action("drag_end"))
                self._flash_label("DROP", now)
            else:
                double = (now - self.last_click) <= cfg.double_click_window
                actions.append(Action("click", double=double))
                self.last_click = -1e9 if double else now
                self._flash_label("DOUBLE CLICK" if double else "CLICK", now)

        # Phase 8: Gesture Shortcuts (Vertical Slide & Horizontal Swipe)
        flick_actions = []
        self._check_gesture_shortcuts(lm, p, now, flick_actions)

        # cursor: follow anchor
        closing = p.pinch_ti < cfg.pinch_freeze and not self.dragging
        self.last_closing = closing
        pointing = (p.idx > FOLDED) or self.dragging

        anchor = self._compute_anchor(lm)
        m = cfg.box_margin
        in_box = True if m <= 0.001 else ((m <= anchor[0] <= 1.0 - m) and (m <= anchor[1] <= 1.0 - m))
        clutch_enabled = getattr(cfg, "clutch_enabled", True)

        if pointing and not (closing or self.rp_down):
            if clutch_enabled and not in_box and not self.dragging:
                self.clutched = True
                self.last_anchor = None
                self.last_anchor_t = None
                self.precision_active = False
            else:
                self.clutched = False
                ax, ay = self._map(*anchor)
                if self.last_anchor is not None and self.last_anchor_t is not None:
                    raw_dt = now - self.last_anchor_t
                    if raw_dt > 0.5:
                        # genuine stall (sleep/resume, camera hiccup): drop this one delta only
                        adx, ady, speed = 0.0, 0.0, 0.0
                    else:
                        dt = max(raw_dt, 1e-3)
                        adx = anchor[0] - self.last_anchor[0]
                        ady = anchor[1] - self.last_anchor[1]
                        speed = math.hypot(adx, ady) / dt
                else:
                    adx, ady, speed = 0.0, 0.0, 0.0
                self.last_anchor = anchor
                self.last_anchor_t = now

                is_precision = speed < getattr(cfg, "precision_threshold", 0.015)
                self.precision_active = is_precision
                actions.append(Action("move", x=ax, y=ay, dx=adx, dy=ady, speed=speed, precision=is_precision))
        else:
            self.clutched = False
            self.last_anchor = None
            self.last_anchor_t = None
            self.precision_active = False

        actions.extend(flick_actions)
        return actions

    # ---------- internals ----------
    def _compute_anchor(self, lm):
        mode = getattr(self.cfg, "anchor_point", "blend")
        if mode == "tip":
            return lm[INDEX_TIP]
        elif mode == "pip":
            if len(lm) > INDEX_PIP and lm[INDEX_PIP] != (0.5, 0.6):
                return lm[INDEX_PIP]
            return ((lm[INDEX_MCP][0] + lm[INDEX_TIP][0]) / 2.0, (lm[INDEX_MCP][1] + lm[INDEX_TIP][1]) / 2.0)
        elif mode == "mcp":
            return lm[INDEX_MCP]
        elif mode == "blend":
            w = getattr(self.cfg, "anchor_blend_weight", 0.65)
            if len(lm) > INDEX_PIP and lm[INDEX_PIP] != (0.5, 0.6):
                pip = lm[INDEX_PIP]
            else:
                pip = ((lm[INDEX_MCP][0] + lm[INDEX_TIP][0]) / 2.0, (lm[INDEX_MCP][1] + lm[INDEX_TIP][1]) / 2.0)
            tip = lm[INDEX_TIP]
            ax = (1.0 - w) * pip[0] + w * tip[0]
            ay = (1.0 - w) * pip[1] + w * tip[1]
            return (ax, ay)
        elif mode == "palm_offset":
            palm_x = (lm[WRIST][0] + lm[MIDDLE_MCP][0]) / 2.0
            palm_y = (lm[WRIST][1] + lm[MIDDLE_MCP][1]) / 2.0
            off_x = lm[INDEX_TIP][0] - lm[INDEX_MCP][0]
            off_y = lm[INDEX_TIP][1] - lm[INDEX_MCP][1]
            return (palm_x + off_x * 0.75, palm_y + off_y * 0.75)
        return lm[INDEX_TIP]

    def _reset_state(self):
        self.palm_since = None
        self.palm_latched = False
        self.palm_last_seen = 0.0
        self.thumbs_up_since = None
        self.last_pose = None
        self.last_closing = False
        self.last_openness = 0.0
        self.pinch_down = False
        self.pinch_t0 = 0.0
        self.dragging = False
        self.rp_down = False
        self.last_click = -1e9
        self.two_since = None
        self.zoom_prev = None
        self.zoom_acc = 0.0
        self.scroll_y = None
        self.scroll_acc = 0.0
        self.lost_since = None
        self.last_anchor = None
        self.last_anchor_t = None
        self.precision_active = False
        self.clutched = False

        # Palm & Fist state
        self.confirmed_pose = "OPEN"
        self.pending_pose = None
        self.pending_pose_count = 0
        self.fist_start_t = 0.0
        self.fist_active = False
        self.reopen_freeze_until = 0.0
        self.zp_down = False
        self.zp_since = 0.0
        self.zoom1_on = False
        self.zp_y = None
        self.zp_acc = 0.0

        # Shortcut state
        self._palm_hist = []
        self._shortcut_cooldown_until = 0.0

    def _clear_motion(self):
        self.two_since = None
        self.zoom_prev = None
        self.zoom_acc = 0.0
        self.scroll_y = None
        self.scroll_acc = 0.0
        self.last_anchor = None
        self.last_anchor_t = None
        self.precision_active = False
        self.clutched = False
        self._palm_hist = []

    def _end_zoom_pinch(self, now):
        """Zoom pinch released (or aborted): hold the cursor still through the release twitch."""
        self.zp_down = False
        self.zoom1_on = False
        self.zp_y = None
        self.zp_acc = 0.0
        self.zoom_acc = 0.0
        self.last_anchor = None
        self.last_anchor_t = None
        self.reopen_freeze_until = now + getattr(self.cfg, "reopen_freeze_ms", 150.0) / 1000.0

    def _end_pinch(self, actions):
        if self.dragging:
            actions.append(Action("drag_end"))
        self.pinch_down = False
        self.dragging = False
        self.rp_down = False
        self.fist_active = False
        self.zp_down = False
        self.zoom1_on = False
        self.zp_y = None
        self.zp_acc = 0.0

    def _set_active(self, want, actions):
        self._end_pinch(actions)
        self._clear_motion()
        self.active = want
        if not want:
            self.palm_latched = False
            self.palm_since = None
            self.progress = 0.0
        actions.append(Action("activate" if want else "deactivate"))

    def _flash_label(self, text, now):
        self._flash = text
        self._flash_until = now + 0.7

    def _map(self, x, y):
        m = self.cfg.box_margin
        s = 1.0 - 2.0 * m
        if s <= 0.001:
            return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        return min(max((x - m) / s, 0.0), 1.0), min(max((y - m) / s, 0.0), 1.0)
