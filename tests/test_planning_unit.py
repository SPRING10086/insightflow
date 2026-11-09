from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow import planning  # noqa: E402
from insightflow.models import FieldSpec  # noqa: E402
from insightflow.planning import PlanArtifact, PlanChart, PlanDraft, PlanValidationError  # noqa: E402

ROLES = {"日期": "date", "区域": "dimension", "订单号": "id", "销售额": "metric", "毛利率": "metric"}


def _draft(**overrides) -> PlanDraft:
    base = dict(metrics=["销售额"], dimensions=["区域"], quality_risks=["未发现显著风险"])
    base.update(overrides)
    return PlanDraft(**base)


class ClosureTests(unittest.TestCase):
    def test_phantom_field_in_metrics(self):
        with self.assertRaises(PlanValidationError) as ctx:
            planning.validate_plan_closure(_draft(metrics=["幽灵字段"]), ROLES)
        self.assertIn("不存在", str(ctx.exception))

    def test_text_or_id_cannot_be_metric(self):
        for name in ("订单号", "区域"):
            with self.assertRaises(PlanValidationError):
                planning.validate_plan_closure(_draft(metrics=[name]), ROLES)

    def test_metric_cannot_be_dimension(self):
        with self.assertRaises(PlanValidationError):
            planning.validate_plan_closure(_draft(dimensions=["销售额"]), ROLES)
        with self.assertRaises(PlanValidationError):
            planning.validate_plan_closure(_draft(dimensions=["幽灵字段"]), ROLES)

    def test_quality_risks_required(self):
        with self.assertRaises(PlanValidationError):
            planning.validate_plan_closure(_draft(quality_risks=[]), ROLES)

    def test_chart_branches(self):
        cases = [
            PlanChart(chart_id="c", chart_type="unknown", title="t"),
            PlanChart(chart_id="c", chart_type="bar", title="t", y_field="幽灵字段"),
            PlanChart(chart_id="c", chart_type="bar", title="t", y_field="区域"),
            PlanChart(chart_id="c", chart_type="bar", title="t", x_field="幽灵字段"),
            PlanChart(chart_id="c", chart_type="bar", title="t", x_field="订单号"),
            PlanChart(chart_id="c", chart_type="bar", title="t", x_field="区域", series_field="销售额"),
        ]
        for chart in cases:
            with self.assertRaises(PlanValidationError, msg=chart):
                planning.validate_plan_closure(_draft(charts=[chart]), ROLES)

    def test_valid_chart_passes(self):
        planning.validate_plan_closure(
            _draft(charts=[PlanChart(chart_id="c", chart_type="bar", title="t", x_field="区域", y_field="销售额")]),
            ROLES,
        )

    def test_histogram_metric_x_passes(self):
        planning.validate_plan_closure(
            _draft(charts=[PlanChart(chart_id="c", chart_type="histogram", title="t", x_field="销售额")]),
            ROLES,
        )

    def test_histogram_and_scatter_reject_categorical_x(self):
        for chart_type in ("histogram", "scatter"):
            with self.assertRaises(PlanValidationError):
                planning.validate_plan_closure(
                    _draft(charts=[PlanChart(chart_id="c", chart_type=chart_type, title="t", x_field="区域")]),
                    ROLES,
                )


def _rule_plan():
    steps = [
        SimpleNamespace(step_id="kpi", title="核心指标", enabled=True, chart_type=""),
        SimpleNamespace(step_id="rank", title="结构排名", enabled=True, chart_type="bar"),
        SimpleNamespace(step_id="skip", title="跳过", enabled=False, chart_type="line"),
    ]
    return SimpleNamespace(
        metrics=["revenue"],
        dimensions=["region"],
        primary_metric="revenue",
        primary_dimension="region",
        steps=steps,
    )


def _fields():
    return {
        "revenue": FieldSpec(original="销售额", canonical="revenue", label="销售额", kind="metric", unit="元"),
        "region": FieldSpec(original="区域", canonical="region", label="区域", kind="dimension", unit=""),
    }


class RuleArtifactTests(unittest.TestCase):
    def test_clean_data_gets_explicit_no_risk(self):
        cleaning = SimpleNamespace(duplicates_removed=0, missing_filled={}, outlier_counts={}, garbled_repaired=0)
        settings = SimpleNamespace(comparison=SimpleNamespace(enabled=True))
        artifact = planning.rule_plan_artifact(_rule_plan(), _fields(), cleaning, settings)
        self.assertEqual([item.chart_id for item in artifact.plan.charts], ["rank"])
        self.assertTrue(any("未发现显著风险" in item for item in artifact.plan.quality_risks))
        self.assertEqual(artifact.plan.comparisons, ["前一日环比"])

    def test_every_risk_branch(self):
        cleaning = SimpleNamespace(
            duplicates_removed=3,
            missing_filled={"revenue": 10},
            outlier_counts={"revenue": 2},
            garbled_repaired=1,
        )
        settings = SimpleNamespace(comparison=SimpleNamespace(enabled=False))
        artifact = planning.rule_plan_artifact(_rule_plan(), _fields(), cleaning, settings)
        risks = " ".join(artifact.plan.quality_risks)
        for needle in ("重复", "填补", "离群", "乱码"):
            self.assertIn(needle, risks)
        self.assertEqual(artifact.plan.comparisons, [])

    def test_serialisation_and_save(self):
        cleaning = SimpleNamespace(duplicates_removed=0, missing_filled={}, outlier_counts={}, garbled_repaired=0)
        settings = SimpleNamespace(comparison=SimpleNamespace(enabled=False))
        artifact = planning.rule_plan_artifact(_rule_plan(), _fields(), cleaning, settings)
        payload = artifact.to_dict()
        self.assertEqual(payload["plan"]["metrics"], ["销售额"])
        self.assertIn("option_skeleton", payload["plan"]["charts"][0])

        with tempfile.TemporaryDirectory() as tmp:
            written = planning.save_plan_artifacts(
                Path(tmp), artifact, artifact, planning.plan_diff(artifact, artifact)
            )
            names = sorted(path.name for path in written)
            self.assertEqual(names, ["plan_diff.json", "plan_llm.json", "plan_rule.json"])
            loaded = json.loads((Path(tmp) / "plan_rule.json").read_text(encoding="utf-8"))
            self.assertEqual(loaded["plan"]["dimensions"], ["区域"])

    def test_diff_handles_disjoint_sets(self):
        left = PlanArtifact("1.0", "rules", "now", "rules", "rule", "dataset", _draft())
        right = PlanArtifact(
            "1.0",
            "plan_v1",
            "now",
            "fake",
            "llm",
            "dataset",
            PlanDraft(metrics=["毛利率"], dimensions=[], quality_risks=["r"]),
        )
        diff = planning.plan_diff(left, right)
        self.assertEqual(diff["metrics"]["only_rule"], ["销售额"])
        self.assertEqual(diff["metrics"]["only_llm"], ["毛利率"])
        self.assertEqual(diff["dimensions"]["only_rule"], ["区域"])
        self.assertEqual(diff["metrics"]["jaccard"], 0.0)


if __name__ == "__main__":
    unittest.main()
