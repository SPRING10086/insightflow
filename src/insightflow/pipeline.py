"""End-to-end orchestration: discover → read → clean → plan → analyze → render.

The pipeline owns the run lifecycle: lock, timings, idempotency, structured
artifacts and the run log.  Any failure is converted into a ``RunRecord`` so a
scheduled trigger never disappears silently.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import warnings
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .analysis import analyze
from .cleaning import clean_frame
from .config import Settings, load_settings
from .discovery import SourceNotFoundError, discover_files
from .logging_setup import append_jsonl, configure_logging, get_logger
from .models import (
    AnalysisPlan,
    CleaningReport,
    FieldSpec,
    ReadResult,
    RunRecord,
    STATUS_FAILED,
    STATUS_NO_DATA,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)
from .normalize import PeriodExtraction, extract_periods, unit_mix_issues
from .narrative import (
    NarrativeDraft,
    NarrativeError,
    build_facts,
    render_narrative,
    save_narrative_artifacts,
    validate_narrative,
)
from .planner import build_plan, effective_fields
from .readers import read_sources
from .report import ReportError, render_report
from .execution import (
    EXECUTION_VERSION,
    CodeArtifact,
    ExecutionResult,
    ExecutionSpec,
    reconcile,
    rule_execute,
    save_execution_artifacts,
)
from .schema import detect_schema
from .stats import LLMStats
from .planning import PLAN_VERSION, PlanArtifact, plan_diff, rule_plan_artifact, save_plan_artifacts
from .semantic import (
    ARTIFACT_VERSION,
    FieldSemantics,
    SemanticArtifact,
    TableSample,
    TableSemantics,
    diff_artifacts,
    fields_from_rule_schema,
    rule_artifact,
    save_artifacts,
)
from .utils import (
    atomic_write_json,
    ensure_dir,
    file_fingerprint,
    humanize_duration,
    jsonable,
    now_local,
    read_json,
    safe_relpath,
)


class PipelineError(RuntimeError):
    """Raised for a user-actionable pipeline failure."""


#: Optional observer of pipeline steps, used by the desktop shell to show live
#: stage progress.  It is a ContextVar so no step call site has to thread a
#: parameter through, and the default ``None`` keeps CLI and scheduled runs
#: byte-for-byte unchanged.
_STEP_OBSERVER: ContextVar[Callable[[str, str], None] | None] = ContextVar(
    "insightflow_step_observer", default=None
)


class _RunLock:
    # Task Scheduler kills a run after two hours.  Keep the stale window just
    # above that limit so an interrupted task does not block the next day's
    # manual run for another two hours.
    def __init__(self, settings: Settings, stale_minutes: int = 150):
        self.path = settings.state_dir / ".pipeline.lock"
        self.stale_seconds = max(60, stale_minutes * 60)
        self.acquired = False

    def acquire(self) -> bool:
        ensure_dir(self.path.parent)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "pid": os.getpid(),
                            "started_at": now_local().isoformat(timespec="seconds"),
                            "cwd": str(Path.cwd()),
                        },
                        handle,
                        ensure_ascii=False,
                    )
                self.acquired = True
                return True
            except FileExistsError:
                age = _lock_age(self.path)
                if age is not None and age > self.stale_seconds:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                return False
            except OSError:
                return False
        return False

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False


def _lock_age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def execute(
    settings: Settings | None = None,
    target_date: date | str | None = None,
    trigger: str = "manual",
    force: bool = False,
    now: datetime | None = None,
    source_dir: Path | str | None = None,
    on_step: Callable[[str, str], None] | None = None,
    semantic_provider: Any | None = None,
    plan_provider: Any | None = None,
    execution_provider: Any | None = None,
    narrative_provider: Any | None = None,
    adoption_decision: Any | None = None,
) -> RunRecord:
    """Run the full pipeline once and always return a structured record.

    ``on_step`` is an optional observer called as ``on_step(name, detail)`` after
    each internal step finishes; the desktop shell uses it for live progress.
    It must never influence the run: exceptions raised by an observer are
    swallowed, and ``None`` (the default) means zero behaviour change.

    ``semantic_provider`` enables the optional LLM semantic track.  When given,
    the rule schema and the provider schema are both persisted and diffed, but
    the report still consumes the rule schema (dual-track bypass).  ``None`` is
    the default and keeps every artifact byte-for-byte unchanged.

    ``plan_provider`` enables the optional stage-2 plan track.  It receives the
    stage-1 semantic schema (LLM track when available, rule track otherwise),
    writes plan/rule/LLM/diff artifacts, and never drives the report.
    """
    settings = settings or load_settings()
    settings.ensure_runtime_dirs()
    logger = configure_logging(settings)
    started_at = now or now_local(settings.timezone)
    run_id = _make_run_id(started_at)
    record = RunRecord(
        run_id=run_id,
        trigger=trigger,
        status=STATUS_FAILED,
        target_date="",
        baseline_date=None,
        started_at=started_at.isoformat(timespec="seconds"),
    )
    source_root = Path(source_dir).expanduser().resolve() if source_dir else settings.source_dir
    if not source_root.exists():
        record.failure_reason = f"数据源目录不存在: {source_root}"
        record.error_type = "SourceNotFoundError"
        _finalize(settings, record, logger)
        return record

    lock = _RunLock(settings)
    if not lock.acquire():
        record.status = STATUS_SKIPPED
        record.failure_reason = "另一个 InsightFlow 运行正在执行，本次触发已跳过（防止并发写坏报告）。"
        record.warnings = [record.failure_reason]
        _finalize(settings, record, logger)
        return record

    state: dict[str, Any] = {}
    read_result: ReadResult | None = None
    observer_token = _STEP_OBSERVER.set(on_step)
    try:
        target = _resolve_target_date(settings, target_date, started_at)
        baseline = settings.baseline_date(target)
        record.target_date = target.isoformat()
        record.baseline_date = baseline.isoformat() if baseline else None
        record.metadata.update(
            {
                "source_dir": safe_relpath(source_root, settings.project_root),
                "target_date_mode": settings.target_date_mode,
                "comparison_enabled": settings.comparison.enabled,
                "timezone": settings.timezone,
            }
        )

        # 1) Discovery
        step_start = time.perf_counter()
        files = discover_files(settings, source_root)
        _add_step(
            record,
            "扫描数据源",
            step_start,
            f"{len(files)} 个候选文件；递归={settings.recursive_scan}；扩展名={', '.join(settings.include_extensions)}",
        )
        if not files:
            record.status = STATUS_NO_DATA
            record.failure_reason = f"数据源目录中没有可读取的 Excel/CSV 文件: {source_root}"
            record.warnings = [record.failure_reason]
            logger.warning("run %s: %s", run_id, record.failure_reason)
            return record

        record.files_scanned = len(files)
        record.fingerprint = file_fingerprint([source.path for source in files], source_root)
        record.metadata["fingerprint"] = record.fingerprint

        # 2) Read
        step_start = time.perf_counter()
        read_result = read_sources(files, settings, source_root)
        record.files = [jsonable(report) for report in read_result.file_reports]
        _add_step(
            record,
            "读取与解析",
            step_start,
            f"成功读取 {read_result.files_read}/{read_result.files_scanned} 个文件，"
            f"解析 {len(read_result.tables)} 个数据表。",
        )
        _merge_warnings(record, [issue.as_text() for issue in read_result.warnings if issue.severity in {"warning", "error"}])

        if read_result.files_read == 0:
            record.status = STATUS_FAILED
            record.failure_reason = "没有成功读取任何数据表。请检查文件是否损坏、加密或为纯空文件。"
            logger.error("run %s: %s", run_id, record.failure_reason)
            return record

        # 3) Schema + date extraction
        step_start = time.perf_counter()
        prepared = _prepare(
            read_result=read_result,
            target=target,
            baseline=baseline,
            settings=settings,
        )
        record.rows_target = len(prepared["target_raw"])
        record.rows_baseline = len(prepared["baseline_raw"])
        record.metadata["date_columns"] = prepared["date_columns"]
        record.metadata["table_count"] = len(read_result.tables)
        _merge_warnings(record, prepared["warnings"])
        _update_file_reports(read_result, prepared["extractions"])
        record.files = [jsonable(report) for report in read_result.file_reports]
        record.files_read = read_result.files_read
        _add_step(
            record,
            "字段识别与日期筛选",
            step_start,
            f"命中目标日 {record.rows_target} 行，基准日 {record.rows_baseline} 行；"
            f"识别 {len(prepared['fields'])} 个标准字段。",
        )

        if record.rows_target == 0:
            record.status = STATUS_NO_DATA
            record.failure_reason = f"所有可用文件中都没有 {target.isoformat()} 的数据。"
            logger.warning("run %s: %s", run_id, record.failure_reason)
            return record

        # 4) Idempotency: identical fingerprint + existing report.  The check
        # happens after date extraction so skipped runs still log accurate
        # row counts and per-file target/baseline hits.
        state = read_json(settings.state_dir / "index.json", default={}) or {}
        if not isinstance(state, dict):
            state = {}
        previous = state.get(target.isoformat()) if isinstance(state, dict) else None
        if previous and not force:
            previous_path = _resolve_path(settings.project_root, previous.get("report_path"))
            if (
                previous.get("fingerprint") == read_result.fingerprint
                and previous_path
                and previous_path.is_file()
            ):
                record.status = STATUS_SKIPPED
                record.report_path = safe_relpath(previous_path, settings.project_root)
                record.report_generated = False
                record.rows_clean = int(previous.get("rows_clean", 0) or 0)
                record.metadata["reused_report"] = True
                record.failure_reason = "数据源指纹与上次成功运行一致，复用已有报告；使用 --force 可强制重算。"
                record.warnings = [record.failure_reason]
                logger.info("run %s skipped: identical fingerprint, report=%s", run_id, record.report_path)
                return record

        # 4.5) Optional LLM semantic bypass (dual-track).  Built only after the
        # idempotency check so a skipped run never spends API budget.
        semantic_bundle: dict[str, Any] | None = None
        if semantic_provider is not None:
            step_start = time.perf_counter()
            semantic_bundle = _build_semantic_bundle(semantic_provider, prepared, record, logger)
            artifact = semantic_bundle["provider"]
            _add_step(
                record,
                "语义 Schema（旁路）",
                step_start,
                f"{len(artifact.tables)} 张表；来源={artifact.source_kind}；"
                f"角色一致率={semantic_bundle['diff']['role_agreement']:.2%}；"
                f"成本 {artifact.cost_cny:.4f} 元。",
            )

        # 5) Cleaning
        step_start = time.perf_counter()
        clean_fields = {
            key: field
            for key, field in prepared["fields"].items()
            if field.kind not in {"date", "timestamp"}
            and (key in prepared["target_raw"].columns or key in prepared["baseline_raw"].columns)
        }
        cleaned, cleaning_report = clean_frame(
            prepared["target_raw"],
            clean_fields,
            target,
            settings.outlier_iqr_k,
            unparseable_rows=prepared["unparseable_rows"],
        )
        baseline_clean = pd.DataFrame()
        if not prepared["baseline_raw"].empty:
            baseline_clean, _ = clean_frame(
                prepared["baseline_raw"],
                clean_fields,
                baseline or target,
                settings.outlier_iqr_k,
                unparseable_rows=0,
            )
        if cleaned.empty:
            record.status = STATUS_FAILED
            record.failure_reason = f"{target.isoformat()} 命中 {record.rows_target} 行，但清洗后没有有效记录。"
            logger.error("run %s: %s", run_id, record.failure_reason)
            return record
        record.rows_clean = len(cleaned)
        analysis_fields = effective_fields(
            {key: field for key, field in clean_fields.items() if key in cleaned.columns},
            cleaned,
        )
        if prepared.get("header_repairs"):
            cleaning_report.garbled_repaired += int(prepared["header_repairs"])
        _add_step(
            record,
            "数据清洗",
            step_start,
            f"{cleaning_report.rows_in} → {cleaning_report.rows_out} 行；去重 {cleaning_report.duplicates_removed}；"
            f"质量分 {cleaning_report.quality_score:.1f}/100。",
        )

        # 6) Plan
        step_start = time.perf_counter()
        plan = build_plan(cleaned, baseline_clean, analysis_fields, target, baseline, settings)
        _add_step(
            record,
            "生成分析计划",
            step_start,
            f"{plan.dataset_kind}；主指标={plan.primary_metric or '记录数'}；"
            f"主维度={plan.primary_dimension or '无'}；启用 {sum(1 for s in plan.steps if s.enabled)} 项。",
        )

        # 6.5) Optional LLM plan bypass (dual-track, never drives the report).
        plan_bundle: dict[str, Any] | None = None
        if plan_provider is not None:
            step_start = time.perf_counter()
            plan_bundle = _build_plan_bundle(
                plan_provider,
                semantic_bundle,
                prepared,
                analysis_fields,
                plan,
                cleaning_report,
                settings,
                record,
                logger,
            )
            artifact = plan_bundle["llm"]
            _add_step(
                record,
                "分析计划（旁路）",
                step_start,
                f"来源={artifact.source_kind}；指标 Jaccard={plan_bundle['diff']['metrics']['jaccard']:.2f}；"
                f"成本 {artifact.cost_cny:.4f} 元。",
            )

        # 7) Analysis
        step_start = time.perf_counter()
        result = analyze(
            cleaned,
            baseline_clean,
            analysis_fields,
            plan,
            cleaning_report,
            settings,
        )
        _add_step(
            record,
            "指标计算",
            step_start,
            f"生成 {len(result.kpis)} 个 KPI、{len(result.insights)} 条结论、{len(result.charts)} 个图表。",
        )

        # 7.5) Optional controlled execution (dual-track; never drives the report).
        execution_bundle: dict[str, Any] | None = None
        if execution_provider is not None and plan.primary_metric:
            step_start = time.perf_counter()
            execution_bundle = _build_execution_bundle(
                execution_provider,
                cleaned,
                analysis_fields,
                plan,
                semantic_bundle,
                record,
                logger,
            )
            result_llm = execution_bundle["llm"]
            _add_step(
                record,
                "受控执行（旁路）",
                step_start,
                f"来源={result_llm.source_kind}；对账{'通过' if execution_bundle['diff']['accepted'] else '未通过'}；"
                f"成本 {result_llm.cost_cny:.4f} 元。",
            )

        # 7.6) Optional LLM narrative (escaped text only; numbers locally rendered).
        narrative_bundle: dict[str, Any] | None = None
        if narrative_provider is not None:
            step_start = time.perf_counter()
            narrative_bundle = _build_narrative_bundle(
                narrative_provider,
                result,
                plan,
                cleaning_report,
                analysis_fields,
                cleaned,
                record,
                logger,
            )
            if narrative_bundle is not None:
                rendered = narrative_bundle["rendered"]
                _add_step(
                    record,
                    "叙事（旁路）",
                    step_start,
                    f"{len(rendered.get('key_points') or []) + 1} 段；占位符 {len(rendered.get('mapping') or {})} 处；"
                    f"成本 {narrative_bundle['meta'].get('cost_cny', 0):.4f} 元。",
                )
            else:
                _add_step(record, "叙事（旁路）", step_start, "已降级为规则洞察（原因见 metadata.narrative）")

        # 8) Artifacts + report        step_start = time.perf_counter()
        day_dir = ensure_dir(settings.output_dir / target.isoformat())
        _write_artifacts(day_dir, result, plan, cleaned, cleaning_report, analysis_fields)
        if semantic_bundle is not None:
            _persist_semantic(day_dir, semantic_bundle, record)
        if plan_bundle is not None:
            _persist_plans(day_dir, plan_bundle, record)
        if execution_bundle is not None:
            _persist_execution(day_dir, execution_bundle, record)
        if narrative_bundle is not None:
            _persist_narrative(day_dir, narrative_bundle, record)
        if any(
            item is not None
            for item in (semantic_provider, plan_provider, execution_provider, narrative_provider)
        ):
            record.metadata["llm_stats"] = _collect_llm_stats(
                (semantic_provider, plan_provider, execution_provider, narrative_provider),
                semantic_bundle,
                plan_bundle,
                execution_bundle,
                narrative_bundle,
            ).to_dict()
        # Stage 6a: record the resolved adoption decision only when it is not the
        # default (all shadow), so the default output stays byte-identical.
        if adoption_decision is not None and (
            getattr(adoption_decision, "force_rules", False) or adoption_decision.any_non_shadow()
        ):
            record.metadata["adoption"] = adoption_decision.to_dict()
        report_path = day_dir / f"InsightFlow_{target.isoformat()}_{run_id}.html"
        record.finished_at = now_local(settings.timezone).isoformat(timespec="seconds")
        record.duration_ms = max(0, int((now_local(settings.timezone) - started_at).total_seconds() * 1000))
        render_report(
            result=result,
            plan=plan,
            cleaning=cleaning_report,
            record=record,
            read_result=read_result,
            fields=analysis_fields,
            settings=settings,
            output_path=report_path,
            narrative=narrative_bundle["rendered"] if narrative_bundle else None,
        )
        latest_path = settings.output_dir / "latest.html"
        try:
            shutil.copyfile(report_path, latest_path)
        except OSError as exc:
            record.warnings.append(f"无法写入 latest.html: {exc}")
        record.report_generated = True
        record.report_path = safe_relpath(report_path, settings.project_root)
        record.status = STATUS_SUCCESS
        record.metrics = {
            "target_totals": result.target_totals,
            "baseline_totals": result.baseline_totals,
            "summary": result.summary,
            "quality_score": cleaning_report.quality_score,
            "charts": len(result.charts),
        }
        record.metadata["latest_report"] = safe_relpath(latest_path, settings.project_root)
        _add_step(
            record,
            "渲染离线报告",
            step_start,
            f"{report_path.name}；单文件 {report_path.stat().st_size / 1024 / 1024:.2f} MB；"
            f"含 {len(result.charts)} 个内联 ECharts 图表。",
        )

        _update_index(settings, target, read_result.fingerprint, record)
        logger.info(
            "run %s success: target=%s rows=%s report=%s",
            run_id,
            target.isoformat(),
            record.rows_clean,
            record.report_path,
        )
        return record
    except SourceNotFoundError as exc:
        record.status = STATUS_FAILED
        record.error_type = "SourceNotFoundError"
        record.failure_reason = str(exc)
        logger.error("run %s source error: %s", run_id, exc)
        return record
    except ReportError as exc:
        record.status = STATUS_FAILED
        record.error_type = "ReportError"
        record.failure_reason = f"报告渲染失败: {exc}"
        logger.exception("run %s report error", run_id)
        return record
    except Exception as exc:  # noqa: BLE001 - boundary for scheduled operation
        record.status = STATUS_FAILED
        record.error_type = type(exc).__name__
        record.failure_reason = f"{type(exc).__name__}: {exc}"
        logger.exception("run %s failed", run_id)
        return record
    finally:
        _STEP_OBSERVER.reset(observer_token)
        try:
            if read_result is not None:
                record.files = [jsonable(report) for report in read_result.file_reports]
                record.files_read = read_result.files_read
                record.files_scanned = read_result.files_scanned
        except Exception:
            pass
        if record.status == STATUS_FAILED:
            record.report_generated = False
        lock.release()
        _finalize(settings, record, logger)
        if record.report_generated and record.report_path:
            try:
                day_dir = (settings.project_root / record.report_path).parent
                atomic_write_json(day_dir / "run_record.json", record)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Semantic dual-track (optional, injected provider)
# ---------------------------------------------------------------------------


def _build_semantic_bundle(
    provider: Any,
    prepared: dict[str, Any],
    record: RunRecord,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Ask the provider for semantics, fall back to rule semantics per table."""
    table_schemas = prepared.get("table_schemas") or []
    rule = rule_artifact(table_schemas)
    tables: list[TableSemantics] = []
    spent = 0.0
    latency = 0
    any_llm = False
    any_fallback = False
    prompt_version = str(getattr(provider, "prompt_version", "") or "")
    provider_name = str(getattr(provider, "name", "llm"))

    for table, schema in table_schemas:
        # The LLM track must see every raw *business* column: the rule schema
        # drops fields (e.g. only keeps a few dimensions), and hiding those
        # columns from the model would make the "flexible" bypass as rigid as
        # the rules. Internal metadata columns (``__imputed``, ``__outlier``,
        # ``__negative``) are bookkeeping, never business fields, so they are
        # filtered out of the sampling schema.
        sample = TableSample(
            name=table.source.relative_path,
            source=table.source.relative_path,
            sheet=table.sheet_label,
            field_names=[str(column) for column in table.frame.columns if not str(column).startswith("__")],
            rows=table.frame.head(20).to_dict("records"),
        )
        try:
            result = provider.describe(sample)
        except Exception as exc:  # noqa: BLE001 - every failure degrades, never aborts
            any_fallback = True
            message = f"{type(exc).__name__}: {exc}"
            record.warnings.append(f"[semantic_fallback] {sample.name}: {message}")
            logger.warning("semantic provider fallback %s: %s", sample.name, message)
            tables.append(
                TableSemantics(
                    table=sample.name,
                    source=sample.source,
                    sheet=sample.sheet,
                    provider=provider_name,
                    source_kind="fallback",
                    fields=fields_from_rule_schema(schema),
                    error=message[:300],
                )
            )
            continue

        any_llm = True
        spent += float(getattr(result, "cost_cny", 0.0) or 0.0)
        latency += int(getattr(result, "latency_ms", 0) or 0)
        prompt_version = prompt_version or str(getattr(result, "prompt_version", "") or "")
        tables.append(
            TableSemantics(
                table=sample.name,
                source=sample.source,
                sheet=sample.sheet,
                provider=provider_name,
                source_kind=str(getattr(result, "source_kind", "llm")),
                fields=list(result.fields),
                cost_cny=float(getattr(result, "cost_cny", 0.0) or 0.0),
                latency_ms=int(getattr(result, "latency_ms", 0) or 0),
                anomalies=list(getattr(result, "anomalies", []) or []),
            )
        )

    if any_llm and any_fallback:
        source_kind = "mixed"
    elif any_llm:
        source_kind = "llm"
    else:
        source_kind = "fallback"

    artifact = SemanticArtifact(
        version=ARTIFACT_VERSION,
        prompt_version=prompt_version or "unknown",
        generated_at=now_local().isoformat(timespec="seconds"),
        provider=provider_name,
        source_kind=source_kind,
        tables=tables,
        cost_cny=spent,
        latency_ms=latency,
    )
    return {"rule": rule, "provider": artifact, "diff": diff_artifacts(rule, artifact)}


