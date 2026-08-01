"""State change proposals (spec 9.4).

A subsystem never writes another subsystem's state. It emits a proposal, and
the arbitrator decides whether it may be committed.

Spec 9.4: ``最終的な数値変化量は、可能な限り Python policy が決める``. A proposal
carries an intent (``magnitude``) and reason codes; the accepted magnitude is
bounded by ``config/policies/state_arbitration.yaml``, not by the proposer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import ids
from app.clock import Clock, SystemClock, ensure_aware
from app.state.value import JSONValue

Operation = Literal["set", "adjust", "invalidate"]
ProposalPriority = Literal["low", "normal", "high"]


class StateChangeProposal(BaseModel):
    """A request to change one key of one state domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(default_factory=lambda: ids.new_id(ids.PROPOSAL))
    source_event_id: str
    source_module: str
    target_domain: str
    target_key: str
    operation: Operation
    #: For ``set``: the new value. Ignored for ``adjust``/``invalidate``.
    value: JSONValue = None
    #: For ``adjust``: the requested signed delta.
    magnitude: float | None = None
    #: The proposer's own confidence. Never authoritative on its own (spec 24).
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    priority: ProposalPriority = "normal"
    created_at: datetime = Field(default_factory=lambda: SystemClock().now())

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @field_validator("target_domain", "target_key", "source_module")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("must be a non-empty, untrimmed-free identifier")
        return value

    @model_validator(mode="after")
    def _operation_shape(self) -> Self:
        if self.operation == "adjust":
            if self.magnitude is None:
                raise ValueError("an 'adjust' proposal requires a magnitude")
            if self.value is not None:
                raise ValueError("an 'adjust' proposal must not carry an absolute value")
        if self.operation == "set" and self.magnitude is not None:
            raise ValueError("a 'set' proposal must not carry a magnitude")
        return self

    @property
    def target(self) -> str:
        return f"{self.target_domain}.{self.target_key}"

    @classmethod
    def adjust(
        cls,
        *,
        source_event_id: str,
        source_module: str,
        target_domain: str,
        target_key: str,
        magnitude: float,
        reason_codes: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
        confidence: float | None = None,
        priority: ProposalPriority = "normal",
        clock: Clock | None = None,
    ) -> StateChangeProposal:
        return cls(
            source_event_id=source_event_id,
            source_module=source_module,
            target_domain=target_domain,
            target_key=target_key,
            operation="adjust",
            magnitude=magnitude,
            reason_codes=reason_codes,
            evidence_ids=evidence_ids,
            confidence=confidence,
            priority=priority,
            created_at=(clock or SystemClock()).now(),
        )

    @classmethod
    def set_value(
        cls,
        *,
        source_event_id: str,
        source_module: str,
        target_domain: str,
        target_key: str,
        value: JSONValue,
        reason_codes: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
        confidence: float | None = None,
        priority: ProposalPriority = "normal",
        clock: Clock | None = None,
    ) -> StateChangeProposal:
        return cls(
            source_event_id=source_event_id,
            source_module=source_module,
            target_domain=target_domain,
            target_key=target_key,
            operation="set",
            value=value,
            reason_codes=reason_codes,
            evidence_ids=evidence_ids,
            confidence=confidence,
            priority=priority,
            created_at=(clock or SystemClock()).now(),
        )
