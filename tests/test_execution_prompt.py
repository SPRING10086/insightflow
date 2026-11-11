"""Guards for the execution prompt: taught code must pass the real sandbox whitelist."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.llm.sandbox import SandboxViolation, run_sandbox, validate_code  # noqa: E402

PROMPT = ROOT / "src" / "insightflow" / "llm" / "prompts" / "execution_v1.md"


def _prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _example_code() -> str:
    text = _prompt_text()
    block = text.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)["code"]


class ExecutionPromptTests(unittest.TestCase):
    def test_prompt_has_no_loops(self):
        text = _prompt_text().lower()
        self.assertNotIn("for ", text)
        self.assertNotIn("while ", text)

    def test_example_code_has_no_underscore_names(self):
        code = _example_code()
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9])_[A-Za-z]", code))

    def test_example_code_passes_whitelist(self):
        code = _example_code()
        validate_code(code, {"销售额", "订单量"})  # must not raise

    def test_example_code_runs_in_sandbox(self):
        frame = pd.DataFrame({"销售额": [100.0, 200.0, 300.0], "订单量": [1.0, 2.0, 3.0]})
        data = run_sandbox(
            _example_code(),
            frame,
            allowed_columns={"销售额", "订单量"},
            timeout_seconds=20,
        )
        self.assertAlmostEqual(data["metrics"]["销售额"], 600.0)
        self.assertAlmostEqual(data["metrics"]["订单量"], 6.0)
        self.assertEqual(set(data["metric_groups"]), {"销售额", "订单量"})

    def test_for_loop_is_still_rejected(self):
        loop_code = (
            "metrics = {}\n"
            "for name in ['销售额']:\n"
            "    metrics[name] = float(df[name].sum())\n"
            "result = {'metrics': metrics, 'groups': {'columns': [], 'rows': []}}"
        )
        with self.assertRaises(SandboxViolation):
            validate_code(loop_code, {"销售额"})


if __name__ == "__main__":
    unittest.main()
