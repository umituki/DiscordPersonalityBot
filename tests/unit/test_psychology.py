"""Appraisal, emotion, mood and needs (spec 11, 15.1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.llm.structured import StructuredGenerator
from app.orchestrator.run_view import Interpretation, RunView
from app.psychology.appraisal import AppraisalEngine
from app.psychology.emotion import EmotionEngine
from app.psychology.events import EMOTION_ACTIVATED, MOOD_SHIFTED, NEED_CHANGED
from app.psychology.mood import MoodEngine
from app.psychology.models import Appraisal
from app.psychology.needs import (
    CONNECTION_DESIRE,
    LONELINESS,
    RELATEDNESS,
    SOLITUDE_DESIRE,
    NeedEngine,
)
from app.psychology.policy import PsychologyPolicy
from app.resources.identity import load_identity
from app.state.snapshot import StateSnapshot
from app.state.value import StateValue
from tests.unit.test_llm_structured import ScriptedClient

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def psychology_policy() -> PsychologyPolicy:
    return PsychologyPolicy.load(REPO_ROOT / "config" / "policies" / "psychology.yaml")


def view_with(updated_at: datetime = NOW, **values: float) -> RunView:
    entries = {}
    for target, value in values.items():
        domain, key = target.split("__", 1)
        entries[(domain, key)] = StateValue(
            domain=domain,
            key=key,
            value=value,
            version=1,
            created_at=updated_at,
            updated_at=updated_at,
        )
    return RunView(
        snapshot=StateSnapshot(snapshot_id="snap_test", created_at=NOW, values=entries)
    )


def with_appraisal(view: RunView, appraisal: Appraisal) -> RunView:
    return view.with_interpretation(Interpretation(appraisal=appraisal))


GOOD_NEWS = Appraisal(
    self_relevance=0.9, goal_congruence=0.9, novelty=0.5, certainty=0.8,
    control=0.7, agency=0.3, social_meaning=0.8, expectation_violation=0.2,
    confidence=0.6, source="llm",
)
BAD_NEWS = Appraisal(
    self_relevance=0.9, goal_congruence=-0.9, novelty=0.3, certainty=0.3,
    control=0.2, agency=0.2, social_meaning=-0.7, expectation_violation=0.6,
    confidence=0.6, source="llm",
)
IRRELEVANT = Appraisal(
    self_relevance=0.05, goal_congruence=0.0, novelty=0.0, certainty=0.9,
    control=0.9, agency=0.5, social_meaning=0.0, expectation_violation=0.0,
    source="llm",
)


# --- appraisal --------------------------------------------------------------


@pytest.fixture
def appraisal_engine(prompt_registry, psychology_policy, clock):
    def build(script: list) -> AppraisalEngine:
        return AppraisalEngine(
            identity=load_identity(REPO_ROOT / "character"),
            prompts=prompt_registry,
            structured=StructuredGenerator(
                ScriptedClient(script), prompts=prompt_registry, clock=clock, max_attempts=1
            ),
            policy=psychology_policy.appraisal,
            clock=clock,
        )

    return build


VALID_APPRAISAL = (
    '{"self_relevance": 0.8, "goal_congruence": 0.6, "novelty": 0.4, "certainty": 0.7, '
    '"control": 0.6, "agency": 0.3, "social_meaning": 0.7, "expectation_violation": 0.1, '
    '"confidence": 0.95, "reason": "ひさしぶりに話しかけられた"}'
)


async def test_appraisal_reads_the_event(appraisal_engine, make_event, snapshots) -> None:
    engine = appraisal_engine([VALID_APPRAISAL])
    interpretation = await engine.interpret(make_event(), snapshots.capture(persist=False))

    appraisal = interpretation.appraisal
    assert appraisal.source == "llm"
    assert appraisal.self_relevance == 0.8
    assert appraisal.reason


async def test_appraisal_prompt_uses_the_events_time(
    appraisal_engine, make_event, snapshots, clock
) -> None:
    event_time = clock.now() - timedelta(days=3650)
    engine = appraisal_engine([VALID_APPRAISAL])
    event = make_event(occurred_at=event_time)

    await engine.interpret(event, snapshots.capture(persist=False))

    request = engine._structured._client.requests[0]  # noqa: SLF001
    assert event_time.isoformat() in request.messages[0].content


async def test_model_confidence_is_capped(
    appraisal_engine, make_event, snapshots, psychology_policy
) -> None:
    """Spec 24: a model's self-reported confidence is not authoritative."""
    engine = appraisal_engine([VALID_APPRAISAL])
    interpretation = await engine.interpret(make_event(), snapshots.capture(persist=False))

    assert interpretation.appraisal.confidence == (
        psychology_policy.appraisal.max_trusted_confidence
    )


