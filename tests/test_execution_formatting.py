"""Regression for 6b-2 review issue 1: adopted KPI formatting must match analyze."""

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
from insightflow.pipeline import execute  # noqa: E402


class AggExecution:
    """Honours spec.aggregation so AVG (rate) metrics reconcile correctly."""

    name = "fake"
    last_code = None

    def __init__(self, offset: float = 0.0) -> None:
        self.offset = offset

    def execute(self, spec, frame, allowed_columns, fields=None):
        metrics: dict[str, float] = {}
        metric_groups: dict[str, dict] = {}
        for request in spec.requested_metrics():
            series = frame[request.metric].astype(float)
            base = series.mean() if str(request.aggregation).upper() == "AVG" else series.sum()
            metrics[request.metric] = float(base) + self.offset
            metric_groups[request.metric] = {"columns": [], "rows": []}
        if not spec.metrics:
            only = list(metrics.values())[0]
            return ExecutionResult(
                metrics={"total": only},
                groups={"columns": [], "rows": []},
                source_kind="llm",
                cost_cny=0.01,
                latency_ms=5,
            )
        return ExecutionResult(
            metrics=metrics,
            groups={"columns": [], "rows": []},
            metric_groups=metric_groups,
            source_kind="llm",
            cost_cny=0.01,
            latency_ms=5,
        )


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project(header: str, rows: list[str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state"}', encoding="utf-8"
    )
    (tmp / "raw" / "a.csv").write_text("\n".join([header, *rows]) + "\n", encoding="utf-8-sig")
    return tmp


def _decision() -> AdoptionDecision:
    return AdoptionDecision(
        modes={"semantic": "shadow", "plan": "shadow", "execute": "gated", "narrative": "shadow"},
        reasons={},
        force_rules=False,
    )


def _kpi(settings, day: Path) -> dict:
    metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
    plan = json.loads((day / "analysis_plan.json").read_text(encoding="utf-8"))
    primary = plan["primary_metric"]
    return next(item for item in metrics["kpis"] if item["key"] == primary)


class AdoptedFormattingTests(unittest.TestCase):
    def test_number_metric_keeps_decimals(self):
        tmp = _project("日期,库存", ["2026-11-07,1234.56", "2026-11-07,2000.10", "2026-11-07,300.00"])
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"

        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        baseline = _kpi(settings, day)
        self.assertIn(".", baseline["formatted"])

        with contextlib.redirect_stderr(io.StringIO()):
            execute(
                settings,
                target_date="2026-11-07",
                force=True,
                execution_provider=AggExecution(offset=0.0000001),
                adoption_decision=_decision(),
            )
        adopted = _kpi(settings, day)
        self.assertEqual(adopted["formatted"], baseline["formatted"])
        self.assertNotEqual(adopted["formatted"], "3,535")

    def test_percent_metric_stays_percent(self):
        tmp = _project("日期,discount", ["2026-11-07,0.852", "2026-11-07,0.901", "2026-11-07,0.805"])
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"

        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        baseline = _kpi(settings, day)
        self.assertTrue(baseline["formatted"].endswith("%"))

        with contextlib.redirect_stderr(io.StringIO()):
            execute(
                settings,
                target_date="2026-11-07",
                force=True,
                execution_provider=AggExecution(offset=0.00000001),
                adoption_decision=_decision(),
            )
        adopted = _kpi(settings, day)
        self.assertEqual(adopted["formatted"], baseline["formatted"])
        self.assertFalse(adopted["formatted"].startswith("0."))
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertTrue(meta["execution"]["adopted"])


if __name__ == "__main__":
    unittest.main()
