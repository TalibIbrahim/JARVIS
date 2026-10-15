"""
Dwell Click engine (Kinect-style hover to click).
Pure logic module so it can be thoroughly unit-tested without GUI or hardware.

Features:
- Dwells when cursor stays inside dwell_radius (default ~14 px) for dwell_time (default ~1.0 s).
- Latches upon click to prevent repeated accidental triggers until cursor leaves radius or hand leaves.
- Pauses while pinching, dragging, hand lost, or standby.
- Supports modes: OFF, CLICK (left click), RADIAL (opens radial mouse action menu).
"""
import math
import time


class DwellClicker:
    RADIAL_SECTORS = [
        {"id": "click", "label": "Click", "icon": ""},
        {"id": "double_click", "label": "Double Click", "icon": ""},
        {"id": "right_click", "label": "Right Click", "icon": ""},
        {"id": "drag", "label": "Drag / Drop", "icon": ""},
        {"id": "scroll", "label": "Scroll Mode", "icon": ""},
    ]

    def __init__(self, cfg):
        self.cfg = cfg
        self.anchor_x = None
        self.anchor_y = None
        self.dwell_start_t = None
        self.latched = False
        self.flash_until = 0.0
        self.radial_open = False
        self.radial_hover_id = None
        self.radial_hover_since = None

    def reset(self):
        self.anchor_x = None
        self.anchor_y = None
        self.dwell_start_t = None
        self.latched = False
        self.radial_open = False
        self.radial_hover_id = None
        self.radial_hover_since = None

    def update(self, cur_x, cur_y, now, active=True, hand_present=True, pinching=False, dragging=False):
        """
        Evaluates dwell state on each cursor tick.
        Returns a dictionary with visual state and any action to trigger.
        """
        mode = getattr(self.cfg, "dwell_mode", "click")
        dwell_time = max(getattr(self.cfg, "dwell_time", 1.0), 0.2)
        dwell_radius = max(getattr(self.cfg, "dwell_radius", 14.0), 4.0)

        res = {
            "progress": 0.0,
            "flashing": now < self.flash_until,
            "latched": self.latched,
            "radial_open": self.radial_open,
            "radial_hover": self.radial_hover_id,
            "radial_progress": 0.0,
            "action": None,
            "x": cur_x,
            "y": cur_y
        }

        # Dwell must pause while in standby, no hand, pinching, or dragging
        if mode == "off" or not active or not hand_present or pinching or dragging:
            self.reset()
            return res

        if self.anchor_x is None:
            self.anchor_x = cur_x
            self.anchor_y = cur_y
            self.dwell_start_t = now
            return res

        dist = math.hypot(cur_x - self.anchor_x, cur_y - self.anchor_y)

        # If radial menu is open, handle selection via direction/distance
        if self.radial_open:
            if dist < 12.0:
                # Still in center hub
                self.radial_hover_id = None
                self.radial_hover_since = None
                return res
            elif dist > 110.0:
                # Cancel radial menu if cursor moves far away
                self.radial_open = False
                self.reset()
                return res
            else:
                # Calculate angle in radians: 0 is Up (-Y)
                angle = math.atan2(cur_x - self.anchor_x, -(cur_y - self.anchor_y))
                if angle < 0:
                    angle += 2 * math.pi
                n_sectors = len(self.RADIAL_SECTORS)
                sector_arc = 2 * math.pi / n_sectors
                sector_idx = int((angle + sector_arc / 2.0) % (2 * math.pi) / sector_arc)
                selected_item = self.RADIAL_SECTORS[sector_idx]["id"]

                if self.radial_hover_id != selected_item:
                    self.radial_hover_id = selected_item
                    self.radial_hover_since = now

                hover_time = now - self.radial_hover_since
                select_time = 0.45  # 450 ms hover over a slice selects it
                r_prog = min(hover_time / select_time, 1.0)
                res["radial_hover"] = selected_item
                res["radial_progress"] = r_prog

                if r_prog >= 1.0:
                    res["action"] = selected_item
                    self.flash_until = now + 0.35
                    self.radial_open = False
                    self.latched = True
                    self.anchor_x = cur_x
                    self.anchor_y = cur_y
                    self.dwell_start_t = now
                return res

        # Normal dwell tracking
        if dist > dwell_radius:
            # Cursor moved outside dwell radius: reset dwell anchor and unlatch
            self.anchor_x = cur_x
            self.anchor_y = cur_y
            self.dwell_start_t = now
            self.latched = False
            return res

        # Inside radius: accumulate dwell time
        if self.latched:
            # Already triggered at this location; must leave radius before next trigger
            return res

        elapsed = now - self.dwell_start_t
        progress = min(elapsed / dwell_time, 1.0)
        res["progress"] = progress

        if progress >= 1.0:
            if mode == "click":
                res["action"] = "click"
                self.flash_until = now + 0.35
                self.latched = True
                res["latched"] = True
            elif mode == "radial":
                self.radial_open = True
                self.radial_hover_id = None
                self.radial_hover_since = None
                res["radial_open"] = True

        return res
