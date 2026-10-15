"""
Discord-style auto-updater for J.A.R.V.I.S.

Features:
- Checks for new releases on GitHub in the background without blocking the UI.
- Displays non-intrusive update indicators in the Dynamic Island and System Tray.
- Streams the download with real-time progress.
- Windows in-place self-replacement: generates a detached updater script that waits
  for the current process to exit, swaps the executable, and relaunches the new version.
- Allows manual "Check for Updates" from the Settings dialog and System Tray.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

APP_VERSION = "1.2.2"
GITHUB_REPO = "TalibIbrahim/JARVIS"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


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

    def add_listener(self, callback):
        self._listeners.append(callback)

    def _notify(self, state, **kwargs):
        self.status = state
        if self.overlay:
            try:
                self.overlay.post("updater", state=state, **kwargs)
            except Exception:
                pass
        for cb in self._listeners:
            try:
                cb(state, **kwargs)
            except Exception:
                pass

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
                # Look for standalone JARVIS.exe first, then fallback to zip
                asset_url = None
                asset_name = None
                asset_size = 0
                for a in data.get("assets", []):
                    name = a.get("name", "")
                    if name.lower() == "jarvis.exe":
                        asset_url = a.get("browser_download_url")
                        asset_name = name
                        asset_size = a.get("size", 0)
                        break
                    elif name.lower().endswith(".zip") and not asset_url:
                        asset_url = a.get("browser_download_url")
                        asset_name = name
                        asset_size = a.get("size", 0)

                self.latest_info = {
                    "version": tag_name,
                    "name": data.get("name") or tag_name,
                    "body": data.get("body", "No release notes provided."),
                    "html_url": data.get("html_url", ""),
                    "asset_url": asset_url,
                    "asset_name": asset_name,
                    "asset_size": asset_size,
                }
                self._notify("available", info=self.latest_info)
                if self.overlay:
                    self.overlay.post("say", text=f"Update {tag_name} is available, sir.")
                if self.ctx and getattr(self.ctx, "speaker", None):
                    self.ctx.say(f"An update to Jarvis is available, sir. Version {tag_name.lstrip('v')}.")
            else:
                self._notify("up-to-date", current=APP_VERSION)
                if notify_if_latest and self.overlay:
                    self.overlay.post("say", text=f"Jarvis is up to date (v{APP_VERSION}).")
        except Exception as exc:
            self._notify("error", message=str(exc))
        finally:
            self.is_checking = False

    def download_async(self, progress_callback=None, completion_callback=None):
        """Streams the update file download into a temporary directory."""
        if not self.latest_info or not self.latest_info.get("asset_url"):
            self._notify("error", message="No download asset found for this release.")
            return
        if self.is_downloading:
            return
        threading.Thread(target=self._download_worker, args=(progress_callback, completion_callback), daemon=True).start()

    def _download_worker(self, progress_callback, completion_callback):
        self.is_downloading = True
        self.download_progress = 0
        self._notify("downloading", percent=0)
        url = self.latest_info["asset_url"]
        temp_dir = os.path.join(tempfile.gettempdir(), "jarvis_updater")
        os.makedirs(temp_dir, exist_ok=True)
        dest_file = os.path.join(temp_dir, self.latest_info.get("asset_name") or "JARVIS_update.exe")

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-AutoUpdater"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 64 * 1024

                with open(dest_file, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            if percent != self.download_progress:
                                self.download_progress = percent
                                self._notify("downloading", percent=percent)
                                if progress_callback:
                                    progress_callback(percent)

            self.downloaded_path = dest_file
            self._notify("downloaded", path=dest_file, version=self.latest_info["version"])
            if completion_callback:
                completion_callback(dest_file)
            if self.overlay:
                self.overlay.post("say", text="Update downloaded! Ready to restart.")
        except Exception as exc:
            self._notify("error", message=f"Download failed: {exc}")
        finally:
            self.is_downloading = False

    def apply_update_and_restart(self):
        """
        Replaces the current running executable with the newly downloaded version
        and relaunches Jarvis cleanly.
        """
        if not self.downloaded_path or not os.path.exists(self.downloaded_path):
            self._notify("error", message="Update file not found on disk.")
            return False

        # Current executable location
        current_exe = sys.executable
        is_frozen = getattr(sys, "frozen", False)

        if not is_frozen and not current_exe.lower().endswith("jarvis.exe"):
            # Running from source (python main.py): show where update was saved
            if self.overlay:
                self.overlay.post("say", text="Running from source. Update saved to temp folder.")
            subprocess.Popen(f'explorer.exe /select,"{self.downloaded_path}"', shell=True)
            return True

        # Running as compiled jarvis.exe: perform atomic Windows replacement
        pid = os.getpid()
        bat_script = os.path.join(tempfile.gettempdir(), f"jarvis_update_{pid}.bat")

        # Create self-deleting Windows batch replacer
        bat_content = f"""@echo off
chcp 65001 >nul
echo [JARVIS Updater] Waiting for Jarvis process {pid} to terminate...
:wait_loop
taskkill /PID {pid} /F >nul 2>&1
timeout /t 1 /nobreak >nul
copy /y "{self.downloaded_path}" "{current_exe}" >nul 2>&1
if errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait_loop
)

echo [JARVIS Updater] Update applied successfully! Relaunching...
start "" "{current_exe}"
del "{self.downloaded_path}" >nul 2>&1
(goto) 2>nul & del "%~f0"
exit
"""
        with open(bat_script, "w", encoding="utf-8") as f:
            f.write(bat_content)

        # Launch the updater script in a detached background process (elevating if in Program Files)
        if "program files" in current_exe.lower() and sys.platform == "win32":
            ps_script = f'Start-Process cmd.exe -ArgumentList "/c `"{bat_script}`"" -Verb RunAs'
            subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script])
        else:
            subprocess.Popen(
                ["cmd.exe", "/c", bat_script],
                creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
                close_fds=True
            )

        # Trigger clean application shutdown
        if self.ctx:
            self.ctx.quit()
        if self.overlay:
            self.overlay.quit_app()
        return True

    def start_periodic_check(self, interval_hours=4):
        """Starts periodic background checks (every N hours)."""
        def loop():
            # Initial startup check after 8 seconds so the app boots instantly
            time.sleep(8)
            if self.auto_check:
                self.check_async()

            while True:
                time.sleep(interval_hours * 3600)
                if self.auto_check:
                    self.check_async()

        threading.Thread(target=loop, daemon=True).start()
