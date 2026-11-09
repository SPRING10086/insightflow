from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.llm.client import CostLimitExceeded, LLMConfig  # noqa: E402
from insightflow.llm.contracts import ContractError  # noqa: E402
from insightflow.llm.narrative_gen import LLMNarrativeProvider  # noqa: E402

DRAFT = {
    "title": "本期概览",
    "summary": "销售额为 [[kpi.revenue.value]]。",
    "key_points": ["区域 [[topn.region.first]] 领先。"],
    "chart_notes": {"chart_1": "读图提示"},
}


def _response(payload: dict) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
        },
        ensure_ascii=False,
    )


def _config(price: float = 1.0) -> LLMConfig:
    return LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=price, price_out_cny_per_mtok=price)


class NarrativeProviderTests(unittest.TestCase):
    def test_generate_returns_draft_and_masks_samples(self):
        seen: list[str] = []

        def transport(url, headers, payload, timeout):
            seen.append(json.dumps(payload, ensure_ascii=False))
            return 200, _response(DRAFT)

        provider = LLMNarrativeProvider(_config(), transport=transport)
        draft = provider.generate(
            ["kpi.revenue.value", "topn.region.first"],
            ["销售额"],
            ["chart_1"],
            [{"客户电话": "13812345678", "备注": "正常"}],
        )
        self.assertEqual(draft.title, "本期概览")
        self.assertEqual(draft.chart_notes, {"chart_1": "读图提示"})
        self.assertIn("chart_1", seen[0])
        self.assertNotIn("13812345678", seen[0])
        self.assertGreater(provider.last_meta["cost_cny"], 0)

    def test_invalid_json_raises(self):
        envelope = json.dumps(
            {
                "choices": [{"message": {"content": "not-json-at-all"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
        provider = LLMNarrativeProvider(_config(), transport=lambda *args: (200, envelope))
        with self.assertRaises(ContractError):
            provider.generate([], [], [], [])

    def test_missing_fields_raise(self):
        bad = {"title": "x", "summary": 1, "key_points": "no", "chart_notes": []}
        provider = LLMNarrativeProvider(_config(), transport=lambda *args: (200, _response(bad)))
        with self.assertRaises(ContractError):
            provider.generate([], [], [], [])

    def test_budget_trips(self):
        provider = LLMNarrativeProvider(
            _config(price=5000.0),
            transport=lambda *args: (200, _response(DRAFT)),
            batch_budget_cny=0.5,
        )
        with self.assertRaises(CostLimitExceeded):
            provider.generate([], [], [], [])


if __name__ == "__main__":
    unittest.main()