def _persist_semantic(day_dir: Path, bundle: dict[str, Any], record: RunRecord) -> None:
    """Write the three artifacts and surface a compact summary in the run record."""
    written = save_artifacts(day_dir, bundle["rule"], bundle["provider"], bundle["diff"])
    artifact = bundle["provider"]
    diff = bundle["diff"]
    record.metadata["semantic_schema"] = {
        "provider": artifact.provider,
        "source_kind": artifact.source_kind,
        "prompt_version": artifact.prompt_version,
        "role_agreement": diff["role_agreement"],
        "unit_agreement": diff["unit_agreement"],
        "key_role_errors": len(diff["key_role_errors"]),
        "cost_cny": round(artifact.cost_cny, 6),
        "latency_ms": artifact.latency_ms,
        "files": [path.name for path in written],
    }


def _semantic_for_plan(semantic_bundle: dict[str, Any] | None, prepared: dict[str, Any], generated_at: str):
    """The LLM semantic artifact if the stage-1 bypass ran, else the rule one."""
    if semantic_bundle:
        return semantic_bundle.get("provider") or semantic_bundle.get("rule")
    return rule_artifact(prepared.get("table_schemas") or [], generated_at=generated_at)


def _build_plan_bundle(
    plan_provider: Any,
    semantic_bundle: dict[str, Any] | None,
    prepared: dict[str, Any],
    fields: dict[str, Any],
    rule_plan: Any,
    cleaning: Any,
    settings: Any,
    record: RunRecord,
    logger: logging.Logger,
) -> dict[str, Any]:
    """Build rule plan artifact, ask the provider, and fall back on any failure."""
    generated_at = now_local().isoformat(timespec="seconds")
    rule = rule_plan_artifact(rule_plan, fields, cleaning, settings, generated_at=generated_at)

    source = _semantic_for_plan(semantic_bundle, prepared, generated_at)
    seen: dict[str, Any] = {}
    ordered: list[Any] = []
    for table in source.tables:
        for item in table.fields:
            if item.name not in seen:
                seen[item.name] = item
                ordered.append(item)
    anomalies = [
        {"table": table.table, **item}
        for table in source.tables
        for item in (getattr(table, "anomalies", None) or [])
    ]

    try:
        result = plan_provider.plan(ordered, anomalies, {"table_count": len(source.tables)})
        llm = PlanArtifact(
            version=PLAN_VERSION,
            prompt_version=result.prompt_version,
            generated_at=generated_at,
            provider=getattr(plan_provider, "name", "llm"),
            source_kind=result.source_kind,
            table="dataset",
            plan=result.draft,
            cost_cny=float(result.cost_cny or 0.0),
            latency_ms=int(result.latency_ms or 0),
        )
    except Exception as exc:  # noqa: BLE001 - every failure degrades, never aborts
        message = f"{type(exc).__name__}: {exc}"
        record.warnings.append(f"[plan_fallback] {message}")
        logger.warning("plan provider fallback: %s", message)
        llm = PlanArtifact(
            version=PLAN_VERSION,
            prompt_version="rules",
            generated_at=generated_at,
            provider=getattr(plan_provider, "name", "llm"),
            source_kind="fallback",
            table="dataset",
            plan=rule.plan,
            error=message[:300],
        )

    return {"rule": rule, "llm": llm, "diff": plan_diff(rule, llm)}


