"""Command-line entry points for InsightFlow."""

from __future__ import annotations

import argparse
import json
import runpy
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

from . import __app_name__, __version__
from .config import ConfigError, Settings, load_settings
from .discovery import SourceNotFoundError, discover_files
from .logging_setup import read_run_history
from .models import RunRecord, STATUS_FAILED, STATUS_NO_DATA, STATUS_SKIPPED, STATUS_SUCCESS
from .pipeline import execute
from .readers import read_sources
from .scheduler import (
    daemon_status,
    install_windows_task,
    query_windows_task,
    remove_windows_task,
    run_daemon,
)
from .schema import detect_schema
from .normalize import extract_periods
from .utils import jsonable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insightflow",
        description="InsightFlow · 本地数据清洗、自动分析与单文件离线报告流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m insightflow run --date 2026-11-07 --force\n"
            "  python -m insightflow scan --date 2026-11-07\n"
            "  python -m insightflow schedule install --dry-run\n"
            "  python -m insightflow schedule daemon --once\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="配置文件路径（默认 config/config.json）")
    common.add_argument("--project-root", help="项目根目录（默认自动探测）")

    run = subparsers.add_parser("run", parents=[common], help="执行一次完整流水线")
    run.add_argument("--date", help="报告日期 YYYY-MM-DD；默认配置时区的昨天")
    run.add_argument("--source", help="临时覆盖数据源目录")
    run.add_argument("--trigger", choices=["manual", "scheduled"], default="manual")
    run.add_argument("--force", action="store_true", help="忽略数据指纹，强制重新生成")
    run.add_argument("--json", action="store_true", help="以 JSON 输出运行记录")

    scan = subparsers.add_parser("scan", parents=[common], help="只扫描与预览，不生成报告")
    scan.add_argument("--date", help="目标日期 YYYY-MM-DD；默认昨天")
    scan.add_argument("--source", help="临时覆盖数据源目录")
    scan.add_argument("--json", action="store_true")

    demo = subparsers.add_parser("demo", parents=[common], help="生成演示数据并运行一次")
    demo.add_argument("--date", help="演示报告日期；默认昨天")
    demo.add_argument("--force", action="store_true", default=True, help="演示默认强制重算")
    demo.add_argument("--json", action="store_true")

    logs = subparsers.add_parser("logs", parents=[common], help="查看最近运行记录")
    logs.add_argument("--limit", type=int, default=10)
    logs.add_argument("--json", action="store_true")

    init = subparsers.add_parser("init", parents=[common], help="初始化目录与配置文件")
    init.add_argument("--force", action="store_true", help="覆盖已有 config.json")

    schedule = subparsers.add_parser("schedule", parents=[common], help="定时任务管理")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    install = schedule_sub.add_parser("install", parents=[common], help="注册 Windows 每日计划任务")
    install.add_argument("--time", help="每日触发时间 HH:MM；默认读取配置")
    install.add_argument("--task-name", help="计划任务名称")
    install.add_argument("--dry-run", action="store_true", help="只打印任务 XML，不写入系统")
    status = schedule_sub.add_parser("status", parents=[common], help="查看计划任务与 daemon 状态")
    status.add_argument("--task-name")
    status.add_argument("--json", action="store_true")
    uninstall = schedule_sub.add_parser("uninstall", parents=[common], help="删除 Windows 计划任务")
    uninstall.add_argument("--task-name")
    daemon = schedule_sub.add_parser("daemon", parents=[common], help="启动跨平台内置调度器")
    daemon.add_argument("--time", help="每日触发时间 HH:MM；默认读取配置")
    daemon.add_argument("--once", action="store_true", help="立即执行一次后退出")
    daemon.add_argument("--poll-seconds", type=int, default=30)
    daemon.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(
            config_path=getattr(args, "config", None),
            project_root=getattr(args, "project_root", None),
        )
    except ConfigError as exc:
        _error(f"配置错误: {exc}")
        return 1

    if args.command == "run":
        return _cmd_run(settings, args)
    if args.command == "scan":
        return _cmd_scan(settings, args)
    if args.command == "demo":
        return _cmd_demo(settings, args)
    if args.command == "logs":
        return _cmd_logs(settings, args)
    if args.command == "init":
        return _cmd_init(settings, args)
    if args.command == "schedule":
        return _cmd_schedule(settings, args)
    parser.print_help()
    return 1


def _cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    record = execute(
        settings,
        target_date=args.date,
        trigger=args.trigger,
        force=args.force,
        source_dir=args.source,
    )
    if args.json:
        print(json.dumps(jsonable(record), ensure_ascii=False, indent=2))
    else:
        _print_run(record)
    return _exit_code(record)


