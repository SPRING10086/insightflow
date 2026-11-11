"""Controlled executor: AST whitelist + subprocess + Job Object + strict output.

Security stance: generated code is assumed malicious.  Static validation runs
before any process starts; the worker re-validates as defence in depth; the
process is wall-clock bounded and memory-capped by a Windows Job Object.

The worker is the only place that executes model code, and it never receives
os/sys/open: ``__builtins__`` is a small whitelist dict.
"""

from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
from pathlib import Path

MAX_CODE_CHARS = 8000
MAX_OUTPUT_BYTES = 1_000_000
MAX_OUTPUT_ROWS = 5000
MAX_OUTPUT_METRICS = 200
MAX_INPUT_ROWS = 200_000

_ALLOWED_STATEMENTS = {
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.Attribute,
    ast.Subscript,
    ast.Slice,
    ast.Name,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.IfExp,
    ast.If,
    ast.ListComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.keyword,
}

_DENIED_STATEMENTS = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Raise,
    ast.Global,
    ast.Nonlocal,
    ast.Delete,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Starred,
    ast.NamedExpr,
)

BUILTIN_ALLOWLIST = {
    "len",
    "min",
    "max",
    "sum",
    "round",
    "abs",
    "int",
    "float",
    "str",
    "sorted",
    "bool",
}

#: ``GroupBy.apply`` may only name these aggregations (no dunders, no callables).
APPLY_FUNCTIONS = {"sum", "mean", "count", "size", "min", "max", "median", "std", "nunique"}

FORBIDDEN_NAMES = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
    "input",
    "print",
    "type",
    "object",
    "super",
    "memoryview",
}

ALLOWED_ATTRS = {
    # pandas / numpy functions
    "to_numeric",
    "to_datetime",
    "cut",
    "qcut",
    "concat",
    "mean",
    "sum",
    "median",
    "std",
    "percentile",
    "round",
    "abs",
    "minimum",
    "maximum",
    "where",
    "isnan",
    # DataFrame / Series / GroupBy methods
    "groupby",
    "agg",
    "size",
    "nunique",
    "sort_values",
    "head",
    "tail",
    "rename",
    "reset_index",
    "merge",
    "astype",
    "fillna",
    "dropna",
    "isin",
    "between",
    "clip",
    "idxmax",
    "nlargest",
    "value_counts",
    "pivot_table",
    "crosstab",
    "loc",
    "iloc",
    "columns",
    "index",
    "values",
    "items",
    "keys",
    "get",
    "apply",
    "count",
    "min",
    "max",
}

class SandboxError(RuntimeError):
    """Base class for controlled-execution failures."""


class SandboxViolation(SandboxError):
    """Static policy rejection (imports, dunder, I/O, hallucinated fields...)."""


class SandboxTimeout(SandboxError):
    """Wall-clock budget exceeded; the worker tree was killed."""


class SandboxResourceError(SandboxError):
    """Input too large, memory guard unavailable, or memory limit hit."""


class SandboxOutputError(SandboxError):
    """The worker output is missing, malformed, non-numeric or oversized."""


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if isinstance(node, ast.Attribute) else (
            node.func if isinstance(node, ast.Call) else node.value
        )
    return node.id if isinstance(node, ast.Name) else None


def _check_attribute(node: ast.Attribute) -> None:
    attr = node.attr
    if attr.startswith("_"):
        raise SandboxViolation(f"dunder/underscore attribute is forbidden: {attr}")
    if attr not in ALLOWED_ATTRS:
        raise SandboxViolation(f"attribute not in allowlist: {attr}")
    root = _root_name(node)
    # Roots may be df/pd/np or a local variable produced by allowed operations;
    # the attribute allowlist plus the dunder ban is what bounds the chain.
    if root is None or root.startswith("_"):
        raise SandboxViolation(f"attribute chain has no safe root: {root!r}")


