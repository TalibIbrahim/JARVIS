"""
File & Folder Operations System Adapter for J.A.R.V.I.S. (Phase 4).

Strictly NO Qt imports. 100% unit-testable with pure Python and mocks.

Security Allowlist & Operations:
- open_folder(folder_name): Resolves alias -> indexer fuzzy match. Opens via os.startfile ONLY on directories.
- open_recent_file(folder='downloads', extension=None): Opens newest file in folder.
  SECURITY ALLOWLIST: Files can ONLY be opened if extension is in ALLOWED_EXTENSIONS.
  Reject all executables (.exe, .bat, .cmd, .ps1, .msi, etc.).
- show_last_download(): Finds latest file in Downloads and reveals it in Explorer (explorer /select,<path>).
- remember_folder(alias, path, confirmed=True): Persists alias in config after confirmation.
"""

from dataclasses import dataclass
import os
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


ALLOWED_EXTENSIONS: Set[str] = {
    ".pdf", ".txt", ".md", ".docx", ".xlsx", ".pptx", ".csv",
    ".json", ".png", ".jpg", ".jpeg", ".mp4", ".mp3", ".wav",
    ".py", ".html",
}

DISALLOWED_EXECUTABLE_EXTENSIONS: Set[str] = {
    ".exe", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".js",
    ".com", ".scr", ".pif", ".reg", ".wsf", ".cpl", ".hta",
}


@dataclass
class FileOpResult:
    """Result returned by file and folder operations."""
    success: bool
    message: str
    path: Optional[str] = None

    def __iter__(self):
        """Allows unpacking as (success, message)."""
        return iter((self.success, self.message))


def is_allowed_file(filepath: str) -> bool:
    """
    Validates that a file's extension is strictly in ALLOWED_EXTENSIONS
    and explicitly rejects executables and scripts.
    """
    _, ext = os.path.splitext(filepath)
    ext_lower = ext.lower().strip()
    if not ext_lower:
        return False
    if ext_lower in DISALLOWED_EXECUTABLE_EXTENSIONS:
        return False
    return ext_lower in ALLOWED_EXTENSIONS


def default_opener(path: str) -> None:
    """Default platform opener using os.startfile on Windows."""
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        # Fallback for non-Windows / mock environments
        pass


def default_explorer_launcher(path: str) -> None:
    """Reveals a file or directory in Windows Explorer using explorer /select,<path>."""
    norm_path = os.path.normpath(path)
    subprocess.Popen(f'explorer /select,"{norm_path}"', shell=True)


