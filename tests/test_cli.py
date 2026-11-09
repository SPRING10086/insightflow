from __future__ import annotations

import contextlib
import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow import cli  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.models import STATUS_FAILED, STATUS_NO_DATA, STATUS_SKIPPED, STATUS_SUCCESS, RunRecord  # noqa: E402


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project(with_data: bool = True) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"comparison":{"enabled":true,"lookback_days":1},"max_charts":4}',
        encoding="utf-8",
    )
    if with_data:
        rows = ["订单日期,区域,销售额,订单量"]
        rows += [f"2026-11-07,华东,{100 + i * 10},2" for i in range(4)]
        rows += ["2026-11-06,华东,90,2"]
        (tmp / "raw" / "sales.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    return tmp


def _run(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(args)
    return code, out.getvalue(), err.getvalue()


class CliCommandTests(unittest.TestCase):
    def test_init_creates_config_from_example(self):
        tmp = _project(with_data=False)
        self.addCleanup(_cleanup, tmp)
        (tmp / "config" / "config.example.json").write_text('{"source_dir":"raw"}', encoding="utf-8")
        (tmp / "config" / "config.json").unlink()

        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}):
            code, out, _ = _run(["init", "--project-root", str(tmp)])
        self.assertEqual(code, 0)
        self.assertTrue((tmp / "config" / "config.json").is_file())
        self.assertIn("目录已就绪", out)

    def test_init_without_example_reports_error(self):
        tmp = _project(with_data=False)
        self.addCleanup(_cleanup, tmp)
        (tmp / "config" / "config.json").unlink()
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}):
            code, _out, _err = _run(["init", "--project-root", str(tmp)])
        self.assertEqual(code, 1)

    def test_run_success_json_and_text(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}), contextlib.redirect_stderr(io.StringIO()):
            code, out, _ = _run(["run", "--date", "2026-11-07", "--force"])
            self.assertEqual(code, 0)
            self.assertIn("InsightFlow 运行结果", out)

            code_json, out_json, _ = _run(["run", "--date", "2026-11-07", "--json"])
            self.assertIn(code_json, (0, 3))
            payload = json.loads(out_json)
            self.assertIn("status", payload)

    def test_run_no_data_and_invalid_date(self):
        tmp = _project()
        self.addCleanup(_cleanup, tmp)
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}), contextlib.redirect_stderr(io.StringIO()):
            code, out, _ = _run(["run", "--date", "1999-01-01", "--force"])
            self.assertEqual(code, 2)
            self.assertIn("无新数据", out)

            # scan resolves the date itself, so a malformed date exits loudly.
            with self.assertRaises(SystemExit):
                _run(["scan", "--date", "2026/11/07"])

    def test_scan_missing_and_override_source(self):
        tmp = _project(with_data=False)
        self.addCleanup(_cleanup, tmp)
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}):
            code, _out, err = _run(["scan", "--date", "2026-11-07"])
            self.assertEqual(code, 2)
            self.assertIn("没有 Excel/CSV", err)

            code, _out, err = _run(["scan", "--date", "2026-11-07", "--source", str(tmp / "missing")])
            self.assertEqual(code, 1)
            self.assertIn("不存在", err)

            code, out, _ = _run(["scan", "--date", "2026-11-07", "--source", str(ROOT / "examples" / "acceptance")])
            self.assertIn(code, (0, 2))
            self.assertIn("扫描预览", out)

    def test_logs_empty_and_with_records(self):
        tmp = _project(with_data=False)
        self.addCleanup(_cleanup, tmp)
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}):
            code, out, _ = _run(["logs"])
            self.assertEqual(code, 0)
            self.assertIn("暂无运行记录", out)

            logs = tmp / "logs"
            logs.mkdir(parents=True)
            record = {
                "run_id": "r1",
                "status": "success",
                "target_date": "2026-11-07",
                "started_at": "2026-11-08T09:30:00+08:00",
                "rows_clean": 4,
                "report_path": "out/x.html",
            }
            (logs / "runs.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

            code, out, _ = _run(["logs", "--limit", "5"])
            self.assertEqual(code, 0)
            self.assertIn("2026-11-07", out)

            code, out, _ = _run(["logs", "--json"])
            self.assertIn("r1", out)

    def test_schedule_status_and_dry_run(self):
        tmp = _project(with_data=False)
        self.addCleanup(_cleanup, tmp)
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}), patch(
            "insightflow.cli.query_windows_task", return_value=(True, "TaskName: X")
        ):
            code, out, _ = _run(["schedule", "status"])
            self.assertEqual(code, 0)
            self.assertIn("TaskName", out)

            code, out, _ = _run(["schedule", "status", "--json"])
            payload = json.loads(out)
            self.assertEqual(code, 0)
            self.assertIn("task_name", payload)

            code, out, _ = _run(["schedule", "install", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("Task", out)

    def test_schedule_uninstall_and_daemon(self):
        tmp = _project(with_data=False)
        self.addCleanup(_cleanup, tmp)
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}), patch(
            "insightflow.cli.remove_windows_task", return_value=(True, "deleted")
        ):
            code, _out, _err = _run(["schedule", "uninstall"])
            self.assertEqual(code, 0)

        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}), patch(
            "insightflow.cli.run_daemon", return_value=0
        ) as daemon:
            code, _out, _err = _run(["schedule", "daemon", "--once"])
            self.assertEqual(code, 0)
            daemon.assert_called_once()

    def test_config_error_is_reported(self):
        tmp = _project(with_data=False)
        self.addCleanup(_cleanup, tmp)
        (tmp / "config" / "config.json").write_text("{not json", encoding="utf-8")
        with patch.dict("os.environ", {"INSIGHTFLOW_HOME": str(tmp)}):
            code, _out, err = _run(["scan", "--date", "2026-11-07"])
        self.assertEqual(code, 1)
        self.assertIn("配置错误", err)


class CliHelperTests(unittest.TestCase):
    def _record(self, status: str) -> RunRecord:
        return RunRecord(
            run_id="r",
            trigger="manual",
            status=status,
            target_date="2026-11-07",
            baseline_date=None,
            started_at="2026-11-08T09:30:00+08:00",
            duration_ms=1200,
            report_path="out/x.html" if status == STATUS_SUCCESS else "",
            failure_reason="原因" if status == STATUS_FAILED else "",
            warnings=["w"] if status == STATUS_FAILED else [],
        )

    def test_print_run_covers_every_status(self):
        for status in (STATUS_SUCCESS, STATUS_NO_DATA, STATUS_SKIPPED, STATUS_FAILED):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli._print_run(self._record(status))
            self.assertIn("运行编号", out.getvalue())
        self.assertEqual(cli._exit_code(self._record(STATUS_SUCCESS)), 0)
        self.assertEqual(cli._exit_code(self._record(STATUS_NO_DATA)), 2)
        self.assertEqual(cli._exit_code(self._record(STATUS_SKIPPED)), 3)
        self.assertEqual(cli._exit_code(self._record(STATUS_FAILED)), 1)

    def test_clip_and_target(self):
        self.assertEqual(cli._clip("abc", 10), "abc")
        self.assertTrue(cli._clip("x" * 20, 5).endswith("…"))
        self.assertEqual(cli._clip(None, 5), "")
        settings = load_settings(project_root=ROOT)
        self.assertEqual(cli._target(settings, "2026-11-07"), date(2026, 11, 7))


if __name__ == "__main__":
    unittest.main()
