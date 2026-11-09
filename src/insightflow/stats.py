"""Neutral per-run LLM statistics (core module; no network/llm imports).

Providers keep their own counters; the pipeline merges them into
``run_record.metadata.llm_stats`` so retry/fallback rates are auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMStats:
    calls: int = 0
    retries: int = 0
    json_failures: int = 0
    fallbacks: int = 0
    retry_blocked: int = 0
    cost_cny: float = 0.0
    max_prompt_tokens: int = 0
    prompt_warn: bool = False
    warnings: list[str] = field(default_factory=list)

    def record_call(self) -> None:
        self.calls += 1

    def record_json_failure(self) -> None:
        self.json_failures += 1

    def record_retry(self) -> None:
        self.retries += 1

    def record_retry_blocked(self) -> None:
        self.retry_blocked += 1

    def record_fallback(self) -> None:
        self.fallbacks += 1

    def add_usage(self, prompt_tokens: int, cost_cny: float, warn_tokens: int = 8000) -> None:
        self.cost_cny += float(cost_cny or 0.0)
        self.max_prompt_tokens = max(self.max_prompt_tokens, int(prompt_tokens or 0))
        if warn_tokens and int(prompt_tokens or 0) > int(warn_tokens):
            self.prompt_warn = True

    def warn(self, message: str) -> None:
        self.warnings.append(str(message)[:300])

    def merge(self, other: "LLMStats | None") -> None:
        if other is None:
            return
        self.calls += other.calls
        self.retries += other.retries
        self.json_failures += other.json_failures
        self.fallbacks += other.fallbacks
        self.retry_blocked += other.retry_blocked
        self.cost_cny += other.cost_cny
        self.max_prompt_tokens = max(self.max_prompt_tokens, other.max_prompt_tokens)
        self.prompt_warn = self.prompt_warn or other.prompt_warn
        self.warnings.extend(other.warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "json_failures": self.json_failures,
            "fallbacks": self.fallbacks,
            "retry_blocked": self.retry_blocked,
            "cost_cny": round(self.cost_cny, 6),
            "max_prompt_tokens": self.max_prompt_tokens,
            "prompt_warn": self.prompt_warn,
            "warnings": list(self.warnings),
        }
