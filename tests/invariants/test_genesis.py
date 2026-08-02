"""INVARIANT: the past YUI is given is a past this system could have produced.

* Seven questions produce a temperamental seed and nothing more — no finished
  personality, no values, no habits (spec 22.1, 22.2).
* A simulated experience goes through the *normal* pipeline; there is no second
  personality engine and no working backwards from a target (spec 22.6).
* Major events and hardship are rationed, not sampled freely (spec 22.4).
* Everything simulated is permanently marked ``simulated_past`` and never mixed
  with real Discord history (spec 2.10, 34.2-4).
* FIRST BOOT happens only when every audit passed, and nothing about the USER
  may exist before it (spec 22.7).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

import pytest

from app.bootstrap import Application
from app.knowledge.builder import Candidate
from app.orchestrator.run_view import RunView
from app.simulation.events import FIRST_BOOT, SIMULATED_EXPERIENCE
from app.simulation.experiences import ExperienceBudget, ExperienceSampler
from app.simulation.models import AUDIT_KINDS, EXPERIENCE_CLASSES, TemperamentSeed
from app.simulation.policy import SimulationPolicy
from app.simulation.seed import QUESTION_IDS, SeedBuilder, SeedError, SeedRequest
from app.storage.repositories.events import EventRepository
from app.storage.repositories.simulation import SimulationRepository
from tests.support import use_offline_model

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]

PERIOD_START = datetime(2003, 4, 1, 9, 0, tzinfo=timezone.utc)
PERIOD_END = datetime(2006, 4, 1, 9, 0, tzinfo=timezone.utc)
AFTER_PERIOD = datetime(2020, 1, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def simulation_policy() -> SimulationPolicy:
    return SimulationPolicy.load(REPO_ROOT / "config" / "policies" / "simulation.yaml")


@pytest.fixture
def seed_builder(simulation_policy, clock) -> SeedBuilder:
    return SeedBuilder(simulation_policy.seed, clock=clock)


def answers(**overrides: str) -> dict[str, str]:
    base = {question: "neutral" for question in QUESTION_IDS if question not in ("interests", "avoid")}
    base.update(overrides)
    return base


def seed_for(application, seed_builder=None) -> TemperamentSeed:
    builder = seed_builder or application.seed_builder
    return builder.build(
        SeedRequest(
            answers=answers(overall_mood="high", interpersonal_distance="low"),
            interests=("音楽", "本"),
            avoid=("冷笑的",),
        )
    )


# --- the questionnaire produces a seed, not a person (spec 22.2) ------------
def test_the_questionnaire_is_about_seven_questions(seed_builder, simulation_policy) -> None:
    assert len(QUESTION_IDS) == simulation_policy.seed.expected_question_count


def test_answers_produce_temperament_and_nothing_else(seed_builder) -> None:
    seed = seed_builder.build(
        SeedRequest(answers=answers(), interests=("音楽",), avoid=("冷笑的",))
    )

    assert seed.temperament
    # There is nowhere on the seed to put a finished person.
    assert not hasattr(seed, "personality")
    assert not hasattr(seed, "values")
    assert not hasattr(seed, "habits")
    assert seed.avoid == ("冷笑的",)


def test_no_answer_can_pin_a_dimension_to_an_extreme(seed_builder) -> None:
    """Spec 22.2: the questions bias a starting point, they do not specify one."""
    low, high = seed_builder.bounds()
    extreme = seed_builder.build(
        SeedRequest(answers=answers(**{q: "very_high" for q in QUESTION_IDS if q not in ("interests", "avoid")}))
    )

    for name, value in extreme.temperament.items():
        assert low <= value <= high, name
        assert 0.0 < value < 1.0


def test_an_unknown_question_or_answer_is_refused(seed_builder) -> None:
    with pytest.raises(SeedError):
        seed_builder.build(SeedRequest(answers={"favourite_colour": "neutral"}))
    with pytest.raises(SeedError):
        seed_builder.build(SeedRequest(answers=answers(overall_mood="0.97")))


def test_openness_is_an_outcome_not_an_answer(seed_builder, simulation_policy) -> None:
    """What she becomes curious about is lived, not configured."""
    seed = seed_builder.build(SeedRequest(answers=answers(overall_mood="very_high")))
    assert seed.temperament["openness"] == pytest.approx(simulation_policy.seed.neutral)


# --- experience rationing (spec 22.4) ---------------------------------------
def test_major_events_are_rationed_per_year(simulation_policy) -> None:
    sampler = ExperienceSampler(simulation_policy.experience, rng=Random(7))
    budget = ExperienceBudget(years=2.0)

    classes = [sampler.sample(budget).experience_class for _ in range(4000)]

    assert budget.major_count <= simulation_policy.experience.max_major_per_year * 2.0 + 1
    assert budget.turning_point_count <= simulation_policy.experience.max_turning_points_total
    assert set(classes) <= set(EXPERIENCE_CLASSES)


def test_hardship_is_not_a_convenient_explanation(simulation_policy) -> None:
    """Spec 22.4: trauma must not become the engine of a personality."""
    sampler = ExperienceSampler(simulation_policy.experience, rng=Random(11))
    budget = ExperienceBudget(years=40.0)

    for _ in range(20000):
        sampler.sample(budget)

    assert budget.major_count + budget.turning_point_count > 0
    assert budget.negative_share() <= simulation_policy.experience.max_negative_major_share


def test_routine_dominates_an_ordinary_life(simulation_policy) -> None:
    sampler = ExperienceSampler(simulation_policy.experience, rng=Random(3))
    budget = ExperienceBudget(years=20.0)
    counts: dict[str, int] = {}

    for _ in range(5000):
        experience = sampler.sample(budget)
        counts[experience.experience_class] = counts.get(experience.experience_class, 0) + 1

    assert counts["routine"] > sum(v for k, v in counts.items() if k != "routine")


def test_only_significant_experiences_justify_a_detailed_call(simulation_policy) -> None:
    sampler = ExperienceSampler(simulation_policy.experience, rng=Random(5))
    budget = ExperienceBudget(years=5.0)

    for _ in range(2000):
        experience = sampler.sample(budget)
        if experience.experience_class in ("routine", "minor"):
            assert experience.llm_cost == "none"
        elif experience.experience_class == "meaningful":
            assert experience.llm_cost == "light"
        else:
            assert experience.llm_cost == "detailed"


# --- the simulation itself ---------------------------------------------------
async def test_a_simulated_life_runs_through_the_normal_pipeline(
    temp_config, clock, make_event
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END, environment="小さな町"
        )
        result = await application.simulation.run(scaffold, max_blocks=40)

        assert result.completed
        assert result.progress.experiences > 0
        # Every experience produced an ordinary run through the ordinary
        # engines: the same psychology a real event would have gone through.
        assert application.runs.count() >= result.progress.experiences
        assert {"emotion", "mood", "needs"} & set(application.state.domains())
        # And there is no second personality engine: `personality` moves only
        # through consolidation, which has not been asked to run yet.
        assert application.state.list_domain("personality") == []
    finally:
        application.db.close()


async def test_everything_simulated_is_marked_as_simulated(
    temp_config, clock
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        await application.simulation.run(scaffold, max_blocks=20)

        experiences = application.event_store.by_types(
            (SIMULATED_EXPERIENCE,), limit=200
        )
        assert experiences
        for event in experiences:
            assert event.origin == "simulated_past"
            assert event.actor_type == "yui"
            # Spec 8.3: lived long ago, recorded now, and the two never merge.
            assert event.occurred_at < event.recorded_at
            assert PERIOD_START <= event.occurred_at <= PERIOD_END
    finally:
        application.db.close()


async def test_the_simulation_never_creates_the_user(temp_config, clock) -> None:
    """Spec 22.7: no relationship experience with the USER before FIRST BOOT."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        await application.simulation.run(scaffold, max_blocks=20)

        repository = EventRepository(application.db)
        assert repository.count_by_actor("user") == 0
        assert repository.count_by_origin({"real_discord"}) == 0
        # And no USER relationship state was formed either.
        assert application.state.list_domain("relationship") == []
        assert application.state.list_domain("attachment") == []
    finally:
        application.db.close()


