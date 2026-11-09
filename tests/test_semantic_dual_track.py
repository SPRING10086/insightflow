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
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow import semantic  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.llm import payloads  # noqa: E402
from insightflow.llm.adapter import LLMSemanticProvider  # noqa: E402
from insightflow.llm.client import CostLimitExceeded, LLMConfig  # noqa: E402
from insightflow.models import FieldSpec  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402
from insightflow.semantic import FieldSemantics, ProviderTableResult, TableSample  # noqa: E402


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


def _fake_schema() -> SimpleNamespace:
    field = FieldSpec(original="销售额", canonical="revenue", label="销售额", kind="metric", unit="元")
    return SimpleNamespace(fields={"revenue": field})


def _fake_table() -> SimpleNamespace:
    return SimpleNamespace(
        source=SimpleNamespace(relative_path="sales.csv"),
        sheet_label="CSV",
    )


class FakeProvider:
    name = "fake"
    prompt_version = "test-v1"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def describe(self, sample: TableSample) -> ProviderTableResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        fields = []
        for name in sample.field_names:
            if "日期" in name:
                role = "date"
            elif "销售额" in name or "订单量" in name:
                role = "metric"
            elif "区域" in name or "品类" in name:
                role = "dimension"
            else:
                role = "text"
            fields.append(
                FieldSemantics(name=name, role=role, unit="", meaning="", confidence=0.9, source="llm", relations=[])
            )
        return ProviderTableResult(fields=fields, prompt_version="test-v1", cost_cny=0.01, latency_ms=5)


class PayloadCardinalityTests(unittest.TestCase):
    def test_payload_carries_counts_but_not_values(self):
        rows = [
            {"区域": "华东", "企业名称": "甲公司"},
            {"区域": "华东", "企业名称": "乙公司"},
            {"区域": "华南", "企业名称": "丙公司"},
        ]
        payload = payloads.build_payload("t", "s.csv", ["区域", "企业名称"], rows)
        stats = {field["name"]: field for field in payload["fields"]}
        self.assertEqual(stats["区域"]["unique_count"], 2)
        self.assertAlmostEqual(stats["区域"]["unique_ratio"], 0.6667, places=3)
        self.assertEqual(stats["企业名称"]["unique_count"], 3)
        self.assertEqual(stats["企业名称"]["unique_ratio"], 1.0)
        # Counts only: the full table is not serialised, and samples stay capped.
        self.assertEqual(len(payload["samples"]), 3)
        self.assertNotIn("unique_ratio", payload["samples"][0])

    def test_prompt_v3_contains_cardinality_rules(self):
        prompt = (ROOT / "src" / "insightflow" / "llm" / "prompts" / "analysis_plan_v3.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("unique_ratio", prompt)
        self.assertIn("高基数自由文本", prompt)
        self.assertIn("不要因为", prompt)


class SemanticArtifactTests(unittest.TestCase):
    def test_rule_artifact_and_diff_flags_key_role_error(self):
        rule = semantic.rule_artifact([(_fake_table(), _fake_schema())])
        self.assertEqual(rule.source_kind, "rule")
        self.assertEqual(rule.tables[0].fields[0].role, "metric")

        fields = [FieldSemantics(name="销售额", role="dimension", source="llm")]
        other = semantic.SemanticArtifact(
            version="1.0",
            prompt_version="test",
            generated_at="now",
            provider="fake",
            source_kind="llm",
            tables=[
                semantic.TableSemantics(
                    table="sales.csv",
                    source="sales.csv",
                    sheet="CSV",
                    provider="fake",
                    source_kind="llm",
                    fields=fields,
                )
            ],
        )
        diff = semantic.diff_artifacts(rule, other)
        self.assertEqual(diff["matched_fields"], 1)
        self.assertEqual(diff["role_agreement"], 0.0)
        self.assertEqual(len(diff["key_role_errors"]), 1)
        self.assertEqual(diff["mismatches"][0]["actual_role"], "dimension")

    def test_save_artifacts_writes_versioned_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
        self.addCleanup(_cleanup, tmp)
        rule = semantic.rule_artifact([(_fake_table(), _fake_schema())])
        written = semantic.save_artifacts(tmp / "day", rule, rule, semantic.diff_artifacts(rule, rule))
        names = sorted(path.name for path in written)
        self.assertEqual(names, ["semantic_diff.json", "semantic_rule.json", "semantic_schema.json"])
        payload = json.loads((tmp / "day" / "semantic_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], semantic.ARTIFACT_VERSION)
        self.assertIn("prompt_version", payload)
        self.assertEqual(payload["tables"][0]["fields"][0]["source"], "rule")


def _chat_response(answer: dict) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        },
        ensure_ascii=False,
    )


ANSWER = {
    "semantic_schema": {
        "fields": [
            {
                "name": "销售额",
                "role": "metric",
                "unit": "元",
                "meaning": "成交金额",
                "confidence": 0.95,
                "relations": ["订单号→销售额"],
            }
        ]
    },
    "analysis_plan": {
        "questions": ["哪类最好卖"],
        "metrics": ["销售额"],
        "dimensions": [],
        "charts": [],
        "quality_focus": ["离群值"],
        "anomalies": [{"field": "销售额", "reason": "量级异常", "row_hint": "第 3 行"}],
    },
}


class AdapterTests(unittest.TestCase):
    def _sample(self) -> TableSample:
        return TableSample(
            name="t",
            source="t.csv",
            sheet="CSV",
            field_names=["销售额"],
            rows=[{"销售额": "100"}, {"销售额": "999999"}],
        )

    def test_describe_returns_llm_fields_and_cost(self):
        provider = LLMSemanticProvider(
            LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=1000.0, price_out_cny_per_mtok=1000.0),
            transport=lambda *args: (200, _chat_response(ANSWER)),
        )
        result = provider.describe(self._sample())
        self.assertEqual(result.source_kind, "llm")
        self.assertEqual(result.fields[0].role, "metric")
        self.assertEqual(result.fields[0].relations, ["订单号→销售额"])
        self.assertEqual(result.prompt_version, "analysis_plan_v3")
        self.assertAlmostEqual(result.cost_cny, 1.5, places=6)

    def test_batch_budget_stops_further_calls(self):
        provider = LLMSemanticProvider(
            LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=1000.0, price_out_cny_per_mtok=1000.0),
            transport=lambda *args: (200, _chat_response(ANSWER)),
            batch_budget_cny=0.5,
        )
        with self.assertRaises(CostLimitExceeded):
            provider.describe(self._sample())

    def test_missing_field_triggers_one_retry_with_feedback(self):
        incomplete = json.loads(json.dumps(ANSWER, ensure_ascii=False))
        incomplete["semantic_schema"]["fields"] = []  # model forgot the only field
        responses = [_chat_response(incomplete), _chat_response(ANSWER)]
        calls: list[int] = []

        def transport(url, headers, payload, timeout):
            calls.append(1)
            return 200, responses[min(len(calls) - 1, 1)]

        provider = LLMSemanticProvider(
            LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=1.0, price_out_cny_per_mtok=1.0),
            transport=transport,
        )
        result = provider.describe(self._sample())
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.fields[0].name, "销售额")

    def test_missing_field_twice_raises(self):
        incomplete = json.loads(json.dumps(ANSWER, ensure_ascii=False))
        incomplete["semantic_schema"]["fields"] = []
        provider = LLMSemanticProvider(
            LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=1.0, price_out_cny_per_mtok=1.0),
            transport=lambda *args: (200, _chat_response(incomplete)),
        )
        with self.assertRaises(Exception):
            provider.describe(self._sample())


