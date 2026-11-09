"""Stage-2 plan adapter: LLM analysis plan behind the neutral ``PlanProvider``."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..planning import PlanResult
from ..semantic import FieldSemantics
from .client import CostLimitExceeded, DeepSeekClient, LLMConfig
from .contracts import ContractError
from .plan_contracts import PLAN_PROMPT_VERSION, parse_plan
from .stats import LLMStats

PLAN_PROMPT_FILE = Path(__file__).with_name("prompts") / "plan_v1.md"


class LLMPlanProvider:
    """Dataset-level plan provider; one plan per run (with bounded retries)."""

    name = "deepseek"

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Any | None = None,
        batch_budget_cny: float = 20.0,
        prompt_version: str = PLAN_PROMPT_VERSION,
        prompt_text: str | None = None,
        stats: LLMStats | None = None,
        max_structured_retries: int = 2,
        warn_prompt_tokens: int = 8000,
    ) -> None:
        self.config = config
        self.transport = transport
        self.batch_budget_cny = float(batch_budget_cny)
        self.prompt_version = prompt_version
        self.prompt_text = prompt_text or PLAN_PROMPT_FILE.read_text(encoding="utf-8")
        self.spent_cny = 0.0
        self.stats = stats or LLMStats()
        self.max_structured_retries = max(0, int(max_structured_retries))
        self.warn_prompt_tokens = int(warn_prompt_tokens)

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
        attempts = 1 + self.max_structured_retries
        for attempt in range(attempts):
            self.stats.record_call()
            content, usage = client.complete(messages)
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            self.stats.add_usage(
                prompt_tokens,
                client.meter.cost_for(prompt_tokens, int(usage.get("completion_tokens") or 0)),
                self.warn_prompt_tokens,
            )
            if self.warn_prompt_tokens and prompt_tokens > self.warn_prompt_tokens:
                logging.getLogger(__name__).warning(
                    "LLM prompt tokens %s exceed warn threshold %s", prompt_tokens, self.warn_prompt_tokens
                )
            try:
                draft = parse_plan(content, roles)
                break
            except ContractError as exc:
                last_error = str(exc)
                self.stats.record_json_failure()
                if attempt == attempts - 1:
                    raise
                remaining = self.batch_budget_cny - self.spent_cny - client.meter.total_cny
                estimate = client.meter.cost_for(prompt_tokens, self.config.max_output_tokens)
                if remaining <= 0 or estimate > remaining:
                    self.stats.record_retry_blocked()
                    raise CostLimitExceeded(
                        f"retry_blocked: 剩余预算 {remaining:.4f} 元，预估重试 {estimate:.4f} 元"
                    ) from None
                self.stats.record_retry()
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
            raise RuntimeError(f"计划重试耗尽：{last_error}")

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
