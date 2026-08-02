"""Atomic state commit (spec 9.5 phase 8, 31.1).

One transaction covers everything a run produced:

* the new values of every accepted change, with an optimistic version check,
* the ``state_changes`` audit rows,
* the ``failures`` rows for every rejected proposal,
* the delivery rows that consumed the event,
* the run's final status.

If any part fails the whole run rolls back, deliveries stay unconsumed, and the
event can be reprocessed. Partial psychology is never persisted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from app import ids
from app.clock import Clock, SystemClock
from app.state.arbitrator import ArbitrationResult
from app.state.value import value_type_of
from app.storage.database import Database
from app.storage.repositories.deliveries import DeliveryRepository
from app.storage.repositories.failures import FailureRecord, FailureRepository
from app.storage.repositories.runs import ProcessingRunRepository
from app.storage.repositories.state import StateRepository

logger = logging.getLogger(__name__)

COMPONENT = "state_committer"


@dataclass(frozen=True, slots=True)
class CommitResult:
    run_id: str
    change_ids: tuple[str, ...]
    committed_targets: tuple[str, ...]
    rejected_proposals: int
    failure_ids: tuple[str, ...]
    completed_deliveries: tuple[str, ...]

    @property
    def committed_count(self) -> int:
        return len(self.change_ids)


class StateCommitter:
    def __init__(
        self,
        db: Database,
        state_repository: StateRepository,
        run_repository: ProcessingRunRepository,
        delivery_repository: DeliveryRepository,
        failure_repository: FailureRepository,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._state = state_repository
        self._runs = run_repository
        self._deliveries = delivery_repository
        self._failures = failure_repository
        self._clock = clock or SystemClock()

    def commit(
        self,
        *,
        run_id: str,
        root_event_id: str,
        result: ArbitrationResult,
        delivery_ids: Sequence[str] = (),
        now: datetime | None = None,
        run_finished_at: datetime | None = None,
    ) -> CommitResult:
        moment = now or self._clock.now()
        finished_at = run_finished_at or moment
        change_ids: list[str] = []
        failure_ids: list[str] = []

        with self._db.transaction():
            for change in result.accepted:
                version_after = self._state.write_value(
                    domain=change.domain,
                    key=change.key,
                    value=change.new_value,
                    confidence=change.confidence,
                    now=moment,
                    run_id=run_id,
                    event_id=change.source_event_id,
                    expected_version=change.expected_version,
                )
                change_id = ids.new_id(ids.CHANGE)
                self._state.record_change(
                    change_id=change_id,
                    run_id=run_id,
                    source_event_id=change.source_event_id,
                    proposal_id=change.proposal_id,
                    source_module=change.source_module,
                    domain=change.domain,
                    key=change.key,
                    operation=change.operation,
                    value_type=value_type_of(change.new_value),
                    previous_value=change.previous_value,
                    new_value=change.new_value,
                    delta=change.delta,
                    confidence=change.confidence,
                    version_after=version_after,
                    reason_codes=change.reason_codes,
                    evidence_ids=change.evidence_ids,
                    now=moment,
                )
                change_ids.append(change_id)

            for rejection in result.rejected:
                failure_ids.append(
                    self._failures.record(
                        FailureRecord(
                            failure_type="state",
                            component=COMPONENT,
                            reason_code=rejection.reason_code,
                            severity="warning",
                            run_id=run_id,
                            event_id=root_event_id,
                            reference_id=rejection.proposal_ids[0]
                            if rejection.proposal_ids
                            else None,
                            detail={
                                "domain": rejection.domain,
                                "key": rejection.key,
                                "detail": rejection.detail,
                                "proposal_ids": list(rejection.proposal_ids),
                                "source_modules": list(rejection.source_modules),
                            },
                        ),
                        now=moment,
                    )
                )

            for delivery_id in delivery_ids:
                self._deliveries.mark_completed(delivery_id, now=moment)

            self._runs.finish(
                run_id=run_id,
                status="committed" if result.accepted else "rejected",
                now=finished_at,
                proposals_received=result.received,
                proposals_accepted=result.accepted_count,
                proposals_rejected=result.rejected_proposal_count,
            )

        logger.info(
            "run committed run_id=%s accepted=%d rejected=%d targets=%s",
            run_id,
            len(change_ids),
            result.rejected_proposal_count,
            ",".join(result.targets()) or "-",
        )
        return CommitResult(
            run_id=run_id,
            change_ids=tuple(change_ids),
            committed_targets=result.targets(),
            rejected_proposals=result.rejected_proposal_count,
            failure_ids=tuple(failure_ids),
            completed_deliveries=tuple(delivery_ids),
        )
