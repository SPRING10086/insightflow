"""Aggregate LLM adoption/fallback/reconcile statistics across runs (stage 6c).

    python scripts/llm_stats.py --root . --out docs/llm-stats.json

Reads ``output/*/run_record.json`` and reports, per capability:
mode distribution, adoption count/rate, fallback count/rate and gate reasons;
plus execute reconcile pass rate, semantic role agreement, plan Jaccard, cost,
JSON-failure/retry counters, distinct data fingerprints and a 6c -> 6d
readiness verdict against the configured thresholds.

A run without the ``llm_stats`` block (LLM disabled) still counts as a run but
contributes zeros.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FEATURES = ("semantic", "plan", "execute", "narrative")


def _section(meta: dict[str, Any], name: str) -> dict[str, Any]:
    if name == "semantic":
        return meta.get("semantic_schema") or {}
    if name == "execute":
        return meta.get("execution") or {}
    return meta.get(name) or {}


def _adopted(meta: dict[str, Any], name: str) -> bool:
    if name == "narrative":
        return bool((meta.get("narrative") or {}).get("source_kind") == "llm")
    return bool(_section(meta, name).get("adopted"))


def _reason(meta: dict[str, Any], name: str) -> str:
    section = _section(meta, name)
    reason = str(section.get("gate_reason") or "")
    if not reason and name == "narrative":
        reason = str(section.get("reason") or "")
    return reason


def aggregate(
    root: Path,
    *,
    required_runs: int = 10,
    max_fallback_rate: float = 0.05,
    min_samples: int = 3,
    sample_classes: int | None = None,
) -> dict[str, Any]:
    base = Path(root)
    runs = 0
    fingerprints: set[str] = set()
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
    features: dict[str, dict[str, Any]] = {
        name: {
            "attempted": 0,
            "adopted": 0,
            "fallbacks": 0,
            "modes": {},
            "reasons": {},
        }
        for name in FEATURES
    }
    reconcile_attempted = 0
    reconcile_passed = 0
    role_agreements: list[float] = []
    plan_jaccards: list[float] = []

    for record_path in sorted(base.glob("output/*/run_record.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs += 1
        fingerprint = str(record.get("fingerprint") or "")
        if fingerprint:
            fingerprints.add(fingerprint)

        meta = record.get("metadata") or {}
        stats = meta.get("llm_stats") or {}
        if stats:
            totals["runs_with_llm"] += 1
            for key in ("calls", "json_failures", "retries", "fallbacks", "retry_blocked"):
                totals[key] += int(stats.get(key) or 0)
            totals["cost_cny"] += float(stats.get("cost_cny") or 0.0)
            totals["max_prompt_tokens"] = max(
                totals["max_prompt_tokens"], int(stats.get("max_prompt_tokens") or 0)
            )
            if stats.get("prompt_warn"):
                totals["prompt_warn_runs"] += 1

        adoption = meta.get("adoption") or {}
        final_mode = adoption.get("final_mode") or {}
        for name in FEATURES:
            mode = str(final_mode.get(name, "absent"))
            entry = features[name]
            entry["modes"][mode] = entry["modes"].get(mode, 0) + 1
            if mode in {"shadow", "absent"}:
                continue
            entry["attempted"] += 1
            if _adopted(meta, name):
                entry["adopted"] += 1
            else:
                entry["fallbacks"] += 1
                reason = _reason(meta, name) or "unknown"
                entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1

        if final_mode.get("execute") not in (None, "shadow", "absent"):
            reconcile_attempted += 1
            if (meta.get("execution") or {}).get("accepted"):
                reconcile_passed += 1
        semantic = meta.get("semantic_schema") or {}
        if semantic.get("role_agreement") is not None:
            role_agreements.append(float(semantic["role_agreement"]))
        plan_meta = meta.get("plan") or {}
        if plan_meta.get("metrics_jaccard") is not None:
            plan_jaccards.append(float(plan_meta["metrics_jaccard"]))

    for entry in features.values():
        attempted = entry["attempted"]
        entry["adoption_rate"] = round(entry["adopted"] / attempted, 4) if attempted else 0.0
        entry["fallback_rate"] = round(entry["fallbacks"] / attempted, 4) if attempted else 0.0

    attempted_total = sum(entry["attempted"] for entry in features.values())
    fallback_total = sum(entry["fallbacks"] for entry in features.values())
    overall_fallback_rate = round(fallback_total / attempted_total, 4) if attempted_total else 0.0
    reconcile_rate = (
        round(reconcile_passed / reconcile_attempted, 4) if reconcile_attempted else 1.0
    )

    calls = totals["calls"] or 1
    sample_count = sample_classes if sample_classes is not None else len(fingerprints)
    summary: dict[str, Any] = dict(totals)
    summary.update(
        {
            "runs": runs,
            "samples": sample_count,
            "cost_cny": round(totals["cost_cny"], 6),
            "json_failure_rate": round(totals["json_failures"] / calls, 4),
            "fallback_rate": round(totals["fallbacks"] / calls, 4),
            "avg_retries_per_call": round(totals["retries"] / calls, 4),
            "features": features,
            "overall_fallback_rate": overall_fallback_rate,
            "reconcile_attempted": reconcile_attempted,
            "reconcile_passed": reconcile_passed,
            "reconcile_pass_rate": reconcile_rate,
            "semantic_role_agreement_avg": round(sum(role_agreements) / len(role_agreements), 4)
            if role_agreements
            else 0.0,
            "plan_metrics_jaccard_avg": round(sum(plan_jaccards) / len(plan_jaccards), 4)
            if plan_jaccards
            else 0.0,
        }
    )

    blockers: list[str] = []
    if runs < required_runs:
        blockers.append(f"runs {runs} < {required_runs}")
    if sample_count < min_samples:
        blockers.append(f"samples {sample_count} < {min_samples}")
    if overall_fallback_rate > max_fallback_rate:
        blockers.append(f"fallback_rate {overall_fallback_rate} > {max_fallback_rate}")
    if reconcile_attempted and reconcile_rate < 1.0:
        blockers.append(f"reconcile_pass_rate {reconcile_rate} < 1.0")
    summary["readiness"] = {
        "required_runs": required_runs,
        "required_samples": min_samples,
        "max_fallback_rate": max_fallback_rate,
        "ready": runs > 0 and not blockers,
        "blockers": blockers,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总 InsightFlow 的 LLM 采信/回退/对账统计")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out", default=str(ROOT / "logs" / "llm_stats.json"))
    parser.add_argument("--required-runs", type=int, default=10)
    parser.add_argument("--min-samples", type=int, default=3)
    parser.add_argument("--max-fallback-rate", type=float, default=0.05)
    parser.add_argument(
        "--sample-classes",
        type=int,
        default=None,
        help="data classes covered (defaults to distinct run fingerprints)",
    )
    args = parser.parse_args(argv)
    summary = aggregate(
        Path(args.root),
        required_runs=args.required_runs,
        min_samples=args.min_samples,
        max_fallback_rate=args.max_fallback_rate,
        sample_classes=args.sample_classes,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
