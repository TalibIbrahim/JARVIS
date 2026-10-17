"""
Background Folder Indexer for J.A.R.V.I.S. (Phase 4).

Strictly NO Qt imports. 100% unit-testable with pure Python and mocks.

Provides:
- Background scanner indexing user-chosen roots (e.g. C:\\Programming, Documents, Downloads).
- Strict constraints: depth <= 3.
- Exclusion list: Windows, Program Files, Program Files (x86), AppData, .git,
  node_modules, __pycache__, .venv, $RECYCLE.BIN, System Volume Information.
- Fast fuzzy matcher using character 3-grams to resolve queries like
  "programming", "react project", "ai notes" to matching paths.
"""

from dataclasses import dataclass, field
import os
import re
import threading
import time
from typing import Dict, List, Optional, Set, Tuple


# Standard directory exclusion list (case-insensitive)
EXCLUSION_NAMES: Set[str] = {
    "windows",
    "program files",
    "program files (x86)",
    "appdata",
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "$recycle.bin",
    "system volume information",
}


def normalize_text(text: str) -> str:
    """Normalizes text by removing non-alphanumeric chars and collapsing whitespace."""
    clean = re.sub(r"[^\w\s]", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", clean)


def generate_trigrams(text: str) -> Set[str]:
    """
    Extracts character 3-grams (trigrams) from normalized text.
    Handles short words and boundary tokens gracefully.
    """
    clean = normalize_text(text)
    if not clean:
        return set()

    words = clean.split()
    trigrams: Set[str] = set()

    # Per-word boundary-padded trigrams
    for w in words:
        if len(w) <= 2:
            trigrams.add(f"^{w}$")
        else:
            trigrams.add(f"^{w[:2]}")
            for i in range(len(w) - 2):
                trigrams.add(w[i:i + 3])
            trigrams.add(f"{w[-2:]}$")

    # Joined phrase trigrams across word boundaries
    joined = "".join(words)
    if len(joined) >= 3:
        for i in range(len(joined) - 2):
            trigrams.add(joined[i:i + 3])

    return trigrams


def compute_trigram_similarity(query: str, target: str) -> float:
    """
    Computes calibrated fuzzy similarity using character 3-grams and token overlap.
    Returns float score in range [0.0, 1.0].
    """
    clean_q = normalize_text(query)
    clean_t = normalize_text(target)

    if not clean_q or not clean_t:
        return 0.0

    # Exact normalized match
    if clean_q == clean_t:
        return 1.0

    q_words = clean_q.split()
    t_words = clean_t.split()

    if q_words == t_words:
        return 1.0

    # Substring match bonus
    if clean_q in clean_t:
        ratio = len(clean_q) / max(1, len(clean_t))
        return min(1.0, 0.85 + 0.15 * ratio)
    if clean_t in clean_q:
        ratio = len(clean_t) / max(1, len(clean_q))
        return min(1.0, 0.75 + 0.20 * ratio)

    # Word subset match
    if set(q_words).issubset(set(t_words)):
        word_ratio = len(q_words) / max(1, len(t_words))
        return min(1.0, 0.80 + 0.15 * word_ratio)

    q_tri = generate_trigrams(clean_q)
    t_tri = generate_trigrams(clean_t)

    if not q_tri or not t_tri:
        return 0.0

    intersection = len(q_tri.intersection(t_tri))
    if intersection == 0:
        return 0.0

    # Sørensen–Dice coefficient + query coverage
    dice = (2.0 * intersection) / (len(q_tri) + len(t_tri))
    coverage = intersection / len(q_tri)

    score = 0.55 * dice + 0.45 * coverage
    return min(1.0, max(0.0, score))


def is_excluded_directory(dir_name: str, relative_path: str = "") -> bool:
    """
    Checks if a directory name or relative path matches any pattern in EXCLUSION_NAMES.
    """
    name_lower = dir_name.lower().strip()
    if name_lower in EXCLUSION_NAMES:
        return True

    if relative_path:
        norm = os.path.normpath(relative_path).lower()
        parts = norm.replace("\\", "/").split("/")
        for part in parts:
            if part in EXCLUSION_NAMES:
                return True

    return False


@dataclass
class IndexedFolder:
    path: str
    name: str
    normalized_name: str
    depth: int
    trigrams: Set[str] = field(default_factory=set)


class FolderIndexer:
    """
    Indexes configured root directories up to depth <= 3.
    Thread-safe and runnable in a daemon background thread.
    """

    def __init__(self, roots: Optional[List[str]] = None, max_depth: int = 3, max_folders: int = 10000):
        self._roots: List[str] = list(roots) if roots else []
        self._max_depth = max_depth
        self._max_folders = max_folders
        self._indexed: Dict[str, IndexedFolder] = {}  # normalized abspath -> IndexedFolder
        self._lock = threading.RLock()
        self._scan_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_scanning = False

    @property
    def max_depth(self) -> int:
        return self._max_depth

    @property
    def roots(self) -> List[str]:
        with self._lock:
            return list(self._roots)

    def set_roots(self, roots: List[str]) -> None:
        with self._lock:
            self._roots = list(roots)

    def add_root(self, root: str) -> None:
        with self._lock:
            if root not in self._roots:
                self._roots.append(root)

    def is_scanning(self) -> bool:
        return self._is_scanning

    def clear(self) -> None:
        with self._lock:
            self._indexed.clear()

    def get_indexed_count(self) -> int:
        with self._lock:
            return len(self._indexed)

    def get_all_folders(self) -> List[str]:
        with self._lock:
            return [f.path for f in self._indexed.values()]

    def add_folder(self, path: str, depth: int = 0) -> bool:
        """
        Explicitly registers a folder into the index if not excluded and depth <= max_depth.
        """
        if depth > self._max_depth:
            return False

        abs_path = os.path.abspath(path)
        base_name = os.path.basename(abs_path) or abs_path
        if is_excluded_directory(base_name):
            return False

        norm_name = normalize_text(base_name)
        tri = generate_trigrams(base_name)

        folder = IndexedFolder(
            path=abs_path,
            name=base_name,
            normalized_name=norm_name,
            depth=depth,
            trigrams=tri,
        )
        with self._lock:
            if abs_path not in self._indexed and len(self._indexed) >= self._max_folders:
                return False
            self._indexed[abs_path] = folder
        return True

    def scan_root(self, root_path: str) -> int:
        """
        Scans a single root directory up to max_depth (<= 3).
        Excludes directories matching EXCLUSION_NAMES.
        Returns count of newly indexed folders.
        """
        abs_root = os.path.abspath(root_path)
        if not os.path.isdir(abs_root):
            return 0

        root_name = os.path.basename(abs_root) or abs_root
        if is_excluded_directory(root_name):
            return 0

        count = 0
        # Breadth-first traversal with explicit depth tracking
        # Tuple: (current_dir, current_depth)
        queue = [(abs_root, 0)]

        while queue and not self._stop_event.is_set():
            curr_dir, depth = queue.pop(0)

            # Register folder
            if self.add_folder(curr_dir, depth):
                count += 1

            # If current directory is at max_depth, do NOT scan its children (they would be depth + 1 > max_depth)
            if depth >= self._max_depth:
                continue

            try:
                with os.scandir(curr_dir) as entries:
                    for entry in entries:
                        if self._stop_event.is_set():
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                rel = os.path.relpath(entry.path, abs_root)
                                if not is_excluded_directory(entry.name, rel):
                                    queue.append((entry.path, depth + 1))
                        except (PermissionError, OSError):
                            continue
            except (PermissionError, OSError):
                continue

        return count

    def scan_all(self, roots: Optional[List[str]] = None) -> int:
        """
        Synchronously indexes all configured roots.
        """
        target_roots = roots if roots is not None else self.roots
        total_indexed = 0
        self._stop_event.clear()
        self._is_scanning = True
        try:
            for r in target_roots:
                if self._stop_event.is_set():
                    break
                if os.path.exists(r):
                    total_indexed += self.scan_root(r)
        finally:
            self._is_scanning = False
        return total_indexed

    def start_background_scan(self, roots: Optional[List[str]] = None) -> None:
        """
        Launches background scanning thread.
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return

        self._stop_event.clear()
        self._scan_thread = threading.Thread(
            target=self.scan_all,
            args=(roots,),
            name="JARVIS-FolderIndexer",
            daemon=True,
        )
        self._scan_thread.start()

    def stop_scan(self) -> None:
        """Signals any running scan thread to stop."""
        self._stop_event.set()
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=2.0)

    def wait_until_complete(self, timeout: Optional[float] = 10.0) -> bool:
        """Waits for background scanning thread to finish."""
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=timeout)
            return not self._scan_thread.is_alive()
        return True

    def find_matches(self, query: str, limit: int = 5, min_score: float = 0.3) -> List[Tuple[str, float]]:
        """
        Ranks indexed folders by 3-gram fuzzy match against query.
        Returns list of (folder_path, score) sorted descending by score.
        """
        clean_q = normalize_text(query)
        if not clean_q:
            return []

        candidates: List[Tuple[str, float, int]] = []  # (path, score, depth)
        with self._lock:
            folders = list(self._indexed.values())

        for f in folders:
            # Score against folder base name
            score_name = compute_trigram_similarity(clean_q, f.normalized_name)
            # Also test against full relative path components if multi-word query
            score_full = compute_trigram_similarity(clean_q, f.path) * 0.90
            best_score = max(score_name, score_full)

            # Slight depth bonus for shallower folders when scores are close
            depth_bonus = max(0.0, (self._max_depth - f.depth) * 0.01)
            final_score = min(1.0, best_score + depth_bonus)

            if final_score >= min_score:
                candidates.append((f.path, final_score, f.depth))

        # Sort descending by score, then ascending by depth
        candidates.sort(key=lambda x: (x[1], -x[2]), reverse=True)
        return [(path, score) for path, score, _ in candidates[:limit]]

    def find_best_match(self, query: str, min_score: float = 0.3) -> Optional[str]:
        """
        Returns the top matching folder path for a given query, or None if no candidate passes threshold.
        """
        matches = self.find_matches(query, limit=1, min_score=min_score)
        if matches:
            return matches[0][0]
        return None


# Global singleton instance
_GLOBAL_INDEXER: Optional[FolderIndexer] = None
_GLOBAL_LOCK = threading.Lock()


def get_folder_indexer() -> FolderIndexer:
    """Returns the process-wide FolderIndexer instance."""
    global _GLOBAL_INDEXER
    with _GLOBAL_LOCK:
        if _GLOBAL_INDEXER is None:
            from config import load_config
            cfg = load_config()
            roots = getattr(cfg, "indexed_roots", [])
            _GLOBAL_INDEXER = FolderIndexer(roots=roots, max_depth=3)
        return _GLOBAL_INDEXER


def set_folder_indexer(indexer: Optional[FolderIndexer]) -> None:
    """Sets or overrides the process-wide FolderIndexer instance (useful for unit testing)."""
    global _GLOBAL_INDEXER
    with _GLOBAL_LOCK:
        _GLOBAL_INDEXER = indexer
