"""Header understanding and semantic field inference.

The inference layer is deliberately explainable.  Every column gets a role
(date / metric / dimension / id), a confidence and a human-readable label.
The report exposes the resulting mapping so a reviewer can audit it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from .models import DateCandidate, FieldSpec, FileIssue, TableData, TableSchema
from .utils import normalize_text, parse_date_value, parse_number, maybe_repair_text, slugify

# Canonical field metadata: label, kind, unit, priority, display format.
FIELD_META: dict[str, dict[str, Any]] = {
    "date": {"label": "日期", "kind": "date", "unit": "", "priority": 100, "format": "date"},
    "timestamp": {"label": "时间", "kind": "timestamp", "unit": "", "priority": 95, "format": "datetime"},
    "revenue": {"label": "销售额", "kind": "metric", "unit": "元", "priority": 100, "format": "money"},
    "orders": {"label": "订单量", "kind": "metric", "unit": "单", "priority": 90, "format": "int"},
    "new_customers": {"label": "新增客户数", "kind": "metric", "unit": "人", "priority": 70, "format": "int"},
    "customers": {"label": "客户数", "kind": "metric", "unit": "人", "priority": 65, "format": "int"},
    "profit": {"label": "利润", "kind": "metric", "unit": "元", "priority": 60, "format": "money"},
    "cost": {"label": "成本", "kind": "metric", "unit": "元", "priority": 55, "format": "money"},
    "price": {"label": "单价", "kind": "metric", "unit": "元", "priority": 50, "format": "money"},
    "discount": {"label": "折扣", "kind": "metric", "unit": "%", "priority": 40, "format": "percent"},
    "region": {"label": "区域", "kind": "dimension", "unit": "", "priority": 92, "format": "text"},
    "category": {"label": "品类", "kind": "dimension", "unit": "", "priority": 95, "format": "text"},
    "channel": {"label": "渠道", "kind": "dimension", "unit": "", "priority": 88, "format": "text"},
    "store": {"label": "门店", "kind": "dimension", "unit": "", "priority": 78, "format": "text"},
    "province": {"label": "省份", "kind": "dimension", "unit": "", "priority": 72, "format": "text"},
    "city": {"label": "城市", "kind": "dimension", "unit": "", "priority": 70, "format": "text"},
    "product": {"label": "商品", "kind": "dimension", "unit": "", "priority": 68, "format": "text"},
    "brand": {"label": "品牌", "kind": "dimension", "unit": "", "priority": 62, "format": "text"},
    "salesperson": {"label": "销售员", "kind": "dimension", "unit": "", "priority": 58, "format": "text"},
    "customer_id": {"label": "客户ID", "kind": "id", "unit": "", "priority": 30, "format": "text"},
    "order_id": {"label": "订单ID", "kind": "id", "unit": "", "priority": 40, "format": "text"},
}

# Aliases are matched after lower-casing and removing punctuation/spaces.
ALIASES: dict[str, tuple[str, ...]] = {
    "date": (
        "日期", "数据日期", "订单日期", "业务日期", "交易日期", "统计日期", "账期", "账单日期",
        "date", "orderdate", "bizdate", "statdate", "dt", "day", "日期时间", "订单日期时间",
    ),
    "timestamp": (
        "订单时间", "交易时间", "下单时间", "创建时间", "支付时间", "时间戳", "发生时间",
        "timestamp", "datetime", "ordertime", "createdat", "paidat", "time",
    ),
    "revenue": (
        "销售额", "销售金额", "销售总额", "总销售额", "营收", "营业收入", "收入", "成交额",
        "交易额", "营业额", "消费金额", "总消费", "gmv", "revenue", "sales", "salesamount",
        "amount", "totalamount", "total spent", "totalspent", "total spend", "amount spent",
    ),
    "orders": (
        "订单量", "订单数", "订单笔数", "订单数量", "成交单量", "销量", "销售数量", "件数",
        "orders", "ordercount", "orderqty", "qty", "quantity", "salesvolume",
    ),
    "new_customers": (
        "新增客户", "新增客户数", "新客数", "新增用户", "新增用户数", "newcustomers", "newusers",
    ),
    "customers": (
        "客户数", "客户数量", "用户数", "会员数", "购买客户数", "成交客户数", "customers",
        "customercount", "usercount", "buyers",
    ),
    "profit": ("利润", "毛利", "毛利额", "净利润", "profit", "grossprofit", "netprofit"),
    "cost": ("成本", "销售成本", "总成本", "成本额", "商品成本", "cost", "totalcost", "cogs"),
    "price": ("单价", "客单价", "均价", "商品单价", "price", "avgprice", "aov"),
    "discount": ("折扣", "折扣率", "优惠率", "discount", "discountrate"),
    "region": ("区域", "地区", "大区", "销售区域", "region", "area"),
    "category": (
        "品类", "类目", "产品品类", "商品品类", "商品类别", "品类名称", "category",
        "productcategory", "segment",
    ),
    "channel": ("渠道", "销售渠道", "平台", "渠道类型", "channel", "platform"),
    "store": ("门店", "门店名称", "店铺", "店铺名称", "store", "shop", "storename"),
    "province": ("省份", "省", "province"),
    "city": ("城市", "市", "city"),
    "product": ("产品", "商品", "产品名称", "商品名称", "sku名称", "product", "productname", "item", "itemname"),
    "brand": ("品牌", "brand"),
    "salesperson": ("销售员", "销售代表", "业务员", "负责人", "salesrep", "salesperson", "owner"),
    "customer_id": (
        "客户id", "客户编号", "客户号", "用户id", "会员id", "customerid", "userid", "memberid",
    ),
    "order_id": ("订单id", "订单号", "订单编号", "orderno", "orderid", "billno"),
}


def normalize_header_key(value: Any) -> str:
    text = maybe_repair_text(normalize_text(value)).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


_ALIAS_INDEX: dict[str, str] = {}
for _canonical, _aliases in ALIASES.items():
    for _alias in _aliases:
        _ALIAS_INDEX[normalize_header_key(_alias)] = _canonical


_RATE_HEADER_MARKERS = ("率", "占比", "比例", "百分比", "同比", "环比", "平均", "均值")
_NEGATIVE_HEADER_PREFIXES = ("非", "不是", "排除", "不计", "不包含", "未包含")


def _is_non_additive_header(key: str) -> bool:
    """Reject headers whose values must not be summed as amounts.

    ``毛利率`` / ``利润率`` are ratios, not profit amounts; ``非销售额`` is a
    negated/auxiliary field, not revenue.  They fall through to unknown
    metrics instead of silently polluting a SUM.
    """
    if any(key.startswith(prefix) for prefix in _NEGATIVE_HEADER_PREFIXES):
        return True
    return any(marker in key for marker in _RATE_HEADER_MARKERS)


def match_alias(header: Any) -> tuple[str | None, float]:
    """Return ``(canonical, confidence)`` for a header cell.

    Exact matches are preferred; otherwise the longest contained alias wins so
    that ``新增客户数`` does not get classified as ``客户数``.  Rate and negated
    headers are deliberately not mapped to additive amount metrics.
    """
    key = normalize_header_key(header)
    if not key:
        return None, 0.0
    if _is_non_additive_header(key):
        return None, 0.0
    exact = _ALIAS_INDEX.get(key)
    if exact:
        return exact, 1.0

    candidates: list[tuple[int, int, str]] = []
    for alias, canonical in _ALIAS_INDEX.items():
        if len(alias) >= 2 and alias in key:
            candidates.append((len(alias), FIELD_META.get(canonical, {}).get("priority", 0), canonical))
    if not candidates:
        return None, 0.0
    candidates.sort(reverse=True)
    return candidates[0][2], 0.82


def header_semantic_hits(cells: Iterable[Any]) -> int:
    return sum(1 for cell in cells if match_alias(cell)[0] is not None)


def _extract_unit(header: Any, canonical: str | None) -> tuple[str, float]:
    text = maybe_repair_text(normalize_text(header))
    unit_match = re.search(r"[\(（\[]\s*(亿元|万元|千元|元|%|％|人|单|件|个)\s*[\)）\]]", text)
    if not unit_match:
        unit_match = re.search(r"(亿元|万元|千元|元|%|％|人|单|件|个)\s*$", text)
    if not unit_match:
        return (FIELD_META.get(canonical or "", {}).get("unit", ""), 1.0)

    token = unit_match.group(1)
    if token in {"%", "％"}:
        return "%", 1.0
    mapping = {"亿元": (1e8, "元"), "万元": (1e4, "元"), "千元": (1e3, "元"), "元": (1.0, "元")}
    if token in mapping:
        multiplier, unit = mapping[token]
        return unit, multiplier
    return token, 1.0


def _series_values(series: pd.Series, limit: int = 5000) -> list[Any]:
    values: list[Any] = []
    for value in series.head(limit).tolist():
        text = normalize_text(value)
        if text:
            values.append(value)
    return values


def _numeric_rate(series: pd.Series) -> tuple[float, int]:
    values = _series_values(series)
    if not values:
        return 0.0, 0
    valid = sum(1 for value in values if parse_number(value) is not None)
    return valid / len(values), valid


def _date_rate(series: pd.Series) -> tuple[float, int]:
    values = _series_values(series)
    if not values:
        return 0.0, 0
    valid = sum(1 for value in values if parse_date_value(value) is not None)
    return valid / len(values), valid


def _looks_like_id(header: str, series: pd.Series) -> bool:
    key = normalize_header_key(header)
    if any(token in key for token in ("id", "编号", "客户号", "订单号", "单号", "sku")):
        return True
    values = [normalize_text(value) for value in _series_values(series, 200) if normalize_text(value)]
    if len(values) < 10:
        return False
    unique_ratio = len(set(values)) / len(values)
    integer_like = all(re.fullmatch(r"\d{4,}", value) for value in values[:20])
    return unique_ratio > 0.92 and integer_like


def detect_schema(df: pd.DataFrame, header_scan: int = 20) -> TableSchema:
    """Infer canonical roles for every source column.

    ``header_scan`` is accepted for symmetry with the reader; the dataframe is
    already expected to have its header extracted at this point.
    """
    issues: list[FileIssue] = []
    fields: dict[str, FieldSpec] = {}
    column_map: dict[str, str] = {}
    date_candidates: list[DateCandidate] = []
    dropped: list[str] = []
    unknown_dimension_count = 0

    for column in list(df.columns):
        # Reader-owned lineage columns (for example __source_row) are
        # metadata, never business fields.
        if str(column).startswith("__"):
            continue
        series = df[column]
        non_empty = [value for value in _series_values(series, 8000) if normalize_text(value)]
        coverage = len(non_empty) / max(1, len(df))
        unique_count = len({normalize_text(value) for value in non_empty})
        if not non_empty:
            dropped.append(str(column))
            continue

        canonical, confidence = match_alias(column)
        unit, multiplier = _extract_unit(column, canonical)
        numeric_rate, _ = _numeric_rate(series)
        date_rate, _ = _date_rate(series)

        kind: str
        label: str
        priority: int
        value_format = "number"
        if canonical:
            meta = FIELD_META.get(canonical, {})
            kind = str(meta.get("kind", "text"))
            label = str(meta.get("label", normalize_text(column)))
            priority = int(meta.get("priority", 0))
            value_format = str(meta.get("format", "number"))
            if not unit:
                unit = str(meta.get("unit", ""))
        else:
            kind = "text"
            label = maybe_repair_text(normalize_text(column)) or "未命名列"
            priority = 0

        # Header aliases can be wrong when a column has been mislabeled.  For
        # dates, require at least some parseable values; for metrics, keep the
        # semantic label but record low confidence.
        if kind in {"date", "timestamp"} and date_rate < 0.15:
            issues.append(
                FileIssue(
                    "date_parse_low",
                    f"字段「{label}」仅 {date_rate:.0%} 的值可解析为日期，已保留为候选。",
                    severity="warning",
                )
            )
        if kind == "metric" and numeric_rate < 0.5:
            issues.append(
                FileIssue(
                    "metric_parse_low",
                    f"字段「{label}」仅 {numeric_rate:.0%} 的值可解析为数值。",
                    severity="warning",
                )
            )

        if not canonical:
            # Unknown numeric columns become metrics; low-cardinality text
            # becomes a dimension; high-cardinality text is kept out of the
            # analytical frame to avoid noisy reports.
            if numeric_rate >= 0.78 and not _looks_like_id(column, series):
                kind = "metric"
                label = maybe_repair_text(normalize_text(column)) or "指标"
                priority = 12
                value_format = "number"
            elif date_rate >= 0.82 and not _looks_like_id(column, series):
                kind = "date"
                label = "日期"
                priority = 60
            elif 2 <= unique_count <= 20 and coverage >= 0.55 and not _looks_like_id(column, series):
                kind = "dimension"
                label = maybe_repair_text(normalize_text(column)) or "维度"
                priority = 20
            else:
                kind = "text"

        if kind == "text":
            dropped.append(str(column))
            continue

        if kind in {"date", "timestamp"}:
            date_candidates.append(
                DateCandidate(
                    column=str(column),
                    kind=kind,
                    parse_rate=date_rate,
                    priority=priority,
                )
            )

        if kind == "id":
            canonical = canonical or f"id_{slugify(str(column))}"
        elif canonical is None:
            canonical = f"metric_{slugify(str(column))}" if kind == "metric" else f"dim_{slugify(str(column))}"
        elif canonical in fields:
            # Preserve a second same-semantic column instead of silently
            # overwriting it.  It remains visible in lineage.
            canonical = f"{canonical}__{slugify(str(column))}"
            issues.append(
                FileIssue(
                    "duplicate_semantic_column",
                    f"字段「{normalize_text(column)}」与已有同义字段重复，已重命名为 {canonical}。",
                    severity="info",
                )
            )

        if kind == "dimension" and canonical.startswith("dim_"):
            unknown_dimension_count += 1
            if unknown_dimension_count > 3:
                dropped.append(str(column))
                continue

        field = FieldSpec(
            original=str(column),
            canonical=canonical,
            label=label,
            kind=kind,
            unit=unit,
            priority=priority,
            confidence=confidence if confidence else 0.55,
            parse_rate=numeric_rate if kind == "metric" else date_rate,
            coverage=coverage,
            unique_count=unique_count,
            multiplier=multiplier,
            value_format=value_format,
        )
        fields[canonical] = field
        column_map[str(column)] = canonical

    date_candidates.sort(key=lambda candidate: (candidate.priority, candidate.parse_rate), reverse=True)
    selected = date_candidates[0].column if date_candidates else ""
    if not date_candidates:
        issues.append(FileIssue("no_date_column", "未识别到日期字段，无法按目标日期筛选。", severity="error"))
    if not any(field.kind == "metric" for field in fields.values()):
        issues.append(
            FileIssue(
                "no_metric_column",
                "未识别到数值指标，报告将退化为记录频次与数据质量分析。",
                severity="warning",
            )
        )
    if not any(field.kind == "dimension" for field in fields.values()):
        issues.append(
            FileIssue(
                "no_dimension_column",
                "未识别到可分组维度，结构分析将不可用。",
                severity="warning",
            )
        )

    return TableSchema(
        fields=fields,
        column_map=column_map,
        date_candidates=date_candidates,
        dropped_columns=dropped,
        issues=issues,
        selected_date_column=selected,
    )


def table_schema_for(table: TableData) -> TableSchema:
    return detect_schema(table.frame)


def metric_score(field: FieldSpec) -> float:
    """Prefer high-value, well-covered, non-constant metrics."""
    return field.priority + min(20.0, field.coverage * 10.0) + (5.0 if field.parse_rate >= 0.9 else 0.0)


def choose_primary_metric(fields: dict[str, FieldSpec]) -> str | None:
    metrics = [field for field in fields.values() if field.kind == "metric" and field.coverage >= 0.2]
    if not metrics:
        return None
    metrics.sort(key=metric_score, reverse=True)
    return metrics[0].canonical


def choose_dimensions(fields: dict[str, FieldSpec], limit: int = 4) -> list[str]:
    """Rank dimensions by business meaning and usable cardinality."""
    candidates: list[tuple[float, str]] = []
    for field in fields.values():
        if field.kind != "dimension":
            continue
        if field.unique_count < 2 or field.unique_count > 50:
            continue
        cardinality_bonus = 8.0 if 3 <= field.unique_count <= 12 else 2.0
        coverage_bonus = min(10.0, field.coverage * 10.0)
        candidates.append((field.priority + cardinality_bonus + coverage_bonus, field.canonical))
    candidates.sort(reverse=True)
    return [canonical for _, canonical in candidates[:limit]]


def infer_value_format(field: FieldSpec) -> str:
    if field.value_format in {"money", "int", "percent", "number"}:
        return field.value_format
    if field.unit == "元":
        return "money"
    if field.unit in {"单", "人", "件", "个"}:
        return "int"
    return "number"


def display_label(field: FieldSpec) -> str:
    label = normalize_text(field.label) or field.canonical
    if field.unit and field.kind == "metric" and field.unit not in {"%"}:
        return f"{label}（{field.unit}）" if "（" not in label else label
    return label


def describe_column(field: FieldSpec) -> str:
    role = {"date": "日期", "timestamp": "时间", "metric": "指标", "dimension": "维度", "id": "标识"}.get(
        field.kind, "文本"
    )
    return f"{display_label(field)} · {role} · {field.unique_count} 个取值 · 覆盖 {field.coverage:.0%}"


@dataclass
class DatasetProfile:
    fields: dict[str, FieldSpec]
    metrics: list[str]
    dimensions: list[str]
    primary_metric: str | None
    primary_dimension: str | None
    secondary_dimension: str | None
    time_column: str | None
    dataset_kind: str
    rows: int
    column_count: int
    reasoning: list[str]


def profile_dataset(df: pd.DataFrame, fields: dict[str, FieldSpec], rows: int) -> DatasetProfile:
    metrics = sorted(
        [field.canonical for field in fields.values() if field.kind == "metric"],
        key=lambda key: metric_score(fields[key]),
        reverse=True,
    )
    dimensions = choose_dimensions(fields)
    primary_metric = choose_primary_metric(fields)
    time_column = None
    if "__hour" in df.columns and pd.to_numeric(df["__hour"], errors="coerce").notna().any():
        time_column = "__hour"
    primary_dimension = dimensions[0] if dimensions else None
    secondary_dimension = dimensions[1] if len(dimensions) > 1 else None

    if primary_metric in {"revenue", "orders", "profit"}:
        dataset_kind = "销售经营明细"
    elif primary_metric in {"customers", "new_customers"}:
        dataset_kind = "客户/用户经营数据"
    elif metrics:
        dataset_kind = "通用业务指标数据"
    else:
        dataset_kind = "通用业务明细数据"

    reasoning: list[str] = []
    if primary_metric:
        field = fields[primary_metric]
        reasoning.append(
            f"主指标选择「{field.label}」：业务优先级最高（{field.priority}），覆盖率 {field.coverage:.0%}。"
        )
    if primary_dimension:
        field = fields[primary_dimension]
        reasoning.append(
            f"主维度选择「{field.label}」：{field.unique_count} 个取值，适合做结构、排名与集中度分析。"
        )
    if time_column:
        reasoning.append("识别到时间字段，可补充分时走势分析。")
    if len(metrics) >= 2:
        reasoning.append(f"识别到 {len(metrics)} 个数值指标，可计算指标间相关性与效率对比。")
    if not metrics:
        reasoning.append("未识别到数值指标，分析计划自动降级为频次与数据质量视角。")

    return DatasetProfile(
        fields=fields,
        metrics=metrics,
        dimensions=dimensions,
        primary_metric=primary_metric,
        primary_dimension=primary_dimension,
        secondary_dimension=secondary_dimension,
        time_column=time_column,
        dataset_kind=dataset_kind,
        rows=rows,
        column_count=len(df.columns),
        reasoning=reasoning,
    )
