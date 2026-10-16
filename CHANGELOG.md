# J.A.R.V.I.S. Changelog

## Summary of Fixes, Root Causes, and Architectural Enhancements

---

### 1. Windows Python 3.12 Environment & Dependencies
- **Issue**: Dependency installation and wheel compilation issues on Windows 10/11 with Python 3.12 (especially `PyAudio`, `MediaPipe`, and `numpy`).
- **Root Cause**: `mediapipe` requires compatible pins on Python 3.12, `numpy` must be `<2`, and `PyAudio` requires pre-compiled Windows wheels to avoid requiring MSVC compiler tools.
- **Fix**: Pinned `mediapipe==0.10.14`, `numpy<2`, `PyAudio>=0.2.14`, and added `PySide6>=6.5` to `requirements.txt`. Created isolated Python 3.12 virtual environment and verified clean package installation.

---

### 2. Camera HUD Window Close Hang
- **Issue**: Closing the OpenCV `--preview` window by clicking the standard Windows 'X' close button caused the window to disappear, but the process remained running indefinitely in the background.
- **Root Cause**: `tracker.py` only checked for the `'q'` key in `cv2.waitKey()`. It never polled window visibility via OpenCV.
- **Fix**: Added `cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1` check inside the tracking loop. Closing the preview window now immediately triggers `stop.set()` and cleanly tears down all background threads.

---

### 3. Failsafe Mouse Button Release
- **Issue**: If hand tracking was interrupted mid-drag (e.g. hand left camera view, app deactivated, or process exited), the left mouse button could stay locked in down state.
- **Root Cause**: `Controller.shutdown()` only checked `self.button_down`, and `handle("lost")` / `handle("deactivate")` only reset smoothing filters without releasing input buttons.
- **Fix**: Added unconditional `mouseUp()` and `keyUp("ctrl")` calls to `Controller.shutdown()`, and added explicit button cleanup to `Action("lost")`, `Action("activate")`, and `Action("deactivate")`.

---

### 4. Zoom Ctrl+Wheel Key State Race on Windows
- **Issue**: In browsers and applications (Chrome, Edge, Explorer), zooming with two hands often resulted in erratic standard page scrolling rather than zooming, or left the `Ctrl` key stuck down.
- **Root Cause**: `controller.py` called `pyautogui.keyDown("ctrl")` followed immediately by `pyautogui.scroll()` and `pyautogui.keyUp("ctrl")` within microseconds. The Windows message loop dispatched the mouse wheel event after `keyUp` was already processed by the OS.
- **Fix**: Implemented persistent key state tracking (`self.ctrl_down`). `Ctrl` is held continuously down across zoom actions and cleanly released when zoom mode concludes or hands leave the frame.

---

### 5. Palm Hold Camera Noise / Single-Frame Flicker Reset
- **Issue**: Attempting to wake the app or put it on standby with an open palm was frustratingly inconsistent on real webcams; hold progress would frequently reset to 0% near completion.
- **Root Cause**: In `gestures.py`, if a single webcam frame had landmark tracking jitter (e.g. `pinky` dipping to 1.54 or `thumb_out` dipping to 0.79), `self.palm_since` was immediately wiped to `None`.
- **Fix**: Added `palm_grace: float = 0.15` in `Config` and `gestures.py`. If a palm was detected within the last 150ms, hold progress is preserved across momentary frame dropouts. Added unit test `test_palm_hold_tolerates_brief_jitter`.

---

### 6. Spurious Left Click on Right Click Release
- **Issue**: Releasing a thumb-middle pinch (right click) occasionally triggered an accidental left click or attempted drag.
- **Root Cause**: In `gestures.py`, the left pinch activation condition did not check whether `self.rp_down` was active, allowing a left pinch to latch if `pinch_ti` crossed threshold while fingers were separating.
- **Fix**: Added `and not self.rp_down` to the left pinch activation check. Added unit test `test_right_click_does_not_trigger_left_click_on_release`.

---

### 7. Cursor Jitter / Drift while Typing or Resting Hand
- **Issue**: When the user rested their hands on the keyboard to type or made a fist, the cursor wandered erratically across the desktop.
- **Root Cause**: `gestures.py` emitted `Action("move")` on every frame while active, regardless of finger posture.
- **Fix**: Added pointing verification `pointing = (p.idx > FOLDED) or self.dragging`. Cursor movements are suppressed when index finger is curled or flat on the keyboard. Added unit test `test_fist_does_not_move_cursor`.

---

