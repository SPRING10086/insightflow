"""The single assembly point allowed to reference ``insightflow.llm``.

Everything else in ``insightflow`` (including ``pipeline.py``) receives a
``SemanticProvider`` instance by dependency injection; this keeps the compute
layer free of network code while still allowing a controlled opt-in.

The isolation test whitelists exactly this file.
"""

from __future__ import annotations

from typing import Any, Mapping

from .semantic import SemanticProvider


def build_semantic_provider(
    kind: str | None = "none",
    *,
    env: Mapping[str, str] | None = None,
    transport: Any | None = None,
    batch_budget_cny: float = 20.0,
) -> SemanticProvider | None:
    """Build a provider from a CLI/config token; ``none`` disables the bypass."""
    token = (kind or "none").strip().lower()
    if token in {"", "none", "off", "rules"}:
        return None
    if token != "llm":
        raise ValueError(f"未知的语义提供者：{kind!r}（可选 none/llm）")

    # Imported lazily and only here: the one controlled reference to the LLM package.
    from .llm.adapter import LLMSemanticProvider
    from .llm.client import LLMConfig

    return LLMSemanticProvider(
        LLMConfig.from_env(env),
        transport=transport,
        batch_budget_cny=batch_budget_cny,
    )
