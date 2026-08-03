"""Capability Contracts (rebuild spec 4.3).

    クラスが存在すること、Bootstrap で instantiate されていること、
    Unit Test が通ることを「実装済み」と呼んではならない。

The previous build shipped Activity, Goal, Habit, NPC, Proactive and Scheduler
as classes that were constructed at startup and never once driven. A Capability
Contract is the thing that makes that visible: for each autonomous capability it
names the trigger, the runtime entry point, the hard gate, the action that
actually happens, the event it produces, where it is persisted, how it recovers,
how to see it, and the test that proves it end to end.

Loading them is not decoration. The contracts are validated on every test run,
and the status field is the only place a capability may claim to be done:

    NOT_STARTED   nothing yet
    CODE_ONLY     the code exists and nothing drives it
    WIRED         a real trigger reaches it
    E2E_VERIFIED  a test drives the whole path and checks the rows it wrote

``DONE`` is deliberately not a value (4.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MODULE = "capability_contracts"

DEFAULT_DIRECTORY = Path("config/capabilities")

#: Rebuild spec 4.2. Ordered from least to most complete.
STATUSES: tuple[str, ...] = ("NOT_STARTED", "CODE_ONLY", "WIRED", "E2E_VERIFIED")

#: Rebuild spec 4.3 lists exactly these. A contract missing from the directory
#: is a capability nobody has thought about yet, which is itself the finding.
REQUIRED_CAPABILITIES: tuple[str, ...] = (
    "normal_reply",
    # Rebuild spec Phase 3 §45.
    "natural_conversation_realization",
    "intentional_silence",
    "activity",
    "sleep",
    "diary",
    "spontaneous_memory",
    "npc_interaction",
    "group_activity",
    "goal_action",
    "habit_action",
    "proactive_contact",
    "web_search",
    "genesis",
    # Rebuild spec 30, Phase 5. Two contracts, not one: reading must change
    # nothing, and `backup` writes a file.
    "admin_debug_readonly",
    "admin_backup",
)

#: Every field 4.3's example carries. All of them are required: a contract with
#: no ``actual_action`` or no ``success_event`` is exactly the shape of a
#: subsystem that exists and never runs.
REQUIRED_FIELDS: tuple[str, ...] = (
    "capability",
    "trigger",
    "runtime_entry",
    "candidate_source",
    "hard_gate",
    "actual_action",
    "success_event",
    "persistence",
    "recovery",
    "observability",
    "acceptance_test",
    "status",
)


class CapabilityContractError(RuntimeError):
    """Raised when a contract is missing or malformed."""


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    capability: str
    trigger: str
    runtime_entry: str
    candidate_source: str
    hard_gate: str
    actual_action: str
    success_event: str
    persistence: str
    recovery: str
    observability: str
    acceptance_test: str
    status: str
    note: str = ""

    @property
    def complete(self) -> bool:
        """Only an end-to-end proof counts (rebuild spec 4.2, 4.4)."""
        return self.status == "E2E_VERIFIED"

    @property
    def driven(self) -> bool:
        """Whether a real trigger reaches it at all."""
        return self.status in ("WIRED", "E2E_VERIFIED")


def load_contracts(
    directory: Path | str = DEFAULT_DIRECTORY,
) -> dict[str, CapabilityContract]:
    """Read every contract, refusing anything malformed."""
    root = Path(directory)
    if not root.is_dir():
        raise CapabilityContractError(f"capability contracts not found: {root}")

    contracts: dict[str, CapabilityContract] = {}
    for path in sorted(root.glob("*.y*ml")):
        contract = _read(path)
        if contract.capability in contracts:
            raise CapabilityContractError(f"duplicate capability: {contract.capability}")
        contracts[contract.capability] = contract

    missing = [name for name in REQUIRED_CAPABILITIES if name not in contracts]
    if missing:
        raise CapabilityContractError(
            f"no capability contract for: {', '.join(missing)} (rebuild spec 4.3)"
        )
    return contracts


def _read(path: Path) -> CapabilityContract:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise CapabilityContractError(f"capability contract must be a mapping: {path}")

    missing = [field for field in REQUIRED_FIELDS if not str(raw.get(field, "")).strip()]
    if missing:
        raise CapabilityContractError(
            f"{path.name} is missing: {', '.join(missing)}"
        )
    status = str(raw["status"])
    if status not in STATUSES:
        raise CapabilityContractError(
            f"{path.name} has status {status!r}; expected one of {list(STATUSES)}"
        )
    return CapabilityContract(
        **{field: str(raw[field]) for field in REQUIRED_FIELDS},
        note=str(raw.get("note", "")),
    )


def summary(contracts: dict[str, CapabilityContract]) -> dict[str, int]:
    """How many capabilities sit at each status."""
    counts = {status: 0 for status in STATUSES}
    for contract in contracts.values():
        counts[contract.status] += 1
    return counts


__all__ = [
    "CapabilityContract",
    "CapabilityContractError",
    "DEFAULT_DIRECTORY",
    "MODULE",
    "REQUIRED_CAPABILITIES",
    "REQUIRED_FIELDS",
    "STATUSES",
    "load_contracts",
    "summary",
]
