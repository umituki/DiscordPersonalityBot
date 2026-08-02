"""Relationship and attachment tuning values (spec 40)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class RelationshipPolicyError(RuntimeError):
    """Raised when the relationship policy cannot be loaded."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FamiliarityRule(_Frozen):
    contact_gain: float = 0.010
    max_per_event: float = 0.015
    ceiling_pull: float = Field(default=0.6, ge=0.0, le=1.0)


class TrustRule(_Frozen):
    evidence_gain: float = 0.012
    max_per_event: float = 0.02
    #: Spec 13.1: contact volume alone never raises trust.
    requires_evidence: bool = True
    ceiling_pull: float = Field(default=0.75, ge=0.0, le=1.0)


class ClosenessRule(_Frozen):
    disclosure_gain: float = 0.015
    positive_affect_gain: float = 0.008
    max_per_event: float = 0.025


class SecurityRule(_Frozen):
    consistency_gain: float = 0.008
    unresolved_conflict_penalty: float = 0.020
    max_per_event: float = 0.02


class RespectRule(_Frozen):
    competence_gain: float = 0.010
    max_per_event: float = 0.015


class ConflictRule(_Frozen):
    #: Spec 13.3: these are different things, not degrees of one thing.
    disagreement: float = 0.03
    conflict: float = 0.12
    transgression: float = 0.28
    betrayal: float = 0.50
    natural_decay_per_day: float = 0.02
    max_per_event: float = 0.5


class ExpectationRule(_Frozen):
    adjust_rate: float = 0.05
    max_per_event: float = 0.05


class Dimensions(_Frozen):
    familiarity: FamiliarityRule = FamiliarityRule()
    trust: TrustRule = TrustRule()
    emotional_closeness: ClosenessRule = ClosenessRule()
    security: SecurityRule = SecurityRule()
    respect: RespectRule = RespectRule()
    conflict_residue: ConflictRule = ConflictRule()
    expectation: ExpectationRule = ExpectationRule()


class ApologyEffect(_Frozen):
    conflict_residue_relief: float = 0.25
    emotion_recovery: float = 0.40
    #: Spec 34.2-7: an apology on its own restores no trust at all.
    trust_restoration: float = 0.0


class ConsistentBehaviour(_Frozen):
    trust_restoration_per_event: float = 0.01
    required_events: int = Field(default=5, ge=1)


class RepairPolicy(_Frozen):
    apology: ApologyEffect = ApologyEffect()
    consistent_behaviour: ConsistentBehaviour = ConsistentBehaviour()
    severe_damage_threshold: float = 0.25
    unresolved_hurt_floor: float = 0.05


class AttachmentPolicy(_Frozen):
    baseline_felt_security: float = 0.5
    activation_from_conflict: float = 0.30
    activation_from_separation_per_day: float = 0.05
    activation_decay_per_hour: float = 0.08
    felt_security_gain_per_positive: float = 0.010
    felt_security_loss_per_conflict: float = 0.040
    max_per_event: float = Field(default=0.10, ge=0.0, le=1.0)
    proximity_desire_from_activation: float = 0.50
    reassurance_need_from_insecurity: float = 0.60
    withdrawal_from_unresolved: float = 0.35
    #: Spec 13.2: more security means more tolerance for separation, not less.
    separation_tolerance_from_security: float = 0.70


class RelationshipPolicy(_Frozen):
    policy_version: int = 1
    dimensions: Dimensions = Dimensions()
    repair: RepairPolicy = RepairPolicy()
    attachment: AttachmentPolicy = AttachmentPolicy()

    @classmethod
    def load(cls, path: Path | str) -> RelationshipPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise RelationshipPolicyError(f"relationship policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw: Any = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise RelationshipPolicyError(
                f"relationship policy must be a mapping: {policy_path}"
            )
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise RelationshipPolicyError(
                f"invalid relationship policy {policy_path}: {exc}"
            ) from exc
