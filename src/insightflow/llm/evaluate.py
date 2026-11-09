"""Small evaluation helpers for the PoC report (no API calls here)."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping


def role_accuracy(fields: Iterable[Any], expected: Mapping[str, str]) -> dict[str, Any]:
    """Compare the model's field roles with a gold mapping (field name -> role)."""
    predicted = {field.name: field.role for field in fields}
    total = len(expected)
    correct = 0
    mismatches: list[dict[str, str]] = []
    for name, gold in expected.items():
        got = predicted.get(name, "<missing>")
        if got == gold:
            correct += 1
        else:
            mismatches.append({"field": name, "expected": gold, "got": got})
    extra = sorted(set(predicted) - set(expected))
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "mismatches": mismatches,
        "extra_fields": extra,
    }


def contains_business_numbers(text: str) -> bool:
    """Heuristic: does the raw answer carry metric-like numbers?

    Contract validation already rejects numbers under metric keys; this helper
    is the independent check used by the report (it must be 0 for every run).
    """
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return True
    forbidden = {
        "value",
        "total",
        "sum",
        "amount",
        "result",
        "kpi",
        "metric_value",
        "revenue",
        "orders",
        "cost",
        "profit",
        "margin",
        "count",
    }

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in forbidden and isinstance(value, (int, float)) and not isinstance(value, bool):
                    return True
                if walk(value):
                    return True
        elif isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(data)


def summarize_runs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-table PoC runs into the report's headline numbers."""
    if not rows:
        return {"runs": 0}
    costs = [float(row.get("cost_cny") or 0) for row in rows]
    latencies = [float(row.get("latency_ms") or 0) for row in rows]
    accuracies = [float(row.get("role_accuracy") or 0) for row in rows]
    parse_failures = sum(1 for row in rows if not row.get("parse_ok", False))
    fabricated = sum(1 for row in rows if row.get("business_numbers", False))
    return {
        "runs": len(rows),
        "cost_total_cny": round(sum(costs), 4),
        "cost_max_cny": round(max(costs), 4),
        "latency_avg_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "latency_max_ms": round(max(latencies)) if latencies else 0,
        "role_accuracy_avg": round(sum(accuracies) / len(accuracies), 3) if accuracies else 0.0,
        "parse_failures": parse_failures,
        "fabricated_number_runs": fabricated,
    }
