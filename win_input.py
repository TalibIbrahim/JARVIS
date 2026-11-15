"""Minimal Win32 SendInput wrapper: all synthetic mouse/keyboard input in J.A.R.V.I.S. goes through here.

Semantics match pyautogui with _pause=False (no failsafe, no duration):
- click / double_click / right_click act at the CURRENT cursor position (no move).
- mouse_down / mouse_up default to the left button.
- scroll(n) sends one MOUSEEVENTF_WHEEL event with dwData == n (callers pass 120 * notches).
- hotkey(*keys) presses keys in order and releases them in reverse order.
- write(text, interval) types each character as a Unicode key event, sleeping `interval` between them.
- screenshot(path) grabs the primary screen with Pillow's ImageGrab (what pyautogui/pyscreeze used).
Importing this module is cheap (ctypes only: Pillow is imported inside screenshot()).
"""
import ctypes
import sys
import time
from ctypes import wintypes

_WIN = sys.platform == "win32"

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

_ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "winleft": 0x5B, "winright": 0x5C,
    "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
}
for _i in range(1, 13):
    _VK["f%d" % _i] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    _VK[_c] = ord(_c.upper())

_EXTENDED = {0x5B, 0x5C, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0xAD, 0xAE, 0xAF,
             0xB0, 0xB1, 0xB2, 0xB3}

_user32 = None


def _u32():
    global _user32
    if _user32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.SetCursorPos.restype = wintypes.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.GetCursorPos.restype = wintypes.BOOL
        _user32 = u
    return _user32


def _send(inputs):
    """Sends a list of INPUT structs in one SendInput call. Returns the number of events injected."""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    return _u32().SendInput(n, arr, ctypes.sizeof(INPUT))


def _mouse_input(flags, data=0):
    i = INPUT()
    i.type = INPUT_MOUSE
    i.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    return i


def _key_input(vk, up):
    i = INPUT()
    i.type = INPUT_KEYBOARD
    flags = (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0) | (KEYEVENTF_KEYUP if up else 0)
    i.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return i


def _vk(key):
    try:
        return _VK[str(key).lower()]
    except KeyError:
        raise ValueError("win_input: unsupported key %r" % (key,)) from None


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _btn(button):
    try:
        return _BUTTONS[button]
    except KeyError:
        raise ValueError("win_input: unknown mouse button %r" % (button,)) from None


def set_cursor_pos(x, y):
    _u32().SetCursorPos(int(x), int(y))


move_to = set_cursor_pos


def position():
    pt = wintypes.POINT()
    _u32().GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def size():
    u = _u32()
    return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))


def mouse_down(button="left"):
    _send([_mouse_input(_btn(button)[0])])


def mouse_up(button="left"):
    _send([_mouse_input(_btn(button)[1])])


def click(button="left", clicks=1):
    down, up = _btn(button)
    _send([_mouse_input(f) for _ in range(int(clicks)) for f in (down, up)])


def double_click():
    click("left", 2)


def right_click():
    click("right")


def scroll(amount):
    """Vertical wheel; amount is raw wheel delta (120 per notch, positive = up), like pyautogui.scroll."""
    _send([_mouse_input(MOUSEEVENTF_WHEEL, int(amount))])


def key_down(key):
    _send([_key_input(_vk(key), False)])


def key_up(key):
    _send([_key_input(_vk(key), True)])


def press(key, presses=1):
    vk = _vk(key)
    _send([_key_input(vk, up) for _ in range(int(presses)) for up in (False, True)])


def hotkey(*keys):
    vks = [_vk(k) for k in keys]
    _send([_key_input(v, False) for v in vks] + [_key_input(v, True) for v in reversed(vks)])


def _unicode_inputs(ch):
    """Down/up KEYEVENTF_UNICODE events for one character (UTF-16 surrogate pairs above U+FFFF)."""
    code = ord(ch)
    units = [code] if code <= 0xFFFF else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
    out = []
    for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
        for u in units:
            i = INPUT()
            i.type = INPUT_KEYBOARD
            i.ki = KEYBDINPUT(0, u, flags, 0, 0)
            out.append(i)
    return out


_WRITE_KEYS = {"\n": "enter", "\r": "enter", "\t": "tab"}


def write(text, interval=0.0):
    """Types `text` into the focused window (pyautogui.write equivalent; Enter/Tab for newline/tab)."""
    for n, ch in enumerate(str(text)):
        if n and interval:
            time.sleep(interval)
        key = _WRITE_KEYS.get(ch)
        if key:
            press(key)
        else:
            _send(_unicode_inputs(ch))


def screenshot(path=None):
    """Grabs the primary screen as a PIL Image; saves it to `path` when given."""
    from PIL import ImageGrab
    img = ImageGrab.grab()
    if path:
        img.save(path)
    return img
