"""INVARIANT: silence is a decision, and there are turns she may not leave
(rebuild spec 12, Phase 4).

Two failures sit on either side of this.

A bot that always answers is not a companion, it is a service desk: every 「うん」
gets a sentence back, and the conversation can never close. That is what the
system did before.

A bot that goes quiet when it is confused is worse, because the USER cannot tell
the difference between restraint and a crash. So every degraded path here lands
on answering, and there is a hard veto Python holds and the model cannot argue
with: a question, a request, a correction, someone having a bad day, something
urgent. Those are turns where silence does not read as restraint — it reads as
being ignored.
"""

from __future__ import annotations

import pytest

from app.conversation.response_intent import (
    IntentDecision,
    ResponseIntent,
    ResponseIntentGate,
    VetoReason,
)
from app.conversation.social_interpretation import SocialInterpretation

pytestmark = pytest.mark.invariant


@pytest.fixture
def gate() -> ResponseIntentGate:
    return ResponseIntentGate()


def _social(**overrides) -> SocialInterpretation:
    return SocialInterpretation(**overrides)


def _wants_silence(**overrides) -> SocialInterpretation:
    fields = {
        "primary_move": "acknowledge",
        "wants_to_speak": "silence",
        "topic_direction": "close",
        "response_energy": "very_low",
    }
    fields.update(overrides)
    return _social(**fields)


# --- 12.1: the four modes ----------------------------------------------------


def test_only_intentional_silence_says_nothing() -> None:
    assert ResponseIntent.NORMAL_REPLY.speaks
    assert ResponseIntent.BRIEF_REPLY.speaks
    assert ResponseIntent.LEAVE_SPACE.speaks
    assert not ResponseIntent.INTENTIONAL_SILENCE.speaks


@pytest.mark.parametrize(
    ("inclination", "expected"),
    [
        ("speak", ResponseIntent.NORMAL_REPLY),
        ("brief", ResponseIntent.BRIEF_REPLY),
        ("leave_space", ResponseIntent.LEAVE_SPACE),
    ],
)
def test_a_speaking_inclination_is_taken_as_it_stands(
    gate: ResponseIntentGate, inclination, expected
) -> None:
    decision = gate.decide(
        _social(wants_to_speak=inclination), user_text="今日はいい天気だね"
    )
    assert decision.intent is expected
    assert decision.source == "llm"
    assert not decision.was_vetoed


# --- 12.2: the hard veto — turns she may not leave ---------------------------


VETO_CASES = [
    ("ゆいは何歳？", VetoReason.DIRECT_QUESTION, {}),
    ("これ教えて", VetoReason.EXPLICIT_REQUEST, {}),
    ("ちょっとお願いがあるんだけど", VetoReason.EXPLICIT_REQUEST, {}),
    ("助けて", VetoReason.URGENT, {}),
    ("今日ずっと落ち込んでてしんどかった", VetoReason.SUPPORT_NEEDED,
     {"user_state_hint": "possibly_negative"}),
    ("それについてなんだけど", VetoReason.MOVE_REQUIRES_SPEECH, {"primary_move": "answer"}),
    ("あれってどういう意味", VetoReason.MOVE_REQUIRES_SPEECH, {"primary_move": "clarify"}),
]


@pytest.mark.parametrize(("text", "expected", "overrides"), VETO_CASES)
def test_a_required_response_is_never_left_silent(
    gate: ResponseIntentGate, text, expected, overrides
) -> None:
    """Spec 12.2: Silence を原則禁止 の一覧."""
    decision = gate.decide(_wants_silence(**overrides), user_text=text)

    assert decision.intent is ResponseIntent.NORMAL_REPLY
    assert decision.veto is expected
    assert decision.source == "veto"
    # The model's wish is kept, so the override is auditable rather than silent.
    assert decision.proposed is ResponseIntent.INTENTIONAL_SILENCE


def test_a_pending_correction_is_always_spoken(gate: ResponseIntentGate) -> None:
    """Phase 1 owns correction, and leaving a retraction unsaid is worse than
    anything she could say."""
    decision = gate.decide(
        _wants_silence(), user_text="違うよ", correction="裏づけがない。取り消すこと。"
    )

    assert decision.intent is ResponseIntent.NORMAL_REPLY
    assert decision.veto is VetoReason.CORRECTION


