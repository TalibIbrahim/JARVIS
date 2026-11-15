"""In-memory system_control backends for deterministic unit tests (moved out of the production module)."""
from typing import Any, Dict, List, Optional, Tuple


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


class MockWindowBackend:
    """In-memory mock window backend for deterministic testing."""

    def __init__(self, foreground_title: str = "Test Window", is_elevated: bool = False):
        self.foreground_title = foreground_title
        self.is_elevated = is_elevated
        self.last_snap: Optional[str] = None
        self.all_minimized = False
        self.moved_monitor = False
        self.closed = False
        self.switched_apps: List[str] = []
        self.calls: List[Tuple[str, Any]] = []

    def is_foreground_elevated(self) -> bool:
        return self.is_elevated

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