def _check_call(node: ast.Call) -> None:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id not in BUILTIN_ALLOWLIST:
            raise SandboxViolation(f"function not allowed: {func.id}")
        return
    if isinstance(func, ast.Attribute):
        if func.attr.startswith("_") or func.attr not in ALLOWED_ATTRS:
            raise SandboxViolation(f"method not allowed: {func.attr}")
        if func.attr == "apply":
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(
                node.args[0].value, str
            ):
                raise SandboxViolation("GroupBy.apply only accepts a string function name")
            if node.keywords:
                raise SandboxViolation("GroupBy.apply does not accept keyword arguments")
            name = node.args[0].value
            if name.startswith("_") or name not in APPLY_FUNCTIONS:
                raise SandboxViolation(f"GroupBy.apply name is not allowlisted: {name!r}")
        return
    raise SandboxViolation("calls must target an allowlisted name or method")


def _check_names(node: ast.Name) -> None:
    name = node.id
    if name.startswith("_"):
        raise SandboxViolation(f"underscore name is forbidden: {name}")
    if name in FORBIDDEN_NAMES:
        raise SandboxViolation(f"name is forbidden: {name}")


def _string_constants(node: ast.AST) -> list[str]:
    return [child.value for child in ast.walk(node) if isinstance(child, ast.Constant) and isinstance(child.value, str)]


def validate_code(code: str, allowed_columns: set[str] | list[str]) -> None:
    """Reject non-whitelisted code before any process starts."""
    if not isinstance(code, str) or not code.strip():
        raise SandboxViolation("code is empty")
    if len(code) > MAX_CODE_CHARS:
        raise SandboxViolation("code is too long")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxViolation(f"syntax error: {exc}") from exc

    columns = {str(column) for column in allowed_columns}
    for node in ast.walk(tree):
        if isinstance(node, (ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context)):
            continue
        if isinstance(node, _DENIED_STATEMENTS):
            raise SandboxViolation(f"statement is forbidden: {type(node).__name__}")
        if type(node) not in _ALLOWED_STATEMENTS:
            raise SandboxViolation(f"node is not in the allowlist: {type(node).__name__}")
        if isinstance(node, ast.Attribute):
            _check_attribute(node)
        elif isinstance(node, ast.Call):
            _check_call(node)
        elif isinstance(node, ast.Name):
            _check_names(node)
        elif isinstance(node, ast.Subscript):
            # Any string used to index a frame must be a real column: catches
            # df["幽灵列"], df.loc[:, "幽灵列"], df[["a","幽灵"]] and friends.
            for text in _string_constants(node.slice):
                if text not in columns:
                    raise SandboxViolation(f"hallucinated_field: {text}")

    # groupby("字段") must reference a real column as well
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "groupby"
        ):
            for arg in node.args:
                for text in _string_constants(arg):
                    if text not in columns:
                        raise SandboxViolation(f"hallucinated_field: {text}")


