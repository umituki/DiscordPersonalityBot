"""Event store behaviour (spec 8.3, 36 completion definition)."""

from __future__ import annotations

from app.events.model import EventInvalidatedPayload
from app.events.store import EventStore
from app.storage.database import Database
from app.storage.repositories.events import EventRepository
from tests.conftest import SignalPayload


def test_append_and_read_back(event_store: EventStore, make_event) -> None:
    event = make_event(payload=SignalPayload(label="hello", strength=0.5))
    assert event_store.append(event) is True

    loaded = event_store.get(event.event_id)
    assert loaded is not None
    assert loaded.event_id == event.event_id
    assert loaded.payload.label == "hello"
    assert loaded.occurred_at == event.occurred_at
    assert loaded.origin == "real_discord"


def test_append_is_idempotent(event_store: EventStore, make_event) -> None:
    event = make_event()
    assert event_store.append(event) is True
    assert event_store.append(event) is False
    assert event_store.count() == 1


def test_events_survive_a_reopen(db: Database, event_store: EventStore, make_event) -> None:
    event = make_event()
    event_store.append(event)
    path = db.path
    db.close()

    with Database(path) as reopened:
        reloaded = EventStore(reopened, EventRepository(reopened)).get(event.event_id)
    assert reloaded is not None
    assert reloaded.payload.label == event.payload.label


def test_chain_orders_by_occurrence(event_store: EventStore, make_event, clock) -> None:
    root = make_event()
    event_store.append(root)
    clock.advance(seconds=5)
    child = root.child(
        event_type="TEST_SIGNAL",
        category="internal",
        actor_type="yui",
        source_type="test",
        payload=SignalPayload(label="second"),
        clock=clock,
    )
    event_store.append(child)

    chain = event_store.chain(root.event_id)
    assert [item.event_id for item in chain] == [root.event_id, child.event_id]


def test_invalidation_appends_and_preserves_original(event_store: EventStore, make_event) -> None:
    event = make_event()
    event_store.append(event)

    correction = event_store.invalidate(event, reason_code="duplicate_ingest")

    assert event_store.get(event.event_id) is not None  # original still there
    assert isinstance(correction.payload, EventInvalidatedPayload)
    assert correction.payload.invalidated_event_id == event.event_id
    assert event_store.invalidated_ids(event.root_event_id) == {event.event_id}


def test_reinterpretation_does_not_touch_the_original(event_store: EventStore, make_event) -> None:
    event = make_event(payload=SignalPayload(label="original"))
    event_store.append(event)

    event_store.reinterpret(event, reason_code="later_understanding")

    stored = event_store.get(event.event_id)
    assert stored is not None
    assert stored.payload.label == "original"
    assert len(event_store.chain(event.root_event_id)) == 2


def test_append_all_is_atomic(event_store: EventStore, make_event) -> None:
    first, second = make_event(), make_event()
    inserted, duplicates = event_store.append_all([first, second, first])
    assert (inserted, duplicates) == (2, 1)
    assert event_store.count() == 2


def test_recent_filters_by_origin(event_store: EventStore, make_event) -> None:
    real = make_event()
    simulated = make_event(origin="simulated_past", actor_type="npc")
    event_store.append_all([real, simulated])

    assert [e.event_id for e in event_store.recent(origin="simulated_past")] == [
        simulated.event_id
    ]
    assert [e.event_id for e in event_store.recent(origin="real_discord")] == [real.event_id]
