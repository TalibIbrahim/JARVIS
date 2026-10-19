"""ui/island_parts/common.py - shared constants and helpers for the island renderers.

Re-exported by ui.island so `from ui.island import IslandState, _SIZE, ...` keeps working.
"""

from __future__ import annotations

from enum import auto, Enum

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainterPath

from ui.components import create_squircle_path

# ──────────────────────────────────────────────────────────────────────────────
# Squircle Path Cache (60 FPS Performance, Zero Heap Allocations)
# ──────────────────────────────────────────────────────────────────────────────

_PATH_CACHE: dict[tuple[float, float, float, float, float], QPainterPath] = {}

def create_cached_squircle_path(rect: QRectF, radius: float) -> QPainterPath:
    """Returns a cached QPainterPath for continuous squircle geometry."""
    key = (
        round(rect.x(), 1),
        round(rect.y(), 1),
        round(rect.width(), 1),
        round(rect.height(), 1),
        round(radius, 1)
    )
    path = _PATH_CACHE.get(key)
    if path is None:
        if len(_PATH_CACHE) > 512:
            _PATH_CACHE.clear()
        path = create_squircle_path(rect, radius)
        _PATH_CACHE[key] = path
    return path


# ──────────────────────────────────────────────────────────────────────────────
# State enum
# ──────────────────────────────────────────────────────────────────────────────

class IslandState(Enum):
    IDLE          = auto()
    WAKE          = auto()
    TRACKING      = auto()
    GESTURE       = auto()
    LISTENING     = auto()
    THINKING      = auto()
    PROMPT        = auto()
    SPEAKING      = auto()
    RESULT        = auto()
    NO_SPEECH     = auto()
    ERROR         = auto()
    # Expanded Cards
    CARD_MEDIA          = auto()
    CARD_WEATHER        = auto()
    CARD_TIME           = auto()
    CARD_HARDWARE_GRID  = auto()
    CARD_HARDWARE_STRIP = auto()
    CARD_JARVIS         = auto()
    CARD_DISAMBIGUATE   = auto()
    CARD_TIMER          = auto()
    CARD_SENDING        = auto()
    CARD_RESULT         = auto()
    # Gesture HUD States
    HUD_VOLUME          = auto()
    HUD_WINDOW          = auto()


# ──────────────────────────────────────────────────────────────────────────────
# Target sizes per state (w, h)
# ──────────────────────────────────────────────────────────────────────────────

_SIZE: dict[IslandState, tuple[float, float]] = {
    IslandState.IDLE:                (175.0,  36.0),
    IslandState.WAKE:                (280.0,  44.0),
    IslandState.TRACKING:            (210.0,  36.0),
    IslandState.GESTURE:             (270.0,  40.0),
    IslandState.LISTENING:           (390.0,  54.0),
    IslandState.THINKING:            (320.0,  46.0),
    IslandState.PROMPT:              (360.0,  48.0),   # dynamically widened if needed
    IslandState.SPEAKING:            (380.0,  50.0),   # dynamically widened if needed
    IslandState.RESULT:              (380.0,  58.0),   # dynamically widened if needed
    IslandState.NO_SPEECH:           (175.0,  36.0),
    IslandState.ERROR:               (340.0,  48.0),
    IslandState.CARD_MEDIA:          (390.0, 188.0),
    IslandState.CARD_WEATHER:        (390.0, 188.0),
    IslandState.CARD_TIME:           (390.0, 188.0),
    IslandState.CARD_HARDWARE_GRID:  (390.0, 188.0),
    IslandState.CARD_HARDWARE_STRIP: (446.0,  69.0),
    IslandState.CARD_JARVIS:         (390.0, 188.0),
    IslandState.CARD_DISAMBIGUATE:   (440.0,  90.0),
    IslandState.CARD_TIMER:          (380.0, 150.0),
    IslandState.CARD_SENDING:        (300.0,  44.0),
    IslandState.CARD_RESULT:         (380.0,  58.0),
    IslandState.HUD_VOLUME:          (260.0,  38.0),
    IslandState.HUD_WINDOW:          (290.0,  38.0),
}


# Apple-grade spring constants:
# response ~0.4s: omega = 2*pi/0.4 = 15.71 -> k = omega^2 = 246.74
# damping ~0.7:   c = 2 * 0.7 * omega = 21.99
SPRING_K_RESP04: float = 246.74
SPRING_C_DAMP07: float = 22.00