async def test_unusable_model_output_degrades_to_defaults(
    appraisal_engine, make_event, snapshots
) -> None:
    engine = appraisal_engine(["not json at all"])
    interpretation = await engine.interpret(make_event(), snapshots.capture(persist=False))

    assert interpretation.appraisal.source == "degraded"
    assert interpretation.appraisal.self_relevance == 0.5


async def test_system_events_are_not_appraised(appraisal_engine, make_event, snapshots) -> None:
    engine = appraisal_engine([])
    interpretation = await engine.interpret(
        make_event(category="system", actor_type="system", origin="system"),
        snapshots.capture(persist=False),
    )
    assert interpretation.has_appraisal is False


# --- emotion ----------------------------------------------------------------


async def test_positive_appraisal_activates_positive_emotion(
    psychology_policy, make_event, clock
) -> None:
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    result = await engine.handle(make_event(), with_appraisal(view_with(), GOOD_NEWS))

    activated = {proposal.target_key for proposal in result.proposals}
    assert "joy" in activated
    assert "sadness" not in activated
    assert all(proposal.target_domain == "emotion" for proposal in result.proposals)
    assert all(proposal.source_module == "emotion_engine" for proposal in result.proposals)


async def test_the_same_event_type_can_produce_opposite_emotions(
    psychology_policy, make_event, clock
) -> None:
    """Spec 11.1: an event does not map to an emotion — its appraisal does."""
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    event = make_event()

    good = await engine.handle(event, with_appraisal(view_with(), GOOD_NEWS))
    bad = await engine.handle(event, with_appraisal(view_with(), BAD_NEWS))

    assert "joy" in {p.target_key for p in good.proposals}
    assert "joy" not in {p.target_key for p in bad.proposals}
    assert {"sadness", "fear", "anger"} & {p.target_key for p in bad.proposals}


async def test_mixed_emotions_are_allowed(psychology_policy, make_event, clock) -> None:
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    result = await engine.handle(make_event(), with_appraisal(view_with(), BAD_NEWS))
    assert len(result.proposals) > 1
    assert len(result.proposals) <= psychology_policy.emotion.max_concurrent


async def test_irrelevant_events_move_nothing(psychology_policy, make_event, clock) -> None:
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    result = await engine.handle(make_event(), with_appraisal(view_with(), IRRELEVANT))
    assert result.proposals == ()


async def test_without_an_appraisal_no_emotion_happens(
    psychology_policy, make_event, clock
) -> None:
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    result = await engine.handle(make_event(), view_with())
    assert result.proposals == ()
    assert result.events == ()


async def test_emotion_decays_between_events(psychology_policy, make_event, clock) -> None:
    """Intensity and duration are separate: time alone lowers intensity."""
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    stale = view_with(
        updated_at=clock.now() - timedelta(hours=6), emotion__joy=0.9
    )
    result = await engine.handle(make_event(), with_appraisal(stale, IRRELEVANT))

    # Nothing re-activates joy, so the engine proposes nothing…
    assert result.proposals == ()

    # …but when something does, it starts from the decayed value, not 0.9.
    activated = await engine.handle(make_event(), with_appraisal(stale, GOOD_NEWS))
    joy = next(p for p in activated.proposals if p.target_key == "joy")
    assert joy.value < 0.9


async def test_activation_records_its_trigger_and_tendency(
    psychology_policy, make_event, clock
) -> None:
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    event = make_event(actor_type="user", actor_id="owner")

    result = await engine.handle(event, with_appraisal(view_with(), GOOD_NEWS))

    activation = next(e for e in result.events if e.event_type == EMOTION_ACTIVATED)
    assert activation.category == "internal"
    assert activation.payload.trigger_event_id == event.event_id
    assert activation.payload.target_id == "owner"
    assert activation.payload.action_tendency  # a tendency, never a behaviour
    assert activation.parent_event_id == event.event_id


async def test_low_certainty_marks_the_emotion_unresolved(
    psychology_policy, make_event, clock
) -> None:
    engine = EmotionEngine(psychology_policy.emotion, clock=clock)
    result = await engine.handle(make_event(), with_appraisal(view_with(), BAD_NEWS))
    activation = next(e for e in result.events if e.event_type == EMOTION_ACTIVATED)
    assert activation.payload.unresolved is True


# --- mood -------------------------------------------------------------------


async def test_mood_follows_emotion_slowly(psychology_policy, make_event, clock) -> None:
    engine = MoodEngine(psychology_policy.mood, clock=clock)
    view = view_with(mood__valence=0.5, mood__arousal=0.4, emotion__joy=0.9)

    result = await engine.handle(make_event(), view)

    valence = next(p for p in result.proposals if p.target_key == "valence")
    assert valence.value > 0.5
    assert valence.value - 0.5 <= psychology_policy.mood.max_change_per_event + 1e-9
    assert all(p.target_domain == "mood" for p in result.proposals)


