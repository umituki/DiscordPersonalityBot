"""INVARIANT: a sentence that asserts something is only sent if it is true
(rebuild spec 15, GROUND-001..GROUND-004).

The failure mode is not a model that decides to lie. It is a model completing a
plausible pattern — 「今日は本を読んだ」 — in a system with no way to notice that
no Activity was ever completed. The sentence reads perfectly, and once sent it
becomes a turn, then an episode, then a memory: a fabricated past with a clean
provenance chain, indistinguishable afterwards from a real one.

These tests hold the four rules that stop it, and the last one drives the whole
service to prove that a suppressed draft leaves no trace anywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
from app.grounding.context import GroundingContextBuilder
from app.grounding.models import (
    ACCEPTED_EVIDENCE,
    CLAIM_KINDS,
    Evidence,
    GroundingContext,
)
from app.grounding.policy import GroundingPolicy

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def policy() -> GroundingPolicy:
    return GroundingPolicy.load(REPO_ROOT / "config" / "policies" / "grounding.yaml")


@pytest.fixture
def guard(policy: GroundingPolicy) -> ClaimGroundingGuard:
    return ClaimGroundingGuard(ClaimExtractor(policy))


def _activity(name: str) -> Evidence:
    return Evidence(kind="activity", reference="act_1", summary=name, occurred_at=NOW)


# --- 15.2: what counts as a claim -------------------------------------------


def test_a_completed_action_is_a_claim(guard: ClaimGroundingGuard) -> None:
    verdict = guard.review("今日は本を読んだよ。", GroundingContext())
    assert verdict.blocking
    assert verdict.blocking[0].claim.kind == "yui_completed_action"


def test_a_question_is_not_a_claim(guard: ClaimGroundingGuard) -> None:
    """An assertion needs evidence. Asking is not asserting."""
    assert guard.review("今日は本を読んだ？", GroundingContext()).accepted


def test_a_wish_is_not_a_claim(guard: ClaimGroundingGuard) -> None:
    assert guard.review("本を読みたいな。", GroundingContext()).accepted


def test_a_plan_is_not_a_completed_action(guard: ClaimGroundingGuard) -> None:
    """Spec 18.2, 2.15: a plan is never a completed experience."""
    assert guard.review("明日は散歩する予定。", GroundingContext()).accepted


def test_ordinary_talk_is_not_gated(guard: ClaimGroundingGuard) -> None:
    """§16: 「少し文章がぎこちない」程度を guard で書き換えない.

    A guard that stops ordinary conversation is not protecting anything.
    """
    for text in (
        "うん、そうだね。",
        "こんにちは。",
        "どうしたの？",
        "うれしいな。話せてよかった。",
        "そっか、ゆっくりしてていいよ。",
    ):
        assert guard.review(text, GroundingContext()).accepted, text


def test_every_claim_kind_has_an_evidence_rule() -> None:
    assert set(ACCEPTED_EVIDENCE) == set(CLAIM_KINDS)


# --- 15.3: evidence resolution ----------------------------------------------


def test_a_completed_activity_supports_the_claim(guard: ClaimGroundingGuard) -> None:
    context = GroundingContext(completed_activities_today=(_activity("本を読む"),))
    assert guard.review("今日は本を読んだよ。", context).accepted


def test_a_different_activity_does_not_support_it(guard: ClaimGroundingGuard) -> None:
    """15.3: the evidence must be about what is being claimed."""
    context = GroundingContext(completed_activities_today=(_activity("川沿いを散歩する"),))
    assert not guard.review("今日は本を読んだよ。", context).accepted


def test_the_wrong_kind_of_evidence_does_not_support_it(
    guard: ClaimGroundingGuard,
) -> None:
    """A semantic memory that she likes reading does not make today's reading real."""
    context = GroundingContext(
        known_semantic_memories=(
            Evidence(kind="semantic_memory", reference="sem_1", summary="本を読むのが好き"),
        )
    )
    assert not guard.review("今日は本を読んだよ。", context).accepted


def test_a_tool_claim_needs_a_tool_call(guard: ClaimGroundingGuard) -> None:
    """Spec 26: only the Tool Manager can make a tool claim sayable."""
    assert not guard.review("調べてみたら、そうだった。", GroundingContext()).accepted


def test_a_memory_claim_is_grounded_by_a_recalled_memory(
    guard: ClaimGroundingGuard,
) -> None:
    context = GroundingContext(
        recalled_subjective_memories=(
            Evidence(kind="subjective_memory", reference="mem_1", summary="あのとき楽しかった"),
        )
    )
    assert guard.review("覚えてる、あのとき楽しかったこと。", context).accepted


