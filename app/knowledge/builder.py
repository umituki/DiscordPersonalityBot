"""Historical Knowledge Builder (spec 21.1-21.3).

Turns "what did the world know, and when" into checkable records. Every
candidate must arrive with an availability date, because a claim without one
cannot be tested against a past moment — and an untestable claim is exactly how
hindsight gets in.

Coverage is organised as jobs over a class and a period (spec 21.2), so a gap
in what YUI could plausibly have picked up is visible rather than silently
absent.

This module writes only the external-knowledge layer. It never says YUI knows
anything; that requires an acquisition, which requires surviving the exposure
funnel (:mod:`app.knowledge.service`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from app import ids
from app.clock import Clock, SystemClock
from app.knowledge.models import (
    COVERAGE_CLASSES,
    CoverageClass,
    CoverageJob,
    KnowledgeItem,
    KnowledgeSource,
    Stability,
)
from app.knowledge.policy import KnowledgePolicy
from app.storage.repositories.knowledge import CoverageJobRepository, KnowledgeRepository

logger = logging.getLogger(__name__)

MODULE = "knowledge_builder"
KNOWLEDGE = "knw"


class KnowledgeError(ValueError):
    """Raised when a knowledge candidate cannot be accepted as it stands."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A proposed piece of period knowledge, before validation."""

    statement: str
    coverage_class: CoverageClass
    available_from: datetime
    topic: str = ""
    geography: str = "global"
    language: str = "ja"
    available_until: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    source_published_at: datetime | None = None
    stability: Stability = "CHANGEABLE"
    truth_confidence: float | None = None
    complexity: float = 0.5
    salience: float = 0.3
    source_id: str | None = None


