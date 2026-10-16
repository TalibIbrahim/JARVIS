<p align="center">
  <h1 align="center">J.A.R.V.I.S.</h1>
</p>

<h3 align="center">Touchless Hand-Gesture PC Control with a Dynamic-Island Notch for Windows.</h3>

<p align="center">
  Real-time MediaPipe hand tracking with zero cloud dependencies at runtime,<br>
  a Tony Stark Arc-Reactor Dynamic Island, jitter-free One Euro cursor smoothing,<br>
  local voice commands, and a quiet background footprint.
</p>

<p align="center">
  <a href="https://github.com/TalibIbrahim/JARVIS/releases"><img alt="Download" src="https://img.shields.io/badge/download-latest%20release-00e5ff?style=for-the-badge" /></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-27272a?style=for-the-badge" />
  <img alt="Python" src="https://img.shields.io/badge/python-3.12-3b82f6?style=for-the-badge" />
  <img alt="UI" src="https://img.shields.io/badge/UI-PySide6%2060fps-10b981?style=for-the-badge" />
  <img alt="Tracking" src="https://img.shields.io/badge/tracking-MediaPipe-f59e0b?style=for-the-badge" />
  <img alt="License" src="https://img.shields.io/badge/license-MIT-27272a?style=for-the-badge" />
</p>

<p align="center">
  <a href="#why-jarvis">Why JARVIS</a> &middot;
  <a href="#features">Features</a> &middot;
  <a href="#gestures">Gestures</a> &middot;
  <a href="#voice-commands">Voice</a> &middot;
  <a href="#performance">Performance</a> &middot;
  <a href="#setup">Setup</a> &middot;
  <a href="#calibration">Calibration</a> &middot;
  <a href="#architecture">Architecture</a> &middot;
  <a href="#troubleshooting">Troubleshooting</a>
</p>

---

## Why JARVIS

| | |
|---|---|
| **Local & Real-Time** | 100% of computer vision and gesture state estimation executes on your machine. Zero video frames or landmarks leave your PC, delivering instantaneous sub-millisecond gesture latency. |
| **System 1 Reflex Engine** | Non-autoregressive on-device decision engine (Laya-style) executing natural desktop voice commands (*"crank up the audio"*, *"hide all my windows"*, *"go dark"*) in **< 1.0 ms** with zero cloud latency and zero API cost. |
| **Dynamic-Island Notch** | A polished, frameless, 32-bit translucent, click-through pill hugs the top-centre of your screen with an animated Tony Stark Arc-Reactor motif, circular wake ring, and live progressive speech typewriter. |
| **Speaker Voice Recognition** | Real-time acoustic speaker verification ($F_0$ pitch, 8-band formant spectral envelope) calibrated to your voice so Jarvis only responds to you and ignores background room chatter. |
| **Zero Click Drift** | With custom **Click Freeze** technology, the cursor automatically freezes the instant fingers start closing for a pinch (`pinch_ti < 0.55`), guaranteeing single and double clicks land exactly where aimed. |
| **Effortless Reach** | Screen reach mapping uses a configurable `box_margin` (default 18%), so your hand only covers the center 64% of the camera frame to reach every corner of your multi-monitor desktop. |
| **Failsafe Hardware Protection** | The mouse button and `Ctrl` key can never get stuck down. Involuntary tracking drops, app sleep, and shutdown trigger unconditional hardware button/key releases. |
| **Quiet in the Background** | When asleep in standby mode, MediaPipe neural network processing throttles to ~10 FPS, dropping idle CPU consumption to under 15% on laptops. |

---

## A Closer Look

<table>
  <tr>
    <td align="center"><b>Collapsed Standby Pill</b><br><sub>Compact 140x32 pill resting at the top of your screen with dim Arc Reactor</sub></td>
    <td align="center"><b>Wake-Progress Ring</b><br><sub>Golden circular progress ring sweeps around Arc Reactor during 0.8s palm hold</sub></td>
  </tr>
  <tr>
    <td align="center"><b>Active Tracking & Badges</b><br><sub>Expanded pill with neon cyan glow, live mini hand-skeleton, and gesture badges</sub></td>
    <td align="center"><b>Voice & Waveform</b><br><sub>Animated 5-bar dancing audio waveform displaying spoken assistant responses</sub></td>
  </tr>
  <tr>
    <td align="center"><b>Vision Telemetry HUD</b><br><sub>Real-time debug HUD showing FPS, pinch ratios, finger extension, and tracking bounds</sub></td>
    <td align="center"><b>Interactive Settings & Calibration</b><br><sub>Graphical control panel with live pinch measurement and slider tuning</sub></td>
  </tr>
