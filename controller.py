"""
Turns gesture Actions into high-precision, decoupled mouse / keyboard input.
Features:
- Decoupled 120+ Hz high-refresh cursor positioning thread with sub-pixel interpolation.
- Trackpad-style relative mode with pointer-acceleration curve & precision fine-aim mode.
- Absolute mapping mode with One Euro & dead-zone filtering.
- Position lookback buffer (150-250 ms) to completely eliminate click drift during pinch.
- Integrated Kinect-style dwell click and radial action menu.
- Failsafe mouse button and modifier key release on all exit/error paths.
"""
import atexit
import collections
import ctypes
import math
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pyautogui

from filters import OneEuro, DeadzoneFilter, OneEuroPoint
from dwell import DwellClicker

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False      # hand control would otherwise trip the corner failsafe
pyautogui.MINIMUM_DURATION = 0


class LookbackBuffer:
    """Preallocated circular buffer for zero-allocation cursor position lookback."""
    __slots__ = ("_cap", "_t", "_x", "_y", "_head", "_count")

    def __init__(self, capacity: int = 400):
        self._cap = capacity
        self._t = [0.0] * capacity
        self._x = [0.0] * capacity
        self._y = [0.0] * capacity
        self._head = 0
        self._count = 0

    def append_fast(self, t: float, x: float, y: float) -> None:
        idx = self._head
        self._t[idx] = t
        self._x[idx] = x
        self._y[idx] = y
        self._head = (idx + 1) % self._cap
        if self._count < self._cap:
            self._count += 1

    def clear(self) -> None:
        self._head = 0
        self._count = 0

    def append(self, item_or_t, x=None, y=None) -> None:
        if x is None:
            t, x, y = item_or_t
        else:
            t = item_or_t
        self.append_fast(float(t), float(x), float(y))

    def get_lookback(self, target_t: float, default_x: float, default_y: float):
        count = self._count
        if count == 0:
            return default_x, default_y
        head = self._head
        cap = self._cap
        best_x = default_x
        best_y = default_y
        for i in range(count):
            idx = (head - 1 - i) % cap
            t = self._t[idx]
            if t <= target_t:
                return self._x[idx], self._y[idx]
            best_x = self._x[idx]
            best_y = self._y[idx]
        return best_x, best_y

    def __len__(self) -> int:
        return self._count

    def __iter__(self):
        head = self._head
        count = self._count
        cap = self._cap
        start = (head - count) % cap
        for i in range(count):
            idx = (start + i) % cap
            yield (self._t[idx], self._x[idx], self._y[idx])

    def __reversed__(self):
        head = self._head
        count = self._count
        cap = self._cap
        for i in range(count):
            idx = (head - 1 - i) % cap
            yield (self._t[idx], self._x[idx], self._y[idx])


