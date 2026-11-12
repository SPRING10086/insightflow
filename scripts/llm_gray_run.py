"""Stage 6c gray observation: 4 sample types x 3 runs with all four capabilities gated.

Writes per-run records under ``logs/gray/output/<sample>-<n>/run_record.json`` and
aggregates them into ``docs/llm-stage6-gray.json``.
"""

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
sys.path.insert(0, str(ROOT / "scripts"))

from insightflow.adoption import resolve_adoption  # noqa: E402
from insightflow.config import load_settings  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402
from insightflow.semantic_assembly import (  # noqa: E402
    build_execution_provider,
    build_narrative_provider,
    build_plan_provider,
    build_semantic_provider,
)
from llm_stats import aggregate  # noqa: E402

ACCEPTANCE = ROOT / "examples" / "acceptance"
# 可选外部真实样本：设置环境变量 INSIGHTFLOW_REAL_SAMPLE 指向本地 CSV；未设置时自动跳过，仅用 examples 合成样本
REAL = Path(os.environ.get("INSIGHTFLOW_REAL_SAMPLE", ""))
GRAY = ROOT / "logs" / "gray"
OUT = ROOT / "docs" / "llm-stage6-gray.json"


def _project(source: Path, target: str, rows_limit: int | None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-gray-", dir=str(ROOT / "tests")))
    (tmp / "raw").mkdir()
    (tmp / "config").mkdir()
    config = {
        "source_dir": "raw",
        "output_dir": "out",
        "log_dir": "logs",
        "state_dir": "state",
        "comparison": {"enabled": True, "lookback_days": 1},
        "max_charts": 4,
        "llm": {
            "enabled": True,
            "features": {"semantic": True, "plan": True, "execute": True, "narrative": True},
            "adoption": {
                "semantic": "gated",
                "plan": "gated",
                "execute": "gated",
                "narrative": "gated",
            },
            "budget": {"per_table_cny": 5.0, "batch_cny": 20.0},
        },
    }
    (tmp / "config" / "config.json").write_text(json.dumps(config), encoding="utf-8")
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


def _providers(settings):
    llm = settings.llm
    kw = {
        "batch_budget_cny": llm.budget_batch_cny,
        "request_timeout_seconds": llm.request_timeout_seconds,
        "transport_retries": llm.retry_transport,
        "max_structured_retries": llm.retry_structured,
        "warn_prompt_tokens": llm.warn_prompt_tokens,
    }
    return {
        "semantic_provider": build_semantic_provider("llm", **kw),
        "plan_provider": build_plan_provider("llm", **kw),
        "execution_provider": build_execution_provider(
            "llm",
            batch_budget_cny=llm.budget_batch_cny,
            timeout_seconds=llm.sandbox_timeout_seconds,
            memory_mb=llm.memory_mb,
            request_timeout_seconds=llm.request_timeout_seconds,
            transport_retries=llm.retry_transport,
            warn_prompt_tokens=llm.warn_prompt_tokens,
        ),
        "narrative_provider": build_narrative_provider("llm", **kw),
    }


def main() -> int:
    jobs = [
        ("01_clean", ACCEPTANCE / "01_主样本_干净.csv", "2026-11-07", None),
        ("02_boundary", ACCEPTANCE / "02_边界样本_含问题.csv", "2026-11-07", None),
        ("03_registry", ACCEPTANCE / "03_工商登记_合成样本.csv", "2026-11-07", None),
        ("real_2018_sh", REAL, "2018-01-01", 300),
    ]
    runs_per_sample = 3
    gray_output = GRAY / "output"
    shutil.rmtree(gray_output, ignore_errors=True)
    gray_output.mkdir(parents=True, exist_ok=True)

    rows = []
    for name, source, target, limit in jobs:
        if not source.is_file():
            rows.append({"sample": name, "status": "missing"})
            continue
        for index in range(runs_per_sample):
            tmp = _project(source, target, limit)
            try:
                settings = load_settings(project_root=tmp)
                providers = _providers(settings)
                record = execute(
                    settings,
                    target_date=target,
                    force=True,
                    adoption_decision=resolve_adoption(settings.llm),
                    **providers,
                )
                day = settings.output_dir / target
                record_path = day / "run_record.json"
                if record_path.is_file():
                    dest = gray_output / f"{name}-{index + 1}"
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(record_path, dest / "run_record.json")
                meta = record.metadata or {}
                rows.append(
                    {
                        "sample": name,
                        "run": index + 1,
                        "status": record.status,
                        "semantic": (meta.get("semantic_schema") or {}).get("adopted"),
                        "plan": (meta.get("plan") or {}).get("adopted"),
                        "execute": (meta.get("execution") or {}).get("adopted"),
                        "reconcile": (meta.get("execution") or {}).get("accepted"),
                        "narrative": (meta.get("narrative") or {}).get("source_kind"),
                        "cost_cny": (meta.get("llm_stats") or {}).get("cost_cny"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                rows.append({"sample": name, "run": index + 1, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                logging.shutdown()
                shutil.rmtree(tmp, ignore_errors=True)
            print(name, index + 1, rows[-1].get("status"), rows[-1].get("semantic"), rows[-1].get("plan"), rows[-1].get("execute"))

    summary = aggregate(GRAY, required_runs=10, min_samples=3, sample_classes=len(jobs))
    OUT.write_text(
        json.dumps(
            {
                "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                "runs": rows,
                "aggregate": summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("ready:", summary["readiness"])
    print("written", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
