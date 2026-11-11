"""6b-2 execute adoption (transitional "b"): adopt only the reconciled total."""

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
from insightflow.execution import ExecutionResult  # noqa: E402
from insightflow.narrative import NarrativeDraft  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402


class FakeExecution:
    name = "fake"

    def __init__(self, offset: float = 0.0, fail: bool = False) -> None:
        self.offset = offset
        self.fail = fail
        self.last_code = None

    def execute(self, spec, frame, allowed_columns, fields=None):
        if self.fail:
            raise RuntimeError("sandbox down")
        metrics: dict[str, float] = {}
        metric_groups: dict[str, dict] = {}
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
            if spec.metrics:
                metrics[request.metric] = float(base) + self.offset
                metric_groups[request.metric] = groups
                if request.metric == spec.metric:
                    primary_groups = groups
            else:
                metrics = {"total": float(base) + self.offset}
                primary_groups = groups
        return ExecutionResult(
            metrics=metrics,
            groups=primary_groups,
            metric_groups=metric_groups,
            source_kind="llm",
            cost_cny=0.01,
            latency_ms=5,
        )


class FakeNarrative:
    name = "fake"
    prompt_version = "narrative_v1"
    last_meta = {"source_kind": "llm", "cost_cny": 0.001, "latency_ms": 5}

    def generate(self, fact_keys, field_labels, chart_ids, samples):
        return NarrativeDraft("x", "<script>alert(1)</script>", [], {})


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
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _decision(execute_mode: str, force_rules: bool = False) -> AdoptionDecision:
    modes = {"semantic": "shadow", "plan": "shadow", "execute": execute_mode, "narrative": "shadow"}
    if force_rules:
        modes = {name: "shadow" for name in modes}
    return AdoptionDecision(modes=modes, reasons={}, force_rules=force_rules)


def _run(tmp: Path, provider, decision, narrative_provider=None):
    settings = load_settings(project_root=tmp)
    with contextlib.redirect_stderr(io.StringIO()):
        record = execute(
            settings,
            target_date="2026-11-07",
            force=True,
            execution_provider=provider,
            narrative_provider=narrative_provider,
            adoption_decision=decision,
        )
    day = settings.output_dir / "2026-11-07"
    html = max(day.glob("*.html"), key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8")
    return record, html, day


class ExecuteAdoptionTests(unittest.TestCase):
    def test_gated_adopts_reconciled_total_and_labels_sources(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, day = _run(tmp, FakeExecution(offset=0.0000005), _decision("gated"))
        self.assertEqual(record.status, "success")
        self.assertIn("数据来源", html)
        self.assertIn("主指标合计", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertTrue(meta["execution"]["adopted"])
        self.assertTrue(meta["execution"]["adopted_metrics"])
        self.assertIn("规则引擎计算", html)
        metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        total = next(item for item in metrics["kpis"] if item["key"] == "revenue")["value"]
        self.assertAlmostEqual(total, 6150.0000005, places=6)

    def test_gated_reconcile_mismatch_falls_back(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, day = _run(tmp, FakeExecution(offset=100), _decision("gated"))
        self.assertEqual(record.status, "success")
        self.assertNotIn("数据来源", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["execution"]["adopted"])
        self.assertEqual(meta["execution"]["gate_reason"], "reconcile_mismatch")

    def test_gated_execution_failure_falls_back(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, day = _run(tmp, FakeExecution(fail=True), _decision("gated"))
        self.assertEqual(record.status, "success")
        self.assertNotIn("数据来源", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["execution"]["adopted"])

    def test_force_rules_blocks_execute_adoption(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, _day = _run(tmp, FakeExecution(), _decision("gated", force_rules=True))
        self.assertNotIn("数据来源", html)
        self.assertTrue(record.metadata["adoption"]["force_rules"])

    def test_narrative_fallback_matches_pure_rule_output(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        _record, with_provider, day = _run(
            tmp,
            FakeExecution(),
            _decision("gated"),
            narrative_provider=FakeNarrative(),
        )
        tmp2 = _project()
        self.addCleanup(_cleanup, tmp2)
        _record2, without_provider, day2 = _run(tmp2, FakeExecution(), _decision("gated"))

        # The fallback must not change any rule insight/number: compare the
        # deterministic content and confirm neither report shows the LLM section.
        with_metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        without_metrics = json.loads((day2 / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(with_metrics["kpis"], without_metrics["kpis"])
        self.assertEqual(with_metrics.get("insights"), without_metrics.get("insights"))
        self.assertIn("narrative_fallback", with_provider)
        self.assertNotIn("智能解读", with_provider)
        self.assertNotIn("智能解读", without_provider)


if __name__ == "__main__":
    unittest.main()
