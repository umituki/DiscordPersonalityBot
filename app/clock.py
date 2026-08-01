"""Time authority.

Spec 2.2: Python owns time. Spec 38: timestamps are timezone-aware ISO-8601 and
the internal reference is consistent (UTC).

Nothing in the system may call ``datetime.now()`` without a timezone; use a
``Clock``. Tests use ``FixedClock`` so that temporal behaviour is reproducible
(spec 29 reproducibility priorities).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

UTC = timezone.utc


class NaiveDatetimeError(ValueError):
    """Raised when a naive (timezone-less) datetime crosses a boundary."""


@runtime_checkable
class Clock(Protocol):
    """Source of the current instant."""

    def now(self) -> datetime:
        """Return the current timezone-aware instant."""


class SystemClock:
    """Wall-clock time in UTC."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests and simulation runs."""

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        self._now = ensure_aware(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0, **kwargs: float) -> datetime:
        """Move the clock forward. Never backwards."""
        delta = timedelta(seconds=seconds, **kwargs)
        if delta < timedelta(0):
            raise ValueError("FixedClock cannot move backwards")
        self._now = self._now + delta
        return self._now

    def set(self, moment: datetime) -> datetime:
        self._now = ensure_aware(moment)
        return self._now


def ensure_aware(value: datetime) -> datetime:
    """Reject naive datetimes; normalise everything else to UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(f"timezone-aware datetime required, got {value!r}")
    return value.astimezone(UTC)


def to_iso(value: datetime) -> str:
    """Serialise to a storage/transport ISO-8601 string in UTC."""
    return ensure_aware(value).isoformat()


def from_iso(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp back into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    return ensure_aware(parsed)
