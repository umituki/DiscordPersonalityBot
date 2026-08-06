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
from dataclasses import dataclass, field

from app.storage.database import Database
from app.world.scope import CURRENT_WORLD_MODEL_VERSION

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorldProvenanceReport:
    """Which stores hold rows nothing checked under the current rules.

    Split, because the two halves mean different things. A stale life is a life
    that cannot be lived forward — the months, the experiences, the run that
    produced them, the epoch they belong to. A stale Common Ground claim is a
    sentence she said once, and it is already refused re-admission as a fact at
    the correction boundary, so it cannot become anything. Blocking go-live on
    it would stop her talking over a row that cannot hurt anyone; hiding it
    would leave the OWNER unaware it is there.
    """

    current: int
    #: Stores whose staleness means this life cannot go live.
    blocking: tuple[str, ...] = ()
    #: Stores worth reporting that do not, on their own, hold the door.
    advisory: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def rebuild_required(self) -> bool:
        return bool(self.blocking)

    @property
    def stale(self) -> tuple[str, ...]:
        """Everything, blocking first. For a report that lists the lot."""
        return self.blocking + self.advisory


def read_world_provenance(
    db: Database, *, authoritative_genesis_run_id: str | None = None
) -> WorldProvenanceReport:
    """Count what predates the current world model, in the life that counts.

    ``authoritative_genesis_run_id`` comes from FIRST BOOT and is the only
    thing that decides which Genesis life is being asked about. The reader does
    not go looking for one: it used to take the most recently *started* run,
    and that is a different question — a later run can exist that FIRST BOOT
    never pointed at, and then the stale life FIRST BOOT *is* pointing at
    passes because a newer row is sitting beside it.

    ``None`` means no life is attached. Nothing about the Genesis tables is
    counted, because there is no authoritative life to count; `first_boot_
    complete` is what holds the door in that state, and it does.

    Only rows that would be used as *current* authority are counted. A forensic
    archive is a separate database and is never opened here — a fresh reset
    must not be blocked by the life it replaced.
    """
    counts: dict[str, int] = {}
    blocking: list[str] = []
    advisory: list[str] = []

    def scalar(sql: str, params: tuple) -> int | None:
        try:
            return int(db.scalar(sql, params) or 0)
        except Exception:  # noqa: BLE001 - a table a phase has not created yet
            return None

    # The active epoch decides whether *this* life is current. An older epoch
    # in the history is a record of a life that was replaced.
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
        blocking.append("rebuild_epoch=1")

    run_id = authoritative_genesis_run_id
    if run_id is not None:
        run = None
        try:
            run = db.query_one(
                "SELECT world_model_version FROM genesis_runs "
                "WHERE genesis_run_id = ?",
                (run_id,),
            )
        except Exception:  # noqa: BLE001
            run = None
        if run is None or int(run["world_model_version"] or 0) != (
            CURRENT_WORLD_MODEL_VERSION
        ):
            # A missing run is as blocking as a stale one: FIRST BOOT points at
            # a life that is not there.
            counts["genesis_run"] = 1
            blocking.append("genesis_run=1")

        # Scoped to that life. A replaced run's leftovers are history, and
        # counting them would block a fresh life over the one it replaced.
        found = scalar(
            "SELECT COUNT(*) FROM life_months "
            "JOIN life_years USING (year_id) "
            "WHERE life_years.genesis_run_id = ? "
            "AND life_months.world_model_version != ?",
            (run_id, CURRENT_WORLD_MODEL_VERSION),
        )
        if found is not None:
            counts["life_months"] = found
            if found:
                blocking.append(f"life_months={found}")

        found = scalar(
            "SELECT COUNT(*) FROM genesis_experiences WHERE genesis_run_id = ? "
            "AND world_model_version != ?",
            (run_id, CURRENT_WORLD_MODEL_VERSION),
        )
        if found is not None:
            counts["genesis_experiences"] = found
            if found:
                blocking.append(f"genesis_experiences={found}")

    # Advisory. An unverified Common Ground row is already refused at the
    # correction boundary, so it cannot become a fact — but the OWNER should
    # know the rows are there.
    found = scalar(
        "SELECT COUNT(*) FROM common_ground_claims WHERE world_model_version != ? "
        "AND status IN ('provisional', 'accepted', 'contested')",
        (CURRENT_WORLD_MODEL_VERSION,),
    )
    if found is not None:
        counts["common_ground_claims"] = found
        if found:
            advisory.append(f"common_ground_claims={found}")

    if blocking or advisory:
        logger.warning(
            "this database holds rows from before world model v%s: %s",
            CURRENT_WORLD_MODEL_VERSION,
            ", ".join(blocking + advisory),
        )
    return WorldProvenanceReport(
        current=CURRENT_WORLD_MODEL_VERSION,
        blocking=tuple(blocking),
        advisory=tuple(advisory),
        counts=counts,
    )


__all__ = ["WorldProvenanceReport", "read_world_provenance"]