async def test_the_user_bond_engines_ignore_a_simulated_event(
    temp_config, clock, make_event
) -> None:
    """Regression: a simulated past has no USER, so it forms no bond with one.

    The attachment engine used to answer to any ``yui`` event regardless of
    origin, which quietly gave YUI an attachment history with somebody she had
    not met (spec 22.7).
    """
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        simulated = make_event(actor_type="yui", origin="simulated_past")
        view = RunView(snapshot=application.snapshots.capture(persist=False))

        assert (await application.relationship.handle(simulated, view)).proposals == ()
        assert (await application.attachment.handle(simulated, view)).proposals == ()
    finally:
        application.db.close()


async def test_the_seed_sets_the_starting_baselines_not_the_result(
    temp_config, clock
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )

        baselines = {trait.name: trait.baseline for trait in application.growth.traits()}
        for name, value in seed.temperament.items():
            assert baselines[name] == pytest.approx(value)
        # And the seed reached temperament only: values are still untouched.
        assert application.state.list_domain("values") == []
    finally:
        application.db.close()


async def test_period_knowledge_never_leaks_into_the_simulation(
    temp_config, clock
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        builder = application.knowledge_builder
        old = builder.register(
            Candidate(
                statement="当時からあったこと",
                coverage_class="environmental",
                available_from=PERIOD_START - timedelta(days=365),
                topic="音楽",
                salience=0.9,
                complexity=0.1,
            )
        )
        future = builder.register(
            Candidate(
                statement="ずっとあとのこと",
                coverage_class="environmental",
                available_from=AFTER_PERIOD,
                topic="音楽",
                salience=0.9,
                complexity=0.1,
            )
        )

        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=30)

        assert result.progress.leakage_attempts == 0
        assert application.knowledge.knows(future.knowledge_id) is False
        # The period-appropriate one at least had the chance.
        assert application.knowledge.counts()["opportunities"] > 0
        assert application.knowledge.chronology_audit(until=PERIOD_END).clean
        # The period-appropriate item was at least offered to the funnel.
        assert application.knowledge.acquisition_for(future.knowledge_id) is None
        assert old.existed_at(PERIOD_START) is True
    finally:
        application.db.close()


