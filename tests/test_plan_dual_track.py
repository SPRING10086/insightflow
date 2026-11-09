from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.llm.contracts import ContractError  # noqa: E402
from insightflow.llm.plan_contracts import parse_plan  # noqa: E402
from insightflow.planning import PlanChart, PlanDraft, PlanResult, plan_diff  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402

ROLES = {"日期": "date", "区域": "dimension", "订单号": "id", "销售额": "metric", "毛利率": "metric"}

VALID = {
    "questions": ["哪个区域销售额最高？"],
    "metrics": ["销售额"],
    "dimensions": ["区域"],
    "comparisons": ["前一日环比"],
    "charts": [
        {
            "chart_id": "chart_1",
            "chart_type": "bar",
            "title": "区域销售额",
            "x_field": "区域",
            "y_field": "销售额",
            "option_skeleton": {
                "xAxis": {"type": "category", "data": []},
                "yAxis": {"type": "value"},
                "series": [{"type": "bar", "data": []}],
            },
        }
    ],
    "quality_risks": ["未发现显著风险"],
}


def _plan(**overrides) -> dict:
    data = json.loads(json.dumps(VALID, ensure_ascii=False))
    data.update(overrides)
    return data


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
    rows = ["订单日期,区域,品类,销售额,订单量"]
    rows += [f"2026-11-07,华东,手机数码,{1000 + i * 10},2" for i in range(6)]
    rows += ["2026-11-06,华东,手机数码,900,2"]
    (tmp / "raw" / "sales.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


class FakePlanProvider:
    name = "fake-plan"
    prompt_version = "plan_v1"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def plan(self, fields, anomalies, context):
        self.calls += 1
        if self.fail:
            raise RuntimeError("plan provider down")
        metrics = [field.name for field in fields if field.role == "metric"][:1]
        dimensions = [field.name for field in fields if field.role == "dimension"][:1]
        draft = PlanDraft(
            questions=["哪个区域销售额最高？"],
            metrics=metrics,
            dimensions=dimensions,
            comparisons=["前一日环比"],
            charts=[
                PlanChart(
                    chart_id="chart_1",
                    chart_type="bar",
                    title="区域销售额",
                    x_field=dimensions[0] if dimensions else "",
                    y_field=metrics[0] if metrics else "",
                    option_skeleton={"xAxis": {"type": "category", "data": []}, "series": [{"type": "bar", "data": []}]},
                )
            ],
            quality_risks=["未发现显著风险"],
        )
        return PlanResult(draft=draft, prompt_version="plan_v1", cost_cny=0.01, latency_ms=7)


class PlanContractTests(unittest.TestCase):
    def test_valid_plan_passes(self):
        draft = parse_plan(json.dumps(VALID, ensure_ascii=False), ROLES)
        self.assertEqual(draft.metrics, ["销售额"])
        self.assertEqual(draft.charts[0].chart_type, "bar")

    def test_phantom_field_rejected(self):
        bad = _plan(metrics=["不存在字段"])
        with self.assertRaises(ContractError):
            parse_plan(json.dumps(bad, ensure_ascii=False), ROLES)

    def test_text_or_id_as_metric_rejected(self):
        for name in ("订单号", "区域"):
            with self.assertRaises(ContractError):
                parse_plan(json.dumps(_plan(metrics=[name]), ensure_ascii=False), ROLES)

    def test_metric_as_dimension_rejected(self):
        with self.assertRaises(ContractError):
            parse_plan(json.dumps(_plan(dimensions=["销售额"]), ensure_ascii=False), ROLES)

    def test_empty_quality_risks_rejected(self):
        with self.assertRaises(ContractError):
            parse_plan(json.dumps(_plan(quality_risks=[]), ensure_ascii=False), ROLES)

    def test_non_empty_chart_slots_rejected(self):
        charts = json.loads(json.dumps(VALID["charts"], ensure_ascii=False))
        charts[0]["option_skeleton"]["series"][0]["data"] = [1, 2]
        with self.assertRaises(ContractError):
            parse_plan(json.dumps(_plan(charts=charts), ensure_ascii=False), ROLES)

    def test_business_numbers_rejected(self):
        with self.assertRaises(ContractError):
            parse_plan(json.dumps(_plan(total=100), ensure_ascii=False), ROLES)

    def test_histogram_allows_metric_on_x_but_bar_does_not(self):
        chart = {
            "chart_id": "c1",
            "chart_type": "bar",
            "title": "销售额分布",
            "x_field": "销售额",
            "y_field": "销售额",
            "option_skeleton": {"xAxis": {"data": []}, "series": [{"data": []}]},
        }
        with self.assertRaises(ContractError):
            parse_plan(json.dumps(_plan(charts=[chart]), ensure_ascii=False), ROLES)
        chart["chart_type"] = "histogram"
        draft = parse_plan(json.dumps(_plan(charts=[chart]), ensure_ascii=False), ROLES)
        self.assertEqual(draft.charts[0].chart_type, "histogram")

    def test_plan_diff_reports_only_llm_items(self):
        from insightflow.planning import PlanArtifact

        rule = PlanArtifact(
            version="1.0",
            prompt_version="rules",
            generated_at="now",
            provider="rules",
            source_kind="rule",
            table="dataset",
            plan=PlanDraft(metrics=["销售额"], dimensions=["区域"], quality_risks=["规则风险"]),
        )
        other = PlanArtifact(
            version="1.0",
            prompt_version="plan_v1",
            generated_at="now",
            provider="fake",
            source_kind="llm",
            table="dataset",
            plan=PlanDraft(metrics=["销售额", "毛利率"], dimensions=[], quality_risks=["LLM 风险"]),
        )
        diff = plan_diff(rule, other)
        self.assertEqual(diff["metrics"]["only_llm"], ["毛利率"])
        self.assertEqual(diff["metrics"]["jaccard"], 0.5)
        self.assertEqual(diff["dimensions"]["only_rule"], ["区域"])
        self.assertEqual(diff["quality_risks"]["llm_count"], 1)


class PipelinePlanTests(unittest.TestCase):
    def test_plan_dual_track_writes_artifacts_and_keeps_numbers(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"

        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        baseline = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        self.assertFalse((day / "plan_llm.json").exists())

        provider = FakePlanProvider()
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(settings, target_date="2026-11-07", force=True, plan_provider=provider)
        self.assertEqual(record.status, "success")
        self.assertEqual(provider.calls, 1)
        for name in ("plan_rule.json", "plan_llm.json", "plan_diff.json"):
            self.assertTrue((day / name).is_file(), name)
        artifact = json.loads((day / "plan_llm.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["source_kind"], "llm")
        self.assertTrue(artifact["plan"]["quality_risks"])
        self.assertEqual(record.metadata["plan"]["source_kind"], "llm")

        after = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["kpis"], after["kpis"])
        self.assertEqual(baseline["target_totals"], after["target_totals"])

    def test_plan_failure_falls_back_and_run_succeeds(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"

        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(
                settings, target_date="2026-11-07", force=True, plan_provider=FakePlanProvider(fail=True)
            )
        self.assertEqual(record.status, "success")
        self.assertTrue(any("plan_fallback" in warning for warning in record.warnings))
        artifact = json.loads((day / "plan_llm.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["source_kind"], "fallback")
        self.assertIn("plan provider down", artifact["error"])
        self.assertTrue(artifact["plan"]["quality_risks"])

    def test_clean_data_gets_explicit_no_risk_statement(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True, plan_provider=FakePlanProvider())
        rule_plan = json.loads((day / "plan_rule.json").read_text(encoding="utf-8"))
        risks = rule_plan["plan"]["quality_risks"]
        self.assertTrue(risks, "规则计划也必须带质量风险项")
        self.assertTrue(any("未发现显著风险" in item or "重复" in item or "填补" in item for item in risks))


class CliPlanDegradeTests(unittest.TestCase):
    def test_plan_llm_without_config_degrades(self):
        from insightflow import cli

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        keys = ("INSIGHTFLOW_LLM_MODEL", "DEEPSEEK_API_KEY", "INSIGHTFLOW_LLM_PRICE_IN", "INSIGHTFLOW_LLM_PRICE_OUT")
        cleaned = {key: value for key, value in os.environ.items() if key not in keys}
        err = io.StringIO()
        with patch.dict(os.environ, cleaned, clear=True), patch(
            "insightflow.llm.client.dotenv_candidates", return_value=[]
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["run", "--date", "2026-11-07", "--force", "--plan", "llm", "--project-root", str(tmp)])
        self.assertEqual(code, 0)
        self.assertIn("降级", err.getvalue())
        self.assertTrue((tmp / "out" / "2026-11-07" / "metrics.json").is_file())
        self.assertFalse((tmp / "out" / "2026-11-07" / "plan_llm.json").exists())


if __name__ == "__main__":
    unittest.main()
