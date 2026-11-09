"""LLM PoC: versioned output contract for the "semantic schema + plan" call.

The PoC never lets the model produce business numbers.  Numbers may only appear
in structural/config places (chart layout, confidence); any metric-like key with
a numeric value is rejected, and chart data slots must be empty so the local
engine fills them later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "analysis_plan_v3"

ALLOWED_ROLES = {"date", "dimension", "metric", "id", "text"}

#: Keys that must never carry a number produced by the model.
FORBIDDEN_NUMERIC_KEYS = {
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


class ContractError(ValueError):
    """Raised when model output cannot be trusted or parsed."""


@dataclass
class SemanticField:
    name: str
    role: str
    unit: str = ""
    meaning: str = ""
    confidence: float = 0.0
    relations: list[str] = field(default_factory=list)


@dataclass
class ChartSkeleton:
    """An ECharts option skeleton with empty data slots."""

    chart_id: str
    chart_type: str
    title: str
    x_field: str = ""
    y_field: str = ""
    series_field: str = ""
    note: str = ""
    option_skeleton: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalysisPlanDraft:
    questions: list[str]
    metrics: list[str]
    dimensions: list[str]
    charts: list[ChartSkeleton]
    quality_focus: list[str]
    anomalies: list[dict[str, str]] = field(default_factory=list)
    notes: str = ""


@dataclass
class LLMResult:
    fields: list[SemanticField]
    plan: AnalysisPlanDraft
    usage: dict[str, int]
    raw_text: str


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _check_no_business_numbers(node: Any, path: str = "$") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lower = str(key).lower()
            if lower in FORBIDDEN_NUMERIC_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
                raise ContractError(f"{path}.{key} 不允许出现业务数字（模型只做规划，不产出数值）")
            _check_no_business_numbers(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _check_no_business_numbers(item, f"{path}[{index}]")


def _check_empty_data_slots(option: dict[str, Any], chart_id: str) -> None:
    series = option.get("series")
    if isinstance(series, list):
        for item in series:
            if isinstance(item, dict) and item.get("data") not in (None, []):
                raise ContractError(f"图表 {chart_id} 的 series.data 必须留空，由本地填充")
    for axis in ("xAxis", "yAxis"):
        value = option.get(axis)
        if isinstance(value, dict) and value.get("data") not in (None, []):
            raise ContractError(f"图表 {chart_id} 的 {axis}.data 必须留空，由本地填充")


def _parse_field(item: dict[str, Any]) -> SemanticField:
    name = str(item.get("name") or "").strip()
    _require(name, "字段缺少 name")
    role = str(item.get("role") or "").strip().lower()
    _require(role in ALLOWED_ROLES, f"字段 {name} 的 role 非法：{role!r}")
    confidence = item.get("confidence", 0.0)
    _require(isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0, f"字段 {name} 的 confidence 越界")
    return SemanticField(
        name=name,
        role=role,
        unit=str(item.get("unit") or ""),
        meaning=str(item.get("meaning") or ""),
        confidence=float(confidence),
        relations=[str(value) for value in item.get("relations") or []],
    )


def _parse_chart(item: dict[str, Any], index: int) -> ChartSkeleton:
    chart_id = str(item.get("chart_id") or f"chart_{index + 1}")
    chart_type = str(item.get("chart_type") or "").strip()
    _require(chart_type, f"图表 {chart_id} 缺少 chart_type")
    title = str(item.get("title") or chart_type)
    option = item.get("option_skeleton") or {}
    _require(isinstance(option, dict), f"图表 {chart_id} 的 option_skeleton 必须是对象")
    _check_empty_data_slots(option, chart_id)
    return ChartSkeleton(
        chart_id=chart_id,
        chart_type=chart_type,
        title=title,
        x_field=str(item.get("x_field") or ""),
        y_field=str(item.get("y_field") or ""),
        series_field=str(item.get("series_field") or ""),
        note=str(item.get("note") or ""),
        option_skeleton=option,
    )


def check_no_business_numbers(node: Any) -> None:
    """Public wrapper used by both the semantic and the plan contract."""
    _check_no_business_numbers(node)


def parse_result(text: str, usage: dict[str, int] | None = None) -> LLMResult:
    """Parse and validate the model's JSON answer; raise ``ContractError`` otherwise."""
    try:
        data = json.loads(text, strict=False)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError(f"输出不是合法 JSON：{exc}") from exc
    _require(isinstance(data, dict), "输出顶层必须是对象")
    _check_no_business_numbers(data)

    schema = data.get("semantic_schema")
    plan = data.get("analysis_plan")
    _require(isinstance(schema, dict) and isinstance(schema.get("fields"), list), "缺少 semantic_schema.fields")
    _require(isinstance(plan, dict), "缺少 analysis_plan")

    fields = [_parse_field(item) for item in schema["fields"] if isinstance(item, dict)]
    _require(fields, "semantic_schema.fields 为空")

    charts_raw = plan.get("charts") or []
    _require(isinstance(charts_raw, list), "analysis_plan.charts 必须是数组")
    charts = [_parse_chart(item, index) for index, item in enumerate(charts_raw) if isinstance(item, dict)]

    for key in ("questions", "metrics", "dimensions", "quality_focus"):
        value = plan.get(key, [])
        _require(isinstance(value, list), f"analysis_plan.{key} 必须是数组")

    anomalies_raw = plan.get("anomalies") or []
    _require(isinstance(anomalies_raw, list), "analysis_plan.anomalies 必须是数组")
    anomalies: list[dict[str, str]] = []
    for item in anomalies_raw:
        _require(isinstance(item, dict), "analysis_plan.anomalies 的元素必须是对象")
        if not all(isinstance(value, str) for value in item.values()):
            raise ContractError("anomalies 只能包含文字说明（字段名/原因/行提示），不能带业务数字")
        anomalies.append({str(key): str(value) for key, value in item.items()})

    draft = AnalysisPlanDraft(
        questions=[str(item) for item in plan.get("questions", [])],
        metrics=[str(item) for item in plan.get("metrics", [])],
        dimensions=[str(item) for item in plan.get("dimensions", [])],
        charts=charts,
        quality_focus=[str(item) for item in plan.get("quality_focus", [])],
        anomalies=anomalies,
        notes=str(plan.get("notes") or ""),
    )
    return LLMResult(fields=fields, plan=draft, usage=dict(usage or {}), raw_text=text)
