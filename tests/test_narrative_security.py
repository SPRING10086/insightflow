"""Stage-4 narrative security tests: written before implementation (red first)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.narrative import (  # noqa: E402
    BareNumberError,
    NarrativeDraft,
    PlaceholderSyntaxError,
    UnknownReferenceError,
    UnsafeMarkupError,
    build_facts,
    render_narrative,
    validate_narrative,
)

FACTS = {
    "meta.date": "2026-11-07",
    "meta.rows": "14",
    "total.records": "14",
    "kpi.revenue.value": "125,200 元",
    "kpi.revenue.unit": "元",
    "kpi.revenue.delta_pct": "+12.5%",
    "kpi.revenue.label": "销售额",
    "quality.score": "96.5",
    "quality.duplicates": "0",
    "topn.region.first": "华东",
    "chart.c1.title": "区域销售额",
}
CHARTS = ["c1"]


def draft(**overrides) -> NarrativeDraft:
    base = dict(
        title="本期经营概览",
        summary="[[kpi.revenue.label]] 与 [[topn.region.first]] 表现值得关注。",
        key_points=["整体记录 [[total.records]] 条。"],
        chart_notes={"c1": "[[chart.c1.title]] 显示头部集中。"},
    )
    base.update(overrides)
    return NarrativeDraft(**base)


class BareNumberTests(unittest.TestCase):
    def test_rejects_arabic_digits_and_percent(self):
        for text in ("增长 5%", "达到 1200 元", "占比 30％", "第3名"):
            with self.subTest(text=text):
                with self.assertRaises(BareNumberError):
                    validate_narrative(draft(summary=text), FACTS, CHARTS)

    def test_rejects_chinese_quantifiers(self):
        for text in ("增长百分之五十", "提升三成", "翻两倍", "利润翻一番"):
            with self.subTest(text=text):
                with self.assertRaises(BareNumberError):
                    validate_narrative(draft(summary=text), FACTS, CHARTS)

    def test_allows_plain_chinese(self):
        validate_narrative(draft(summary="整体呈现一个向好的趋势。"), FACTS, CHARTS)


class PlaceholderTests(unittest.TestCase):
    def test_valid_placeholders_pass(self):
        validate_narrative(draft(), FACTS, CHARTS)

    def test_unknown_reference(self):
        with self.assertRaises(UnknownReferenceError):
            validate_narrative(draft(summary="见 [[kpi.ghost.value]]。"), FACTS, CHARTS)

    def test_privilege_escalation_reference(self):
        with self.assertRaises(UnknownReferenceError):
            validate_narrative(draft(summary="见 [[raw.customer_phone]]。"), FACTS, CHARTS)

    def test_syntax_errors(self):
        for text in ("未闭合 [[kpi.revenue.value]", "嵌套 [[kpi.[[x]]]]", "空引用 [[]]", "带空格 [[ kpi.revenue.value ]]"):
            with self.subTest(text=text):
                with self.assertRaises(PlaceholderSyntaxError):
                    validate_narrative(draft(summary=text), FACTS, CHARTS)

    def test_unknown_chart_id(self):
        with self.assertRaises(UnknownReferenceError):
            validate_narrative(draft(chart_notes={"ghost": "编造图表"}), FACTS, CHARTS)

    def test_unicode_and_hyphen_keys(self):
        facts = dict(FACTS)
        facts["kpi.订单-数.value"] = "10"
        validate_narrative(draft(summary="见 [[kpi.订单-数.value]]。"), facts, CHARTS)


class XssTests(unittest.TestCase):
    PAYLOADS = [
        "<script>alert(1)</script>",
        '" onmouseover=alert(1) x="',
        "javascript:alert(1)",
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "<img src=x onerror=alert(1)>",
    ]

    def test_five_payloads_rejected(self):
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                with self.assertRaises(UnsafeMarkupError):
                    validate_narrative(draft(summary=payload), FACTS, CHARTS)

    def test_titles_and_points_are_checked_too(self):
        with self.assertRaises(UnsafeMarkupError):
            validate_narrative(draft(title="<b>x</b>"), FACTS, CHARTS)
        with self.assertRaises(UnsafeMarkupError):
            validate_narrative(draft(key_points=["<script>x</script>"]), FACTS, CHARTS)


class RenderTests(unittest.TestCase):
    def test_renders_values_and_escapes_literals(self):
        rendered = render_narrative(
            NarrativeDraft(title="概览", summary="销售额为 [[kpi.revenue.value]]，区域 [[topn.region.first]]。", key_points=[], chart_notes={}),
            FACTS,
        )
        self.assertIn("125,200 元", rendered["summary"])
        self.assertIn("华东", rendered["summary"])
        self.assertNotIn("[[", rendered["summary"])

    def test_literal_specials_are_escaped(self):
        rendered = render_narrative(
            NarrativeDraft(title="概览", summary="A & B 的对比，括号（示例）。", key_points=[], chart_notes={}),
            FACTS,
        )
        self.assertIn("&amp;", rendered["summary"])
        self.assertNotIn("<", rendered["summary"])

    def test_unsafe_fact_value_is_rejected(self):
        facts = dict(FACTS)
        facts["kpi.revenue.value"] = "<script>alert(1)</script>"
        with self.assertRaises(UnsafeMarkupError):
            render_narrative(draft(summary="[[kpi.revenue.value]]"), facts)


class FactMapTests(unittest.TestCase):
    def test_build_facts_exposes_whitelisted_keys_only(self):
        kpi = SimpleNamespace(key="revenue", label="销售额", formatted="125,200 元", unit="元", delta_text="+12.5%")
        chart = SimpleNamespace(chart_id="c1", title="区域销售额")
        result = SimpleNamespace(target_date="2026-11-07", kpis=[kpi], charts=[chart], dimension_table=[])
        plan = SimpleNamespace(dataset_kind="交易流水")
        cleaning = SimpleNamespace(
            rows_out=14,
            quality_score=96.5,
            duplicates_removed=0,
            missing_filled={},
            outlier_counts={},
        )
        fields = {}
        facts = build_facts(result, plan, cleaning, fields)
        self.assertEqual(facts["kpi.revenue.value"], "125,200 元")
        self.assertEqual(facts["kpi.revenue.unit"], "元")
        self.assertIn("total.records", facts)
        self.assertIn("quality.score", facts)
        self.assertNotIn("raw.customer_phone", facts)


if __name__ == "__main__":
    unittest.main()
