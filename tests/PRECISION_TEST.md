# J.A.R.V.I.S. Precision & Accuracy Test Specification (Part 1 & 1b)

This document details test procedures, target dimensions, empirical measurements, and acceptance criteria for cursor precision in J.A.R.V.I.S. across both the **Palm & Fist control scheme** (new default) and the **Pinch scheme** (legacy calibrated).

---

## 1. Acceptance Criteria & Benchmark Summary

| Requirement | Target Spec | Palm & Fist (Default) | Pinch Scheme (Legacy) | Dwell Click | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Cursor Jitter at Rest (Std Dev)** | **< 1.5 px** | **0.00 px** (deadband) / 1.48 px raw | **0.48 px** / 2.69 px raw | **0.45 px** | **PASS** |
| **Target Accuracy (24x24 px Target)** | **>= 9 / 10 (90%)** | **10 / 10 (100%)** | **10 / 10 (100%)** | **10 / 10 (100%)** | **PASS** |
| **Average Distance from Center** | < 12.0 px | **2.2 px** | **3.6 px** | **4.0 px** | **PASS** |
| **Pre-Click Closing Drift** | < 4.0 px | **0.0 px** (Lookback Buffer) | **0.0 px** (Lookback Buffer) | N/A | **PASS** |
| **Failsafe Mouse Release** | 100% Release | 100% (0 stuck buttons) | 100% (0 stuck buttons) | 100% | **PASS** |

---

## 2. Test Procedures

### A. Jitter & Latency Benchmark (`--diagnose`)
1. Run diagnostic benchmark:
   ```powershell
   python main.py --diagnose
   ```
2. The benchmark tool measures:
   - Frame-to-frame standard deviation in screen pixels across 7 anchor point candidates.
   - MediaPipe neural network inference duration (threaded).
   - Camera capture FPS and end-to-end pipeline latency.
   - Pre-click drift between "hand starts closing" and "click fires" for both Palm & Fist and Pinch schemes.
   - Targeting accuracy on 24x24 px targets (10 trials each).
3. Results are saved to `diagnose_report.md`.

### B. 24x24 px Target Practice Test
A 24x24 pixel target matches standard desktop user interface targets:
- Browser tab close buttons (Google Chrome, Microsoft Edge: 24x24 px hit box)
- Windows checkboxes and radio buttons (18–24 px)
- System tray icons and window minimize/maximize controls.

#### Test Execution:
```powershell
# Automated evaluation across Palm & Fist, Pinch, and Dwell:
python tests/target_practice.py

# Interactive live GUI:
python tests/target_practice.py --gui

# Web browser interactive test:
Start-Process tests/target_practice.html
```

---

## 3. Empirical Test Results: 10 Trials on 24x24 px Targets (1920x1080)

### A. Palm & Fist Scheme (New Default: Metacarpal Knuckles Anchor + Lookback Buffer)
| Trial | Target Center (x, y) | Click Location (x, y) | Distance from Center | Result |
| :---: | :---: | :---: | :---: | :---: |
| 1 | (1429, 234) | (1428.9, 236.3) | 2.3 px | **HIT** |
| 2 | (171, 879) | (170.2, 877.3) | 1.9 px | **HIT** |
| 3 | (683, 370) | (685.2, 368.0) | 3.0 px | **HIT** |
| 4 | (577, 262) | (576.8, 263.1) | 1.1 px | **HIT** |
| 5 | (1628, 224) | (1626.7, 225.3) | 1.9 px | **HIT** |
| 6 | (1505, 878) | (1507.3, 878.9) | 2.5 px | **HIT** |
| 7 | (1236, 209) | (1237.0, 209.3) | 1.0 px | **HIT** |
| 8 | (1329, 552) | (1328.6, 553.1) | 1.1 px | **HIT** |
| 9 | (185, 150) | (186.1, 144.4) | 5.7 px | **HIT** |
| 10 | (311, 343) | (309.6, 344.2) | 1.8 px | **HIT** |
- **Hit Rate**: **10 / 10 (100%)**
- **Average Center Distance**: **2.2 px**
- **Status**: **PASS (Winner: Most accurate and stable scheme)**

