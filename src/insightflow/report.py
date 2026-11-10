"""Single-file, offline HTML report renderer.

The generated document contains all CSS, JavaScript and ECharts code inline.
There are no CDN links, no fetch calls and no backend dependencies.  Every
chart also has a data table so the report remains useful even if JavaScript is
disabled.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .models import (
    AnalysisPlan,
    AnalysisResult,
    ChartSpec,
    CleaningReport,
    FieldSpec,
    Insight,
    KPI,
    ReadResult,
    RunRecord,
)
from .utils import (
    fmt_int,
    fmt_pct,
    html_escape,
    humanize_bytes,
    humanize_duration,
)


class ReportError(RuntimeError):
    """Raised when the offline report cannot be assembled."""


_SECTION_LABELS = {
    "summary": "结论摘要",
    "plan": "分析计划",
    "structure": "结构洞察",
    "trend": "趋势与对比",
    "distribution": "分布与关联",
    "quality": "数据质量",
    "lineage": "血缘与附录",
}

_INSIGHT_CLASS = {"info": "", "good": "good", "warn": "warn", "risk": "risk"}
_STATUS_BADGE = {
    "read": ("ok", "已读取"),
    "error": ("err", "读取失败"),
    "no_target": ("muted", "无目标数据"),
    "skipped": ("warn", "已跳过"),
}
_RUN_STATUS = {"success": ("ok", "生成成功"), "no_data": ("warn", "无新数据"), "failed": ("err", "生成失败"), "skipped": ("muted", "已跳过")}


def render_report(
    result: AnalysisResult,
    plan: AnalysisPlan,
    cleaning: CleaningReport,
    record: RunRecord,
    read_result: ReadResult,
    fields: dict[str, FieldSpec],
    settings: Settings,
    output_path: Path,
    narrative: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> Path:
    template_path = Path(__file__).resolve().parent / "templates" / "report.html"
    echarts_path = Path(__file__).resolve().parent / "assets" / "echarts.min.js"
    if not echarts_path.is_file():
        # Source checkout fallback for older layouts.
        echarts_path = Path(__file__).resolve().parents[2] / "assets" / "vendor" / "echarts.min.js"
    if not template_path.is_file():
        raise ReportError(f"报告模板不存在: {template_path}")
    if not echarts_path.is_file():
        raise ReportError(f"ECharts 离线资源不存在: {echarts_path}")
    template = template_path.read_text(encoding="utf-8")
    echarts_js = echarts_path.read_text(encoding="utf-8")
    echarts_js = echarts_js.replace("</script", "<\\/script").replace("</SCRIPT", "<\\/SCRIPT")

    chart_sections = _chart_sections(result)
    # Executive summary → evidence charts → methodology → quality → lineage.
    # Keeping the plan out of the first viewport makes the report useful at a
    # glance while preserving a complete audit trail further down.
    section_ids = ["summary", *chart_sections.keys(), "plan", "quality", "lineage"]
    section_ids = [section for section in section_ids if section in _SECTION_LABELS]
    nav = "".join(
        f'<a href="#{section_id}">{html_escape(_SECTION_LABELS[section_id])}</a>'
        for section_id in section_ids
    )

    body_parts: list[str] = []
    body_parts.append(_summary_section(result))
    for section_id, charts in chart_sections.items():
        body_parts.append(_chart_section(section_id, charts, result))
    body_parts.append(_plan_section(plan, fields, result))
    body_parts.append(_quality_section(cleaning, read_result, result, record))
    body_parts.append(_lineage_section(result, plan, cleaning, record, read_result, fields))

    kpi_strip = _kpi_strip(result.kpis)
    title = f"{result.target_date} · {settings.report.title}"
    headline = _headline(result)
    lead = _lead_text(result, plan, read_result)
    hero_meta = _hero_meta(result, plan, record, read_result)
    snapshot = _snapshot(result, plan, cleaning, record)
    foot_meta = (
        f"生成时间 {html_escape(record.started_at)} · run {html_escape(_short_run_id(record.run_id))} "
        f"· <b>{html_escape(settings.version)}</b>"
    )
    meta = {
        "targetDate": result.target_date,
        "baselineDate": result.baseline_date,
        "runId": record.run_id,
        "generatedAt": result.generated_at,
        "offline": True,
        "charts": len(result.charts),
    }
    charts_json = _json_for_script(
        [
            {
                "chart_id": chart.chart_id,
                "type": chart.chart_type,
                "title": chart.title,
                "subtitle": chart.subtitle,
                "value_type": chart.value_type,
                "insight": chart.insight,
                "data": chart.data,
                "height": chart.height,
            }
            for chart in result.charts
        ]
    )
    meta_json = _json_for_script(meta)

    replacements = {
        "@@TITLE@@": html_escape(title),
        "@@BRAND@@": html_escape(settings.app_name),
        "@@EYEBROW@@": "DAILY DATA BRIEF · SCHEDULED 09:30",
        "@@HEADLINE@@": headline,
        "@@LEAD@@": lead,
        "@@HERO_META@@": hero_meta,
        "@@SNAPSHOT@@": snapshot,
        "@@NAV@@": nav,
        "@@KPI_STRIP@@": kpi_strip,
        "@@BODY@@": "".join(body_parts),
        "@@FOOT_META@@": foot_meta,
        "@@META@@": meta_json,
        "@@CHARTS@@": charts_json,
        "@@ECHARTS@@": echarts_js,
    }
    html = template
    for token, value in replacements.items():
        html = html.replace(token, value)

    if narrative:
        from .narrative import narrative_section

        section = narrative_section(narrative)
        html = html.replace('<main class="wrap">', '<main class="wrap">' + section, 1)

    if provenance:
        # Mixed-source transparency: plan and/or the reconciled primary total may
        # come from the LLM track; every other number is rule-computed.
        adopted = "、".join(html_escape(str(item)) for item in provenance.get("adopted_metrics") or [])
        rule = "、".join(html_escape(str(item)) for item in provenance.get("rule_metrics") or [])
        plan_part = ""
        if provenance.get("plan_adopted"):
            plan_primary = html_escape(str(provenance.get("plan_primary") or "—"))
            plan_part = f"分析计划由 LLM 提供（主指标：{plan_primary}）；"
        semantic_part = "字段语义由 LLM 识别；" if provenance.get("semantic_adopted") else ""
        if adopted or rule:
            details = (
                f"主指标合计（{adopted or '—'}）由 LLM 沙箱计算并已与规则对账；"
                f"其余指标（{rule or '—'}）与所有分组图表由规则引擎计算。"
            )
        else:
            details = "其余数字与分组图表由规则引擎计算。"
        note = (
            '<div class="provenance-note" style="margin:0 0 12px;padding:8px 12px;'
            'border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;color:#475569;font-size:12px">'
            f"数据来源：{semantic_part}{plan_part}{details}</div>"
        )
        html = html.replace('<main class="wrap">', '<main class="wrap">' + note, 1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(html, encoding="utf-8", newline="")
    temporary.replace(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Hero / KPI / summary
# ---------------------------------------------------------------------------


def _headline(result: AnalysisResult) -> str:
    return f"{html_escape(result.target_date)}<br><span style='color:#93c5fd'>经营数据洞察</span>"


def _lead_text(result: AnalysisResult, plan: AnalysisPlan, read_result: ReadResult) -> str:
    summary = result.summary
    metric_label = html_escape(summary.get("metric_label") or "记录数")
    metric_value = html_escape(summary.get("metric_formatted") or "—")
    text = (
        f"自动扫描 {read_result.files_scanned} 个数据文件，识别为 {html_escape(plan.dataset_kind)}，"
        f"本期 {fmt_int(summary.get('rows'))} 条有效记录，{metric_label}合计 {metric_value}。"
        "所有图表与结论均来自同一份清洗后明细，可沿数据血缘回溯。"
    )
    return text


def _hero_meta(result: AnalysisResult, plan: AnalysisPlan, record: RunRecord, read_result: ReadResult) -> str:
    status_class, status_text = _RUN_STATUS.get(record.status, ("muted", record.status))
    trigger = "定时任务" if record.trigger == "scheduled" else "手动执行"
    chips = [
        f'<span class="pill"><span class="dot"></span>报告日期 <b>{html_escape(result.target_date)}</b></span>',
        f'<span class="pill">对比基准 <b>{html_escape(result.baseline_date or "无")}</b></span>',
        f'<span class="pill">数据源 <b>{read_result.files_read} / {read_result.files_scanned} 个文件</b></span>',
        f'<span class="pill">质量分 <b>{result.quality.quality_score:.1f}</b></span>',
        f'<span class="pill"><span class="badge {status_class}">{html_escape(status_text)}</span> {html_escape(trigger)}</span>',
    ]
    return "".join(chips)


def _snapshot(result: AnalysisResult, plan: AnalysisPlan, cleaning: CleaningReport, record: RunRecord) -> str:
    rows = [
        ("运行编号", _short_run_id(record.run_id)),
        ("目标日期", result.target_date),
        ("基准日期", result.baseline_date or "未启用 / 无数据"),
        ("清洗后记录", fmt_int(cleaning.rows_out)),
        ("数据质量", f"{cleaning.quality_score:.1f} / 100"),
        ("分析计划", f"{sum(1 for step in plan.steps if step.enabled)} 项启用"),
        ("图表", f"{len(result.charts)} 个"),
        ("生成时间", result.generated_at.replace("T", " ")[:19]),
    ]
    return "".join(
        f'<div class="snapshot-row"><span>{html_escape(label)}</span><b>{html_escape(value)}</b></div>'
        for label, value in rows
    )


def _kpi_strip(kpis: list[KPI]) -> str:
    if not kpis:
        return ""
    cards = []
    for kpi in kpis:
        trend = kpi.trend if kpi.trend in {"up", "down", "flat"} else "flat"
        arrow = "↑" if trend == "up" else "↓" if trend == "down" else "→"
        delta = kpi.delta_text if kpi.delta is not None else "—"
        # P2-1: when part of a summed metric is estimated, show the measured
        # value next to the total instead of silently mixing them.
        valid_line = (
            f'<div class="kpi-hint" style="color:#b45309">有效值 {html_escape(kpi.valid_formatted)}'
            f"（含填补 {kpi.imputed_count} 个）</div>"
            if kpi.imputed_count
            else ""
        )
        cards.append(
            f"""
            <div class="kpi">
              <div class="kpi-label">{html_escape(kpi.label)}<span class="kpi-unit">{html_escape(kpi.unit or "值")}</span></div>
              <div class="kpi-value" title="{html_escape(kpi.formatted)}">{html_escape(kpi.formatted)}</div>
              <div class="kpi-delta {trend}"><span class="arrow">{arrow}</span>{html_escape(delta)}<small>{html_escape(kpi.delta_label)}</small></div>
              <div class="kpi-hint" title="{html_escape(kpi.hint)}">{html_escape(kpi.hint)}</div>
              {valid_line}
            </div>
            """
        )
    return f'<div class="kpis">{"".join(cards)}</div>'


def _summary_section(result: AnalysisResult) -> str:
    cards = "".join(_insight_card(insight) for insight in result.insights)
    if not cards:
        cards = '<div class="empty">本次没有生成结论摘要，请查看数据质量与血缘信息。</div>'
    content = f'<div class="insights">{cards}</div>'
    return _section("summary", "EXECUTIVE SUMMARY", "结论摘要", "先看事实，再看推断；每条结论均带数字证据。", content)


def _insight_card(insight: Insight) -> str:
    tag_class = "infer" if insight.tag == "推断" else ""
    evidence = "".join(f"<span>{html_escape(item)}</span>" for item in insight.evidence)
    return f"""
    <article class="insight {_INSIGHT_CLASS.get(insight.severity, '')}">
      <div class="insight-top"><h3>{html_escape(insight.title)}</h3><span class="tag {tag_class}">{html_escape(insight.tag)}</span></div>
      <p>{html_escape(insight.body)}</p>
      <div class="evidence">{evidence}</div>
    </article>
    """


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _plan_section(plan: AnalysisPlan, fields: dict[str, FieldSpec], result: AnalysisResult) -> str:
    metric_count = len([field for field in fields.values() if field.kind == "metric"])
    dimension_count = len([field for field in fields.values() if field.kind == "dimension"])
    enabled = sum(1 for step in plan.steps if step.enabled)
    profile = f"""
    <div class="plan-grid">
      <div class="metric-box"><span>数据规模</span><b>{fmt_int(plan.rows)}</b><small>清洗后记录</small></div>
      <div class="metric-box"><span>识别字段</span><b>{len(fields)}</b><small>{metric_count} 指标 · {dimension_count} 维度</small></div>
      <div class="metric-box"><span>数据集类型</span><b style="font-size:15px">{html_escape(plan.dataset_kind)}</b><small>{html_escape(_field_label(fields, plan.primary_metric) or "无主指标")} 为主指标</small></div>
      <div class="metric-box"><span>分析计划</span><b>{enabled}</b><small>启用项 / {len(plan.steps)} 个候选</small></div>
    </div>
    """
    decisions = []
    for step in plan.steps:
        mark = "✓" if step.enabled else "–"
        detail = step.rationale if step.enabled else (step.skip_reason or step.rationale)
        decisions.append(
            f"""
            <div class="decision {'on' if step.enabled else 'off'}">
              <span class="mark">{mark}</span>
              <div><b>{html_escape(step.title)}</b><small>{html_escape(detail)}</small></div>
            </div>
            """
        )
    reasoning = "".join(f"<p>{html_escape(text)}</p>" for text in plan.reasoning)
    metric_rows = [
        [definition.label, definition.formula, definition.unit or "—", definition.aggregation]
        for definition in plan.metric_definitions
    ]
    metric_table = _table(["指标", "口径 / 公式", "单位", "聚合"], metric_rows, numeric_columns={2})
    field_rows = [
        [
            field.label,
            _kind_label(field.kind),
            f"{field.coverage:.0%}",
            fmt_int(field.unique_count),
            field.original,
        ]
        for field in sorted(
            fields.values(),
            key=lambda item: ({"metric": 0, "dimension": 1, "id": 2, "date": 3}.get(item.kind, 4), -item.priority),
        )
    ]
    field_table = _table(["字段", "角色", "覆盖率", "唯一值", "来源列"], field_rows, numeric_columns={2, 3})
    content = f"""
    {profile}
    <div class="decision-list">{''.join(decisions)}</div>
    <div class="reasoning">{reasoning}</div>
    <div style="margin-top:18px">
      <h3 style="font-size:13px;margin:0 0 8px">指标口径</h3>
      {metric_table}
    </div>
    <details class="data-table" style="border-top:1px solid var(--line);margin-top:16px">
      <summary>查看字段识别与来源映射</summary>
      {field_table}
    </details>
    """
    return _section("plan", "AUTOMATIC PLAN", "分析计划", "字段识别、方向判断与图表预算均记录在案。", f'<div class="panel">{content}</div>')


def _field_label(fields: dict[str, FieldSpec], key: str | None) -> str:
    if key and key in fields:
        return fields[key].label
    return ""


def _kind_label(kind: str) -> str:
    return {"metric": "指标", "dimension": "维度", "id": "标识", "date": "日期", "timestamp": "时间"}.get(kind, "文本")


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _chart_sections(result: AnalysisResult) -> dict[str, list[ChartSpec]]:
    by_id = {chart.chart_id: chart for chart in result.charts}
    structure = [by_id[key] for key in ("chart-rank", "chart-share", "chart-heatmap", "chart-pareto") if key in by_id]
    trend = [by_id[key] for key in ("chart-hourly", "chart-dod") if key in by_id]
    distribution = [by_id[key] for key in ("chart-distribution", "chart-correlation", "chart-margin") if key in by_id]
    sections: dict[str, list[ChartSpec]] = {}
    if structure:
        sections["structure"] = structure
    if trend:
        sections["trend"] = trend
    if distribution:
        sections["distribution"] = distribution
    return sections


def _chart_section(section_id: str, charts: list[ChartSpec], result: AnalysisResult) -> str:
    content = "".join(_chart_card(chart) for chart in charts)
    extra = ""
    if section_id == "structure" and result.dimension_table:
        headers = list(result.dimension_table[0].keys())
        rows = [[row.get(header, "") for header in headers] for row in result.dimension_table[:20]]
        extra = f"""
        <div class="panel" style="margin-top:14px">
          <h3 style="font-size:13px;margin:0 0 8px">主维度明细（Top 20）</h3>
          {_table(headers, rows, numeric_columns=set(range(1, len(headers))))}
          <div class="table-note">占比与累计占比均按本期同口径汇总计算；累计占比用于判断长尾和集中度。</div>
        </div>
        """
    if section_id == "distribution" and result.secondary_tables:
        blocks = []
        for label, rows in result.secondary_tables.items():
            if not rows:
                continue
            headers = list(rows[0].keys())
            blocks.append(
                f"<div style='margin-top:12px'><h4 style='font-size:12px;margin:0 0 6px'>{html_escape(label)}</h4>"
                + _table(headers, [[row.get(header, "") for header in headers] for row in rows], numeric_columns=set(range(1, len(headers))))
                + "</div>"
            )
        if blocks:
            extra = f'<div class="panel" style="margin-top:14px"><h3 style="font-size:13px;margin:0 0 4px">其他维度视图</h3>{"".join(blocks)}</div>'
    subtitles = {
        "structure": "主维度排名、构成、双维交叉与集中度。",
        "trend": "分时节奏与前一日对比；单日截面不构成跨日趋势。",
        "distribution": "分布形态、指标关联与毛利结构。",
    }
    eyebrow = {"structure": "STRUCTURE", "trend": "TREND & COMPARISON", "distribution": "DISTRIBUTION & DRIVERS"}[section_id]
    return _section(section_id, eyebrow, _SECTION_LABELS[section_id], subtitles[section_id], content + extra)


def _chart_card(chart: ChartSpec) -> str:
    table_html = ""
    if chart.table_headers and chart.table_rows:
        table_html = _details_table(chart.table_headers, chart.table_rows, chart.table_note)
    classes = "chart-card wide" if chart.layout == "wide" else "chart-card"
    return f"""
    <article class="{classes}">
      <div class="chart-head">
        <div><h3>{html_escape(chart.title)}</h3><p>{html_escape(chart.subtitle)}</p></div>
        <button class="icon-btn" type="button" data-download="{html_escape(chart.chart_id)}" title="下载 PNG" aria-label="下载 {html_escape(chart.title)} PNG">
          <svg viewBox="0 0 24 24" fill="none"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v3h16v-3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </div>
      <div class="chart-insight">{html_escape(chart.insight)}</div>
      <div class="chart" id="{html_escape(chart.chart_id)}" style="height:{int(chart.height)}px" role="img" aria-label="{html_escape(chart.title)}"></div>
      {table_html}
    </article>
    """


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------


def _quality_section(
    cleaning: CleaningReport,
    read_result: ReadResult,
    result: AnalysisResult,
    record: RunRecord,
) -> str:
    score = cleaning.quality_score
    ring = "#0d9488" if score >= 85 else "#d97706" if score >= 70 else "#e11d48"
    gauge = f"""
    <div>
      <div class="gauge" style="--score:{score:.1f};--ring:{ring}"><div class="gauge-inner"><b>{score:.1f}</b><span>质量分 / 100</span></div></div>
      <div class="quality-note">启发式评分，公式见下方</div>
    </div>
    """
    labels = {
        "completeness": "字段完整度",
        "duplicate_control": "重复控制",
        "outlier_control": "异常控制",
        "parse_quality": "解析质量",
    }
    bars = "".join(
        f"""
        <div>
          <div class="bar-label"><span>{labels.get(key, key)}</span><b>{value:.1f}</b></div>
          <div class="bar"><i style="width:{max(0, min(100, value)):.1f}%"></i></div>
        </div>
        """
        for key, value in cleaning.quality_components.items()
    )
    missing_total = sum(stat.missing_before for stat in cleaning.field_stats)
    mini = f"""
    <div class="quality-summary">
      <div class="mini-stat"><span>清洗前 → 后</span><b>{fmt_int(cleaning.rows_in)} → {fmt_int(cleaning.rows_out)}</b></div>
      <div class="mini-stat"><span>完全重复去重</span><b>{fmt_int(cleaning.duplicates_removed)}</b></div>
      <div class="mini-stat"><span>缺失单元格</span><b>{fmt_int(missing_total)}</b></div>
      <div class="mini-stat"><span>命中异常标记</span><b>{fmt_int(sum(cleaning.outlier_counts.values()))}</b></div>
    </div>
    """
    field_rows = [
        [
            stat.label,
            _kind_label(stat.kind),
            fmt_int(stat.missing_before),
            fmt_pct(stat.missing_rate * 100),
            fmt_int(stat.filled),
            fmt_int(stat.outliers),
        ]
        for stat in cleaning.field_stats
    ]
    field_table = _table(["字段", "角色", "缺失数", "缺失率", "填补数", "异常值"], field_rows, numeric_columns={2, 3, 4, 5})
    issue_items = []
    seen: set[str] = set()
    reader_codes: set[str] = set()
    reader_messages: set[str] = set()
    for issue in read_result.warnings:
        if issue.severity not in {"warning", "error"}:
            continue
        reader_codes.add(issue.code)
        reader_messages.add(issue.message)
        key = f"{issue.code}|{issue.file}|{issue.sheet}|{issue.message}"
        if key in seen:
            continue
        seen.add(key)
        issue_items.append(
            f'<div class="issue {issue.severity}"><span class="code">{html_escape(issue.code)}</span><span>{html_escape(issue.message)}</span></div>'
        )
    for report in read_result.file_reports:
        if report.error and report.error not in reader_messages:
            issue_items.append(
                f'<div class="issue error"><span class="code">file_error</span><span>{html_escape(report.relative_path)}：{html_escape(report.error)}</span></div>'
            )
    for warning in record.warnings:
        if not warning:
            continue
        match = re.match(r"^\[([^\]]+)\]\s*(.*)$", warning)
        code = match.group(1) if match else "run_warning"
        text = match.group(2) if match else warning
        if code in reader_codes:
            continue
        if any(message and message in text for message in reader_messages):
            continue
        if any(text == message for message in reader_messages):
            continue
        issue_items.append(
            f'<div class="issue"><span class="code">{html_escape(code)}</span><span>{html_escape(text)}</span></div>'
        )
    issues_html = "".join(issue_items[:12])
    if len(issue_items) > 12:
        issues_html += f'<div class="issue"><span class="code">more</span><span>另有 {len(issue_items) - 12} 条提示，见运行日志。</span></div>'
    if not issues_html:
        issues_html = '<div class="issue" style="border-color:#bbf7d0;background:#f0fdf4;color:#166534"><span class="code">ok</span><span>本次读取未发现需要重点提示的异常。</span></div>'
    notes = "".join(f"<p>{html_escape(note)}</p>" for note in cleaning.notes)
    content = f"""
    <div class="panel">
      <div class="quality-layout">{gauge}<div class="bars">{bars}</div></div>
      {mini}
      <div class="quality-summary" style="grid-template-columns:repeat(3,1fr);margin-top:10px">
        <div class="mini-stat"><span>重复行率</span><b>{fmt_pct(cleaning.duplicates_removed / max(1, cleaning.rows_in) * 100)}</b></div>
        <div class="mini-stat"><span>日期解析异常</span><b>{fmt_int(cleaning.invalid_date_rows)}</b></div>
        <div class="mini-stat"><span>乱码/异常文本修复</span><b>{fmt_int(cleaning.garbled_repaired)}</b></div>
      </div>
      <div style="margin-top:18px"><h3 style="font-size:13px;margin:0 0 8px">字段质量</h3>{field_table}</div>
      <div style="margin-top:18px"><h3 style="font-size:13px;margin:0 0 8px">兼容性提示</h3><div class="issues">{issues_html}</div></div>
      <details class="data-table" style="margin-top:16px"><summary>查看清洗规则与质量分公式</summary><div class="reasoning">{notes}<p>质量分 = 60% 完整度 + 15% 重复控制 + 15% 异常控制 + 10% 解析质量，均为启发式口径。</p></div></details>
    </div>
    """
    return _section("quality", "DATA QUALITY", "数据质量", "缺失、重复、异常和兼容性处理全部可审计。", content)


# ---------------------------------------------------------------------------
# Lineage / appendix
# ---------------------------------------------------------------------------


def _lineage_section(
    result: AnalysisResult,
    plan: AnalysisPlan,
    cleaning: CleaningReport,
    record: RunRecord,
    read_result: ReadResult,
    fields: dict[str, FieldSpec],
) -> str:
    file_rows = []
    for report in read_result.file_reports:
        effective_status = report.status
        if effective_status == "read" and report.rows_target == 0 and report.rows_baseline == 0:
            effective_status = "no_target"
        status_class, status_text = _STATUS_BADGE.get(effective_status, ("muted", effective_status))
        note = report.error or ("；".join(report.warnings) if report.warnings else "")
        file_rows.append(
            [
                ("badge", status_class, status_text),
                report.relative_path,
                report.file_type.upper(),
                humanize_bytes(report.size_bytes),
                fmt_int(report.rows_scanned),
                fmt_int(report.rows_target),
                fmt_int(report.rows_baseline),
                report.encoding or "—",
                str(report.header_row or "—"),
                note,
            ]
        )
    file_table = _table(
        ["状态", "文件", "类型", "大小", "扫描行", "本期行", "基准行", "编码", "表头行", "提示"],
        file_rows,
        numeric_columns={4, 5, 6, 8},
    )
    steps = record.steps or []
    step_html = "".join(
        f"""
        <div class="timeline-row">
          <span class="timeline-dot"></span>
          <div><b>{html_escape(step.get('name', ''))}</b><small>{html_escape(step.get('detail', ''))}</small></div>
          <time>{html_escape(humanize_duration(step.get('duration_ms', 0)))}</time>
        </div>
        """
        for step in steps
    )
    if not step_html:
        step_html = '<div class="empty">本次运行没有记录步骤耗时。</div>'
    top_headers = ["#", "维度组合", _field_label(fields, plan.primary_metric) or "指标", "订单量", "标记", "来源"]
    top_rows = [
        [row["rank"], row["dimensions"], row["metric"], row["orders"], row["flags"] or "—", row["source"]]
        for row in result.top_records
    ]
    top_table = _table(top_headers, top_rows, numeric_columns={0, 2, 3})
    outlier_headers = ["分组", "指标", "数值", "订单量", "判定", "来源"]
    outlier_rows = [
        [row["dimension"] or "—", row["metric"], row["value"], row["orders"], row["rule"], row["source"]]
        for row in result.outliers
    ]
    outlier_table = (
        _table(outlier_headers, outlier_rows, numeric_columns={2, 3})
        if outlier_rows
        else '<div class="empty">本期没有命中 IQR 异常值。</div>'
    )
    if outlier_rows and sum(cleaning.outlier_counts.values()) > len(outlier_rows):
        outlier_table += (
            f'<div class="table-note">共命中 {sum(cleaning.outlier_counts.values())} 个指标异常值，'
            f"此处展示按主指标排序的前 {len(outlier_rows)} 条。</div>"
        )
    source_fingerprint = read_result.fingerprint[:16] if read_result.fingerprint else "—"
    mapping_rows = []
    for field in fields.values():
        mapping_rows.append([field.label, field.original, _kind_label(field.kind), field.unit or "—", f"{field.coverage:.0%}"])
    mapping_table = _table(["标准字段", "原始列名", "角色", "单位", "覆盖率"], mapping_rows, numeric_columns={4})
    content = f"""
    <div class="panel">
      <h3 style="font-size:13px;margin:0 0 8px">源文件与读取结果</h3>
      {file_table}
      <div class="table-note">数据指纹：{html_escape(source_fingerprint)} · 递归扫描：{len(read_result.file_reports)} 个文件 · 成功读取 {read_result.files_read} 个。</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px">
        <div class="mini-stat"><span>目标日期</span><b>{html_escape(plan.target_date)}</b></div>
        <div class="mini-stat"><span>对比基准</span><b>{html_escape(plan.baseline_date or "无")}</b></div>
        <div class="mini-stat"><span>清洗前记录</span><b>{fmt_int(cleaning.rows_in)}</b></div>
        <div class="mini-stat"><span>清洗后记录</span><b>{fmt_int(cleaning.rows_out)}</b></div>
      </div>
    </div>
    <div class="panel" style="margin-top:14px">
      <h3 style="font-size:13px;margin:0 0 8px">高价值明细（Top 10）</h3>
      {top_table}
      <h3 style="font-size:13px;margin:18px 0 8px">异常值清单</h3>
      {outlier_table}
    </div>
    <div class="panel" style="margin-top:14px">
      <div class="split">
        <div><h3 style="font-size:13px;margin:0 0 8px">字段映射</h3>{mapping_table}</div>
        <div><h3 style="font-size:13px;margin:0 0 8px">运行步骤</h3><div class="timeline">{step_html}</div></div>
      </div>
    </div>
    <div class="panel" style="margin-top:14px">
      <h3 style="font-size:13px;margin:0 0 8px">口径与可复现说明</h3>
      <div class="reasoning">
        <p>目标日期严格按配置时区的前一自然日计算；本期只纳入该日数据，前一日仅作为对比基准。</p>
        <p>指标先按行清洗，再在分析层聚合；金额单位为元，比例以百分比展示；四舍五入可能造成 ±0.1 的尾差。</p>
        <p>缺失数值以当期非缺失中位数填补；文本缺失标记「未填写」；异常值按 IQR 1.5 倍标记但不删除。</p>
        <p>图表数据在每张图下方提供明细表；报告为单文件 HTML，ECharts 已内联，无外部网络请求。</p>
        <p>运行编号 {html_escape(record.run_id)} · 触发方式 {html_escape(record.trigger)} · 数据指纹 {html_escape(source_fingerprint)}。</p>
      </div>
    </div>
    """
    return _section("lineage", "LINEAGE & APPENDIX", "数据血缘与附录", "从源文件到图表，每一步都有记录。", content)


# ---------------------------------------------------------------------------
# Small HTML helpers
# ---------------------------------------------------------------------------


def _section(section_id: str, eyebrow: str, title: str, subtitle: str, content: str) -> str:
    return f"""
    <section class="section" id="{html_escape(section_id)}">
      <div class="section-head">
        <div><div class="section-tag">{html_escape(eyebrow)}</div><h2>{html_escape(title)}</h2><p>{html_escape(subtitle)}</p></div>
      </div>
      {content}
    </section>
    """


def _table(headers: Iterable[Any], rows: Iterable[Iterable[Any]], numeric_columns: set[int] | None = None) -> str:
    numeric_columns = numeric_columns or set()
    head_html = "".join(f"<th>{html_escape(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            if isinstance(cell, tuple) and len(cell) == 3 and cell[0] == "badge":
                _, badge_class, badge_text = cell
                cells.append(
                    f'<td><span class="badge {html_escape(badge_class)}">{html_escape(badge_text)}</span></td>'
                )
                continue
            text = "" if cell is None else str(cell)
            klass = ' class="num"' if index in numeric_columns else ""
            cells.append(f"<td{klass}>{html_escape(text)}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{head_html}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'


def _details_table(headers: list[str], rows: list[list[Any]], note: str = "") -> str:
    table = _table(headers, rows, numeric_columns=set(range(1, len(headers))))
    note_html = f'<div class="table-note">{html_escape(note)}</div>' if note else ""
    return f'<details class="data-table"><summary>查看图表数据</summary>{table}{note_html}</details>'


def _json_for_script(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _short_run_id(run_id: str) -> str:
    if not run_id:
        return "—"
    return run_id[-12:] if len(run_id) > 12 else run_id
