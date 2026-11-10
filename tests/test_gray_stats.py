"""6c gray-observation aggregation: per-capability rates and readiness."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "llm_stats.py"
    spec = importlib.util.spec_from_file_location("llm_stats", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(
    *,
    fingerprint: str,
    modes: dict[str, str] | None = None,
    adopted: bool = True,
    accepted: bool = True,
    reason: str = "",
) -> dict:
    modes = modes or {name: "gated" for name in ("semantic", "plan", "execute", "narrative")}
    return {
        "run_id": f"r-{fingerprint}",
        "fingerprint": fingerprint,
        "metadata": {
            "llm_stats": {
                "calls": 4,
                "json_failures": 0,
                "retries": 0,
                "fallbacks": 0,
                "retry_blocked": 0,
                "cost_cny": 0.02,
                "max_prompt_tokens": 1200,
                "prompt_warn": False,
            },
            "adoption": {"force_rules": False, "final_mode": modes, "downgrade_reason": {}},
            "semantic_schema": {
                "adopted": adopted,
                "role_agreement": 1.0 if adopted else 0.5,
                "gate_reason": reason,
            },
            "plan": {"adopted": adopted, "metrics_jaccard": 1.0 if adopted else 0.0, "gate_reason": reason},
            "execution": {"adopted": adopted, "accepted": accepted, "gate_reason": reason},
            "narrative": {"source_kind": "llm" if adopted else "fallback", "reason": reason},
        },
    }


def _write(root: Path, records: list[dict]) -> None:
    for index, record in enumerate(records):
        day = root / "output" / f"2026-11-{index + 1:02d}"
        day.mkdir(parents=True, exist_ok=True)
        (day / "run_record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


class GrayStatsTests(unittest.TestCase):
    def test_zero_runs(self):
        module = _load()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tests")) as tmp:
            summary = module.aggregate(Path(tmp))
        self.assertEqual(summary["runs"], 0)
        self.assertFalse(summary["readiness"]["ready"])
        self.assertTrue(summary["readiness"]["blockers"])

    def test_all_good_reaches_readiness(self):
        module = _load()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tests")) as tmp:
            root = Path(tmp)
            _write(root, [_record(fingerprint=f"fp{i % 3}") for i in range(10)])
            summary = module.aggregate(root, required_runs=10, min_samples=3)
        self.assertTrue(summary["readiness"]["ready"])
        self.assertEqual(summary["samples"], 3)
        self.assertEqual(summary["features"]["semantic"]["adoption_rate"], 1.0)
        self.assertEqual(summary["reconcile_pass_rate"], 1.0)
        self.assertAlmostEqual(summary["cost_cny"], 0.2, places=6)

    def test_fallback_rate_blocks(self):
        module = _load()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tests")) as tmp:
            root = Path(tmp)
            records = [_record(fingerprint=f"fp{i % 3}") for i in range(10)]
            for record in records[:3]:
                record["metadata"]["semantic_schema"]["adopted"] = False
                record["metadata"]["semantic_schema"]["gate_reason"] = "role_conflict"
            _write(root, records)
            summary = module.aggregate(root)
        self.assertFalse(summary["readiness"]["ready"])
        self.assertEqual(summary["features"]["semantic"]["fallbacks"], 3)
        self.assertAlmostEqual(summary["features"]["semantic"]["fallback_rate"], 0.3, places=4)
        self.assertTrue(any("fallback_rate" in item for item in summary["readiness"]["blockers"]))

    def test_reconcile_failure_blocks(self):
        module = _load()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tests")) as tmp:
            root = Path(tmp)
            records = [_record(fingerprint=f"fp{i % 3}") for i in range(10)]
            records[0]["metadata"]["execution"]["accepted"] = False
            _write(root, records)
            summary = module.aggregate(root)
        self.assertFalse(summary["readiness"]["ready"])
        self.assertLess(summary["reconcile_pass_rate"], 1.0)
        self.assertTrue(any("reconcile_pass_rate" in item for item in summary["readiness"]["blockers"]))

    def test_shadow_modes_are_not_attempted(self):
        module = _load()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "tests")) as tmp:
            root = Path(tmp)
            shadow = {name: "shadow" for name in ("semantic", "plan", "execute", "narrative")}
            _write(root, [_record(fingerprint=f"fp{i % 3}", modes=shadow) for i in range(10)])
            summary = module.aggregate(root)
        self.assertEqual(summary["features"]["semantic"]["attempted"], 0)
        self.assertEqual(summary["overall_fallback_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
