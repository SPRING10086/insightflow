"""6b narrative adoption: gated/primary consume the LLM section, shadow never does."""

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.adoption import AdoptionDecision  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.narrative import NarrativeDraft  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402


class FakeNarrative:
    name = "fake"
    prompt_version = "narrative_v1"

    def __init__(self, draft: NarrativeDraft | None = None, fail: bool = False) -> None:
        self.draft = draft
        self.fail = fail
        self.last_meta = {"source_kind": "llm", "cost_cny": 0.001, "latency_ms": 5}

    def generate(self, fact_keys, field_labels, chart_ids, samples):
        if self.fail:
            raise RuntimeError("narrative down")
        return self.draft or NarrativeDraft(
            title="智能概览",
            summary="本期共 [[total.records]] 条有效记录。",
            key_points=["质量分 [[quality.score]]。"],
            chart_notes={},
        )


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":true,"lookback_days":1},"max_charts":4}',
        encoding="utf-8",
    )
    rows = ["日期,区域,销售额", "2026-11-07,华东,100", "2026-11-06,华东,90"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _decision(narrative_mode: str, force_rules: bool = False) -> AdoptionDecision:
    modes = {"semantic": "shadow", "plan": "shadow", "execute": "shadow", "narrative": narrative_mode}
    if force_rules:
        modes = {name: "shadow" for name in modes}
    return AdoptionDecision(modes=modes, reasons={}, force_rules=force_rules)


def _run(tmp: Path, provider, decision) -> tuple[dict, str, Path]:
    settings = load_settings(project_root=tmp)
    with contextlib.redirect_stderr(io.StringIO()):
        record = execute(
            settings,
            target_date="2026-11-07",
            force=True,
            narrative_provider=provider,
            adoption_decision=decision,
        )
    day = settings.output_dir / "2026-11-07"
    html = max(day.glob("*.html"), key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8")
    return json.loads((day / "run_record.json").read_text(encoding="utf-8")), html, day


class NarrativeAdoptionTests(unittest.TestCase):
    def test_shadow_keeps_artifacts_but_not_the_section(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, day = _run(tmp, FakeNarrative(), _decision("shadow"))
        self.assertTrue((day / "narrative_llm.json").is_file())
        self.assertNotIn("智能解读", html)
        self.assertNotIn("adoption", record["metadata"])

    def test_gated_adopts_valid_narrative(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, _day = _run(tmp, FakeNarrative(), _decision("gated"))
        self.assertIn("智能解读", html)
        self.assertEqual(record["metadata"]["narrative"]["source_kind"], "llm")

    def test_gated_rejects_unsafe_narrative(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        provider = FakeNarrative(NarrativeDraft("x", "<script>alert(1)</script>", [], {}))
        record, html, _day = _run(tmp, provider, _decision("gated"))
        self.assertNotIn("智能解读", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertEqual(record["metadata"]["narrative"]["source_kind"], "fallback")
        self.assertTrue(record["metadata"]["narrative"]["reason"])

    def test_force_rules_blocks_adoption(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, _day = _run(tmp, FakeNarrative(), _decision("primary", force_rules=True))
        self.assertNotIn("智能解读", html)
        self.assertTrue(record["metadata"]["adoption"]["force_rules"])

    def test_primary_without_provider_is_safe(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        record, html, _day = _run(tmp, None, _decision("primary"))
        self.assertNotIn("智能解读", html)
        self.assertEqual(record["status"], "success")


if __name__ == "__main__":
    unittest.main()
