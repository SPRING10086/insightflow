"""Main window of the InsightFlow desktop operator shell.

Stage 1 wires the main path: put data in → run the real pipeline → open the
report and cleaned data.  Layering rule (enforced by review): this module and
``worker.py`` never import the core package; all business calls go through
``insightflow.gui.services``, which in turn calls ``pipeline.execute()``.
"""

from __future__ import annotations

import os
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from ..config import Settings
from . import services
from .envcheck import COLOUR_BAD, EnvStatus, banner, check_environment, gate_message
from .worker import MSG_DONE, MSG_FAILED, MSG_PROGRESS, MSG_STARTED, BackgroundWorker, Progress

POLL_MS = 80
TICK_MS = 200
FONT_FAMILY = "Microsoft YaHei UI"

ZONES: tuple[tuple[str, str, str], ...] = (
    (
        "import",
        "① 数据导入",
        "点击「放入待分析数据」打开数据源目录，把 Excel / CSV 拖进去，再回到窗口点「刷新清单」。",
    ),
    (
        "run",
        "② 运行控制",
        "确认目标日期后点「开始分析」；进度按 读取 → 清洗 → 分析 → 出报告 四段显示。",
    ),
    (
        "result",
        "③ 结果",
        "运行成功后可直接打开清洗数据、报告或报告所在文件夹；失败时会说明原因。",
    ),
    (
        "history",
        "④ 历史记录（阶段 2）",
        "最近 N 次运行：时间 / 目标日期 / 状态 / 行数 / 报告入口。对应 FR-6.1。",
    ),
)


