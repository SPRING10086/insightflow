"""Independent reconciliation for one run directory (P2-3).

Reads the artifacts of a finished run and re-derives the invariants that a
silent data bug would break:

* ``cleaned_data.csv`` row count == ``metrics.quality.rows_out``
* ``rows_in - duplicates_removed - dropped_all_empty == rows_out``
* every summed metric total in ``metrics.target_totals`` equals the sum of the
  cleaned column (averaged metrics compare to the column mean)
* every row keeps a positive ``__source_row`` (lineage is traceable)
* the imputed row flags are consistent with ``missing_filled``

Exit code 0 when everything reconciles, 1 otherwise.  This script is
deliberately independent from ``pipeline.py``: it re-computes from the exported
files, so a bug in the pipeline cannot excuse itself.

Usage: ``python scripts/reconcile.py output/2026-11-07``
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

TOLERANCE = 1e-6


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _close(left: float, right: float) -> bool:
    scale = max(1.0, abs(left), abs(right))
    return abs(left - right) <= TOLERANCE * scale


def reconcile(day_dir: str | Path) -> dict[str, Any]:
    """Return ``{"ok": bool, "checks": [...], "errors": [...]}``."""
    day = Path(day_dir)
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            errors.append(f"{name}: {detail}")

    metrics_path = day / "metrics.json"
    cleaned_path = day / "cleaned_data.csv"
    plan_path = day / "analysis_plan.json"
    for path in (metrics_path, cleaned_path, plan_path):
        if not path.is_file():
            check(f"缺少产物 {path.name}", False, str(path))
    if errors:
        return {"ok": False, "checks": checks, "errors": errors}

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    quality = metrics.get("quality") or {}

    with cleaned_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = reader.fieldnames or []

    # 1) row conservation
    rows_out = int(quality.get("rows_out") or 0)
    check("清洗后行数与 metrics 一致", len(rows) == rows_out, f"csv={len(rows)} metrics={rows_out}")

    rows_in = int(quality.get("rows_in") or 0)
    duplicates = int(quality.get("duplicates_removed") or 0)
    dropped = int(quality.get("dropped_all_empty") or 0)
    check(
        "行数守恒（入 - 去重 - 空行 = 出）",
        rows_in - duplicates - dropped == rows_out,
        f"{rows_in} - {duplicates} - {dropped} = {rows_in - duplicates - dropped}，期望 {rows_out}",
    )

    # 2) totals vs cleaned columns
    aggregations = {
        str(item.get("key")): str(item.get("aggregation") or "SUM")
        for item in plan.get("metric_definitions") or []
    }
    totals = (metrics.get("target_totals") or {}).get("metrics") or {}
    compared = 0
    for key, expected in totals.items():
        if key not in columns:
            check(f"指标列存在 {key}", False, "cleaned_data.csv 缺少该列")
            continue
        values = [value for value in (_num(row.get(key)) for row in rows) if value is not None]
        if not values:
            continue
        actual = sum(values) / len(values) if aggregations.get(key) == "AVG" else sum(values)
        compared += 1
        check(
            f"指标合计对账 {key}",
            _close(float(expected), actual),
            f"metrics={expected} cleaned={actual}（{aggregations.get(key, 'SUM')}）",
        )
    check("至少对账一个指标", compared > 0, f"compared={compared}")

    # 3) lineage
    if "__source_row" in columns:
        bad_rows = [
            index
            for index, row in enumerate(rows, start=2)
            if not str(row.get("__source_row") or "").strip()
            or (row.get("__source_row") or "").strip() in {"0", "nan", "None"}
        ]
        check("血缘行号完整", not bad_rows, f"异常行号 {len(bad_rows)} 个（示例：{bad_rows[:5]}）")
    else:
        check("血缘行号列存在", False, "__source_row 缺失")

    # 4) imputed flags vs metrics counts
    missing_filled = quality.get("missing_filled") or {}
    expected_values = sum(int(value or 0) for value in missing_filled.values())
    imputed_rows = 0
    if "__imputed" in columns:
        imputed_rows = sum(1 for row in rows if str(row.get("__imputed")).lower() == "true")
    if expected_values:
        check(
            "填补标记与 metrics 相容",
            0 < imputed_rows <= expected_values,
            f"imputed_rows={imputed_rows} 填补值={expected_values}",
        )
    else:
        check("无填补时无填补标记", imputed_rows == 0, f"imputed_rows={imputed_rows}")

    return {"ok": not errors, "checks": checks, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法: python scripts/reconcile.py <output/日期 目录>")
        return 2
    report = reconcile(args[0])
    for item in report["checks"]:
        mark = "PASS" if item["ok"] else "FAIL"
        print(f"[{mark}] {item['name']}: {item['detail']}")
    if report["ok"]:
        print(f"对账通过：{len(report['checks'])} 项")
        return 0
    print(f"对账失败：{len(report['errors'])} 项不一致")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
