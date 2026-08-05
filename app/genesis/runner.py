"""Genesis v2 (rebuild spec 34 — Phase 12).

    Genesis は FIRST BOOT 一度だけの高品質処理。通常会話の latency 要件を
    適用しない。処理時間より整合性・リアリティ・因果を優先する。

So this file optimises for nothing except being right. It is slow by design,
checkpointed because it is slow, and idempotent because a nineteen-year run
that cannot resume is one that has to be perfect on the first attempt.

The stages::

    anchors      decided once, ages computed in Python (34.1)
    Stage A      19 annual scaffolds — provisional (34.3)
    Stage B      up to 228 months, classified by density (34.4, 34.5)
    Stage C      each year rewritten from the months that happened (34.6)
    extraction   experiences, not sentences (34.11)
    replay       through the ordinary processor, forwards (34.12)
    audits       nine of them, before FIRST_BOOT_COMPLETE (34.20)

Two rules matter more than the rest.

**最終人格へ逆算しない.** The personality is whatever nineteen years of replay
produce. Nothing here writes a trait, and nothing consults a desired outcome —
which is why replay runs forwards through the same processor a live message
does, rather than fitting a curve to an answer decided in advance.

**Narrative is not memory (34.13).** Annual and monthly text is the objective
life record and the generation source. Only what passes through extraction and
the Memory Engine becomes something she remembers, and the difference between
those two is the difference between a biography and a life.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from app.clock import Clock, SystemClock, from_iso
from app.genesis.anchors import LifeAnchors, LifeYearSpan, month_spans
from app.genesis.critics import (
    CriticBoard,
    Review,
    ReviewTarget,
    check_identity,
)
from app.genesis.experience_world import validate_experience
from app.world.scope import (
    CURRENT_WORLD_MODEL_VERSION,
    is_current_world_model,
)
from app.genesis.ledger import ContinuityLedger
from app.genesis.models import (
    WORTH_DETAIL,
    AnnualScaffold,
    AnnualSynthesis,
    CriticIssue,
    ExperienceCandidate,
    MonthNarrative,
)
from app.llm.types import LLMMessage
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

MODULE = "genesis_runner"

SCAFFOLD_PROMPT = "genesis_annual_scaffold"
MONTH_PROMPT = "genesis_month"
SYNTHESIS_PROMPT = "genesis_annual_synthesis"
EXTRACTION_PROMPT = "genesis_experience_extraction"

#: 34.18's checkpoint names. Anything not on this list is not a resume point.
CHECKPOINTS: tuple[str, ...] = (
    "annual_scaffolds_done",
    "year_months_done",
    "year_critics_done",
    "year_extraction_done",
    "year_replay_done",
    "year_memory_done",
    "final_audits_done",
)

#: 34.20. Every one of these must pass before FIRST_BOOT_COMPLETE is written.
FIRST_BOOT_AUDITS: tuple[str, ...] = (
    # Hardening 1: her past has to reach the present. A run that stopped at the
    # last completed birthday leaves months missing that nothing else notices.
    "coverage",
    "chronology",
    "continuity",
    "identity",
    "experience_replay",
    "memory_health",
    "personality_growth",
    "knowledge_chronology",
    "npc_continuity",
    "no_real_user_before_first_boot",
)



def _replay_refusal(row) -> str:
    """Why this stored experience may not become an Event, if it may not.

    Provenance first. A row written before the world model carries version 0,
    and there is no way to check it after the fact — the metadata columns hold
    a migration's defaults, not a judgement. It is not replayed, and the
    resolution is a fresh Genesis rather than a reinterpretation.
    """
    import json as _json

    if not is_current_world_model(_int_column(row, "world_model_version")):
        return "world_model_unverified"
    try:
        participants = tuple(_json.loads(row["participants_json"] or "[]"))
        actors = tuple(_json.loads(row["actor_subjects_json"] or "[]"))
    except Exception:  # noqa: BLE001 - unreadable metadata is not a pass
        return "world_metadata_unreadable"
    scope = row["interaction_scope"] or ""
    if not scope:
        return "world_metadata_missing"
    return validate_experience(
        participants=participants,
        interaction_scope=scope,
        actor_subjects=actors,
    )


def _int_column(row, name: str) -> int:
    try:
        return int(row[name] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        return 0


def _stored_participants(row) -> tuple[str, ...]:
    """The world metadata a month was written with.

    Rows from before migration 36 have the column's default — an empty list —
    which is what they were generated under: the substring critic was already
    looking for the USER, so a month that got through named nobody it should
    not have. Nothing is inferred from the prose here.
    """
    import json as _json

    try:
        return tuple(_json.loads(row["participants_json"] or "[]"))
    except Exception:  # noqa: BLE001 - a malformed blob names nobody
        return ()


class Extraction(BaseModel):
    """What one month yielded. Deliberately a short list (34.11).

    全 narrative sentence を Event にしない — a month of prose is not a queue
    of events, and repetitive daily life is one compressed experience rather
    than thirty separate ones.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    experiences: tuple[ExperienceCandidate, ...] = ()


