"""Main window of the InsightFlow desktop operator shell.

Stage 1 wired the main path (put data in → run → open results); stage 2 adds
the trust layer: date-order choice, quality score, the five alert families, bad
files, and the run history.  Layering rule (enforced by review): this module and
``worker.py`` never import the core package; all business calls go through
``insightflow.gui.services``, which in turn calls ``pipeline.execute()``.
"""

from __future__ import annotations

import dataclasses
import os
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..config import Settings, load_settings
from . import services
from .envcheck import COLOUR_BAD, EnvStatus, banner, check_environment, gate_message
from .worker import MSG_DONE, MSG_FAILED, MSG_PROGRESS, MSG_STARTED, BackgroundWorker, Progress

POLL_MS = 80
TICK_MS = 200
FONT_FAMILY = "Microsoft YaHei UI"

#: FR-4.2: explain the ambiguity without inventing terminology.
AMBIGUITY_HINT = (
    "日月都 ≤12 的写法有歧义：默认按「月/日/年」（3 月 4 日），欧式数据选「日/月/年」（4 月 3 日），"
    "也可选「歧义时报错跳过」；跨区域数据建议用 YYYY-MM-DD。"
)

ZONES: tuple[tuple[str, str], ...] = (
    ("import", "① 数据导入 · 放入 Excel/CSV"),
    ("run", "② 运行控制 · 目标日期与日期口径"),
    ("result", "③ 结果与数据质量 · 质量分与告警"),
    ("history", "④ 历史记录 · 双击打开报告"),
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
        self._history_rows: list[dict] = []
        self._run_started = 0.0
        self._pump_job: str | None = None
        self._elapsed_job: str | None = None

        self._status_var = tk.StringVar(value="空闲")
        self._stage_var = tk.StringVar(value="尚未运行。")
        self._files_var = tk.StringVar(value="尚未刷新。")
        self._date_var = tk.StringVar(value=services.default_target_date(settings).isoformat())
        self._date_order_var = tk.StringVar(value=settings.date_order)
        self._result_var = tk.StringVar(value=services.result_caption(None))
        self._elapsed_var = tk.StringVar(value="")
        self._force_var = tk.BooleanVar(value=False)
        self._schedule_var = tk.StringVar(value="定时任务：查询中…")
        self._score_grade_var = tk.StringVar(value="未运行")
        self._score_detail_var = tk.StringVar(value="")
        self._coverage_var = tk.StringVar(value="")
        self._history_var = tk.StringVar(value="")

        self.title(f"{settings.app_name} 操作端")
        self.geometry("1024x720")
        self.minsize(960, 640)
        self._configure_style()

        self.columnconfigure(0, weight=1)
        for row, weight in ((0, 0), (1, 2), (2, 1), (3, 3), (4, 2), (5, 0)):
            self.rowconfigure(row, weight=weight)

        self._build_header()
        self._build_zones()
        self._build_footer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_job = self.after(POLL_MS, self._pump)
        self.refresh_files()
        self._render_quality(None)
        self.refresh_history()
        self._refresh_schedule()

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
        header = ttk.Frame(self, padding=(12, 6, 12, 2))
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
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        ttk.Label(
            header,
            style="Hint.TLabel",
            wraplength=960,
            justify="left",
            text=f"解释器：{self.env.interpreter}（Python {self.env.python_version}）",
        ).grid(row=2, column=0, sticky="w")

        if not self.env.trusted:
            tk.Label(
                header,
                text="⚠ 当前环境不是锁定版本：分析结果可能不可复现，仅建议用于排障。",
                bg="#fef2f2",
                fg=COLOUR_BAD,
                anchor="w",
                padx=8,
                pady=3,
                font=(FONT_FAMILY, 9),
            ).grid(row=3, column=0, sticky="ew", pady=(2, 0))

    def _build_zones(self) -> None:
        for index, (key, title) in enumerate(ZONES, start=1):
            frame = ttk.LabelFrame(self, text=title, padding=(8, 2))
            frame.grid(row=index, column=0, sticky="nsew", padx=10, pady=(0, 3))
            frame.columnconfigure(0, weight=1)
            self.zones[key] = frame

        self._build_import(self.zones["import"])
        self._build_run(self.zones["run"])
        self._build_result(self.zones["result"])
        self._build_history(self.zones["history"])

    def _build_import(self, parent: ttk.LabelFrame) -> None:
        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 3))
        bar.columnconfigure(2, weight=1)

        self._folder_btn = ttk.Button(bar, text="放入待分析数据", command=self._open_source_dir)
        self._folder_btn.grid(row=0, column=0, sticky="w")
        self._choose_btn = ttk.Button(bar, text="选择文件…", command=self._choose_files)
        self._choose_btn.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self._refresh_btn = ttk.Button(bar, text="刷新清单", command=self.refresh_files)
        self._refresh_btn.grid(row=0, column=2, sticky="w", padx=(6, 0))
        self._remove_btn = ttk.Button(bar, text="移除所选", command=self._remove_selected)
        self._remove_btn.grid(row=0, column=3, sticky="w", padx=(6, 0))
        ttk.Label(bar, textvariable=self._files_var, style="Hint.TLabel").grid(row=0, column=4, sticky="w", padx=(10, 0))
        bar.columnconfigure(4, weight=1)

        table = ttk.Frame(parent)
        table.grid(row=2, column=0, sticky="nsew")
        parent.rowconfigure(2, weight=1)
        table.columnconfigure(0, weight=1)

        columns = ("name", "type", "size", "modified", "state")
        self._tree = ttk.Treeview(table, columns=columns, show="headings", height=2, selectmode="browse")
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
        row.grid(row=1, column=0, sticky="ew", pady=(4, 2))

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

        self._preview_btn = ttk.Button(row, text="只预览", command=self._preview)
        self._preview_btn.grid(row=0, column=6, sticky="w", padx=(6, 8))

        self._progress = ttk.Progressbar(row, mode="determinate", maximum=services.STAGE_TOTAL)
        self._progress.grid(row=0, column=7, sticky="ew", padx=(0, 0))
        row.columnconfigure(7, weight=1)

        order = ttk.Frame(parent)
        order.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        order.columnconfigure(5, weight=1)
        ttk.Label(order, text="日期口径").grid(row=0, column=0, sticky="w")
        self._order_buttons: list[ttk.Radiobutton] = []
        for index, (value, label) in enumerate(services.DATE_ORDER_LABELS.items(), start=1):
            button = ttk.Radiobutton(
                order,
                text=label,
                value=value,
                variable=self._date_order_var,
                command=self._on_date_order_change,
            )
            button.grid(row=0, column=index, sticky="w", padx=(10, 0))
            self._order_buttons.append(button)
        ttk.Checkbutton(
            order,
            text="强制重新分析（忽略数据指纹）",
            variable=self._force_var,
        ).grid(row=0, column=4, sticky="w", padx=(16, 0))
        ttk.Label(order, textvariable=self._elapsed_var, style="Hint.TLabel").grid(row=0, column=6, sticky="e")

        ttk.Label(
            parent,
            text=AMBIGUITY_HINT,
            style="Hint.TLabel",
            wraplength=940,
            justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(2, 0))

        ttk.Label(
            parent,
            textvariable=self._stage_var,
            style="Status.TLabel",
            wraplength=940,
            justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(3, 0))

    def _build_result(self, parent: ttk.LabelFrame) -> None:
        body = ttk.Frame(parent)
        body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        parent.rowconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=0)
        body.columnconfigure(2, weight=1)

        # Column 1: quality score badge (FR-5.1).
        score_box = ttk.Frame(body)
        score_box.grid(row=0, column=0, sticky="nw")
        self._score_label = tk.Label(
            score_box,
            text="—",
            width=6,
            height=2,
            bg="#e5e7eb",
            fg="white",
            font=(FONT_FAMILY, 18, "bold"),
        )
        self._score_label.grid(row=0, column=0)
        ttk.Label(score_box, textvariable=self._score_grade_var, style="Hint.TLabel").grid(row=1, column=0, pady=(3, 0))
        ttk.Label(
            score_box,
            textvariable=self._score_detail_var,
            style="Hint.TLabel",
            wraplength=190,
            justify="left",
        ).grid(row=2, column=0, pady=(2, 0))

        # Column 2: result actions + caption.
        actions = ttk.Frame(body)
        actions.grid(row=0, column=1, sticky="nw", padx=(12, 14))
        self._clean_btn = ttk.Button(actions, text="获取清洗后数据", command=self._open_cleaned)
        self._clean_btn.grid(row=0, column=0, sticky="w")
        self._report_btn = ttk.Button(actions, text="获取数据报表", command=self._open_report)
        self._report_btn.grid(row=0, column=1, sticky="w", padx=(4, 0))
        self._report_dir_btn = ttk.Button(actions, text="报告所在文件夹", command=self._open_report_dir)
        self._report_dir_btn.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self._more_btn = ttk.Menubutton(actions, text="更多产物")
        self._artifacts_menu = tk.Menu(self._more_btn, tearoff=False)
        self._more_btn.configure(menu=self._artifacts_menu)
        self._more_btn.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._more_btn.state(["disabled"])

        ttk.Label(
            actions,
            textvariable=self._result_var,
            style="Status.TLabel",
            wraplength=300,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Column 3: alerts (FR-5.2 / 5.3 / 5.4).
        alerts = ttk.Frame(body)
        alerts.grid(row=0, column=2, sticky="nsew")
        alerts.columnconfigure(0, weight=1)
        alerts.rowconfigure(1, weight=1)
        self._alert_summary_var = tk.StringVar(value="尚无质量数据。")
        ttk.Label(
            alerts,
            textvariable=self._alert_summary_var,
            style="Status.TLabel",
            wraplength=500,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        self._alerts_text = tk.Text(
            alerts,
            height=4,
            wrap="word",
            relief="flat",
            bg="#f9fafb",
            fg="#111827",
            font=(FONT_FAMILY, 9),
            padx=8,
            pady=5,
            state="disabled",
        )
        self._alerts_text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(alerts, orient="vertical", command=self._alerts_text.yview)
        self._alerts_text.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")
        self._alerts_text.tag_configure("head_error", foreground="#b91c1c", font=(FONT_FAMILY, 9, "bold"))
        self._alerts_text.tag_configure("head_warn", foreground="#b45309", font=(FONT_FAMILY, 9, "bold"))
        self._alerts_text.tag_configure("head_info", foreground="#0f766e", font=(FONT_FAMILY, 9, "bold"))
        self._alerts_text.tag_configure("body", foreground="#374151")
        self._alerts_text.tag_configure("empty", foreground="#6b7280")

        ttk.Label(
            alerts,
            textvariable=self._coverage_var,
            style="Hint.TLabel",
            wraplength=500,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(3, 0))

        self._update_result_buttons()

    def _build_history(self, parent: ttk.LabelFrame) -> None:
        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 2))
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="刷新历史", command=self.refresh_history).grid(row=0, column=0, sticky="w")
        self._history_open_btn = ttk.Button(bar, text="打开报告", command=self._open_history_selected)
        self._history_open_btn.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self._history_rerun_btn = ttk.Button(bar, text="重跑所选", command=self._rerun_selected)
        self._history_rerun_btn.grid(row=0, column=2, sticky="w", padx=(6, 0))
        ttk.Label(bar, textvariable=self._history_var, style="Hint.TLabel").grid(row=0, column=3, sticky="w", padx=(10, 0))
        bar.columnconfigure(3, weight=1)

        table = ttk.Frame(parent)
        table.grid(row=2, column=0, sticky="nsew")
        parent.rowconfigure(2, weight=1)
        table.columnconfigure(0, weight=1)

        self._history_tree = ttk.Treeview(
            table,
            columns=("time", "target", "status", "rows"),
            show="headings",
            height=2,
            selectmode="browse",
        )
        for column, text, width, anchor in (
            ("time", "运行时间", 150, "w"),
            ("target", "目标日期", 110, "center"),
            ("status", "状态", 100, "center"),
            ("rows", "清洗后行数", 100, "e"),
        ):
            self._history_tree.heading(column, text=text)
            self._history_tree.column(column, width=width, anchor=anchor, stretch=(column == "time"))
        self._history_tree.grid(row=0, column=0, sticky="nsew")
        self._history_tree.bind("<Double-1>", self._open_history_report)

        scroll = ttk.Scrollbar(table, orient="vertical", command=self._history_tree.yview)
        self._history_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, padding=(12, 0, 12, 8))
        footer.grid(row=5, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, textvariable=self._status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(footer, textvariable=self._schedule_var, style="Hint.TLabel").grid(row=0, column=1, padx=(12, 10))
        ttk.Button(footer, text="帮助", command=self._show_help).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(footer, text="打包日志", command=self._package_logs).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(footer, text="管理员设置", command=self._open_settings).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(footer, text="打开日志目录", command=self._open_logs).grid(row=0, column=5)

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

    def refresh_history(self) -> None:
        """FR-6.1: recent runs from logs/runs.jsonl."""
        try:
            self._history_rows = services.run_history(self.settings, limit=10)
        except Exception:  # noqa: BLE001 - history must never break the window
            self._history_rows = []
        self._history_tree.delete(*self._history_tree.get_children())
        for item in self._history_rows:
            self._history_tree.insert(
                "",
                "end",
                values=(item["time"], item["target_date"], item["status_label"], item["rows"]),
            )
        if self._history_rows:
            self._history_var.set(f"最近 {len(self._history_rows)} 次（双击打开报告）")
        else:
            self._history_var.set("暂无运行记录")

    def _open_history_report(self, _event: object = None) -> None:
        self._open_history_selected()

    def _selected_history(self) -> dict | None:
        selection = self._history_tree.selection()
        if not selection:
            return None
        index = self._history_tree.index(selection[0])
        if index >= len(self._history_rows):
            return None
        return self._history_rows[index]

    def _open_history_selected(self) -> None:
        item = self._selected_history()
        if item is None:
            messagebox.showinfo("未选择记录", "请先在历史列表里选中一行。")
            return
        if not item["report_path"]:
            reason = item["failure_reason"] or "该次运行没有生成报告。"
            messagebox.showinfo("没有可打开的报告", f"{item['time']} · {item['status_label']}\n{reason}")
            return
        self._open_path(item["report_path"], "报告")

    def _rerun_selected(self) -> None:
        """FR-6.3: re-run a historical date (commonly after a failure)."""
        item = self._selected_history()
        if item is None:
            messagebox.showinfo("未选择记录", "请先在历史列表里选中一行。")
            return
        target = item["target_date"]
        if item["status"] != "success" and item["failure_reason"]:
            messagebox.showinfo("上次失败原因", f"{item['time']} · {item['status_label']}\n{item['failure_reason']}")
        if not messagebox.askyesno("重跑", f"用目标日期 {target} 重新运行一次分析？"):
            return
        self._date_var.set(target)
        self._start_run()

    # -- date order (FR-4.1 / FR-4.2) -------------------------------------

    def _on_date_order_change(self) -> None:
        value = self._date_order_var.get()
        if value == self.settings.date_order:
            return
        label = services.DATE_ORDER_LABELS.get(value, value)
        # FR-8.4: writing the configuration is a "覆盖配置" action → confirm.
        persist = messagebox.askyesno(
            "写入配置",
            f"日期口径已切换为「{label}」并立即生效。\n\n"
            "是否同时写入 config/config.json，作为以后运行与定时任务的默认口径？\n"
            "（选「否」则只对本次打开的操作端生效）",
        )
        self.settings = dataclasses.replace(self.settings, date_order=value)
        if not persist:
            self._status_var.set(f"日期口径：{label}（仅本次会话）")
            return
        try:
            path = services.save_date_order(self.settings, value)
            self._status_var.set(f"日期口径已写入 {self._display_path(path)}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("写入配置失败", services.friendly_error(exc))
            self._status_var.set("日期口径已生效，但写入配置失败")

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

        settings = self.settings
        force = bool(self._force_var.get())

        def task(progress: Progress) -> dict:
            return services.run_analysis(
                settings,
                target,
                force=force,
                on_stage=lambda index, label: progress.stage(
                    label, current=index, total=services.STAGE_TOTAL
                ),
            )

        if not self.worker.submit(task, label="正在执行分析…"):
            self._set_busy(False)

    # -- preview (FR-2.6) --------------------------------------------------

    def _preview(self) -> None:
        if self.worker.running:
            return
        try:
            target = date.fromisoformat(self._date_var.get().strip())
        except ValueError:
            messagebox.showerror("日期格式不对", "目标日期请使用 YYYY-MM-DD 格式，例如 2026-11-07。")
            return

        self._set_busy(True)
        self._stage_var.set("正在预览（只读取，不生成报告）…")
        settings = self.settings

        def task(progress: Progress) -> dict:
            progress.stage("正在读取数据源（只预览）", current=1, total=1)
            return {"preview": services.preview_source(settings, target)}

        if not self.worker.submit(task, label="正在预览…"):
            self._set_busy(False)

    def _show_preview_dialog(self, preview: dict) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("只预览（不会生成报告）")
        dialog.geometry("760x420")
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=10)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", height=18, font=(FONT_FAMILY, 9))
        text.pack(fill="both", expand=True)
        lines = [services.preview_caption(preview), ""]
        for table in preview["tables"]:
            lines.append(
                f"{table['file']} / {table['sheet']}：共 {table['rows']} 行，"
                f"目标日 {table['target_rows']}，基准日 {table['baseline_rows']}，日期列 {table['date_column']}"
            )
        for item in preview["file_errors"]:
            lines.append(f"读取失败：{item['file']}：{item['error']}")
        for warning in preview["warnings"][:10]:
            lines.append(f"[{warning['code']}] {warning['message']}")
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        ttk.Button(body, text="关闭", command=dialog.destroy).pack(anchor="e", pady=(8, 0))

    # -- file management (FR-1.4 / FR-1.5) --------------------------------

    def _choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择要分析的 Excel / CSV 文件",
            filetypes=[("Excel / CSV", "*.xlsx *.xls *.csv"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        try:
            result = services.import_files(self.settings, list(paths))
        except services.ServiceError as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self.refresh_files()
        parts = []
        if result["imported"]:
            parts.append(f"已复制 {len(result['imported'])} 个文件到数据源目录")
        if result["skipped"]:
            detail = "；".join(f"{item['name']}（{item['reason']}）" for item in result["skipped"][:3])
            parts.append(f"跳过 {len(result['skipped'])} 个：{detail}")
        self._status_var.set("；".join(parts) or "没有可导入的文件")

    def _remove_selected(self) -> None:
        selection = self._tree.selection()
        if not selection:
            messagebox.showinfo("未选择文件", "请先在清单里选中一个文件。")
            return
        relative = str(self._tree.item(selection[0], "values")[0])
        if not messagebox.askyesno(
            "移除文件",
            f"将从数据源目录移除：\n{relative}\n\n"
            "文件会被移动到 state/removed（可恢复），不会被永久删除。继续吗？",
            icon="warning",
        ):
            return
        try:
            destination = services.remove_source_file(self.settings, relative)
        except services.ServiceError as exc:
            messagebox.showerror("移除失败", str(exc))
            return
        self.refresh_files()
        self._status_var.set(f"已移除 {relative}（移入 {destination}）")

    def _set_busy(self, busy: bool) -> None:
        widgets = [
            self._date_entry,
            self._prev_btn,
            self._next_btn,
            self._refresh_btn,
            self._folder_btn,
            self._choose_btn,
            self._remove_btn,
            self._preview_btn,
            self._more_btn,
        ]
        widgets.extend(self._order_buttons)
        for widget in widgets:
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
                    self._progress["value"] = current
                self._stage_var.set(str(payload.get("text", "")))
            elif message.kind == MSG_DONE:
                result = message.payload.get("result")
                self._set_busy(False)
                self._elapsed_var.set("")
                if isinstance(result, dict) and "preview" in result:
                    preview = result["preview"]
                    self._progress["value"] = 0
                    self._stage_var.set(services.preview_caption(preview))
                    self._show_preview_dialog(preview)
                    self._status_var.set("预览完成（未生成报告）")
                else:
                    self.summary = result if isinstance(result, dict) else None
                    self._progress["value"] = services.STAGE_TOTAL if (self.summary or {}).get("status") == "success" else 0
                    self._stage_var.set(self._stage_text(self.summary))
                    self._update_result_buttons()
                    self._render_quality((self.summary or {}).get("quality"))
                    self.refresh_files()
                    self.refresh_history()
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

    # -- quality (FR-5.1 - FR-5.4) ----------------------------------------

    def _render_quality(self, quality: dict | None) -> None:
        if not quality or not quality.get("available"):
            self._score_label.configure(text="—", bg="#e5e7eb")
            self._score_grade_var.set("暂无质量数据")
            self._score_detail_var.set("")
            self._coverage_var.set("")
            self._alert_summary_var.set(services.alerts_summary(None))
            reason = (quality or {}).get("reason") or "尚未运行。"
            self._set_alerts_text([(reason, "empty")])
            return

        self._score_label.configure(text=f"{quality['score']:.1f}", bg=quality["colour"])
        self._score_grade_var.set(f"质量分 · {quality['grade']}")
        components = quality.get("components") or {}
        self._score_detail_var.set(
            f"完整性 {components.get('completeness', '—')} · 重复 {components.get('duplicate_control', '—')}\n"
            f"异常 {components.get('outlier_control', '—')} · 解析 {components.get('parse_quality', '—')}"
        )

        chunks: list[tuple[str, str]] = []
        if not quality["alerts"]:
            chunks.append(("本次未发现质量问题：无填补、无离群、无重复、无坏文件。", "empty"))
        for alert in quality["alerts"]:
            tag = {"error": "head_error", "warning": "head_warn"}.get(alert["severity"], "head_info")
            chunks.append((f"【{alert['type']}】{alert['scope']} · {alert['count_text']}\n", tag))
            chunks.append((f"    {alert['meaning']} {alert['advice']}\n", "body"))
        self._set_alerts_text(chunks)
        self._alert_summary_var.set(services.alerts_summary(quality))

        # FR-5.4 is covered by the first alert entry (file, reason, impact).
        self._coverage_var.set(services.quality_summary_text(quality))

    def _set_alerts_text(self, chunks: list[tuple[str, str]]) -> None:
        self._alerts_text.configure(state="normal")
        self._alerts_text.delete("1.0", "end")
        for text, tag in chunks:
            self._alerts_text.insert("end", text, tag)
        self._alerts_text.configure(state="disabled")
        self._alerts_text.yview_moveto(0)

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
        self._refresh_artifacts_menu()

    def _refresh_artifacts_menu(self) -> None:
        """FR-3.4: metrics.json / analysis_plan.json / run_record.json …"""
        menu = self._artifacts_menu
        menu.delete(0, "end")
        entries = services.artifact_entries(self.summary)
        if not entries:
            menu.add_command(label="（暂无产物）", state="disabled")
            self._more_btn.state(["disabled"])
            return
        for entry in entries:
            menu.add_command(
                label=entry["label"],
                command=lambda path=entry["path"], label=entry["label"]: self._open_path(path, label),
            )
        self._more_btn.state(["!disabled"])

    def _open_cleaned(self) -> None:
        self._open_from_summary("cleaned_path", "清洗数据")

    def _open_report(self) -> None:
        self._open_from_summary("report_path", "报告")

    def _open_report_dir(self) -> None:
        self._open_from_summary("report_dir", "报告所在文件夹")

    def _open_from_summary(self, key: str, label: str) -> None:
        path = (self.summary or {}).get(key) or ""
        self._open_path(path, label)

    def _open_path(self, path: str, label: str) -> None:
        if not path or not Path(path).exists():
            messagebox.showerror(f"找不到{label}", f"路径不存在：{path or '（空）'}")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(f"无法打开{label}", f"{path}\n{services.friendly_error(exc)}")

    # -- misc --------------------------------------------------------------

    # -- settings / schedule / logs (FR-4.3/4.4, 7.1, 8.1, 8.3) ----------

    def _refresh_schedule(self) -> None:
        try:
            self._schedule_var.set(services.scheduler_caption(services.scheduler_status(self.settings)))
        except Exception:  # noqa: BLE001 - a status query must never break the window
            self._schedule_var.set("定时任务：状态未知")

    def _open_settings(self) -> None:
        paths = services.settings_paths(self.settings)
        try:
            status = services.scheduler_status(self.settings)
        except Exception:  # noqa: BLE001
            status = {"installed": False, "schedule_time": self.settings.schedule_time, "next_run": ""}

        dialog = tk.Toplevel(self)
        dialog.title("管理员设置")
        dialog.geometry("720x340")
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        source_var = tk.StringVar(value=str(self.settings.source_dir))
        output_var = tk.StringVar(value=str(self.settings.output_dir))
        ttk.Label(body, text="数据源目录").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=source_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="输出目录").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=output_var).grid(row=1, column=1, sticky="ew", pady=4)

        info = "\n".join(
            [
                f"日志目录：{paths['日志目录']}",
                f"状态目录：{paths['状态目录']}",
                f"配置文件：{paths['配置文件']}",
                services.scheduler_caption(status),
            ]
        )
        ttk.Label(body, text=info, style="Hint.TLabel", wraplength=660, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="关闭", command=dialog.destroy).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(
            buttons,
            text="保存并生效",
            style="Primary.TButton",
            command=lambda: self._save_settings(source_var.get(), output_var.get(), dialog),
        ).grid(row=0, column=1)

    def _save_settings(self, source: str, output: str, dialog: tk.Toplevel | None = None) -> bool:
        # FR-8.4: overwriting the configuration needs confirmation.
        if not messagebox.askyesno("保存设置", "将把数据源 / 输出路径写入 config/config.json 并立即生效，继续吗？"):
            return False
        try:
            path = services.save_paths(self.settings, source, output)
        except (OSError, ValueError) as exc:
            messagebox.showerror("保存失败", services.friendly_error(exc))
            return False
        self.settings = load_settings(project_root=self.settings.project_root)
        self.refresh_files()
        self._refresh_schedule()
        self._status_var.set(f"设置已保存：{self._display_path(path)}")
        if dialog is not None:
            dialog.destroy()
        return True

    def _package_logs(self) -> None:
        try:
            path = services.package_logs(self.settings)
        except services.ServiceError as exc:
            messagebox.showerror("打包日志失败", str(exc))
            return
        if messagebox.askyesno("日志包已生成", f"{path}\n\n是否打开所在文件夹？"):
            self._open_path(str(path.parent), "输出目录")

    def _show_wizard(self) -> None:
        """FR-8.1: first-run guide with a three-step flow."""
        dialog = tk.Toplevel(self)
        dialog.title("首次使用向导")
        dialog.geometry("700x420")
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)

        text = tk.Text(body, wrap="word", height=15, relief="flat", font=(FONT_FAMILY, 10))
        text.pack(fill="both", expand=True)
        text.insert(
            "1.0",
            "三步完成一次分析\n\n"
            "1. 点「放入待分析数据」：打开数据源目录，把 Excel / CSV 放进去；\n"
            "   （也可以点「选择文件…」直接挑选文件，会自动复制到数据源目录）\n\n"
            "2. 点「刷新清单」：确认文件出现在列表里，灰色的「不支持的类型」不会被分析。\n\n"
            "3. 确认「目标日期」（默认昨天）和「日期口径」，点「开始分析」。\n\n"
            "完成后：\n"
            "• 「结果」区可打开清洗后数据、报告与更多产物（metrics / 分析计划）；\n"
            "• 「质量分」与告警说明哪些数字含填补、哪些行是离群，引用前请先看一眼；\n"
            "• 想先看看会读到什么，可以点「只预览」，不会生成报告。\n\n"
            "遇到问题：点「打包日志」把日志发给我们。",
        )
        text.configure(state="disabled")

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(8, 0))
        keep = tk.BooleanVar(value=True)
        ttk.Checkbutton(footer, text="不再显示这个向导", variable=keep).pack(side="left")
        ttk.Button(
            footer, text="知道了", command=lambda: self._close_wizard(dialog, keep)
        ).pack(side="right")

    def _close_wizard(self, dialog: tk.Toplevel, keep: tk.BooleanVar) -> None:
        if bool(keep.get()):
            services.mark_wizard_seen(self.settings)
        dialog.destroy()

    def _open_source_dir(self) -> None:
        target = self.settings.source_dir
        try:
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
            os.startfile(target)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开数据源目录", f"{target}\n{services.friendly_error(exc)}")

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
            "3. 确认目标日期与日期口径，点「开始分析」；\n"
            "4. 看质量分与告警：有填补/离群/歧义时请谨慎引用数字；\n"
            "5. 在「结果」区打开清洗数据或报告。\n\n"
            "数据不会离开本机；分析结果与命令行完全一致。",
        )

    def _open_logs(self) -> None:
        try:
            os.startfile(self.settings.log_dir)  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            messagebox.showerror("无法打开日志目录", f"{self.settings.log_dir}\n{services.friendly_error(exc)}")

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
        for name in (
            "_status_var",
            "_stage_var",
            "_result_var",
            "_coverage_var",
            "_alert_summary_var",
            "_history_var",
            "_schedule_var",
            "_force_var",
            "_score_grade_var",
            "_score_detail_var",
        ):
            setattr(self, name, None)
        self.destroy()
