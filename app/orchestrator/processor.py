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
from typing import Literal, Protocol

from app import ids
from app.clock import Clock, SystemClock
from app.config import RuntimeMode
from app.events.dispatcher import DispatchResult, EventDispatcher
from app.events.model import Event
from app.events.store import EventStore
from app.orchestrator.run_context import RunContext
from app.orchestrator.run_view import Interpretation, RunView
from app.state.arbitrator import ArbitrationResult, StateArbitrator
from app.state.committer import CommitResult, StateCommitter
from app.state.snapshot import SnapshotService, StateSnapshot
from app.storage.database import Database
from app.storage.repositories.failures import FailureRecord, FailureRepository
from app.storage.repositories.runs import ProcessingRunRepository
from app.storage.repositories.state import ConcurrentStateWriteError

logger = logging.getLogger(__name__)

COMPONENT = "event_processor"

ProcessingStatus = Literal[
    "committed", "rejected", "no_subscribers", "failed", "conflict"
]


class Interpreter(Protocol):
    """Produces the Layer 1 interpretation of an event (spec 9.1, 9.5 phase 1)."""

    async def interpret(self, event: Event, snapshot: StateSnapshot) -> Interpretation: ...


@dataclass(frozen=True, slots=True)
class ProcessingOutcome:
    run: RunContext
    event: Event
    status: ProcessingStatus
    newly_stored: bool
    dispatch: DispatchResult | None
    arbitration: ArbitrationResult | None
    commit: CommitResult | None
    #: Layer 1 derivations for this run, if an interpreter produced any.
    interpretation: Interpretation | None = None
    #: The S0 snapshot the run read. Callers that generate an action from the
    #: same event must read this, not live state (spec 9.2).
    snapshot: StateSnapshot | None = None
    error: str | None = None
    #: Patch spec 7.5 telemetry.
    commit_attempt: int = 1
    retry_of_run_id: str | None = None

    @property
    def conflicted(self) -> bool:
        """The commit lost a version race and nothing was written."""
        return self.status == "conflict"

    @property
    def committed_targets(self) -> tuple[str, ...]:
        return () if self.commit is None else self.commit.committed_targets

    @property
    def post_commit_snapshot(self) -> StateSnapshot | None:
        """S0 plus what this run committed (patch spec 9).

        An action generated from this event should express the state the event
        produced. When the commit did not happen, nothing was written, so S0
        *is* the current state — and because a conflicted run is reprocessed
        against a fresh snapshot, S0 here is never the stale one that lost.
        """
        if self.snapshot is None:
            return None
        if self.status != "committed" or self.arbitration is None:
            return self.snapshot
        return self.snapshot.with_committed(
            (change.domain, change.key, change.new_value, change.confidence)
            for change in self.arbitration.accepted
        )

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
        interpreter: Interpreter | None = None,
        manifest_id: str | None = None,
        mode: RuntimeMode = "normal",
        clock: Clock | None = None,
        max_commit_attempts: int = 2,
    ) -> None:
        if max_commit_attempts < 1:
            raise ValueError("max_commit_attempts must be at least 1")
        self._db = db
        self._events = event_store
        self._dispatcher = dispatcher
        self._snapshots = snapshots
        self._arbitrator = arbitrator
        self._committer = committer
        self._runs = runs
        self._failures = failures
        self._interpreter = interpreter
        self._manifest_id = manifest_id
        self._mode = mode
        self._clock = clock or SystemClock()
        #: Patch spec 7.2: bounded. One reprocess, never an unbounded loop.
        self._max_commit_attempts = max_commit_attempts

    async def process(self, event: Event, *, mode: RuntimeMode | None = None) -> ProcessingOutcome:
        """Process one event, reprocessing once if a commit lost a version race.

        Patch spec 7.1-7.3. A ``ConcurrentStateWriteError`` used to end the run
        as ``failed`` with zero state changes, which silently discarded the
        psychological effect of a USER message because a background world tick
        had moved on underneath it.

        The retry is a *full* reprocess against a fresh snapshot, not a replay
        of the old proposals: proposals derived from a stale snapshot are
        exactly what must not be committed (7.3). Bounded to one retry (7.2).
        """
        retry_of: str | None = None
        outcome: ProcessingOutcome | None = None

        for attempt in range(1, self._max_commit_attempts + 1):
            outcome = await self._process_once(
                event, mode=mode, retry_of=retry_of, commit_attempt=attempt
            )
            if not outcome.conflicted:
                return outcome
            if attempt >= self._max_commit_attempts:
                logger.error(
                    "giving up after %d commit conflicts event_id=%s",
                    attempt,
                    event.event_id,
                )
                break
            logger.warning(
                "commit conflict; reprocessing against a fresh snapshot event_id=%s",
                event.event_id,
            )
            retry_of = outcome.run.run_id

        assert outcome is not None
        return outcome

    async def _process_once(
        self,
        event: Event,
        *,
        mode: RuntimeMode | None = None,
        retry_of: str | None = None,
        commit_attempt: int = 1,
    ) -> ProcessingOutcome:
        run_mode = mode or self._mode
        newly_stored = await asyncio.to_thread(self._events.append, event)
        run, snapshot = await asyncio.to_thread(
            self._open_run, event, run_mode, retry_of, commit_attempt
        )

        # Phase 1 of the update order: interpretation, before any psychology
        # reacts to it (spec 9.5). A failed interpretation degrades the run
        # rather than stopping it.
        interpretation = Interpretation()
        if self._interpreter is not None:
            try:
                interpretation = await self._interpreter.interpret(event, snapshot)
            except Exception as exc:  # noqa: BLE001 - recorded, then degraded
                logger.exception(
                    "interpretation failed run_id=%s event_id=%s", run.run_id, event.event_id
                )
                await asyncio.to_thread(
                    self._record_failure, run, event, "interpretation_failed", repr(exc)
                )

        view = RunView(
            snapshot=snapshot,
            interpretation=interpretation,
            run_id=run.run_id,
            mode=run_mode,
        )

        try:
            dispatch = await self._dispatcher.dispatch(event, view, run_id=run.run_id)
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
                interpretation=interpretation,
                snapshot=snapshot,
                error=repr(exc),
            )

        arbitration = self._arbitrator.arbitrate(dispatch.proposals, snapshot)

        try:
            commit = await asyncio.to_thread(self._commit, run, event, dispatch, arbitration)
        except ConcurrentStateWriteError as exc:
            # Patch spec 7.2: the snapshot went stale while this run was
            # thinking. Nothing was committed, so nothing is inconsistent —
            # the work simply has to be redone against what is true now.
            await asyncio.to_thread(self._conflict_run, run, event, exc)
            return ProcessingOutcome(
                run=run,
                event=event,
                status="conflict",
                newly_stored=newly_stored,
                dispatch=dispatch,
                arbitration=arbitration,
                commit=None,
                interpretation=interpretation,
                snapshot=snapshot,
                error=repr(exc),
                commit_attempt=commit_attempt,
                retry_of_run_id=retry_of,
            )
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
                interpretation=interpretation,
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
            interpretation=interpretation,
            snapshot=snapshot,
            commit_attempt=commit_attempt,
            retry_of_run_id=retry_of,
        )

    # --- synchronous units of work ----------------------------------------
    def _open_run(
        self,
        event: Event,
        mode: RuntimeMode,
        retry_of: str | None = None,
        commit_attempt: int = 1,
    ) -> tuple[RunContext, StateSnapshot]:
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
                # Patch spec 7.5: the retry chain and the snapshot this run
                # was built against are both answerable afterwards.
                retry_of_run_id=retry_of,
                commit_attempt=commit_attempt,
                snapshot_fingerprint=snapshot.fingerprint(),
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

    def _conflict_run(
        self, run: RunContext, event: Event, error: BaseException
    ) -> None:
        """Close a run that lost a version race (patch spec 7.2, 7.5)."""
        now = self._clock.now()
        with self._db.transaction():
            self._runs.record_conflict(run.run_id)
            self._failures.record(
                FailureRecord(
                    failure_type="state",
                    component=COMPONENT,
                    reason_code="commit_conflict",
                    severity="warning",
                    run_id=run.run_id,
                    event_id=event.event_id,
                    detail={"error": repr(error)[:1000]},
                ),
                now=now,
            )
            self._runs.finish(
                run_id=run.run_id, status="conflicted", now=now, error=repr(error)[:500]
            )

    def _record_failure(
        self, run: RunContext, event: Event, reason_code: str, error: str
    ) -> None:
        """Record a degradation without ending the run."""
        self._failures.record(
            FailureRecord(
                failure_type="behavior",
                component=COMPONENT,
                reason_code=reason_code,
                severity="warning",
                run_id=run.run_id,
                event_id=event.event_id,
                detail={"error": error[:1000]},
            ),
            now=self._clock.now(),
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