def _cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    source_root = Path(args.source).expanduser().resolve() if args.source else settings.source_dir
    target = _target(settings, args.date)
    baseline = settings.baseline_date(target)
    try:
        files = discover_files(settings, source_root)
    except SourceNotFoundError as exc:
        _error(str(exc))
        return 1
    if not files:
        _error(f"数据源目录没有 Excel/CSV 文件: {source_root}")
        return 2
    read_result = read_sources(files, settings, source_root)
    rows: list[dict[str, Any]] = []
    for table in read_result.tables:
        schema = detect_schema(table.frame)
        extraction = extract_periods(table, schema, target, baseline)
        rows.append(
            {
                "file": table.source.relative_path,
                "sheet": table.sheet_label,
                "rows": len(table.frame),
                "target_rows": len(extraction.target),
                "baseline_rows": len(extraction.baseline),
                "date_column": extraction.selected_column or "—",
                "fields": len(schema.fields),
                "issues": [issue.message for issue in [*schema.issues, *extraction.issues] if issue.severity in {"warning", "error"}],
            }
        )
    payload = {
        "target_date": target.isoformat(),
        "baseline_date": baseline.isoformat() if baseline else None,
        "source_dir": str(source_root),
        "files_scanned": read_result.files_scanned,
        "files_read": read_result.files_read,
        "tables": rows,
        "file_errors": [
            {"file": report.relative_path, "status": report.status, "error": report.error}
            for report in read_result.file_reports
            if report.error
        ],
    }
    if args.json:
        print(json.dumps(jsonable(payload), ensure_ascii=False, indent=2))
    else:
        print(f"InsightFlow 扫描预览 · 目标 {target.isoformat()}")
        print(f"数据源: {source_root}")
        print(f"文件: {read_result.files_scanned} 扫描 / {read_result.files_read} 成功读取\n")
        print(f"{'文件':<34} {'表':<14} {'行':>5} {'目标':>5} {'基准':>5} {'日期列':<16} {'字段':>4}")
        print("-" * 105)
        for row in rows:
            print(
                f"{_clip(row['file'], 33):<34} {_clip(row['sheet'], 13):<14} "
                f"{row['rows']:>5} {row['target_rows']:>5} {row['baseline_rows']:>5} "
                f"{_clip(row['date_column'], 15):<16} {row['fields']:>4}"
            )
        if payload["file_errors"]:
            print("\n读取失败:")
            for item in payload["file_errors"]:
                print(f"  - {item['file']}: {_clip(item['error'], 120)}")
        warnings = [issue for issue in read_result.warnings if issue.severity in {"warning", "error"}]
        if warnings:
            print(f"\n兼容性提示（{len(warnings)} 条）:")
            for issue in warnings[:8]:
                print(f"  - [{issue.code}] {issue.message}")
    return 0 if any(row["target_rows"] for row in rows) else 2


def _cmd_demo(settings: Settings, args: argparse.Namespace) -> int:
    target = _target(settings, args.date)
    script = settings.project_root / "scripts" / "generate_sample_data.py"
    if not script.is_file():
        _error(f"演示数据脚本不存在: {script}")
        return 1
    old_argv = sys.argv
    try:
        sys.argv = [
            str(script),
            "--end-date",
            target.isoformat(),
            "--output",
            str(settings.source_dir),
        ]
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (0, None):
            _error("演示数据生成失败。")
            return 1
    finally:
        sys.argv = old_argv
    record = execute(settings, target_date=target, trigger="manual", force=True)
    if args.json:
        print(json.dumps(jsonable(record), ensure_ascii=False, indent=2))
    else:
        _print_run(record)
    return _exit_code(record)


def _cmd_logs(settings: Settings, args: argparse.Namespace) -> int:
    records = read_run_history(settings.log_dir / "runs.jsonl", limit=max(1, args.limit))
    if args.json:
        print(json.dumps(jsonable(records), ensure_ascii=False, indent=2))
        return 0
    if not records:
        print("暂无运行记录。先执行 `python -m insightflow run --force`。")
        return 0
    print(f"{'触发时间':<20} {'目标日期':<12} {'状态':<10} {'触发':<10} {'文件':>5} {'行数':>6} {'报告'}")
    print("-" * 112)
    for item in records:
        print(
            f"{_clip(item.get('started_at',''), 19):<20} {item.get('target_date',''):<12} "
            f"{item.get('status',''):<10} {item.get('trigger',''):<10} "
            f"{item.get('files_read',0):>5} {item.get('rows_clean',0):>6} {item.get('report_path') or '—'}"
        )
        if item.get("failure_reason"):
            print(f"  └─ {_clip(item['failure_reason'], 140)}")
    return 0