class FileOperationsAdapter:
    """
    Encapsulates file and folder management behind testable interfaces.
    All system calls (opener, explorer launcher, config persistence, indexer)
    can be injected and mocked.
    """

    def __init__(
        self,
        indexer=None,
        config=None,
        opener: Optional[Callable[[str], None]] = None,
        explorer_launcher: Optional[Callable[[str], None]] = None,
    ):
        self._indexer = indexer
        self._config = config
        self._opener = opener or default_opener
        self._explorer_launcher = explorer_launcher or default_explorer_launcher
        self._lock = threading.RLock()

    def _get_config(self):
        if self._config is not None:
            return self._config
        try:
            from config import load_config
            return load_config()
        except Exception:
            return None

    def _get_indexer(self):
        if self._indexer is not None:
            return self._indexer
        try:
            from folder_indexer import get_folder_indexer
            return get_folder_indexer()
        except Exception:
            return None

    def _get_alias_map(self) -> Dict[str, str]:
        cfg = self._get_config()
        if cfg and hasattr(cfg, "folders_whitelist") and isinstance(cfg.folders_whitelist, dict):
            return {k.lower().strip(): v for k, v in cfg.folders_whitelist.items()}

        # Default standard alias fallbacks
        home = os.path.expanduser("~")
        return {
            "programming": "C:\\Programming" if os.path.exists("C:\\Programming") else os.path.join(home, "Programming"),
            "downloads": os.path.join(home, "Downloads"),
            "documents": os.path.join(home, "Documents"),
            "desktop": os.path.join(home, "Desktop"),
            "pictures": os.path.join(home, "Pictures"),
            "music": os.path.join(home, "Music"),
            "videos": os.path.join(home, "Videos"),
        }

    def open_folder(self, folder_name: str) -> FileOpResult:
        """
        Resolves folder_name via spoken alias -> indexer fuzzy match.
        Opens via opener (os.startfile) ONLY on directories (os.path.isdir).
        """
        clean_name = str(folder_name).lower().strip(" '\"")
        if not clean_name:
            return FileOpResult(False, "No folder name specified, sir.", None)

        aliases = self._get_alias_map()
        target_path: Optional[str] = None

        # 1. Exact or normalized alias lookup
        if clean_name in aliases:
            target_path = aliases[clean_name]
        else:
            # Check if clean_name without 'folder' suffix matches alias (e.g. 'downloads folder' -> 'downloads')
            stripped = clean_name.replace(" folder", "").replace(" directory", "").strip()
            if stripped in aliases:
                target_path = aliases[stripped]

        # 2. If not found in aliases or alias path does not exist, resolve via indexer fuzzy matching
        if not target_path or not os.path.exists(target_path):
            indexer = self._get_indexer()
            if indexer:
                matched = indexer.find_best_match(clean_name, min_score=0.3)
                if matched and os.path.exists(matched):
                    target_path = matched

        # 3. Direct path check if user gave absolute path
        if not target_path and os.path.isabs(folder_name) and os.path.exists(folder_name):
            target_path = folder_name

        if not target_path or not os.path.exists(target_path):
            return FileOpResult(False, f"Folder '{folder_name}' could not be located, sir.", None)

        # STRICT CONSTRAINT: Must be a directory!
        if not os.path.isdir(target_path):
            return FileOpResult(False, f"Security restriction: '{target_path}' is not a directory.", None)

        try:
            self._opener(target_path)
            base = os.path.basename(os.path.normpath(target_path)) or target_path
            return FileOpResult(True, f"Opening {base} folder, sir.", target_path)
        except Exception as exc:
            return FileOpResult(False, f"Failed to open folder '{target_path}': {exc}", target_path)

    def open_recent_file(self, folder: str = "downloads", extension: Optional[str] = None) -> FileOpResult:
        """
        Opens the newest file in the given folder.
        SECURITY ALLOWLIST: Files can ONLY be opened if extension is in ALLOWED_EXTENSIONS.
        Rejects all executables.
        """
        clean_folder = str(folder).lower().strip()
        aliases = self._get_alias_map()
        target_dir = aliases.get(clean_folder)

        if not target_dir or not os.path.exists(target_dir):
            if os.path.isdir(folder):
                target_dir = folder
            else:
                indexer = self._get_indexer()
                if indexer:
                    matched = indexer.find_best_match(clean_folder)
                    if matched and os.path.isdir(matched):
                        target_dir = matched

        if not target_dir or not os.path.isdir(target_dir):
            return FileOpResult(False, f"Directory for '{folder}' could not be found, sir.", None)

        # Format extension filter if provided
        norm_ext = None
        if extension:
            norm_ext = extension.lower().strip()
            if not norm_ext.startswith("."):
                norm_ext = f".{norm_ext}"

        # Collect candidate files
        candidates: List[Tuple[str, float]] = []
        try:
            with os.scandir(target_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            if norm_ext:
                                _, ext = os.path.splitext(entry.name)
                                if ext.lower() != norm_ext:
                                    continue
                            stat = entry.stat()
                            candidates.append((entry.path, stat.st_mtime))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError) as exc:
            return FileOpResult(False, f"Cannot access folder '{target_dir}': {exc}", None)

        if not candidates:
            filter_msg = f" with extension '{norm_ext}'" if norm_ext else ""
            return FileOpResult(False, f"No recent files found in {os.path.basename(target_dir)}{filter_msg}, sir.", None)

        # Sort descending by mtime (newest first)
        candidates.sort(key=lambda x: x[1], reverse=True)
        newest_file = candidates[0][0]
        filename = os.path.basename(newest_file)

        # SECURITY ALLOWLIST ENFORCEMENT
        if not is_allowed_file(newest_file):
            _, file_ext = os.path.splitext(filename)
            return FileOpResult(
                False,
                f"Security alert: File '{filename}' has unauthorized extension '{file_ext}'. Execution blocked.",
                newest_file,
            )

        try:
            self._opener(newest_file)
            return FileOpResult(True, f"Opening recent file '{filename}', sir.", newest_file)
        except Exception as exc:
            return FileOpResult(False, f"Failed to open '{filename}': {exc}", newest_file)

    def show_last_download(self) -> FileOpResult:
        """
        Finds the latest file in the user's Downloads folder
        and reveals it in Windows Explorer using explorer /select,<path>.
        """
        aliases = self._get_alias_map()
        downloads_dir = aliases.get("downloads", os.path.join(os.path.expanduser("~"), "Downloads"))

        if not os.path.isdir(downloads_dir):
            return FileOpResult(False, "Downloads folder could not be found, sir.", None)

        candidates: List[Tuple[str, float]] = []
        try:
            with os.scandir(downloads_dir) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            stat = entry.stat()
                            candidates.append((entry.path, stat.st_mtime))
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError) as exc:
            return FileOpResult(False, f"Cannot access Downloads directory: {exc}", None)

        if not candidates:
            return FileOpResult(False, "No downloads found in your Downloads folder, sir.", None)

        candidates.sort(key=lambda x: x[1], reverse=True)
        latest_file = candidates[0][0]
        filename = os.path.basename(latest_file)

        try:
            self._explorer_launcher(latest_file)
            return FileOpResult(True, f"Revealed latest download '{filename}' in Explorer, sir.", latest_file)
        except Exception as exc:
            return FileOpResult(False, f"Failed to reveal '{filename}' in Explorer: {exc}", latest_file)

    def remember_folder(self, alias: str, path: str, confirmed: bool = True) -> FileOpResult:
        """
        Persists alias in config after confirmation.
        Validates that path exists and is an authentic directory.
        """
        clean_alias = str(alias).lower().strip(" '\"")
        abs_path = os.path.abspath(path)

        if not clean_alias:
            return FileOpResult(False, "Alias cannot be empty, sir.", None)

        if not os.path.exists(abs_path) or not os.path.isdir(abs_path):
            return FileOpResult(False, f"Path '{path}' is not a valid directory, sir.", None)

        if not confirmed:
            return FileOpResult(
                False,
                f"Confirmation required before remembering '{abs_path}' as '{clean_alias}', sir.",
                None,
            )

        try:
            from config import remember_folder_alias
            cfg = self._get_config()
            ok = remember_folder_alias(clean_alias, abs_path, cfg=cfg)
            if ok:
                indexer = self._get_indexer()
                if indexer:
                    indexer.add_root(abs_path)
                    indexer.add_folder(abs_path, depth=0)
                return FileOpResult(True, f"Remembered folder '{abs_path}' as '{clean_alias}', sir.", abs_path)
            return FileOpResult(False, "Failed to persist folder alias to configuration file, sir.", None)
        except Exception as exc:
            return FileOpResult(False, f"Error remembering folder alias: {exc}", None)

    def open_file(self, filepath: str) -> FileOpResult:
        """
        Opens a specific file subject to strict ALLOWED_EXTENSIONS validation.
        """
        abs_path = os.path.abspath(filepath)
        if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
            return FileOpResult(False, f"File '{filepath}' not found, sir.", None)

        filename = os.path.basename(abs_path)
        if not is_allowed_file(abs_path):
            _, ext = os.path.splitext(filename)
            return FileOpResult(
                False,
                f"Security violation: File '{filename}' has unauthorized extension '{ext}'. Blocked.",
                abs_path,
            )

        try:
            self._opener(abs_path)
            return FileOpResult(True, f"Opening file '{filename}', sir.", abs_path)
        except Exception as exc:
            return FileOpResult(False, f"Failed to open '{filename}': {exc}", abs_path)


