"""Time and identifier primitives (spec 38)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import ids
from app.clock import FixedClock, NaiveDatetimeError, SystemClock, ensure_aware, from_iso, to_iso


def test_system_clock_is_timezone_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_aware(datetime(2026, 1, 1, 12, 0))


def test_non_utc_input_is_normalised() -> None:
    tokyo = timezone(timedelta(hours=9))
    value = ensure_aware(datetime(2026, 1, 1, 21, 0, tzinfo=tokyo))
    assert value.utcoffset() == timedelta(0)
    assert value.hour == 12


def test_iso_round_trip() -> None:
    moment = datetime(2026, 5, 4, 3, 2, 1, tzinfo=timezone.utc)
    assert from_iso(to_iso(moment)) == moment


def test_fixed_clock_advances_forward_only() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    first = clock.now()
    clock.advance(seconds=60)
    assert clock.now() - first == timedelta(seconds=60)
    with pytest.raises(ValueError):
        clock.advance(seconds=-1)


def test_ids_carry_a_domain_prefix() -> None:
    event_id = ids.new_id(ids.EVENT)
    assert event_id.startswith("evt_")
    assert ids.prefix_of(event_id) == "evt"
    assert ids.has_prefix(event_id, ids.EVENT)
    assert not ids.has_prefix(event_id, ids.RUN)


def test_ids_are_unique_and_monotonic() -> None:
    generated = [ids.new_id(ids.EVENT) for _ in range(500)]
    assert len(set(generated)) == 500
    assert generated == sorted(generated)


def test_invalid_prefix_rejected() -> None:
    with pytest.raises(ValueError):
        ids.new_id("bad_prefix")
    with pytest.raises(ValueError):
        ids.prefix_of("noprefix")
