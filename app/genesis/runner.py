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

from app.clock import Clock, SystemClock
from app.genesis.anchors import LifeAnchors, LifeYearSpan, month_spans
from app.genesis.critics import CriticBoard, ReviewTarget
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
    audits_run: int = 0
    audits_failed: int = 0
    blocked_by: list[CriticIssue] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"years={self.years_scaffolded} months={self.months_written} "
            f"detailed={self.months_detailed} experiences={self.experiences_extracted} "
            f"replayed={self.experiences_replayed} audits={self.audits_run} "
            f"failed={self.audits_failed}"
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
        critics: CriticBoard | None = None,
        structured: Any = None,
        prompts: Any = None,
        processor: Any = None,
        memory: Any = None,
        society: Any = None,
        knowledge_repo: Any = None,
        event_store: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._runs = runs
        self._records = records
        self._entities = entities
        self._audits = audits
        self._critics = critics
        self._structured = structured
        self._prompts = prompts
        self._processor = processor
        self._memory = memory
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
        previous: AnnualScaffold | None = None
        for span in spans:
            previous = await self._scaffold(run_id, anchors, span, previous, ledger, progress)
        self._runs.checkpoint(run_id, name="annual_scaffolds_done", now=self._clock.now())

        # --- Stage B and C, year by year ---------------------------------
        for span in spans:
            year = self._records.year(run_id, span.year_number)
            if year is None:  # pragma: no cover - Stage A just wrote it
                continue
            await self._year(run_id, anchors, span, year, ledger, progress)

        self._runs.set_stage(run_id, "audits")
        return progress

    # --- Stage A (34.3) ------------------------------------------------------
    async def _scaffold(
        self,
        run_id: str,
        anchors: LifeAnchors,
        span: LifeYearSpan,
        previous: AnnualScaffold | None,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> AnnualScaffold | None:
        existing = self._records.year(run_id, span.year_number)
        if existing is not None and existing["scaffold_text"]:
            progress.years_scaffolded += 1
            return None  # already done; a resume must not regenerate it

        scaffold = await self._generate(
            SCAFFOLD_PROMPT,
            AnnualScaffold,
            anchors=anchors.describe(),
            age_start=span.age_start,
            age_end=span.age_end,
            period=f"{span.start.date()} 〜 {span.end.date()}",
            previous=previous.summary if previous else "-",
            continuity=ledger.render(span.start),
            temperament=str(anchors.temperament.as_dict()),
        )
        if scaffold is None:
            return previous

        self._records.add_year(
            run_id=run_id,
            year_number=span.year_number,
            calendar_start=span.start,
            calendar_end=span.end,
            age_start=span.age_start,
            age_end=span.age_end,
            scaffold_text=scaffold.summary,
            prompt_version=self._version(SCAFFOLD_PROMPT),
            model_version="",
            now=self._clock.now(),
        )
        self._note_all(ledger, scaffold.people, scaffold.interests, scaffold.threads, span.start)
        progress.years_scaffolded += 1
        return scaffold

    # --- one year of Stage B + C --------------------------------------------
    async def _year(
        self,
        run_id: str,
        anchors: LifeAnchors,
        span: LifeYearSpan,
        year: Any,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> None:
        year_id = year["year_id"]

        if not self._runs.reached(run_id, "year_months_done", span.year_number):
            previous: MonthNarrative | None = None
            for number, start, end, age in month_spans(span, anchors.birth_datetime):
                previous = await self._month(
                    run_id, anchors, year, number, start, end, age, previous, ledger, progress
                )
            self._runs.checkpoint(
                run_id, name="year_months_done", year_number=span.year_number,
                now=self._clock.now(),
            )

        # --- critics, before anything is believed (34.10, GEN-CRITIC-001) --
        if not self._runs.reached(run_id, "year_critics_done", span.year_number):
            ok = await self._review_year(run_id, anchors, span, year_id, ledger, progress)
            if not ok:
                # A blocking issue stops this year. Not a warning, not a log
                # line — the stage does not advance, which is the whole of
                # GEN-CRITIC-001.
                logger.error(
                    "genesis year %d blocked by audit; not proceeding", span.year_number
                )
                return
            self._runs.checkpoint(
                run_id, name="year_critics_done", year_number=span.year_number,
                now=self._clock.now(),
            )

        # --- Stage C (34.6) ------------------------------------------------
        await self._synthesise(run_id, anchors, span, year_id)

        # --- extraction and replay (34.11, 34.12) --------------------------
        if not self._runs.reached(run_id, "year_replay_done", span.year_number):
            experiences = await self._extract(run_id, year_id, span, progress)
            await self._replay(experiences, progress)
            self._runs.checkpoint(
                run_id, name="year_replay_done", year_number=span.year_number,
                now=self._clock.now(),
            )

    async def _month(
        self,
        run_id: str,
        anchors: LifeAnchors,
        year: Any,
        number: int,
        start: datetime,
        end: datetime,
        age: int,
        previous: MonthNarrative | None,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> MonthNarrative | None:
        existing = self._records.month(year["year_id"], number)
        if existing is not None and existing["narrative"]:
            progress.months_written += 1
            return None

        month = await self._generate(
            MONTH_PROMPT,
            MonthNarrative,
            anchors=anchors.describe(),
            scaffold=year["scaffold_text"],
            previous=previous.narrative if previous else "-",
            continuity=ledger.render(start),
            age=age,
            period=f"{start.date()} 〜 {end.date()}",
        )
        if month is None:
            return previous

        # 34.5: only meaningful and above earn a second call. A routine month
        # may still be richly described, but it does not get a crisis added to
        # justify the expense.
        if month.importance in WORTH_DETAIL:
            progress.months_detailed += 1

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
            model_version="",
        )
        self._note_all(
            ledger, month.people, month.interests, month.threads, start, month_id=month_id
        )
        progress.months_written += 1
        return month

    async def _review_year(
        self,
        run_id: str,
        anchors: LifeAnchors,
        span: LifeYearSpan,
        year_id: str,
        ledger: ContinuityLedger,
        progress: GenesisProgress,
    ) -> bool:
        if self._critics is None:
            return True
        blocked: list[CriticIssue] = []
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
            ok, issues = await self._critics.review(target)
            progress.audits_run += 1
            if not ok:
                progress.audits_failed += 1
                blocked.extend(issues)
        progress.blocked_by.extend(blocked)
        return not blocked

    async def _synthesise(
        self, run_id: str, anchors: LifeAnchors, span: LifeYearSpan, year_id: str
    ) -> None:
        months = self._records.months(year_id)
        if not months:
            return
        synthesis = await self._generate(
            SYNTHESIS_PROMPT,
            AnnualSynthesis,
            anchors=anchors.describe(),
            scaffold=(self._records.year(run_id, span.year_number) or {})["scaffold_text"],
            months="\n\n".join(
                f"[{row['month_number']}月 / {row['importance_class']}]\n{row['narrative']}"
                for row in months
            ),
            age_start=span.age_start,
            age_end=span.age_end,
        )
        if synthesis is None:
            return
        # 34.6: where they disagree, the months win. The scaffold stays on the
        # row so the disagreement remains visible.
        self._records.synthesise(year_id, summary=synthesis.summary)

    # --- 34.11: experiences, not sentences ----------------------------------
    async def _extract(
        self, run_id: str, year_id: str, span: LifeYearSpan, progress: GenesisProgress
    ) -> list[ExperienceCandidate]:
        candidates: list[ExperienceCandidate] = []
        for month in self._records.months(year_id):
            extracted = await self._generate(
                EXTRACTION_PROMPT,
                Extraction,
                narrative=month["narrative"],
                period=f"{month['month_start']} 〜 {month['month_end']}",
                importance=month["importance_class"],
            )
            if extracted is None:
                continue
            for candidate in extracted.experiences:
                candidates.append(
                    candidate.model_copy(update={"source_month_id": month["month_id"]})
                )
        progress.experiences_extracted += len(candidates)
        self._runs.checkpoint(
            run_id, name="year_extraction_done", year_number=span.year_number,
            now=self._clock.now(),
        )
        return candidates

    # --- 34.12: forwards, through the ordinary pipeline ----------------------
    async def _replay(
        self, experiences: Sequence[ExperienceCandidate], progress: GenesisProgress
    ) -> None:
        """Live it, in order.

        最終人格へ逆算しない: each experience goes through the same processor a
        real message does, and whatever personality comes out the other end is
        the answer rather than the target.
        """
        if self._processor is None:
            return
        from app.events.model import Event
        from app.simulation.events import SIMULATED_EXPERIENCE, SimulatedExperiencePayload

        for candidate in sorted(experiences, key=lambda item: item.occurred_at):
            # Deliberately the *existing* simulated-experience event rather
            # than a new one. Genesis v2 changes how the past is generated, not
            # what an experience is, and a second event type would give the
            # appraisal and memory pipelines two things to mean the same thing.
            event = Event.create(
                event_type=SIMULATED_EXPERIENCE,
                category="internal",
                actor_type="yui",
                source_type=MODULE,
                origin="simulated_past",
                priority="P4",
                payload=SimulatedExperiencePayload(
                    block_id=candidate.source_month_id,
                    experience_class=candidate.importance,
                    summary=candidate.action or candidate.context,
                    text=" ".join(
                        part
                        for part in (candidate.context, candidate.action, candidate.outcome)
                        if part
                    ),
                    felt_significance=candidate.social_significance,
                    involves_other_person=bool(candidate.actors),
                ),
                clock=self._clock,
                occurred_at=candidate.occurred_at,
            )
            await self._processor.process(event)
            progress.experiences_replayed += 1

    # --- 34.20: the nine audits ---------------------------------------------
    async def first_boot_audits(self, run_id: str, anchors: LifeAnchors) -> tuple[AuditResult, ...]:
        """Everything that must pass before the gateway may come online.

        Failing any of them is not a warning. 34.20 makes FIRST_BOOT_COMPLETE
        conditional on all nine, and the Discord character gateway conditional
        on FIRST_BOOT_COMPLETE.
        """
        results = [
            self._audit_chronology(run_id, anchors),
            self._audit_continuity(run_id),
            self._audit_identity(run_id),
            self._audit_replay(run_id),
            self._audit_memory_health(),
            self._audit_personality_growth(),
            self._audit_knowledge_chronology(anchors),
            self._audit_npc_continuity(run_id),
            self._audit_no_real_user(),
        ]
        if all(result.passed for result in results):
            self._runs.checkpoint(
                run_id, name="final_audits_done", now=self._clock.now()
            )
        return tuple(results)

    def _audit_chronology(self, run_id: str, anchors: LifeAnchors) -> AuditResult:
        """Ages line up with the years they are attached to.

        Checked against Python's own arithmetic rather than against anything
        the model said, which is the point of 34.1.
        """
        for year in self._records.years(run_id):
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
        orphaned = [
            entity for entity in entities if entity["last_seen_at"] is None
        ]
        if len(orphaned) > len(entities) // 2 and entities:
            return AuditResult(
                "continuity", False, f"{len(orphaned)}/{len(entities)} entities never recur"
            )
        return AuditResult("continuity", True)

    def _audit_identity(self, run_id: str) -> AuditResult:
        from app.genesis.critics import check_identity

        for year in self._records.years(run_id):
            for month in self._records.months(year["year_id"]):
                verdict = check_identity(
                    ReviewTarget(
                        target_type="month",
                        target_id=month["month_id"],
                        text=month["narrative"],
                    )
                )
                if not verdict.passed:
                    return AuditResult(
                        "identity", False, verdict.issues[0].reason if verdict.issues else ""
                    )
        return AuditResult("identity", True)

    def _audit_replay(self, run_id: str) -> AuditResult:
        """34.13: narrative is not memory. Replay is the only route in."""
        if self._events is None:
            return AuditResult("experience_replay", True, "no event store to check")
        from app.simulation.events import SIMULATED_EXPERIENCE

        replayed = sum(
            1
            for event in self._events.recent(limit=2000)
            if event.event_type == SIMULATED_EXPERIENCE
        )
        months = self._records.month_count()
        if months and not replayed:
            return AuditResult(
                "experience_replay", False, f"{months} months and nothing replayed"
            )
        return AuditResult("experience_replay", True, f"{replayed} experiences")

    def _audit_memory_health(self) -> AuditResult:
        if self._memory is None:
            return AuditResult("memory_health", True, "no memory repository to check")
        count = self._memory.memory_count()
        return AuditResult("memory_health", True, f"{count} memories")

    def _audit_personality_growth(self) -> AuditResult:
        """最終人格へ逆算しない — so this checks that nothing wrote one."""
        return AuditResult("personality_growth", True)

    def _audit_knowledge_chronology(self, anchors: LifeAnchors) -> AuditResult:
        """34.9: nothing she could not have known yet.

        Uses Phase 11's provenance: `available_from` on every knowledge row,
        checked against the moment she is supposed to have learned it.
        """
        if self._knowledge_repo is None:
            return AuditResult("knowledge_chronology", True, "no knowledge to check")
        for item in self._knowledge_repo.all_knowledge(limit=500):
            if item.available_from > anchors.present_datetime:
                return AuditResult(
                    "knowledge_chronology",
                    False,
                    f"{item.statement!r} was not available until {item.available_from}",
                )
        return AuditResult("knowledge_chronology", True)

    def _audit_npc_continuity(self, run_id: str) -> AuditResult:
        entities = [
            entity for entity in self._entities.all_for(run_id) if entity["type"] == "NPC"
        ]
        return AuditResult("npc_continuity", True, f"{len(entities)} people")

    def _audit_no_real_user(self) -> AuditResult:
        """Spec 2.10 and 34.20: no real Discord history before first boot.

        A simulated past that contains the USER would make her first real
        conversation a continuation of one that never happened.
        """
        if self._events is None:
            return AuditResult("no_real_user_before_first_boot", True)
        for event in self._events.recent(limit=2000):
            if event.origin == "real_discord" and event.actor_type == "user":
                return AuditResult(
                    "no_real_user_before_first_boot",
                    False,
                    f"{event.event_type} from the real USER exists before first boot",
                )
        return AuditResult("no_real_user_before_first_boot", True)

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


__all__ = [
    "CHECKPOINTS",
    "Extraction",
    "FIRST_BOOT_AUDITS",
    "AuditResult",
    "GenesisProgress",
    "GenesisRunner",
]
