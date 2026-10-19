"""
island_manager.py — J.A.R.V.I.S. Dynamic Island Page Manager & Configuration Logic.

Owns expanded card navigation, active page ordering, inactivity auto-collapse timing,
scale dimensions, hardware layouts, shape styling, trigger modes, and assistant preemption.

Strictly NO Qt imports. 100% unit-testable behind clean interfaces with mocks.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


class IslandPage(str, Enum):
    """Expanded card page identifiers."""
    MEDIA = "media"
    CALENDAR = "calendar"
    WEATHER = "weather"
    HARDWARE = "hardware"
    JARVIS = "jarvis"

    @classmethod
    def from_str(cls, val: Union[str, IslandPage]) -> IslandPage:
        if isinstance(val, cls):
            return val
        cleaned = str(val).strip().lower()
        for member in cls:
            if member.value == cleaned or member.name.lower() == cleaned:
                return member
        raise ValueError(f"Unknown IslandPage: '{val}'")


class IslandSizeScale(str, Enum):
    """Size scales: small (0.85x), medium (1.0x), large (1.15x)."""
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"

    @classmethod
    def from_str(cls, val: Union[str, IslandSizeScale]) -> IslandSizeScale:
        if isinstance(val, cls):
            return val
        cleaned = str(val).strip().lower()
        for member in cls:
            if member.value == cleaned or member.name.lower() == cleaned:
                return member
        return cls.MEDIUM


SCALE_FACTORS: Dict[Union[IslandSizeScale, str], float] = {
    IslandSizeScale.SMALL: 0.85,
    IslandSizeScale.MEDIUM: 1.0,
    IslandSizeScale.LARGE: 1.15,
    "small": 0.85,
    "medium": 1.0,
    "large": 1.15,
}


class IslandHardwareLayout(str, Enum):
    """Hardware telemetry layout: 2x3 grid vs horizontal strip."""
    GRID = "grid"
    STRIP = "strip"

    @classmethod
    def from_str(cls, val: Union[str, IslandHardwareLayout]) -> IslandHardwareLayout:
        if isinstance(val, cls):
            return val
        cleaned = str(val).strip().lower()
        for member in cls:
            if member.value == cleaned or member.name.lower() == cleaned:
                return member
        return cls.GRID


class IslandShapeStyle(str, Enum):
    """Island outline shape: detached pill vs display-flush notch."""
    PILL = "pill"
    NOTCH = "notch"

    @classmethod
    def from_str(cls, val: Union[str, IslandShapeStyle]) -> IslandShapeStyle:
        if isinstance(val, cls):
            return val
        cleaned = str(val).strip().lower()
        for member in cls:
            if member.value == cleaned or member.name.lower() == cleaned:
                return member
        return cls.PILL


class IslandExpandTrigger(str, Enum):
    """Trigger mode to transition island from compact pill to expanded card."""
    HOVER = "hover"
    CLICK = "click"
    DWELL = "dwell"
    FIST = "fist"
    VOICE = "voice"

    @classmethod
    def from_str(cls, val: Union[str, IslandExpandTrigger]) -> IslandExpandTrigger:
        if isinstance(val, cls):
            return val
        cleaned = str(val).strip().lower()
        for member in cls:
            if member.value == cleaned or member.name.lower() == cleaned:
                return member
        return cls.HOVER


# Assistant states that preempt expanded card pages
ASSISTANT_PREEMPTIVE_STATES = {
    "WAKE",
    "LISTENING",
    "THINKING",
    "SPEAKING",
    "RESULT",
    "ERROR",
    "PROMPT",
}

DEFAULT_INACTIVITY_TIMEOUT = 4.0

# Base card dimensions (width, height) at 1.0x (medium scale)
BASE_PAGE_DIMENSIONS: Dict[IslandPage, Tuple[float, float]] = {
    IslandPage.MEDIA: (400.0, 168.0),
    IslandPage.CALENDAR: (370.0, 142.0),
    IslandPage.WEATHER: (400.0, 180.0),
    IslandPage.HARDWARE: (400.0, 180.0),
    IslandPage.JARVIS: (380.0, 150.0),
}
BASE_HARDWARE_STRIP_DIMENSIONS: Tuple[float, float] = (420.0, 72.0)
BASE_COMPACT_PILL_DIMENSIONS: Tuple[float, float] = (110.0, 30.0)


class IslandPageManager:
    """
    Manages Dynamic Island expanded page navigation, inactivity auto-collapse timing,
    size scaling, shape styles, hardware layouts, and assistant preemption arbitration.
    """

    def __init__(
        self,
        config: Optional[Any] = None,
        clock_fn: Callable[[], float] = time.time,
    ):
        self._clock = clock_fn
        self.inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT
        self.last_activity_time: float = self._clock()
        self.pin_open: bool = False

        # Display & Expansion State
        self.is_expanded: bool = False
        self.is_hidden: bool = False
        self.is_fullscreen: bool = False

        # Configurable pages and enabled mapping
        self._pages: List[IslandPage] = [
            IslandPage.MEDIA,
            IslandPage.CALENDAR,
            IslandPage.WEATHER,
            IslandPage.HARDWARE,
            IslandPage.JARVIS,
        ]
        self._enabled: Dict[IslandPage, bool] = {p: True for p in self._pages}
        self._current_page: Optional[IslandPage] = IslandPage.MEDIA

        # Layout & Style Configuration
        self.size_scale: IslandSizeScale = IslandSizeScale.MEDIUM
        self.hardware_layout: IslandHardwareLayout = IslandHardwareLayout.GRID
        self.shape_style: IslandShapeStyle = IslandShapeStyle.PILL
        self.expand_trigger: IslandExpandTrigger = IslandExpandTrigger.HOVER
        self.compact_mode: str = "time_weather"
        self.time_format: str = "auto"
        self.first_weekday: str = "locale"
        self.hide_fullscreen: bool = True
        self.waveform_color: str = "blue"

        # Preemption Arbitration State
        self._assistant_active: bool = False
        self._assistant_state: str = "IDLE"
        self._assistant_payload: Dict[str, Any] = {}
        self._assistant_expires_at: float = 0.0

        # Subsystem & Assistant Status Tracking
        self.tracking_enabled: bool = True
        self.mic_state: str = "active"
        self.camera_state: str = "active"
        self._last_command_text: str = ""
        self._last_command_result: str = ""
        self._last_command_timestamp: float = 0.0

        if config is not None:
            self.apply_config(config)

    # ──────────────────────────────────────────────────────────────────────────
    # Page Configuration & Properties
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def pages(self) -> List[IslandPage]:
        """Ordered list of all configured pages."""
        return list(self._pages)

    @pages.setter
    def pages(self, new_pages: List[Union[IslandPage, str]]) -> None:
        self.set_pages(new_pages)

    def set_pages(self, pages: List[Union[IslandPage, str]]) -> None:
        """Sets the ordered list of island pages."""
        normalized: List[IslandPage] = []
        for p in pages:
            try:
                norm_p = IslandPage.from_str(p)
                if norm_p not in normalized:
                    normalized.append(norm_p)
            except ValueError:
                continue

        self._pages = normalized
        # Preserve existing enabled statuses, default to True for new pages
        new_enabled: Dict[IslandPage, bool] = {}
        for p in self._pages:
            new_enabled[p] = self._enabled.get(p, True)
        self._enabled = new_enabled

        active = self.active_pages
        self._current_page = active[0] if active else None

    @property
    def enabled_pages(self) -> Dict[IslandPage, bool]:
        """Mapping of pages to enabled state."""
        return dict(self._enabled)

    @property
    def active_pages(self) -> List[IslandPage]:
        """List of currently enabled pages in configured order."""
        return [p for p in self._pages if self._enabled.get(p, True)]

    def set_page_enabled(self, page: Union[IslandPage, str], enabled: bool) -> None:
        """Enables or disables an island page."""
        try:
            norm_p = IslandPage.from_str(page)
        except ValueError:
            return

        self._enabled[norm_p] = bool(enabled)
        self._clamp_current_page()

    def set_pages_enabled(self, enabled_map: Dict[Union[IslandPage, str], bool]) -> None:
        """Batch updates enabled status for multiple pages."""
        for p, enabled in enabled_map.items():
            try:
                norm_p = IslandPage.from_str(p)
                self._enabled[norm_p] = bool(enabled)
            except ValueError:
                continue
        self._clamp_current_page()

    @property
    def current_page(self) -> Optional[IslandPage]:
        """Currently selected active page."""
        active = self.active_pages
        if not active:
            return None
        if self._current_page not in active:
            self._current_page = active[0]
        return self._current_page

    @current_page.setter
    def current_page(self, page: Union[IslandPage, str]) -> None:
        self.set_page(page)

    @property
    def page_index(self) -> int:
        """Zero-based index of current page within active pages, or -1 if empty."""
        active = self.active_pages
        cur = self.current_page
        if cur is None or cur not in active:
            return -1
        return active.index(cur)

    @page_index.setter
    def page_index(self, index: int) -> None:
        active = self.active_pages
        if not active:
            self._current_page = None
            return
        clamped = max(0, min(int(index), len(active) - 1))
        self._current_page = active[clamped]
        self.touch_activity()

    def set_page(self, page: Union[IslandPage, str]) -> bool:
        """Sets the active page if enabled. Returns True if switch succeeded."""
        try:
            norm_p = IslandPage.from_str(page)
        except ValueError:
            return False

        if norm_p in self.active_pages:
            self._current_page = norm_p
            self.touch_activity()
            return True
        return False

    def set_active_page(self, page: Union[IslandPage, str]) -> bool:
        """Alias for set_page."""
        return self.set_page(page)

    @property
    def active_page(self) -> Optional[IslandPage]:
        """Alias for current_page."""
        return self.current_page

    @active_page.setter
    def active_page(self, page: Union[IslandPage, str]) -> None:
        self.set_page(page)

    def next_page(self) -> Optional[IslandPage]:
        """Cycles to the next enabled page (wrapping around)."""
        active = self.active_pages
        if not active:
            self._current_page = None
            return None

        idx = self.page_index
        next_idx = (idx + 1) % len(active) if idx >= 0 else 0
        self._current_page = active[next_idx]
        self.touch_activity()
        return self._current_page

    def prev_page(self) -> Optional[IslandPage]:
        """Cycles to the previous enabled page (wrapping around)."""
        active = self.active_pages
        if not active:
            self._current_page = None
            return None

        idx = self.page_index
        prev_idx = (idx - 1) % len(active) if idx >= 0 else (len(active) - 1)
        self._current_page = active[prev_idx]
        self.touch_activity()
        return self._current_page

    def previous_page(self) -> Optional[IslandPage]:
        """Alias for prev_page."""
        return self.prev_page()

    def _clamp_current_page(self) -> None:
        """Ensures _current_page points to a valid active page."""
        active = self.active_pages
        if not active:
            self._current_page = None
        elif self._current_page not in active:
            self._current_page = active[0]

    # ──────────────────────────────────────────────────────────────────────────
    # Inactivity Tracking & Auto-Collapse
    # ──────────────────────────────────────────────────────────────────────────

    def touch_activity(self, now: Optional[float] = None) -> None:
        """Resets the inactivity timer to the given or current timestamp."""
        self.last_activity_time = float(now if now is not None else self._clock())

    def is_expired(self, now: Optional[float] = None) -> bool:
        """
        Returns True if the expanded card has timed out due to inactivity.
        Suppressed if pin_open is True, or if collapsed, or if assistant is active.
        """
        if self.pin_open:
            return False
        if not self.is_expanded:
            return False
        if self.is_assistant_active:
            return False

        current = float(now if now is not None else self._clock())
        return (current - self.last_activity_time) >= self.inactivity_timeout

    def check_inactivity(self, now: Optional[float] = None) -> bool:
        """Checks expiration and automatically collapses if expired. Returns True if collapsed."""
        if self.is_expired(now):
            self.collapse()
            return True
        return False

    def remaining_inactivity_time(self, now: Optional[float] = None) -> float:
        """Returns remaining seconds before auto-collapse, or inf if pinned/collapsed."""
        if self.pin_open or not self.is_expanded:
            return float("inf")
        if self.is_assistant_active:
            return float("inf")

        current = float(now if now is not None else self._clock())
        elapsed = current - self.last_activity_time
        return max(0.0, self.inactivity_timeout - elapsed)

    def toggle_pin(self) -> bool:
        """Toggles pin_open flag and touches activity."""
        self.pin_open = not self.pin_open
        self.touch_activity()
        return self.pin_open

    def set_pin_open(self, pinned: bool) -> bool:
        """Sets pin_open flag explicitly to suppress or restore auto-collapse."""
        self.pin_open = bool(pinned)
        self.touch_activity()
        return self.pin_open

    # ──────────────────────────────────────────────────────────────────────────
    # Expansion & Collapse
    # ──────────────────────────────────────────────────────────────────────────

    def expand(
        self,
        page: Optional[Union[IslandPage, str]] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Expands the island to card mode. Suppressed if assistant is preempting."""
        if self.is_assistant_active:
            return False

        self.is_expanded = True
        if page is not None:
            self.set_page(page)
        self.touch_activity(now)
        return True

    def collapse(self) -> bool:
        """Collapses the island back to compact pill resting state."""
        self.is_expanded = False
        return True

    def toggle_expand(self, page: Optional[Union[IslandPage, str]] = None) -> bool:
        """Toggles between expanded card and compact pill."""
        if self.is_expanded:
            self.collapse()
            return False
        return self.expand(page)

    def toggle_expanded(self, page: Optional[Union[IslandPage, str]] = None) -> bool:
        """Alias for toggle_expand."""
        return self.toggle_expand(page)

    # ──────────────────────────────────────────────────────────────────────────
    # Scaling & Dimensions
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def scale_factor(self) -> float:
        """Numerical scale multiplier corresponding to size_scale."""
        return SCALE_FACTORS.get(self.size_scale, 1.0)

    def set_size_scale(self, scale: Union[IslandSizeScale, str]) -> None:
        """Sets size scale (small: 0.85x, medium: 1.0x, large: 1.15x)."""
        self.size_scale = IslandSizeScale.from_str(scale)

    def scale_dimensions(self, width: float, height: float) -> Tuple[int, int]:
        """Applies current scale factor to width and height, returning integer pixels."""
        factor = self.scale_factor
        return round(width * factor), round(height * factor)

    def get_page_dimensions(self, page: Optional[Union[IslandPage, str]] = None) -> Tuple[int, int]:
        """Returns the scaled (width, height) in pixels for the specified or current page."""
        target_page = IslandPage.from_str(page) if page is not None else self.current_page
        if target_page == IslandPage.HARDWARE and self.hardware_layout == IslandHardwareLayout.STRIP:
            base_w, base_h = BASE_HARDWARE_STRIP_DIMENSIONS
        elif target_page is not None and target_page in BASE_PAGE_DIMENSIONS:
            base_w, base_h = BASE_PAGE_DIMENSIONS[target_page]
        else:
            base_w, base_h = (400.0, 180.0)

        return self.scale_dimensions(base_w, base_h)

    def get_compact_dimensions(self) -> Tuple[int, int]:
        """Returns the scaled (width, height) in pixels for the compact resting pill."""
        return self.scale_dimensions(
            BASE_COMPACT_PILL_DIMENSIONS[0],
            BASE_COMPACT_PILL_DIMENSIONS[1],
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Preemption Arbitration (Assistant Precedence)
    # ──────────────────────────────────────────────────────────────────────────

    def update_assistant_state(
        self,
        state_name: str,
        active: bool = True,
        payload: Optional[Dict[str, Any]] = None,
        duration: Optional[float] = None,
    ) -> None:
        """
        Updates assistant state. Assistant states (wake, listening, thinking,
        speaking, result, error) take precedence over expanded card pages.
        When assistant finishes or transitions to IDLE, returns to compact pill.
        """
        now = self._clock()
        expires = (now + duration) if (duration is not None and duration > 0) else 0.0
        clean_state = str(state_name).strip().upper()

        if active and clean_state in ASSISTANT_PREEMPTIVE_STATES:
            self._assistant_active = True
            self._assistant_state = clean_state
            self._assistant_payload = payload or {}
            self._assistant_expires_at = expires
        else:
            # Assistant ended or returned to idle/non-preemptive state
            was_active = self._assistant_active
            self._assistant_active = False
            self._assistant_state = clean_state if clean_state in ("IDLE", "NO_SPEECH") else "IDLE"
            self._assistant_payload = {}
            self._assistant_expires_at = 0.0

            # Preemption specification: Return to compact pill when done!
            if was_active or self.is_expanded:
                self.collapse()

    def clear_assistant(self) -> None:
        """Clears assistant preemption and returns island to compact pill."""
        self.update_assistant_state("IDLE", active=False)

    @property
    def is_assistant_active(self) -> bool:
        """Returns True if an assistant state is actively preempting."""
        if not self._assistant_active:
            return False
        if self._assistant_expires_at > 0.0 and self._clock() >= self._assistant_expires_at:
            return False
        return self._assistant_state in ASSISTANT_PREEMPTIVE_STATES

    @property
    def assistant_state(self) -> str:
        """Current preempting assistant state or 'IDLE'."""
        return self._assistant_state if self.is_assistant_active else "IDLE"

    @property
    def assistant_payload(self) -> Dict[str, Any]:
        """Payload associated with current assistant state."""
        return dict(self._assistant_payload) if self.is_assistant_active else {}

    @property
    def is_preempted(self) -> bool:
        """True if expanded cards are currently preempted by assistant states."""
        return self.is_assistant_active

    @property
    def effective_view_mode(self) -> str:
        """
        Arbitrated top-level presentation mode:
        - 'assistant:<state>' when assistant preempts
        - 'card:<page>' when expanded on an enabled page
        - 'compact_pill' when resting or collapsed
        """
        if self.is_assistant_active:
            return f"assistant:{self.assistant_state.lower()}"
        if self.is_expanded and self.current_page is not None:
            return f"card:{self.current_page.value}"
        return "compact_pill"

    @property
    def effective_card_page(self) -> Optional[IslandPage]:
        """Returns the visible card page, or None if preempted by assistant or collapsed."""
        if self.is_assistant_active or not self.is_expanded:
            return None
        return self.current_page

    # ──────────────────────────────────────────────────────────────────────────
    # Polling & Energy Optimization (Zero Poll Waste)
    # ──────────────────────────────────────────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        """Returns True if the island surface is visible to the user."""
        if self.is_hidden:
            return False
        if self.is_fullscreen and self.hide_fullscreen and not self.is_assistant_active:
            return False
        return True

    @property
    def should_poll(self) -> bool:
        """
        Returns True if adapters and background telemetry should poll.
        Strictly pauses all polling when collapsed or hidden to avoid CPU/power waste.
        """
        if not self.is_visible:
            return False
        return self.is_expanded or self.is_assistant_active

    def set_fullscreen(self, is_fs: bool) -> None:
        """Notifies manager of fullscreen application presence."""
        self.is_fullscreen = bool(is_fs)

    def set_hidden(self, hidden: bool) -> None:
        """Explicitly hides or unhides the island."""
        self.is_hidden = bool(hidden)

    # ──────────────────────────────────────────────────────────────────────────
    # Config Integration
    # ──────────────────────────────────────────────────────────────────────────

    def apply_config(self, config: Any) -> None:
        """Applies configuration settings from a Config dataclass or dictionary."""
        if config is None:
            return

        def _get(key: str, default: Any) -> Any:
            if isinstance(config, dict):
                return config.get(key, default)
            return getattr(config, key, default)

        # Pages & Enabled Map
        raw_pages = _get("island_pages", None)
        if raw_pages is not None and isinstance(raw_pages, (list, tuple)):
            self.set_pages(list(raw_pages))

        raw_enabled = _get("island_pages_enabled", None)
        if raw_enabled is not None and isinstance(raw_enabled, dict):
            self.set_pages_enabled(raw_enabled)

        # Layout, Shape, Scale, Trigger
        self.size_scale = IslandSizeScale.from_str(_get("island_size_scale", self.size_scale))
        self.hardware_layout = IslandHardwareLayout.from_str(
            _get("island_hardware_layout", self.hardware_layout)
        )
        self.shape_style = IslandShapeStyle.from_str(_get("island_shape_style", self.shape_style))
        self.expand_trigger = IslandExpandTrigger.from_str(
            _get("island_expand_trigger", self.expand_trigger)
        )

        # Modes & Flags
        self.compact_mode = str(_get("island_compact_mode", self.compact_mode))
        self.time_format = str(_get("island_clock_format", _get("island_time_format", self.time_format)))
        self.first_weekday = str(_get("island_first_weekday", self.first_weekday))
        self.pin_open = bool(_get("island_pin_open", self.pin_open))
        self.hide_fullscreen = bool(_get("island_hide_fullscreen", self.hide_fullscreen))
        self.waveform_color = str(_get("island_waveform_color", self.waveform_color))

    # ──────────────────────────────────────────────────────────────────────────
    # Tracking & Assistant Subsystem Status Helpers (Phase 4)
    # ──────────────────────────────────────────────────────────────────────────

    def toggle_tracking(self) -> bool:
        """Toggles hand tracking state on/off, returning new state."""
        self.tracking_enabled = not self.tracking_enabled
        self.touch_activity()
        return self.tracking_enabled

    def set_tracking(self, enabled: bool) -> bool:
        """Sets hand tracking state explicitly."""
        self.tracking_enabled = bool(enabled)
        self.touch_activity()
        return self.tracking_enabled

    def get_assistant_status(self) -> Dict[str, Any]:
        """Returns assistant status including tracking, mic, and camera states."""
        return {
            "tracking": "active" if self.tracking_enabled else "paused",
            "tracking_enabled": self.tracking_enabled,
            "mic": self.mic_state,
            "camera": self.camera_state,
            "is_expanded": self.is_expanded,
            "current_page": self.current_page.value if self.current_page else "none",
            "pin_open": self.pin_open,
        }

    def set_assistant_status(
        self,
        tracking: Optional[bool] = None,
        mic: Optional[str] = None,
        camera: Optional[str] = None,
    ) -> None:
        """Updates assistant subsystem statuses."""
        if tracking is not None:
            self.tracking_enabled = bool(tracking)
        if mic is not None:
            self.mic_state = str(mic)
        if camera is not None:
            self.camera_state = str(camera)

    def get_last_command(self) -> Dict[str, Any]:
        """Returns the most recent command execution details."""
        return {
            "text": self._last_command_text,
            "result": self._last_command_result,
            "timestamp": self._last_command_timestamp,
        }

    def set_last_command(self, text: str, result: str) -> None:
        """Records the most recent command text and result."""
        self._last_command_text = str(text)
        self._last_command_result = str(result)
        self._last_command_timestamp = self._clock()


# Global singleton island page manager
_ISLAND_PAGE_MANAGER: Optional[IslandPageManager] = None


def get_island_page_manager() -> IslandPageManager:
    """Returns singleton IslandPageManager instance."""
    global _ISLAND_PAGE_MANAGER
    if _ISLAND_PAGE_MANAGER is None:
        _ISLAND_PAGE_MANAGER = IslandPageManager()
    return _ISLAND_PAGE_MANAGER


def set_island_page_manager(manager: Optional[IslandPageManager]) -> None:
    """Sets or overrides singleton IslandPageManager instance."""
    global _ISLAND_PAGE_MANAGER
    _ISLAND_PAGE_MANAGER = manager

