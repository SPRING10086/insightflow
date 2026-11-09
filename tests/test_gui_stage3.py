from __future__ import annotations

import contextlib
import gc
import io
import json
import logging
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
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


def _make_project(rows: int = 2) -> Path:
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


class FileManagementTests(unittest.TestCase):
    def test_import_files_copies_supported_and_skips_duplicates(self):
        tmp = _make_project()
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)
        incoming = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
        self.addCleanup(_cleanup_project, incoming)
        (incoming / "new.csv").write_text("订单日期,销售额\n2026-11-07,10\n", encoding="utf-8")
        (incoming / "note.txt").write_text("ignore", encoding="utf-8")

        result = services.import_files(
            settings,
            [incoming / "new.csv", incoming / "note.txt", tmp / "raw" / "sales.csv"],
        )
        self.assertEqual(result["imported"], ["new.csv"])
        self.assertTrue((tmp / "raw" / "new.csv").is_file())
        reasons = {item["name"]: item["reason"] for item in result["skipped"]}
        self.assertEqual(reasons["note.txt"], "不支持的类型")
        self.assertEqual(reasons["sales.csv"], "同名文件已存在")

    def test_remove_source_file_moves_into_state(self):
        tmp = _make_project()
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)

        destination = services.remove_source_file(settings, "sales.csv")
        self.assertFalse((tmp / "raw" / "sales.csv").exists())
        self.assertTrue(destination.is_file())
        self.assertIn("removed", str(destination))
        self.assertTrue(str(destination).startswith(str(settings.state_dir)))

        with self.assertRaises(services.ServiceError):
            services.remove_source_file(settings, "../config/config.json")

    def test_preview_source_never_writes_artifacts(self):
        tmp = _make_project()
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)

        preview = services.preview_source(settings, date(2026, 11, 7))
        self.assertTrue(preview["has_target_rows"])
        self.assertEqual(preview["files_read"], 1)
        self.assertEqual(sum(table["target_rows"] for table in preview["tables"]), 2)
        self.assertFalse((tmp / "out").exists(), "预览不应创建输出目录")
        self.assertIn("预计命中 2 行", services.preview_caption(preview))


class ArtefactAndSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _make_project()
        self.addCleanup(_cleanup_project, self.tmp)
        self.settings = load_settings(project_root=self.tmp)

    def test_artifact_entries_cover_the_run_outputs(self):
        with contextlib.redirect_stderr(io.StringIO()):
            summary = services.run_analysis(self.settings, date(2026, 11, 7), force=True)
        labels = [entry["label"] for entry in services.artifact_entries(summary)]
        for expected in ("报告 HTML", "清洗数据 CSV", "指标 metrics.json", "分析计划 analysis_plan.json", "运行记录 run_record.json"):
            self.assertIn(expected, labels)
        self.assertTrue(all(Path(entry["path"]).is_file() for entry in services.artifact_entries(summary)))

    def test_force_flag_bypasses_idempotency(self):
        with contextlib.redirect_stderr(io.StringIO()):
            first = services.run_analysis(self.settings, date(2026, 11, 7), force=True)
            second = services.run_analysis(self.settings, date(2026, 11, 7))
            forced = services.run_analysis(self.settings, date(2026, 11, 7), force=True)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(forced["status"], "success")

    def test_save_paths_persists_and_keeps_other_keys(self):
        config = self.tmp / "config" / "config.json"
        path = services.save_paths(self.settings, "raw2", "out2")
        self.assertEqual(path, config)
        data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data["source_dir"], "raw2")
        self.assertEqual(data["output_dir"], "out2")
        self.assertIn("comparison", data)
        reloaded = load_settings(project_root=self.tmp)
        self.assertEqual(reloaded.source_dir.name, "raw2")
        with self.assertRaises(ValueError):
            services.save_paths(self.settings, "", "out2")

    def test_package_logs_builds_zip_with_environment(self):
        with contextlib.redirect_stderr(io.StringIO()):
            services.run_analysis(self.settings, date(2026, 11, 7), force=True)
        archive_path = services.package_logs(self.settings)
        self.assertTrue(archive_path.is_file())
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertIn("environment.txt", names)
            self.assertTrue(any(name.startswith("logs/") for name in names))
            self.assertIn("pandas=", archive.read("environment.txt").decode("utf-8"))

    def test_wizard_flag_roundtrip(self):
        self.assertFalse(services.wizard_seen(self.settings))
        services.mark_wizard_seen(self.settings)
        self.assertTrue(services.wizard_seen(self.settings))
        state = json.loads((self.settings.state_dir / "gui_state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["wizard_seen"])

    def test_scheduler_caption_states(self):
        installed = services.scheduler_caption(
            {"installed": True, "schedule_time": "09:30", "next_run": "2026-11-10T09:30:00+08:00"}
        )
        self.assertIn("已安装", installed)
        self.assertIn("2026-11-10 09:30", installed)
        missing = services.scheduler_caption({"installed": False, "schedule_time": "09:30", "next_run": ""})
        self.assertIn("未安装", missing)


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

    def test_preview_does_not_create_a_report(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        app._date_var.set("2026-11-07")
        with contextlib.redirect_stderr(io.StringIO()):
            app._preview()
            deadline = time.monotonic() + 30
            while app.worker.running and time.monotonic() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()
        self.assertIsNone(app.summary)
        self.assertFalse((tmp / "out").exists())
        self.assertIn("预计命中", app._stage_var.get())
        dialogs = [child for child in app.winfo_children() if child.winfo_class() == "Toplevel"]
        self.assertTrue(dialogs, "预览应弹出说明窗口")
        for dialog in dialogs:
            dialog.destroy()

    def test_artifacts_menu_fills_after_run(self):
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
        self.assertEqual(app.summary["status"], "success")
        labels = [app._artifacts_menu.entrycget(index, "label") for index in range(app._artifacts_menu.index("end") + 1)]
        self.assertGreaterEqual(len(labels), 4)
        self.assertTrue(app._more_btn.instate(["!disabled"]))

    def test_settings_save_reloads_paths(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        (tmp / "raw2").mkdir()

        with patch("insightflow.gui.app.messagebox.askyesno", return_value=True):
            saved = app._save_settings("raw2", "out2")
        self.assertTrue(saved)
        data = json.loads((tmp / "config" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["source_dir"], "raw2")
        self.assertEqual(app.settings.source_dir, (tmp / "raw2").resolve())
        self.assertEqual(app.files_summary["supported_count"], 0)

    def test_wizard_marks_seen_when_dismissed(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        self.assertFalse(services.wizard_seen(app.settings))
        app._show_wizard()
        dialogs = [child for child in app.winfo_children() if child.winfo_class() == "Toplevel"]
        self.assertTrue(dialogs)
        keep = __import__("tkinter").BooleanVar(value=True)
        app._close_wizard(dialogs[0], keep)
        self.assertTrue(services.wizard_seen(app.settings))


if __name__ == "__main__":
    unittest.main()