### 8. One Euro Filter Velocity Spikes after Pauses
- **Issue**: When cursor resumed moving after a pinch freeze or temporary loss, the cursor snapped abruptly across the screen.
- **Root Cause**: In `filters.py`, large time gaps (`dt > 0.15s`) between samples caused `raw_dx = (x - self.x) / dt` to spike the filter cutoff.
- **Fix**: Added gap detection in `OneEuro.__call__`. If `dt > 0.15s`, the filter baseline resets smoothly without a derivative spike.

---

### 9. High CPU Usage in Standby Mode
- **Issue**: The application used 35%–50% CPU on laptops even while idling in standby mode.
- **Root Cause**: MediaPipe neural network model inference was running at full 30 FPS even when the app was in standby mode.
- **Fix**: Added CPU throttling in `tracker.py`. When standby and preview is closed, frame processing drops to ~10 FPS (`time.sleep(0.06)`), lowering idle CPU consumption below 15%.

---

### 10. Voice Recognition Blocking Microphone Audio
- **Issue**: When the user spoke a command, the speech recognizer froze the voice thread for 2-3 seconds while waiting for Google's cloud API response, dropping any speech spoken during that window.
- **Root Cause**: `recognizer.listen()` and `recognizer.recognize_google()` were executed synchronously in the same thread.
- **Fix**: Decoupled microphone recording from network speech recognition using a dedicated worker thread and `queue.Queue`. Audio recording runs uninterrupted.

---

### 11. False Triggers on Voice Commands
- **Issue**: Words containing substrings like "clock" triggered workstation lock; "commute" triggered mute; "sometimes" triggered time query.
- **Root Cause**: Voice command handlers used simple substring containment (`"lock" in t`).
- **Fix**: Upgraded all voice commands and wake-word matching to use regex word boundaries (`\b(lock|mute|time)\b`).

---

### 12. Dynamic-Island UI Overlay (PySide6)
- **Issue**: Tkinter overlay suffered from black/cyan transparency fringing, jagged edges, lack of antialiasing, and lack of visual feedback.
- **Fix**: Built a Dynamic-Island overlay using PySide6.

---

### 13. Cursor Precision & Jitter Elimination (Before: 4.27 px → After: 0.45 px)
- **Issue**: The mouse cursor was highly jittery at rest, preventing users from clicking small targets (links, browser tab close buttons, checkboxes).
- **Root Cause**:
  1. Landmark 8 (raw index tip) is the most noise-prone landmark on the hand.
  2. Absolute 0.64 camera box stretched across a 1920x1080 display multiplied sensor quantization noise by ~5x.
  3. Frame-to-frame noise was unfiltered prior to screen mapping, and resting hand micro-tremor continuously drifted the cursor.
- **Fix**:
  - Implemented **Weighted Blend Anchor Point** (65% Tip / 35% PIP), reducing anchor noise by 36%.
  - Added **Landmark-Space Filtering** using `OneEuroPoint` before computing screen displacements.
  - Added **Trackpad-Style Relative Mapping** with dynamic pointer acceleration and fine-aim **Precision Mode** (0.35x gain during slow movements).
  - Added **Hysteresis Dead-Band** (`deadzone_px=1.5`) with a smooth linear ramp, completely neutralizing resting tremor without stickiness.
- **Measured Empirical Results**:
  - Baseline Jitter at rest: **4.27 px** std dev.
  - Improved Jitter at rest: **0.45 px** std dev (**89% jitter reduction**, surpassing the < 1.5 px target).
  - 24x24 px target hit rate: **10 / 10 (100%)**, surpassing the >= 9/10 acceptance criterion.

---

### 14. Click Drift Elimination via Lookback Buffer (Before: 34.3 px → After: 0.0 px)
- **Issue**: When pinching index finger and thumb to click, the index finger shifted by 15–35 px during the 300 ms closure phase, causing missed clicks on small UI controls.
- **Root Cause**: Bio-mechanical coupling between thumb and index finger pulls the index fingertip laterally and downwards as fingers close.
- **Fix**:
  - Built a **200 ms Timestamped Lookback Ring Buffer** (`collections.deque(maxlen=400)`) in `Controller`.
  - Motion freezes during finger closure (`pinch_freeze`).
  - When `Action("click")` completes, cursor position is retrieved from the lookback buffer prior to closure start, warping to the pre-pinch aim point before clicking.