class Controller:
    def __init__(self, cfg, overlay=None):
        self.cfg = cfg
        self.overlay = overlay
        self._win = sys.platform == "win32"
        if self._win:
            self._user32 = ctypes.windll.user32
            # Per-monitor / DPI aware system metrics
            self.vx = self._user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            self.vy = self._user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
            self.vw = self._user32.GetSystemMetrics(78) or pyautogui.size()[0]
            self.vh = self._user32.GetSystemMetrics(79) or pyautogui.size()[1]
            self.sw = self._user32.GetSystemMetrics(0) or pyautogui.size()[0]
            self.sh = self._user32.GetSystemMetrics(1) or pyautogui.size()[1]
        else:
            self.sw, self.sh = pyautogui.size()
            self.vx, self.vy, self.vw, self.vh = 0, 0, self.sw, self.sh

        # Smoothing & Deadzone filters
        self.fx = OneEuro(cfg.smooth_min_cutoff, cfg.smooth_beta, getattr(cfg, "smooth_d_cutoff", 1.0))
        self.fy = OneEuro(cfg.smooth_min_cutoff, cfg.smooth_beta, getattr(cfg, "smooth_d_cutoff", 1.0))
        self.deadzone = DeadzoneFilter(getattr(cfg, "deadzone_px", 1.5))
        self.landmark_filter = OneEuroPoint(cfg.smooth_min_cutoff, cfg.smooth_beta)

        try:
            cur_pos = pyautogui.position()
            self.cur_x = float(cur_pos[0])
            self.cur_y = float(cur_pos[1])
        except Exception:
            self.cur_x = 0.0
            self.cur_y = 0.0
        self.target_x = self.cur_x
        self.target_y = self.cur_y
        self.history = LookbackBuffer(400)  # Preallocated zero-allocation buffer
        self.history.append_fast(time.perf_counter(), self.cur_x, self.cur_y)
        self._last_dwell_active = False

        # Relative tracking state
        self.last_landmark_x = None
        self.last_landmark_y = None
        self.last_move_t = None

        # State flags
        self.button_down = False
        self.ctrl_down = False
        self.scroll_mode_active = False
        self.active = False
        self.hand_present = False
        self.pinching = False
        self.dragging = False

        # Dwell clicker engine
        self.dwell = DwellClicker(cfg)
        self.island_buttons = {}

        # Decoupled 120+ Hz cursor update thread
        self._stop_thread = threading.Event()
        self._thread = threading.Thread(target=self._cursor_loop, daemon=True)
        self._thread.start()
        try:
            atexit.register(self.failsafe_release)
        except Exception:
            pass

    def set_overlay(self, overlay):
        self.overlay = overlay

    def _move(self, x, y):
        if self._win:
            self._user32.SetCursorPos(int(x), int(y))          # fast native cursor positioning
        else:
            pyautogui.moveTo(int(x), int(y), _pause=False)

    def _reset_filters(self):
        self.fx.reset()
        self.fy.reset()
        self.deadzone.reset()
        self.landmark_filter.reset()
        self.last_landmark_x = None
        self.last_landmark_y = None
        self.last_move_t = None
        self.dwell.reset()

    def _release_ctrl(self):
        try:
            pyautogui.keyUp("ctrl", _pause=False)
        except Exception:
            pass
        self.ctrl_down = False

    def failsafe_release(self):
        """Unconditional failsafe release of mouse buttons and modifier keys."""
        try:
            pyautogui.mouseUp(_pause=False)
        except Exception:
            pass
        self.button_down = False
        self.dragging = False
        self.pinching = False
        self.scroll_mode_active = False

        try:
            pyautogui.keyUp("ctrl", _pause=False)
        except Exception:
            pass
        self.ctrl_down = False

    def on_sleep(self):
        """Unconditional failsafe release and filter reset on system sleep or screen lock."""
        self.failsafe_release()
        self._reset_filters()
        self.target_x = self.cur_x
        self.target_y = self.cur_y

    def on_resume(self):
        """Resets move baseline and releases any latched input on resume."""
        self.failsafe_release()
        self._reset_filters()
        self.last_move_t = None

    def __del__(self):
        try:
            self.failsafe_release()
        except Exception:
            pass

    def _cursor_loop(self):
        """Dedicated high-frequency loop (120+ Hz) for smooth, decoupled cursor interpolation."""
        rate = max(float(getattr(self.cfg, "cursor_rate_hz", 120)), 60.0)
        dt_target = 1.0 / (rate + 1.0)
        alpha = 1.0 - math.exp(-38.0 * dt_target)

        if self._win:
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:
                pass
            try:
                k32 = ctypes.windll.kernel32
                k32.GetCurrentThread.restype = ctypes.c_void_p
                k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
                k32.SetThreadPriority.restype = ctypes.c_bool
                # THREAD_PRIORITY_HIGHEST = 2
                k32.SetThreadPriority(k32.GetCurrentThread(), 2)
            except Exception:
                pass

        next_tick = time.perf_counter() + dt_target
        try:
            while not self._stop_thread.is_set():
                # Smoothly interpolate towards target position
                # Critically damped exponential approach
                dx = (self.target_x - self.cur_x) * alpha
                dy = (self.target_y - self.cur_y) * alpha

                if abs(dx) > 0.02 or abs(dy) > 0.02:
                    self.cur_x += dx
                    self.cur_y += dy
                    self._move(round(self.cur_x), round(self.cur_y))

                now = time.perf_counter()
                self.history.append_fast(now, self.cur_x, self.cur_y)

                # Update dwell click state
                dwell_state = self.dwell.update(
                    self.cur_x, self.cur_y, now,
                    active=self.active,
                    hand_present=self.hand_present,
                    pinching=self.pinching,
                    dragging=self.dragging
                )

                # Handle dwell actions
                if dwell_state.get("action"):
                    self._handle_dwell_action(dwell_state["action"])

                # Post dwell feedback to overlay only when active or transitioning
                is_dwell_active = bool(dwell_state.get("progress", 0.0) > 0.0 or
                                       dwell_state.get("radial_open") or
                                       dwell_state.get("flashing") or
                                       dwell_state.get("latched"))
                if is_dwell_active or self._last_dwell_active:
                    self._last_dwell_active = is_dwell_active
                    if self.overlay:
                        self.overlay.post("cursor_dwell", **dwell_state)

                # Hybrid sleep/spin to lock cursor loop frequency at 120+ Hz with < 0.3 ms jitter
                rem = next_tick - time.perf_counter()
                if rem > 0.0015:
                    time.sleep(rem - 0.001)
                while time.perf_counter() < next_tick and not self._stop_thread.is_set():
                    pass

                next_tick += dt_target
                now_after = time.perf_counter()
                if now_after > next_tick:
                    # If behind due to system sleep/resume or lock screen stall
                    if (now_after - next_tick) > 0.20:
                        self.target_x = self.cur_x
                        self.target_y = self.cur_y
                        self.failsafe_release()
                    next_tick = now_after + dt_target
        finally:
            if self._win:
                try:
                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    pass

    def _handle_dwell_action(self, act):
        if act == "click":
            btn = self.get_island_button_at(self.cur_x, self.cur_y)
            if btn:
                self.trigger_island_button(btn)
            pyautogui.click(_pause=False)
        elif act == "double_click":
            pyautogui.doubleClick(_pause=False)
        elif act == "right_click":
            pyautogui.rightClick(_pause=False)
        elif act == "drag":
            if self.button_down:
                pyautogui.mouseUp(_pause=False)
                self.button_down = False
            else:
                pyautogui.mouseDown(_pause=False)
                self.button_down = True
        elif act == "scroll":
            self.scroll_mode_active = not self.scroll_mode_active

    def get_island_button_at(self, x: float, y: float) -> Optional[str]:
        """
        Hit-tests interactive island buttons at screen coordinate (x, y):
        Supported buttons: 'prev', 'play_pause', 'next', page dots ('dot_0'..'dot_4'), 'pause_tracking_toggle'.
        """
        # 1. Custom registered button rects: name -> (x1, y1, x2, y2)
        if hasattr(self, "island_buttons") and isinstance(self.island_buttons, dict):
            for name, rect in self.island_buttons.items():
                if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                    return name

        # 2. Overlay / Island UI Hit Test
        if self.overlay and hasattr(self.overlay, "island"):
            island = self.overlay.island
            if hasattr(island, "get_island_button_at"):
                try:
                    res = island.get_island_button_at(x, y)
                    if res:
                        return res
                except Exception:
                    pass

            # Map screen (x, y) into local island coordinates
            try:
                geom = getattr(island, "geometry", None)
                if callable(geom):
                    g = geom()
                    gx, gy = float(g.x()), float(g.y())
                    gw, gh = float(g.width()), float(g.height())
                else:
                    gx, gy = float(getattr(island, "x", 0)), float(getattr(island, "y", 0))
                    gw, gh = float(getattr(island, "width", self.sw)), float(getattr(island, "height", 220))

                if gx <= x <= (gx + gw) and gy <= y <= (gy + gh):
                    local_x = x - gx
                    local_y = y - gy
                    from collections import namedtuple
                    Pt = namedtuple("Pt", ["x", "y"])
                    pt = Pt(local_x, local_y)

                    if hasattr(island, "_button_at"):
                        btn = island._button_at(pt)
                        if btn:
                            return btn

                    if hasattr(island, "_dot_at"):
                        dot = island._dot_at(pt)
                        if dot is not None:
                            return f"dot_{dot}"
            except Exception:
                pass

        # 3. Default Geometry Fallback for Top-Screen Expanded Island
        # Centered horizontally at top of primary screen (y: 0 to 220)
        if 0 <= y <= 220:
            cx = self.sw / 2.0
            scale = 1.0
            cy_btn = 136.0 * scale
            # Prev, Play/Pause, Next
            if abs(y - cy_btn) <= 28.0 * scale:
                if abs(x - (cx - 52.0 * scale)) <= 24.0 * scale:
                    return "prev"
                if abs(x - cx) <= 26.0 * scale:
                    return "play_pause"
                if abs(x - (cx + 52.0 * scale)) <= 24.0 * scale:
                    return "next"

            # Page dots column (right edge of card: x ~ cx + 179)
            dot_x = cx + 179.0 * scale
            if abs(x - dot_x) <= 16.0 * scale and 50.0 * scale <= y <= 160.0 * scale:
                dot_idx = min(4, max(0, int((y - 50.0 * scale) / (14.0 * scale))))
                return f"dot_{dot_idx}"

            # Pause tracking toggle: top pill header area
            if y <= 40.0 * scale and abs(x - (cx + 65.0 * scale)) <= 35.0 * scale:
                return "pause_tracking_toggle"

        return None

    def trigger_island_button(self, btn: str) -> bool:
        """
        Triggers an interactive island button action.
        Buttons: 'prev', 'play_pause', 'next', page dots ('dot_0'..'dot_4'), 'pause_tracking_toggle'.
        """
        if not btn:
            return False

        # Post event to overlay
        if self.overlay and hasattr(self.overlay, "post"):
            try:
                self.overlay.post("island_button_click", button=btn)
            except Exception:
                pass

        # Direct execution on island or media adapter
        if btn in ("prev", "previous"):
            if self.overlay and hasattr(self.overlay, "island") and hasattr(self.overlay.island, "media_prev"):
                try:
                    self.overlay.island.media_prev()
                except Exception:
                    pass
            else:
                try:
                    from media_adapter import get_media_adapter
                    get_media_adapter().previous_track()
                except Exception:
                    pass
            return True

        elif btn in ("play_pause", "play/pause"):
            if self.overlay and hasattr(self.overlay, "island") and hasattr(self.overlay.island, "media_toggle_play"):
                try:
                    self.overlay.island.media_toggle_play()
                except Exception:
                    pass
            else:
                try:
                    from media_adapter import get_media_adapter
                    get_media_adapter().play_pause()
                except Exception:
                    pass
            return True

        elif btn in ("next",):
            if self.overlay and hasattr(self.overlay, "island") and hasattr(self.overlay.island, "media_next"):
                try:
                    self.overlay.island.media_next()
                except Exception:
                    pass
            else:
                try:
                    from media_adapter import get_media_adapter
                    get_media_adapter().next_track()
                except Exception:
                    pass
            return True

        elif btn.startswith("dot_") or btn in ("page_dots", "page_dot"):
            dot_idx = 0
            if btn.startswith("dot_"):
                try:
                    dot_idx = int(btn.split("_")[1])
                except Exception:
                    dot_idx = 0
            if self.overlay and hasattr(self.overlay, "island"):
                island = self.overlay.island
                if hasattr(island, "expand_card") and hasattr(island, "card_pages"):
                    pages = island.card_pages
                    if 0 <= dot_idx < len(pages):
                        island.expand_card(pages[dot_idx])
            return True

        elif btn in ("pause_tracking_toggle", "tracking_toggle", "pause tracking toggle"):
            if hasattr(self, "engine") and hasattr(self.engine, "request_toggle"):
                self.engine.request_toggle()
            else:
                self.active = not self.active
            if self.overlay and hasattr(self.overlay, "post"):
                try:
                    self.overlay.post("toggle_tracking", active=self.active)
                except Exception:
                    pass
            return True

        return False

    def island_page_step(self, direction: int = 1):
        """Triggers island page step (1 = next, -1 = prev) via overlay."""
        if self.overlay and hasattr(self.overlay, "post"):
            try:
                self.overlay.post("island_page_step", direction=direction)
            except Exception:
                pass
        if self.overlay and hasattr(self.overlay, "island"):
            island = self.overlay.island
            if hasattr(island, "expand_card") and hasattr(island, "card_pages"):
                try:
                    curr = getattr(island, "expanded_card", "weather") or "weather"
                    pages = getattr(island, "card_pages", ["media", "time", "weather", "hardware", "jarvis"])
                    idx = pages.index(curr) if curr in pages else 0
                    island.expand_card(pages[(idx + direction) % len(pages)])
                except Exception:
                    pass

    def _get_lookback_position(self, lookback_ms):
        """Returns the cursor position from lookback_ms before now."""
        target_t = time.perf_counter() - (lookback_ms / 1000.0)
        return self.history.get_lookback(target_t, self.cur_x, self.cur_y)

    def handle(self, a):
        k = a.kind
        now = time.perf_counter()

        if k != "zoom" and self.ctrl_down:
            self._release_ctrl()

        # Multi-monitor bounds
        if getattr(self.cfg, "multi_monitor", False) and self.vw > self.sw:
            min_x, min_y = self.vx, self.vy
            max_x, max_y = self.vx + self.vw - 1, self.vy + self.vh - 1
            span_w, span_h = self.vw, self.vh
        else:
            min_x, min_y = 0, 0
            max_x, max_y = self.sw - 1, self.sh - 1
            span_w, span_h = self.sw, self.sh

        if k == "move":
            self.hand_present = True
            tracking_mode = getattr(self.cfg, "tracking_mode", "relative")

            if tracking_mode == "relative":
                # Trackpad-style relative mapping
                flm_x, flm_y = self.landmark_filter(a.x, a.y, now)

                if self.last_landmark_x is None or self.last_move_t is None:
                    self.last_landmark_x = flm_x
                    self.last_landmark_y = flm_y
                    self.last_move_t = now
                    return

                raw_dt = now - self.last_move_t
                dt = max(min(raw_dt, 0.05), 1e-4)

                # Gap detected from sleep/resume, screen lock, or frame drop
                if raw_dt > 0.10:
                    self.failsafe_release()
                    self._reset_filters()
                    self.last_landmark_x = flm_x
                    self.last_landmark_y = flm_y
                    self.last_move_t = now
                    return

                # Normalized landmark displacement
                d_x = flm_x - self.last_landmark_x
                d_y = flm_y - self.last_landmark_y
                self.last_landmark_x = flm_x
                self.last_landmark_y = flm_y
                self.last_move_t = now

                # Pixel travel on target screen
                px_dx = d_x * span_w
                px_dy = d_y * span_h
                step_dist = math.hypot(px_dx, px_dy)
                velocity = step_dist / dt

                # Deadzone suppression below threshold
                deadzone = getattr(self.cfg, "deadzone_px", 1.5)
                if step_dist < (deadzone * 0.75):
                    px_dx, px_dy = 0.0, 0.0
                else:
                    speed = getattr(self.cfg, "pointer_speed", 1.15)
                    prec_thresh = getattr(self.cfg, "precision_threshold", 0.015) * span_w
                    accel_thresh = getattr(self.cfg, "accel_threshold", 0.035) * span_w
                    accel_gain = getattr(self.cfg, "accel_gain", 1.8)
                    prec_gain = getattr(self.cfg, "precision_gain", 0.35)

                    if a.precision or velocity < prec_thresh:
                        # Fine-aim precision mode
                        gain = speed * prec_gain
                    elif velocity > accel_thresh:
                        # Pointer acceleration
                        excess = (velocity - accel_thresh) / (span_w * 0.1)
                        gain = speed * (1.0 + accel_gain * min(excess, 2.5))
                    else:
                        gain = speed

                    px_dx *= gain
                    px_dy *= gain

                # In scroll mode, vertical motion scrolls instead of moves
                if self.scroll_mode_active and abs(px_dy) > 4.0:
                    notch = 1 if px_dy < 0 else -1
                    pyautogui.scroll(120 * notch, _pause=False)
                    return

                self.target_x = min(max(self.target_x + px_dx, min_x), max_x)
                self.target_y = min(max(self.target_y + px_dy, min_y), max_y)

            else:
                # Absolute mapping
                raw_x = min_x + a.x * (span_w - 1)
                raw_y = min_y + a.y * (span_h - 1)

                fx = min(max(self.fx(raw_x, now), min_x), max_x)
                fy = min(max(self.fy(raw_y, now), min_y), max_y)
                out_x, out_y = self.deadzone(fx, fy)
                self.target_x = out_x
                self.target_y = out_y

        elif k == "click":
            # Eliminate click drift with position lookback
            lookback_ms = getattr(self.cfg, "cursor_lookback_ms", 200.0)
            lb_x, lb_y = self._get_lookback_position(lookback_ms)

            # Snap cursor to exact pre-click lookback position (0 px pre-click drift invariant)
            self.target_x, self.target_y = lb_x, lb_y
            self.cur_x, self.cur_y = lb_x, lb_y
            self._move(round(lb_x), round(lb_y))

            # Fist-click gesture over interactive island buttons triggers immediate click
            btn = self.get_island_button_at(lb_x, lb_y)
            if btn:
                self.trigger_island_button(btn)

            if a.double:
                pyautogui.doubleClick(_pause=False)
            else:
                pyautogui.click(_pause=False)

            self.pinching = False

        elif k == "island_page_step":
            self.island_page_step(a.n)

        elif k == "right_click":
            pyautogui.rightClick(_pause=False)
            self.pinching = False

        elif k == "drag_start":
            self.pinching = True
            self.dragging = True
            pyautogui.mouseDown(_pause=False)
            self.button_down = True

        elif k == "drag_end":
            self.pinching = False
            self.dragging = False
            pyautogui.mouseUp(_pause=False)
            self.button_down = False

        elif k == "scroll":
            pyautogui.scroll(120 * a.n, _pause=False)

        elif k == "zoom":
            if not self.ctrl_down:
                try:
                    pyautogui.keyDown("ctrl", _pause=False)
                    self.ctrl_down = True
                except Exception:
                    pass
            pyautogui.scroll(120 * a.n, _pause=False)

        elif k == "activate":
            self.active = True
            self.hand_present = True
            self._reset_filters()

        elif k in ("deactivate", "lost", "sleep"):
            self.active = (k != "deactivate" and self.active)
            self.hand_present = False
            self.failsafe_release()
            self._reset_filters()

    def shutdown(self):
        self._stop_thread.set()
        self.failsafe_release()
