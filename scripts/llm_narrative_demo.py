"""Stage-4 real narrative calls: 3 acceptance samples + one real registry slice."""

from __future__ import annotations

import csv
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from insightflow.config import load_settings  # noqa: E402
from insightflow.llm.client import LLMConfig  # noqa: E402
from insightflow.llm.narrative_gen import LLMNarrativeProvider  # noqa: E402
from insightflow.pipeline import execute  # noqa: E402

ACCEPTANCE = ROOT / "examples" / "acceptance"
REAL = Path(r"C:\Users\Acer\Downloads\csv\2018\上海.csv")
OUT = ROOT / "docs" / "llm-narrative-evidence.json"


def _project(source: Path, rows_limit: int | None = None) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="insightflow-test-narr-", dir=str(ROOT / "tests")))
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
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
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
            provider = LLMNarrativeProvider(config, batch_budget_cny=20.0)
            record = execute(settings, target_date=target, force=True, narrative_provider=provider)
            day = settings.output_dir / target
            html = max(day.glob("*.html"), key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8")
            meta = record.metadata.get("narrative", {})
            render_path = day / "narrative_render.json"
            rendered = json.loads(render_path.read_text(encoding="utf-8"))["rendered"] if render_path.is_file() else {}
            llm_path = day / "narrative_llm.json"
            draft = json.loads(llm_path.read_text(encoding="utf-8")).get("draft") if llm_path.is_file() else None
            rows.append(
                {
                    "sample": name,
                    "status": record.status,
                    "source_kind": meta.get("source_kind"),
                    "reason": meta.get("reason", ""),
                    "prompt_version": meta.get("prompt_version"),
                    "placeholders": meta.get("placeholders", 0),
                    "cost_cny": meta.get("cost_cny", 0),
                    "latency_ms": meta.get("latency_ms", 0),
                    "section_rendered": "智能解读" in html,
                    "no_placeholder_left": not any(token in html for token in ("[[total.", "[[quality.", "[[kpi.")),
                    "title": (draft or {}).get("title", ""),
                    "summary": (draft or {}).get("summary", ""),
                    "rendered_summary": rendered.get("summary", ""),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"sample": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            logging.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)
        print(rows[-1].get("sample"), rows[-1].get("status"), rows[-1].get("source_kind"), rows[-1].get("reason", "")[:60])

    OUT.write_text(
        json.dumps({"generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("written", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
