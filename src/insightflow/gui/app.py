"""Main window of the InsightFlow desktop shell (stage 0 skeleton).

The window is deliberately a *shell*: the four placeholder zones match the
final information architecture, and the only behaviours wired up are the
environment banner, the log shortcut and the background-worker round trip.
Business actions arrive in later stages and must go through
``insightflow.gui.services`` — this module never imports the pipeline.
"""

from __future__ import annotations

import os
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from ..config import Settings
from .envcheck import COLOUR_BAD, EnvStatus, banner, check_environment, gate_message
from .worker import MSG_DONE, MSG_FAILED, MSG_PROGRESS, MSG_STARTED, BackgroundWorker, Progress

POLL_MS = 80
FONT_FAMILY = "Microsoft YaHei UI"

#: (key, frame title, placeholder text).  FR references keep the shell honest.
ZONES: tuple[tuple[str, str, str], ...] = (
    (
        "import",
        "① 数据导入（阶段 1）",
        "按钮「放入待分析数据」+ 待处理文件清单：文件名 / 类型 / 大小 / 修改时间，支持手动刷新。\n"
        "对应 FR-1.1、FR-1.2、FR-1.3、FR-1.6。",
    ),
    (
        "run",
        "② 运行控制（阶段 1）",
        "目标日期选择 + 日期口径 + 主按钮「开始分析」+ 读取→清洗→分析→出报告 的阶段进度。\n"
        "对应 FR-2.1、FR-2.2、FR-2.3、FR-2.4。",
    ),
    (
        "result",
        "③ 结果与质量（阶段 1 / 阶段 2）",
        "质量分与五类告警；按钮「获取清洗后数据」「获取数据报表」及其禁用原因。\n"
        "对应 FR-3.1、FR-3.2、FR-3.3、FR-5.1 – FR-5.4。",
    ),
    (
        "history",
        "④ 历史记录（阶段 2）",
        "最近 N 次运行：时间 / 目标日期 / 状态 / 行数 / 报告入口。\n"
        "对应 FR-6.1。",
    ),
)


def _selfcheck_task(progress: Progress) -> dict[str, object]:
    """Stage-0 self test: prove thread -> queue -> window without touching Tk."""
    stages = ("读取数据源（模拟）", "清洗与去重（模拟）", "生成报告（模拟）")
    started = time.perf_counter()
    for index, text in enumerate(stages, start=1):
        progress.check_cancelled()
        progress.stage(text, current=index, total=len(stages))
        time.sleep(0.6)
    return {"stages": len(stages), "elapsed_ms": int((time.perf_counter() - started) * 1000)}


