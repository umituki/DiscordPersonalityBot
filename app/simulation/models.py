"""Past simulation domain models (spec 22).

The shape of these types is the argument of spec 22.1:

    少数のユーザー希望から完成人格を直接作らず、Temperamental Seed と仮想人生経験を
    同じ通常心理 Pipeline に通し、現在人格を形成する。

So ``TemperamentSeed`` has somewhere to put a temperament and nowhere to put a
personality, a value ranking, or a set of habits. ``LifeScaffold`` holds the
circumstances that may legitimately be decided in advance (spec 22.3) and
nothing about who YUI turns out to be. Everything else is an outcome.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.clock import ensure_aware

#: Spec 22.4, cheapest first. Only the top two justify a detailed LLM call.
ExperienceClass = Literal["routine", "minor", "meaningful", "major", "turning_point"]

EXPERIENCE_CLASSES: tuple[ExperienceClass, ...] = (
    "routine",
    "minor",
    "meaningful",
    "major",
    "turning_point",
)

#: Spec 22.5: a stable stretch is one block; an eventful one is expanded.
DetailLevel = Literal["compressed", "expanded"]

#: Spec 22.7, in order. FIRST BOOT happens only if all of them pass.
AuditKind = Literal[
    "consistency",
    "knowledge_chronology",
    "identity",
    "drift",
    "quality",
]

AUDIT_KINDS: tuple[AuditKind, ...] = (
    "consistency",
    "knowledge_chronology",
    "identity",
    "drift",
    "quality",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class TemperamentSeed(_Frozen):
    """What the questionnaire is allowed to produce (spec 22.2).

    Note what is absent: no traits beyond temperament, no values, no habits,
    no finished personality. Those are what the simulation is *for*.
    """

    seed_id: str
    #: The raw answers, kept for provenance only.
    answers: dict[str, str] = Field(default_factory=dict)
    #: Temperamental bias — the bottom layer of spec 12.1, nothing above it.
    temperament: dict[str, float] = Field(default_factory=dict)
    #: Directions the owner asked to avoid. A constraint, not a personality.
    avoid: tuple[str, ...] = ()
    #: A few starting interests. What becomes an interest is still an outcome.
    interests: tuple[str, ...] = ()
    created_at: datetime

    @model_validator(mode="after")
    def _temperament_in_range(self):
        for name, value in self.temperament.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"temperament {name!r} must be within 0..1, got {value}")
        return self


class LifeScaffold(_Frozen):
    """Circumstances that may be decided in advance (spec 22.3)."""

    scaffold_id: str
    seed_id: str
    period_start: datetime
    period_end: datetime
    environment: str = ""
    education_context: str = ""
    social_density: float = Field(default=0.5, ge=0.0, le=1.0)
    technology_availability: float = Field(default=0.5, ge=0.0, le=1.0)
    life_stage: str = ""
    created_at: datetime

    @model_validator(mode="after")
    def _ordered(self):
        if self.period_end <= self.period_start:
            raise ValueError("a life scaffold must end after it starts")
        return self

    @property
    def years(self) -> float:
        return (self.period_end - self.period_start).total_seconds() / 31_557_600.0


class SimulationRun(_Frozen):
    simulation_id: str
    seed_id: str
    scaffold_id: str
    status: Literal["running", "completed", "failed", "aborted"] = "running"
    started_at: datetime
    ended_at: datetime | None = None
    simulated_from: datetime
    simulated_to: datetime
    blocks_run: int = 0
    experiences: int = 0
    #: Set only once every audit of spec 22.7 has passed.
    first_boot_at: datetime | None = None

    @property
    def booted(self) -> bool:
        return self.first_boot_at is not None


class LifePhase(_Frozen):
    phase_id: str
    simulation_id: str
    name: str
    started_at: datetime
    ended_at: datetime
    summary: str = ""
    ordinal: int = 0


class SimulationBlock(_Frozen):
    """One stretch of simulated time (spec 22.5)."""

    block_id: str
    simulation_id: str
    phase_id: str | None = None
    started_at: datetime
    ended_at: datetime
    detail_level: DetailLevel = "compressed"
    experience_class: ExperienceClass = "routine"
    summary: str = ""
    event_count: int = 0
    ordinal: int = 0

    @property
    def days(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() / 86400.0


class GenesisAudit(_Frozen):
    """One of the gates in front of FIRST BOOT (spec 22.7)."""

    audit_id: str
    simulation_id: str
    kind: AuditKind
    passed: bool = False
    detail: dict[str, object] = Field(default_factory=dict)
    recorded_at: datetime
