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
class ExecutionSpec:
    table: str
    metric: str  # original column name
    metric_label: str
    dimension: str = ""  # original column name
    dimension_label: str = ""
    aggregation: str = "SUM"  # SUM | AVG (percent/rate metrics are AVG)
    group_limit: int = GROUP_LIMIT


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


def rule_execute(spec: ExecutionSpec, frame: Any) -> ExecutionResult:
    """Deterministic baseline computed on the same cleaned frame."""
    if spec.metric not in frame.columns:
        raise KeyError(f"metric column missing: {spec.metric}")
    series = frame[spec.metric]
    if str(spec.aggregation).upper() == "AVG":
        total = float(series.mean())
    else:
        total = float(series.sum())
    metrics = {"total": total}
    groups: dict[str, Any] = {"columns": [], "rows": []}
    if spec.dimension and spec.dimension in frame.columns:
        grouped = frame.groupby(spec.dimension, dropna=False)[spec.metric]
        grouped = grouped.mean() if str(spec.aggregation).upper() == "AVG" else grouped.sum()
        grouped = grouped.sort_values(ascending=False).head(spec.group_limit)
        groups = {
            "columns": [spec.dimension],
            "rows": [[str(key), float(value)] for key, value in grouped.items()],
        }
    return ExecutionResult(metrics=metrics, groups=groups, source_kind="rule", memory_guard="none")


def _numbers_close(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= max(tolerance, tolerance * abs(right))


def reconcile(rule: ExecutionResult, other: ExecutionResult) -> dict[str, Any]:
    """The LLM numbers are only credible when they match the rule baseline."""
    problems: list[str] = []
    total_rule = rule.metrics.get("total")
    total_llm = other.metrics.get("total")
    if total_rule is None or total_llm is None:
        problems.append("total metric missing on one side")
    elif not _numbers_close(float(total_llm), float(total_rule), TOTAL_TOLERANCE):
        problems.append(f"total mismatch: rule={total_rule} llm={total_llm}")

    rule_keys = {row[0] for row in rule.groups.get("rows", [])}
    llm_keys = {row[0] for row in other.groups.get("rows", [])}
    if rule_keys != llm_keys:
        missing = sorted(rule_keys - llm_keys)
        extra = sorted(llm_keys - rule_keys)
        problems.append(f"group keys mismatch: missing={missing[:5]} extra={extra[:5]}")

    rule_values = {row[0]: row[1] for row in rule.groups.get("rows", [])}
    llm_values = {row[0]: row[1] for row in other.groups.get("rows", [])}
    for key in sorted(rule_keys & llm_keys):
        if not _numbers_close(float(llm_values[key]), float(rule_values[key]), RATIO_TOLERANCE):
            problems.append(f"group value mismatch at {key!r}: rule={rule_values[key]} llm={llm_values[key]}")

    return {
        "schema_version": EXECUTION_VERSION,
        "accepted": not problems,
        "problems": problems,
        "total_rule": total_rule,
        "total_llm": total_llm,
        "group_keys_rule": len(rule_keys),
        "group_keys_llm": len(llm_keys),
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
                "spec": spec.__dict__,
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
                "spec": spec.__dict__,
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
