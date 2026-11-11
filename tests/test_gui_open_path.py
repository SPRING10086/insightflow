from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.gui import services  # noqa: E402


class OpenPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = self.root / "out"
        self.folder.mkdir()
        self.file = self.folder / "report.html"
        self.file.write_text("<html></html>", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_native_startfile_success_skips_fallbacks(self):
        with patch.object(services.os, "startfile") as start, \
                patch.object(services.webbrowser, "open") as browser, \
                patch.object(services.subprocess, "Popen") as popen:
            services.open_path(self.folder)
        start.assert_called_once_with(str(self.folder))
        browser.assert_not_called()
        popen.assert_not_called()

    def test_missing_path_raises_filenotfound(self):
        missing = self.root / "nope"
        with self.assertRaises(FileNotFoundError):
            services.open_path(missing)

    def test_folder_falls_back_to_explorer(self):
        with patch.object(services.os, "startfile", side_effect=PermissionError(5)), \
                patch.object(services.webbrowser, "open") as browser, \
                patch.object(services.subprocess, "Popen") as popen:
            services.open_path(self.folder)
        browser.assert_not_called()  # folders never go through the browser
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertIsInstance(argv, list)  # argument list, never a shell string
        self.assertEqual(argv[0], "explorer.exe")
        self.assertEqual(argv[1], str(self.folder))

    def test_file_falls_back_to_browser_first(self):
        with patch.object(services.os, "startfile", side_effect=OSError(5)), \
                patch.object(services.webbrowser, "open", return_value=True) as browser, \
                patch.object(services.subprocess, "Popen") as popen:
            services.open_path(self.file)
        browser.assert_called_once()
        self.assertTrue(browser.call_args[0][0].startswith("file:"))
        popen.assert_not_called()

    def test_file_browser_failure_then_explorer(self):
        with patch.object(services.os, "startfile", side_effect=OSError(5)), \
                patch.object(services.webbrowser, "open", return_value=False), \
                patch.object(services.subprocess, "Popen") as popen:
            services.open_path(self.file)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "explorer.exe")
        self.assertEqual(argv[1], str(self.file))

    def test_all_methods_fail_raises_oserror(self):
        with patch.object(services.os, "startfile", side_effect=OSError(5)), \
                patch.object(services.webbrowser, "open", return_value=False), \
                patch.object(services.subprocess, "Popen", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                services.open_path(self.file)

    def test_attribute_error_on_non_windows_also_falls_back(self):
        # os.startfile is absent on non-Windows; the fallback must still work.
        with patch.object(services.os, "startfile", create=True, new=None), \
                patch.object(services.webbrowser, "open", return_value=True), \
                patch.object(services.subprocess, "Popen") as popen:
            # Simulate the AttributeError branch explicitly.
            with patch.object(services.os, "startfile", side_effect=AttributeError):
                services.open_path(self.file)
        popen.assert_not_called()

    # -- Low-integrity sandboxed workspace branch --------------------------

    def test_low_integrity_file_uses_dedicated_edge(self):
        with patch.object(services, "_current_integrity_rid", return_value=0x1000), \
                patch.object(services, "_edge_executable", return_value=r"C:\edge\msedge.exe"), \
                patch.object(services, "_runtime_dir", return_value=self.root / "rt"), \
                patch.object(services.os, "startfile") as start, \
                patch.object(services.subprocess, "Popen") as popen:
            services.open_path(self.file)
        start.assert_not_called()  # native call is skipped entirely in the sandbox
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], r"C:\edge\msedge.exe")
        self.assertIn("--no-sandbox", argv)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in argv))
        self.assertTrue(argv[-1].startswith("file:"))
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["TEMP"], str(self.root / "rt" / "temp"))
        self.assertEqual(env["TMP"], str(self.root / "rt" / "temp"))

    def test_low_integrity_folder_uses_edge_directory_listing(self):
        with patch.object(services, "_current_integrity_rid", return_value=0x1000), \
                patch.object(services, "_edge_executable", return_value=r"C:\edge\msedge.exe"), \
                patch.object(services, "_runtime_dir", return_value=self.root / "rt"), \
                patch.object(services.subprocess, "Popen") as popen:
            services.open_path(self.folder)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], r"C:\edge\msedge.exe")
        self.assertIn("--no-sandbox", argv)
        self.assertTrue(argv[-1].startswith("file:"))  # folder URI -> listing

    def test_low_integrity_without_edge_raises_oserror(self):
        with patch.object(services, "_current_integrity_rid", return_value=0x1000), \
                patch.object(services, "_edge_executable", return_value=None):
            with self.assertRaises(OSError):
                services.open_path(self.file)

    def test_non_low_integrity_uses_native_path(self):
        with patch.object(services, "_current_integrity_rid", return_value=0x2000), \
                patch.object(services.os, "startfile") as start, \
                patch.object(services.subprocess, "Popen") as popen:
            services.open_path(self.folder)
        start.assert_called_once_with(str(self.folder))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
