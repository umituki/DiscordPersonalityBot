"""INVARIANT: understanding is not authority (Dialogue Understanding v2).

The system has one job that only the model can do — read what a Japanese
sentence means in the conversation it arrived in — and one job that only Python
may do: decide what is true. The failures that motivated this file all came
from those two being tangled.

    the reply could not be classified without the question it answered, so
    「静かに過ごしていました」 was checked against nothing;

    the evidence lost its owner on the way to the prompt, so the USER's
    「詠んだよ」 read as hers;

    the ownership filter ran for two claim kinds and not for the two others
    that need it;

    and the common ground was written by re-reading the sent sentence with a
    weaker reader than the one that had just approved it.

So the tests here push on the seam. Interpretation context is allowed in and
must never come out as support; evidence is allowed to support and must never
lose its subject.
"""

from __future__ import annotations

import pytest

from app.conversation.engine import _render_grounding, _render_unusable
from app.dialogue.semantic_claims import (
    EvidenceResolver,
    SemanticClaimCandidate,
    SemanticReviewOutcome,
)
from app.dialogue.situation import SituationBuilder, SituationContext
from app.dialogue.turn import TurnState
from app.dialogue.understanding import UNREAD, TurnUnderstanding
from app.grounding.claims import GroundingVerdict
from app.grounding.models import SELF_CLAIM_KINDS, Evidence, GroundingContext

pytestmark = pytest.mark.invariant


class _Value:
    def __init__(self, name: str, priority: float) -> None:
        self.name = name
        self.priority = priority


def _yui(summary: str, kind: str = "activity", reference: str = "act_1") -> Evidence:
    return Evidence(kind=kind, reference=reference, summary=summary, subject="yui")


def _user(summary: str, kind: str = "objective_event", reference: str = "evt_1") -> Evidence:
    return Evidence(kind=kind, reference=reference, summary=summary, subject="user")


# =============================================================================
# 2 / 3 / 4: ownership survives to the end
# =============================================================================


def test_evidence_carries_its_owner_into_the_prompt() -> None:
    """Requirement 2. 「詠んだよ」 must not arrive as an unattributed fact.

    The old renderer emitted `f"- {item.summary}"`, so a USER-owned fact
    appeared under 「確かなこと」 with nothing saying whose it was.
    """
    context = GroundingContext(
        verified_user_facts=(_user("詩を詠んだ", kind="verified_user_fact", reference="uf_1"),)
    )

    rendered = _render_grounding(context)

    assert "USER" in rendered
    assert "詩を詠んだ" in rendered
    # And it is not offered as something she may say happened to her.
    assert "YUI自身のこととして言えること" not in rendered
    assert "自身の経験として語ってはいけない" in rendered


def test_her_own_facts_and_theirs_are_rendered_apart() -> None:
    context = GroundingContext(
        completed_activities_today=(_yui("本を読んだ"),),
        verified_user_facts=(_user("詩を詠んだ", kind="verified_user_fact", reference="uf_1"),),
    )

    rendered = _render_grounding(context)

    hers = rendered.index("YUI自身のこととして言えること")
    theirs = rendered.index("相手・他者のこと")
    assert rendered.index("本を読んだ") > hers
    assert rendered.index("詩を詠んだ") > theirs


def test_every_claim_about_her_own_life_is_ownership_filtered() -> None:
    """Requirement 3. Two of the four kinds were missing from the filter.

    `yui_experience_habit` and `yui_memory_claim` both accept
    `objective_event`, and a USER message is an objective event — so without
    this the USER mentioning an action supported her claiming the habit.
    """
    assert SELF_CLAIM_KINDS == {
        "yui_completed_action",
        "yui_perception",
        "yui_experience_habit",
        "yui_memory_claim",
    }