class InsightFlowApp(tk.Tk):
    """Single-window operator shell."""

    def __init__(self, settings: Settings, env: EnvStatus | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.env = env or check_environment(settings.project_root / "requirements.lock.txt")
        self.worker = BackgroundWorker()
        self.zones: dict[str, ttk.LabelFrame] = {}

        self._status_var = tk.StringVar(value="空闲")
        self._stage_var = tk.StringVar(
            value="尚未运行。阶段 1 将在此显示 读取→清洗→分析→出报告 的真实阶段与耗时。"
        )

        self.title(f"{settings.app_name} 操作端")
        self.geometry("1024x720")
        self.minsize(960, 640)
        self._configure_style()

        self.columnconfigure(0, weight=1)
        for row, weight in ((0, 0), (1, 1), (2, 1), (3, 2), (4, 1), (5, 0)):
            self.rowconfigure(row, weight=weight)

        self._build_header()
        self._build_zones()
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._pump)

    # -- construction ------------------------------------------------------

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        base = (FONT_FAMILY, 10)
        self.option_add("*Font", f"{{{FONT_FAMILY}}} 10")
        style.configure(".", font=base)
        style.configure("Title.TLabel", font=(FONT_FAMILY, 14, "bold"))
        style.configure("Zone.TLabelframe.Label", font=(FONT_FAMILY, 10, "bold"))
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Status.TLabel", foreground="#374151")

    def _display_path(self, path: Path) -> str:
        """Show a path relative to the project root when it lives inside it."""
        try:
            return path.relative_to(self.settings.project_root).as_posix()
        except ValueError:
            return str(path)

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(12, 10, 12, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        top = ttk.Frame(header)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(
            top,
            text=f"{self.settings.app_name} · 本地数据洞察操作端",
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")

        badge_text, badge_colour = banner(self.env)
        tk.Label(
            top,
            text=badge_text,
            bg=badge_colour,
            fg="white",
            padx=10,
            pady=3,
            font=(FONT_FAMILY, 9, "bold"),
        ).grid(row=0, column=1, sticky="e")

        ttk.Label(
            header,
            style="Hint.TLabel",
            text=f"项目根：{self.settings.project_root}    生效配置：{self._display_path(self.settings.config_path)}",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # Hard requirement from the review: the interpreter actually in use must
        # be visible, so a PATH/venv drift can be spotted without a console.
        ttk.Label(
            header,
            style="Hint.TLabel",
            wraplength=960,
            justify="left",
            text=(
                f"运行环境：{self.env.detail}\n"
                f"解释器：{self.env.interpreter}（Python {self.env.python_version}）"
            ),
        ).grid(row=2, column=0, sticky="w")

        if not self.env.trusted:
            tk.Label(
                header,
                text="⚠ 当前环境不是锁定版本：分析结果可能不可复现，仅建议用于排障。",
                bg="#fef2f2",
                fg=COLOUR_BAD,
                anchor="w",
                padx=8,
                pady=4,
                font=(FONT_FAMILY, 9),
            ).grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def _build_zones(self) -> None:
        for index, (key, title, hint) in enumerate(ZONES, start=1):
            frame = ttk.LabelFrame(self, text=title, padding=(10, 8))
            frame.grid(row=index, column=0, sticky="nsew", padx=12, pady=(0, 6))
            frame.columnconfigure(0, weight=1)
            ttk.Label(
                frame,
                text=hint,
                style="Hint.TLabel",
                wraplength=940,
                justify="left",
            ).grid(row=0, column=0, sticky="w")
            self.zones[key] = frame
            if key == "run":
                self._build_selfcheck(frame)

    def _build_selfcheck(self, parent: ttk.LabelFrame) -> None:
        controls = ttk.Frame(parent)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)

        self._selfcheck_btn = ttk.Button(
            controls,
            text="阶段 0 自检：验证后台线程回传",
            command=self._run_selfcheck,
        )
        self._selfcheck_btn.grid(row=0, column=0, sticky="w")

        self._progress = ttk.Progressbar(controls, mode="determinate", maximum=3)
        self._progress.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        ttk.Label(
            parent,
            textvariable=self._stage_var,
            style="Status.TLabel",
            wraplength=940,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, padding=(12, 0, 12, 10))
        footer.grid(row=5, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, textvariable=self._status_var, style="Status.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(footer, text="帮助", command=self._show_help).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(footer, text="打开日志目录", command=self._open_logs).grid(row=0, column=2, padx=(0, 6))

        schedule_btn = ttk.Button(footer, text="定时设置（阶段 3）")
        schedule_btn.state(["disabled"])
        schedule_btn.grid(row=0, column=3)

    # -- behaviour ---------------------------------------------------------

    def _pump(self) -> None:
        """Drain worker messages; never blocks, never long-running itself."""
        for message in self.worker.pump():
            if message.kind == MSG_STARTED:
                self._set_busy(True)
                self._status_var.set(message.payload.get("label") or "正在执行…")
            elif message.kind == MSG_PROGRESS:
                payload = message.payload
                total = payload.get("total")
                current = payload.get("current")
                if isinstance(total, (int, float)) and total:
                    self._progress.configure(maximum=total)
                if isinstance(current, (int, float)):
                    self._progress["value"] = current
                self._stage_var.set(str(payload.get("text", "")))
            elif message.kind == MSG_DONE:
                self._set_busy(False)
                self._progress["value"] = 0
                result = message.payload.get("result") or {}
                self._status_var.set("空闲 · 最近一次后台任务成功")
                self._stage_var.set(
                    "自检完成："
                    f"{result.get('stages', '?')} 个阶段、耗时 {result.get('elapsed_ms', '?')} ms；"
                    "线程 → 队列 → 界面 通路正常。"
                )
            elif message.kind == MSG_FAILED:
                self._set_busy(False)
                self._status_var.set("空闲 · 最近一次后台任务失败")
                messagebox.showerror("执行失败", str(message.payload.get("message", "执行失败。")))
        self.after(POLL_MS, self._pump)

    def _set_busy(self, busy: bool) -> None:
        self._selfcheck_btn.state(["disabled"] if busy else ["!disabled"])

    def _run_selfcheck(self) -> None:
        if not self.worker.submit(_selfcheck_task, label="阶段 0 自检：后台线程正在执行…"):
            messagebox.showinfo("已有任务在执行", "请等待当前后台任务结束后再试。")

    def confirm_environment(self) -> bool:
        """Startup gate: an untrusted environment must be acknowledged.

        Fail-closed: if the dialog cannot be shown, the window does not open.
        """
        message = gate_message(self.env)
        if not message:
            return True
        try:
            answer = messagebox.askyesno("环境不可信", message, icon="warning", default="no")
        except tk.TclError:
            return False
        return bool(answer)

    def _show_help(self) -> None:
        messagebox.showinfo(
            "帮助（阶段 0）",
            "这是操作端的工程骨架，四分区为占位。\n\n"
            "阶段 1 起依次提供：放入数据 → 开始分析 → 获取清洗数据 / 报表。\n"
            "当前可用：环境与配置核对、打开日志目录、后台线程自检。",
        )

    def _open_logs(self) -> None:
        try:
            os.startfile(self.settings.log_dir)  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            messagebox.showerror("无法打开日志目录", f"{self.settings.log_dir}\n{exc}")

    def _on_close(self) -> None:
        if self.worker.running and not messagebox.askyesno("退出", "后台任务仍在执行，确定退出吗？"):
            return
        self.worker.shutdown()
        # Drop Tcl variables while the interpreter is alive and we are on the UI
        # thread: a later garbage collection inside a worker thread would
        # finalise them there, which aborts the process (Tcl_AsyncDelete).
        self._status_var = None  # type: ignore[assignment]
        self._stage_var = None  # type: ignore[assignment]
        self.destroy()
