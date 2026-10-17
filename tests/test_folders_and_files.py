"""
Unit tests for J.A.R.V.I.S. Phase 4: Files & Folders Management.

Covers:
1. FolderIndexer depth <= 3 constraint.
2. FolderIndexer directory exclusion list (Windows, Program Files, AppData, .git, etc.).
3. Character 3-gram fuzzy matcher ("programming", "react project", "ai notes", typos).
4. Config folders_whitelist and indexed_roots defaults & persistence.
5. Security allowlist: allowed extensions open, executables (.exe, .bat, etc.) rejected.
6. open_folder: alias -> indexer fuzzy match, os.startfile ONLY on directories.
7. open_recent_file: newest file lookup, extension filter, security enforcement.
8. show_last_download: finds latest download and invokes explorer /select,<path>.
9. remember_folder: confirmation required, valid directory validation, config persistence.
10. Context resolver "open it" referential resolution for folders and files.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from config import Config, load_config, remember_folder_alias, save_config
from context_resolver import ContextResolver, get_context_resolver
from file_ops import (
    ALLOWED_EXTENSIONS,
    DISALLOWED_EXECUTABLE_EXTENSIONS,
    FileOperationsAdapter,
    is_allowed_file,
    open_file,
    open_folder,
    open_recent_file,
    remember_folder,
    show_last_download,
)
from folder_indexer import (
    EXCLUSION_NAMES,
    FolderIndexer,
    compute_trigram_similarity,
    generate_trigrams,
    is_excluded_directory,
)


class TestFolderIndexer(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="jarvis_test_indexer_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_depth_constraint_less_than_equal_to_three(self):
        """Strict constraint: scanner indexes up to depth <= 3 only."""
        # Create hierarchy: root (0) -> d1 (1) -> d2 (2) -> d3 (3) -> d4 (4) -> d5 (5)
        d1 = os.path.join(self.test_dir, "depth1")
        d2 = os.path.join(d1, "depth2")
        d3 = os.path.join(d2, "depth3")
        d4 = os.path.join(d3, "depth4")
        d5 = os.path.join(d4, "depth5")
        os.makedirs(d5, exist_ok=True)

        indexer = FolderIndexer(roots=[self.test_dir], max_depth=3)
        indexer.scan_all()
        indexed = indexer.get_all_folders()

        # Depth 0 (root), 1 (d1), 2 (d2), 3 (d3) must be present
        self.assertIn(os.path.abspath(self.test_dir), indexed)
        self.assertIn(os.path.abspath(d1), indexed)
        self.assertIn(os.path.abspath(d2), indexed)
        self.assertIn(os.path.abspath(d3), indexed)

        # Depth 4 (d4) and 5 (d5) MUST NOT be present
        self.assertNotIn(os.path.abspath(d4), indexed)
        self.assertNotIn(os.path.abspath(d5), indexed)

    def test_exclusion_list(self):
        """Folders in exclusion list and their children are completely skipped."""
        excluded_dirs = [
            "Windows",
            "Program Files",
            "Program Files (x86)",
            "AppData",
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "$RECYCLE.BIN",
            "System Volume Information",
        ]

        for d_name in excluded_dirs:
            parent = os.path.join(self.test_dir, d_name)
            child = os.path.join(parent, "subfolder")
            os.makedirs(child, exist_ok=True)

        # Also create valid folders
        valid1 = os.path.join(self.test_dir, "ValidProject")
        valid2 = os.path.join(self.test_dir, "Documents")
        os.makedirs(valid1, exist_ok=True)
        os.makedirs(valid2, exist_ok=True)

        indexer = FolderIndexer(roots=[self.test_dir], max_depth=3)
        indexer.scan_all()
        indexed = indexer.get_all_folders()

        for d_name in excluded_dirs:
            parent = os.path.abspath(os.path.join(self.test_dir, d_name))
            child = os.path.abspath(os.path.join(parent, "subfolder"))
            self.assertNotIn(parent, indexed, f"{d_name} should be excluded")
            self.assertNotIn(child, indexed, f"Subfolder inside {d_name} should be excluded")

        self.assertIn(os.path.abspath(valid1), indexed)
        self.assertIn(os.path.abspath(valid2), indexed)

    def test_fuzzy_matcher_3grams(self):
        """Character 3-gram fuzzy matcher accurately resolves user queries."""
        f_prog = os.path.join(self.test_dir, "Programming")
        f_react = os.path.join(self.test_dir, "react-project")
        f_notes = os.path.join(self.test_dir, "ai_notes")
        f_ml = os.path.join(self.test_dir, "machine-learning-notes")
        for p in (f_prog, f_react, f_notes, f_ml):
            os.makedirs(p, exist_ok=True)

        indexer = FolderIndexer(roots=[self.test_dir], max_depth=3)
        indexer.scan_all()

        # 1. Exact / normalized matches
        self.assertEqual(indexer.find_best_match("programming"), os.path.abspath(f_prog))
        self.assertEqual(indexer.find_best_match("react project"), os.path.abspath(f_react))
        self.assertEqual(indexer.find_best_match("ai notes"), os.path.abspath(f_notes))

        # 2. Typos & subwords handled by 3-grams
        self.assertEqual(indexer.find_best_match("programing"), os.path.abspath(f_prog))
        self.assertEqual(indexer.find_best_match("reakt project"), os.path.abspath(f_react))

        # 3. Unrelated query returns None or doesn't match
        self.assertIsNone(indexer.find_best_match("completely_unrelated_xyz", min_score=0.4))

    def test_background_scan_threading(self):
        """FolderIndexer runs asynchronously in background thread."""
        indexer = FolderIndexer(roots=[self.test_dir], max_depth=3)
        indexer.start_background_scan()
        finished = indexer.wait_until_complete(timeout=5.0)
        self.assertTrue(finished)
        self.assertFalse(indexer.is_scanning())


class TestConfigAndWhitelist(unittest.TestCase):
    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_file.close()

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.remove(self.temp_file.name)

    def test_config_defaults(self):
        """Config has folders_whitelist and indexed_roots."""
        cfg = Config()
        self.assertTrue(hasattr(cfg, "folders_whitelist"))
        self.assertTrue(hasattr(cfg, "indexed_roots"))
        self.assertIn("downloads", cfg.folders_whitelist)
        self.assertIn("documents", cfg.folders_whitelist)
        self.assertIn("programming", cfg.folders_whitelist)
        self.assertIsInstance(cfg.indexed_roots, list)

    def test_remember_folder_alias_persistence(self):
        """remember_folder_alias persists to config file."""
        cfg = Config()
        test_path = os.path.abspath(tempfile.gettempdir())
        ok = remember_folder_alias("workspace", test_path, cfg=cfg, config_path=self.temp_file.name)
        self.assertTrue(ok)
        self.assertEqual(cfg.folders_whitelist["workspace"], test_path)

        with open(self.temp_file.name, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("folders_whitelist", data)
        self.assertEqual(data["folders_whitelist"]["workspace"], test_path)


class TestSecurityAllowlistAndFileOps(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="jarvis_test_fileops_")
        self.mock_opener = MagicMock()
        self.mock_launcher = MagicMock()
        self.mock_indexer = MagicMock()

        self.adapter = FileOperationsAdapter(
            indexer=self.mock_indexer,
            opener=self.mock_opener,
            explorer_launcher=self.mock_launcher,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_security_extension_allowlist(self):
        """Permitted extensions return True; executables explicitly return False."""
        for ext in ALLOWED_EXTENSIONS:
            sample = f"testfile{ext}"
            self.assertTrue(is_allowed_file(sample), f"{ext} should be allowed")

        for bad_ext in DISALLOWED_EXECUTABLE_EXTENSIONS:
            bad_sample = f"dangerous{bad_ext}"
            self.assertFalse(is_allowed_file(bad_sample), f"{bad_ext} must be rejected")

    def test_open_file_security_rejection(self):
        """open_file blocks execution of executable files."""
        exe_file = os.path.join(self.test_dir, "virus.exe")
        with open(exe_file, "w") as f:
            f.write("mock binary")

        res = self.adapter.open_file(exe_file)
        self.assertFalse(res.success)
        self.assertIn("Security violation", res.message)
        self.mock_opener.assert_not_called()

    def test_open_file_allowed(self):
        """open_file succeeds on valid allowlisted files."""
        pdf_file = os.path.join(self.test_dir, "document.pdf")
        with open(pdf_file, "w") as f:
            f.write("mock pdf")

        res = self.adapter.open_file(pdf_file)
        self.assertTrue(res.success)
        self.mock_opener.assert_called_once_with(os.path.abspath(pdf_file))

    def test_open_folder_strictly_on_directories(self):
        """open_folder calls opener ONLY on authentic directories."""
        folder_path = os.path.join(self.test_dir, "my_folder")
        os.makedirs(folder_path, exist_ok=True)

        self.mock_indexer.find_best_match.return_value = folder_path

        res = self.adapter.open_folder("my_folder")
        self.assertTrue(res.success)
        self.mock_opener.assert_called_with(folder_path)

        # Must reject if path is a file
        file_path = os.path.join(self.test_dir, "not_a_folder.txt")
        with open(file_path, "w") as f:
            f.write("content")
        self.mock_indexer.find_best_match.return_value = file_path

        res2 = self.adapter.open_folder("not_a_folder.txt")
        self.assertFalse(res2.success)
        self.assertIn("not a directory", res2.message)

    def test_open_recent_file_newest_lookup_and_extension_filter(self):
        """open_recent_file finds newest file and respects extension filters."""
        # Create older txt
        old_txt = os.path.join(self.test_dir, "old.txt")
        with open(old_txt, "w") as f:
            f.write("old")
        os.utime(old_txt, (1000, 1000))

        # Create newer pdf
        newer_pdf = os.path.join(self.test_dir, "newer.pdf")
        with open(newer_pdf, "w") as f:
            f.write("newer")
        os.utime(newer_pdf, (2000, 2000))

        # 1. Open newest overall -> should pick newer.pdf
        res = self.adapter.open_recent_file(folder=self.test_dir)
        self.assertTrue(res.success)
        self.assertEqual(res.path, newer_pdf)
        self.mock_opener.assert_called_with(newer_pdf)

        # 2. Filter by extension .txt -> should pick old.txt
        res_txt = self.adapter.open_recent_file(folder=self.test_dir, extension=".txt")
        self.assertTrue(res_txt.success)
        self.assertEqual(res_txt.path, old_txt)

    def test_open_recent_file_security_blocks_executable(self):
        """open_recent_file blocks opening if newest file is executable."""
        exe_file = os.path.join(self.test_dir, "trojan.bat")
        with open(exe_file, "w") as f:
            f.write("bad")
        os.utime(exe_file, (5000, 5000))

        res = self.adapter.open_recent_file(folder=self.test_dir)
        self.assertFalse(res.success)
        self.assertIn("Security alert", res.message)
        self.mock_opener.assert_not_called()

    def test_show_last_download(self):
        """show_last_download finds latest file and calls explorer /select."""
        dl_dir = os.path.join(self.test_dir, "Downloads")
        os.makedirs(dl_dir, exist_ok=True)
        file1 = os.path.join(dl_dir, "file1.txt")
        file2 = os.path.join(dl_dir, "file2.pdf")
        with open(file1, "w") as f:
            f.write("1")
        os.utime(file1, (1000, 1000))
        with open(file2, "w") as f:
            f.write("2")
        os.utime(file2, (2000, 2000))

        # Mock alias map to return our temp dl_dir
        with patch.object(self.adapter, "_get_alias_map", return_value={"downloads": dl_dir}):
            res = self.adapter.show_last_download()
            self.assertTrue(res.success)
            self.assertEqual(res.path, file2)
            self.mock_launcher.assert_called_once_with(file2)

    def test_remember_folder_confirmation_and_validation(self):
        """remember_folder enforces confirmation and directory validation."""
        valid_dir = os.path.join(self.test_dir, "MyWork")
        os.makedirs(valid_dir, exist_ok=True)

        # 1. Unconfirmed request must fail
        res_unconfirmed = self.adapter.remember_folder("work", valid_dir, confirmed=False)
        self.assertFalse(res_unconfirmed.success)
        self.assertIn("Confirmation required", res_unconfirmed.message)

        # 2. Non-existent path must fail
        res_nonexistent = self.adapter.remember_folder("work", "C:\\nonexistent_dir_12345", confirmed=True)
        self.assertFalse(res_nonexistent.success)
        self.assertIn("not a valid directory", res_nonexistent.message)

        # 3. Confirmed valid request succeeds
        res_ok = self.adapter.remember_folder("work", valid_dir, confirmed=True)
        self.assertTrue(res_ok.success)
        self.assertEqual(res_ok.path, os.path.abspath(valid_dir))


class TestVoiceCommandsAndContextIntegration(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.overlay = MagicMock()
        self.resolver = ContextResolver()

    def test_voice_command_open_folder(self):
        """commands.py handles 'open downloads folder'."""
        from commands import run_command
        with patch("file_ops.open_folder") as mock_open:
            from file_ops import FileOpResult
            mock_open.return_value = FileOpResult(True, "Opening downloads folder, sir.", "C:\\Users\\test\\Downloads")
            reply = run_command("open downloads folder", self.ctx)
            self.assertIn("Opening downloads folder", reply)

    def test_voice_command_show_last_download(self):
        """commands.py handles 'show last download'."""
        from commands import run_command
        with patch("file_ops.show_last_download") as mock_show:
            from file_ops import FileOpResult
            mock_show.return_value = FileOpResult(True, "Revealed latest download 'setup.zip' in Explorer, sir.", "C:\\Users\\test\\Downloads\\setup.zip")
            reply = run_command("show last download", self.ctx)
            self.assertIn("Revealed latest download", reply)

    def test_context_resolver_open_it_after_file_action(self):
        """Saying 'open it' after showing last download resolves to opening that file."""
        from commands import run_command
        with patch("file_ops.show_last_download") as mock_show:
            from file_ops import FileOpResult
            test_file = "C:\\Users\\test\\Downloads\\report.pdf"
            mock_show.return_value = FileOpResult(True, "Revealed latest download in Explorer", test_file)
            run_command("show last download", self.ctx)

        # Next turn: "open it"
        with patch("file_ops.open_file") as mock_open_file:
            from file_ops import FileOpResult
            mock_open_file.return_value = FileOpResult(True, "Opened file report.pdf", test_file)
            reply = run_command("open it", self.ctx)
            self.assertIsNotNone(reply)
            mock_open_file.assert_called_with(test_file)


if __name__ == "__main__":
    unittest.main()
