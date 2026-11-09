"""Stage-4 narrative: fact map, placeholder validation, escaping and rendering.

The model writes text with ``[[key]]`` placeholders only.  Numbers are formatted
locally from the deterministic result; model text is always escaped as text and
never reaches tags/attributes.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

NARRATIVE_VERSION = "1.0"
MAX_SEGMENT_CHARS = 500
MAX_SEGMENTS = 20

PLACEHOLDER_RE = re.compile(r"\[\[([a-z][a-z0-9_]*)\.([A-Za-z0-9_\u4e00-\u9fa5-]+(?:\.[A-Za-z0-9_\u4e00-\u9fa5-]+)?)\]\]")
#: Replacement values may only contain digits/units/common punctuation, never letters or markup.
SAFE_VALUE_RE = re.compile(r"^[-+0-9.,%％—–\s\u4e00-\u9fa5¥/（）()]+$")
UNSAFE_MARKUP_RE = re.compile(
    r"[<>`]|javascript\s*:|data\s*:|vbscript\s*:|on[a-z]+\s*=|&#(?:x[0-9a-f]+|\d+);|&(?:lt|gt|amp|quot|apos);",
    re.IGNORECASE,
)
DIGIT_RE = re.compile(r"[0-9０-９]")
CURRENCY_RE = re.compile(r"[%％￥$]")
CHINESE_QUANTITY_RE = re.compile(
    r"(?:百分之|百分)[零一二三四五六七八九十百千万亿两0-9０-９]+"
    r"|[零一二三四五六七八九十百千万亿两0-9０-９]+(?:成|倍)"
    r"|翻[零一二三四五六七八九十百千万亿两0-9０-９]+番"
    r"|第[零一二三四五六七八九十百千万亿两0-9０-９]+"
)


class NarrativeError(ValueError):
    reason = "invalid"


class BareNumberError(NarrativeError):
    reason = "bare_number"


class PlaceholderSyntaxError(NarrativeError):
    reason = "syntax"


class UnknownReferenceError(NarrativeError):
    reason = "unknown_ref"


class UnsafeMarkupError(NarrativeError):
    reason = "unsafe_markup"


@dataclass
class NarrativeDraft:
    title: str
    summary: str
    key_points: list[str] = field(default_factory=list)
    chart_notes: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class NarrativeProvider(Protocol):
    name: str

    def generate(
        self,
        fact_keys: list[str],
        field_labels: list[str],
        chart_ids: list[str],
        samples: list[dict[str, Any]],
    ) -> NarrativeDraft:
        ...


def _fmt_number(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}" if isinstance(value, int) else str(value)


def build_facts(result: Any, plan: Any, cleaning: Any, fields: dict[str, Any]) -> dict[str, str]:
    """Deterministic values the model may reference by key (values never sent to it)."""
    facts: dict[str, str] = {}
    facts["meta.date"] = str(getattr(result, "target_date", "") or "")
    facts["meta.dataset_kind"] = str(getattr(plan, "dataset_kind", "") or "")
    rows_out = int(getattr(cleaning, "rows_out", 0) or 0)
    facts["meta.rows"] = _fmt_number(rows_out)
    facts["total.records"] = _fmt_number(rows_out)

    for kpi in getattr(result, "kpis", []) or []:
        key = str(getattr(kpi, "key", "") or "")
        if not key:
            continue
        facts[f"kpi.{key}.value"] = str(getattr(kpi, "formatted", "") or "—")
        facts[f"kpi.{key}.label"] = str(getattr(kpi, "label", "") or "")
        unit = str(getattr(kpi, "unit", "") or "")
        if unit:
            facts[f"kpi.{key}.unit"] = unit
        facts[f"kpi.{key}.delta_pct"] = str(getattr(kpi, "delta_text", "") or "—")

    for key, spec in (fields or {}).items():
        kind = getattr(spec, "kind", "")
        label = str(getattr(spec, "label", "") or key)
        if kind == "metric":
            facts[f"metric.{key}.label"] = label
        elif kind == "dimension":
            facts[f"dimension.{key}.label"] = label

    facts["quality.score"] = f"{float(getattr(cleaning, 'quality_score', 0.0) or 0.0):.1f}"
    facts["quality.duplicates"] = _fmt_number(int(getattr(cleaning, "duplicates_removed", 0) or 0))
    missing = getattr(cleaning, "missing_filled", {}) or {}
    facts["quality.missing"] = _fmt_number(sum(int(value or 0) for value in missing.values()))
    outliers = getattr(cleaning, "outlier_counts", {}) or {}
    facts["quality.outliers"] = _fmt_number(sum(int(value or 0) for value in outliers.values()))

    dimension = str(getattr(plan, "primary_dimension", "") or "")
    records = list(getattr(result, "dimension_table", []) or [])
    if dimension and records:
        for index, rank in enumerate(("first", "second", "third")):
            if index >= len(records):
                break
            row = records[index]
            facts[f"topn.{dimension}.{rank}"] = str(row.get("name", ""))
            if index == 0 and row.get("share") is not None:
                facts[f"topn.{dimension}.first_share"] = f"{float(row['share']):.1f}%"

    for chart in getattr(result, "charts", []) or []:
        chart_id = str(getattr(chart, "chart_id", "") or "")
        if chart_id:
            facts[f"chart.{chart_id}.title"] = str(getattr(chart, "title", "") or "")
    return {key: value for key, value in facts.items() if str(value).strip()}


def _segments(draft: NarrativeDraft) -> list[str]:
    segments = [draft.title, draft.summary, *draft.key_points, *draft.chart_notes.values()]
    return [segment for segment in segments if segment is not None]


def _validate_text(text: str, facts: dict[str, str], chart_ids: set[str] | None = None) -> None:
    if not isinstance(text, str):
        raise PlaceholderSyntaxError("segment must be a string")
    if len(text) > MAX_SEGMENT_CHARS:
        raise PlaceholderSyntaxError(f"segment exceeds {MAX_SEGMENT_CHARS} characters")
    if UNSAFE_MARKUP_RE.search(text):
        raise UnsafeMarkupError("markup is forbidden in narrative text")

    remainder = PLACEHOLDER_RE.sub("", text)
    if any(token in remainder for token in ("[[", "]]", "[", "]", "{", "}")):
        raise PlaceholderSyntaxError("placeholder syntax is invalid or unclosed")

    for match in PLACEHOLDER_RE.finditer(text):
        key = f"{match.group(1)}.{match.group(2)}"
        if key not in facts:
            raise UnknownReferenceError(f"unknown placeholder: {key}")

    if DIGIT_RE.search(remainder) or CURRENCY_RE.search(remainder) or CHINESE_QUANTITY_RE.search(remainder):
        raise BareNumberError("narrative must not contain numbers outside placeholders")


def validate_narrative(draft: NarrativeDraft, facts: dict[str, str], chart_ids: list[str] | None = None) -> None:
    segments = _segments(draft)
    if len(segments) > MAX_SEGMENTS:
        raise PlaceholderSyntaxError(f"too many segments (>{MAX_SEGMENTS})")
    allowed_charts = set(chart_ids or [])
    for text in segments:
        _validate_text(text, facts)
    for chart_id, note in draft.chart_notes.items():
        if allowed_charts and chart_id not in allowed_charts:
            raise UnknownReferenceError(f"unknown chart_id: {chart_id}")
    if not str(draft.summary).strip() and not draft.key_points:
        raise PlaceholderSyntaxError("narrative is empty")


def _escape(text: str) -> str:
    return html.escape(str(text), quote=True)


def render_narrative(draft: NarrativeDraft, facts: dict[str, str]) -> dict[str, Any]:
    """Split text, escape literals, substitute safe formatted values."""
    mapping: dict[str, str] = {}

    def render_text(text: str) -> str:
        parts: list[str] = []
        position = 0
        for match in PLACEHOLDER_RE.finditer(text):
            parts.append(_escape(text[position : match.start()]))
            key = f"{match.group(1)}.{match.group(2)}"
            value = str(facts[key])
            if not SAFE_VALUE_RE.match(value):
                raise UnsafeMarkupError(f"replacement value is not safe: {value[:40]!r}")
            mapping[match.group(0)] = value
            parts.append(value)
            position = match.end()
        parts.append(_escape(text[position:]))
        return "".join(parts)

    rendered = {
        "title": render_text(draft.title),
        "summary": render_text(draft.summary),
        "key_points": [render_text(item) for item in draft.key_points],
        "chart_notes": {chart_id: render_text(note) for chart_id, note in draft.chart_notes.items()},
        "mapping": mapping,
    }
    for text in [rendered["title"], rendered["summary"], *rendered["key_points"], *rendered["chart_notes"].values()]:
        if "[[" in text or "]]" in text:
            raise PlaceholderSyntaxError("unrendered placeholder left in output")
    return rendered


def narrative_section(rendered: dict[str, Any]) -> str:
    """Escaped HTML block; only local renderer output is inserted."""
    parts = [
        '<section class="section" id="narrative">',
        '<div class="section-head"><div><h2>智能解读</h2>'
        '<p>LLM 生成，仅供参考；数字来自确定性计算</p></div>'
        '<span class="section-tag">AI</span></div>',
        '<div class="card">',
    ]
    if rendered.get("summary"):
        parts.append(f'<p class="narrative-summary">{rendered["summary"]}</p>')
    points = rendered.get("key_points") or []
    if points:
        parts.append('<ul class="narrative-points">')
        parts.extend(f"<li>{point}</li>" for point in points)
        parts.append("</ul>")
    parts.append("</div></section>")
    return "\n".join(parts)


def save_narrative_artifacts(
    day_dir: Path,
    facts: dict[str, str],
    draft: NarrativeDraft | None,
    rendered: dict[str, Any] | None,
    meta: dict[str, Any],
) -> list[Path]:
    day_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().isoformat(timespec="seconds")
    raw = (
        {
            "title": draft.title,
            "summary": draft.summary,
            "key_points": list(draft.key_points),
            "chart_notes": dict(draft.chart_notes),
        }
        if draft
        else None
    )
    payloads = (
        ("narrative_facts.json", {"schema_version": NARRATIVE_VERSION, "generated_at": generated_at, "facts": facts}),
        (
            "narrative_llm.json",
            {"schema_version": NARRATIVE_VERSION, "generated_at": generated_at, "meta": meta, "draft": raw},
        ),
        (
            "narrative_render.json",
            {"schema_version": NARRATIVE_VERSION, "generated_at": generated_at, "rendered": rendered or {}},
        ),
    )
    written: list[Path] = []
    for name, payload in payloads:
        target = day_dir / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(target)
    return written