@dataclass(slots=True)
class GenesisProgress:
    """What the run has done so far. Written to the log and the report."""

    run_id: str
    years_scaffolded: int = 0
    months_written: int = 0
    months_detailed: int = 0
    experiences_extracted: int = 0
    experiences_replayed: int = 0
    memories_encoded: int = 0
    audits_run: int = 0
    audits_failed: int = 0
    blocked_by: list[CriticIssue] = field(default_factory=list)
    #: Required critics that could not run. Different from `blocked_by`: this
    #: is "we could not check", which is worth retrying, rather than "this is
    #: wrong", which is not.
    unavailable_critics: list[str] = field(default_factory=list)
    #: Postconditions that were not met, named. A run with anything here is
    #: incomplete and resumable, never finished.
    incomplete: list[str] = field(default_factory=list)
    #: The year the run stopped at, if it stopped.
    stopped_at: int | None = None

    @property
    def complete(self) -> bool:
        return self.stopped_at is None and not self.incomplete

    @property
    def retryable(self) -> bool:
        """True when what stopped it was "unverified", not "wrong"."""
        return bool(self.incomplete or self.unavailable_critics) and not self.blocked_by

    def describe(self) -> str:
        return (
            f"years={self.years_scaffolded} months={self.months_written} "
            f"detailed={self.months_detailed} experiences={self.experiences_extracted} "
            f"replayed={self.experiences_replayed} encoded={self.memories_encoded} "
            f"audits={self.audits_run} "
            f"failed={self.audits_failed} stopped_at={self.stopped_at} "
            f"incomplete={len(self.incomplete)}"
        )


@dataclass(frozen=True, slots=True)
class AuditResult:
    name: str
    passed: bool
    detail: str = ""


