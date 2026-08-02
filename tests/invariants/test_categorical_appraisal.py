"""Categorical appraisal (rebuild spec APP-001..APP-003).

The old contract asked the model for eight floats and hoped they landed on the
scale. They did not always: ``agency: -0.3`` is not a quantity that exists, and
``self_relevance: 9.9`` reached the schema validator rather than being
unthinkable. These tests hold the new contract — the model names a meaning, and
Python owns the ruler.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.bootstrap import Application
from app.psychology.models import (
    APPRAISAL_DIMENSIONS,
    DIMENSION_SCALES,
    AppraisalCandidate,
)
from app.psychology.policy import AppraisalScales, PsychologyPolicy
from tests.support import use_offline_model

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.invariant


@pytest.fixture
def scales() -> AppraisalScales:
    policy = PsychologyPolicy.load(REPO_ROOT / "config" / "policies" / "psychology.yaml")
    return policy.appraisal.scales


def _candidate(**overrides: str) -> AppraisalCandidate:
    fields = {
        "self_relevance": "medium",
        "goal_congruence": "neutral",
        "novelty": "low",
        "certainty": "medium",
        "control": "medium",
        "agency": "other",
        "social_meaning": "neutral",
        "expectation_violation": "low",
        "confidence": "medium",
    }
    fields.update(overrides)
    return AppraisalCandidate(**fields)  # type: ignore[arg-type]


# --- APP-001: the model names a meaning -------------------------------------


def test_every_dimension_is_read_on_a_named_scale() -> None:
    assert set(DIMENSION_SCALES) == set(APPRAISAL_DIMENSIONS)


def test_a_number_is_not_an_appraisal_answer() -> None:
    """APP-001: 0.0〜1.0 小数を直接全部出させる方式を廃止する."""
    with pytest.raises(ValidationError):
        _candidate(self_relevance="0.8")  # type: ignore[arg-type]


def test_the_mapping_lives_in_the_policy_file(scales: AppraisalScales) -> None:
    """APP-001: Mapping は policy file へ置く — not in the prompt, not inline."""
    assert scales.value("self_relevance", "high") > scales.value("self_relevance", "low")
    assert scales.value("goal_congruence", "strong_negative") < 0.0
    assert scales.value("goal_congruence", "neutral") == 0.0
    assert scales.value("goal_congruence", "strong_positive") > 0.0


def test_a_candidate_maps_to_every_dimension(scales: AppraisalScales) -> None:
    dimensions = scales.as_dimensions(_candidate())
    assert set(dimensions) == set(APPRAISAL_DIMENSIONS)
    assert all(isinstance(value, float) for value in dimensions.values())


# --- APP-002: impossible values cannot be produced ---------------------------


def test_agency_cannot_be_negative() -> None:
    """APP-002: 数学的にあり得ない値を作る余地をなくす.

    There is no label for it, so it is not a value the model can reach for.
    """
    with pytest.raises(ValidationError):
        _candidate(agency="-0.3")  # type: ignore[arg-type]
    for label in ("self", "mixed", "other", "situation"):
        assert _candidate(agency=label).agency == label


def test_agency_is_never_off_scale(scales: AppraisalScales) -> None:
    for label in ("self", "mixed", "other", "situation"):
        assert 0.0 <= scales.value("agency", label) <= 1.0


def test_unipolar_dimensions_stay_within_zero_and_one(scales: AppraisalScales) -> None:
    dimensions = scales.as_dimensions(_candidate(self_relevance="high", novelty="high"))
    for name, value in dimensions.items():
        low, high = (-1.0, 1.0) if DIMENSION_SCALES[name] == "valence" else (0.0, 1.0)
        assert low <= value <= high, name


def test_an_invented_category_is_refused() -> None:
    with pytest.raises(ValidationError):
        _candidate(social_meaning="catastrophic")  # type: ignore[arg-type]


def test_a_scale_missing_a_label_is_refused() -> None:
    """A half-filled policy file must not load: the gap would surface as a
    KeyError in the middle of a live turn."""
    with pytest.raises(ValueError):
        AppraisalScales(magnitude={"low": 0.1, "high": 0.9})


def test_a_scale_with_an_unknown_label_is_refused() -> None:
    with pytest.raises(ValueError):
        AppraisalScales(
            magnitude={"low": 0.1, "medium": 0.5, "high": 0.9, "extreme": 1.5}
        )


# --- APP-003: the ruler does not bend ---------------------------------------


def test_the_scale_does_not_depend_on_who_is_reading(scales: AppraisalScales) -> None:
    """APP-003: Appraisal semantics は安定した尺度である.

    ``value`` takes a dimension and a label. It has nowhere to put a
    personality, a mood or a snapshot, which is the point: the same words
    cannot be worth more on a good day than on a bad one.
    """
    first = scales.as_dimensions(_candidate(goal_congruence="positive"))
    second = scales.as_dimensions(_candidate(goal_congruence="positive"))
    assert first == second


def test_the_scale_is_frozen(scales: AppraisalScales) -> None:
    with pytest.raises(ValidationError):
        scales.magnitude = {"low": 0.9, "medium": 0.9, "high": 0.9}  # type: ignore[misc]


def test_an_unknown_dimension_is_a_hard_error(scales: AppraisalScales) -> None:
    with pytest.raises(KeyError):
        scales.value("charisma", "high")
    with pytest.raises(KeyError):
        _candidate().label("charisma")


# --- E2E: the mapped numbers are what the pipeline actually commits ----------


async def test_a_categorical_reading_drives_the_real_pipeline(
    temp_config, clock, make_event
) -> None:
    """APP-001 end to end.

    The model answers in labels only. The run that follows is the real one —
    real processor, real arbitrator, real committer — and what lands in the
    database is the number the policy file says that label is worth.
    """
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        client = use_offline_model(application)
        scales = PsychologyPolicy.load(
            REPO_ROOT / "config" / "policies" / "psychology.yaml"
        ).appraisal.scales

        outcome = await application.processor.process(make_event(actor_type="user"))

        # The answer came from the model, not from the degraded defaults.
        appraisal = outcome.interpretation.appraisal
        assert appraisal.source == "llm"
        assert "AppraisalCandidate" in [
            (request.format_schema or {}).get("title") for request in client.requests
        ]

        # tests/support answers "positive" / "medium" / "other".
        assert appraisal.goal_congruence == scales.value("goal_congruence", "positive")
        assert appraisal.agency == scales.value("agency", "other")
        assert appraisal.self_relevance == scales.value("self_relevance", "medium")

        # And that reading reached committed state rather than stopping at the
        # interpretation: a positive appraisal activates a positive emotion.
        assert outcome.status == "committed"
        emotions = {
            target.split(".", 1)[1]
            for target in outcome.committed_targets
            if target.startswith("emotion.")
        }
        assert emotions
        for name in emotions:
            assert application.state.get("emotion", name) is not None
    finally:
        application.db.close()


async def test_an_off_scale_number_never_reaches_state(
    temp_config, clock, make_event
) -> None:
    """APP-002 end to end: the old contract's failure mode, replayed.

    ``agency: -0.3`` used to validate against ``ge=-1.0`` on a bipolar field and
    be committed. Now it is not a value the schema can express, so the run
    degrades to policy defaults instead of committing an impossible reading.
    """
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        use_offline_model(
            application,
            {
                "AppraisalCandidate": (
                    '{"self_relevance": 0.5, "goal_congruence": -0.3, '
                    '"novelty": 0.4, "certainty": 0.6, "control": 0.5, '
                    '"agency": -0.3, "social_meaning": 0.2, '
                    '"expectation_violation": 0.1, "confidence": 0.6, '
                    '"reason": "off scale"}'
                )
            },
        )

        outcome = await application.processor.process(make_event(actor_type="user"))

        appraisal = outcome.interpretation.appraisal
        assert appraisal.source == "degraded"
        assert 0.0 <= appraisal.agency <= 1.0
    finally:
        application.db.close()
