"""Stage-3 real-call evidence: 3 acceptance samples + 1 real registry slice."""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.llm.adapter import LLMSemanticProvider  # noqa: E402
from insightflow.llm.client import LLMConfig  # noqa: E402
from insightflow.llm.executor import LLMExecutionProvider  # noqa: E402
from insightflow.llm.plan_adapter import LLMPlanProvider  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402

ACCEPTANCE = ROOT / "examples" / "acceptance"
# 可选外部真实样本：设置环境变量 INSIGHTFLOW_REAL_SAMPLE 指向本地 CSV；未设置时自动跳过，仅用 examples 合成样本
REAL = Path(os.environ.get("INSIGHTFLOW_REAL_SAMPLE", ""))
OUT = ROOT / "docs" / "llm-execution-evidence.json"


def _project(source: Path, rows_limit: int | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-exec-", dir=str(ROOT / "tests")))
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
        shutil.copyfile(source, tmp / "raw" / source.name)
    else:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            rows = [next(reader)]
            for index, row in enumerate(reader):
                if index >= rows_limit:
                    break
                rows.append(row)
        with (tmp / "raw" / source.name).open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerows(rows)
    return tmp


def main() -> int:
    config = LLMConfig.from_env()
    jobs = [
        ("01_主样本_干净", ACCEPTANCE / "01_主样本_干净.csv", "2026-11-07", None),
        ("02_边界样本_含问题", ACCEPTANCE / "02_边界样本_含问题.csv", "2026-11-07", None),
        ("03_工商登记_合成样本", ACCEPTANCE / "03_工商登记_合成样本.csv", "2026-11-07", None),
        ("real_2018_上海(前300行)", REAL, "2018-01-01", 300),
    ]
    rows = []
    for name, source, target, limit in jobs:
        if not source.is_file():
            rows.append({"sample": name, "status": "missing"})
            continue
        tmp = _project(source, limit)
        try:
            settings = load_settings(project_root=tmp)
            semantic = LLMSemanticProvider(config, batch_budget_cny=20.0)
            planner = LLMPlanProvider(config, batch_budget_cny=20.0)
            executor = LLMExecutionProvider(config, batch_budget_cny=20.0)
            record = execute(
                settings,
                target_date=target,
                force=True,
                semantic_provider=semantic,
                plan_provider=planner,
                execution_provider=executor,
            )
            day = settings.output_dir / target
            diff = json.loads((day / "execution_diff.json").read_text(encoding="utf-8"))
            llm = json.loads((day / "execution_llm.json").read_text(encoding="utf-8"))
            code = json.loads((day / "execution_code.json").read_text(encoding="utf-8"))
            rows.append(
                {
                    "sample": name,
                    "status": record.status,
                    "source_kind": llm["result"]["source_kind"],
                    "accepted": diff["accepted"],
                    "problems": diff["problems"],
                    "total_rule": diff["total_rule"],
                    "total_llm": diff["total_llm"],
                    "group_keys_rule": diff["group_keys_rule"],
                    "group_keys_llm": diff["group_keys_llm"],
                    "memory_guard": llm["result"]["memory_guard"],
                    "code_cost_cny": code["cost_cny"],
                    "code_latency_ms": code["latency_ms"],
                    "code": code["code"][:400],
                    "error": llm["result"]["error"],
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"sample": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            logging.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)
        print(rows[-1].get("sample"), rows[-1].get("status"), rows[-1].get("source_kind"), rows[-1].get("accepted"))

    OUT.write_text(json.dumps({"generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