class GenesisRunner:
    """Builds nineteen years, once, carefully."""

    name = MODULE

    def __init__(
        self,
        *,
        runs: Any,
        records: Any,
        entities: Any,
        audits: Any,
        experiences: Any = None,
        critics: CriticBoard | None = None,
        structured: Any = None,
        prompts: Any = None,
        processor: Any = None,
        memory: Any = None,
        memory_engine: Any = None,
        society: Any = None,
        knowledge_repo: Any = None,
        event_store: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._runs = runs
        self._records = records
        self._entities = entities
        self._audits = audits
        #: 34.19 at experience granularity. Without it a crash mid-replay
        #: leaves the year checkpoint unwritten and the resume relives every
        #: experience in the year.
        self._experiences = experiences
        self._critics = critics
        self._structured = structured
        self._prompts = prompts
        self._processor = processor
        #: The repository, for the audit to count rows.
        self._memory = memory
        #: The engine, which is the only way an experience becomes a memory.
        self._memory_engine = memory_engine
        self._society = society
        self._knowledge_repo = knowledge_repo
        self._events = event_store
        self._clock = clock or SystemClock()

    # --- the whole thing -----------------------------------------------------
    async def run(
        self, anchors: LifeAnchors, *, resume: str | None = None, max_years: int | None = None
    ) -> GenesisProgress:
        """Build a life. Resumable, and never twice for the same year."""
        run_id = resume or self._runs.start(
            birth=anchors.birth_datetime,
            present=anchors.present_datetime,
            years=len(anchors.years),
            now=self._clock.now(),
        )
        self._runs.save_anchors(run_id, anchors)
        progress = GenesisProgress(run_id=run_id)
        ledger = ContinuityLedger(self._entities, genesis_run_id=run_id)

        spans = anchors.years
        if max_years is not None:
            spans = spans[:max_years]

        # --- Stage A -----------------------------------------------------
        self._runs.set_stage(run_id, "stage_a")
        for span in spans:
            await self._scaffold(run_id, anchors, span, ledger, progress)

        # The checkpoint is a *postcondition*, not a place in the code. It says
        # every year has a scaffold, and it is only written when that is true —
        # a model that returned nothing for year seven leaves the run
        # incomplete and retryable rather than marked done (hardening 2).
        if self._all_scaffolded(run_id, spans):
            self._runs.checkpoint(
                run_id, name="annual_scaffolds_done", now=self._clock.now()
            )
        else:
            progress.incomplete.append("annual_scaffolds")
            logger.warning("Stage A incomplete; not checkpointing")

        # --- Stage B and C, year by year ---------------------------------
        for span in spans:
            year = self._records.year(run_id, span.year_number)
            if year is None:
                # Stage A produced nothing for this year. Later years depend on
                # it as their `previous`, so continuing would generate a life
                # with a hole in the middle.
                progress.incomplete.append(f"year_{span.year_number}_scaffold")
                progress.stopped_at = span.year_number
                break
            proceed = await self._year(run_id, anchors, span, year, ledger, progress)
            if not proceed:
                # Hardening 3: a blocked year stops the *run*. Continuing into
                # year eight while year seven is known to be wrong builds
                # everything after it on a foundation that is being repaired.
                progress.stopped_at = span.year_number
                logger.error(
                    "genesis stopped at year %d; later years not attempted",
                    span.year_number,
                )
                break

        if progress.stopped_at is None:
            self._runs.set_stage(run_id, "audits")
        return progress

    # --- Stage A (34.3) ------------------------------------------------------
    async def _scaffold(
        self,
        run_id: str,
        anchors: LifeAnchors,
        span: LifeYearSpan,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> None:
        existing = self._records.year(run_id, span.year_number)
        if existing is not None and existing["scaffold_text"]:
            progress.years_scaffolded += 1
            return  # already done; a resume must not regenerate it

        # Hardening 5: the previous year comes from the *record*, not from a
        # local variable. A resume that started mid-Stage-A would otherwise
        # hand year eight a `previous` of None and restart her life there.
        scaffold = await self._generate(
            SCAFFOLD_PROMPT,
            AnnualScaffold,
            anchors=anchors.describe(),
            age_start=span.age_start,
            age_end=span.age_end,
            period=f"{span.start.date()} 〜 {span.end.date()}",
            previous=self._previous_year_text(run_id, span.year_number),
            continuity=ledger.render(span.start),
            temperament=str(anchors.temperament.as_dict()),
        )
        if scaffold is None:
            progress.incomplete.append(f"year_{span.year_number}_scaffold")
            return

        self._records.add_year(
            run_id=run_id,
            year_number=span.year_number,
            calendar_start=span.start,
            calendar_end=span.end,
            age_start=span.age_start,
            age_end=span.age_end,
            scaffold_text=scaffold.summary,
            prompt_version=self._version(SCAFFOLD_PROMPT),
            model_version=self._model(),
            now=self._clock.now(),
        )
        self._note_all(ledger, scaffold.people, scaffold.interests, scaffold.threads, span.start)
        progress.years_scaffolded += 1

    # --- one year of Stage B + C --------------------------------------------
    async def _year(
        self,
        run_id: str,
        anchors: LifeAnchors,
        span: LifeYearSpan,
        year: Any,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> bool:
        """One year, all stages. Returns whether the run may continue.

        False means something is wrong or unverified in *this* year, and
        hardening 3 says the run stops rather than building year eight on it.
        """
        year_id = year["year_id"]
        expected_months = span.months

        if not self._runs.reached(run_id, "year_months_done", span.year_number):
            for number, start_at, end_at, age in month_spans(span, anchors.birth_datetime):
                await self._month(
                    run_id, anchors, year, number, start_at, end_at, age, ledger, progress
                )
            written = [
                row for row in self._records.months(year_id) if row["narrative"]
            ]
            if len(written) < expected_months:
                # Hardening 2: not done, so not checkpointed. A resume comes
                # back and generates the months that are missing.
                progress.incomplete.append(
                    f"year_{span.year_number}_months "
                    f"({len(written)}/{expected_months})"
                )
                return False
            self._runs.checkpoint(
                run_id, name="year_months_done", year_number=span.year_number,
                now=self._clock.now(),
            )

        # --- critics, before anything is believed (34.10, GEN-CRITIC-001) --
        if not self._runs.reached(run_id, "year_critics_done", span.year_number):
            review = await self._review_year(run_id, anchors, span, year_id, ledger, progress)
            if not review.ok:
                logger.error(
                    "genesis year %d blocked: issues=%s unavailable=%s",
                    span.year_number,
                    [issue.code for issue in review.blocking],
                    review.unavailable,
                )
                return False
            self._runs.checkpoint(
                run_id, name="year_critics_done", year_number=span.year_number,
                now=self._clock.now(),
            )

        # --- Stage C (34.6) ------------------------------------------------
        if not await self._synthesise(run_id, anchors, span, year_id):
            progress.incomplete.append(f"year_{span.year_number}_synthesis")
            return False

        # --- extraction, persisted before any replay (34.11, 34.19) --------
        if not self._runs.reached(run_id, "year_extraction_done", span.year_number):
            extracted = await self._extract(run_id, year_id, span, progress)
            if not extracted:
                progress.incomplete.append(f"year_{span.year_number}_extraction")
                return False
            self._runs.checkpoint(
                run_id, name="year_extraction_done", year_number=span.year_number,
                now=self._clock.now(),
            )

        # --- replay, one experience at a time (34.12) ----------------------
        if not self._runs.reached(run_id, "year_replay_done", span.year_number):
            if not await self._replay(run_id, span.year_number, progress):
                progress.incomplete.append(f"year_{span.year_number}_replay")
                return False
            self._runs.checkpoint(
                run_id, name="year_replay_done", year_number=span.year_number,
                now=self._clock.now(),
            )

        # --- memory (34.13): the encoding the replay produced ---------------
        # Hardening 8: this checkpoint used to be named and never written.
        # It now records a real postcondition — every experience of the year
        # went through the processor, which is the only route into memory.
        if not self._runs.reached(run_id, "year_memory_done", span.year_number):
            pending = self._pending_count(run_id, span.year_number)
            if pending:
                progress.incomplete.append(
                    f"year_{span.year_number}_memory ({pending} unreplayed)"
                )
                return False
            self._runs.checkpoint(
                run_id, name="year_memory_done", year_number=span.year_number,
                now=self._clock.now(),
                detail=f"{self._replayed_count(run_id, span.year_number)} experiences encoded",
            )
        return True

    async def _month(
        self,
        run_id: str,
        anchors: LifeAnchors,
        year: Any,
        number: int,
        start: datetime,
        end: datetime,
        age: int,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> None:
        existing = self._records.month(year["year_id"], number)
        if existing is not None and existing["narrative"]:
            progress.months_written += 1
            return

        # Hardening 5 again: the previous month is read back, so a resume that
        # restarts inside a year does not hand month seven an empty past.
        month = await self._generate(
            MONTH_PROMPT,
            MonthNarrative,
            anchors=anchors.describe(),
            scaffold=year["scaffold_text"],
            previous=self._previous_month_text(run_id, year, number),
            continuity=ledger.render(start),
            age=age,
            period=f"{start.date()} 〜 {end.date()}",
        )
        if month is None:
            progress.incomplete.append(
                f"year_{year['year_number']}_month_{number}"
            )
            return

        # 34.5: only meaningful and above earn a second call. A routine month
        # may still be richly described, but it does not get a crisis added to
        # justify the expense.
        if month.importance in WORTH_DETAIL:
            progress.months_detailed += 1

        # identity v2. The world check runs *before* the row exists, because a
        # committed month is a durable past: it becomes a life record, then an
        # experience, then a memory. Auditing after the write means deciding
        # what to do with something already written.
        world = check_identity(
            ReviewTarget(
                target_type="month",
                target_id=f"{year['year_id']}#{number}",
                text=month.narrative,
                subject=month.subject,
                participants=month.participants,
                interaction_scope=month.interaction_scope,
            )
        )
        if not world.passed:
            # Reported through the same channel the critics use, so it lands as
            # a fatal block rather than an error: a world contradiction is not
            # something the next attempt answers differently, and the OWNER has
            # to look. What differs from a critic verdict is only *when* — the
            # month never becomes a row, because a committed month becomes a
            # life record, then an experience, then a memory.
            progress.blocked_by.extend(world.issues)
            progress.incomplete.append(
                f"year_{year['year_number']}_month_{number} (world)"
            )
            return

        month_id = self._records.add_month(
            year_id=year["year_id"],
            month_number=number,
            month_start=start,
            month_end=end,
            age_start=age,
            age_end=anchors.age_on(end),
            narrative=month.narrative,
            importance_class=month.importance,
            prompt_version=self._version(MONTH_PROMPT),
            model_version=self._model(),
            participants=month.participants,
            interaction_scope=month.interaction_scope,
        )
        self._note_all(
            ledger, month.people, month.interests, month.threads, start, month_id=month_id
        )
        progress.months_written += 1

    async def _review_year(
        self,
        run_id: str,
        anchors: LifeAnchors,
        span: LifeYearSpan,
        year_id: str,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> Review:
        if self._critics is None:
            return Review(ok=True)
        blocked: list[CriticIssue] = []
        unavailable: list[str] = []
        for month in self._records.months(year_id):
            target = ReviewTarget(
                target_type="month",
                target_id=month["month_id"],
                text=month["narrative"],
                age_start=month["age_start"],
                age_end=month["age_end"],
                anchors=anchors.describe(),
                continuity=ledger.render(span.start),
            )
            review = await self._critics.review(target)
            progress.audits_run += 1
            if not review.ok:
                progress.audits_failed += 1
                blocked.extend(review.blocking)
                unavailable.extend(review.unavailable)
        progress.blocked_by.extend(blocked)
        progress.unavailable_critics.extend(unavailable)
        return Review(
            ok=not blocked and not unavailable,
            blocking=tuple(blocked),
            unavailable=tuple(dict.fromkeys(unavailable)),
        )

    async def _synthesise(
        self, run_id: str, anchors: LifeAnchors, span: LifeYearSpan, year_id: str
    ) -> bool:
        year = self._records.year(run_id, span.year_number)
        if year is not None and year["final_summary"]:
            return True  # a resume must not rewrite a finished synthesis
        months = self._records.months(year_id)
        if not months:
            return False
        synthesis = await self._generate(
            SYNTHESIS_PROMPT,
            AnnualSynthesis,
            anchors=anchors.describe(),
            scaffold=year["scaffold_text"] if year is not None else "-",
            months="\n\n".join(
                f"[{row['month_number']}月 / {row['importance_class']}]\n{row['narrative']}"
                for row in months
            ),
            age_start=span.age_start,
            age_end=span.age_end,
        )
        if synthesis is None or not synthesis.summary.strip():
            # Hardening 2: no summary means the year is not synthesised. Saying
            # otherwise would leave `final_summary` empty behind a status that
            # claims it is not.
            return False
        # 34.6: where they disagree, the months win. The scaffold stays on the
        # row so the disagreement remains visible.
        self._records.synthesise(year_id, summary=synthesis.summary)
        return True

    # --- 34.11: experiences, not sentences ----------------------------------
    async def _extract(
        self, run_id: str, year_id: str, span: LifeYearSpan, progress: GenesisProgress
    ) -> bool:
        """Pull experiences out of the months and *persist* them.

        Persisted before any replay, and keyed on (month, sequence), so a crash
        between extraction and replay loses nothing and a re-extraction after
        one produces the same rows rather than a second set.
        """
        if self._experiences is None:
            return True
        staged = 0
        for month in self._records.months(year_id):
            if self._experiences.for_month(month["month_id"]):
                continue  # already extracted; a resume does not redo it
            extracted = await self._generate(
                EXTRACTION_PROMPT,
                Extraction,
                narrative=month["narrative"],
                period=f"{month['month_start']} 〜 {month['month_end']}",
                importance=month["importance_class"],
                # The month's own world metadata, so extraction knows what it
                # is compressing rather than guessing who was there.
                month_participants=", ".join(_stored_participants(month)) or "(なし)",
                month_interaction_scope=(
                    month["interaction_scope"] or "local_to_subject_world"
                ),
            )
            if extracted is None:
                progress.incomplete.append(f"month_{month['month_id']}_extraction")
                return False
            month_participants = _stored_participants(month)
            for sequence, candidate in enumerate(extracted.experiences):
                # Before the row exists. An experience that is written and then
                # rejected is a past that already happened — it has an id, a
                # month, a sequence, and the next resume finds it there.
                refusal = validate_experience(
                    participants=candidate.participants,
                    interaction_scope=candidate.interaction_scope,
                    actor_subjects=[
                        ref.subject for ref in candidate.actor_refs
                    ],
                    month_participants=month_participants,
                )
                if refusal:
                    progress.blocked_by.append(
                        CriticIssue(
                            severity="fatal",
                            target_id=month["month_id"],
                            code="EXPERIENCE_WORLD_CONTRADICTION",
                            reason=f"extracted experience is not of this month: {refusal}",
                            repair_scope="month",
                        )
                    )
                    progress.incomplete.append(
                        f"month_{month['month_id']}_experience_{sequence} (world)"
                    )
                    return False
                self._experiences.stage(
                    genesis_run_id=run_id,
                    month_id=month["month_id"],
                    year_number=span.year_number,
                    sequence=sequence,
                    candidate=candidate,
                )
                staged += 1
        progress.experiences_extracted += staged
        return True

    # --- 34.12: forwards, through the ordinary pipeline ----------------------
    async def _replay(
        self, run_id: str, year_number: int, progress: GenesisProgress
    ) -> bool:
        """Live it, in order, one experience at a time.

        最終人格へ逆算しない: each experience goes through the same processor a
        real message does, and whatever personality comes out the other end is
        the answer rather than the target.

        The row is marked replayed immediately after its event is processed, so
        a crash on experience 41 leaves the first 40 marked and the resume
        starts at 41. Marking the whole year at the end would replay all of
        them, and she would live the same fortnight twice.
        """
        if self._processor is None or self._experiences is None:
            return True
        from app.events.model import Event
        from app.simulation.events import SIMULATED_EXPERIENCE, SimulatedExperiencePayload

        for row in self._experiences.pending(run_id, year_number=year_number):
            # Defence in depth, at the boundary where an experience becomes an
            # Event and from there a memory, a mood, a personality drift. The
            # pre-stage check ran against an object in memory; this runs
            # against what is actually on disk — which is what a restart, a
            # legacy row, or a hand-edited database presents.
            refusal = _replay_refusal(row)
            if refusal:
                logger.error(
                    "experience %s is not replayable: %s", row["experience_id"], refusal
                )
                progress.incomplete.append(
                    f"experience_{row['experience_id']} ({refusal})"
                )
                return False
            event = Event.create(
                event_type=SIMULATED_EXPERIENCE,
                category="internal",
                actor_type="yui",
                source_type=MODULE,
                origin="simulated_past",
                priority="P4",
                payload=SimulatedExperiencePayload(
                    block_id=row["month_id"],
                    experience_class=row["importance"],
                    summary=row["action"] or row["context"],
                    text=" ".join(
                        part
                        for part in (row["context"], row["action"], row["outcome"])
                        if part
                    ),
                    felt_significance=row["social_significance"],
                    involves_other_person=bool(row["actors"]),
                ),
                clock=self._clock,
                occurred_at=from_iso(row["occurred_at"]),
            )
            await self._processor.process(event)
            # 34.13: the *only* route from the life record into what she
            # remembers. The narrative is not memory; this is where an
            # experience is offered to the encoder, which may refuse it.
            await self._encode(event, progress)
            # Marked immediately, and only after both returned. An exception
            # above leaves this row `pending`, which is exactly right.
            self._experiences.mark_replayed(
                row["experience_id"], event_id=event.event_id, now=self._clock.now()
            )
            progress.experiences_replayed += 1
        return True

    async def _encode(self, event: Any, progress: GenesisProgress) -> None:
        """Offer the experience to the Memory Engine, on simulated time.

        Forgetting runs as the years pass rather than once at the end, so a
        memory from her fourth year has had fifteen years of decay by the time
        she boots — which is what makes what survives a selection rather than
        a complete record.
        """
        if self._memory_engine is None:
            return
        try:
            self._memory_engine.observe(
                event, conversation_id=None, now=event.occurred_at
            )
            self._memory_engine.close_due_episodes(now=event.occurred_at)
            results = await self._memory_engine.encode_pending(
                limit=20, now=event.occurred_at
            )
            self._memory_engine.apply_forgetting(now=event.occurred_at)
        except Exception:  # noqa: BLE001 - recorded, and the life continues
            logger.exception("genesis memory encoding failed")
            return
        progress.memories_encoded += sum(1 for result in results if result.encoded)

    # --- 34.20: the nine audits ---------------------------------------------
    async def first_boot_audits(
        self, run_id: str, anchors: LifeAnchors
    ) -> tuple[AuditResult, ...]:
        """Everything that must pass before the gateway may come online.

        Failing any of them is not a warning. 34.20 makes FIRST_BOOT_COMPLETE
        conditional on all nine, and the Discord character gateway conditional
        on FIRST_BOOT_COMPLETE.

        No audit passes because it could not look. A missing dependency is
        reported as ``unavailable`` and blocks, because "I could not check
        whether her memory is intact" and "her memory is intact" are not the
        same sentence.
        """
        results = [
            self._audit_coverage(run_id, anchors),
            self._audit_chronology(run_id, anchors),
            self._audit_continuity(run_id),
            self._audit_identity(run_id),
            self._audit_replay(run_id),
            self._audit_memory_health(run_id),
            self._audit_personality_growth(run_id),
            self._audit_knowledge_chronology(anchors),
            self._audit_npc_continuity(run_id),
            self._audit_no_real_user(),
        ]
        if all(result.passed for result in results):
            self._runs.checkpoint(
                run_id, name="final_audits_done", now=self._clock.now()
            )
        return tuple(results)

    def _audit_coverage(self, run_id: str, anchors: LifeAnchors) -> AuditResult:
        """Hardening 1: her past reaches the present with no gap.

        A run that stopped at the last completed birthday leaves up to twelve
        months missing between the end of Genesis and her first real
        conversation, and nothing else here would notice.
        """
        years = self._records.years(run_id)
        if not years:
            return AuditResult("coverage", False, "no years were generated")
        expected = {span.year_number for span in anchors.years}
        got = {row["year_number"] for row in years}
        missing = sorted(expected - got)
        if missing:
            return AuditResult("coverage", False, f"years missing: {missing}")

        last_span = anchors.years[-1]
        months = self._records.months(
            next(row["year_id"] for row in years if row["year_number"] == last_span.year_number)
        )
        if len(months) < last_span.months:
            return AuditResult(
                "coverage",
                False,
                f"the final year has {len(months)}/{last_span.months} months",
            )
        latest = max(from_iso(row["month_end"]) for row in months)
        if latest < anchors.present_datetime:
            return AuditResult(
                "coverage",
                False,
                f"her past ends at {latest.date()}, before {anchors.present_datetime.date()}",
            )
        return AuditResult("coverage", True)

    def _audit_chronology(self, run_id: str, anchors: LifeAnchors) -> AuditResult:
        """Ages line up with the years they are attached to.

        Checked against Python's own arithmetic rather than against anything
        the model said, which is the point of 34.1.
        """
        years = self._records.years(run_id)
        if not years:
            return AuditResult("chronology", False, "no years to check")
        for year in years:
            if year["age_start"] != year["year_number"] - 1:
                return AuditResult(
                    "chronology",
                    False,
                    f"year {year['year_number']} starts at age {year['age_start']}",
                )
            for month in self._records.months(year["year_id"]):
                if not (year["age_start"] <= month["age_start"] <= year["age_end"]):
                    return AuditResult(
                        "chronology",
                        False,
                        f"month {month['month_number']} is age {month['age_start']} "
                        f"inside a year covering {year['age_start']}-{year['age_end']}",
                    )
        return AuditResult("chronology", True)

    def _audit_continuity(self, run_id: str) -> AuditResult:
        entities = self._entities.all_for(run_id)
        if not entities:
            return AuditResult(
                "continuity", False, "the continuity ledger is empty"
            )
        orphaned = [entity for entity in entities if entity["last_seen_at"] is None]
        if len(orphaned) > len(entities) // 2:
            return AuditResult(
                "continuity", False, f"{len(orphaned)}/{len(entities)} entities never recur"
            )
        return AuditResult("continuity", True)

    def _audit_identity(self, run_id: str) -> AuditResult:
        from app.genesis.critics import check_identity

        checked = 0
        for year in self._records.years(run_id):
            for month in self._records.months(year["year_id"]):
                checked += 1
                verdict = check_identity(
                    ReviewTarget(
                        target_type="month",
                        target_id=month["month_id"],
                        text=month["narrative"],
                        participants=_stored_participants(month),
                        interaction_scope=(
                            month["interaction_scope"] or "local_to_subject_world"
                        ),
                    )
                )
                if not verdict.passed:
                    return AuditResult(
                        "identity", False, verdict.issues[0].reason if verdict.issues else ""
                    )
        if not checked:
            return AuditResult("identity", False, "there was nothing to check")
        return AuditResult("identity", True, f"{checked} months")

    def _audit_replay(self, run_id: str) -> AuditResult:
        """34.13: narrative is not memory. Replay is the only route in."""
        if self._experiences is None:
            return AuditResult(
                "experience_replay", False, "no experience record to audit"
            )
        pending = self._experiences.count(
            genesis_run_id=run_id, replay_status="pending"
        )
        replayed = self._experiences.count(
            genesis_run_id=run_id, replay_status="replayed"
        )
        if pending:
            return AuditResult(
                "experience_replay", False, f"{pending} experiences never replayed"
            )
        if not replayed:
            return AuditResult(
                "experience_replay", False, "nothing was replayed at all"
            )
        # And the events really exist: a marked row with no event is a lie.
        if self._events is not None:
            from app.simulation.events import SIMULATED_EXPERIENCE

            in_store = sum(
                1
                for event in self._events.recent(limit=5000)
                if event.event_type == SIMULATED_EXPERIENCE
            )
            if in_store < replayed:
                return AuditResult(
                    "experience_replay",
                    False,
                    f"{replayed} marked replayed but {in_store} events exist",
                )
        return AuditResult("experience_replay", True, f"{replayed} experiences")

    def _audit_memory_health(self, run_id: str) -> AuditResult:
        """Did replay actually reach the Memory Engine?

        A count of zero after a nineteen-year replay means the encoding path is
        broken, and reporting "0 memories" as a pass is the shape of audit this
        hardening exists to remove.
        """
        if self._memory is None:
            return AuditResult(
                "memory_health", False, "no memory repository to audit"
            )
        if self._experiences is None:
            return AuditResult("memory_health", False, "no experience record")
        replayed = self._experiences.count(
            genesis_run_id=run_id, replay_status="replayed"
        )
        count = self._memory.memory_count()
        if replayed and not count:
            return AuditResult(
                "memory_health",
                False,
                f"{replayed} experiences replayed and no memory was encoded",
            )
        return AuditResult("memory_health", True, f"{count} memories")

    def _audit_personality_growth(self, run_id: str) -> AuditResult:
        """最終人格へ逆算しない — so this checks that growth *came from* replay.

        Two failures to catch. A personality that never moved means the replay
        did not reach the psychological pipeline. A personality that exists
        with no replay behind it means something wrote one directly, which is
        the back-calculation 34.12 forbids.
        """
        if self._experiences is None:
            return AuditResult("personality_growth", False, "no experience record")
        replayed = self._experiences.count(
            genesis_run_id=run_id, replay_status="replayed"
        )
        if self._events is None:
            return AuditResult("personality_growth", False, "no event store to audit")
        from app.simulation.events import SIMULATED_EXPERIENCE

        processed = sum(
            1
            for event in self._events.recent(limit=5000)
            if event.event_type == SIMULATED_EXPERIENCE
        )
        if replayed and not processed:
            return AuditResult(
                "personality_growth",
                False,
                "experiences are marked replayed but produced no events",
            )
        if processed and not replayed:
            return AuditResult(
                "personality_growth",
                False,
                "simulated experiences exist that no extraction produced",
            )
        return AuditResult("personality_growth", True, f"{processed} lived events")

    def _audit_knowledge_chronology(self, anchors: LifeAnchors) -> AuditResult:
        """34.9: nothing she could not have known yet.

        Uses Phase 11's provenance: `available_from` on every knowledge row,
        checked against the moment she is supposed to have learned it.
        """
        if self._knowledge_repo is None:
            return AuditResult(
                "knowledge_chronology", False, "no knowledge repository to audit"
            )
        for item in self._knowledge_repo.all_knowledge(limit=500):
            if item.available_from > anchors.present_datetime:
                return AuditResult(
                    "knowledge_chronology",
                    False,
                    f"{item.statement!r} was not available until {item.available_from}",
                )
        return AuditResult("knowledge_chronology", True)

    def _audit_npc_continuity(self, run_id: str) -> AuditResult:
        """People exist, recur, and the ones still around became NPCs.

        A ledger with people in it and no sightings means the continuity
        retrieval never fed anything back into generation — everybody was
        introduced once and forgotten, which is not a social history.
        """
        people = [
            entity for entity in self._entities.all_for(run_id) if entity["type"] == "NPC"
        ]
        if not people:
            return AuditResult(
                "npc_continuity", False, "nobody was ever in her life"
            )
        seen_again = [person for person in people if person["last_seen_at"] is not None]
        if not seen_again:
            return AuditResult(
                "npc_continuity",
                False,
                f"{len(people)} people, none of whom recurs",
            )
        return AuditResult(
            "npc_continuity", True, f"{len(seen_again)}/{len(people)} recur"
        )

    def _audit_no_real_user(self) -> AuditResult:
        """Spec 2.10 and 34.20: no real Discord history before first boot.

        A simulated past that contains the USER would make her first real
        conversation a continuation of one that never happened.
        """
        if self._events is None:
            return AuditResult(
                "no_real_user_before_first_boot", False, "no event store to audit"
            )
        for event in self._events.recent(limit=5000):
            if event.origin == "real_discord" and event.actor_type == "user":
                return AuditResult(
                    "no_real_user_before_first_boot",
                    False,
                    f"{event.event_type} from the real USER exists before first boot",
                )
        return AuditResult("no_real_user_before_first_boot", True)

    def ready_for_completion(self, run_id: str, anchors: LifeAnchors) -> bool:
        """As far as Genesis goes: is this life finished?

        Phase 13, point 5. This is the *most* a runner may say. Whether a
        finished Genesis is grounds for opening the character plane is a
        different judgement — it involves survivors, the transition to the
        present, and the state of the whole system — and it belongs to the
        FirstBootOrchestrator. A generator that declares itself shippable is a
        generator grading its own homework.
        """
        years = self._records.years(run_id)
        if not years:
            return False
        expected = {span.year_number for span in anchors.years}
        if {row["year_number"] for row in years} != expected:
            return False
        for span in anchors.years:
            row = next(
                (item for item in years if item["year_number"] == span.year_number), None
            )
            if row is None or not row["final_summary"]:
                return False
            if len(self._records.months(row["year_id"])) < span.months:
                return False
            if not self._runs.reached(run_id, "year_memory_done", span.year_number):
                return False
        if self._experiences is not None and self._experiences.count(
            genesis_run_id=run_id, replay_status="pending"
        ):
            return False
        return True

    @property
    def critic_names(self) -> tuple[str, ...]:
        """Which critics are configured, for the preflight to check."""
        if self._critics is None:
            return ()
        return tuple(getattr(self._critics, "_enabled", ()))

    # --- 34.8: hand the survivors to the runtime -----------------------------
    def promote_survivors(self, run_id: str, anchors: LifeAnchors) -> int:
        """People still in her life become runtime NPCs.

        The ones who faded stay in the archive. A friend she has not seen for
        six years is not a friend she never had, and deleting them would make
        her past thinner than it was.
        """
        if self._society is None:
            return 0
        ledger = ContinuityLedger(self._entities, genesis_run_id=run_id)
        promoted = 0
        for entry in ledger.survivors(anchors.present_datetime):
            # Point 7: idempotent. Already linked means already promoted, and
            # calling this twice must not produce two people with one name.
            # The unique index on `npc_id` backs this up in the schema, because
            # "promote twice" is the shape of bug that survives code review.
            existing = self._entities.npc_for(entry.entity_id)
            if existing:
                continue
            npc = self._society.introduce(name=entry.canonical_name, tier=1)
            self._entities.link_npc(entry.entity_id, npc.npc_id)
            promoted += 1
        return promoted

    # --- internals -----------------------------------------------------------
    async def _generate(self, prompt_id: str, schema: type, **values: Any) -> Any:
        if self._structured is None or self._prompts is None:
            return None
        try:
            template = self._prompts.get(prompt_id)
            content = template.render(**values)
            outcome = await self._structured.generate(
                schema,
                (LLMMessage(role="user", content=content),),
                purpose=prompt_id,
                temperature=0.85,
                max_tokens=1500,
                prompt_id=prompt_id,
                prompt_version=template.prompt_version,
            )
        except Exception:  # noqa: BLE001 - a failed generation is not a fake one
            logger.exception("genesis generation failed prompt=%s", prompt_id)
            return None
        if not getattr(outcome, "ok", True):
            return None
        return outcome.value

    def _all_scaffolded(self, run_id: str, spans: Sequence[LifeYearSpan]) -> bool:
        """The postcondition behind `annual_scaffolds_done` (hardening 2)."""
        written = {
            row["year_number"]
            for row in self._records.years(run_id)
            if row["scaffold_text"]
        }
        return all(span.year_number in written for span in spans)

    def _previous_year_text(self, run_id: str, year_number: int) -> str:
        """Last year, read back from the record (hardening 5).

        A resume that begins mid-Stage-A would otherwise give year eight a
        previous year of nothing, and restart her life in the middle of it.
        """
        if year_number <= 1:
            return "-"
        row = self._records.year(run_id, year_number - 1)
        if row is None:
            return "-"
        return row["final_summary"] or row["scaffold_text"] or "-"

    def _previous_month_text(self, run_id: str, year: Any, month_number: int) -> str:
        """Last month, read back — including December of the year before."""
        if month_number > 1:
            row = self._records.month(year["year_id"], month_number - 1)
            if row is not None and row["narrative"]:
                return row["narrative"]
            return "-"
        earlier = self._records.year(run_id, year["year_number"] - 1)
        if earlier is None:
            return "-"
        months = self._records.months(earlier["year_id"])
        return months[-1]["narrative"] if months else "-"

    def _pending_count(self, run_id: str, year_number: int) -> int:
        if self._experiences is None:
            return 0
        return len(self._experiences.pending(run_id, year_number=year_number))

    def _replayed_count(self, run_id: str, year_number: int) -> int:
        if self._experiences is None:
            return 0
        return sum(
            1
            for row in self._experiences.for_year(run_id, year_number)
            if row["replay_status"] == "replayed"
        )

    def _note_all(
        self,
        ledger: ContinuityLedger,
        people: Sequence[str],
        interests: Sequence[str],
        threads: Sequence[str],
        moment: datetime,
        month_id: str | None = None,
    ) -> None:
        for name in people:
            ledger.note(type="NPC", name=name, moment=moment, month_id=month_id)
        for name in interests:
            ledger.note(type="INTEREST", name=name, moment=moment, month_id=month_id)
        for name in threads:
            ledger.note(
                type="ONGOING_THREAD", name=name, moment=moment, month_id=month_id
            )

    def _version(self, prompt_id: str) -> str:
        if self._prompts is None:
            return ""
        try:
            return self._prompts.get(prompt_id).prompt_version
        except Exception:  # noqa: BLE001
            return ""

    def _model(self) -> str:
        """Which model wrote this row (34.20 point 44).

        Recorded per row rather than once per run because a resume days later
        can legitimately finish a life a different model started, and "who made
        her" then has two answers. Both belong in the first boot report.
        """
        return str(getattr(self._structured, "model", "") or "")


__all__ = [
    "CHECKPOINTS",
    "Extraction",
    "FIRST_BOOT_AUDITS",
    "AuditResult",
    "GenesisProgress",
    "GenesisRunner",
]
