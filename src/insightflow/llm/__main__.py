"""CLI for the LLM PoC.

    python -m insightflow.llm payload examples/acceptance/01_主样本_干净.csv
    python -m insightflow.llm plan    examples/acceptance/01_主样本_干净.csv --dry-run
    python -m insightflow.llm plan    examples/acceptance/01_主样本_干净.csv          # 需要密钥与单价

``payload`` and ``--dry-run`` never touch the network: they print the exact
masked payload so it can be reviewed before anything leaves the machine.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .client import CostLimitExceeded, DeepSeekClient, LLMConfig, LLMConfigError, LLMRequestError
from .contracts import PROMPT_VERSION, ContractError, parse_result
from .payloads import build_payload, dump_payload, read_csv_rows

EXIT_CONFIG = 2
EXIT_REQUEST = 3
EXIT_CONTRACT = 4
EXIT_COST = 5


def _prompt_text() -> str:
    path = Path(__file__).with_name("prompts") / "analysis_plan_v1.md"
    return path.read_text(encoding="utf-8")


def _payload_for(csv_path: Path) -> dict:
    fields, rows = read_csv_rows(csv_path)
    return build_payload(table_name=csv_path.stem, source=str(csv_path), field_names=fields, rows=rows)


def _cmd_payload(args: argparse.Namespace) -> int:
    payload = _payload_for(Path(args.csv))
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        print(f"已落盘：{dump_payload(payload, args.out)}", file=sys.stderr)
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    payload = _payload_for(Path(args.csv))
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("dry-run：仅输出脱敏载荷，未发起网络请求。", file=sys.stderr)
        return 0

    try:
        config = LLMConfig.from_env()
    except LLMConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    messages = [
        {"role": "system", "content": _prompt_text()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    client = DeepSeekClient(config)
    started = time.perf_counter()
    try:
        content, usage = client.complete(messages)
    except CostLimitExceeded as exc:
        print(f"成本熔断：{exc}", file=sys.stderr)
        return EXIT_COST
    except LLMRequestError as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return EXIT_REQUEST
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    try:
        result = parse_result(content, usage)
    except ContractError as exc:
        print(f"契约校验失败：{exc}", file=sys.stderr)
        return EXIT_CONTRACT

    summary = {
        "prompt_version": PROMPT_VERSION,
        "model": config.model,
        "latency_ms": elapsed_ms,
        "usage": usage,
        "cost": client.meter.summary(),
        "fields": [field.__dict__ for field in result.fields],
        "plan": {
            "questions": result.plan.questions,
            "metrics": result.plan.metrics,
            "dimensions": result.plan.dimensions,
            "charts": [chart.__dict__ for chart in result.plan.charts],
            "quality_focus": result.plan.quality_focus,
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insightflow.llm", description="InsightFlow LLM PoC（不接入主流程）")
    sub = parser.add_subparsers(dest="command", required=True)

    payload = sub.add_parser("payload", help="只输出脱敏载荷，不联网")
    payload.add_argument("csv")
    payload.add_argument("--out", help="把载荷写到指定文件，便于人工检查")
    payload.set_defaults(handler=_cmd_payload)

    plan = sub.add_parser("plan", help="调用模型产出语义 Schema 与分析计划")
    plan.add_argument("csv")
    plan.add_argument("--dry-run", action="store_true", help="只打印载荷，不调用 API")
    plan.set_defaults(handler=_cmd_plan)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
