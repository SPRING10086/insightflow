"""Turn a detected table into canonical, typed rows for the target period.

The reader keeps original headers and values.  This module is the boundary
where semantics become stable: ``__date``, ``__hour``, canonical metric and
dimension columns, plus source lineage columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from .models import DateCandidate, FieldSpec, FileIssue, TableData, TableSchema
from .utils import (
    maybe_repair_text,
    normalize_text,
    parse_date_value,
    parse_number,
    parse_timestamp_value,
)

_INLINE_UNIT = re.compile(r"(亿|万|千|百|[kKmMwW])$")
_ID_NAME = re.compile(r"(^id$|id$|编号$|单号$|号$)", re.IGNORECASE)


@dataclass
class PeriodExtraction:
    target: pd.DataFrame
    baseline: pd.DataFrame
    selected_column: str
    selected_kind: str
    target_raw_rows: int
    baseline_raw_rows: int
    unparseable_rows: int
    date_parse_rate: float
    issues: list[FileIssue] = field(default_factory=list)


def date_series(series: pd.Series, kind: str, date_order: str = "month_first") -> pd.Series:
    """Parse a date/timestamp column into a pandas ``date`` series."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.date
    if kind == "timestamp":
        return series.map(lambda value: _timestamp_date(value, date_order))
    return series.map(lambda value: parse_date_value(value, date_order=date_order))


def _timestamp_date(value: Any, date_order: str = "month_first") -> date | None:
    parsed = parse_timestamp_value(value, date_order=date_order)
    return parsed.date() if parsed else None


def pick_date_candidate(
    table: TableData,
    schema: TableSchema,
    target_date: date,
    baseline_date: date | None,
    date_order: str = "month_first",
) -> tuple[DateCandidate | None, pd.Series | None, dict[str, int]]:
    """Choose the date column that actually contains the target period.

    Header semantics are a prior, but the deciding vote is how many rows match
    the requested dates.  This handles workbooks with both ``下单日期`` and
    ``发货日期`` without hard-coding one of them.
    """
    best: tuple[float, DateCandidate, pd.Series] | None = None
    counts: dict[str, int] = {}
    for candidate in schema.date_candidates:
        if candidate.column not in table.frame.columns:
            continue
        parsed = date_series(table.frame[candidate.column], candidate.kind, date_order=date_order)
        target_count = int((parsed == target_date).sum())
        baseline_count = int((parsed == baseline_date).sum()) if baseline_date else 0
        parse_rate = float(parsed.notna().mean()) if len(parsed) else 0.0
        score = target_count * 20.0 + baseline_count * 4.0 + parse_rate * 5.0 + candidate.priority / 100.0
        counts[candidate.column] = target_count
        if best is None or score > best[0]:
            best = (score, candidate, parsed)
    if best is None:
        return None, None, counts
    return best[1], best[2], counts


def extract_periods(
    table: TableData,
    schema: TableSchema,
    target_date: date,
    baseline_date: date | None,
    date_order: str = "month_first",
) -> PeriodExtraction:
    issues: list[FileIssue] = []
    candidate, parsed, counts = pick_date_candidate(
        table, schema, target_date, baseline_date, date_order=date_order
    )
    if candidate is None or parsed is None:
        issues.append(
            FileIssue(
                "no_parseable_dates",
                "未找到可解析的日期列，无法筛选目标日期。",
                severity="error",
                file=table.source.relative_path,
                sheet=table.sheet_label,
            )
        )
        return PeriodExtraction(
            target=pd.DataFrame(),
            baseline=pd.DataFrame(),
            selected_column="",
            selected_kind="",
            target_raw_rows=0,
            baseline_raw_rows=0,
            unparseable_rows=len(table.frame),
            date_parse_rate=0.0,
            issues=issues,
        )

    unparseable = int(parsed.isna().sum())
    parse_rate = float(parsed.notna().mean()) if len(parsed) else 0.0
    target_mask = parsed == target_date
    baseline_mask = parsed == baseline_date if baseline_date else pd.Series(False, index=table.frame.index)
    target_raw = table.frame.loc[target_mask].copy()
    baseline_raw = table.frame.loc[baseline_mask].copy()

    if unparseable:
        issues.append(
            FileIssue(
                "date_values_unparsed",
                f"{unparseable} 行的日期无法解析，已跳过。",
                severity="warning",
                file=table.source.relative_path,
                sheet=table.sheet_label,
            )
        )
    if target_raw.empty:
        issues.append(
            FileIssue(
                "target_date_absent",
                f"文件（{table.sheet_label}）中没有 {target_date.isoformat()} 的数据。",
                severity="info",
                file=table.source.relative_path,
                sheet=table.sheet_label,
            )
        )
    else:
        issues.append(
            FileIssue(
                "date_column_selected",
                f"使用日期列「{candidate.column}」；命中 {target_date.isoformat()} 共 {len(target_raw)} 行"
                + (f"，前一日 {len(baseline_raw)} 行。" if baseline_date else "。"),
                severity="info",
                file=table.source.relative_path,
                sheet=table.sheet_label,
            )
        )

    target = normalize_rows(target_raw, schema, table, "target", candidate, date_order=date_order)
    baseline = (
        normalize_rows(baseline_raw, schema, table, "baseline", candidate, date_order=date_order)
        if baseline_date
        else pd.DataFrame()
    )

    return PeriodExtraction(
        target=target,
        baseline=baseline,
        selected_column=candidate.column,
        selected_kind=candidate.kind,
        target_raw_rows=len(target_raw),
        baseline_raw_rows=len(baseline_raw),
        unparseable_rows=unparseable,
        date_parse_rate=parse_rate,
        issues=issues,
    )


