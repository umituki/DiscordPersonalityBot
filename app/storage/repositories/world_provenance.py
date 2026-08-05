"""What in this database was produced under the world model in force now.

A repository rather than a world-module helper, because it is SQL and SQL lives
here (`.claude/rules/architecture.md`). It reads provenance and counts it; what
the number *means* belongs to `app.world.scope`, which owns the constant.

Migration 37 records provenance; this reads it. The separation matters: the
migration could not answer the question, which is why the columns had to exist
in the first place. A month generated under the old substring critic came out
of migration 36 carrying `participants=[]` and `interaction_scope=local` —
structurally identical to a month the current validator had actually passed.
Only a version stamped by the code that ran the validation tells them apart.

Nothing here repairs anything. There is no safe automatic repair: inferring
what an old month meant would mean reading its prose, which is the structure
the world model replaced. The answer to a stale life is an explicit
`rebuild-reset`, and this exists so that answer is a fact the system can state
rather than something an operator has to remember.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.storage.database import Database
from app.world.scope import CURRENT_WORLD_MODEL_VERSION

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorldProvenanceReport:
    """Which stores hold rows nothing checked under the current rules."""

    current: int
    #: Human-readable descriptions of what is stale, one per store.
    stale: tuple[str, ...] = ()
    #: Counts per store, for the go-live report and the admin views.
    counts: dict[str, int] = None  # type: ignore[assignment]

    @property
    def rebuild_required(self) -> bool:
        return bool(self.stale)


def read_world_provenance(db: Database) -> WorldProvenanceReport:
    """Count what predates the current world model.

    Only rows that would be used as *current* authority are counted. A
    forensic archive is a separate database and is never opened here — a fresh
    reset must not be blocked by the life it replaced.
    """
    counts: dict[str, int] = {}
    stale: list[str] = []

    def count(name: str, sql: str) -> None:
        try:
            found = int(db.scalar(sql, (CURRENT_WORLD_MODEL_VERSION,)) or 0)
        except Exception:  # noqa: BLE001 - a table a phase has not created yet
            return
        counts[name] = found
        if found:
            stale.append(f"{name}={found}")

    # The active epoch is the one that decides whether *this* life is current.
    # An older epoch in the history is a record of a life that was replaced.
    try:
        epoch = db.query_one(
            "SELECT world_model_version FROM rebuild_epochs "
            "ORDER BY started_at DESC, epoch_id DESC LIMIT 1"
        )
    except Exception:  # noqa: BLE001
        epoch = None
    if epoch is not None and int(epoch["world_model_version"] or 0) != (
        CURRENT_WORLD_MODEL_VERSION
    ):
        counts["rebuild_epoch"] = 1
        stale.append("rebuild_epoch=1")

    count(
        "life_months",
        "SELECT COUNT(*) FROM life_months WHERE world_model_version != ?",
    )
    count(
        "genesis_experiences",
        "SELECT COUNT(*) FROM genesis_experiences WHERE world_model_version != ?",
    )
    # Advisory rather than blocking on its own: an unverified Common Ground row
    # is already refused re-admission at the correction boundary, so it cannot
    # become a fact. It is reported because the OWNER should know it is there.
    count(
        "common_ground_claims",
        "SELECT COUNT(*) FROM common_ground_claims WHERE world_model_version != ? "
        "AND status IN ('provisional', 'accepted', 'contested')",
    )

    if stale:
        logger.warning(
            "this database holds rows from before world model v%s: %s",
            CURRENT_WORLD_MODEL_VERSION,
            ", ".join(stale),
        )
    return WorldProvenanceReport(
        current=CURRENT_WORLD_MODEL_VERSION,
        stale=tuple(stale),
        counts=counts,
    )


__all__ = ["WorldProvenanceReport", "read_world_provenance"]
