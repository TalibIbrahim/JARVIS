"""Tunable settings. Override any value by creating jarvis_config.json next to main.py."""
import json
import os
import shutil
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional


@dataclass
class Config:
    # --- camera ---
    # --- camera & tracking quality ---
    camera_index: int = 0
    frame_width: int = 1280
    frame_height: int = 720
    camera_fps: int = 30
    camera_mjpg: bool = True
    threaded_capture: bool = True
    model_complexity: int = 1           # 0 = lightweight, 1 = higher accuracy
    min_detection_confidence: float = 0.6
    min_tracking_confidence: float = 0.6
    show_preview: bool = False          # HUD window with the tracked skeleton (or run with --preview)

    # --- control scheme ---
    control_scheme: str = "palm_fist"   # "palm_fist" (new default) or "pinch"
    palm_anchor: str = "knuckles"       # "knuckles" (MCPs 5,9,13,17), "palm_center" (0 & 9), "centroid" (0,5,9,13,17), "index_mcp" (5)

    # --- palm & fist pose classification ---
    open_enter_thresh: float = 0.70     # openness score above this = OPEN PALM
    open_exit_thresh: float = 0.58      # openness drops below this = exits OPEN (enters NEUTRAL)
    fist_enter_thresh: float = 0.28     # openness below this = FIST
    fist_exit_thresh: float = 0.38      # openness above this = exits FIST
    fist_click_max_hold: float = 0.40   # fist closed & reopened within this duration = click
    fist_double_window: float = 0.65    # two quick fists inside this window = double click
    fist_drag_min_hold: float = 0.35    # fist held longer than this becomes a drag
    reopen_freeze_ms: float = 150.0     # cursor stays frozen this long after reopening
    thumbs_up_hold: float = 1.2         # hold thumbs-up this long while awake to sleep
    pose_debounce_frames: int = 2       # frames required to confirm pose transition

    # --- tracking box & anchor ---
    # Fraction of the camera frame ignored on every edge (0.0 = 100% full camera frame tracking).
    box_margin: float = 0.0
    anchor_point: str = "blend"         # used when in pinch scheme: "blend", "tip", "pip", "mcp", "palm_offset"
    anchor_blend_weight: float = 0.65   # blend ratio: 65% tip + 35% PIP

    # --- pointer mapping & precision ---
    tracking_mode: str = "relative"     # "relative" (trackpad-style) or "absolute"
    pointer_speed: float = 1.15         # pointer speed multiplier
    accel_gain: float = 1.8             # pointer acceleration curve gain
    accel_threshold: float = 0.035      # hand velocity threshold for acceleration
    precision_mode: bool = True         # magnetic slow-down during fine aiming
    precision_gain: float = 0.35        # gain multiplier during fine aim
    precision_threshold: float = 0.015  # velocity threshold for precision mode
    deadzone_px: float = 1.5            # suppresses resting micro-jitter below this threshold (pixels)
    clutch_enabled: bool = True         # pause pose / leaving margin box stops cursor

    # --- gesture timing / thresholds (distances are relative to hand size) ---
    activate_hold: float = 0.5          # seconds of open palm to wake up
    deactivate_hold: float = 1.5        # seconds of open palm to go to standby
    palm_grace: float = 0.35            # grace period to absorb multi-frame landmark flicker during palm hold
    pinch_down: float = 0.28            # thumb-index distance that counts as a pinch
    pinch_up: float = 0.42              # distance at which the pinch is released (hysteresis)
    pinch_freeze: float = 0.55          # cursor freezes while fingers close, so clicks don't drift
    drag_hold: float = 0.35             # pinch held longer than this becomes a drag
    double_click_window: float = 0.45   # second quick pinch inside this window = double click
    zoom_step: float = 0.035            # change in distance between index tips per zoom notch
    zoom_enter_delay: float = 0.25      # both hands must be visible this long before zoom starts
    scroll_step: float = 0.025          # vertical hand travel per scroll notch (peace sign)
    scroll_invert: bool = False
    lost_grace: float = 0.4             # tolerate this long without a hand before releasing everything

    # --- gesture shortcuts (phase 8) ---
    gesture_shortcuts_enabled: bool = True  # enable vertical slide & horizontal swipe shortcuts
    gesture_slide_volume: bool = True      # vertical slide adjusts volume (+/- 5%)
    gesture_swipe_switch: bool = True      # horizontal swipe switches windows (Alt+Tab)

    # --- cursor smoothing & decoupling ---
    smooth_min_cutoff: float = 0.6      # lower = smoother when slow, higher = more responsive
    smooth_beta: float = 0.02           # higher = less lag during fast moves
    smooth_d_cutoff: float = 1.0
    cursor_rate_hz: int = 120           # decoupled cursor update rate (120+ Hz)
    cursor_lookback_ms: float = 200.0   # position lookback buffer to eliminate click drift

    # --- dwell click ---
    dwell_mode: str = "click"           # "off", "click", "radial"
    dwell_time: float = 1.0             # seconds of stationarity required to trigger
    dwell_radius: float = 14.0          # radius in screen pixels allowed during dwell
    dwell_repeat_delay: float = 0.8     # must leave radius before next dwell

    # --- voice ---
    voice_enabled: bool = True
    speak: bool = True                  # spoken replies
    tts_engine: str = "piper"           # "piper" (Paul Bettany MCU) or "system" (pyttsx3 SAPI5)
    tts_model_path: str = "models/voice/jarvis-medium.onnx" # local Piper voice model
    wake_word: str = "jarvis"
    wake_word_enabled: bool = True      # openWakeWord / wake word detection toggle
    wake_sensitivity: str = "balanced"  # "low", "balanced", "high"
    wake_followup_window: float = 6.0   # seconds Jarvis keeps listening after wake detection
    chime_enabled: bool = True          # play soft ascending two-note chime on wake
    wake_aliases: list = field(default_factory=lambda: [
        "jarvis", "hey jarvis", "ok jarvis", "okay jarvis", "hi jarvis", "hello jarvis",
        "yo jarvis", "jarvis please", "jarvas", "jervis", "gervais", "javis", "jarviz",
        "jarves", "jarv", "travis", "service", "harvest", "charvis", "darvis", "jar is",
        "jar business"
    ])
    command_window: float = 7.0         # seconds Jarvis keeps listening after "Jarvis?"
    mic_device_index: int = None        # specific microphone device index, or None for auto-detect
    mic_energy_threshold: int = 300     # minimum audio energy threshold for speech trigger (lower = more sensitive)
    mic_dynamic_energy: bool = False    # adapt threshold dynamically (False prevents runaway threshold on laptop mics)
    mic_pause_threshold: float = 1.0    # seconds of silence before finishing phrase (prevents cutting off mid-sentence)
    mic_phrase_time_limit: float = 10.0 # maximum seconds per voice command phrase
    voice_profile_enabled: bool = True  # enable speaker recognition and voice profile
    voice_speaker_verification: bool = False # strict speaker verification (filters out other voices when trained)
    debug_log_transcripts: bool = False  # write full transcripts to the diagnostic log (default: only lengths)

    # --- ui / apple hig appearance ---
    theme_mode: str = "auto"            # "auto" (follows Windows), "dark", "light"
    glass_blur: bool = True             # Windows 11 Acrylic / Windows 10 blur behind
    reduce_motion: bool = False         # reduce UI animations
    show_skeleton_mini: bool = True     # mini skeleton preview inside dynamic island
    ui_status_line_enabled: bool = True  # Subtle status line below thinking glyph for extended operations
    ui_cancel_hint_enabled: bool = True  # Cancellation hint after 3.0s continuous listening/thinking
    ui_progressive_shimmer_enabled: bool = True  # Shimmer highlight across streaming tokens
    show_health_panel: bool = True      # Diagnostic health card in Settings About tab

    # --- dynamic island & widgets ---
    idle_display: str = "time_weather"  # "off", "time", "time_weather"
    show_now_playing: bool = True       # display media now playing widget in island
    show_weather: bool = True           # display weather information in island
    weather_city: str = "New York"      # default location name
    weather_lat: float = 40.7128        # default latitude
    weather_lon: float = -74.0060       # default longitude
    weather_units: str = "auto"         # "auto", "celsius", "fahrenheit"
    time_format: str = "auto"           # "auto", "12h", "24h"
    expand_trigger: str = "hover"       # "hover", "dwell", "click"
    hide_fullscreen: bool = True        # hide island when fullscreen app or game active
    island_size: str = "default"        # "compact", "default", "large"

    # --- dynamic island pages & settings (Phase 1) ---
    island_pages: list[str] = field(default_factory=lambda: ["media", "calendar", "weather", "hardware", "jarvis"])
    island_pages_enabled: dict[str, bool] = field(default_factory=lambda: {
        "media": True,
        "calendar": True,
        "weather": True,
        "hardware": True,
        "jarvis": True,
    })
    island_compact_mode: str = "time_weather"       # "time_weather" or "media_aware"
    island_time_format: str = "auto"               # "auto", "12h", "24h"
    island_first_weekday: str = "locale"           # "locale", "sunday", "monday"
    island_hardware_layout: str = "grid"           # "grid", "strip"
    island_size_scale: str = "medium"              # "medium", "small", "large"
    island_shape_style: str = "pill"               # "pill", "notch"
    island_backdrop_blur: bool = False             # backdrop acrylic/blur
    island_backdrop_opacity: float = 0.95          # background opacity (0.0 - 1.0)
    island_pin_open: bool = False                  # suppress auto-collapse when pinned
    island_expand_trigger: str = "hover"           # "hover", "click", "dwell", "fist", "voice"
    island_hide_fullscreen: bool = True            # hide island when fullscreen app active
    island_waveform_color: str = "blue"            # "blue" (default: blue only), "cyan", "green", "orange", "purple", "red", "pink", "monochrome", "multicolor", or hex "#RRGGBB"

    # --- updates ---
    auto_update_check: bool = True      # automatically check for GitHub releases on startup

    # --- ai / groq intelligence ---
    ai_enabled: bool = False
    ai_provider: str = "groq"
    ai_llm_model: str = "openai/gpt-oss-120b"
    ai_stt_model: str = "whisper-large-v3-turbo"
    ai_vision_model: str = "qwen/qwen3.8-27b"
    ai_vision_enabled: bool = True
    ai_timeout: int = 10
    ai_max_history: int = 6
    groq_api_key: str = ""              # optional saved Groq API key persisted in config file
    local_decision_engine: bool = True  # ultra-fast local semantic intent matcher (System 1 reflex)

    # --- files & folders management (phase 4) ---
    folders_whitelist: dict = field(default_factory=lambda: {
        "programming": "C:\\Programming" if os.path.exists("C:\\Programming") else os.path.join(os.path.expanduser("~"), "Programming"),
        "downloads": os.path.join(os.path.expanduser("~"), "Downloads"),
        "documents": os.path.join(os.path.expanduser("~"), "Documents"),
        "desktop": os.path.join(os.path.expanduser("~"), "Desktop"),
        "pictures": os.path.join(os.path.expanduser("~"), "Pictures"),
        "music": os.path.join(os.path.expanduser("~"), "Music"),
        "videos": os.path.join(os.path.expanduser("~"), "Videos"),
    })
    indexed_roots: list = field(default_factory=lambda: [
        "C:\\Programming" if os.path.exists("C:\\Programming") else os.path.join(os.path.expanduser("~"), "Programming"),
        os.path.join(os.path.expanduser("~"), "Documents"),
        os.path.join(os.path.expanduser("~"), "Downloads"),
        os.path.join(os.path.expanduser("~"), "Desktop"),
    ])
    # --- productivity, notes & routines (phase 6) ---
    notes_file_path: str = field(default_factory=lambda: os.path.join(os.path.expanduser("~"), "Documents", "Jarvis_Notes.md"))
    scheduler_file_path: str = ""
    routines: dict = field(default_factory=lambda: {
        "work mode": [
            {"action": "open_app", "args": {"app": "code"}},
            {"action": "set_volume", "args": {"level": 30}},
        ],
        "focus mode": [
            {"action": "toggle_do_not_disturb", "args": {}},
            {"action": "set_volume", "args": {"level": 15}},
        ],
        "evening mode": [
            {"action": "toggle_night_light", "args": {}},
            {"action": "set_brightness", "args": {"level": 30}},
            {"action": "set_volume", "args": {"level": 20}},
        ],
    })

    def to_dict(self) -> Dict[str, Any]:
        """Returns serializable dictionary of all configuration fields."""
        result: Dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if isinstance(val, (str, int, float, bool, list, dict)) or val is None:
                result[f.name] = val
            else:
                result[f.name] = str(val)
        return result

    def update(self, **kwargs) -> None:
        """Dynamically updates configuration fields with safe type coercion."""
        field_map = {f.name: f for f in fields(self)}
        for key, value in kwargs.items():
            if key in field_map:
                default_val = getattr(self, key)
                coerced = _coerce_field_value(key, value, default_val)
                setattr(self, key, coerced)



