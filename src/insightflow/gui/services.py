"""Application services used by the desktop shell.

This is the **only** module under ``insightflow.gui`` allowed to import the core
package (pipeline / discovery / services).  Everything returned here is plain
data — dicts, lists, primitives — so ``app.py`` and ``worker.py`` never touch
``RunRecord``, pandas or the pipeline directly, and every business number still
comes from ``pipeline.execute()``.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..pipeline import execute
from ..utils import humanize_bytes, humanize_duration

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


class SourceUnavailable(RuntimeError):
    """Operator-facing error for a missing or unreadable source directory."""


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

    record = execute(settings, target_date=target, trigger="manual", force=force, on_step=observe)
    return summarise(settings, record, target)


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
        "warnings": list(record.warnings)[:20],
        "warning_count": record.warning_count(),
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
