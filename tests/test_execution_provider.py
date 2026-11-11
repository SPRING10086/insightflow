from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.execution import ExecutionSpec, MetricRequest  # noqa: E402
from insightflow.llm.client import CostLimitExceeded, LLMConfig  # noqa: E402
from insightflow.llm.executor import LLMExecutionProvider  # noqa: E402
from insightflow.llm.sandbox import SandboxViolation  # noqa: E402

FRAME = pd.DataFrame({"region": ["A", "A", "B"], "revenue": [100.0, 200.0, 300.0]})
SPEC = ExecutionSpec(table="dataset", metric="revenue", metric_label="销售额", dimension="region", dimension_label="区域")
MULTI_SPEC = ExecutionSpec(
    table="dataset",
    metric="revenue",
    metric_label="销售额",
    dimension="region",
    dimension_label="区域",
    metrics=[MetricRequest("revenue", "销售额", "SUM")],
)
MULTI_FRAME = FRAME.assign(orders=[1.0, 2.0, 3.0])
MULTI_SPEC.metrics.append(MetricRequest("orders", "订单量", "SUM"))
MULTI_CODE = (
    "metrics = {'revenue': float(df['revenue'].sum()), 'orders': float(df['orders'].sum())}\n"
    "g1 = df.groupby('region')['revenue'].sum()\n"
    "g2 = df.groupby('region')['orders'].sum()\n"
    "metric_groups = {'revenue': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g1.items()]},"
    " 'orders': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g2.items()]}}\n"
    "result = {'metrics': metrics, 'metric_groups': metric_groups, 'groups': metric_groups['revenue']}"
)

GOOD = (
    "g = df.groupby('region')['revenue'].sum()\n"
    "result = {'metrics': {'total': float(df['revenue'].sum())},"
    " 'groups': {'columns': ['region'], 'rows': [[str(k), float(v)] for k, v in g.items()]}}"
)
BAD = "d = df.assign(v=df['revenue'])\nresult = {'metrics': {}, 'groups': {'columns': [], 'rows': []}}"


def _response(code: str) -> str:
    content = json.dumps({"code": code, "notes": "ok"}, ensure_ascii=False)
    return json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300},
        },
        ensure_ascii=False,
    )


def _config() -> LLMConfig:
    return LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=1.0, price_out_cny_per_mtok=1.0)


class ExecutionProviderTests(unittest.TestCase):
    def test_generate_then_run(self):
        provider = LLMExecutionProvider(_config(), transport=lambda *args: (200, _response(GOOD)), memory_mb=2048)
        result = provider.execute(SPEC, FRAME, ["region", "revenue"], [])
        self.assertAlmostEqual(result.metrics["total"], 600.0)
        self.assertEqual(result.groups["columns"], ["region"])
        self.assertEqual(result.source_kind, "llm")
        self.assertGreater(result.cost_cny, 0)

    def test_multi_metric_code_and_metric_groups(self):
        provider = LLMExecutionProvider(_config(), transport=lambda *args: (200, _response(MULTI_CODE)), memory_mb=2048)
        result = provider.execute(MULTI_SPEC, MULTI_FRAME, ["region", "revenue", "orders"], [])
        self.assertAlmostEqual(result.metrics["revenue"], 600.0)
        self.assertAlmostEqual(result.metrics["orders"], 6.0)
        self.assertEqual(set(result.metric_groups), {"revenue", "orders"})
        self.assertEqual(result.groups["columns"], ["region"])

    def test_bad_code_retries_once_then_raises(self):
        calls: list[int] = []

        def transport(url, headers, payload, timeout):
            calls.append(1)
            return 200, _response(BAD)

        provider = LLMExecutionProvider(_config(), transport=transport, memory_mb=2048)
        with self.assertRaises(SandboxViolation):
            provider.execute(SPEC, FRAME, ["region", "revenue"], [])
        self.assertEqual(len(calls), 2)

    def test_budget_trips(self):
        provider = LLMExecutionProvider(
            LLMConfig(model="m", api_key="k", price_in_cny_per_mtok=5000.0, price_out_cny_per_mtok=5000.0),
            transport=lambda *args: (200, _response(GOOD)),
            batch_budget_cny=0.5,
        )
        with self.assertRaises(CostLimitExceeded):
            provider.execute(SPEC, FRAME, ["region", "revenue"], [])


if __name__ == "__main__":
    unittest.main()
