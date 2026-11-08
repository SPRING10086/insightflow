"""Robust CSV / Excel readers with encoding, delimiter and header detection.

Real business exports are rarely clean: they contain cover rows, merged
headers, GBK encoding, inconsistent delimiters, empty sheets and malformed
files.  The reader's contract is simple: never crash the whole run because of
one bad file; return structured issues so the report and run log can explain
what happened.
"""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .config import Settings
from .models import FileIssue, FileReport, ReadResult, SourceFile, TableData
from .schema import header_semantic_hits
from .utils import maybe_repair_text, normalize_text, parse_number, unique_names


class ReaderError(RuntimeError):
    """Fatal error for one source file; the pipeline continues with others."""


# ---------------------------------------------------------------------------
# Encoding and delimiter detection
# ---------------------------------------------------------------------------


def decode_bytes(data: bytes) -> tuple[str, str, list[str]]:
    """Decode spreadsheet bytes with an explicit, testable fallback order."""
    warnings: list[str] = []
    if not data:
        return "", "utf-8", ["empty"]

    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16"), "utf-16", warnings
        except UnicodeDecodeError:
            warnings.append("utf-16 解码失败")
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig", warnings
        except UnicodeDecodeError:
            warnings.append("utf-8-sig 解码失败")

    # GB18030 is a superset of GBK and decodes almost all Chinese exports.
    # UTF-8 is attempted first because it is the modern default.
    candidates = ("utf-8", "gb18030", "gbk", "big5", "cp1252")
    decoded: list[tuple[float, str, str]] = []
    for encoding in candidates:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _decode_score(text, encoding)
        decoded.append((score, encoding, text))
    if not decoded:
        warnings.append("所有编码均解码失败，使用 UTF-8 替换字符")
        return data.decode("utf-8", errors="replace"), "utf-8 (replace)", warnings

    decoded.sort(key=lambda item: item[0], reverse=True)
    score, encoding, text = decoded[0]
    if encoding not in {"utf-8", "utf-8-sig", "gb18030"}:
        warnings.append(f"使用回退编码 {encoding} 读取")
    if "\ufffd" in text:
        warnings.append("文本包含无法解码的替换字符")
    return text, encoding, warnings


def _decode_score(text: str, encoding: str) -> float:
    sample = text[:20_000]
    replacement = sample.count("\ufffd")
    controls = sum(1 for char in sample if ord(char) < 32 and char not in "\r\n\t")
    cjk = sum(1 for char in sample if "\u4e00" <= char <= "\u9fff")
    printable = sum(1 for char in sample if char.isprintable())
    score = 0.0
    score += (printable / max(1, len(sample))) * 4.0
    score += min(30.0, cjk / max(1, len(sample)) * 60.0)
    score -= replacement * 25.0
    score -= controls * 10.0
    if encoding in {"utf-8", "utf-8-sig"}:
        score += 1.0
    if encoding == "cp1252" and cjk == 0:
        score -= 1.0
    return score


def detect_delimiter(text: str) -> tuple[str, list[str]]:
    """Pick a delimiter by column-count consistency, not by a single line."""
    sample_lines = [line for line in text.splitlines()[:20] if line.strip()]
    if not sample_lines:
        return ",", ["empty"]
    sample = "\n".join(sample_lines)
    warnings: list[str] = []
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        sniffer_delimiter = dialect.delimiter
    except csv.Error:
        sniffer_delimiter = ""

    scores: list[tuple[float, int, str]] = []
    for delimiter in (",", "\t", ";", "|"):
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
        except csv.Error:
            continue
        widths = [sum(1 for cell in row if normalize_text(cell)) for row in rows]
        widths = [width for width in widths if width > 0]
        if not widths:
            continue
        mode_width = max(set(widths), key=widths.count)
        consistency = widths.count(mode_width) / len(widths)
        score = mode_width * consistency
        if delimiter == sniffer_delimiter:
            score += 1.5
        scores.append((score, mode_width, delimiter))
    if not scores:
        return ",", ["delimiter_fallback"]
    scores.sort(reverse=True)
    best_score, best_width, best_delimiter = scores[0]
    if best_width <= 1:
        warnings.append("single_column_csv")
    return best_delimiter, warnings


# ---------------------------------------------------------------------------
# Header and frame construction
# ---------------------------------------------------------------------------


def _row_width(row: Iterable[Any]) -> int:
    return sum(1 for cell in row if normalize_text(cell))


def _is_number_like(text: str) -> bool:
    return parse_number(text) is not None


