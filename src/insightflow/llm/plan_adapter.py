"""Stage-2 plan adapter: LLM analysis plan behind the neutral ``PlanProvider``."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..planning import PlanResult
from ..semantic import FieldSemantics
from .client import CostLimitExceeded, DeepSeekClient, LLMConfig
from .contracts import ContractError
from .plan_contracts import PLAN_PROMPT_VERSION, parse_plan

PLAN_PROMPT_FILE = Path(__file__).with_name("prompts") / "plan_v1.md"


class LLMPlanProvider:
    """Dataset-level plan provider; one call per run."""

    name = "deepseek"

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Any | None = None,
        batch_budget_cny: float = 20.0,
        prompt_version: str = PLAN_PROMPT_VERSION,
        prompt_text: str | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.batch_budget_cny = float(batch_budget_cny)
        self.prompt_version = prompt_version
        self.prompt_text = prompt_text or PLAN_PROMPT_FILE.read_text(encoding="utf-8")
        self.spent_cny = 0.0

    def plan(
        self,
        fields: list[FieldSemantics],
        anomalies: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> PlanResult:
        if self.spent_cny >= self.batch_budget_cny:
            raise CostLimitExceeded(f"整批预算已用尽（{self.spent_cny:.2f} 元），计划降级")

        roles = {field.name: field.role for field in fields}
        payload = {
            "schema_version": "1.0",
            "table_count": int(context.get("table_count") or 1),
            "fields": [
                {
                    "name": field.name,
                    "role": field.role,
                    "unit": field.unit,
                    "meaning": field.meaning,
                    "confidence": round(float(field.confidence), 3),
                }
                for field in fields
            ],
            "anomalies": [
                {
                    "field": str(item.get("field", "")),
                    "reason": str(item.get("reason", "")),
                    "row_hint": str(item.get("row_hint", "")),
                }
                for item in anomalies
            ],
        }
        messages = [
            {"role": "system", "content": self.prompt_text},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        client = DeepSeekClient(self.config, transport=self.transport)
        started = time.perf_counter()

        draft = None
        last_error = ""
        for attempt in range(3):
            content, usage = client.complete(messages)
            try:
                draft = parse_plan(content, roles)
                break
            except ContractError as exc:
                last_error = str(exc)
                if attempt == 2:
                    raise
                messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            f"上次计划未通过校验：{exc}。请只引用输入 fields 中存在的字段："
                            "metrics 必须是 role=metric；dimensions 必须是 role=dimension；"
                            "图表 x 轴只能是 dimension 或 date（histogram/scatter 例外，可用 metric），"
                            "y 轴必须是 metric，分系列只能是 dimension；quality_risks 不能为空；"
                            "JSON 必须完整闭合（大括号成对），字符串内不要有未转义引号或换行。"
                        ),
                    }
                ]
        latency_ms = int((time.perf_counter() - started) * 1000)
        if draft is None:
            raise RuntimeError(f"计划两次均未通过校验：{last_error}")

        table_cost = client.meter.total_cny
        self.spent_cny += table_cost
        if self.spent_cny >= self.batch_budget_cny:
            raise CostLimitExceeded(f"整批预算已用尽（{self.spent_cny:.2f} 元）")
        return PlanResult(
            draft=draft,
            prompt_version=self.prompt_version,
            cost_cny=table_cost,
            latency_ms=latency_ms,
            source_kind="llm",
        )
