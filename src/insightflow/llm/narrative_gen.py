"""LLM narrative provider: model writes placeholder text; core validates/renders."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..narrative import NarrativeDraft
from .client import CostLimitExceeded, DeepSeekClient, LLMConfig
from .contracts import ContractError, loads_llm_json
from .payloads import mask_value
from .stats import LLMStats

NARRATIVE_PROMPT_VERSION = "narrative_v1"
PROMPT_FILE = Path(__file__).with_name("prompts") / "narrative_v1.md"


class LLMNarrativeProvider:
    name = "deepseek"
    prompt_version = NARRATIVE_PROMPT_VERSION

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Any | None = None,
        batch_budget_cny: float = 20.0,
        prompt_text: str | None = None,
        stats: LLMStats | None = None,
        max_structured_retries: int = 2,
        warn_prompt_tokens: int = 8000,
    ) -> None:
        self.config = config
        self.transport = transport
        self.batch_budget_cny = float(batch_budget_cny)
        self.prompt_text = prompt_text or PROMPT_FILE.read_text(encoding="utf-8")
        self.spent_cny = 0.0
        self.last_meta: dict[str, Any] = {}
        self.stats = stats or LLMStats()
        self.max_structured_retries = max(0, int(max_structured_retries))
        self.warn_prompt_tokens = int(warn_prompt_tokens)

    @staticmethod
    def _parse(content: str) -> NarrativeDraft:
        try:
            data = loads_llm_json(content)
        except json.JSONDecodeError as exc:
            raise ContractError(f"叙事不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ContractError("叙事顶层必须是对象")
        title = data.get("title")
        summary = data.get("summary")
        key_points = data.get("key_points") or []
        chart_notes = data.get("chart_notes") or {}
        if not isinstance(title, str) or not isinstance(summary, str):
            raise ContractError("title/summary 必须是字符串")
        if not isinstance(key_points, list) or not all(isinstance(item, str) for item in key_points):
            raise ContractError("key_points 必须是字符串数组")
        if not isinstance(chart_notes, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in chart_notes.items()
        ):
            raise ContractError("chart_notes 必须是字符串到字符串的对象")
        return NarrativeDraft(title=title, summary=summary, key_points=key_points, chart_notes=chart_notes)

    def generate(
        self,
        fact_keys: list[str],
        field_labels: list[str],
        chart_ids: list[str],
        samples: list[dict[str, Any]],
    ) -> NarrativeDraft:
        if self.spent_cny >= self.batch_budget_cny:
            raise CostLimitExceeded(f"整批预算已用尽（{self.spent_cny:.2f} 元），叙事降级")

        payload = {
            "placeholders": list(fact_keys),
            "fields": list(field_labels),
            "chart_ids": list(chart_ids),
            "samples": [
                {str(column): mask_value(row.get(column), str(column)) for column in row}
                for row in samples
            ],
        }
        messages = [
            {"role": "system", "content": self.prompt_text},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        client = DeepSeekClient(self.config, transport=self.transport)
        started = time.perf_counter()
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
                draft = self._parse(content)
            except ContractError as exc:
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
                            f"上次叙事未通过校验：{exc}。只输出一个 JSON 对象，且必须包含字符串字段 "
                            "title、summary、key_points 数组、chart_notes 对象；不要输出其他内容。"
                        ),
                    }
                ]
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            cost = client.meter.total_cny
            self.spent_cny += cost
            self.last_meta = {
                "source_kind": "llm",
                "provider": self.name,
                "prompt_version": self.prompt_version,
                "cost_cny": cost,
                "latency_ms": latency_ms,
                "usage": usage,
            }
            if self.spent_cny >= self.batch_budget_cny:
                raise CostLimitExceeded(f"整批预算已用尽（{self.spent_cny:.2f} 元）")
            return draft

        raise ContractError("叙事重试耗尽")