</table>

---

## Features

### Touchless Control
- **Precision Pointing**: Move the cursor smoothly with your index fingertip, filtered by an adaptive One Euro filter that cancels hand tremor at rest while eliminating lag during fast swipes.
- **Natural Pinch Clicks**: Quick thumb + index pinch for left click; two rapid pinches for double click.
- **Drag & Drop**: Holding a pinch for longer than 0.35s latches the left mouse button into a drag; opening fingers drops the item.
- **Context Menu**: Thumb + middle finger pinch triggers right click without accidental left clicks.
- **Two-Finger Scroll**: Peace sign (index + middle fingers extended) moved up or down scrolls documents and web pages smoothly.
- **Two-Hand Zoom**: Spreading both hands apart or bringing them together smoothly zooms web pages, PDFs, and maps via continuous `Ctrl` + wheel control.

### Dynamic-Island Overlay (PySide6)
- **True Alpha & Antialiasing**: Built with native PySide6 for smooth 60 FPS spring animations without color-key fringing or jagged artifacts.
- **Full Click-Through**: Configured with Windows `WS_EX_TRANSPARENT`, allowing you to click anywhere beneath the overlay without stealing window focus.
- **Tony Stark Arc-Reactor**: Spinning segmented blades that spin slowly in standby and accelerate into high-speed rotation when active.
- **Distinct State Engine**: Seamlessly transitions between Standby, Wake Ring, Tracking, Gesture Flash badges, Voice Listening, Speaking, and Error indicators.
- **Embedded Mini Skeleton**: Displays a live miniature wireframe of your tracked hand inside the island.

### Local Voice & Intelligence
- **Wake Word Recognition**: Say *"Jarvis"* to trigger listening, or speak your command all at once (*"Jarvis, open Chrome"*).
- **Laya-Style System 1 Reflex Engine**: Non-autoregressive on-device semantic intent vectorizer (`decision_engine.py`) that classifies and executes un-regexed commands (*"crank up the audio"*, *"hide all my windows"*, *"go dark"*) in **< 1.0 ms** with zero cloud latency.
- **Acoustic Voice Profile & Speaker Verification**: Calibrate Jarvis to your vocal signature (*"train my voice"*). Uses fundamental pitch ($F_0$), 8-band formant spectral envelope, and centroid matching to filter out background room voices.
- **Siri-Style Live Typewriter Streaming**: Speech streams letter-by-letter across the Dynamic Island with a smooth blinking cyan cursor (`▍`) in real time.
- **Groq Multimodal Intelligence (System 2)**: Seamless cloud fallback for general reasoning, coding, and camera vision analysis (*"what is this"* / *"look at this"*).
- **Decoupled Audio Pipeline**: Threaded audio worker queue decouples microphone capture from recognition, ensuring zero audio dropouts.
- **Spoken Text-to-Speech**: Integrated neural voice engine running on an isolated thread with Paul Bettany's J.A.R.V.I.S. personality.

### Settings & Live Calibration
- **System Tray Icon**: Right-click the Arc Reactor tray icon in your Windows taskbar to toggle Wake/Standby, show the Vision HUD, open Settings, or Quit.
- **Custom Sensitivity**: Fine-tune screen reach margin, pinch distance thresholds, cursor smoothing cutoff, and zoom step.
- **Interactive Hand Calibration**: Live 3-step wizard that measures your personal thumb-to-index pinch distance and auto-tunes thresholds to your exact hand geometry.
- **JSON Persistence**: Automatically saves and loads overrides from `jarvis_config.json`.

