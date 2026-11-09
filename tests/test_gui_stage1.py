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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow import pipeline  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.discovery import discover_files  # noqa: E402
from insightflow.gui import services  # noqa: E402
from insightflow.gui.envcheck import EnvStatus  # noqa: E402

TRUSTED = EnvStatus(state="ok", detail="锁定环境（测试）", interpreter="python", python_version="3.10.4")


def _cleanup_project(tmp: Path) -> None:
    """Close log handlers first: Windows keeps logs/*.log locked otherwise."""
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _make_project(rows: list[str] | None = None) -> Path:
    """Temporary project with one CSV (default: two rows on the target date)."""
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":true,"lookback_days":1},"max_charts":8}',
        encoding="utf-8",
    )
    body = rows or [
        "2026-11-07,华东,手机数码,线上,1000,2,1,600",
        "2026-11-07,华南,家用电器,线下,1500,3,2,900",
    ]
    (tmp / "raw" / "sales.csv").write_text(
        "订单日期,区域,品类,渠道,销售额,订单量,客户数,成本\n" + "\n".join(body) + "\n",
        encoding="utf-8-sig",
    )
    return tmp


class FileAndLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings(project_root=ROOT)

    def test_file_listing_supported_set_matches_core_discovery(self):
        listing = services.list_source_files(self.settings)
        gui_supported = {item["relative_path"] for item in listing["files"] if item["supported"]}
        core = {source.relative_path for source in discover_files(self.settings)}
        # If discovery's ignore rules change, the GUI listing must follow.
        self.assertEqual(gui_supported, core)
        self.assertEqual(listing["supported_count"], len(core))

        by_path = {item["relative_path"]: item for item in listing["files"]}
        # examples/raw deliberately contains a non-spreadsheet file.
        self.assertIn("readme.txt", by_path)
        self.assertFalse(by_path["readme.txt"]["supported"])
        self.assertEqual(by_path["readme.txt"]["type"], "TXT")
        # Unsupported files are listed after the supported ones.
        first_unsupported = next(index for index, item in enumerate(listing["files"]) if not item["supported"])
        self.assertTrue(all(item["supported"] for item in listing["files"][:first_unsupported]))

    def test_lock_state_fresh_stale_and_absent(self):
        tmp = _make_project()
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)

        self.assertEqual(services.lock_state(settings)["locked"], False)

        lock = settings.state_dir / ".pipeline.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("{}", encoding="utf-8")
        fresh = services.lock_state(settings)
        self.assertTrue(fresh["locked"])
        self.assertFalse(fresh["stale"])

        old = time.time() - (services.LOCK_STALE_MINUTES + 60) * 60
        import os

        os.utime(lock, (old, old))
        stale = services.lock_state(settings)
        self.assertFalse(stale["locked"])
        self.assertTrue(stale["stale"])

    def test_lock_stale_window_matches_pipeline(self):
        tmp = _make_project()
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)
        self.assertEqual(pipeline._RunLock(settings).stale_seconds, services.LOCK_STALE_MINUTES * 60)


class RunTests(unittest.TestCase):
    def test_stage_mapping_matches_core_step_names(self):
        # The four operator-facing stages must cover every core step name.
        self.assertEqual(sorted(set(services.STAGE_BY_STEP.values())), [1, 2, 3, 4])
        self.assertEqual(set(services.STAGE_LABELS), {1, 2, 3, 4})

    def test_run_analysis_returns_summary_and_reaches_all_stages(self):
        tmp = _make_project()
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)

        seen: list[int] = []
        with contextlib.redirect_stderr(io.StringIO()):
            summary = services.run_analysis(
                settings,
                date(2026, 11, 7),
                on_stage=lambda index, _label: seen.append(index),
            )

        self.assertEqual(summary["status"], "success")
        self.assertTrue(Path(summary["report_path"]).is_file())
        self.assertTrue(Path(summary["cleaned_path"]).is_file())
        self.assertTrue(Path(summary["metrics_path"]).is_file())
        self.assertEqual(summary["rows_target"], 2)
        self.assertEqual(summary["rows_clean"], 2)
        self.assertEqual(sorted(set(seen)), [1, 2, 3, 4])
        # Steps are plain data, no core objects cross the service boundary.
        self.assertTrue(all(isinstance(step, dict) for step in summary["steps"]))
        ok, reason = services.result_availability(summary)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_run_analysis_without_target_rows_is_not_available(self):
        tmp = _make_project(rows=["2026-10-01,华东,手机数码,线上,1000,2,1,600"])
        self.addCleanup(_cleanup_project, tmp)
        settings = load_settings(project_root=tmp)

        with contextlib.redirect_stderr(io.StringIO()):
            summary = services.run_analysis(settings, date(2026, 11, 7))
        self.assertEqual(summary["status"], "no_data")
        self.assertEqual(summary["report_path"], "")
        ok, reason = services.result_availability(summary)
        self.assertFalse(ok)
        self.assertIn("没有目标日期", reason)
        self.assertIn("无数据", services.result_caption(summary))


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

    def test_window_lists_files_and_blocks_empty_source(self):
        tmp = _make_project()
        (tmp / "raw" / "notes.txt").write_text("unsupported", encoding="utf-8")
        app = self._app(load_settings(project_root=tmp), tmp)

        self.assertEqual(len(app._tree.get_children()), 2)
        values = [app._tree.item(item, "values") for item in app._tree.get_children()]
        states = {row[0]: row[4] for row in values}
        self.assertEqual(states["sales.csv"], "可读取")
        self.assertEqual(states["notes.txt"], "不支持的类型")
        self.assertTrue(app._start_btn.instate(["!disabled"]))

        # Release this interpreter before opening a second window.
        self._dispose(app, tmp)

        empty_tmp = _make_project()
        (empty_tmp / "raw" / "sales.csv").unlink()
        empty_app = self._app(load_settings(project_root=empty_tmp), empty_tmp)
        self.assertEqual(len(empty_app._tree.get_children()), 0)
        self.assertTrue(empty_app._start_btn.instate(["disabled"]))
        self.assertIn("数据源为空", empty_app._status_var.get())

    def test_window_runs_pipeline_and_enables_result_buttons(self):
        tmp = _make_project()
        app = self._app(load_settings(project_root=tmp), tmp)

        self.assertTrue(app._clean_btn.instate(["disabled"]))
        app._date_var.set("2026-11-07")
        with contextlib.redirect_stderr(io.StringIO()):
            app._start_run()
            self.assertTrue(app.worker.running)

            deadline = time.monotonic() + 30
            while app.summary is None and time.monotonic() < deadline:
                app.update()
                time.sleep(0.02)
            app.update()

        self.assertIsNotNone(app.summary, "运行未在 30 秒内结束")
        self.assertEqual(app.summary["status"], "success")
        self.assertTrue(app._clean_btn.instate(["!disabled"]))
        self.assertTrue(app._report_btn.instate(["!disabled"]))
        self.assertIn("成功", app._status_var.get())
        self.assertIn("清洗后 2 行", app._stage_var.get())


if __name__ == "__main__":
    unittest.main()
