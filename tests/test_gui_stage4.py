from __future__ import annotations

import contextlib
import gc
import io
import logging
import shutil
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.gui import services  # noqa: E402
from insightflow.gui.envcheck import EnvStatus  # noqa: E402

TRUSTED = EnvStatus(state="ok", detail="锁定环境（测试）", interpreter="python", python_version="3.10.4")


def _cleanup_project(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _make_project(rows: int = 3) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":true,"lookback_days":1},"max_charts":8}',
        encoding="utf-8",
    )
    body = [f"2026-11-07,华东,手机数码,线上,{1000 + i},2,1,600" for i in range(rows)]
    (tmp / "raw" / "sales.csv").write_text(
        "订单日期,区域,品类,渠道,销售额,订单量,客户数,成本\n" + "\n".join(body) + "\n",
        encoding="utf-8-sig",
    )
    return tmp


class PackagingAndSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _make_project()
        self.addCleanup(_cleanup_project, self.tmp)
        self.settings = load_settings(project_root=self.tmp)

    def test_scheduler_install_and_uninstall_go_through_core(self):
        with patch("insightflow.scheduler.install_windows_task", return_value=(True, "已创建")) as install:
            result = services.install_schedule(self.settings, "09:30")
        self.assertTrue(result["installed"])
        self.assertEqual(result["time"], "09:30")
        install.assert_called_once()

        with patch("insightflow.scheduler.remove_windows_task", return_value=(True, "已删除")) as remove:
            services.uninstall_schedule(self.settings)
        remove.assert_called_once()

    def test_scheduler_rejects_bad_time_and_reports_failure(self):
        with self.assertRaises(services.ServiceError) as bad:
            services.install_schedule(self.settings, "9点半")
        self.assertIn("HH:MM", str(bad.exception))

        with patch("insightflow.scheduler.install_windows_task", return_value=(False, "拒绝访问")):
            with self.assertRaises(services.ServiceError) as denied:
                services.install_schedule(self.settings, "09:30")
        self.assertIn("拒绝访问", str(denied.exception))

    def test_source_has_no_network_imports(self):
        # The approved LLM bypass (src/insightflow/llm/) is the only place that
        # may touch the network; the analysis pipeline itself stays offline.
        forbidden = (
            "import requests",
            "import socket",
            "import urllib",
            "from urllib",
            "import http.client",
            "from http",
            "import aiohttp",
            "import ftplib",
        )
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            if "llm" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {needle}")
        self.assertEqual(offenders, [], "核心与界面代码不得发起网络请求")

    def test_llm_bypass_is_isolated_from_core(self):
        """Only the assembly module may reference the LLM package; core stays clean."""
        llm_dir = ROOT / "src" / "insightflow" / "llm"
        self.assertTrue(llm_dir.is_dir(), "LLM 旁路目录应存在")

        assembly = ROOT / "src" / "insightflow" / "semantic_assembly.py"
        self.assertTrue(assembly.is_file(), "应存在唯一装配点 semantic_assembly.py")

        offenders = []
        for path in (ROOT / "src" / "insightflow").rglob("*.py"):
            if "llm" in path.parts or path == assembly:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "insightflow.llm" in text or "from .llm" in text or "import .llm" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "除 semantic_assembly.py 外，核心/GUI 不得引用 LLM 包")

        # The whitelist is exact: exactly one assembly point references llm.
        referencing = [
            path.name
            for path in (ROOT / "src" / "insightflow").rglob("*.py")
            if "llm" not in path.parts
            and ("insightflow.llm" in path.read_text(encoding="utf-8", errors="replace")
                 or "from .llm" in path.read_text(encoding="utf-8", errors="replace"))
        ]
        self.assertEqual(referencing, ["semantic_assembly.py"])

        network_users = [
            path.name
            for path in llm_dir.rglob("*.py")
            if "urllib" in path.read_text(encoding="utf-8", errors="replace")
        ]
        self.assertEqual(network_users, ["client.py"], "只有 LLM 客户端允许使用 urllib")

    def test_launchers_exist_and_avoid_console(self):
        vbs = ROOT / "启动操作端(无控制台).vbs"
        self.assertTrue(vbs.is_file())
        text = vbs.read_text(encoding="ascii")
        self.assertIn("pythonw.exe", text)
        self.assertIn("insightflow.gui", text)
        self.assertIn(", 0, False", text)  # window style 0 = no console

        bat = ROOT / "启动操作端.bat"
        self.assertTrue(bat.is_file())
        bat.read_text(encoding="ascii")  # ASCII-only by design

    def test_build_script_describes_portable_layout(self):
        script = (ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8-sig")
        for expected in ("pythonw.exe", "app\\src", "启动操作端.vbs", "robocopy"):
            self.assertIn(expected, script)


class WindowTests(unittest.TestCase):
    def _app(self, settings, tmp):
        import tkinter as tk

        from insightflow.gui.app import InsightFlowApp

        try:
            app = InsightFlowApp(settings, env=TRUSTED)
        except tk.TclError as exc:
            self.skipTest(f"无可用桌面会话：{exc}")
        self.addCleanup(self._dispose, app, tmp)
        return app

    @staticmethod
    def _dispose(app, tmp):
        app.worker.shutdown(2.0)
        try:
            app._on_close()
        except Exception:
            pass
        gc.collect()
        _cleanup_project(tmp)

    def test_install_schedule_confirms_and_reports(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        with patch("insightflow.gui.app.messagebox.askyesno", return_value=True), patch(
            "insightflow.gui.services.install_schedule", return_value={"installed": True, "time": "09:30"}
        ) as install:
            self.assertTrue(app._install_schedule("09:30"))
        install.assert_called_once_with(app.settings, "09:30")
        self.assertIn("09:30", app._status_var.get())

    def test_install_schedule_cancelled_does_nothing(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        with patch("insightflow.gui.app.messagebox.askyesno", return_value=False), patch(
            "insightflow.gui.services.install_schedule"
        ) as install:
            self.assertFalse(app._install_schedule("09:30"))
        install.assert_not_called()

    def test_uninstall_schedule_confirms(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        with patch("insightflow.gui.app.messagebox.askyesno", return_value=True), patch(
            "insightflow.gui.services.uninstall_schedule", return_value={"installed": False}
        ) as uninstall:
            self.assertTrue(app._uninstall_schedule())
        uninstall.assert_called_once_with(app.settings)
        self.assertIn("已卸载", app._status_var.get())

    def test_state_is_readable_without_colour(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        app._date_var.set("2026-11-07")
        with contextlib.redirect_stderr(io.StringIO()):
            app._start_run()
            deadline = time.monotonic() + 40
            while app.summary is None and time.monotonic() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()

        # NFR-8: every colour cue also carries text.
        self.assertTrue(app._score_label.cget("text").strip())
        self.assertIn("质量分", app._score_grade_var.get())
        alerts_text = app._alerts_text.get("1.0", "end")
        self.assertTrue("【" in alerts_text or "未发现质量问题" in alerts_text)
        # Unsupported files are described in words, not only greyed out.
        app._tree.delete(*app._tree.get_children())
        app._fill_tree([{"relative_path": "a.txt", "type": "TXT", "size_text": "1 B", "modified": "—", "supported": False}])
        self.assertEqual(app._tree.item(app._tree.get_children()[0], "values")[4], "不支持的类型")


if __name__ == "__main__":
    unittest.main()
