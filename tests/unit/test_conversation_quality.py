"""Conversation Quality Guard and Expression Context (patch spec 8-10).

The two rejections this stage exists for came off real hardware on 2026-08-02
and are covered as regressions in ``tests/regression/test_real_conversations.py``.
What is unit-tested here is the mechanism: which shapes are rejected, which are
deliberately left alone, and that the bands handed to the prompt are words
rather than the state table.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.conversation.expression import FLOOR, NOTHING, ExpressionContext
from app.conversation.models import ConversationTurn
from app.conversation.quality import ConversationQualityGuard, QualityIssue
from app.conversation.text import looks_like_question
from app.state.snapshot import StateSnapshot
from app.state.value import StateValue

#: Phase 3 §19: the guard now takes the SurfacePlan's question budget rather
#: than a decision object, so these are simply whether a question is allowed.
NO_QUESTION = False
MAY_ASK = True


@pytest.fixture
def quality():
    return ConversationQualityGuard()


def turn(clock, speaker: str, content: str, minutes: int = 1) -> ConversationTurn:
    return ConversationTurn(
        turn_id=f"turn_{speaker}_{minutes}",
        conversation_id="conv_1",
        event_id=f"evt_{speaker}_{minutes}",
        speaker=speaker,
        author_id="1" if speaker == "user" else None,
        content=content,
        occurred_at=clock.now() - timedelta(minutes=minutes),
    )


# --- 10.1 duplicate greeting ------------------------------------------------
def test_the_same_greeting_twice_is_rejected(quality) -> None:
    """Patch spec 21 Case A, verbatim from the 2026-08-02 run."""
    verdict = quality.review(
        "初めまして、はじめまして。", allows_question=NO_QUESTION, user_text="初めまして～"
    )
    assert verdict.rejected
    assert QualityIssue.DUPLICATE_GREETING in verdict.issues


def test_one_greeting_is_fine(quality) -> None:
    verdict = quality.review("はじめまして。", allows_question=NO_QUESTION, user_text="初めまして～")
    assert verdict.accepted


# --- 10.2 verbatim echo -----------------------------------------------------
def test_reading_the_users_message_back_is_rejected(quality) -> None:
    user_text = "きょうは仕事のあとに図書館へ行ってきた"
    verdict = quality.review(user_text + "。", allows_question=NO_QUESTION, user_text=user_text)
    assert verdict.rejected
    assert QualityIssue.ECHOES_USER in verdict.issues


def test_quoting_a_short_phrase_is_not_an_echo(quality) -> None:
    """A reply that picks up the USER's words and adds to them is normal."""
    verdict = quality.review(
        "図書館いいね。わたしも静かなところは好き。",
        allows_question=NO_QUESTION,
        user_text="きょうは図書館へ行ってきた",
    )
    assert verdict.accepted


# --- 10.3 asking the same thing again ---------------------------------------
def test_asking_again_what_she_just_asked_is_rejected(quality, clock) -> None:
    recent = (
        turn(clock, "yui", "どうしましたか？", minutes=3),
        turn(clock, "user", "いや、特に用はないんだ", minutes=2),
    )
    verdict = quality.review(
        "どうしましたか？", allows_question=MAY_ASK, user_text="いや、特に用はないんだ", recent_turns=recent
    )
    assert verdict.rejected
    assert QualityIssue.REPEATED_QUESTION in verdict.issues


def test_a_new_question_is_allowed_when_the_decision_asked_for_one(quality, clock) -> None:
    recent = (turn(clock, "yui", "どうしましたか？", minutes=3),)
    verdict = quality.review(
        "その本、どんな話だった？", allows_question=MAY_ASK, user_text="本を読んだ", recent_turns=recent
    )
    assert verdict.accepted


# --- 10.4 the decision said no question -------------------------------------
def test_a_question_against_the_decision_is_rejected(quality) -> None:
    """Prohibition 9: ``ask_followup=false`` なのに質問を許す — never."""
    verdict = quality.review("そうなんだ。何かあった？", allows_question=NO_QUESTION, user_text="ねえ")
    assert verdict.rejected
    assert QualityIssue.UNWANTED_QUESTION in verdict.issues


