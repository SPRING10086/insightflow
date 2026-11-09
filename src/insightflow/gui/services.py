"""Application services used by the desktop shell.

This is the **only** module under ``insightflow.gui`` allowed to import the core
package (pipeline / logging_setup / utils).  Everything returned here is plain
data — dicts, lists, primitives — so ``app.py`` and ``worker.py`` never touch
``RunRecord``, pandas or the pipeline directly, and every business number still
comes from ``pipeline.execute()``.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..logging_setup import read_run_history
from ..pipeline import execute
from ..services import preview as core_preview
from ..utils import atomic_write_json, humanize_bytes, humanize_duration

#: Mirrors ``pipeline._RunLock(stale_minutes=150)``; a test keeps them in sync.
LOCK_STALE_MINUTES = 150

#: Human-facing stages of one run (FR-2.3).
STAGE_TOTAL = 4
STAGE_LABELS: dict[int, str] = {1: "读取数据", 2: "清洗与去重", 3: "分析与计划", 4: "生成报告"}

#: Core step name -> stage number.  Unknown names simply do not move the bar.
STAGE_BY_STEP: dict[str, int] = {
    "扫描数据源": 1,
    "读取与解析": 1,
    "字段识别与日期筛选": 1,
    "数据清洗": 2,
    "生成分析计划": 3,
    "指标计算": 3,
    "渲染离线报告": 4,
}

STATUS_LABELS: dict[str, str] = {
    "success": "成功",
    "no_data": "无数据",
    "failed": "失败",
    "skipped": "已跳过",
}

RESULT_UNAVAILABLE: dict[str, str] = {
    "no_data": "本次没有目标日期的数据，未生成报告与清洗数据。",
    "failed": "本次运行失败，未生成结果；请查看日志后重试。",
    "skipped": "本次被跳过（并发锁或数据未变化），没有新结果。",
}

#: FR-4.1 / FR-4.2: the three date-order choices, in operator wording.
DATE_ORDER_LABELS: dict[str, str] = {
    "month_first": "月/日/年（默认）",
    "day_first": "日/月/年（欧式）",
    "error": "歧义时报错跳过",
}

#: Quality bands (FR-5.1).  Highest threshold first.
QUALITY_GRADES: tuple[tuple[float, str, str], ...] = (
    (90.0, "优秀", "#15803d"),
    (80.0, "良好", "#0f766e"),
    (60.0, "需关注", "#b45309"),
    (0.0, "较差", "#b91c1c"),
)

#: Order in which alerts are shown: worst first.
ALERT_ORDER = ("bad_file", "duplicate", "fill", "outlier", "date_order")


class SourceUnavailable(RuntimeError):
    """Operator-facing error for a missing or unreadable source directory."""


class ServiceError(RuntimeError):
    """A failure already translated into operator wording (FR-8.2)."""


def friendly_error(exc: BaseException) -> str:
    """Translate an exception into 'what happened + what to do' (FR-8.2)."""
    if isinstance(exc, PermissionError):
        return "没有权限访问或写入文件。请确认文件未被 Excel 等程序占用，或在文件属性中取消只读。"
    if isinstance(exc, FileNotFoundError):
        return "找不到需要的文件或目录。请确认数据源目录和配置里的路径仍然存在。"
    if isinstance(exc, OSError):
        return f"读写文件时出错：{exc}。请检查磁盘空间、文件是否被占用后重试。"
    text = str(exc).strip()
    if text:
        return f"{text}。如果重复出现，请点「打开日志目录」，把最新的日志发给技术支持。"
    return "发生未知错误。请点「打开日志目录」查看最新记录，再联系技术支持。"


# ---------------------------------------------------------------------------
# Source folder
# ---------------------------------------------------------------------------


def list_source_files(settings: Settings, source_dir: Path | str | None = None) -> dict[str, Any]:
    """List every candidate file, marking unsupported types (FR-1.2 / FR-1.3).

    The ignore rules mirror ``discovery.discover_files`` (hidden files and
    ``~$`` Office lock files); a test asserts the supported subset equals the
    core discovery result, so the two can never drift apart silently.
    """
    root = (Path(source_dir) if source_dir else settings.source_dir).expanduser().resolve()
    if not root.exists():
        raise SourceUnavailable(f"数据源目录不存在：{root}")
    if not root.is_dir():
        raise SourceUnavailable(f"数据源路径不是目录：{root}")

    allowed = {str(extension).lower() for extension in settings.include_extensions}
    iterator = root.rglob("*") if settings.recursive_scan else root.glob("*")
    files: list[dict[str, Any]] = []
    for path in iterator:
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("~$") or name.startswith("."):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        suffix = path.suffix.lower()
        files.append(
            {
                "name": name,
                "relative_path": path.relative_to(root).as_posix(),
                "type": suffix.lstrip(".").upper() or "—",
                "size_text": humanize_bytes(stat.st_size),
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "supported": suffix in allowed,
            }
        )

    files.sort(key=lambda item: (not item["supported"], str(item["relative_path"]).lower()))
    return {
        "source_dir": str(root),
        "recursive": settings.recursive_scan,
        "total_count": len(files),
        "supported_count": sum(1 for item in files if item["supported"]),
        "files": files,
    }


def default_target_date(settings: Settings) -> date:
    """The date the pipeline would use if the operator changes nothing."""
    return settings.target_date()


# ---------------------------------------------------------------------------
# Date-order configuration (FR-4.1 / FR-4.2 / FR-8.4)
# ---------------------------------------------------------------------------


def save_date_order(settings: Settings, value: str) -> Path:
    """Persist ``date_order`` into ``config/config.json`` (atomic write)."""
    if value not in DATE_ORDER_LABELS:
        raise ValueError(f"未知的日期口径：{value}")
    path = settings.project_root / "config" / "config.json"
    data: dict[str, Any] = {}
    example = settings.project_root / "config" / "config.example.json"
    for candidate in (path, example):
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                data = loaded
            if candidate == path:
                break
    data["date_order"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)
    return path


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def lock_state(settings: Settings) -> dict[str, Any]:
    """Read ``state/.pipeline.lock`` without acquiring anything (FR-2.4)."""
    path = settings.state_dir / ".pipeline.lock"
    try:
        age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return {"locked": False, "age_minutes": None, "stale": False}
    stale = age_seconds > LOCK_STALE_MINUTES * 60
    return {
        "locked": not stale,
        "age_minutes": round(age_seconds / 60, 1),
        "stale": stale,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_analysis(
    settings: Settings,
    target: date,
    *,
    force: bool = False,
    on_stage: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Run the real pipeline once and return a plain summary for the window.

    The only business entry point: ``pipeline.execute()`` does all the work.
    ``on_stage`` receives ``(stage_number, stage_label)`` as core steps finish.
    """
    def observe(name: str, _detail: str) -> None:
        stage = STAGE_BY_STEP.get(name)
        if stage is not None and on_stage is not None:
            on_stage(stage, STAGE_LABELS[stage])

    try:
        record = execute(settings, target_date=target, trigger="manual", force=force, on_step=observe)
    except Exception as exc:  # noqa: BLE001 - translate, never leak a traceback
        raise ServiceError(friendly_error(exc)) from exc

    summary = summarise(settings, record, target)
    summary["quality"] = collect_quality(settings, summary)
    return summary


