"""
system_control.py — System Control System Adapter for J.A.R.V.I.S. (Phase 5).

Provides native Windows system adapters and reflex controllers for:
  - Audio: volume_up, volume_down, set_volume, get_volume, mute (pycaw / IAudioEndpointVolume + fallback)
  - Display: brightness_up, brightness_down, set_brightness, get_brightness, toggle_night_light (WMI / CIM)
  - Window Management: snap_window, minimize_all, switch_to_app, move_to_other_monitor, close_window (with unsaved doc detection)
  - Power & Session: lock_workstation, sleep_system, take_screenshot, toggle_wifi, toggle_bluetooth, toggle_do_not_disturb,
    shutdown_system, restart_system (with confirmation gate)

Strictly NO Qt imports. 100% unit-testable with interfaces and mocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union


# ============================================================================
# Protocols (Interfaces for 100% Mockability)
# ============================================================================

class AudioBackendProtocol(Protocol):
    """Interface for system audio endpoint volume and mute operations."""

    def get_volume(self) -> int:
        ...

    def set_volume(self, level: int) -> int:
        ...

    def volume_up(self, steps: int = 5) -> int:
        ...

    def volume_down(self, steps: int = 5) -> int:
        ...

    def mute(self, toggle: bool = True) -> bool:
        ...

    def is_muted(self) -> bool:
        ...


class DisplayBackendProtocol(Protocol):
    """Interface for system display brightness and night light operations."""

    def get_brightness(self) -> int:
        ...

    def set_brightness(self, level: int) -> int:
        ...

    def brightness_up(self, steps: int = 10) -> int:
        ...

    def brightness_down(self, steps: int = 10) -> int:
        ...

    def toggle_night_light(self) -> bool:
        ...


class WindowBackendProtocol(Protocol):
    """Interface for window management and application switching."""

    def snap_window(self, direction: str) -> bool:
        ...

    def minimize_all(self) -> bool:
        ...

    def switch_to_app(self, app_name: str) -> bool:
        ...

    def move_to_other_monitor(self) -> bool:
        ...

    def close_window(self, confirm: bool = False) -> Tuple[bool, str]:
        ...

    def get_foreground_window_title(self) -> str:
        ...

    def resolve_window_under_cursor(self, point: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        ...

    def open_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        ...

    def copy_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        ...

    def close_this(self, confirm: bool = False, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        ...


class PowerSessionBackendProtocol(Protocol):
    """Interface for workstation power, radio, and session controls."""

    def lock_workstation(self) -> bool:
        ...

    def sleep_system(self) -> bool:
        ...

    def take_screenshot(self, path: Optional[str] = None) -> Optional[str]:
        ...

    def toggle_wifi(self) -> Tuple[bool, str]:
        ...

    def toggle_bluetooth(self) -> Tuple[bool, str]:
        ...

    def toggle_do_not_disturb(self) -> Tuple[bool, str]:
        ...

    def shutdown_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        ...

    def restart_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        ...


# ============================================================================
# Audio Implementations (pycaw + Fallback + Mock)
# ============================================================================

class FallbackAudioBackend:
    """
    Fallback audio backend using pyautogui hardware key simulation
    and internal volume tracking when pycaw is unavailable.
    """

    def __init__(self, initial_volume: int = 50, initial_muted: bool = False):
        self._volume = max(0, min(100, int(initial_volume)))
        self._muted = bool(initial_muted)

    def get_volume(self) -> int:
        return self._volume

    def set_volume(self, level: int) -> int:
        clamped = max(0, min(100, int(level)))
        self._volume = clamped
        return self._volume

    def volume_up(self, steps: int = 5) -> int:
        steps_val = max(1, min(50, int(steps)))
        try:
            import pyautogui
            pyautogui.press("volumeup", presses=max(1, steps_val // 2), _pause=False)
        except Exception:
            pass
        self._volume = min(100, self._volume + steps_val)
        return self._volume

    def volume_down(self, steps: int = 5) -> int:
        steps_val = max(1, min(50, int(steps)))
        try:
            import pyautogui
            pyautogui.press("volumedown", presses=max(1, steps_val // 2), _pause=False)
        except Exception:
            pass
        self._volume = max(0, self._volume - steps_val)
        return self._volume

    def mute(self, toggle: bool = True) -> bool:
        try:
            import pyautogui
            pyautogui.press("volumemute", _pause=False)
        except Exception:
            pass
        if toggle:
            self._muted = not self._muted
        else:
            self._muted = True
        return self._muted

    def is_muted(self) -> bool:
        return self._muted


class PycawAudioBackend:
    """
    Hardware-direct audio backend using Windows Core Audio API (pycaw / IAudioEndpointVolume).
    Gracefully falls back to FallbackAudioBackend on COM or initialization error.
    """

    def __init__(self):
        self._fallback = FallbackAudioBackend()
        self._available = False
        self._check_available()

    def _check_available(self) -> bool:
        if sys.platform != "win32":
            self._available = False
            return False
        try:
            from pycaw.pycaw import AudioUtilities
            dev = AudioUtilities.GetSpeakers()
            if dev is not None:
                self._available = True
                return True
        except Exception:
            pass
        self._available = False
        return False

    def _get_endpoint(self):
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetSpeakers()
        if dev is None:
            raise RuntimeError("No speakers device found")
        if hasattr(dev, "EndpointVolume"):
            return dev.EndpointVolume
        # Legacy pycaw activate
        from pycaw.pycaw import IAudioEndpointVolume
        import comtypes
        interface = dev.Activate(IAudioEndpointVolume._iid_, 7, None)
        return interface.QueryInterface(IAudioEndpointVolume)

    def get_volume(self) -> int:
        try:
            endpoint = self._get_endpoint()
            scalar = endpoint.GetMasterVolumeLevelScalar()
            val = int(round(scalar * 100))
            self._fallback.set_volume(val)
            return val
        except Exception:
            return self._fallback.get_volume()

    def set_volume(self, level: int) -> int:
        clamped = max(0, min(100, int(level)))
        try:
            endpoint = self._get_endpoint()
            endpoint.SetMasterVolumeLevelScalar(clamped / 100.0, None)
            self._fallback.set_volume(clamped)
            return clamped
        except Exception:
            return self._fallback.set_volume(clamped)

    def volume_up(self, steps: int = 5) -> int:
        try:
            current = self.get_volume()
            return self.set_volume(current + steps)
        except Exception:
            return self._fallback.volume_up(steps)

    def volume_down(self, steps: int = 5) -> int:
        try:
            current = self.get_volume()
            return self.set_volume(current - steps)
        except Exception:
            return self._fallback.volume_down(steps)

    def mute(self, toggle: bool = True) -> bool:
        try:
            endpoint = self._get_endpoint()
            current = bool(endpoint.GetMute())
            target = (not current) if toggle else True
            endpoint.SetMute(int(target), None)
            self._fallback._muted = target
            return target
        except Exception:
            return self._fallback.mute(toggle)

    def is_muted(self) -> bool:
        try:
            endpoint = self._get_endpoint()
            val = bool(endpoint.GetMute())
            self._fallback._muted = val
            return val
        except Exception:
            return self._fallback.is_muted()


class MockAudioBackend:
    """In-memory mock audio backend for deterministic unit tests."""

    def __init__(self, volume: int = 50, muted: bool = False):
        self.volume = volume
        self.muted = muted
        self.calls: List[Tuple[str, Any]] = []

    def get_volume(self) -> int:
        self.calls.append(("get_volume", None))
        return self.volume

    def set_volume(self, level: int) -> int:
        self.calls.append(("set_volume", level))
        self.volume = max(0, min(100, int(level)))
        return self.volume

    def volume_up(self, steps: int = 5) -> int:
        self.calls.append(("volume_up", steps))
        self.volume = min(100, self.volume + steps)
        return self.volume

    def volume_down(self, steps: int = 5) -> int:
        self.calls.append(("volume_down", steps))
        self.volume = max(0, self.volume - steps)
        return self.volume

    def mute(self, toggle: bool = True) -> bool:
        self.calls.append(("mute", toggle))
        if toggle:
            self.muted = not self.muted
        else:
            self.muted = True
        return self.muted

    def is_muted(self) -> bool:
        self.calls.append(("is_muted", None))
        return self.muted


# ============================================================================
# Display Implementations (WMI / CIM + Fallback + Mock)
# ============================================================================

class FallbackDisplayBackend:
    """In-memory display backend for systems without DDC/CI or headless environments."""

    def __init__(self, initial_brightness: int = 70, initial_night_light: bool = False):
        self._brightness = max(0, min(100, int(initial_brightness)))
        self._night_light = bool(initial_night_light)

    def get_brightness(self) -> int:
        return self._brightness

    def set_brightness(self, level: int) -> int:
        self._brightness = max(0, min(100, int(level)))
        return self._brightness

    def brightness_up(self, steps: int = 10) -> int:
        self._brightness = min(100, self._brightness + max(1, int(steps)))
        return self._brightness

    def brightness_down(self, steps: int = 10) -> int:
        self._brightness = max(0, self._brightness - max(1, int(steps)))
        return self._brightness

    def toggle_night_light(self) -> bool:
        self._night_light = not self._night_light
        return self._night_light


class WindowsDisplayBackend:
    """
    Direct Windows display adapter using WmiMonitorBrightnessMethods and PowerShell CIM
    with night light registry state toggling.
    """

    def __init__(self):
        self._fallback = FallbackDisplayBackend()

    def get_brightness(self) -> int:
        if sys.platform != "win32":
            return self._fallback.get_brightness()

        # 1. Try python wmi if installed
        try:
            import wmi  # type: ignore
            w = wmi.WMI(namespace="wmi")
            monitors = w.WmiMonitorBrightness()
            if monitors and len(monitors) > 0:
                b = int(monitors[0].CurrentBrightness)
                self._fallback.set_brightness(b)
                return b
        except Exception:
            pass

        # 2. Try PowerShell CIM query
        try:
            cmd = "Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness | Select-Object -ExpandProperty CurrentBrightness"
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode == 0 and res.stdout.strip():
                lines = res.stdout.strip().split()
                if lines and lines[0].isdigit():
                    b = int(lines[0])
                    self._fallback.set_brightness(b)
                    return b
        except Exception:
            pass

        return self._fallback.get_brightness()

    def set_brightness(self, level: int) -> int:
        clamped = max(0, min(100, int(level)))
        self._fallback.set_brightness(clamped)

        if sys.platform != "win32":
            return clamped

        # 1. Try python wmi
        try:
            import wmi  # type: ignore
            w = wmi.WMI(namespace="wmi")
            methods = w.WmiMonitorBrightnessMethods()
            if methods and len(methods) > 0:
                methods[0].WmiSetBrightness(Timeout=1, Brightness=clamped)
                return clamped
        except Exception:
            pass

        # 2. Try PowerShell CIM call
        try:
            cmd = f"(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods).WmiSetBrightness(1, {clamped})"
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode == 0:
                return clamped
        except Exception:
            pass

        return clamped

    def brightness_up(self, steps: int = 10) -> int:
        current = self.get_brightness()
        return self.set_brightness(current + steps)

    def brightness_down(self, steps: int = 10) -> int:
        current = self.get_brightness()
        return self.set_brightness(current - steps)

    def toggle_night_light(self) -> bool:
        if sys.platform != "win32":
            return self._fallback.toggle_night_light()

        # Query and toggle night light registry if possible
        try:
            import winreg
            root = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current",
            )
            for i in range(winreg.QueryInfoKey(root)[0]):
                sub = winreg.EnumKey(root, i)
                if "bluelightreductionstate" in sub and "device" not in sub:
                    sk = winreg.OpenKey(root, sub)
                    for j in range(winreg.QueryInfoKey(sk)[0]):
                        child_name = winreg.EnumKey(sk, j)
                        child = winreg.OpenKey(sk, child_name, 0, winreg.KEY_READ | winreg.KEY_WRITE)
                        val, typ = winreg.QueryValueEx(child, "Data")
                        if isinstance(val, (bytes, bytearray)) and len(val) > 18:
                            current_on = (val[18] == 0x15)
                            new_byte = 0x13 if current_on else 0x15
                            new_val = bytearray(val)
                            new_val[18] = new_byte
                            winreg.SetValueEx(child, "Data", 0, typ, bytes(new_val))
                            new_state = (new_byte == 0x15)
                            self._fallback._night_light = new_state
                            return new_state
        except Exception:
            pass

        # Fallback: Launch nightlight settings or toggle internal state
        try:
            subprocess.Popen("start ms-settings:nightlight", shell=True)
        except Exception:
            pass
        return self._fallback.toggle_night_light()


class MockDisplayBackend:
    """In-memory mock display backend for testing."""

    def __init__(self, brightness: int = 70, night_light: bool = False):
        self.brightness = brightness
        self.night_light = night_light
        self.calls: List[Tuple[str, Any]] = []

    def get_brightness(self) -> int:
        self.calls.append(("get_brightness", None))
        return self.brightness

    def set_brightness(self, level: int) -> int:
        self.calls.append(("set_brightness", level))
        self.brightness = max(0, min(100, int(level)))
        return self.brightness

    def brightness_up(self, steps: int = 10) -> int:
        self.calls.append(("brightness_up", steps))
        self.brightness = min(100, self.brightness + steps)
        return self.brightness

    def brightness_down(self, steps: int = 10) -> int:
        self.calls.append(("brightness_down", steps))
        self.brightness = max(0, self.brightness - steps)
        return self.brightness

    def toggle_night_light(self) -> bool:
        self.calls.append(("toggle_night_light", None))
        self.night_light = not self.night_light
        return self.night_light


# ============================================================================
# Window Management Implementations
# ============================================================================

class WindowsWindowBackend:
    """Window management backend using Win32 API and pyautogui hotkeys."""

    def snap_window(self, direction: str) -> bool:
        dir_clean = str(direction).lower().strip()
        try:
            import pyautogui
            if dir_clean in ("left", "west"):
                pyautogui.hotkey("win", "left", _pause=False)
            elif dir_clean in ("right", "east"):
                pyautogui.hotkey("win", "right", _pause=False)
            elif dir_clean in ("up", "maximize", "north"):
                pyautogui.hotkey("win", "up", _pause=False)
            elif dir_clean in ("down", "restore", "south"):
                pyautogui.hotkey("win", "down", _pause=False)
            else:
                return False
            return True
        except Exception:
            return False

    def minimize_all(self) -> bool:
        try:
            import pyautogui
            pyautogui.hotkey("win", "d", _pause=False)
            return True
        except Exception:
            return False

    def move_to_other_monitor(self) -> bool:
        try:
            import pyautogui
            pyautogui.hotkey("win", "shift", "right", _pause=False)
            return True
        except Exception:
            return False

    def get_foreground_window_title(self) -> str:
        if sys.platform != "win32":
            return ""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                return buff.value
        except Exception:
            pass
        return ""

    def close_window(self, confirm: bool = False) -> Tuple[bool, str]:
        title = self.get_foreground_window_title()
        # Unsaved document check: '*' or 'untitled' or 'unsaved'
        has_unsaved = (
            "*" in title
            or "untitled" in title.lower()
            or "unsaved" in title.lower()
        )
        if has_unsaved and not confirm:
            doc_name = title.strip() or "Active document"
            return False, f"Document '{doc_name}' has unsaved changes. Please confirm to close."

        try:
            import pyautogui
            pyautogui.hotkey("alt", "f4", _pause=False)
            return True, "Window closed."
        except Exception as exc:
            return False, f"Unable to close window: {exc}"

    def resolve_window_under_cursor(self, point: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        import window_resolver
        return window_resolver.resolve_window_under_cursor(point=point)

    def open_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        import window_resolver
        return window_resolver.open_this(point=point)

    def copy_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        import window_resolver
        return window_resolver.copy_this(point=point)

    def close_this(self, confirm: bool = False, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        import window_resolver
        return window_resolver.close_this(confirm=confirm, point=point)

    def switch_to_app(self, app_name: str) -> bool:
        target = app_name.lower().strip()
        if not target:
            return False

        # 1. Search existing running top-level windows
        if sys.platform == "win32":
            try:
                import ctypes
                matched_hwnd = None

                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)

                def enum_cb(hwnd, _lparam):
                    nonlocal matched_hwnd
                    if ctypes.windll.user32.IsWindowVisible(hwnd):
                        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                        if length > 0:
                            buff = ctypes.create_unicode_buffer(length + 1)
                            ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                            t = buff.value.lower()
                            if target in t:
                                matched_hwnd = hwnd
                                return False
                    return True

                ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

                if matched_hwnd:
                    # SW_RESTORE = 9
                    ctypes.windll.user32.ShowWindow(matched_hwnd, 9)
                    ctypes.windll.user32.SetForegroundWindow(matched_hwnd)
                    return True
            except Exception:
                pass

        # 2. If window not found, launch the app via APPS map or executable
        try:
            from commands import APPS, _launch
            if target in APPS:
                _launch(APPS[target])
                return True
            else:
                subprocess.Popen(f'start "" {target}', shell=True)
                return True
        except Exception:
            return False


class MockWindowBackend:
    """In-memory mock window backend for deterministic testing."""

    def __init__(self, foreground_title: str = "Test Window"):
        self.foreground_title = foreground_title
        self.last_snap: Optional[str] = None
        self.all_minimized = False
        self.moved_monitor = False
        self.closed = False
        self.switched_apps: List[str] = []
        self.calls: List[Tuple[str, Any]] = []

    def snap_window(self, direction: str) -> bool:
        self.calls.append(("snap_window", direction))
        self.last_snap = direction
        return True

    def minimize_all(self) -> bool:
        self.calls.append(("minimize_all", None))
        self.all_minimized = True
        return True

    def switch_to_app(self, app_name: str) -> bool:
        self.calls.append(("switch_to_app", app_name))
        self.switched_apps.append(app_name)
        return True

    def move_to_other_monitor(self) -> bool:
        self.calls.append(("move_to_other_monitor", None))
        self.moved_monitor = True
        return True

    def get_foreground_window_title(self) -> str:
        return self.foreground_title

    def close_window(self, confirm: bool = False) -> Tuple[bool, str]:
        self.calls.append(("close_window", confirm))
        title = self.foreground_title
        has_unsaved = ("*" in title or "untitled" in title.lower() or "unsaved" in title.lower())
        if has_unsaved and not confirm:
            return False, f"Document '{title}' has unsaved changes. Please confirm to close."
        self.closed = True
        return True, "Window closed."

    def resolve_window_under_cursor(self, point: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        self.calls.append(("resolve_window_under_cursor", point))
        has_unsaved = ("*" in self.foreground_title or "untitled" in self.foreground_title.lower() or "unsaved" in self.foreground_title.lower())
        return {
            "hwnd": 1234,
            "raw_hwnd": 1234,
            "title": self.foreground_title,
            "process_id": 100,
            "process_name": "notepad.exe",
            "point": point or (100, 100),
            "has_unsaved": has_unsaved,
            "is_valid": True,
        }

    def open_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        self.calls.append(("open_this", point))
        return True, f"Opened target in '{self.foreground_title}', sir."

    def copy_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        self.calls.append(("copy_this", point))
        return True, f"Copied selection from '{self.foreground_title}' to clipboard, sir."

    def close_this(self, confirm: bool = False, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        self.calls.append(("close_this", (confirm, point)))
        has_unsaved = ("*" in self.foreground_title or "untitled" in self.foreground_title.lower() or "unsaved" in self.foreground_title.lower())
        if has_unsaved and not confirm:
            return False, f"Document '{self.foreground_title}' has unsaved changes. Please confirm to close."
        self.closed = True
        return True, f"Closed '{self.foreground_title}', sir."


# ============================================================================
# Power & Session Implementations
# ============================================================================

class WindowsPowerSessionBackend:
    """Power, lock, screenshot, and session controls for Windows."""

    def lock_workstation(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return True
        except Exception:
            return False

    def sleep_system(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            res = ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
            return bool(res)
        except Exception:
            try:
                subprocess.run(
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                    check=False,
                    timeout=3,
                )
                return True
            except Exception:
                return False

    def take_screenshot(self, path: Optional[str] = None) -> Optional[str]:
        try:
            import pyautogui
            if not path:
                folder = os.path.join(os.path.expanduser("~"), "Pictures", "Jarvis")
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, time.strftime("shot-%Y%m%d-%H%M%S.png"))
            pyautogui.screenshot(path)
            return path
        except Exception:
            return None

    def toggle_wifi(self) -> Tuple[bool, str]:
        if sys.platform != "win32":
            return True, "Wi-Fi toggled (non-windows simulated)."

        try:
            # Check current Wi-Fi status
            check = subprocess.run(
                ["netsh", "interface", "show", "interface"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            is_enabled = False
            for line in check.stdout.splitlines():
                if "wi-fi" in line.lower() or "wireless" in line.lower():
                    if "enabled" in line.lower():
                        is_enabled = True
                    break

            target_state = "disable" if is_enabled else "enable"
            res = subprocess.run(
                ["netsh", "interface", "set", "interface", "Wi-Fi", f"admin={target_state}"],
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode == 0:
                action_str = "disabled" if is_enabled else "enabled"
                return True, f"Wi-Fi has been {action_str}, sir."
        except Exception:
            pass

        # Fallback: Launch Wi-Fi settings page
        try:
            subprocess.Popen("start ms-settings:network-wifi", shell=True)
            return True, "Opened Wi-Fi settings, sir."
        except Exception as exc:
            return False, f"Unable to toggle Wi-Fi: {exc}"

    def toggle_bluetooth(self) -> Tuple[bool, str]:
        # Launch Bluetooth settings for quick toggle or radio command
        try:
            subprocess.Popen("start ms-settings:bluetooth", shell=True)
            return True, "Opened Bluetooth settings for toggle, sir."
        except Exception as exc:
            return False, f"Unable to open Bluetooth settings: {exc}"

    def toggle_do_not_disturb(self) -> Tuple[bool, str]:
        # Launch Focus Assist / Do Not Disturb settings
        try:
            subprocess.Popen("start ms-settings:quiethours", shell=True)
            return True, "Opened Focus Assist / Do Not Disturb settings, sir."
        except Exception as exc:
            return False, f"Unable to open Focus Assist: {exc}"

    def shutdown_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        if not confirmed:
            return False, "Shutdown requires confirmation. Please say 'confirm shutdown' to proceed."

        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["shutdown", "/s", "/t", "10"],
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            return True, "System shutdown initiated. Powering off in 10 seconds, sir."
        except Exception as exc:
            return False, f"Failed to initiate shutdown: {exc}"

    def restart_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        if not confirmed:
            return False, "Restart requires confirmation. Please say 'confirm restart' to proceed."

        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["shutdown", "/r", "/t", "10"],
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            return True, "System reboot initiated. Restarting in 10 seconds, sir."
        except Exception as exc:
            return False, f"Failed to initiate restart: {exc}"


class MockPowerSessionBackend:
    """Mock power and session backend for safe unit testing."""

    def __init__(self):
        self.locked = False
        self.slept = False
        self.screenshots: List[str] = []
        self.wifi_toggled = False
        self.bluetooth_toggled = False
        self.dnd_toggled = False
        self.shutdown_executed = False
        self.restart_executed = False
        self.calls: List[Tuple[str, Any]] = []

    def lock_workstation(self) -> bool:
        self.calls.append(("lock_workstation", None))
        self.locked = True
        return True

    def sleep_system(self) -> bool:
        self.calls.append(("sleep_system", None))
        self.slept = True
        return True

    def take_screenshot(self, path: Optional[str] = None) -> Optional[str]:
        p = path or "mock_screenshot.png"
        self.calls.append(("take_screenshot", p))
        self.screenshots.append(p)
        return p

    def toggle_wifi(self) -> Tuple[bool, str]:
        self.calls.append(("toggle_wifi", None))
        self.wifi_toggled = not self.wifi_toggled
        state = "enabled" if self.wifi_toggled else "disabled"
        return True, f"Wi-Fi {state}."

    def toggle_bluetooth(self) -> Tuple[bool, str]:
        self.calls.append(("toggle_bluetooth", None))
        self.bluetooth_toggled = not self.bluetooth_toggled
        return True, "Bluetooth toggled."

    def toggle_do_not_disturb(self) -> Tuple[bool, str]:
        self.calls.append(("toggle_do_not_disturb", None))
        self.dnd_toggled = not self.dnd_toggled
        return True, "Do Not Disturb toggled."

    def shutdown_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        self.calls.append(("shutdown_system", confirmed))
        if not confirmed:
            return False, "Shutdown requires confirmation. Please say 'confirm shutdown' to proceed."
        self.shutdown_executed = True
        return True, "System shutdown initiated."

    def restart_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        self.calls.append(("restart_system", confirmed))
        if not confirmed:
            return False, "Restart requires confirmation. Please say 'confirm restart' to proceed."
        self.restart_executed = True
        return True, "System restart initiated."


# ============================================================================
# System Control Adapter
# ============================================================================

class SystemControlAdapter:
    """
    Unified high-level system control adapter connecting reflex intents,
    LLM actions, and native Windows interfaces.
    """

    def __init__(
        self,
        audio_backend: Optional[AudioBackendProtocol] = None,
        display_backend: Optional[DisplayBackendProtocol] = None,
        window_backend: Optional[WindowBackendProtocol] = None,
        power_backend: Optional[PowerSessionBackendProtocol] = None,
    ):
        self.audio = audio_backend or PycawAudioBackend()
        self.display = display_backend or WindowsDisplayBackend()
        self.window = window_backend or WindowsWindowBackend()
        self.power = power_backend or WindowsPowerSessionBackend()
        self._pending_destructive_action: Optional[str] = None

    # --- Audio API ---

    def volume_up(self, steps: int = 5) -> Tuple[bool, str]:
        lvl = self.audio.volume_up(steps=steps)
        return True, f"Volume increased to {lvl} percent, sir."

    def volume_down(self, steps: int = 5) -> Tuple[bool, str]:
        lvl = self.audio.volume_down(steps=steps)
        return True, f"Volume decreased to {lvl} percent, sir."

    def set_volume(self, level: int) -> Tuple[bool, str]:
        lvl = self.audio.set_volume(level=level)
        return True, f"Volume set to {lvl} percent, sir."

    def get_volume(self) -> Tuple[bool, str]:
        lvl = self.audio.get_volume()
        muted_str = " (muted)" if self.audio.is_muted() else ""
        return True, f"Current master volume is at {lvl} percent{muted_str}, sir."

    def mute(self, toggle: bool = True) -> Tuple[bool, str]:
        muted = self.audio.mute(toggle=toggle)
        state_str = "muted" if muted else "unmuted"
        return True, f"Audio {state_str}, sir."

    # --- Display API ---

    def brightness_up(self, steps: int = 10) -> Tuple[bool, str]:
        lvl = self.display.brightness_up(steps=steps)
        return True, f"Screen brightness increased to {lvl} percent, sir."

    def brightness_down(self, steps: int = 10) -> Tuple[bool, str]:
        lvl = self.display.brightness_down(steps=steps)
        return True, f"Screen brightness lowered to {lvl} percent, sir."

    def set_brightness(self, level: int) -> Tuple[bool, str]:
        lvl = self.display.set_brightness(level=level)
        return True, f"Screen brightness calibrated to {lvl} percent, sir."

    def get_brightness(self) -> Tuple[bool, str]:
        lvl = self.display.get_brightness()
        return True, f"Current display brightness is at {lvl} percent, sir."

    def toggle_night_light(self) -> Tuple[bool, str]:
        on = self.display.toggle_night_light()
        state = "engaged" if on else "disengaged"
        return True, f"Night Light display filter {state}, sir."

    # --- Window API ---

    def snap_window(self, direction: str) -> Tuple[bool, str]:
        ok = self.window.snap_window(direction)
        if ok:
            return True, f"Window snapped {direction}, sir."
        return False, f"Invalid snap direction '{direction}', sir."

    def minimize_all(self) -> Tuple[bool, str]:
        ok = self.window.minimize_all()
        if ok:
            return True, "All windows minimized, sir."
        return False, "Failed to minimize all windows, sir."

    def switch_to_app(self, app_name: str) -> Tuple[bool, str]:
        ok = self.window.switch_to_app(app_name)
        if ok:
            return True, f"Switched focus to {app_name}, sir."
        return False, f"Could not find or launch application '{app_name}', sir."

    def move_to_other_monitor(self) -> Tuple[bool, str]:
        ok = self.window.move_to_other_monitor()
        if ok:
            return True, "Active window moved to other monitor, sir."
        return False, "Failed to move window to other monitor, sir."

    def close_window(self, confirm: bool = False) -> Tuple[bool, str]:
        return self.window.close_window(confirm=confirm)

    def resolve_window_under_cursor(self, point: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        return self.window.resolve_window_under_cursor(point=point)

    def open_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        return self.window.open_this(point=point)

    def copy_this(self, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        return self.window.copy_this(point=point)

    def close_this(self, confirm: bool = False, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
        return self.window.close_this(confirm=confirm, point=point)

    # --- Power & Session API ---

    def lock_workstation(self) -> Tuple[bool, str]:
        ok = self.power.lock_workstation()
        if ok:
            return True, "Workstation locked securely, sir."
        return False, "Lock operation not supported on this platform, sir."

    def sleep_system(self) -> Tuple[bool, str]:
        ok = self.power.sleep_system()
        if ok:
            return True, "Suspending system power states, sir."
        return False, "Sleep mode could not be engaged, sir."

    def take_screenshot(self, path: Optional[str] = None) -> Tuple[bool, str]:
        p = self.power.take_screenshot(path=path)
        if p:
            return True, f"Screenshot captured and saved to {p}, sir."
        return False, "Failed to capture screenshot, sir."

    def toggle_wifi(self) -> Tuple[bool, str]:
        return self.power.toggle_wifi()

    def toggle_bluetooth(self) -> Tuple[bool, str]:
        return self.power.toggle_bluetooth()

    def toggle_do_not_disturb(self) -> Tuple[bool, str]:
        return self.power.toggle_do_not_disturb()

    def shutdown_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        return self.power.shutdown_system(confirmed=confirmed)

    def restart_system(self, confirmed: bool = False) -> Tuple[bool, str]:
        return self.power.restart_system(confirmed=confirmed)


# ============================================================================
# Singleton & Global Registry
# ============================================================================

_SYSTEM_CONTROL: Optional[SystemControlAdapter] = None
_LOCK = threading.Lock()


def get_system_control() -> SystemControlAdapter:
    """Returns the singleton instance of SystemControlAdapter."""
    global _SYSTEM_CONTROL
    with _LOCK:
        if _SYSTEM_CONTROL is None:
            _SYSTEM_CONTROL = SystemControlAdapter()
        return _SYSTEM_CONTROL


def set_system_control(adapter: SystemControlAdapter) -> None:
    """Injects a custom or mock SystemControlAdapter."""
    global _SYSTEM_CONTROL
    with _LOCK:
        _SYSTEM_CONTROL = adapter


def reset_system_control() -> None:
    """Resets the singleton to default."""
    global _SYSTEM_CONTROL
    with _LOCK:
        _SYSTEM_CONTROL = None


# Module-level convenience functions
def volume_up(steps: int = 5) -> Tuple[bool, str]:
    return get_system_control().volume_up(steps=steps)

def volume_down(steps: int = 5) -> Tuple[bool, str]:
    return get_system_control().volume_down(steps=steps)

def set_volume(level: int) -> Tuple[bool, str]:
    return get_system_control().set_volume(level=level)

def get_volume() -> Tuple[bool, str]:
    return get_system_control().get_volume()

def mute(toggle: bool = True) -> Tuple[bool, str]:
    return get_system_control().mute(toggle=toggle)

def brightness_up(steps: int = 10) -> Tuple[bool, str]:
    return get_system_control().brightness_up(steps=steps)

def brightness_down(steps: int = 10) -> Tuple[bool, str]:
    return get_system_control().brightness_down(steps=steps)

def set_brightness(level: int) -> Tuple[bool, str]:
    return get_system_control().set_brightness(level=level)

def get_brightness() -> Tuple[bool, str]:
    return get_system_control().get_brightness()

def toggle_night_light() -> Tuple[bool, str]:
    return get_system_control().toggle_night_light()

def snap_window(direction: str) -> Tuple[bool, str]:
    return get_system_control().snap_window(direction=direction)

def minimize_all() -> Tuple[bool, str]:
    return get_system_control().minimize_all()

def switch_to_app(app_name: str) -> Tuple[bool, str]:
    return get_system_control().switch_to_app(app_name=app_name)

def move_to_other_monitor() -> Tuple[bool, str]:
    return get_system_control().move_to_other_monitor()

def close_window(confirm: bool = False) -> Tuple[bool, str]:
    return get_system_control().close_window(confirm=confirm)

def get_foreground_window_title() -> str:
    return get_system_control().window.get_foreground_window_title()

def resolve_window_under_cursor(point: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    return get_system_control().resolve_window_under_cursor(point=point)

def open_this(point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    return get_system_control().open_this(point=point)

def copy_this(point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    return get_system_control().copy_this(point=point)

def close_this(confirm: bool = False, point: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    return get_system_control().close_this(confirm=confirm, point=point)

def lock_workstation() -> Tuple[bool, str]:
    return get_system_control().lock_workstation()

def sleep_system() -> Tuple[bool, str]:
    return get_system_control().sleep_system()

def take_screenshot(path: Optional[str] = None) -> Tuple[bool, str]:
    return get_system_control().take_screenshot(path=path)

def toggle_wifi() -> Tuple[bool, str]:
    return get_system_control().toggle_wifi()

def toggle_bluetooth() -> Tuple[bool, str]:
    return get_system_control().toggle_bluetooth()

def toggle_do_not_disturb() -> Tuple[bool, str]:
    return get_system_control().toggle_do_not_disturb()

def shutdown_system(confirmed: bool = False) -> Tuple[bool, str]:
    return get_system_control().shutdown_system(confirmed=confirmed)

def restart_system(confirmed: bool = False) -> Tuple[bool, str]:
    return get_system_control().restart_system(confirmed=confirmed)