def get_config_path(explicit_path=None):
    r"""
    Returns persistent config file path.
    Uses %APPDATA%\Jarvis\jarvis_config.json when running compiled or when APPDATA exists,
    falling back to local jarvis_config.json next to source files.
    """
    import sys
    if explicit_path:
        return explicit_path
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_config.json")
    if not getattr(sys, "frozen", False) and os.path.exists(local_path):
        return local_path
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    jarvis_dir = os.path.join(appdata, "Jarvis")
    try:
        os.makedirs(jarvis_dir, exist_ok=True)
    except Exception:
        pass
    appdata_path = os.path.join(jarvis_dir, "jarvis_config.json")
    if os.path.exists(appdata_path) or getattr(sys, "frozen", False):
        return appdata_path
    return local_path


def _atomic_write_json(file_path: str, data: dict) -> bool:
    """
    Atomically writes data to JSON file:
    1. Writes to file_path.tmp
    2. Calls flush() and os.fsync()
    3. Keeps backup in file_path.bak
    4. Atomically replaces file_path using os.replace()
    """
    try:
        dir_name = os.path.dirname(os.path.abspath(file_path))
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        tmp_path = f"{file_path}.tmp"
        bak_path = f"{file_path}.bak"

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())

        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            try:
                shutil.copy2(file_path, bak_path)
            except Exception:
                pass

        os.replace(tmp_path, file_path)
        return True
    except Exception as exc:
        print(f"[config] atomic write error for {file_path}: {exc}")
        return False