def summarise(settings: Settings, record: Any, target: date) -> dict[str, Any]:
    """Convert a ``RunRecord`` into plain data (no core objects leave services)."""
    day_dir = settings.output_dir / (record.target_date or target.isoformat())
    report = settings.project_root / record.report_path if record.report_path else None
    report_exists = bool(report and report.is_file())
    cleaned = day_dir / "cleaned_data.csv"
    metrics = day_dir / "metrics.json"
    plan = day_dir / "analysis_plan.json"

    return {
        "run_id": record.run_id,
        "status": record.status,
        "status_label": STATUS_LABELS.get(record.status, record.status),
        "target_date": record.target_date or target.isoformat(),
        "baseline_date": record.baseline_date,
        "rows_target": record.rows_target,
        "rows_clean": record.rows_clean,
        "files_scanned": record.files_scanned,
        "files_read": record.files_read,
        "duration_ms": record.duration_ms,
        "duration_text": humanize_duration(record.duration_ms),
        "failure_reason": record.failure_reason,
        "warnings": list(record.warnings),
        "warning_count": record.warning_count(),
        "files": [
            {
                "relative_path": str(item.get("relative_path", "")),
                "status": str(item.get("status", "")),
                "error": str(item.get("error", "")),
                "rows_target": int(item.get("rows_target", 0) or 0),
            }
            for item in record.files
            if isinstance(item, dict)
        ],
        "report_path": str(report) if report_exists else "",
        "report_dir": str(report.parent) if report_exists else "",
        "cleaned_path": str(cleaned) if cleaned.is_file() else "",
        "metrics_path": str(metrics) if metrics.is_file() else "",
        "plan_path": str(plan) if plan.is_file() else "",
        "steps": [
            {"name": str(step.get("name", "")), "duration_ms": int(step.get("duration_ms", 0) or 0)}
            for step in record.steps
        ],
    }


