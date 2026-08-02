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


async def test_a_commit_conflict_is_reprocessed_not_dropped(
    processor, bus, state_repo, runs, failures, make_event, recording_subscriber, clock
) -> None:
    """Patch spec 7.1-7.2: a stale snapshot costs a retry, not the USER's event.

    The 2026-08-02 run lost a USER message's entire psychological effect this
    way: a background world tick moved a value while the run was thinking, and
    the commit failed with zero state changes.
    """
    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    races = {"remaining": 1}

    def racing(event, snapshot_view):
        # A background writer moves the value once, under the first attempt.
        if races["remaining"]:
            races["remaining"] -= 1
            state_repo.write_value(
                domain="emotion", key="joy", value=0.35, confidence=None, now=clock.now(),
                run_id=None, event_id=None, expected_version=1,
            )
        return emotion_proposal(event, snapshot_view)

    bus.register(recording_subscriber("emotion_engine", proposal_factory=racing))
    outcome = await processor.process(make_event())

    # The reprocess committed against what was actually true.
    assert outcome.status == "committed"
    assert outcome.commit_attempt == 2
    assert outcome.retry_of_run_id is not None
    assert runs.get(outcome.run.run_id)["status"] == "committed"

    # The first run is recorded as conflicted, not failed, and the retry links
    # back to it (patch spec 7.5).
    first = runs.get(outcome.retry_of_run_id)
    assert first["status"] == "conflicted"
    assert first["conflict_count"] == 1
    assert runs.get(outcome.run.run_id)["retry_of_run_id"] == outcome.retry_of_run_id
    assert any(
        row["reason_code"] == "commit_conflict"
        for row in failures.for_run(outcome.retry_of_run_id)
    )


async def test_a_conflict_retry_uses_a_fresh_snapshot(
    processor, bus, state_repo, make_event, recording_subscriber, clock
) -> None:
    """Patch spec 7.3: stale proposals are not re-committed, they are redone."""
    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    seen: list[float | None] = []
    races = {"remaining": 1}

    def racing(event, snapshot_view):
        seen.append(snapshot_view.number("emotion", "joy"))
        if races["remaining"]:
            races["remaining"] -= 1
            state_repo.write_value(
                domain="emotion", key="joy", value=0.35, confidence=None, now=clock.now(),
                run_id=None, event_id=None, expected_version=1,
            )
        return emotion_proposal(event, snapshot_view)

    bus.register(recording_subscriber("emotion_engine", proposal_factory=racing))
    await processor.process(make_event())

    # The second evaluation saw the value the background writer left behind.
    assert len(seen) == 2
    assert seen[0] == pytest.approx(0.3)
    assert seen[1] == pytest.approx(0.35)


async def test_repeated_conflicts_stop_rather_than_loop(
    processor, bus, state_repo, runs, make_event, recording_subscriber, clock
) -> None:
    """Patch spec 7.2: bounded. A permanently racing writer must not spin."""
    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    attempts = {"count": 0}

    def always_racing(event, snapshot_view):
        attempts["count"] += 1
        current = state_repo.get("emotion", "joy")
        state_repo.write_value(
            domain="emotion", key="joy", value=0.3 + 0.01 * attempts["count"],
            confidence=None, now=clock.now(), run_id=None, event_id=None,
            expected_version=current.version,
        )
        return emotion_proposal(event, snapshot_view)

    bus.register(recording_subscriber("emotion_engine", proposal_factory=always_racing))
    outcome = await processor.process(make_event())

    assert outcome.conflicted is True
    assert attempts["count"] == 2  # the initial run plus exactly one retry
    assert runs.get(outcome.run.run_id)["status"] == "conflicted"


async def test_a_hard_commit_failure_still_fails_the_run(
    processor, bus, state_repo, runs, failures, make_event, recording_subscriber,
    monkeypatch, clock,
) -> None:
    """A disk error is not a version race and must not be retried as one."""
    import sqlite3

    from app.storage.repositories.state import StateRepository

    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(StateRepository, "record_change", explode)
    bus.register(recording_subscriber("emotion_engine", proposal_factory=emotion_proposal))
    outcome = await processor.process(make_event())

    assert outcome.status == "failed"
    assert outcome.conflicted is False
    assert runs.get(outcome.run.run_id)["status"] == "failed"
    assert any(
        row["reason_code"] == "commit_failed" for row in failures.for_run(outcome.run.run_id)
    )


# --- the state an action is generated from (patch spec 9) -------------------
async def test_the_post_commit_snapshot_matches_what_was_written(
    processor, bus, state_repo, make_event, recording_subscriber, clock
) -> None:
    """Patch spec 9: ``current USER Eventのcommit後stateだけを使用``.

    S0 is what the run *read*; a reply is spoken after the event was taken in,
    so it must see what the event produced.
    """
    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    bus.register(recording_subscriber("emotion_engine", proposal_factory=emotion_proposal))

    outcome = await processor.process(make_event())

    assert outcome.snapshot.number_of("emotion", "joy") == pytest.approx(0.3)
    post = outcome.post_commit_snapshot
    assert post.number_of("emotion", "joy") == pytest.approx(0.5)
    assert post.number_of("emotion", "joy") == state_repo.get("emotion", "joy").value


async def test_a_run_that_wrote_nothing_reads_its_own_snapshot(
    processor, bus, make_event, recording_subscriber
) -> None:
    """Nothing was committed, so S0 *is* current state — not a stale view."""
    bus.register(recording_subscriber("emotion_engine"))
    outcome = await processor.process(make_event())

    assert outcome.committed_targets == ()
    assert outcome.post_commit_snapshot is outcome.snapshot