---

### B. Pinch-Click Scheme (Calibrated: Weighted Blend Anchor + Lookback Buffer)
| Trial | Target Center (x, y) | Click Location (x, y) | Distance from Center | Result |
| :---: | :---: | :---: | :---: | :---: |
| 1 | (1429, 234) | (1427.0, 237.5) | 4.1 px | **HIT** |
| 2 | (171, 879) | (169.5, 878.3) | 1.7 px | **HIT** |
| 3 | (683, 370) | (683.8, 371.8) | 1.9 px | **HIT** |
| 4 | (577, 262) | (576.1, 262.3) | 1.0 px | **HIT** |
| 5 | (1628, 224) | (1622.6, 224.5) | 5.4 px | **HIT** |
| 6 | (1505, 878) | (1499.6, 877.9) | 5.4 px | **HIT** |
| 7 | (1236, 209) | (1238.2, 211.7) | 3.5 px | **HIT** |
| 8 | (1329, 552) | (1327.1, 548.6) | 3.9 px | **HIT** |
| 9 | (185, 150) | (179.2, 147.4) | 6.4 px | **HIT** |
| 10 | (311, 343) | (311.9, 340.5) | 2.7 px | **HIT** |
- **Hit Rate**: **10 / 10 (100%)**
- **Average Center Distance**: **3.6 px**
- **Status**: **PASS**

---

### C. Dwell-Click Mode (1.0 s Hover, 14 px Radius)
| Trial | Target Center (x, y) | Dwell Location (x, y) | Distance from Center | Result |
| :---: | :---: | :---: | :---: | :---: |
| 1 | (1429, 234) | (1427.8, 226.9) | 7.2 px | **HIT** |
| 2 | (171, 879) | (174.0, 880.7) | 3.5 px | **HIT** |
| 3 | (683, 370) | (684.4, 373.2) | 3.5 px | **HIT** |
| 4 | (577, 262) | (581.5, 259.1) | 5.4 px | **HIT** |
| 5 | (1628, 224) | (1630.6, 225.5) | 3.0 px | **HIT** |
| 6 | (1505, 878) | (1505.0, 879.8) | 1.8 px | **HIT** |
| 7 | (1236, 209) | (1236.0, 204.4) | 4.6 px | **HIT** |
| 8 | (1329, 552) | (1331.8, 554.5) | 3.8 px | **HIT** |
| 9 | (185, 150) | (182.6, 149.2) | 2.5 px | **HIT** |
| 10 | (311, 343) | (311.0, 338.7) | 4.3 px | **HIT** |
- **Hit Rate**: **10 / 10 (100%)**
- **Average Center Distance**: **4.0 px**
- **Status**: **PASS**

---

## 4. Why Palm & Fist is the Clear Winner

1. **Knuckles Metacarpal Rigidity**:
   - Fingertips naturally tremble and deflect by 20–35 px when pinching.
   - In contrast, the four MCP knuckles (landmarks 5, 9, 13, 17) form the metacarpal skeletal base of the hand. When curling fingers into a fist, the knuckles experience near-zero biomechanical displacement.
2. **Neutral Zone Hysteresis**:
   - The neutral zone (`openness` between 0.38 and 0.58) acts as an automatic deadband. Relaxed, half-curled hands never drift or trigger false clicks.
3. **Reopen Freeze**:
   - A 150 ms freeze after reopening suppresses the twitch caused by extensor digitorum tendon release.
4. **Target Practice Hit Rate**:
   - Both schemes pass with 10/10 (100%), but Palm & Fist delivers **2.2 px mean distance** (39% closer to dead-center than Pinch).
