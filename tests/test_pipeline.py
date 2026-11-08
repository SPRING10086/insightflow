from __future__ import annotations

import contextlib
import io
import logging
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
        (self.tmp / "raw").mkdir()
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "config.json").write_text(
            '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
            '"comparison":{"enabled":true,"lookback_days":1},"max_charts":8}',
            encoding="utf-8",
        )
        self.settings = load_settings(project_root=self.tmp)
        self.now = datetime(2026, 11, 8, 9, 30, tzinfo=__import__("datetime").timezone.utc)

    def tearDown(self):
        # Windows keeps log files locked until handlers are closed.  Without
        # this, the test suite leaves temp directories behind.
        logging.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_csv(self, name: str, rows: list[str]) -> Path:
        path = self.tmp / "raw" / name
        path.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
        return path

    def _success_source(self) -> None:
        rows = ["订单日期,区域,品类,渠道,销售额,订单量,客户数,成本"]
        for index in range(14):
            day = "2026-11-07" if index < 8 else "2026-11-06"
            revenue = 1000 + index * 100
            rows.append(f"{day},华东,手机数码,线上,{revenue},2,1,{revenue * 0.6}")
        self._write_csv("sales.csv", rows)

    def test_success_generates_single_file_report_and_idempotency(self):
        self._success_source()
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(self.settings, target_date=date(2026, 11, 7), force=True, now=self.now)
        self.assertEqual(record.status, "success")
        self.assertTrue(record.report_generated)
        report = self.tmp / record.report_path
        self.assertTrue(report.is_file())
        html = report.read_text(encoding="utf-8")
        self.assertGreater(len(html), 1_000_000)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("<link rel=\"stylesheet\"", html)
        self.assertIn("单文件", html)
        self.assertTrue((self.tmp / "out" / "2026-11-07" / "analysis_plan.json").is_file())
        self.assertTrue((self.tmp / "out" / "2026-11-07" / "metrics.json").is_file())
        self.assertTrue((self.tmp / "out" / "2026-11-07" / "cleaned_data.csv").is_file())

        with contextlib.redirect_stderr(io.StringIO()):
            second = execute(self.settings, target_date=date(2026, 11, 7), force=False, now=self.now)
        self.assertEqual(second.status, "skipped")
        self.assertIn("指纹", second.failure_reason or "")

    def test_missing_target_is_no_data(self):
        self._write_csv(
            "old.csv",
            ["日期,区域,销售额", "2026-11-05,华东,100", "2026-11-05,华南,80"],
        )
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(self.settings, target_date=date(2026, 11, 7), force=True, now=self.now)
        self.assertEqual(record.status, "no_data")
        self.assertFalse(record.report_generated)
        self.assertIn("2026-11-07", record.failure_reason)

    def test_empty_source_is_no_data(self):
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(self.settings, target_date=date(2026, 11, 7), force=True, now=self.now)
        self.assertEqual(record.status, "no_data")

    def test_only_corrupt_files_fails_cleanly(self):
        (self.tmp / "raw" / "broken.xlsx").write_text("not a workbook", encoding="utf-8")
        (self.tmp / "raw" / "empty.csv").write_bytes(b"")
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(self.settings, target_date=date(2026, 11, 7), force=True, now=self.now)
        self.assertEqual(record.status, "failed")
        self.assertTrue(record.failure_reason)
        self.assertIn("没有成功读取", record.failure_reason)

    def test_existing_lock_skips(self):
        lock = self.tmp / "state" / ".pipeline.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text('{"pid":1}', encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(self.settings, target_date=date(2026, 11, 7), force=True, now=self.now)
        self.assertEqual(record.status, "skipped")
        self.assertIn("并发", record.failure_reason)

    def test_html_escapes_source_values(self):
        self._write_csv(
            "escape.csv",
            [
                "日期,区域,品类,销售额,订单量",
                '2026-11-07,<script>alert(1)</script>,手机,100,1',
                "2026-11-07,华南,手机,80,1",
            ],
        )
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(self.settings, target_date=date(2026, 11, 7), force=True, now=self.now)
        self.assertEqual(record.status, "success")
        html = (self.tmp / record.report_path).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


if __name__ == "__main__":
    unittest.main()
