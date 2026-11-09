"""Zero-dependency line-coverage gate for the core package (P2-3).

The CI environment is allowed to install ``coverage.py``; this machine is
offline, so the gate is implemented with ``sys.settrace`` and an AST statement
count instead: same idea, no third-party dependency, reproducible everywhere.

Usage:
    python scripts/coverage_gate.py                 # gate at 85%
    python scripts/coverage_gate.py --threshold 70  # explicit threshold
    python scripts/coverage_gate.py --skip-gui      # faster: core tests only
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "insightflow"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
#: GUI is measured by its own test suites; the commercial gate is about the
#: analysis chain (discovery → readers → schema → normalize → cleaning →
#: planner → analysis → report → pipeline).
EXCLUDED_DIRS = {"gui"}
#: Entry points that run in a child process (or as a script) never appear in the
#: parent tracer: they are covered by their own integration/fault-injection tests.
EXCLUDED_FILES = {"__main__.py", "sandbox_worker.py"}

_EXECUTED: dict[str, set[int]] = defaultdict(set)


def _target(path: str) -> bool:
    file = Path(path)
    try:
        relative = file.relative_to(SRC)
    except ValueError:
        return False
    if relative.parts and relative.parts[0] in EXCLUDED_DIRS:
        return False
    return relative.name not in EXCLUDED_FILES


def _tracer(frame, event, arg):  # noqa: ANN001 - sys.settrace signature
    if not _target(frame.f_code.co_filename):
        # Do not trace unrelated frames line by line; keeps the gate fast.
        return None
    if event == "line":
        _EXECUTED[frame.f_code.co_filename].add(frame.f_lineno)
    return _tracer


def _statements(path: Path) -> set[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    return {node.lineno for node in ast.walk(tree) if isinstance(node, ast.stmt)}


def run(threshold: float, skip_gui: bool, show_missing: bool = False) -> int:
    loader = unittest.TestLoader()
    names = [f"tests.test_{name}" for name in ("defects", "pipeline", "readers", "schema_cleaning", "utils")]

    stream = io.StringIO()
    # Trace before *importing* the app/test modules: module-level and dataclass
    # class-body statements execute at import time and must count as covered.
    sys.settrace(_tracer)
    try:
        if skip_gui:
            suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in names)
        else:
            suite = loader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    finally:
        sys.settrace(None)

    if not result.wasSuccessful():
        print("测试未通过，覆盖率门槛不生效：")
        print(stream.getvalue()[-4000:])
        return 1

    total = covered = 0
    rows = []
    for path in sorted(SRC.rglob("*.py")):
        if not _target(str(path)):
            continue
        statements = _statements(path)
        if not statements:
            continue
        hit = statements & _EXECUTED.get(str(path), set())
        total += len(statements)
        covered += len(hit)
        rows.append((path.relative_to(ROOT).as_posix(), len(hit), len(statements)))

    if not total:
        print("没有收集到可统计的语句")
        return 1

    percent = covered / total * 100
    print(f"{'模块':<45}{'覆盖':>6}{'语句':>6}{'覆盖率':>9}")
    for name, hit, count in rows:
        print(f"{name:<45}{hit:>6}{count:>6}{hit / count * 100:>8.1f}%")
    print("-" * 66)
    print(f"核心合计：{covered}/{total} = {percent:.1f}%  门槛：{threshold:.1f}%")

    if show_missing:
        print("\n未覆盖行：")
        for path in sorted(SRC.rglob("*.py")):
            if not _target(str(path)):
                continue
            missing = sorted(_statements(path) - _EXECUTED.get(str(path), set()))
            if missing:
                rel = path.relative_to(ROOT).as_posix()
                chunks = [str(line) for line in missing]
                print(f"{rel}: {', '.join(chunks)}")
    return 0 if percent >= threshold else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核心代码行覆盖率门槛（零依赖）")
    parser.add_argument("--threshold", type=float, default=85.0)
    parser.add_argument("--skip-gui", action="store_true", help="只跑核心测试，速度更快")
    parser.add_argument("--show-missing", action="store_true", help="列出未覆盖行号")
    args = parser.parse_args(argv)
    return run(args.threshold, args.skip_gui, args.show_missing)


if __name__ == "__main__":
    raise SystemExit(main())
