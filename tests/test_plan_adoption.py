"""6b-3 plan adoption: pure adapter + gated pipeline consumption."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.adoption import AdoptionDecision  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.models import FieldSpec  # noqa: E402
from insightflow.planning import (  # noqa: E402
    PlanChart,
    PlanDraft,
    PlanResult,
    PlanValidationError,
    adapt_plan,
)
from insightflow.pipeline import execute  # noqa: E402


def _fields() -> dict:
    return {
        "revenue": FieldSpec(original="销售额", canonical="revenue", label="销售额", kind="metric", unit="元"),
        "orders": FieldSpec(original="订单量", canonical="orders", label="订单量", kind="metric", unit="单"),
        "region": FieldSpec(original="区域", canonical="region", label="区域", kind="dimension"),
        "date": FieldSpec(original="日期", canonical="date", label="日期", kind="date"),
    }


def _draft(**overrides) -> PlanDraft:
    base = dict(
        metrics=["orders"],
        dimensions=["region"],
        charts=[
            PlanChart(chart_id="c1", chart_type="bar", title="区域订单", x_field="region", y_field="orders")
        ],
        quality_risks=["未发现显著风险"],
    )
    base.update(overrides)
    return PlanDraft(**base)


class PlanAdapterTests(unittest.TestCase):
    def test_maps_draft_to_analysis_plan(self):
        plan = adapt_plan(_draft(), _fields(), target_date="2026-11-07", rows=3, column_count=4)
        self.assertEqual(plan.primary_metric, "orders")
        self.assertEqual(plan.primary_dimension, "region")
        self.assertIn("kpi", {step.step_id for step in plan.steps})
        self.assertIn("rank", {step.step_id for step in plan.steps})
        self.assertEqual(plan.metric_definitions[0].key, "orders")

    def test_rejects_unknown_metric(self):
        with self.assertRaises(PlanValidationError):
            adapt_plan(_draft(metrics=["ghost"]), _fields(), target_date="2026-11-07")

    def test_rejects_non_metric_as_metric(self):
        with self.assertRaises(PlanValidationError):
            adapt_plan(_draft(metrics=["region"]), _fields(), target_date="2026-11-07")

    def test_rejects_non_dimension_as_dimension(self):
        with self.assertRaises(PlanValidationError):
            adapt_plan(_draft(dimensions=["revenue"]), _fields(), target_date="2026-11-07")

    def test_rejects_missing_metric(self):
        with self.assertRaises(PlanValidationError):
            adapt_plan(_draft(metrics=[]), _fields(), target_date="2026-11-07")

    def test_rejects_unsupported_chart(self):
        chart = PlanChart(chart_id="c1", chart_type="sankey", title="x")
        with self.assertRaises(PlanValidationError):
            adapt_plan(_draft(charts=[chart]), _fields(), target_date="2026-11-07")

    def test_name_map_resolves_original_column_names(self):
        plan = adapt_plan(
            _draft(metrics=["订单量"], dimensions=["区域"]),
            _fields(),
            target_date="2026-11-07",
            name_map={"订单量": "orders", "区域": "region"},
        )
        self.assertEqual(plan.primary_metric, "orders")
        self.assertEqual(plan.primary_dimension, "region")


class FakePlanProvider:
    name = "fake"
    prompt_version = "plan_v1"

    def __init__(self, draft: PlanDraft) -> None:
        self.draft = draft

    def plan(self, fields, anomalies, context):
        return PlanResult(draft=self.draft, prompt_version="plan_v1", cost_cny=0.001, latency_ms=5)


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":true,"lookback_days":1},"max_charts":4}',
        encoding="utf-8",
    )
    rows = ["订单日期,区域,销售额,订单量"]
    rows += [f"2026-11-07,华东,{1000 + i * 10},2" for i in range(3)]
    rows += ["2026-11-07,华南,500,1", "2026-11-06,华东,900,2"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _decision(plan_mode: str, force_rules: bool = False) -> AdoptionDecision:
    modes = {"semantic": "shadow", "plan": plan_mode, "execute": "shadow", "narrative": "shadow"}
    if force_rules:
        modes = {name: "shadow" for name in modes}
    return AdoptionDecision(modes=modes, reasons={}, force_rules=force_rules)


def _run(tmp: Path, provider, decision):
    settings = load_settings(project_root=tmp)
    with contextlib.redirect_stderr(io.StringIO()):
        record = execute(
            settings,
            target_date="2026-11-07",
            force=True,
            plan_provider=provider,
            adoption_decision=decision,
        )
    day = settings.output_dir / "2026-11-07"
    html = max(day.glob("*.html"), key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8")
    metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
    return record, html, day, metrics


class PlanAdoptionTests(unittest.TestCase):
    def test_gated_plan_drives_analyze(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, day, metrics = _run(tmp, FakePlanProvider(_draft()), _decision("gated"))
        self.assertEqual(record.status, "success")
        self.assertEqual(metrics["kpis"][0]["key"], "orders")
        self.assertIn("分析计划由 LLM 提供", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertTrue(meta["plan"]["adopted"])
        self.assertEqual(meta["plan"]["adopted_primary"], "订单量")

    def test_invalid_plan_falls_back_to_rule(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        bad = _draft(metrics=["ghost"])
        record, html, day, metrics = _run(tmp, FakePlanProvider(bad), _decision("gated"))
        self.assertEqual(record.status, "success")
        self.assertEqual(metrics["kpis"][0]["key"], "revenue")
        self.assertNotIn("分析计划由 LLM 提供", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["plan"]["adopted"])
        self.assertTrue(meta["plan"]["gate_reason"].startswith("plan_invalid"))

    def test_shadow_does_not_adopt(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        _record, html, day, metrics = _run(tmp, FakePlanProvider(_draft()), _decision("shadow"))
        self.assertEqual(metrics["kpis"][0]["key"], "revenue")
        self.assertNotIn("分析计划由 LLM 提供", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertNotIn("adopted", meta["plan"])

    def test_force_rules_does_not_adopt(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        _record, html, _day, metrics = _run(
            tmp, FakePlanProvider(_draft()), _decision("gated", force_rules=True)
        )
        self.assertEqual(metrics["kpis"][0]["key"], "revenue")
        self.assertNotIn("分析计划由 LLM 提供", html)


if __name__ == "__main__":
    unittest.main()