@pytest.mark.parametrize(
    "category",
    ["yui_completed_action", "yui_experience_habit", "yui_perception"],
)
def test_a_user_event_cannot_support_a_claim_about_her(category) -> None:
    """Requirement 4, at the resolver rather than at the token overlap.

    The USER's event shares every content word with her claim — which is
    exactly why bigram overlap could not tell them apart.
    """
    evidence = _user("詩を詠んだ")
    context = GroundingContext(recent_objective_events=(evidence,))
    candidate = SemanticClaimCandidate(
        proposition="YUIも詩を詠んだ",
        trigger="わたしも詠んだよ",
        subject="yui",
        category=category,
        supporting_ids=(evidence.evidence_id,),
    )

    resolved = EvidenceResolver().resolve(candidate, context)

    assert not resolved.supported
    assert resolved.blocking
    assert any(reason.startswith("wrong_subject") for reason in resolved.refusals)


def test_her_own_event_does_support_the_same_claim() -> None:
    """The gate has to let the true case through, or it is just a mute button."""
    evidence = _yui("詩を詠んだ")
    context = GroundingContext(completed_activities_today=(evidence,))
    candidate = SemanticClaimCandidate(
        proposition="YUIは詩を詠んだ",
        trigger="詠んだよ",
        subject="yui",
        category="yui_completed_action",
        supporting_ids=(evidence.evidence_id,),
    )

    resolved = EvidenceResolver().resolve(candidate, context)

    assert resolved.supported
    assert not resolved.blocking


# =============================================================================
# Evidence IDs: the model cites, Python resolves
# =============================================================================


def test_an_invented_evidence_id_supports_nothing() -> None:
    """GROUND-001, in the shape a fluent model actually breaks it.

    It will not say "I have no evidence". It will cite something that looks
    exactly like a real identifier.
    """
    context = GroundingContext(completed_activities_today=(_yui("本を読んだ"),))
    candidate = SemanticClaimCandidate(
        proposition="YUIは散歩した",
        trigger="散歩したよ",
        subject="yui",
        category="yui_completed_action",
        supporting_ids=("activity:act_walk_today",),
    )

    resolved = EvidenceResolver().resolve(candidate, context)

    assert not resolved.supported
    assert any(reason.startswith("unknown_evidence") for reason in resolved.refusals)


def test_evidence_of_the_wrong_kind_is_refused() -> None:
    """A tool call cannot make a completed activity true."""
    tool = Evidence(kind="tool_call", reference="tc_1", summary="本を検索した", subject="yui")
    context = GroundingContext(successful_tool_calls=(tool,))
    candidate = SemanticClaimCandidate(
        proposition="YUIは本を読んだ",
        trigger="読んだよ",
        subject="yui",
        category="yui_completed_action",
        supporting_ids=(tool.evidence_id,),
    )

    resolved = EvidenceResolver().resolve(candidate, context)

    assert not resolved.supported
    assert any(reason.startswith("wrong_kind") for reason in resolved.refusals)


def test_citing_nothing_is_recorded_as_citing_nothing() -> None:
    candidate = SemanticClaimCandidate(
        proposition="YUIは本を読んだ",
        trigger="読んだよ",
        subject="yui",
        category="yui_completed_action",
    )

    resolved = EvidenceResolver().resolve(candidate, GroundingContext())

    assert resolved.refusals == ("no_citation",)
    assert resolved.blocking


def test_a_non_assertion_needs_no_evidence() -> None:
    """A wish, a plan or a supposition asserts nothing, so nothing is owed.

    Note what is *not* on this list: `hedged_assertion` and `uncertain_recall`
    both commit to something being true, however softly (audit finding 5).
    """
    for modality in ("intention", "hypothetical", "question"):
        candidate = SemanticClaimCandidate(
            proposition="本を読みたい",
            trigger="読みたいな",
            subject="yui",
            category="yui_completed_action",
            modality=modality,
        )

        resolved = EvidenceResolver().resolve(candidate, GroundingContext())

        assert not resolved.blocking, modality


