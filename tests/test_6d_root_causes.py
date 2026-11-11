"""Stage 6d task 3 root-cause fixes: units, plan scope, narrative facts, JSON."""

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
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.adoption import AdoptionDecision  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.llm.contracts import loads_llm_json  # noqa: E402
from insightflow.models import FieldSpec  # noqa: E402
from insightflow.narrative import (  # noqa: E402
    NarrativeDraft,
    UnknownReferenceError,
    UnsafeMarkupError,
    build_facts,
    is_safe_fact_value,
    normalize_fact_value,
    render_narrative,
    validate_narrative,
)
from insightflow.pipeline import execute  # noqa: E402
from insightflow.planning import PlanDraft, PlanResult  # noqa: E402
from insightflow.semantic import FieldSemantics, ProviderTableResult  # noqa: E402
from insightflow.semantic_adoption import (  # noqa: E402
    SemanticAdoptionError,
    adopt_semantic_fields,
    canonical_unit,
)


def _artifact(fields: list[FieldSemantics], table: str = "a.csv"):
    return SimpleNamespace(tables=[SimpleNamespace(table=table, fields=fields)])


def _schema(unit: str) -> tuple[SimpleNamespace, SimpleNamespace]:
    frame = pd.DataFrame({"销售额": [100.0], "备注": ["x"]})
    table = SimpleNamespace(source=SimpleNamespace(relative_path="a.csv"), frame=frame)
    schema = SimpleNamespace(
        fields={
            "revenue": FieldSpec(
                original="销售额", canonical="revenue", label="销售额", kind="metric", unit=unit
            )
        }
    )
    return table, schema


def _fields(unit: str) -> list[FieldSemantics]:
    return [
        FieldSemantics(name="销售额", role="metric", unit=unit),
        FieldSemantics(name="备注", role="text"),
    ]


class UnitCanonicalTests(unittest.TestCase):
    def test_synonyms(self):
        self.assertEqual(canonical_unit("万"), canonical_unit("万元"))
        self.assertEqual(canonical_unit("元"), canonical_unit("人民币元"))
        self.assertEqual(canonical_unit("％"), "%")

    def test_synonym_units_adopt(self):
        table, schema = _schema("万元")
        result = adopt_semantic_fields(_artifact(_fields("万")), [(table, schema)], {})
        self.assertIn("revenue", result.fields)

    def test_different_units_rejected(self):
        table, schema = _schema("元")
        with self.assertRaises(SemanticAdoptionError) as ctx:
            adopt_semantic_fields(_artifact(_fields("千克")), [(table, schema)], {})
        self.assertEqual(ctx.exception.reason, "unit_conflict")

    def test_unknown_units_fail_closed(self):
        table, schema = _schema("widgets")
        with self.assertRaises(SemanticAdoptionError) as ctx:
            adopt_semantic_fields(_artifact(_fields("gadgets")), [(table, schema)], {})
        self.assertEqual(ctx.exception.reason, "unit_conflict")

    def test_header_unit_hint_resolves_generic_rule_unit(self):
        frame = pd.DataFrame({"注册资本(万元)": [100.0], "备注": ["x"]})
        table = SimpleNamespace(source=SimpleNamespace(relative_path="a.csv"), frame=frame)
        schema = SimpleNamespace(
            fields={
                "registered_capital": FieldSpec(
                    original="注册资本(万元)",
                    canonical="registered_capital",
                    label="注册资本",
                    kind="metric",
                    unit="元",
                )
            }
        )
        fields = [
            FieldSemantics(name="注册资本(万元)", role="metric", unit="万元"),
            FieldSemantics(name="备注", role="text"),
        ]
        result = adopt_semantic_fields(_artifact(fields), [(table, schema)], {})
        self.assertEqual(result.fields["registered_capital"].unit, "万元")

    def test_dropped_column_dimension_is_ignored(self):
        table, schema = _schema("元")
        fields = [
            FieldSemantics(name="销售额", role="metric", unit="元"),
            FieldSemantics(name="备注", role="dimension"),
        ]
        result = adopt_semantic_fields(_artifact(fields), [(table, schema)], {})
        self.assertIn("revenue", result.fields)
        self.assertEqual(len(result.fields), 1)

    def test_dropped_column_metric_still_rejected(self):
        table, schema = _schema("元")
        fields = [
            FieldSemantics(name="销售额", role="metric", unit="元"),
            FieldSemantics(name="备注", role="metric"),
        ]
        with self.assertRaises(SemanticAdoptionError) as ctx:
            adopt_semantic_fields(_artifact(fields), [(table, schema)], {})
        self.assertEqual(ctx.exception.reason, "unknown_canonical")


