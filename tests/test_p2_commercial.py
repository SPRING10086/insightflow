from __future__ import annotations

import contextlib
import csv
import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import reconcile  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402

import coverage_gate  # noqa: E402


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project(header: str, rows: list[str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":false},"max_charts":4}',
        encoding="utf-8",
    )
    (tmp / "raw" / "sales.csv").write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _run(tmp: Path) -> tuple[object, Path]:
    settings = load_settings(project_root=tmp)
    with contextlib.redirect_stderr(io.StringIO()):
        record = execute(settings, target_date=date(2026, 11, 7), force=True)
    return record, settings.output_dir / "2026-11-07"


class ImputationDoubleTrackTests(unittest.TestCase):
    def test_imputed_sum_and_valid_value_reach_the_report(self):
        tmp = _project(
            "订单日期,区域,销售额,订单量",
            ["2026-11-07,华东,100,2", "2026-11-07,华东,,3", "2026-11-07,华东,300,4"],
        )
        self.addCleanup(_cleanup, tmp)
        record, day = _run(tmp)
        self.assertEqual(record.status, "success")

        metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        quality = metrics["quality"]
        # median(100, 300) = 200 filled once → 200 of the 600 total is estimated
        self.assertEqual(quality["missing_filled"].get("revenue"), 1)
        self.assertAlmostEqual(quality["imputed_sums"]["revenue"], 200.0, places=6)

        kpi = next(item for item in metrics["kpis"] if item["key"] == "revenue")
        self.assertEqual(kpi["imputed_count"], 1)
        self.assertAlmostEqual(kpi["valid_value"], 400.0, places=6)
        self.assertIn("含填补", kpi["hint"])

        html = next(day.glob("InsightFlow_*.html")).read_text(encoding="utf-8")
        self.assertIn("有效值", html)
        self.assertIn("含填补 1 个", html)

    def test_clean_data_has_no_valid_line(self):
        tmp = _project("订单日期,区域,销售额,订单量", ["2026-11-07,华东,100,2", "2026-11-07,华南,300,4"])
        self.addCleanup(_cleanup, tmp)
        record, day = _run(tmp)
        metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        kpi = next(item for item in metrics["kpis"] if item["key"] == "revenue")
        self.assertEqual(kpi["imputed_count"], 0)
        self.assertNotIn("有效值", next(day.glob("InsightFlow_*.html")).read_text(encoding="utf-8"))


class MixedUnitTests(unittest.TestCase):
    def test_header_and_inline_units_are_scaled_per_value(self):
        tmp = _project(
            "订单日期,区域,销售额(万元)",
            ["2026-11-07,华东,1.2万", "2026-11-07,华南,12000", "2026-11-07,华北,3"],
        )
        self.addCleanup(_cleanup, tmp)
        record, day = _run(tmp)
        self.assertEqual(record.status, "success")

        with (day / "cleaned_data.csv").open(encoding="utf-8-sig", newline="") as handle:
            values = {row["region"]: float(row["revenue"]) for row in csv.DictReader(handle)}
        # 1.2万 → 12,000 元; 12000(万元) → 120,000,000 元; 3(万元) → 30,000 元
        self.assertEqual(values, {"华东": 12000.0, "华北": 30000.0, "华南": 120000000.0})

        mixed = [warning for warning in record.warnings if "mixed_unit" in warning]
        self.assertEqual(len(mixed), 1)
        self.assertIn("同时存在带单位", mixed[0])

    def test_column_without_inline_units_uses_header_multiplier(self):
        tmp = _project("订单日期,区域,销售额(万元)", ["2026-11-07,华东,1", "2026-11-07,华南,2"])
        self.addCleanup(_cleanup, tmp)
        record, day = _run(tmp)
        with (day / "cleaned_data.csv").open(encoding="utf-8-sig", newline="") as handle:
            values = [float(row["revenue"]) for row in csv.DictReader(handle)]
        self.assertEqual(values, [10000.0, 20000.0])
        self.assertEqual([w for w in record.warnings if "mixed_unit" in w], [])


class ReconcileTests(unittest.TestCase):
    def _day(self) -> Path:
        tmp = _project(
            "订单日期,区域,销售额,订单量",
            ["2026-11-07,华东,100,2", "2026-11-07,华南,300,4", "2026-11-06,华东,50,1"],
        )
        self.addCleanup(_cleanup, tmp)
        record, day = _run(tmp)
        self.assertEqual(record.status, "success")
        return day

    def test_reconcile_passes_on_a_healthy_run(self):
        report = reconcile.reconcile(self._day())
        self.assertTrue(report["ok"], report["errors"])
        names = [check["name"] for check in report["checks"]]
        self.assertTrue(any("行数守恒" in name for name in names))
        self.assertTrue(any("指标合计对账" in name for name in names))
        self.assertTrue(any("血缘行号" in name for name in names))

    def test_reconcile_detects_tampered_total(self):
        day = self._day()
        metrics_path = day / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["target_totals"]["metrics"]["revenue"] += 1.0
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")

        report = reconcile.reconcile(day)
        self.assertFalse(report["ok"])
        self.assertTrue(any("指标合计对账" in error for error in report["errors"]))

    def test_reconcile_detects_row_count_mismatch(self):
        day = self._day()
        metrics_path = day / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["quality"]["rows_out"] += 1
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")

        report = reconcile.reconcile(day)
        self.assertFalse(report["ok"])
        self.assertTrue(any("清洗后行数" in error for error in report["errors"]))


class CoverageGateUnitTests(unittest.TestCase):
    def test_target_selection_excludes_gui(self):
        self.assertTrue(coverage_gate._target(str(ROOT / "src" / "insightflow" / "pipeline.py")))
        self.assertFalse(coverage_gate._target(str(ROOT / "src" / "insightflow" / "gui" / "app.py")))
        self.assertFalse(coverage_gate._target(str(ROOT / "src" / "insightflow" / "__main__.py")))

    def test_statement_count_is_positive(self):
        statements = coverage_gate._statements(ROOT / "src" / "insightflow" / "cleaning.py")
        self.assertGreater(len(statements), 50)


if __name__ == "__main__":
    unittest.main()
