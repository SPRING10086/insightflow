"""Deterministic cleaning with an auditable report.

Rules are intentionally conservative: duplicates are removed only when every
business column is identical, missing numbers are filled with the median, and
statistical outliers are flagged rather than deleted.  Every transformation
that changes a count is recorded in :class:`CleaningReport`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from .models import CleaningReport, FieldCleaningStat, FieldSpec
from .utils import clamp, iqr_bounds, maybe_repair_text, normalize_text

_METADATA_COLUMNS = {
    "__source_file",
    "__source_sheet",
    "__source_row",
    "__period",
    "__record",
    "__outlier",
    "__negative",
    "__imputed",
}


def clean_frame(
    frame: pd.DataFrame,
    fields: dict[str, FieldSpec],
    report_date: date,
    iqr_k: float = 1.5,
    unparseable_rows: int = 0,
) -> tuple[pd.DataFrame, CleaningReport]:
    if frame is None or frame.empty:
        return pd.DataFrame(), CleaningReport(
            period_date=report_date.isoformat(),
            rows_in=0,
            rows_out=0,
            invalid_date_rows=unparseable_rows,
            quality_score=0.0,
            notes=["目标日期没有可清洗的数据。"],
        )

    working = frame.copy()
    rows_in = len(working)
    metric_fields = [field for field in fields.values() if field.kind == "metric" and field.canonical in working.columns]
    dimension_fields = [
        field for field in fields.values() if field.kind == "dimension" and field.canonical in working.columns
    ]
    id_fields = [field for field in fields.values() if field.kind == "id" and field.canonical in working.columns]
    business_columns = [field.canonical for field in [*metric_fields, *dimension_fields, *id_fields]]
    if not business_columns:
        business_columns = [column for column in working.columns if column not in _METADATA_COLUMNS]

    # 1) Drop rows that contain no business value at all.
    if business_columns:
        nonempty = working[business_columns].apply(
            lambda column: column.map(lambda value: bool(normalize_text(value)))
        )
        keep = nonempty.any(axis=1)
        dropped_all_empty = int((~keep).sum())
        working = working.loc[keep].copy()
    else:
        dropped_all_empty = 0

    # 2) Remove exact duplicates after source metadata is excluded.  Keeping
    # the first occurrence is deterministic because files are sorted.
    dedupe_columns = [column for column in business_columns if column in working.columns]
    if dedupe_columns:
        before = len(working)
        working = working.drop_duplicates(subset=dedupe_columns, keep="first")
        duplicates_removed = before - len(working)
    else:
        duplicates_removed = 0

    missing_before: dict[str, int] = {}
    missing_filled: dict[str, int] = {}
    field_stats: list[FieldCleaningStat] = []
    imputed_mask = pd.Series(False, index=working.index)
    garbled_repaired_total = 0

    # 3) Missing values: medians for metrics, explicit sentinel for text.
    for field in metric_fields:
        series = pd.to_numeric(working[field.canonical], errors="coerce")
        missing_mask = series.isna()
        missing = int(missing_mask.sum())
        missing_before[field.canonical] = missing
        if missing:
            median = series.dropna().median()
            fill_value = float(median) if pd.notna(median) else 0.0
            series = series.fillna(fill_value)
            missing_filled[field.canonical] = missing
            imputed_mask = imputed_mask | missing_mask
        working[field.canonical] = series.astype(float)

    for field in [*dimension_fields, *id_fields]:
        series = working[field.canonical].map(normalize_text)
        missing = int((series == "").sum())
        missing_before[field.canonical] = missing
        if missing:
            fill_value = "未填写" if field.kind == "dimension" else "未提供"
            series = series.mask(series == "", fill_value)
            missing_filled[field.canonical] = missing
        # Repair is idempotent; this also catches values that were not fixed by
        # the normalizer (e.g. a hand-edited CSV with isolated mojibake).
        repaired: list[str] = []
        repaired_count = 0
        for value in series.tolist():
            fixed = maybe_repair_text(value)
            if fixed != value:
                repaired_count += 1
            repaired.append(fixed)
        if repaired_count:
            series = pd.Series(repaired, index=series.index)
            garbled_repaired_total += repaired_count
        working[field.canonical] = series

    # 4) Flag statistical outliers and suspicious negatives.  Never drop them:
    # a big order is often the most important row in a daily report.
    outlier_counts: dict[str, int] = {}
    outlier_bounds: dict[str, dict[str, float]] = {}
    outlier_mask = pd.Series(False, index=working.index)
    negative_counts: dict[str, int] = {}
    negative_mask = pd.Series(False, index=working.index)
    for field in metric_fields:
        series = working[field.canonical].astype(float)
        bounds = iqr_bounds(series.tolist(), k=iqr_k)
        if bounds:
            q1, q3, lower, upper = bounds
            mask = (series < lower) | (series > upper)
            outlier_counts[field.canonical] = int(mask.sum())
            outlier_bounds[field.canonical] = {
                "q1": round(q1, 4),
                "q3": round(q3, 4),
                "lower": round(lower, 4),
                "upper": round(upper, 4),
            }
            outlier_mask = outlier_mask | mask.reindex(outlier_mask.index, fill_value=False)
        if field.canonical not in {"discount", "price"}:
            negative = series < 0
            negative_counts[field.canonical] = int(negative.sum())
            negative_mask = negative_mask | negative

    working["__outlier"] = outlier_mask.astype(bool)
    working["__negative"] = negative_mask.astype(bool)
    working["__imputed"] = imputed_mask.reindex(working.index, fill_value=False).astype(bool)

    # 5) Field-level audit table.
    for field in [*metric_fields, *dimension_fields, *id_fields]:
        missing = missing_before.get(field.canonical, 0)
        field_stats.append(
            FieldCleaningStat(
                column=field.canonical,
                label=field.label,
                kind=field.kind,
                missing_before=missing,
                missing_after=0,
                missing_rate=missing / max(1, rows_in),
                filled=missing_filled.get(field.canonical, 0),
                outliers=outlier_counts.get(field.canonical, 0),
                unit=field.unit,
            )
        )

    # 6) Deterministic ordering.  Missing hours sort first; that is acceptable
    # because a daily file without a timestamp is still valid.
    sort_columns = [column for column in ("__hour", "__date") if column in working.columns]
    dimension_sort = [
        field.canonical for field in dimension_fields[:1] if field.canonical in working.columns
    ]
    if sort_columns:
        working = working.sort_values(sort_columns + dimension_sort, kind="mergesort").reset_index(drop=True)
    else:
        working = working.reset_index(drop=True)

    # 7) Heuristic quality score.  The formula is deliberately visible in the
    # report; it is a communication aid, not a statistical certification.
    total_cells = max(1, len(working) * max(1, len([*metric_fields, *dimension_fields, *id_fields])))
    total_missing = sum(missing_before.values())
    completeness = 1.0 - total_missing / total_cells
    duplicate_rate = duplicates_removed / max(1, rows_in)
    outlier_rate = int(working["__outlier"].sum()) / max(1, len(working))
    parse_error_rate = unparseable_rows / max(1, rows_in + unparseable_rows)
    score = 100.0 * (
        0.60 * clamp(completeness)
        + 0.15 * clamp(1.0 - min(1.0, duplicate_rate * 2.0))
        + 0.15 * clamp(1.0 - min(1.0, outlier_rate * 3.0))
        + 0.10 * clamp(1.0 - min(1.0, parse_error_rate * 2.0))
    )
    notes = [
        "数值缺失使用当期非缺失值中位数填补；无有效中位数时填 0，并计入质量分。",
        "文本缺失统一标记为「未填写」，ID 缺失标记为「未提供」，不做猜测。",
        "异常值按 IQR 法标记（仅标记、不删除）；负数保留并在报告中提示。",
        "完全重复行保留首次出现；跨文件重复同样按业务列判断。",
    ]
    if score < 70:
        notes.append("质量分低于 70，阅读结论时应结合数据质量章节。")

    report = CleaningReport(
        period_date=report_date.isoformat(),
        rows_in=rows_in,
        rows_out=len(working),
        duplicates_removed=duplicates_removed,
        invalid_date_rows=unparseable_rows,
        missing_filled=missing_filled,
        outlier_counts=outlier_counts,
        outlier_bounds=outlier_bounds,
        garbled_repaired=garbled_repaired_total,
        negative_counts={key: value for key, value in negative_counts.items() if value},
        field_stats=field_stats,
        quality_score=round(score, 1),
        quality_components={
            "completeness": round(completeness * 100, 1),
            "duplicate_control": round(clamp(1.0 - min(1.0, duplicate_rate * 2.0)) * 100, 1),
            "outlier_control": round(clamp(1.0 - min(1.0, outlier_rate * 3.0)) * 100, 1),
            "parse_quality": round(clamp(1.0 - min(1.0, parse_error_rate * 2.0)) * 100, 1),
        },
        notes=notes,
        dropped_all_empty=dropped_all_empty,
    )
    return working, report


def merge_cleaning_reports(reports: list[CleaningReport]) -> CleaningReport:
    """Used only when the pipeline is extended to multiple partitions."""
    if not reports:
        return CleaningReport(period_date="", rows_in=0, rows_out=0)
    if len(reports) == 1:
        return reports[0]
    first = reports[0]
    return CleaningReport(
        period_date=first.period_date,
        rows_in=sum(report.rows_in for report in reports),
        rows_out=sum(report.rows_out for report in reports),
        duplicates_removed=sum(report.duplicates_removed for report in reports),
        invalid_date_rows=sum(report.invalid_date_rows for report in reports),
        missing_filled=_merge_counts(report.missing_filled for report in reports),
        outlier_counts=_merge_counts(report.outlier_counts for report in reports),
        garbled_repaired=sum(report.garbled_repaired for report in reports),
        quality_score=round(sum(report.quality_score for report in reports) / len(reports), 1),
        notes=first.notes,
    )


def _merge_counts(count_maps: Any) -> dict[str, int]:
    merged: dict[str, int] = {}
    for counts in count_maps:
        for key, value in counts.items():
            merged[key] = merged.get(key, 0) + int(value)
    return merged
