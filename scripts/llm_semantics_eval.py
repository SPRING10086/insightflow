"""Evaluate the LLM semantic schema against the golden role baseline.

Runs the real provider (needs ``.env``) over the acceptance samples and prints
role accuracy, key-role errors, unit agreement, cost/latency and outlier recall.
The golden file is reviewer-owned; this script only reads it.

    python scripts/llm_semantics_eval.py --live
    python scripts/llm_semantics_eval.py --live --out logs/llm_semantics_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.llm.adapter import LLMSemanticProvider  # noqa: E402
from insightflow.llm.client import LLMConfig  # noqa: E402
from insightflow.llm.payloads import read_csv_rows  # noqa: E402
from insightflow.semantic import TableSample  # noqa: E402

GOLDEN = ROOT / "tests" / "data" / "llm_golden" / "roles.json"
SAMPLES = ROOT / "examples" / "acceptance"


def _overlaps(expected: str, predicted: str) -> bool:
    return bool(expected) and (expected in predicted or predicted in expected)


def evaluate_one(provider: LLMSemanticProvider, csv_path: Path, expected: dict[str, str], anomalies_expected: list[str]) -> dict[str, Any]:
    field_names, rows = read_csv_rows(csv_path)
    sample = TableSample(
        name=csv_path.stem,
        source=str(csv_path),
        sheet="CSV",
        field_names=field_names,
        rows=rows,
    )
    started = time.perf_counter()
    result = provider.describe(sample)
    latency_ms = int((time.perf_counter() - started) * 1000)

    predicted = {field.name: field.role for field in result.fields}
    correct = sum(1 for name, role in expected.items() if predicted.get(name) == role)
    key_role_errors = [
        name
        for name, role in expected.items()
        if role in {"date", "metric", "id"} and predicted.get(name) != role
    ]
    predicted_fields = {str(item.get("field", "")) for item in result.anomalies}
    recall = [name for name in anomalies_expected if any(_overlaps(name, hit) for hit in predicted_fields)]
    return {
        "sample": csv_path.name,
        "fields_total": len(expected),
        "fields_correct": correct,
        "role_accuracy": round(correct / max(1, len(expected)), 4),
        "key_role_errors": key_role_errors,
        "mismatches": {name: {"expected": role, "got": predicted.get(name, "<missing>")} for name, role in expected.items() if predicted.get(name) != role},
        "anomalies_expected": anomalies_expected,
        "anomalies_predicted": sorted(predicted_fields),
        "anomaly_recall": round(len(recall) / max(1, len(anomalies_expected)), 4) if anomalies_expected else 1.0,
        "cost_cny": round(result.cost_cny, 6),
        "latency_ms": latency_ms,
        "prompt_version": result.prompt_version,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 语义 Schema 一致率评估（真实调用）")
    parser.add_argument("--golden", default=str(GOLDEN))
    parser.add_argument("--samples", default=str(SAMPLES))
    parser.add_argument("--out", default=str(ROOT / "logs" / "llm_semantics_eval.json"))
    args = parser.parse_args(argv)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    if not str(golden.get("status", "")).startswith("draft") and golden.get("status") != "confirmed":
        print(f"golden 状态未知：{golden.get('status')!r}", file=sys.stderr)
    if golden.get("status") != "confirmed":
        print("注意：golden 仍为草稿（draft），待审查方确认后数字才具最终效力。", file=sys.stderr)

    try:
        provider = LLMSemanticProvider(LLMConfig.from_env())
    except Exception as exc:  # noqa: BLE001
        print(f"无法初始化 provider（检查 .env）：{exc}", file=sys.stderr)
        return 2

    rows: list[dict[str, Any]] = []
    for stem, expected in (golden.get("samples") or {}).items():
        csv_path = Path(args.samples) / f"{stem}.csv"
        if not csv_path.is_file():
            print(f"跳过缺失样本：{csv_path}", file=sys.stderr)
            continue
        anomalies_expected = (golden.get("anomalies_expected") or {}).get(stem, [])
        try:
            rows.append(evaluate_one(provider, csv_path, expected, anomalies_expected))
        except Exception as exc:  # noqa: BLE001
            rows.append({"sample": csv_path.name, "error": f"{type(exc).__name__}: {exc}"})

    total_fields = sum(row.get("fields_total", 0) for row in rows)
    correct = sum(row.get("fields_correct", 0) for row in rows)
    total_cost = sum(row.get("cost_cny", 0) for row in rows)
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "golden_status": golden.get("status"),
        "prompt_version": rows[0].get("prompt_version") if rows else "",
        "samples": len(rows),
        "fields_total": total_fields,
        "fields_correct": correct,
        "role_accuracy": round(correct / total_fields, 4) if total_fields else 0.0,
        "key_role_errors": [err for row in rows for err in row.get("key_role_errors", [])],
        "anomaly_recall_avg": round(sum(row.get("anomaly_recall", 0) for row in rows) / len(rows), 4) if rows else 0.0,
        "cost_total_cny": round(total_cost, 6),
        "latency_avg_ms": round(sum(row.get("latency_ms", 0) for row in rows) / len(rows)) if rows else 0,
        "rows": rows,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    for row in rows:
        if "error" in row:
            print(f"[FAIL] {row['sample']}: {row['error']}")
            continue
        print(
            f"[{row['sample']}] 角色 {row['fields_correct']}/{row['fields_total']}"
            f" = {row['role_accuracy']:.1%} · 关键错误 {len(row['key_role_errors'])}"
            f" · 异常召回 {row['anomaly_recall']:.0%} · {row['cost_cny']:.4f} 元 · {row['latency_ms']} ms"
        )
    print(
        f"合计：{summary['samples']} 个样本 · 角色一致率 {summary['role_accuracy']:.1%}"
        f" · 关键角色错误 {len(summary['key_role_errors'])} · 成本 {summary['cost_total_cny']:.4f} 元"
    )
    print(f"结果已写入 {args.out}")
    return 0 if not summary["key_role_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