### Discord-Style Auto-Updater
- **Silent Background Checks**: Queries GitHub Releases in the background on startup and every 4 hours without stalling tracking or framerate.
- **In-Place Atomic Replacement**: Uses a detached Windows replacer script to wait for the current process to terminate, overwrite the executable in-place, and relaunch.
- **Dedicated Updates Dialog Tab**: View current vs latest version, preview release notes, stream updates with live percentage progress, and configure auto-check preferences.
- **Tray & Voice Controls**: Access *"Check for Updates..."* directly from the taskbar tray context menu, or say *"Jarvis, check for updates"* and *"Jarvis, install update"*.

---

## Gestures & Control Schemes

J.A.R.V.I.S. offers two precision control schemes, selectable in Settings (`Control Scheme`).

### 1. Palm & Fist Scheme (Default)
The most stable and ergonomic control scheme. Cursor position is anchored on the rigid **metacarpal knuckles (landmarks 0, 5, 9, 13, 17)** rather than moving fingertips, eliminating jitter and closing displacement.

| Gesture | Action | Thresholds & Mechanics |
|---|---|---|
| **Open Palm** | **Move Cursor** | Rigid metacarpal anchor (MCP knuckles 5, 9, 13, 17); trackpad-style relative acceleration + precision aim. |
| **Quick Fist (< 400 ms)** | **Left Click** | Closing fingers into a fist and reopening within 400 ms triggers left click. 200 ms Lookback Buffer locks aim. |
| **Two Quick Fists (< 0.65 s)** | **Double Click** | Two quick fists within 0.65 s fires native Windows double click. |
| **Fist Held (> 350 ms)** | **Drag & Drop** | Latch left mouse button down; cursor follows hand; opening the hand drops the item (`DROP`). |
| **Thumb + Middle Pinch** | **Right Click** | Thumb contacts middle fingertip (`pinch_tm < 0.28`). |
| **Peace Sign (move up / down)** | **Scroll** | Index and middle fingers extended; vertical travel scrolls documents and webpages. |
| **Two Open Hands (spread / close)** | **Zoom In / Out** | Moving both hands apart zooms in; bringing together zooms out (`Ctrl` + scroll). |
| **Open Palm in Standby (hold 0.8 s)** | **Wake Up** | Circular golden wake ring completes; app transitions from Standby to Active. *(Open palm while awake never sleeps!)* |
| **Thumbs-Up (hold 1.2 s)** | **Standby** | 4 fingers curled, thumb extended upward for 1.2 s returns Jarvis to standby. Fists never sleep. |

### 2. Dwell-Click (Kinect-Style Hover to Click)
Hover the cursor stationary inside a target area to click completely hands-free:
- **Hover Trigger**: Hovering still within a 14 px radius for 1.0 s triggers a left click.
- **Glass Progress Ring**: An Apple visionOS style translucent ring fills around the cursor and pulses on completion.
- **Radial Action Menu**: Optional mode where hovering reveals an action menu: *Click, Double Click, Right Click, Drag, Scroll*.
- **Anti-Repeat Protection**: The cursor or hand must leave the target area before another dwell click can fire.

### 3. Pinch Scheme (Legacy Selectable in Settings)
- **Pointing Finger**: Move cursor with blended index tip / PIP joint.
- **Thumb + Index Pinch**: Single click, double click, and hold to drag.
- **Open Palm Hold (1.5 s)**: Returns app to standby.

---

## Voice Commands

Speak naturally after the wake word *"Jarvis"*:
```
"Jarvis, open Chrome"        "open YouTube"          "open VS Code"
"open File Explorer"         "open Notepad"          "open Calculator"
"search for quantum physics" "volume up / down"      "mute / unmute"
"play / pause"               "next track"            "previous track"
"screenshot"                 "show desktop"          "minimize / maximize"
"switch window"              "close window"          "lock"
"run diagnostics"            "suit status"           "power levels"
"house party protocol"       "stealth mode"          "clean slate protocol"
"target practice"            "thank you jarvis"      "who are you"
"train my voice"             "calibrate voice"       "learn my voice"
"what time is it"            "what day is it"        "status"
"wake up"                    "go to sleep"           "shut down Jarvis"
```

