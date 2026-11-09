from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.llm.client import CostLimitExceeded, LLMConfig  # noqa: E402
from insightflow.llm.contracts import ContractError  # noqa: E402
from insightflow.llm.plan_adapter import LLMPlanProvider  # noqa: E402
from insightflow.semantic import FieldSemantics  # noqa: E402

FIELDS = [
    FieldSemantics(name="日期", role="date", source="llm"),
    FieldSemantics(name="区域", role="dimension", source="llm"),
    FieldSemantics(name="销售额", role="metric", unit="元", source="llm"),
    FieldSemantics(name="毛利率", role="metric", source="llm"),
]

PLAN = {
    "questions": ["哪个区域最好？"],
    "metrics": ["销售额"],
    "dimensions": ["区域"],
    "comparisons": [],
    "charts": [
        {
            "chart_id": "c1",
            "chart_type": "bar",
            "title": "区域销售额",
            "x_field": "区域",
            "y_field": "销售额",
            "option_skeleton": {"xAxis": {"data": []}, "series": [{"data": []}]},
        }
    ],
    "quality_risks": ["未发现显著风险"],
}


def _response(plan: dict) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        },
        ensure_ascii=False,
    )


def _config(**overrides) -> LLMConfig:
    base = dict(model="m", api_key="k", price_in_cny_per_mtok=1000.0, price_out_cny_per_mtok=1000.0)
    base.update(overrides)
    return LLMConfig(**base)


class PlanAdapterTests(unittest.TestCase):
    def test_success(self):
        provider = LLMPlanProvider(_config(), transport=lambda *args: (200, _response(PLAN)))
        result = provider.plan(FIELDS, [{"field": "销售额", "reason": "离群"}], {"table_count": 2})
        self.assertEqual(result.draft.metrics, ["销售额"])
        self.assertEqual(result.prompt_version, "plan_v1")
        self.assertAlmostEqual(result.cost_cny, 1.5, places=6)

    def test_invalid_then_valid_retries(self):
        calls: list[int] = []
        bad = dict(PLAN, metrics=["区域"])  # dimension used as metric -> contract failure

        def transport(url, headers, payload, timeout):
            calls.append(1)
            return 200, _response(bad if len(calls) < 3 else PLAN)

        provider = LLMPlanProvider(_config(), transport=transport)
        result = provider.plan(FIELDS, [], {"table_count": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.draft.metrics, ["销售额"])

    def test_always_invalid_raises(self):
        bad = dict(PLAN, metrics=["区域"])
        provider = LLMPlanProvider(_config(), transport=lambda *args: (200, _response(bad)))
        with self.assertRaises(ContractError):
            provider.plan(FIELDS, [], {"table_count": 1})

    def test_budget_trips(self):
        provider = LLMPlanProvider(
            _config(),
            transport=lambda *args: (200, _response(PLAN)),
            batch_budget_cny=0.5,
        )
        with self.assertRaises(CostLimitExceeded):
            provider.plan(FIELDS, [], {"table_count": 1})


if __name__ == "__main__":
    unittest.main()