class InsightFlowApp(tk.Tk):
    """Single-window operator shell."""

    def __init__(self, settings: Settings, env: EnvStatus | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.env = env or check_environment(settings.project_root / "requirements.lock.txt")
        self.worker = BackgroundWorker()
        self.zones: dict[str, ttk.LabelFrame] = {}

        # Plain data only: services returns dicts, never core objects.
        self.files_summary: dict | None = None
        self.summary: dict | None = None
        self._run_started = 0.0
        self._pump_job: str | None = None
        self._elapsed_job: str | None = None

        self._status_var = tk.StringVar(value="空闲")
        self._stage_var = tk.StringVar(value="尚未运行。")
        self._files_var = tk.StringVar(value="尚未刷新。")
        self._date_var = tk.StringVar(value=services.default_target_date(settings).isoformat())
        self._result_var = tk.StringVar(value=services.result_caption(None))
        self._elapsed_var = tk.StringVar(value="")

        self.title(f"{settings.app_name} 操作端")
        self.geometry("1024x720")
        self.minsize(960, 640)
        self._configure_style()

        self.columnconfigure(0, weight=1)
        for row, weight in ((0, 0), (1, 2), (2, 2), (3, 1), (4, 1), (5, 0)):
            self.rowconfigure(row, weight=weight)

        self._build_header()
        self._build_zones()
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_job = self.after(POLL_MS, self._pump)
        self.refresh_files()

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
        style.configure("Primary.TButton", font=(FONT_FAMILY, 10, "bold"))

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.settings.project_root).as_posix()
        except ValueError:
            return str(path)

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(12, 10, 12, 4))
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
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

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
            ).grid(row=3, column=0, sticky="ew", pady=(4, 0))

    def _build_zones(self) -> None:
        for index, (key, title, hint) in enumerate(ZONES, start=1):
            frame = ttk.LabelFrame(self, text=title, padding=(10, 6))
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

        self._build_import(self.zones["import"])
        self._build_run(self.zones["run"])
        self._build_result(self.zones["result"])

    def _build_import(self, parent: ttk.LabelFrame) -> None:
        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        bar.columnconfigure(2, weight=1)

        self._folder_btn = ttk.Button(bar, text="放入待分析数据", command=self._open_source_dir)
        self._folder_btn.grid(row=0, column=0, sticky="w")
        self._refresh_btn = ttk.Button(bar, text="刷新清单", command=self.refresh_files)
        self._refresh_btn.grid(row=0, column=1, sticky="w", padx=(6, 10))
        ttk.Label(bar, textvariable=self._files_var, style="Hint.TLabel").grid(row=0, column=2, sticky="w")

        table = ttk.Frame(parent)
        table.grid(row=2, column=0, sticky="nsew")
        parent.rowconfigure(2, weight=1)
        table.columnconfigure(0, weight=1)

        columns = ("name", "type", "size", "modified", "state")
        self._tree = ttk.Treeview(table, columns=columns, show="headings", height=6, selectmode="browse")
        for column, text, width, anchor in (
            ("name", "文件名", 360, "w"),
            ("type", "类型", 70, "center"),
            ("size", "大小", 90, "e"),
            ("modified", "修改时间", 140, "center"),
            ("state", "状态", 140, "w"),
        ):
            self._tree.heading(column, text=text)
            self._tree.column(column, width=width, anchor=anchor, stretch=(column == "name"))
        self._tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(table, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

        self._tree.tag_configure("unsupported", foreground="#9ca3af")
        self._tree.tag_configure("supported", foreground="#111827")

    def _build_run(self, parent: ttk.LabelFrame) -> None:
        row = ttk.Frame(parent)
        row.grid(row=1, column=0, sticky="ew", pady=(6, 2))
        row.columnconfigure(6, weight=1)

        ttk.Label(row, text="目标日期").grid(row=0, column=0, sticky="w")
        self._date_entry = ttk.Entry(row, textvariable=self._date_var, width=12)
        self._date_entry.grid(row=0, column=1, sticky="w", padx=(8, 4))
        self._prev_btn = ttk.Button(row, text="◀", width=3, command=lambda: self._shift_date(-1))
        self._prev_btn.grid(row=0, column=2)
        self._next_btn = ttk.Button(row, text="▶", width=3, command=lambda: self._shift_date(1))
        self._next_btn.grid(row=0, column=3)
        ttk.Label(row, text="默认昨天（配置时区）", style="Hint.TLabel").grid(row=0, column=4, padx=(8, 16))

        self._start_btn = ttk.Button(
            row,
            text="开始分析",
            style="Primary.TButton",
            command=self._start_run,
        )
        self._start_btn.grid(row=0, column=5, sticky="w")

        self._progress = ttk.Progressbar(row, mode="determinate", maximum=services.STAGE_TOTAL)
        self._progress.grid(row=0, column=6, sticky="ew", padx=(12, 0))

        info = ttk.Frame(parent)
        info.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        info.columnconfigure(0, weight=1)
        ttk.Label(info, textvariable=self._stage_var, style="Status.TLabel", wraplength=780, justify="left").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(info, textvariable=self._elapsed_var, style="Hint.TLabel").grid(row=0, column=1, sticky="e")

    def _build_result(self, parent: ttk.LabelFrame) -> None:
        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, sticky="ew", pady=(6, 2))
        bar.columnconfigure(3, weight=1)

        self._clean_btn = ttk.Button(bar, text="获取清洗后数据", command=self._open_cleaned)
        self._clean_btn.grid(row=0, column=0, sticky="w")
        self._report_btn = ttk.Button(bar, text="获取数据报表", command=self._open_report)
        self._report_btn.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self._report_dir_btn = ttk.Button(bar, text="报告所在文件夹", command=self._open_report_dir)
        self._report_dir_btn.grid(row=0, column=2, sticky="w", padx=(6, 0))

        ttk.Label(
            parent,
            textvariable=self._result_var,
            style="Status.TLabel",
            wraplength=940,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        self._update_result_buttons()

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, padding=(12, 0, 12, 10))
        footer.grid(row=5, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, textvariable=self._status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="帮助", command=self._show_help).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(footer, text="打开日志目录", command=self._open_logs).grid(row=0, column=2, padx=(0, 6))

        schedule_btn = ttk.Button(footer, text="定时设置（阶段 3）")
        schedule_btn.state(["disabled"])
        schedule_btn.grid(row=0, column=3)

    # -- data loading ------------------------------------------------------

    def refresh_files(self) -> None:
        """Reload the folder listing (FR-1.2)."""
        try:
            summary = services.list_source_files(self.settings)
        except services.SourceUnavailable as exc:
            self.files_summary = None
            self._files_var.set("数据源不可用")
            self._fill_tree([])
            self._status_var.set(f"数据源不可用：{exc}")
            self._update_start_button()
            return

        self.files_summary = summary
        text = f"共 {summary['total_count']} 个文件，{summary['supported_count']} 个可读取"
        unsupported = summary["total_count"] - summary["supported_count"]
        if unsupported:
            text += f"，{unsupported} 个不支持"
        self._files_var.set(text)
        self._fill_tree(summary["files"])

        if summary["supported_count"] == 0:
            self._status_var.set("数据源为空：请先放入 Excel / CSV 文件，再开始分析。")
        elif not self.worker.running:
            self._status_var.set("空闲")
        self._update_start_button()

    def _fill_tree(self, files: list[dict]) -> None:
        self._tree.delete(*self._tree.get_children())
        for item in files:
            supported = bool(item.get("supported"))
            self._tree.insert(
                "",
                "end",
                values=(
                    item.get("relative_path", ""),
                    item.get("type", ""),
                    item.get("size_text", ""),
                    item.get("modified", ""),
                    "可读取" if supported else "不支持的类型",
                ),
                tags=("supported",) if supported else ("unsupported",),
            )

    def _update_start_button(self) -> None:
        ready = bool(self.files_summary and self.files_summary.get("supported_count")) and not self.worker.running
        self._start_btn.state(["!disabled"] if ready else ["disabled"])

    # -- run ---------------------------------------------------------------

    def _shift_date(self, days: int) -> None:
        try:
            current = date.fromisoformat(self._date_var.get().strip())
        except ValueError:
            current = services.default_target_date(self.settings)
        self._date_var.set((current + timedelta(days=days)).isoformat())

    def _start_run(self) -> None:
        if self.worker.running:
            return
        try:
            target = date.fromisoformat(self._date_var.get().strip())
        except ValueError:
            messagebox.showerror("日期格式不对", "目标日期请使用 YYYY-MM-DD 格式，例如 2026-11-07。")
            return

        lock = services.lock_state(self.settings)
        if lock["locked"]:
            self._status_var.set(f"已有任务在执行（锁已存在 {lock['age_minutes']} 分钟）")
            messagebox.showinfo(
                "已有任务在执行",
                "检测到另一个 InsightFlow 运行正在使用同一数据目录。\n"
                f"锁已存在 {lock['age_minutes']} 分钟；若确认没有其他任务，可在 state 目录删除 .pipeline.lock。",
            )
            return

        if not (self.files_summary and self.files_summary.get("supported_count")):
            messagebox.showinfo("数据源为空", "请先点击「放入待分析数据」，把 Excel / CSV 放进数据源目录，再点「刷新清单」。")
            return

        self._set_busy(True)
        self._run_started = time.perf_counter()
        self._stage_var.set("正在读取数据…")
        self._progress["value"] = 0
        self._update_elapsed()

        def task(progress: Progress) -> dict:
            return services.run_analysis(
                self.settings,
                target,
                force=False,
                on_stage=lambda index, label: progress.stage(
                    label, current=index, total=services.STAGE_TOTAL
                ),
            )

        if not self.worker.submit(task, label="正在执行分析…"):
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self._date_entry, self._prev_btn, self._next_btn, self._refresh_btn, self._folder_btn):
            widget.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self._start_btn.state(["disabled"])
        else:
            self._update_start_button()

    def _update_elapsed(self) -> None:
        if not self.worker.running:
            return
        self._elapsed_var.set(f"已用时 {time.perf_counter() - self._run_started:.1f} s")
        self._elapsed_job = self.after(TICK_MS, self._update_elapsed)

    # -- worker messages ---------------------------------------------------

    def _pump(self) -> None:
        for message in self.worker.pump():
            if message.kind == MSG_STARTED:
                self._status_var.set(str(message.payload.get("label") or "正在执行…"))
            elif message.kind == MSG_PROGRESS:
                payload = message.payload
                total = payload.get("total")
                current = payload.get("current")
                if isinstance(total, (int, float)) and total:
                    self._progress.configure(maximum=total)
                if isinstance(current, (int, float)):
                    # A finished stage fills its slot; the bar never looks stuck.
                    self._progress["value"] = current
                self._stage_var.set(str(payload.get("text", "")))
            elif message.kind == MSG_DONE:
                self.summary = message.payload.get("result") if isinstance(message.payload.get("result"), dict) else None
                self._set_busy(False)
                self._progress["value"] = services.STAGE_TOTAL if (self.summary or {}).get("status") == "success" else 0
                self._stage_var.set(self._stage_text(self.summary))
                self._elapsed_var.set("")
                self._update_result_buttons()
                # Refresh the listing first: it resets the status line when it
                # succeeds, and the run verdict must win.
                self.refresh_files()
                self._status_var.set(f"空闲 · 最近一次运行：{(self.summary or {}).get('status_label', '—')}")
            elif message.kind == MSG_FAILED:
                self._set_busy(False)
                self._elapsed_var.set("")
                self._status_var.set("空闲 · 最近一次运行失败")
                self._stage_var.set("运行没有完成。")
                messagebox.showerror("执行失败", str(message.payload.get("message", "执行失败。")))
        self._pump_job = self.after(POLL_MS, self._pump)

    @staticmethod
    def _stage_text(summary: dict | None) -> str:
        if not summary:
            return "尚未运行。"
        status = summary.get("status")
        if status == "success":
            return (
                f"完成：{summary.get('target_date')} · 读取 {summary.get('rows_target')} 行 → "
                f"清洗后 {summary.get('rows_clean')} 行 · 耗时 {summary.get('duration_text')}"
            )
        reason = summary.get("failure_reason") or services.RESULT_UNAVAILABLE.get(str(status), "")
        return f"结果：{summary.get('status_label')}" + (f" · {reason}" if reason else "")

    # -- results -----------------------------------------------------------

    def _update_result_buttons(self) -> None:
        ok, reason = services.result_availability(self.summary)
        self._result_var.set(services.result_caption(self.summary) if self.summary else reason)
        for widget, usable in (
            (self._clean_btn, bool(ok and (self.summary or {}).get("cleaned_path"))),
            (self._report_btn, bool(ok and (self.summary or {}).get("report_path"))),
            (self._report_dir_btn, bool(ok and (self.summary or {}).get("report_dir"))),
        ):
            widget.state(["!disabled"] if usable else ["disabled"])

    def _open_cleaned(self) -> None:
        self._open_from_summary("cleaned_path", "清洗数据")

    def _open_report(self) -> None:
        self._open_from_summary("report_path", "报告")

    def _open_report_dir(self) -> None:
        self._open_from_summary("report_dir", "报告所在文件夹")

    def _open_from_summary(self, key: str, label: str) -> None:
        path = (self.summary or {}).get(key) or ""
        if not path or not Path(path).exists():
            messagebox.showerror(f"找不到{label}", f"路径不存在：{path or '（空）'}")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(f"无法打开{label}", f"{path}\n{exc}")

    # -- misc --------------------------------------------------------------

    def _open_source_dir(self) -> None:
        target = self.settings.source_dir
        try:
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
            os.startfile(target)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开数据源目录", f"{target}\n{exc}")

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
            "帮助",
            "使用顺序：\n"
            "1. 点「放入待分析数据」，把 Excel / CSV 放进数据源目录；\n"
            "2. 回到窗口点「刷新清单」，确认文件出现在列表里；\n"
            "3. 确认目标日期（默认昨天），点「开始分析」；\n"
            "4. 完成后在「结果」区打开清洗数据或报告。\n\n"
            "数据不会离开本机；分析结果与命令行完全一致。",
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
        for job in (self._pump_job, self._elapsed_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._pump_job = self._elapsed_job = None
        # Flush pending ttk theme/idle events while the interpreter is alive;
        # otherwise a later interpreter reports "application has been destroyed".
        try:
            self.update_idletasks()
        except tk.TclError:
            pass
        # Drop Tcl variables while the interpreter is alive and we are on the UI
        # thread: a later garbage collection inside a worker thread would
        # finalise them there, which aborts the process (Tcl_AsyncDelete).
        self._status_var = None  # type: ignore[assignment]
        self._stage_var = None  # type: ignore[assignment]
        self.destroy()
