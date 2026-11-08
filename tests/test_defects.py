"""Regression tests for the P0/P1 audit findings.

These tests are intentionally written before the fixes.  Each one targets a
silent-wrong-number failure mode, not an obvious crash.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.models import SourceFile  # noqa: E402
from insightflow.normalize import extract_periods  # noqa: E402
from insightflow.readers import read_csv_file, read_excel_file, read_sources  # noqa: E402
from insightflow import analysis as analysis_module  # noqa: E402
from insightflow import pipeline as pipeline_module  # noqa: E402
from insightflow import planner  # noqa: E402
from insightflow import scheduler  # noqa: E402
from insightflow.schema import detect_schema  # noqa: E402
from insightflow.utils import parse_date_value, read_json  # noqa: E402


class DefectRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="insightflow-defect-", dir=str(ROOT / "tests")))
        (self.tmp / "raw").mkdir()
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "config.json").write_text(
            json.dumps(
                {
                    "source_dir": "raw",
                    "output_dir": "out",
                    "log_dir": "logs",
                    "state_dir": "state",
                    "max_rows_per_file": 5,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _settings(self, max_rows: int = 5):
        settings = load_settings(project_root=self.tmp)
        settings.max_rows_per_file = max_rows
        settings.ensure_runtime_dirs()
        return settings

    def _source(self, path: Path) -> SourceFile:
        stat = path.stat()
        return SourceFile(
            path=path,
            relative_path=path.name,
            suffix=path.suffix.lower(),
            size_bytes=stat.st_size,
            modified_at=datetime.now().astimezone(),
        )

    # P0-1 -----------------------------------------------------------------
    def test_excel_does_not_lose_one_row_at_the_business_row_limit(self):
        """Exactly N business rows must survive, with header row excluded."""
        path = self.tmp / "raw" / "exactly_five.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Data"
        sheet.append(["日期", "区域", "销售额"])
        for index in range(5):
            sheet.append([f"2026-11-0{index + 1}", "华东", 100 + index])
        workbook.save(path)

        settings = self._settings(max_rows=5)
        tables, _, _ = read_excel_file(self._source(path), settings)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0].frame), 5)
        self.assertFalse(tables[0].truncated)

        csv_path = self.tmp / "raw" / "exactly_five.csv"
        csv_path.write_text(
            "日期,区域,销售额\n"
            + "".join(f"2026-11-0{index + 1},华东,{100 + index}\n" for index in range(5)),
            encoding="utf-8-sig",
        )
        csv_tables, _, _ = read_csv_file(self._source(csv_path), settings)
        self.assertEqual(len(csv_tables[0].frame), 5)
        self.assertFalse(csv_tables[0].truncated)

    # P0-2 -----------------------------------------------------------------
    def test_source_row_uses_physical_csv_line_not_filtered_position(self):
        path = self.tmp / "raw" / "lineage.csv"
        path.write_text(
            "销售日报\n"
            "日期,区域,销售额\n"
            "2026-11-05,华东,100\n"
            "2026-11-06,华东,110\n"
            "2026-11-07,华东,120\n"
            "2026-11-07,华南,130\n",
            encoding="utf-8-sig",
        )
        settings = self._settings(max_rows=200)
        tables, _, _ = read_csv_file(self._source(path), settings)
        schema = detect_schema(tables[0].frame)
        extraction = extract_periods(tables[0], schema, date(2026, 11, 7), date(2026, 11, 6))
        self.assertEqual(extraction.target["__source_row"].tolist(), [5, 6])
        self.assertEqual(extraction.baseline["__source_row"].tolist(), [4])

    def test_excel_source_row_uses_physical_sheet_row(self):
        path = self.tmp / "raw" / "lineage.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Data"
        sheet.append(["销售日报"])
        sheet.append(["日期", "区域", "销售额"])
        sheet.append(["2026-11-05", "华东", 100])
        sheet.append(["2026-11-06", "华东", 110])
        sheet.append(["2026-11-07", "华东", 120])
        sheet.append(["2026-11-07", "华南", 130])
        workbook.save(path)

        settings = self._settings(max_rows=200)
        tables, _, _ = read_excel_file(self._source(path), settings)
        schema = detect_schema(tables[0].frame)
        extraction = extract_periods(tables[0], schema, date(2026, 11, 7), date(2026, 11, 6))
        self.assertEqual(extraction.target["__source_row"].tolist(), [5, 6])
        self.assertEqual(extraction.baseline["__source_row"].tolist(), [4])

    # P1-3 -----------------------------------------------------------------
    def test_rate_and_negative_headers_are_not_summed_as_amount_metrics(self):
        frame = pd.DataFrame(
            {
                "日期": ["2026-11-07", "2026-11-07"],
                "销售额": [100.0, 200.0],
                "毛利率": [30.0, 35.0],
                "利润率": [10.0, 12.0],
                "非销售额": [1.0, 2.0],
            }
        )
        schema = detect_schema(frame)
        mapping = {field.original: field for field in schema.fields.values()}
        self.assertEqual(mapping["销售额"].canonical, "revenue")
        for header in ("毛利率", "利润率", "非销售额"):
            with self.subTest(header=header):
                field = mapping[header]
                self.assertNotIn(field.canonical, {"revenue", "profit", "cost", "orders"})
                self.assertNotEqual(field.canonical, "revenue")

    def test_date_order_warning_survives_no_target_rows(self):
        path = self.tmp / "raw" / "ambiguous.csv"
        path.write_text("日期,销售额\n03/04/2026,100\n", encoding="utf-8-sig")
        settings = self._settings(max_rows=200)
        read_result = read_sources([self._source(path)], settings, self.tmp / "raw")
        prepared = pipeline_module._prepare(
            read_result=read_result,
            target=date(2026, 4, 3),  # default month-first would not match
            baseline=None,
            settings=settings,
        )
        self.assertTrue(prepared["target_raw"].empty)
        self.assertTrue(any("date_order_ambiguous" in warning for warning in prepared["warnings"]))

    def test_rate_metric_uses_average_and_skips_share(self):
        settings = self._settings(max_rows=200)
        schema = detect_schema(
            pd.DataFrame(
                {
                    "日期": ["2026-11-07"] * 3,
                    "区域": ["华东", "华东", "华南"],
                    "毛利率": [30.0, 40.0, 50.0],
                }
            ),
            date_order=settings.date_order,
        )
        rate_field = next(field for field in schema.fields.values() if field.original == "毛利率")
        self.assertEqual(rate_field.aggregation, "AVG")

        target = pd.DataFrame(
            {
                "__date": [date(2026, 11, 7)] * 3,
                "__hour": [None] * 3,
                "region": ["华东", "华东", "华南"],
                rate_field.canonical: [30.0, 40.0, 50.0],
            }
        )
        fields = {rate_field.canonical: rate_field, "region": schema.fields["region"]}
        totals = analysis_module._totals(target, fields)
        self.assertAlmostEqual(totals["metrics"][rate_field.canonical], 40.0)
        _, records = analysis_module._dimension_table(target, "region", rate_field.canonical, fields)
        self.assertTrue(all(record["share"] is None for record in records))

        plan = planner.build_plan(
            target,
            pd.DataFrame(),
            fields,
            date(2026, 11, 7),
            None,
            settings,
        )
        definition = next(
            item for item in plan.metric_definitions if item.key == rate_field.canonical
        )
        self.assertEqual(definition.aggregation, "AVG")
        self.assertTrue(definition.formula.startswith("AVG("))
        share_step = next(step for step in plan.steps if step.step_id == "share")
        self.assertFalse(share_step.enabled)

    # P1-4 -----------------------------------------------------------------
    def test_ambiguous_dates_default_to_month_first(self):
        # 03/04 is March 4 under month-first (the default).
        self.assertEqual(parse_date_value("03/04/2026"), date(2026, 3, 4))
        self.assertEqual(parse_date_value("15/04/2026"), date(2026, 4, 15))
        self.assertEqual(parse_date_value("04/15/2026"), date(2026, 4, 15))
        self.assertEqual(parse_date_value("03/04/2026", date_order="day_first"), date(2026, 4, 3))

    def test_ambiguous_dates_emit_warning(self):
        schema = detect_schema(
            pd.DataFrame({"日期": ["03/04/2026", "2026-11-07"], "销售额": [1, 2]})
        )
        self.assertTrue(any(issue.code == "date_order_ambiguous" for issue in schema.issues))

    # P1-5 -----------------------------------------------------------------
    def test_sheets_skipped_counts_empty_sheets(self):
        path = self.tmp / "raw" / "two_sheets.xlsx"
        workbook = Workbook()
        data = workbook.active
        data.title = "Data"
        data.append(["日期", "区域", "销售额"])
        data.append(["2026-11-07", "华东", 100])
        workbook.create_sheet("Empty")
        workbook.save(path)

        settings = self._settings(max_rows=200)
        result = read_sources([self._source(path)], settings, self.tmp / "raw")
        report = result.file_reports[0]
        self.assertEqual(report.sheets_read, 1)
        self.assertEqual(report.sheets_skipped, 1)

    def test_all_sheets_empty_still_reports_two_skipped(self):
        path = self.tmp / "raw" / "all_empty.xlsx"
        workbook = Workbook()
        workbook.active.title = "Empty1"
        workbook.create_sheet("Empty2")
        workbook.save(path)

        settings = self._settings(max_rows=200)
        result = read_sources([self._source(path)], settings, self.tmp / "raw")
        report = result.file_reports[0]
        self.assertEqual(report.sheets_read, 0)
        self.assertEqual(report.sheets_skipped, 2)

    # P1-6 -----------------------------------------------------------------
    def test_trigger_is_due_within_grace_window_not_35_seconds(self):
        settings = self._settings(max_rows=200)
        # 90 minutes after the schedule, still inside the 180-minute grace.
        now = datetime(2026, 11, 8, 11, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertTrue(scheduler.trigger_is_due(settings, "09:30", now, state={}))

    def test_failed_trigger_is_not_marked_as_completed_and_can_retry(self):
        settings = self._settings(max_rows=200)
        now = datetime(2026, 11, 8, 9, 30, tzinfo=timezone(timedelta(hours=8)))
        failed = SimpleNamespace(status="failed", run_id="run-failed", report_path="")
        with patch("insightflow.scheduler.execute", return_value=failed):
            scheduler._trigger(settings, now)
        state = read_json(settings.state_dir / "scheduler_state.json", default={}) or {}
        self.assertNotIn("last_triggered_date", state)
        self.assertEqual(state.get("last_status"), "failed")
        self.assertIn("last_attempt_at", state)

        retry_now = now + timedelta(minutes=10)
        self.assertTrue(scheduler.trigger_is_due(settings, "09:30", retry_now, state=state))


if __name__ == "__main__":
    unittest.main()
