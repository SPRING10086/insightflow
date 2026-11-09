"""Run sample 03 three times and record whether the LLM plan is stable.

    python scripts/llm_plan_stability.py
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.llm.adapter import LLMSemanticProvider  # noqa: E402
from insightflow.llm.client import LLMConfig  # noqa: E402
from insightflow.llm.plan_adapter import LLMPlanProvider  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402

SAMPLE = ROOT / "examples" / "acceptance" / "03_工商登记_合成样本.csv"
OUT = ROOT / "docs" / "llm-plan-stability.json"


def _project() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-stab-", dir=str(ROOT / "tests")))
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
    shutil.copyfile(SAMPLE, tmp / "raw" / SAMPLE.name)
    return tmp


def main() -> int:
    config = LLMConfig.from_env()
    rows = []
    for index in range(3):
        tmp = _project()
        try:
            settings = load_settings(project_root=tmp)
            semantic = LLMSemanticProvider(config, batch_budget_cny=20.0)
            planner = LLMPlanProvider(config, batch_budget_cny=20.0)
            record = execute(
                settings,
                target_date="2026-11-07",
                force=True,
                semantic_provider=semantic,
                plan_provider=planner,
            )
            day = settings.output_dir / "2026-11-07"
            llm_plan = json.loads((day / "plan_llm.json").read_text(encoding="utf-8"))
            semantic_artifact = json.loads((day / "semantic_schema.json").read_text(encoding="utf-8"))
            diff = json.loads((day / "plan_diff.json").read_text(encoding="utf-8"))
            rows.append(
                {
                    "run": index + 1,
                    "status": record.status,
                    "semantic_source": semantic_artifact["source_kind"],
                    "plan_source": llm_plan["source_kind"],
                    "plan_error": llm_plan.get("error", ""),
                    "metrics": llm_plan["plan"]["metrics"],
                    "dimensions": llm_plan["plan"]["dimensions"],
                    "charts": [item["chart_type"] for item in llm_plan["plan"]["charts"]],
                    "risks": len(llm_plan["plan"]["quality_risks"]),
                    "dimension_jaccard": diff["dimensions"]["jaccard"],
                    "cost_cny": llm_plan["cost_cny"],
                    "latency_ms": llm_plan["latency_ms"],
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"run": index + 1, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            logging.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)

    OUT.write_text(json.dumps({"sample": SAMPLE.name, "runs": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(row.get("run"), row.get("plan_source"), row.get("dimensions"), row.get("plan_error", "")[:60])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
