"""Stage 5a-3: structured-output retry, budget-blocked retry and LLM statistics."""

from __future__ import annotations

import contextlib
import importlib.util
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

from insightflow.config import load_settings  # noqa: E402
from insightflow.llm.adapter import LLMSemanticProvider  # noqa: E402
from insightflow.llm.client import CostLimitExceeded, LLMConfig  # noqa: E402
from insightflow.llm.contracts import ContractError  # noqa: E402
from insightflow.llm.narrative_gen import LLMNarrativeProvider  # noqa: E402
from insightflow.llm.stats import LLMStats  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402
from insightflow.semantic import TableSample  # noqa: E402


def _bad_response() -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": "not a json answer"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        }
    )


def _good_plan_envelope() -> str:
    draft = {
        "title": "概览",
        "summary": "本期 [[total.records]] 条。",
        "key_points": ["质量分 [[quality.score]]。"],
        "chart_notes": {},
    }
    return json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(draft, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        },
        ensure_ascii=False,
    )


def _config(price: float = 1.0) -> LLMConfig:
    return LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=price, price_out_cny_per_mtok=price)


def _sample() -> TableSample:
    return TableSample(
        name="t",
        source="t.csv",
        sheet="CSV",
        field_names=["销售额"],
        rows=[{"销售额": "100"}],
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
        '"comparison":{"enabled":true,"lookback_days":1},"max_charts":4}',
        encoding="utf-8",
    )
    rows = ["日期,区域,销售额", "2026-11-07,华东,100", "2026-11-06,华东,90"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


class RetryTests(unittest.TestCase):
    def test_semantic_retries_with_stats(self):
        stats = LLMStats()
        provider = LLMSemanticProvider(
            _config(),
            transport=lambda *args: (200, _bad_response()),
            stats=stats,
            max_structured_retries=2,
        )
        with self.assertRaises(ContractError):
            provider.describe(_sample())
        self.assertEqual(stats.calls, 3)
        self.assertEqual(stats.json_failures, 3)
        self.assertEqual(stats.retries, 2)

    def test_retry_blocked_when_budget_too_low(self):
        stats = LLMStats()
        provider = LLMSemanticProvider(
            _config(price=1000.0),
            transport=lambda *args: (200, _bad_response()),
            stats=stats,
            max_structured_retries=2,
            batch_budget_cny=2.0,
        )
        with self.assertRaises(CostLimitExceeded) as ctx:
            provider.describe(_sample())
        self.assertIn("retry_blocked", str(ctx.exception))
        self.assertEqual(stats.calls, 1)
        self.assertEqual(stats.retry_blocked, 1)

    def test_narrative_retries_then_succeeds(self):
        responses = [_bad_response(), _good_plan_envelope()]
        calls: list[int] = []

        def transport(url, headers, payload, timeout):
            calls.append(1)
            return 200, responses[min(len(calls) - 1, 1)]

        stats = LLMStats()
        provider = LLMNarrativeProvider(_config(), transport=transport, stats=stats, max_structured_retries=1)
        draft = provider.generate(["total.records", "quality.score"], ["记录数"], [], [])
        self.assertEqual(draft.title, "概览")
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats.retries, 1)
        self.assertEqual(stats.json_failures, 1)


class PipelineStatsTests(unittest.TestCase):
    def test_json_failure_falls_back_and_records_stats(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        stats = LLMStats()
        provider = LLMSemanticProvider(
            _config(),
            transport=lambda *args: (200, _bad_response()),
            stats=stats,
            max_structured_retries=1,
        )
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(settings, target_date="2026-11-07", force=True, semantic_provider=provider)
        self.assertEqual(record.status, "success")
        artifact = json.loads((day / "semantic_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["source_kind"], "fallback")
        meta = record.metadata.get("llm_stats") or {}
        self.assertGreaterEqual(meta.get("json_failures", 0), 2)
        self.assertGreaterEqual(meta.get("retries", 0), 1)
        self.assertGreaterEqual(meta.get("fallbacks", 0), 1)


class CrossRunStatsTests(unittest.TestCase):
    def _load(self):
        path = ROOT / "scripts" / "llm_stats.py"
        spec = importlib.util.spec_from_file_location("llm_stats", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_zero_runs(self):
        module = self._load()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tests")) as tmp:
            summary = module.aggregate(Path(tmp))
        self.assertEqual(summary["runs"], 0)
        self.assertEqual(summary["json_failures"], 0)
        self.assertEqual(summary["fallback_rate"], 0.0)

    def test_aggregates_run_records(self):
        module = self._load()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tests")) as tmp:
            root = Path(tmp)
            for index, stats in enumerate(
                [
                    {"calls": 4, "json_failures": 1, "retries": 1, "fallbacks": 0, "cost_cny": 0.02},
                    {"calls": 2, "json_failures": 2, "retries": 2, "fallbacks": 2, "cost_cny": 0.01},
                ]
            ):
                day = root / "output" / f"2026-11-0{index + 6}"
                day.mkdir(parents=True)
                (day / "run_record.json").write_text(
                    json.dumps({"run_id": f"r{index}", "metadata": {"llm_stats": stats}}), encoding="utf-8"
                )
            summary = module.aggregate(root)
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["calls"], 6)
        self.assertEqual(summary["json_failures"], 3)
        self.assertEqual(summary["fallbacks"], 2)
        self.assertAlmostEqual(summary["cost_cny"], 0.03, places=6)


if __name__ == "__main__":
    unittest.main()
