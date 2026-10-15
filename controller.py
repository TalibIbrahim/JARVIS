"""Turns gesture Actions into real mouse / keyboard input."""
import sys
import time
import ctypes

import pyautogui

from filters import OneEuro

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False      # hand control would otherwise trip the corner failsafe
pyautogui.MINIMUM_DURATION = 0


class Controller:
    def __init__(self, cfg):
        self.cfg = cfg
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

        self.fx = OneEuro(cfg.smooth_min_cutoff, cfg.smooth_beta)
        self.fy = OneEuro(cfg.smooth_min_cutoff, cfg.smooth_beta)
        self.button_down = False
        self.ctrl_down = False

    def _move(self, x, y):
        if self._win:
            self._user32.SetCursorPos(int(x), int(y))          # fast native cursor positioning
        else:
            pyautogui.moveTo(int(x), int(y), _pause=False)

    def _reset_filters(self):
        self.fx.reset()
        self.fy.reset()

    def _release_ctrl(self):
        if self.ctrl_down:
            try:
                pyautogui.keyUp("ctrl", _pause=False)
            except Exception:
                pass
            self.ctrl_down = False

    def handle(self, a):
        k = a.kind
        if k != "zoom" and self.ctrl_down:
            self._release_ctrl()

        if k == "move":
            t = time.perf_counter()
            # If multi-monitor is requested and virtual desktop is broader than primary screen
            if getattr(self.cfg, "multi_monitor", False) and self.vw > self.sw:
                raw_x = self.vx + a.x * (self.vw - 1)
                raw_y = self.vy + a.y * (self.vh - 1)
                max_x, max_y = self.vx + self.vw - 1, self.vy + self.vh - 1
                min_x, min_y = self.vx, self.vy
            else:
                raw_x = a.x * (self.sw - 1)
                raw_y = a.y * (self.sh - 1)
                max_x, max_y = self.sw - 1, self.sh - 1
                min_x, min_y = 0, 0

            fx = min(max(self.fx(raw_x, t), min_x), max_x)
            fy = min(max(self.fy(raw_y, t), min_y), max_y)
            self._move(round(fx), round(fy))
        elif k == "click":
            pyautogui.click(_pause=False)
        elif k == "right_click":
            pyautogui.rightClick(_pause=False)
        elif k == "drag_start":
            pyautogui.mouseDown(_pause=False)
            self.button_down = True
        elif k == "drag_end":
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
        elif k in ("lost", "activate", "deactivate"):
            if self.button_down:
                try:
                    pyautogui.mouseUp(_pause=False)
                except Exception:
                    pass
                self.button_down = False
            self._release_ctrl()
            self._reset_filters()

    def shutdown(self):
        try:
            pyautogui.mouseUp(_pause=False)
        except Exception:
            pass
        self.button_down = False
        self._release_ctrl()