def normalize_rows(
    raw: pd.DataFrame,
    schema: TableSchema,
    table: TableData,
    period: str,
    date_candidate: DateCandidate,
    date_order: str = "month_first",
) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    records: list[dict[str, Any]] = []
    date_values = date_series(raw[date_candidate.column], date_candidate.kind, date_order=date_order)
    timestamp_available = "timestamp" in schema.fields and schema.fields["timestamp"].original in raw.columns
    timestamp_series = (
        raw[schema.fields["timestamp"].original].map(
            lambda value: parse_timestamp_value(value, date_order=date_order)
        )
        if timestamp_available
        else None
    )

    parsed_columns: dict[str, pd.Series] = {}
    for canonical, field in schema.fields.items():
        if field.kind in {"date", "timestamp"}:
            continue
        if field.original not in raw.columns:
            continue
        parsed_columns[canonical] = _parse_field_series(raw[field.original], field)

    source_row_values = raw["__source_row"] if "__source_row" in raw.columns else None
    for position, (_, row) in enumerate(raw.iterrows()):
        parsed_date = date_values.iloc[position]
        if parsed_date is None or pd.isna(parsed_date):
            continue
        source_row = position + table.header_row + 1
        if source_row_values is not None:
            raw_source_row = source_row_values.iloc[position]
            try:
                if not pd.isna(raw_source_row):
                    source_row = int(raw_source_row)
            except (TypeError, ValueError):
                pass
        timestamp = None
        if timestamp_series is not None:
            timestamp = timestamp_series.iloc[position]
        if timestamp is None and date_candidate.kind == "timestamp":
            timestamp = parse_timestamp_value(row.get(date_candidate.column), date_order=date_order)
        if timestamp is None:
            timestamp = None
        hour = timestamp.hour if timestamp is not None else None

        record: dict[str, Any] = {
            "__date": parsed_date,
            "__timestamp": timestamp,
            "__hour": hour,
            "__source_file": table.source.relative_path,
            "__source_sheet": table.sheet_label,
            "__source_row": source_row,
            "__period": period,
            "__record": 1,
        }
        for canonical, series in parsed_columns.items():
            record[canonical] = series.iloc[position]
        records.append(record)

    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    # Stable column order: lineage, time, metrics, dimensions, ids.
    ordered = [
        "__date",
        "__timestamp",
        "__hour",
        "__period",
        "__record",
        "__source_file",
        "__source_sheet",
        "__source_row",
    ]
    metrics = [field.canonical for field in schema.metrics if field.canonical in frame.columns]
    dimensions = [field.canonical for field in schema.dimensions if field.canonical in frame.columns]
    ids = [
        field.canonical
        for field in schema.fields.values()
        if field.kind == "id" and field.canonical in frame.columns
    ]
    ordered.extend(metrics)
    ordered.extend(dimensions)
    ordered.extend(ids)
    remaining = [column for column in frame.columns if column not in ordered]
    ordered.extend(remaining)
    return frame[ordered]


def _parse_field_series(series: pd.Series, field: FieldSpec) -> pd.Series:
    if field.kind == "metric":
        # P2-2: decide per value, never for the whole column.  A single inline
        # unit must not turn the header multiplier off for every other row.
        return series.map(lambda value: _metric_value(value, field.multiplier))
    if field.kind == "dimension":
        return series.map(_text_value)
    if field.kind == "id":
        return series.map(_id_value)
    return series.map(_text_value)


def _has_inline_unit(value: Any) -> bool:
    """True when the cell carries its own unit (``1.2万``, ``3k``, ``5亿元``)."""
    text = normalize_text(value)
    if not text:
        return False
    text = re.sub(r"[¥￥$€£]", "", text)
    text = re.sub(r"(人民币|rmb|cny|元|圆|块钱|块)$", "", text, flags=re.IGNORECASE)
    text = text.replace("，", ",").replace(",", "").replace(" ", "")
    return bool(_INLINE_UNIT.search(text))


def _metric_value(value: Any, header_multiplier: float) -> float | None:
    number = parse_number(value)
    if number is None:
        return None
    # ``parse_number`` already applied any inline unit; applying the header unit
    # again would double-scale.  Bare values do inherit the header unit.
    if header_multiplier != 1.0 and not _has_inline_unit(value):
        return number * header_multiplier
    return number


def unit_mix_issues(frame: pd.DataFrame, schema: TableSchema, table: TableData) -> list[FileIssue]:
    """P2-2: flag columns that mix header units with inline units.

    Mixed columns are still converted per value (the safe path); the warning
    exists so the operator can check whether the source export is consistent.
    """
    issues: list[FileIssue] = []
    for field in schema.fields.values():
        if field.kind != "metric" or field.multiplier == 1.0 or field.original not in frame.columns:
            continue
        inline = bare = 0
        for value in frame[field.original].tolist():
            if parse_number(value) is None:
                continue
            if _has_inline_unit(value):
                inline += 1
            else:
                bare += 1
        if inline and bare:
            issues.append(
                FileIssue(
                    code="mixed_unit",
                    message=(
                        f"字段「{field.label}」同时存在带单位（{inline} 个，如 1.2万）"
                        f"与不带单位（{bare} 个）的值；表头单位「{field.unit or '倍率'}」只作用于不带单位的值，"
                        "已按逐值口径换算，请核对源数据。"
                    ),
                    severity="warning",
                    file=table.source.relative_path,
                    sheet=table.sheet_label,
                )
            )
    return issues


def _text_value(value: Any) -> str:
    text = maybe_repair_text(normalize_text(value))
    return text


def _id_value(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = normalize_text(value)
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def is_id_field(canonical: str, field: FieldSpec) -> bool:
    return field.kind == "id" or bool(_ID_NAME.search(canonical))
