"""Repairing a database that booted from a broken Genesis (patch spec 23).

The recommended procedure of 23.4, in the order it gives:

    1.  back up production
    2.  read the original seed / scaffold / owner inputs
    3.  rebuild the Genesis into a *shadow* database with the patched engine
    4.  put it through the strengthened FIRST BOOT audits
    5.  extract the real Discord events recorded after the original FIRST BOOT
    6.  replay them into the shadow database
    7.  no Discord side effects during the replay
    8.  what YUI actually said stays what she actually said
    9.  no reply is regenerated and nothing is re-sent
    10. rebuild the state consequences of the real events on the repaired past
    11. compare integrity, invariants, counts, chronology
    12. ask the owner
    13. switch atomically
    14. keep the old database as the rollback

Three things this module structurally cannot do.

**Delete anything on its own** (23.1). Nothing here removes a row or a file
except :meth:`RepairService.switch`, which renames the old database aside and
requires an explicit owner confirmation token. A full reset is the owner's
choice and is not implemented here at all.

**Re-send a message.** The replay constructs no gateway and no conversation
service. It appends the original events — including what YUI already said —
and reprocesses them for their psychological effect. The wording in the payload
is a record, not a draft (23.4 steps 8-9).

**Switch to a shadow that did not pass.** :meth:`RepairService.switch` refuses
unless the rebuild booted, the verification is clean, and the confirmation
matches.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from app.admin.legacy import LegacyHealthScanner, LegacyReport
from app.clock import Clock, SystemClock, to_iso
from app.events.model import Event
from app.simulation.genesis import GenesisReport
from app.simulation.models import LifeScaffold, TemperamentSeed
from app.storage.backup import BackupRecord, BackupService
from app.storage.database import Database, verify_file
from app.storage.repositories.events import EventRepository
from app.storage.repositories.health import HealthRepository
from app.storage.repositories.simulation import SimulationRepository

logger = logging.getLogger(__name__)

MODULE = "genesis_repair"

#: Origins that are real history and must survive a repair untouched (23.1).
REAL_ORIGINS: frozenset[str] = frozenset({"real_discord"})

#: The owner has to type this. A repair that can happen by accident is a reset.
CONFIRMATION = "REPLACE PRODUCTION"


class RepairRefused(RuntimeError):
    """Raised when a repair step was asked for without its precondition."""


@dataclass(frozen=True, slots=True)
class RealHistory:
    """The real Discord events recorded after the original FIRST BOOT."""

    events: tuple[Event, ...] = ()
    first_boot_at: datetime | None = None

    @property
    def count(self) -> int:
        return len(self.events)

    @property
    def chronological(self) -> bool:
        moments = [event.occurred_at for event in self.events]
        return moments == sorted(moments)

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        if not self.events:
            return None
        return (self.events[0].occurred_at, self.events[-1].occurred_at)


@dataclass
class VerificationReport:
    """Whether the shadow is a database the owner may switch to (23.4 step 11)."""

    integrity: str = "unknown"
    schema_version: int = 0
    real_events_before: int = 0
    real_events_after: int = 0
    chronological: bool = False
    history_preserved: bool = False
    genesis_booted: bool = False
    audits_failed: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return (
            self.integrity == "ok"
            and self.history_preserved
            and self.chronological
            and self.genesis_booted
            and not self.audits_failed
            and not self.findings
        )

    def as_detail(self) -> dict[str, object]:
        return {
            "integrity": self.integrity,
            "schema_version": self.schema_version,
            "real_events_before": self.real_events_before,
            "real_events_after": self.real_events_after,
            "history_preserved": self.history_preserved,
            "chronological": self.chronological,
            "genesis_booted": self.genesis_booted,
            "audits_failed": list(self.audits_failed),
            "findings": list(self.findings),
            "clean": self.clean,
        }


@dataclass
class RepairPlan:
    """What a repair would do, decided before anything is written (23.2)."""

    diagnosis: LegacyReport
    production_path: Path
    shadow_path: Path
    seed: TemperamentSeed | None = None
    scaffold: LifeScaffold | None = None
    history: RealHistory = field(default_factory=RealHistory)
    blockers: list[str] = field(default_factory=list)

    @property
    def needed(self) -> bool:
        return not self.diagnosis.sound

    @property
    def possible(self) -> bool:
        return self.needed and not self.blockers

    def render(self) -> str:
        lines = [self.diagnosis.render(), ""]
        if not self.needed:
            lines.append("nothing to repair: the Genesis under this database holds up.")
            return "\n".join(lines)
        lines.append(f"shadow database: {self.shadow_path}")
        lines.append(f"real Discord events to replay: {self.history.count}")
        if self.history.span:
            start, end = self.history.span
            lines.append(f"  from {to_iso(start)} to {to_iso(end)}")
        for blocker in self.blockers:
            lines.append(f"  BLOCKED: {blocker}")
        if self.possible:
            lines.append("")
            lines.append("this is a dry run. Nothing has been written.")
        return "\n".join(lines)

    def as_detail(self) -> dict[str, object]:
        return {
            "needed": self.needed,
            "possible": self.possible,
            "blockers": list(self.blockers),
            "shadow_path": str(self.shadow_path),
            "real_events": self.history.count,
            "diagnosis": self.diagnosis.as_detail(),
        }


@dataclass
class RepairResult:
    plan: RepairPlan
    backup: BackupRecord | None = None
    genesis: GenesisReport | None = None
    replayed: int = 0
    verification: VerificationReport = field(default_factory=VerificationReport)
    switched: bool = False
    rollback_path: Path | None = None
    refusal: str = ""

    @property
    def ready_to_switch(self) -> bool:
        return self.verification.clean and not self.refusal


#: Builds a shadow application and rebuilds the Genesis in it. Supplied by the
#: caller so this module never assembles the world itself — and so a test can
#: rebuild without a model.
ShadowRebuild = Callable[[Path, TemperamentSeed, LifeScaffold], Awaitable[GenesisReport]]

#: Replays one real event into the shadow database. Supplied for the same
#: reason; the contract is that it must not send anything.
ShadowReplay = Callable[[Path, Sequence[Event]], Awaitable[int]]


class RepairService:
    """Diagnose, rebuild in a shadow, verify, and only then offer the switch."""

    name = MODULE

    def __init__(
        self,
        *,
        db: Database,
        events: EventRepository,
        simulations: SimulationRepository,
        health: HealthRepository,
        backups: BackupService,
        shadow_dir: Path,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._events = events
        self._simulations = simulations
        self._health = health
        self._backups = backups
        self._shadow_dir = Path(shadow_dir)
        self._clock = clock or SystemClock()
        self._scanner = LegacyHealthScanner(health=health, simulations=simulations)

    # --- 23.2 / 23.3: look before touching ----------------------------------
    def dry_run(self) -> RepairPlan:
        """Diagnose and plan. Writes nothing (patch spec 23.2)."""
        diagnosis = self._scanner.scan()
        production = self._production_path()
        plan = RepairPlan(
            diagnosis=diagnosis,
            production_path=production,
            shadow_path=self._shadow_path(),
        )
        if not diagnosis.sound:
            plan.seed = (
                None
                if diagnosis.seed_id is None
                else self._simulations.seed(diagnosis.seed_id)
            )
            plan.scaffold = (
                None
                if diagnosis.scaffold_id is None
                else self._simulations.scaffold(diagnosis.scaffold_id)
            )
            plan.history = self.real_history()
            plan.blockers = list(self._blockers(plan))

        logger.info(
            "repair dry run health=%s needed=%s possible=%s replay=%d",
            diagnosis.genesis_health,
            plan.needed,
            plan.possible,
            plan.history.count,
        )
        return plan

    def _blockers(self, plan: RepairPlan) -> list[str]:
        blockers: list[str] = []
        if plan.seed is None:
            blockers.append(
                "the original temperament seed is missing; a rebuild would be a "
                "different person, which is a reset and the owner's call (23.4)"
            )
        if plan.scaffold is None:
            blockers.append("the original life scaffold is missing")
        if not plan.history.chronological:
            blockers.append(
                "the real Discord history is not in chronological order; replaying "
                "it would put effects before their causes"
            )
        if plan.shadow_path.exists():
            blockers.append(
                f"a shadow database already exists at {plan.shadow_path}; move it "
                "aside rather than overwriting a previous attempt"
            )
        return blockers

    # --- 23.4 step 5: what must survive -------------------------------------
    def real_history(self) -> RealHistory:
        """Every real Discord event after the original FIRST BOOT, in order.

        Read straight from the archive. These are the rows that make the repair
        worth doing carefully — deleting the database would lose them (23.1).
        """
        booted = self._simulations.booted_run()
        first_boot_at = None if booted is None else booted.first_boot_at
        events = self._events.list_by_origins(REAL_ORIGINS, since=first_boot_at)
        return RealHistory(events=tuple(events), first_boot_at=first_boot_at)

    # --- 23.4 steps 1-11: rebuild into a shadow -----------------------------
    async def rebuild(
        self,
        *,
        rebuild: ShadowRebuild,
        replay: ShadowReplay,
        plan: RepairPlan | None = None,
    ) -> RepairResult:
        """Back up, rebuild in a shadow, replay the real history, verify.

        Production is untouched throughout: everything happens in the shadow
        file, and switching to it is a separate, confirmed step.
        """
        plan = plan or self.dry_run()
        result = RepairResult(plan=plan)

        if not plan.needed:
            result.refusal = "the Genesis under this database is sound"
            return result
        if not plan.possible:
            result.refusal = "; ".join(plan.blockers)
            return result

        # 1. A repair without a verified backup is not a repair (23.2).
        result.backup = self._backups.create(kind="pre_repair", reason="genesis repair")
        if not result.backup.usable:
            result.refusal = "the pre-repair backup did not verify"
            return result

        # 3-4. The patched engine builds the life again, and the strengthened
        # audits decide whether it is one worth keeping.
        assert plan.seed is not None and plan.scaffold is not None
        self._shadow_dir.mkdir(parents=True, exist_ok=True)
        genesis = await rebuild(plan.shadow_path, plan.seed, plan.scaffold)
        result.genesis = genesis
        if not genesis.booted:
            result.refusal = f"the rebuilt Genesis did not pass its audits: {genesis.refusal}"
            result.verification = self._verify(plan, genesis, replayed=0)
            return result

        # 5-10. The real history is replayed onto the repaired past. Nothing is
        # sent, and nothing is rewritten: the events already say what happened.
        result.replayed = await replay(plan.shadow_path, plan.history.events)

        # 11. Compare before offering the switch.
        result.verification = self._verify(plan, genesis, replayed=result.replayed)
        logger.info(
            "repair rebuild complete booted=%s replayed=%d clean=%s",
            genesis.booted,
            result.replayed,
            result.verification.clean,
        )
        return result

    def _verify(
        self, plan: RepairPlan, genesis: GenesisReport | None, *, replayed: int
    ) -> VerificationReport:
        integrity, schema = ("missing", 0)
        if plan.shadow_path.exists():
            integrity, schema = verify_file(plan.shadow_path)

        report = VerificationReport(
            integrity=integrity,
            schema_version=schema,
            real_events_before=plan.history.count,
            real_events_after=replayed,
            chronological=plan.history.chronological,
            history_preserved=replayed == plan.history.count,
            genesis_booted=bool(genesis and genesis.booted),
            audits_failed=() if genesis is None else genesis.failed,
        )
        if plan.shadow_path.exists() and integrity == "ok":
            report.findings = tuple(self._shadow_findings(plan.shadow_path))
        return report

    @staticmethod
    def _shadow_findings(path: Path) -> list[str]:
        """Run the legacy scan against the shadow: it must come back healthy."""
        shadow = Database(path)
        try:
            shadow.connect()
            scanner = LegacyHealthScanner(
                health=HealthRepository(shadow),
                simulations=SimulationRepository(shadow),
            )
            report = scanner.scan()
            return [finding.code for finding in report.findings]
        finally:
            shadow.close()

    # --- 23.4 steps 12-14: the switch ---------------------------------------
    def switch(self, result: RepairResult, *, confirmation: str) -> RepairResult:
        """Replace production with the shadow, keeping the old one to roll back to.

        Refuses unless the rebuild booted, the verification came back clean and
        the owner typed the confirmation. The old database is renamed, never
        removed: the rollback is the file that was already there (23.4 step 14).
        """
        if confirmation != CONFIRMATION:
            raise RepairRefused(
                f"switching production requires the confirmation {CONFIRMATION!r}"
            )
        if not result.ready_to_switch:
            raise RepairRefused(
                "the rebuilt database did not verify: "
                + (result.refusal or ", ".join(result.verification.findings) or "unknown")
            )

        production = result.plan.production_path
        shadow = result.plan.shadow_path
        if not shadow.exists():
            raise RepairRefused(f"no shadow database at {shadow}")

        stamp = to_iso(self._clock.now()).replace(":", "").replace("-", "")
        rollback = production.with_suffix(f".rollback-{stamp}.db")

        # The live database has to be closed before its file moves, and the
        # WAL/SHM siblings move with it or the rollback is half a database.
        self._db.close()
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(production) + suffix)
            if source.exists():
                shutil.move(str(source), str(Path(str(rollback) + suffix)))
        shutil.move(str(shadow), str(production))

        result.switched = True
        result.rollback_path = rollback
        logger.warning(
            "production database replaced by repaired rebuild rollback=%s", rollback
        )
        return result

    def rollback(self, result: RepairResult) -> Path:
        """Put the original database back (23.4 step 14)."""
        if not result.switched or result.rollback_path is None:
            raise RepairRefused("nothing to roll back: the switch never happened")

        production = result.plan.production_path
        rollback = result.rollback_path
        if not rollback.exists():
            raise RepairRefused(f"the rollback copy is missing: {rollback}")

        self._db.close()
        replaced = production.with_suffix(".repaired.db")
        for suffix in ("", "-wal", "-shm"):
            current = Path(str(production) + suffix)
            if current.exists():
                shutil.move(str(current), str(Path(str(replaced) + suffix)))
            source = Path(str(rollback) + suffix)
            if source.exists():
                shutil.move(str(source), str(Path(str(production) + suffix)))

        result.switched = False
        logger.warning("production database rolled back; repaired copy kept at %s", replaced)
        return replaced

    # --- paths ---------------------------------------------------------------
    def _production_path(self) -> Path:
        path = self._db.path
        if not isinstance(path, Path):
            raise RepairRefused("an in-memory database has nothing to repair")
        return path

    def _shadow_path(self) -> Path:
        return self._shadow_dir / f"{self._production_path().stem}.shadow.db"


__all__ = [
    "CONFIRMATION",
    "MODULE",
    "REAL_ORIGINS",
    "RealHistory",
    "RepairPlan",
    "RepairRefused",
    "RepairResult",
    "RepairService",
    "VerificationReport",
]
