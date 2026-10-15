"""
Gesture recognition. Pure Python (no camera, no OS calls) so it can be tested with fake landmarks.

Input : list of Hand (21 landmarks each, x already mirrored, normalised 0..1) + a timestamp.
Output: list of Action for the controller / overlay.

Gesture map
  open palm (hold)            wake up (0.8s) / standby (1.5s)
  index finger                move cursor
  thumb + index pinch, quick  click      (two quick pinches = double click)
  thumb + index pinch, hold   drag
  thumb + middle pinch        right click
  peace sign, move up/down    scroll
  both hands, spread/close    zoom in / zoom out  (Ctrl + wheel)
"""
import math
from dataclasses import dataclass

WRIST = 0
THUMB_TIP = 4
INDEX_MCP, INDEX_TIP = 5, 8
MIDDLE_MCP, MIDDLE_TIP = 9, 12
RING_MCP, RING_TIP = 13, 16
PINKY_MCP, PINKY_TIP = 17, 20

EXTENDED = 1.55   # wrist->tip / wrist->knuckle ratio above this = finger straight
CURLED = 1.40     # below this = finger folded
FOLDED = 1.30     # below this the finger is tucked into the palm (can't be part of a pinch)


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class Hand:
    lm: list            # 21 (x, y) tuples
    label: str = ""


@dataclass
class Action:
    kind: str           # move click right_click drag_start drag_end scroll zoom activate deactivate lost
    x: float = 0.0
    y: float = 0.0
    n: int = 0
    double: bool = False


class Pose:
    """Finger states and pinch distances for one hand."""

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

    @property
    def palm(self):
        return (min(self.idx, self.mid, self.ring, self.pinky) > EXTENDED
                and self.thumb_out > 0.8 and self.pinch_ti > 0.6)

    @property
    def peace(self):
        return (self.idx > EXTENDED and self.mid > EXTENDED
                and self.ring < CURLED and self.pinky < CURLED and self.pinch_ti > 0.6)


class GestureEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.active = False
        self.nhands = 0
        self.progress = 0.0
        self._req = None
        self._flash = ""
        self._flash_until = 0.0
        self._mode = ""
        self._lost_sent = False
        self._reset_state()

    # ---------- public ----------
    def request(self, want):
        """Thread-safe: voice / hotkey ask for wake (True) or standby (False)."""
        self._req = bool(want)

    def request_toggle(self):
        """Thread-safe toggle of active state."""
        self._req = "toggle"

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
        res = {"active": self.active, "label": label, "progress": self.progress, "hands": self.nhands}
        if hasattr(self, "last_pose") and self.last_pose is not None:
            p = self.last_pose
            res["debug"] = {
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
            self.last_pose = None
            self.last_closing = False
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

        # palm hold toggles wake / standby
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
            palm_grace = getattr(cfg, "palm_grace", 0.15)
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

        # scroll: peace sign, move hand up/down
        if p.peace:
            self._mode = "SCROLL"
            y = (lm[INDEX_TIP][1] + lm[MIDDLE_TIP][1]) / 2.0
            if self.scroll_y is not None:
                self.scroll_acc += self.scroll_y - y          # hand up -> positive
                while abs(self.scroll_acc) >= cfg.scroll_step:
                    n = 1 if self.scroll_acc > 0 else -1
                    self.scroll_acc -= n * cfg.scroll_step
                    actions.append(Action("scroll", n=-n if cfg.scroll_invert else n))
            self.scroll_y = y
            return actions
        self.scroll_y = None
        self.scroll_acc = 0.0

        # right click: thumb + middle pinch
        # (the middle finger must be straight-ish, otherwise a tucked thumb on a curled finger would trigger it)
        rp = (p.pinch_tm < (cfg.pinch_up if self.rp_down else cfg.pinch_down)
              and p.pinch_tm < p.pinch_ti and p.mid > FOLDED)
        if rp and not self.rp_down and not self.pinch_down:
            actions.append(Action("right_click"))
            self._flash_label("RIGHT CLICK", now)
        self.rp_down = rp

        # left pinch: click / double click / drag
        lp = p.pinch_ti < (cfg.pinch_up if self.pinch_down else cfg.pinch_down)
        if lp and not self.pinch_down and (p.pinch_ti > p.pinch_tm and p.mid > FOLDED):
            lp = False                      # the middle finger is the closer one: that's a right click
        if lp and not self.pinch_down and p.idx < FOLDED:
            lp = False                      # index folded into a fist: not a pinch
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

        # cursor: follow the index tip, but hold still while the fingers are closing
        closing = p.pinch_ti < cfg.pinch_freeze and not self.dragging
        self.last_closing = closing
        pointing = (p.idx > FOLDED) or self.dragging
        if pointing and not (closing or self.rp_down):
            ax, ay = self._map(*lm[INDEX_TIP])
            actions.append(Action("move", x=ax, y=ay))
        return actions

    # ---------- internals ----------
    def _reset_state(self):
        self.palm_since = None
        self.palm_latched = False
        self.palm_last_seen = 0.0
        self.last_pose = None
        self.last_closing = False
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

    def _clear_motion(self):
        self.two_since = None
        self.zoom_prev = None
        self.zoom_acc = 0.0
        self.scroll_y = None
        self.scroll_acc = 0.0

    def _end_pinch(self, actions):
        if self.dragging:
            actions.append(Action("drag_end"))
        self.pinch_down = False
        self.dragging = False
        self.rp_down = False

    def _set_active(self, want, actions):
        self._end_pinch(actions)
        self._clear_motion()
        self.active = want
        actions.append(Action("activate" if want else "deactivate"))

    def _flash_label(self, text, now):
        self._flash = text
        self._flash_until = now + 0.7

    def _map(self, x, y):
        m = self.cfg.box_margin
        s = 1.0 - 2.0 * m
        return min(max((x - m) / s, 0.0), 1.0), min(max((y - m) / s, 0.0), 1.0)