async def test_mood_is_not_emotion(psychology_policy, make_event, clock) -> None:
    """A strong emotion does not become the mood; it nudges it."""
    engine = MoodEngine(psychology_policy.mood, clock=clock)
    view = view_with(mood__valence=0.5, mood__arousal=0.4, emotion__sadness=1.0)

    result = await engine.handle(make_event(), view)

    valence = next(p for p in result.proposals if p.target_key == "valence")
    assert valence.value < 0.5
    assert valence.value > 0.2  # nowhere near the emotion's own intensity


async def test_mood_returns_towards_baseline_when_nothing_happens(
    psychology_policy, make_event, clock
) -> None:
    engine = MoodEngine(psychology_policy.mood, clock=clock)
    stale = view_with(
        updated_at=clock.now() - timedelta(days=3), mood__valence=0.2, mood__arousal=0.4
    )

    result = await engine.handle(make_event(), stale)

    valence = next(p for p in result.proposals if p.target_key == "valence")
    assert valence.value > 0.2


async def test_mood_shift_is_recorded(psychology_policy, make_event, clock) -> None:
    engine = MoodEngine(psychology_policy.mood, clock=clock)
    result = await engine.handle(
        make_event(), view_with(mood__valence=0.5, mood__arousal=0.4, emotion__joy=0.8)
    )
    assert any(item.event_type == MOOD_SHIFTED for item in result.events)


# --- needs ------------------------------------------------------------------


async def test_contact_raises_relatedness_and_relieves_loneliness(
    psychology_policy, make_event, clock
) -> None:
    engine = NeedEngine(psychology_policy.needs, clock=clock)
    view = view_with(**{f"needs__{RELATEDNESS}": 0.4, f"needs__{LONELINESS}": 0.6})

    result = await engine.handle(make_event(actor_type="user"), view)

    values = {p.target_key: p.value for p in result.proposals}
    assert values[RELATEDNESS] > 0.4
    assert values[LONELINESS] < 0.6
    assert all(p.target_domain == "needs" for p in result.proposals)


async def test_loneliness_and_solitude_desire_are_independent(
    psychology_policy, make_event, clock
) -> None:
    """Spec 15.1: both can be high at the same time."""
    engine = NeedEngine(psychology_policy.needs, clock=clock)
    view = view_with(
        **{
            f"needs__{RELATEDNESS}": 0.5,
            f"needs__{LONELINESS}": 0.7,
            f"needs__{SOLITUDE_DESIRE}": 0.6,
        }
    )

    result = await engine.handle(make_event(actor_type="user"), view)
    values = {p.target_key: p.value for p in result.proposals}

    assert values[LONELINESS] < 0.7  # contact relieves loneliness
    assert values[SOLITUDE_DESIRE] > 0.6  # and simultaneously feeds solitude


async def test_time_alone_raises_loneliness(psychology_policy, make_event, clock) -> None:
    engine = NeedEngine(psychology_policy.needs, clock=clock)
    stale = view_with(
        updated_at=clock.now() - timedelta(days=2),
        **{f"needs__{RELATEDNESS}": 0.6, f"needs__{LONELINESS}": 0.2},
    )

    result = await engine.handle(make_event(actor_type="world"), stale)
    values = {p.target_key: p.value for p in result.proposals}

    assert values[LONELINESS] > 0.2
    assert values[RELATEDNESS] < 0.6


async def test_connection_desire_follows_loneliness(
    psychology_policy, make_event, clock
) -> None:
    engine = NeedEngine(psychology_policy.needs, clock=clock)
    stale = view_with(
        updated_at=clock.now() - timedelta(days=5),
        **{
            f"needs__{RELATEDNESS}": 0.5,
            f"needs__{LONELINESS}": 0.3,
            f"needs__{CONNECTION_DESIRE}": 0.2,
        },
    )

    result = await engine.handle(make_event(actor_type="world"), stale)
    values = {p.target_key: p.value for p in result.proposals}

    assert values[CONNECTION_DESIRE] > 0.2


async def test_need_changes_stay_within_policy(psychology_policy, make_event, clock) -> None:
    engine = NeedEngine(psychology_policy.needs, clock=clock)
    stale = view_with(
        updated_at=clock.now() - timedelta(days=365),
        **{f"needs__{RELATEDNESS}": 0.9, f"needs__{LONELINESS}": 0.1},
    )

    result = await engine.handle(make_event(actor_type="world"), stale)

    for proposal in result.proposals:
        assert 0.0 <= proposal.value <= 1.0
    loneliness = next(p for p in result.proposals if p.target_key == LONELINESS)
    assert loneliness.value - 0.1 <= psychology_policy.needs.max_change_per_event + 1e-9
    assert any(item.event_type == NEED_CHANGED for item in result.events)
