from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.gui.envcheck import (  # noqa: E402
    STATE_MISMATCH,
    STATE_OK,
    STATE_UNKNOWN,
    check_environment,
    gate_message,
)
from insightflow.gui.worker import (  # noqa: E402
    MSG_DONE,
    MSG_FAILED,
    MSG_PROGRESS,
    MSG_STARTED,
    BackgroundWorker,
    TaskCancelled,
)

LOCK = ROOT / "requirements.lock.txt"


def _drain_until(worker: BackgroundWorker, kinds: set[str], timeout: float = 5.0) -> list:
    """Collect messages until one of ``kinds`` arrives (bounded wait)."""
    seen: list = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen.extend(worker.pump())
        if any(message.kind in kinds for message in seen):
            return seen
        time.sleep(0.01)
    raise AssertionError(f"等待 {sorted(kinds)} 超时，已收到 {[message.kind for message in seen]}")


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = BackgroundWorker()

    def tearDown(self) -> None:
        self.worker.shutdown(2.0)

    def test_worker_emits_started_progress_done(self):
        def task(progress):
            progress.stage("一", current=1, total=2)
            progress.stage("二", current=2, total=2)
            return "ok"

        self.assertTrue(self.worker.submit(task, label="自检"))
        seen = _drain_until(self.worker, {MSG_DONE})
        self.assertEqual(
            [message.kind for message in seen],
            [MSG_STARTED, MSG_PROGRESS, MSG_PROGRESS, MSG_DONE],
        )
        self.assertEqual(seen[1].payload, {"text": "一", "current": 1, "total": 2})
        self.assertEqual(seen[-1].payload["result"], "ok")
        self.assertTrue(self.worker.shutdown(2.0))
        self.assertFalse(self.worker.running)

    def test_worker_rejects_concurrent_submit(self):
        def slow(progress):
            time.sleep(0.5)
            return "done"

        self.assertTrue(self.worker.submit(slow))
        self.assertTrue(self.worker.running)
        # The window disables its button, but the worker must refuse on its own.
        self.assertFalse(self.worker.submit(lambda progress: "second"))

    def test_worker_reports_failure_without_traceback(self):
        def boom(progress):
            raise ValueError("数据源目录不可读")

        # The traceback is logged here; assert it never reaches the payload.
        with self.assertLogs("insightflow.gui", level="ERROR"):
            self.worker.submit(boom)
            seen = _drain_until(self.worker, {MSG_FAILED})
        failed = [message for message in seen if message.kind == MSG_FAILED][0]
        self.assertEqual(failed.payload["error_type"], "ValueError")
        self.assertIn("数据源目录不可读", failed.payload["message"])
        # Traceback goes to the log file only (FR-8.2).
        self.assertNotIn("Traceback", failed.payload["message"])

    def test_progress_cancel_raises_task_cancelled(self):
        def loop(progress):
            for _ in range(200):
                progress.check_cancelled()
                time.sleep(0.01)
            return "finished"

        self.worker.submit(loop)
        time.sleep(0.05)
        self.worker.cancel()
        seen = _drain_until(self.worker, {MSG_FAILED}, timeout=3.0)
        failed = [message for message in seen if message.kind == MSG_FAILED][0]
        self.assertEqual(failed.payload["error_type"], TaskCancelled.__name__)

    def test_pump_returns_empty_without_blocking(self):
        started = time.perf_counter()
        self.assertEqual(self.worker.pump(), [])
        self.assertLess(time.perf_counter() - started, 0.1)

    def test_shutdown_joins_running_task(self):
        self.worker.submit(lambda progress: time.sleep(0.3) or "ok")
        time.sleep(0.05)
        self.assertTrue(self.worker.shutdown(2.0))
        self.assertFalse(self.worker.running)


class EnvironmentTests(unittest.TestCase):
    def test_environment_check_matches_lock(self):
        status = check_environment(LOCK)
        self.assertEqual(status.state, STATE_OK)
        self.assertTrue(status.trusted)
        self.assertEqual(status.expected["pandas"], "2.3.3")
        self.assertEqual(status.expected["numpy"], "2.2.6")
        self.assertTrue(status.interpreter)
        # A trusted environment starts without any prompt.
        self.assertEqual(gate_message(status), "")

    def test_environment_check_blocks_drift_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "requirements.lock.txt"
            lock.write_text("pandas==0.0.1\nnumpy==0.0.1\n", encoding="utf-8")
            drifted = check_environment(lock)
            self.assertEqual(drifted.state, STATE_MISMATCH)
            self.assertFalse(drifted.trusted)
            self.assertIn("环境", gate_message(drifted))

            unknown = check_environment(Path(tmp) / "missing.lock")
            self.assertEqual(unknown.state, STATE_UNKNOWN)
            self.assertFalse(unknown.trusted)
            self.assertIn("环境", gate_message(unknown))


class WindowTests(unittest.TestCase):
    def test_window_builds_four_zones_and_closes_clean(self):
        import gc
        import tkinter as tk

        from insightflow.gui.app import InsightFlowApp

        try:
            app = InsightFlowApp(load_settings(project_root=ROOT))
        except tk.TclError as exc:  # headless CI: skip instead of failing
            self.skipTest(f"无可用桌面会话：{exc}")

        try:
            self.assertEqual(set(app.zones), {"import", "run", "result", "history"})
            self.assertIn("InsightFlow", app.title())
            self.assertTrue(app.env.interpreter)
            # C1 baseline: 1024x720 default, 960x640 floor (1366x768 laptops).
            self.assertEqual(app.minsize(), (960, 640))
            self.assertEqual([frame.cget("text") for frame in app.zones.values()][0][:1], "①")

            # Hard condition 1: an untrusted environment must be gated, and a
            # declined prompt must keep the window closed (fail-closed).
            import dataclasses
            from unittest.mock import patch

            app.env = dataclasses.replace(app.env, state=STATE_MISMATCH, detail="测试：环境偏离")
            with patch("insightflow.gui.app.messagebox.askyesno", return_value=False) as ask:
                self.assertFalse(app.confirm_environment())
            ask.assert_called_once()

            # WM_DELETE_WINDOW path: worker stops, window goes away.
            app._on_close()
            self.assertFalse(app.worker.running)
        finally:
            app.worker.shutdown(2.0)
            try:
                app.destroy()
            except tk.TclError:
                pass
            # Tk keeps widget/parent cycles; collect them on the main thread so
            # no worker thread finalises the Tcl interpreter later in the suite.
            del app
            gc.collect()


if __name__ == "__main__":
    unittest.main()
