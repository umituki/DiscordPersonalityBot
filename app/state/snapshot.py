"""S0 state snapshots (spec 9.2).

One snapshot is taken when a root event starts processing. Every engine in that
run reads the snapshot, not the live database, so a value written during the
run cannot feed back into an appraisal in the same run and re-amplify itself.
Feedback loops cross event boundaries, not statements.
"""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Iterator, Mapping

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
