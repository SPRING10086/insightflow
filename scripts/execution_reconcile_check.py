"""Independent recompute for 6d task 1 (full-metric reconcile (a)).

Builds a frame, computes every metric with plain pandas, compares against
rule_execute, then tampers each metric/group and asserts reconcile rejects it.
Writes docs/llm-stage6-6d-task1-check.json.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.execution import (  # noqa: E402
    ExecutionResult,
    ExecutionSpec,
    MetricRequest,
    reconcile,
    rule_execute,
)


def main() -> int:
    frame = pd.DataFrame(
        {
            "region": ["A", "A", "B", "C"],
            "revenue": [100.0, 200.0, 300.0, 400.5],
            "orders": [1.0, 2.0, 3.0, 4.0],
        }
    )
    spec = ExecutionSpec(
        table="dataset",
        metric="revenue",
        metric_label="销售额",
        dimension="region",
        metrics=[
            MetricRequest("revenue", "销售额", "SUM"),
            MetricRequest("orders", "订单量", "SUM"),
        ],
    )
    rule = rule_execute(spec, frame)
    expected = {
        "revenue": float(frame["revenue"].sum()),
        "orders": float(frame["orders"].sum()),
    }
    independent = {
        "expected_totals": expected,
        "rule_totals": rule.metrics,
        "totals_match": all(abs(rule.metrics[key] - value) <= 1e-9 for key, value in expected.items()),
        "expected_groups": {
            "revenue": {str(k): float(v) for k, v in frame.groupby("region")["revenue"].sum().items()},
            "orders": {str(k): float(v) for k, v in frame.groupby("region")["orders"].sum().items()},
        },
        "rule_groups": {
            key: {row[0]: row[1] for row in groups["rows"]} for key, groups in rule.metric_groups.items()
        },
        "groups_match": all(
            {row[0]: row[1] for row in rule.metric_groups[key]["rows"]
             } == {str(k): float(v) for k, v in frame.groupby("region")[key].sum().items()}
            for key in ("revenue", "orders")
        ),
    }

    def llm() -> ExecutionResult:
        return ExecutionResult(
            metrics=dict(rule.metrics),
            groups=copy.deepcopy(rule.groups),
            metric_groups=copy.deepcopy(rule.metric_groups),
            source_kind="llm",
        )

    cases: dict[str, bool] = {}
    cases["all_equal"] = reconcile(rule, llm())["accepted"]
    for metric in ("revenue", "orders"):
        broken = llm()
        broken.metrics[metric] += 1
        cases[f"total_tamper_{metric}"] = not reconcile(rule, broken)["accepted"]
        broken = llm()
        broken.metric_groups[metric]["rows"][0][1] += 1
        cases[f"group_value_tamper_{metric}"] = not reconcile(rule, broken)["accepted"]
        broken = llm()
        broken.metric_groups[metric]["rows"][0][0] = "Z"
        cases[f"group_key_tamper_{metric}"] = not reconcile(rule, broken)["accepted"]

    payload = {
        "spec": spec.to_dict(),
        "independent": independent,
        "tamper_cases": cases,
        "all_checks_pass": independent["totals_match"]
        and independent["groups_match"]
        and all(cases.values()),
    }
    out = ROOT / "docs" / "llm-stage6-6d-task1-check.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["all_checks_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
