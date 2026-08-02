"""INVARIANT: a moment is not a trait (spec 14, 34.3).

``USER short message once → no trait overgeneralization`` is one of the
specification's required scenarios. This suite also pins the other spec 14
separations: state versus trait, provisional versus established, and a model
error being recorded as a discrepancy rather than as the USER having changed.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app.psychology.models import Appraisal
from app.social.policy import RelationshipPolicy
from app.social.user_model import (
    COUNTER_DOMAIN,
    DOMAIN,
    MODEL_ERRORS,
    OBSERVATION_COUNT,
    PROVISIONAL,
    STATE_ENERGY,
    STATE_WARMTH,
    TRAIT_WARMTH,
    SocialCognitionEngine,
)
from tests.unit.test_psychology import GOOD_NEWS, view_with, with_appraisal

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]

WARM = Appraisal(
    self_relevance=0.7, goal_congruence=0.6, social_meaning=0.9, certainty=0.7, confidence=0.6
)
SURPRISING = Appraisal(
    self_relevance=0.7, goal_congruence=0.1, social_meaning=0.2, certainty=0.4,
    expectation_violation=0.9, confidence=0.6,
)


@pytest.fixture
def user_model_policy() -> RelationshipPolicy:
    return RelationshipPolicy.load(REPO_ROOT / "config" / "policies" / "relationship.yaml")


@pytest.fixture
def engine(user_model_policy, clock) -> SocialCognitionEngine:
    return SocialCognitionEngine(user_model_policy.user_model, clock=clock)


def values_of(result) -> dict[str, float]:
    return {proposal.target_key: proposal.value for proposal in result.proposals}


async def feed(engine, make_event, appraisal, state: dict, *, times: int, text: str) -> dict:
    """Repeat the same kind of message, carrying the model forward."""
    carried = dict(state)
    for _ in range(times):
        view = with_appraisal(view_with(**carried), appraisal)
        result = await engine.handle(make_event(actor_type="user", text=text), view)
        for key, value in values_of(result).items():
            domain = COUNTER_DOMAIN if key.startswith("evidence_") or key in (
                OBSERVATION_COUNT,
                MODEL_ERRORS,
            ) else DOMAIN
            carried[f"{domain}__{key}"] = value
    return carried


async def test_one_warm_message_does_not_make_a_warm_person(engine, make_event) -> None:
    view = with_appraisal(view_with(**{f"{DOMAIN}__{TRAIT_WARMTH}": 0.5}), WARM)

    result = await engine.handle(
        make_event(actor_type="user", text="きょうは会えてうれしかった"), view
    )
    values = values_of(result)

    # The state estimate moves…
    assert values[STATE_WARMTH] > 0.5
    # …and the trait does not.
    assert TRAIT_WARMTH not in values


async def test_a_trait_moves_only_after_repeated_observation(
    engine, make_event, user_model_policy
) -> None:
    required = user_model_policy.user_model.trait_evidence_required
    start = {f"{DOMAIN}__{TRAIT_WARMTH}": 0.5}

    short_of_it = await feed(
        engine, make_event, WARM, start, times=required - 1, text="うれしい"
    )
    assert short_of_it[f"{DOMAIN}__{TRAIT_WARMTH}"] == pytest.approx(0.5)

    enough = await feed(engine, make_event, WARM, short_of_it, times=3, text="うれしい")
    assert enough[f"{DOMAIN}__{TRAIT_WARMTH}"] > 0.5
    # And even then it barely moves.
    assert enough[f"{DOMAIN}__{TRAIT_WARMTH}"] < 0.62


async def test_a_thin_model_is_marked_provisional(
    engine, make_event, user_model_policy
) -> None:
    result = await engine.handle(
        make_event(actor_type="user", text="やあ"),
        with_appraisal(view_with(), GOOD_NEWS),
    )
    # It starts provisional and stays there, so there is nothing to change yet.
    assert PROVISIONAL not in values_of(result)
    assert result.events[0].payload.provisional is True

    grown = await feed(
        engine,
        make_event,
        GOOD_NEWS,
        {},
        times=user_model_policy.user_model.provisional_until_observations + 1,
        text="やあ",
    )
    assert grown[f"{DOMAIN}__{PROVISIONAL}"] == 0.0


async def test_state_estimates_fade_when_nothing_is_observed(
    engine, make_event, clock
) -> None:
    """A read of "they seem low" should not persist for days on its own."""
    stale = view_with(
        updated_at=clock.now() - timedelta(days=2),
        **{f"{DOMAIN}__{STATE_ENERGY}": 0.95},
    )

    result = await engine.handle(
        make_event(actor_type="user", text="ん"), with_appraisal(stale, GOOD_NEWS)
    )

    assert values_of(result)[STATE_ENERGY] < 0.95


async def test_a_violated_expectation_is_recorded_as_a_model_error(
    engine, make_event
) -> None:
    """Spec 14: ``person_changed`` and ``model_was_wrong`` are different."""
    result = await engine.handle(
        make_event(actor_type="user", text="やっぱりやめる"),
        with_appraisal(view_with(), SURPRISING),
    )
    values = values_of(result)

    assert values[MODEL_ERRORS] == 1.0
    # The surprise is logged as a discrepancy; no trait is rewritten from it.
    assert TRAIT_WARMTH not in values


async def test_only_the_user_is_modelled(engine, make_event) -> None:
    """Spec 2.10: an NPC never merges into the USER model."""
    npc = await engine.handle(
        make_event(actor_type="npc", origin="virtual_life", text="こんにちは"),
        with_appraisal(view_with(), WARM),
    )
    assert npc.proposals == ()


async def test_counters_do_not_live_in_the_bounded_model_space(engine, make_event) -> None:
    result = await engine.handle(
        make_event(actor_type="user", text="やあ"), with_appraisal(view_with(), WARM)
    )

    by_domain = {proposal.target_key: proposal.target_domain for proposal in result.proposals}
    assert by_domain[OBSERVATION_COUNT] == COUNTER_DOMAIN
    assert by_domain[STATE_WARMTH] == DOMAIN


async def test_the_user_model_writes_nothing_else(engine, make_event) -> None:
    result = await engine.handle(
        make_event(actor_type="user", text="やあ"), with_appraisal(view_with(), WARM)
    )
    domains = {proposal.target_domain for proposal in result.proposals}
    assert domains <= {DOMAIN, COUNTER_DOMAIN}
