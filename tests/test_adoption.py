"""Stage 6a: adoption modes, dependency resolution and kill switch (red first)."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.adoption import AdoptionDecision, resolve_adoption  # noqa: E402
from insightflow.config import ConfigError, LLMSettings, load_settings  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project(llm: dict | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    payload = {
        "source_dir": "raw",
        "output_dir": "out",
        "log_dir": "logs",
        "state_dir": "state",
        "comparison": {"enabled": True, "lookback_days": 1},
    }
    if llm is not None:
        payload["llm"] = llm
    (tmp / "config" / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    rows = ["日期,区域,销售额", "2026-11-07,华东,100", "2026-11-07,华南,250", "2026-11-06,华东,90"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


class AdoptionConfigTests(unittest.TestCase):
    def test_defaults_are_shadow(self):
        llm = LLMSettings()
        self.assertFalse(llm.force_rules)
        self.assertEqual(set(llm.adoption), set(LLMSettings.FEATURES))
        self.assertTrue(all(mode == "shadow" for mode in llm.adoption.values()))

    def test_invalid_adoption_fails_closed(self):
        cases = [
            {"adoption": {"semantic": "auto"}},
            {"adoption": {"bogus": "shadow"}},
            {"adoption": {"semantic": 1}},
            {"force_rules": "yes"},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    LLMSettings.from_dict(raw)

    def test_modes_accepted(self):
        llm = LLMSettings.from_dict(
            {
                "enabled": True,
                "features": {"semantic": True},
                "adoption": {"semantic": "gated"},
                "force_rules": False,
            }
        )
        self.assertEqual(llm.adoption["semantic"], "gated")


class ResolveTests(unittest.TestCase):
    def _llm(self, **raw) -> LLMSettings:
        base = {"enabled": True, "features": {name: True for name in LLMSettings.FEATURES}}
        base.update(raw)
        return LLMSettings.from_dict(base)

    def test_all_shadow_by_default(self):
        decision = resolve_adoption(LLMSettings())
        self.assertTrue(all(mode == "shadow" for mode in decision.modes.values()))
        self.assertFalse(decision.force_rules)

    def test_gated_passthrough_when_enabled(self):
        decision = resolve_adoption(self._llm(adoption={"semantic": "gated"}))
        self.assertEqual(decision.modes["semantic"], "gated")
        self.assertEqual(decision.modes["plan"], "shadow")

    def test_feature_off_forces_shadow(self):
        llm = self._llm(adoption={"semantic": "gated"}, features={"semantic": False})
        decision = resolve_adoption(llm)
        self.assertEqual(decision.modes["semantic"], "shadow")
        self.assertEqual(decision.reasons["semantic"], "feature_off")

    def test_plan_requires_semantic(self):
        llm = self._llm(adoption={"plan": "gated"})
        decision = resolve_adoption(llm)
        self.assertEqual(decision.modes["plan"], "shadow")
        self.assertEqual(decision.reasons["plan"], "upstream_semantic_shadow")

    def test_execute_requires_plan(self):
        llm = self._llm(adoption={"semantic": "gated", "execute": "gated"})
        decision = resolve_adoption(llm)
        self.assertEqual(decision.modes["execute"], "shadow")
        self.assertEqual(decision.reasons["execute"], "upstream_plan_shadow")

    def test_dependency_chain_passes(self):
        llm = self._llm(adoption={"semantic": "gated", "plan": "gated", "execute": "gated"})
        decision = resolve_adoption(llm)
        self.assertEqual(decision.modes["semantic"], "gated")
        self.assertEqual(decision.modes["plan"], "gated")
        self.assertEqual(decision.modes["execute"], "gated")

    def test_force_rules_kills_everything(self):
        llm = self._llm(adoption={"semantic": "primary", "narrative": "primary"}, force_rules=True)
        decision = resolve_adoption(llm)
        self.assertTrue(decision.force_rules)
        self.assertTrue(all(mode == "shadow" for mode in decision.modes.values()))

    def test_env_kill_switch(self):
        llm = self._llm(adoption={"narrative": "primary"})
        with patch.dict(os.environ, {"INSIGHTFLOW_FORCE_RULES": "1"}):
            decision = resolve_adoption(llm)
        self.assertTrue(decision.force_rules)
        self.assertEqual(decision.modes["narrative"], "shadow")


class PipelineZeroChangeTests(unittest.TestCase):
    def test_shadow_decision_adds_no_metadata_and_same_metrics(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"

        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        baseline_metrics = (day / "metrics.json").read_bytes()
        baseline_cleaned = (day / "cleaned_data.csv").read_bytes()
        baseline_plan = (day / "analysis_plan.json").read_bytes()
        baseline_record = json.loads((day / "run_record.json").read_text(encoding="utf-8"))

        with contextlib.redirect_stderr(io.StringIO()):
            execute(
                settings,
                target_date="2026-11-07",
                force=True,
                adoption_decision=AdoptionDecision.shadow(),
            )
        self.assertEqual((day / "metrics.json").read_bytes(), baseline_metrics)
        self.assertEqual((day / "cleaned_data.csv").read_bytes(), baseline_cleaned)
        # analysis_plan.json embeds per-run timestamps/ids; compare its content
        # after dropping those volatile fields.
        plan_before = json.loads(baseline_plan)
        plan_after = json.loads((day / "analysis_plan.json").read_text(encoding="utf-8"))
        for volatile in ("generated_at", "run_id", "duration_ms", "elapsed_ms", "created_at"):
            plan_before.pop(volatile, None)
            plan_after.pop(volatile, None)
        self.assertEqual(plan_after, plan_before)
        record = json.loads((day / "run_record.json").read_text(encoding="utf-8"))
        self.assertNotIn("adoption", record["metadata"])
        self.assertNotIn("adoption", baseline_record["metadata"])

    def test_non_shadow_decision_is_recorded_only(self):
        tmp = _project(
            {
                "enabled": True,
                "features": {"narrative": True},
                "adoption": {"narrative": "gated"},
            }
        )
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        decision = resolve_adoption(settings.llm)
        self.assertEqual(decision.modes["narrative"], "gated")
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(
                settings, target_date="2026-11-07", force=True, adoption_decision=decision
            )
        self.assertEqual(record.status, "success")
        day = settings.output_dir / "2026-11-07"
        meta = json.loads((day / "run_record.json").read_text(encoding="utf-8"))["metadata"]
        self.assertEqual(meta["adoption"]["modes"]["narrative"], "gated")
        self.assertFalse((day / "narrative_llm.json").exists())


if __name__ == "__main__":
    unittest.main()
