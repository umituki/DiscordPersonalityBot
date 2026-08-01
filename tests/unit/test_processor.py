"""End-to-end Phase 1 pipeline (spec 35 Phase 1 "Done")."""

from __future__ import annotations

import pytest

from app.orchestrator.processor import EventProcessor
from app.state.proposal import StateChangeProposal
from app.versioning.manifest import ManifestService, RuntimeManifest


@pytest.fixture
def manifest_id(manifest_repo, clock) -> str:
    """A real manifest row: processing_runs references it by foreign key."""
    service = ManifestService(manifest_repo, clock=clock)
    return service.ensure(
        RuntimeManifest(config_version=1, event_schema_version=1, code_commit_hash="test")
    ).manifest_id


@pytest.fixture
def processor(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures, clock,
    manifest_id,
) -> EventProcessor:
    return EventProcessor(
        db=db,
        event_store=event_store,
        dispatcher=dispatcher,
        snapshots=snapshots,
        arbitrator=arbitrator,
        committer=committer,
        runs=runs,
        failures=failures,
        manifest_id=manifest_id,
        mode="test",
        clock=clock,
    )


def emotion_proposal(event, _snapshot):
    return (
        StateChangeProposal.adjust(
            source_event_id=event.event_id,
            source_module="emotion_engine",
            target_domain="emotion",
            target_key="joy",
            magnitude=0.2,
            reason_codes=("user_greeted_yui",),
        ),
    )


async def test_event_flows_to_state(
    processor, bus, state_repo, make_event, recording_subscriber, clock
) -> None:
    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    bus.register(recording_subscriber("emotion_engine", proposal_factory=emotion_proposal))
    event = make_event()

    outcome = await processor.process(event)

    assert outcome.status == "committed"
    assert outcome.newly_stored is True
    assert outcome.committed_targets == ("emotion.joy",)
    assert state_repo.get("emotion", "joy").value == pytest.approx(0.5)


async def test_run_is_recorded_with_manifest_and_snapshot(
    processor, bus, runs, make_event, recording_subscriber, snapshot_repo, manifest_id
) -> None:
    bus.register(recording_subscriber("emotion_engine"))
    outcome = await processor.process(make_event())

    row = runs.get(outcome.run.run_id)
    assert row["runtime_manifest_id"] == manifest_id
    assert row["state_snapshot_id"] == outcome.run.state_snapshot_id
    assert row["mode"] == "test"
    assert snapshot_repo.count() == 1


async def test_rejected_proposals_leave_state_untouched(
    processor, bus, state_repo, make_event, recording_subscriber, failures
) -> None:
    def bad_proposal(event, _snapshot):
        return (
            StateChangeProposal.adjust(
                source_event_id=event.event_id,
                source_module="emotion_engine",
                target_domain="personality",
                target_key="openness",
                magnitude=0.5,
            ),
        )

    bus.register(recording_subscriber("emotion_engine", proposal_factory=bad_proposal))
    outcome = await processor.process(make_event())

    assert outcome.status == "rejected"
    assert state_repo.get("personality", "openness") is None
    assert failures.for_run(outcome.run.run_id)


async def test_derived_events_are_persisted_with_the_commit(
    processor, bus, event_store, make_event
) -> None:
    from app.events.bus import SubscriberResult
    from tests.conftest import SignalPayload

    class Deriving:
        name = "world_service"

        async def handle(self, event, snapshot):
            follow_up = event.child(
                event_type="TEST_SIGNAL",
                category="internal",
                actor_type="yui",
                source_type="unit_test",
                payload=SignalPayload(label="derived"),
            )
            return SubscriberResult(events=(follow_up,))

    bus.register(Deriving())
    root = make_event()
    await processor.process(root)

    chain = event_store.chain(root.event_id)
    assert len(chain) == 2
    assert chain[1].parent_event_id == root.event_id


async def test_no_subscribers_is_a_clean_outcome(processor, make_event, runs) -> None:
    outcome = await processor.process(make_event())
    assert outcome.status == "no_subscribers"
    assert runs.get(outcome.run.run_id)["status"] == "rejected"


async def test_commit_failure_marks_the_run_failed(
    processor, bus, state_repo, runs, failures, make_event, recording_subscriber, clock
) -> None:
    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )

    def racing(event, _snapshot):
        # Move the value out from under the snapshot the run is holding.
        state_repo.write_value(
            domain="emotion", key="joy", value=0.35, confidence=None, now=clock.now(),
            run_id=None, event_id=None, expected_version=1,
        )
        return emotion_proposal(event, _snapshot)

    bus.register(recording_subscriber("emotion_engine", proposal_factory=racing))
    outcome = await processor.process(make_event())

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert runs.get(outcome.run.run_id)["status"] == "failed"
    assert state_repo.get("emotion", "joy").value == pytest.approx(0.35)
    assert any(row["reason_code"] == "commit_failed" for row in failures.for_run(outcome.run.run_id))