def detect_header_row(rows: list[list[Any]], scan_rows: int = 20) -> int:
    """Score the first N rows and return the most likely header index."""
    best_index = 0
    best_score = -math.inf
    limit = min(len(rows), max(3, scan_rows))
    for index in range(limit):
        row = rows[index]
        nonempty = [normalize_text(cell) for cell in row if normalize_text(cell)]
        if len(nonempty) < 2:
            continue
        width = len(nonempty)
        unique_ratio = len(set(nonempty)) / width
        text_ratio = sum(1 for cell in nonempty if not _is_number_like(cell)) / width
        numeric_ratio = 1.0 - text_ratio
        semantic = header_semantic_hits(nonempty)
        width_consistency = 0.0
        following = rows[index + 1 : index + 4]
        if following:
            matching = sum(1 for next_row in following if abs(_row_width(next_row) - width) <= max(1, width * 0.25))
            width_consistency = matching / len(following)

        score = (
            semantic * 4.5
            + text_ratio * 2.4
            + unique_ratio * 2.0
            + min(width, 10) * 0.2
            + width_consistency * 2.0
            + (1.5 if index == 0 else 0.0)
            - numeric_ratio * 3.5
            - index * 0.04
        )
        if score > best_score:
            best_score = score
            best_index = index

    if best_score == -math.inf:
        for index, row in enumerate(rows):
            if _row_width(row) >= 2:
                return index
        return 0
    return best_index


def _repair_header_row(row: list[Any]) -> tuple[list[str], int]:
    repaired: list[str] = []
    repair_count = 0
    for cell in row:
        raw = normalize_text(cell)
        fixed = maybe_repair_text(raw)
        if fixed != raw:
            repair_count += 1
        repaired.append(fixed)
    return unique_names(repaired), repair_count


