"""Single-writer registry (spec 9.3).

Every dynamic state domain has exactly one authoritative writer. Any other
module that wants an effect emits evidence or a proposal; the arbitrator then
rejects it here if the source module is not the owner.

Only Phase 1 infrastructure and the domains named in spec 9.3 are registered.
Engines are added by the phase that implements them.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

#: Writers that may exist before their engine phase: the arbitration and
#: bootstrap paths themselves.
SYSTEM_WRITER = "system_bootstrap"
ADMIN_WRITER = "admin_control_plane"


@dataclass(frozen=True, slots=True)
class DomainOwnership:
    domain: str
    writer: str
    #: The phase that introduces the real engine, for traceability.
    spec_reference: str


_OWNERSHIP: Mapping[str, DomainOwnership] = MappingProxyType(
    {
        entry.domain: entry
        for entry in (
            DomainOwnership("world", "world_service", "spec 9.3 / 18.1"),
            DomainOwnership("emotion", "emotion_engine", "spec 9.3 / 11.2"),
            DomainOwnership("mood", "mood_engine", "spec 9.3 / 11.3"),
            DomainOwnership("needs", "need_engine", "spec 9.3 / 15.1"),
            DomainOwnership("relationship", "relationship_engine", "spec 9.3 / 13.1"),
            DomainOwnership("attachment", "attachment_engine", "spec 9.3 / 13.2"),
            DomainOwnership("beliefs", "belief_engine", "spec 9.3 / 12"),
            DomainOwnership("self_schema", "self_engine", "spec 9.3 / 12.4"),
            DomainOwnership("user_model", "social_cognition_engine", "spec 9.3 / 14"),
            # Counters (observations, evidence tallies) are bookkeeping, not
            # psychological magnitudes: they are unbounded and belong in their
            # own domain rather than distorting the 0..1 model space.
            DomainOwnership(
                "user_model_counters", "social_cognition_engine", "spec 14 bookkeeping"
            ),
            DomainOwnership("goals", "goal_engine", "spec 9.3 / 15.2"),
            DomainOwnership("habits", "habit_engine", "spec 9.3 / 15.3"),
            DomainOwnership("personality", "growth_engine", "spec 9.3 / 12.1"),
            DomainOwnership("values", "value_engine", "spec 9.3 / 12.5"),
            DomainOwnership("subjective_memory", "memory_engine", "spec 9.3 / 10"),
            # Deep dispositions consolidated by the growth pipeline (spec 23.1).
            DomainOwnership("attachment_disposition", "growth_engine", "spec 13.2 / 23.1"),
            DomainOwnership("narrative_identity", "growth_engine", "spec 12.4 / 23.1"),
            # Infrastructure-owned domains.
            DomainOwnership("system", SYSTEM_WRITER, "spec 29"),
        )
    }
)


class OwnershipError(PermissionError):
    """Raised when a module attempts to write state it does not own."""


def known_domains() -> tuple[str, ...]:
    return tuple(sorted(_OWNERSHIP))


def is_known_domain(domain: str) -> bool:
    return domain in _OWNERSHIP


def owner_of(domain: str) -> str:
    entry = _OWNERSHIP.get(domain)
    if entry is None:
        raise OwnershipError(f"unknown state domain: {domain!r}")
    return entry.writer

def ownership_of(domain: str) -> DomainOwnership:
    entry = _OWNERSHIP.get(domain)
    if entry is None:
        raise OwnershipError(f"unknown state domain: {domain!r}")
    return entry


def may_write(domain: str, module: str) -> bool:
    """``True`` only for the single registered writer of ``domain``."""
    entry = _OWNERSHIP.get(domain)
    if entry is None:
        return False
    if module == ADMIN_WRITER:
        # Admin operations are a separate, audited control plane (spec 30).
        # They still go through arbitration and commit, never a direct write.
        return True
    return entry.writer == module


def assert_may_write(domain: str, module: str) -> None:
    if not may_write(domain, module):
        raise OwnershipError(
            f"{module!r} may not write {domain!r}; owner is {owner_of(domain)!r}. "
            "Emit a StateChangeProposal instead (spec 9.3)."
        )
