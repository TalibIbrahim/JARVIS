"""
Discord-style auto-updater for J.A.R.V.I.S.

Features:
- Checks for new releases on GitHub in the background without blocking the UI.
- Displays non-intrusive update indicators in the Dynamic Island and System Tray.
- Downloads the release's JARVIS-Setup-<version>.exe (Inno Setup) with progress, a size check and a
  SHA-256 check when the release publishes a .sha256 asset.
- Installs by launching that installer silently (/VERYSILENT /SUPPRESSMSGBOXES /NORESTART) and exiting
  Jarvis; the running exe is never replaced directly. Running from source never self-updates.
- Allows manual "Check for Updates" from the Settings dialog and System Tray.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request

import jarvis_log as _jlog
from version import APP_VERSION  # noqa: F401  (re-exported; defined in version.py)
GITHUB_REPO = "TalibIbrahim/JARVIS"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALLER_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
_INSTALLER_RE = re.compile(r"^jarvis-setup-.+\.exe$", re.I)


def parse_version(tag):
    """Parses 'v1.2.3' or '1.2.3' into a tuple of ints (1, 2, 3)."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", tag or "")
    if m:
        major = int(m.group(1))
        minor = int(m.group(2))
        patch = int(m.group(3)) if m.group(3) else 0
        return (major, minor, patch)
    return (0, 0, 0)


def is_newer(remote_tag, current_ver=None):
    if current_ver is None:
        current_ver = APP_VERSION
    return parse_version(remote_tag) > parse_version(current_ver)


def is_frozen():
    """True only in the packaged app; source runs never self-update."""
    return bool(getattr(sys, "frozen", False))


def select_assets(assets):
    """Pick the installer asset (JARVIS-Setup-<ver>.exe) and its optional .sha256 sibling from a release's
    asset list. Returns {"name","url","size","sha256_url"}; every value is empty/0/None when not found."""
    out = {"name": None, "url": None, "size": 0, "sha256_url": None}
    for a in assets or []:
        name = str(a.get("name", ""))
        if _INSTALLER_RE.match(name):
            out.update(name=name, url=a.get("browser_download_url"), size=int(a.get("size") or 0))
            break
    if out["name"]:
        want = out["name"].lower()
        for a in assets:
            n = str(a.get("name", "")).lower()
            if n in (want + ".sha256", want[:-4] + ".sha256"):
                out["sha256_url"] = a.get("browser_download_url")
                break
    return out


