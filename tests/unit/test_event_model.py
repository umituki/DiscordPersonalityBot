"""Event domain model (spec 8.2, 8.3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.clock import FixedClock
from app.events.model import (
    Event,
    EventPayload,
    SystemStartedPayload,
    build_payload,
    register_payload,
)
from tests.conftest import SignalPayload


def test_create_defaults_root_to_itself(make_event) -> None:
    event = make_event()
    assert event.is_root
    assert event.root_event_id == event.event_id
    assert event.parent_event_id is None


def test_child_keeps_root_and_links_parent(make_event) -> None:
    root = make_event()
    child = root.child(
        event_type="TEST_SIGNAL",
        category="internal",
        actor_type="yui",
        source_type="test",
        payload=SignalPayload(label="derived"),
    )
    assert child.root_event_id == root.root_event_id
    assert child.parent_event_id == root.event_id
    assert child.origin == root.origin


def test_events_are_frozen(make_event) -> None:
    event = make_event()
    with pytest.raises(ValidationError):
        event.event_type = "SOMETHING_ELSE"  # type: ignore[misc]


def test_occurred_at_and_recorded_at_are_independent(clock: FixedClock) -> None:
    past = clock.now() - timedelta(days=365 * 10)
    event = Event.create(
        event_type="TEST_SIGNAL",
        category="social",
        actor_type="npc",
        source_type="past_simulation",
        origin="simulated_past",
        payload=SignalPayload(),
        occurred_at=past,
        clock=clock,
    )
    assert event.occurred_at == past
    assert event.recorded_at == clock.now()
    assert event.is_simulated


def test_naive_timestamps_rejected(make_event) -> None:
    with pytest.raises(ValidationError):
        Event.create(
            event_type="TEST_SIGNAL",
            category="social",
            actor_type="user",
            source_type="test",
            origin="real_discord",
            payload=SignalPayload(),
            occurred_at=datetime(2026, 1, 1, 12, 0),
        )


def test_event_type_must_be_upper_snake(make_event) -> None:
    with pytest.raises(ValidationError):
        make_event(event_type="lower_case")


def test_payload_must_match_event_type(clock: FixedClock) -> None:
    with pytest.raises(ValidationError, match="belongs to"):
        Event.create(
            event_type="TEST_SIGNAL",
            category="system",
            actor_type="system",
            source_type="test",
            origin="system",
            payload=SystemStartedPayload(manifest_id="man_1", schema_version_applied=1, mode="test"),
            clock=clock,
        )


def test_unknown_payload_stays_explicitly_untyped() -> None:
    payload = build_payload("SOMETHING_FROM_THE_FUTURE", 9, {"a": 1})
    assert payload.raw == {"a": 1}
    assert payload.declared_version == 9


def test_registering_two_models_for_one_type_fails() -> None:
    with pytest.raises(Exception):

        @register_payload("TEST_SIGNAL")
        class Duplicate(EventPayload):
            pass


def test_admin_and_system_events_are_not_delivered_to_psychology(clock: FixedClock) -> None:
    system_event = Event.create(
        event_type="SYSTEM_STARTED",
        category="system",
        actor_type="system",
        source_type="bootstrap",
        origin="system",
        payload=SystemStartedPayload(manifest_id="man_1", schema_version_applied=1, mode="test"),
        clock=clock,
    )
    assert not system_event.deliverable_to_psychology


def test_event_cannot_be_its_own_parent(make_event) -> None:
    event = make_event()
    with pytest.raises(ValidationError, match="own parent"):
        Event.model_validate(event.model_dump() | {"parent_event_id": event.event_id})


def test_a_dumped_event_validates_back_into_a_typed_payload(make_event) -> None:
    event = make_event(payload=SignalPayload(label="round trip", strength=0.25))

    restored = Event.model_validate(event.model_dump())

    assert isinstance(restored.payload, SignalPayload)
    assert restored.payload.label == "round trip"
    assert restored.payload.strength == 0.25
    assert restored == event


def test_ids_must_use_event_prefix(clock: FixedClock) -> None:
    with pytest.raises(ValidationError):
        Event.create(
            event_type="TEST_SIGNAL",
            category="social",
            actor_type="user",
            source_type="test",
            origin="real_discord",
            payload=SignalPayload(),
            event_id="user_123",
            clock=clock,
        )


def test_utc_normalisation_on_construction(clock: FixedClock) -> None:
    tokyo = timezone(timedelta(hours=9))
    event = Event.create(
        event_type="TEST_SIGNAL",
        category="social",
        actor_type="user",
        source_type="test",
        origin="real_discord",
        payload=SignalPayload(),
        occurred_at=datetime(2026, 1, 1, 21, 0, tzinfo=tokyo),
        clock=clock,
    )
    assert event.occurred_at.utcoffset() == timedelta(0)
