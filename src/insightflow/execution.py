"""Stage-3 controlled execution: neutral contracts, rule baseline, reconcile.

Pure core module: no network, no sandbox import.  The LLM implementation lives
in ``llm/executor.py`` and is injected at the single assembly point.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

EXECUTION_VERSION = "1.0"
TOTAL_TOLERANCE = 1e-6
RATIO_TOLERANCE = 1e-4
GROUP_LIMIT = 50


@dataclass
class MetricRequest:
    metric: str
    label: str = ""
    aggregation: str = "SUM"


@dataclass
class ExecutionSpec:
    table: str
    metric: str  # original column name
    metric_label: str
    dimension: str = ""  # original column name
    dimension_label: str = ""
    aggregation: str = "SUM"  # SUM | AVG (percent/rate metrics are AVG)
    group_limit: int = GROUP_LIMIT
    metrics: list[MetricRequest] = field(default_factory=list)

    def requested_metrics(self) -> list[MetricRequest]:
        if self.metrics:
            return list(self.metrics)
        return [MetricRequest(self.metric, self.metric_label, self.aggregation)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "metric": self.metric,
            "metric_label": self.metric_label,
            "dimension": self.dimension,
            "dimension_label": self.dimension_label,
            "aggregation": self.aggregation,
            "group_limit": self.group_limit,
            "metrics": [
                {"metric": item.metric, "label": item.label, "aggregation": item.aggregation}
                for item in self.requested_metrics()
            ],
        }


@dataclass
class CodeArtifact:
    code: str
    prompt_version: str = ""
    cost_cny: float = 0.0
    latency_ms: int = 0


@dataclass
class ExecutionResult:
    metrics: dict[str, float] = field(default_factory=dict)
    groups: dict[str, Any] = field(default_factory=lambda: {"columns": [], "rows": []})
    metric_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_kind: str = "llm"
    memory_guard: str = "job"
    error: str = ""
    cost_cny: float = 0.0
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "memory_guard": self.memory_guard,
            "error": self.error,
            "cost_cny": round(self.cost_cny, 6),
            "latency_ms": self.latency_ms,
            "metrics": dict(self.metrics),
            "groups": self.groups,
            "metric_groups": dict(self.metric_groups),
        }


@runtime_checkable
class ExecutionProvider(Protocol):
    name: str

    def execute(
        self,
        spec: ExecutionSpec,
        frame: Any,
        allowed_columns: list[str],
        fields: list[Any] | None = None,
    ) -> ExecutionResult:
        ...


def _aggregate(series: Any, aggregation: str) -> float:
    return float(series.mean()) if str(aggregation).upper() == "AVG" else float(series.sum())


def _groups_for(frame: Any, spec: ExecutionSpec, metric: str, aggregation: str) -> dict[str, Any]:
    if not spec.dimension or spec.dimension not in frame.columns:
        return {"columns": [], "rows": []}
    grouped = frame.groupby(spec.dimension, dropna=False)[metric]
    grouped = grouped.mean() if str(aggregation).upper() == "AVG" else grouped.sum()
    grouped = grouped.sort_values(ascending=False).head(spec.group_limit)
    return {"columns": [spec.dimension], "rows": [[str(key), float(value)] for key, value in grouped.items()]}


def rule_execute(spec: ExecutionSpec, frame: Any) -> ExecutionResult:
    """Deterministic baseline computed on the same cleaned frame."""
    requests = spec.requested_metrics()
    for request in requests:
        if request.metric not in frame.columns:
            raise KeyError(f"metric column missing: {request.metric}")
    metrics: dict[str, float] = {}
    metric_groups: dict[str, dict[str, Any]] = {}
    for request in requests:
        metrics[request.metric] = _aggregate(frame[request.metric], request.aggregation)
        metric_groups[request.metric] = _groups_for(frame, spec, request.metric, request.aggregation)
    if not spec.metrics and len(requests) == 1:
        # Legacy single-metric contract used the neutral "total" key.
        return ExecutionResult(
            metrics={"total": metrics[requests[0].metric]},
            groups=metric_groups[requests[0].metric],
            source_kind="rule",
            memory_guard="none",
        )
    return ExecutionResult(
        metrics=metrics,
        groups=metric_groups.get(spec.metric, {"columns": [], "rows": []}),
        metric_groups=metric_groups,
        source_kind="rule",
        memory_guard="none",
    )


def _numbers_close(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= max(tolerance, tolerance * abs(right))


def _compare_groups(
    metric: str,
    rule_groups: dict[str, Any],
    llm_groups: dict[str, Any],
    problems: list[str],
) -> None:
    rule_keys = {str(row[0]) for row in rule_groups.get("rows", [])}
    llm_keys = {str(row[0]) for row in llm_groups.get("rows", [])}
    if rule_keys != llm_keys:
        missing = sorted(rule_keys - llm_keys)
        extra = sorted(llm_keys - rule_keys)
        problems.append(f"{metric}: group keys mismatch: missing={missing[:5]} extra={extra[:5]}")
    rule_values = {str(row[0]): row[1] for row in rule_groups.get("rows", [])}
    llm_values = {str(row[0]): row[1] for row in llm_groups.get("rows", [])}
    for key in sorted(rule_keys & llm_keys):
        try:
            left = float(llm_values[key])
            right = float(rule_values[key])
        except (TypeError, ValueError):
            problems.append(f"{metric}: non-numeric group value at {key!r}")
            continue
        if not (math.isfinite(left) and math.isfinite(right)):
            problems.append(f"{metric}: non-finite group value at {key!r}")
            continue
        if not _numbers_close(left, right, RATIO_TOLERANCE):
            problems.append(
                f"{metric}: group value mismatch at {key!r}: rule={rule_values[key]} llm={llm_values[key]}"
            )


def _metric_groups(result: ExecutionResult, metric: str, multi: bool) -> dict[str, Any]:
    if result.metric_groups:
        return result.metric_groups.get(metric, {"columns": [], "rows": []})
    return result.groups if not multi else {"columns": [], "rows": []}


def reconcile(rule: ExecutionResult, other: ExecutionResult) -> dict[str, Any]:
    """All adopted metric totals and per-metric groups must match the rule baseline."""
    problems: list[str] = []
    legacy = (
        not rule.metric_groups
        and not other.metric_groups
        and set(rule.metrics) == {"total"}
        and set(other.metrics) == {"total"}
    )
    if legacy:
        metrics = ["total"]
    else:
        metrics = sorted(set(rule.metrics) | set(other.metrics))
    detail: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        rule_total = rule.metrics.get(metric)
        llm_total = other.metrics.get(metric)
        entry = {"rule": rule_total, "llm": llm_total, "ok": True}
        if rule_total is None or llm_total is None:
            problems.append(f"{metric}: total missing on one side")
            entry["ok"] = False
        else:
            try:
                left = float(llm_total)
                right = float(rule_total)
            except (TypeError, ValueError):
                problems.append(f"{metric}: total is not numeric")
                entry["ok"] = False
            else:
                if not (math.isfinite(left) and math.isfinite(right)):
                    problems.append(f"{metric}: total is not finite")
                    entry["ok"] = False
                elif not _numbers_close(left, right, TOTAL_TOLERANCE):
                    problems.append(f"{metric}: total mismatch: rule={rule_total} llm={llm_total}")
                    entry["ok"] = False
                if entry["ok"]:
                    _compare_groups(
                        metric,
                        _metric_groups(rule, metric, not legacy),
                        _metric_groups(other, metric, not legacy),
                        problems,
                    )
        detail[metric] = entry

    accepted = not problems
    primary = "total" if legacy else (metrics[0] if metrics else None)
    return {
        "schema_version": EXECUTION_VERSION,
        "accepted": accepted,
        "problems": problems,
        "total_rule": rule.metrics.get(primary) if primary else None,
        "total_llm": other.metrics.get(primary) if primary else None,
        "metrics": detail,
        "metric_count": len(metrics),
        "group_keys_rule": len({row[0] for row in _metric_groups(rule, primary, not legacy).get("rows", [])})
        if primary
        else 0,
        "group_keys_llm": len({row[0] for row in _metric_groups(other, primary, not legacy).get("rows", [])})
        if primary
        else 0,
    }


def save_execution_artifacts(
    day_dir: Path,
    spec: ExecutionSpec,
    rule: ExecutionResult,
    code: CodeArtifact | None,
    result: ExecutionResult,
    diff: dict[str, Any],
) -> list[Path]:
    day_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().isoformat(timespec="seconds")
    payloads = (
        (
            "execution_rule.json",
            {
                "schema_version": EXECUTION_VERSION,
                "generated_at": generated_at,
                "spec": spec.to_dict(),
                "result": rule.to_dict(),
            },
        ),
        (
            "execution_code.json",
            {
                "schema_version": EXECUTION_VERSION,
                "generated_at": generated_at,
                "prompt_version": code.prompt_version if code else "",
                "cost_cny": round(code.cost_cny, 6) if code else 0.0,
                "latency_ms": code.latency_ms if code else 0,
                "code": code.code if code else "",
            },
        ),
        (
            "execution_llm.json",
            {
                "schema_version": EXECUTION_VERSION,
                "generated_at": generated_at,
                "spec": spec.to_dict(),
                "result": result.to_dict(),
            },
        ),
        ("execution_diff.json", diff),
    )
    written: list[Path] = []
    for name, payload in payloads:
        target = day_dir / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(target)
    return written
