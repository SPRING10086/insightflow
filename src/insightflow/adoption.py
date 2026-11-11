"""Stage 6a: adoption-mode resolution for the four LLM capabilities.

Pure core module: no network, no pandas, no llm import.  It answers one
question per run — for each capability, should the report consume the LLM
result or the rule result?  Stage 6a only records the decision; actual adoption
lands in 6b, so the default (all shadow) keeps report output unchanged.

Rules:
* ``force_rules`` (config or ``INSIGHTFLOW_FORCE_RULES=1``) forces every
  capability to shadow;
* a capability without ``enabled`` / its feature switch is forced to shadow;
* ``plan`` requires ``semantic`` to be non-shadow; ``execute`` requires
  ``plan`` to be non-shadow (prevents undefined mixes such as "LLM plan with
  rule semantics").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

MODES = ("shadow", "gated", "primary")
FEATURES = ("semantic", "plan", "execute", "narrative")
TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AdoptionDecision:
    modes: dict[str, str]
    reasons: dict[str, str] = field(default_factory=dict)
    force_rules: bool = False

    @classmethod
    def shadow(cls) -> "AdoptionDecision":
        return cls(modes={name: "shadow" for name in FEATURES}, reasons={}, force_rules=False)

    def mode(self, name: str) -> str:
        if name not in FEATURES:
            raise ValueError(f"unknown capability: {name!r}")
        return self.modes.get(name, "shadow")

    def any_non_shadow(self) -> bool:
        return any(mode != "shadow" for mode in self.modes.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "force_rules": self.force_rules,
            "final_mode": dict(self.modes),
            "downgrade_reason": dict(self.reasons),
        }


def _env_force(env: Mapping[str, str] | None) -> bool | None:
    """Return the env override when explicitly set, else ``None``.

    Environment wins over config so operations can force rules without editing
    the file; an explicit ``0``/``false`` disables a config-level force too.
    """
    source = os.environ if env is None else env
    raw = source.get("INSIGHTFLOW_FORCE_RULES")
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() in TRUTHY


def force_rules_active(llm: Any, env: Mapping[str, str] | None = None) -> bool:
    """True when config or env requests the global rule-only kill switch."""
    env_force = _env_force(env)
    return bool(env_force if env_force is not None else getattr(llm, "force_rules", False))


def resolve_adoption(llm: Any, env: Mapping[str, str] | None = None) -> AdoptionDecision:
    """Resolve configured adoption modes into this run's effective decision."""
    force_rules = force_rules_active(llm, env)
    configured = getattr(llm, "adoption", None) or {}
    modes: dict[str, str] = {}
    reasons: dict[str, str] = {}

    for name in FEATURES:
        if force_rules:
            modes[name] = "shadow"
            reasons[name] = "force_rules"
        elif not getattr(llm, "enabled", False):
            modes[name] = "shadow"
            reasons[name] = "llm_disabled"
        elif not getattr(llm, name, False):
            modes[name] = "shadow"
            reasons[name] = "feature_off"
        else:
            modes[name] = str(configured.get(name, "shadow"))

    if modes["plan"] != "shadow" and modes["semantic"] == "shadow":
        modes["plan"] = "shadow"
        reasons["plan"] = "upstream_semantic_shadow"
    if modes["execute"] != "shadow" and modes["plan"] == "shadow":
        modes["execute"] = "shadow"
        reasons["execute"] = "upstream_plan_shadow"

    return AdoptionDecision(modes=modes, reasons=reasons, force_rules=force_rules)