def test_question_need_optional_without_ask_followup_still_means_no_question(
    quality,
) -> None:
    from app.conversation.social_interpretation import SocialInterpretation
    from app.conversation.surface import SurfacePlanner

    social = SocialInterpretation(primary_move="acknowledge", question="optional")
    # 「あってもよい」は「する」ではない。initiative が high でない限り 0 (§19, §20).
    assert SurfacePlanner.question_budget(social) == 0
    assert quality.review("どうしたの？", allows_question=False, user_text="ねえ").rejected


def test_a_statement_is_not_read_as_a_question(quality) -> None:
    assert looks_like_question("どうも、ありがとう") is False
    assert quality.review("どうも、ありがとう。", allows_question=NO_QUESTION, user_text="はい").accepted


# --- 10.5 / 10.6 repetition and emptiness -----------------------------------
def test_the_same_sentence_twice_in_one_reply_is_rejected(quality) -> None:
    verdict = quality.review(
        "うれしいな。ほんとうにうれしいことだ。うれしいな。", allows_question=NO_QUESTION, user_text="よかったね"
    )
    assert verdict.rejected
    assert QualityIssue.SELF_REPETITION in verdict.issues


def test_an_empty_reply_is_rejected(quality) -> None:
    verdict = quality.review("   ", allows_question=NO_QUESTION, user_text="ねえ")
    assert verdict.rejected
    assert verdict.issues == (QualityIssue.EMPTY,)


# --- 10.7 stock phrases -----------------------------------------------------
def test_the_same_stock_sentence_every_turn_is_rejected(quality, clock) -> None:
    recent = (
        turn(clock, "yui", "そう言ってもらえてうれしい。", minutes=5),
        turn(clock, "user", "元気だった?", minutes=4),
        turn(clock, "yui", "そう言ってもらえてうれしい。", minutes=3),
    )
    verdict = quality.review(
        "そう言ってもらえてうれしい。", allows_question=NO_QUESTION, user_text="また話そう", recent_turns=recent
    )
    assert verdict.rejected
    assert QualityIssue.FORMULAIC in verdict.issues


def test_short_acknowledgement_may_repeat_once(quality, clock) -> None:
    recent = (turn(clock, "yui", "うん。", minutes=3),)
    verdict = quality.review(
        "うん。", allows_question=NO_QUESTION, user_text="そうだね", recent_turns=recent
    )
    assert verdict.accepted


def test_replaying_a_recent_user_turn_under_yuis_name_is_rejected(
    quality, clock
) -> None:
    recent = (
        turn(
            clock,
            "user",
            "いや、今の説明は違うよ。私はそうは言っていない",
            minutes=2,
        ),
        turn(clock, "yui", "そうですか、勘違いしていました。", minutes=1),
    )
    verdict = quality.review(
        "いや、今の説明は違うよ。私はそうは言っていない",
        allows_question=NO_QUESTION,
        user_text="前に好きだと言った食べ物、覚えてる？",
        recent_turns=recent,
    )
    assert verdict.rejected
    assert QualityIssue.ECHOES_USER in verdict.issues


def test_replaying_one_sentence_of_a_recent_user_turn_is_rejected(
    quality, clock
) -> None:
    recent = (
        turn(
            clock,
            "user",
            "いや、今の説明は違うよ。私はそうは言っていない",
            minutes=2,
        ),
    )
    verdict = quality.review(
        "いえ、私はそうは言っていないと記憶しています",
        allows_question=NO_QUESTION,
        user_text="前に好きだと言った食べ物、覚えてる？",
        recent_turns=recent,
    )
    assert verdict.rejected
    assert QualityIssue.ECHOES_USER in verdict.issues


def test_arguing_with_an_explicit_correction_is_rejected(quality) -> None:
    verdict = quality.review(
        "わたしは今、そう思っていたつもりです。誤解があったのでしょうか？",
        allows_question=MAY_ASK,
        user_text="いや、今の説明は違うよ。私はそうは言っていない",
    )
    assert verdict.rejected
    assert QualityIssue.CORRECTION_ARGUMENT in verdict.issues


# --- the rejection is legible to the repair prompt --------------------------
def test_the_rejection_is_described_in_words(quality) -> None:
    verdict = quality.review("初めまして、はじめまして。", allows_question=NO_QUESTION, user_text="初めまして～")
    described = quality.describe(verdict)
    assert "挨拶" in described
    assert described.startswith("- ")


