"""Locked-environment verification for the InsightFlow desktop shell.

The window must state whether the running interpreter uses the dependency
versions that were verified for this project (``requirements.lock.txt``).  A
silent version drift is the class of problem that keeps numbers looking
plausible while behaving differently, so an unverified environment is treated
as a risk, not as decoration.

This module imports neither tkinter nor pandas: the lock file is parsed as text
and installed versions come from ``importlib.metadata``.  That keeps startup
fast and makes the decision logic testable without a display.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

STATE_OK = "ok"
STATE_MISMATCH = "mismatch"
STATE_UNKNOWN = "unknown"

#: Packages whose drift can change reported numbers or report rendering.
DEFAULT_PACKAGES: tuple[str, ...] = ("pandas", "numpy", "openpyxl", "xlrd")

#: ``pandas==2.3.3`` or ``tzdata==2026.5; platform_system == "Windows"``.
_PIN = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*==\s*([^;#\s]+)")

#: Badge / banner colours, also used by the window.
COLOUR_OK = "#15803d"
COLOUR_BAD = "#b91c1c"


@dataclass(frozen=True)
class EnvStatus:
    """Outcome of comparing the lock file with the running interpreter."""

    state: str
    detail: str
    expected: dict[str, str] = field(default_factory=dict)
    actual: dict[str, str] = field(default_factory=dict)
    interpreter: str = ""
    python_version: str = ""

    @property
    def trusted(self) -> bool:
        """Only a fully matching environment may run without a prompt."""
        return self.state == STATE_OK


def read_lock_pins(lock_path: Path) -> dict[str, str]:
    """Parse ``name==version`` pins, ignoring comments and markers."""
    try:
        text = Path(lock_path).read_text(encoding="utf-8")
    except OSError:
        return {}
    pins: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN.match(stripped)
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def installed_versions(packages: tuple[str, ...]) -> dict[str, str]:
    """Installed versions; an empty string means 'not installed'."""
    found: dict[str, str] = {}
    for name in packages:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = ""
    return found


def _interpreter() -> str:
    return sys.executable or "python"


def _python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def check_environment(
    lock_path: Path,
    packages: tuple[str, ...] = DEFAULT_PACKAGES,
) -> EnvStatus:
    """Compare the lock pins with the interpreter that is actually running."""
    interpreter = _interpreter()
    python_version = _python_version()
    pins = read_lock_pins(lock_path)
    if not pins:
        return EnvStatus(
            state=STATE_UNKNOWN,
            detail=f"无法读取锁定依赖清单（{Path(lock_path).name} 缺失或为空），无法判定环境是否可信。",
            interpreter=interpreter,
            python_version=python_version,
        )

    expected = {name: pins[name.lower()] for name in packages if pins.get(name.lower())}
    if not expected:
        return EnvStatus(
            state=STATE_UNKNOWN,
            detail="锁定清单中没有可比对的依赖（pandas / numpy 等），无法判定环境是否可信。",
            interpreter=interpreter,
            python_version=python_version,
        )

    actual = installed_versions(tuple(expected))
    missing = [name for name, version in actual.items() if not version]
    if missing:
        return EnvStatus(
            state=STATE_UNKNOWN,
            detail=f"无法判定环境：当前解释器未安装 {'、'.join(missing)}。",
            expected=expected,
            actual=actual,
            interpreter=interpreter,
            python_version=python_version,
        )

    drifted = [f"{name} {expected[name]}→{actual[name]}" for name in expected if actual[name] != expected[name]]
    if drifted:
        return EnvStatus(
            state=STATE_MISMATCH,
            detail="环境偏离锁定版本：" + "；".join(drifted) + "。分析结果可能与其他机器不可复现。",
            expected=expected,
            actual=actual,
            interpreter=interpreter,
            python_version=python_version,
        )

    summary = " / ".join(f"{name} {version}" for name, version in expected.items())
    return EnvStatus(
        state=STATE_OK,
        detail=f"锁定环境（{summary}）",
        expected=expected,
        actual=actual,
        interpreter=interpreter,
        python_version=python_version,
    )


def banner(status: EnvStatus) -> tuple[str, str]:
    """Short badge text plus its background colour."""
    if status.trusted:
        return "锁定环境", COLOUR_OK
    if status.state == STATE_MISMATCH:
        return "环境偏离 · 结果不可信", COLOUR_BAD
    return "环境未知 · 结果不可信", COLOUR_BAD


def gate_message(status: EnvStatus) -> str:
    """Startup gate text; empty string means 'no gate, start normally'."""
    if status.trusted:
        return ""
    if status.state == STATE_MISMATCH:
        body = (
            "当前环境不是本项目的锁定版本。\n\n"
            f"{status.detail}\n\n"
            "继续使用可能得到与其他机器不一致的结果，既有报告也无法复现。\n"
            "建议先按 requirements.lock.txt 安装依赖，再使用操作端。"
        )
    else:
        body = (
            "无法确认当前环境是否可信。\n\n"
            f"{status.detail}\n\n"
            "在此状态下运行分析，结果可能不可复现，也无法与历史报告对比。\n"
            "建议先按 requirements.lock.txt 安装依赖，再使用操作端。"
        )
    return f"{body}\n\n当前解释器：{status.interpreter}（Python {status.python_version}）\n\n仅建议用于排障。仍要进入操作端吗？"
