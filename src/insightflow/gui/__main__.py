"""``python -m insightflow.gui`` entry point."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from ..config import ConfigError, load_settings
from ..logging_setup import configure_logging, get_logger
from . import services
from .app import InsightFlowApp


def _ensure_standard_streams() -> None:
    """``pythonw.exe`` has no console; logging and prints must not crash.

    Only the missing-stream case is replaced.  Instead of throwing the output
    away, it is appended to ``%TEMP%\\insightflow_gui_stderr.log`` so a windowed
    launch that fails early can still be diagnosed (the bundled launcher uses
    ``pythonw.exe`` on purpose, to avoid the black console window).
    """
    if sys.stderr is not None and sys.stdout is not None:
        return
    target = Path(tempfile.gettempdir()) / "insightflow_gui_stderr.log"
    try:
        handle = open(target, "a", encoding="utf-8", buffering=1)
    except OSError:
        handle = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = handle
    if sys.stdout is None:
        sys.stdout = handle


def _enable_dpi_awareness() -> None:
    """Crisp text on scaled Windows displays (1366x768 laptop panels)."""
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - purely cosmetic, never fatal
        pass


def _report_startup_error(message: str) -> None:
    """Show configuration problems without a traceback."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("InsightFlow 操作端无法启动", message)
        root.destroy()
    except Exception:  # noqa: BLE001 - last resort, console/log only
        print(f"InsightFlow 操作端无法启动：{message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _ensure_standard_streams()
    try:
        settings = load_settings()
    except ConfigError as exc:
        _report_startup_error(f"配置文件有误：{exc}\n请检查 config/config.json 或先运行 init 生成模板。")
        return 1

    # Same logger, same log directory as the CLI and the scheduled task.
    configure_logging(settings)
    get_logger("insightflow.gui").info(
        "GUI 启动：python=%s 解释器=%s 配置=%s",
        sys.version.split()[0],
        sys.executable,
        settings.config_path,
    )
    _enable_dpi_awareness()

    app = InsightFlowApp(settings)
    # Startup gate: an unverified environment needs explicit consent.
    if not app.confirm_environment():
        app.destroy()
        return 2
    # First-run guide (FR-8.1) — shown once, dismissed state kept in state/.
    if not services.wizard_seen(settings):
        app._show_wizard()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
