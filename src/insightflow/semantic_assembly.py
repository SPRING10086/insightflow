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


def build_semantic_provider(
    kind: str | None = "none",
    *,
    env: Mapping[str, str] | None = None,
    transport: Any | None = None,
    batch_budget_cny: float = 20.0,
) -> SemanticProvider | None:
    """Build a stage-1 semantic provider; ``none`` disables the bypass."""
    if not _token(kind, "语义"):
        return None

    # Imported lazily and only here: the one controlled reference to the LLM package.
    from .llm.adapter import LLMSemanticProvider
    from .llm.client import LLMConfig

    return LLMSemanticProvider(
        LLMConfig.from_env(env),
        transport=transport,
        batch_budget_cny=batch_budget_cny,
    )


def build_plan_provider(
    kind: str | None = "none",
    *,
    env: Mapping[str, str] | None = None,
    transport: Any | None = None,
    batch_budget_cny: float = 20.0,
) -> PlanProvider | None:
    """Build a stage-2 plan provider; ``none`` keeps the rule plan only."""
    if not _token(kind, "计划"):
        return None

    from .llm.client import LLMConfig
    from .llm.plan_adapter import LLMPlanProvider

    return LLMPlanProvider(
        LLMConfig.from_env(env),
        transport=transport,
        batch_budget_cny=batch_budget_cny,
    )
