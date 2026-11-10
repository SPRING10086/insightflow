"""Configuration loading and path resolution.

The application is deliberately configured with JSON instead of YAML so the
repository has no hidden parser dependency.  Relative paths are always
resolved against the project root, never against the caller's current working
directory; that makes Task Scheduler runs reproducible.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .utils import ensure_dir, read_json

APP_VERSION = "1.0.2"

_DEFAULTS: dict[str, Any] = {
    "app_name": "InsightFlow",
    "version": APP_VERSION,
    "timezone": "Asia/Shanghai",
    "source_dir": "examples/raw",
    "output_dir": "output",
    "log_dir": "logs",
    "state_dir": "state",
    "schedule_time": "09:30",
    "target_date_mode": "previous_day",
    "date_order": "month_first",
    "recursive_scan": True,
    "include_extensions": [".xlsx", ".xls", ".csv"],
    "max_file_mb": 50,
    "max_rows_per_file": 200_000,
    "header_scan_rows": 20,
    "outlier_iqr_k": 1.5,
    "top_n": 8,
    "max_charts": 8,
    "comparison": {"enabled": True, "lookback_days": 1},
    "report": {
        "title": "每日经营数据洞察报告",
        "company": "InsightFlow · Portfolio Demo",
        "max_table_rows": 100,
        "accent": "#2563eb",
    },
    "scheduler": {"task_name": "InsightFlow_Daily_0930", "grace_minutes": 180},
}


class ConfigError(RuntimeError):
    """Raised when configuration is missing required or invalid values."""


@dataclass
class ReportSettings:
    title: str = "每日经营数据洞察报告"
    company: str = "InsightFlow · Portfolio Demo"
    max_table_rows: int = 100
    accent: str = "#2563eb"


@dataclass
class ComparisonSettings:
    enabled: bool = True
    lookback_days: int = 1


@dataclass
class SchedulerSettings:
    task_name: str = "InsightFlow_Daily_0930"
    grace_minutes: int = 180


@dataclass
class LLMSettings:
    """LLM bypass configuration; every feature defaults to off (fail-closed)."""

    enabled: bool = False
    semantic: bool = False
    plan: bool = False
    execute: bool = False
    narrative: bool = False
    budget_per_table_cny: float = 5.0
    budget_batch_cny: float = 20.0
    request_timeout_seconds: float = 60.0
    sandbox_timeout_seconds: float = 10.0
    memory_mb: int = 1024
    max_input_rows: int = 200_000
    retry_structured: int = 2
    retry_transport: int = 2
    warn_prompt_tokens: int = 8000
    force_rules: bool = False
    adoption: dict[str, str] = field(
        default_factory=lambda: {"semantic": "shadow", "plan": "shadow", "execute": "shadow", "narrative": "shadow"}
    )

    FEATURES = ("semantic", "plan", "execute", "narrative")
    ADOPTION_MODES = ("shadow", "gated", "primary")

    def feature_enabled(self, name: str) -> bool:
        """Master switch gates every feature; unknown names are a config error."""
        if name not in self.FEATURES:
            raise ConfigError(f"未知的 LLM 功能开关: {name!r}")
        return bool(self.enabled and getattr(self, name))

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "LLMSettings":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError("llm 配置必须是对象")
        allowed = {
            "enabled",
            "features",
            "budget",
            "timeout",
            "sandbox",
            "retry",
            "warn_prompt_tokens",
            "force_rules",
            "adoption",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ConfigError(f"llm 配置包含未知键: {sorted(unknown)}")

        def require_bool(value: Any, label: str) -> bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{label} 必须是 true/false")
            return value

        def require_number(value: Any, label: str, low: float, high: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{label} 必须是数字")
            number = float(value)
            if not (low <= number <= high):
                raise ConfigError(f"{label} 必须在 {low}~{high} 之间")
            return number

        def require_int(value: Any, label: str, low: int, high: int) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{label} 必须是整数")
            if not (low <= value <= high):
                raise ConfigError(f"{label} 必须在 {low}~{high} 之间")
            return value

        enabled = require_bool(raw.get("enabled", False), "llm.enabled")

        features_raw = raw.get("features", {})
        if not isinstance(features_raw, dict):
            raise ConfigError("llm.features 必须是对象")
        unknown_features = set(features_raw) - set(cls.FEATURES)
        if unknown_features:
            raise ConfigError(f"llm.features 包含未知开关: {sorted(unknown_features)}")
        features = {
            name: require_bool(features_raw.get(name, False), f"llm.features.{name}") for name in cls.FEATURES
        }

        budget = raw.get("budget", {})
        if not isinstance(budget, dict):
            raise ConfigError("llm.budget 必须是对象")
        if set(budget) - {"per_table_cny", "batch_cny"}:
            raise ConfigError(f"llm.budget 包含未知键: {sorted(set(budget) - {'per_table_cny', 'batch_cny'})}")
        per_table = require_number(budget.get("per_table_cny", 5.0), "llm.budget.per_table_cny", 0, 100)
        batch = require_number(budget.get("batch_cny", 20.0), "llm.budget.batch_cny", 0, 100)
        if per_table > batch:
            raise ConfigError("llm.budget.per_table_cny 不能大于 batch_cny")

        timeout = raw.get("timeout", {})
        if not isinstance(timeout, dict):
            raise ConfigError("llm.timeout 必须是对象")
        if set(timeout) - {"request_seconds", "sandbox_seconds"}:
            raise ConfigError(f"llm.timeout 包含未知键: {sorted(set(timeout) - {'request_seconds', 'sandbox_seconds'})}")
        request_timeout = require_number(timeout.get("request_seconds", 60.0), "llm.timeout.request_seconds", 1, 600)
        sandbox_timeout = require_number(timeout.get("sandbox_seconds", 10.0), "llm.timeout.sandbox_seconds", 1, 600)

        sandbox = raw.get("sandbox", {})
        if not isinstance(sandbox, dict):
            raise ConfigError("llm.sandbox 必须是对象")
        if set(sandbox) - {"memory_mb", "max_input_rows"}:
            raise ConfigError(f"llm.sandbox 包含未知键: {sorted(set(sandbox) - {'memory_mb', 'max_input_rows'})}")
        memory_mb = require_int(sandbox.get("memory_mb", 1024), "llm.sandbox.memory_mb", 64, 4096)
        max_input_rows = require_int(sandbox.get("max_input_rows", 200_000), "llm.sandbox.max_input_rows", 1, 10_000_000)

        retry = raw.get("retry", {})
        if not isinstance(retry, dict):
            raise ConfigError("llm.retry 必须是对象")
        if set(retry) - {"structured_output", "transport"}:
            raise ConfigError(f"llm.retry 包含未知键: {sorted(set(retry) - {'structured_output', 'transport'})}")
        retry_structured = require_int(retry.get("structured_output", 2), "llm.retry.structured_output", 0, 3)
        retry_transport = require_int(retry.get("transport", 2), "llm.retry.transport", 0, 3)

        warn_prompt_tokens = require_int(raw.get("warn_prompt_tokens", 8000), "llm.warn_prompt_tokens", 0, 1_000_000)
        force_rules = require_bool(raw.get("force_rules", False), "llm.force_rules")

        adoption_raw = raw.get("adoption", {})
        if not isinstance(adoption_raw, dict):
            raise ConfigError("llm.adoption 必须是对象")
        unknown_adoption = set(adoption_raw) - set(cls.FEATURES)
        if unknown_adoption:
            raise ConfigError(f"llm.adoption 包含未知能力: {sorted(unknown_adoption)}")
        adoption: dict[str, str] = {}
        for name in cls.FEATURES:
            value = adoption_raw.get(name, "shadow")
            if value not in cls.ADOPTION_MODES:
                raise ConfigError(f"llm.adoption.{name} 必须是 shadow/gated/primary")
            adoption[name] = value

        return cls(
            enabled=enabled,
            semantic=features["semantic"],
            plan=features["plan"],
            execute=features["execute"],
            narrative=features["narrative"],
            budget_per_table_cny=per_table,
            budget_batch_cny=batch,
            request_timeout_seconds=request_timeout,
            sandbox_timeout_seconds=sandbox_timeout,
            memory_mb=memory_mb,
            max_input_rows=max_input_rows,
            retry_structured=retry_structured,
            retry_transport=retry_transport,
            warn_prompt_tokens=warn_prompt_tokens,
            force_rules=force_rules,
            adoption=adoption,
        )


@dataclass
class Settings:
    project_root: Path
    config_path: Path
    source_dir: Path
    output_dir: Path
    log_dir: Path
    state_dir: Path
    app_name: str = "InsightFlow"
    version: str = APP_VERSION
    timezone: str = "Asia/Shanghai"
    schedule_time: str = "09:30"
    target_date_mode: str = "previous_day"
    date_order: str = "month_first"
    recursive_scan: bool = True
    include_extensions: tuple[str, ...] = (".xlsx", ".xls", ".csv")
    max_file_mb: float = 50
    max_rows_per_file: int = 200_000
    header_scan_rows: int = 20
    outlier_iqr_k: float = 1.5
    top_n: int = 8
    max_charts: int = 8
    comparison: ComparisonSettings = field(default_factory=ComparisonSettings)
    report: ReportSettings = field(default_factory=ReportSettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    def ensure_runtime_dirs(self) -> None:
        for path in (self.output_dir, self.log_dir, self.state_dir):
            ensure_dir(path)

    def target_date(self, reference: date | None = None) -> date:
        """Return the reporting date (the previous calendar day by default)."""
        base = reference or _today(self.timezone)
        if self.target_date_mode == "previous_day":
            return base - timedelta(days=1)
        if self.target_date_mode == "same_day":
            return base
        raise ConfigError(f"不支持的 target_date_mode: {self.target_date_mode!r}")

    def baseline_date(self, target: date) -> date | None:
        if not self.comparison.enabled:
            return None
        return target - timedelta(days=max(1, int(self.comparison.lookback_days)))

    def public_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "version": self.version,
            "timezone": self.timezone,
            "source_dir": self.source_dir.as_posix(),
            "output_dir": self.output_dir.as_posix(),
            "log_dir": self.log_dir.as_posix(),
            "state_dir": self.state_dir.as_posix(),
            "schedule_time": self.schedule_time,
            "target_date_mode": self.target_date_mode,
            "date_order": self.date_order,
            "recursive_scan": self.recursive_scan,
            "include_extensions": list(self.include_extensions),
            "max_file_mb": self.max_file_mb,
            "comparison": {
                "enabled": self.comparison.enabled,
                "lookback_days": self.comparison.lookback_days,
            },
            "llm": {
                "enabled": self.llm.enabled,
                "features": {name: getattr(self.llm, name) for name in LLMSettings.FEATURES},
                "budget": {
                    "per_table_cny": self.llm.budget_per_table_cny,
                    "batch_cny": self.llm.budget_batch_cny,
                },
                "timeout": {
                    "request_seconds": self.llm.request_timeout_seconds,
                    "sandbox_seconds": self.llm.sandbox_timeout_seconds,
                },
                "sandbox": {"memory_mb": self.llm.memory_mb, "max_input_rows": self.llm.max_input_rows},
                "retry": {"structured_output": self.llm.retry_structured, "transport": self.llm.retry_transport},
                "force_rules": self.llm.force_rules,
                "adoption": dict(self.llm.adoption),
                "warn_prompt_tokens": self.llm.warn_prompt_tokens,
            },
        }


def _today(timezone_name: str) -> date:
    from .utils import now_local

    return now_local(timezone_name).date()


def find_project_root(start: Path | None = None) -> Path:
    """Locate the repository root, preferring an explicit environment hint."""
    candidates: list[Path] = []
    env_home = os.environ.get("INSIGHTFLOW_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser())
    if start:
        candidates.append(Path(start).resolve())
    candidates.append(Path.cwd().resolve())
    package_root = Path(__file__).resolve().parents[2]
    candidates.append(package_root)

    for candidate in candidates:
        current = candidate if candidate.is_dir() else candidate.parent
        for parent in [current, *current.parents]:
            if (parent / "config" / "config.json").is_file() or (parent / "pyproject.toml").is_file():
                return parent
    return package_root


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(
    config_path: str | Path | None = None,
    project_root: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    root = Path(project_root).expanduser().resolve() if project_root else find_project_root()
    if config_path:
        path = Path(config_path).expanduser()
        if not path.is_absolute():
            path = root / path
    else:
        path = root / "config" / "config.json"
        if not path.is_file():
            path = root / "config" / "config.example.json"

    raw = read_json(path, default=None) if path.is_file() else {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件不是有效的 JSON 对象: {path}")
    merged = _deep_merge(_DEFAULTS, raw)
    if overrides:
        merged = _deep_merge(merged, overrides)

    include_extensions = tuple(
        str(extension).lower() if str(extension).startswith(".") else f".{str(extension).lower()}"
        for extension in merged.get("include_extensions", _DEFAULTS["include_extensions"])
    )
    if not include_extensions:
        raise ConfigError("include_extensions 不能为空")

    schedule_time = str(merged.get("schedule_time", "09:30"))
    _validate_schedule_time(schedule_time)
    date_order = str(merged.get("date_order", "month_first")).lower()
    if date_order not in {"month_first", "day_first", "error"}:
        raise ConfigError("date_order 必须是 month_first / day_first / error")

    settings = Settings(
        project_root=root,
        config_path=path,
        source_dir=_resolve_path(root, merged["source_dir"]),
        output_dir=_resolve_path(root, merged["output_dir"]),
        log_dir=_resolve_path(root, merged["log_dir"]),
        state_dir=_resolve_path(root, merged["state_dir"]),
        app_name=str(merged.get("app_name", "InsightFlow")),
        version=str(merged.get("version", APP_VERSION)),
        timezone=str(merged.get("timezone", "Asia/Shanghai")),
        schedule_time=schedule_time,
        target_date_mode=str(merged.get("target_date_mode", "previous_day")),
        date_order=date_order,
        recursive_scan=bool(merged.get("recursive_scan", True)),
        include_extensions=include_extensions,
        max_file_mb=float(merged.get("max_file_mb", 50)),
        max_rows_per_file=int(merged.get("max_rows_per_file", 200_000)),
        header_scan_rows=max(3, int(merged.get("header_scan_rows", 20))),
        outlier_iqr_k=float(merged.get("outlier_iqr_k", 1.5)),
        top_n=max(3, int(merged.get("top_n", 8))),
        max_charts=max(3, int(merged.get("max_charts", 8))),
        comparison=ComparisonSettings(
            enabled=bool(merged.get("comparison", {}).get("enabled", True)),
            lookback_days=max(1, int(merged.get("comparison", {}).get("lookback_days", 1))),
        ),
        report=ReportSettings(
            title=str(merged.get("report", {}).get("title", _DEFAULTS["report"]["title"])),
            company=str(merged.get("report", {}).get("company", _DEFAULTS["report"]["company"])),
            max_table_rows=max(10, int(merged.get("report", {}).get("max_table_rows", 100))),
            accent=str(merged.get("report", {}).get("accent", "#2563eb")),
        ),
        scheduler=SchedulerSettings(
            task_name=str(merged.get("scheduler", {}).get("task_name", _DEFAULTS["scheduler"]["task_name"])),
            grace_minutes=max(0, int(merged.get("scheduler", {}).get("grace_minutes", 180))),
        ),
        llm=LLMSettings.from_dict(merged.get("llm")),
    )
    if settings.max_file_mb <= 0:
        raise ConfigError("max_file_mb 必须大于 0")
    if settings.max_rows_per_file <= 0:
        raise ConfigError("max_rows_per_file 必须大于 0")
    return settings


def _resolve_path(root: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _validate_schedule_time(value: str) -> None:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"schedule_time 必须是 HH:MM 格式: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"schedule_time 超出范围: {value!r}")
