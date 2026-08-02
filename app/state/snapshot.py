"""S0 state snapshots (spec 9.2).

One snapshot is taken when a root event starts processing. Every engine in that
run reads the snapshot, not the live database, so a value written during the
run cannot feed back into an appraisal in the same run and re-amplify itself.
Feedback loops cross event boundaries, not statements.
"""

from __future__ import annotations

import hashlib

from datetime import datetime
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from app import ids
from app.clock import Clock, SystemClock, from_iso, to_iso
from app.state.value import JSONValue, StateValue, UnknownValueError
from app.storage.repositories.snapshots import SnapshotRepository
from app.storage.repositories.state import StateRepository


class StateSnapshot:
    """An immutable read-only view of state at one instant."""

    __slots__ = ("_created_at", "_root_event_id", "_snapshot_id", "_values")

    def __init__(
        self,
        *,
        snapshot_id: str,
        created_at: datetime,
        values: Mapping[tuple[str, str], StateValue],
        root_event_id: str | None = None,
    ) -> None:
        self._snapshot_id = snapshot_id
        self._created_at = created_at
        self._root_event_id = root_event_id
        self._values: Mapping[tuple[str, str], StateValue] = MappingProxyType(dict(values))

    # --- identity ----------------------------------------------------------
    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def root_event_id(self) -> str | None:
        return self._root_event_id

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[StateValue]:
        return iter(self._values.values())

    # --- reads -------------------------------------------------------------
    def get(self, domain: str, key: str) -> StateValue | None:
        """Return the value, or ``None`` when the key was never written.

        Spec 24: absent is ``unknown``, not ``0``.
        """
        return self._values.get((domain, key))

    def require(self, domain: str, key: str) -> StateValue:
        value = self.get(domain, key)
        if value is None:
            raise UnknownValueError(f"{domain}.{key} has never been written")
        return value

    def value_of(self, domain: str, key: str, default: JSONValue = None) -> JSONValue:
        entry = self.get(domain, key)
        return default if entry is None else entry.value

    def number_of(self, domain: str, key: str, default: float | None = None) -> float | None:
        entry = self.get(domain, key)
        return default if entry is None else entry.numeric

    def version_of(self, domain: str, key: str) -> int | None:
        entry = self.get(domain, key)
        return None if entry is None else entry.version

    def domain(self, domain: str) -> Mapping[str, StateValue]:
        return MappingProxyType(
            {key: value for (dom, key), value in self._values.items() if dom == domain}
        )

    def fingerprint(self) -> str:
        """Hash of every key's version (patch spec 7.5).

        Staleness is exactly a version disagreement, so the fingerprint is
        built from versions rather than values: two snapshots with the same
        fingerprint can be committed against interchangeably.
        """
        material = ";".join(
            f"{domain}.{key}={value.version}"
            for (domain, key), value in sorted(self._values.items())
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def domains(self) -> tuple[str, ...]:
        return tuple(sorted({domain for domain, _ in self._values}))

    # --- serialisation -----------------------------------------------------
    def to_payload(self) -> dict[str, dict[str, object]]:
        payload: dict[str, dict[str, object]] = {}
        for (domain, key), value in self._values.items():
            payload.setdefault(domain, {})[key] = {
                "value": value.value,
                "confidence": value.confidence,
                "version": value.version,
                "updated_at": to_iso(value.updated_at),
            }
        return payload

    @classmethod
    def from_payload(
        cls,
        *,
        snapshot_id: str,
        created_at: datetime,
        payload: Mapping[str, Mapping[str, dict]],
        root_event_id: str | None = None,
    ) -> StateSnapshot:
        values: dict[tuple[str, str], StateValue] = {}
        for domain, keys in payload.items():
            for key, entry in keys.items():
                updated_at = from_iso(entry["updated_at"])
                values[(domain, key)] = StateValue(
                    domain=domain,
                    key=key,
                    value=entry.get("value"),
                    confidence=entry.get("confidence"),
                    version=int(entry.get("version", 1)),
                    created_at=updated_at,
                    updated_at=updated_at,
                )
        return cls(
            snapshot_id=snapshot_id,
            created_at=created_at,
            values=values,
            root_event_id=root_event_id,
        )

    @classmethod
    def empty(cls, *, snapshot_id: str, created_at: datetime) -> StateSnapshot:
        return cls(snapshot_id=snapshot_id, created_at=created_at, values={})

    # --- derived views ------------------------------------------------------
    def with_committed(
        self,
        changes: Iterable[tuple[str, str, JSONValue, float | None]],
        *,
        now: datetime | None = None,
    ) -> StateSnapshot:
        """S0 plus the changes this run committed (patch spec 9).

        A reply is spoken *after* the event has been taken in, so what it
        expresses must be the state that resulted from the event, not the state
        that preceded it. This is derived from the accepted changes rather than
        re-read from the database: a fresh read would pick up other runs'
        writes, which this event's reply has not experienced.

        Not persisted, and never used as an S0 for another run.
        """
        merged = dict(self._values)
        for domain, key, value, confidence in changes:
            previous = merged.get((domain, key))
            moment = now or (previous.updated_at if previous else self._created_at)
            merged[(domain, key)] = StateValue(
                domain=domain,
                key=key,
                value=value,
                confidence=confidence if confidence is not None else (
                    previous.confidence if previous else None
                ),
                version=(previous.version + 1) if previous else 1,
                created_at=previous.created_at if previous else moment,
                updated_at=moment,
                updated_by_run_id=previous.updated_by_run_id if previous else None,
                updated_by_event_id=previous.updated_by_event_id if previous else None,
            )
        return StateSnapshot(
            snapshot_id=self._snapshot_id,
            created_at=self._created_at,
            values=merged,
            root_event_id=self._root_event_id,
        )


class SnapshotService:
    """Captures and persists S0 snapshots."""

    def __init__(
        self,
        state_repository: StateRepository,
        snapshot_repository: SnapshotRepository,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._state = state_repository
        self._snapshots = snapshot_repository
        self._clock = clock or SystemClock()

    def capture(
        self, *, root_event_id: str | None = None, run_id: str | None = None, persist: bool = True
    ) -> StateSnapshot:
        created_at = self._clock.now()
        values = {(value.domain, value.key): value for value in self._state.list_all()}
        snapshot = StateSnapshot(
            snapshot_id=ids.new_id(ids.SNAPSHOT),
            created_at=created_at,
            values=values,
            root_event_id=root_event_id,
        )
        if persist:
            self._snapshots.save(
                snapshot_id=snapshot.snapshot_id,
                run_id=run_id,
                root_event_id=root_event_id,
                created_at=created_at,
                payload=snapshot.to_payload(),
            )
        return snapshot

    def load(self, snapshot_id: str, *, created_at: datetime | None = None) -> StateSnapshot | None:
        payload = self._snapshots.load_payload(snapshot_id)
        if payload is None:
            return None
        return StateSnapshot.from_payload(
            snapshot_id=snapshot_id,
            created_at=created_at or self._clock.now(),
            payload=payload,
        )