# ---------------------------------------------------------------------------
# Quality / alerts (FR-5.1 - FR-5.4)
# ---------------------------------------------------------------------------


def grade_for(score: float) -> tuple[str, str]:
    """Return ``(grade_label, colour)`` for a quality score."""
    for threshold, label, colour in QUALITY_GRADES:
        if score >= threshold:
            return label, colour
    return QUALITY_GRADES[-1][1], QUALITY_GRADES[-1][2]


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "是"}


def count_flag_rows(cleaned_path: str | Path) -> dict[str, int]:
    """Count rows flagged ``__imputed`` / ``__outlier`` in cleaned_data.csv."""
    counts = {"imputed_rows": 0, "outlier_rows": 0}
    path = Path(cleaned_path)
    if not path.is_file():
        return counts
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if _truthy(row.get("__imputed")):
                    counts["imputed_rows"] += 1
                if _truthy(row.get("__outlier")):
                    counts["outlier_rows"] += 1
    except (OSError, csv.Error):
        return counts
    return counts


def _labels(quality: dict[str, Any]) -> dict[str, str]:
    return {
        str(stat.get("column")): str(stat.get("label") or stat.get("column"))
        for stat in quality.get("field_stats") or []
        if stat.get("column")
    }


def _join_names(names: list[str], limit: int = 3) -> str:
    if not names:
        return "—"
    head = "、".join(names[:limit])
    return head if len(names) <= limit else f"{head} 等 {len(names)} 个字段"


