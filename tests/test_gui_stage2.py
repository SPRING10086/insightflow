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
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.gui import services  # noqa: E402
from insightflow.gui.envcheck import EnvStatus  # noqa: E402

TRUSTED = EnvStatus(state="ok", detail="锁定环境（测试）", interpreter="python", python_version="3.10.4")
BOUNDARY = ROOT / "examples" / "acceptance" / "02_边界样本_含问题.csv"
BROKEN = ROOT / "examples" / "raw" / "broken.xlsx"


def _cleanup_project(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _make_project(with_broken: bool = True) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":true,"lookback_days":1},"max_charts":8}',
        encoding="utf-8",
    )
    shutil.copyfile(BOUNDARY, tmp / "raw" / "02.csv")
    if with_broken:
        shutil.copyfile(BROKEN, tmp / "raw" / "broken.xlsx")
    return tmp


class QualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = _make_project()
        cls.settings = load_settings(project_root=cls.tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            cls.summary = services.run_analysis(cls.settings, date(2026, 11, 7), force=True)
        cls.quality = cls.summary["quality"]
        cls.metrics = json.loads(Path(cls.summary["metrics_path"]).read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        _cleanup_project(cls.tmp)

    def test_five_alert_families_present_and_consistent_with_metrics(self):
        self.assertTrue(self.quality["available"])
        kinds = [alert["kind"] for alert in self.quality["alerts"]]
        self.assertEqual(set(kinds), {"bad_file", "duplicate", "fill", "outlier", "date_order"})
        # Worst first: read failure before duplicate/fill/outlier/date ambiguity.
        self.assertEqual(kinds[0], "bad_file")

        raw_quality = self.metrics["quality"]
        by_kind = {alert["kind"]: alert for alert in self.quality["alerts"]}
        self.assertEqual(by_kind["fill"]["count"], sum(raw_quality["missing_filled"].values()))
        self.assertEqual(by_kind["outlier"]["count"], sum(raw_quality["outlier_counts"].values()))
        self.assertEqual(by_kind["duplicate"]["count"], raw_quality["duplicates_removed"])
        self.assertEqual(by_kind["bad_file"]["count"], 1)
        self.assertEqual(by_kind["date_order"]["count"], 1)

    def test_score_equals_metrics_and_has_grade(self):
        self.assertEqual(self.quality["score"], round(float(self.metrics["quality"]["quality_score"]), 1))
        grade, colour = services.grade_for(self.quality["score"])
        self.assertEqual(self.quality["grade"], grade)
        self.assertEqual(self.quality["colour"], colour)
        self.assertIn("completeness", self.quality["components"])

    def test_imputed_and_outlier_rows_come_from_cleaned_flags(self):
        self.assertGreater(self.quality["imputed_rows"], 0)
        self.assertGreater(self.quality["outlier_rows"], 0)
        flags = services.count_flag_rows(self.summary["cleaned_path"])
        self.assertEqual(self.quality["imputed_rows"], flags["imputed_rows"])
        self.assertEqual(self.quality["outlier_rows"], flags["outlier_rows"])
        # FR-5.3: the operator-facing note must mention both flags.
        note = services.quality_summary_text(self.quality)
        self.assertIn("__imputed", note)
        self.assertIn("__outlier", note)

    def test_bad_file_does_not_break_the_batch(self):
        self.assertEqual(self.summary["status"], "success")
        self.assertEqual(self.summary["files_read"], 1)
        self.assertEqual(len(self.quality["bad_files"]), 1)
        self.assertEqual(self.quality["bad_files"][0]["relative_path"], "broken.xlsx")
        alert = next(item for item in self.quality["alerts"] if item["kind"] == "bad_file")
        self.assertIn("其余 1 个文件正常", alert["advice"])

    def test_grade_bands(self):
        self.assertEqual(services.grade_for(95)[0], "优秀")
        self.assertEqual(services.grade_for(90)[0], "优秀")
        self.assertEqual(services.grade_for(89.9)[0], "良好")
        self.assertEqual(services.grade_for(75.2)[0], "需关注")
        self.assertEqual(services.grade_for(59.9)[0], "较差")


class ConfigAndHistoryTests(unittest.TestCase):
    def test_date_order_persists_and_keeps_other_keys(self):
        tmp = _make_project(with_broken=False)
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)

        path = services.save_date_order(settings, "day_first")
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["date_order"], "day_first")
        self.assertEqual(data["source_dir"], "raw")
        self.assertEqual(load_settings(project_root=tmp).date_order, "day_first")

        with self.assertRaises(ValueError):
            services.save_date_order(settings, "day-later")

    def test_history_rows_carry_status_and_report(self):
        tmp = _make_project(with_broken=False)
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            services.run_analysis(settings, date(2026, 11, 7), force=True)

        rows = services.run_history(settings, limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["status_label"], "成功")
        self.assertEqual(rows[0]["target_date"], "2026-11-07")
        self.assertTrue(Path(rows[0]["report_path"]).is_file())
        self.assertRegex(rows[0]["time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_friendly_errors_give_actionable_text(self):
        self.assertIn("权限", services.friendly_error(PermissionError("denied")))
        self.assertIn("找不到", services.friendly_error(FileNotFoundError("missing")))
        self.assertIn("磁盘空间", services.friendly_error(OSError("locked")))
        self.assertIn("日志", services.friendly_error(ValueError("奇怪的问题")))
        self.assertIn("未知错误", services.friendly_error(RuntimeError("")))


class WindowTests(unittest.TestCase):
    def _app(self, settings, tmp):
        import tkinter as tk

        from insightflow.gui.app import InsightFlowApp

        try:
            app = InsightFlowApp(settings, env=TRUSTED)
        except tk.TclError as exc:  # headless CI: skip instead of failing
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

    def _run(self, app) -> None:
        app._date_var.set("2026-11-07")
        with contextlib.redirect_stderr(io.StringIO()):
            app._start_run()
            deadline = time.monotonic() + 40
            while app.summary is None and time.monotonic() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()

    def test_window_renders_score_alerts_and_bad_files(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)
        self._run(app)

        quality = app.summary["quality"]
        self.assertEqual(app._score_label.cget("text"), f"{quality['score']:.1f}")
        self.assertIn("质量分", app._score_grade_var.get())
        text = app._alerts_text.get("1.0", "end")
        for expected in ("缺失值填补", "离群值标记", "重复行去重", "文件读取失败", "日期存在歧义"):
            self.assertIn(expected, text)
        self.assertIn("broken.xlsx", text)
        self.assertIn("其余 1 个文件正常", text)
        self.assertIn("__imputed", app._coverage_var.get())
        self.assertIn("5 类", app._alert_summary_var.get())

    def test_date_order_change_persists_only_when_confirmed(self):
        tmp = _make_project(with_broken=False)
        app = self._app(load_settings(project_root=tmp), tmp)
        config = tmp / "config" / "config.json"

        app._date_order_var.set("day_first")
        with patch("insightflow.gui.app.messagebox.askyesno", return_value=False):
            app._on_date_order_change()
        self.assertEqual(app.settings.date_order, "day_first")
        self.assertNotEqual(json.loads(config.read_text(encoding="utf-8")).get("date_order"), "day_first")
        self.assertIn("仅本次会话", app._status_var.get())

        app._date_order_var.set("error")
        with patch("insightflow.gui.app.messagebox.askyesno", return_value=True):
            app._on_date_order_change()
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["date_order"], "error")
        self.assertEqual(load_settings(project_root=tmp).date_order, "error")

    def test_window_history_lists_recent_runs(self):
        tmp = _make_project(with_broken=False)
        app = self._app(load_settings(project_root=tmp), tmp)
        self.assertEqual(app._history_var.get(), "暂无运行记录")

        self._run(app)
        rows = app._history_tree.get_children()
        self.assertEqual(len(rows), 1)
        values = app._history_tree.item(rows[0], "values")
        self.assertEqual(values[1], "2026-11-07")
        self.assertEqual(values[2], "成功")
        self.assertIn("双击", app._history_var.get())


if __name__ == "__main__":
    unittest.main()