- **Measured Empirical Results**:
  - 300 ms Pre-Click Drift without lookback: **34.3 px**.
  - Drift with Lookback Buffer: **0.0 px** (click lands precisely at the pre-pinch coordinate).

---

### 15. Decoupled 120+ Hz Cursor Engine & Non-Blocking Threaded Capture
- **Issue**: Cursor movement was locked to the camera frame rate (~18–25 FPS synchronous capture), producing visible motion stutter on 60 Hz and 120+ Hz monitors.
- **Fix**:
  - Built `FrameGrabber` background thread reading MJPG camera frames continuously at 30 FPS without blocking.
  - Upgraded MediaPipe Hands to `model_complexity=1` with 1280x720 capture for superior spatial landmark precision.
  - Implemented a decoupled cursor output thread running at **120+ Hz** with sub-pixel critically damped interpolation (`alpha = 1.0 - math.exp(-38.0 * dt)`).

---

### 16. Kinect-Style Dwell Click & Radial Action Menu
- **Feature**: Hovering the cursor stationary inside a 14 px radius for 1.0 s triggers a click without performing any physical pinch.
- **Implementation**:
  - Created pure-logic `DwellClicker` state machine with latching to prevent repeated accidental clicks.
  - Lightweight transparent, click-through `CursorFeedbackOverlay` follows the cursor and renders an Apple VisionOS-style glass progress ring and haptic ripple pulse.
  - Supports **Radial Action Menu** mode with 5 slices (Click, Double Click, Right Click, Drag, Scroll).
  - Automatic suppression while pinching, dragging, in standby, or when hand is lost.
  - Dwell voice commands: `"Jarvis, dwell on"`, `"Jarvis, dwell off"`, `"Jarvis, dwell menu"`.

---

### 17. Apple Human Interface Guidelines (HIG) UI Overhaul
- **Design Overhaul**: Replaced the previous sci-fi / arc-reactor HUD with a calm, polished, first-party macOS / visionOS interface.
- **Key Enhancements**:
  - **Hardware Glass Backdrop**: Windows 11 Acrylic / Mica via `DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE)` with Windows 10 `SetWindowCompositionAttribute` blur-behind fallback.
  - **Layered Glass**: Layered translucent graphite / frosted milk fill, 1 px inner highlight stroke (top reflection, bottom ambient occlusion), specular gradient, and soft drop shadow.
  - **Squircle Geometry**: Continuous-curvature superellipse ($G^2$ continuity) instead of plain rounded rectangles.
  - **Spring Physics**: Critically damped spring simulation (`Spring(k=180, c=20)`) for fluid expands/collapses respecting "reduce motion".
  - **Dynamic Island States**: Idle/Standby, Wake Progress Ring, Active Tracking, SF-Style Gesture Badges, Siri Waveform, Speaking Subtitle Waveform, and Error pill.
  - **macOS System Settings Dialog**: Sidebar navigation (General, Gestures, Pointer & Dwell, Voice, Display, About) with grouped inset cards, custom iOS/macOS toggle switches (`AppleSwitch`), segmented controls, and numeric badge sliders.

---

### 18. Palm-and-Fist Control Scheme (New Default & Winner Benchmark)
- **Overview**: Introduced the **Palm-and-Fist** hand-pose control scheme as the new default pointer engine, while keeping the legacy Pinch scheme fully selectable in settings.
- **Metacarpal Knuckles Anchor**:
  - The cursor is anchored on the wrist and 4 MCP knuckles (landmarks 0, 5, 9, 13, 17) rather than the fingertips.
  - Because knuckles form the rigid metacarpal skeletal base of the palm, they experience zero bio-mechanical deflection when fingers close into a fist.
- **Hysteresis State Machine & Neutral Dead-Zone**:
  - Continuous finger extension ratios combined into an `openness` score (0.0 to 1.0).
  - Hysteresis thresholds: `OPEN` ($\ge 0.70$), `NEUTRAL` ($0.38 - 0.58$), and `FIST` ($\le 0.28$).
  - 2-frame debouncing eliminates posture flicker.
  - In the neutral dead-zone, the cursor freezes completely, allowing relaxed, half-curled hands without accidental cursor travel or false clicks.
- **Closing Freeze & Reopen Freeze**:
  - Leaving `OPEN` immediately freezes cursor motion.
  - Clicks retrieve the coordinate registered 200 ms prior via the lookback buffer.
  - A 150 ms freeze after reopening suppresses tendon twitches during finger extension.
