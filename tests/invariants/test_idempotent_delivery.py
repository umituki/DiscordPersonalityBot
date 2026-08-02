"""INVARIANT: the same event delivered twice produces one effect.

Spec 34.3: ``same Event double delivery → idempotent result``. A retried or
replayed event must not double-apply psychology.
"""

from __future__ import annotations

import pytest

from app.orchestrator.run_view import RunView

from app.orchestrator.processor import EventProcessor
from app.state.proposal import StateChangeProposal

pytestmark = pytest.mark.invariant


@pytest.fixture
def processor(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures, clock
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
        mode="test",
        clock=clock,
    )


def joy_proposal(event, _snapshot):
    return (
        StateChangeProposal.adjust(
            source_event_id=event.event_id,
            source_module="emotion_engine",
            target_domain="emotion",
            target_key="joy",
            magnitude=0.2,
        ),
    )


async def test_redelivered_event_does_not_move_state_twice(
    processor, bus, state_repo, deliveries, make_event, recording_subscriber, clock
) -> None:
    state_repo.write_value(
        domain="emotion", key="joy", value=0.3, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    subscriber = recording_subscriber("emotion_engine", proposal_factory=joy_proposal)
    bus.register(subscriber)
    event = make_event()

    first = await processor.process(event)
    second = await processor.process(event)

    assert first.status == "committed"
    assert second.status == "rejected"  # nothing left to apply
    assert subscriber.calls == [event.event_id]  # handled exactly once
    assert state_repo.get("emotion", "joy").value == pytest.approx(0.5)
    assert state_repo.change_count() == 1
    assert deliveries.status_of(event.event_id, "emotion_engine") == "completed"


async def test_event_row_is_written_once(processor, bus, event_store, make_event, recording_subscriber) -> None:
    bus.register(recording_subscriber("emotion_engine"))
    event = make_event()

    first = await processor.process(event)
    second = await processor.process(event)

    assert first.newly_stored is True
    assert second.newly_stored is False
    assert event_store.count() == 1


async def test_delivery_not_completed_until_commit(
    bus, dispatcher, snapshots, deliveries, event_store, make_event, recording_subscriber
) -> None:
    """A crash between handling and commit must leave the delivery replayable."""
    subscriber = recording_subscriber("emotion_engine", proposal_factory=joy_proposal)
    bus.register(subscriber)
    event = make_event()
    event_store.append(event)

    # Dispatch only — this simulates the process dying before commit.
    result = await dispatcher.dispatch(event, RunView(snapshot=snapshots.capture(persist=False)))

    assert result.delivery_ids  # the committer would have completed these
    assert deliveries.status_of(event.event_id, "emotion_engine") == "pending"
    assert deliveries.attempts_of(event.event_id, "emotion_engine") == 1

    # After restart the same event is offered again and is processed.
    second = await dispatcher.dispatch(event, RunView(snapshot=snapshots.capture(persist=False)))
    assert [outcome.status for outcome in second.outcomes] == ["handled"]
    assert len(subscriber.calls) == 2


async def test_repeated_failures_become_permanent(
    bus, dispatcher, snapshots, deliveries, event_store, failures, make_event, recording_subscriber
) -> None:
    bus.register(recording_subscriber("emotion_engine", raises=RuntimeError("still broken")))
    event = make_event()
    event_store.append(event)

    for _ in range(4):
        await dispatcher.dispatch(event, RunView(snapshot=snapshots.capture(persist=False)))

    assert deliveries.status_of(event.event_id, "emotion_engine") == "failed_permanent"
    # A permanently failed delivery is not silently retried forever.
    outcome = await dispatcher.dispatch(event, RunView(snapshot=snapshots.capture(persist=False)))
    assert [item.status for item in outcome.outcomes] == ["skipped"]
