"""Scheduling: Windows Task Scheduler integration plus a portable daemon.

Task Scheduler is the recommended production path on Windows because it
survives logoff/reboot.  The daemon is a dependency-free fallback for demos or
non-Windows hosts.  Both paths call the same ``execute`` pipeline and therefore
write the same structured run history.
"""

from __future__ import annotations

import locale
import os
import subprocess
import sys
import threading
import xml.sax.saxutils as saxutils
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .pipeline import execute
from .utils import atomic_write_json, now_local, read_json

DEFAULT_POLL_SECONDS = 30
RETRY_INTERVAL_SECONDS = 300


def parse_schedule_time(value: str) -> dt_time:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        return dt_time(hour=hour, minute=minute)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"时间必须是 HH:MM 格式: {value!r}") from exc


def next_occurrence(now: datetime, value: str) -> datetime:
    clock = parse_schedule_time(value)
    candidate = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _scheduled_today(now: datetime, value: str) -> datetime:
    clock = parse_schedule_time(value)
    return now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)


def _state_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def trigger_is_due(
    settings: Settings,
    value: str,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
) -> bool:
    """Whether a scheduled run should fire now.

    The window is the configured grace period (default 180 minutes), not a
    single poll interval.  A failed attempt is retried after five minutes
    instead of being marked complete for the day.
    """
    now = now or now_local(settings.timezone)
    scheduled = _scheduled_today(now, value)
    if now < scheduled:
        return False
    if now > scheduled + timedelta(minutes=settings.scheduler.grace_minutes):
        return False
    if state is None:
        state = read_json(_state_path(settings), default={}) or {}
    if state.get("last_triggered_date") == now.date().isoformat():
        return False
    if state.get("last_status") == "failed":
        last_attempt = _state_datetime(state.get("last_attempt_at"))
        if last_attempt is not None:
            if last_attempt.tzinfo is None and now.tzinfo is not None:
                last_attempt = last_attempt.replace(tzinfo=now.tzinfo)
            if (now - last_attempt).total_seconds() < RETRY_INTERVAL_SECONDS:
                return False
    return True


def should_catch_up(settings: Settings, value: str, now: datetime | None = None) -> bool:
    return trigger_is_due(settings, value, now=now)


def run_daemon(
    settings: Settings,
    value: str | None = None,
    once: bool = False,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    stop_event: threading.Event | None = None,
) -> Any:
    """Run the portable scheduler until interrupted (or once)."""
    schedule_time = value or settings.schedule_time
    parse_schedule_time(schedule_time)
    stop_event = stop_event or threading.Event()
    now = now_local(settings.timezone)

    if once:
        return _trigger(settings, now)

    if trigger_is_due(settings, schedule_time, now):
        _trigger(settings, now)

    while not stop_event.is_set():
        now = now_local(settings.timezone)
        # Wake at least every poll interval so a laptop resume can catch a
        # missed 09:30 run inside the grace window.
        upcoming = next_occurrence(now, schedule_time)
        wait_seconds = max(1.0, min(poll_seconds, (upcoming - now).total_seconds()))
        if stop_event.wait(wait_seconds):
            break
        now = now_local(settings.timezone)
        state = read_json(_state_path(settings), default={}) or {}
        if trigger_is_due(settings, schedule_time, now, state=state):
            _trigger(settings, now)
    return None


def _trigger(settings: Settings, now: datetime) -> Any:
    record = execute(settings, trigger="scheduled", now=now)
    state = read_json(_state_path(settings), default={}) or {}
    state.update(
        {
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "last_run_id": record.run_id,
            "last_status": record.status,
            "last_report": record.report_path,
        }
    )
    if record.status == "failed":
        # Do not mark the day complete: trigger_is_due() will retry after the
        # retry interval while still inside grace_minutes.
        state["last_failure_reason"] = getattr(record, "failure_reason", "")
    else:
        state["last_triggered_date"] = now.date().isoformat()
        state["last_triggered_at"] = now.isoformat(timespec="seconds")
        state.pop("last_failure_reason", None)
    atomic_write_json(_state_path(settings), state)
    return record


def _state_path(settings: Settings) -> Path:
    return settings.state_dir / "scheduler_state.json"


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------


def task_command(settings: Settings) -> tuple[str, str, str]:
    script = settings.project_root / "scripts" / "run_scheduled.ps1"
    command = "powershell.exe"
    arguments = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
    return command, arguments, str(settings.project_root)


def build_task_xml(settings: Settings, value: str | None = None) -> str:
    schedule_time = value or settings.schedule_time
    now = now_local(settings.timezone)
    start = next_occurrence(now, schedule_time)
    command, arguments, workdir = task_command(settings)
    description = f"InsightFlow daily data insight report at {schedule_time}"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{saxutils.escape(description)}</Description>
    <URI>\\{saxutils.escape(settings.scheduler.task_name)}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start.strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{saxutils.escape(command)}</Command>
      <Arguments>{saxutils.escape(arguments)}</Arguments>
      <WorkingDirectory>{saxutils.escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_windows_task(
    settings: Settings,
    value: str | None = None,
    dry_run: bool = False,
    task_name: str | None = None,
) -> tuple[bool, str]:
    xml = build_task_xml(settings, value)
    name = task_name or settings.scheduler.task_name
    xml_path = settings.state_dir / f"{name}.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16", newline="")
    if dry_run:
        return True, xml
    if os.name != "nt":
        return False, (
            "当前系统不是 Windows。请在 Linux/macOS 使用 `schedule daemon`，"
            "或把以下 cron 行加入 crontab：\n"
            f"30 9 * * * cd {settings.project_root} && PYTHONPATH=src python -m insightflow run --trigger scheduled"
        )
    command = ["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False) or "utf-8",
        errors="replace",
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return False, output.strip() or f"schtasks 退出码 {result.returncode}"
    return True, output.strip() or f"已创建计划任务 {name}"


def query_windows_task(settings: Settings, task_name: str | None = None) -> tuple[bool, str]:
    name = task_name or settings.scheduler.task_name
    if os.name != "nt":
        return False, "非 Windows 系统，请使用 `schedule daemon`。"
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"],
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False) or "utf-8",
        errors="replace",
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def remove_windows_task(settings: Settings, task_name: str | None = None) -> tuple[bool, str]:
    name = task_name or settings.scheduler.task_name
    if os.name != "nt":
        return False, "非 Windows 系统，请直接停止 `schedule daemon` 进程。"
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", name, "/F"],
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False) or "utf-8",
        errors="replace",
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def daemon_status(settings: Settings) -> dict[str, Any]:
    state = read_json(_state_path(settings), default={}) or {}
    return {
        "state": state,
        "next_run": next_occurrence(now_local(settings.timezone), settings.schedule_time).isoformat(timespec="seconds"),
        "platform": sys.platform,
        "task_name": settings.scheduler.task_name,
    }
