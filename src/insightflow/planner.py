"""Rule-based analysis planning.

The plan is generated from the actual data shape, not from a fixed template.
Each candidate analysis is enabled or skipped with a human-readable reason,
and the selected chart budget is bounded by configuration.  This is the
"automatic judgement" layer of the pipeline.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd

from .config import Settings
from .models import AnalysisPlan, AnalysisStep, FieldSpec, MetricDefinition
from .schema import DatasetProfile, display_label, infer_value_format, profile_dataset
from .utils import now_local


def effective_fields(fields: dict[str, FieldSpec], target: pd.DataFrame) -> dict[str, FieldSpec]:
    """Refresh coverage/cardinality from the combined, cleaned target frame."""
    updated: dict[str, FieldSpec] = {}
    for key, field in fields.items():
        if key not in target.columns:
            continue
        series = target[key]
        if field.kind == "metric":
            coverage = float(pd.to_numeric(series, errors="coerce").notna().mean())
            unique = int(pd.to_numeric(series, errors="coerce").nunique(dropna=True))
        else:
            text = series.map(lambda value: str(value).strip())
            coverage = float((text != "").mean())
            unique = int(text[text != ""].nunique())
        updated[key] = replace(field, coverage=coverage, unique_count=unique)
    return updated


def build_plan(
    target: pd.DataFrame,
    baseline: pd.DataFrame,
    fields: dict[str, FieldSpec],
    target_date: date,
    baseline_date: date | None,
    settings: Settings,
) -> AnalysisPlan:
    fields = effective_fields(fields, target)
    profile: DatasetProfile = profile_dataset(target, fields, len(target))
    primary_metric = profile.primary_metric
    primary_dimension = profile.primary_dimension
    secondary_dimension = profile.secondary_dimension
    primary_field = fields.get(primary_metric) if primary_metric else None
    primary_is_additive = primary_field is None or primary_field.aggregation != "AVG"
    dimension_cardinalities = {
        key: int(target[key].astype(str).nunique()) for key in profile.dimensions if key in target.columns
    }
    metric_pairs = _eligible_metric_pairs(target, profile.metrics)

    chart_steps: list[AnalysisStep] = []
    steps: list[AnalysisStep] = []

    steps.append(
        AnalysisStep(
            step_id="kpi",
            title="核心指标总览",
            rationale="先给出规模、效率与可比变化，作为阅读全文的锚点。",
            priority=100,
        )
    )

    if primary_dimension:
        steps.append(
            AnalysisStep(
                step_id="dimension_rank",
                title="结构排名",
                rationale=f"按「{fields[primary_dimension].label}」拆解主指标，定位贡献最大的分组。",
                chart_type="bar",
                priority=95,
            )
        )
    else:
        steps.append(
            AnalysisStep(
                step_id="dimension_rank",
                title="结构排名",
                rationale="未识别到可用维度。",
                enabled=False,
                skip_reason="没有 2–50 个取值的分类维度。",
            )
        )

    hour_values = _distinct_hours(target)
    if len(hour_values) >= 3 and (primary_metric or primary_dimension):
        steps.append(
            AnalysisStep(
                step_id="hourly",
                title="分时走势",
                rationale=f"识别到时间字段，且覆盖 {len(hour_values)} 个不同小时，可观察日内节奏。",
                chart_type="line",
                priority=90,
            )
        )
    else:
        steps.append(
            AnalysisStep(
                step_id="hourly",
                title="分时走势",
                rationale="时间字段缺失或小时覆盖不足，无法形成可靠走势。",
                enabled=False,
                skip_reason="有效小时数少于 3 个。",
            )
        )

    if (
        baseline is not None
        and not baseline.empty
        and primary_metric
        and primary_metric in baseline.columns
    ):
        steps.append(
            AnalysisStep(
                step_id="dod",
                title="日环比对比",
                rationale=f"存在 {baseline_date.isoformat() if baseline_date else '前一日'} 基准数据，可比较规模与结构变化。",
                chart_type="dod",
                priority=88,
            )
        )
    else:
        steps.append(
            AnalysisStep(
                step_id="dod",
                title="日环比对比",
                rationale="没有可用的前一日基准数据，本期仅做单日截面分析。",
                enabled=False,
                skip_reason="前一日无数据或对比开关关闭。",
            )
        )

    if primary_dimension and dimension_cardinalities.get(primary_dimension, 0) <= 12:
        if primary_is_additive:
            steps.append(
                AnalysisStep(
                    step_id="share",
                    title="构成占比",
                    rationale="主维度取值不超过 12 个，适合用环形图展示贡献结构。",
                    chart_type="donut",
                    priority=82,
                )
            )
        else:
            steps.append(
                AnalysisStep(
                    step_id="share",
                    title="构成占比",
                    rationale="主指标是率/均值，不能把各分组均值相加成占比。",
                    enabled=False,
                    skip_reason="主指标不可相加（AVG/率），占比与累计贡献不适用。",
                )
            )
    else:
        steps.append(
            AnalysisStep(
                step_id="share",
                title="构成占比",
                rationale="主维度取值过多，占比图会碎片化。",
                enabled=False,
                skip_reason="主维度取值超过 12 个。",
            )
        )

    if primary_dimension and dimension_cardinalities.get(primary_dimension, 0) >= 4:
        if primary_is_additive:
            steps.append(
                AnalysisStep(
                    step_id="pareto",
                    title="集中度 / 帕累托",
                    rationale="维度至少有 4 个分组，可检验头部贡献是否接近 80/20。",
                    chart_type="pareto",
                    priority=78,
                )
            )
        else:
            steps.append(
                AnalysisStep(
                    step_id="pareto",
                    title="集中度 / 帕累托",
                    rationale="主指标是率/均值，累计贡献无法相加。",
                    enabled=False,
                    skip_reason="主指标不可相加（AVG/率），帕累托不适用。",
                )
            )
    else:
        steps.append(
            AnalysisStep(
                step_id="pareto",
                title="集中度 / 帕累托",
                rationale="分组过少，集中度结论不稳定。",
                enabled=False,
                skip_reason="主维度分组少于 4 个。",
            )
        )

    if (
        primary_dimension
        and secondary_dimension
        and 2 <= dimension_cardinalities.get(primary_dimension, 0) <= 10
        and 2 <= dimension_cardinalities.get(secondary_dimension, 0) <= 10
    ):
        steps.append(
            AnalysisStep(
                step_id="heatmap",
                title="双维交叉",
                rationale=(
                    f"「{fields[primary_dimension].label}」×「{fields[secondary_dimension].label}」"
                    "均具备可读的类别数，可观察结构差异。"
                ),
                chart_type="heatmap",
                priority=72,
            )
        )
    else:
        steps.append(
            AnalysisStep(
                step_id="heatmap",
                title="双维交叉",
                rationale="缺少两个类别数适中的维度。",
                enabled=False,
                skip_reason="需要两个各 2–10 个取值的维度。",
            )
        )

    if metric_pairs:
        left, right = metric_pairs[0]
        steps.append(
            AnalysisStep(
                step_id="correlation",
                title="指标相关性",
                rationale=(
                    f"「{fields[left].label}」与「{fields[right].label}」均有足够样本，"
                    "可计算 Pearson 相关系数并绘制散点图。"
                ),
                chart_type="scatter",
                priority=68,
            )
        )
    else:
        steps.append(
            AnalysisStep(
                step_id="correlation",
                title="指标相关性",
                rationale="有效指标对不足，无法计算可靠相关性。",
                enabled=False,
                skip_reason="至少需要两个各 10 个有效样本的数值指标。",
            )
        )

    if primary_metric and len(target) >= 8 and target[primary_metric].nunique(dropna=True) >= 3:
        steps.append(
            AnalysisStep(
                step_id="distribution",
                title="分布与异常",
                rationale="主指标取值足够分散，可观察分布形态、长尾和异常值。",
                chart_type="histogram",
                priority=64,
            )
        )
    else:
        steps.append(
            AnalysisStep(
                step_id="distribution",
                title="分布与异常",
                rationale="样本或取值过少，分布图信息量不足。",
                enabled=False,
                skip_reason="至少需要 8 行且主指标有 3 个不同取值。",
            )
        )

    _append_margin_step(steps, fields, target)
    _append_quality_step(steps, target)

    # Budget chart cards by priority, keeping non-chart steps in the plan.
    planned_charts = sorted(
        [step for step in steps if step.chart_type and step.enabled],
        key=lambda step: step.priority,
        reverse=True,
    )
    winners = {step.step_id for step in planned_charts[: settings.max_charts]}
    for step in steps:
        if step.chart_type and step.enabled and step.step_id not in winners:
            step.enabled = False
            step.skip_reason = f"图表数量达到上限 {settings.max_charts}，按优先级保留。"

    metric_definitions = _metric_definitions(fields, profile)
    reasoning = list(profile.reasoning)
    if baseline is not None and not baseline.empty:
        reasoning.append("检测到前一日数据，加入日环比与结构对比，所有变化均以两期同口径汇总计算。")
    else:
        reasoning.append("没有可用的前一日数据，报告不展示环比结论，避免无基准推断。")
    if hour_values:
        reasoning.append(f"时间字段覆盖 {len(hour_values)} 个小时，分时图仅描述日内分布，不代表跨日趋势。")
    reasoning.append(
        f"图表预算上限 {settings.max_charts} 个；优先级较低的候选分析会明确记录跳过原因。"
    )

    return AnalysisPlan(
        target_date=target_date.isoformat(),
        baseline_date=baseline_date.isoformat() if baseline_date and baseline is not None and not baseline.empty else None,
        dataset_kind=profile.dataset_kind,
        rows=len(target),
        column_count=len([column for column in target.columns if not column.startswith("__")]),
        metrics=profile.metrics,
        dimensions=profile.dimensions,
        primary_metric=primary_metric,
        primary_dimension=primary_dimension,
        secondary_dimension=secondary_dimension,
        time_column="__hour" if hour_values else None,
        steps=steps,
        metric_definitions=metric_definitions,
        reasoning=reasoning,
        generated_at=now_local(settings.timezone).isoformat(timespec="seconds"),
        profile={
            "fields": [
                {
                    "key": field.canonical,
                    "label": display_label(field),
                    "kind": field.kind,
                    "unit": field.unit,
                    "coverage": round(field.coverage * 100, 1),
                    "unique": field.unique_count,
                    "format": infer_value_format(field),
                }
                for field in sorted(
                    fields.values(),
                    key=lambda item: ({"metric": 0, "dimension": 1, "id": 2}.get(item.kind, 3), -item.priority),
                )
            ],
            "dimension_cardinalities": dimension_cardinalities,
            "hour_count": len(hour_values),
        },
    )


def _distinct_hours(frame: pd.DataFrame) -> list[int]:
    if "__hour" not in frame.columns:
        return []
    values = pd.to_numeric(frame["__hour"], errors="coerce").dropna().astype(int)
    return sorted(set(int(value) for value in values.tolist() if 0 <= int(value) <= 23))


def _eligible_metric_pairs(frame: pd.DataFrame, metrics: list[str]) -> list[tuple[str, str]]:
    # Mirror the analysis layer's preference: financial pairs are useful for
    # margin work but make a tautological scatter (revenue vs cost).
    non_financial = [metric for metric in metrics if metric not in {"cost", "profit"}]
    candidate_metrics = non_financial if len(non_financial) >= 2 else metrics
    candidates: list[tuple[float, str, str]] = []
    for index, left in enumerate(candidate_metrics):
        if left not in frame.columns:
            continue
        left_values = pd.to_numeric(frame[left], errors="coerce")
        if left_values.notna().sum() < 10 or left_values.nunique() < 2:
            continue
        for right in candidate_metrics[index + 1 :]:
            if right not in frame.columns:
                continue
            right_values = pd.to_numeric(frame[right], errors="coerce")
            valid = pd.concat([left_values, right_values], axis=1).dropna()
            if len(valid) < 10 or valid.iloc[:, 0].nunique() < 2 or valid.iloc[:, 1].nunique() < 2:
                continue
            correlation = valid.iloc[:, 0].corr(valid.iloc[:, 1])
            if pd.isna(correlation):
                continue
            candidates.append((abs(float(correlation)), left, right))
    candidates.sort(reverse=True)
    return [(left, right) for _, left, right in candidates]


def _append_margin_step(steps: list[AnalysisStep], fields: dict[str, FieldSpec], target: pd.DataFrame) -> None:
    if {"revenue", "cost"}.issubset(target.columns):
        revenue = pd.to_numeric(target["revenue"], errors="coerce").sum()
        if revenue:
            steps.append(
                AnalysisStep(
                    step_id="margin",
                    title="毛利结构",
                    rationale="同时识别到销售额与成本，可计算毛利率并按主维度比较。",
                    chart_type="bar",
                    priority=58,
                )
            )
            return
    steps.append(
        AnalysisStep(
            step_id="margin",
            title="毛利结构",
            rationale="缺少成本字段，无法计算毛利率。",
            enabled=False,
            skip_reason="需要同时存在销售额与成本。",
        )
    )


def _append_quality_step(steps: list[AnalysisStep], target: pd.DataFrame) -> None:
    steps.append(
        AnalysisStep(
            step_id="quality",
            title="数据质量体检",
            rationale="始终输出缺失、重复、异常与解析质量，避免只看结论不看口径。",
            chart_type="",
            priority=40,
        )
    )


def _metric_definitions(fields: dict[str, FieldSpec], profile: DatasetProfile) -> list[MetricDefinition]:
    definitions: list[MetricDefinition] = [
        MetricDefinition(
            key="__record",
            label="记录数",
            formula="COUNT(*)",
            unit="行",
            aggregation="COUNT",
            value_format="int",
            source_columns=[],
        )
    ]
    for key in profile.metrics:
        field = fields[key]
        definitions.append(
            MetricDefinition(
                key=key,
                label=display_label(field),
                formula=f"{field.aggregation}({field.label})",
                unit=field.unit,
                aggregation=field.aggregation,
                value_format=infer_value_format(field),
                source_columns=[field.original],
            )
        )
    if {"revenue", "orders"}.issubset(fields) and "revenue" in profile.metrics and "orders" in profile.metrics:
        definitions.append(
            MetricDefinition(
                key="aov",
                label="客单价",
                formula="SUM(销售额) ÷ SUM(订单量)",
                unit="元",
                aggregation="RATIO",
                value_format="money",
                derived=True,
                source_columns=["revenue", "orders"],
            )
        )
    if {"revenue", "cost"}.issubset(fields) and "revenue" in profile.metrics:
        definitions.append(
            MetricDefinition(
                key="gross_margin",
                label="毛利率",
                formula="(SUM(销售额) − SUM(成本)) ÷ SUM(销售额)",
                unit="%",
                aggregation="RATIO",
                value_format="percent",
                derived=True,
                source_columns=["revenue", "cost"],
            )
        )
    return definitions
