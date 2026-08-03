"""The Life Continuity Ledger (rebuild spec 34.7, 34.8 — Phase 12).

    毎月すべてを prompt へ入れず relevant subset を retrieval。

Nineteen years produce hundreds of people, places, interests and unfinished
threads. A prompt cannot carry them, and a generator that only sees last month
reintroduces the same friend three times and forgets the piano after April.

So continuity lives in a table rather than in prose, and each month is given
the subset that is actually relevant: whoever was around recently, whatever is
still unfinished, and whatever this month's scaffold already mentions.

34.8 adds the part that outlives Genesis. People are tracked by a stable id
rather than a name, they carry when they were introduced and when they were
last seen, and the ones still around at the end are handed to the runtime
society. The ones who drifted away stay in the archive — a friend she has not
seen for six years is not a friend she never had.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from app.genesis.models import EntityType

logger = logging.getLogger(__name__)

MODULE = "continuity_ledger"

#: How long someone stays "recently around" for retrieval purposes.
RECENT_MONTHS = 6

#: How many entities one month's prompt may carry. Past this the subset stops
#: being a subset.
MAX_IN_CONTEXT = 12

#: Types that are always worth carrying if they are open, however old.
ALWAYS_RELEVANT: frozenset[str] = frozenset({"ONGOING_THREAD", "COMMITMENT", "LIFE_FACT"})


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entity_id: str
    type: EntityType
    canonical_name: str
    introduced_at: datetime
    last_seen_at: datetime | None = None
    status: str = "active"
    objective: dict[str, Any] | None = None

    def describe(self) -> str:
        seen = "" if self.last_seen_at is None else f"（最後: {self.last_seen_at.date()}）"
        return f"[{self.type}] {self.canonical_name}{seen}"


class ContinuityLedger:
    """Reads and writes the ledger. Owns no narrative."""

    name = MODULE

    def __init__(self, repository: Any, *, genesis_run_id: str) -> None:
        self._repository = repository
        self._run_id = genesis_run_id

    def note(
        self,
        *,
        type: EntityType,
        name: str,
        moment: datetime,
        objective: dict[str, Any] | None = None,
        month_id: str | None = None,
    ) -> str:
        """Record that this thing exists, or that it came up again.

        Idempotent on (run, type, name): a person mentioned in eleven months is
        one person with eleven sightings, not eleven people.
        """
        entity_id = self._repository.upsert(
            genesis_run_id=self._run_id,
            type=type,
            canonical_name=name.strip(),
            introduced_at=moment,
            objective=objective or {},
        )
        self._repository.touch(entity_id, seen_at=moment, month_id=month_id)
        return entity_id

    def retire(self, entity_id: str, *, moment: datetime, reason: str = "") -> None:
        """Someone drifted out of her life. The record does not (34.8)."""
        self._repository.retire(entity_id, retired_at=moment, reason=reason)

    def relevant_for(
        self, moment: datetime, *, mentioned: Sequence[str] = ()
    ) -> tuple[LedgerEntry, ...]:
        """The subset this month's prompt should carry (34.7).

        Three reasons to be included, in priority order: this month's scaffold
        names you; you are an open thread or a standing commitment; you were
        around recently. Everything else is left out, which is the point.
        """
        named = {name.strip() for name in mentioned if name.strip()}
        cutoff = moment - timedelta(days=30 * RECENT_MONTHS)

        chosen: dict[str, LedgerEntry] = {}
        for entry in self._all():
            if entry.introduced_at > moment:
                continue  # not yet part of her life
            if entry.canonical_name in named:
                chosen[entry.entity_id] = entry
                continue
            if entry.status != "active":
                continue
            if entry.type in ALWAYS_RELEVANT:
                chosen[entry.entity_id] = entry
                continue
            seen = entry.last_seen_at or entry.introduced_at
            if seen >= cutoff:
                chosen[entry.entity_id] = entry

        ordered = sorted(
            chosen.values(),
            key=lambda item: (
                item.canonical_name not in named,
                item.type not in ALWAYS_RELEVANT,
                -(item.last_seen_at or item.introduced_at).timestamp(),
            ),
        )
        return tuple(ordered[:MAX_IN_CONTEXT])

    def render(self, moment: datetime, *, mentioned: Sequence[str] = ()) -> str:
        entries = self.relevant_for(moment, mentioned=mentioned)
        if not entries:
            return "（まだ何もない）"
        return "\n".join(f"- {entry.describe()}" for entry in entries)

    def survivors(self, present: datetime) -> tuple[LedgerEntry, ...]:
        """People still in her life at the end (34.8).

        These are handed to the runtime society. Anyone who faded is left in
        the archive rather than deleted — a friend she has not seen for six
        years is not a friend she never had.
        """
        cutoff = present - timedelta(days=365)
        return tuple(
            entry
            for entry in self._all()
            if entry.type == "NPC"
            and entry.status == "active"
            and (entry.last_seen_at or entry.introduced_at) >= cutoff
        )

    def _all(self) -> tuple[LedgerEntry, ...]:
        return tuple(
            LedgerEntry(
                entity_id=row["entity_id"],
                type=row["type"],
                canonical_name=row["canonical_name"],
                introduced_at=row["introduced_at"],
                last_seen_at=row["last_seen_at"],
                status=row["status"],
                objective=json.loads(row["objective_json"] or "{}"),
            )
            for row in self._repository.all_for(self._run_id)
        )


__all__ = ["ALWAYS_RELEVANT", "MAX_IN_CONTEXT", "ContinuityLedger", "LedgerEntry"]