# --- Expression Context (patch spec 9) --------------------------------------
def snapshot_with(clock, values: dict[tuple[str, str], float | str], confidence=None):
    now = clock.now()
    return StateSnapshot(
        snapshot_id="snap_1",
        created_at=now,
        values={
            (domain, key): StateValue(
                domain=domain,
                key=key,
                value=value,
                confidence=confidence,
                created_at=now,
                updated_at=now,
            )
            for (domain, key), value in values.items()
        },
    )


def test_expression_is_bands_not_numbers(clock) -> None:
    """Patch spec 9: ``DB数値全量dumpは禁止``."""
    snapshot = snapshot_with(
        clock,
        {
            ("emotion", "joy"): 0.82,
            ("emotion", "affection"): 0.61,
            ("relationship", "familiarity"): 0.05,
            ("relationship", "emotional_closeness"): 0.1,
        },
    )

    rendered = ExpressionContext.from_snapshot(snapshot).render()

    assert "喜び: とても強い" in rendered
    assert "好意: 強い" in rendered
    assert "まだ初対面に近い" in rendered
    assert "親しさ: 低い" in rendered
    # The numbers themselves never reach the prompt.
    assert "0.82" not in rendered
    assert "0.6" not in rendered


def test_weak_values_are_not_mentioned_at_all(clock) -> None:
    snapshot = snapshot_with(clock, {("emotion", "anger"): FLOOR - 0.05})
    assert ExpressionContext.from_snapshot(snapshot).render() == NOTHING


def test_only_the_strongest_few_emotions_reach_the_prompt(clock) -> None:
    snapshot = snapshot_with(
        clock,
        {
            ("emotion", "joy"): 0.9,
            ("emotion", "affection"): 0.8,
            ("emotion", "interest"): 0.7,
            ("emotion", "surprise"): 0.6,
            ("emotion", "sadness"): 0.5,
        },
    )
    lines = ExpressionContext.from_snapshot(snapshot).lines
    assert len(lines) == 3
    assert lines[0].startswith("喜び")
    assert not any(line.startswith("悲しさ") for line in lines)


def test_a_low_confidence_guess_about_the_user_is_not_stated(clock) -> None:
    """Patch spec 9: high-confidence estimates only."""
    unsure = snapshot_with(clock, {("user_model", "state"): "つかれている"}, confidence=0.3)
    sure = snapshot_with(clock, {("user_model", "state"): "つかれている"}, confidence=0.9)

    assert "つかれている" not in ExpressionContext.from_snapshot(unsure).render()
    assert "つかれている" in ExpressionContext.from_snapshot(sure).render()


def test_the_expression_does_not_carry_the_turns_intention(clock) -> None:
    """Phase 3: the intention is rendered by the social interpretation. Holding
    it here as well would put one decision in two places."""
    import inspect

    parameters = set(
        inspect.signature(ExpressionContext.from_snapshot).parameters
    )
    assert parameters == {"snapshot"}


def test_no_snapshot_is_not_an_error(clock) -> None:
    assert ExpressionContext.from_snapshot(None).render() == NOTHING


# --- ResponseContract question policy: all four states -----------------------
#
# The contract has four question policies and the guard checked one of them.
# `forbidden` was enforced; `optional`, `encouraged` and `required` all meant
# "accept", so a turn whose contract *required* a question was satisfied by a
# reply asking none. The state existed in the contract and had no effect at the
# boundary that is supposed to enforce it.

#: A non-greeting turn, so nothing here is decided by the greeting rules.
_USER_TURN = "今日は天気がよかったね"
_WITH_QUESTION = "そうだね、外に出たくなる。そっちはどうだった？"
_WITHOUT_QUESTION = "そうだね、外に出たくなる陽気だった。"


def _review(quality, policy: str, reply: str):
    return quality.review(
        reply,
        # Deliberately the permissive boolean in every case. The contract is
        # the authority when it is present, so this value must not change any
        # answer below — including the `forbidden` ones.
        allows_question=MAY_ASK,
        question_policy=policy,
        user_text=_USER_TURN,
    )


def test_forbidden_rejects_a_question(quality) -> None:
    """A."""
    verdict = _review(quality, "forbidden", _WITH_QUESTION)

    assert verdict.rejected
    assert QualityIssue.UNWANTED_QUESTION in verdict.issues


def test_forbidden_accepts_no_question(quality) -> None:
    """B."""
    assert _review(quality, "forbidden", _WITHOUT_QUESTION).accepted


def test_optional_accepts_a_question(quality) -> None:
    """C."""
    assert _review(quality, "optional", _WITH_QUESTION).accepted


