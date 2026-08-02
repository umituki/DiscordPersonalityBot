"""Planning what to know about, period by period (patch spec 16.3).

    期間ごとに foundational / environmental / historical_cultural /
    interest_driven を計画する。

The builder can store a candidate and the funnel can decide whether YUI picked
it up, but neither of them decides *what to look for*. Nothing did, which is
why nineteen simulated years ran with an empty knowledge layer.

This plans one request per window per coverage class, asks the providers, and
fulfils the coverage jobs. It writes only the external-knowledge layer: nothing
here says YUI knows anything, and the temporal fields are preserved exactly as
the providers gave them, so the leakage guard still has something to check
(patch spec 16.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from app.clock import Clock, SystemClock
from app.knowledge.builder import KnowledgeBuilder, KnowledgeError
from app.knowledge.models import COVERAGE_CLASSES, CoverageClass
from app.knowledge.providers import CoverageRequest, ProviderRegistry

logger = logging.getLogger(__name__)

MODULE = "coverage_planner"


@dataclass(frozen=True, slots=True)
class CoverageOutcome:
    """What one request produced."""

    request: CoverageRequest
    offered: int
    stored: int
    rejected: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class CoverageReport:
    """What the whole plan produced, for the knowledge health audit (17.3)."""

    outcomes: list[CoverageOutcome] = field(default_factory=list)

    @property
    def requests(self) -> int:
        return len(self.outcomes)

    @property
    def candidates_offered(self) -> int:
        return sum(outcome.offered for outcome in self.outcomes)

    @property
    def candidates_stored(self) -> int:
        return sum(outcome.stored for outcome in self.outcomes)

    @property
    def classes_covered(self) -> tuple[str, ...]:
        """Classes the providers had something for.

        Measured on what was *offered*, not on what was newly stored: a
        long-standing fact registered in the first window is still coverage of
        the tenth, and counting only new rows would report a class as missing
        the moment it stopped changing.
        """
        return tuple(
            sorted(
                {
                    outcome.request.coverage_class
                    for outcome in self.outcomes
                    if outcome.offered > 0
                }
            )
        )

    @property
    def missing_classes(self) -> tuple[str, ...]:
        return tuple(name for name in COVERAGE_CLASSES if name not in self.classes_covered)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(outcome.error for outcome in self.outcomes if outcome.error)

    def as_detail(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "candidates_offered": self.candidates_offered,
            "candidates_stored": self.candidates_stored,
            "classes_covered": list(self.classes_covered),
            "missing_classes": list(self.missing_classes),
            "errors": list(self.errors[:10]),
        }


class CoveragePlanner:
    name = MODULE

    def __init__(
        self,
        *,
        builder: KnowledgeBuilder,
        providers: ProviderRegistry,
        window_days: float = 365.0,
        per_request_limit: int = 40,
        classes: Sequence[CoverageClass] = COVERAGE_CLASSES,
        clock: Clock | None = None,
    ) -> None:
        if window_days <= 0:
            raise ValueError("a coverage window must be a positive number of days")
        self._builder = builder
        self._providers = providers
        self._window = timedelta(days=window_days)
        self._limit = per_request_limit
        self._classes = tuple(classes)
        self._clock = clock or SystemClock()

    def plan(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        topics: Sequence[str] = (),
        geography: str = "global",
        language: str = "ja",
    ) -> list[CoverageRequest]:
        """One request per window per class, covering the whole period."""
        if period_end <= period_start:
            raise ValueError("a coverage period must end after it starts")

        requests: list[CoverageRequest] = []
        window_start = period_start
        while window_start < period_end:
            window_end = min(period_end, window_start + self._window)
            # A stub of a window left over at the end is folded into this one:
            # a coverage job for the last three days of a decade is noise, and
            # it would report as a gap for every class.
            if period_end - window_end < self._window / 2:
                window_end = period_end
            for coverage_class in self._classes:
                requests.append(
                    CoverageRequest(
                        coverage_class=coverage_class,
                        period_start=window_start,
                        period_end=window_end,
                        geography=geography,
                        language=language,
                        topics=tuple(topics),
                        limit=self._limit,
                    )
                )
            window_start = window_end
        return requests

    def run(
        self,
        *,
        period_start: datetime,
        period_end: datetime,
        topics: Sequence[str] = (),
        geography: str = "global",
        language: str = "ja",
    ) -> CoverageReport:
        """Execute the plan. Gaps are reported, never filled with guesses."""
        report = CoverageReport()
        for request in self.plan(
            period_start=period_start,
            period_end=period_end,
            topics=topics,
            geography=geography,
            language=language,
        ):
            report.outcomes.append(self._fulfil(request))

        logger.info(
            "coverage complete requests=%d stored=%d missing=%s",
            report.requests,
            report.candidates_stored,
            ",".join(report.missing_classes) or "-",
        )
        return report

    def _fulfil(self, request: CoverageRequest) -> CoverageOutcome:
        offered = self._providers.candidates_for(request)
        if not offered:
            # An empty window is recorded, not skipped: patch spec 16.1 —
            # ``0件silent pass禁止``. The audit reads these.
            return CoverageOutcome(request=request, offered=0, stored=0)

        # A claim already in the layer does not need a second coverage job.
        # It still counts as coverage of this window — see `classes_covered`.
        candidates = [
            candidate
            for candidate in offered
            if not self._builder.knows(candidate.statement)
        ]
        if not candidates:
            return CoverageOutcome(request=request, offered=len(offered), stored=0)

        job = self._builder.request_coverage(
            coverage_class=request.coverage_class,
            period_start=request.period_start,
            period_end=request.period_end,
        )
        try:
            completed = self._builder.fulfil(job.job_id, candidates)
        except KnowledgeError as exc:
            logger.warning("coverage job rejected: %s", exc)
            return CoverageOutcome(
                request=request,
                offered=len(offered),
                stored=0,
                rejected=len(candidates),
                error=str(exc)[:300],
            )
        return CoverageOutcome(
            request=request, offered=len(offered), stored=completed.produced_count
        )


__all__ = ["CoverageOutcome", "CoveragePlanner", "CoverageReport", "MODULE"]
