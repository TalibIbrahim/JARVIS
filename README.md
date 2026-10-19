<p align="center">
  <h1 align="center">J.A.R.V.I.S. v2.6.0</h1>
</p>

<h3 align="center">Touchless Hand-Gesture PC Control & Dynamic Island Assistant for Windows</h3>

<p align="center">
  Real-time MediaPipe hand tracking with zero cloud dependencies at runtime,<br>
  an Apple HIG Dynamic Island with 60 FPS spring physics, OpenWakeWord streaming,<br>
  WinRT SMTC media integration, Open-Meteo weather, and Liquid Glass–style settings.
</p>

<p align="center">
  <a href="https://github.com/TalibIbrahim/JARVIS/releases"><img alt="Release" src="https://img.shields.io/badge/release-v2.6.0-00e5ff?style=for-the-badge" /></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-27272a?style=for-the-badge" />
  <img alt="Python" src="https://img.shields.io/badge/python-3.12-3b82f6?style=for-the-badge" />
  <img alt="UI" src="https://img.shields.io/badge/UI-PySide6%2060fps-10b981?style=for-the-badge" />
  <img alt="Tests" src="https://img.shields.io/badge/tests-1000%2B%20passed-brightgreen?style=for-the-badge" />
  <img alt="License" src="https://img.shields.io/badge/license-MIT-27272a?style=for-the-badge" />
</p>

<p align="center">
  <img src="assets/readme/hero.png" alt="J.A.R.V.I.S. Dynamic Island: compact pill, media, calendar, weather and hardware cards" width="900" />
</p>

<p align="center">
  <a href="#whats-new-in-v260">What's New in v2.6</a> &middot;
  <a href="#core-architecture">Architecture</a> &middot;
  <a href="#dynamic-island">Dynamic Island</a> &middot;
  <a href="#settings-control-center">Settings</a> &middot;
  <a href="#gestures">Gestures</a> &middot;
  <a href="#voice--intelligence">Voice & Intelligence</a> &middot;
  <a href="#performance">Performance</a> &middot;
  <a href="#installation">Installation</a> &middot;
  <a href="#testing">Tests</a>
</p>

---

## What's New in v2.6.0

- **Smooth hand tracking again.** Cursor motion is restored to v1 behaviour: every hand movement is applied using the real frame time, the cursor loop no longer busy-waits, and open-palm shortcuts only fire on deliberate fast flicks, so normal pointing never switches windows or changes volume.
- **New gestures.** One-handed zoom (pinch thumb + index, hold, move up/down), a 🤙 *listen* gesture that talks to JARVIS without saying its name, and fast-flick window switching (right = next, left = previous).
- **Listen for "Jarvis".** Like Siri's *Listen for Siri*: turn it off and the microphone stays closed until you make the listen gesture.
- **Hears you over music.** Other apps' volume is ducked while JARVIS listens, plus an optional *Voice Over Music (Experimental)* echo canceller.
- **Reliable voice pipeline.** Speech detection now uses Silero VAD with a noise-adaptive gate (ambient noise no longer counts as speech), "play" no longer toggles twice, background-noise hallucinations can't quit or put JARVIS to sleep, and the island tells you when there is no internet for speech recognition.
- **Redesigned Dynamic Island** matching the Windhawk *Dynamic Island for Windows* look: solid black, continuous (squircle) corners, Media, Calendar, Weather, Hardware and JARVIS pages, real hardware data, and optional synced lyrics.
- **Liquid Glass Settings** with a Clear / Tinted glass style and transparency slider, page order, and a searchable **Commands** page listing everything JARVIS can do.
- **Self-healing workers and diagnostics.** Crashed or stalled camera/voice workers restart automatically, a status dot on the island shows worker health, and a rotating diagnostic log plus `--self-test` make problems easy to report.

---

## Dynamic Island

Built entirely with native PySide6 and running at **60 FPS**, the Dynamic Island floats seamlessly at the top center of your primary screen. It combines `WS_EX_TRANSPARENT` for total click-through transparency during idle states with bounded interactive windows (`WS_EX_NOACTIVATE`) when expanded—ensuring focus is never stolen from your games, IDEs, or full-screen apps.