def test_optional_accepts_no_question(quality) -> None:
    """D."""
    assert _review(quality, "optional", _WITHOUT_QUESTION).accepted


def test_encouraged_accepts_a_question(quality) -> None:
    """E."""
    assert _review(quality, "encouraged", _WITH_QUESTION).accepted


def test_encouraged_accepts_no_question(quality) -> None:
    """F. Encouraged is a preference, not an obligation.

    Rejecting a good reply for declining a suggestion would make `encouraged` a
    second spelling of `required`, and the contract would have three states.
    """
    assert _review(quality, "encouraged", _WITHOUT_QUESTION).accepted


def test_required_accepts_a_question(quality) -> None:
    """G."""
    assert _review(quality, "required", _WITH_QUESTION).accepted


def test_required_rejects_no_question(quality) -> None:
    """H. The finding: this used to be accepted."""
    verdict = _review(quality, "required", _WITHOUT_QUESTION)

    assert verdict.rejected
    assert QualityIssue.REQUIRED_QUESTION_MISSING in verdict.issues


def test_the_two_question_failures_are_different_reason_codes(quality) -> None:
    """"Asked when told not to" and "did not ask when told to" call for
    opposite repairs, so they must be distinguishable in the trace."""
    unwanted = _review(quality, "forbidden", _WITH_QUESTION)
    missing = _review(quality, "required", _WITHOUT_QUESTION)

    assert QualityIssue.UNWANTED_QUESTION != QualityIssue.REQUIRED_QUESTION_MISSING
    assert QualityIssue.REQUIRED_QUESTION_MISSING not in unwanted.issues
    assert QualityIssue.UNWANTED_QUESTION not in missing.issues


def test_the_repair_prompt_is_told_which_question_failure_it_is(quality) -> None:
    """A new reason code with no description would reach repair as "something
    was unnatural", which is not something a rewrite can act on."""
    missing = quality.describe(_review(quality, "required", _WITHOUT_QUESTION))
    unwanted = quality.describe(_review(quality, "forbidden", _WITH_QUESTION))

    assert "質問が必要" in missing and "含まれていない" in missing
    assert missing != unwanted
    assert "不自然な返事" not in missing, "the issue has no problem description"


def test_the_contract_outranks_the_legacy_boolean(quality) -> None:
    """`allows_question` is not a second authority.

    Every case above passes `allows_question=True`; `forbidden` still rejects
    and `required` still demands. The boolean only speaks when no contract
    reached the guard.
    """
    assert _review(quality, "forbidden", _WITH_QUESTION).rejected
    assert _review(quality, "required", _WITHOUT_QUESTION).rejected


def test_the_legacy_boolean_still_works_without_a_contract(quality) -> None:
    """Callers written before the contract keep their two states — and a
    boolean that cannot express an obligation never imposes one."""
    assert quality.review(
        _WITH_QUESTION, allows_question=NO_QUESTION, user_text=_USER_TURN
    ).rejected
    assert quality.review(
        _WITHOUT_QUESTION, allows_question=NO_QUESTION, user_text=_USER_TURN
    ).accepted
    assert quality.review(
        _WITHOUT_QUESTION, allows_question=MAY_ASK, user_text=_USER_TURN
    ).accepted


def test_an_unrecognised_policy_falls_back_rather_than_skipping(quality) -> None:
    """An unknown policy string is not a licence to check nothing."""
    verdict = quality.review(
        _WITH_QUESTION,
        allows_question=NO_QUESTION,
        question_policy="something_nobody_defined",
        user_text=_USER_TURN,
    )

    assert verdict.rejected
    assert QualityIssue.UNWANTED_QUESTION in verdict.issues


def test_every_question_policy_state_is_covered(quality) -> None:
    """Closed, so adding a fifth state forces a decision here.

    The failure this guards against is exactly the one being fixed: a state was
    added to the contract and the boundary silently treated it as "accept".
    """
    from app.dialogue.response_contract import QuestionPolicy

    outcomes = {
        policy.value: (
            _review(quality, policy.value, _WITH_QUESTION).accepted,
            _review(quality, policy.value, _WITHOUT_QUESTION).accepted,
        )
        for policy in QuestionPolicy
    }

    assert outcomes == {
        "forbidden": (False, True),
        "optional": (True, True),
        "encouraged": (True, True),
        "required": (True, False),
    }
