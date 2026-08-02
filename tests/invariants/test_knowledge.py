"""INVARIANT: YUI never knows something she could not have learned.

* Nothing that postdates a moment may be exposed at that moment (spec 21.4,
  2.11, 34.2-6). A modern *source* is fine; modern *content* is not.
* Being famous is not knowing. Every stage of the exposure funnel can be where
  it stops (spec 21.5).
* The model having read the internet is not evidence about YUI: only an
  acquisition record makes something known (spec 21.1).
* A knowledge candidate without an availability date cannot be checked, so it
  is refused rather than defaulted (spec 21.3).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.knowledge.builder import Candidate, KnowledgeBuilder, KnowledgeError
from app.knowledge.exposure import ExposureFunnel, ExposureSignals
from app.knowledge.policy import KnowledgePolicy
from app.knowledge.service import KnowledgeService
from app.knowledge.temporal import TemporalLeakageError, audit, available, guard
from app.storage.repositories.knowledge import (
    AcquisitionRepository,
    CoverageJobRepository,
    ExposureRepository,
    KnowledgeRepository,
)

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]

PAST = datetime(2003, 6, 1, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2011, 6, 1, 12, 0, tzinfo=timezone.utc)
MODERN = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def knowledge_policy() -> KnowledgePolicy:
    return KnowledgePolicy.load(REPO_ROOT / "config" / "policies" / "knowledge.yaml")


@pytest.fixture
def builder(db, knowledge_policy, clock) -> KnowledgeBuilder:
    return KnowledgeBuilder(
        knowledge=KnowledgeRepository(db),
        jobs=CoverageJobRepository(db),
        policy=knowledge_policy,
        clock=clock,
    )


@pytest.fixture
def knowledge(db, knowledge_policy, clock) -> KnowledgeService:
    return KnowledgeService(
        knowledge=KnowledgeRepository(db),
        exposures=ExposureRepository(db),
        acquisitions=AcquisitionRepository(db),
        policy=knowledge_policy,
        clock=clock,
    )


def candidate(
    statement: str,
    *,
    available_from: datetime,
    coverage_class: str = "environmental",
    topic: str = "生活",
    salience: float = 0.8,
    complexity: float = 0.2,
    **kwargs,
) -> Candidate:
    return Candidate(
        statement=statement,
        coverage_class=coverage_class,  # type: ignore[arg-type]
        available_from=available_from,
        topic=topic,
        salience=salience,
        complexity=complexity,
        **kwargs,
    )


# --- the temporal leakage guard (spec 21.4) ---------------------------------
def test_hindsight_cannot_be_handed_to_a_past_self(builder) -> None:
    future_fact = builder.register(candidate("あとから起きたこと", available_from=LATER))

    with pytest.raises(TemporalLeakageError):
        guard(future_fact, PAST)
    assert future_fact.existed_at(PAST) is False
    assert future_fact.existed_at(LATER) is True


def test_exposing_future_knowledge_raises_rather_than_filters(builder, knowledge) -> None:
    """A leak is a caller bug, not a candidate to drop quietly."""
    future_fact = builder.register(candidate("あとから起きたこと", available_from=LATER))

    with pytest.raises(TemporalLeakageError):
        knowledge.expose(future_fact, moment=PAST, interest=0.9, curiosity=0.9)


def test_a_modern_source_may_attest_to_an_old_fact(builder, knowledge) -> None:
    """Spec 21.4: later sources are fine; later content is not."""
    source = builder.add_source(name="後年の資料", published_at=MODERN)
    old_fact = builder.register(
        candidate(
            "当時すでに公表されていたこと",
            available_from=datetime(1998, 1, 1, tzinfo=timezone.utc),
            source_published_at=MODERN,
            source_id=source.source_id,
        )
    )

    assert old_fact.source_published_at == MODERN
    assert guard(old_fact, PAST) is old_fact
    result = knowledge.expose(old_fact, moment=PAST, interest=0.9, curiosity=0.9)
    assert result.opportunity.occurred_at == PAST


def test_knowledge_that_stopped_being_available_is_also_guarded(builder) -> None:
    item = builder.register(
        candidate(
            "一時的にしか出回らなかったこと",
            available_from=PAST,
            available_until=PAST + timedelta(days=30),
        )
    )

    assert item.existed_at(PAST) is True
    with pytest.raises(TemporalLeakageError):
        guard(item, LATER)


def test_filtering_and_auditing_agree_with_the_guard(builder) -> None:
    old = builder.register(candidate("むかしのこと", available_from=PAST))
    new = builder.register(candidate("あとのこと", available_from=LATER))

    assert available([old, new], PAST) == [old]
    assert audit([old], PAST).clean is True
    report = audit([old, new], PAST)
    assert report.clean is False
    assert report.leaked == (new.knowledge_id,)


def test_the_repository_query_and_the_guard_agree(builder, db) -> None:
    builder.register(candidate("むかしのこと", available_from=PAST))
    builder.register(candidate("あとのこと", available_from=LATER))

    at_past = KnowledgeRepository(db).available_at(PAST)
    assert [item.statement for item in at_past] == ["むかしのこと"]
    for item in at_past:
        assert guard(item, PAST) is item


# --- fame is not knowledge (spec 21.5) --------------------------------------
def test_a_famous_fact_that_never_reached_her_is_not_known(
    builder, knowledge
) -> None:
    item = builder.register(
        candidate("有名だったこと", available_from=PAST, salience=0.95)
    )
    result = knowledge.expose(
        item, moment=PAST, interest=0.9, curiosity=0.9, reach=0.05
    )

    assert result.learned is False
    assert result.outcome.stage_reached == "opportunity"
    assert knowledge.knows(item.knowledge_id) is False


def test_noticing_is_not_learning(builder, knowledge) -> None:
    item = builder.register(
        candidate("目にはしたこと", available_from=PAST, salience=0.9)
    )
    result = knowledge.expose(item, moment=PAST, interest=0.05, curiosity=0.05)

    assert result.learned is False
    assert result.outcome.reached("reached")
    assert result.outcome.reached("retained") is False
    assert knowledge.knows(item.knowledge_id) is False


def test_something_too_hard_is_engaged_with_and_not_understood(
    builder, knowledge
) -> None:
    item = builder.register(
        candidate("難しすぎたこと", available_from=PAST, salience=0.9, complexity=1.0)
    )
    result = knowledge.expose(
        item, moment=PAST, interest=0.9, curiosity=0.9, comprehension_capacity=0.3
    )

    assert result.learned is False
    assert result.outcome.reached("curious")
    assert result.outcome.stage_reached in ("curious", "comprehended")


def test_an_interesting_reachable_fact_can_be_learned(builder, knowledge) -> None:
    item = builder.register(
        candidate("ちゃんと届いたこと", available_from=PAST, salience=0.9, complexity=0.1)
    )
    result = knowledge.expose(
        item, moment=PAST, interest=0.9, curiosity=0.9, reach=0.9
    )

    assert result.learned is True
    assert result.outcome.stage_reached == "retained"
    assert knowledge.knows(item.knowledge_id) is True


def test_every_funnel_stage_can_be_where_it_stops(knowledge_policy) -> None:
    funnel = ExposureFunnel(knowledge_policy.exposure)
    stopped = {
        funnel.evaluate(
            ExposureSignals(
                salience=0.1, reach=0.05, complexity=0.1, interest=0.9, curiosity=0.9
            )
        ).stage_reached,
        funnel.evaluate(
            ExposureSignals(
                salience=0.9, reach=0.9, complexity=0.1, interest=0.02, curiosity=0.02
            )
        ).stage_reached,
        funnel.evaluate(
            ExposureSignals(
                salience=0.9,
                reach=0.9,
                complexity=1.0,
                interest=0.9,
                curiosity=0.9,
                comprehension_capacity=0.0,
            )
        ).stage_reached,
        funnel.evaluate(
            ExposureSignals(
                salience=0.9, reach=0.9, complexity=0.1, interest=0.9, curiosity=0.9
            )
        ).stage_reached,
    }
    assert len(stopped) >= 3
    assert "retained" in stopped


# --- knowing requires a record (spec 21.1) ----------------------------------
def test_existing_in_the_world_is_not_knowing(builder, knowledge) -> None:
    """The knowledge table is the world's, not YUI's."""
    item = builder.register(candidate("世界が知っていたこと", available_from=PAST))

    assert knowledge.knows(item.knowledge_id) is False
    assert knowledge.knows_statement(item.statement) is False
    assert knowledge.counts()["knowledge"] == 1
    assert knowledge.counts()["acquired"] == 0


def test_an_unknown_statement_is_not_known_by_default(knowledge) -> None:
    """Whatever the model may 'know', YUI has no record of it."""
    assert knowledge.knows_statement("だれでも知っているような一般常識") is False
    assert knowledge.knows("knw_does_not_exist") is False


def test_an_opportunity_alone_never_makes_it_known(builder, knowledge, db) -> None:
    item = builder.register(
        candidate("すぐそばにあったこと", available_from=PAST, salience=0.9)
    )
    knowledge.expose(item, moment=PAST, interest=0.01, curiosity=0.01, reach=0.9)

    assert ExposureRepository(db).count() == 1
    assert AcquisitionRepository(db).count() == 0
    assert knowledge.knows(item.knowledge_id) is False


# --- candidates must be checkable (spec 21.3) -------------------------------
def test_a_candidate_needs_a_statement(builder) -> None:
    with pytest.raises(KnowledgeError):
        builder.register(candidate("   ", available_from=PAST))


def test_a_candidate_needs_a_known_coverage_class(builder) -> None:
    with pytest.raises(KnowledgeError):
        builder.register(
            candidate("なにか", available_from=PAST, coverage_class="made_up")
        )


def test_an_availability_window_must_be_coherent(builder) -> None:
    with pytest.raises(ValueError):
        builder.register(
            candidate(
                "矛盾した期間",
                available_from=LATER,
                available_until=PAST,
            )
        )


# --- coverage jobs (spec 21.2) ----------------------------------------------
def test_a_coverage_job_refuses_material_from_outside_its_period(builder) -> None:
    job = builder.request_coverage(
        coverage_class="historical_cultural", period_start=PAST, period_end=LATER
    )

    with pytest.raises(KnowledgeError):
        builder.fulfil(job.job_id, [candidate("ずっとあとのこと", available_from=MODERN)])

    assert builder.coverage_jobs()[0].status == "failed"


def test_a_coverage_job_records_what_it_produced(builder) -> None:
    job = builder.request_coverage(
        coverage_class="environmental", period_start=PAST, period_end=LATER
    )
    completed = builder.fulfil(
        job.job_id,
        [
            candidate("当時のこと1", available_from=PAST),
            candidate("当時のこと2", available_from=PAST + timedelta(days=100)),
        ],
    )

    assert completed.status == "completed"
    assert completed.produced_count == 2


def test_missing_coverage_classes_are_visible(builder) -> None:
    job = builder.request_coverage(
        coverage_class="environmental", period_start=PAST, period_end=LATER
    )
    builder.fulfil(job.job_id, [candidate("当時のこと", available_from=PAST)])

    gaps = builder.gaps(period_start=PAST, period_end=LATER)
    assert "environmental" not in gaps
    assert "foundational" in gaps


# --- audits (spec 22.7) ------------------------------------------------------
def test_the_chronology_audit_passes_on_an_honest_history(builder, knowledge) -> None:
    item = builder.register(
        candidate("ちゃんと届いたこと", available_from=PAST, salience=0.9, complexity=0.1)
    )
    knowledge.expose(item, moment=PAST, interest=0.9, curiosity=0.9, reach=0.9)

    report = knowledge.chronology_audit(until=MODERN)
    assert report.clean is True
    assert report.checked == 1


def test_the_chronology_audit_catches_a_leak(builder, knowledge, db) -> None:
    """If a leak ever got past the guard, the audit still finds it."""
    item = builder.register(candidate("あとのこと", available_from=LATER))
    AcquisitionRepository(db).record(
        knowledge_id=item.knowledge_id,
        opportunity_id=None,
        acquired_at=PAST,  # impossible: acquired before it existed
        comprehension=0.9,
        retention=0.9,
    )

    report = knowledge.chronology_audit(until=MODERN)
    assert report.clean is False
    assert report.leaked == (item.knowledge_id,)


# --- forgetting -------------------------------------------------------------
def test_knowledge_fades_and_volatile_knowledge_fades_faster(
    builder, knowledge, clock
) -> None:
    stable = builder.register(
        candidate(
            "変わらないこと", available_from=PAST, salience=0.9, complexity=0.1,
            stability="STABLE",
        )
    )
    volatile = builder.register(
        candidate(
            "すぐ古くなること", available_from=PAST, salience=0.9, complexity=0.1,
            stability="VOLATILE",
        )
    )
    for item in (stable, volatile):
        knowledge.expose(item, moment=PAST, interest=0.9, curiosity=0.9, reach=0.9)

    clock.advance(days=365 * 3)
    knowledge.apply_fading(now=clock.now())

    assert (
        knowledge.acquisition_for(volatile.knowledge_id).retention
        < knowledge.acquisition_for(stable.knowledge_id).retention
    )
