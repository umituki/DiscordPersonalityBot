"""Bus routing and dispatch (spec 8.1, 28.3)."""

from __future__ import annotations

import asyncio

import pytest

from app.events.bus import EventBus, SubscriberResult, SubscriptionError
from app.events.dispatcher import EventDispatcher
from app.events.store import EventStore
from app.orchestrator.run_view import RunView
from app.state.proposal import StateChangeProposal
from app.state.snapshot import SnapshotService
from app.storage.repositories.deliveries import DeliveryRepository
from app.storage.repositories.failures import FailureRepository


def _view(snapshots: SnapshotService) -> RunView:
    """Engines are handed a RunView, never the live database (spec 9.2)."""
    return RunView(snapshot=snapshots.capture(persist=False))


def test_duplicate_registration_rejected(bus: EventBus, recording_subscriber) -> None:
    bus.register(recording_subscriber("a"))
    with pytest.raises(SubscriptionError):
        bus.register(recording_subscriber("a"))


def test_unnamed_subscriber_rejected(bus: EventBus) -> None:
    class Anonymous:
        name = ""

        async def handle(self, event, snapshot):
            return SubscriberResult()

    with pytest.raises(SubscriptionError):
        bus.register(Anonymous())


def test_filters_by_event_type_and_category(bus: EventBus, make_event, recording_subscriber) -> None:
    typed = recording_subscriber("typed")
    categorised = recording_subscriber("categorised")
    bus.register(typed, event_types=["OTHER_TYPE"])
    bus.register(categorised, categories=["social"])

    event = make_event(category="social")
    names = {item.subscriber.name for item in bus.subscriptions_for(event)}
    assert names == {"categorised"}


def test_subscribers_run_in_declared_order(bus: EventBus, make_event, recording_subscriber) -> None:
    bus.register(recording_subscriber("late"), order=200)
    bus.register(recording_subscriber("early"), order=10)
    assert bus.names() == ("early", "late")


async def test_dispatch_collects_proposals(
    bus: EventBus,
    dispatcher: EventDispatcher,
    event_store: EventStore,
    snapshots: SnapshotService,
    make_event,
    recording_subscriber,
) -> None:
    event = make_event()
    event_store.append(event)
    proposal = StateChangeProposal.adjust(
        source_event_id=event.event_id,
        source_module="emotion_engine",
        target_domain="emotion",
        target_key="joy",
        magnitude=0.2,
    )
    subscriber = recording_subscriber("emotion_engine", (proposal,))
    bus.register(subscriber)

    result = await dispatcher.dispatch(event, _view(snapshots))

    assert result.proposals == (proposal,)
    assert [outcome.status for outcome in result.outcomes] == ["handled"]
    assert len(result.delivery_ids) == 1
    assert subscriber.calls == [event.event_id]


async def test_failing_subscriber_is_isolated_and_recorded(
    bus: EventBus,
    dispatcher: EventDispatcher,
    event_store: EventStore,
    snapshots: SnapshotService,
    failures: FailureRepository,
    deliveries: DeliveryRepository,
    make_event,
    recording_subscriber,
) -> None:
    event = make_event()
    event_store.append(event)
    proposal = StateChangeProposal.adjust(
        source_event_id=event.event_id,
        source_module="mood_engine",
        target_domain="mood",
        target_key="valence",
        magnitude=0.1,
    )
    bus.register(recording_subscriber("broken", raises=RuntimeError("engine exploded")))
    bus.register(recording_subscriber("mood_engine", (proposal,)))

    result = await dispatcher.dispatch(event, _view(snapshots))

    assert result.degraded
    assert {outcome.status for outcome in result.outcomes} == {"failed", "handled"}
    # The healthy subscriber still contributed.
    assert result.proposals == (proposal,)
    # The failure is recorded, not swallowed.
    recorded = failures.recent()
    assert any(row["reason_code"] == "subscriber_exception" for row in recorded)
    assert deliveries.status_of(event.event_id, "broken") == "failed_retryable"
    # A failed subscriber's delivery is not handed to the committer.
    assert len(result.delivery_ids) == 1


async def test_slow_subscriber_times_out(
    bus: EventBus,
    deliveries: DeliveryRepository,
    failures: FailureRepository,
    event_store: EventStore,
    snapshots: SnapshotService,
    clock,
    make_event,
) -> None:
    class Slow:
        name = "slow_engine"

        async def handle(self, event, snapshot):
            await asyncio.sleep(5)
            return SubscriberResult()

    bus.register(Slow())
    dispatcher = EventDispatcher(
        bus, deliveries, failures, clock=clock, subscriber_timeout_s=0.05
    )
    event = make_event()
    event_store.append(event)

    result = await dispatcher.dispatch(event, _view(snapshots))

    assert [outcome.status for outcome in result.outcomes] == ["timeout"]
    assert any(row["reason_code"] == "subscriber_timeout" for row in failures.recent())


async def test_invalid_subscriber_result_is_rejected(
    bus: EventBus,
    dispatcher: EventDispatcher,
    event_store: EventStore,
    snapshots: SnapshotService,
    failures: FailureRepository,
    make_event,
) -> None:
    class Sloppy:
        name = "sloppy"

        async def handle(self, event, snapshot):
            return {"proposals": []}

    bus.register(Sloppy())
    event = make_event()
    event_store.append(event)

    result = await dispatcher.dispatch(event, _view(snapshots))

    assert [outcome.status for outcome in result.outcomes] == ["failed"]
    assert result.proposals == ()
    assert any(row["reason_code"] == "invalid_subscriber_result" for row in failures.recent())


async def test_subscriber_reads_the_snapshot_not_the_database(
    bus: EventBus,
    dispatcher: EventDispatcher,
    event_store: EventStore,
    snapshots: SnapshotService,
    state_repo,
    clock,
    make_event,
    recording_subscriber,
) -> None:
    state_repo.write_value(
        domain="relationship", key="trust", value=0.5, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=None,
    )
    subscriber = recording_subscriber("relationship_engine")
    bus.register(subscriber)
    event = make_event()
    event_store.append(event)
    snapshot = _view(snapshots)

    # State moves after the snapshot was taken.
    state_repo.write_value(
        domain="relationship", key="trust", value=0.9, confidence=None, now=clock.now(),
        run_id=None, event_id=None, expected_version=1,
    )
    await dispatcher.dispatch(event, snapshot)

    seen = subscriber.seen_snapshots[0]
    assert seen.number_of("relationship", "trust") == 0.5
