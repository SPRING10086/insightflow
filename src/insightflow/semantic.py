"""Neutral semantic-schema contracts shared by the rule track and the LLM track.

Design rules:
* this module has **no network and no pandas** — any core module may import it;
* the LLM adapter implements :class:`SemanticProvider` and is assembled only at
  the outermost edge (`semantic_assembly.py`), never inside compute modules;
* artifacts are versioned, JSON-serialisable and diffable, so the rule track and
  the LLM track can be compared and audited after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

ARTIFACT_VERSION = "1.0"
ROLE_VALUES = ("date", "dimension", "metric", "id", "text")


@dataclass
class TableSample:
    """Everything a semantic provider may see about one table."""

    name: str
    source: str
    sheet: str
    field_names: list[str]
    rows: list[dict[str, Any]]


@dataclass
class FieldSemantics:
    name: str
    role: str
    unit: str = ""
    meaning: str = ""
    confidence: float = 0.0
    source: str = "rule"
    relations: list[str] = field(default_factory=list)


@dataclass
class ProviderTableResult:
    """One table's answer from a provider; exceptions are reported by raising."""

    fields: list[FieldSemantics]
    prompt_version: str = ""
    cost_cny: float = 0.0
    latency_ms: int = 0
    source_kind: str = "llm"
    anomalies: list[dict[str, str]] = field(default_factory=list)


@runtime_checkable
class SemanticProvider(Protocol):
    """Controlled semantic access point (no network in this signature)."""

    name: str

    def describe(self, sample: TableSample) -> ProviderTableResult:
        ...


@dataclass
class TableSemantics:
    table: str
    source: str
    sheet: str
    provider: str
    source_kind: str  # llm | rule | fallback
    fields: list[FieldSemantics]
    error: str = ""
    cost_cny: float = 0.0
    latency_ms: int = 0
    anomalies: list[dict[str, str]] = field(default_factory=list)


@dataclass
class SemanticArtifact:
    version: str
    prompt_version: str
    generated_at: str
    provider: str
    source_kind: str
    tables: list[TableSemantics]
    cost_cny: float = 0.0
    latency_ms: int = 0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.version,
            "prompt_version": self.prompt_version,
            "generated_at": self.generated_at,
            "provider": self.provider,
            "source_kind": self.source_kind,
            "cost_cny": round(self.cost_cny, 6),
            "latency_ms": self.latency_ms,
            "notes": self.notes,
            "tables": [
                {
                    "table": table.table,
                    "source": table.source,
                    "sheet": table.sheet,
                    "provider": table.provider,
                    "source_kind": table.source_kind,
                    "error": table.error,
                    "cost_cny": round(table.cost_cny, 6),
                    "latency_ms": table.latency_ms,
                    "anomalies": list(table.anomalies),
                    "fields": [
                        {
                            "name": item.name,
                            "role": item.role,
                            "unit": item.unit,
                            "meaning": item.meaning,
                            "confidence": round(float(item.confidence), 4),
                            "source": item.source,
                            "relations": list(item.relations),
                        }
                        for item in table.fields
                    ],
                }
                for table in self.tables
            ],
        }


def fields_from_rule_schema(schema: Any) -> list[FieldSemantics]:
    """Convert a rule ``TableSchema`` into neutral field semantics."""
    result: list[FieldSemantics] = []
    for spec in schema.fields.values():
        role = spec.kind if spec.kind in ROLE_VALUES else "text"
        result.append(
            FieldSemantics(
                name=spec.original,
                role=role,
                unit=spec.unit,
                meaning=spec.label,
                confidence=1.0,
                source="rule",
            )
        )
    return result


def rule_artifact(table_schemas: list[tuple[Any, Any]], generated_at: str | None = None) -> SemanticArtifact:
    """Build the rule-track artifact from ``(TableData, TableSchema)`` pairs."""
    tables = [
        TableSemantics(
            table=table.source.relative_path,
            source=table.source.relative_path,
            sheet=table.sheet_label,
            provider="rules",
            source_kind="rule",
            fields=fields_from_rule_schema(schema),
        )
        for table, schema in table_schemas
    ]
    return SemanticArtifact(
        version=ARTIFACT_VERSION,
        prompt_version="rules",
        generated_at=generated_at or datetime.now().isoformat(timespec="seconds"),
        provider="rules",
        source_kind="rule",
        tables=tables,
    )


def _index(artifact: SemanticArtifact) -> dict[tuple[str, str], FieldSemantics]:
    index: dict[tuple[str, str], FieldSemantics] = {}
    for table in artifact.tables:
        for item in table.fields:
            index[(table.table, item.name)] = item
    return index


def diff_artifacts(rule: SemanticArtifact, other: SemanticArtifact) -> dict[str, Any]:
    """Compare two artifacts; returns counts, rate and per-field mismatches."""
    left = _index(rule)
    right = _index(other)
    keys = sorted(set(left) | set(right))
    matched = [key for key in keys if key in left and key in right]
    role_agree = 0
    unit_agree = 0
    key_role_errors: list[dict[str, str]] = []
    mismatches: list[dict[str, str]] = []
    for key in matched:
        expected, actual = left[key], right[key]
        if expected.role == actual.role:
            role_agree += 1
        else:
            mismatches.append(
                {"table": key[0], "field": key[1], "expected_role": expected.role, "actual_role": actual.role}
            )
            if expected.role in {"date", "metric", "id"} or actual.role in {"date", "metric", "id"}:
                key_role_errors.append(
                    {"table": key[0], "field": key[1], "expected_role": expected.role, "actual_role": actual.role}
                )
        if (expected.unit or "").strip() == (actual.unit or "").strip():
            unit_agree += 1
    total = len(matched) or 1
    return {
        "schema_version": ARTIFACT_VERSION,
        "provider": other.provider,
        "source_kind": other.source_kind,
        "tables": len({key[0] for key in keys}),
        "matched_fields": len(matched),
        "role_agreement": round(role_agree / total, 4),
        "unit_agreement": round(unit_agree / total, 4),
        "key_role_errors": key_role_errors,
        "mismatches": mismatches,
        "missing_in_other": [{"table": key[0], "field": key[1]} for key in keys if key in left and key not in right],
        "extra_in_other": [{"table": key[0], "field": key[1]} for key in keys if key not in left and key in right],
    }


def save_artifacts(
    day_dir: Path,
    rule: SemanticArtifact,
    other: SemanticArtifact,
    diff: dict[str, Any],
) -> list[Path]:
    """Persist rule / provider / diff artifacts next to the run outputs."""
    import json

    day_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    payloads = (
        ("semantic_rule.json", rule.to_dict()),
        ("semantic_schema.json", other.to_dict()),
        ("semantic_diff.json", diff),
    )
    for name, payload in payloads:
        target = day_dir / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(target)
    return written
