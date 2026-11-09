"""Stage-2 dual-track plan diff: rule plan vs LLM plan on 3 acceptance + 2 real samples.

Real registry samples are read-only slices (first 300 rows) copied into a temp
project; the originals under Downloads are never modified or bulk-uploaded.

    python scripts/llm_plan_diff.py
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.llm.adapter import LLMSemanticProvider  # noqa: E402
from insightflow.llm.client import LLMConfig  # noqa: E402
from insightflow.llm.plan_adapter import LLMPlanProvider  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402

ACCEPTANCE = ROOT / "examples" / "acceptance"
REAL = Path(r"C:\Users\Acer\Downloads\csv")
OUT_JSON = ROOT / "docs" / "llm-plan-diff.json"
OUT_MD = ROOT / "docs" / "llm-plan-diff.md"


def _project(source_csv: Path, target_date: str, rows_limit: int | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-plan-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    (tmp / "config" / "config.json").write_text(
        json.dumps(
            {
                "source_dir": "raw",
                "output_dir": "out",
                "log_dir": "logs",
                "state_dir": "state",
                "comparison": {"enabled": True, "lookback_days": 1},
                "max_charts": 4,
            }
        ),
        encoding="utf-8",
    )
    if rows_limit is None:
        shutil.copyfile(source_csv, tmp / "raw" / source_csv.name)
    else:
        with source_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            rows = [next(reader)]
            for index, row in enumerate(reader):
                if index >= rows_limit:
                    break
                rows.append(row)
        with (tmp / "raw" / source_csv.name).open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerows(rows)
    return tmp


def _first_date(csv_path: Path) -> str:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            for key in ("注册日期", "日期", "订单日期"):
                if key in row and row[key]:
                    return str(row[key])[:10]
    return "2018-01-01"


def run_sample(name: str, csv_path: Path, target_date: str, rows_limit: int | None, providers) -> dict[str, Any]:
    semantic_provider, plan_provider = providers
    tmp = _project(csv_path, target_date, rows_limit)
    try:
        settings = load_settings(project_root=tmp)
        record = execute(
            settings,
            target_date=target_date,
            force=True,
            semantic_provider=semantic_provider,
            plan_provider=plan_provider,
        )
        day = settings.output_dir / target_date
        diff = json.loads((day / "plan_diff.json").read_text(encoding="utf-8"))
        llm_plan = json.loads((day / "plan_llm.json").read_text(encoding="utf-8"))
        return {
            "sample": name,
            "status": record.status,
            "semantic_source": (record.metadata.get("semantic_schema") or {}).get("source_kind"),
            "plan_source": llm_plan["source_kind"],
            "metrics": diff["metrics"],
            "dimensions": diff["dimensions"],
            "charts": diff["charts"],
            "questions": diff["questions"],
            "quality_risks": diff["quality_risks"],
            "plan_cost_cny": llm_plan["cost_cny"],
            "plan_latency_ms": llm_plan["latency_ms"],
            "plan_error": llm_plan.get("error", ""),
        }
    finally:
        logging.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    config = LLMConfig.from_env()
    semantic_provider = LLMSemanticProvider(config, batch_budget_cny=20.0)
    plan_provider = LLMPlanProvider(config, batch_budget_cny=20.0)

    jobs: list[tuple[str, Path, str, int | None]] = [
        ("01_主样本_干净", ACCEPTANCE / "01_主样本_干净.csv", "2026-11-07", None),
        ("02_边界样本_含问题", ACCEPTANCE / "02_边界样本_含问题.csv", "2026-11-07", None),
        ("03_工商登记_合成样本", ACCEPTANCE / "03_工商登记_合成样本.csv", "2026-11-07", None),
    ]
    real_2018 = REAL / "2018" / "上海.csv"
    real_2019 = REAL / "2019" / "上海.csv"
    if real_2018.is_file():
        jobs.append(("real_2018_上海(前300行)", real_2018, _first_date(real_2018), 300))
    if real_2019.is_file():
        jobs.append(("real_2019_上海(前300行)", real_2019, _first_date(real_2019), 300))

    rows: list[dict[str, Any]] = []
    for name, path, target, limit in jobs:
        if not path.is_file():
            rows.append({"sample": name, "status": "missing_source"})
            continue
        try:
            rows.append(run_sample(name, path, target, limit, (semantic_provider, plan_provider)))
        except Exception as exc:  # noqa: BLE001 - record honestly, keep going
            rows.append({"sample": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        print(f"[{rows[-1].get('status')}] {name}")

    summary = {
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "semantic_cost_cny": round(semantic_provider.spent_cny, 6),
        "plan_cost_cny": round(plan_provider.spent_cny, 6),
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 阶段 2 双轨分析计划差异报告",
        "",
        f"生成时间：{summary['generated_at']} · 语义成本 {summary['semantic_cost_cny']} 元 · 计划成本 {summary['plan_cost_cny']} 元",
        "",
        "| 样本 | 状态 | 计划来源 | 指标 Jaccard | 维度 Jaccard | 图表 Jaccard | 仅 LLM 指标 | 仅 LLM 维度 | 质量风险数 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if "metrics" not in row:
            lines.append(f"| {row['sample']} | {row['status']} | - | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {row['sample']} | {row['status']} | {row['plan_source']} | "
            f"{row['metrics']['jaccard']} | {row['dimensions']['jaccard']} | {row['charts']['jaccard']} | "
            f"{', '.join(row['metrics']['only_llm']) or '-'} | "
            f"{', '.join(row['dimensions']['only_llm']) or '-'} | {row['quality_risks']['llm_count']} |"
        )
    lines.append("")
    lines.append("> 差异中性呈现：`only_llm` 既可能是合理补充，也可能是误判，需人工按 checklist 裁定。")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written {OUT_JSON} and {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