# Global adapter instance and delegation functions
_GLOBAL_ADAPTER: Optional[FileOperationsAdapter] = None
_ADAPTER_LOCK = threading.Lock()


def get_file_ops_adapter() -> FileOperationsAdapter:
    """Returns the process-wide FileOperationsAdapter instance."""
    global _GLOBAL_ADAPTER
    with _ADAPTER_LOCK:
        if _GLOBAL_ADAPTER is None:
            _GLOBAL_ADAPTER = FileOperationsAdapter()
        return _GLOBAL_ADAPTER


def set_file_ops_adapter(adapter: Optional[FileOperationsAdapter]) -> None:
    """Sets or overrides the process-wide FileOperationsAdapter instance."""
    global _GLOBAL_ADAPTER
    with _ADAPTER_LOCK:
        _GLOBAL_ADAPTER = adapter


def open_folder(folder_name: str) -> FileOpResult:
    return get_file_ops_adapter().open_folder(folder_name)


def open_recent_file(folder: str = "downloads", extension: Optional[str] = None) -> FileOpResult:
    return get_file_ops_adapter().open_recent_file(folder=folder, extension=extension)


def show_last_download() -> FileOpResult:
    return get_file_ops_adapter().show_last_download()


def remember_folder(alias: str, path: str, confirmed: bool = True) -> FileOpResult:
    return get_file_ops_adapter().remember_folder(alias=alias, path=path, confirmed=confirmed)


def open_file(filepath: str) -> FileOpResult:
    return get_file_ops_adapter().open_file(filepath)
