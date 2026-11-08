"""Application services shared by the CLI and the desktop operator shell.

``preview`` is the read-only counterpart of a full run: it scans the source
directory, reads every table and reports how many rows fall on the target and
baseline dates — without writing a report, taking the pipeline lock or touching
the idempotency index.

Living here (instead of inside ``cli.py``) is what lets the GUI show a preview
without copying the read logic; the CLI keeps only argument handling and
printing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from .config import Settings
from .discovery import discover_files
from .normalize import extract_periods
from .readers import read_sources
from .schema import detect_schema

__all__ = ["PreviewPayload", "preview"]


class PreviewPayload(dict[str, Any]):
    """Structured preview result.

    It is a plain ``dict`` (so ``scan --json`` keeps exactly the keys it had
    before the refactor) plus one attribute: ``warnings`` carries the
    compatibility warnings for the text output and for the GUI, and is
    deliberately *not* a key so the JSON contract does not change.
    """

    warnings: list[dict[str, str]]

    @property
    def empty_source(self) -> bool:
        """True when the directory holds no supported files."""
        return int(self.get("files_scanned") or 0) == 0

    @property
    def has_target_rows(self) -> bool:
        """True when at least one table has rows on the target date."""
        return any(int(row.get("target_rows") or 0) > 0 for row in self.get("tables") or [])


def preview(
    settings: Settings,
    target: date,
    source_dir: Path | str | None = None,
) -> PreviewPayload:
    """Scan and parse ``source_dir`` as of ``target``; never writes anything.

    Raises ``SourceNotFoundError`` when the directory is missing.  An existing
    but empty directory returns a payload with :attr:`PreviewPayload.empty_source`
    true, so each caller can print its own guidance.
    """
    source_root = Path(source_dir).expanduser().resolve() if source_dir else settings.source_dir
    baseline = settings.baseline_date(target)
    files = discover_files(settings, source_root)

    payload = PreviewPayload(
        target_date=target.isoformat(),
        baseline_date=baseline.isoformat() if baseline else None,
        source_dir=str(source_root),
        files_scanned=0,
        files_read=0,
        tables=[],
        file_errors=[],
    )
    payload.warnings = []
    if not files:
        return payload

    read_result = read_sources(files, settings, source_root)
    tables: list[dict[str, Any]] = []
    for table in read_result.tables:
        # Same call as the previous CLI implementation: header detection stays
        # on the default scan window so the preview is byte-for-byte unchanged.
        schema = detect_schema(table.frame, date_order=settings.date_order)
        extraction = extract_periods(table, schema, target, baseline, date_order=settings.date_order)
        tables.append(
            {
                "file": table.source.relative_path,
                "sheet": table.sheet_label,
                "rows": len(table.frame),
                "target_rows": len(extraction.target),
                "baseline_rows": len(extraction.baseline),
                "date_column": extraction.selected_column or "—",
                "fields": len(schema.fields),
                "issues": [
                    issue.message
                    for issue in [*schema.issues, *extraction.issues]
                    if issue.severity in {"warning", "error"}
                ],
            }
        )

    payload["files_scanned"] = read_result.files_scanned
    payload["files_read"] = read_result.files_read
    payload["tables"] = tables
    payload["file_errors"] = [
        {"file": report.relative_path, "status": report.status, "error": report.error}
        for report in read_result.file_reports
        if report.error
    ]
    payload.warnings = [
        {
            "code": issue.code,
            "message": issue.message,
            "severity": issue.severity,
            "file": issue.file,
            "sheet": issue.sheet,
        }
        for issue in read_result.warnings
        if issue.severity in {"warning", "error"}
    ]
    return payload