class KnowledgeBuilder:
    name = MODULE

    def __init__(
        self,
        *,
        knowledge: KnowledgeRepository,
        jobs: CoverageJobRepository,
        policy: KnowledgePolicy,
        clock: Clock | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._jobs = jobs
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- sources -----------------------------------------------------------
    def add_source(
        self,
        *,
        name: str,
        kind: str = "reference",
        url: str | None = None,
        published_at: datetime | None = None,
        reliability: float = 0.5,
    ) -> KnowledgeSource:
        """Record where a claim came from.

        A modern source is fine (spec 21.4): it can attest that something was
        already public long before it was written about. What matters is the
        knowledge's own ``available_from``, which the guard checks.
        """
        return self._knowledge.add_source(
            name=name,
            kind=kind,
            url=url,
            published_at=published_at,
            reliability=reliability,
            now=self._clock.now(),
        )

    # --- candidates --------------------------------------------------------
    def register(self, candidate: Candidate) -> KnowledgeItem:
        """Validate and store one candidate."""
        if not candidate.statement.strip():
            raise KnowledgeError("a knowledge candidate needs a statement")
        if candidate.coverage_class not in COVERAGE_CLASSES:
            raise KnowledgeError(
                f"unknown coverage class {candidate.coverage_class!r}; "
                f"expected one of {list(COVERAGE_CLASSES)}"
            )
        if self._policy.builder.require_available_from and candidate.available_from is None:
            raise KnowledgeError(
                "a knowledge candidate without available_from cannot be checked "
                "against a past moment (spec 21.3)"
            )

        existing = self._knowledge.by_statement(candidate.statement)
        if existing is not None:
            return existing

        salience = candidate.salience
        if candidate.coverage_class == "interest_driven":
            salience = min(
                1.0, salience + self._policy.builder.interest_salience_bonus
            )

        now = self._clock.now()
        item = KnowledgeItem(
            knowledge_id=ids.new_id(KNOWLEDGE),
            statement=candidate.statement,
            coverage_class=candidate.coverage_class,
            topic=candidate.topic,
            geography=candidate.geography,
            language=candidate.language,
            available_from=candidate.available_from,
            available_until=candidate.available_until,
            valid_from=candidate.valid_from or candidate.available_from,
            valid_until=candidate.valid_until,
            source_published_at=candidate.source_published_at,
            stability=candidate.stability,
            truth_confidence=(
                self._policy.builder.default_truth_confidence
                if candidate.truth_confidence is None
                else candidate.truth_confidence
            ),
            complexity=candidate.complexity,
            salience=salience,
            source_id=candidate.source_id,
            created_at=now,
        )
        stored = self._knowledge.add_knowledge(item)
        self._knowledge.record_version(
            knowledge_id=stored.knowledge_id,
            version=1,
            statement=stored.statement,
            valid_from=stored.valid_from,
            valid_until=stored.valid_until,
            truth_confidence=stored.truth_confidence,
            reason="registered",
            now=now,
        )
        return stored

    def register_all(self, candidates: Iterable[Candidate]) -> list[KnowledgeItem]:
        return [self.register(candidate) for candidate in candidates]

    def revise(
        self,
        knowledge_id: str,
        *,
        statement: str,
        truth_confidence: float,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        reason: str = "revised",
    ) -> str:
        """What we now believe, without erasing what we believed before."""
        item = self._knowledge.knowledge(knowledge_id)
        if item is None:
            raise KnowledgeError(f"unknown knowledge: {knowledge_id!r}")
        return self._knowledge.record_version(
            knowledge_id=knowledge_id,
            version=len(self._knowledge.versions(knowledge_id)) + 1,
            statement=statement,
            valid_from=valid_from or item.valid_from,
            valid_until=valid_until or item.valid_until,
            truth_confidence=truth_confidence,
            reason=reason,
            now=self._clock.now(),
        )

    # --- coverage (spec 21.2) ----------------------------------------------
    def request_coverage(
        self, *, coverage_class: CoverageClass, period_start: datetime, period_end: datetime
    ) -> CoverageJob:
        if coverage_class not in COVERAGE_CLASSES:
            raise KnowledgeError(f"unknown coverage class {coverage_class!r}")
        if period_end <= period_start:
            raise KnowledgeError("a coverage period must end after it starts")
        return self._jobs.create(
            coverage_class=coverage_class,
            period_start=period_start,
            period_end=period_end,
            now=self._clock.now(),
        )

    def fulfil(self, job_id: str, candidates: Sequence[Candidate]) -> CoverageJob:
        """Store a batch against a coverage job.

        Candidates outside the job's period are rejected rather than clipped:
        a coverage job for the nineties that quietly absorbed 2020 material
        would be a leak with a paper trail saying otherwise.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise KnowledgeError(f"unknown coverage job: {job_id!r}")

        limit = self._policy.builder.max_candidates_per_job
        if len(candidates) > limit:
            raise KnowledgeError(
                f"{len(candidates)} candidates exceeds the per-job limit of {limit}"
            )

        outside = [
            candidate.statement
            for candidate in candidates
            if not (job.period_start <= candidate.available_from < job.period_end)
        ]
        if outside:
            self._jobs.complete(
                job_id,
                produced=0,
                detail={"rejected_outside_period": outside[:20]},
                now=self._clock.now(),
                status="failed",
            )
            raise KnowledgeError(
                f"{len(outside)} candidate(s) fall outside the job period "
                f"{job.period_start.date()}..{job.period_end.date()}"
            )

        stored = self.register_all(candidates)
        logger.info(
            "coverage job fulfilled class=%s produced=%d", job.coverage_class, len(stored)
        )
        return self._jobs.complete(
            job_id,
            produced=len(stored),
            detail={"coverage_class": job.coverage_class},
            now=self._clock.now(),
        )

    # --- reads --------------------------------------------------------------
    def known_world_at(
        self, moment: datetime, *, coverage_class: str | None = None, limit: int = 100
    ) -> list[KnowledgeItem]:
        """What the *world* knew then. Says nothing about YUI."""
        return self._knowledge.available_at(
            moment, coverage_class=coverage_class, limit=limit
        )

    def coverage_jobs(self) -> list[CoverageJob]:
        return self._jobs.all()

    def gaps(self, *, period_start: datetime, period_end: datetime) -> list[str]:
        """Coverage classes with no completed job for this period (spec 21.2)."""
        covered = {
            job.coverage_class
            for job in self._jobs.all()
            if job.status == "completed"
            and job.period_start <= period_start
            and job.period_end >= period_end
        }
        return [name for name in COVERAGE_CLASSES if name not in covered]
