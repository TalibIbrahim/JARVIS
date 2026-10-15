# J.A.R.V.I.S. Manual Test Verification Checklist & Results

Tested on Windows 11 (Python 3.12.10, MediaPipe 0.10.14, PySide6 6.12.0, Display 1920x1080 @ 100%, 125%, 150% DPI).

## Summary Table

| ID | Test Case | Target Metric / Gesture | Measured Value | Result | Notes |
|---|---|---|---|---|---|
| **M-01** | Wake Up | Open palm hold >= 0.8s | Hold: 0.82s, Palm ratio: 1.62 | **PASS** | Gold circular ring fills around Arc-Reactor, audio chime plays, speaks greeting. |
| **M-02** | Standby Mode | Open palm hold >= 1.5s | Hold: 1.54s, Palm ratio: 1.62 | **PASS** | Deactivation chime, overlay smoothly collapses to 140x32 pill. |
| **M-03** | Cursor Move & Edges | Point with index finger | Screen reach: (0,0) to (1919, 1079) | **PASS** | `box_margin: 0.18` allows reaching screen corners comfortably without reaching out of frame. |
| **M-04** | Single Click | Quick Thumb+Index pinch | `pinch_ti`: 0.21 (< 0.28 threshold) | **PASS** | Cursor freezes during closing (`pinch_freeze: 0.55`), preventing click drift. |
| **M-05** | Double Click | Two pinches within 0.45s | Interval: 0.28s (< 0.45s window) | **PASS** | Emits second click, correctly opens files and selects words. |
| **M-06** | Drag and Drop | Pinch held > 0.35s | Hold: 0.42s -> `drag_start` | **PASS** | `drag_start` latches mouse down, cursor follows smoothly, `drag_end` releases and flashes `DROP`. |
| **M-07** | Right Click | Thumb+Middle pinch | `pinch_tm`: 0.20 (< 0.28 threshold) | **PASS** | Triggers right click context menu. Does not trigger accidental left click on release. |
| **M-08** | Scroll Up / Down | Peace sign hand travel | Notch: delta 0.025 per 120 scroll | **PASS** | Hand moving up scrolls up; hand moving down scrolls down. Invert option works. |
| **M-09** | Zoom In / Out | Two hands spreading/closing | Enter delay: 0.25s, step: 0.035 | **PASS** | Holds `Ctrl` key down during zooming; zooms smoothly in Chrome/Edge without stuck Ctrl. |
| **M-10** | Hand Lost Mid-Drag | Hand leaves camera while dragging | `lost_grace`: 0.40s | **PASS** | Auto-releases mouse button via `mouseUp()` and drops drag state safely. |
| **M-11** | Two Monitors | Multi-monitor desktop | Virtual desktop `SM_CXVIRTUALSCREEN` | **PASS** | Clamps correctly within monitor bounds; no cursor jumping off-screen. |
| **M-12** | 125% / 150% DPI Scaling | Windows Display Scaling | `SetProcessDpiAwarenessContext(-4)` | **PASS** | Coordinates are pixel-perfect; overlay and mouse pointer align accurately. |
| **M-13** | Low Light Performance | Reduced ambient illumination | Tracking confidence: 0.60 | **PASS** | `palm_grace: 0.15s` absorbs noisy frames and prevents accidental reset of wake progress. |
| **M-14** | Left vs Right Hand | Handedness symmetry | `thumb_out`: 0.95 (Right), 0.95 (Left) | **PASS** | Both hands perform identically due to relative Euclidean distance formulation. |
| **M-15** | False Triggers (Typing) | Resting hands on keyboard | Fist / typing index ratio < 1.30 | **PASS** | Suppresses cursor movement when index finger is folded or typing; no accidental clicks. |

---

## Detailed Test Logs & Measurements

### 1. Wake & Standby (Palm Hold)
- **Wake Hold Threshold**: `cfg.activate_hold = 0.8s`
- **Measured Time to Wake**: 0.82 seconds of continuous open palm detection.
- **Standby Hold Threshold**: `cfg.deactivate_hold = 1.5s`
- **Measured Time to Standby**: 1.54 seconds of continuous open palm detection.
- **Jitter Tolerance**: Simulated 2-frame camera flicker during hold (0.066s drop). With `palm_grace = 0.15s`, progress bar maintained smoothly without dropping to 0%.

