"""The single assembly point allowed to reference ``insightflow.llm``.

Everything else in ``insightflow`` (including ``pipeline.py``) receives provider
instances by dependency injection; this keeps the compute layer free of network
code while still allowing a controlled opt-in.

The isolation test whitelists exactly this file.
"""

from __future__ import annotations

from typing import Any, Mapping

from .planning import PlanProvider
from .semantic import SemanticProvider


def _token(kind: str | None, label: str) -> str:
    token = (kind or "none").strip().lower()
    if token in {"", "none", "off", "rules"}:
        return ""
    if token != "llm":
        raise ValueError(f"未知的{label}提供者：{kind!r}（可选 none/llm）")
    return token


def _llm_config(env: Mapping[str, str] | None, request_timeout_seconds: float | None, transport_retries: int | None):
    from .llm.client import LLMConfig

    config = LLMConfig.from_env(env)
    if request_timeout_seconds is not None:
        config.timeout_seconds = float(request_timeout_seconds)
    if transport_retries is not None:
        config.max_retries = max(0, int(transport_retries))
    return config


def build_semantic_provider(
    kind: str | None = "none",
    *,
    env: Mapping[str, str] | None = None,
    transport: Any | None = None,
    batch_budget_cny: float = 20.0,
    request_timeout_seconds: float | None = None,
    transport_retries: int | None = None,
    max_structured_retries: int | None = None,
    warn_prompt_tokens: int | None = None,
) -> SemanticProvider | None:
    """Build a stage-1 semantic provider; ``none`` disables the bypass."""
    if not _token(kind, "语义"):
        return None

    # Imported lazily and only here: the one controlled reference to the LLM package.
    from .llm.adapter import LLMSemanticProvider

    return LLMSemanticProvider(
        _llm_config(env, request_timeout_seconds, transport_retries),
        transport=transport,
        batch_budget_cny=batch_budget_cny,
        max_structured_retries=2 if max_structured_retries is None else int(max_structured_retries),
        warn_prompt_tokens=8000 if warn_prompt_tokens is None else int(warn_prompt_tokens),
    )


def build_execution_provider(
    kind: str | None = "none",
    *,
    env: Mapping[str, str] | None = None,
    transport: Any | None = None,
    batch_budget_cny: float = 20.0,
    timeout_seconds: float = 10.0,
    memory_mb: int = 512,
    request_timeout_seconds: float | None = None,
    transport_retries: int | None = None,
    max_structured_retries: int | None = None,
    warn_prompt_tokens: int | None = None,
) -> Any | None:
    """Build a stage-3 execution provider; ``none`` disables controlled execution."""
    if not _token(kind, "执行"):
        return None

    from .llm.executor import LLMExecutionProvider

    return LLMExecutionProvider(
        _llm_config(env, request_timeout_seconds, transport_retries),
        transport=transport,
        batch_budget_cny=batch_budget_cny,
        timeout_seconds=timeout_seconds,
        memory_mb=memory_mb,
        warn_prompt_tokens=8000 if warn_prompt_tokens is None else int(warn_prompt_tokens),
    )


def build_narrative_provider(
    kind: str | None = "none",
    *,
    env: Mapping[str, str] | None = None,
    transport: Any | None = None,
    batch_budget_cny: float = 20.0,
    request_timeout_seconds: float | None = None,
    transport_retries: int | None = None,
    max_structured_retries: int | None = None,
    warn_prompt_tokens: int | None = None,
) -> Any | None:
    """Build a stage-4 narrative provider; ``none`` keeps the rule insights only."""
    if not _token(kind, "叙事"):
        return None

    from .llm.narrative_gen import LLMNarrativeProvider

    return LLMNarrativeProvider(
        _llm_config(env, request_timeout_seconds, transport_retries),
        transport=transport,
        batch_budget_cny=batch_budget_cny,
        max_structured_retries=2 if max_structured_retries is None else int(max_structured_retries),
        warn_prompt_tokens=8000 if warn_prompt_tokens is None else int(warn_prompt_tokens),
    )


def build_plan_provider(
    kind: str | None = "none",
    *,
    env: Mapping[str, str] | None = None,
    transport: Any | None = None,
    batch_budget_cny: float = 20.0,
    request_timeout_seconds: float | None = None,
    transport_retries: int | None = None,
    max_structured_retries: int | None = None,
    warn_prompt_tokens: int | None = None,
) -> PlanProvider | None:
    """Build a stage-2 plan provider; ``none`` keeps the rule plan only."""
    if not _token(kind, "计划"):
        return None

    from .llm.plan_adapter import LLMPlanProvider

    return LLMPlanProvider(
        _llm_config(env, request_timeout_seconds, transport_retries),
        transport=transport,
        batch_budget_cny=batch_budget_cny,
        max_structured_retries=2 if max_structured_retries is None else int(max_structured_retries),
        warn_prompt_tokens=8000 if warn_prompt_tokens is None else int(warn_prompt_tokens),
    )
