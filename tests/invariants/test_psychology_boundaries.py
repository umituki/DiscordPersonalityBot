"""INVARIANT: immediate psychology stays inside its own lane.

* Emotion, mood and needs are separate states with separate writers
  (spec 9.3, 11.2, 11.3, 41).
* An event does not determine an emotion; its appraisal does (spec 11.1).
* A single event never moves deep state, no matter how strong the feeling
  (spec 2.13, 34.2-5).
* An unusable model answer degrades the reading instead of corrupting state
  (spec 28.2).
"""

from __future__ import annotations

import pytest

from app.bootstrap import Application
from app.psychology.emotion import EmotionEngine
from app.psychology.mood import MoodEngine
from app.psychology.needs import NeedEngine
from app.state import ownership
from tests.unit.test_psychology import (  # noqa: F401 - shared fixtures
    BAD_NEWS,
    GOOD_NEWS,
    appraisal_engine,
    psychology_policy,
    view_with,
    with_appraisal,
)

pytestmark = pytest.mark.invariant


def test_each_psychology_domain_has_its_own_writer() -> None:
    assert ownership.owner_of("emotion") == EmotionEngine.name
    assert ownership.owner_of("mood") == MoodEngine.name
    assert ownership.owner_of("needs") == NeedEngine.name
    assert len({EmotionEngine.name, MoodEngine.name, NeedEngine.name}) == 3


@pytest.mark.parametrize(
    "engine_factory,foreign_domains",
    [
        (lambda policy, clock: EmotionEngine(policy.emotion, clock=clock), {"mood", "needs"}),
        (lambda policy, clock: MoodEngine(policy.mood, clock=clock), {"emotion", "needs"}),
        (lambda policy, clock: NeedEngine(policy.needs, clock=clock), {"emotion", "mood"}),
    ],
)
async def test_no_engine_proposes_another_domain(
    psychology_policy, make_event, clock, engine_factory, foreign_domains
) -> None:
    engine = engine_factory(psychology_policy, clock)
    view = with_appraisal(
        view_with(
            emotion__joy=0.6,
            mood__valence=0.5,
            mood__arousal=0.4,
            needs__relatedness_satisfaction=0.5,
        ),
        GOOD_NEWS,
    )

    result = await engine.handle(make_event(actor_type="user"), view)

    domains = {proposal.target_domain for proposal in result.proposals}
    assert domains & foreign_domains == set()


async def test_psychology_never_proposes_deep_state(
    psychology_policy, make_event, clock
) -> None:
    """Spec 34.2-5: one event, however intense, does not touch personality."""
    engines = [
        EmotionEngine(psychology_policy.emotion, clock=clock),
        MoodEngine(psychology_policy.mood, clock=clock),
        NeedEngine(psychology_policy.needs, clock=clock),
    ]
    view = with_appraisal(view_with(mood__valence=0.5, mood__arousal=0.4), BAD_NEWS)

    for engine in engines:
        result = await engine.handle(make_event(actor_type="user"), view)
        for proposal in result.proposals:
            assert proposal.target_domain not in {
                "personality",
                "values",
                "attachment_disposition",
                "narrative_identity",
            }


async def test_a_strong_event_still_commits_only_immediate_state(
    temp_config, clock, make_event
) -> None:
    """End to end: the real pipeline, with the real arbitrator."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        # No Ollama in tests: the appraisal degrades to policy defaults, and
        # the run must still be safe.
        outcome = await application.processor.process(make_event(actor_type="user"))

        assert outcome.status in ("committed", "rejected")
        assert outcome.interpretation is not None
        assert outcome.interpretation.appraisal.source == "degraded"

        changed = {target.split(".")[0] for target in outcome.committed_targets}
        assert changed <= {"emotion", "mood", "needs"}
        assert application.state.get("personality", "openness") is None
    finally:
        application.db.close()


async def test_degraded_appraisal_does_not_corrupt_state(
    appraisal_engine, make_event, snapshots
) -> None:
    engine = appraisal_engine(['{"self_relevance": 9.9}'])  # out of range
    interpretation = await engine.interpret(make_event(), snapshots.capture(persist=False))

    appraisal = interpretation.appraisal
    assert appraisal.source == "degraded"
    assert 0.0 <= appraisal.self_relevance <= 1.0


async def test_emotion_state_survives_the_full_pipeline(
    temp_config, clock, make_event
) -> None:
    """Spec 41: emotion, mood and needs are distinct, persisted states."""
    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        for _ in range(3):
            await application.processor.process(make_event(actor_type="user"))
            clock.advance(seconds=120)

        domains = set(application.state.domains())
        assert "needs" in domains
        # Mood and emotion only appear if something actually activated; needs
        # always respond to contact. What must never happen is a deep domain.
        assert domains <= {"emotion", "mood", "needs"}
    finally:
        application.db.close()