def build_frame(
    header: list[str],
    data_rows: list[list[Any]],
    source_rows: list[int] | None = None,
) -> pd.DataFrame:
    """Build a dataframe and optionally preserve each row's physical line.

    ``source_rows`` is aligned to ``data_rows`` before blank rows are removed.
    The hidden ``__source_row`` column lets lineage survive date filtering and
    cleaning; it is excluded from schema inference and display.
    """
    if not header:
        return pd.DataFrame()
    max_width = max([len(header), *(len(row) for row in data_rows)], default=len(header))
    if max_width > len(header):
        header = list(header) + [f"未命名列_{index}" for index in range(len(header) + 1, max_width + 1)]
    matrix: list[list[Any]] = []
    for row in data_rows:
        values = list(row)
        if len(values) < max_width:
            values.extend([None] * (max_width - len(values)))
        elif len(values) > max_width:
            values = values[:max_width]
        matrix.append(values)
    frame = pd.DataFrame(matrix, columns=header, dtype=object)
    if frame.empty:
        return frame
    if source_rows is not None:
        if len(source_rows) < len(frame):
            source_rows = [*source_rows, *range(len(source_rows) + 1, len(frame) + 1)]
        frame["__source_row"] = [int(value) for value in source_rows[: len(frame)]]

    visible_columns = [column for column in frame.columns if column != "__source_row"]
    if not visible_columns:
        return pd.DataFrame()
    nonempty = frame[visible_columns].apply(
        lambda column: column.map(lambda value: bool(normalize_text(value)))
    )
    keep_rows = nonempty.any(axis=1)
    keep_columns = nonempty.any(axis=0)
    columns = [column for column in visible_columns if bool(keep_columns[column])]
    if "__source_row" in frame.columns:
        columns.append("__source_row")
    return frame.loc[keep_rows, columns].reset_index(drop=True)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def read_csv_file(source: SourceFile, settings: Settings) -> tuple[list[TableData], list[FileIssue]]:
    issues: list[FileIssue] = []
    if source.size_bytes == 0:
        return [], [_issue("empty_file", "文件大小为 0 字节。", "error", source.relative_path)]

    try:
        data = source.path.read_bytes()
    except OSError as exc:
        return [], [_issue("file_read_error", f"无法读取文件: {exc}", "error", source.relative_path)]

    if b"\x00" in data[:4096] and not (data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff")):
        return [], [_issue("binary_file", "文件包含 NUL 字节，疑似二进制文件而非 CSV。", "error", source.relative_path)]

    text, encoding, decode_warnings = decode_bytes(data)
    issues.extend(_issue("encoding_fallback", message, "warning", source.relative_path) for message in decode_warnings)
    if not text.strip():
        return [], [_issue("empty_file", "CSV 解码后没有可读内容。", "error", source.relative_path)]
    if "\ufffd" in text:
        issues.append(_issue("garbled_text", "文件中存在无法解码的字符，已按替换字符保留。", "warning", source.relative_path))

    delimiter, delimiter_warnings = detect_delimiter(text)
    issues.extend(_issue("delimiter_warning", message, "info", source.relative_path) for message in delimiter_warnings)
    issues.append(
        _issue(
            "encoding",
            f"编码 {encoding}，分隔符 {repr(delimiter)}。",
            "info",
            source.relative_path,
        )
    )

    rows: list[list[Any]] = []
    row_numbers: list[int] = []
    max_rows = settings.max_rows_per_file
    previous_line_num = 0
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        for index, row in enumerate(reader):
            if len(rows) > max_rows + settings.header_scan_rows + 10:
                break
            start_line = previous_line_num + 1
            previous_line_num = int(reader.line_num)
            if any(normalize_text(cell) for cell in row):
                rows.append(row)
                # Start line (not end line) keeps quoted multi-line cells and
                # blank lines mapped back to the physical CSV row.
                row_numbers.append(start_line)
    except csv.Error as exc:
        issues.append(_issue("csv_parse_error", f"CSV 解析异常: {exc}", "warning", source.relative_path))

    if not rows:
        return [], issues + [_issue("empty_table", "CSV 没有数据行。", "error", source.relative_path)]
    header_index = detect_header_row(rows, settings.header_scan_rows)
    header, header_repairs = _repair_header_row(rows[header_index])
    if header_repairs:
        issues.append(
            _issue(
                "header_repaired",
                f"已自动修复 {header_repairs} 个疑似乱码表头。",
                "warning",
                source.relative_path,
            )
        )
    data_rows = rows[header_index + 1 :]
    data_row_numbers = row_numbers[header_index + 1 :]
    original_data_count = len(data_rows)
    truncated = original_data_count > max_rows
    if truncated:
        data_rows = data_rows[:max_rows]
        data_row_numbers = data_row_numbers[:max_rows]
        issues.append(
            _issue(
                "rows_truncated",
                f"数据行超过上限 {max_rows:,}，本次仅读取前 {max_rows:,} 行。",
                "warning",
                source.relative_path,
            )
        )
    frame = build_frame(header, data_rows, data_row_numbers)
    if frame.empty:
        return [], issues + [_issue("empty_table", "CSV 只有表头，没有有效数据行。", "warning", source.relative_path)]

    ragged = sum(1 for row in data_rows if len(row) != len(header))
    if ragged:
        issues.append(
            _issue(
                "ragged_rows",
                f"{ragged} 行的列数与表头不一致，已按表头宽度补齐/截断。",
                "warning",
                source.relative_path,
            )
        )
    table = TableData(
        source=source,
        sheet=None,
        frame=frame,
        header_row=header_index + 1,
        raw_row_count=original_data_count,
        encoding=encoding,
        delimiter=delimiter,
        columns=[column for column in frame.columns if not str(column).startswith("__")],
        issues=issues,
        truncated=truncated,
        header_repairs=header_repairs,
        sheet_total=1,
    )
    return [table], issues


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def read_excel_file(source: SourceFile, settings: Settings) -> tuple[list[TableData], list[FileIssue]]:
    issues: list[FileIssue] = []
    if source.size_bytes == 0:
        return [], [_issue("empty_file", "文件大小为 0 字节。", "error", source.relative_path)]

    engine = "xlrd" if source.suffix.lower() == ".xls" else "openpyxl"
    if engine == "xlrd":
        try:
            import xlrd  # noqa: F401
        except ImportError:
            return [], [
                _issue(
                    "missing_xls_engine",
                    "读取 .xls 需要 xlrd，请执行 pip install xlrd。",
                    "error",
                    source.relative_path,
                )
            ]

    tables: list[TableData] = []
    try:
        workbook = pd.ExcelFile(source.path, engine=engine)
    except Exception as exc:  # openpyxl raises several format-specific errors
        return [], [
            _issue(
                "excel_open_error",
                f"无法打开工作簿（可能损坏、加密或格式不匹配）: {exc}",
                "error",
                source.relative_path,
            )
        ]

    sheet_total = len(workbook.sheet_names)
    try:
        for sheet_name in workbook.sheet_names:
            try:
                frame = pd.read_excel(
                    workbook,
                    sheet_name=sheet_name,
                    header=None,
                    dtype=object,
                )
            except Exception as exc:
                issues.append(_issue("sheet_read_error", f"工作表读取失败: {exc}", "warning", source.relative_path, sheet_name))
                continue
            if frame.empty:
                issues.append(_issue("empty_sheet", "工作表为空。", "info", source.relative_path, sheet_name))
                continue
            # Bind memory before header detection, but never let the header
            # row consume one of the configured business-row slots.
            scan_cap = settings.max_rows_per_file + settings.header_scan_rows + 10
            if len(frame) > scan_cap:
                frame = frame.head(scan_cap)
            rows = frame.where(pd.notna(frame), None).values.tolist()
            rows = [[cell for cell in row] for row in rows]
            while rows and _row_width(rows[-1]) == 0:
                rows.pop()
            if not rows:
                continue
            header_index = detect_header_row(rows, settings.header_scan_rows)
            header, header_repairs = _repair_header_row(rows[header_index])
            if header_repairs:
                issues.append(
                    _issue(
                        "header_repaired",
                        f"工作表「{sheet_name}」已自动修复 {header_repairs} 个疑似乱码表头。",
                        "warning",
                        source.relative_path,
                        sheet_name,
                    )
                )
            data_rows = rows[header_index + 1 :]
            original_data_count = len(data_rows)
            source_rows = [header_index + 2 + index for index in range(original_data_count)]
            truncated = original_data_count > settings.max_rows_per_file
            if truncated:
                data_rows = data_rows[: settings.max_rows_per_file]
                source_rows = source_rows[: settings.max_rows_per_file]
                issues.append(
                    _issue(
                        "rows_truncated",
                        f"工作表业务数据超过 {settings.max_rows_per_file:,} 行，已截断。",
                        "warning",
                        source.relative_path,
                        sheet_name,
                    )
                )
            table_frame = build_frame(header, data_rows, source_rows)
            if table_frame.empty:
                issues.append(
                    _issue("empty_sheet", "工作表只有表头或全为空行。", "info", source.relative_path, sheet_name)
                )
                continue
            table = TableData(
                source=source,
                sheet=sheet_name,
                frame=table_frame,
                header_row=header_index + 1,
                raw_row_count=original_data_count,
                encoding="excel",
                delimiter="",
                columns=[column for column in table_frame.columns if not str(column).startswith("__")],
                issues=[],
                truncated=truncated,
                header_repairs=header_repairs,
                sheet_total=sheet_total,
            )
            tables.append(table)
    finally:
        workbook.close()

    if not tables:
        issues.append(
            _issue(
                "no_usable_sheet",
                "工作簿中没有可用的数据表（空表或表头不可识别）。",
                "error",
                source.relative_path,
            )
        )
    return tables, issues


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def read_sources(files: list[SourceFile], settings: Settings, root: Path) -> ReadResult:
    from .utils import file_fingerprint

    tables: list[TableData] = []
    file_reports: list[FileReport] = []
    all_warnings: list[FileIssue] = []
    files_read = 0

    for source in files:
        issues: list[FileIssue] = []
        file_tables: list[TableData] = []
        error_message = ""
        status = "no_target"

        if source.size_bytes > settings.max_file_bytes:
            issues.append(
                _issue(
                    "file_too_large",
                    f"文件 {source.size_bytes / 1024 / 1024:.1f} MB 超过上限 {settings.max_file_mb:.0f} MB，已跳过。",
                    "error",
                    source.relative_path,
                )
            )
            status = "skipped"
        else:
            try:
                if source.suffix.lower() == ".csv":
                    file_tables, issues = read_csv_file(source, settings)
                elif source.suffix.lower() in {".xlsx", ".xls"}:
                    file_tables, issues = read_excel_file(source, settings)
                else:
                    issues = [_issue("unsupported_type", "不支持的文件类型。", "error", source.relative_path)]
                    status = "skipped"
            except Exception as exc:  # defensive boundary: one file never kills the batch
                error_message = f"{type(exc).__name__}: {exc}"
                issues.append(_issue("unexpected_read_error", error_message, "error", source.relative_path))
                status = "error"

            if file_tables:
                tables.extend(file_tables)
                files_read += 1
                status = "read"
            elif status != "skipped":
                # A zero-byte/corrupt file is an error; a valid workbook with
                # only cover sheets is simply no_target.
                status = "error" if any(issue.severity == "error" for issue in issues) else "no_target"

        if not error_message:
            error_message = next((issue.message for issue in issues if issue.severity == "error"), "")
        all_warnings.extend(issues)
        total_sheets = max((table.sheet_total for table in file_tables), default=0)
        file_reports.append(
            FileReport(
                relative_path=source.relative_path,
                file_type=source.file_type,
                size_bytes=source.size_bytes,
                modified_at=source.modified_at.isoformat(),
                status=status,
                sheets_read=len(file_tables),
                sheets_skipped=max(0, total_sheets - len(file_tables)),
                rows_scanned=sum(table.raw_row_count for table in file_tables),
                encoding=file_tables[0].encoding if file_tables else "",
                header_row=file_tables[0].header_row if file_tables else 0,
                warnings=[issue.as_text() for issue in issues if issue.severity in {"warning", "error"}],
                error=error_message,
            )
        )
        for table in file_tables:
            table.issues = list(issues)

    return ReadResult(
        tables=tables,
        file_reports=file_reports,
        fingerprint=file_fingerprint([source.path for source in files], root),
        files_scanned=len(files),
        files_read=files_read,
        warnings=all_warnings,
    )


def _issue(
    code: str,
    message: str,
    severity: str = "warning",
    file: str = "",
    sheet: str = "",
) -> FileIssue:
    return FileIssue(code=code, message=message, severity=severity, file=file, sheet=sheet)
