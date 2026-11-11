"""Stage 6b-4: semantic adoption gate (pure core; no llm import).

The LLM decides what each column *means*; the rule schema supplies the
canonical mapping and every number is still computed by ``analyze()`` from the
cleaned frame.  Adoption is all-or-nothing per run: any missing column, key-role
conflict, unit conflict or unmappable field drops the whole LLM schema and the
rule fields stay in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

KEY_ROLES = {"date", "id", "metric"}
ROLE_VALUES = {"date", "dimension", "metric", "id", "text"}
UNIT_HINT_RE = re.compile(r"[（(]\s*(万元|千元|元|%|％)\s*[）)]")

#: Deterministic unit synonyms; both rule and LLM units go through this map.
UNIT_SYNONYMS = {
    "万": "万元",
    "万元": "万元",
    "万人民币": "万元",
    "万元人民币": "万元",
    "元": "元",
    "人民币元": "元",
    "元人民币": "元",
    "rmb": "元",
    "cny": "元",
    "%": "%",
    "％": "%",
    "百分比": "%",
    "百分数": "%",
    "单": "单",
    "件": "件",
    "个": "个",
    "人": "人",
    "行": "行",
}


def canonical_unit(text: object) -> str:
    """Normalise a unit deterministically; unknown units keep their cleaned text.

    Empty stays empty (unknown units on both sides are compared, never silently
    dropped).  Full-width / whitespace / latin case differences are removed so
    only real unit differences survive.
    """
    value = str(text or "").strip().lower().replace(" ", "").replace("\u3000", "")
    value = value.replace("（", "(").replace("）", ")")
    return UNIT_SYNONYMS.get(value, value)


class SemanticAdoptionError(ValueError):
    """A semantic gate failure; ``reason`` is a short machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


@dataclass
class SemanticAdoptionResult:
    fields: dict[str, Any]
    changed: list[str]
    tables: list[str]


def _header_unit_hint(original: str) -> str:
    match = UNIT_HINT_RE.search(str(original or ""))
    return match.group(1) if match else ""


def adopt_semantic_fields(
    artifact: Any,
    table_schemas: list[tuple[Any, Any]],
    rule_fields: dict[str, Any],
) -> SemanticAdoptionResult:
    """Return adopted FieldSpecs or raise :class:`SemanticAdoptionError`."""
    llm_tables = {str(table.table): table for table in getattr(artifact, "tables", []) or []}
    adopted: dict[str, Any] = {}
    changed: list[str] = []
    used_tables: list[str] = []

    for table, schema in table_schemas:
        name = str(table.source.relative_path)
        llm_table = llm_tables.get(name)
        if llm_table is None:
            raise SemanticAdoptionError("missing_table", f"LLM 语义缺少表 {name}")
        used_tables.append(name)

        by_name = {str(item.name): item for item in llm_table.fields}
        columns = [str(column) for column in table.frame.columns if not str(column).startswith("__")]
        for column in columns:
            if column not in by_name:
                raise SemanticAdoptionError("missing_column", f"{name}: 缺少字段 {column}")

        canonical_by_original = {
            str(getattr(spec, "original", key)): key for key, spec in schema.fields.items()
        }
        for column in columns:
            item = by_name[column]
            role = str(item.role or "").lower()
            if role not in ROLE_VALUES:
                raise SemanticAdoptionError("bad_role", f"{name}: {column} role={role!r}")
            canonical = canonical_by_original.get(column)
            if canonical is None:
                # Rule-dropped columns never enter analysis. A free-text role
                # (text/id, or dimension guessed from a small sample) is ignored
                # deterministically; claiming metric/date is still rejected.
                if role in {"metric", "date"}:
                    raise SemanticAdoptionError(
                        "unknown_canonical", f"{name}: {column} 无法映射却声明为 {role}"
                    )
                continue

            spec = schema.fields[canonical]
            if str(getattr(spec, "kind", "")) in KEY_ROLES and role != spec.kind:
                raise SemanticAdoptionError(
                    "role_conflict",
                    f"{name}: {column} 规则={spec.kind} LLM={role}",
                )
            llm_unit = str(item.unit or "").strip()
            rule_unit = str(getattr(spec, "unit", "") or "").strip()
            if (
                llm_unit
                and rule_unit
                and canonical_unit(llm_unit) != canonical_unit(rule_unit)
            ):
                # A header such as 注册资本(万元) is a deterministic unit hint;
                # when it agrees with the LLM, adopt the hinted unit instead of
                # failing on the rule's generic "元".
                hint = _header_unit_hint(str(getattr(spec, "original", "")))
                if hint and canonical_unit(hint) == canonical_unit(llm_unit):
                    spec = replace(spec, unit=hint)
                else:
                    raise SemanticAdoptionError(
                        "unit_conflict",
                        f"{name}: {column} 规则单位={rule_unit} LLM单位={llm_unit}",
                    )

            new_spec = spec if role == spec.kind else replace(spec, kind=role)
            if new_spec is not spec:
                changed.append(f"{name}:{column}")
            adopted[canonical] = new_spec

    # Preserve the caller's field order when provided, otherwise adoption order.
    order = list(rule_fields) if rule_fields else list(adopted)
    final = {key: adopted[key] for key in order if key in adopted}
    if not final:
        raise SemanticAdoptionError("empty", "采信后没有可用字段")
    return SemanticAdoptionResult(fields=final, changed=changed, tables=used_tables)
