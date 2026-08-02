"""Historical knowledge domain models (spec 21).

Three layers, kept apart (spec 21.1)::

    External / World Knowledge      what was true and public, and when
    Autobiographical / Semantic     what YUI lived through
    Current Retained Knowledge      what she can actually still use

``KnowledgeItem`` is the first layer. It says nothing about YUI. Only an
``Acquisition`` — the end of the exposure funnel — says she knows it, and that
is on purpose:

    LLM の pretrained knowledge があることを、YUI が知っている根拠にしない。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.clock import ensure_aware
from app.events.model import EventOrigin

#: Spec 21.2.
CoverageClass = Literal[
    "foundational",
    "environmental",
    "historical_cultural",
    "interest_driven",
]

COVERAGE_CLASSES: tuple[CoverageClass, ...] = (
    "foundational",
    "environmental",
    "historical_cultural",
    "interest_driven",
)

#: How likely a claim is to still hold later. Used by forgetting and by the
#: knowledge audit, not by the temporal guard.
Stability = Literal["STABLE", "CHANGEABLE", "VOLATILE"]

#: Spec 21.5, in order. Each one can be where it stops.
ExposureStage = Literal[
    "existed",
    "opportunity",
    "reached",
    "attended",
    "curious",
    "comprehended",
    "encoded",
    "retained",
]

STAGES: tuple[ExposureStage, ...] = (
    "existed",
    "opportunity",
    "reached",
    "attended",
    "curious",
    "comprehended",
    "encoded",
    "retained",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def _reject_naive(cls, value: object) -> object:
        return ensure_aware(value) if isinstance(value, datetime) else value


class KnowledgeSource(_Frozen):
    source_id: str
    name: str
    kind: str = "reference"
    url: str | None = None
    published_at: datetime | None = None
    reliability: float = Field(default=0.5, ge=0.0, le=1.0)
    created_at: datetime


class KnowledgeItem(_Frozen):
    """One thing the world knew, from when, and how sure we are (spec 21.3)."""

    knowledge_id: str
    statement: str
    coverage_class: CoverageClass
    topic: str = ""
    geography: str = "global"
    language: str = "ja"
    #: The moment this became publicly available. The temporal guard is built
    #: entirely on this field, and it is required for exactly that reason.
    available_from: datetime
    available_until: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    #: When the *document we are using* was published. May be far later than
    #: ``available_from``: a modern reference can attest that something was
    #: already public in 1998 (spec 21.4).
    source_published_at: datetime | None = None
    stability: Stability = "CHANGEABLE"
    truth_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    complexity: float = Field(default=0.5, ge=0.0, le=1.0)
    salience: float = Field(default=0.3, ge=0.0, le=1.0)
    source_id: str | None = None
    version: int = 1
    created_at: datetime

    @model_validator(mode="after")
    def _coherent_window(self):
        if self.available_until is not None and self.available_until <= self.available_from:
            raise ValueError("available_until must be after available_from")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        return self

    def existed_at(self, moment: datetime) -> bool:
        """Was this publicly available at ``moment``? (spec 21.4)"""
        if moment < self.available_from:
            return False
        return self.available_until is None or moment < self.available_until

    def true_at(self, moment: datetime) -> bool:
        """Was it still the case then? Separate question from availability."""
        if self.valid_from is not None and moment < self.valid_from:
            return False
        return self.valid_until is None or moment < self.valid_until


class ExposureOpportunity(_Frozen):
    """A moment when YUI *could* have come across something (spec 21.5)."""

    opportunity_id: str
    knowledge_id: str
    occurred_at: datetime
    channel: str = "ambient"
    salience: float = Field(default=0.3, ge=0.0, le=1.0)
    reach: float = Field(default=0.5, ge=0.0, le=1.0)
    stage_reached: ExposureStage = "opportunity"
    acquired: bool = False
    reason: str = ""
    simulation_block_id: str | None = None
    created_at: datetime

    @property
    def became_knowledge(self) -> bool:
        return self.acquired and self.stage_reached == "retained"


class Acquisition(_Frozen):
    """The only record that means YUI knows something (spec 21.1)."""

    acquisition_id: str
    knowledge_id: str
    opportunity_id: str | None = None
    acquired_at: datetime
    comprehension: float = Field(default=0.5, ge=0.0, le=1.0)
    retention: float = Field(default=0.5, ge=0.0, le=1.0)
    status: Literal["retained", "faded", "forgotten"] = "retained"
    semantic_memory_id: str | None = None
    origin: EventOrigin = "simulated_past"

    @property
    def usable(self) -> bool:
        return self.status == "retained"


class CoverageJob(_Frozen):
    """A request to fill in one class of knowledge for one period (spec 21.2)."""

    job_id: str
    coverage_class: CoverageClass
    period_start: datetime
    period_end: datetime
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    requested_at: datetime
    completed_at: datetime | None = None
    produced_count: int = 0

    @model_validator(mode="after")
    def _ordered_period(self):
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        return self
