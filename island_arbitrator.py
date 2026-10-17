"""
island_arbitrator.py — J.A.R.V.I.S. Dynamic Island Content Arbitration Logic.

Evaluates competing content sources and determines the active widget state
and payload for the Apple HIG Dynamic Island capsule.

Priority Hierarchy:
  1. Assistant States (Wake, Listening, Thinking, Speaking, Result, Tracking, Prompt, Error)
  2. Manual User Override ("show weather", "show media")
  3. Active Timer / Countdown
  4. Media Playback (Playing or Paused <= 30s)
  5. Time + Weather (or Time-only)
  6. Plain Idle Pill (Standby 110x30 capsule)

Rules:
  - If media pauses for > 30s, arbitrator drops media priority and falls back to
    time+weather or idle pill.
  - When fullscreen app or game is active and hide_fullscreen is True, island is hidden
    unless an assistant state is actively triggered by the user.
  - Pure state evaluation behind interfaces: 100% unit-testable with zero Qt imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Dict, Optional


class IslandWidgetType(Enum):
    HIDDEN = "hidden"
    ASSISTANT = "assistant"
    OVERRIDE = "override"
    TIMER = "timer"
    MEDIA = "media"
    TIME_WEATHER = "time_weather"
    TIME = "time"
    IDLE = "idle"


# Content Priorities (Higher number = higher precedence)
PRIORITY_ASSISTANT = 50
PRIORITY_OVERRIDE = 45
PRIORITY_TIMER = 40
PRIORITY_MEDIA = 30
PRIORITY_IDLE_WIDGET = 20
PRIORITY_IDLE = 10
PRIORITY_HIDDEN = 0

MEDIA_PAUSE_GRACE_PERIOD_SEC = 30.0


@dataclass
class AssistantInput:
    state_name: str = "IDLE"  # "WAKE", "LISTENING", "THINKING", "SPEAKING", "RESULT", etc.
    active: bool = False
    payload: Dict[str, Any] = field(default_factory=dict)
    expires_at: float = 0.0


@dataclass
class TimerInput:
    is_active: bool = False
    remaining_seconds: float = 0.0
    total_seconds: float = 0.0
    label: str = ""
    is_paused: bool = False
    has_finished: bool = False


@dataclass
class MediaInput:
    is_playing: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    position: float = 0.0
    paused_at: Optional[float] = None
    last_active_time: float = 0.0
    source_app_id: str = ""
    thumbnail_bytes: Optional[bytes] = None


@dataclass
class WeatherInput:
    temperature: float = 0.0
    apparent_temperature: float = 0.0
    relative_humidity: int = 0
    condition: str = "sunny"
    description: str = ""
    city: str = ""
    is_day: bool = True
    unit_symbol: str = "°C"
    stale: bool = False
    is_available: bool = True


@dataclass
class ArbitratorConfig:
    idle_display: str = "time_weather"  # "off", "time", "time_weather"
    show_now_playing: bool = True
    show_weather: bool = True
    hide_fullscreen: bool = True
    time_format: str = "auto"           # "auto", "12h", "24h"
    allow_assistant_in_fullscreen: bool = True


@dataclass
class ArbitrationInputs:
    current_time: float = field(default_factory=time.time)
    assistant: AssistantInput = field(default_factory=AssistantInput)
    timer: TimerInput = field(default_factory=TimerInput)
    media: MediaInput = field(default_factory=MediaInput)
    weather: WeatherInput = field(default_factory=WeatherInput)
    config: ArbitratorConfig = field(default_factory=ArbitratorConfig)
    is_fullscreen: bool = False
    widgets_hidden: bool = False
    override_widget: Optional[IslandWidgetType] = None
    override_expires_at: float = 0.0
    override_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ArbitrationResult:
    widget_type: IslandWidgetType
    payload: Dict[str, Any]
    priority: int
    reason: str = ""


def arbitrate_content(inputs: ArbitrationInputs) -> ArbitrationResult:
    """
    Pure state evaluation returning the active dynamic island widget type and payload.
    Deterministic, thread-safe, and zero external dependencies.
    """
    now = inputs.current_time

    # 1. Fullscreen Gate: If fullscreen app/game active and config says hide
    if inputs.is_fullscreen and inputs.config.hide_fullscreen:
        # Check if an assistant state is active and permitted
        if (
            inputs.config.allow_assistant_in_fullscreen
            and inputs.assistant.active
            and (inputs.assistant.expires_at == 0.0 or inputs.assistant.expires_at > now)
            and inputs.assistant.state_name.upper() not in ("IDLE", "NO_SPEECH")
        ):
            return ArbitrationResult(
                widget_type=IslandWidgetType.ASSISTANT,
                payload={"state": inputs.assistant.state_name, **inputs.assistant.payload},
                priority=PRIORITY_ASSISTANT,
                reason="Assistant active during fullscreen",
            )
        return ArbitrationResult(
            widget_type=IslandWidgetType.HIDDEN,
            payload={"reason": "fullscreen_active"},
            priority=PRIORITY_HIDDEN,
            reason="Fullscreen application active",
        )

    # 2. Assistant States: Highest priority during interaction
    if inputs.assistant.active:
        is_expired = inputs.assistant.expires_at > 0.0 and inputs.assistant.expires_at <= now
        if not is_expired and inputs.assistant.state_name.upper() not in ("IDLE", ""):
            return ArbitrationResult(
                widget_type=IslandWidgetType.ASSISTANT,
                payload={"state": inputs.assistant.state_name, **inputs.assistant.payload},
                priority=PRIORITY_ASSISTANT,
                reason=f"Assistant state '{inputs.assistant.state_name}' active",
            )

    # 3. Manual User Override (e.g. "show weather", "show media")
    if inputs.override_widget and inputs.override_expires_at > now:
        return ArbitrationResult(
            widget_type=inputs.override_widget,
            payload=dict(inputs.override_payload),
            priority=PRIORITY_OVERRIDE,
            reason=f"Manual override '{inputs.override_widget.value}' active",
        )

    # If user explicitly hid island widgets, skip timer, media, and time_weather
    if inputs.widgets_hidden:
        return ArbitrationResult(
            widget_type=IslandWidgetType.IDLE,
            payload={},
            priority=PRIORITY_IDLE,
            reason="Island widgets explicitly hidden by user",
        )

    # 4. Active Timer
    if inputs.timer.is_active or inputs.timer.has_finished:
        return ArbitrationResult(
            widget_type=IslandWidgetType.TIMER,
            payload={
                "remaining_seconds": inputs.timer.remaining_seconds,
                "total_seconds": inputs.timer.total_seconds,
                "label": inputs.timer.label,
                "is_paused": inputs.timer.is_paused,
                "has_finished": inputs.timer.has_finished,
            },
            priority=PRIORITY_TIMER,
            reason="Active countdown/timer running",
        )

    # 5. Media Playback (Playing or Paused <= 30s)
    if inputs.config.show_now_playing and bool(inputs.media.title or inputs.media.artist):
        if inputs.media.is_playing:
            return ArbitrationResult(
                widget_type=IslandWidgetType.MEDIA,
                payload={
                    "is_playing": True,
                    "title": inputs.media.title,
                    "artist": inputs.media.artist,
                    "album": inputs.media.album,
                    "duration": inputs.media.duration,
                    "position": inputs.media.position,
                    "source_app_id": inputs.media.source_app_id,
                    "thumbnail_bytes": inputs.media.thumbnail_bytes,
                },
                priority=PRIORITY_MEDIA,
                reason="Media currently playing",
            )
        else:
            # Check 30-second pause grace period
            pause_timestamp = inputs.media.paused_at or inputs.media.last_active_time
            time_paused = (now - pause_timestamp) if pause_timestamp > 0 else float("inf")
            if time_paused <= MEDIA_PAUSE_GRACE_PERIOD_SEC:
                return ArbitrationResult(
                    widget_type=IslandWidgetType.MEDIA,
                    payload={
                        "is_playing": False,
                        "title": inputs.media.title,
                        "artist": inputs.media.artist,
                        "album": inputs.media.album,
                        "duration": inputs.media.duration,
                        "position": inputs.media.position,
                        "source_app_id": inputs.media.source_app_id,
                        "thumbnail_bytes": inputs.media.thumbnail_bytes,
                        "time_paused": time_paused,
                    },
                    priority=PRIORITY_MEDIA,
                    reason=f"Media paused ({time_paused:.1f}s <= {MEDIA_PAUSE_GRACE_PERIOD_SEC}s grace)",
                )
            # Otherwise: media pause exceeded 30s -> drops media priority and falls through!

    # 6. Idle Display: Time + Weather / Time / Plain Idle
    idle_mode = inputs.config.idle_display.lower()

    if idle_mode == "time_weather":
        if inputs.config.show_weather and inputs.weather.is_available:
            return ArbitrationResult(
                widget_type=IslandWidgetType.TIME_WEATHER,
                payload={
                    "temperature": inputs.weather.temperature,
                    "apparent_temperature": inputs.weather.apparent_temperature,
                    "relative_humidity": inputs.weather.relative_humidity,
                    "condition": inputs.weather.condition,
                    "description": inputs.weather.description,
                    "city": inputs.weather.city,
                    "is_day": inputs.weather.is_day,
                    "unit_symbol": inputs.weather.unit_symbol,
                    "stale": inputs.weather.stale,
                    "time_format": inputs.config.time_format,
                },
                priority=PRIORITY_IDLE_WIDGET,
                reason="Idle display: Time and Weather",
            )
        else:
            # Weather not available or disabled: fall back to time only
            return ArbitrationResult(
                widget_type=IslandWidgetType.TIME,
                payload={"time_format": inputs.config.time_format},
                priority=PRIORITY_IDLE_WIDGET,
                reason="Idle display: Time only (weather unavailable or disabled)",
            )

    elif idle_mode == "time":
        return ArbitrationResult(
            widget_type=IslandWidgetType.TIME,
            payload={"time_format": inputs.config.time_format},
            priority=PRIORITY_IDLE_WIDGET,
            reason="Idle display: Time only configured",
        )

    # 7. Fallback: Plain Idle Pill (110x30 capsule)
    return ArbitrationResult(
        widget_type=IslandWidgetType.IDLE,
        payload={},
        priority=PRIORITY_IDLE,
        reason="Plain idle standby pill",
    )


class IslandArbitrator:
    """
    Stateful arbitrator managing dynamic island content streams.
    Thread-safe and decoupled from UI or hardware.
    """

    def __init__(self, config: Optional[ArbitratorConfig] = None, clock_fn=time.time):
        self._config = config or ArbitratorConfig()
        self._clock = clock_fn
        self._assistant = AssistantInput()
        self._timer = TimerInput()
        self._media = MediaInput()
        self._weather = WeatherInput()
        self._is_fullscreen = False
        self._widgets_hidden = False
        self._override_widget: Optional[IslandWidgetType] = None
        self._override_expires_at: float = 0.0
        self._override_payload: Dict[str, Any] = {}

    @property
    def config(self) -> ArbitratorConfig:
        return self._config

    @config.setter
    def config(self, cfg: ArbitratorConfig):
        self._config = cfg

    def update_assistant(
        self,
        state_name: str,
        active: bool = True,
        payload: Optional[Dict[str, Any]] = None,
        duration: Optional[float] = None,
    ):
        now = self._clock()
        expires = (now + duration) if (duration is not None and duration > 0) else 0.0
        self._assistant = AssistantInput(
            state_name=state_name,
            active=active,
            payload=payload or {},
            expires_at=expires,
        )

    def clear_assistant(self):
        self._assistant = AssistantInput(state_name="IDLE", active=False)

    def update_timer(
        self,
        is_active: bool,
        remaining_seconds: float = 0.0,
        total_seconds: float = 0.0,
        label: str = "",
        is_paused: bool = False,
        has_finished: bool = False,
    ):
        self._timer = TimerInput(
            is_active=is_active,
            remaining_seconds=remaining_seconds,
            total_seconds=total_seconds,
            label=label,
            is_paused=is_paused,
            has_finished=has_finished,
        )

    def update_media(
        self,
        is_playing: bool,
        title: str = "",
        artist: str = "",
        album: str = "",
        duration: float = 0.0,
        position: float = 0.0,
        source_app_id: str = "",
        thumbnail_bytes: Optional[bytes] = None,
    ):
        now = self._clock()
        paused_at = self._media.paused_at
        if self._media.is_playing and not is_playing:
            paused_at = now
        elif is_playing:
            paused_at = None

        self._media = MediaInput(
            is_playing=is_playing,
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            position=position,
            paused_at=paused_at,
            last_active_time=now if is_playing else (paused_at or self._media.last_active_time),
            source_app_id=source_app_id,
            thumbnail_bytes=thumbnail_bytes,
        )

    def update_weather(
        self,
        temperature: float,
        condition: str,
        description: str = "",
        city: str = "",
        apparent_temperature: float = 0.0,
        relative_humidity: int = 0,
        is_day: bool = True,
        unit_symbol: str = "°C",
        stale: bool = False,
        is_available: bool = True,
    ):
        self._weather = WeatherInput(
            temperature=temperature,
            apparent_temperature=apparent_temperature,
            relative_humidity=relative_humidity,
            condition=condition,
            description=description,
            city=city,
            is_day=is_day,
            unit_symbol=unit_symbol,
            stale=stale,
            is_available=is_available,
        )

    def set_fullscreen(self, is_fullscreen: bool):
        self._is_fullscreen = is_fullscreen

    def set_widgets_hidden(self, hidden: bool):
        self._widgets_hidden = hidden

    def toggle_widgets_hidden(self) -> bool:
        self._widgets_hidden = not self._widgets_hidden
        return self._widgets_hidden

    def set_manual_override(
        self,
        widget_type: IslandWidgetType,
        duration_sec: float = 5.0,
        payload: Optional[Dict[str, Any]] = None,
    ):
        now = self._clock()
        self._override_widget = widget_type
        self._override_expires_at = now + duration_sec
        self._override_payload = payload or {}

    def clear_manual_override(self):
        self._override_widget = None
        self._override_expires_at = 0.0
        self._override_payload = {}

    def evaluate(self, now: Optional[float] = None) -> ArbitrationResult:
        current_time = self._clock() if now is None else now
        inputs = ArbitrationInputs(
            current_time=current_time,
            assistant=self._assistant,
            timer=self._timer,
            media=self._media,
            weather=self._weather,
            config=self._config,
            is_fullscreen=self._is_fullscreen,
            widgets_hidden=self._widgets_hidden,
            override_widget=self._override_widget,
            override_expires_at=self._override_expires_at,
            override_payload=self._override_payload,
        )
        return arbitrate_content(inputs)


# Module-level singleton instance
_ISLAND_ARBITRATOR: Optional[IslandArbitrator] = None


def get_island_arbitrator(config: Optional[ArbitratorConfig] = None) -> IslandArbitrator:
    global _ISLAND_ARBITRATOR
    if _ISLAND_ARBITRATOR is None:
        _ISLAND_ARBITRATOR = IslandArbitrator(config=config)
    return _ISLAND_ARBITRATOR


def set_island_arbitrator(arbitrator: Optional[IslandArbitrator]) -> None:
    global _ISLAND_ARBITRATOR
    _ISLAND_ARBITRATOR = arbitrator
