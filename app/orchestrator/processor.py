"""Root event processing (spec 7.1, 9.5).

::

    append event
    → S0 snapshot
    → open run
    → dispatch to subscribers
    → collect proposals
    → arbitrate
    → atomic commit

The processor decides *order*, never meaning. It does not compute emotions,
magnitudes or relevance; those belong to the engines and to policy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from app import ids
from app.clock import Clock, SystemClock
from app.config import RuntimeMode
from app.events.dispatcher import DispatchResult, EventDispatcher
from app.events.model import Event
from app.events.store import EventStore
from app.orchestrator.run_context import RunContext
from app.state.arbitrator import ArbitrationResult, StateArbitrator
from app.state.committer import CommitResult, StateCommitter
from app.state.snapshot import SnapshotService, StateSnapshot
from app.storage.database import Database
from app.storage.repositories.failures import FailureRecord, FailureRepository
from app.storage.repositories.runs import ProcessingRunRepository

logger = logging.getLogger(__name__)

COMPONENT = "event_processor"

ProcessingStatus = Literal["committed", "rejected", "no_subscribers", "failed"]


@dataclass(frozen=True, slots=True)
class ProcessingOutcome:
    run: RunContext
    event: Event
    status: ProcessingStatus
    newly_stored: bool
    dispatch: DispatchResult | None
    arbitration: ArbitrationResult | None
    commit: CommitResult | None
    #: The S0 snapshot the run read. Callers that generate an action from the
    #: same event must read this, not live state (spec 9.2).
    snapshot: StateSnapshot | None = None
    error: str | None = None

    @property
    def committed_targets(self) -> tuple[str, ...]:
        return () if self.commit is None else self.commit.committed_targets

    @property
    def degraded(self) -> bool:
        return bool(self.dispatch and self.dispatch.degraded)


class EventProcessor:
    def __init__(
        self,
        *,
        db: Database,
        event_store: EventStore,
        dispatcher: EventDispatcher,
        snapshots: SnapshotService,
        arbitrator: StateArbitrator,
        committer: StateCommitter,
        runs: ProcessingRunRepository,
        failures: FailureRepository,
        manifest_id: str | None = None,
        mode: RuntimeMode = "normal",
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._events = event_store
        self._dispatcher = dispatcher
        self._snapshots = snapshots
        self._arbitrator = arbitrator
        self._committer = committer
        self._runs = runs
        self._failures = failures
        self._manifest_id = manifest_id
        self._mode = mode
        self._clock = clock or SystemClock()

    async def process(self, event: Event, *, mode: RuntimeMode | None = None) -> ProcessingOutcome:
        run_mode = mode or self._mode
        newly_stored = await asyncio.to_thread(self._events.append, event)
        run, snapshot = await asyncio.to_thread(self._open_run, event, run_mode)

        try:
            dispatch = await self._dispatcher.dispatch(event, snapshot, run_id=run.run_id)
        except Exception as exc:  # noqa: BLE001 - recorded, then reported
            logger.exception("dispatch failed run_id=%s event_id=%s", run.run_id, event.event_id)
            await asyncio.to_thread(self._fail_run, run, event, "dispatch_failed", repr(exc))
            return ProcessingOutcome(
                run=run,
                event=event,
                status="failed",
                newly_stored=newly_stored,
                dispatch=None,
                arbitration=None,
                commit=None,
                snapshot=snapshot,
                error=repr(exc),
            )

        arbitration = self._arbitrator.arbitrate(dispatch.proposals, snapshot)

        try:
            commit = await asyncio.to_thread(self._commit, run, event, dispatch, arbitration)
        except Exception as exc:  # noqa: BLE001 - the transaction already rolled back
            logger.exception("commit failed run_id=%s event_id=%s", run.run_id, event.event_id)
            await asyncio.to_thread(self._fail_run, run, event, "commit_failed", repr(exc))
            return ProcessingOutcome(
                run=run,
                event=event,
                status="failed",
                newly_stored=newly_stored,
                dispatch=dispatch,
                arbitration=arbitration,
                commit=None,
                snapshot=snapshot,
                error=repr(exc),
            )

        if not dispatch.outcomes:
            status: ProcessingStatus = "no_subscribers"
        elif arbitration.accepted:
            status = "committed"
        else:
            status = "rejected"

        return ProcessingOutcome(
            run=run,
            event=event,
            status=status,
            newly_stored=newly_stored,
            dispatch=dispatch,
            arbitration=arbitration,
            commit=commit,
            snapshot=snapshot,
        )

    # --- synchronous units of work ----------------------------------------
    def _open_run(self, event: Event, mode: RuntimeMode) -> tuple[RunContext, StateSnapshot]:
        run_id = ids.new_id(ids.RUN)
        with self._db.transaction():
            snapshot = self._snapshots.capture(
                root_event_id=event.root_event_id, run_id=run_id, persist=True
            )
            run = RunContext.create(
                run_id=run_id,
                root_event_id=event.event_id,
                state_snapshot_id=snapshot.snapshot_id,
                runtime_manifest_id=self._manifest_id,
                priority=event.priority,
                mode=mode,
                clock=self._clock,
            )
            self._runs.start(
                run_id=run.run_id,
                root_event_id=event.event_id,
                snapshot_id=snapshot.snapshot_id,
                manifest_id=self._manifest_id,
                mode=mode,
                priority=event.priority,
                now=run.started_at,
            )
        return run, snapshot

    def _commit(
        self,
        run: RunContext,
        event: Event,
        dispatch: DispatchResult,
        arbitration: ArbitrationResult,
    ) -> CommitResult:
        with self._db.transaction():
            # Follow-up events belong to the same atomic unit as the state they
            # describe. Processing them is scheduled work, not part of this run.
            if dispatch.derived_events:
                self._events.append_all(dispatch.derived_events)
            return self._committer.commit(
                run_id=run.run_id,
                root_event_id=event.event_id,
                result=arbitration,
                delivery_ids=dispatch.delivery_ids,
            )

    def _fail_run(self, run: RunContext, event: Event, reason_code: str, error: str) -> None:
        now = self._clock.now()
        with self._db.transaction():
            self._failures.record(
                FailureRecord(
                    failure_type="state" if reason_code == "commit_failed" else "behavior",
                    component=COMPONENT,
                    reason_code=reason_code,
                    severity="error",
                    run_id=run.run_id,
                    event_id=event.event_id,
                    detail={"error": error[:1000]},
                ),
                now=now,
            )
            self._runs.finish(run_id=run.run_id, status="failed", now=now, error=error)