def _cmd_init(settings: Settings, args: argparse.Namespace) -> int:
    config_path = settings.project_root / "config" / "config.json"
    example = settings.project_root / "config" / "config.example.json"
    if not config_path.exists() or args.force:
        if example.exists():
            shutil.copyfile(example, config_path)
            print(f"已写入配置: {config_path}")
        else:
            print(f"未找到配置模板: {example}")
            return 1
    settings.ensure_runtime_dirs()
    print("目录已就绪:")
    for label, path in (("数据源", settings.source_dir), ("输出", settings.output_dir), ("日志", settings.log_dir), ("状态", settings.state_dir)):
        path.mkdir(parents=True, exist_ok=True)
        print(f"  {label}: {path}")
    return 0


def _cmd_schedule(settings: Settings, args: argparse.Namespace) -> int:
    command = args.schedule_command
    if command == "install":
        ok, message = install_windows_task(
            settings,
            value=args.time,
            dry_run=args.dry_run,
            task_name=args.task_name,
        )
        if args.dry_run:
            print(message)
            return 0
        print(("✔ " if ok else "✖ ") + message)
        if ok:
            print(f"任务名称: {args.task_name or settings.scheduler.task_name}")
            print(f"每日触发: {args.time or settings.schedule_time}")
            print("运行入口: scripts/run_scheduled.ps1")
        return 0 if ok else 1
    if command == "status":
        ok, output = query_windows_task(settings, task_name=args.task_name)
        payload = {
            "installed": ok,
            "task_name": args.task_name or settings.scheduler.task_name,
            "query": output,
            "daemon": daemon_status(settings),
        }
        if args.json:
            print(json.dumps(jsonable(payload), ensure_ascii=False, indent=2))
        else:
            print(f"计划任务: {payload['task_name']}")
            print("状态: " + ("已安装" if ok else "未安装 / 无法查询"))
            if output:
                print(output)
            daemon = payload["daemon"]
            print(f"下次计划时间: {daemon['next_run']}")
            if daemon["state"]:
                print(f"最近 daemon 触发: {daemon['state'].get('last_triggered_at', '—')}")
        return 0
    if command == "uninstall":
        ok, message = remove_windows_task(settings, task_name=args.task_name)
        print(("✔ " if ok else "✖ ") + message)
        return 0 if ok else 1
    if command == "daemon":
        print(f"InsightFlow daemon 启动，每日 {args.time or settings.schedule_time} 触发；Ctrl+C 退出。")
        try:
            record = run_daemon(
                settings,
                value=args.time,
                once=args.once,
                poll_seconds=max(5, args.poll_seconds),
            )
        except KeyboardInterrupt:
            print("\n已停止 daemon。")
            return 0
        if args.once and isinstance(record, RunRecord):
            if args.json:
                print(json.dumps(jsonable(record), ensure_ascii=False, indent=2))
            else:
                _print_run(record)
            return _exit_code(record)
        return 0
    return 1


def _target(settings: Settings, value: str | None) -> date:
    if not value:
        return settings.target_date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"日期格式应为 YYYY-MM-DD: {value!r}") from exc


def _print_run(record: RunRecord) -> None:
    status_text = {
        STATUS_SUCCESS: "成功",
        STATUS_NO_DATA: "无新数据",
        STATUS_SKIPPED: "已跳过",
        STATUS_FAILED: "失败",
    }.get(record.status, record.status)
    print("\nInsightFlow 运行结果")
    print("─" * 58)
    print(f"运行编号   {record.run_id}")
    print(f"触发方式   {record.trigger}")
    print(f"目标日期   {record.target_date or '—'}")
    print(f"对比基准   {record.baseline_date or '—'}")
    print(f"状态       {status_text}")
    print(f"文件       扫描 {record.files_scanned} / 读取 {record.files_read}")
    print(f"记录       目标日 {record.rows_target} → 清洗后 {record.rows_clean}")
    if record.report_generated and record.report_path:
        print(f"报告       {record.report_path}")
    if record.failure_reason:
        print(f"原因       {record.failure_reason}")
    if record.warnings:
        print(f"提示       {len(record.warnings)} 条（详见 logs/insightflow.log）")
    if record.duration_ms:
        print(f"耗时       {record.duration_ms / 1000:.2f} s")


def _exit_code(record: RunRecord) -> int:
    return {STATUS_SUCCESS: 0, STATUS_FAILED: 1, STATUS_NO_DATA: 2, STATUS_SKIPPED: 3}.get(record.status, 1)


def _clip(value: Any, length: int) -> str:
    text = str(value or "").replace("\n", " ")
    return text if len(text) <= length else text[: length - 1] + "…"


def _error(message: str) -> None:
    print(f"错误: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
