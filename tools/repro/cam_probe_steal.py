"""Manual repro (touches the real camera): does list_cameras(force_refresh=True)
kill a live FrameGrabber stream, and does a probe during reconnect crash?

Usage: python tools/repro/cam_probe_steal.py [--iterations N] [--sample-secs S]

Verdict line: STREAM_KILLED / CRASH_RISK / OK.
A native crash (e.g. 0xC0000374 heap corruption) kills the process before any verdict
prints; run via a wrapper and treat nonzero exit with no VERDICT line as a crash.
CRASH_RISK is printed when a probe is issued while the grabber is reconnecting.
"""
import argparse
import faulthandler
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
faulthandler.enable()
faulthandler.dump_traceback_later(180, exit=True)

from config import load_config  # noqa: E402
from tracker import FrameGrabber, list_cameras  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=5)
    ap.add_argument("--sample-secs", type=float, default=3.0)
    a = ap.parse_args()

    cfg = load_config()
    stop = threading.Event()
    g = FrameGrabber(cfg, stop)
    t0 = time.perf_counter()
    ok = g.open_camera()
    print(f"grabber open={ok} in {(time.perf_counter()-t0)*1000:.0f} ms idx={getattr(g, '_active_cam_idx', None)}")
    if not ok:
        print("VERDICT: OK (camera could not be opened; nothing to test)")
        return
    g.start()

    def sample(label, secs):
        last_t = g.latest_t
        n = 0
        max_gap = 0.0
        prev = time.perf_counter()
        end = prev + secs
        while time.perf_counter() < end:
            _, ft = g.get_latest()
            if ft != last_t:
                now = time.perf_counter()
                max_gap = max(max_gap, now - prev)
                prev = now
                last_t = ft
                n += 1
            time.sleep(0.002)
        stale = time.perf_counter() - prev
        print(f"{label:28s} frames={n:4d} fps={n/secs:5.1f} max_gap={max_gap*1000:6.0f} ms "
              f"stale_at_end={stale*1000:6.0f} ms failures={g.failures} "
              f"opened={g.opened} reconnecting={g.reconnecting}", flush=True)
        return n, stale

    base_n, _ = sample("baseline", a.sample_secs)
    killed = base_n == 0
    crash_risk = False
    for i in range(a.iterations):
        if g.reconnecting:
            crash_risk = True
            print("  !! probing while grabber is reconnecting (crash risk)", flush=True)
        t = time.perf_counter()
        cams = list_cameras(force_refresh=True)
        print(f"  list_cameras #{i+1} took {(time.perf_counter()-t)*1000:.0f} ms -> {cams}", flush=True)
        n, stale = sample(f"after list_cameras #{i+1}", a.sample_secs)
        if n == 0 or stale > 1000:
            killed = True
    stop.set()
    time.sleep(0.3)
    g.release()
    if killed and crash_risk:
        print("VERDICT: CRASH_RISK (stream killed, and a probe overlapped a reconnect)")
    elif killed:
        print("VERDICT: STREAM_KILLED")
    elif crash_risk:
        print("VERDICT: CRASH_RISK")
    else:
        print("VERDICT: OK")


if __name__ == "__main__":
    main()