# --- GROUND-001: the model's own prose is never evidence --------------------


class _FakeEvents:
    """An event store holding one USER message and one YUI reply."""

    def __init__(self) -> None:
        self.events = (
            _FakeEvent("USER_MESSAGE_RECEIVED", "social", "user", "音楽の話をした"),
            _FakeEvent("YUI_MESSAGE_SENT", "social", "yui", "今日は本を読んだよ"),
        )

    def recent(self, *, limit: int = 40):
        return list(self.events)


class _FakeEvent:
    def __init__(self, event_type: str, category: str, actor: str, text: str) -> None:
        self.event_id = f"evt_{event_type}"
        self.event_type = event_type
        self.category = category
        self.actor_type = actor
        self.occurred_at = NOW
        self.payload = type("P", (), {"text": text})()


def test_yuis_own_sentence_is_not_evidence_for_itself(
    guard: ClaimGroundingGuard,
) -> None:
    """GROUND-001. She said 「今日は本を読んだよ」 an hour ago. It is in the event
    store as a real ``YUI_MESSAGE_SENT`` row. That proves she spoke — it does
    not make the book real, and saying it again must not be self-supporting."""
    context = GroundingContextBuilder(events=_FakeEvents()).build(now=NOW)

    assert all(
        item.summary != "今日は本を読んだよ" for item in context.recent_objective_events
    )
    assert not guard.review("今日は本を読んだよ。", context).accepted


def test_the_users_own_message_can_still_be_evidence() -> None:
    """The exclusion is of YUI's authorship, not of the conversation."""
    context = GroundingContextBuilder(events=_FakeEvents()).build(now=NOW)
    assert any(
        item.summary == "音楽の話をした" for item in context.recent_objective_events
    )


def test_the_guard_cannot_reach_the_conversation(guard: ClaimGroundingGuard) -> None:
    """GROUND-001, structurally: ``review`` takes the text and the context.

    There is no parameter through which recent turns — which contain YUI's own
    sentences — could be offered as evidence.
    """
    import inspect

    parameters = set(inspect.signature(guard.review).parameters)
    assert parameters == {"text", "context"}


# --- 15.1: the context is built from rows, before the reply exists ----------


def test_an_unfinished_activity_does_not_ground_a_past_tense_claim() -> None:
    """Spec 18.2: Current Activity is an ongoing fact, not an occurred one."""
    builder = GroundingContextBuilder(activities=_FakeActivities())
    context = builder.build(now=NOW)

    assert context.current_activity  # she is reading right now
    assert not context.completed_activities_today  # she has not finished


def test_a_stale_activity_is_not_today() -> None:
    builder = GroundingContextBuilder(activities=_FakeActivities(days_ago=5))
    assert not builder.build(now=NOW).completed_activities_today


def test_a_failing_source_makes_the_guard_stricter_not_looser(
    guard: ClaimGroundingGuard,
) -> None:
    """A broken read must not open the gate."""

    class _Broken:
        def completed(self, *, limit: int = 50):
            raise RuntimeError("database is gone")

        def ongoing(self):
            raise RuntimeError("database is gone")

    context = GroundingContextBuilder(activities=_Broken()).build(now=NOW)
    assert context.is_empty
    assert not guard.review("今日は本を読んだよ。", context).accepted


def test_the_running_system_actually_wires_the_guard(temp_config, clock) -> None:
    """§4.4: Bootstrap instantiation is not completion — but its absence *is*
    incompletion. The guard is optional in the constructor so a caller with
    nothing to resolve against can still draft; production must never be that
    caller, and this is what says so.
    """
    from app.bootstrap import Application

    application = Application.build(temp_config, clock=clock, configure_logs=False)
    try:
        engine = application.conversation_engine
        assert engine._grounding is not None  # noqa: SLF001
    finally:
        application.db.close()


class _FakeActivities:
    def __init__(self, *, days_ago: int = 0) -> None:
        self._days_ago = days_ago

    def ongoing(self):
        return _FakeActivity("本を読む", ended_at=None, status="ongoing")

    def completed(self, *, limit: int = 50):
        if self._days_ago == 0:
            return []
        return [
            _FakeActivity(
                "本を読む",
                ended_at=NOW - timedelta(days=self._days_ago),
                status="completed",
            )
        ]


class _FakeActivity:
    def __init__(self, name: str, *, ended_at, status: str) -> None:
        self.activity_id = "act_1"
        self.name = name
        self.started_at = NOW - timedelta(hours=1)
        self.ended_at = ended_at
        self.status = status
        self.outcome = None

    @property
    def has_happened(self) -> bool:
        return self.status == "completed" and self.ended_at is not None
