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

from insightflow.config import load_settings  # noqa: E402
from insightflow.execution import ExecutionResult  # noqa: E402
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


class FakeExecution:
    name = "fake"

    def __init__(self, offset: float = 0.0, fail: bool = False) -> None:
        self.offset = offset
        self.fail = fail
        self.last_code = None

    def execute(self, spec, frame, allowed_columns, fields=None):
        if self.fail:
            raise RuntimeError("sandbox exploded")
        total = float(frame[spec.metric].sum()) + self.offset
        groups = {"columns": [], "rows": []}
        if spec.dimension:
            grouped = frame.groupby(spec.dimension)[spec.metric].sum().head(50)
            groups = {"columns": [spec.dimension], "rows": [[str(k), float(v)] for k, v in grouped.items()]}
        return ExecutionResult(metrics={"total": total}, groups=groups, source_kind="llm", cost_cny=0.01, latency_ms=9)


class ExecutionPipelineTests(unittest.TestCase):
    def test_default_off_writes_nothing(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        self.assertEqual(sorted(p.name for p in day.glob("execution_*")), [])

    def test_success_writes_artifacts_and_keeps_numbers(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        with contextlib.redirect_stderr(io.StringIO()):
            execute(settings, target_date="2026-11-07", force=True)
        baseline = json.loads((day / "metrics.json").read_text(encoding="utf-8"))

        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(settings, target_date="2026-11-07", force=True, execution_provider=FakeExecution())
        self.assertEqual(record.status, "success")
        names = sorted(p.name for p in day.glob("execution_*"))
        self.assertEqual(
            names,
            ["execution_code.json", "execution_diff.json", "execution_llm.json", "execution_rule.json"],
        )
        self.assertTrue(record.metadata["execution"]["accepted"])
        self.assertEqual(record.metadata["execution"]["source_kind"], "llm")
        after = json.loads((day / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["kpis"], after["kpis"])

    def test_reconcile_mismatch_is_not_accepted(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(settings, target_date="2026-11-07", force=True, execution_provider=FakeExecution(offset=100))
        self.assertFalse(record.metadata["execution"]["accepted"])
        diff = json.loads((day / "execution_diff.json").read_text(encoding="utf-8"))
        self.assertFalse(diff["accepted"])
        self.assertTrue(any("total mismatch" in item for item in diff["problems"]))

    def test_provider_failure_falls_back(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        day = settings.output_dir / "2026-11-07"
        with contextlib.redirect_stderr(io.StringIO()):
            record = execute(settings, target_date="2026-11-07", force=True, execution_provider=FakeExecution(fail=True))
        self.assertEqual(record.status, "success")
        self.assertTrue(any("execution_fallback" in item for item in record.warnings))
        llm = json.loads((day / "execution_llm.json").read_text(encoding="utf-8"))
        self.assertEqual(llm["result"]["source_kind"], "fallback")
        self.assertIn("sandbox exploded", llm["result"]["error"])
        self.assertTrue(record.metadata["execution"]["accepted"])


class ExecutionSandboxLimitTests(unittest.TestCase):
    def test_input_over_limit_degrades_before_spawning(self):
        import pandas as pd

        from insightflow.llm.sandbox import SandboxResourceError, run_sandbox

        frame = pd.DataFrame({"a": range(200_001)})
        with self.assertRaises(SandboxResourceError):
            run_sandbox('result = {"metrics": {}, "groups": {"columns": [], "rows": []}}', frame, allowed_columns={"a"})


class CliExecuteDegradeTests(unittest.TestCase):
    def test_execute_llm_without_config_degrades(self):
        from insightflow import cli

        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        keys = ("INSIGHTFLOW_LLM_MODEL", "DEEPSEEK_API_KEY", "INSIGHTFLOW_LLM_PRICE_IN", "INSIGHTFLOW_LLM_PRICE_OUT")
        cleaned = {key: value for key, value in os.environ.items() if key not in keys}
        err = io.StringIO()
        with patch.dict(os.environ, cleaned, clear=True), patch(
            "insightflow.llm.client.dotenv_candidates", return_value=[]
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["run", "--date", "2026-11-07", "--force", "--execute", "llm", "--project-root", str(tmp)])
        self.assertEqual(code, 0)
        self.assertIn("降级", err.getvalue())
        self.assertTrue((tmp / "out" / "2026-11-07" / "metrics.json").is_file())
        self.assertFalse((tmp / "out" / "2026-11-07" / "execution_llm.json").exists())


if __name__ == "__main__":
    unittest.main()