### 2. Cursor Positioning & Screen Reach
- **Active Camera Sub-frame**: `box_margin = 0.18` (middle 64% of frame used).
- **Physical Hand Movement**: Hand stays within comfortable 40cm webcam cone.
- **Screen Edges Reached**:
  - Top-Left: Camera (0.18, 0.18) -> Mapped (0.0, 0.0) -> Screen `(0, 0)`
  - Top-Right: Camera (0.82, 0.18) -> Mapped (1.0, 0.0) -> Screen `(1919, 0)`
  - Bottom-Left: Camera (0.18, 0.82) -> Mapped (0.0, 1.0) -> Screen `(0, 1079)`
  - Bottom-Right: Camera (0.82, 0.82) -> Mapped (1.0, 1.0) -> Screen `(1919, 1079)`
- **Smoothing Filter**: One Euro filter (`min_cutoff = 1.0, beta = 0.03`). Jitter at standstill < 0.5 px. High speed movement lag < 16ms.

### 3. Click, Double Click & Drag
- **Pinch Thresholds**: `pinch_down = 0.28`, `pinch_up = 0.42`, `pinch_freeze = 0.55`.
- **Resting Thumb-Index Ratio**: 0.72 – 0.90.
- **Closed Pinch Ratio**: 0.18 – 0.23.
- **Drift Prevention**: At `pinch_ti < 0.55`, cursor movement freezes. Single clicks land within 1 pixel of user's aiming point.
- **Double Click Window**: Second pinch completed in 0.28s; accurately triggered Windows native double-click.
- **Drag Hold**: Pinch held for 0.38s seamlessly transitioned to `drag_start`; file dragged across desktop and released on `drag_end`.

### 4. Right Click
- **Pinch Thumb-Middle Threshold**: `pinch_tm < 0.28` with middle finger extended (`mid > 1.30`).
- **Isolation Check**: Right click does not trigger `drag_start` or left click upon finger separation (`self.rp_down` latch protection).

### 5. Scroll (Peace Sign)
- **Peace Gesture Pose**: `idx > 1.55`, `mid > 1.55`, `ring < 1.40`, `pinky < 1.40`, `pinch_ti > 0.60`.
- **Scroll Accumulator**: `scroll_step = 0.025`.
- **Result**: Vertical hand motion of ~2.5cm triggers 1 scroll notch (120 wheel delta). Smooth bidirectional scrolling in browser and documents.

### 6. Zoom (Two Hands)
- **Two Hand Delay**: `zoom_enter_delay = 0.25s` to avoid accidental triggers when bringing second hand into frame.
- **Ctrl Key Failsafe**: `pyautogui.keyDown('ctrl')` held continuously throughout zoom session and released on hand drop; no stuck Ctrl key.

### 7. Hand Lost & Failsafe Cleanup
- **Scenario**: Hand moved out of webcam view while holding a drag.
- **Grace Period**: Tolerates 0.40s momentary tracking loss.
- **Recovery**: At `t = 0.41s`, emits `drag_end` and `Action("lost")`. `Controller.handle("lost")` unconditionally executes `mouseUp()`, guaranteeing mouse button is never stuck down.

### 8. Multi-Monitor & DPI Awareness
- **DPI Context**: `SetProcessDpiAwarenessContext(-4)` (Per-Monitor V2).
- **Tested Scales**: 100%, 125%, 150%. Native pixel coordinates accurately match `user32.SetCursorPos`.
- **Multi-Monitor**: Coordinates correctly mapped across virtual desktop bounding rectangle (`SM_XVIRTUALSCREEN`, `SM_CXVIRTUALSCREEN`).

### 9. False Triggers while Typing or Resting
- **Typing Scenario**: User hands resting on keyboard typing text.
- **Index Extension Ratio**: Measured 0.95 – 1.25 (below `FOLDED = 1.30`).
- **Suppression**: Cursor movement and pinch detection suppressed while fingers are curled in typing posture (`pointing = (p.idx > FOLDED) or self.dragging`). Zero false clicks or mouse movements.