# =============================================================================
# 1 / 7: interpretation context reaches the reader, and stops being evidence
# =============================================================================


def test_the_turn_carries_the_question_the_reply_answers() -> None:
    """Requirement 1's real subject.

    「静かに過ごしていました」 is only classifiable as a claim about today
    because the question was 「今日は何してた？」.
    """
    turn = TurnState(
        user_text="今日は何してた？",
        understanding=TurnUnderstanding(
            question_target="yui_activity", user_intent="ask_about_yui"
        ),
    )

    assert turn.understanding.asks_about_yuis_day
    assert "今日は何してた？" == turn.user_text
    assert "yui_activity" in turn.render_understanding()


def test_an_identity_question_is_not_a_memory_question() -> None:
    """Requirement 5. 「19歳です」 answering 「何歳？」 is a self fact.

    Classifying it as a memory claim demanded a recalled row for something
    that has nothing to do with recall, and the reply was suppressed.
    """
    understanding = TurnUnderstanding(question_target="yui_identity")

    assert understanding.is_identity_question
    assert not understanding.asks_about_yuis_day
    assert understanding.question_target != "yui_memory"


def test_user_owned_actions_are_visible_in_the_reading() -> None:
    understanding = TurnUnderstanding(referenced_action_owner="user")

    assert understanding.user_owns_the_action
    assert "user" in understanding.render()


# =============================================================================
# 12: the memory query is composed, not copied
# =============================================================================


def test_an_elliptical_message_gets_a_usable_query() -> None:
    """「詩？」 as a raw query retrieves nothing. The referent is a turn back."""
    understanding = TurnUnderstanding(
        resolved_message="さっき話していた詩のこと？", memory_query="詩 短歌 昨日の話"
    )

    assert understanding.retrieval_query("詩？") == "詩 短歌 昨日の話"


def test_the_query_falls_back_to_what_the_user_typed() -> None:
    """Interpretation improves retrieval; it is never load-bearing for it."""
    assert UNREAD.retrieval_query("本を読んだ？") == "本を読んだ？"
    assert TurnState(user_text="本を読んだ？").memory_query == "本を読んだ？"


def test_a_resolved_message_is_used_when_no_query_was_proposed() -> None:
    understanding = TurnUnderstanding(resolved_message="さっきの詩のこと")

    assert understanding.retrieval_query("それは？") == "さっきの詩のこと"


# =============================================================================
# SituationContext: zero is not unknown
# =============================================================================


def test_no_rows_and_no_source_read_differently() -> None:
    """The distinction that cannot be recovered later, so it is captured here.

    A model shown an empty list narrates an empty day. A model shown
    「読めなかった」 does not.
    """

    class NoActivities:
        def current_activity(self):
            return None

        def completed_activities(self, limit=20):
            return []

    empty = SituationBuilder(world=NoActivities()).build()
    broken = SituationBuilder(world=None).build()

    assert empty.get("completed_today").availability == "empty"
    assert broken.get("completed_today").availability == "unavailable"
    assert "記録されていない" in empty.render()
    assert "読めなかった" in broken.render()
    assert "completed_today" in broken.unavailable


def test_an_empty_day_is_not_reported_as_having_done_nothing() -> None:
    class NoActivities:
        def current_activity(self):
            return None

        def completed_activities(self, limit=20):
            return []

    rendered = SituationBuilder(world=NoActivities()).build().render()

    assert "何もしていないという意味ではない" in rendered


def test_a_broken_source_does_not_break_the_turn() -> None:
    class Exploding:
        def current_activity(self):
            raise RuntimeError("gone")

        def completed_activities(self, limit=20):
            raise RuntimeError("gone")

    situation = SituationBuilder(world=Exploding()).build()

    assert isinstance(situation, SituationContext)
    assert "current_activity" in situation.unavailable
    assert situation.render()