<table>
  <tr>
    <td align="center"><img src="assets/readme/island_media.png" alt="Now Playing card" width="420" /><br><sub>Now Playing (album-art tint)</sub></td>
    <td align="center"><img src="assets/readme/island_calendar.png" alt="Calendar card" width="420" /><br><sub>Calendar</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="assets/readme/island_weather.png" alt="Weather card" width="420" /><br><sub>Weather</sub></td>
    <td align="center"><img src="assets/readme/island_hardware.png" alt="Hardware monitor card" width="420" /><br><sub>Hardware Monitor</sub></td>
  </tr>
  <tr>
    <td align="center"><img src="assets/readme/island_jarvis.png" alt="JARVIS status card" width="420" /><br><sub>JARVIS status &amp; tracking control</sub></td>
    <td align="center"><img src="assets/readme/island_listening.png" alt="Assistant reply" width="380" /><br><br><img src="assets/readme/island_compact.png" alt="Compact pill" width="230" /><br><sub>Assistant reply &middot; compact pill</sub></td>
  </tr>
</table>

<p align="center"><img src="assets/readme/island_hardware_strip.png" alt="Hardware strip with tracking FPS" width="520" /><br><sub>Hardware strip with live tracking FPS</sub></p>

### Island Pages & States

The island is a solid black stadium with continuous corners, a hairline edge and a soft shadow. Hover (or click, dwell or fist-click) expands it; the mouse wheel, the page dots, a fast vertical flick near the island, or voice (*"next page"*, *"show weather"*) changes pages. It never takes focus and is click-through outside its shape.

| Page / State | What it shows |
|---|---|
| **Compact pill** | Time, weather icon and temperature, plus a status dot (green = healthy, grey = tracking paused, red = a worker is down). With lyrics on and music playing, it grows to show the current lyric line on up to two lines. |
| **Media** | Album art with source badge, title / artist / album, waveform, scrubber with elapsed and remaining time, previous / play-pause / next. Colours are tinted from the album art. Optional synced lyrics (current and next line). |
| **Calendar** | Month, big day number and weekday, month grid with today highlighted; first weekday follows your setting. |
| **Weather** | City, condition icon, temperature, condition, and Wind (with direction), Feels Like and Humidity tiles. The last reading is cached so the pill is never empty. |
| **Hardware** | CPU, RAM, GPU, network up/down and disk with live bars (`--` when a sensor is unavailable), plus a strip variant with the live tracking FPS. Sampled only while visible. |
| **JARVIS** | Assistant state, last command, camera FPS, a large Pause / Resume tracking control, and the reason when a worker is down. |
| **Assistant states** | Wake, listening waveform, thinking shimmer, your words and JARVIS's reply (white text), result and error capsules, disambiguation chips, and the screen-analysis indicator. |
| **Fullscreen** | The island hides while a fullscreen app or game is in front (optional). |

---

## Settings Control Center (Liquid Glass)

<p align="center"><img src="assets/readme/settings_glass.png" alt="Liquid Glass settings window" width="760" /></p>

The Settings window (`ui/settings_window.py`) uses the Liquid Glass material: real Mica / Acrylic blur where Windows supports it, with a solid tinted fallback when Windows transparency effects are off. Every option has a one-line description and applies live.

| Section | Highlights |
|---|---|
| **General** | Camera, startup, assistant options. |
| **Gestures** | Control scheme, gesture shortcuts (flicks, volume, one-handed zoom). |
| **Pointer & Dwell** | Pointer speed, precision aim, dwell mode and dwell time (0.5–2.5 s). |
| **Voice** | Listen for "Jarvis", Listen Gesture, Voice Over Music (experimental), microphone, wake sensitivity, voice. |
| **AI & Intelligence** | Groq API key (stored in Windows Credential Manager), models, vision. |
| **Display** | Theme, Glass Style (Clear / Tinted) and transparency, island shape (Pill / Notch), size, pages on/off and order, compact content, 12/24 h clock, weather city and units, lyrics, pin open, expand trigger, hide during fullscreen. |
| **Commands** | Every voice command and gesture, with search and category filters. |
| **About** | Version, updates, system pre-flight health. |

---

## Gestures & Control Schemes

Choose between two high-precision control schemes in Settings (`Control Scheme`).

### 1. Palm & Fist Scheme (Default)
Anchored on the rigid **metacarpal knuckles (landmarks 0, 5, 9, 13, 17)** rather than moving fingertips, eliminating jitter and closing displacement.