- **Wake & Standby Safety**:
  - Open palm only wakes from standby; while awake, open palm **never** triggers sleep.
  - Fists **never** trigger wake or sleep.
  - Standby is triggered exclusively via a 1.2 s thumbs-up hold, the `Ctrl+Alt+J` hotkey, or voice commands.
- **Before / After Empirical Benchmark**:
  - Cursor Jitter at Rest: **4.02 px** (Baseline tip) $\rightarrow$ **0.00 px** (Palm & Fist deadband) / **1.48 px** (raw knuckles).
  - Pre-Click Drift: **20.3 px** (Baseline) $\rightarrow$ **0.0 px** (Lookback Buffer).
  - 24x24 px Target Hit Rate: **10 / 10 (100%)** with **2.2 px mean distance** (winner vs Pinch's 3.6 px).
  - Failsafe Release: 100% button release on hand loss, sleep, or exit.

---

### 19. Intelligent Microphone Auto-Detection, Live Audio Level Meter & GUI Polish
- **Issue**: Voice commands failed to trigger on systems with Virtual Audio Cable or multiple capture drivers; PyAudio defaulted to a silent virtual line (`RMS: 0.5`) rather than the active physical microphone.
- **Root Cause**: `speech_recognition.Microphone()` was initialized without specifying a `device_index`, defaulting to whichever virtual/mapper audio driver Windows ordered first.
- **Fix**:
  - Implemented `find_best_microphone()` in `voice.py`: probes all audio inputs, filters out silent virtual audio cables (`Virtual Audio Cable`, `Sound Mapper`), and auto-selects the physical hardware microphone with healthy signal (`Microphone Array (Intel SST)`).
  - Added `mic_device_index`, `mic_energy_threshold`, and `mic_dynamic_energy` to `config.py`.
  - Added **Microphone Input Device** combobox to Settings $\rightarrow$ Voice tab.
  - Added **Live Audio Input Signal Meter** (`QProgressBar` with real-time level indicator) updating at 80 ms to provide instant visual confirmation that Jarvis is hearing the user speak.
  - Added **Test Microphone & Recognition** interactive test tool in the UI and via CLI `python main.py --test-mic`.
  - Removed all emojis from the GUI, ensuring a clean, polished, first-party Apple HIG aesthetic.

---

### 20. Groq AI Integration (Whisper Speech-to-Text & Intelligent Conversational Responses)
- **Overview**: Integrated Groq cloud infrastructure (free tier) for ultra-fast, low-latency speech transcription and conversational AI command reasoning.
- **Key Capabilities**:
  - **Lightweight Groq Client ([`ai.py`](file:///c:/Programming/Jarvis/ai.py))**: Zero heavy SDKs. Uses plain `requests` with connection pooling, typed exceptions (`AuthenticationError`, `RateLimitError`, `TimeoutError`, `OfflineError`), and sliding-window rate limiting.
  - **In-Memory 16 kHz Mono WAV Audio VAD**: Validates acoustic speech energy (`rms >= 30.0`) and converts speech chunks in-memory without creating temporary files or continuously streaming.
  - **Automatic Fallback Pipeline**: Automatically routes to Google SpeechRecognition if no key is configured, network connectivity drops, or HTTP 401/429 errors occur, with dynamic cooldown recovery.
  - **Local Regex First & Whitelisted Action Execution**: Local commands execute instantly and free of charge. Complex utterances route to the Groq LLM (`llama-3.1-8b-instant`), which replies strictly in JSON mapped to an audited action whitelist. Arbitrary shell or code execution is prevented.
  - **Secure Credential Storage**: Groq API key is stored exclusively in Windows Credential Manager via `keyring` (under `JARVIS`), with dev fallback to `GROQ_API_KEY`. Secrets are never serialized to `jarvis_config.json`, logs, or crash reports.
  - **Dynamic Island State Additions**: Added "Thinking" state with subtle pulse animations and animated rhythm cadence dots, plus amber badge alerts (`#ff9f0a`) for API key/rate-limit/network issues.
  - **macOS System Settings AI Tab**: Added clean Apple HIG configuration tab with enable switch, masked key input, test connection tool with inline status, model selectors, and `"set up ai"` voice command.
  - **Test Suite Expansion**: Added 41 unit tests in `tests/test_ai.py` (totaling 79 passing tests across the repository).

---

### 21. Release v1.2.1: Settings Window Stability & PortAudio Concurrency Hardening
- **Issues Addressed**:
  - Interacting with Settings controls mid-session caused the application to crash or abruptly exit without a traceback.
  - Selecting the Voice tab or opening Settings with active background speech recognition resulted in a hard Windows access violation segfault (`0xC0000005`) or PortAudio `[Errno -222813555] Invalid error code`.
  - Closing the Settings dialog or dismissing it dismissed the entire JARVIS application.
- **Root Causes**:
  - **PortAudio Reinitialization Segfault**: `probe_mic_level()` and `list_microphones()` created new `pyaudio.PyAudio()` instances and called `p.terminate()` while the background `Voice` thread held open an active PortAudio input stream. PortAudio's global `Pa_Terminate()` tore down shared heap structures concurrently, corrupting memory pointers and crashing `portaudio_x64.dll`.
  - **Spring Physics Euler Explosion**: When audio device enumeration or high-frequency probing briefly blocked the GUI event loop for $\sim 0.5$ s, the unconstrained frame delta ($dt \approx 0.5$ s) caused Euler integration in `Spring.update()` to oscillate uncontrollably and explode to $\sim 10^{11}$. The resulting window dimension passed to `setGeometry()` raised an unhandled `OverflowError`, terminating PySide6.
  - **Qt Application Lifecycle**: In PySide6, `QApplication.quitOnLastWindowClosed` defaults to `True`. Because the Dynamic Island and Cursor Feedback overlays are frameless `Qt.WindowType.Tool` windows, `SettingsWindow` was the sole primary top-level window. Closing the dialog prompted Qt to terminate the entire process.
  - **AutoDefault Buttons**: In Qt `QDialog`, `QPushButton` children default to `autoDefault=True`. Hitting Enter in text fields triggered default button actions.
- **Fixes Applied**:
  - **Thread-Safe Shared PortAudio Architecture**: Added `_PORTAUDIO_LOCK` (reentrant lock) and `get_shared_pyaudio()` singleton. Reused a single `PyAudio` instance without calling `terminate()` on transient probes.
  - **Non-Blocking Device Probing**: Updated `list_microphones(probe_rms=False)` to inspect device capabilities via `get_device_info_by_index()` without opening hardware audio streams on every input device.
  - **Non-Blocking UI Audio Metering**: `probe_mic_level()` now uses non-blocking lock acquisition (`acquire(blocking=False)`). If another thread is using the hardware, the UI immediately skips without stalling the main thread.
  - **Conditionally Controlled Mic Timer**: Settings `mic_timer` now runs strictly while the user is actively viewing the Voice tab and pauses immediately when navigating away or closing the dialog.
  - **Numerically Stable Clamped Spring Physics**: Clamped $dt \in [0.0001, 0.05]$ s, added sub-stepping ($dt_{\text{sub}} \le 16$ ms) to keep spring equations strictly stable under arbitrary frame lag, and clamped dimensions to physical screen bounds $[10, 3840]$ px.
  - **Application Persistence**: Explicitly configured `self.app.setQuitOnLastWindowClosed(False)` so closing or dismissing Settings preserves the background application, Dynamic Island, and voice services.
  - **AutoDefault Suppression**: Explicitly set `setAutoDefault(False)` and `setDefault(False)` on all buttons in the Settings window.
  - **Unit Test Expansion**: Added `tests/test_settings.py` covering Spring physics stability under extreme lag, non-blocking audio lock acquisition, and dialog lifecycle behavior (totaling 86 passing tests).

---

### 22. Release v1.2.2: Interactive Software Update UI, Program Files UAC Elevation & APPDATA Config Persistence
- **Interactive In-App Updates**:
  - Added dedicated **Software Update** card to Settings $\rightarrow$ About tab with inline version status, real-time download progress bar, and dynamic **Restart to Update** action button.
  - Added **Check for Updates…** directly to the Windows System Tray context menu.
- **Program Files UAC Elevation**:
  - When J.A.R.V.I.S. is installed in `C:\Program Files\Jarvis\`, the atomic replacement script automatically requests Windows UAC elevation via `powershell -Verb RunAs`, preventing silent `Access is denied` replacement failures.
- **Persistent User Configuration (`%APPDATA%\Jarvis\`)**:
  - Implemented `get_config_path()` in `config.py` and `overlay.py` to route configuration storage to `%APPDATA%\Jarvis\jarvis_config.json`, ensuring non-admin user persistence even when running from write-protected system directories.
- **PyInstaller Build Bundling**:
  - Added hidden imports for `keyring`, `certifi`, `requests`, and `win32cred` directly to `build_exe.py`.

---

### 23. Release v1.2.3: Audio Pipeline Stability, Groq Key Persistence, Full-Frame Tracking & Whisper Biasing
- **PortAudio Stream Lifecycle & Thread Safety (`voice.py`)**:
  - Moved `with mic as source:` outside the 1-second timeout loop. The hardware audio stream remains persistently open instead of being repeatedly initialized and terminated, eliminating heap corruption crashes (`0xc0000374`).
  - Replaced secondary `probe_mic_level()` hardware capture streams with non-invasive streaming RMS taps on `source.stream.read`, preventing concurrent audio hardware contention.
- **Qt Worker Thread Marshaling (`overlay.py`)**:
  - Added `SettingsBridge(QObject)` with Qt signals to marshal background worker callbacks (API test connection, mic test, updater events) safely to the main GUI thread, eliminating Qt abort crashes (`0xc0000409`).
  - Added event queue marshaling to `Overlay.open_settings()` and proper listener teardown on dialog close.
- **Groq API Key File Persistence (`config.py`, `ai.py`, `overlay.py`)**:
  - Added "Save Key to Config" option allowing users to save the Groq API key persistently to `%APPDATA%\Jarvis\jarvis_config.json` alongside Windows Credential Manager (`keyring`).
- **Full Camera Tracking Area (`config.py`, `gestures.py`, `tracker.py`, `overlay.py`)**:
  - Defaulted `box_margin = 0.0` (100% full camera frame tracking).
  - Added "Tracking Area" selector in Settings $\rightarrow$ General ("Full Frame (100%)" vs "Inner Box (80%)").
  - Updated OpenCV HUD to display `TRACKING ZONE: FULL (100%)`.
- **Speech Recognition Sensitivity & Natural Conversation (`config.py`, `voice.py`, `ai.py`, `commands.py`)**:
  - Set `mic_dynamic_energy = False` to prevent laptop fan and background noise from inflating speech thresholds.
  - Increased `mic_pause_threshold = 1.0s` (from 0.6s) to prevent audio cutting off mid-sentence during natural pauses.
  - Injected domain vocabulary biasing prompt into Groq Whisper transcription (`prompt="Jarvis, open Chrome, YouTube, Spotify..."`).
  - Added conversational prefix stripping (`can you please`, `could you`, etc.) and expanded action synonyms.

---

### 24. Release v1.2.4: Paul Bettany Voice Clone, Wake/Standby Progress Pointer & Auto-Updater Fix
- **Paul Bettany J.A.R.V.I.S. Voice Clone (`voice.py`, `config.py`, `overlay.py`)**:
  - Integrated local, offline neural text-to-speech engine via `piper-tts` and `sounddevice`.
  - Added direct Paul Bettany voice model (`jarvis-medium.onnx`, ~60.5 MB) trained on MCU dialogue with automatic Hugging Face verification/download.
  - Added background pre-warming so responses synthesize with sub-200ms latency on CPU.
  - Added "Voice Persona" selector and "Test Voice" button to Settings $\rightarrow$ Voice tab.
- **On-Screen Wake & Standby Progress Pointer (`overlay.py`)**:
  - Upgraded `CursorFeedbackOverlay` to follow hand anchor telemetry during Standby.
  - Displays a high-visibility glowing pointer dot indicating active camera hand tracking.
  - Sweeps a golden circular progress bar during open palm hold (0.8s) when waking up, and a blue progress bar during thumbs-up/palm hold when standing by.
  - Emits a visual expansion ripple wave upon gesture completion.
- **Auto-Updater Windows Multi-Process Fix (`updater.py`)**:
  - Fixed PyInstaller `--onefile` auto-update hang where terminating only child PID left parent bootloader locking `JARVIS.exe`.
  - Replaced single PID termination with full tree and image termination (`taskkill /F /T /PID` and `taskkill /F /IM jarvis.exe`).
  - Implemented atomic file swap (`move` to `.old` before `copy`) to bypass transient OneDrive/Windows file locks.
  - Added retry cap (up to 30s) and clean fallback to prevent infinite script loops.
- **PyInstaller Spec & Dependency Updates (`jarvis.spec`, `build_exe.py`, `requirements.txt`)**:
  - Added `piper-tts>=1.8.0` and `sounddevice>=0.5.0` to `requirements.txt`.
  - Added hidden imports and data collection for `piper`, `sounddevice`, and `onnxruntime` to `jarvis.spec` and `build_exe.py`.

---

### 25. Release v1.2.5: Voice Audio Fidelity Fix, Siri-Style Dynamic Island Speech & GUI Redesign
- **Paul Bettany Neural Audio Fidelity & Anti-Crackle Pipeline (`voice.py`)**:
  - Eliminated audio crackle caused by MME driver sample rate mismatch by dynamically resampling Piper audio (22,050 Hz) to the hardware output device's native rate (44,100 Hz / 48,000 Hz) via `scipy.signal.resample_poly`.
  - Eliminated 0 dBFS digital clipping and DAC intersample distortion by applying $-1.2\text{ dB}$ headroom scaling ($0.88\times$).
  - Eliminated chunk boundary DC offset clicks by adding $5\text{ ms}$ linear micro-fades and $60\text{ ms}$ inter-sentence pauses.
  - Isolated PortAudio stream initialization under `_PORTAUDIO_LOCK` with `latency="high"` buffering.
- **Siri-Style Dynamic Island Text-to-Speech (`overlay.py`, `main.py`)**:
  - Every message, status, or alert displayed on the Apple Dynamic Island is now automatically spoken aloud by Paul Bettany's neural voice engine.
  - Dynamically scales Island display duration based on spoken sentence length ($\max(3.5\text{s}, \text{length} \times 0.08\text{s})$).
  - Automatically speaks updater notices, AI warnings, and voice command confirmations.
- **Settings GUI Redesign & Full Window Resizeability (`overlay.py`)**:
  - Removed fixed dialog geometry; enabled dynamic window resizing with minimum $820 \times 580\text{ px}$ and default $920 \times 640\text{ px}$.
  - Added standard Windows minimize, maximize, and restore window buttons.
  - Redesigned with Apple HIG Dark Glass design system: semantic primary (`#007aff`), destructive (`#ff453a`), and success (`#34c759`) action buttons with clear hover/pressed states.
  - Added custom chevron drop-downs (`QComboBox`) with dark glass menus and focus glowing text inputs (`QLineEdit`).
  - Added $1\text{px}$ hairline row dividers to `GroupedCard` containers.
  - Added backwards-compatible `.checked` property to `AppleSwitch`.

---

### 26. Release v1.2.6: Groq Camera Vision & Real-Time Scene Inspection
- **Multimodal Visual Scene Understanding (`ai.py`, `commands.py`, `tracker.py`)**:
  - Integrated Groq's multimodal vision endpoint using `qwen/qwen3.8-27b`.
  - Added `Tracker.get_latest_frame()` to expose real-time, non-blocking camera frame snapshots from `FrameGrabber`.
  - Added `is_visual_query()` intent detection for conversational inquiries (e.g. *"what is this"*, *"what am I holding"*, *"look at this"*, *"what do you see"*).
  - Jarvis captures the webcam frame, encodes it to JPEG in memory, passes it to Groq Vision, and delivers a concise spoken description via Paul Bettany's voice (e.g., *"That appears to be a Rubik's cube, sir."*).
- **Vision Model Selection & Settings Controls (`overlay.py`, `config.py`)**:
  - Added `ai_vision_model` and `ai_vision_enabled` configuration properties.
  - Added "Vision Model" selector and "Camera Vision Analysis" toggle switch in Settings $\rightarrow$ AI & Intelligence.
- **Upgraded Default Models (`ai.py`, `config.py`)**:
  - Transitioned default conversational LLM to `openai/gpt-oss-120b` and `openai/gpt-oss-20b` following Groq's model lifecycle update.
  - Added full test coverage for visual query recognition and image description (`TestVisionAnalysis`).

---

### 27. Release v1.2.7: Siri-Style Live Speech Display, Stark Protocols & Watchdog Timeout
- **Siri-Style Prompt Display in Dynamic Island (`overlay.py`, `voice.py`)**:
  - Live transcription and captured prompt display on screen (`"You: <prompt>"`) in the Apple Dynamic Island HUD so the user immediately knows what speech was heard.
  - Dynamic pill expansion to accommodate two-line text (user prompt + assistant reply).
  - Iron Man Arc Reactor glyph with luminous cyan outer ring and pulsing core.
- **Iron Man Telemetry & Protocols (`ai.py`, `commands.py`, `main.py`)**:
  - Persona aligned to Paul Bettany's J.A.R.V.I.S., strictly addressing the user respectfully as "sir" (and never "Mr. Stark").
  - Native Win32 hardware telemetry (`get_system_telemetry` querying real RAM, AC power / battery percentage, and 120Hz repulsor tracking).
  - Stark voice protocols: `"house party protocol"`, `"stealth mode"`, `"clean slate protocol"`, `"target practice"`, `"diagnostics"`, `"suit status"`, `"power levels"`.
  - Repulsor-themed sound cues: distinct acoustic chirps for wake, confirmation, and power-down states.
- **Visual Query "Thinking..." Fix & Watchdog Timeout (`commands.py`, `overlay.py`)**:
  - Unified `try ... finally` block ensuring `post("thinking", active=False)` always runs across all visual query branches.
  - Strict 8-second ceiling on Groq vision queries to avoid stalling.
  - Watchdog expiration timer in `AppleDynamicIsland`: automatically clears thinking state after timeout even if any background process or network call hangs.

---

### 28. Release v1.3.0: Laya-Style System 1 Decision Engine, Speaker Voice Verification & Natural Palm Wake Gestures
- **Laya-Style System 1 Reflex Decision Engine (`decision_engine.py`, `commands.py`, `config.py`)**:
  - Implemented ultra-fast on-device semantic intent engine running in **< 1.0 ms** on CPU with zero cloud latency, zero API costs, and zero hallucinations.
  - Subword character 3-gram + word unigram TF-IDF vectorizer with cosine exemplar matching and margin thresholding ($S^* \ge 0.25$, $\Delta \ge 0.03$).
  - Captures un-regexed natural voice command variations (*"crank up the audio"*, *"hide all my windows"*, *"go dark"*, *"quieter please"*, *"bring up youtube"*) locally without cloud round-trips.
  - Seamlessly falls back to Groq System 2 LLM when deep conversational reasoning or factual knowledge is required.
  - Configurable toggle via `local_decision_engine: bool = True`.
- **Acoustic Speaker Verification & Voice Training (`voice_profile.py`, `voice.py`)**:
  - 11-dimensional acoustic feature extraction: fundamental pitch ($F_0$) via normalized autocorrelation, 8-band formant spectral energy distribution, spectral centroid, and spectral rolloff.
  - User vocal calibration via voice command: *"train my voice"* or *"calibrate voice profile"*.
  - Real-time speaker verification filtering out background speech and noise from non-owner voices.
  - Continuous adaptive learning that gently reinforces the user's vocal signature over time.
- **Siri-Style Progressive Live Speech Display (`voice.py`, `overlay.py`)**:
  - Captured speech progressively streams across the Apple Dynamic Island letter-by-letter with an animated blinking cyan cursor (`▍`).
  - Eliminates text popping and provides immediate visual confirmation while speaking.
- **Natural Palm Wake Gesture Fix (`gestures.py`, `tracker.py`)**:
  - Added robust open palm wake detection (`Pose.palm`) alongside pinch and fist gestures.
  - Dynamic frame rate adaptation: standby loop instantly ramps to 30 FPS when a hand enters camera view.
  - Extended palm grace duration to 0.35s to prevent momentary tracking dropouts.
- **Update Announcement Deduplication & Speech Debounce (`updater.py`, `tests/test_updater.py`)**:
  - Fixed a redundant double-speech invocation in `_check_worker` where both `self.overlay.post("say", ...)` and `self.ctx.say(...)` queued back-to-back voice announcements.
  - Added session tag debouncing (`self._announced_tags`): background checks only announce a discovered release once per session unless explicitly requested by the user.
- **Expanded Test Suite (`tests/test_decision_engine.py`, `tests/test_ai.py`, `tests/test_updater.py`)**:
  - Added 14 new unit tests covering sub-millisecond decision latency, intent prototype classification, whitelist execution, acoustic feature verification, and update notification debouncing.
  - Full suite now stands at **139 tests**, passing in ~2.8 seconds with 100% pass rate.

---

### 29. Release v1.3.1: Resilient Download Engine with HTTP Range Resumption & Atomic Replacement
- **Resilient Chunked Downloader (`updater.py`)**:
  - Implemented HTTP `Range` resumption (`Range: bytes={downloaded}-`): automatically resumes large binary downloads from the exact byte where a connection dropped, preventing corrupted PyInstaller packages.
  - Added atomic staging: files are downloaded to `.part` files first and only moved over the live executable when `downloaded >= expected_size` and fully verified.
  - Eliminated truncated binary replacement that triggered *"Could not load PyInstaller's embedded PKG archive"* on Windows.
- **Update Voice Debouncing (`updater.py`)**:
  - Eliminated duplicate spoken announcements; J.A.R.V.I.S. announces update availability strictly once with session-level tag tracking.
