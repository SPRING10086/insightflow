"""Stage 6d task 1: full-metric (a) reconcile with all-or-nothing adoption."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.adoption import AdoptionDecision  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.execution import (  # noqa: E402
    ExecutionResult,
    ExecutionSpec,
    MetricRequest,
    reconcile,
    rule_execute,
)
from insightflow.pipeline import execute  # noqa: E402


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["A", "A", "B"],
            "revenue": [100.0, 200.0, 300.0],
            "orders": [1.0, 2.0, 3.0],
        }
    )


def _spec() -> ExecutionSpec:
    return ExecutionSpec(
        table="dataset",
        metric="revenue",
        metric_label="销售额",
        dimension="region",
        dimension_label="区域",
        metrics=[
            MetricRequest("revenue", "销售额", "SUM"),
            MetricRequest("orders", "订单量", "SUM"),
        ],
    )


class RuleMultiMetricTests(unittest.TestCase):
    def test_rule_execute_covers_every_metric_and_groups(self):
        result = rule_execute(_spec(), _frame())
        self.assertAlmostEqual(result.metrics["revenue"], 600.0)
        self.assertAlmostEqual(result.metrics["orders"], 6.0)
        self.assertAlmostEqual(result.metric_groups["revenue"]["rows"][0][1], 300.0)
        self.assertAlmostEqual(result.metric_groups["orders"]["rows"][0][1], 3.0)
        self.assertEqual(result.groups, result.metric_groups["revenue"])


class ReconcileAllMetricTests(unittest.TestCase):
    def _llm(self) -> ExecutionResult:
        rule = rule_execute(_spec(), _frame())
        return ExecutionResult(
            metrics=dict(rule.metrics),
            groups=copy.deepcopy(rule.groups),
            metric_groups=copy.deepcopy(rule.metric_groups),
            source_kind="llm",
        )

    def test_all_metrics_equal_is_accepted(self):
        diff = reconcile(rule_execute(_spec(), _frame()), self._llm())
        self.assertTrue(diff["accepted"])
        self.assertEqual(diff["metric_count"], 2)
        self.assertTrue(all(item["ok"] for item in diff["metrics"].values()))

    def test_each_metric_total_tamper_rejected(self):
        for metric in ("revenue", "orders"):
            with self.subTest(metric=metric):
                llm = self._llm()
                llm.metrics[metric] += 1
                diff = reconcile(rule_execute(_spec(), _frame()), llm)
                self.assertFalse(diff["accepted"])
                self.assertEqual(diff["metrics"][metric]["ok"], False)
                self.assertTrue(any(metric in problem for problem in diff["problems"]))

    def test_each_metric_group_value_tamper_rejected(self):
        for metric in ("revenue", "orders"):
            with self.subTest(metric=metric):
                llm = self._llm()
                llm.metric_groups[metric]["rows"][0][1] += 1
                diff = reconcile(rule_execute(_spec(), _frame()), llm)
                self.assertFalse(diff["accepted"])
                self.assertTrue(any(metric in problem for problem in diff["problems"]))

    def test_each_metric_group_key_tamper_rejected(self):
        for metric in ("revenue", "orders"):
            with self.subTest(metric=metric):
                llm = self._llm()
                llm.metric_groups[metric]["rows"][0][0] = "Z"
                diff = reconcile(rule_execute(_spec(), _frame()), llm)
                self.assertFalse(diff["accepted"])
                self.assertTrue(any("group keys mismatch" in problem for problem in diff["problems"]))

    def test_missing_metric_rejected(self):
        llm = self._llm()
        llm.metrics.pop("orders")
        llm.metric_groups.pop("orders")
        diff = reconcile(rule_execute(_spec(), _frame()), llm)
        self.assertFalse(diff["accepted"])
        self.assertTrue(any("orders" in problem for problem in diff["problems"]))

    def test_non_finite_metric_rejected(self):
        llm = self._llm()
        llm.metrics["orders"] = float("nan")
        diff = reconcile(rule_execute(_spec(), _frame()), llm)
        self.assertFalse(diff["accepted"])
        self.assertTrue(any("finite" in problem for problem in diff["problems"]))

    def test_non_numeric_metric_rejected(self):
        llm = self._llm()
        llm.metrics["orders"] = "six"  # type: ignore[assignment]
        diff = reconcile(rule_execute(_spec(), _frame()), llm)
        self.assertFalse(diff["accepted"])


class FakeMultiExecution:
    name = "fake"
    last_code = None

    def __init__(self, offset: float = 0.0, break_metric: str = "") -> None:
        self.offset = offset
        self.break_metric = break_metric

    def execute(self, spec, frame, allowed_columns, fields=None):
        metrics: dict[str, float] = {}
        groups_by_metric: dict[str, dict] = {}
        primary_groups = {"columns": [], "rows": []}
        for request in spec.requested_metrics():
            series = frame[request.metric].astype(float)
            base = series.mean() if request.aggregation.upper() == "AVG" else series.sum()
            groups = {"columns": [], "rows": []}
            if spec.dimension:
                grouped = frame.groupby(spec.dimension)[request.metric]
                grouped = grouped.mean() if request.aggregation.upper() == "AVG" else grouped.sum()
                groups = {
                    "columns": [spec.dimension],
                    "rows": [[str(k), float(v)] for k, v in grouped.head(50).items()],
                }
            value = float(base) + self.offset
            if request.metric == self.break_metric:
                value += 1
            metrics[request.metric] = value
            groups_by_metric[request.metric] = groups
            if request.metric == spec.metric:
                primary_groups = groups
        return ExecutionResult(
            metrics=metrics,
            groups=primary_groups,
            metric_groups=groups_by_metric,
            source_kind="llm",
            cost_cny=0.01,
            latency_ms=5,
        )


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":true,"lookback_days":1}}',
        encoding="utf-8",
    )
    rows = ["订单日期,区域,销售额,订单量"]
    rows += [f"2026-11-07,华东,{1000 + i},2" for i in range(4)]
    rows += ["2026-11-06,华东,900,2"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _decision() -> AdoptionDecision:
    return AdoptionDecision(
        modes={"semantic": "shadow", "plan": "shadow", "execute": "gated", "narrative": "shadow"},
        reasons={},
        force_rules=False,
    )


class PipelineMultiMetricTests(unittest.TestCase):
    def _run(self, provider):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(
                settings,
                target_date="2026-11-07",
                force=True,
                execution_provider=provider,
                adoption_decision=_decision(),
            )
        day = settings.output_dir / "2026-11-07"
        return record, json.loads((day / "metrics.json").read_text(encoding="utf-8")), day

    def test_all_metrics_adopted_when_all_reconcile(self):
        record, metrics, day = self._run(FakeMultiExecution(offset=1e-7))
        self.assertEqual(record.status, "success")
        kpis = {item["key"]: item for item in metrics["kpis"]}
        self.assertAlmostEqual(kpis["revenue"]["value"], 4006.0000001, places=6)
        self.assertAlmostEqual(kpis["orders"]["value"], 8.0000001, places=6)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertTrue(meta["execution"]["adopted"])
        self.assertEqual(meta["execution"]["metric_count"], 2)
        self.assertEqual(len(meta["execution"]["adopted_metrics"]), 2)

    def test_one_bad_metric_blocks_all_adoption(self):
        record, metrics, day = self._run(FakeMultiExecution(break_metric="orders"))
        self.assertEqual(record.status, "success")
        kpis = {item["key"]: item for item in metrics["kpis"]}
        self.assertAlmostEqual(kpis["revenue"]["value"], 4006.0, places=6)
        self.assertAlmostEqual(kpis["orders"]["value"], 8.0, places=6)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["execution"]["adopted"])
        self.assertEqual(meta["execution"]["gate_reason"], "reconcile_mismatch")
        self.assertFalse(meta["execution"]["accepted"])


if __name__ == "__main__":
    unittest.main()
