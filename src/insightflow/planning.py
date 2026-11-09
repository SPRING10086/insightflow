"""Analysis-plan contracts shared by the rule track and the LLM track (stage 2).

Pure data + validation: no network, no pandas.  The compute layer may import
this module; only ``semantic_assembly.py`` may reference the LLM package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

PLAN_VERSION = "1.0"
ALLOWED_CHART_TYPES = {"bar", "line", "pie", "donut", "pareto", "scatter", "heatmap", "histogram", "dod"}


class PlanValidationError(ValueError):
    """The plan references unknown fields or aggregates a non-metric field."""


@dataclass
class PlanChart:
    chart_id: str
    chart_type: str
    title: str
    x_field: str = ""
    y_field: str = ""
    series_field: str = ""
    option_skeleton: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanDraft:
    questions: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    comparisons: list[str] = field(default_factory=list)
    charts: list[PlanChart] = field(default_factory=list)
    quality_risks: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class PlanResult:
    draft: PlanDraft
    prompt_version: str = ""
    cost_cny: float = 0.0
    latency_ms: int = 0
    source_kind: str = "llm"


@runtime_checkable
class PlanProvider(Protocol):
    name: str

    def plan(self, fields: list[Any], anomalies: list[dict[str, Any]], context: dict[str, Any]) -> PlanResult:
        ...


@dataclass
class PlanArtifact:
    version: str
    prompt_version: str
    generated_at: str
    provider: str
    source_kind: str  # rule | llm | fallback
    table: str
    plan: PlanDraft
    cost_cny: float = 0.0
    latency_ms: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.version,
            "prompt_version": self.prompt_version,
            "generated_at": self.generated_at,
            "provider": self.provider,
            "source_kind": self.source_kind,
            "table": self.table,
            "cost_cny": round(self.cost_cny, 6),
            "latency_ms": self.latency_ms,
            "error": self.error,
            "plan": {
                "questions": list(self.plan.questions),
                "metrics": list(self.plan.metrics),
                "dimensions": list(self.plan.dimensions),
                "comparisons": list(self.plan.comparisons),
                "charts": [
                    {
                        "chart_id": chart.chart_id,
                        "chart_type": chart.chart_type,
                        "title": chart.title,
                        "x_field": chart.x_field,
                        "y_field": chart.y_field,
                        "series_field": chart.series_field,
                        "option_skeleton": chart.option_skeleton,
                    }
                    for chart in self.plan.charts
                ],
                "quality_risks": list(self.plan.quality_risks),
                "notes": self.plan.notes,
            },
        }


def validate_plan_closure(draft: PlanDraft, roles: dict[str, str]) -> None:
    """Reject phantom fields and illegal aggregations (hard anti-hallucination gate)."""
    errors: list[str] = []
    if not draft.quality_risks:
        errors.append("quality_risks 不能为空（干净数据也要写\"未发现显著风险\"）")
    for name in draft.metrics:
        role = roles.get(name)
        if role is None:
            errors.append(f"指标引用了不存在的字段：{name}")
        elif role != "metric":
            errors.append(f"指标「{name}」的字段角色是 {role}，不能作为 metric 聚合")
    for name in draft.dimensions:
        role = roles.get(name)
        if role is None:
            errors.append(f"维度引用了不存在的字段：{name}")
        elif role != "dimension":
            errors.append(f"维度「{name}」的字段角色是 {role}，不能用于分组")
    for chart in draft.charts:
        if chart.chart_type not in ALLOWED_CHART_TYPES:
            errors.append(f"图表类型不支持：{chart.chart_type}")
        if chart.y_field:
            role = roles.get(chart.y_field)
            if role is None:
                errors.append(f"图表 {chart.chart_id} 的 y 轴引用不存在字段：{chart.y_field}")
            elif role != "metric":
                errors.append(f"图表 {chart.chart_id} 的 y 轴「{chart.y_field}」不是 metric")
        if chart.x_field:
            role = roles.get(chart.x_field)
            if role is None:
                errors.append(f"图表 {chart.chart_id} 的 x 轴引用不存在字段：{chart.x_field}")
            elif chart.chart_type in {"histogram", "scatter"}:
                # Distribution/scatter charts put a measure on x by design.
                if role != "metric":
                    errors.append(
                        f"图表 {chart.chart_id} 是 {chart.chart_type}，x 轴「{chart.x_field}」必须是 metric（role={role}）"
                    )
            elif role not in {"dimension", "date"}:
                errors.append(f"图表 {chart.chart_id} 的 x 轴「{chart.x_field}」不可分组（role={role}）")
        if chart.series_field:
            role = roles.get(chart.series_field)
            if role != "dimension":
                errors.append(f"图表 {chart.chart_id} 的分系列「{chart.series_field}」不是 dimension")
    if errors:
        raise PlanValidationError("；".join(errors))


def rule_plan_artifact(
    plan: Any,
    fields: dict[str, Any],
    cleaning: Any,
    settings: Any,
    *,
    generated_at: str | None = None,
) -> PlanArtifact:
    """Convert the existing rule ``AnalysisPlan`` into the neutral plan artifact."""
    original = {key: getattr(spec, "original", key) for key, spec in fields.items()}
    roles = {
        getattr(spec, "original", key): (spec.kind if spec.kind in {"dimension", "metric"} else "text")
        for key, spec in fields.items()
    }
    metrics = [original[key] for key in getattr(plan, "metrics", []) if key in original and roles.get(original[key]) == "metric"]
    dimensions = [original[key] for key in getattr(plan, "dimensions", []) if key in original and roles.get(original[key]) == "dimension"]

    primary_metric = original.get(getattr(plan, "primary_metric", "") or "", "")
    primary_dimension = original.get(getattr(plan, "primary_dimension", "") or "", "")
    charts = [
        PlanChart(
            chart_id=step.step_id,
            chart_type=step.chart_type,
            title=step.title,
            x_field=primary_dimension,
            y_field=primary_metric,
        )
        for step in getattr(plan, "steps", [])
        if getattr(step, "enabled", False) and getattr(step, "chart_type", "")
    ]

    risks: list[str] = []
    duplicates = int(getattr(cleaning, "duplicates_removed", 0) or 0)
    if duplicates:
        risks.append(f"存在完全重复行（已去重 {duplicates} 行）")
    if getattr(cleaning, "missing_filled", None):
        risks.append("存在缺失值填补，相关总量含估算成分")
    if any(int(value or 0) > 0 for value in (getattr(cleaning, "outlier_counts", {}) or {}).values()):
        risks.append("存在 IQR 离群值标记，未删除")
    if int(getattr(cleaning, "garbled_repaired", 0) or 0):
        risks.append("存在乱码/异常文本修复")
    if not risks:
        risks.append("未发现显著风险（无重复、无填补、无离群、无乱码）")

    questions = [step.title for step in getattr(plan, "steps", []) if getattr(step, "enabled", False)][:6]
    comparisons = ["前一日环比"] if getattr(getattr(settings, "comparison", None), "enabled", False) else []
    draft = PlanDraft(
        questions=questions,
        metrics=metrics,
        dimensions=dimensions,
        comparisons=comparisons,
        charts=charts,
        quality_risks=risks,
        notes="规则引擎生成的计划（对照组）",
    )
    return PlanArtifact(
        version=PLAN_VERSION,
        prompt_version="rules",
        generated_at=generated_at or datetime.now().isoformat(timespec="seconds"),
        provider="rules",
        source_kind="rule",
        table="dataset",
        plan=draft,
    )


def _chart_signature(chart: PlanChart) -> tuple[str, str, str, str]:
    return (chart.chart_type, chart.x_field, chart.y_field, chart.series_field)


def plan_diff(rule: PlanArtifact, other: PlanArtifact) -> dict[str, Any]:
    """Neutral comparison: sets, overlaps and per-item differences."""
    left, right = rule.plan, other.plan
    left_metrics, right_metrics = set(left.metrics), set(right.metrics)
    left_dims, right_dims = set(left.dimensions), set(right.dimensions)
    left_charts = {_chart_signature(chart) for chart in left.charts}
    right_charts = {_chart_signature(chart) for chart in right.charts}

    def jaccard(a: set, b: set) -> float:
        union = a | b
        return round(len(a & b) / len(union), 4) if union else 1.0

    return {
        "schema_version": PLAN_VERSION,
        "provider": other.provider,
        "source_kind": other.source_kind,
        "metrics": {
            "rule": sorted(left_metrics),
            "llm": sorted(right_metrics),
            "only_rule": sorted(left_metrics - right_metrics),
            "only_llm": sorted(right_metrics - left_metrics),
            "jaccard": jaccard(left_metrics, right_metrics),
        },
        "dimensions": {
            "rule": sorted(left_dims),
            "llm": sorted(right_dims),
            "only_rule": sorted(left_dims - right_dims),
            "only_llm": sorted(right_dims - left_dims),
            "jaccard": jaccard(left_dims, right_dims),
        },
        "charts": {
            "rule": sorted([list(item) for item in left_charts]),
            "llm": sorted([list(item) for item in right_charts]),
            "only_rule": sorted([list(item) for item in left_charts - right_charts]),
            "only_llm": sorted([list(item) for item in right_charts - left_charts]),
            "jaccard": jaccard(left_charts, right_charts),
        },
        "questions": {"rule_count": len(left.questions), "llm_count": len(right.questions)},
        "quality_risks": {
            "rule_count": len(left.quality_risks),
            "llm_count": len(right.quality_risks),
            "llm_items": list(right.quality_risks),
        },
    }


def save_plan_artifacts(
    day_dir: Path,
    rule: PlanArtifact,
    other: PlanArtifact,
    diff: dict[str, Any],
) -> list[Path]:
    import json

    day_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in (
        ("plan_rule.json", rule.to_dict()),
        ("plan_llm.json", other.to_dict()),
        ("plan_diff.json", diff),
    ):
        target = day_dir / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(target)
    return written
