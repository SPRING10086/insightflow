"""Deterministic analysis and chart specification generation.

No LLM is involved in the calculation path.  Metrics, rankings, Pareto
concentration, distributions, correlations and insights are all derived from
the cleaned dataframe with explicit formulas, so the same inputs produce the
same report.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .config import Settings
from .models import (
    AnalysisPlan,
    AnalysisResult,
    ChartSpec,
    CleaningReport,
    FieldSpec,
    Insight,
    KPI,
)
from .utils import (
    fmt_delta_pct,
    fmt_int,
    fmt_money,
    fmt_number,
    fmt_pct,
    now_local,
    pct_change,
    safe_divide,
)

_MONEY_METRICS = {"revenue", "cost", "profit", "price"}


def analyze(
    target: pd.DataFrame,
    baseline: pd.DataFrame,
    fields: dict[str, FieldSpec],
    plan: AnalysisPlan,
    cleaning: CleaningReport,
    settings: Settings,
) -> AnalysisResult:
    target_date = plan.target_date
    baseline_date = plan.baseline_date
    primary_metric = plan.primary_metric or "__record"
    primary_dimension = plan.primary_dimension
    primary_field = fields.get(primary_metric)

    totals = _totals(target, fields)
    baseline_totals = _totals(baseline, fields) if baseline is not None and not baseline.empty else {}
    dimension_table, dim_records = _dimension_table(target, primary_dimension, primary_metric, fields)
    secondary_tables = _secondary_tables(target, plan.dimensions, primary_metric, fields)
    hourly = _hourly_rows(target, primary_metric, fields)
    dod = _dod_data(target, baseline, primary_dimension, primary_metric, fields, plan)
    pareto = _pareto_data(dim_records)
    share = _share_data(dim_records)
    distribution = _distribution_data(target, primary_metric, fields)
    correlation = _correlation_data(target, plan.metrics, fields, primary_dimension)
    outliers = _outlier_rows(target, primary_metric, fields, primary_dimension)
    top_records = _top_record_rows(target, primary_metric, fields)
    margin_rows = _margin_rows(target, primary_dimension, fields)

    kpis = _build_kpis(totals, baseline_totals, fields, plan)
    insights = _build_insights(
        target=target,
        baseline=baseline,
        fields=fields,
        plan=plan,
        cleaning=cleaning,
        totals=totals,
        baseline_totals=baseline_totals,
        dim_records=dim_records,
        pareto=pareto,
        correlation=correlation,
        outliers=outliers,
        margin_rows=margin_rows,
    )

    charts = _build_charts(
        target=target,
        plan=plan,
        fields=fields,
        dim_records=dim_records,
        hourly=hourly,
        dod=dod,
        share=share,
        pareto=pareto,
        difference=distribution,
        correlation=correlation,
        margin_rows=margin_rows,
        settings=settings,
    )

    summary = {
        "rows": len(target),
        "metric_label": primary_field.label if primary_field else "记录数",
        "metric_value": totals.get("metrics", {}).get(primary_metric),
        "metric_formatted": _format_value(
            totals.get("metrics", {}).get(primary_metric),
            _value_type(primary_metric, fields),
        )
        if primary_metric != "__record"
        else fmt_int(len(target)),
        "top_group": dim_records[0]["name"] if dim_records else "",
        "top_group_value": dim_records[0]["value"] if dim_records else None,
        "top_group_share": dim_records[0]["share"] if dim_records else None,
        "top3_share": sum(row["share"] for row in dim_records[:3]) if dim_records else None,
        "outlier_count": len(outliers),
        "quality_score": cleaning.quality_score,
        "dimension_label": fields[primary_dimension].label if primary_dimension and primary_dimension in fields else "",
    }

    return AnalysisResult(
        target_date=target_date,
        baseline_date=baseline_date,
        plan=plan,
        kpis=kpis,
        insights=insights,
        charts=charts,
        dimension_table=dimension_table,
        secondary_tables=secondary_tables,
        outliers=outliers,
        correlation=correlation,
        hourly=hourly,
        dod=dod,
        top_records=top_records,
        target_totals=totals,
        baseline_totals=baseline_totals,
        quality=cleaning,
        summary=summary,
        generated_at=now_local(settings.timezone).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# Totals and KPI
# ---------------------------------------------------------------------------


def _totals(frame: pd.DataFrame, fields: dict[str, FieldSpec]) -> dict[str, Any]:
    metrics: dict[str, float] = {}
    if frame is not None and not frame.empty:
        for key, field in fields.items():
            if field.kind != "metric" or key not in frame.columns:
                continue
            series = pd.to_numeric(frame[key], errors="coerce")
            if field.aggregation == "AVG":
                value = series.mean()
            else:
                value = series.sum()
            if pd.notna(value):
                metrics[key] = float(value)
    record_count = len(frame) if frame is not None else 0
    revenue = metrics.get("revenue")
    orders = metrics.get("orders")
    cost = metrics.get("cost")
    aov = safe_divide(revenue, orders) if revenue is not None and orders else None
    gross_margin = safe_divide((revenue - cost), revenue) if revenue and cost is not None else None
    return {
        "record_count": record_count,
        "metrics": metrics,
        "aov": aov,
        "gross_margin": gross_margin,
    }


def _build_kpis(
    totals: dict[str, Any],
    baseline_totals: dict[str, Any],
    fields: dict[str, FieldSpec],
    plan: AnalysisPlan,
) -> list[KPI]:
    kpis: list[KPI] = []
    primary = plan.primary_metric
    baseline_label = "较前一日" if baseline_totals else "无基准"
    if primary:
        field = fields[primary]
        value = totals.get("metrics", {}).get(primary)
        previous = baseline_totals.get("metrics", {}).get(primary) if baseline_totals else None
        kpis.append(
            _kpi(
                key=primary,
                label=field.label,
                value=value,
                previous=previous,
                value_type=_value_type(primary, fields),
                unit=field.unit,
                hint=f"前一日 {_format_value(previous, _value_type(primary, fields))}"
                if previous is not None
                else "未找到前一日基准",
                delta_label=baseline_label,
            )
        )
        if primary != "orders" and "orders" in totals.get("metrics", {}):
            orders = totals["metrics"]["orders"]
            prev_orders = baseline_totals.get("metrics", {}).get("orders") if baseline_totals else None
            kpis.append(
                _kpi(
                    key="orders",
                    label="订单量",
                    value=orders,
                    previous=prev_orders,
                    value_type="int",
                    unit="单",
                    hint=f"前一日 {fmt_int(prev_orders)}" if prev_orders is not None else "未找到前一日基准",
                    delta_label=baseline_label,
                )
            )
        if totals.get("aov") is not None:
            kpis.append(
                _kpi(
                    key="aov",
                    label="客单价",
                    value=totals["aov"],
                    previous=baseline_totals.get("aov") if baseline_totals else None,
                    value_type="money",
                    unit="元",
                    hint="销售额 ÷ 订单量（聚合口径）",
                    delta_label=baseline_label,
                )
            )
        if totals.get("gross_margin") is not None:
            kpis.append(
                _kpi(
                    key="gross_margin",
                    label="毛利率",
                    value=totals["gross_margin"],
                    previous=baseline_totals.get("gross_margin") if baseline_totals else None,
                    value_type="percent",
                    unit="%",
                    hint="(销售额 − 成本) ÷ 销售额",
                    delta_label=baseline_label,
                    percentage_points=True,
                )
            )
        if "customers" in totals.get("metrics", {}) and len(kpis) < 5:
            customers = totals["metrics"]["customers"]
            prev = baseline_totals.get("metrics", {}).get("customers") if baseline_totals else None
            kpis.append(
                _kpi(
                    key="customers",
                    label="客户数",
                    value=customers,
                    previous=prev,
                    value_type="int",
                    unit="人",
                    hint="按日汇总，未去重跨日客户",
                    delta_label=baseline_label,
                )
            )
    if len(kpis) < 5:
        kpis.append(
            _kpi(
                key="records",
                label="有效记录",
                value=totals.get("record_count"),
                previous=baseline_totals.get("record_count") if baseline_totals else None,
                value_type="int",
                unit="行",
                hint="清洗后、去重后的记录数",
                delta_label=baseline_label,
            )
        )
    return kpis[:5]


def _kpi(
    key: str,
    label: str,
    value: float | None,
    previous: float | None,
    value_type: str,
    unit: str,
    hint: str,
    delta_label: str,
    percentage_points: bool = False,
) -> KPI:
    if percentage_points and value is not None and previous is not None:
        delta = (value - previous) * 100.0
        delta_text = f"{delta:+.1f} pp"
    else:
        delta = pct_change(value, previous)
        delta_text = fmt_delta_pct(delta)
    trend = "unknown"
    if delta is not None:
        trend = "up" if delta > 0.05 else "down" if delta < -0.05 else "flat"
    return KPI(
        key=key,
        label=label,
        value=value,
        formatted=_format_value(value, value_type),
        unit=unit,
        delta=delta,
        delta_text=delta_text,
        delta_label=delta_label,
        trend=trend,
        hint=hint,
        value_format=value_type,
    )


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def _dimension_table(
    frame: pd.DataFrame,
    dimension: str | None,
    metric: str,
    fields: dict[str, FieldSpec],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not dimension or dimension not in frame.columns or frame.empty:
        return [], []
    if metric != "__record" and metric not in frame.columns:
        return [], []
    working = frame.copy()
    working[dimension] = working[dimension].astype(str)
    grouped = working.groupby(dimension, dropna=False)
    is_average = metric != "__record" and metric in fields and fields[metric].aggregation == "AVG"
    if metric == "__record":
        value = grouped.size().astype(float).rename("value")
    elif is_average:
        value = (
            pd.to_numeric(working[metric], errors="coerce")
            .groupby(working[dimension])
            .mean()
            .rename("value")
        )
    else:
        value = (
            pd.to_numeric(working[metric], errors="coerce")
            .groupby(working[dimension])
            .sum(min_count=1)
            .rename("value")
        )
    records = grouped.size().rename("records")
    data = pd.concat([value, records], axis=1).reset_index()
    if metric == "orders":
        data["orders"] = data["value"]
    if "orders" in working.columns and metric != "orders":
        orders = grouped["orders"].sum(min_count=1).rename("orders")
        data = data.merge(orders.reset_index(), on=dimension, how="left")
    if "cost" in working.columns and "revenue" in working.columns:
        revenue = grouped["revenue"].sum(min_count=1).rename("revenue")
        cost = grouped["cost"].sum(min_count=1).rename("cost")
        data = data.merge(revenue.reset_index(), on=dimension, how="left")
        data = data.merge(cost.reset_index(), on=dimension, how="left")
    data["value"] = pd.to_numeric(data["value"], errors="coerce").fillna(0.0)
    data = data.sort_values(["value", dimension], ascending=[False, True]).reset_index(drop=True)
    if is_average:
        data["share"] = None
        data["cumulative_share"] = None
    else:
        total = float(data["value"].sum())
        data["share"] = data["value"].map(lambda value: value / total * 100 if total else 0.0)
        data["cumulative_share"] = data["share"].cumsum()
    data["rank"] = range(1, len(data) + 1)
    if "orders" in data.columns and not is_average:
        data["aov"] = data.apply(
            lambda row: safe_divide(row["value"], row["orders"]) if pd.notna(row.get("orders")) else None,
            axis=1,
        )
    if {"revenue", "cost"}.issubset(data.columns):
        data["margin"] = data.apply(
            lambda row: safe_divide(row["revenue"] - row["cost"], row["revenue"])
            if pd.notna(row.get("revenue")) and row.get("revenue")
            else None,
            axis=1,
        )

    records_list: list[dict[str, Any]] = []
    display: list[dict[str, Any]] = []
    metric_type = _value_type(metric, fields)
    for _, row in data.iterrows():
        record = {
            "rank": int(row["rank"]),
            "name": str(row[dimension]),
            "value": float(row["value"]),
            "share": float(row["share"]) if pd.notna(row.get("share")) else None,
            "cumulative_share": float(row["cumulative_share"]) if pd.notna(row.get("cumulative_share")) else None,
            "records": int(row["records"]),
            "orders": float(row["orders"]) if "orders" in data.columns and pd.notna(row.get("orders")) else None,
            "aov": float(row["aov"]) if "aov" in data.columns and pd.notna(row.get("aov")) else None,
            "revenue": float(row["revenue"]) if "revenue" in data.columns and pd.notna(row.get("revenue")) else None,
            "cost": float(row["cost"]) if "cost" in data.columns and pd.notna(row.get("cost")) else None,
            "margin": float(row["margin"]) if "margin" in data.columns and pd.notna(row.get("margin")) else None,
        }
        records_list.append(record)
        display.append(
            {
                "排名": int(row["rank"]),
                "分组": str(row[dimension]),
                _metric_header(metric, fields): _format_value(record["value"], metric_type),
                "占比": fmt_pct(record["share"]),
                "累计占比": fmt_pct(record["cumulative_share"]),
                "订单量": fmt_int(record["orders"]),
                "客单价": fmt_money(record["aov"]),
                "记录数": fmt_int(record["records"]),
            }
        )
    return display, records_list


def _secondary_tables(
    frame: pd.DataFrame,
    dimensions: list[str],
    metric: str,
    fields: dict[str, FieldSpec],
) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions[1:4]:
        if dimension not in frame.columns:
            continue
        display, _ = _dimension_table(frame, dimension, metric, fields)
        tables[fields[dimension].label if dimension in fields else dimension] = display[:10]
    return tables


def _metric_header(metric: str, fields: dict[str, FieldSpec]) -> str:
    if metric == "__record":
        return "记录数"
    return fields[metric].label if metric in fields else metric


def _value_type(metric: str, fields: dict[str, FieldSpec]) -> str:
    if metric == "__record":
        return "int"
    field = fields.get(metric)
    if not field:
        return "number"
    if field.value_format in {"money", "int", "percent", "number"}:
        return field.value_format
    if field.unit == "元":
        return "money"
    if field.unit in {"单", "人", "件", "个", "行"}:
        return "int"
    return "number"


# ---------------------------------------------------------------------------
# Chart data builders
# ---------------------------------------------------------------------------


def _hourly_rows(frame: pd.DataFrame, metric: str, fields: dict[str, FieldSpec]) -> list[dict[str, Any]]:
    if "__hour" not in frame.columns or frame.empty:
        return []
    working = frame.copy()
    working["__hour"] = pd.to_numeric(working["__hour"], errors="coerce")
    working = working.dropna(subset=["__hour"])
    if working.empty:
        return []
    working["__hour"] = working["__hour"].astype(int)
    grouped = working.groupby("__hour")
    if metric == "__record":
        values = grouped.size().astype(float)
    elif metric in fields and fields[metric].aggregation == "AVG":
        values = pd.to_numeric(working[metric], errors="coerce").groupby(working["__hour"]).mean()
    else:
        values = pd.to_numeric(working[metric], errors="coerce").groupby(working["__hour"]).sum(min_count=1)
    orders = grouped["orders"].sum(min_count=1) if "orders" in working.columns and metric != "orders" else None
    observed_hours = sorted(int(hour) for hour in values.index.tolist())
    if not observed_hours:
        return []
    rows: list[dict[str, Any]] = []
    for hour in range(observed_hours[0], observed_hours[-1] + 1):
        value = float(values.get(hour, 0.0) or 0.0)
        rows.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "value": value,
                "orders": float(orders.get(hour, 0.0) or 0.0) if orders is not None else None,
            }
        )
    return rows


def _dod_data(
    target: pd.DataFrame,
    baseline: pd.DataFrame,
    dimension: str | None,
    metric: str,
    fields: dict[str, FieldSpec],
    plan: AnalysisPlan,
) -> dict[str, Any] | None:
    if baseline is None or baseline.empty or not plan.baseline_date:
        return None
    if metric != "__record" and metric not in baseline.columns:
        return None
    current_total = _metric_total(target, metric, fields)
    previous_total = _metric_total(baseline, metric, fields)
    result: dict[str, Any] = {
        "current_total": current_total,
        "previous_total": previous_total,
        "delta": pct_change(current_total, previous_total),
        "categories": [],
        "current": [],
        "previous": [],
    }
    if dimension and dimension in target.columns and dimension in baseline.columns:
        current_display, current_records = _dimension_table(target, dimension, metric, fields)
        _, previous_records = _dimension_table(baseline, dimension, metric, fields)
        previous_map = {row["name"]: row["value"] for row in previous_records}
        ordered = current_records[:8]
        for row in ordered:
            result["categories"].append(row["name"])
            result["current"].append(row["value"])
            result["previous"].append(previous_map.get(row["name"], 0.0))
        used = {row["name"] for row in ordered}
        rest_current = sum(row["value"] for row in current_records if row["name"] not in used)
        rest_previous = sum(value for name, value in previous_map.items() if name not in used)
        if current_records and (rest_current > 0 or rest_previous > 0):
            result["categories"].append("其他")
            result["current"].append(rest_current)
            result["previous"].append(rest_previous)
    return result


def _share_data(dim_records: list[dict[str, Any]]) -> dict[str, Any]:
    if not dim_records or any(row.get("share") is None for row in dim_records):
        return {"items": [], "total": 0.0}
    top = dim_records[:6]
    items = [{"name": row["name"], "value": row["value"]} for row in top]
    rest = sum(row["value"] for row in dim_records[6:])
    if rest > 0:
        items.append({"name": "其他", "value": rest})
    return {"items": items, "total": sum(item["value"] for item in items)}


def _pareto_data(dim_records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(dim_records) < 2 or any(row.get("cumulative_share") is None for row in dim_records):
        return {"categories": [], "values": [], "cumulative": []}
    top = dim_records[:12]
    return {
        "categories": [row["name"] for row in top],
        "values": [row["value"] for row in top],
        "cumulative": [row["cumulative_share"] for row in top],
        "all_count": len(dim_records),
    }


def _distribution_data(
    frame: pd.DataFrame,
    metric: str,
    fields: dict[str, FieldSpec],
) -> dict[str, Any] | None:
    if metric == "__record" or metric not in frame.columns:
        return None
    values = pd.to_numeric(frame[metric], errors="coerce").dropna().astype(float)
    if len(values) < 8 or values.nunique() < 3:
        return None
    values_np = values.to_numpy()
    q1, q3 = float(np.quantile(values_np, 0.25)), float(np.quantile(values_np, 0.75))
    iqr = q3 - q1
    bin_width = 2 * iqr / (len(values_np) ** (1 / 3)) if iqr > 0 else (values_np.max() - values_np.min()) / 8
    if not bin_width or not math.isfinite(bin_width) or bin_width <= 0:
        bin_width = (values_np.max() - values_np.min()) / 8 or 1.0
    bins = max(5, min(12, int(math.ceil((values_np.max() - values_np.min()) / bin_width)) or 6))
    counts, edges = np.histogram(values_np, bins=bins)
    labels: list[str] = []
    for index in range(len(edges) - 1):
        labels.append(f"{_compact_number(edges[index])}–{_compact_number(edges[index + 1])}")
    return {
        "labels": labels,
        "counts": [int(value) for value in counts.tolist()],
        "min": float(values_np.min()),
        "max": float(values_np.max()),
        "median": float(np.median(values_np)),
        "mean": float(values_np.mean()),
        "q1": q1,
        "q3": q3,
    }


def _correlation_data(
    frame: pd.DataFrame,
    metrics: list[str],
    fields: dict[str, FieldSpec],
    dimension: str | None,
) -> dict[str, Any] | None:
    # Revenue × cost is mathematically tied to the same transaction and makes
    # a visually impressive but analytically trivial scatter.  Prefer any
    # available non-financial pair; fall back to all metrics only when needed.
    non_financial = [metric for metric in metrics if metric not in {"cost", "profit"}]
    candidates = non_financial if len(non_financial) >= 2 else metrics
    best: tuple[float, str, str, pd.DataFrame] | None = None
    for index, left in enumerate(candidates):
        if left not in frame.columns:
            continue
        for right in candidates[index + 1 :]:
            if right not in frame.columns:
                continue
            valid = frame[[left, right]].apply(pd.to_numeric, errors="coerce").dropna()
            if len(valid) < 10 or valid[left].nunique() < 2 or valid[right].nunique() < 2:
                continue
            correlation = valid[left].corr(valid[right])
            if pd.isna(correlation):
                continue
            if best is None or abs(float(correlation)) > abs(best[0]):
                best = (float(correlation), left, right, valid)
    if best is None:
        return None
    correlation, left, right, valid = best
    sample = frame.loc[valid.index]
    if len(sample) > 300:
        sample = sample.sample(300, random_state=42)
    if dimension and dimension in sample.columns:
        names = sample[dimension].astype(str)
    else:
        names = sample["__source_row"].map(lambda value: f"行 {value}")
    points: list[dict[str, Any]] = []
    for index, row in sample.iterrows():
        points.append(
            {
                "name": str(names.loc[index]),
                "value": [float(row[left]), float(row[right])],
                "sub": str(row.get(dimension, "")) if dimension and dimension in sample.columns else "",
            }
        )
    return {
        "left": left,
        "right": right,
        "left_label": fields[left].label if left in fields else left,
        "right_label": fields[right].label if right in fields else right,
        "left_type": _value_type(left, fields),
        "right_type": _value_type(right, fields),
        "r": round(correlation, 4),
        "n": len(valid),
        "strength": _strength(correlation),
        "points": points,
    }


def _outlier_rows(
    frame: pd.DataFrame,
    metric: str,
    fields: dict[str, FieldSpec],
    dimension: str | None = None,
) -> list[dict[str, Any]]:
    if "__outlier" not in frame.columns or not bool(frame["__outlier"].any()):
        return []
    subset = frame[frame["__outlier"]].copy()
    if metric != "__record" and metric in subset.columns:
        subset = subset.sort_values(metric, ascending=False)
    rows: list[dict[str, Any]] = []
    for _, row in subset.head(8).iterrows():
        group_value = row.get(dimension, "") if dimension else ""
        rows.append(
            {
                "dimension": str(group_value),
                "metric": fields[metric].label if metric in fields else "记录数",
                "value": _format_value(float(row[metric]), _value_type(metric, fields)) if metric in row else "",
                "orders": fmt_int(float(row["orders"])) if "orders" in row and pd.notna(row.get("orders")) else "—",
                "source": f"{row.get('__source_file', '')} / {row.get('__source_sheet', '')} / 行 {row.get('__source_row', '')}",
                "rule": "IQR 1.5×",
            }
        )
    return rows


def _top_record_rows(frame: pd.DataFrame, metric: str, fields: dict[str, FieldSpec]) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    subset = frame.copy()
    if metric != "__record" and metric in subset.columns:
        subset = subset.sort_values(metric, ascending=False)
    dimension_keys = [key for key, field in fields.items() if field.kind == "dimension" and key in subset.columns]
    rows: list[dict[str, Any]] = []
    for rank, (_, row) in enumerate(subset.head(10).iterrows(), start=1):
        rows.append(
            {
                "rank": rank,
                "dimensions": " / ".join(str(row.get(key, "")) for key in dimension_keys[:3]),
                "metric": _format_value(float(row[metric]), _value_type(metric, fields))
                if metric in row
                else fmt_int(1),
                "orders": fmt_int(float(row["orders"])) if "orders" in row and pd.notna(row.get("orders")) else "—",
                "source": f"{row.get('__source_file', '')} / 行 {row.get('__source_row', '')}",
                "flags": "异常" if bool(row.get("__outlier", False)) else ("补值" if bool(row.get("__imputed", False)) else ""),
            }
        )
    return rows


def _margin_rows(
    frame: pd.DataFrame,
    dimension: str | None,
    fields: dict[str, FieldSpec],
) -> list[dict[str, Any]]:
    if "revenue" not in frame.columns or "cost" not in frame.columns or not dimension:
        return []
    if dimension not in frame.columns:
        return []
    working = frame.copy()
    working["revenue"] = pd.to_numeric(working["revenue"], errors="coerce")
    working["cost"] = pd.to_numeric(working["cost"], errors="coerce")
    grouped = working.groupby(dimension, dropna=False).agg(revenue=("revenue", "sum"), cost=("cost", "sum"))
    grouped["margin"] = grouped.apply(
        lambda row: safe_divide(row["revenue"] - row["cost"], row["revenue"])
        if row["revenue"]
        else None,
        axis=1,
    )
    grouped = grouped.dropna(subset=["margin"]).sort_values("revenue", ascending=False)
    rows: list[dict[str, Any]] = []
    for name, row in grouped.head(8).iterrows():
        rows.append(
            {
                "name": str(name),
                "revenue": float(row["revenue"]),
                "cost": float(row["cost"]),
                "margin": float(row["margin"]),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _build_charts(
    target: pd.DataFrame,
    plan: AnalysisPlan,
    fields: dict[str, FieldSpec],
    dim_records: list[dict[str, Any]],
    hourly: list[dict[str, Any]],
    dod: dict[str, Any] | None,
    share: dict[str, Any],
    pareto: dict[str, Any],
    difference: dict[str, Any] | None,
    correlation: dict[str, Any] | None,
    margin_rows: list[dict[str, Any]],
    settings: Settings,
) -> list[ChartSpec]:
    charts: list[ChartSpec] = []
    metric_label = fields[plan.primary_metric].label if plan.primary_metric and plan.primary_metric in fields else "记录数"
    metric_type = _value_type(plan.primary_metric or "__record", fields)
    enabled = {step.step_id: step for step in plan.steps if step.enabled}
    dimension_label = fields[plan.primary_dimension].label if plan.primary_dimension and plan.primary_dimension in fields else "分组"

    if "hourly" in enabled and any(row["value"] for row in hourly):
        peak = max(hourly, key=lambda row: row["value"])
        charts.append(
            ChartSpec(
                chart_id="chart-hourly",
                chart_type="line",
                title="分时走势",
                subtitle=f"{plan.target_date} · 按小时汇总",
                value_type=metric_type,
                insight=f"峰值出现在 {peak['label']}，贡献 {_format_value(peak['value'], metric_type)}。",
                data={
                    "x": [row["label"] for row in hourly],
                    "series": [
                        {
                            "name": metric_label,
                            "data": [row["value"] for row in hourly],
                            "value_type": metric_type,
                            "chart": "line",
                        },
                        {
                            "name": "订单量",
                            "data": [row["orders"] or 0 for row in hourly],
                            "value_type": "int",
                            "chart": "bar",
                            "y_axis": 1,
                        },
                    ],
                },
                table_headers=["小时", metric_label, "订单量"],
                table_rows=[
                    [row["label"], _format_value(row["value"], metric_type), fmt_int(row["orders"])]
                    for row in hourly
                ],
                table_note="仅展示有记录的小时区间；区间内没有数据的小时补 0，便于核对空白时段。",
                layout="wide",
                height=340,
            )
        )

    if "dod" in enabled and dod and dod.get("categories"):
        charts.append(
            ChartSpec(
                chart_id="chart-dod",
                chart_type="dod",
                title="日环比结构",
                subtitle=f"{plan.target_date} vs {plan.baseline_date} · {dimension_label}",
                value_type=metric_type,
                insight=(
                    f"总量由 {_format_value(dod['previous_total'], metric_type)} "
                    f"变为 {_format_value(dod['current_total'], metric_type)}，"
                    f"变化 {fmt_delta_pct(dod['delta'])}。"
                ),
                data={
                    "categories": dod["categories"],
                    "current": dod["current"],
                    "previous": dod["previous"],
                    "metric_label": metric_label,
                },
                table_headers=[dimension_label, "本期", "前一日", "变化"],
                table_rows=[
                    [
                        category,
                        _format_value(current, metric_type),
                        _format_value(previous, metric_type),
                        fmt_delta_pct(pct_change(current, previous)),
                    ]
                    for category, current, previous in zip(dod["categories"], dod["current"], dod["previous"])
                ],
                layout="wide",
                height=340,
            )
        )

    if "dimension_rank" in enabled and dim_records:
        top = dim_records[: settings.top_n]
        charts.append(
            ChartSpec(
                chart_id="chart-rank",
                chart_type="bar",
                title="结构排名",
                subtitle=f"按{dimension_label}拆解 · Top {len(top)}",
                value_type=metric_type,
                insight=f"{top[0]['name']} 排名第一，贡献 {_format_value(top[0]['value'], metric_type)}，占比 {fmt_pct(top[0]['share'])}。",
                data={
                    "categories": [row["name"] for row in top],
                    "values": [row["value"] for row in top],
                    "metric_label": metric_label,
                    "horizontal": True,
                },
                table_headers=["排名", dimension_label, metric_label, "占比", "订单量", "记录数"],
                table_rows=[
                    [
                        row["rank"],
                        row["name"],
                        _format_value(row["value"], metric_type),
                        fmt_pct(row["share"]),
                        fmt_int(row["orders"]),
                        fmt_int(row["records"]),
                    ]
                    for row in top
                ],
                layout="half",
                height=360,
            )
        )

    if "share" in enabled and share.get("items"):
        charts.append(
            ChartSpec(
                chart_id="chart-share",
                chart_type="donut",
                title="构成占比",
                subtitle=f"{dimension_label} · 主指标占比",
                value_type=metric_type,
                insight=f"前 {min(3, len(share['items']))} 个分组合计占 {fmt_pct(sum(item['value'] for item in share['items'][:3]) / share['total'] * 100 if share['total'] else 0)}。",
                data={
                    "items": share["items"],
                    "total": share["total"],
                    "metric_label": metric_label,
                },
                table_headers=[dimension_label, metric_label, "占比"],
                table_rows=[
                    [
                        item["name"],
                        _format_value(item["value"], metric_type),
                        fmt_pct(item["value"] / share["total"] * 100 if share["total"] else 0),
                    ]
                    for item in share["items"]
                ],
                layout="half",
                height=360,
            )
        )

    if "pareto" in enabled and pareto.get("categories"):
        charts.append(
            ChartSpec(
                chart_id="chart-pareto",
                chart_type="pareto",
                title="集中度 / 帕累托",
                subtitle=f"{dimension_label} · 累计贡献",
                value_type=metric_type,
                insight=_pareto_insight(pareto, metric_type),
                data={
                    "categories": pareto["categories"],
                    "values": pareto["values"],
                    "cumulative": pareto["cumulative"],
                    "metric_label": metric_label,
                    "series_types": [metric_type, "percent"],
                },
                table_headers=[dimension_label, metric_label, "累计占比"],
                table_rows=[
                    [category, _format_value(value, metric_type), fmt_pct(cumulative)]
                    for category, value, cumulative in zip(
                        pareto["categories"], pareto["values"], pareto["cumulative"]
                    )
                ],
                layout="wide",
                height=340,
            )
        )

    if "heatmap" in enabled and plan.primary_dimension and plan.secondary_dimension:
        heatmap = _heatmap_data(
            target=target,
            fields=fields,
            plan=plan,
            metric_label=metric_label,
            metric_type=metric_type,
        )
        if heatmap:
            charts.append(heatmap)

    if "correlation" in enabled and correlation:
        charts.append(
            ChartSpec(
                chart_id="chart-correlation",
                chart_type="scatter",
                title="指标相关性",
                subtitle=f"{correlation['left_label']} × {correlation['right_label']} · n={correlation['n']}",
                value_type="number",
                insight=f"Pearson r = {correlation['r']:.2f}（{correlation['strength']}）。相关不等于因果。",
                data={
                    "points": correlation["points"],
                    "x_label": correlation["left_label"],
                    "y_label": correlation["right_label"],
                    "x_type": correlation["left_type"],
                    "y_type": correlation["right_type"],
                    "r": correlation["r"],
                },
                table_headers=["分组", correlation["left_label"], correlation["right_label"]],
                table_rows=[
                    [
                        point["name"],
                        _format_value(point["value"][0], correlation["left_type"]),
                        _format_value(point["value"][1], correlation["right_type"]),
                    ]
                    for point in correlation["points"][:12]
                ],
                table_note="散点图最多抽样 300 个点；表格展示前 12 个样本。",
                layout="half",
                height=360,
            )
        )

    if "distribution" in enabled and difference:
        charts.append(
            ChartSpec(
                chart_id="chart-distribution",
                chart_type="histogram",
                title="分布与长尾",
                subtitle=f"{metric_label} · 中位数 {_format_value(difference['median'], metric_type)}",
                value_type="int",
                insight=(
                    f"中位数 {_format_value(difference['median'], metric_type)}，"
                    f"均值 {_format_value(difference['mean'], metric_type)}；"
                    f"{'均值高于中位数，存在右偏长尾。' if difference['mean'] > difference['median'] else '均值不高于中位数，分布相对集中。'}"
                ),
                data={
                    "labels": difference["labels"],
                    "counts": difference["counts"],
                    "metric_label": metric_label,
                },
                table_headers=["区间", "记录数"],
                table_rows=[[label, fmt_int(count)] for label, count in zip(difference["labels"], difference["counts"])],
                layout="half",
                height=340,
            )
        )

    if "margin" in enabled and margin_rows:
        charts.append(
            ChartSpec(
                chart_id="chart-margin",
                chart_type="bar",
                title="毛利结构",
                subtitle=f"按{dimension_label} · 毛利率",
                value_type="percent",
                insight=f"毛利率最高的分组是 {max(margin_rows, key=lambda row: row['margin'])['name']}。",
                data={
                    "categories": [row["name"] for row in margin_rows],
                    "values": [row["margin"] for row in margin_rows],
                    "metric_label": "毛利率",
                    "horizontal": True,
                },
                table_headers=[dimension_label, "销售额", "成本", "毛利率"],
                table_rows=[
                    [row["name"], fmt_money(row["revenue"]), fmt_money(row["cost"]), fmt_pct(row["margin"] * 100)]
                    for row in margin_rows
                ],
                layout="half",
                height=340,
            )
        )

    # Defensive bound in case a future plan step is added without a budget.
    return charts[: settings.max_charts]


def _heatmap_data(
    target: pd.DataFrame,
    fields: dict[str, FieldSpec],
    plan: AnalysisPlan,
    metric_label: str,
    metric_type: str,
) -> ChartSpec | None:
    if target is None or target.empty or not plan.primary_dimension or not plan.secondary_dimension:
        return None
    first = plan.primary_dimension
    second = plan.secondary_dimension
    if first not in target.columns or second not in target.columns:
        return None
    metric = plan.primary_metric or "__record"
    working = target.copy()
    working["__metric_value"] = (
        pd.to_numeric(working[metric], errors="coerce").fillna(0.0)
        if metric != "__record"
        else 1.0
    )
    first_totals = working.groupby(first)["__metric_value"].sum().sort_values(ascending=False)
    second_totals = working.groupby(second)["__metric_value"].sum().sort_values(ascending=False)
    first_labels = [str(value) for value in first_totals.head(6).index.tolist()]
    second_labels = [str(value) for value in second_totals.head(6).index.tolist()]
    if len(first_labels) < 2 or len(second_labels) < 2:
        return None
    subset = working[
        working[first].astype(str).isin(first_labels) & working[second].astype(str).isin(second_labels)
    ]
    pivot = subset.pivot_table(
        index=first,
        columns=second,
        values="__metric_value",
        aggfunc="sum",
        fill_value=0.0,
    )
    pivot = pivot.reindex(index=first_labels, columns=second_labels, fill_value=0.0)
    points: list[list[Any]] = []
    max_value = 0.0
    max_cell: tuple[str, str, float] | None = None
    for y_index, first_label in enumerate(first_labels):
        for x_index, second_label in enumerate(second_labels):
            value = float(pivot.loc[first_label, second_label])
            max_value = max(max_value, value)
            if max_cell is None or value > max_cell[2]:
                max_cell = (first_label, second_label, value)
            points.append([x_index, y_index, round(value, 4)])
    if max_value <= 0:
        return None
    total = float(pivot.to_numpy().sum())
    headers = [fields[first].label if first in fields else first, *second_labels]
    rows = []
    for first_label in first_labels:
        rows.append(
            [
                first_label,
                *[
                    _format_value(float(pivot.loc[first_label, second_label]), metric_type)
                    for second_label in second_labels
                ],
            ]
        )
    insight = (
        f"最大单元格为「{max_cell[0]} × {max_cell[1]}」，"
        f"贡献 {_format_value(max_cell[2], metric_type)}，占交叉表合计的 "
        f"{fmt_pct(max_cell[2] / total * 100 if total else 0)}。"
    )
    return ChartSpec(
        chart_id="chart-heatmap",
        chart_type="heatmap",
        title="双维交叉",
        subtitle=f"{fields[first].label if first in fields else first} × "
        f"{fields[second].label if second in fields else second}",
        value_type=metric_type,
        insight=insight,
        data={
            "x_labels": second_labels,
            "y_labels": first_labels,
            "points": points,
            "max": max_value,
            "metric_label": metric_label,
        },
        table_headers=headers,
        table_rows=rows,
        table_note="仅展示主指标贡献最高的 6×6 个类别组合；完整分组排名见结构排名表。",
        layout="wide",
        height=380,
    )


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def _build_insights(
    target: pd.DataFrame,
    baseline: pd.DataFrame,
    fields: dict[str, FieldSpec],
    plan: AnalysisPlan,
    cleaning: CleaningReport,
    totals: dict[str, Any],
    baseline_totals: dict[str, Any],
    dim_records: list[dict[str, Any]],
    pareto: dict[str, Any],
    correlation: dict[str, Any] | None,
    outliers: list[dict[str, Any]],
    margin_rows: list[dict[str, Any]],
) -> list[Insight]:
    insights: list[Insight] = []
    primary = plan.primary_metric
    metric_label = fields[primary].label if primary and primary in fields else "记录数"
    metric_type = _value_type(primary or "__record", fields)
    primary_value = totals.get("metrics", {}).get(primary) if primary else None
    formatted_primary = _format_value(primary_value, metric_type) if primary else fmt_int(len(target))

    if plan.primary_dimension and plan.primary_dimension in fields:
        dimension_label = fields[plan.primary_dimension].label
    else:
        dimension_label = "分组"
    top = dim_records[0] if dim_records else None
    top3_share = (
        sum(row["share"] for row in dim_records[:3])
        if dim_records and all(row.get("share") is not None for row in dim_records[:3])
        else None
    )

    # 1) Scale fact
    scope = (
        f"本期（{plan.target_date}）清洗后共 {fmt_int(len(target))} 条有效记录，"
        f"{metric_label}合计 {formatted_primary}"
    )
    if "orders" in totals.get("metrics", {}) and primary != "orders":
        scope += f"，订单量 {fmt_int(totals['metrics']['orders'])} 单"
    if totals.get("aov") is not None:
        scope += f"，客单价 {fmt_money(totals['aov'])}"
    scope += "。"
    insights.append(
        Insight(
            key="scale",
            severity="info",
            tag="事实",
            title="本期经营规模",
            body=scope,
            evidence=[f"有效记录 {fmt_int(len(target))}", f"{metric_label} {formatted_primary}"],
        )
    )

    # 2) Day-over-day
    if baseline_totals and primary:
        previous = baseline_totals.get("metrics", {}).get(primary)
        delta = pct_change(primary_value, previous)
        if delta is not None:
            direction = "增长" if delta >= 0 else "下降"
            body = (
                f"{metric_label}由 {_format_value(previous, metric_type)} 变为 {formatted_primary}，"
                f"{direction} {abs(delta):.1f}%。"
            )
            if "orders" in totals.get("metrics", {}) and primary != "orders":
                order_delta = pct_change(
                    totals["metrics"].get("orders"), baseline_totals.get("metrics", {}).get("orders")
                )
                body += f" 订单量变化 {fmt_delta_pct(order_delta)}。"
            if totals.get("aov") is not None and baseline_totals.get("aov") is not None:
                body += f" 客单价变化 {fmt_delta_pct(pct_change(totals['aov'], baseline_totals['aov']))}。"
            insights.append(
                Insight(
                    key="dod",
                    severity="good" if delta >= 0 else "warn",
                    tag="事实",
                    title="较前一日变化",
                    body=body,
                    evidence=[f"前一日 {_format_value(previous, metric_type)}", f"变化 {fmt_delta_pct(delta)}"],
                )
            )

    # 3) Structure / concentration (only meaningful for additive metrics)
    if top and top.get("share") is not None:
        reaching = next(
            (index + 1 for index, value in enumerate(pareto.get("cumulative", [])) if value >= 80),
            None,
        )
        body = (
            f"{dimension_label}中「{top['name']}」贡献 {_format_value(top['value'], metric_type)}，"
            f"占 {fmt_pct(top['share'])}；Top3 合计占 {fmt_pct(top3_share)}。"
        )
        if reaching:
            body += f" 前 {reaching} 个分组累计贡献达到 80%。"
        insights.append(
            Insight(
                key="structure",
                severity="info",
                tag="事实",
                title="结构集中度",
                body=body,
                evidence=[f"Top1 占比 {fmt_pct(top['share'])}", f"Top3 合计 {fmt_pct(top3_share)}"],
            )
        )

    # 4) Efficiency ranking, guarded by a minimum sample
    if dim_records and any(row.get("aov") for row in dim_records):
        total_orders = totals.get("metrics", {}).get("orders") or 0
        threshold = max(3, total_orders * 0.05)
        eligible = [row for row in dim_records if (row.get("orders") or 0) >= threshold and row.get("aov")]
        if len(eligible) >= 2:
            best = max(eligible, key=lambda row: row["aov"])
            overall = totals.get("aov")
            gap = pct_change(best["aov"], overall)
            insights.append(
                Insight(
                    key="efficiency",
                    severity="info",
                    tag="事实",
                    title="分组效率差异",
                    body=(
                        f"在订单量不低于 {fmt_int(threshold)} 单的分组中，「{best['name']}」客单价最高，"
                        f"为 {fmt_money(best['aov'])}，较整体 {fmt_money(overall)} "
                        f"{'高' if (gap or 0) >= 0 else '低'} {fmt_pct(abs(gap or 0))}。"
                    ),
                    evidence=[f"{best['name']} 客单价 {fmt_money(best['aov'])}", f"整体 {fmt_money(overall)}"],
                )
            )

    # 5) Correlation, labelled as an inference interpretation
    if correlation and abs(correlation["r"]) >= 0.4:
        direction = "正" if correlation["r"] > 0 else "负"
        insights.append(
            Insight(
                key="correlation",
                severity="info",
                tag="推断",
                title="指标关联",
                body=(
                    f"{correlation['left_label']}与{correlation['right_label']}呈{direction}相关"
                    f"（Pearson r={correlation['r']:.2f}，n={correlation['n']}，{correlation['strength']}）。"
                    "该结果仅说明同期共变，不能直接解释为因果。"
                ),
                evidence=[f"r = {correlation['r']:.2f}", f"有效样本 {fmt_int(correlation['n'])}"],
            )
        )

    # 6) Risk and quality
    missing_total = sum(stat.missing_before for stat in cleaning.field_stats)
    total_cells = max(1, cleaning.rows_in * max(1, len(cleaning.field_stats)))
    missing_rate = missing_total / total_cells * 100
    outlier_total = sum(cleaning.outlier_counts.values())
    risk_parts: list[str] = []
    if outlier_total:
        risk_parts.append(f"{outlier_total} 个指标值命中 IQR 异常（已保留）")
    if cleaning.duplicates_removed:
        risk_parts.append(f"{cleaning.duplicates_removed} 行完全重复已去重")
    if missing_total:
        risk_parts.append(f"缺失单元格 {missing_total} 个（{fmt_pct(missing_rate)}）")
    if cleaning.negative_counts:
        risk_parts.append(f"{sum(cleaning.negative_counts.values())} 个负值需业务确认")
    if not risk_parts:
        risk_parts.append("未发现重复、异常或显著缺失")
    insights.append(
        Insight(
            key="quality",
            severity="warn" if cleaning.quality_score < 80 or outlier_total else "good",
            tag="事实",
            title="数据质量提示",
            body=f"数据质量分 {cleaning.quality_score:.1f}/100；" + "；".join(risk_parts) + "。",
            evidence=[f"质量分 {cleaning.quality_score:.1f}", f"异常值 {outlier_total}"],
        )
    )

    return insights[:7]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_value(value: Any, value_type: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if value_type == "money":
        return fmt_money(number)
    if value_type == "int":
        return fmt_int(number)
    if value_type == "percent":
        return fmt_pct(number * 100)
    return fmt_number(number)


def _compact_number(value: float) -> str:
    absolute = abs(float(value))
    if absolute >= 1e8:
        return f"{value / 1e8:.1f}亿"
    if absolute >= 1e4:
        return f"{value / 1e4:.1f}万"
    if absolute >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}" if abs(value) >= 1 else f"{value:.2f}"


def _strength(correlation: float) -> str:
    absolute = abs(correlation)
    if absolute >= 0.7:
        return "强相关"
    if absolute >= 0.4:
        return "中等相关"
    return "弱相关"


def _pareto_insight(pareto: dict[str, Any], metric_type: str) -> str:
    if not pareto.get("categories"):
        return "分组不足，无法计算集中度。"
    reaching = next(
        (index + 1 for index, value in enumerate(pareto["cumulative"]) if value >= 80),
        len(pareto["categories"]),
    )
    total = sum(pareto["values"])
    top_value = pareto["values"][0]
    return (
        f"前 {reaching} 个分组贡献约 80% 的{('指标' if metric_type != 'int' else '数量')}；"
        f"第一大分组占 {fmt_pct(top_value / total * 100 if total else 0)}。"
    )


def _metric_total(frame: pd.DataFrame, metric: str, fields: dict[str, FieldSpec]) -> float:
    if frame is None or frame.empty:
        return 0.0
    if metric == "__record":
        return float(len(frame))
    if metric not in frame.columns:
        return 0.0
    series = pd.to_numeric(frame[metric], errors="coerce")
    if metric in fields and fields[metric].aggregation == "AVG":
        value = series.mean()
    else:
        value = series.sum()
    return float(value) if pd.notna(value) else 0.0