### Siri-Style Streaming Prompt Display & Speaker Recognition
- **Laya-Style System 1 Reflex Decision Engine**: Inspired by non-autoregressive decision models, un-regexed voice commands (*"crank up the audio"*, *"hide all my windows"*, *"go dark"*, *"bring up youtube"*) execute locally on-device in **< 1.0 ms** with zero cloud latency, zero API costs, and zero hallucinations. Knowledge queries (*"explain quantum computing"*) seamlessly route to Groq System 2 LLM.
- **Live Progressive Typewriter Display**: Just like Siri, as words are spoken and recognized, they stream smoothly across the Dynamic Island capsule letter-by-letter with an animated cursor (`“<prompt>▍”`), eliminating sudden text popping.
- **Voice Profile Calibration ("Train My Voice")**: Train Jarvis to recognize your unique vocal characteristics (pitch $F_0$, spectral formant envelope, centroid, rolloff). Simply say *"train my voice"* or *"calibrate voice"*, and Jarvis commits your vocal signature to memory.
- **Silent Background Wake-Word Monitoring**: In passive standby, Jarvis monitors quietly for the wake-word without displaying false "Processing speech..." popups on background room noises, keyboard typing, or distant chatter.
- **Hardware Telemetry Diagnostics**: Run `"diagnostics"` or `"suit status"` to receive real-time Windows hardware reports on physical RAM availability, AC/battery power levels, and 120 Hz repulsor tracking.
- **Stark Defense Protocols**: Direct voice actuation for `"House Party Protocol"`, `"Stealth Mode"`, `"Clean Slate Protocol"`, and `"Target Practice"`.
- **Iron Man Acoustic Cues**: Futuristic HUD frequency chimes for wake acknowledgment (880/1320 Hz) and directive confirmation (1046/1318 Hz).

---

## Performance

Tested on Windows 11 with an Intel Core i7 laptop and integrated 720p/1080p webcam:

| State | CPU Usage | Memory | Latency / FPS |
|---|---|---|---|
| **Standby (Collapsed Pill)** | **< 0.15%** (Background throttled to 10 FPS) | ~110 MB | Wake trigger within 0.8s |
| **Active Tracking (HUD Off)** | **14% – 19%** (Single hand, 30 FPS camera) | ~135 MB | Gesture latency < 1 ms |
| **Active Tracking + Telemetry HUD** | **18% – 24%** (Full skeleton preview) | ~150 MB | 30 FPS camera / 60 FPS overlay |
| **Cursor Smoothing Jitter** | **< 0.5 px** at rest (One Euro filter) | — | Sub-pixel stability |
| **Command Latency** | **< 16 ms** (One frame dispatch to `SetCursorPos`) | — | Instantaneous response |

---

## Setup

### Prerequisites
- **Operating System**: Windows 10 or Windows 11 (64-bit).
- **Python**: Python 3.12 (recommended) or 3.10–3.11.
- **Hardware**: Any standard USB or built-in webcam, microphone, and speakers.

### Installation

```powershell
# 1. Clone the repository
git clone https://github.com/TalibIbrahim/JARVIS.git
cd JARVIS

# 2. Create and activate a Python 3.12 virtual environment
python -m venv .venv
.\.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start J.A.R.V.I.S. with the camera HUD preview
python main.py --preview
```

### CLI Arguments
- `python main.py`: Starts Jarvis in normal background mode with the Dynamic Island overlay.
- `python main.py --preview`: Opens the camera HUD window showing hand skeletons and live telemetry.
- `python main.py --test-mic`: Runs interactive microphone and speech recognition diagnostics.
- `python main.py --diagnose`: Runs jitter, latency, anchor point, and pre-click drift benchmarks.
- `python main.py --no-voice`: Disables speech recognition for silent/presentation environments.
- `python main.py --camera 1`: Selects an alternate camera index.

---

## Calibration & Tuning

Open the graphical Settings dialog from the Windows system tray (**Right-click tray icon > Settings & Calibration...**):

1. **Controls Tab**:
   - **Screen Reach Margin (`box_margin`)**: Default 18%. Increase if you want to reach screen corners with smaller hand movements.
   - **Pinch Distance (`pinch_down`)**: Default 0.28. Lower if clicks trigger too easily; raise if pinching requires too much effort.
   - **Cursor Smoothing (`smooth_min_cutoff`)**: Default 1.0. Lower for maximum stillness; raise for more immediate raw tracking.
   - **Invert Scroll**: Toggle natural vs traditional scroll directions.
