"""Python appraisal for ordinary experiences (patch spec 12.3).

``routine/minorはvalence/significance/contextからPython heuristic appraisalを
作ってよい``. What must not happen is the two failure modes on either side of
it: a model call for every ordinary day (unaffordable, which is how the
appraisal got skipped in the first place), or no appraisal at all (no
psychology, which is what actually happened).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.psychology.appraisal import AppraisalEngine
from app.psychology.heuristic import appraise_event
from app.psychology.models import APPRAISAL_DIMENSIONS
from app.psychology.policy import PsychologyPolicy
from app.simulation.events import SIMULATED_EXPERIENCE, SimulatedExperiencePayload

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def rules():
    policy = PsychologyPolicy.load(REPO_ROOT / "config" / "policies" / "psychology.yaml")
    return policy.appraisal.heuristic


def experience(make_event, **kwargs):
    payload = dict(block_id="blk_1", experience_class="routine", valence=0.0)
    payload.update(kwargs)
    return make_event(
        event_type=SIMULATED_EXPERIENCE,
        category="internal",
        origin="simulated_past",
        actor_type="yui",
        payload=SimulatedExperiencePayload(**payload),
    )


def test_it_produces_a_real_appraisal(make_event, rules) -> None:
    """Patch spec 12.3: ``正式Appraisal objectが下流へ届くこと``."""
    appraisal = appraise_event(experience(make_event, valence=0.4), rules)

    assert appraisal.source == "heuristic"
    assert appraisal.is_trusted
    assert set(appraisal.as_dimension_map()) == set(APPRAISAL_DIMENSIONS)


def test_a_good_day_and_a_bad_day_are_read_differently(make_event, rules) -> None:
    """Nothing maps an event *type* to a feeling (spec 11.1)."""
    good = appraise_event(experience(make_event, valence=0.8), rules)
    bad = appraise_event(experience(make_event, valence=-0.8), rules)

    assert good.goal_congruence > 0 > bad.goal_congruence
    assert good.goal_congruence == pytest.approx(-bad.goal_congruence)


def test_a_flat_day_is_neither(make_event, rules) -> None:
    flat = appraise_event(experience(make_event, valence=0.0), rules)
    assert flat.goal_congruence == pytest.approx(0.0)
    assert flat.expectation_violation == pytest.approx(0.0)


def test_significance_raises_self_relevance(make_event, rules) -> None:
    dull = appraise_event(experience(make_event, felt_significance=0.0), rules)
    landed = appraise_event(experience(make_event, felt_significance=0.9), rules)
    assert landed.self_relevance > dull.self_relevance


def test_a_class_that_matters_more_reads_as_more_novel(make_event, rules) -> None:
    routine = appraise_event(experience(make_event, experience_class="routine"), rules)
    minor = appraise_event(experience(make_event, experience_class="minor"), rules)
    assert minor.novelty > routine.novelty


def test_another_person_changes_the_social_reading(make_event, rules) -> None:
    alone = appraise_event(experience(make_event, valence=0.6), rules)
    together = appraise_event(
        experience(make_event, valence=0.6, involves_other_person=True), rules
    )
    assert together.social_meaning > alone.social_meaning
    assert together.agency > alone.agency


def test_it_is_never_as_confident_as_a_considered_reading(make_event, rules) -> None:
    """Spec 24: confidence is a signal and it is capped."""
    appraisal = appraise_event(experience(make_event, valence=1.0), rules)
    assert appraisal.confidence <= 0.5


def test_extreme_input_stays_in_range(make_event, rules) -> None:
    for valence in (-1.0, 1.0):
        appraisal = appraise_event(
            experience(make_event, valence=valence, felt_significance=1.0), rules
        )
        for name in APPRAISAL_DIMENSIONS:
            value = appraisal.dimension(name)
            assert -1.0 <= value <= 1.0


# --- which events take this path -------------------------------------------
@pytest.fixture
def engine(prompt_registry, clock):
    from app.llm.structured import StructuredGenerator
    from app.resources.identity import load_identity
    from tests.unit.test_llm_structured import ScriptedClient

    policy = PsychologyPolicy.load(REPO_ROOT / "config" / "policies" / "psychology.yaml")
    return AppraisalEngine(
        identity=load_identity(REPO_ROOT / "character"),
        prompts=prompt_registry,
        structured=StructuredGenerator(
            ScriptedClient([]), prompts=prompt_registry, clock=clock, max_attempts=1
        ),
        policy=policy.appraisal,
        clock=clock,
    )


@pytest.mark.parametrize("experience_class", ["routine", "minor"])
def test_ordinary_classes_take_the_python_path(engine, make_event, experience_class) -> None:
    assert engine.uses_heuristic(experience(make_event, experience_class=experience_class))


@pytest.mark.parametrize("experience_class", ["meaningful", "major", "turning_point"])
def test_the_ones_worth_reading_carefully_do_not(engine, make_event, experience_class) -> None:
    """Patch spec 12.3: ``Meaningful/MajorのみLLM appraisalでもよい``."""
    assert not engine.uses_heuristic(experience(make_event, experience_class=experience_class))


def test_a_user_message_never_takes_the_python_path(engine, make_event) -> None:
    assert not engine.uses_heuristic(make_event(text="やっほー"))


async def test_the_engine_returns_the_heuristic_reading_without_a_model(
    engine, make_event, clock
) -> None:
    from app.state.snapshot import StateSnapshot

    snapshot = StateSnapshot.empty(snapshot_id="snap_1", created_at=clock.now())
    interpretation = await engine.interpret(
        experience(make_event, valence=0.5), snapshot
    )

    assert interpretation.has_appraisal
    assert interpretation.appraisal.source == "heuristic"
