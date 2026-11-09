from __future__ import annotations

import gc
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
from insightflow.gui import services  # noqa: E402
from insightflow.gui.envcheck import EnvStatus  # noqa: E402

TRUSTED = EnvStatus(state="ok", detail="锁定环境（测试）", interpreter="python", python_version="3.10.4")


def _project() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        json.dumps(
            {
                "source_dir": "raw",
                "output_dir": "out",
                "log_dir": "logs",
                "state_dir": "state",
                "llm": {"enabled": False, "budget": {"batch_cny": 7.0}},
            }
        ),
        encoding="utf-8",
    )
    rows = ["日期,区域,销售额", "2026-11-07,华东,100", "2026-11-06,华东,90"]
    (tmp / "raw" / "a.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


class LlmServiceTests(unittest.TestCase):
    def test_default_providers_are_off(self):
        tmp = _project()
        self.addCleanup(lambda: (logging.shutdown(), shutil.rmtree(tmp, ignore_errors=True)))
        settings = load_settings(project_root=tmp)
        providers, warnings = services.build_llm_providers(settings)
        self.assertEqual(warnings, [])
        self.assertTrue(all(value is None for value in providers.values()))

    def test_save_llm_settings_persists_and_preserves_budget(self):
        tmp = _project()
        self.addCleanup(lambda: (logging.shutdown(), shutil.rmtree(tmp, ignore_errors=True)))
        settings = load_settings(project_root=tmp)
        updated = services.save_llm_settings(
            settings, enabled=True, features={"semantic": True, "plan": True}
        )
        self.assertTrue(updated.llm.enabled)
        self.assertTrue(updated.llm.feature_enabled("semantic"))
        self.assertTrue(updated.llm.feature_enabled("plan"))
        self.assertFalse(updated.llm.feature_enabled("narrative"))
        self.assertEqual(updated.llm.budget_batch_cny, 7.0)  # preserved from config
        payload = json.loads((tmp / "config" / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["llm"]["enabled"])

    def test_enabled_without_key_degrades_with_warning(self):
        tmp = _project()
        self.addCleanup(lambda: (logging.shutdown(), shutil.rmtree(tmp, ignore_errors=True)))
        settings = services.save_llm_settings(
            load_settings(project_root=tmp), enabled=True, features={"semantic": True}
        )
        cleaned = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DEEPSEEK_API_KEY", "INSIGHTFLOW_LLM_API_KEY"}
        }
        with patch.dict(os.environ, cleaned, clear=True), patch(
            "insightflow.llm.client.dotenv_candidates", return_value=[]
        ):
            providers, warnings = services.build_llm_providers(settings)
        self.assertIsNone(providers["semantic_provider"])
        self.assertTrue(any("未启用" in item for item in warnings))

    def test_result_text_default_and_llm(self):
        self.assertIn("未启用", services.llm_result_text(None))
        summary = {
            "llm": {
                "enabled": True,
                "cost_cny": 0.0123,
                "semantic": "llm",
                "plan": "llm",
                "execution": "fallback",
                "narrative": "",
                "reconcile": False,
                "reasons": ["对账不一致"],
            }
        }
        text = services.llm_result_text(summary)
        self.assertIn("成本", text)
        self.assertIn("语义=LLM", text)
        self.assertIn("执行=已降级", text)
        self.assertIn("对账未通过", text)


class LlmPanelTests(unittest.TestCase):
    def _dispose(self, app, tmp):
        app.worker.shutdown(2.0)
        try:
            app._on_close()
        except Exception:
            pass
        gc.collect()
        logging.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    def test_panel_defaults_off_and_has_switches(self):
        import tkinter as tk

        from insightflow.gui.app import InsightFlowApp

        tmp = _project()
        settings = load_settings(project_root=tmp)
        try:
            app = InsightFlowApp(settings, env=TRUSTED)
        except tk.TclError as exc:
            self.skipTest(f"无可用桌面会话：{exc}")
        self.addCleanup(self._dispose, app, tmp)

        self.assertIn("LLM 增强", app._llm_btn.cget("text"))
        self.assertIn("未启用", app._llm_var.get())

        app._open_llm_dialog()
        dialogs = [child for child in app.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertTrue(dialogs)
        dialog = dialogs[-1]
        checkbuttons = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Checkbutton) or child.winfo_class() == "TCheckbutton":
                    checkbuttons.append(child)
                walk(child)

        walk(dialog)
        self.assertGreaterEqual(len(checkbuttons), 5)  # master + four features
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
