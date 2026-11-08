"""Typed data contracts shared across the pipeline.

These dataclasses are intentionally explicit: they double as the public shape
of the JSON artifacts and make the HTML renderer independent from the
ingestion implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Discovery / reading
# ---------------------------------------------------------------------------


@dataclass
class FileIssue:
    code: str
    message: str
    severity: str = "warning"  # info | warning | error
    file: str = ""
    sheet: str = ""

    def as_text(self) -> str:
        location = self.file
        if self.sheet:
            location += f" / {self.sheet}"
        return f"[{self.code}] {location}: {self.message}" if location else f"[{self.code}] {self.message}"


@dataclass
class SourceFile:
    path: Path
    relative_path: str
    suffix: str
    size_bytes: int
    modified_at: datetime

    @property
    def file_type(self) -> str:
        return self.suffix.lower().lstrip(".")


@dataclass
class TableData:
    """A rectangular table extracted from one CSV file or one Excel sheet."""

    source: SourceFile
    sheet: str | None
    frame: pd.DataFrame
    header_row: int
    raw_row_count: int
    encoding: str = ""
    delimiter: str = ""
    columns: list[str] = field(default_factory=list)
    issues: list[FileIssue] = field(default_factory=list)
    truncated: bool = False
    header_repairs: int = 0

    @property
    def sheet_label(self) -> str:
        return self.sheet or "CSV"


@dataclass
class DateCandidate:
    column: str
    kind: str  # date | timestamp
    parse_rate: float
    priority: int


@dataclass
class FieldSpec:
    """A source column mapped to a canonical analytics field."""

    original: str
    canonical: str
    label: str
    kind: str  # date | timestamp | metric | dimension | id | text
    unit: str = ""
    priority: int = 0
    confidence: float = 1.0
    parse_rate: float = 0.0
    coverage: float = 0.0
    unique_count: int = 0
    multiplier: float = 1.0
    value_format: str = "number"
    aggregation: str = "SUM"  # SUM | AVG | COUNT


@dataclass
class TableSchema:
    fields: dict[str, FieldSpec]
    column_map: dict[str, str]
    date_candidates: list[DateCandidate]
    dropped_columns: list[str] = field(default_factory=list)
    issues: list[FileIssue] = field(default_factory=list)
    selected_date_column: str = ""

    @property
    def metrics(self) -> list[FieldSpec]:
        return sorted(
            (field for field in self.fields.values() if field.kind == "metric"),
            key=lambda field: field.priority,
            reverse=True,
        )

    @property
    def dimensions(self) -> list[FieldSpec]:
        return sorted(
            (field for field in self.fields.values() if field.kind == "dimension"),
            key=lambda field: field.priority,
            reverse=True,
        )


@dataclass
class FileReport:
    relative_path: str
    file_type: str
    size_bytes: int
    modified_at: str
    status: str  # read | no_target | skipped | error
    sheets_read: int = 0
    sheets_skipped: int = 0
    rows_scanned: int = 0
    rows_target: int = 0
    rows_baseline: int = 0
    encoding: str = ""
    header_row: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ReadResult:
    tables: list[TableData]
    file_reports: list[FileReport]
    fingerprint: str
    files_scanned: int
    files_read: int
    warnings: list[FileIssue] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


@dataclass
class FieldCleaningStat:
    column: str
    label: str
    kind: str
    missing_before: int
    missing_after: int
    missing_rate: float
    filled: int = 0
    outliers: int = 0
    unit: str = ""


@dataclass
class CleaningReport:
    period_date: str
    rows_in: int
    rows_out: int
    duplicates_removed: int = 0
    invalid_date_rows: int = 0
    missing_filled: dict[str, int] = field(default_factory=dict)
    outlier_counts: dict[str, int] = field(default_factory=dict)
    outlier_bounds: dict[str, dict[str, float]] = field(default_factory=dict)
    garbled_repaired: int = 0
    negative_counts: dict[str, int] = field(default_factory=dict)
    field_stats: list[FieldCleaningStat] = field(default_factory=list)
    quality_score: float = 0.0
    quality_components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    dropped_all_empty: int = 0


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class AnalysisStep:
    step_id: str
    title: str
    rationale: str
    chart_type: str = ""
    priority: int = 50
    enabled: bool = True
    skip_reason: str = ""


@dataclass
class MetricDefinition:
    key: str
    label: str
    formula: str
    unit: str
    aggregation: str = "SUM"
    value_format: str = "number"
    derived: bool = False
    source_columns: list[str] = field(default_factory=list)


@dataclass
class AnalysisPlan:
    target_date: str
    baseline_date: str | None
    dataset_kind: str
    rows: int
    column_count: int
    metrics: list[str]
    dimensions: list[str]
    primary_metric: str | None
    primary_dimension: str | None
    secondary_dimension: str | None
    time_column: str | None
    steps: list[AnalysisStep]
    metric_definitions: list[MetricDefinition]
    reasoning: list[str]
    generated_at: str
    profile: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Analysis / report
# ---------------------------------------------------------------------------


@dataclass
class KPI:
    key: str
    label: str
    value: float | None
    formatted: str
    unit: str = ""
    delta: float | None = None
    delta_text: str = "—"
    delta_label: str = "较前一日"
    trend: str = "flat"  # up | down | flat | unknown
    hint: str = ""
    value_format: str = "number"


@dataclass
class Insight:
    key: str
    severity: str  # info | good | warn | risk
    tag: str  # 事实 | 推断
    title: str
    body: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class ChartSpec:
    chart_id: str
    chart_type: str
    title: str
    subtitle: str
    value_type: str
    insight: str
    data: dict[str, Any]
    table_headers: list[str] = field(default_factory=list)
    table_rows: list[list[Any]] = field(default_factory=list)
    table_note: str = ""
    layout: str = "half"  # half | wide
    height: int = 320


@dataclass
class AnalysisResult:
    target_date: str
    baseline_date: str | None
    plan: AnalysisPlan
    kpis: list[KPI]
    insights: list[Insight]
    charts: list[ChartSpec]
    dimension_table: list[dict[str, Any]]
    secondary_tables: dict[str, list[dict[str, Any]]]
    outliers: list[dict[str, Any]]
    correlation: dict[str, Any] | None
    hourly: list[dict[str, Any]]
    dod: dict[str, Any] | None
    top_records: list[dict[str, Any]]
    target_totals: dict[str, Any]
    baseline_totals: dict[str, Any]
    quality: CleaningReport
    summary: dict[str, Any]
    generated_at: str


# ---------------------------------------------------------------------------
# Run record
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    run_id: str
    trigger: str
    status: str
    target_date: str
    baseline_date: str | None
    started_at: str
    finished_at: str = ""
    duration_ms: int = 0
    files_scanned: int = 0
    files_read: int = 0
    files: list[dict[str, Any]] = field(default_factory=list)
    rows_target: int = 0
    rows_baseline: int = 0
    rows_clean: int = 0
    report_generated: bool = False
    report_path: str = ""
    failure_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    error_type: str = ""
    fingerprint: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def warning_count(self) -> int:
        return len(self.warnings)


STATUS_SUCCESS = "success"
STATUS_NO_DATA = "no_data"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
