# J.A.R.V.I.S. (Just A Rather Very Intelligent System) - Complete Project Context & Architecture Reference

This document provides a comprehensive, self-contained overview of the J.A.R.V.I.S. codebase for engineers and AI assistants (such as Claude). It details the system architecture, mathematical filters, control schemes, thread model, Apple HIG user interface, voice engine, hardware auto-detection, testing suite, and configuration parameters.

---

## 1. Executive Summary & Philosophy

- **Repository**: [https://github.com/TalibIbrahim/JARVIS](https://github.com/TalibIbrahim/JARVIS)
- **Local Directory**: [`c:\Programming\Jarvis`](file:///c:/Programming/Jarvis)
- **Operating System**: Windows 10 & Windows 11 (64-bit), Per-Monitor V2 DPI Aware
- **Runtime**: Python 3.12 (Virtual Environment at [`.venv`](file:///c:/Programming/Jarvis/.venv))
- **Core Technologies**: MediaPipe Hands, OpenCV, PySide6 (Qt Quick / QPainter), PyAudio, SpeechRecognition, pyttsx3, pyautogui, pynput

### Core Premise
J.A.R.V.I.S. is an ultra-precise, touchless hand-gesture PC control system and desktop assistant for Windows. It runs **100% locally** on the user's computer with zero cloud vision streaming and sub-millisecond gesture latency.

### Key Innovations
1. **Sub-Pixel Cursor Precision**: Solved hand tremor and optical camera jitter, reducing resting jitter from 4.02 px to **0.00 px (deadband) / 1.48 px (raw)**.
2. **Palm & Fist Control Scheme**: Replaces unstable fingertip tracking with rigid metacarpal knuckle anchors (MCP landmarks 5, 9, 13, 17) and an openness hysteresis state machine.
3. **Zero Pre-Click Drift**: A 200 ms Lookback Ring Buffer and 150 ms Reopen Freeze completely eliminate cursor shift when making a click gesture (0.0 px drift).
4. **Decoupled 120+ Hz Cursor Engine**: Interpolates cursor position at 120+ Hz independently of the 30 FPS camera capture rate.
5. **Kinect-Style Dwell Click**: Hover-to-click system with an Apple visionOS-inspired translucent progress ring and 5-action radial menu.
6. **Apple Human Interface Guidelines (HIG) Overlay**: Dynamic Island capsule hugging the top-centre of the screen with native Windows 11 Acrylic / Mica glass blur (`DwmSetWindowAttribute`), continuous-corner squircle curves ($G^2$ continuity), and critically damped spring physics.
7. **Hardware Microphone Auto-Detection**: Probes real-time acoustic RMS across all input devices to bypass silent virtual audio cables and bind directly to physical hardware arrays.

---

## 2. System Architecture & Thread Model

J.A.R.V.I.S. operates on a multi-threaded, lock-free architecture designed so that slow frames, network delays, or audio processing never stall cursor movement or UI rendering.

```
+-----------------------------------------------------------------------------------------------+
|                                      OPERATING SYSTEM (WINDOWS)                               |
+-----------------------------------------------------------------------------------------------+
       |                                       |                                   ^
  Webcam Frames                          Audio Chunks                         user32 Input
  (1280x720 @ 30 FPS)                     (Physical Mic)                     (SetCursorPos,
       |                                       |                             mouse_event)
       v                                       v                                   |
+---------------------+             +--------------------+                         |
|    FrameGrabber     |             |   Voice Worker     |                         |
| (Dedicated Thread)  |             | (Dedicated Thread) |                         |
+---------------------+             +--------------------+                         |
       |                                       |                                   |
  Latest Frame                           VAD 16kHz WAV                             |
       v                                       v                                   |
+---------------------+             +--------------------+                         |
|   Tracker Thread    |             | Groq Whisper STT / |                         |
|  MediaPipe Hands    |             | Google SR Fallback |                         |
+---------------------+             +--------------------+                         |
       |                                       |                                   |
   Landmarks (x,y)                       Command String                            |
       v                                       v                                   |
+---------------------+             +--------------------+                         |
|    GestureEngine    |             |    commands.py     |                         |
| (Pure Python Logic) |             | (Regex First ->    |                         |
+---------------------+             |  Groq LLM Fallback)|                         |
       |                            +--------------------+                         |
  Action Events                                |                                   |
  (move, click, drag)                    Mode Changes /                            |
       |                                 Spoken Replies                            |
       v                                       v                                   |
+--------------------------------------------------------+                         |
|                Controller (120+ Hz Thread)             |-------------------------+
| - Lookback Ring Buffer (200 ms)                        |
| - One Euro Filter & Deadzone Attenuation (1.5 px)      |
| - Pointer Acceleration & Precision Fine-Aim            |
| - Dwell Click State Machine & Radial Menu              |
+--------------------------------------------------------+
       |
  Visual Telemetry (Dwell progress, badges, waveforms, thinking pulses)
       v
+--------------------------------------------------------+
|             PySide6 GUI (Main / Qt Event Loop)         |
| - Dynamic Island Squircle Overlay (60 FPS Springs)     |
| - Windows 11 Acrylic / Mica Backdrop (DWM API)         |
| - Cursor Dwell Progress Ring Overlay (Click-Through)   |
| - System Settings & Live Calibration Window            |
| - Taskbar System Tray Icon                             |
+--------------------------------------------------------+
```

### Thread Summary
1. **Main Thread**: Runs the PySide6 `QApplication` event loop for the Dynamic Island overlay, system tray, and settings window ([`overlay.py`](file:///c:/Programming/Jarvis/overlay.py)).
2. **FrameGrabber Thread**: Captures video frames from OpenCV VideoCapture via DirectShow (`cv2.CAP_DSHOW`) with MJPG compression at 1280x720 @ 30 FPS ([`tracker.py`](file:///c:/Programming/Jarvis/tracker.py)).
3. **Tracker Thread**: Runs MediaPipe Hands neural inference. When in standby, throttles to 10 FPS to minimize laptop CPU consumption (<0.15% idle) ([`tracker.py`](file:///c:/Programming/Jarvis/tracker.py)).
4. **GestureEngine (Synchronous in Tracker)**: Classifies finger ratios and openness scores without OS or camera imports ([`gestures.py`](file:///c:/Programming/Jarvis/gestures.py)).
5. **Controller Thread**: Dedicated 120+ Hz high-refresh cursor interpolation engine using critically damped exponential smoothing and user32 `SetCursorPos` ([`controller.py`](file:///c:/Programming/Jarvis/controller.py)).
6. **Voice Recognition Worker Thread**: Continuously reads audio frames from PyAudio, performs energy-based VAD, executes Groq Whisper STT (or Google Speech API fallback) on background speech chunks, and queries Groq LLM on worker threads ([`voice.py`](file:///c:/Programming/Jarvis/voice.py), [`ai.py`](file:///c:/Programming/Jarvis/ai.py)).
7. **Speaker Worker Thread**: Manages a non-blocking queue for `pyttsx3` text-to-speech feedback ([`voice.py`](file:///c:/Programming/Jarvis/voice.py)).
8. **AutoUpdater Thread**: Background check against GitHub Releases API every 4 hours ([`updater.py`](file:///c:/Programming/Jarvis/updater.py)).
9. **Global Hotkeys Thread**: Uses `pynput.keyboard.GlobalHotKeys` to register `Ctrl+Alt+J` (toggle active/standby) and `Ctrl+Alt+Q` (quit) ([`main.py`](file:///c:/Programming/Jarvis/main.py)).

---

## 3. Repository File Structure & Module Directory

| File Path | Primary Responsibilities |
| :--- | :--- |
| [`main.py`](file:///c:/Programming/Jarvis/main.py) | Application entry point, Per-Monitor V2 DPI awareness initialization, Win32 single-instance mutex (`Local\JARVIS_HandGesture_SingleInstance_Mutex`), CLI parser, lifecycle coordinator. |
| [`gestures.py`](file:///c:/Programming/Jarvis/gestures.py) | Pure-Python gesture recognition engine. Zero camera or OS dependencies. Implements Palm & Fist scheme, legacy Pinch scheme, metacarpal knuckle anchors, openness score calculation, and hysteresis state machines. |
| [`controller.py`](file:///c:/Programming/Jarvis/controller.py) | Actuates Windows mouse and keyboard events. Contains the decoupled 120+ Hz cursor interpolation thread, 200 ms Lookback Ring Buffer, relative trackpad mapping with acceleration, fine-aim precision mode, and failsafe button releases. |
| [`tracker.py`](file:///c:/Programming/Jarvis/tracker.py) | OpenCV threaded capture (`FrameGrabber`), MediaPipe Hands inference pipeline, camera resolution and MJPG setup, CPU standby throttling, and OpenCV HUD telemetry debug window. |
| [`filters.py`](file:///c:/Programming/Jarvis/filters.py) | Mathematical filters: One Euro adaptive low-pass filter (`OneEuro`, `OneEuroPoint`), and smooth linear attenuation deadzone filter (`DeadzoneFilter`). |
| [`dwell.py`](file:///c:/Programming/Jarvis/dwell.py) | Pure-logic Kinect-style hover-to-click state machine. Dwell timing, anti-repeat latching, stationarity radius checks, and 5-action radial menu directional resolver. |
| [`overlay.py`](file:///c:/Programming/Jarvis/overlay.py) | PySide6 Apple HIG Dynamic Island overlay, native Windows 11 Acrylic blur (`DwmSetWindowAttribute`), continuous squircle curves, critically damped spring physics, cursor dwell ring overlay, macOS System Settings dialog (including AI intelligence tab), and taskbar tray icon. |
| [`voice.py`](file:///c:/Programming/Jarvis/voice.py) | Voice recognition subsystem. Hardware microphone probing and auto-selection, live RMS volume level probing, energy-based VAD, Groq Whisper STT with automatic Google Speech API fallback, and `pyttsx3` TTS speaker queue. |
| [`commands.py`](file:///c:/Programming/Jarvis/commands.py) | Voice command directory. Exact regex word-boundary matching first for instant local actions, with intelligent Groq LLM fallback for conversational responses and action reasoning. |
| [`ai.py`](file:///c:/Programming/Jarvis/ai.py) | Groq AI cloud integration module. Plain `requests` client for Groq's OpenAI-compatible API (`GroqClient`), Windows Credential Manager (`keyring`) key storage with dev env fallback, free-tier sliding window rate limiter, Whisper transcription, low-latency chat completions, and action whitelist dispatcher. |
| [`config.py`](file:///c:/Programming/Jarvis/config.py) | Dataclass configuration schema with all default parameters (including AI settings) and JSON loader/saver for [`jarvis_config.json`](file:///c:/Programming/Jarvis/jarvis_config.json). Secrets are never written to disk. |
| [`updater.py`](file:///c:/Programming/Jarvis/updater.py) | Discord-style background auto-updater. Checks GitHub Releases API, downloads assets with progress reporting, and runs detached PowerShell replacer for atomic in-place updates. |
| [`diagnose.py`](file:///c:/Programming/Jarvis/diagnose.py) | Comprehensive diagnostic benchmark tool. Evaluates resting jitter across 7 anchor points, pre-click drift, camera FPS, inference latency, and generates [`diagnose_report.md`](file:///c:/Programming/Jarvis/diagnose_report.md). |
| [`build_exe.py`](file:///c:/Programming/Jarvis/build_exe.py) | Packaging script for PyInstaller standalone executable builds. |
| [`jarvis.spec`](file:///c:/Programming/Jarvis/jarvis.spec) | PyInstaller specification file with binary includes, data assets, icon definitions, certifi CA data, and keyring Windows backend hidden imports. |
| [`tests/test_gestures.py`](file:///c:/Programming/Jarvis/tests/test_gestures.py) | 26 unit tests verifying gesture detection, openness scoring, pose hysteresis, knuckle anchors, lookback buffer, wake/standby rules, and handedness symmetry with synthetic landmarks. |
| [`tests/test_dwell.py`](file:///c:/Programming/Jarvis/tests/test_dwell.py) | 5 unit tests verifying dwell hover progress, anti-repeat latching, gesture pause handling, and radial menu directional selection. |
| [`tests/test_single_instance.py`](file:///c:/Programming/Jarvis/tests/test_single_instance.py) | 2 unit tests verifying Win32 single-instance mutex creation and duplicate process rejection. |
| [`tests/test_updater.py`](file:///c:/Programming/Jarvis/tests/test_updater.py) | 5 unit tests verifying semver version comparison logic and release asset URL matching. |
| [`tests/test_ai.py`](file:///c:/Programming/Jarvis/tests/test_ai.py) | 41 unit tests verifying Groq API key validation, 401/429/timeout/offline handling, rate limiter, Whisper STT, LLM chat completions, JSON action schema validation, whitelist rejection of unapproved shell calls, Google SR fallback, and history reset. |
| [`tests/target_practice.py`](file:///c:/Programming/Jarvis/tests/target_practice.py) | Automated and GUI precision benchmarking tool evaluating hit rates on 24x24 px desktop targets. |
| [`tests/target_practice.html`](file:///c:/Programming/Jarvis/tests/target_practice.html) | Interactive web-based targeting canvas for manual verification of click accuracy. |
| [`tests/PRECISION_TEST.md`](file:///c:/Programming/Jarvis/tests/PRECISION_TEST.md) | Official precision and accuracy test specification and empirical benchmark logs. |

---

## 4. Control Schemes & Gesture Engines

J.A.R.V.I.S. provides two switchable control schemes in Settings (`control_scheme`):

### 1. Palm & Fist Scheme (Default)
The Palm & Fist scheme was engineered to eliminate fingertip biomechanical noise. Cursor motion is anchored on the **rigid metacarpal knuckles (MCP joints: landmarks 5, 9, 13, 17)**. Because the knuckles form the structural palm base, curling the fingers into a fist causes virtually zero displacement of the pointing anchor.

| Gesture | Action | Mechanics & Thresholds |
| :--- | :--- | :--- |
| **Open Palm** | Move Cursor | Metacarpal knuckle centroid anchor (`palm_anchor="knuckles"`). Relative pointer acceleration with precision fine-aim. |
| **Quick Fist (< 400 ms)** | Left Click | Hand transitions from `OPEN` into `FIST` and reopens within 400 ms (`fist_click_max_hold`). Cursor lands using 200 ms Lookback Buffer. |
| **Two Quick Fists (< 0.65 s)** | Double Click | Two quick fist gestures executed within 0.65 s (`fist_double_window`). |
| **Fist Held (> 350 ms)** | Drag & Drop | Fist held for > 350 ms (`fist_drag_min_hold`) latches left mouse button down (`mouseDown`). Hand moves to drag. Opening hand fires `drag_end` (`mouseUp`). |
| **Thumb + Middle Pinch** | Right Click | Thumb tip touches middle fingertip (`pinch_tm < 0.28`). |
| **Peace Sign (Move Up/Down)** | Scroll | Index and middle fingers extended, ring and pinky curled. Moving hand vertically scrolls documents and pages. |
| **Two Open Hands (Spread/Close)** | Zoom In / Out | Both hands visible. Distance between index fingertips dynamically adjusts zoom via `Ctrl` + wheel emulation. |
| **Open Palm Hold (0.8 s)** | Wake Up | When in Standby, hold open palm for 0.8 s. Golden circular progress ring fills. *Open palm while awake NEVER sleeps.* |
| **Thumbs-Up Hold (1.2 s)** | Standby | Four fingers curled into fist with thumb extended upward/outward for 1.2 s (`thumbs_up_hold`). Fists never sleep. |

#### Pose Classification & Hysteresis
Finger extension ratio:
$$r = \frac{\text{dist}(\text{wrist}, \text{tip})}{\text{dist}(\text{wrist}, \text{mcp})}$$

Continuous finger openness score:
$$\text{openness}_i = \text{clamp}\left(\frac{r - 1.25}{1.65 - 1.25}, 0.0, 1.0\right)$$
$$\text{openness} = \frac{1}{4} \sum_{i \in \{\text{index, mid, ring, pinky}\}} \text{openness}_i$$

Hysteresis thresholds:
- `OPEN`: Entering requires $\text{openness} \ge 0.70$. Exiting occurs when $\text{openness} < 0.58$.
- `NEUTRAL`: Range between $0.38$ and $0.58$. In the Neutral zone, cursor motion is completely frozen, and no clicks fire. A relaxed, half-curled hand produces zero unintended input.
- `FIST`: Entering requires $\text{openness} \le 0.28$. Exiting occurs when $\text{openness} > 0.38$.
- **Debounce**: 2 consecutive frames required to confirm any state change (`pose_debounce_frames=2`).

### 2. Dwell-Click System (Kinect-Style Hover to Click)
Allows completely touchless clicking by holding the cursor stationary:
- **Hover Detection**: Cursor remaining within a 14 px radius (`dwell_radius`) for 1.0 s (`dwell_time`) triggers a click.
- **Glass Progress Ring**: Translucent Apple-style ring renders around the cursor, fills progressively, and pulses on click.
- **Anti-Repeat Protection**: After firing, the dwell engine latches until the cursor leaves the 14 px radius or the hand leaves camera view.
- **Radial Action Menu**: When `dwell_mode="radial"`, dwelling opens a 5-slice radial menu centered on the cursor:
  1. *Click* (Top-Right)
  2. *Double Click* (Right)
  3. *Right Click* (Bottom-Right)
  4. *Drag / Drop* (Bottom-Left)
  5. *Scroll Mode* (Top-Left)
  Hovering over a radial sector for 450 ms locks and executes the selected action.
- **Automatic Pausing**: Dwell pauses immediately if a fist or pinch is active, while dragging, when the hand is lost, or when in standby.

### 3. Pinch Scheme (Legacy Calibrated)
Preserved for benchmarking and user preference (`control_scheme="pinch"`):
- Cursor driven by a weighted blend of the index tip (65%) and index PIP joint (35%).
- Thumb + index pinch triggers single click, double click, and drag.
- Open palm held for 1.5 s while awake puts the app into standby.

---

## 5. Cursor Precision Engine & Math

### Root Causes of Cursor Noise and Their Solutions
1. **Camera Noise Amplification**: Stretching the 64% active hand tracking frame across a 1920x1080 display amplifies sensor noise ~5x.
   - *Solution*: Replaced absolute screen mapping with **Trackpad-Style Relative Mapping**. Small hand displacements produce fine sub-pixel motion, while swift hand travel activates an acceleration curve.
2. **Biomechanical Fingertip Deflection**: When curling fingers into a click, the fingertips shift 20–35 px before contact.
   - *Solution*: Metacarpal knuckle centroid anchor (MCP 5, 9, 13, 17) stays stationary throughout finger closure.
3. **Pre-Click Drift**: Cursor shifts in the 150–250 ms window before click detection completes.
   - *Solution*: **200 ms Lookback Ring Buffer**. When a click is recognized, the cursor position is rewound to where it was 200 ms prior, before closure commenced.
4. **Post-Click Extensor Twitch**: Reopening fingers after clicking produces a brief mechanical jerk.
   - *Solution*: **150 ms Reopen Freeze** locks cursor coordinates for 150 ms following click release.
5. **Decoupled 120+ Hz Refresh**:
   - Camera delivers frames at ~30 FPS, causing stepped 33 ms jumps.
   - The decoupled controller thread interpolates toward the target position at 120+ Hz using critically damped exponential approach:
     $$\alpha = 1.0 - e^{-38.0 \cdot \Delta t}$$
     $$x_{\text{cur}} \leftarrow x_{\text{cur}} + (x_{\text{target}} - x_{\text{cur}}) \cdot \alpha$$

### Filter Pipeline
1. **Landmark Space One Euro Filter**: Smooths raw MediaPipe landmark normalized coordinates $(x, y)$.
2. **Velocity & Gain Calculation**:
   - Precision threshold: Hand speed $< 0.015$ screen widths/s triggers **Fine-Aim Mode** ($0.35\times$ gain).
   - Acceleration threshold: Hand speed $> 0.035$ screen widths/s activates pointer acceleration up to $2.5\times$.
3. **Deadzone Hysteresis Filter**:
   - Suppresses micro-jitter below $1.5\text{ px}$.
   - Uses a smooth linear attenuation ramp above $1.5\text{ px}$ to eliminate snapping or stickiness:
     $$\text{factor} = \frac{\text{dist} - \text{deadzone}}{\text{dist}}$$

---

## 6. Apple HIG User Interface & Styling

All user interface components in J.A.R.V.I.S. adhere strictly to Apple's Human Interface Guidelines (clarity, deference, depth) and macOS / visionOS styling.

### 1. Dynamic Island Notch Overlay
- **Geometry**: Continuous-curvature squircle capsule ($G^2$ continuity) positioned at top-centre of the primary display.
- **Backdrop Material**: Real Windows 11 Acrylic blur applied through `DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE=3)` with fallback to Windows 10 `SetWindowCompositionAttribute` blur-behind.
- **Layered Glass**: Blurred backdrop, translucent dark tint (`#161618D8`), 1 px inner specular highlight stroke (lighter at top, darker at bottom), and soft layered drop shadows.
- **Physics**: Critically damped spring physics driving capsule width, height, and content layout transitions at 60 FPS.
- **Click-Through**: Configured with Win32 extended window style `WS_EX_TRANSPARENT | WS_EX_LAYERED`, allowing clicks to pass through to underlying applications.
- **Island States**:
  - *Standby*: Dim, compact 140x32 capsule with idling Arc Reactor blade.
  - *Wake Progress*: Golden sweeping progress ring tracking 0.8 s palm hold.
  - *Active Tracking*: Expanded pill with neon cyan glow, miniature real-time hand skeleton wireframe, and active status.
  - *Gesture Badges*: Discrete pill badges for Click, Double Click, Drag, Drop, Scroll, and Zoom.
  - *Voice Listening*: Soft pulsing Siri-style cyan/blue waveform with active transcribing indicator.
  - *Prompt Display*: Real-time on-screen Siri-style prompt display (`“<prompt>”`) with Tony Stark Arc Reactor core glyph and directive confirmation.
  - *Thinking*: Subtle pulsing accent glow with rhythmic 3-dot cadence indicators while awaiting LLM reasoning.
  - *Speaking*: Stacks user prompt on top line with assistant response and dancing 4-bar audio waveform below.
  - *Error*: Amber warning badge (`#ff9f0a`) for Groq API key/rate-limit/offline errors and disconnected camera/microphone.

### 2. Cursor Dwell Overlay
- Click-through transparent widget following the cursor at high refresh rate.
- Displays an Apple visionOS-inspired circular glass ring that fills radially as the user hovers, followed by a gentle expansion ripple upon completion.

### 3. macOS System Settings Window
- Opened via Taskbar System Tray context menu (**Settings & Calibration...**) or voice command (*"set up AI"*).
- Two-column layout: Styled sidebar navigation with inset grouped cards.
- **Tabs**:
  1. *General*: Video capture device, camera resolution, model complexity, and automatic startup updates.
  2. *Gestures*: Tracking schemes (Palm & Fist / Pinch), knuckle anchor selection, openness thresholds, and precision mode.
  3. *Pointer & Dwell*: Tracking mode (Relative / Absolute), Dwell Mode (Off / Click / Radial), Dwell Delay, Deadzone suppression, and Lookback Buffer.
  4. *Voice*: Physical microphone device selection, live real-time audio volume level meter (`QProgressBar`), trigger sensitivity slider, and microphone diagnostic test tool.
  5. *AI & Intelligence*: Enable AI switch (`AppleSwitch`), masked Groq API key with show/hide toggle and Save/Remove actions, direct link to `console.groq.com`, LLM model and Speech model dropdowns (dynamically updated from `list_models()`), asynchronous "Test Connection" tool with inline status, and data privacy notice.
  6. *Display & Materials*: Appearance theme (System Auto / Dark Glass / Light Glass), Acrylic blur toggle, skeleton mini visualizer, and reduce motion toggle.
  7. *About*: Architecture details, keyboard shortcuts (`Ctrl+Alt+J`, `Ctrl+Alt+Q`), update check action, and precision diagnostics.
- Custom `AppleSwitch` toggle widgets replicating macOS switches with smooth animated sliding thumbs.
- **Typography**: 100% free of emojis across all labels, sidebars, and menus for a clean, professional aesthetic.

---

## 7. Voice Subsystem & Hardware Probing

### Hardware Microphone Auto-Detection
Many Windows PCs have Virtual Audio Cables installed (e.g. from OBS, VoiceMeeter, or VB-Cable) which register as `Device 1` but stream digital silence (`RMS: 0.5`).
- [`voice.py`](file:///c:/Programming/Jarvis/voice.py) implements `list_microphones()` and `find_best_microphone()`:
  - Enumerates all audio input devices using PyAudio.
  - Opens a test audio stream on each device and computes real-time acoustic RMS.
  - Filters out virtual devices matching keywords (`virtual`, `cable`, `mapper`, `stereo mix`).
  - Prioritizes physical microphone arrays (e.g., Intel SST, Realtek High Definition Audio) with active acoustic energy.
  - Exposes `probe_mic_level()` for the Settings GUI live audio meter.

### Speech Pipeline & Intelligence Architecture
- **Audio Capture**: PyAudio stream reading audio chunks into a thread-safe queue.
- **Energy-Based VAD**: Audio chunks are validated using real-time RMS checks (`rms >= 30.0`). Only complete utterances are serialized in memory as 16 kHz mono WAV chunks without temporary files, avoiding unnecessary streaming or burning free-tier quotas.
- **Dual Speech Recognition Pipeline**:
  - *Primary (Groq Whisper)*: When `ai_enabled` is true and a Groq key is configured, audio is transcribed using Groq's high-speed Whisper service (`whisper-large-v3-turbo` with language hint `"en"`).
  - *Automatic Fallback (Google Speech API)*: If no key is set, the network is offline, or the API returns HTTP 401/429, the system automatically routes to Google SpeechRecognition and presents an amber Island error badge. Once connectivity or rate limit cooldown clears, Groq transcription automatically resumes.
- **Siri-Style Progressive Speech Streaming**: As the user speaks, captured audio triggers progressive live word posting to the Dynamic Island overlay with a smooth blinking cyan cursor (`▍`).
- **Speaker Verification & Voice Profiling ([`voice_profile.py`](file:///c:/Programming/Jarvis/voice_profile.py))**: Extracts an 11-dimensional acoustic feature vector ($F_0$ pitch via autocorrelation, 8-band formant spectral envelope, spectral centroid, rolloff). Users can train the model via `"train my voice"`. Verified speakers execute commands while background chatter from non-owners is suppressed.
- **Local Dispatch First**: [`commands.py`](file:///c:/Programming/Jarvis/commands.py) always evaluates exact regex word boundary patterns (`\b...\b`) locally first for instant, zero-latency execution.
- **Laya-Style Local Decision Engine (System 1 Reflex Model, [`decision_engine.py`](file:///c:/Programming/Jarvis/decision_engine.py))**:
  - Non-autoregressive exemplar matching with character 3-gram subword vectorization and IDF weighting.
  - Classifies natural, un-regexed command variations (*"crank up the audio"*, *"hide all my windows"*, *"go dark"*, *"bring up youtube"*) in **< 1.0 ms** on CPU with zero cloud calls, zero network latency, and zero API costs.
  - Uses margin thresholding ($S^* \ge 0.25$, $\Delta \ge 0.03$) to crisply separate desktop commands from general conversational queries.
- **Groq LLM Intelligence Fallback (System 2 Deep Reasoning)**: If no local regex or reflex matches, the query is passed to Groq LLM (`openai/gpt-oss-120b`). The system prompt injects dynamic contextual state (time, active/standby status, tracking mode, dwell mode) and enforces JSON action responses (`action`, `args`, `reply`).
- **Strict Action Whitelisting**: Actions returned by the LLM are validated against a strict whitelist (diagnostics, stealth mode, clean slate, house party, target practice, volume, media controls, whitelist-only app launching, whitelist-only sites, search, screenshot, workstation lock, window controls, mode switches, status). Arbitrary shell execution is impossible. If JSON parsing fails, the assistant speaks the reply text safely.
- **In-Memory History**: Keeps up to 6 turns of conversation history in volatile RAM only. Resets automatically whenever Jarvis enters standby.
- **Feedback**: Spoken responses delivered via an isolated background `pyttsx3` text-to-speech worker thread or Paul Bettany Piper neural voice.

### Voice Command Catalog
- **Power & Mode**: `"wake up"`, `"go to sleep"`, `"shut down jarvis"`
- **AI Configuration**: `"set up ai"` (opens AI settings tab)
- **Pointer & Dwell**: `"dwell on"`, `"dwell off"`, `"dwell menu"`, `"precision mode on/off"`, `"relative mode"`, `"absolute mode"`
- **Window Management**: `"show desktop"`, `"minimize"`, `"maximize"`, `"switch window"`, `"close window"`, `"lock workstation"`, `"screenshot"`
- **Media Controls**: `"volume up"`, `"volume down"`, `"mute"`, `"unmute"`, `"play"`, `"pause"`, `"next track"`, `"previous track"`
- **App Launchers**: `"open Chrome"`, `"open Edge"`, `"open VS Code"`, `"open File Explorer"`, `"open Terminal"`, `"open Notepad"`, `"open Calculator"`, `"open Task Manager"`, `"open Spotify"`
- **Web Navigation**: `"open YouTube"`, `"open GitHub"`, `"search for [query]"`
- **Voice Training & Calibration**: `"train my voice"`, `"calibrate voice"`, `"learn my voice"`, `"enroll voice"`
- **Stark Telemetry & Diagnostics**: `"run diagnostics"`, `"suit status"`, `"power levels"`, `"status report"`
- **Stark Protocols**: `"house party protocol"`, `"stealth mode"`, `"clean slate protocol"`, `"target practice"`
- **Stark Conversational Wits**: `"thank you jarvis"`, `"who are you"`, `"are you there"`
- **Updates**: `"check for updates"`, `"install update"`

---

## 8. Discord-Style Auto-Updater

Implemented in [`updater.py`](file:///c:/Programming/Jarvis/updater.py):
- **Background Checks**: Automatically queries the GitHub Releases API (`repos/TalibIbrahim/JARVIS/releases/latest`) on startup and every 4 hours.
- **Semver Comparison**: Correctly parses Semantic Versioning strings (e.g. `v1.1.0` vs `v1.0.0`).
- **Non-Blocking Download**: Streams update asset packages in chunks on a background thread while updating the GUI progress bar.
- **Atomic In-Place Replacement**: Spawns a detached PowerShell replacer script that:
  1. Waits for the running Jarvis process PID to terminate.
  2. Overwrites the executable files in-place with the new release payload.
  3. Automatically relaunches the updated executable.

---

## 9. Diagnostics, Benchmarks & Test Suite

### Benchmark Results ([`diagnose_report.md`](file:///c:/Programming/Jarvis/diagnose_report.md))

| Benchmark Metric | Baseline Pipeline | Palm & Fist (Default) | Pinch Scheme (Calibrated) | Acceptance Spec |
| :--- | :---: | :---: | :---: | :---: |
| **Resting Cursor Jitter (Std Dev)** | 4.02 px | **0.00 px (deadband)** / 1.48 px raw | **0.48 px (deadband)** / 2.69 px raw | < 1.5 px |
| **Pre-Click Closing Drift** | 20.3 px | **0.0 px (Lookback Buffer)** | **0.0 px (Lookback Buffer)** | 0.0 px |
| **24x24 px Target Hit Rate** | ~4/10 (40%) | **10 / 10 (100%)** | **10 / 10 (100%)** | $\ge$ 9/10 (90%) |
| **Mean Distance to Target Center** | 18.5 px | **2.2 px** | **3.8 px** | < 12.0 px |
| **Camera Capture FPS** | 20.0 FPS | **30.0 FPS** (MJPG threaded) | **30.0 FPS** (MJPG threaded) | $\ge$ 30 FPS |
| **Cursor Output Refresh Rate** | ~30 Hz (camera lock) | **120+ Hz (decoupled)** | **120+ Hz (decoupled)** | $\ge$ 120 Hz |
| **Stuck Mouse Buttons / Keys** | Occasional | **0 (Failsafe guaranteed)** | **0 (Failsafe guaranteed)** | 0 |

### Anchor Point Stability Ranking
Evaluation of 7 anatomical hand anchor candidates over a 20-second stationary capture stream:
1. **Knuckles MCP Centroid (5, 9, 13, 17)**: **1.48 px raw jitter (Winner)** - Zero displacement during hand closure.
2. **Palm Center (Wrist 0 + Middle MCP 9)**: 1.27 px raw jitter - High stability, minor tilt displacement.
3. **Index MCP (5)**: 1.48 px raw jitter - Rigid knuckle, slight lateral bias.
4. **Weighted Blend (PIP 6 + Tip 8)**: 2.69 px raw jitter - Deflects 15–20 px when thumb closes.
5. **Raw Index Tip (8)**: 4.02 px raw jitter (Baseline) - Severe tremor and 25–35 px click drift.

### Unit Test Suite
Run the test suite with:
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
- **138 tests passing in ~2.8 seconds**:
  - `test_gestures.py` (26 tests): Open palm movement, fist clicks, double clicks, drag & drop, neutral zone deadband, wake/standby rules, handedness symmetry, anchor stability.
  - `test_dwell.py` (5 tests): Hover timing, radius bounds, latching, gesture pausing, radial sector selection.
  - `test_single_instance.py` (2 tests): Win32 mutex acquisition and multi-instance rejection.
  - `test_updater.py` (5 tests): Semver comparisons, asset matching, download verification.
  - `test_ai.py` (41 tests): Groq API key validation (200, 401, 429, timeout, offline), Whisper STT, chat completions, sliding-window rate limiting, action whitelist enforcement, rejection of unauthorized shell calls, keyring credential manager storage with dev env fallback, Google SR fallback on network/quota failure, VAD silence rejection, and multi-turn history reset.
  - `test_ironman_voice.py` (46 tests): Siri-style live prompt posting, Stark telemetry reporting, system diagnostics, and action dispatch.
  - `test_decision_engine.py` (13 tests): Sub-15ms (< 1ms actual) non-autoregressive decision classification, volume/media/desktop/Iron Man protocol intent prototypes, System 1 reflex execution, System 2 fallback routing, and acoustic voice profile enrollment/verification.

---

## 10. Configuration Reference (`jarvis_config.json`)

All runtime options can be overridden via `jarvis_config.json` in the root directory:

```json
{
  "camera_index": 0,
  "frame_width": 1280,
  "frame_height": 720,
  "camera_fps": 30,
  "camera_mjpg": true,
  "threaded_capture": true,
  "model_complexity": 1,
  "min_detection_confidence": 0.6,
  "min_tracking_confidence": 0.6,
  "show_preview": false,

  "control_scheme": "palm_fist",
  "palm_anchor": "knuckles",
  "open_enter_thresh": 0.70,
  "open_exit_thresh": 0.58,
  "fist_enter_thresh": 0.28,
  "fist_exit_thresh": 0.38,
  "fist_click_max_hold": 0.40,
  "fist_double_window": 0.65,
  "fist_drag_min_hold": 0.35,
  "reopen_freeze_ms": 150.0,
  "thumbs_up_hold": 1.2,
  "pose_debounce_frames": 2,

  "box_margin": 0.18,
  "anchor_point": "blend",
  "anchor_blend_weight": 0.65,

  "tracking_mode": "relative",
  "pointer_speed": 1.15,
  "accel_gain": 1.8,
  "accel_threshold": 0.035,
  "precision_mode": true,
  "precision_gain": 0.35,
  "precision_threshold": 0.015,
  "deadzone_px": 1.5,
  "clutch_enabled": true,

  "activate_hold": 0.8,
  "deactivate_hold": 1.5,
  "palm_grace": 0.15,
  "pinch_down": 0.28,
  "pinch_up": 0.42,
  "pinch_freeze": 0.55,
  "drag_hold": 0.35,
  "double_click_window": 0.45,
  "zoom_step": 0.035,
  "zoom_enter_delay": 0.25,
  "scroll_step": 0.025,
  "scroll_invert": false,
  "lost_grace": 0.4,

  "smooth_min_cutoff": 0.6,
  "smooth_beta": 0.02,
  "smooth_d_cutoff": 1.0,
  "cursor_rate_hz": 120,
  "cursor_lookback_ms": 200.0,

  "dwell_mode": "click",
  "dwell_time": 1.0,
  "dwell_radius": 14.0,
  "dwell_repeat_delay": 0.8,

  "voice_enabled": true,
  "speak": true,
  "wake_word": "jarvis",
  "command_window": 6.0,
  "mic_device_index": null,
  "mic_energy_threshold": 400,
  "mic_dynamic_energy": true,

  "ai_enabled": false,
  "ai_provider": "groq",
  "ai_llm_model": "llama-3.1-8b-instant",
  "ai_stt_model": "whisper-large-v3-turbo",
  "ai_timeout": 10,
  "ai_max_history": 6,

  "theme_mode": "auto",
  "glass_blur": true,
  "reduce_motion": false,
  "show_skeleton_mini": true,

  "auto_update_check": true
}
```

> [!IMPORTANT]
> **API Key Security**: The Groq API key is **never** written to `jarvis_config.json`, logs, crash reports, or git commits. It is stored securely in the Windows Credential Manager via `keyring` (under service `JARVIS`), with optional fallback to the `GROQ_API_KEY` environment variable during development.

---

## 11. Command-Line Interface (CLI)

```powershell
# Standard run (Dynamic Island overlay + background tracking)
python main.py

# Run with Camera Vision HUD preview window showing tracked hand skeleton
python main.py --preview

# Run automated diagnostic benchmark (records jitter, latency, drift, target tests)
python main.py --diagnose

# Run interactive microphone diagnostic test and voice recognition probe
python main.py --test-mic

# Run without voice recognition (silent presentation mode)
python main.py --no-voice

# Specify alternate camera index
python main.py --camera 1

# Run target practice benchmark
python tests/target_practice.py
python tests/target_practice.py --gui
```

---

## 12. Build & Packaging Instructions

To compile J.A.R.V.I.S. into a standalone Windows executable:
```powershell
# Run the PyInstaller packaging script
python build_exe.py
```
Output executable is generated at:
`dist\jarvis\jarvis.exe`
The build includes all PySide6 binaries, MediaPipe graph models, OpenCV DLLs, and icon assets.