def test_the_veto_only_raises_the_floor(gate: ResponseIntentGate) -> None:
    """A veto turns silence into a reply. It never turns a brief reply into a
    long one — that would be Python overruling a social judgement it has no
    standing to make."""
    decision = gate.decide(_social(wants_to_speak="brief"), user_text="これ教えて")

    assert decision.intent is ResponseIntent.BRIEF_REPLY
    assert not decision.was_vetoed


# --- 12.2: when silence is a normal thing to do ------------------------------


@pytest.mark.parametrize("text", ["うん", "そうだね", "なるほど", "了解", "ok", "そっか"])
def test_a_bare_backchannel_may_be_left(gate: ResponseIntentGate, text) -> None:
    decision = gate.decide(_wants_silence(), user_text=text)

    assert decision.intent is ResponseIntent.INTENTIONAL_SILENCE
    assert decision.source == "llm"
    assert not decision.was_vetoed


def test_a_closing_conversation_may_be_left(gate: ResponseIntentGate) -> None:
    decision = gate.decide(
        _wants_silence(topic_direction="close"), user_text="まあ、そんな感じ"
    )
    assert decision.is_silence


def test_a_soft_close_may_be_left(gate: ResponseIntentGate) -> None:
    decision = gate.decide(
        _wants_silence(primary_move="close_softly", topic_direction="stay"),
        user_text="うん、また今度",
    )
    assert decision.is_silence


def test_wanting_silence_is_not_enough_on_its_own(gate: ResponseIntentGate) -> None:
    """No veto fired, but nothing about the turn makes silence natural either.

    Speaking briefly is the honest middle: she has not been asked anything, and
    she also has no reason to leave a substantive message on read.
    """
    decision = gate.decide(
        _wants_silence(topic_direction="expand", response_energy="normal"),
        user_text="そういえば昨日、駅前の店が新しくなってたんだよね",
    )

    assert decision.intent is ResponseIntent.BRIEF_REPLY
    assert decision.source == "veto"
    assert not decision.is_silence


# --- 12.3: silence is never the failure mode ---------------------------------


def test_an_unreadable_inclination_answers(gate: ResponseIntentGate) -> None:
    """A confused system that goes quiet is indistinguishable from a broken
    one, and the USER cannot tell which they have."""
    social = _social().model_copy(update={"wants_to_speak": "???"})

    decision = gate.decide(social, user_text="やっほー")

    assert decision.intent is ResponseIntent.NORMAL_REPLY
    assert decision.source == "default"


def test_a_degraded_interpretation_answers() -> None:
    """The Phase 3 fallback reading must not be a silent one."""
    assert SocialInterpretation.minimal().wants_to_speak == "speak"
    assert SocialInterpretation.minimal(direct_question=True).wants_to_speak == "speak"


def test_a_correction_reading_always_wants_to_speak() -> None:
    corrected = _wants_silence().with_correction("裏づけがない")
    assert corrected.wants_to_speak == "speak"


# --- the decision is auditable ----------------------------------------------


def test_the_decision_says_who_decided_and_why(gate: ResponseIntentGate) -> None:
    rendered = gate.decide(_wants_silence(), user_text="これ教えて").render()

    assert "NORMAL_REPLY" in rendered
    assert "veto: explicit_request" in rendered
    assert "proposed: INTENTIONAL_SILENCE" in rendered


def test_silence_and_suppression_are_different_things() -> None:
    """Spec 12.3. They must not share a field, a code, or an event type."""
    from app.conversation.events import YUI_INTENTIONAL_SILENCE, YUI_REPLY_SUPPRESSED

    assert YUI_INTENTIONAL_SILENCE != YUI_REPLY_SUPPRESSED

    silent = IntentDecision(intent=ResponseIntent.INTENTIONAL_SILENCE)
    assert silent.is_silence
    # A silence decision carries no failure, no stage and no attempt count —
    # the vocabulary of something going wrong is simply absent.
    assert not hasattr(silent, "stage")
    assert not hasattr(silent, "attempts")
    assert not hasattr(silent, "failure")
