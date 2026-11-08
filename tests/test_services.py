from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow import cli, services  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.discovery import SourceNotFoundError  # noqa: E402

DATA = ROOT / "tests" / "data"
TARGET = "2026-11-07"


def _normalise(text: str) -> str:
    """Replace the machine-specific project root so fixtures are portable."""
    raw = str(ROOT)
    return text.replace(raw.replace("\\", "\\\\"), "<ROOT>").replace(raw, "<ROOT>")


def _run_cli(args: list[str]) -> tuple[str, str, int]:
    """Run the CLI in-process and capture (stdout, stderr, exit code)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(args)
    return out.getvalue(), err.getvalue(), code


class ScanOutputRegressionTests(unittest.TestCase):
    """The refactor must not change a single byte of the CLI's scan output."""

    def setUp(self) -> None:
        # Pin the project root regardless of the directory tests run from.
        self._env = patch.dict(os.environ, {"INSIGHTFLOW_HOME": str(ROOT)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_scan_text_output_matches_golden(self):
        actual, _stderr, code = _run_cli(["scan", "--date", TARGET])
        expected = (DATA / "scan_text_golden.txt").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(_normalise(actual), expected)

    def test_scan_json_output_matches_golden(self):
        actual, _stderr, code = _run_cli(["scan", "--date", TARGET, "--json"])
        expected = (DATA / "scan_json_golden.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(_normalise(actual), expected)
        # Still valid JSON with the historical key set.
        payload = json.loads(actual)
        self.assertEqual(
            list(payload),
            ["target_date", "baseline_date", "source_dir", "files_scanned", "files_read", "tables", "file_errors"],
        )


class PreviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings(project_root=ROOT)

    def test_preview_returns_structured_payload(self):
        payload = services.preview(self.settings, date(2026, 11, 7))
        self.assertIsInstance(payload, dict)
        self.assertEqual(
            list(payload),
            ["target_date", "baseline_date", "source_dir", "files_scanned", "files_read", "tables", "file_errors"],
        )
        self.assertEqual(payload["target_date"], TARGET)
        self.assertEqual(payload["baseline_date"], "2026-11-06")
        self.assertEqual(payload["files_scanned"], 7)
        self.assertEqual(payload["files_read"], 5)
        self.assertTrue(payload.has_target_rows)
        self.assertFalse(payload.empty_source)
        # Warnings are available to callers but stay out of the JSON contract.
        self.assertNotIn("warnings", payload)
        self.assertEqual({warning["code"] for warning in payload.warnings},
                         {"excel_open_error", "empty_file", "header_repaired"})

    def test_preview_empty_source_reports_zero_and_cli_exits_2(self):
        tmp = Path(tempfile.mkdtemp(prefix="insightflow-preview-", dir=str(ROOT / "tests")))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "raw").mkdir()
        (tmp / "config").mkdir()
        (tmp / "config" / "config.json").write_text(
            '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state"}',
            encoding="utf-8",
        )
        settings = load_settings(project_root=tmp)
        payload = services.preview(settings, date(2026, 11, 7))
        self.assertTrue(payload.empty_source)
        self.assertEqual(payload["files_scanned"], 0)
        self.assertEqual(payload["tables"], [])
        self.assertEqual(payload.warnings, [])

        with patch.dict(os.environ, {"INSIGHTFLOW_HOME": str(tmp)}):
            _out, err, code = _run_cli(["scan", "--date", TARGET])
        self.assertEqual(code, 2)
        self.assertIn("没有 Excel/CSV 文件", err)

    def test_preview_missing_source_raises(self):
        with self.assertRaises(SourceNotFoundError):
            services.preview(self.settings, date(2026, 11, 7), ROOT / "does-not-exist")


if __name__ == "__main__":
    unittest.main()
