"""INVARIANT: nineteen years are lived forwards, once (rebuild spec 34 — Phase 12).

Three rules do most of the work here.

**年齢は Python が exact date から計算.** Every age in the run comes from one
function. A model asked how old someone born in March 2007 was in September
2019 will usually say twelve and sometimes thirteen, and nothing downstream can
tell which happened.

**最終人格へ逆算しない.** The personality is whatever the replay produces.
Nothing writes a trait, and replay goes forwards through the same processor a
live message does.

**Audit が失敗したのに silent pass して次へ進まない.** A blocking critic issue
stops the year. Not a log line — the stage does not advance, and the failure is
a row so that "it passed silently" is detectable afterwards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.admin.router import ADMIN_PREFIX
from app.bootstrap import Application
from app.genesis.anchors import (
    LifeAnchors,
    TemperamentSeed,
    age_at,
    month_spans,
    year_spans,
)
from app.genesis.critics import CriticBoard, ReviewTarget, check_chronology, check_identity
from app.genesis.ledger import ContinuityLedger
from app.genesis.models import (
    WORTH_DETAIL,
    AnnualScaffold,
    AnnualSynthesis,
    CriticIssue,
    CriticVerdict,
    ExperienceCandidate,
    MonthNarrative,
)
from app.genesis.runner import CHECKPOINTS, FIRST_BOOT_AUDITS, Extraction
from app.simulation.events import SIMULATED_EXPERIENCE
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"

BIRTH = datetime(2007, 3, 14, tzinfo=timezone.utc)
PRESENT = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def application(temp_config, clock):
    owned = temp_config.model_copy(
        update={
            "secrets": temp_config.secrets.model_copy(
                update={"discord_owner_user_id": OWNER, "discord_channel_id": CHANNEL}
            )
        }
    )
    built = Application.build(owned, clock=clock, configure_logs=False)
    use_offline_model(built)
    try:
        yield built
    finally:
        built.db.close()


@pytest.fixture
def run_id(application) -> str:
    """A real genesis run. `life_entities` has a foreign key to it, which is
    the schema refusing to hold orphaned people."""
    return application.genesis_runs.start(
        birth=BIRTH, present=PRESENT, years=18, now=application.clock.now()
    )


@pytest.fixture
def anchors() -> LifeAnchors:
    return LifeAnchors(
        birth_datetime=BIRTH,
        present_datetime=PRESENT,
        gender_identity="女性",
        embodiment="デジタルな存在。物理的な身体を持たない。",
        culture="日本",
        home="自室",
        family="そういうことになっている家族",
        education="学校のようなもの",
        immutable_rules="身体を持たない",
        temperament=TemperamentSeed(sociability=0.2, openness=0.4),
    )


class Storyteller:
    """A model that tells a small, consistent life."""

    def __init__(self, *, importance: str = "routine", experiences: int = 1) -> None:
        self.importance = importance
        self.experiences = experiences
        self.calls: list[str] = []

    async def generate(self, schema, messages, *, purpose: str, **kwargs):
        self.calls.append(purpose)
        value = self._value(schema, purpose)
        return type("Outcome", (), {"ok": True, "value": value})()

    def _value(self, schema, purpose: str):
        if schema is AnnualScaffold:
            return AnnualScaffold(
                summary="静かな一年だった。",
                routine="毎日似たようなことをしていた。",
                people=("ミカ",),
                interests=("本",),
                threads=("読みかけの本",),
            )
        if schema is MonthNarrative:
            return MonthNarrative(
                narrative="本を読んでいた。",
                importance=self.importance,  # type: ignore[arg-type]
                people=("ミカ",),
                interests=("本",),
            )
        if schema is AnnualSynthesis:
            return AnnualSynthesis(summary="読んでばかりの一年。", revisions=("特になし",))
        if schema is Extraction:
            return Extraction(
                experiences=tuple(
                    ExperienceCandidate(
                        occurred_at=datetime(2008, 6, 1, tzinfo=timezone.utc),
                        actors=("ミカ",),
                        context="家で",
                        action="本を読んだ",
                        outcome="面白かった",
                        social_significance=0.4,
                        compressed=True,
                    )
                    for _ in range(self.experiences)
                )
            )
        if schema is CriticVerdict:
            return CriticVerdict(passed=True)
        return schema()


def _runner(application, storyteller=None, *, critics=None):
    from app.genesis.runner import GenesisRunner

    return GenesisRunner(
        runs=application.genesis_runs,
        records=application.life_records,
        entities=application.life_entities,
        audits=application.generation_audits,
        critics=critics,
        structured=storyteller,
        prompts=application.prompts if storyteller is not None else None,
        processor=application.processor,
        memory=application.memories,
        society=application.society,
        knowledge_repo=application.knowledge_repo,
        event_store=application.event_store,
        clock=application.clock,
    )


# --- 34.1: Python owns every age --------------------------------------------


def test_an_age_is_arithmetic_not_an_opinion() -> None:
    assert age_at(BIRTH, datetime(2019, 3, 13, tzinfo=timezone.utc)) == 11
    assert age_at(BIRTH, datetime(2019, 3, 14, tzinfo=timezone.utc)) == 12
    assert age_at(BIRTH, datetime(2019, 9, 1, tzinfo=timezone.utc)) == 12


def test_a_birthday_that_has_not_arrived_does_not_count() -> None:
    """The mistake a model makes at exactly the rate that hides it."""
    assert age_at(BIRTH, datetime(2020, 1, 1, tzinfo=timezone.utc)) == 12


def test_a_leap_day_birthday_does_not_crash() -> None:
    leap = datetime(2008, 2, 29, tzinfo=timezone.utc)
    assert age_at(leap, datetime(2019, 3, 1, tzinfo=timezone.utc)) == 11
    spans = year_spans(leap, datetime(2013, 6, 1, tzinfo=timezone.utc))
    assert len(spans) == 5


def test_years_run_birthday_to_birthday(anchors) -> None:
    """Not calendar years: "her fourth year" is the unit ages are counted in,
    and mixing the two describes a five-year-old's school year for a
    four-year-old."""
    spans = anchors.years

    assert len(spans) == anchors.developmental_age == 18
    assert spans[0].start == BIRTH
    assert spans[0].age_start == 0
    assert all(span.start.month == BIRTH.month for span in spans)


def test_a_life_year_is_all_one_age(anchors) -> None:
    """The payoff of anchoring on birthdays rather than calendar years.

    Every month inside one life year is the same age, because the birthday is
    the boundary rather than something that falls in the middle. A calendar
    year would straddle one, and a scaffold written for "age 12" would then
    cover months where she was 11.
    """
    span = anchors.years[5]
    ages = {age for _, _, _, age in month_spans(span, BIRTH)}

    assert ages == {span.age_start}
    assert span.age_end == span.age_start + 1


def test_the_developmental_age_is_computed_not_stated(anchors) -> None:
    assert anchors.developmental_age == age_at(BIRTH, PRESENT)
    assert "developmental_age" not in LifeAnchors.model_fields


# --- 34.2: the seed is a bias, not a personality -----------------------------


def test_the_temperament_seed_is_thin() -> None:
    """最終 personality / values / hobbies を直接決めない. Writing them here
    would make Genesis an expensive way of restating the setup form."""
    fields = set(TemperamentSeed.model_fields)

    assert fields == {"sociability", "reactivity", "openness", "persistence", "positivity"}
    for forbidden in ("traits", "values", "hobbies", "personality", "interests"):
        assert forbidden not in fields


# --- 34.10 / GEN-CRITIC-001 --------------------------------------------------


def test_the_chronology_critic_needs_no_model() -> None:
    """It exists to catch arithmetic a model gets wrong. Asking a model to
    check it would put the same failure on both sides."""
    verdict = check_chronology(
        ReviewTarget(
            target_type="month",
            target_id="m1",
            text="8歳のころの話だ。",
            age_start=12,
            age_end=13,
        )
    )

    assert not verdict.passed
    assert verdict.issues[0].code == "AGE_MISMATCH"
    assert verdict.issues[0].blocking


def test_a_correct_age_passes() -> None:
    verdict = check_chronology(
        ReviewTarget(
            target_type="month", target_id="m1", text="12歳の夏。", age_start=12, age_end=13
        )
    )

    assert verdict.passed


def test_the_identity_critic_catches_a_body() -> None:
    """She is a digital being. A scaffold with her eating breakfast is not a
    style problem to nudge in a prompt."""
    verdict = check_identity(
        ReviewTarget(target_type="month", target_id="m1", text="朝ごはんを食べてから出かけた。")
    )

    assert not verdict.passed
    assert verdict.issues[0].code == "EMBODIMENT_CONTRADICTION"


async def test_a_blocking_issue_stops_the_year(application, anchors) -> None:
    """GEN-CRITIC-001, structurally: not a warning, a stop."""

    class Harsh:
        async def generate(self, schema, messages, *, purpose: str, **kwargs):
            if schema is CriticVerdict:
                return type(
                    "Outcome",
                    (),
                    {
                        "ok": True,
                        "value": CriticVerdict(
                            passed=False,
                            issues=(
                                CriticIssue(
                                    severity="fatal",
                                    target_id="m1",
                                    code="CONTINUITY_BREAK",
                                    reason="a person appeared from nowhere",
                                ),
                            ),
                        ),
                    },
                )()
            return await Storyteller().generate(schema, messages, purpose=purpose, **kwargs)

    board = CriticBoard(
        audits=application.generation_audits,
        structured=Harsh(),
        prompts=application.prompts,
        clock=application.clock,
        enabled=("continuity",),
    )
    runner = _runner(application, Harsh(), critics=board)

    progress = await runner.run(anchors, max_years=1)

    assert progress.audits_failed > 0
    assert progress.blocked_by
    # The year did not advance: nothing was synthesised and nothing replayed.
    year = application.life_records.years(progress.run_id)[0]
    assert year["final_summary"] == ""
    assert progress.experiences_replayed == 0


async def test_a_failed_audit_is_a_row(application, anchors) -> None:
    """Only detectable afterwards because the failure was written down."""

    class Harsh:
        async def generate(self, schema, messages, *, purpose: str, **kwargs):
            if schema is CriticVerdict:
                return type(
                    "Outcome",
                    (),
                    {
                        "ok": True,
                        "value": CriticVerdict(
                            passed=False,
                            issues=(
                                CriticIssue(severity="high", target_id="m1", code="X"),
                            ),
                        ),
                    },
                )()
            return await Storyteller().generate(schema, messages, purpose=purpose, **kwargs)

    board = CriticBoard(
        audits=application.generation_audits,
        structured=Harsh(),
        prompts=application.prompts,
        clock=application.clock,
        enabled=("continuity",),
    )
    await _runner(application, Harsh(), critics=board).run(anchors, max_years=1)

    assert application.generation_audits.count(passed=False) > 0
    assert application.generation_audits.failures()


async def test_a_critic_that_could_not_run_is_not_a_pass(application) -> None:
    """A silent pass is exactly what happens if an unavailable critic returns
    True, so it returns a recorded medium issue instead."""
    board = CriticBoard(
        audits=application.generation_audits,
        structured=None,
        prompts=None,
        clock=application.clock,
        enabled=("psychology",),
    )

    ok, blocking = await board.review(
        ReviewTarget(target_type="month", target_id="m1", text="なにか")
    )

    assert ok is True  # medium does not block
    assert blocking == ()
    row = application.generation_audits.recent()[0]
    assert row["passed"] == 0  # but it is on the record as not having approved


def test_there_are_eight_critics() -> None:
    from app.genesis.critics import CRITICS

    assert len(CRITICS) == 8
    assert len(set(CRITICS)) == 8


# --- 34.3-34.6: the stages ---------------------------------------------------


async def test_a_year_is_scaffolded_then_expanded_then_synthesised(
    application, anchors
) -> None:
    runner = _runner(application, Storyteller())

    progress = await runner.run(anchors, max_years=1)

    year = application.life_records.years(progress.run_id)[0]
    assert year["scaffold_text"], "Stage A wrote nothing"
    assert len(application.life_records.months(year["year_id"])) == 12
    assert year["final_summary"], "Stage C never ran"
    assert year["status"] == "synthesised"


async def test_the_scaffold_survives_the_synthesis(application, anchors) -> None:
    """34.6: the months win when they disagree — and the disagreement stays
    visible, because overwriting the sketch would destroy the evidence."""
    runner = _runner(application, Storyteller())

    progress = await runner.run(anchors, max_years=1)

    year = application.life_records.years(progress.run_id)[0]
    assert year["scaffold_text"] != year["final_summary"]
    assert year["scaffold_text"]


async def test_a_routine_month_gets_no_detail_call(application, anchors) -> None:
    """34.5. A generator that cannot say "nothing much happened" invents a
    crisis every four weeks."""
    runner = _runner(application, Storyteller(importance="routine"))

    progress = await runner.run(anchors, max_years=1)

    assert progress.months_written == 12
    assert progress.months_detailed == 0
    assert application.life_records.importance_spread() == {"routine": 12}


async def test_a_meaningful_month_does(application, anchors) -> None:
    runner = _runner(application, Storyteller(importance="meaningful"))

    progress = await runner.run(anchors, max_years=1)

    assert progress.months_detailed == 12
    assert "meaningful" in WORTH_DETAIL


# --- 34.7 / 34.8: the continuity ledger --------------------------------------


async def test_a_person_mentioned_every_month_is_one_person(
    application, anchors
) -> None:
    runner = _runner(application, Storyteller())

    progress = await runner.run(anchors, max_years=1)

    people = [
        entity
        for entity in application.life_entities.all_for(progress.run_id)
        if entity["type"] == "NPC"
    ]
    assert [person["canonical_name"] for person in people] == ["ミカ"]


async def test_the_relevant_subset_is_a_subset(application, run_id, anchors, clock) -> None:
    """毎月すべてを prompt へ入れず relevant subset を retrieval."""
    from app.genesis.ledger import MAX_IN_CONTEXT

    ledger = ContinuityLedger(application.life_entities, genesis_run_id=run_id)
    for index in range(MAX_IN_CONTEXT + 10):
        ledger.note(type="LOCATION", name=f"場所{index}", moment=clock.now())

    relevant = ledger.relevant_for(clock.now())

    assert len(relevant) <= MAX_IN_CONTEXT


def test_an_open_thread_stays_relevant_however_old(application, run_id, clock) -> None:
    ledger = ContinuityLedger(application.life_entities, genesis_run_id=run_id)
    long_ago = clock.now() - timedelta(days=365 * 5)
    ledger.note(type="ONGOING_THREAD", name="ずっと気にしていること", moment=long_ago)
    ledger.note(type="LOCATION", name="昔の場所", moment=long_ago)

    names = {entry.canonical_name for entry in ledger.relevant_for(clock.now())}

    assert "ずっと気にしていること" in names
    assert "昔の場所" not in names


def test_someone_not_yet_born_into_her_life_is_not_offered(
    application, run_id, clock
) -> None:
    ledger = ContinuityLedger(application.life_entities, genesis_run_id=run_id)
    ledger.note(type="NPC", name="未来の人", moment=clock.now() + timedelta(days=400))

    assert ledger.relevant_for(clock.now()) == ()


def test_someone_who_faded_stays_in_the_archive(application, run_id, clock) -> None:
    """34.8. A friend she has not seen for six years is not a friend she never
    had, and deleting them would make her past thinner than it was."""
    ledger = ContinuityLedger(application.life_entities, genesis_run_id=run_id)
    entity_id = ledger.note(
        type="NPC", name="疎遠になった人", moment=clock.now() - timedelta(days=2000)
    )
    ledger.retire(entity_id, moment=clock.now() - timedelta(days=1000))

    stored = application.life_entities.all_for(run_id)

    assert [item["canonical_name"] for item in stored] == ["疎遠になった人"]
    assert stored[0]["status"] == "retired"
    assert ledger.survivors(clock.now()) == ()


async def test_survivors_become_runtime_npcs(application, anchors) -> None:
    runner = _runner(application, Storyteller())
    progress = await runner.run(anchors, max_years=1)

    promoted = runner.promote_survivors(progress.run_id, anchors)

    if promoted:
        assert application.society.by_name("ミカ") is not None


# --- 34.11 / 34.12 / 34.13 ---------------------------------------------------


async def test_experiences_are_replayed_forwards(application, anchors) -> None:
    """最終人格へ逆算しない: through the same processor a live message uses."""
    runner = _runner(application, Storyteller(experiences=2))

    progress = await runner.run(anchors, max_years=1)

    assert progress.experiences_extracted > 0
    assert progress.experiences_replayed == progress.experiences_extracted
    types = [event.event_type for event in application.event_store.recent(limit=200)]
    assert SIMULATED_EXPERIENCE in types


async def test_narrative_is_not_memory(application, anchors) -> None:
    """34.13: annual and monthly text is the objective record. Only what goes
    through extraction and the Memory Engine is something she remembers."""
    runner = _runner(application, Storyteller())

    progress = await runner.run(anchors, max_years=1)

    year = application.life_records.years(progress.run_id)[0]
    summaries = [
        memory.summary for memory in application.memories.recent_memories(limit=200)
    ]
    assert year["scaffold_text"] not in summaries
    assert year["final_summary"] not in summaries


async def test_a_month_does_not_become_thirty_events(application, anchors) -> None:
    """全 narrative sentence を Event にしない."""
    runner = _runner(application, Storyteller(experiences=1))

    progress = await runner.run(anchors, max_years=1)

    # Twelve months, one experience each — not one per sentence.
    assert progress.experiences_extracted == 12
    assert progress.months_written == 12


def test_a_repeated_routine_is_one_compressed_experience() -> None:
    candidate = ExperienceCandidate(
        occurred_at=PRESENT, action="毎朝おなじことをした", compressed=True
    )

    assert candidate.compressed


# --- 34.18 / 34.19: checkpoints and idempotency ------------------------------


async def test_a_resume_does_not_relive_a_year(application, anchors) -> None:
    """The failure this prevents: a crash in year nine replays a fortnight of
    2019 twice."""
    storyteller = Storyteller()
    runner = _runner(application, storyteller)
    progress = await runner.run(anchors, max_years=1)
    replayed_once = progress.experiences_replayed
    events_once = application.event_store.count()

    again = await runner.run(anchors, resume=progress.run_id, max_years=1)

    assert again.experiences_replayed == 0, "the year was lived twice"
    assert application.event_store.count() == events_once
    assert replayed_once > 0


async def test_a_checkpoint_is_written_once(application, anchors) -> None:
    runner = _runner(application, Storyteller())
    progress = await runner.run(anchors, max_years=1)

    first = application.genesis_runs.checkpoint(
        progress.run_id, name="year_replay_done", year_number=1, now=application.clock.now()
    )

    assert first is False  # already reached
    assert application.genesis_runs.reached(progress.run_id, "year_replay_done", 1)


async def test_the_scaffold_is_not_regenerated_on_resume(
    application, anchors
) -> None:
    storyteller = Storyteller()
    runner = _runner(application, storyteller)
    progress = await runner.run(anchors, max_years=1)
    scaffold_calls = storyteller.calls.count("genesis_annual_scaffold")

    await runner.run(anchors, resume=progress.run_id, max_years=1)

    assert storyteller.calls.count("genesis_annual_scaffold") == scaffold_calls


def test_the_checkpoint_names_are_the_specs() -> None:
    assert set(CHECKPOINTS) == {
        "annual_scaffolds_done",
        "year_months_done",
        "year_critics_done",
        "year_extraction_done",
        "year_replay_done",
        "year_memory_done",
        "final_audits_done",
    }


# --- 34.20: the nine audits --------------------------------------------------


def test_there_are_nine_first_boot_audits() -> None:
    assert len(FIRST_BOOT_AUDITS) == 9


async def test_the_audits_run_and_pass_on_a_clean_run(application, anchors) -> None:
    runner = _runner(application, Storyteller())
    progress = await runner.run(anchors, max_years=1)

    results = await runner.first_boot_audits(progress.run_id, anchors)

    assert {result.name for result in results} == set(FIRST_BOOT_AUDITS)
    failed = [result for result in results if not result.passed]
    assert not failed, failed
    assert application.genesis_runs.reached(progress.run_id, "final_audits_done")


async def test_a_real_user_message_fails_the_audit(
    application, anchors, make_event
) -> None:
    """A simulated past containing the USER would make her first real
    conversation a continuation of one that never happened."""
    runner = _runner(application, Storyteller())
    progress = await runner.run(anchors, max_years=1)
    await application.processor.process(make_event(actor_type="user"))

    results = await runner.first_boot_audits(progress.run_id, anchors)

    audit = next(r for r in results if r.name == "no_real_user_before_first_boot")
    assert not audit.passed
    assert not application.genesis_runs.reached(progress.run_id, "final_audits_done")


async def test_a_body_in_the_record_fails_the_identity_audit(
    application, anchors
) -> None:
    runner = _runner(application, Storyteller())
    progress = await runner.run(anchors, max_years=1)
    year = application.life_records.years(progress.run_id)[0]
    month = application.life_records.months(year["year_id"])[0]
    application.db.execute(
        "UPDATE life_months SET narrative = ? WHERE month_id = ?",
        ("その日は朝ごはんを食べてから出かけた。", month["month_id"]),
    )

    results = await runner.first_boot_audits(progress.run_id, anchors)

    assert not next(r for r in results if r.name == "identity").passed


async def test_future_knowledge_fails_the_chronology_audit(
    application, anchors
) -> None:
    """Phase 11's provenance, used for what it was built for."""
    from app.knowledge.models import KnowledgeItem
    from app import ids

    runner = _runner(application, Storyteller())
    progress = await runner.run(anchors, max_years=1)
    application.knowledge_repo.add_knowledge(
        KnowledgeItem(
            knowledge_id=ids.new_id("kno"),
            statement="ずっと先のできごと",
            coverage_class="interest_driven",
            available_from=PRESENT + timedelta(days=4000),
            created_at=application.clock.now(),
        ),
        source_type="web_search",
    )

    results = await runner.first_boot_audits(progress.run_id, anchors)

    audit = next(r for r in results if r.name == "knowledge_chronology")
    assert not audit.passed
    assert "not available until" in audit.detail


# --- the debug path ----------------------------------------------------------


async def test_the_genesis_views_are_wired(application, anchors) -> None:
    await _runner(application, Storyteller()).run(anchors, max_years=1)

    for command in ("genesis years", "genesis audits"):
        outcome = await application.admin_router.route(
            text=f"{ADMIN_PREFIX} {command}", author_id=OWNER, channel_id=CHANNEL
        )
        assert not outcome.result.failed, (command, outcome.result.error)
        assert "not wired yet" not in outcome.result.summary, command


async def test_the_years_view_shows_the_ages(application, anchors) -> None:
    await _runner(application, Storyteller()).run(anchors, max_years=1)

    outcome = await application.admin_router.route(
        text=f"{ADMIN_PREFIX} genesis years", author_id=OWNER, channel_id=CHANNEL
    )

    rows = [row for section in outcome.result.sections for row in section.rows]
    assert rows
    assert rows[0]["age_start"] == 0
    assert rows[0]["months"] == 12