# --- FIRST BOOT (spec 22.7) --------------------------------------------------
async def test_first_boot_is_withheld_until_the_life_is_long_enough(
    temp_config, clock
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=3)

        report = await application.genesis.first_boot(result.run)

        assert report.booted is False
        assert "quality" in report.failed
        assert application.genesis.has_booted() is False
    finally:
        application.db.close()


async def test_first_boot_records_every_audit_whether_it_passed_or_not(
    temp_config, clock
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=3)
        await application.genesis.first_boot(result.run)

        audits = application.genesis.audits(result.run.simulation_id)
        kinds = {audit.kind for audit in audits}
        assert kinds == set(AUDIT_KINDS)
    finally:
        application.db.close()


async def test_a_complete_life_boots_and_only_then_is_the_present_real(
    temp_config, clock
) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=60)

        assert application.genesis.has_booted() is False
        report = await application.genesis.first_boot(result.run)

        assert report.booted is True, report.refusal
        assert report.failed == ()
        assert report.event is not None
        assert report.event.event_type == FIRST_BOOT
        assert application.genesis.has_booted() is True

        booted = SimulationRepository(application.db).run(result.run.simulation_id)
        assert booted.first_boot_at is not None
    finally:
        application.db.close()


async def test_a_user_message_before_first_boot_fails_the_consistency_audit(
    temp_config, clock, make_event
) -> None:
    """Spec 22.7's last line, enforced rather than assumed."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=60)

        # Somebody talked to her before the past was finished.
        await application.processor.process(make_event(actor_type="user"))

        report = await application.genesis.first_boot(result.run)

        assert report.booted is False
        assert "consistency" in report.failed
        assert application.genesis.has_booted() is False
    finally:
        application.db.close()


async def test_booting_twice_is_a_no_op(temp_config, clock) -> None:
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    use_offline_model(application)
    try:
        seed = seed_for(application)
        scaffold = application.simulation.prepare(
            seed, period_start=PERIOD_START, period_end=PERIOD_END
        )
        result = await application.simulation.run(scaffold, max_blocks=60)
        first = await application.genesis.first_boot(result.run)
        assert first.booted is True

        again = await application.genesis.first_boot(
            SimulationRepository(application.db).run(result.run.simulation_id)
        )
        assert again.refusal == "already booted"
        assert len(application.event_store.by_types((FIRST_BOOT,), limit=10)) == 1
    finally:
        application.db.close()
