"""LLM adapter: implements the neutral ``SemanticProvider`` using the PoC stack.

This module lives inside ``llm/`` so the compute layer never imports it.  The
only file allowed to reference it is ``insightflow/semantic_assembly.py``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..semantic import FieldSemantics, ProviderTableResult, TableSample
from .client import CostLimitExceeded, DeepSeekClient, LLMConfig
from .contracts import PROMPT_VERSION, ContractError, parse_result
from .payloads import build_payload

PROMPT_FILE = Path(__file__).with_name("prompts") / "analysis_plan_v3.md"


class CoverageError(Exception):
    """The answer did not cover exactly the input fields."""

    def __init__(self, missing: list[str], extra: list[str]) -> None:
        parts = []
        if missing:
            parts.append("遗漏字段：" + "、".join(missing))
        if extra:
            parts.append("多余字段：" + "、".join(extra))
        super().__init__("；".join(parts) or "字段覆盖不完整")


class LLMSemanticProvider:
    """Semantic schema provider backed by the DeepSeek chat API."""

    name = "deepseek"

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Any | None = None,
        batch_budget_cny: float = 20.0,
        prompt_version: str = PROMPT_VERSION,
        prompt_text: str | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.batch_budget_cny = float(batch_budget_cny)
        self.prompt_version = prompt_version
        self.prompt_text = prompt_text or PROMPT_FILE.read_text(encoding="utf-8")
        self.spent_cny = 0.0
        self.calls = 0

    def describe(self, sample: TableSample) -> ProviderTableResult:
        if self.spent_cny >= self.batch_budget_cny:
            raise CostLimitExceeded(
                f"整批预算已用尽（{self.spent_cny:.2f} / {self.batch_budget_cny:.2f} 元），停止调用并降级"
            )

        payload = build_payload(
            table_name=sample.name,
            source=sample.source,
            field_names=sample.field_names,
            rows=sample.rows,
        )
        messages = [
            {"role": "system", "content": self.prompt_text},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        # A fresh client per table keeps the 5 CNY per-table guard honest.
        client = DeepSeekClient(self.config, transport=self.transport)
        started = time.perf_counter()

        result = None
        last_error = ""
        for attempt in range(3):
            content, usage = client.complete(messages)
            try:
                parsed = parse_result(content, usage)
                expected = set(sample.field_names)
                actual = {field.name for field in parsed.fields}
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                if missing or extra:
                    raise CoverageError(missing, extra)
            except (ContractError, CoverageError) as exc:
                last_error = str(exc)
                if attempt == 2:
                    raise
                messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            f"上次输出未通过校验：{exc}。请严格为输入中的每个字段输出且仅输出一条记录，"
                            "字段名逐字一致；JSON 必须完整闭合（大括号成对），字符串内不要有未转义引号或换行；"
                            "只输出 JSON 对象。"
                        ),
                    }
                ]
                continue
            result = parsed
            break
        latency_ms = int((time.perf_counter() - started) * 1000)
        if result is None:
            raise RuntimeError(f"语义输出两次均未通过校验：{last_error}")

        table_cost = client.meter.total_cny
        self.spent_cny += table_cost
        self.calls += 1
        if self.spent_cny >= self.batch_budget_cny:
            raise CostLimitExceeded(
                f"整批预算已用尽（{self.spent_cny:.2f} / {self.batch_budget_cny:.2f} 元）"
            )

        fields = [
            FieldSemantics(
                name=item.name,
                role=item.role,
                unit=item.unit,
                meaning=item.meaning,
                confidence=item.confidence,
                source="llm",
                relations=list(item.relations),
            )
            for item in result.fields
        ]
        return ProviderTableResult(
            fields=fields,
            prompt_version=self.prompt_version,
            cost_cny=table_cost,
            latency_ms=latency_ms,
            source_kind="llm",
            anomalies=list(result.plan.anomalies),
        )
