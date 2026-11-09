"""LLM PoC package (isolated): payloads → client → contract → evaluation.

Nothing in the core pipeline imports this package; it is a standalone bypass
used to measure whether an LLM can plan analysis and chart skeletons safely.
"""

from __future__ import annotations

from .client import DeepSeekClient, LLMConfig, LLMConfigError, LLMRequestError, CostLimitExceeded
from .contracts import ContractError, LLMResult, parse_result
from .payloads import build_payload, dump_payload, mask_value

__all__ = [
    "CostLimitExceeded",
    "ContractError",
    "DeepSeekClient",
    "LLMConfig",
    "LLMConfigError",
    "LLMRequestError",
    "LLMResult",
    "build_payload",
    "dump_payload",
    "mask_value",
    "parse_result",
]