def _persist_plans(day_dir: Path, bundle: dict[str, Any], record: RunRecord) -> None:
    written = save_plan_artifacts(day_dir, bundle["rule"], bundle["llm"], bundle["diff"])
    artifact = bundle["llm"]
    diff = bundle["diff"]
    record.metadata["plan"] = {
        "provider": artifact.provider,
        "source_kind": artifact.source_kind,
        "prompt_version": artifact.prompt_version,
        "metrics_jaccard": diff["metrics"]["jaccard"],
        "dimensions_jaccard": diff["dimensions"]["jaccard"],
        "charts_jaccard": diff["charts"]["jaccard"],
        "cost_cny": round(artifact.cost_cny, 6),
        "latency_ms": artifact.latency_ms,
        "files": [path.name for path in written],
    }


def _build_execution_bundle(
    provider: Any,
    frame: Any,
    fields: dict[str, Any],
    plan: Any,
    semantic_bundle: dict[str, Any] | None,
    record: RunRecord,
    logger: logging.Logger,
) -> dict[str, Any]:
    metric_field = fields.get(getattr(plan, "primary_metric", "") or "")
    if metric_field is None:
        raise PipelineError("受控执行缺少主指标字段")
    dimension = fields.get(getattr(plan, "primary_dimension", "") or "")
    # The cleaned frame is keyed by canonical names (revenue, orders...), so the
    # execution spec and the generated code must use those, not the raw headers.
    spec = ExecutionSpec(
        table="dataset",
        metric=getattr(plan, "primary_metric", "") or "",
        metric_label=metric_field.label,
        dimension=(getattr(plan, "primary_dimension", "") or "") if dimension else "",
        dimension_label=dimension.label if dimension else "",
    )
    allowed = [str(column) for column in frame.columns if not str(column).startswith("__")]
    rule = rule_execute(spec, frame)

    semantic_fields = [
        FieldSemantics(
            name=key,
            role=item.kind if item.kind in {"dimension", "metric", "date", "id"} else "text",
            unit=item.unit,
            meaning=item.label,
            confidence=1.0,
            source="cleaned",
        )
        for key, item in fields.items()
        if key in allowed
    ]

    try:
        result = provider.execute(spec, frame, allowed, semantic_fields)
    except Exception as exc:  # noqa: BLE001 - every failure degrades, never aborts
        message = f"{type(exc).__name__}: {exc}"
        record.warnings.append(f"[execution_fallback] {message}")
        logger.warning("execution fallback: %s", message)
        result = ExecutionResult(
            metrics=dict(rule.metrics),
            groups=rule.groups,
            source_kind="fallback",
            memory_guard="none",
            error=message[:300],
        )
    return {
        "spec": spec,
        "rule": rule,
        "code": getattr(provider, "last_code", None),
        "llm": result,
        "diff": reconcile(rule, result),
    }