2. **Hand Calibration Tab**:
   - Displays real-time `pinch_ti` measurements for your hand.
   - Pinch your thumb and index finger together and click **Measure Current Pinch as Click Point**.
   - Click **Save & Apply** to persist settings to `jarvis_config.json`.

---

## Architecture

```
JARVIS/
├── main.py              # Application lifecycle, Per-Monitor V2 DPI, hotkey dispatcher
├── tracker.py           # OpenCV threaded capture, MediaPipe inference, HUD telemetry, CPU throttling
├── gestures.py          # Pure Python gesture state machine, Palm & Fist + Pinch schemes
├── controller.py        # Decoupled 120+ Hz cursor engine, relative acceleration, Lookback buffer
├── overlay.py           # PySide6 Apple HIG Dynamic Island, Mica/Acrylic glass, settings window
├── dwell.py             # Kinect-style hover-to-click engine and radial action menu
├── voice.py             # Speech recognition worker, hardware mic auto-probing, pyttsx3 speaker
├── voice_profile.py     # Acoustic speaker verification, F0 pitch autocorrelation, voice enrollment
├── decision_engine.py   # Laya-style System 1 reflex decision engine, sub-1ms intent vectorizer
├── ai.py                # Groq LLM & Multimodal Vision client, prompt builder, action executor
├── commands.py          # Local command directory with regex word boundary matching
├── config.py            # Configuration dataclass and JSON file loader (jarvis_config.json)
├── filters.py           # One Euro adaptive low-pass filter and deadzone hysteresis
├── updater.py           # Discord-style background auto-updater with atomic self-replacement
├── diagnose.py          # Jitter, latency, anchor point, and pre-click drift benchmarking tool
├── build_exe.py         # PyInstaller standalone executable packaging script
├── requirements.txt     # Pinned Python package dependencies
├── tests/
│   ├── test_gestures.py        # 26 synthetic landmark unit tests
│   ├── test_dwell.py           # 5 dwell hover and radial menu unit tests
│   ├── test_single_instance.py # 2 Win32 mutex single-instance unit tests
│   ├── test_updater.py         # 5 semver and release asset unit tests
│   ├── test_ai.py              # 41 Groq LLM, Whisper STT, vision, whitelist unit tests
│   ├── test_ironman_voice.py   # 46 prompt display, Stark telemetry & HUD unit tests
│   ├── test_decision_engine.py # 13 sub-millisecond decision engine & voice profile tests
│   ├── target_practice.py      # Automated and GUI 24x24 px precision targeting tool
│   ├── target_practice.html    # Web-based interactive precision target practice
│   └── PRECISION_TEST.md       # Precision test procedures and empirical benchmark results
├── APP_CONTEXT.md       # Comprehensive architecture and codebase reference for AI / Claude
├── CHANGELOG.md         # Detailed post-mortem of all bug fixes and root causes
├── LICENSE              # MIT License
└── README.md            # Comprehensive project documentation
```

---

## Running Unit Tests

Run the complete unit test suite anytime:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

All **138 tests** pass in ~2.6 seconds across the gesture state machine, dwell hover, single-instance mutex locks, auto-updater, Groq intelligence, vision analysis, Siri-style live display, Stark protocols, and the Laya-style System 1 decision engine.

---

## Troubleshooting

- **Clicks triggering by accident**: Open Settings and lower the Pinch Click Distance (or run the **Hand Calibration** wizard).
- **Cursor laggy during fast movement**: Open Settings and raise the Smoothing slider or increase `smooth_beta` in `jarvis_config.json`.
- **Wrong camera opened**: Run with `python main.py --camera 1` or change the camera index in the Settings dialog.
- **Admin Windows**: To control Task Manager or elevated administrator windows, launch your terminal as Administrator.
- **PyAudio install issues**: Ensure you are running 64-bit Python 3.12 (`python --version`). Pre-compiled wheels are specified in `requirements.txt`.

---

## Packaging to Standalone Executable

To package J.A.R.V.I.S. into a standalone Windows executable (`dist/jarvis/jarvis.exe`):

```powershell
python build_exe.py
```

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