def collect_quality(settings: Settings, summary: dict[str, Any]) -> dict[str, Any]:
    """Build the operator-facing quality block from ``metrics.json``.

    ``metrics.json`` is the single source of truth (FR-5.1): the score shown in
    the window is read from it, never recomputed here.
    """
    metrics_path = str(summary.get("metrics_path") or "")
    if not metrics_path or not Path(metrics_path).is_file():
        return {
            "available": False,
            "reason": "本次没有生成 metrics.json（未运行、无数据或运行失败）。",
            "alerts": [],
            "bad_files": [],
        }
    try:
        payload = json.loads(Path(metrics_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "reason": f"metrics.json 无法读取：{friendly_error(exc)}",
            "alerts": [],
            "bad_files": [],
        }

    quality = payload.get("quality") or {}
    score = float(quality.get("quality_score") or 0.0)
    grade, colour = grade_for(score)
    labels = _labels(quality)
    alerts: list[dict[str, Any]] = []

    files = [item for item in summary.get("files") or [] if item.get("status") == "error" or item.get("error")]
    if files:
        names = [item["relative_path"] for item in files]
        first = files[0]
        others = summary.get("files_read") or 0
        alerts.append(
            {
                "kind": "bad_file",
                "type": "文件读取失败",
                "scope": "、".join(names[:3]) + (" 等" if len(names) > 3 else ""),
                "count": len(files),
                "count_text": f"{len(files)} 个文件",
                "meaning": f"{first['relative_path']}：{first.get('error') or '无法读取'}",
                "advice": f"未参与分析，其余 {others} 个文件正常；修复后重跑。",
                "severity": "error",
            }
        )

    duplicates = int(quality.get("duplicates_removed") or 0)
    if duplicates:
        alerts.append(
            {
                "kind": "duplicate",
                "type": "重复行去重",
                "scope": "全部业务列",
                "count": duplicates,
                "count_text": f"{duplicates} 行",
                "meaning": f"{duplicates} 行完全重复已删除，保留首次出现。",
                "advice": "如属正常重复下单，请与业务确认口径后再引用行数。",
                "severity": "warning",
            }
        )

    missing = {key: int(value) for key, value in (quality.get("missing_filled") or {}).items() if int(value or 0)}
    if missing:
        names = sorted(missing, key=lambda key: missing[key], reverse=True)
        total = sum(missing.values())
        alerts.append(
            {
                "kind": "fill",
                "type": "缺失值填补",
                "scope": _join_names([labels.get(name, name) for name in names]),
                "count": total,
                "count_text": f"{total} 个值",
                "meaning": "缺失值用中位数填补，相关总量含估算成分。",
                "advice": "引用总量请注明含填补值。",
                "severity": "warning",
            }
        )

    outliers = {key: int(value) for key, value in (quality.get("outlier_counts") or {}).items() if int(value or 0)}
    if outliers:
        names = sorted(outliers, key=lambda key: outliers[key], reverse=True)
        total = sum(outliers.values())
        alerts.append(
            {
                "kind": "outlier",
                "type": "离群值标记",
                "scope": _join_names([labels.get(name, name) for name in names]),
                "count": total,
                "count_text": f"{total} 个值",
                "meaning": "按 IQR 规则标记为离群，未删除任何行。",
                "advice": "异常记录请单独核对，不要直接当噪声删除。",
                "severity": "info",
            }
        )

    ambiguity = [
        warning for warning in summary.get("warnings") or []
        if str(warning).startswith("[date_order_ambiguous]")
    ]
    if ambiguity:
        message = str(ambiguity[0]).split("]", 1)[-1].strip()
        if len(message) > 70:
            message = message[:69] + "…"
        alerts.append(
            {
                "kind": "date_order",
                "type": "日期存在歧义",
                "scope": "日期字段",
                "count": len(ambiguity),
                "count_text": f"{len(ambiguity)} 条提示",
                "meaning": message,
                "advice": "源系统是日/月/年时请改选「日/月/年」；无法确定时选「歧义时报错跳过」。",
                "severity": "warning",
            }
        )

    alerts.sort(key=lambda item: ALERT_ORDER.index(item["kind"]) if item["kind"] in ALERT_ORDER else 99)
    flags = count_flag_rows(summary.get("cleaned_path") or "")

    return {
        "available": True,
        "score": round(score, 1),
        "grade": grade,
        "colour": colour,
        "components": {
            str(key): round(float(value), 1)
            for key, value in (quality.get("quality_components") or {}).items()
        },
        "rows_in": int(quality.get("rows_in") or 0),
        "rows_out": int(quality.get("rows_out") or 0),
        "duplicates_removed": duplicates,
        "garbled_repaired": int(quality.get("garbled_repaired") or 0),
        "imputed_fields": [
            {"label": labels.get(key, key), "count": value}
            for key, value in sorted(missing.items(), key=lambda item: item[1], reverse=True)
        ],
        "outlier_fields": [
            {"label": labels.get(key, key), "count": value}
            for key, value in sorted(outliers.items(), key=lambda item: item[1], reverse=True)
        ],
        "imputed_rows": flags["imputed_rows"],
        "outlier_rows": flags["outlier_rows"],
        "alerts": alerts,
        "bad_files": files,
        "metrics_path": metrics_path,
    }


def quality_summary_text(quality: dict[str, Any] | None) -> str:
    """One-line coverage note for FR-5.3."""
    if not quality or not quality.get("available"):
        return ""
    return (
        f"填补 {sum(item['count'] for item in quality.get('imputed_fields') or [])} 个值"
        f"（{quality.get('imputed_rows', 0)} 行）"
        f" · 离群 {sum(item['count'] for item in quality.get('outlier_fields') or [])} 个值"
        f"（{quality.get('outlier_rows', 0)} 行）"
        " · 清洗数据带 __imputed / __outlier 标记"
    )


#: Compact labels so the one-line overview fits at 1024 px.
ALERT_SHORT: dict[str, str] = {
    "bad_file": "读取失败",
    "duplicate": "重复",
    "fill": "填补",
    "outlier": "离群",
    "date_order": "日期歧义",
}


def alerts_summary(quality: dict[str, Any] | None) -> str:
    """Compact 'at a glance' line so all five families are visible at once."""
    if not quality or not quality.get("available"):
        return "尚无质量数据。"
    alerts = quality.get("alerts") or []
    if not alerts:
        return "未发现质量问题：无填补、无离群、无重复、无坏文件、无日期歧义。"
    parts = [f"{ALERT_SHORT.get(alert['kind'], alert['type'])} {alert['count_text']}" for alert in alerts]
    return f"共 {len(alerts)} 类：" + " · ".join(parts)


# ---------------------------------------------------------------------------
# History (FR-6.1)
# ---------------------------------------------------------------------------


def run_history(settings: Settings, limit: int = 10) -> list[dict[str, Any]]:
    """Recent runs from ``logs/runs.jsonl`` rendered as table rows."""
    rows: list[dict[str, Any]] = []
    for item in read_run_history(settings.log_dir / "runs.jsonl", limit=max(1, limit)):
        status = str(item.get("status") or "")
        report = str(item.get("report_path") or "")
        report_path = ""
        if report:
            candidate = Path(report)
            candidate = candidate if candidate.is_absolute() else settings.project_root / candidate
            report_path = str(candidate) if candidate.is_file() else ""
        rows.append(
            {
                "time": str(item.get("started_at") or "").replace("T", " ")[:16],
                "target_date": str(item.get("target_date") or "—"),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "rows": item.get("rows_clean", item.get("rows_target", 0)),
                "report_path": report_path,
                "failure_reason": str(item.get("failure_reason") or ""),
                "run_id": str(item.get("run_id") or ""),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def result_availability(summary: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether the result buttons may open something, plus the reason if not."""
    if not summary:
        return False, "尚未运行，暂无结果可打开。"
    status = str(summary.get("status", ""))
    if status == "success":
        if summary.get("report_path") and summary.get("cleaned_path"):
            return True, ""
        return False, "运行成功但找不到报告或清洗数据，请查看日志。"
    return False, RESULT_UNAVAILABLE.get(status, "没有可用结果。")


def result_caption(summary: dict[str, Any] | None) -> str:
    """One-line description for the result area."""
    if not summary:
        return "尚未运行，暂无结果可打开。"
    if summary.get("status") == "success" and summary.get("report_path"):
        name = Path(str(summary["report_path"])).name
        return (
            f"{summary.get('target_date')} · 清洗后 {summary.get('rows_clean')} 行 · "
            f"耗时 {summary.get('duration_text')} · 报告 {name}"
        )
    label = summary.get("status_label") or summary.get("status")
    reason = summary.get("failure_reason") or RESULT_UNAVAILABLE.get(str(summary.get("status")), "")
    return f"结果：{label}" + (f" · {reason}" if reason else "")


# ---------------------------------------------------------------------------
# Stage 3: file management, preview, artefacts, settings, logs
# ---------------------------------------------------------------------------


def remove_source_file(settings: Settings, relative_path: str) -> Path:
    """Move one file out of the source folder into ``state/removed`` (FR-1.4).

    Moving (instead of deleting) keeps a recoverable copy and avoids destroying
    the operator's only copy of a file.
    """
    root = settings.source_dir.resolve()
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise ServiceError("只能移除数据源目录内的文件。")
    if not target.is_file():
        raise ServiceError(f"文件不存在：{relative_path}")
    removed_dir = settings.state_dir / "removed"
    try:
        removed_dir.mkdir(parents=True, exist_ok=True)
        destination = removed_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{target.name}"
        shutil.move(str(target), str(destination))
    except OSError as exc:
        raise ServiceError(friendly_error(exc)) from exc
    return destination


def import_files(settings: Settings, paths: list[str | Path]) -> dict[str, Any]:
    """Copy chosen files into the source folder (FR-1.5 / 选择文件…)."""
    root = settings.source_dir
    allowed = {str(extension).lower() for extension in settings.include_extensions}
    imported: list[str] = []
    skipped: list[dict[str, str]] = []
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceError(friendly_error(exc)) from exc

    for raw in paths:
        source = Path(raw).expanduser()
        if not source.is_file():
            skipped.append({"name": source.name, "reason": "文件不存在"})
            continue
        if source.suffix.lower() not in allowed:
            skipped.append({"name": source.name, "reason": "不支持的类型"})
            continue
        destination = root / source.name
        if destination.exists():
            skipped.append({"name": source.name, "reason": "同名文件已存在"})
            continue
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            skipped.append({"name": source.name, "reason": friendly_error(exc)})
            continue
        imported.append(source.name)
    return {"imported": imported, "skipped": skipped, "source_dir": str(root)}


def preview_source(settings: Settings, target: date) -> dict[str, Any]:
    """Read-only preview for FR-2.6, returned as plain data."""
    payload = core_preview(settings, target)
    return {
        "target_date": payload["target_date"],
        "baseline_date": payload["baseline_date"],
        "source_dir": payload["source_dir"],
        "files_scanned": payload["files_scanned"],
        "files_read": payload["files_read"],
        "tables": payload["tables"],
        "file_errors": payload["file_errors"],
        "warnings": list(payload.warnings),
        "has_target_rows": payload.has_target_rows,
        "empty_source": payload.empty_source,
    }


def preview_caption(preview: dict[str, Any] | None) -> str:
    """Operator wording for a preview payload (FR-2.6)."""
    if not preview:
        return "尚未预览。"
    if preview["empty_source"]:
        return "数据源为空：没有可读取的 Excel / CSV 文件。"
    if not preview["has_target_rows"]:
        return (
            f"{preview['target_date']} 没有命中任何数据行"
            f"（扫描 {preview['files_scanned']}、读取 {preview['files_read']} 个文件）"
            f"；此时运行会以「无数据」结束，不会生成报告。"
        )
    rows = sum(int(table.get("target_rows") or 0) for table in preview["tables"])
    issues = len(preview["warnings"])
    return (
        f"{preview['target_date']} 预计命中 {rows} 行"
        f"（扫描 {preview['files_scanned']}、读取 {preview['files_read']} 个文件，"
        f"兼容性提示 {issues} 条）；可以直接开始分析。"
    )


def artifact_entries(summary: dict[str, Any] | None) -> list[dict[str, str]]:
    """Everything the run produced, for the 「更多产物」 menu (FR-3.4)."""
    if not summary:
        return []
    cleaned = Path(summary["cleaned_path"]) if summary.get("cleaned_path") else None
    run_record = cleaned.parent / "run_record.json" if cleaned else None
    candidates = (
        ("报告 HTML", summary.get("report_path")),
        ("清洗数据 CSV", summary.get("cleaned_path")),
        ("指标 metrics.json", summary.get("metrics_path")),
        ("分析计划 analysis_plan.json", summary.get("plan_path")),
        ("运行记录 run_record.json", str(run_record) if run_record else ""),
    )
    entries: list[dict[str, str]] = []
    for label, path in candidates:
        if path and Path(path).is_file():
            entries.append({"label": label, "path": str(path)})
    return entries


def settings_paths(settings: Settings) -> dict[str, str]:
    """Paths shown in the settings dialog (FR-4.3)."""
    return {
        "数据源目录": str(settings.source_dir),
        "输出目录": str(settings.output_dir),
        "日志目录": str(settings.log_dir),
        "状态目录": str(settings.state_dir),
        "配置文件": str(settings.project_root / "config" / "config.json"),
    }


def _config_payload(settings: Settings) -> dict[str, Any]:
    path = settings.project_root / "config" / "config.json"
    example = settings.project_root / "config" / "config.example.json"
    for candidate in (path, example):
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                return loaded
    return {}


def save_paths(settings: Settings, source_dir: str, output_dir: str) -> Path:
    """Persist the two operator-visible paths (FR-4.3 / FR-4.4)."""
    source = str(source_dir or "").strip()
    output = str(output_dir or "").strip()
    if not source or not output:
        raise ValueError("数据源目录与输出目录都不能为空。")
    path = settings.project_root / "config" / "config.json"
    data = _config_payload(settings)
    data["source_dir"] = source
    data["output_dir"] = output
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)
    return path


def scheduler_status(settings: Settings) -> dict[str, Any]:
    """Installed state + next run of the daily task (FR-7.1)."""
    from ..scheduler import daemon_status, query_windows_task

    installed, detail = query_windows_task(settings)
    state = daemon_status(settings)
    return {
        "installed": bool(installed),
        "task_name": settings.scheduler.task_name,
        "schedule_time": settings.schedule_time,
        "next_run": str(state.get("next_run") or ""),
        "platform": str(state.get("platform") or ""),
        "detail": str(detail or "")[:400],
    }


def scheduler_caption(status: dict[str, Any]) -> str:
    """One line for the footer (FR-7.1)."""
    if status.get("installed"):
        next_run = status.get("next_run", "").replace("T", " ")[:16]
        return f"定时任务：已安装（每天 {status.get('schedule_time')}，下次 {next_run}）"
    return f"定时任务：未安装（每天 {status.get('schedule_time')} 未生效，可用命令行安装）"


def package_logs(settings: Settings) -> Path:
    """Bundle logs + environment into one zip for support (FR-8.3)."""
    import zipfile
    from importlib import metadata

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = settings.output_dir / f"InsightFlow_logs_{stamp}.zip"
    try:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            if settings.log_dir.is_dir():
                for item in sorted(settings.log_dir.rglob("*")):
                    if item.is_file():
                        archive.write(item, f"logs/{item.relative_to(settings.log_dir).as_posix()}")
            versions = []
            for package in ("pandas", "numpy", "openpyxl", "xlrd"):
                try:
                    versions.append(f"{package}={metadata.version(package)}")
                except metadata.PackageNotFoundError:
                    versions.append(f"{package}=未安装")
            archive.writestr(
                "environment.txt",
                "\n".join(
                    [
                        f"python={sys.version.split()[0]}",
                        f"executable={sys.executable}",
                        f"project_root={settings.project_root}",
                        f"date_order={settings.date_order}",
                        f"schedule_time={settings.schedule_time}",
                        *versions,
                    ]
                ),
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ServiceError(friendly_error(exc)) from exc
    return destination


def wizard_seen(settings: Settings) -> bool:
    """Has the first-run guide been dismissed? (FR-8.1)"""
    path = settings.state_dir / "gui_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("wizard_seen"))


def mark_wizard_seen(settings: Settings) -> None:
    path = settings.state_dir / "gui_state.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        data["wizard_seen"] = True
        atomic_write_json(path, data)
    except OSError:
        pass
