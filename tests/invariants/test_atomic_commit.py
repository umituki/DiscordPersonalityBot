"""INVARIANT: a run commits everything or nothing (spec 9.5, storage rules).

Multi-domain state, its history rows, the failure records and the delivery
bookkeeping share one transaction. A failure anywhere leaves the database
exactly as it was, so a partially-updated psychology is impossible.
"""

from __future__ import annotations

import pytest

from app.state.arbitrator import ArbitrationResult
from app.state.proposal import StateChangeProposal
from app.storage.repositories.state import ConcurrentStateWriteError

pytestmark = pytest.mark.invariant


@pytest.fixture
def prepared(db, event_store, runs, state_repo, snapshots, make_event, clock):
    event = make_event()
    event_store.append(event)
    run_id = "run_ATOMIC"
    runs.start(
        run_id=run_id, root_event_id=event.event_id, snapshot_id=None, manifest_id=None,
        mode="test", priority=event.priority, now=clock.now(),
    )
    for domain, key, value in (
        ("emotion", "joy", 0.4),
        ("mood", "valence", 0.5),
        ("needs", "relatedness", 0.5),
    ):
        state_repo.write_value(
            domain=domain, key=key, value=value, confidence=None, now=clock.now(),
            run_id=None, event_id=None, expected_version=None,
        )
    return event, run_id, snapshots.capture(root_event_id=event.event_id, run_id=run_id)


def multi_domain_proposals(event) -> list[StateChangeProposal]:
    return [
        StateChangeProposal.adjust(
            source_event_id=event.event_id, source_module="emotion_engine",
            target_domain="emotion", target_key="joy", magnitude=0.1,
        ),
        StateChangeProposal.adjust(
            source_event_id=event.event_id, source_module="mood_engine",
            target_domain="mood", target_key="valence", magnitude=0.05,
        ),
        StateChangeProposal.adjust(
            source_event_id=event.event_id, source_module="need_engine",
            target_domain="needs", target_key="relatedness", magnitude=0.1,
        ),
    ]


def test_multi_domain_commit_is_all_or_nothing(
    prepared, arbitrator, committer, state_repo, clock
) -> None:
    event, run_id, snapshot = prepared
    result = arbitrator.arbitrate(multi_domain_proposals(event), snapshot)
    assert len(result.accepted) == 3

    # A concurrent writer invalidates the middle change's expected version.
    state_repo.write_value(
        domain="mood", key="valence", value=0.55, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=1,
    )

    with pytest.raises(ConcurrentStateWriteError):
        committer.commit(run_id=run_id, root_event_id=event.event_id, result=result)

    # The earlier domain in the commit order was rolled back too.
    assert state_repo.get("emotion", "joy").value == pytest.approx(0.4)
    assert state_repo.get("needs", "relatedness").value == pytest.approx(0.5)
    assert state_repo.change_count() == 0


def test_failed_commit_does_not_consume_deliveries(
    prepared, arbitrator, committer, deliveries, state_repo, clock
) -> None:
    event, run_id, snapshot = prepared
    decision = deliveries.begin_attempt(event.event_id, "emotion_engine", now=clock.now())
    result = arbitrator.arbitrate(multi_domain_proposals(event), snapshot)
    state_repo.write_value(
        domain="mood", key="valence", value=0.55, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=1,
    )

    with pytest.raises(ConcurrentStateWriteError):
        committer.commit(
            run_id=run_id,
            root_event_id=event.event_id,
            result=result,
            delivery_ids=[decision.delivery_id],
        )

    assert deliveries.status_of(event.event_id, "emotion_engine") == "pending"


def test_failed_commit_does_not_leave_failure_rows(
    prepared, arbitrator, committer, failures, state_repo, clock
) -> None:
    event, run_id, snapshot = prepared
    proposals = multi_domain_proposals(event)
    proposals.append(
        StateChangeProposal.adjust(
            source_event_id=event.event_id, source_module="emotion_engine",
            target_domain="relationship", target_key="trust", magnitude=0.01,
        )
    )
    result = arbitrator.arbitrate(proposals, snapshot)
    assert result.rejected  # the cross-domain write

    state_repo.write_value(
        domain="mood", key="valence", value=0.55, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=1,
    )
    with pytest.raises(ConcurrentStateWriteError):
        committer.commit(run_id=run_id, root_event_id=event.event_id, result=result)

    assert failures.for_run(run_id) == []


def test_empty_result_commits_cleanly(prepared, committer, runs, state_repo) -> None:
    event, run_id, _snapshot = prepared
    committer.commit(
        run_id=run_id, root_event_id=event.event_id, result=ArbitrationResult()
    )
    assert runs.get(run_id)["status"] == "rejected"
    assert state_repo.change_count() == 0
