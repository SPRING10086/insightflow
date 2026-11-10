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

from insightflow.config import ConfigError, LLMSettings, load_settings  # noqa: E402


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
    rows = ["日期,区域,销售额", "2026-11-07,华东,100", "2026-11-06,华东,90"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _write_llm(tmp: Path, llm: dict) -> Path:
    path = tmp / "config" / "config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["llm"] = llm
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class DefaultsTests(unittest.TestCase):
    def test_defaults_are_all_off(self):
        llm = LLMSettings()
        self.assertFalse(llm.enabled)
        for name in ("semantic", "plan", "execute", "narrative"):
            self.assertFalse(getattr(llm, name))
            self.assertFalse(llm.feature_enabled(name))
        self.assertEqual(llm.budget_per_table_cny, 5.0)
        self.assertEqual(llm.budget_batch_cny, 20.0)
        self.assertEqual(llm.memory_mb, 1024)
        self.assertEqual(llm.retry_structured, 2)

    def test_truth_table(self):
        cases = [
            (False, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ]
        for enabled, feature, expected in cases:
            with self.subTest(enabled=enabled, feature=feature):
                llm = LLMSettings(enabled=enabled, semantic=feature)
                self.assertEqual(llm.feature_enabled("semantic"), expected)

    def test_unknown_feature_raises(self):
        with self.assertRaises(ConfigError):
            LLMSettings().feature_enabled("bogus")


class ValidationTests(unittest.TestCase):
    INVALID = [
        {"budget": {"per_table_cny": -1}},
        {"budget": {"per_table_cny": 101}},
        {"budget": {"batch_cny": -1}},
        {"budget": {"batch_cny": 101}},
        {"timeout": {"request_seconds": 0}},
        {"timeout": {"request_seconds": 601}},
        {"timeout": {"sandbox_seconds": 0}},
        {"timeout": {"sandbox_seconds": 601}},
        {"sandbox": {"memory_mb": 32}},
        {"sandbox": {"memory_mb": 4097}},
        {"sandbox": {"max_input_rows": 0}},
        {"retry": {"structured_output": -1}},
        {"retry": {"structured_output": 4}},
        {"retry": {"transport": 4}},
        {"bogus_key": 1},
        {"features": {"bogus": True}},
        {"enabled": "yes"},
        {"budget": {"per_table_cny": "5"}},
    ]

    def test_invalid_values_fail_closed(self):
        for raw in self.INVALID:
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    LLMSettings.from_dict(raw)


class LoadSettingsTests(unittest.TestCase):
    def test_missing_block_uses_defaults(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        self.assertFalse(settings.llm.enabled)
        self.assertFalse(settings.llm.feature_enabled("semantic"))

    def test_partial_block_merges_defaults(self):
        tmp = _project({"enabled": True, "features": {"semantic": True}})
        self.addCleanup(_cleanup, tmp)
        settings = load_settings(project_root=tmp)
        self.assertTrue(settings.llm.feature_enabled("semantic"))
        self.assertFalse(settings.llm.feature_enabled("plan"))
        self.assertEqual(settings.llm.budget_per_table_cny, 5.0)

    def test_invalid_block_aborts_run(self):
        from insightflow import cli

        tmp = _project({"budget": {"per_table_cny": -1}})
        self.addCleanup(_cleanup, tmp)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["run", "--date", "2026-11-07", "--force", "--project-root", str(tmp)])
        self.assertEqual(code, 1)
        self.assertIn("配置", err.getvalue())
        self.assertFalse((tmp / "out" / "2026-11-07" / "metrics.json").exists())


class CliOverrideTests(unittest.TestCase):
    KEYS = ("INSIGHTFLOW_LLM_MODEL", "DEEPSEEK_API_KEY", "INSIGHTFLOW_LLM_PRICE_IN", "INSIGHTFLOW_LLM_PRICE_OUT")

    def _run(self, tmp: Path, args: list[str]) -> tuple[int, str]:
        from insightflow import cli

        cleaned = {key: value for key, value in os.environ.items() if key not in self.KEYS}
        err = io.StringIO()
        with patch.dict(os.environ, cleaned, clear=True), patch(
            "insightflow.llm.client.dotenv_candidates", return_value=[]
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["run", "--date", "2026-11-07", "--force", "--project-root", str(tmp), *args])
        return code, err.getvalue()

    def test_cli_override_does_not_persist(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        before = (tmp / "config" / "config.json").read_bytes()
        code, err = self._run(tmp, ["--semantic", "llm"])
        self.assertEqual(code, 0)
        self.assertIn("降级", err)
        self.assertEqual((tmp / "config" / "config.json").read_bytes(), before)

    def test_config_feature_is_used_when_flag_omitted(self):
        tmp = _project({"enabled": True, "features": {"semantic": True}})
        self.addCleanup(_cleanup, tmp)
        code, err = self._run(tmp, [])
        self.assertEqual(code, 0)
        self.assertIn("降级", err)

    def test_explicit_none_overrides_config(self):
        tmp = _project({"enabled": True, "features": {"semantic": True}})
        self.addCleanup(_cleanup, tmp)
        code, err = self._run(tmp, ["--semantic", "none"])
        self.assertEqual(code, 0)
        self.assertNotIn("降级", err)


if __name__ == "__main__":
    unittest.main()