class NarrationFactTests(unittest.TestCase):
    def test_normalize_fullwidth_and_spaces(self):
        self.assertEqual(normalize_fact_value("  １２３ 万元 "), "123 万元")

    def test_safe_values(self):
        self.assertTrue(is_safe_fact_value("12,345.67"))
        self.assertTrue(is_safe_fact_value("85.2%"))
        self.assertFalse(is_safe_fact_value("LLM 分析计划"))
        self.assertFalse(is_safe_fact_value("<script>"))

    def test_total_rows_alias(self):
        result = SimpleNamespace(target_date="2026-11-07", kpis=[], dimension_table=[])
        plan = SimpleNamespace(dataset_kind="交易", primary_dimension="")
        cleaning = SimpleNamespace(
            rows_out=42,
            quality_score=90.0,
            duplicates_removed=0,
            missing_filled={},
            outlier_counts={},
        )
        facts = build_facts(result, plan, cleaning, {})
        self.assertEqual(facts["total.rows"], "42")
        self.assertEqual(facts["total.records"], "42")


class JsonParsingTests(unittest.TestCase):
    def test_accepts_prose_wrapped_complete_object(self):
        data = loads_llm_json('前言 {"code": "x = 1"} 结尾')
        self.assertEqual(data["code"], "x = 1")

    def test_rejects_truncated_object(self):
        with self.assertRaises(json.JSONDecodeError):
            loads_llm_json('{"code": "x = 1')


class FakeSemantic:
    name = "fake"

    def describe(self, sample):
        fields = [
            FieldSemantics(name="订单日期", role="date"),
            FieldSemantics(name="区域", role="dimension"),
            FieldSemantics(name="销售额", role="metric", unit="元"),
            FieldSemantics(name="登记机关", role="text"),
        ]
        return ProviderTableResult(fields=fields, prompt_version="v3", cost_cny=0.001, latency_ms=5)


class CapturingPlan:
    name = "capture"
    prompt_version = "plan_v1"

    def __init__(self):
        self.seen: list[str] = []

    def plan(self, fields, anomalies, context):
        self.seen = [item.name for item in fields]
        return PlanResult(
            draft=PlanDraft(metrics=["销售额"], dimensions=["区域"], charts=[], quality_risks=[]),
            prompt_version="plan_v1",
            cost_cny=0.001,
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
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state"}', encoding="utf-8"
    )
    rows = ["订单日期,区域,销售额,登记机关"]
    for index in range(25):
        rows.append(f"2026-11-07,华东,{1000 + index},机关{index}")
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _decision(**modes) -> AdoptionDecision:
    base = {"semantic": "shadow", "plan": "shadow", "execute": "shadow", "narrative": "shadow"}
    base.update(modes)
    return AdoptionDecision(modes=base, reasons={}, force_rules=False)


class PlanScopeTests(unittest.TestCase):
    def test_dropped_column_not_offered_to_plan(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        plan_provider = CapturingPlan()
        with contextlib.redirect_stderr(io.StringIO()):
            execute(
                settings,
                target_date="2026-11-07",
                force=True,
                semantic_provider=FakeSemantic(),
                plan_provider=plan_provider,
                adoption_decision=_decision(semantic="gated", plan="gated"),
            )
        self.assertNotIn("登记机关", plan_provider.seen)
        self.assertIn("销售额", plan_provider.seen)


class NarrativeScopeTests(unittest.TestCase):
    def test_total_rows_placeholder_resolves(self):
        facts = {"total.rows": "42"}
        rendered = render_narrative(NarrativeDraft("x", "共 [[total.rows]] 条记录。", [], {}), facts)
        self.assertIn("42", rendered["summary"])

    def test_unsafe_value_still_rejected(self):
        with self.assertRaises(UnsafeMarkupError):
            render_narrative(
                NarrativeDraft("x", "计划：[[meta.dataset_kind]]", [], {}),
                {"meta.dataset_kind": "LLM 分析计划"},
            )

    def test_unknown_placeholder_still_rejected(self):
        with self.assertRaises(UnknownReferenceError):
            validate_narrative(
                NarrativeDraft("x", "未知 [[total.missing]]", [], {}),
                {"total.rows": "42"},
            )


if __name__ == "__main__":
    unittest.main()