class PipelineDualTrackTests(unittest.TestCase):
    def test_dual_track_writes_artifacts_without_changing_numbers(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"

        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        baseline = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        self.assertFalse((day / "semantic_schema.json").exists())

        provider = FakeProvider()
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(settings, target_date="2026-11-07", force=True, semantic_provider=provider)
        self.assertEqual(record.status, "success")
        self.assertEqual(provider.calls, 1)

        for name in ("semantic_rule.json", "semantic_schema.json", "semantic_diff.json"):
            self.assertTrue((day / name).is_file(), name)
        artifact = json.loads((day / "semantic_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["source_kind"], "llm")
        self.assertEqual(artifact["provider"], "fake")
        self.assertEqual(record.metadata["semantic_schema"]["source_kind"], "llm")

        after = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["kpis"], after["kpis"])
        self.assertEqual(baseline["target_totals"], after["target_totals"])

    def test_semantic_sampling_uses_all_raw_columns(self):
        import pandas as pd

        from insightflow.models import FieldSpec
        from insightflow.pipeline import _build_semantic_bundle

        frame = pd.DataFrame([{"日期": "2026-11-07", "销售额": "100", "登记机关": "杭州"}])
        table = SimpleNamespace(
            source=SimpleNamespace(relative_path="a.csv"),
            sheet_label="CSV",
            frame=frame,
        )
        schema = SimpleNamespace(
            fields={"revenue": FieldSpec(original="销售额", canonical="revenue", label="销售额", kind="metric")}
        )
        seen: list[list[str]] = []

        class RecordingProvider:
            name = "recording"

            def describe(self, sample):
                seen.append(list(sample.field_names))
                return ProviderTableResult(fields=[], prompt_version="x")

        record = SimpleNamespace(warnings=[])
        _build_semantic_bundle(RecordingProvider(), {"table_schemas": [(table, schema)]}, record, logging.getLogger("t"))
        self.assertEqual(seen, [["日期", "销售额", "登记机关"]])

    def test_provider_failure_falls_back_and_keeps_run_successful(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"

        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(
                settings, target_date="2026-11-07", force=True, semantic_provider=FakeProvider(fail=True)
            )
        self.assertEqual(record.status, "success")
        self.assertTrue(any("semantic_fallback" in warning for warning in record.warnings))

        artifact = json.loads((day / "semantic_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["source_kind"], "fallback")
        self.assertEqual(artifact["tables"][0]["source_kind"], "fallback")
        self.assertIn("provider down", artifact["tables"][0]["error"])
        diff = json.loads((day / "semantic_diff.json").read_text(encoding="utf-8"))
        self.assertEqual(diff["role_agreement"], 1.0)


class CliDegradeTests(unittest.TestCase):
    def test_semantic_llm_without_config_degrades_to_rules(self):
        from insightflow import cli

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        keys = ("INSIGHTFLOW_LLM_MODEL", "DEEPSEEK_API_KEY", "INSIGHTFLOW_LLM_PRICE_IN", "INSIGHTFLOW_LLM_PRICE_OUT")
        cleaned_env = {key: value for key, value in os.environ.items() if key not in keys}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, cleaned_env, clear=True), patch(
            "insightflow.llm.client.dotenv_candidates", return_value=[]
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["run", "--date", "2026-11-07", "--force", "--semantic", "llm", "--project-root", str(tmp)])
        self.assertEqual(code, 0)
        self.assertIn("降级", err.getvalue())
        self.assertTrue((tmp / "out" / "2026-11-07" / "metrics.json").is_file())
        self.assertFalse((tmp / "out" / "2026-11-07" / "semantic_schema.json").exists())


if __name__ == "__main__":
    unittest.main()