| Gesture | Action | Thresholds & Mechanics |
|---|---|---|
| **Open Palm** | **Move Cursor** | Rigid metacarpal knuckle anchor; trackpad-style relative acceleration + precision aim. |
| **Quick Fist (< 400 ms)** | **Left Click** | Closing fingers into a fist and reopening within 400 ms triggers left click. 200 ms Lookback Buffer locks aim. |
| **Two Quick Fists (< 0.65 s)** | **Double Click** | Two quick fists within 0.65 s fires native Windows double click. |
| **Fist Held (> 350 ms)** | **Drag & Drop** | Latches left mouse button down; cursor follows hand; opening the hand drops the item (`DROP`). |
| **Thumb + Middle Pinch** | **Right Click** | Thumb contacts middle fingertip (`pinch_tm < 0.28`). |
| **Peace Sign (move up / down)** | **Scroll** | Index and middle fingers extended; vertical travel scrolls documents and webpages smoothly. |
| **Two Open Hands (spread / close)** | **Zoom In / Out** | Moving both hands apart zooms in; bringing together zooms out (`Ctrl` + scroll). |
| **Fast Flick Up / Down** | **Volume / Island Page** | A quick vertical flick changes volume by 5% (near the island it flips pages). Slow movement never triggers it. |
| **Fast Flick Right / Left** | **Next / Previous Window** | A quick sideways flick sends Alt+Tab / Alt+Shift+Tab and snaps the cursor back to where it was. Normal pointing never switches windows. |
| **Pinch & Hold (thumb + index), move up / down** | **One-Handed Zoom** | Hold the pinch for 0.3 s, then move up to zoom in or down to zoom out (`Ctrl` + scroll). The cursor stays still. |
| **🤙 Call-Me Sign (hold 0.5 s)** | **Listen** | Thumb and pinky out: JARVIS ducks your media and listens for one command, no wake word needed. |
| **Open Palm in Standby (0.8 s)** | **Wake Up** | Circular golden wake ring completes; app transitions from Standby to Active. |
| **Thumbs-Up (hold 1.2 s)** | **Standby** | 4 fingers curled, thumb extended upward for 1.2 s returns Jarvis to standby. |

### 2. Dwell-Click (Kinect-Style Hover to Click)
- **Hover Trigger**: Hovering still within a 14 px radius for the dwell time (default 1.0 s, adjustable 0.5–2.5 s in Settings → Pointer & Dwell) triggers a left click.
- **Translucent Progress Ring**: An Apple visionOS-style translucent glass ring fills around the cursor and pulses on completion.
- **Radial Action Menu**: Optional mode where hovering reveals an action menu: *Click, Double Click, Right Click, Drag, Scroll*.

<p align="center"><img src="assets/readme/radial_menu.png" alt="Dwell radial menu" width="240" /></p>

---

## Voice & Intelligence

Speak naturally after the wake word *"Jarvis"*:

```
"Jarvis, what's the weather"       "what's playing"         "next track"
"play / pause"                    "open my downloads"      "open programming folder"
"what's 45 times 128"             "convert 50 miles to km" "set timer for 10 minutes"
"what time is it in Tokyo"        "note that review PR"    "inspect my screen"
"volume up"                       "snap window right"      "switch to Chrome"
"copy this"                       "open this"              "close window"
"start work mode"                 "stealth mode"           "run diagnostics"
```

### Listening

- **Wake word**: say *"Jarvis"* or *"Hey Jarvis"*, then your command. JARVIS ducks other apps' audio while it listens and restores it afterwards.
- **Listen for "Jarvis"** (Settings → Voice): turn it off and the microphone stays closed and nothing is recorded until you make the 🤙 listen gesture.
- **Voice Over Music (experimental)**: cancels this PC's own playback from the microphone so commands get through over music (about 15% of one CPU core, only while audio plays).
- Speech is transcribed with Groq Whisper (Google fallback). If there is no internet, the island shows *"No internet – speech unavailable"*.

### Intelligent System Components

