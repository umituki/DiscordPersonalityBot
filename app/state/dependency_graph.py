"""State layers and update ordering (spec 9.1, 9.5, 23.1).

Layers, from objective fact to slow disposition::

    0 Objective    world, time, action outcome, plan status
    1 Interpretive appraisal, intent hypothesis, user state estimate
    2 Immediate    emotion, mood, needs, curiosity, attachment activation
    3 Adaptive     relationship, beliefs, self schema, habits, interests, user model
    4 Deep         personality traits, values, attachment disposition, narrative identity

Two rules are enforced from here:

* **Commit ordering.** Accepted changes are applied shallow-to-deep, so a
  single run's history reads in the same order the pipeline ran (spec 9.5).
* **Deep-state gating.** Layer 4 domains change only through consolidation with
  accumulated evidence (spec 12.3, 34.2-5) — never as a direct consequence of
  one event. The numeric policy lives in ``config/policies``; the structural
  rule lives here.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

from app.state import ownership

OBJECTIVE: Final = 0
INTERPRETIVE: Final = 1
IMMEDIATE: Final = 2
ADAPTIVE: Final = 3
DEEP: Final = 4

_LAYERS: Mapping[str, int] = MappingProxyType(
    {
        "world": OBJECTIVE,
        "system": OBJECTIVE,
        "appraisal": INTERPRETIVE,
        "user_state_estimate": INTERPRETIVE,
        "emotion": IMMEDIATE,
        "mood": IMMEDIATE,
        "needs": IMMEDIATE,
        "relationship": ADAPTIVE,
        "attachment": ADAPTIVE,
        "beliefs": ADAPTIVE,
        "self_schema": ADAPTIVE,
        "user_model": ADAPTIVE,
        "user_model_counters": ADAPTIVE,
        "goals": ADAPTIVE,
        "habits": ADAPTIVE,
        "subjective_memory": ADAPTIVE,
        "personality": DEEP,
        "values": DEEP,
        "attachment_disposition": DEEP,
        "narrative_identity": DEEP,
    }
)

#: Modules allowed to propose Layer 4 changes at all. Membership is not enough:
#: the arbitration policy additionally requires accumulated evidence.
CONSOLIDATION_WRITERS: frozenset[str] = frozenset({"growth_engine", "value_engine"})


class LayerError(ValueError):
    """Raised when a domain has no declared layer."""


def layer_of(domain: str) -> int:
    layer = _LAYERS.get(domain)
    if layer is None:
        raise LayerError(f"no layer declared for state domain {domain!r}")
    return layer


def is_deep(domain: str) -> bool:
    return layer_of(domain) == DEEP


def domains_in_layer(layer: int) -> tuple[str, ...]:
    return tuple(sorted(domain for domain, value in _LAYERS.items() if value == layer))


def is_consolidation_writer(module: str) -> bool:
    return module in CONSOLIDATION_WRITERS


def commit_order_key(domain: str) -> tuple[int, str]:
    """Sort key applying shallow layers before deep ones."""
    return (layer_of(domain), domain)


def validate_registry() -> None:
    """Every owned domain must have a layer, and vice versa."""
    owned = set(ownership.known_domains())
    layered = set(_LAYERS)
    missing_layer = owned - layered
    if missing_layer:
        raise LayerError(f"owned domains without a layer: {sorted(missing_layer)}")
    unowned = layered - owned
    # Interpretive layer domains are per-run derivations, not committed state.
    unowned -= set(domains_in_layer(INTERPRETIVE))
    if unowned:
        raise LayerError(f"layered domains without an owner: {sorted(unowned)}")
