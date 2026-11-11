"""Stage 6d task 2: real fault injection through executor + pipeline fallback."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.adoption import AdoptionDecision, force_rules_active  # noqa: E402
from insightflow.config import LLMSettings, load_settings  # noqa: E402
from insightflow.llm.client import CostLimitExceeded, LLMConfig  # noqa: E402
from insightflow.llm.contracts import ContractError  # noqa: E402
from insightflow.llm.executor import LLMExecutionProvider  # noqa: E402
from insightflow.llm.sandbox import (  # noqa: E402
    SandboxResourceError,
    SandboxTimeout,
    SandboxViolation,
    run_sandbox,
    validate_code,
)
from insightflow.narrative import NarrativeDraft  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402


def _config(price: float = 1.0) -> LLMConfig:
    return LLMConfig(
        model="m", api_key="k", price_in_cny_per_mtok=price, price_out_cny_per_mtok=price
    )


def _response(code: str) -> str:
    content = json.dumps({"code": code, "notes": "ok"}, ensure_ascii=False)
    return json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
        },
        ensure_ascii=False,
    )


GOOD_MULTI = (
    "metrics = {'revenue': float(df['revenue'].sum()), 'orders': float(df['orders'].sum())}\n"
    "g1 = df.groupby('region')['revenue'].sum()\n"
    "g2 = df.groupby('region')['orders'].sum()\n"
    "metric_groups = {'revenue': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g1.items()]},"
    " 'orders': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g2.items()]}}\n"
    "result = {'metrics': metrics, 'metric_groups': metric_groups, 'groups': metric_groups['revenue']}"
)


class TimeoutProvider:
    """Real busy-loop sandbox call (skip AST only to allow the loop)."""

    name = "timeout-real"

    def execute(self, spec, frame, allowed_columns, fields=None):
        run_sandbox(
            "while True:\n    pass",
            frame,
            allowed_columns=allowed_columns,
            timeout_seconds=1.0,
            memory_mb=1024,
            skip_validation=True,
        )
        raise AssertionError("busy loop must time out")


class RaisingSemantic:
    name = "bad-semantic"

    def describe(self, sample):
        raise ContractError("bad semantic json")


class RaisingPlan:
    name = "bad-plan"

    def plan(self, fields, anomalies, context):
        raise ContractError("bad plan json")


class UnsafeNarrative:
    name = "bad-narrative"
    prompt_version = "v1"

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
        '"comparison":{"enabled":true,"lookback_days":1}}',
        encoding="utf-8",
    )
    rows = ["订单日期,区域,销售额,订单量"]
    rows += [f"2026-11-07,华东,{1000 + i},2" for i in range(4)]
    rows += ["2026-11-06,华东,900,2"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _decision(**modes) -> AdoptionDecision:
    base = {"semantic": "shadow", "plan": "shadow", "execute": "gated", "narrative": "shadow"}
    base.update(modes)
    return AdoptionDecision(modes=base, reasons={}, force_rules=False)


def _run(tmp: Path, provider=None, decision=None, **kwargs):
    settings = load_settings(project_root=tmp)
    with contextlib.redirect_stderr(io.StringIO()):
        record = execute(
            settings,
            target_date="2026-11-07",
            force=True,
            execution_provider=provider,
            adoption_decision=decision,
            **kwargs,
        )
    day = settings.output_dir / "2026-11-07"
    metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
    return record, metrics, day


def _kpi_values(metrics: dict) -> dict[str, float]:
    return {item["key"]: item["value"] for item in metrics["kpis"]}


def _python_pids() -> set[int]:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


class ExecutionFaultTests(unittest.TestCase):
    def _baseline(self, tmp: Path) -> dict[str, float]:
        _record, metrics, _day = _run(tmp)
        return _kpi_values(metrics)

    def _assert_fallback(self, record, metrics, day, baseline, warning_tag):
        self.assertEqual(record.status, "success")
        self.assertEqual(_kpi_values(metrics), baseline)
        self.assertTrue(any(warning_tag in warning for warning in record.warnings))
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertEqual(meta["execution"]["source_kind"], "fallback")
        self.assertFalse(meta["execution"]["adopted"])
        html = max(day.glob("*.html"), key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", html)
        return meta

    def test_memory_limit_real_sandbox_then_pipeline_fallback(self):
        provider = LLMExecutionProvider(
            _config(),
            transport=lambda *args: (200, _response("x = [0] * 100000000")),
            memory_mb=64,
        )
        with self.assertRaises(SandboxResourceError):
            provider.execute(
                _exec_spec(),
                _frame(),
                ["region", "revenue", "orders"],
                [],
            )
        self.assertGreaterEqual(provider.stats.retries, 1)

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = self._baseline(tmp)
        record, metrics, day = _run(tmp, provider, _decision())
        self._assert_fallback(record, metrics, day, baseline, "[execution_fallback]")

    def test_timeout_kills_worker_tree_and_pipeline_falls_back(self):
        with self.assertRaises(SandboxTimeout):
            TimeoutProvider().execute(_exec_spec(), _frame(), ["region", "revenue", "orders"])
        # A fresh sandbox call after the kill must still succeed (no orphan lock).
        data = run_sandbox(GOOD_MULTI, _frame(), allowed_columns={"region", "revenue", "orders"}, memory_mb=1024)
        self.assertAlmostEqual(data["metrics"]["revenue"], 600.0)

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = self._baseline(tmp)
        before = _python_pids()
        record, metrics, day = _run(tmp, TimeoutProvider(), _decision())
        self._assert_fallback(record, metrics, day, baseline, "[execution_fallback]")
        time.sleep(0.5)
        self.assertEqual(_python_pids() - before, set(), "timed-out worker must not linger")

    def test_illegal_code_retries_then_pipeline_fallback(self):
        bad_codes = [
            "import os\nresult = {'metrics': {}, 'groups': {'columns': [], 'rows': []}}",
            "eval('1')\nresult = {'metrics': {}, 'groups': {'columns': [], 'rows': []}}",
            "x = df.__class__\nresult = {'metrics': {}, 'groups': {'columns': [], 'rows': []}}",
            "df = df.assign(v=1)\nresult = {'metrics': {}, 'groups': {'columns': [], 'rows': []}}",
        ]
        for code in bad_codes:
            with self.subTest(code=code.splitlines()[0]):
                calls: list[int] = []

                def transport(url, headers, payload, timeout, _calls=calls, _code=code):
                    _calls.append(1)
                    return 200, _response(_code)

                provider = LLMExecutionProvider(_config(), transport=transport, memory_mb=2048)
                with self.assertRaises(SandboxViolation):
                    provider.execute(_exec_spec(), _frame(), ["region", "revenue", "orders"], [])
                self.assertEqual(len(calls), 2)

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = self._baseline(tmp)
        provider = LLMExecutionProvider(
            _config(),
            transport=lambda *args: (200, _response(bad_codes[0])),
            memory_mb=2048,
        )
        record, metrics, day = _run(tmp, provider, _decision())
        self._assert_fallback(record, metrics, day, baseline, "[execution_fallback]")

    def test_json_failures_retry_then_pipeline_fallback(self):
        def body(content: str) -> str:
            return json.dumps(
                {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
                }
            )

        bodies = [
            body('{"code": "x = 1'),
            body(json.dumps({"notes": "no code"})),
            body(json.dumps({"code": 123})),
        ]
        for body in bodies:
            with self.subTest(body=body[:20]):
                calls: list[int] = []

                def transport(url, headers, payload, timeout, _calls=calls, _body=body):
                    _calls.append(1)
                    return 200, _body

                provider = LLMExecutionProvider(_config(), transport=transport, memory_mb=2048)
                with self.assertRaises(ContractError):
                    provider.execute(_exec_spec(), _frame(), ["region", "revenue", "orders"], [])
                self.assertEqual(len(calls), 2)
                self.assertGreaterEqual(provider.stats.json_failures, 1)

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = self._baseline(tmp)
        provider = LLMExecutionProvider(
            _config(), transport=lambda *args: (200, "not json"), memory_mb=2048
        )
        record, metrics, day = _run(tmp, provider, _decision())
        self._assert_fallback(record, metrics, day, baseline, "[execution_fallback]")

    def test_reconcile_mismatch_blocks_all_adoption(self):
        bad = (
            "metrics = {'revenue': float(df['revenue'].sum()) + 1, 'orders': float(df['orders'].sum())}\n"
            "g1 = df.groupby('region')['revenue'].sum()\n"
            "g2 = df.groupby('region')['orders'].sum()\n"
            "metric_groups = {'revenue': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g1.items()]},"
            " 'orders': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g2.items()]}}\n"
            "result = {'metrics': metrics, 'metric_groups': metric_groups, 'groups': metric_groups['revenue']}"
        )
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = self._baseline(tmp)
        provider = LLMExecutionProvider(_config(), transport=lambda *args: (200, _response(bad)), memory_mb=2048)
        record, metrics, day = _run(tmp, provider, _decision())
        self.assertEqual(record.status, "success")
        self.assertEqual(_kpi_values(metrics), baseline)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["execution"]["accepted"])
        self.assertFalse(meta["execution"]["adopted"])
        self.assertEqual(meta["execution"]["gate_reason"], "reconcile_mismatch")

    def test_retry_blocked_by_budget(self):
        calls: list[int] = []

        def transport(url, headers, payload, timeout):
            calls.append(1)
            return 200, _response("import os\nresult = {'metrics': {}, 'groups': {'columns': [], 'rows': []}}")

        provider = LLMExecutionProvider(_config(price=0.6), transport=transport, memory_mb=2048, batch_budget_cny=0.001)
        with self.assertRaises(CostLimitExceeded) as ctx:
            provider.execute(_exec_spec(), _frame(), ["region", "revenue", "orders"], [])
        self.assertIn("retry_blocked", str(ctx.exception))
        self.assertEqual(provider.stats.retry_blocked, 1)
        self.assertEqual(len(calls), 2)


def _frame():
    import pandas as pd

    return pd.DataFrame(
        {
            "region": ["A", "A", "B"],
            "revenue": [100.0, 200.0, 300.0],
            "orders": [1.0, 2.0, 3.0],
        }
    )


def _exec_spec():
    from insightflow.execution import ExecutionSpec, MetricRequest

    return ExecutionSpec(
        table="dataset",
        metric="revenue",
        metric_label="销售额",
        dimension="region",
        metrics=[MetricRequest("revenue", "销售额", "SUM"), MetricRequest("orders", "订单量", "SUM")],
    )


class KillSwitchTests(unittest.TestCase):
    def test_force_rules_active_config_and_env(self):
        self.assertTrue(force_rules_active(LLMSettings(force_rules=True)))
        with patch.dict("os.environ", {"INSIGHTFLOW_FORCE_RULES": "1"}):
            self.assertTrue(force_rules_active(LLMSettings()))
        with patch.dict("os.environ", {"INSIGHTFLOW_FORCE_RULES": "0"}):
            self.assertFalse(force_rules_active(LLMSettings(force_rules=True)))

    def test_gui_builds_no_providers_under_force_rules(self):
        from insightflow.gui import services

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        settings.llm.enabled = True
        settings.llm.semantic = settings.llm.plan = settings.llm.execute = settings.llm.narrative = True
        settings.llm.force_rules = True

        with patch.object(services, "build_semantic_provider", side_effect=AssertionError("no build")), patch.object(
            services, "build_plan_provider", side_effect=AssertionError("no build")
        ), patch.object(services, "build_execution_provider", side_effect=AssertionError("no build")), patch.object(
            services, "build_narrative_provider", side_effect=AssertionError("no build")
        ):
            providers, warnings = services.build_llm_providers(settings)
        self.assertTrue(all(value is None for value in providers.values()))
        self.assertEqual(warnings, [])

    def test_gui_env_force_rules_skips_builders(self):
        from insightflow.gui import services

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        settings.llm.enabled = True
        settings.llm.semantic = settings.llm.plan = settings.llm.execute = settings.llm.narrative = True

        with patch.dict(os.environ, {"INSIGHTFLOW_FORCE_RULES": "1"}), patch.object(
            services, "build_semantic_provider", side_effect=AssertionError("no build")
        ), patch.object(
            services, "build_plan_provider", side_effect=AssertionError("no build")
        ), patch.object(
            services, "build_execution_provider", side_effect=AssertionError("no build")
        ), patch.object(
            services, "build_narrative_provider", side_effect=AssertionError("no build")
        ):
            providers, warnings = services.build_llm_providers(settings)
        self.assertTrue(all(value is None for value in providers.values()))
        self.assertEqual(warnings, [])


class OtherCapabilityFallbackTests(unittest.TestCase):
    def test_semantic_contract_failure_falls_back(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = _kpi_values(_run(tmp)[1])
        record, metrics, day = _run(
            tmp, None, _decision(semantic="gated"), semantic_provider=RaisingSemantic()
        )
        self.assertEqual(record.status, "success")
        self.assertEqual(_kpi_values(metrics), baseline)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["semantic_schema"]["adopted"])
        self.assertIn("source_fallback", meta["semantic_schema"]["gate_reason"])

    def test_plan_contract_failure_falls_back(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = _kpi_values(_run(tmp)[1])
        record, metrics, day = _run(tmp, None, _decision(plan="gated"), plan_provider=RaisingPlan())
        self.assertEqual(record.status, "success")
        self.assertEqual(_kpi_values(metrics), baseline)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["plan"]["adopted"])
        self.assertIn("source_fallback", meta["plan"]["gate_reason"])

    def test_narrative_unsafe_markup_falls_back_without_xss(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        baseline = _kpi_values(_run(tmp)[1])
        record, metrics, day = _run(
            tmp, None, _decision(narrative="gated"), narrative_provider=UnsafeNarrative()
        )
        self.assertEqual(record.status, "success")
        self.assertEqual(_kpi_values(metrics), baseline)
        html = max(day.glob("*.html"), key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("智能解读", html)


if __name__ == "__main__":
    unittest.main()
