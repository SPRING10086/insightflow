"""Stage-2 plan contract: structure + anti-hallucination + empty data slots."""

from __future__ import annotations

import json
from typing import Any

from ..planning import PlanChart, PlanDraft, PlanValidationError, validate_plan_closure
from .contracts import ContractError, check_no_business_numbers

PLAN_PROMPT_VERSION = "plan_v1"


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _check_empty_slots(option: dict[str, Any], chart_id: str) -> None:
    series = option.get("series")
    if isinstance(series, list):
        for item in series:
            if isinstance(item, dict) and item.get("data") not in (None, []):
                raise ContractError(f"图表 {chart_id} 的 series.data 必须留空")
    for axis in ("xAxis", "yAxis"):
        value = option.get(axis)
        if isinstance(value, dict) and value.get("data") not in (None, []):
            raise ContractError(f"图表 {chart_id} 的 {axis}.data 必须留空")


def parse_plan(text: str, roles: dict[str, str]) -> PlanDraft:
    """Parse a model plan and enforce structure + field-closure against the schema."""
    try:
        data = json.loads(text, strict=False)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError(f"计划不是合法 JSON：{exc}") from exc
    _require(isinstance(data, dict), "计划顶层必须是对象")
    check_no_business_numbers(data)

    for key in ("questions", "metrics", "dimensions", "comparisons", "charts", "quality_risks"):
        _require(isinstance(data.get(key, []), list), f"计划字段 {key} 必须是数组")
    _require(data.get("quality_risks"), "quality_risks 不能为空")

    charts: list[PlanChart] = []
    for index, item in enumerate(data.get("charts") or []):
        _require(isinstance(item, dict), "charts 元素必须是对象")
        chart_id = str(item.get("chart_id") or f"chart_{index + 1}")
        option = item.get("option_skeleton") or {}
        _require(isinstance(option, dict), f"图表 {chart_id} 的 option_skeleton 必须是对象")
        _check_empty_slots(option, chart_id)
        charts.append(
            PlanChart(
                chart_id=chart_id,
                chart_type=str(item.get("chart_type") or ""),
                title=str(item.get("title") or ""),
                x_field=str(item.get("x_field") or ""),
                y_field=str(item.get("y_field") or ""),
                series_field=str(item.get("series_field") or ""),
                option_skeleton=option,
            )
        )

    draft = PlanDraft(
        questions=[str(item) for item in data.get("questions") or []],
        metrics=[str(item) for item in data.get("metrics") or []],
        dimensions=[str(item) for item in data.get("dimensions") or []],
        comparisons=[str(item) for item in data.get("comparisons") or []],
        charts=charts,
        quality_risks=[str(item) for item in data.get("quality_risks") or []],
        notes=str(data.get("notes") or ""),
    )
    try:
        validate_plan_closure(draft, roles)
    except PlanValidationError as exc:
        raise ContractError(f"字段引用校验失败：{exc}") from exc
    return draft
