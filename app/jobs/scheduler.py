"""Scheduler (spec 19).

``Scheduler は Action を直接実行せず Opportunity を生成する``. Everything here
produces *opportunities*; whether anything is done about one is a decision made
elsewhere, by the Decision Engine.

Job classes (spec 19): FIXED fires at a time, WINDOW is valid across a span,
CONDITION is checked rather than timed, BACKGROUND is maintenance work.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from app.clock import Clock, SystemClock
from app.storage.repositories.world import JobRepository
from app.world.models import Opportunity, ScheduledJob
from app.world.policy import SchedulerPolicy

logger = logging.getLogger(__name__)

MODULE = "scheduler"

#: A condition job asks a predicate whether its moment has come.
Condition = Callable[[datetime], bool]


class Scheduler:
    name = MODULE

    def __init__(
        self,
        repository: JobRepository,
        policy: SchedulerPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._clock = clock or SystemClock()
        self._conditions: dict[str, Condition] = {}

    # --- scheduling --------------------------------------------------------
    def schedule_fixed(
        self,
        *,
        job_type: str,
        due_at: datetime,
        payload: dict[str, Any] | None = None,
        priority: str = "P4",
        misfire_policy: str | None = None,
        expires_at: datetime | None = None,
    ) -> ScheduledJob:
        return self._repository.schedule(
            job_type=job_type,
            job_class="FIXED",
            due_at=due_at,
            window_end=None,
            payload=payload or {},
            priority=priority,
            misfire_policy=misfire_policy or self._policy.default_misfire_policy,
            expires_at=expires_at,
            now=self._clock.now(),
        )

    def schedule_window(
        self,
        *,
        job_type: str,
        opens_at: datetime,
        closes_at: datetime,
        payload: dict[str, Any] | None = None,
        priority: str = "P5",
    ) -> ScheduledJob:
        return self._repository.schedule(
            job_type=job_type,
            job_class="WINDOW",
            due_at=opens_at,
            window_end=closes_at,
            payload=payload or {},
            priority=priority,
            misfire_policy="skip",
            expires_at=closes_at,
            now=self._clock.now(),
        )

    def schedule_background(
        self, *, job_type: str, due_at: datetime, payload: dict[str, Any] | None = None
    ) -> ScheduledJob:
        return self._repository.schedule(
            job_type=job_type,
            job_class="BACKGROUND",
            due_at=due_at,
            window_end=None,
            payload=payload or {},
            priority="P6",
            misfire_policy="run_once",
            expires_at=None,
            now=self._clock.now(),
        )

    def register_condition(self, job_type: str, condition: Condition) -> None:
        self._conditions[job_type] = condition

    def schedule_condition(
        self, *, job_type: str, payload: dict[str, Any] | None = None, priority: str = "P5"
    ) -> ScheduledJob:
        return self._repository.schedule(
            job_type=job_type,
            job_class="CONDITION",
            due_at=None,
            window_end=None,
            payload=payload or {},
            priority=priority,
            misfire_policy="skip",
            expires_at=None,
            now=self._clock.now(),
        )

    # --- ticking -----------------------------------------------------------
    def tick(self, *, now: datetime | None = None) -> list[Opportunity]:
        """Produce opportunities. Nothing is executed here (spec 19)."""
        moment = now or self._clock.now()
        opportunities: list[Opportunity] = []

        for job in self._repository.pending(limit=200):
            if len(opportunities) >= self._policy.max_opportunities_per_tick:
                break

            if job.is_expired(moment):
                self._repository.mark(job.job_id, "expired", now=moment)
                continue

            if job.job_class == "CONDITION":
                condition = self._conditions.get(job.job_type)
                if condition is None or not condition(moment):
                    continue
            elif job.due_at is None or moment < job.due_at:
                continue
            elif job.missed_window(moment):
                # The window closed before anything acted on it (spec 19).
                status = "misfired" if job.misfire_policy == "run_once" else "expired"
                self._repository.mark(job.job_id, status, now=moment)
                if job.misfire_policy != "run_once":
                    continue

            self._repository.mark(job.job_id, "fired", now=moment)
            opportunities.append(
                Opportunity(
                    kind=job.job_type,
                    detail=str(job.payload.get("detail", "")),
                    job_id=job.job_id,
                    urgency=float(job.payload.get("urgency", 0.3)),
                    created_at=moment,
                )
            )

        if opportunities:
            logger.info("scheduler produced %d opportunit(y|ies)", len(opportunities))
        return opportunities

    # --- recovery (spec 32) -------------------------------------------------
    def restore(self, *, now: datetime | None = None) -> int:
        """After a restart, retire jobs whose moment passed while offline."""
        moment = now or self._clock.now()
        retired = 0
        for job in self._repository.pending(limit=500):
            if job.is_expired(moment) or (
                job.window_end is not None
                and moment > job.window_end + timedelta(
                    minutes=self._policy.window_grace_minutes
                )
            ):
                self._repository.mark(job.job_id, "expired", now=moment)
                retired += 1
        return retired

    def pending(self, *, limit: int = 50) -> list[ScheduledJob]:
        return self._repository.pending(limit=limit)
