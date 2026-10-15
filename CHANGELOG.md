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
- **Fix**: Built a brand-new Dynamic-Island overlay using PySide6:
  - Frameless, translucent, click-through (`WS_EX_TRANSPARENT`), always-on-top.
  - Smooth spring-style 60 FPS animations between Standby (140x32) and Active (280x44 to 450x64).
  - Tony Stark Arc-Reactor motif with spinning segmented blades and pulsing core.
  - Circular gold wake-progress ring during palm hold.
  - Distinct state rendering: Standby, Tracking, Gesture Flash badges, Listening, Speaking (with animated 5-bar audio waveform), and Error states.
  - Live mini hand-skeleton visualizer embedded inside the island.
  - System tray icon with wake toggle, preview toggle, settings dialog, and clean quit.
  - Full Settings window with camera picker, sensitivity sliders, audio toggles, and live 3-step calibration wizard.