def _win_job(pid: int, memory_bytes: int) -> bool:
    """Best-effort Job Object memory cap; returns False when unavailable."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        handle = None
        try:
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x00000100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = memory_bytes
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return False
            handle = kernel32.OpenProcess(0x0101, False, int(pid))  # SET_QUOTA | TERMINATE
            if not handle:
                return False
            if not kernel32.AssignProcessToJobObject(job, handle):
                return False
            return True
        finally:
            # Close every handle on every path; the job has no KILL_ON_JOB_CLOSE
            # flag, so closing the handle does not terminate the assigned worker.
            if handle:
                kernel32.CloseHandle(handle)
            if job:
                kernel32.CloseHandle(job)
    except Exception:  # noqa: BLE001 - guard failure means "no memory cap"
        return False


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _validate_groups(groups: object, label: str) -> None:
    if not isinstance(groups, dict) or set(groups) != {"columns", "rows"}:
        raise SandboxOutputError(f"{label} must contain exactly columns and rows")
    columns, rows = groups["columns"], groups["rows"]
    if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
        raise SandboxOutputError(f"{label} columns must be strings")
    if not isinstance(rows, list) or len(rows) > MAX_OUTPUT_ROWS:
        raise SandboxOutputError(f"{label} rows exceed the limit")
    for row in rows:
        if not isinstance(row, list):
            raise SandboxOutputError(f"each {label} row must be a list")
        for item in row:
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                raise SandboxOutputError(f"{label} row values must be strings or numbers")
            if isinstance(item, float) and not math.isfinite(item):
                raise SandboxOutputError(f"{label} row contains a non-finite number")


def _validate_output(data: object) -> dict:
    if not isinstance(data, dict):
        raise SandboxOutputError("output must be a JSON object")
    if set(data) not in ({"metrics", "groups"}, {"metrics", "groups", "metric_groups"}):
        raise SandboxOutputError("output must contain exactly metrics, groups and optional metric_groups")
    metrics = data["metrics"]
    if not isinstance(metrics, dict) or len(metrics) > MAX_OUTPUT_METRICS:
        raise SandboxOutputError("metrics must be an object within size limits")
    for name, value in metrics.items():
        if not isinstance(name, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SandboxOutputError("metric values must be plain numbers")
        if not math.isfinite(float(value)):
            raise SandboxOutputError("metric value is not finite")
    _validate_groups(data["groups"], "groups")
    if "metric_groups" in data:
        metric_groups = data["metric_groups"]
        if not isinstance(metric_groups, dict) or len(metric_groups) > MAX_OUTPUT_METRICS:
            raise SandboxOutputError("metric_groups must be an object within size limits")
        for name, value in metric_groups.items():
            if not isinstance(name, str):
                raise SandboxOutputError("metric_groups keys must be strings")
            _validate_groups(value, f"metric_groups[{name}]")
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise SandboxOutputError("output exceeds the byte limit")
    return data


def run_sandbox(
    code: str,
    frame,
    *,
    allowed_columns: set[str] | list[str],
    timeout_seconds: float = 10.0,
    memory_mb: int = 1024,
    skip_validation: bool = False,
    max_input_rows: int = MAX_INPUT_ROWS,
) -> dict:
    """Validate + execute in a child process; raise typed errors on any failure."""
    if len(frame) > max_input_rows:
        raise SandboxResourceError(f"input has {len(frame)} rows, above {max_input_rows}")
    if not skip_validation:
        validate_code(code, allowed_columns)

    payload = json.dumps(
        {
            "code": code,
            "allowed_columns": sorted(str(column) for column in allowed_columns),
            "skip_validation": bool(skip_validation),
        }
    ).encode("utf-8")

    import pickle

    src_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-m", "insightflow.llm.sandbox_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Two-phase: let the worker import pandas first, then cap the job.  The cap
    # must stay above the pandas/numpy baseline commit (measured 524-530MB), so
    # the default is 1024MB; a 512MB cap intermittently kills the worker.
    ready = proc.stdout.readline() if proc.stdout else b""
    if ready.strip() != b"READY":
        _kill_tree(proc)
        raise SandboxOutputError("sandbox worker failed to start")

    job_ok = _win_job(proc.pid, int(memory_mb) * 1024 * 1024)
    # Fail closed: without a memory cap we refuse to run untrusted code.
    if not job_ok:
        _kill_tree(proc)
        raise SandboxResourceError("memory_guard_unavailable")

    blob = pickle.dumps(
        {
            "code": code,
            "frame": frame,
            "allowed_columns": sorted(str(c) for c in allowed_columns),
            "skip_validation": bool(skip_validation),
        }
    )
    try:
        proc.stdin.write(blob)
        proc.stdin.close()
        proc.stdin = None
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise SandboxTimeout(f"execution exceeded {timeout_seconds}s") from None

    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = message[-1] if message else "worker failed"
        if "MemoryError" in tail or proc.returncode in {3221225495, 3221225725}:
            raise SandboxResourceError("memory limit exceeded")
        raise SandboxOutputError(f"worker exit {proc.returncode}: {tail[:300]}")

    try:
        data = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SandboxOutputError(f"worker output is not JSON: {exc}") from None
    return _validate_output(data)
