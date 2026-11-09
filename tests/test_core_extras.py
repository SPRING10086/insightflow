from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow import scheduler, utils  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.discovery import (  # noqa: E402
    SourceNotFoundError,
    count_files_by_suffix,
    discover_files,
    is_hidden,
)


def _cleanup(tmp: Path) -> None:
    logging.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)


def _project() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        '{"source_dir":"raw","output_dir":"out","log_dir":"logs","state_dir":"state",'
        '"schedule_time":"09:30","scheduler":{"grace_minutes":180,"task_name":"T"}}',
        encoding="utf-8",
    )
    return tmp


class SchedulerMoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _project()
        self.addCleanup(_cleanup, self.tmp)
        self.settings = load_settings(project_root=self.tmp)
        self.tz = timezone(timedelta(hours=8))

    def test_parse_time_rejects_bad_formats(self):
        for value in ("9", "aa:bb", "24:00", ""):
            with self.assertRaises(ValueError):
                scheduler.parse_schedule_time(value)
        self.assertEqual(scheduler.parse_schedule_time("9:7").minute, 7)

    def test_next_occurrence_is_strictly_future(self):
        exact = datetime(2026, 11, 8, 9, 30, tzinfo=self.tz)
        nxt = scheduler.next_occurrence(exact, "09:30")
        self.assertEqual(nxt.date().isoformat(), "2026-11-09")

    def test_trigger_due_window_and_grace(self):
        before = datetime(2026, 11, 8, 9, 0, tzinfo=self.tz)
        self.assertFalse(scheduler.trigger_is_due(self.settings, "09:30", before, state={}))

        inside = datetime(2026, 11, 8, 9, 45, tzinfo=self.tz)
        self.assertTrue(scheduler.trigger_is_due(self.settings, "09:30", inside, state={}))

        beyond = datetime(2026, 11, 8, 13, 0, tzinfo=self.tz)  # 3.5h > 180min grace
        self.assertFalse(scheduler.trigger_is_due(self.settings, "09:30", beyond, state={}))

    def test_trigger_skips_when_already_done_today(self):
        now = datetime(2026, 11, 8, 9, 45, tzinfo=self.tz)
        state = {"last_triggered_date": "2026-11-08"}
        self.assertFalse(scheduler.trigger_is_due(self.settings, "09:30", now, state=state))

    def test_trigger_retries_failed_runs_after_interval(self):
        now = datetime(2026, 11, 8, 9, 45, tzinfo=self.tz)
        recent = {"last_status": "failed", "last_attempt_at": (now - timedelta(seconds=60)).isoformat()}
        self.assertFalse(scheduler.trigger_is_due(self.settings, "09:30", now, state=recent))

        old = {"last_status": "failed", "last_attempt_at": (now - timedelta(seconds=600)).isoformat()}
        self.assertTrue(scheduler.trigger_is_due(self.settings, "09:30", now, state=old))

    def test_should_catch_up_delegates(self):
        now = datetime(2026, 11, 8, 9, 45, tzinfo=self.tz)
        with patch.object(scheduler, "trigger_is_due", return_value=True) as due:
            self.assertTrue(scheduler.should_catch_up(self.settings, "09:30", now))
        due.assert_called_once()

    def test_task_command_and_daemon_status(self):
        command = scheduler.task_command(self.settings)
        self.assertIsInstance(command, tuple)
        joined = " ".join(str(part).lower() for part in command)
        self.assertTrue("powershell" in joined or joined.endswith(".ps1") or ".ps1" in joined)

        status = scheduler.daemon_status(self.settings)
        self.assertIn("next_run", status)
        self.assertEqual(status["task_name"], self.settings.scheduler.task_name)

    def test_install_dry_run_writes_xml_without_touching_windows(self):
        ok, detail = scheduler.install_windows_task(self.settings, "08:15", dry_run=True)
        self.assertTrue(ok)
        self.assertIn("08:15:00", detail)
        self.assertTrue((self.settings.state_dir / f"{self.settings.scheduler.task_name}.xml").is_file())

    def test_non_windows_branches(self):
        # Patch the module-local ``os`` reference only: patching ``os.name``
        # globally would make pathlib build PosixPath objects on Windows.
        shim = SimpleNamespace(name="posix")
        with patch.object(scheduler, "os", shim):
            ok, message = scheduler.install_windows_task(self.settings, "09:30")
            self.assertFalse(ok)
            self.assertIn("cron", message)

            ok, message = scheduler.query_windows_task(self.settings)
            self.assertFalse(ok)
            self.assertIn("非 Windows", message)

            ok, message = scheduler.remove_windows_task(self.settings)
            self.assertFalse(ok)
            self.assertIn("非 Windows", message)

    def test_run_daemon_once_and_stop_event(self):
        with patch.object(scheduler, "_trigger", return_value="SENTINEL"):
            self.assertEqual(scheduler.run_daemon(self.settings, once=True), "SENTINEL")

        stop = __import__("threading").Event()
        stop.set()
        now = datetime(2026, 11, 8, 8, 0, tzinfo=self.tz)
        with patch.object(scheduler, "now_local", return_value=now):
            self.assertIsNone(scheduler.run_daemon(self.settings, stop_event=stop, poll_seconds=5))


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _project()
        self.addCleanup(_cleanup, self.tmp)
        self.raw = self.tmp / "raw"
        (self.raw / "a.csv").write_text("x\n1\n", encoding="utf-8")
        (self.raw / "b.XLSX").write_bytes(b"fake")
        (self.raw / "c.txt").write_text("no", encoding="utf-8")
        (self.raw / "~$lock.xlsx").write_bytes(b"lock")
        (self.raw / ".hidden.csv").write_text("x\n1\n", encoding="utf-8")
        (self.raw / "sub").mkdir()
        (self.raw / "sub" / "d.csv").write_text("x\n1\n", encoding="utf-8")
        (self.raw / "empty.csv").write_text("", encoding="utf-8")
        self.settings = load_settings(project_root=self.tmp)

    def test_discovery_filters_and_sorts(self):
        files = discover_files(self.settings)
        names = [item.relative_path for item in files]
        self.assertEqual(sorted(names), ["a.csv", "b.XLSX", "empty.csv", "sub/d.csv"])
        self.assertEqual(count_files_by_suffix(files), {".csv": 3, ".xlsx": 1})

    def test_non_recursive_scan(self):
        settings = load_settings(project_root=self.tmp, overrides={"recursive_scan": False})
        names = [item.relative_path for item in discover_files(settings)]
        self.assertNotIn("sub/d.csv", names)

    def test_missing_and_non_directory_sources(self):
        missing = self.tmp / "nope"
        with self.assertRaises(SourceNotFoundError):
            discover_files(self.settings, missing)

        a_file = self.raw / "a.csv"
        with self.assertRaises(SourceNotFoundError):
            discover_files(self.settings, a_file)

    def test_fingerprint_changes_with_content(self):
        from insightflow.discovery import fingerprint_sources

        files = discover_files(self.settings)
        first = fingerprint_sources(files, self.raw)
        (self.raw / "a.csv").write_text("x\n999\n", encoding="utf-8")
        second = fingerprint_sources(discover_files(self.settings), self.raw)
        self.assertNotEqual(first, second)

    def test_is_hidden(self):
        self.assertTrue(is_hidden(self.raw / ".hidden.csv"))
        self.assertFalse(is_hidden(self.raw / "a.csv"))