- **OpenWakeWord 16 kHz Streaming**: Low-latency, high-accuracy acoustic wake-word neural network running locally on-device.
- **Laya-Style System 1 Reflex Engine**: Sub-millisecond (< 1.0 ms) non-autoregressive intent vectorizer (`decision_engine.py`) classifying commands with zero cloud latency.
- **Conversational Context Memory**: 5-turn in-memory resolver resolving natural pronouns (*"open it"*, *"make that louder"*, *"do that again"*, *"no, the other one"*).
- **Local AST Math & Unit Engine**: Deterministic Python AST arithmetic parser (`+`, `-`, `*`, `/`, `^`, `%`, `sqrt`) and multi-dimensional unit converter without cloud LLM dependencies.
- **Security-Gated Folder Indexer**: Depth-bounded fuzzy indexer with strict extension allowlists preventing arbitrary executable execution.
- **One-Shot Groq Vision** (only when AI is enabled with an API key): Screen awareness for visual troubleshooting (*"explain this error"*, *"what is on my screen"*), with visual camera aperture indicator on the Dynamic Island.
- **Privacy Audit Log**: Thread-safe ring buffer recording timestamps and payload digests for every cloud request.

---

## Performance

Measured on an Intel Core i7-8665U laptop (4 cores / 8 threads), Windows 10, built-in 720p webcam:

| Metric | Measured |
|---|---|
| **Startup to island visible** | about 0.35 s in-process (about 0.55 s including Python start-up); camera and voice start after the first frame |
| **System 1 intent decision** | p50 0.19 ms, p99 0.46 ms |
| **Wake word → island animation** | p99 14 ms |
| **Speech transcript → action** | p99 3 ms warm, about 23 ms for the very first command |
| **Hand inference (MediaPipe)** | 22–37 ms per frame with no other load |
| **Cursor engine** | 121 Hz with glide between camera frames (2–3× smoother motion, about half the lag); 0.0 px pre-click drift (200 ms lookback) |
| **Island rendering** | 0.2–1.5 ms per frame (budget 16.7 ms); repaints only when something visible changes |
| **Settings window** | first build about 0.15 s; other tabs build when first opened |
| **Standby hand scanning** | about a quarter of one CPU core |
| **Memory** | plateaus around 150 MB with no growth over time |

---

## Installation

### Prerequisites
- **Operating System**: Windows 10 or Windows 11 (64-bit).
- **Python**: Python 3.12 (MediaPipe 0.10.14 is pinned for its Windows wheels).
- **Hardware**: Standard USB or built-in webcam, microphone, and audio speakers.

```powershell
# 1. Clone the repository
git clone https://github.com/TalibIbrahim/JARVIS.git
cd JARVIS

# 2. Create and activate a Python 3.12 virtual environment
python -m venv .venv
.\.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch J.A.R.V.I.S.
python main.py
```

### Command Line Arguments
- `python main.py`: Starts Jarvis in normal background mode with the Dynamic Island overlay.
- `python main.py --preview`: Opens the camera HUD window showing hand skeletons and live telemetry.
- `python main.py --island-demo`: Cycles through all 23 Dynamic Island states, widgets, and gesture HUDs.
- `python main.py --test-mic`: Runs interactive microphone and speech recognition diagnostics.
- `python main.py --diagnose`: Runs jitter, latency, anchor point, and pre-click drift benchmarks.
- `python main.py --no-voice`: Disables speech recognition for presentation environments.
- `python main.py --camera 1`: Selects an alternate camera index.
- `python main.py --self-test`: Checks camera, microphone, models, network, media, mutex and the speech detector against your room noise, with timings.
- `python main.py --perf-report`: Prints the latency budget report.

---

## Testing

Run the comprehensive unit test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The suite has over 1,000 tests, including regression tests for every fixed bug, voice replay tests (synthesized speech over measured room noise), synthetic hand-landmark replays that prove 0 px pre-click drift, and pixel-exact visual regression checks of every island page and Settings tab at 100% and 150% DPI (reference images are generated in `tests/goldens/` on the first run and compared on every run after that).

---

## Troubleshooting

- Run `python main.py --self-test` first; it reports what is failing and how long each check took.
- JARVIS writes a rotating diagnostic log to `%LOCALAPPDATA%\JARVIS\logs\jarvis.log` (and native crashes to `crash.log`): worker heartbeats, camera and microphone details, and per-command stages. Transcripts are recorded only as lengths unless you enable `debug_log_transcripts`. Attach these files when reporting a problem.

---

## Packaging to Standalone Executable

To build a standalone Windows executable (`dist/JARVIS.exe`):

```powershell
python build_exe.py
```

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