def _coerce_field_value(field_name: str, value: Any, default_val: Any) -> Any:
    """Safely coerces or falls back to default if value is wrong type."""
    if value is None:
        return default_val if default_val is not None else None

    if isinstance(default_val, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() in ("true", "1", "yes", "on"):
                return True
            if value.lower() in ("false", "0", "no", "off"):
                return False
        return default_val

    if isinstance(default_val, int) and not isinstance(default_val, bool):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default_val

    if isinstance(default_val, float):
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            return float(value)
        try:
            return float(value)
        except (ValueError, TypeError):
            return default_val

    if isinstance(default_val, str):
        if isinstance(value, str):
            return value
        return str(value)

    if isinstance(default_val, list):
        if isinstance(value, (list, tuple)):
            return list(value)
        return default_val

    if isinstance(default_val, dict):
        if isinstance(value, dict):
            return dict(value)
        return default_val

    return value


def load_config(path=None):
    """
    Loads configuration from JSON file.
    Validates JSON integrity, enforces strict type safety with safe defaults,
    and automatically repairs file or recovers from .bak backup.
    """
    cfg = Config()
    config_path = get_config_path(path)
    bak_path = f"{config_path}.bak"
    data = None
    needs_repair = False

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                needs_repair = True
                data = None
        except Exception as exc:
            needs_repair = True
            print(f"[config] corrupted config at {config_path}: {exc}. Attempting recovery from backup...")
            if os.path.exists(bak_path):
                try:
                    with open(bak_path, "r", encoding="utf-8") as f_bak:
                        data = json.load(f_bak)
                    if isinstance(data, dict):
                        print(f"[config] successfully restored config from {bak_path}")
                        _atomic_write_json(config_path, data)
                        needs_repair = False
                    else:
                        data = None
                except Exception as bak_exc:
                    print(f"[config] backup at {bak_path} also corrupted: {bak_exc}")
                    data = None
            else:
                print(f"[config] no backup found at {bak_path}, falling back to defaults")
    elif os.path.exists(bak_path):
        try:
            with open(bak_path, "r", encoding="utf-8") as f_bak:
                data = json.load(f_bak)
            if isinstance(data, dict):
                _atomic_write_json(config_path, data)
            else:
                data = None
                needs_repair = True
        except Exception:
            needs_repair = True

    type_repaired = False
    if isinstance(data, dict):
        field_map = {f.name: f for f in fields(Config)}
        for key, value in data.items():
            if key in field_map:
                default_val = getattr(cfg, key)
                coerced = _coerce_field_value(key, value, default_val)
                if coerced != value:
                    type_repaired = True
                setattr(cfg, key, coerced)
    else:
        needs_repair = True

    if needs_repair or type_repaired:
        try:
            save_config(cfg, path=config_path)
        except Exception:
            pass

    return cfg


def save_config(cfg: Config, path=None) -> bool:
    """Persists current Config fields to jarvis_config.json atomically."""
    config_path = get_config_path(path)
    try:
        existing = {}
        if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        for f in fields(Config):
            val = getattr(cfg, f.name)
            if isinstance(val, (str, int, float, bool, list, dict)) or val is None:
                existing[f.name] = val
        return _atomic_write_json(config_path, existing)
    except Exception as exc:
        print(f"[config] save error: {exc}")
        return False


def remember_folder_alias(alias: str, folder_path: str, cfg: Optional[Config] = None, config_path: Optional[str] = None) -> bool:
    """Persists a spoken alias -> folder path mapping in config atomically."""
    clean_alias = str(alias).strip().lower()
    abs_path = os.path.abspath(folder_path)
    if cfg is not None:
        if not hasattr(cfg, "folders_whitelist") or not isinstance(cfg.folders_whitelist, dict):
            cfg.folders_whitelist = {}
        cfg.folders_whitelist[clean_alias] = abs_path

    target_path = get_config_path(config_path)
    try:
        data = {}
        if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if "folders_whitelist" not in data or not isinstance(data["folders_whitelist"], dict):
            data["folders_whitelist"] = {}
        data["folders_whitelist"][clean_alias] = abs_path
        return _atomic_write_json(target_path, data)
    except Exception as exc:
        print(f"[config] remember_folder_alias error: {exc}")
        return False


def update_island_settings(cfg: Optional[Config] = None, config_path: Optional[str] = None, **kwargs) -> bool:
    """
    Applies live dynamic updates to Dynamic Island settings and atomically persists to disk.
    Synchronizes live state with IslandPageManager if active.
    """
    target_cfg = cfg if cfg is not None else load_config(config_path)
    target_cfg.update(**kwargs)

    # Live sync with IslandPageManager if available
    try:
        from island_manager import get_island_page_manager
        get_island_page_manager().apply_config(target_cfg)
    except Exception:
        pass

    return save_config(target_cfg, path=config_path)


