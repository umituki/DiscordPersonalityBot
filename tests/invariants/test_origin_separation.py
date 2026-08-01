"""INVARIANT: USER, NPC, simulated past, real Discord and admin never merge.

Spec 2.10 / 2.16 / 34.2-4. Phase 1 owns the structural half of this rule: every
event carries an immutable ``origin`` and ``actor_type``, they survive storage,
and admin/system traffic never reaches psychology subscribers (spec 8.1).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.events.model import Event, SystemStartedPayload
from tests.conftest import SignalPayload

pytestmark = pytest.mark.invariant


def test_origin_and_actor_survive_storage(event_store, make_event) -> None:
    user_event = make_event(actor_type="user", actor_id="owner", origin="real_discord")
    npc_event = make_event(actor_type="npc", actor_id="npc_haruka", origin="virtual_life")
    past_event = make_event(actor_type="npc", actor_id="npc_haruka", origin="simulated_past")
    event_store.append_all([user_event, npc_event, past_event])

    for original in (user_event, npc_event, past_event):
        stored = event_store.get(original.event_id)
        assert stored.origin == original.origin
        assert stored.actor_type == original.actor_type
        assert stored.actor_id == original.actor_id


def test_queries_can_isolate_generated_past_from_real_history(event_store, make_event) -> None:
    real = make_event(actor_type="user", origin="real_discord")
    generated = make_event(actor_type="npc", origin="simulated_past")
    event_store.append_all([real, generated])

    real_only = event_store.recent(origin="real_discord")
    assert [event.event_id for event in real_only] == [real.event_id]
    assert all(event.origin != "simulated_past" for event in real_only)


def test_simulated_past_keeps_generation_time_separate(clock, event_store) -> None:
    occurred = clock.now() - timedelta(days=365 * 12)
    event = Event.create(
        event_type="TEST_SIGNAL",
        category="social",
        actor_type="npc",
        source_type="past_simulation",
        origin="simulated_past",
        payload=SignalPayload(label="childhood"),
        occurred_at=occurred,
        clock=clock,
    )
    event_store.append(event)

    stored = event_store.get(event.event_id)
    assert stored.occurred_at < stored.recorded_at
    assert stored.is_simulated


def test_user_actor_is_never_an_npc(make_event) -> None:
    user_event = make_event(actor_type="user", actor_id="owner")
    npc_event = make_event(actor_type="npc", actor_id="owner")
    # Same id string, different actor namespace — they must not compare equal.
    assert user_event.actor_type != npc_event.actor_type


async def test_admin_and_system_events_bypass_psychology(
    bus, dispatcher, snapshots, event_store, clock, recording_subscriber, make_event
) -> None:
    psychology = recording_subscriber("emotion_engine")
    observer = recording_subscriber("audit_log")
    bus.register(psychology, kind="psychology")
    bus.register(observer, kind="observer")

    system_event = Event.create(
        event_type="SYSTEM_STARTED",
        category="system",
        actor_type="system",
        source_type="bootstrap",
        origin="system",
        payload=SystemStartedPayload(manifest_id="man_1", schema_version_applied=1, mode="test"),
        clock=clock,
    )
    event_store.append(system_event)
    await dispatcher.dispatch(system_event, snapshots.capture(persist=False))

    assert psychology.calls == []
    assert observer.calls == [system_event.event_id]

    social_event = make_event()
    event_store.append(social_event)
    await dispatcher.dispatch(social_event, snapshots.capture(persist=False))
    assert psychology.calls == [social_event.event_id]