def _persist_execution(day_dir: Path, bundle: dict[str, Any], record: RunRecord) -> None:
    written = save_execution_artifacts(
        day_dir, bundle["spec"], bundle["rule"], bundle["code"], bundle["llm"], bundle["diff"]
    )
    result = bundle["llm"]
    diff = bundle["diff"]
    record.metadata["execution"] = {
        "source_kind": result.source_kind,
        "memory_guard": result.memory_guard,
        "accepted": bool(diff["accepted"]),
        "problems": list(diff["problems"]),
        "total_rule": diff["total_rule"],
        "total_llm": diff["total_llm"],
        "cost_cny": round(result.cost_cny, 6),
        "latency_ms": result.latency_ms,
        "files": [path.name for path in written],
    }


def _build_narrative_bundle(
    provider: Any,
    result: Any,
    plan: Any,
    cleaning: Any,
    fields: dict[str, Any],
    frame: Any,
    record: RunRecord,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    facts = build_facts(result, plan, cleaning, fields)
    chart_ids = [str(chart.chart_id) for chart in getattr(result, "charts", [])]
    labels = [str(getattr(spec, "label", key)) for key, spec in fields.items()]
    samples = [
        {str(column): row.get(column) for column in frame.columns if not str(column).startswith("__")}
        for row in frame.head(10).to_dict("records")
    ]
    try:
        draft = provider.generate(sorted(facts), labels, chart_ids, samples)
        validate_narrative(draft, facts, chart_ids)
        rendered = render_narrative(draft, facts)
    except Exception as exc:  # noqa: BLE001 - any failure falls back to rule insights
        message = f"{type(exc).__name__}: {exc}"
        record.warnings.append(f"[narrative_fallback] {message}")
        logger.warning("narrative fallback: %s", message)
        record.metadata["narrative"] = {
            "source_kind": "fallback",
            "reason": message[:300],
            "prompt_version": str(getattr(provider, "prompt_version", "")),
        }
        return None

    meta = dict(getattr(provider, "last_meta", {}) or {})
    meta.setdefault("provider", str(getattr(provider, "name", "llm")))
    meta.setdefault("prompt_version", str(getattr(provider, "prompt_version", "")))
    return {
        "facts": facts,
        "draft": draft,
        "rendered": rendered,
        "rule": [{"title": item.title, "body": item.body} for item in result.insights],
        "meta": meta,
    }


def _persist_narrative(day_dir: Path, bundle: dict[str, Any], record: RunRecord) -> None:
    written = save_narrative_artifacts(
        day_dir, bundle["facts"], bundle["draft"], bundle["rendered"], bundle["meta"]
    )
    rule_path = day_dir / "narrative_rule.json"
    rule_path.write_text(
        json.dumps({"schema_version": "1.0", "insights": bundle["rule"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written.append(rule_path)
    meta = bundle["meta"]
    record.metadata["narrative"] = {
        "source_kind": meta.get("source_kind", "llm"),
        "provider": meta.get("provider", "llm"),
        "prompt_version": meta.get("prompt_version", ""),
        "cost_cny": round(float(meta.get("cost_cny") or 0), 6),
        "latency_ms": int(meta.get("latency_ms") or 0),
        "placeholders": len(bundle["rendered"].get("mapping") or {}),
        "files": [path.name for path in written],
    }


def _collect_llm_stats(
    providers: tuple[Any, Any, Any, Any],
    semantic_bundle: dict[str, Any] | None,
    plan_bundle: dict[str, Any] | None,
    execution_bundle: dict[str, Any] | None,
    narrative_bundle: dict[str, Any] | None,
) -> LLMStats:
    """Merge provider counters and count fallbacks from the four bypass artifacts."""
    stats = LLMStats()
    for provider in providers:
        stats.merge(getattr(provider, "stats", None))

    for bundle, pick in (
        (semantic_bundle, lambda item: item["provider"].source_kind),
        (plan_bundle, lambda item: item["llm"].source_kind),
        (execution_bundle, lambda item: item["llm"].source_kind),
    ):
        if bundle is not None and pick(bundle) == "fallback":
            stats.record_fallback()

    if narrative_bundle is None:
        if providers[3] is not None:
            stats.record_fallback()
    elif (narrative_bundle.get("meta") or {}).get("source_kind") == "fallback":
        stats.record_fallback()
    return stats


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def _prepare(
    read_result: ReadResult,
    target: date,
    baseline: date | None,
    settings: Settings,
) -> dict[str, Any]:
    target_frames: list[pd.DataFrame] = []
    baseline_frames: list[pd.DataFrame] = []
    fields: dict[str, FieldSpec] = {}
    date_columns: dict[str, str] = {}
    warnings: list[str] = []
    extractions: list[tuple[Any, PeriodExtraction]] = []
    table_schemas: list[tuple[Any, Any]] = []
    unparseable_rows = 0
    header_repairs = 0

    for table in read_result.tables:
        schema = detect_schema(table.frame, settings.header_scan_rows, date_order=settings.date_order)
        table_schemas.append((table, schema))
        extraction = extract_periods(table, schema, target, baseline, date_order=settings.date_order)
        extractions.append((table, extraction))
        header_repairs += int(getattr(table, "header_repairs", 0) or 0)
        key = f"{table.source.relative_path} :: {table.sheet_label}"
        if extraction.selected_column:
            date_columns[key] = extraction.selected_column
        if extraction.target.empty and extraction.baseline.empty:
            # Keep the date-order warning even when the wrong assumption
            # produced zero target rows: this is exactly when the operator
            # needs to see it.  Other cover-sheet warnings stay quiet.
            for issue in schema.issues:
                if issue.code == "date_order_ambiguous":
                    warnings.append(issue.as_text())
            continue
        for issue in schema.issues:
            if issue.severity in {"warning", "error"}:
                warnings.append(issue.as_text())
        # P2-2: mixed header/inline units are converted per value; warn anyway.
        for issue in unit_mix_issues(table.frame, schema, table):
            warnings.append(issue.as_text())
        if not extraction.target.empty:
            target_frames.append(extraction.target)
            unparseable_rows += extraction.unparseable_rows
        if not extraction.baseline.empty:
            baseline_frames.append(extraction.baseline)
        for issue in extraction.issues:
            if issue.severity in {"warning", "error"}:
                warnings.append(issue.as_text())
        for canonical, field in schema.fields.items():
            if canonical not in extraction.target.columns and canonical not in extraction.baseline.columns:
                continue
            existing = fields.get(canonical)
            if existing is None or _field_rank(field) > _field_rank(existing):
                fields[canonical] = field

    target_raw = _concat_frames(target_frames)
    baseline_raw = _concat_frames(baseline_frames)
    return {
        "target_raw": target_raw,
        "baseline_raw": baseline_raw,
        "fields": fields,
        "date_columns": date_columns,
        "warnings": _dedupe(warnings),
        "extractions": extractions,
        "table_schemas": table_schemas,
        "unparseable_rows": unparseable_rows,
        "header_repairs": header_repairs,
    }


def _field_rank(field: FieldSpec) -> tuple[float, float, int]:
    richness = field.unique_count if field.kind == "dimension" else field.parse_rate
    return (field.coverage, float(richness), field.priority)


def _concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame()
    nonempty = [frame for frame in frames if not frame.empty]
    if not nonempty:
        return pd.DataFrame()
    if len(nonempty) == 1:
        return nonempty[0].reset_index(drop=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        return pd.concat(nonempty, ignore_index=True, sort=False)


def _update_file_reports(read_result: ReadResult, extractions: list[tuple[Any, PeriodExtraction]]) -> None:
    report_map = {report.relative_path: report for report in read_result.file_reports}
    for table, extraction in extractions:
        report = report_map.get(table.source.relative_path)
        if not report:
            continue
        report.rows_target += len(extraction.target)
        report.rows_baseline += len(extraction.baseline)
        if len(extraction.target) or len(extraction.baseline):
            report.status = "read"


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def _write_artifacts(
    day_dir: Path,
    result: Any,
    plan: AnalysisPlan,
    cleaned: pd.DataFrame,
    cleaning: CleaningReport,
    fields: dict[str, FieldSpec],
) -> None:
    atomic_write_json(day_dir / "analysis_plan.json", plan)
    metrics_payload = {
        "target_date": result.target_date,
        "baseline_date": result.baseline_date,
        "summary": result.summary,
        "kpis": [jsonable(kpi) for kpi in result.kpis],
        "target_totals": result.target_totals,
        "baseline_totals": result.baseline_totals,
        "correlation": result.correlation,
        "quality": jsonable(cleaning),
        "charts": [{"id": chart.chart_id, "title": chart.title, "type": chart.chart_type} for chart in result.charts],
    }
    atomic_write_json(day_dir / "metrics.json", metrics_payload)
    columns = ["__date", "__hour"]
    columns.extend(key for key, field in fields.items() if field.kind == "metric" and key in cleaned.columns)
    columns.extend(key for key, field in fields.items() if field.kind == "dimension" and key in cleaned.columns)
    columns.extend(key for key, field in fields.items() if field.kind == "id" and key in cleaned.columns)
    columns.extend(
        [
            "__outlier",
            "__negative",
            "__imputed",
            "__source_file",
            "__source_sheet",
            "__source_row",
        ]
    )
    columns = [column for column in dict.fromkeys(columns) if column in cleaned.columns]
    export = cleaned[columns].copy()
    if "__date" in export.columns:
        export["__date"] = export["__date"].map(lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value))
    export.to_csv(day_dir / "cleaned_data.csv", index=False, encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# State, logs and helpers
# ---------------------------------------------------------------------------


def _resolve_target_date(settings: Settings, value: date | str | None, now: datetime) -> date:
    if value is None:
        return settings.target_date(now.date())
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise PipelineError(f"目标日期格式应为 YYYY-MM-DD: {value!r}") from exc


def _make_run_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"


def _add_step(record: RunRecord, name: str, started: float, detail: str, status: str = "ok") -> None:
    record.steps.append(
        {
            "name": name,
            "status": status,
            "detail": detail,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    )
    observer = _STEP_OBSERVER.get()
    if observer is not None:
        try:
            observer(name, detail)
        except Exception:  # noqa: BLE001 - an observer must never break a run
            pass


def _merge_warnings(record: RunRecord, warnings: list[str]) -> None:
    existing = list(record.warnings)
    for warning in warnings:
        if warning and warning not in existing:
            existing.append(warning)
    record.warnings = existing[:80]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _resolve_path(root: Path, value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else (root / path)


def _update_index(settings: Settings, target: date, fingerprint: str, record: RunRecord) -> None:
    index_path = settings.state_dir / "index.json"
    index = read_json(index_path, default={}) or {}
    if not isinstance(index, dict):
        index = {}
    index[target.isoformat()] = {
        "fingerprint": fingerprint,
        "report_path": record.report_path,
        "run_id": record.run_id,
        "generated_at": now_local(settings.timezone).isoformat(timespec="seconds"),
        "rows_target": record.rows_target,
        "rows_clean": record.rows_clean,
    }
    try:
        atomic_write_json(index_path, index)
    except Exception:
        get_logger().warning("无法更新幂等索引 %s", index_path)


def _finalize(settings: Settings, record: RunRecord, logger: logging.Logger) -> None:
    now = now_local(settings.timezone)
    record.finished_at = now.isoformat(timespec="seconds")
    try:
        start = datetime.fromisoformat(record.started_at)
        if start.tzinfo is None and now.tzinfo is not None:
            start = start.replace(tzinfo=now.tzinfo)
        record.duration_ms = max(0, int((now - start).total_seconds() * 1000))
    except (ValueError, TypeError):
        record.duration_ms = 0
    record.failure_reason = str(record.failure_reason or "")[:1200]
    record.warnings = _dedupe(record.warnings)
    payload = jsonable(record)
    try:
        ensure_dir(settings.log_dir / "runs")
        atomic_write_json(settings.log_dir / "runs" / f"{record.run_id}.json", payload)
        append_jsonl(settings.log_dir / "runs.jsonl", payload)
        atomic_write_json(settings.log_dir / "latest_run.json", payload)
    except Exception as exc:
        logger.error("无法写入运行日志: %s", exc)
    logger.info(
        "run %s finished status=%s duration=%s report=%s reason=%s",
        record.run_id,
        record.status,
        humanize_duration(record.duration_ms),
        record.report_path or "—",
        record.failure_reason or "—",
    )