def installer_command(installer_path):
    """Command line that runs the downloaded installer silently."""
    return [installer_path, *INSTALLER_ARGS]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class AutoUpdater:
    def __init__(self, ctx=None, overlay=None, auto_check=True):
        self.ctx = ctx
        self.overlay = overlay
        self.auto_check = auto_check
        self.latest_info = None
        self.downloaded_path = None
        self.is_checking = False
        self.is_downloading = False
        self.download_progress = 0
        self.status = "idle"
        self._listeners = []
        self._announced_tags = set()

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._listeners:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def _notify(self, state, **kwargs):
        self.status = state
        log = _jlog.get_logger("updater")
        if self.overlay:
            try:
                self.overlay.post("updater", state=state, **kwargs)
            except Exception:
                log.exception("overlay updater post failed")
        for cb in self._listeners:
            try:
                cb(state, **kwargs)
            except Exception:
                log.exception("updater listener failed")

    def _address(self):
        try:
            import personal
            return personal.address_word()
        except Exception:
            _jlog.get_logger("updater").exception("address lookup failed")
            return "sir"

    def check_async(self, notify_if_latest=False):
        """Asynchronously checks for updates on a background thread."""
        if self.is_checking:
            return
        threading.Thread(target=self._check_worker, args=(notify_if_latest,), daemon=True).start()

    def _check_worker(self, notify_if_latest=False):
        self.is_checking = True
        self._notify("checking")
        try:
            req = urllib.request.Request(API_URL, headers={"User-Agent": "Jarvis-AutoUpdater"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            tag_name = data.get("tag_name", "")
            if is_newer(tag_name):
                found = select_assets(data.get("assets", []))
                if not found["url"]:
                    _jlog.get_logger("updater").warning("release %s has no JARVIS-Setup-<version>.exe asset", tag_name)
                self.latest_info = {
                    "version": tag_name,
                    "name": data.get("name") or tag_name,
                    "body": data.get("body", "No release notes provided."),
                    "html_url": data.get("html_url", ""),
                    "asset_url": found["url"],
                    "asset_name": found["name"],
                    "asset_size": found["size"],
                    "sha256_url": found["sha256_url"],
                }
                self._notify("available", info=self.latest_info)
                # Announce verbally only once per version tag (or if explicitly requested by user)
                if notify_if_latest or (tag_name not in self._announced_tags):
                    self._announced_tags.add(tag_name)
                    spoken_msg = f"Update {tag_name} is available, {self._address()}."
                    if self.overlay:
                        self.overlay.post("say", text=spoken_msg)
                    elif self.ctx and hasattr(self.ctx, "say"):
                        self.ctx.say(spoken_msg)
            else:
                self._notify("up-to-date", current=APP_VERSION)
                if notify_if_latest:
                    spoken_msg = f"Jarvis is up to date (v{APP_VERSION})."
                    if self.overlay:
                        self.overlay.post("say", text=spoken_msg)
                    elif self.ctx and hasattr(self.ctx, "say"):
                        self.ctx.say(spoken_msg)
        except Exception as exc:
            _jlog.get_logger("updater").error("update check failed: %s", exc)
            self._notify("error", message=str(exc))
        finally:
            self.is_checking = False

    def download_async(self, progress_callback=None, completion_callback=None):
        """Streams the installer into %TEMP%\\jarvis_updater (size and optional SHA-256 verified)."""
        if not is_frozen():
            self._notify("error", message="Updates install only from the installed app, not from source.")
            return
        if not self.latest_info or not self.latest_info.get("asset_url"):
            self._notify("error", message="No installer (JARVIS-Setup-<version>.exe) found for this release.")
            return
        if self.is_downloading:
            return
        threading.Thread(target=self._download_worker, args=(progress_callback, completion_callback), daemon=True).start()

    def _fetch_expected_sha256(self):
        """SHA-256 from the release's .sha256 asset, or None when the release does not publish one."""
        url = (self.latest_info or {}).get("sha256_url")
        if not url:
            return None
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-AutoUpdater"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read(4096).decode("utf-8", errors="replace")
        m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
        if not m:
            raise ValueError("checksum asset has no SHA-256 value")
        return m.group(1).lower()

    def _download_worker(self, progress_callback, completion_callback):
        log = _jlog.get_logger("updater")
        self.is_downloading = True
        self.download_progress = 0
        self._notify("downloading", percent=0)
        try:
            info = self.latest_info
            url = info["asset_url"]
            temp_dir = os.path.join(tempfile.gettempdir(), "jarvis_updater")
            os.makedirs(temp_dir, exist_ok=True)
            name = os.path.basename(info.get("asset_name") or "JARVIS-Setup.exe")
            dest_file = os.path.join(temp_dir, name)
            part_file = dest_file + ".part"
            expected_size = int(info.get("asset_size") or 0)
            try:
                expected_sha = self._fetch_expected_sha256()
            except Exception as exc:
                log.error("update checksum unavailable, refusing to download: %s", exc)
                self._notify("error", message=f"Could not read the update checksum: {exc}")
                return
            if expected_sha is None:
                log.info("release publishes no .sha256; verifying size only")

            last_error = ""
            done = False
            for attempt in range(1, 4):      # first try plus resumes; every failure is logged with its reason
                downloaded = os.path.getsize(part_file) if os.path.exists(part_file) else 0
                if expected_size > 0 and downloaded >= expected_size:
                    done = True
                    break
                headers = {"User-Agent": "Jarvis-AutoUpdater"}
                if downloaded > 0:
                    headers["Range"] = f"bytes={downloaded}-"
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        status = resp.status if hasattr(resp, "status") else resp.getcode()
                        content_len = int(resp.headers.get("Content-Length", 0) or 0)
                        if status != 206:
                            downloaded = 0           # no Range support: restart from byte 0
                        total = (downloaded + content_len) if status == 206 else (content_len or expected_size)
                        with open(part_file, "ab" if status == 206 else "wb") as f:
                            while True:
                                chunk = resp.read(256 * 1024)
                                if not chunk:
                                    break
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total > 0:
                                    pct = min(100, int(downloaded * 100 / total))
                                    if pct != self.download_progress:
                                        self.download_progress = pct
                                        self._notify("downloading", percent=pct)
                                        if progress_callback:
                                            progress_callback(pct)
                    if expected_size > 0 and downloaded < expected_size:
                        raise IOError(f"truncated download ({downloaded}/{expected_size} bytes)")
                    done = True
                    break
                except Exception as exc:
                    last_error = str(exc)
                    log.error("update download attempt %d failed: %s", attempt, exc)
            if not done:
                self._notify("error", message=f"Download failed: {last_error or 'incomplete file'}")
                return

            size = os.path.getsize(part_file)
            if expected_size > 0 and size != expected_size:
                os.remove(part_file)
                log.error("update size mismatch: %d != %d", size, expected_size)
                self._notify("error", message="Downloaded update has the wrong size; discarded.")
                return
            if expected_sha is not None and sha256_file(part_file) != expected_sha:
                os.remove(part_file)
                log.error("update SHA-256 mismatch; file discarded")
                self._notify("error", message="Update checksum did not match; discarded.")
                return
            os.replace(part_file, dest_file)
            self.downloaded_path = dest_file
            log.info("update downloaded: %s (%d bytes, sha256 %s)", name, size,
                     "verified" if expected_sha else "not published")
            self._notify("downloaded", path=dest_file, version=info["version"])
            if completion_callback:
                completion_callback(dest_file)
            if self.overlay:
                self.overlay.post("say", text="Update downloaded. Ready to install.")
        except Exception as exc:
            log.exception("update download failed")
            self._notify("error", message=f"Download failed: {exc}")
        finally:
            self.is_downloading = False

    def apply_update_and_restart(self):
        """Run the downloaded installer silently and exit Jarvis so it can replace the files.
        Never replaces the running exe directly. Source runs never self-update."""
        log = _jlog.get_logger("updater")
        if not is_frozen():
            log.info("apply_update ignored: running from source")
            if self.overlay:
                self.overlay.post("say", text="Running from source, so I won't update myself.")
            return False
        if not self.downloaded_path or not os.path.exists(self.downloaded_path):
            self._notify("error", message="Update file not found on disk.")
            return False
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(installer_command(self.downloaded_path), creationflags=flags, close_fds=True)
        except Exception as exc:
            log.exception("could not launch the installer")
            self._notify("error", message=f"Could not start the installer: {exc}")
            return False
        log.info("installer launched; exiting for update")
        self._notify("installing", path=self.downloaded_path)
        if self.ctx:
            self.ctx.quit()
        if self.overlay and hasattr(self.overlay, "quit_app"):
            self.overlay.quit_app()
        return True

    def start_periodic_check(self, interval_hours=4):
        """Starts periodic background checks (every N hours). Returns the worker thread so a
        supervisor can watch it. The loop ends when the app stop event (ctx.stop) is set."""
        stop = getattr(getattr(self, "ctx", None), "stop", None)
        if not isinstance(stop, threading.Event):
            stop = threading.Event()

        def loop():
            # Initial startup check after 8 seconds so the app boots instantly
            if stop.wait(8):
                return
            if self.auto_check:
                self.check_async()

            while not stop.wait(interval_hours * 3600):
                if self.auto_check:
                    self.check_async()

        t = threading.Thread(target=loop, name="AutoUpdaterLoop", daemon=True)
        t.start()
        return t
