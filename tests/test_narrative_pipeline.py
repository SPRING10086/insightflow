from __future__ import annotations

import contextlib
import hashlib
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

from insightflow.config import load_settings  # noqa: E402
from insightflow.narrative import NarrativeDraft  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402


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
    rows = ["订单日期,区域,品类,销售额,订单量"]
    rows += [f"2026-11-07,华东,手机数码,{1000 + i * 10},2" for i in range(6)]
    rows += ["2026-11-06,华东,手机数码,900,2"]
    (tmp / "raw" / "sales.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _latest_html(day: Path) -> Path:
    return max(day.glob("*.html"), key=lambda path: path.stat().st_mtime)


class FakeNarrative:
    name = "fake"
    prompt_version = "narrative_v1"

    def __init__(self, draft: NarrativeDraft | None = None, fail: bool = False) -> None:
        self.draft = draft
        self.fail = fail
        self.last_meta = {"source_kind": "llm", "cost_cny": 0.002, "latency_ms": 12}

    def generate(self, fact_keys, field_labels, chart_ids, samples):
        if self.fail:
            raise RuntimeError("narrative down")
        if self.draft is not None:
            return self.draft
        return NarrativeDraft(
            title="智能概览",
            summary="本期共 [[total.records]] 条有效记录。",
            key_points=["数据质量分 [[quality.score]]。"],
            chart_notes={},
        )


class NarrativePipelineTests(unittest.TestCase):
    def test_default_off_has_no_narrative_section(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        day = settings.output_dir / "2026-11-07"
        html = _latest_html(day).read_text(encoding="utf-8")
        self.assertNotIn("智能解读", html)
        self.assertEqual(sorted(p.name for p in day.glob("narrative_*")), [])
        self.assertNotIn("narrative", json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"])

    def test_success_renders_escaped_text_and_keeps_scripts(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        baseline_html = _latest_html(day).read_text(encoding="utf-8")
        baseline_scripts = baseline_html.count("<script")
        baseline_metrics = json.loads((day / "metrics.json").read_text(encoding="utf-8"))

        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(settings, target_date="2026-11-07", force=True, narrative_provider=FakeNarrative())
        self.assertEqual(record.status, "success")
        html = _latest_html(day).read_text(encoding="utf-8")
        self.assertIn("智能解读", html)
        self.assertIn("LLM 生成，仅供参考", html)
        for prefix in ("[[total.", "[[quality.", "[[kpi.", "[[topn.", "[[chart."):
            self.assertNotIn(prefix, html)
        self.assertEqual(html.count("<script"), baseline_scripts)
        self.assertEqual(record.metadata["narrative"]["source_kind"], "llm")
        files = sorted(p.name for p in day.glob("narrative_*"))
        self.assertEqual(
            files,
            ["narrative_facts.json", "narrative_llm.json", "narrative_render.json", "narrative_rule.json"],
        )
        self.assertEqual(baseline_metrics["kpis"], json.loads((day / "metrics.json").read_text(encoding="utf-8"))["kpis"])

    def test_render_is_deterministic_without_narrative(self):
        import re

        def normalized(html: str) -> str:
            html = re.sub(r'"(runId|generatedAt)":"[^"]*"', r'"\1":"X"', html)
            html = re.sub(r"20\d\d-\d\d-\d\dT[\d:+\-]+", "TIMESTAMP", html)
            html = re.sub(r"20\d\d-\d\d-\d\d \d\d:\d\d:\d\d", "TIMESTAMP", html)
            html = re.sub(r"\b(?:20\d{6}-)?\d{6}-[0-9a-f]{4}\b", "RUNID", html)
            # Step durations are wall-clock and legitimately differ per run.
            return re.sub(r"\d+(?:\.\d+)?\s*(?:ms|s|秒|毫秒)", "DURATION", html)

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        first = normalized(_latest_html(day).read_text(encoding="utf-8"))
        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        second = normalized(_latest_html(day).read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(first.encode("utf-8")).hexdigest(), hashlib.sha256(second.encode("utf-8")).hexdigest())

    def test_failures_fall_back(self):
        cases = {
            "raise": FakeNarrative(fail=True),
            "bare_number": FakeNarrative(NarrativeDraft("x", "增长 5%", [], {})),
            "unknown_ref": FakeNarrative(NarrativeDraft("x", "见 [[kpi.ghost.value]]", [], {})),
            "xss": FakeNarrative(NarrativeDraft("x", "<script>alert(1)</script>", [], {})),
        }
        for label, provider in cases.items():
            with self.subTest(label=label):
                tmp = _project()
                self.addCleanup(_cleanup, tmp)
                settings = load_settings(project_root=tmp)
                day = settings.output_dir / "2026-11-07"
                with contextlib.redirect_stderr(io.StringIO()):
                    record = execute(settings, target_date="2026-11-07", force=True, narrative_provider=provider)
                self.assertEqual(record.status, "success")
                html = _latest_html(day).read_text(encoding="utf-8")
                self.assertNotIn("智能解读", html)
                self.assertNotIn("<script>alert(1)</script>", html)
                meta = record.metadata["narrative"]
                self.assertEqual(meta["source_kind"], "fallback")
                self.assertTrue(meta["reason"])
                self.assertTrue(any("narrative_fallback" in item for item in record.warnings))


if __name__ == "__main__":
    unittest.main()
