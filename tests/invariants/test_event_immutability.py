"""INVARIANT: stored events are immutable (spec 8.3).

Corrections are appended as new events. History is never rewritten — not by
application code, and not by anything holding the connection.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import SignalPayload

pytestmark = pytest.mark.invariant


def test_sql_update_of_an_event_is_refused(db, event_store, make_event) -> None:
    event = make_event(payload=SignalPayload(label="as it happened"))
    event_store.append(event)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = ?",
            ('{"label": "rewritten", "strength": 1.0}', event.event_id),
        )

    stored = event_store.get(event.event_id)
    assert stored.payload.label == "as it happened"


def test_sql_delete_of_an_event_is_refused(db, event_store, make_event) -> None:
    event = make_event()
    event_store.append(event)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM events WHERE event_id = ?", (event.event_id,))

    assert event_store.exists(event.event_id)


def test_store_exposes_no_mutation_api(event_store) -> None:
    for forbidden in ("update", "delete", "remove", "overwrite", "edit"):
        assert not hasattr(event_store, forbidden), (
            f"EventStore.{forbidden} would allow history rewriting (spec 8.3)"
        )


def test_reappending_a_changed_event_with_the_same_id_does_not_overwrite(
    event_store, make_event
) -> None:
    original = make_event(payload=SignalPayload(label="original"))
    event_store.append(original)

    forged = original.model_copy(update={"payload": SignalPayload(label="forged")})
    assert event_store.append(forged) is False

    assert event_store.get(original.event_id).payload.label == "original"


def test_correction_is_a_new_event_in_the_same_chain(event_store, make_event) -> None:
    event = make_event()
    event_store.append(event)

    correction = event_store.invalidate(event, reason_code="mistaken_ingest")

    assert correction.event_id != event.event_id
    assert correction.root_event_id == event.root_event_id
    assert event_store.get(event.event_id) is not None
    assert event_store.count() == 2
