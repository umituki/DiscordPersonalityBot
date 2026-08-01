"""Commit and snapshot behaviour (spec 9.2, 9.5, 31)."""

from __future__ import annotations

import pytest

from app.state.arbitrator import StateArbitrator
from app.state.committer import StateCommitter
from app.state.proposal import StateChangeProposal
from app.state.snapshot import SnapshotService
from app.state.value import UnknownValueError
from app.storage.repositories.state import ConcurrentStateWriteError


@pytest.fixture
def open_run(db, event_store, runs, make_event, clock):
    """Append a root event and open a run for it."""

    def factory():
        event = make_event()
        event_store.append(event)
        run_id = "run_TESTRUN"
        runs.start(
            run_id=run_id,
            root_event_id=event.event_id,
            snapshot_id=None,
            manifest_id=None,
            mode="test",
            priority=event.priority,
            now=clock.now(),
        )
        return event, run_id

    return factory


def seed(state_repo, clock, domain: str, key: str, value) -> None:
    state_repo.write_value(
        domain=domain, key=key, value=value, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )


def test_commit_writes_value_history_and_run(
    state_repo,
    arbitrator: StateArbitrator,
    committer: StateCommitter,
    snapshots,
    open_run,
    runs,
    clock,
) -> None:
    event, run_id = open_run()
    seed(state_repo, clock, "emotion", "joy", 0.4)
    snapshot = snapshots.capture(root_event_id=event.event_id, run_id=run_id)

    proposal = StateChangeProposal.adjust(
        source_event_id=event.event_id,
        source_module="emotion_engine",
        target_domain="emotion",
        target_key="joy",
        magnitude=0.25,
        reason_codes=("positive_feedback",),
    )
    result = arbitrator.arbitrate([proposal], snapshot)
    commit = committer.commit(run_id=run_id, root_event_id=event.event_id, result=result)

    stored = state_repo.get("emotion", "joy")
    assert stored.value == pytest.approx(0.65)
    assert stored.version == 2
    assert stored.updated_by_run_id == run_id

    history = state_repo.change_history("emotion", "joy")
    assert len(history) == 1
    assert history[0]["previous_value_json"] == "0.4"
    assert history[0]["source_module"] == "emotion_engine"

    assert commit.committed_count == 1
    assert runs.get(run_id)["status"] == "committed"


def test_first_write_creates_the_key(
    state_repo, arbitrator, committer, snapshots, open_run
) -> None:
    event, run_id = open_run()
    snapshot = snapshots.capture(root_event_id=event.event_id, run_id=run_id)
    proposal = StateChangeProposal.set_value(
        source_event_id=event.event_id,
        source_module="world_service",
        target_domain="world",
        target_key="current_activity",
        value="reading",
    )
    result = arbitrator.arbitrate([proposal], snapshot)
    committer.commit(run_id=run_id, root_event_id=event.event_id, result=result)

    stored = state_repo.get("world", "current_activity")
    assert stored.value == "reading"
    assert stored.version == 1


def test_rejections_are_recorded_as_failures(
    arbitrator, committer, snapshots, open_run, failures, runs, state_repo
) -> None:
    event, run_id = open_run()
    snapshot = snapshots.capture(root_event_id=event.event_id, run_id=run_id)
    proposal = StateChangeProposal.adjust(
        source_event_id=event.event_id,
        source_module="emotion_engine",
        target_domain="relationship",  # not its state
        target_key="trust",
        magnitude=0.5,
    )
    result = arbitrator.arbitrate([proposal], snapshot)
    committer.commit(run_id=run_id, root_event_id=event.event_id, result=result)

    recorded = failures.for_run(run_id)
    assert len(recorded) == 1
    assert recorded[0]["reason_code"] == "not_state_owner"
    assert runs.get(run_id)["status"] == "rejected"
    assert state_repo.get("relationship", "trust") is None


def test_stale_snapshot_version_aborts_the_commit(
    state_repo, arbitrator, committer, snapshots, open_run, clock
) -> None:
    event, run_id = open_run()
    seed(state_repo, clock, "mood", "valence", 0.5)
    snapshot = snapshots.capture(root_event_id=event.event_id, run_id=run_id)

    # Something else advanced the value after the snapshot.
    state_repo.write_value(
        domain="mood", key="valence", value=0.6, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=1,
    )

    result = arbitrator.arbitrate(
        [
            StateChangeProposal.adjust(
                source_event_id=event.event_id,
                source_module="mood_engine",
                target_domain="mood",
                target_key="valence",
                magnitude=0.1,
            )
        ],
        snapshot,
    )
    with pytest.raises(ConcurrentStateWriteError):
        committer.commit(run_id=run_id, root_event_id=event.event_id, result=result)

    # The concurrent value stands; nothing was half-applied.
    assert state_repo.get("mood", "valence").value == pytest.approx(0.6)
    assert state_repo.change_count() == 0


def test_snapshot_reports_unknown_rather_than_zero(snapshots: SnapshotService) -> None:
    snapshot = snapshots.capture(persist=False)
    assert snapshot.get("relationship", "trust") is None
    assert snapshot.number_of("relationship", "trust") is None
    assert snapshot.number_of("relationship", "trust", default=0.0) == 0.0
    with pytest.raises(UnknownValueError):
        snapshot.require("relationship", "trust")


def test_snapshot_round_trips_through_storage(
    state_repo, snapshots: SnapshotService, clock
) -> None:
    seed(state_repo, clock, "needs", "relatedness", 0.42)
    captured = snapshots.capture(root_event_id="evt_ROOT")
    restored = snapshots.load(captured.snapshot_id, created_at=captured.created_at)

    assert restored is not None
    assert restored.number_of("needs", "relatedness") == pytest.approx(0.42)
    assert restored.version_of("needs", "relatedness") == 1


def test_snapshot_is_read_only(state_repo, snapshots, clock) -> None:
    seed(state_repo, clock, "needs", "autonomy", 0.5)
    snapshot = snapshots.capture(persist=False)
    with pytest.raises(TypeError):
        snapshot._values[("needs", "autonomy")] = None  # type: ignore[index]
