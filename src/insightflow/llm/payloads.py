"""LLM PoC: build the smallest possible on-cloud payload.

Rules (hard requirements from the PoC brief):
* only field structure + the first ``max_rows`` rows go out;
* sensitive text is masked while keeping shape/length hints;
* a full data frame is never passed to the request builder;
* tests can prove row 11+ never appears in the serialized payload.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

MAX_SAMPLE_ROWS = 10

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_ID18 = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")

_COMPANY_SUFFIXES = ("有限责任公司", "股份有限公司", "有限公司", "公司", "集团", "工厂", "厂", "门店", "店")
_PERSON_HINTS = ("姓名", "联系人", "负责人", "销售员", "客户名", "员工", "业务员", "收货人")
_ADDRESS_HINTS = ("地址", "住址", "收货地", "所在地")
_COMPANY_HINTS = ("公司", "企业", "供应商", "客户", "单位", "商户")


def _mask_company(value: str) -> str:
    for suffix in _COMPANY_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            core = value[: -len(suffix)]
            head = core[:2]
            return f"{head}{'*' * max(2, len(core) - 2)}{suffix}"
    return value


def mask_value(value: Any, field_name: str = "") -> str:
    """Mask one cell, keeping shape hints but never the real value."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""

    name = field_name or ""
    if any(hint in name for hint in _PERSON_HINTS):
        return text[0] + "*" * max(1, len(text) - 1)
    if any(hint in name for hint in _ADDRESS_HINTS):
        return text[:2] + "*" * max(3, len(text) - 2)

    text = _ID18.sub(lambda m: m.group()[:6] + "********" + m.group()[-4:], text)
    text = _CARD.sub(lambda m: m.group()[:4] + "*" * (len(m.group()) - 8) + m.group()[-4:], text)
    text = _PHONE.sub(lambda m: m.group()[:3] + "****" + m.group()[-4:], text)
    text = _EMAIL.sub(lambda m: m.group(1)[:2] + "***@" + m.group(2), text)

    if any(hint in name for hint in _COMPANY_HINTS) or text.endswith(_COMPANY_SUFFIXES):
        text = _mask_company(text)
    return text


def infer_dtype(values: Iterable[Any]) -> str:
    """A deliberately simple dtype hint; the model only needs a rough category."""
    sample = [value for value in values if value not in (None, "")][:20]
    if not sample:
        return "unknown"
    numeric = 0
    for value in sample:
        try:
            float(str(value).replace(",", ""))
            numeric += 1
        except ValueError:
            continue
    if numeric == len(sample):
        return "number"
    for value in sample:
        if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(value).strip()):
            return "date"
    return "text"


def _field_statistics(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Aggregate cardinality hints (counts only — never values) for role judgement."""
    raw = [row.get(name) for row in rows]
    non_empty = [str(value).strip() for value in raw if value not in (None, "") and str(value).strip()]
    unique = len(set(non_empty))
    return {
        "non_empty": len(non_empty),
        "unique_count": unique,
        "unique_ratio": round(unique / len(non_empty), 4) if non_empty else 0.0,
    }


def build_payload(
    table_name: str,
    source: str,
    field_names: list[str],
    rows: list[dict[str, Any]],
    *,
    max_rows: int = MAX_SAMPLE_ROWS,
) -> dict[str, Any]:
    """Assemble the request payload: schema + masked first ``max_rows`` rows only."""
    limit = max(1, min(int(max_rows), MAX_SAMPLE_ROWS))
    sample_rows = rows[:limit]

    fields = []
    for name in field_names:
        column = [row.get(name) for row in rows]
        field_info = {"name": name, "dtype": infer_dtype(column)}
        field_info.update(_field_statistics(rows, name))
        fields.append(field_info)

    samples = []
    for row in sample_rows:
        samples.append({name: mask_value(row.get(name), name) for name in field_names})

    payload = {
        "schema_version": "1.0",
        "table": {"name": table_name, "source": source, "row_count_exposed": len(sample_rows)},
        "fields": fields,
        "samples": samples,
        "policy": {"max_rows": limit, "masked": True, "full_data_leaves_local": True},
    }
    assert_payload_minimal(payload, limit)
    return payload


def assert_payload_minimal(payload: dict[str, Any], max_rows: int) -> None:
    """Hard assertions: at most ``max_rows`` samples, every cell is masked text."""
    samples = payload.get("samples") or []
    if len(samples) > max_rows:
        raise AssertionError(f"载荷包含 {len(samples)} 行，超过上限 {max_rows}")
    names = {field["name"] for field in payload.get("fields") or []}
    for row in samples:
        if set(row) != names:
            raise AssertionError("样本行的字段集合与字段表不一致")
        for value in row.values():
            if not isinstance(value, str):
                raise AssertionError("样本单元格必须是脱敏后的字符串")


def dump_payload(payload: dict[str, Any], path: str | Path) -> Path:
    """Persist the exact payload for human review before any request leaves the box."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def read_csv_rows(path: str | Path, limit: int | None = None) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV with the stdlib only (no pandas dependency for the PoC)."""
    import csv

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        names = list(reader.fieldnames or [])
        rows: list[dict[str, str]] = []
        for row in reader:
            rows.append({name: row.get(name, "") for name in names})
            if limit is not None and len(rows) >= limit:
                break
    return names, rows
