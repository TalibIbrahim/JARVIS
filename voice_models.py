"""TTS voice model registry for J.A.R.V.I.S.

Voices are opt-in: nothing here downloads on its own. The UI calls `download()` when the user picks a
voice; `Speaker` only ever calls `get_paths()` and falls back to the system voice when the chosen voice
is not installed.

Adding a voice is one registry entry (a Piper voice: two files; another engine: its own file list and
`engine` tag). Installed voices live in %LOCALAPPDATA%\\JARVIS\\voices\\<id>\\. Installs that predate the
registry (models/voice next to the app, or %APPDATA%\\Jarvis\\models\\voice) count as installed.
"""
import os
import sys
import threading

import jarvis_log

_log = jarvis_log.get_logger("voice")

_HF_JARVIS = "https://huggingface.co/jgkawell/jarvis/resolve/main/en/en_GB/jarvis/medium"
_HF_PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US"

# id -> entry. `files`: (local file name, url, expected size in bytes or None). `model` names the file
# the engine loads; a Piper voice also needs its `.json` next to it. `legacy` lists pre-registry dirs.
# `note` is the short line shown under the Voice row; `license` is the model's license (the two
# rhasspy voices are trained on non-commercial datasets).
REGISTRY = {
    "hfc_male": {
        "name": "Male (fast)",
        "note": "Instant",
        "license": "CC BY-NC-SA 4.0 (non-commercial)",
        "engine": "piper",
        "model": "en_US-hfc_male-medium.onnx",
        "files": [
            ("en_US-hfc_male-medium.onnx.json", _HF_PIPER + "/hfc_male/medium/en_US-hfc_male-medium.onnx.json", 5033),
            ("en_US-hfc_male-medium.onnx", _HF_PIPER + "/hfc_male/medium/en_US-hfc_male-medium.onnx", 63201294),
        ],
    },
    "ryan": {
        "name": "Ryan",
        "note": "Richer, ~1 s delay",
        "license": "CC BY-NC-SA 4.0 (non-commercial)",
        "engine": "piper",
        "model": "en_US-ryan-high.onnx",
        "files": [
            ("en_US-ryan-high.onnx.json", _HF_PIPER + "/ryan/high/en_US-ryan-high.onnx.json", 4166),
            ("en_US-ryan-high.onnx", _HF_PIPER + "/ryan/high/en_US-ryan-high.onnx", 120786792),
        ],
    },
    "jarvis": {
        "name": "J.A.R.V.I.S. (Paul Bettany)",
        "note": "Movie voice",
        "license": "MIT (jgkawell/jarvis)",
        "engine": "piper",
        "model": "jarvis-medium.onnx",
        "files": [
            ("jarvis-medium.onnx.json", _HF_JARVIS + "/jarvis-medium.onnx.json", 7266),
            ("jarvis-medium.onnx", _HF_JARVIS + "/jarvis-medium.onnx", 63511038),
        ],
        "legacy": ("models/voice",),
    },
}

_download_lock = threading.Lock()


def voices_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "JARVIS", "voices")


def _app_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _legacy_dirs(entry):
    dirs = []
    for rel in entry.get("legacy", ()):
        dirs.append(os.path.join(_app_dir(), rel))
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        dirs.append(os.path.join(appdata, "Jarvis", rel))
    return dirs


def _complete(directory, entry) -> bool:
    for fname, _url, size in entry["files"]:
        path = os.path.join(directory, fname)
        if not os.path.isfile(path):
            return False
        if size is not None and os.path.getsize(path) != size:
            return False
    return True


def _find_dir(voice_id):
    entry = REGISTRY.get(voice_id)
    if entry is None:
        return None
    for d in [os.path.join(voices_dir(), voice_id)] + _legacy_dirs(entry):
        if _complete(d, entry):
            return d
    return None


def get_paths(voice_id):
    """{"model": ..., "config": ...} for an installed voice, else None. Never downloads."""
    d = _find_dir(voice_id)
    if d is None:
        return None
    model = os.path.join(d, REGISTRY[voice_id]["model"])
    return {"model": model, "config": model + ".json", "engine": REGISTRY[voice_id]["engine"]}


def is_installed(voice_id) -> bool:
    return _find_dir(voice_id) is not None


def available_voices():
    out = []
    for vid, e in REGISTRY.items():
        total = sum(s or 0 for _f, _u, s in e["files"])
        out.append({"id": vid, "name": e["name"], "size_mb": round(total / (1024.0 * 1024.0), 1),
                    "installed": is_installed(vid), "engine": e["engine"],
                    "note": e.get("note", ""), "license": e.get("license", "")})
    return out


def download(voice_id, progress_cb=None) -> bool:
    """Fetch a voice into %LOCALAPPDATA%\\JARVIS\\voices\\<id>\\. Resumes a partial `.part` file with an HTTP
    Range request and verifies the final size. progress_cb(done_bytes, total_bytes) may be called from
    this thread. Returns True when the voice is installed afterwards, False on any failure (logged)."""
    entry = REGISTRY.get(voice_id)
    if entry is None:
        _log.warning("voice download: unknown voice %r", voice_id)
        return False
    if is_installed(voice_id):
        return True
    if not _download_lock.acquire(blocking=False):
        _log.warning("voice download: another download is running")
        return False
    try:
        import requests
        dest_dir = os.path.join(voices_dir(), voice_id)
        os.makedirs(dest_dir, exist_ok=True)
        total = sum(s or 0 for _f, _u, s in entry["files"])
        done_before = 0
        for fname, url, size in entry["files"]:
            dest = os.path.join(dest_dir, fname)
            if os.path.isfile(dest) and (size is None or os.path.getsize(dest) == size):
                done_before += os.path.getsize(dest)
                continue
            part = dest + ".part"
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            if size is not None and have > size:
                os.remove(part)
                have = 0
            headers = {"Range": "bytes=%d-" % have} if have else {}
            with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                if have and resp.status_code != 206:
                    have = 0        # server ignored the Range request: restart this file
                with open(part, "ab" if have else "wb") as fh:
                    got = have
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        got += len(chunk)
                        if progress_cb:
                            progress_cb(done_before + got, total)
            if size is not None and os.path.getsize(part) != size:
                _log.warning("voice download: %s size %d != expected %d; keeping .part to resume",
                             fname, os.path.getsize(part), size)
                return False
            os.replace(part, dest)
            done_before += os.path.getsize(dest)
        return is_installed(voice_id)
    except Exception as exc:
        _log.warning("voice download failed for %s: %r", voice_id, exc)
        return False
    finally:
        _download_lock.release()


def remove(voice_id) -> bool:
    """Delete the downloaded copy (pre-registry installs next to the app are left alone)."""
    import shutil
    d = os.path.join(voices_dir(), voice_id)
    if voice_id not in REGISTRY or not os.path.isdir(d):
        return False
    shutil.rmtree(d, ignore_errors=False)
    return True