def test_user_and_npc_facts_keep_their_owner_in_the_situation() -> None:
    grounding = GroundingContext(
        verified_user_facts=(_user("詩を詠んだ", kind="verified_user_fact", reference="uf_1"),),
    )

    situation = SituationBuilder().build(grounding=grounding)

    rendered = situation.get("user_facts").render()
    assert "USER" in rendered
    assert "USER所有" in situation.render()


def test_values_stay_out_unless_the_turn_is_about_her() -> None:
    """Requirement 11. Everything relevant, nothing dumped.

    A reply that recites her values unprompted is worse than one that omits
    them, so the section is gated on the turn actually calling for it.
    """

    class Values:
        """The real `ValueRepository` API — `all()`, not a fixture-only `top()`."""

        def all(self):
            return [_Value("誠実さ", 0.8)]

    builder = SituationBuilder(values=Values())

    quiet = builder.build(understanding=TurnUnderstanding(self_disclosure_relevant=False))
    personal = builder.build(
        understanding=TurnUnderstanding(self_disclosure_relevant=True)
    )

    assert not quiet.get("self_and_values").has_content
    assert personal.get("self_and_values").has_content


# =============================================================================
# 6: repair knows what it may say
# =============================================================================


def test_repair_is_told_why_each_claim_failed() -> None:
    """"No evidence" and "that is the USER's record" need different rewrites."""
    outcome = SemanticReviewOutcome(
        claims=(
            EvidenceResolver().resolve(
                SemanticClaimCandidate(
                    trigger="わたしも詠んだよ",
                    subject="yui",
                    category="yui_completed_action",
                    supporting_ids=("objective_event:evt_1",),
                ),
                GroundingContext(recent_objective_events=(_user("詩を詠んだ"),)),
            ),
        )
    )

    rendered = _render_unusable(GroundingVerdict(), outcome)

    assert "わたしも詠んだよ" in rendered
    assert "YUI自身のことではない" in rendered


def test_an_invented_citation_is_named_as_one_in_repair() -> None:
    outcome = SemanticReviewOutcome(
        claims=(
            EvidenceResolver().resolve(
                SemanticClaimCandidate(
                    trigger="散歩したよ",
                    subject="yui",
                    category="yui_completed_action",
                    supporting_ids=("activity:made_up",),
                ),
                GroundingContext(),
            ),
        )
    )

    assert "存在しない根拠" in _render_unusable(GroundingVerdict(), outcome)


def test_nothing_unusable_says_so_plainly() -> None:
    assert "裏づけの取れない主張はない" in _render_unusable(GroundingVerdict(), None)


# =============================================================================
# An unavailable reviewer is not a clean draft
# =============================================================================


def test_an_unavailable_review_does_not_read_as_accepted() -> None:
    """The Phase 12 lesson, applied here: could-not-check is not a pass.

    Accepting on a failed classification would switch grounding off exactly
    when the model is having a bad time — which is when it fabricates most.
    """
    outcome = SemanticReviewOutcome(unavailable=True, detail="timeout")

    assert not outcome.accepted
    assert "unavailable" in outcome.describe()


def test_a_review_with_no_claims_is_accepted() -> None:
    assert SemanticReviewOutcome().accepted


# =============================================================================
# Temperature: repair is a rewrite, not composition
# =============================================================================


def test_repair_is_cooler_than_the_realizer() -> None:
    """Repair reused the realizer's warmth by accident.

    The intention is already fixed, the usable facts are supplied and the
    failed claims are named — variety buys nothing and costs accuracy. This is
    a candidate value with its own knob, so a fixture run can compare it.
    """
    from app.conversation.policy import ConversationPolicy

    policy = ConversationPolicy.load("config/policies/conversation.yaml")

    assert policy.generation.repair_temperature < policy.generation.realizer_temperature
    # Not zero: repair still writes Japanese, and 0.0 reads like a form letter.
    assert policy.generation.repair_temperature > 0.0
