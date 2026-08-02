"""Admin control plane models (spec 30).

    Character Mode と Admin Mode を完全分離する。
    通常会話の「忘れて」は DB hard delete を意味しない。
    YUI 自身に Admin Tool 権限を与えない。

The risk class is not documentation — it selects the procedure. A SAFE
operation runs; a MUTATING one is recorded; a DESTRUCTIVE one has to walk the
whole path of spec 30 before it may touch anything.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

#: Spec 30 memory operations, least to most final.
MemoryOperation = Literal["suppress", "invalidate", "redact", "hard_delete"]

#: Spec 30 risk classes.
RiskClass = Literal["SAFE", "MUTATING", "DESTRUCTIVE"]

#: Spec 30's destructive sequence, in order.
Stage = Literal[
    "requested",
    "impact_analysed",
    "previewed",
    "confirmed",
    "snapshotted",
    "mutated",
    "cascaded",
    "validated",
    "audited",
    "refused",
]

DESTRUCTIVE_STAGES: tuple[Stage, ...] = (
    "requested",
    "impact_analysed",
    "previewed",
    "confirmed",
    "snapshotted",
    "mutated",
    "cascaded",
    "validated",
    "audited",
)

#: Which class each operation falls into. ``hard_delete`` is the only one that
#: destroys anything, and forgetting in conversation reaches none of these.
OPERATION_RISK: dict[str, RiskClass] = {
    "suppress": "MUTATING",
    "invalidate": "MUTATING",
    "redact": "DESTRUCTIVE",
    "hard_delete": "DESTRUCTIVE",
    "inspect": "SAFE",
    "export": "SAFE",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class ImpactAnalysis(_Frozen):
    """What this would touch, produced before anything is touched."""

    target_type: str
    target_id: str | None = None
    directly_affected: int = 0
    #: Rows that reference the target and would need re-evaluating afterwards.
    cascade_affected: int = 0
    reversible: bool = True
    notes: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.directly_affected + self.cascade_affected


class AdminAction(_Frozen):
    """One recorded admin operation, whether or not it was carried out."""

    action_id: str
    operation: str
    risk_class: RiskClass
    target_type: str
    target_id: str | None = None
    stage: Stage = "requested"
    dry_run: bool = True
    confirmed_by: str | None = None
    snapshot_path: str | None = None
    impact: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    requested_at: datetime
    completed_at: datetime | None = None

    @property
    def carried_out(self) -> bool:
        return self.stage == "audited" and not self.dry_run

    @property
    def refused(self) -> bool:
        return self.stage == "refused"