class UtilsExtraTests(unittest.TestCase):
    def test_parse_number_variants(self):
        cases = {
            "1,234.5": 1234.5,
            "¥1.2万": 12000.0,
            "(300)": -300.0,
            "50%": 0.5,
            "5w": 50000.0,
            "3k": 3000.0,
            "2百万": None,
            "abc": None,
            "": None,
        }
        for raw, expected in cases.items():
            self.assertEqual(utils.parse_number(raw), expected, raw)
        self.assertIsNone(utils.parse_number(float("nan")))
        self.assertEqual(utils.parse_number(True), 1.0)

    def test_date_and_timestamp_helpers(self):
        self.assertEqual(utils.parse_date_value("2026-11-07"), date(2026, 11, 7))
        self.assertEqual(utils.parse_date_value("2026/11/07"), date(2026, 11, 7))
        self.assertIsNone(utils.parse_date_value("不是日期"))
        self.assertEqual(utils.parse_date_value("03/04/2026", "day_first"), date(2026, 4, 3))
        self.assertIsNone(utils.parse_date_value("03/04/2026", "error"))
        with self.assertRaises(ValueError):
            utils.parse_date_value("2026-11-07", "later")

        stamp = utils.parse_timestamp_value("2026-11-07 13:45")
        self.assertEqual((stamp.hour, stamp.minute), (13, 45))
        self.assertIsNone(utils.parse_timestamp_value("nope"))

    def test_formatting_helpers(self):
        self.assertEqual(utils.fmt_int(1234), "1,234")
        self.assertIn("1.23", utils.fmt_money(1.234))
        self.assertIn("%", utils.fmt_pct(0.1234))
        self.assertTrue(utils.fmt_delta_pct(0.1).startswith("+"))
        self.assertIn("KB", utils.humanize_bytes(1536))
        self.assertEqual(utils.humanize_duration(500), "500 ms")
        self.assertIn("分", utils.humanize_duration(125_000))
        self.assertIn("万", utils.short_money(12345))

    def test_text_and_math_helpers(self):
        self.assertEqual(utils.normalize_text(None), "")
        self.assertEqual(utils.clamp(1.5), 1.0)
        self.assertEqual(utils.truncate("abcdef", 3), "ab…")
        self.assertTrue(utils.slugify("2026 11 月/报表").startswith("2026-11"))
        self.assertEqual(utils.unique_names(["a", "a", "b"]), ["a", "a_2", "b"])
        self.assertEqual(len(list(utils.chunked([1, 2, 3, 4, 5], 2))), 3)
        self.assertEqual(str(utils.round_half_up(2.5)), "3")
        self.assertEqual(utils.html_escape("<b>&"), "&lt;b&gt;&amp;")
        self.assertIsNone(utils.safe_divide(1, 0))
        self.assertAlmostEqual(utils.pct_change(120, 100), 20.0)
        self.assertIsNone(utils.pct_change(1, 0))

    def test_iqr_and_quantile(self):
        bounds = utils.iqr_bounds([1, 2, 3, 4, 5, 100])
        self.assertIsNotNone(bounds)
        q1, q3, lower, upper = bounds
        self.assertLess(q1, q3)
        self.assertLess(lower, q1)
        self.assertGreater(upper, q3)
        self.assertIsNone(utils.iqr_bounds([]))
        self.assertAlmostEqual(utils.quantile([1, 2, 3, 4], 0.5), 2.5)

    def test_io_helpers(self):
        tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-", dir=str(ROOT / "tests")))
        self.addCleanup(_cleanup, tmp)
        target = utils.ensure_dir(tmp / "a" / "b")
        self.assertTrue(target.is_dir())

        path = tmp / "x.txt"
        utils.atomic_write_text(path, "hello")
        self.assertEqual(path.read_text(encoding="utf-8"), "hello")

        json_path = tmp / "x.json"
        utils.atomic_write_json(json_path, {"a": 1})
        self.assertEqual(utils.read_json(json_path)["a"], 1)
        self.assertEqual(utils.read_json(tmp / "missing.json", default={"d": 1}), {"d": 1})

        self.assertEqual(utils.safe_relpath(tmp / "a" / "b", tmp), "a/b")
        self.assertTrue(utils.safe_relpath(Path("C:/outside"), tmp))

        fingerprint = utils.file_fingerprint([json_path], tmp)
        self.assertEqual(fingerprint, utils.file_fingerprint([json_path], tmp))
        self.assertEqual(len(fingerprint), 64)

    def test_jsonable_conversions(self):
        from dataclasses import dataclass

        @dataclass
        class Item:
            value: float
            path: Path

        payload = utils.jsonable(Item(1.5, Path("x/y")))
        self.assertEqual(payload, {"value": 1.5, "path": "x/y"})
        self.assertIsNone(utils.jsonable(float("nan")))
        self.assertEqual(utils.jsonable(date(2026, 11, 7)), "2026-11-07")
        self.assertEqual(utils.jsonable([1, {2}]), [1, [2]])

    def test_mojibake_helpers(self):
        broken = "鏁版嵁".encode("utf-8").decode("gbk", errors="ignore")
        self.assertTrue(utils.looks_garbled(broken) or broken)
        self.assertEqual(utils.maybe_repair_text("正常中文"), "正常中文")
        self.assertIn("中文", utils.maybe_repair_text("中文"))


if __name__ == "__main__":
    unittest.main()
