"""
command_catalog.py - static, Qt-free catalog of everything J.A.R.V.I.S. can do.

Data source for the Settings "What can Jarvis do" section. Every voice example is a phrase
that routes today (verified by a scratch self-check against commands.run_command); the
`action` field names the action the router reports (jarvis_log RESULT / _post_action).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

CATEGORIES: Tuple[str, ...] = (
    "Media", "Apps & Web", "Windows", "System", "Island",
    "Productivity", "Assistant", "AI & Vision", "Gestures",
)


@dataclass(frozen=True)
class CommandSpec:
    category: str
    title: str
    examples: Tuple[str, ...]
    description: str
    action: str
    kind: str  # "voice" | "gesture"
    requires_ai: bool = False


def _v(category, title, examples, description, action, requires_ai=False) -> CommandSpec:
    return CommandSpec(category, title, tuple(examples), description, action, "voice", requires_ai)


def _g(title, examples, description, action) -> CommandSpec:
    return CommandSpec("Gestures", title, tuple(examples), description, action, "gesture", False)


_CATALOG: List[CommandSpec] = [
    # ---------------------------------------------------------------- Media
    _v("Media", "Play / pause", ("Jarvis, play", "pause the music"),
       "Toggle playback in whatever media app is active.", "play_pause"),
    _v("Media", "Next track", ("next track", "skip"), "Skip to the next song.", "next_track"),
    _v("Media", "Previous track", ("previous song",), "Go back to the previous song.", "previous_track"),
    _v("Media", "Play on Spotify", ("play Drake on Spotify",),
       "Search and play an artist or track in Spotify.", "play_music"),
    _v("Media", "Play on YouTube", ("play lo-fi beats on YouTube",),
       "Search an artist or track on YouTube.", "play_music"),
    _v("Media", "Play anything", ("play Bohemian Rhapsody",),
       "Play a track on Spotify if installed, otherwise on YouTube.", "play_music"),
    _v("Media", "What's playing", ("what's playing", "what song is this"),
       "Show and speak the current track.", "now_playing"),
    _v("Media", "Volume up", ("volume up", "louder"), "Raise the system volume.", "volume_up"),
    _v("Media", "Volume down", ("volume down", "quieter"), "Lower the system volume.", "volume_down"),
    _v("Media", "Set volume", ("set volume to 40",), "Set the volume to an exact percentage.", "set_volume"),
    _v("Media", "Mute", ("mute", "unmute"), "Mute or unmute the audio explicitly.", "mute"),
    _v("Media", "Toggle mute", ("toggle mute",), "Flip the current mute state.", "mute"),

    # ----------------------------------------------------------- Apps & Web
    _v("Apps & Web", "Open an app", ("open Chrome", "launch notepad", "open VS Code"),
       "Launch Chrome, Edge, Notepad, Calculator, Terminal, Spotify, Office and more.", "open_app"),
    _v("Apps & Web", "Open a website", ("open YouTube", "go to GitHub"),
       "Open YouTube, Google, GitHub, Gmail, Maps or LinkedIn.", "open_site"),
    _v("Apps & Web", "Web search", ("search for python tutorials", "google best pizza near me"),
       "Search Google in your default browser.", "search"),
    _v("Apps & Web", "Switch to an app", ("switch to Chrome",),
       "Bring a running app to the front.", "switch_to_app"),
    _v("Apps & Web", "Open a folder", ("open downloads folder",),
       "Open Downloads, Documents, Desktop or a remembered folder.", "open_folder"),
    _v("Apps & Web", "Open latest file", ("open latest pdf", "open recent file"),
       "Open the newest file in Downloads, Documents or Desktop.", "open_recent_file"),
    _v("Apps & Web", "Show last download", ("show last download",),
       "Reveal your newest download in Explorer.", "show_last_download"),
    _v("Apps & Web", "Remember a folder", ("remember this folder as projects",),
       "Save the current folder under a spoken name.", "remember_folder"),

    # -------------------------------------------------------------- Windows
    _v("Windows", "Show desktop", ("show desktop", "minimize everything"),
       "Minimize all windows.", "show_desktop"),
    _v("Windows", "Minimize window", ("minimize",), "Minimize the active window.", "minimize_window"),
    _v("Windows", "Maximize window", ("maximize window",), "Maximize the active window.", "maximize_window"),
    _v("Windows", "Snap window", ("snap left", "snap right"), "Snap the active window to a screen half.", "snap_window"),
    _v("Windows", "Restore window", ("restore window",), "Restore the active window to its normal size.", "snap_window"),
    _v("Windows", "Switch window", ("switch window",), "Alt-Tab to the next window.", "switch_window"),
    _v("Windows", "Other monitor", ("move to other monitor",), "Send the active window to your other display.", "move_to_other_monitor"),
    _v("Windows", "Close window", ("close window",), "Close the active window (asks first if unsaved).", "close_window"),
    _v("Windows", "Window under cursor", ("open this", "copy this", "close this"),
       "Act on the window or item your cursor is over.", "open_this"),
    _v("Windows", "Scroll", ("scroll down", "scroll up"), "Scroll the active window.", "scroll"),
    _v("Windows", "Screenshot", ("take a screenshot",), "Save a screenshot to Pictures.", "screenshot"),
    _v("Windows", "Lock PC", ("lock the pc",), "Lock your workstation.", "lock_workstation"),

    # --------------------------------------------------------------- System
    _v("System", "Standby", ("go to sleep", "stand by"), "Put Jarvis on standby.", "power_mode"),
    _v("System", "Wake", ("wake up",), "Bring Jarvis back online.", "power_mode"),
    _v("System", "Quit Jarvis", ("shut down jarvis",), "Close the Jarvis application.", "power_mode"),
    _v("System", "Sleep PC", ("sleep pc",), "Put the computer to sleep.", "sleep_system"),
    _v("System", "Shut down / restart PC", ("shut down the pc", "restart pc"),
       "Power off or reboot after a spoken confirmation.", "shutdown_system"),
    _v("System", "Wi-Fi", ("toggle wifi",), "Turn Wi-Fi on or off.", "toggle_wifi"),
    _v("System", "Bluetooth", ("toggle bluetooth",), "Turn Bluetooth on or off.", "toggle_bluetooth"),
    _v("System", "Do not disturb", ("do not disturb",), "Toggle Focus Assist.", "toggle_do_not_disturb"),
    _v("System", "Night light", ("toggle night light",), "Toggle the Windows night light.", "toggle_night_light"),
    _v("System", "Brightness", ("brightness up", "set brightness to 50"),
       "Raise, lower or set the screen brightness.", "set_brightness"),
    _v("System", "Dwell click", ("dwell on", "dwell off", "dwell menu"),
       "Enable, disable or switch dwell click to the radial menu.", "dwell_toggle"),
    _v("System", "Precision mode", ("precision mode on", "precision mode off"),
       "Toggle fine-aim slowdown for small targets.", "precision_mode"),
    _v("System", "Pointer mode", ("relative mode", "absolute mode"),
       "Switch between trackpad-style and screen-mapped pointing.", "tracking_mode"),
    _v("System", "Diagnostics", ("run diagnostics",), "Report memory, power and tracking status.", "diagnostics"),
    _v("System", "House Party protocol", ("house party protocol",), "Launch your workstation apps.", "house_party"),
    _v("System", "Stealth mode", ("stealth mode",), "Mute audio and minimize the window.", "stealth_mode"),
    _v("System", "Clean Slate protocol", ("clean slate protocol",), "Clear the desktop and go to standby.", "clean_slate"),
    _v("System", "Target practice", ("target practice",), "Open the aiming practice range.", "target_practice"),
    _v("System", "Voice training", ("train my voice",), "Calibrate Jarvis to your voice.", "train_voice"),
    _v("System", "Check for updates", ("check for updates", "install update"),
       "Look for and apply a Jarvis update.", "check_updates"),
    _v("System", "AI setup", ("set up AI",), "Open the AI tab in Settings.", "settings"),

    # --------------------------------------------------------------- Island
    _v("Island", "Next page", ("next page",), "Show the next Island card.", "island_page_step"),
    _v("Island", "Previous page", ("previous page",), "Show the previous Island card.", "island_page_step"),
    _v("Island", "Show a page", ("show weather", "show hardware", "show calendar"),
       "Jump to the weather, hardware, media, calendar or Jarvis card.", "island_show_page"),
    _v("Island", "Expand / collapse", ("expand island", "collapse island"),
       "Open or close the Dynamic Island.", "island_expand"),
    _v("Island", "Hide widgets", ("hide island widgets",), "Toggle the Island widgets.", "toggle_widgets"),
    _v("Island", "Pause tracking", ("pause tracking", "stop tracking"),
       "Pause hand tracking (standby).", "tracking_paused"),
    _v("Island", "Resume tracking", ("resume tracking", "start tracking"),
       "Resume hand tracking.", "tracking_resumed"),
    _v("Island", "Pin the Island", ("pin the island", "unpin the island"),
       "Keep the Island open or release it.", "island_set_pin"),

    # --------------------------------------------------------- Productivity
    _v("Productivity", "Timer", ("set a timer for 5 minutes", "set a timer for 10 minutes for pasta"),
       "Start a countdown timer.", "set_timer"),
    _v("Productivity", "Timer status", ("time left on timer", "cancel the timer"),
       "Check or cancel the active timer.", "set_timer"),
    _v("Productivity", "Alarm", ("set an alarm for 7:30 am",), "Set an alarm for a clock time.", "set_timer"),
    _v("Productivity", "Reminder", ("remind me to stretch in 30 minutes",), "Get a reminder after a delay.", "set_timer"),
    _v("Productivity", "Routines", ("start work mode", "start focus mode", "start evening mode"),
       "Run a saved sequence of actions.", "start_routine"),
    _v("Productivity", "Quick notes", ("take a note buy milk", "read my notes"),
       "Save a note by voice and read it back.", "quick_note"),
    _v("Productivity", "Dictation", ("start dictation", "stop dictation"),
       "Type what you say into the focused window.", "quick_note"),
    _v("Productivity", "Type text", ("type hello world",), "Type a phrase into the focused window.", "quick_note"),
    _v("Productivity", "Clipboard", ("read clipboard", "summarize clipboard"),
       "Read or summarize what you copied.", "quick_note"),
    _v("Productivity", "Math", ("what is 25 times 4", "calculate 15 percent of 200"),
       "Do arithmetic offline.", "calculate"),
    _v("Productivity", "Unit conversion", ("convert 10 miles to kilometers",),
       "Convert lengths, weights, volumes and temperatures.", "calculate"),

    # ------------------------------------------------------------ Assistant
    _v("Assistant", "Time", ("what time is it",), "Tell the current time.", "time"),
    _v("Assistant", "Date", ("what day is it",), "Tell today's date.", "date"),
    _v("Assistant", "Weather", ("what's the weather",), "Current conditions for your city.", "weather"),
    _v("Assistant", "World time", ("what time is it in Tokyo",), "Time in another city.", "world_time"),
    _v("Assistant", "Wikipedia", ("wiki Nikola Tesla",), "Read a short Wikipedia summary.", "wiki"),
    _v("Assistant", "Small talk", ("hello", "who are you"), "Greetings and status.", "chatter"),

    # ---------------------------------------------------------- AI & Vision
    _v("AI & Vision", "Ask anything", ("tell me a joke", "why is the sky blue"),
       "Open-ended questions answered by the cloud AI.", "conversation", True),
    _v("AI & Vision", "What am I holding", ("what am I holding", "look at this"),
       "Describe what the camera sees.", "visual_query", True),
    _v("AI & Vision", "Read my screen", ("what's on my screen", "explain this error", "summarize this page"),
       "Analyze a snapshot of your screen.", "screen_awareness", True),

    # ------------------------------------------------------------- Gestures
    _g("Wake", ("Hold an open palm for half a second while on standby",),
       "Open palm hold wakes Jarvis.", "activate"),
    _g("Listen for a command", ("Hold a \U0001F919 call-me hand (thumb and pinky out, other fingers curled) for half a second",),
       "Ducks media and listens for one command, no wake word needed.", "listen"),
    _g("Standby", ("Hold a thumbs-up for about a second",), "Thumbs-up hold puts Jarvis on standby.", "deactivate"),
    _g("Move cursor", ("Keep your hand open and move it",), "An open palm moves the pointer.", "move"),
    _g("Click", ("Close your fist and reopen it quickly",), "Quick fist is a left click.", "click"),
    _g("Double click", ("Make two quick fists",), "Two quick fists double-click.", "double_click"),
    _g("Drag", ("Close your fist and hold, then move",), "Hold a fist to drag; open to drop.", "drag"),
    _g("Right click", ("Pinch thumb and middle finger",), "Thumb and middle pinch is a right click.", "right_click"),
    _g("Scroll", ("Raise a peace sign and move up or down",), "Peace sign scrolls the page.", "scroll"),
    _g("Zoom (two hands)", ("Show two hands and spread or close them",), "Two hands zoom in and out.", "zoom"),
    _g("Zoom (one hand)", ("Pinch thumb and index and hold, then move the hand up or down",),
       "Pinch and hold, then move up to zoom in, down to zoom out.", "zoom_one_hand"),
    _g("Switch window", ("Flick your hand quickly right or left",),
       "A fast flick right is the next window, left is the previous; slow movement never switches.", "switch_window"),
    _g("Volume / Island pages", ("Flick your hand quickly up or down",),
       "A fast flick up or down changes volume; near the Island it flips pages.", "volume_slide"),
    _g("Dwell click", ("Hold the cursor still for a second",), "Hover to click without a gesture.", "dwell_click"),
    _g("Radial menu", ("Hover still in radial dwell mode",),
       "Pick click, double, right click, drag or scroll from a ring.", "dwell_radial"),
]


def get_catalog() -> List[CommandSpec]:
    """Return a copy of the catalog (entries are immutable)."""
    return list(_CATALOG)
