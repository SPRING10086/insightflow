"""Background execution primitives for the InsightFlow desktop shell.

Tkinter is not thread-safe: widgets may only be touched from the thread that
created them.  Every long operation therefore runs in one worker thread which
communicates *only* through a queue of :class:`Message` objects; the interface
drains that queue from ``after()`` callbacks.

The message kinds are frozen here so later stages extend payloads instead of
re-inventing the protocol.
"""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

MSG_STARTED = "started"
MSG_PROGRESS = "progress"
MSG_DONE = "done"
MSG_FAILED = "failed"

logger = logging.getLogger("insightflow.gui")


class TaskCancelled(Exception):
    """Raised at a cooperative cancel point inside a background task."""


@dataclass
class Message:
    """One worker -> interface event.  ``payload`` carries plain data only."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


def describe_error(exc: BaseException) -> str:
    """Operator-facing sentence for a failure, without a traceback.

    The traceback is written to ``logs/insightflow.log`` by the worker; the
    window only ever receives this short sentence.
    """

    text = str(exc).strip()
    return text if text else f"执行失败（{type(exc).__name__}）。"


class Progress:
    """The only channel a task may use to report back."""

    def __init__(self, emit: Callable[[Message], None], cancel: threading.Event) -> None:
        self._emit = emit
        self._cancel = cancel

    def stage(self, text: str, *, current: int | None = None, total: int | None = None) -> None:
        """Publish a human-readable stage; ``None`` means 'indeterminate'."""
        self._emit(Message(MSG_PROGRESS, {"text": text, "current": current, "total": total}))

    def log(self, text: str) -> None:
        """Write a diagnostic line to the shared log file (not to the window)."""
        logger.info("GUI 任务：%s", text)

    def check_cancelled(self) -> None:
        """Cooperative cancellation point; raises :class:`TaskCancelled`."""
        if self._cancel.is_set():
            raise TaskCancelled("任务已取消。")


class BackgroundWorker:
    """A single daemon worker thread with a message queue.

    Contract:
    - task signature is ``task(progress) -> Any``;
    - the task must not touch tkinter widgets;
    - the interface polls :meth:`pump` from ``after()`` and never blocks.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[Message] = queue.Queue()
        self._cancel = threading.Event()
        self._state = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """True while a task is being executed (used to disable buttons)."""
        with self._state:
            return self._running

    def submit(self, task: Callable[[Progress], Any], *, label: str = "") -> bool:
        """Start ``task``; returns False when a task is already running.

        Refusing instead of queueing is what makes the window's "one run at a
        time" rule hold even if the operator double-clicks.
        """
        if not callable(task):
            raise TypeError("task 必须是可调用对象，签名为 task(progress) -> Any")
        with self._state:
            if self._running:
                return False
            self._running = True
            self._cancel.clear()
        self._queue.put(Message(MSG_STARTED, {"label": label}))
        self._thread = threading.Thread(
            target=self._run,
            args=(task,),
            name="insightflow-gui-worker",
            daemon=True,
        )
        self._thread.start()
        return True

    def pump(self, limit: int = 50) -> list[Message]:
        """Drain up to ``limit`` messages without ever blocking."""
        messages: list[Message] = []
        for _ in range(max(0, limit)):
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return messages

    def cancel(self) -> None:
        """Ask the task to stop at its next cooperative cancel point."""
        self._cancel.set()

    def shutdown(self, timeout: float = 2.0) -> bool:
        """Cancel and join; returns True when the thread finished in time."""
        self._cancel.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        return not (thread is not None and thread.is_alive())

    def _run(self, task: Callable[[Progress], Any]) -> None:
        progress = Progress(self._queue.put, self._cancel)
        try:
            result = task(progress)
        except TaskCancelled:
            logger.info("GUI 后台任务已取消")
            self._queue.put(Message(MSG_FAILED, {"message": "任务已取消。", "error_type": "TaskCancelled"}))
        except Exception as exc:  # noqa: BLE001 - the worker is the error boundary
            # Traceback goes to the log only; the window gets one short sentence.
            logger.error("GUI 后台任务失败：%s\n%s", describe_error(exc), traceback.format_exc())
            self._queue.put(
                Message(MSG_FAILED, {"message": describe_error(exc), "error_type": type(exc).__name__})
            )
        else:
            self._queue.put(Message(MSG_DONE, {"result": result}))
        finally:
            with self._state:
                self._running = False
