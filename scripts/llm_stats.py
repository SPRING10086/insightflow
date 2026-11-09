"""Aggregate LLM statistics across runs (stage 5a-3).

    python scripts/llm_stats.py --root . --out docs/llm-stats.json

Reads ``output/*/run_record.json`` files and reports calls, JSON failures,
retries, fallbacks, budget-blocked retries and cost. A run without the
``llm_stats`` block (LLM disabled) is counted as a run but contributes zeros.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def aggregate(root: Path) -> dict[str, Any]:
    base = Path(root)
    runs = 0
    totals = {
        "calls": 0,
        "json_failures": 0,
        "retries": 0,
        "fallbacks": 0,
        "retry_blocked": 0,
        "cost_cny": 0.0,
        "max_prompt_tokens": 0,
        "prompt_warn_runs": 0,
        "runs_with_llm": 0,
    }
    for record_path in sorted(base.glob("output/*/run_record.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs += 1
        stats = (record.get("metadata") or {}).get("llm_stats") or {}
        if not stats:
            continue
        totals["runs_with_llm"] += 1
        for key in ("calls", "json_failures", "retries", "fallbacks", "retry_blocked"):
            totals[key] += int(stats.get(key) or 0)
        totals["cost_cny"] += float(stats.get("cost_cny") or 0.0)
        totals["max_prompt_tokens"] = max(totals["max_prompt_tokens"], int(stats.get("max_prompt_tokens") or 0))
        if stats.get("prompt_warn"):
            totals["prompt_warn_runs"] += 1

    calls = totals["calls"] or 1
    totals["cost_cny"] = round(totals["cost_cny"], 6)
    totals["runs"] = runs
    totals["json_failure_rate"] = round(totals["json_failures"] / calls, 4)
    totals["fallback_rate"] = round(totals["fallbacks"] / calls, 4)
    totals["avg_retries_per_call"] = round(totals["retries"] / calls, 4)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 InsightFlow 的 LLM 调用统计")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out", default=str(ROOT / "logs" / "llm_stats.json"))
    args = parser.parse_args(argv)
    summary = aggregate(Path(args.root))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
