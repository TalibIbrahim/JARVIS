"""Manual repro (touches the real camera): Settings prewarm list_cameras() at +delay s
overlapping FrameGrabber.open_camera().

Single run: python tools/repro/cam_startup_race.py [delay_s]     (negative = no prewarm)
Wrapper:    python tools/repro/cam_startup_race.py [delay_s] --runs N
"""
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def wrapper(delay, runs):
    rows = []
    for i in range(runs):
        p = subprocess.run([sys.executable, os.path.abspath(__file__), str(delay)],
                           capture_output=True, text=True, timeout=120)
        out = p.stdout + p.stderr
        av = "access violation" in out.lower()
        res = next((ln for ln in out.splitlines() if ln.startswith("RESULT")), "(no RESULT)")
        rows.append((i + 1, p.returncode, av, res))
    print(f"{'run':>3} {'exit':>12} {'access_violation':>17}  result")
    for r in rows:
        print(f"{r[0]:>3} {r[1] & 0xFFFFFFFF:>#12x} {str(r[2]):>17}  {r[3]}")
    bad = sum(1 for r in rows if r[1] != 0 or r[2])
    print(f"crashed {bad}/{runs}")


def single(delay):
    import faulthandler
    faulthandler.enable()
    sys.path.insert(0, ROOT)
    from config import load_config
    from tracker import FrameGrabber, list_cameras

    cfg = load_config()
    stop = threading.Event()
    g = FrameGrabber(cfg, stop)
    res = {}

    def tracker_side():
        t = time.perf_counter()
        res["open"] = g.open_camera()
        res["open_ms"] = (time.perf_counter() - t) * 1000
        res["idx"] = getattr(g, "_active_cam_idx", None)
        g.start()

    th = threading.Thread(target=tracker_side, daemon=True)
    th.start()
    if delay >= 0:
        time.sleep(delay)
        t = time.perf_counter()
        cams = list_cameras()
        print(f"prewarm list_cameras at +{delay}s took {(time.perf_counter()-t)*1000:.0f} ms -> "
              f"{[c['index'] for c in cams]}", flush=True)
    th.join(15)
    print(f"tracker open={res.get('open')} idx={res.get('idx')} in {res.get('open_ms', -1):.0f} ms", flush=True)
    last = g.latest_t
    n = 0
    prev = time.perf_counter()
    gap = 0.0
    end = prev + 8
    while time.perf_counter() < end:
        _, ft = g.get_latest()
        if ft != last:
            now = time.perf_counter()
            gap = max(gap, now - prev)
            prev = now
            last = ft
            n += 1
        time.sleep(0.002)
    print(f"RESULT frames_8s={n} max_gap_ms={gap*1000:.0f} stale_ms={(time.perf_counter()-prev)*1000:.0f} "
          f"failures={g.failures} opened={g.opened} reconnecting={g.reconnecting}", flush=True)
    stop.set()
    time.sleep(0.3)
    g.release()


if __name__ == "__main__":
    args = list(sys.argv[1:])
    runs = 0
    if "--runs" in args:
        k = args.index("--runs")
        runs = int(args[k + 1])
        del args[k:k + 2]
    delay = float(args[0]) if args else 1.5
    if runs > 0:
        wrapper(delay, runs)
    else:
        single(delay)
