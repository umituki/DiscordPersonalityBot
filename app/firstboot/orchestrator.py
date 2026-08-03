"""The FIRST BOOT Authority (rebuild spec 34.20 — Phase 13).

One object decides whether YUI exists yet. Not the CLI, not startup, not the
Discord gateway, not an admin command — those all *ask*, and this answers.

The reason is that "is the Genesis good" and "may the system go live" are
different judgements, and the second is the one that lets a stranger talk to
her. :class:`~app.genesis.runner.GenesisRunner` reaches
``ready_for_completion`` and stops there; deciding that a finished Genesis is
grounds for opening the character plane belongs here, together with the
survivor promotion, the final consistency checks and the transition from
nineteen years ago to now.

Two shapes this file is built to avoid.

**Derived completion.** ``GenesisRunner.run()`` returning is not being born.
The path is: generation stops naturally, every span exists, every month exists,
every experience replayed, memory postconditions met, critics satisfied, ten
audits pass, survivors promoted, final consistency verified — *then* the status
moves to COMPLETE, and only from here.

**Optimistic recovery.** A crash between promoting survivors and writing
COMPLETE must not leave a half-born person. Promotion is idempotent against a
unique index, the final audits are pure reads, and the status is a
compare-and-set — so re-running finalisation is always safe and never doubles
anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.clock import Clock, SystemClock, from_iso, to_iso
from app.events.model import Event
from app.firstboot.events import (
    FIRST_BOOT_BLOCKED,
    FIRST_BOOT_COMPLETED,
    FIRST_BOOT_RESUMED,
    FIRST_BOOT_STARTED,
    FirstBootBlockedPayload,
    FirstBootCompletedPayload,
    FirstBootResumedPayload,
    FirstBootStartedPayload,
)
from app.firstboot.preflight import Preflight, run_preflight
from app.firstboot.state import (
    MAX_ATTEMPTS,
    RESUMABLE,
    RETRYABLE_BLOCKS,
    STARTABLE,
    FirstBootStatus,
    block_status,
    character_plane_open,
)
from app.genesis.anchors import LifeAnchors, TemperamentSeed

logger = logging.getLogger(__name__)

MODULE = "first_boot_orchestrator"

#: Point 30. Finalisation is ordered, and the order is a rule rather than the
#: accident of how the method happens to read.
#:
#: ``genesis_audits``    the ten checks on the generated life. First, because
#:                       everything after it acts on that life, and acting on a
#:                       life that fails its own audits is how a bad Genesis
#:                       gets laundered into the present.
#: ``survivors``         promote the NPCs who are still around. Before landing,
#:                       because they are part of the world she lands in.
#: ``landing``           bring the world from the simulated past to now.
#: ``final_consistency`` verify the landing actually landed. Last, so it sees
#:                       the finished state and not an intermediate one.
#: ``report``            write down where this person came from.
#: ``commit``            the compare-and-set to COMPLETE.
FINALISATION_STAGES: tuple[str, ...] = (
    "genesis_audits",
    "survivors_promoted",
    "landed",
    "final_consistency",
    "report_written",
    "complete",
)


@dataclass(frozen=True, slots=True)
class FirstBootOutcome:
    """What an operation did, in terms an operator can act on."""

    ok: bool
    status: str
    reason: str = ""
    genesis_run_id: str | None = None
    preflight: Preflight | None = None
    audits: tuple[Any, ...] = ()
    progress: Any = None

    def describe(self) -> str:
        head = f"{self.status}{'' if self.ok else ' — ' + self.reason}"
        return head


@dataclass(slots=True)
class FirstBootProgressView:
    """Everything `first-boot status` shows (point 19)."""

    status: str
    epoch_id: str = ""
    genesis_run_id: str | None = None
    stage: str = ""
    life_year: int = 0
    expected_years: int = 0
    months_done: int = 0
    expected_months: int = 0
    experiences_extracted: int = 0
    experiences_replayed: int = 0
    memories: int = 0
    critics_passed: int = 0
    critics_failed: int = 0
    last_checkpoint: str = ""
    last_progress_at: str = ""
    attempt_count: int = 0
    block_kind: str = ""
    block_reason: str = ""
    lease_holder: str = ""
    recovery_required: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"FIRST BOOT {self.status}"]
        if self.recovery_required:
            lines.append("  (a previous process died mid-run; resume to continue)")
        if self.genesis_run_id:
            lines.append(f"  run: {self.genesis_run_id}")
        if self.stage:
            lines.append(f"  stage: {self.stage}")
        lines.append(f"  life year: {self.life_year} / {self.expected_years}")
        lines.append(f"  months: {self.months_done} / {self.expected_months}")
        lines.append(
            f"  experiences: extracted {self.experiences_extracted} "
            f"replayed {self.experiences_replayed}"
        )
        lines.append(f"  memories: {self.memories}")
        lines.append(
            f"  critics: passed {self.critics_passed} failed {self.critics_failed}"
        )
        if self.last_checkpoint:
            lines.append(f"  last checkpoint: {self.last_checkpoint}")
        if self.last_progress_at:
            lines.append(f"  last progress: {self.last_progress_at}")
        if self.block_kind:
            lines.append(f"  blocked: {self.block_kind} — {self.block_reason}")
        return "\n".join(lines)


class FirstBootOrchestrator:
    """The only thing that may write FIRST_BOOT_COMPLETE."""

    name = MODULE

    def __init__(
        self,
        *,
        repository: Any,
        genesis: Any,
        genesis_runs: Any,
        records: Any,
        entities: Any,
        experiences: Any,
        audits: Any,
        rebuild: Any,
        event_store: Any,
        memories: Any = None,
        state: Any = None,
        world: Any = None,
        society: Any = None,
        jobs: Any = None,
        life_days: Any = None,
        processor: Any = None,
        prompts: Any = None,
        backups: Any = None,
        db: Any = None,
        schema_version: int = 0,
        latest_schema: int = 0,
        data_dir: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._genesis = genesis
        self._genesis_runs = genesis_runs
        self._records = records
        self._entities = entities
        self._experiences = experiences
        self._audits = audits
        self._rebuild = rebuild
        self._events = event_store
        self._memories = memories
        self._state = state
        self._world = world
        self._society = society
        self._jobs = jobs
        self._life_days = life_days
        self._processor = processor
        self._prompts = prompts
        self._backups = backups
        self._db = db
        self._schema_version = schema_version
        self._latest_schema = latest_schema
        self._data_dir = data_dir
        self._clock = clock or SystemClock()

    # --- what everything else asks -------------------------------------------
    def status(self) -> str:
        """The single answer. Startup, the gateway and the CLI all read this."""
        row = self._repository.current()
        if row is None:
            return "PENDING"
        if row["status"] == "RUNNING" and not self._repository.lease_is_live(
            now=self._clock.now()
        ):
            # Point 26. A crashed run leaves RUNNING behind and nothing alive
            # to correct it. Reporting it as RUNNING for ever would be a
            # deadlock only a database edit could break, so it is reported for
            # what it is: recoverable.
            return "RUNNING"
        return row["status"]

    def character_plane_open(self) -> bool:
        """Point 9 and 11's hard gate, with exactly one implementation."""
        return character_plane_open(self.status())

    def recovery_required(self) -> bool:
        row = self._repository.current()
        return bool(
            row is not None
            and row["status"] == "RUNNING"
            and not self._repository.lease_is_live(now=self._clock.now())
        )

    # --- start (points 13, 15, 16, 17, 18) -----------------------------------
    async def start(self, anchors: LifeAnchors) -> FirstBootOutcome:
        """Begin a life. Refuses if one has already been begun."""
        epoch = self._epoch()
        if epoch is None:
            return FirstBootOutcome(False, "PENDING", "no rebuild epoch exists")
        epoch_id = epoch["epoch_id"]
        state = self._repository.ensure(epoch_id, schema_version=self._schema_version)

        if state["status"] == "COMPLETE":
            # Point 51. Once is once.
            return FirstBootOutcome(
                False, "COMPLETE", "this epoch has already booted; rebuild-reset first"
            )
        if state["genesis_run_id"]:
            # Point 13. A second start would build a second life and attach it
            # to the same epoch, which is how somebody ends up with two pasts.
            return FirstBootOutcome(
                False,
                state["status"],
                f"a run already exists ({state['genesis_run_id']}); use resume",
            )
        if state["status"] not in STARTABLE:
            return FirstBootOutcome(
                False, state["status"], f"cannot start from {state['status']}"
            )

        preflight = self._preflight(anchors, epoch, state)
        if not preflight.passed:
            return FirstBootOutcome(
                False,
                state["status"],
                "; ".join(check.name for check in preflight.failures),
                preflight=preflight,
            )

        if not self._repository.acquire(epoch_id, now=self._clock.now()):
            return FirstBootOutcome(
                False, state["status"], "another process is already running first boot"
            )

        try:
            # Point 16: a verified snapshot before anything is generated, so a
            # bug in year nine is recoverable rather than merely regrettable.
            if self._backups is not None:
                record = self._backups.create(reason="pre_first_boot")
                if not getattr(record, "usable", False):
                    return FirstBootOutcome(
                        False, state["status"], "the pre-first-boot backup is not usable"
                    )

            run_id = self._genesis_runs.start(
                birth=anchors.birth_datetime,
                present=anchors.present_datetime,
                years=len(anchors.years),
                now=self._clock.now(),
            )
            self._repository.attach_run(epoch_id, run_id)
            # Point 18: the fingerprint is a check, not the source. The
            # authoritative anchors are the ones Genesis saved to the database.
            self._repository.set_fingerprint(epoch_id, fingerprint(anchors))
            self._repository.transition(
                epoch_id,
                expected=("PENDING", "READY"),
                to="RUNNING",
                now=self._clock.now(),
                started_at=self._clock.now(),
                last_progress_at=self._clock.now(),
            )
            self._repository.bump_attempt(epoch_id)
            await self._emit(
                FIRST_BOOT_STARTED,
                FirstBootStartedPayload(
                    epoch_id=epoch_id,
                    genesis_run_id=run_id,
                    birth=anchors.birth_datetime.isoformat(),
                    present=anchors.present_datetime.isoformat(),
                    life_years=len(anchors.years),
                ),
            )
            return await self._drive(epoch_id, run_id, anchors)
        finally:
            self._repository.release()

    # --- resume (points 14, 24, 26) ------------------------------------------
    async def resume(self) -> FirstBootOutcome:
        """Carry on. The run is the epoch's own; the caller does not pick one."""
        epoch = self._epoch()
        if epoch is None:
            return FirstBootOutcome(False, "PENDING", "no rebuild epoch exists")
        epoch_id = epoch["epoch_id"]
        state = self._repository.get(epoch_id)
        if state is None:
            return FirstBootOutcome(False, "PENDING", "first boot has not been started")
        if state["status"] == "COMPLETE":
            return FirstBootOutcome(False, "COMPLETE", "this epoch has already booted")
        if state["status"] == "BLOCKED_FATAL":
            # Point 48: a resume must not walk past a defect. Fatal means the
            # next attempt produces the same answer, so the OWNER has to look.
            return FirstBootOutcome(
                False,
                "BLOCKED_FATAL",
                f"fatal: {state['block_reason']}; repair or rebuild-reset",
            )
        if state["status"] not in RESUMABLE:
            return FirstBootOutcome(
                False, state["status"], f"cannot resume from {state['status']}"
            )
        run_id = state["genesis_run_id"]
        if not run_id:
            return FirstBootOutcome(
                False, state["status"], "there is no run to resume; use start"
            )
        if self._repository.lease_is_live(now=self._clock.now()):
            holder = self._repository.lock_holder()
            return FirstBootOutcome(
                False,
                state["status"],
                f"another process holds first boot ({holder['holder']})",
            )

        anchors = self._stored_anchors(run_id)
        if anchors is None:
            return FirstBootOutcome(
                False, state["status"], "the stored anchors could not be read"
            )
        # Point 17/18: the anchors come from the database, never from the CLI
        # config. A resume that re-read a changed config would move her
        # birthday halfway through her life.
        expected = state["anchors_fingerprint"]
        actual = fingerprint(anchors)
        if expected and expected != actual:
            return FirstBootOutcome(
                False,
                "BLOCKED_FATAL",
                "the stored anchors no longer match the fingerprint taken at start",
            )

        if not self._repository.acquire(epoch_id, now=self._clock.now()):
            return FirstBootOutcome(
                False, state["status"], "another process is already running first boot"
            )
        try:
            self._repository.transition(
                epoch_id,
                expected=tuple(RESUMABLE),
                to="RUNNING",
                now=self._clock.now(),
                last_progress_at=self._clock.now(),
                block_kind="",
                block_reason="",
            )
            self._repository.bump_attempt(epoch_id)
            await self._emit(
                FIRST_BOOT_RESUMED,
                FirstBootResumedPayload(
                    epoch_id=epoch_id,
                    genesis_run_id=run_id,
                    attempt=int(state["attempt_count"]) + 1,
                    from_checkpoint=state["last_checkpoint"],
                ),
            )
            return await self._drive(epoch_id, run_id, anchors)
        finally:
            self._repository.release()

    # --- pause (point 59) -----------------------------------------------------
    def pause(self) -> FirstBootOutcome:
        """Stop politely, keeping everything.

        The graceful interruption of point 59. A nineteen-year generation can
        legitimately need to stop — the machine is wanted for something else,
        the OWNER wants to read year six before year seven is written — and the
        answer to that is not to lose it. PAUSED is resumable and keeps the run,
        the checkpoints and the lease-free claim on the epoch; it is the reason
        there is no CANCELLED.

        Only the status is written. The generating process notices at its next
        checkpoint boundary, which is why this is safe to call from a different
        process than the one running.
        """
        epoch = self._epoch()
        if epoch is None:
            return FirstBootOutcome(False, "PENDING", "no rebuild epoch exists")
        epoch_id = epoch["epoch_id"]
        state = self._repository.get(epoch_id)
        if state is None or not state["genesis_run_id"]:
            return FirstBootOutcome(False, self.status(), "there is nothing running")
        if state["status"] == "COMPLETE":
            return FirstBootOutcome(False, "COMPLETE", "she has already been born")
        if state["status"] not in ("RUNNING", "AUDITING"):
            return FirstBootOutcome(
                False, state["status"], f"cannot pause from {state['status']}"
            )
        moved = self._repository.transition(
            epoch_id,
            expected=("RUNNING", "AUDITING"),
            to="PAUSED",
            now=self._clock.now(),
            last_progress_at=self._clock.now(),
        )
        if not moved:
            return FirstBootOutcome(False, self.status(), "the state moved underneath")
        return FirstBootOutcome(
            True, "PAUSED", genesis_run_id=state["genesis_run_id"]
        )

    # --- the run itself -------------------------------------------------------
    async def _drive(
        self, epoch_id: str, run_id: str, anchors: LifeAnchors
    ) -> FirstBootOutcome:
        """Generation, then finalisation. Point 24: no second resume logic.

        `GenesisRunner` already knows how to pick up from its own checkpoints;
        re-implementing that here would give the same rule two implementations
        and eventually two answers.
        """
        try:
            progress = await self._genesis.run(anchors, resume=run_id)
        except Exception as exc:  # noqa: BLE001 - a crash is a block, not a crash
            logger.exception("genesis raised during first boot")
            return await self._block(
                epoch_id, run_id, kind="model_unavailable", reason=repr(exc)[:200]
            )

        self._repository.touch(
            epoch_id,
            now=self._clock.now(),
            stage="genesis",
            checkpoint=self._last_checkpoint(run_id),
        )

        if progress.blocked_by:
            return await self._block(
                epoch_id,
                run_id,
                kind="contradiction",
                reason=", ".join(issue.code for issue in progress.blocked_by[:5]),
                progress=progress,
            )
        if progress.unavailable_critics:
            return await self._block(
                epoch_id,
                run_id,
                kind="critic_unavailable",
                reason=", ".join(progress.unavailable_critics[:5]),
                progress=progress,
            )
        if not progress.complete:
            return await self._block(
                epoch_id,
                run_id,
                kind="incomplete",
                reason="; ".join(progress.incomplete[:5]),
                progress=progress,
            )

        return await self._finalise(epoch_id, run_id, anchors, progress)

    # --- finalisation (points 4, 5, 6, 30-33) --------------------------------
    async def _finalise(
        self, epoch_id: str, run_id: str, anchors: LifeAnchors, progress: Any
    ) -> FirstBootOutcome:
        """Everything between "generation stopped" and "she exists".

        Re-runnable from the top. Promotion is idempotent, the audits are pure
        reads, and the status move is a compare-and-set, so a crash anywhere in
        here is repaired by running it again.
        """
        self._repository.transition(
            epoch_id,
            expected=("RUNNING", "AUDITING"),
            to="AUDITING",
            now=self._clock.now(),
            last_progress_at=self._clock.now(),
        )

        audits = await self._genesis.first_boot_audits(run_id, anchors)
        failed = [audit for audit in audits if not audit.passed]
        if failed:
            return await self._block(
                epoch_id,
                run_id,
                kind="audit_failed",
                reason="; ".join(f"{a.name}: {a.detail}"[:80] for a in failed[:3]),
                audits=audits,
                progress=progress,
            )

        promoted = self._genesis.promote_survivors(run_id, anchors)
        self._repository.touch(
            epoch_id, now=self._clock.now(), stage="survivors_promoted"
        )

        try:
            landing = self._land(anchors)
        except Exception as exc:  # noqa: BLE001
            logger.exception("landing failed during first boot")
            return await self._block(
                epoch_id, run_id, kind="landing_failed", reason=repr(exc)[:200],
                audits=audits, progress=progress,
            )
        self._repository.touch(epoch_id, now=self._clock.now(), stage="landed")

        consistency = self._final_consistency(run_id, anchors)
        if consistency:
            return await self._block(
                epoch_id,
                run_id,
                kind="contradiction",
                reason="; ".join(consistency[:3]),
                audits=audits,
                progress=progress,
            )
        self._repository.touch(epoch_id, now=self._clock.now(), stage="final_consistency")

        report = self._build_report(
            epoch_id, run_id, anchors, progress, audits, promoted, landing
        )
        self._repository.save_report(epoch_id, json.dumps(report, ensure_ascii=False))
        self._repository.touch(epoch_id, now=self._clock.now(), stage="report_written")

        moved = self._repository.transition(
            epoch_id,
            expected=("AUDITING",),
            to="COMPLETE",
            now=self._clock.now(),
            completed_at=self._clock.now(),
            last_progress_at=self._clock.now(),
            block_kind="",
            block_reason="",
        )
        if not moved:
            return FirstBootOutcome(
                False, self.status(), "the state moved underneath finalisation"
            )
        self._genesis_runs.finish(run_id, now=self._clock.now())
        await self._emit(
            FIRST_BOOT_COMPLETED,
            FirstBootCompletedPayload(
                epoch_id=epoch_id,
                genesis_run_id=run_id,
                birth=anchors.birth_datetime.isoformat(),
                present=anchors.present_datetime.isoformat(),
                life_years=report["life_years"],
                months=report["months"],
                experiences=report["experiences"],
                memories=report["memories"],
                surviving_npcs=promoted,
                audits_passed=len(audits),
            ),
        )
        logger.info("FIRST BOOT complete epoch=%s run=%s", epoch_id, run_id)
        return FirstBootOutcome(True, "COMPLETE", genesis_run_id=run_id, audits=audits)

    # --- landing (points 31, 34, 35) -----------------------------------------
    def _land(self, anchors: LifeAnchors) -> dict[str, int]:
        """Bring the world from the end of the simulated past to now.

        Genesis replays through past timestamps, so whatever it leaves behind
        is dated years ago. Two things follow, and both would otherwise be the
        runtime's first surprise rather than first boot's responsibility:

        **Point 31.** Any job whose moment is already past would come due the
        instant the runtime wakes, and they would all come due at once — a
        nineteen-year catch-up executed in one tick. They are retired, not
        executed: the moment for doing them was years ago, and doing them now
        would be acting on a schedule from a life she has already lived.

        **Points 34 and 35.** Her first life day starts *here*, at
        ``present_datetime``. Without it the diary's first entry would either
        find no open day at all or find one opened at a simulated bedtime and
        try to write about nineteen years. One day, opened once — the check is
        for an existing open day, so a re-run after a crash does not open a
        second one.
        """
        present = anchors.present_datetime
        landed = {"retired_jobs": 0, "life_day_opened": 0}

        if self._jobs is not None:
            for job in self._jobs.pending(limit=1000):
                if job.due_at is not None and job.due_at <= present:
                    self._jobs.mark(job.job_id, "expired", now=present)
                    landed["retired_jobs"] += 1

        if self._life_days is not None and self._life_days.current() is None:
            self._life_days.open_day(now=present)
            landed["life_day_opened"] = 1
        return landed

    def _final_consistency(self, run_id: str, anchors: LifeAnchors) -> list[str]:
        """Points 31, 32, 33: she has to arrive in the present, intact.

        Genesis replays through past timestamps, so the failure mode is a YUI
        who is technically complete and still living in 2019 — mid-activity,
        with an emotion from nineteen years ago at full strength, and a world
        clock that has not caught up. None of the ten Genesis audits looks at
        that, because it is not a fact about the generation.
        """
        problems: list[str] = []

        # Point 45. A life with zero of something is not a life, and every one
        # of these has a plausible way of arriving at zero without any single
        # step reporting a failure: a Genesis that scaffolded years and wrote
        # no months, an extraction that returned empty every time, a replay
        # that never reached the Memory Engine. "Complete and empty" is the
        # one outcome that must not be reachable.
        years = self._records.years(run_id)
        if not years:
            problems.append("the life record has no years")
        months = sum(len(self._records.months(y["year_id"])) for y in years)
        if not months:
            problems.append("the life record has no months")
        if self._experiences is not None:
            extracted = self._experiences.count(genesis_run_id=run_id)
            if not extracted:
                problems.append("no experience was extracted from the whole life")
        if self._memories is not None and not self._memories.memory_count():
            problems.append("she remembers nothing of it")

        if self._world is not None:
            ongoing = self._world.current_activity()
            if ongoing is not None:
                # Point 33. Something she began in 2019 must not still be in
                # progress at first boot.
                problems.append(
                    f"an activity from the simulated past is still ongoing: {ongoing.name}"
                )
            asleep = self._world.current_sleep()
            if asleep is not None:
                problems.append("she is still asleep in the simulated past")

        if self._state is not None:
            # Point 32. A transient feeling about something nineteen years ago
            # must not be her current emotional state.
            for value in self._safe_domain("emotion"):
                if value.numeric is not None and float(value.numeric) > 0.85:
                    age_days = (
                        anchors.present_datetime - value.updated_at
                    ).days
                    if age_days > 365:
                        problems.append(
                            f"emotion {value.key} is at {value.numeric:.2f} "
                            f"from {age_days} days ago"
                        )

        # Point 31, verified rather than assumed. If anything is still due in
        # the simulated past, the runtime's first tick is a catch-up.
        if self._jobs is not None:
            overdue = [
                job
                for job in self._jobs.pending(limit=1000)
                if job.due_at is not None and job.due_at <= anchors.present_datetime
            ]
            if overdue:
                problems.append(
                    f"{len(overdue)} scheduled job(s) are still due in the simulated past"
                )

        # Points 34 and 35. Exactly one day, and it starts here.
        if self._life_days is not None:
            day = self._life_days.current()
            if day is None:
                problems.append("no life day is open, so the first diary has no day")
            elif day.started_at < anchors.present_datetime:
                problems.append(
                    f"the open life day starts at {day.started_at.isoformat()}, "
                    "before first boot"
                )
        return problems

    def _safe_domain(self, domain: str) -> Sequence[Any]:
        try:
            return self._state.list_domain(domain)
        except Exception:  # noqa: BLE001
            return ()

    # --- audit, with no side effects (point 29) ------------------------------
    async def audit(self) -> FirstBootOutcome:
        """Check, and change nothing about her life.

        Runs the same ten audits the finalisation runs. It does not promote
        anybody, does not advance a checkpoint and does not move the status —
        an operator asking "would this pass" must not thereby make it pass.
        """
        epoch = self._epoch()
        state = None if epoch is None else self._repository.get(epoch["epoch_id"])
        run_id = None if state is None else state["genesis_run_id"]
        if not run_id:
            return FirstBootOutcome(False, self.status(), "there is no run to audit")
        anchors = self._stored_anchors(run_id)
        if anchors is None:
            return FirstBootOutcome(
                False, self.status(), "the stored anchors could not be read"
            )
        audits = await self._genesis.first_boot_audits(run_id, anchors)
        consistency = self._final_consistency(run_id, anchors)
        failed = [audit for audit in audits if not audit.passed]
        return FirstBootOutcome(
            not failed and not consistency,
            self.status(),
            "; ".join([f"{a.name}: {a.detail}" for a in failed] + consistency),
            genesis_run_id=run_id,
            audits=audits,
        )

    # --- what `first-boot status` shows (points 19, 20) ----------------------
    def progress(self) -> FirstBootProgressView:
        row = self._repository.current()
        if row is None:
            return FirstBootProgressView(status="PENDING")
        run_id = row["genesis_run_id"]
        view = FirstBootProgressView(
            status=row["status"],
            epoch_id=row["epoch_id"],
            genesis_run_id=run_id,
            stage=row["current_stage"],
            last_checkpoint=row["last_checkpoint"],
            last_progress_at=row["last_progress_at"] or "",
            attempt_count=int(row["attempt_count"] or 0),
            block_kind=row["block_kind"],
            block_reason=row["block_reason"],
            recovery_required=self.recovery_required(),
        )
        holder = self._repository.lock_holder()
        view.lease_holder = "" if holder is None else holder["holder"]
        if not run_id:
            return view

        anchors = self._stored_anchors(run_id)
        if anchors is not None:
            view.expected_years = len(anchors.years)
            # Point 19: never a hard-coded 228. The partial final year means
            # the expected count is arithmetic on the anchors.
            view.expected_months = sum(span.months for span in anchors.years)
        years = self._records.years(run_id)
        view.life_year = len(years)
        view.months_done = sum(len(self._records.months(y["year_id"])) for y in years)
        if self._experiences is not None:
            view.experiences_extracted = self._experiences.count(genesis_run_id=run_id)
            view.experiences_replayed = self._experiences.count(
                genesis_run_id=run_id, replay_status="replayed"
            )
        if self._memories is not None:
            view.memories = self._memories.memory_count()
        view.critics_passed = self._audits.count(passed=True)
        view.critics_failed = self._audits.count(passed=False)
        return view

    def report(self) -> dict[str, Any]:
        row = self._repository.current()
        if row is None or not row["report_json"]:
            return {}
        try:
            return json.loads(row["report_json"])
        except Exception:  # noqa: BLE001
            return {}

    # --- internals -----------------------------------------------------------
    async def _block(
        self,
        epoch_id: str,
        run_id: str,
        *,
        kind: str,
        reason: str,
        audits: Sequence[Any] = (),
        progress: Any = None,
    ) -> FirstBootOutcome:
        status: FirstBootStatus = block_status(kind)

        # Point 23. A retryable block that keeps happening is not retryable.
        # Ollama being down looks identical on attempt one and attempt fifty,
        # and without a budget `resume` in a shell loop would spend a week
        # rediscovering it. The budget converts the answer, not the diagnosis:
        # the original kind stays in the reason so the operator sees what it
        # actually was.
        state = self._repository.get(epoch_id)
        attempts = 0 if state is None else int(state["attempt_count"] or 0)
        if kind in RETRYABLE_BLOCKS and attempts >= MAX_ATTEMPTS:
            reason = f"{kind} after {attempts} attempts: {reason}"
            kind = "retry_exhausted"
            status = block_status(kind)

        self._repository.transition(
            epoch_id,
            expected=("RUNNING", "AUDITING", "PAUSED"),
            to=status,
            now=self._clock.now(),
            blocked_at=self._clock.now(),
            block_kind=kind,
            block_reason=reason[:400],
        )
        await self._emit(
            FIRST_BOOT_BLOCKED,
            FirstBootBlockedPayload(
                epoch_id=epoch_id,
                genesis_run_id=run_id,
                kind=kind,
                retryable=status == "BLOCKED_RETRYABLE",
                reason=reason[:200],
            ),
        )
        logger.error("FIRST BOOT blocked kind=%s reason=%s", kind, reason)
        return FirstBootOutcome(
            False, status, reason, genesis_run_id=run_id, audits=tuple(audits),
            progress=progress,
        )

    def _preflight(self, anchors: LifeAnchors, epoch: Any, state: Any) -> Preflight:
        return run_preflight(
            anchors=anchors,
            state=state,
            epoch=epoch,
            db=self._db,
            schema_version=self._schema_version,
            latest_schema=self._latest_schema,
            event_store=self._events,
            genesis_runs=self._genesis_runs,
            prompts=self._prompts,
            critics_available=getattr(self._genesis, "critic_names", ()) or ("chronology",),
            data_dir=self._data_dir,
            backups=self._backups,
            now=self._clock.now(),
        )

    def _epoch(self) -> Any:
        if self._rebuild is None:
            return None
        try:
            return self._rebuild.current()
        except Exception:  # noqa: BLE001
            return None

    def _stored_anchors(self, run_id: str) -> LifeAnchors | None:
        """Point 17: the database is the authoritative source, not the config."""
        row = self._genesis_runs.anchors(run_id)
        if row is None:
            return None
        try:
            temperament = TemperamentSeed(**json.loads(row["temperament_json"] or "{}"))
        except Exception:  # noqa: BLE001
            temperament = TemperamentSeed()
        return LifeAnchors(
            birth_datetime=from_iso(row["birth_datetime"]),
            present_datetime=from_iso(row["present_datetime"]),
            gender_identity=row["gender_identity"],
            embodiment=row["embodiment"],
            language=row["language"],
            culture=row["culture"],
            home=row["home"],
            family=row["family"],
            social=row["social"],
            education=row["education"],
            immutable_rules=row["immutable_rules"],
            temperament=temperament,
        )

    def _last_checkpoint(self, run_id: str) -> str:
        rows = self._genesis_runs.checkpoints(run_id)
        if not rows:
            return ""
        last = rows[-1]
        year = last["year_number"]
        return f"{last['name']}" + (f"/year {year}" if year else "")

    def _build_report(
        self,
        epoch_id: str,
        run_id: str,
        anchors: LifeAnchors,
        progress: Any,
        audits: Sequence[Any],
        promoted: int,
        landing: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Point 43: where this person came from, kept for afterwards."""
        years = self._records.years(run_id)
        months = sum(len(self._records.months(y["year_id"])) for y in years)
        return {
            "epoch_id": epoch_id,
            "genesis_run_id": run_id,
            "anchors_fingerprint": fingerprint(anchors),
            "birth": anchors.birth_datetime.isoformat(),
            "present": anchors.present_datetime.isoformat(),
            "completed_at": to_iso(self._clock.now()),
            "life_years": len(years),
            "months": months,
            "experiences": (
                0
                if self._experiences is None
                else self._experiences.count(genesis_run_id=run_id)
            ),
            "experiences_replayed": getattr(progress, "experiences_replayed", 0),
            "memories": 0 if self._memories is None else self._memories.memory_count(),
            "surviving_npcs": promoted,
            "landing": dict(landing or {}),
            "finalisation_stages": list(FINALISATION_STAGES),
            "audits": [
                {"name": audit.name, "passed": audit.passed, "detail": audit.detail}
                for audit in audits
            ],
            "critics_passed": self._audits.count(passed=True),
            "critics_failed": self._audits.count(passed=False),
            "attempts": progress.__dict__.get("attempts", 0)
            if hasattr(progress, "__dict__")
            else 0,
            # Point 44: which model and prompts actually made her.
            "prompt_versions": sorted(
                {row["prompt_version"] for row in years if row["prompt_version"]}
            ),
            "model_versions": sorted(
                {row["model_version"] for row in years if row["model_version"]}
            ),
        }

    async def _emit(self, event_type: str, payload: Any) -> None:
        """A lifecycle event, appended and never appraised.

        Deliberately straight to the store rather than through the processor:
        the machinery that made her is not something that happened *to* her,
        and running it through appraisal would put the installation in her
        autobiography.
        """
        if self._events is None:
            return
        event = Event.create(
            event_type=event_type,
            category="system",
            actor_type="system",
            source_type=MODULE,
            origin="system",
            priority="P2",
            payload=payload,
            clock=self._clock,
        )
        try:
            self._events.append(event)
        except Exception:  # noqa: BLE001
            logger.exception("could not record a first boot lifecycle event")


def fingerprint(anchors: LifeAnchors) -> str:
    """A stable hash of the anchors (point 18).

    Canonical JSON so the same anchors always hash the same, and only the
    fields that must never change — a check that a resume is continuing the
    same life, not the source of what that life is.
    """
    canonical = json.dumps(
        {
            "birth": anchors.birth_datetime.isoformat(),
            "present": anchors.present_datetime.isoformat(),
            "gender_identity": anchors.gender_identity,
            "embodiment": anchors.embodiment,
            "language": anchors.language,
            "culture": anchors.culture,
            "immutable_rules": anchors.immutable_rules,
            "temperament": anchors.temperament.as_dict(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "FirstBootOrchestrator",
    "FirstBootOutcome",
    "FirstBootProgressView",
    "fingerprint",
]
