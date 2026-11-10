"""6b-4 semantic adoption: coverage/key-role/unit/canonical gates."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.adoption import AdoptionDecision  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.models import FieldSpec  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402
from insightflow.semantic import FieldSemantics, ProviderTableResult  # noqa: E402
from insightflow.semantic_adoption import (  # noqa: E402
    SemanticAdoptionError,
    adopt_semantic_fields,
)


def _artifact(fields: list[FieldSemantics], table: str = "a.csv"):
    return SimpleNamespace(tables=[SimpleNamespace(table=table, fields=fields)])


def _schema() -> tuple[SimpleNamespace, SimpleNamespace]:
    frame = pd.DataFrame(
        {
            "日期": ["2026-11-07"],
            "区域": ["华东"],
            "销售额": [100.0],
            "备注编号": ["X-1"],
        }
    )
    table = SimpleNamespace(source=SimpleNamespace(relative_path="a.csv"), frame=frame)
    schema = SimpleNamespace(
        fields={
            "date": FieldSpec(original="日期", canonical="date", label="日期", kind="date"),
            "region": FieldSpec(original="区域", canonical="region", label="区域", kind="dimension"),
            "revenue": FieldSpec(original="销售额", canonical="revenue", label="销售额", kind="metric", unit="元"),
        }
    )
    return table, schema


def _fields(**overrides) -> list[FieldSemantics]:
    base = {
        "日期": FieldSemantics(name="日期", role="date"),
        "区域": FieldSemantics(name="区域", role="dimension"),
        "销售额": FieldSemantics(name="销售额", role="metric", unit="元"),
        "备注编号": FieldSemantics(name="备注编号", role="text"),
    }
    for name, role in overrides.items():
        base[name] = FieldSemantics(name=name, role=role, unit=base[name].unit)
    return list(base.values())


class AdapterTests(unittest.TestCase):
    def test_matching_roles_adopt(self):
        table, schema = _schema()
        result = adopt_semantic_fields(_artifact(_fields()), [(table, schema)], {})
        self.assertIn("revenue", result.fields)
        self.assertEqual(result.fields["revenue"].unit, "元")

    def test_missing_column_rejected(self):
        table, schema = _schema()
        partial = [item for item in _fields() if item.name != "销售额"]
        with self.assertRaises(SemanticAdoptionError) as ctx:
            adopt_semantic_fields(_artifact(partial), [(table, schema)], {})
        self.assertEqual(ctx.exception.reason, "missing_column")

    def test_key_role_conflict_rejected(self):
        table, schema = _schema()
        with self.assertRaises(SemanticAdoptionError) as ctx:
            adopt_semantic_fields(_artifact(_fields(销售额="dimension")), [(table, schema)], {})
        self.assertEqual(ctx.exception.reason, "role_conflict")

    def test_unit_conflict_rejected(self):
        table, schema = _schema()
        fields = _fields()
        for item in fields:
            if item.name == "销售额":
                item.unit = "万元"
        with self.assertRaises(SemanticAdoptionError) as ctx:
            adopt_semantic_fields(_artifact(fields), [(table, schema)], {})
        self.assertEqual(ctx.exception.reason, "unit_conflict")

    def test_unknown_canonical_metric_rejected(self):
        table, schema = _schema()
        with self.assertRaises(SemanticAdoptionError) as ctx:
            adopt_semantic_fields(_artifact(_fields(备注编号="metric")), [(table, schema)], {})
        self.assertEqual(ctx.exception.reason, "unknown_canonical")

    def test_dimension_can_become_metric(self):
        table, schema = _schema()
        result = adopt_semantic_fields(_artifact(_fields(区域="metric")), [(table, schema)], {})
        self.assertEqual(result.fields["region"].kind, "metric")
        self.assertTrue(result.changed)


class FakeSemantic:
    name = "fake"

    def __init__(self, fields: list[FieldSemantics]) -> None:
        self.fields = fields

    def describe(self, sample):
        return ProviderTableResult(fields=self.fields, prompt_version="v3", cost_cny=0.001, latency_ms=5)


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state"}', encoding="utf-8"
    )
    rows = ["日期,区域,销售额", "2026-11-07,华东,100", "2026-11-07,华南,250"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _semantic_fields(**overrides) -> list[FieldSemantics]:
    base = {
        "日期": FieldSemantics(name="日期", role="date"),
        "区域": FieldSemantics(name="区域", role="dimension"),
        "销售额": FieldSemantics(name="销售额", role="metric", unit="元"),
    }
    for name, role in overrides.items():
        base[name] = FieldSemantics(name=name, role=role, unit=base[name].unit)
    return list(base.values())


def _decision(mode: str) -> AdoptionDecision:
    return AdoptionDecision(
        modes={"semantic": mode, "plan": "shadow", "execute": "shadow", "narrative": "shadow"},
        reasons={},
        force_rules=False,
    )


class PipelineTests(unittest.TestCase):
    def _run(self, provider, decision):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(
                settings,
                target_date="2026-11-07",
                force=True,
                semantic_provider=provider,
                adoption_decision=decision,
            )
        day = settings.output_dir / "2026-11-07"
        html = max(day.glob("*.html"), key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8")
        return record, html, day

    def test_gated_adopts_and_labels(self):
        record, html, day = self._run(FakeSemantic(_semantic_fields()), _decision("gated"))
        self.assertEqual(record.status, "success")
        self.assertIn("字段语义由 LLM 识别", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertTrue(meta["semantic_schema"]["adopted"])

    def test_role_conflict_falls_back(self):
        record, html, day = self._run(
            FakeSemantic(_semantic_fields(销售额="dimension")), _decision("gated")
        )
        self.assertEqual(record.status, "success")
        self.assertNotIn("字段语义由 LLM 识别", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertFalse(meta["semantic_schema"]["adopted"])
        self.assertIn("role_conflict", meta["semantic_schema"]["gate_reason"])

    def test_shadow_does_not_adopt(self):
        _record, html, day = self._run(FakeSemantic(_semantic_fields()), _decision("shadow"))
        self.assertNotIn("字段语义由 LLM 识别", html)
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertNotIn("adopted", meta["semantic_schema"])


if __name__ == "__main__":
    unittest.main()
