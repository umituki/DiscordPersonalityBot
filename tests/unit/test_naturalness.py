"""Naturalness is measured, not enforced (Phase 3 §36, §37, §41-§43).

The distinction this file exists to keep: an awkward reply is a thing to improve
and a leaked prompt is a thing to stop. Phase 1 narrowed the Output Guard to
hard detections only; if naturalness quietly grew back into the guard, replies
would get blander every release and nobody would be able to say why.

So these tests check two things. That the metrics measure what §37 lists, and
that nothing in the reply path imports them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.evaluation.naturalness import METRICS, Turn, evaluate

pytestmark = pytest.mark.regression

REPO_ROOT = Path(__file__).resolve().parents[2]


def _turns(*pairs: tuple[str, str, bool]) -> list[Turn]:
    return [
        Turn(user=user, reply=reply, question_needed=needed)
        for user, reply, needed in pairs
    ]


# --- §41: question rate, by necessity rather than by quota -------------------


def test_a_reply_that_always_ends_in_a_question_is_visible() -> None:
    """§41: ``every reply ends with question`` になっていないこと."""
    report = evaluate(
        _turns(
            ("うん", "そうなんだ。何かあった？", False),
            ("べつに", "そっか。どうしたの？", False),
            ("ねむい", "うん。なんで眠いの？", False),
        )
    )

    assert report.question_rate == 1.0
    assert report.unnecessary_questions == 3
    assert report.longest_question_streak == 3


def test_a_question_that_was_called_for_is_not_counted_against_her() -> None:
    """§41: a fixed 30% target is the wrong shape — necessity is the question."""
    report = evaluate(
        _turns(
            ("明日って何時からだっけ", "何時からのこと？", True),
            ("うん", "うん。", False),
        )
    )

    assert report.unnecessary_questions == 0
    assert report.necessary_questions_missed == 0
    assert report.question_rate == 0.5


def test_a_question_that_was_needed_and_not_asked_is_also_counted() -> None:
    report = evaluate(_turns(("あれってどうなった？", "そっか。", True)))
    assert report.necessary_questions_missed == 1


# --- §42: repetition, without demanding zero ---------------------------------


def test_a_repeated_opening_is_visible() -> None:
    report = evaluate(
        _turns(
            ("うん", "ふむ、そうなんだ。", False),
            ("そう", "ふむ、なるほど。", False),
            ("だね", "ふむ、たしかに。", False),
            ("ね", "ふむ、そうかも。", False),
        )
    )

    assert report.most_common_opening == "ふむ"
    assert report.repeated_opening_rate == 1.0


def test_using_the_same_opening_twice_is_not_a_problem() -> None:
    """§42: ゼロ反復を求めない. People repeat their own backchannels."""
    report = evaluate(
        _turns(
            ("うん", "うん。そうだね。", False),
            ("そう", "うん。わかる。", False),
            ("だね", "そっか。", False),
            ("ね", "たしかに。", False),
        )
    )
    assert report.repeated_opening_rate <= 0.5


# --- §43: assistant tells, measured not banned -------------------------------


def test_stock_assistant_phrasing_is_counted() -> None:
    report = evaluate(
        _turns(
            ("本読んでる", "それは興味深いですね。", False),
            ("うん", "何か他に話したいことはありますか？", False),
        )
    )
    assert report.ai_tell_rate == 1.0


def test_one_such_phrase_is_not_treated_as_a_failure() -> None:
    """§43: 単発使用は禁止しない — it is a rate, and a rate is not a verdict."""
    report = evaluate(
        _turns(
            ("本読んでる", "それは興味深いですね。", False),
            ("うん", "うん。", False),
            ("そう", "そっか。", False),
            ("ね", "だね。", False),
        )
    )
    assert 0 < report.ai_tell_rate <= 0.25


# --- length ------------------------------------------------------------------


def test_a_paragraph_answering_one_word_is_visible() -> None:
    report = evaluate(_turns(("うん", "うん、" + "そうだね。" * 20, False)))
    assert report.overlong_rate == 1.0


def test_a_short_reply_to_a_short_message_is_fine() -> None:
    report = evaluate(_turns(("うん", "うん。", False)))
    assert report.overlong_rate == 0.0


# --- the boundary ------------------------------------------------------------


def test_every_metric_the_spec_lists_has_a_name() -> None:
    assert set(METRICS) == {
        "japanese_naturalness",
        "turn_appropriateness",
        "question_necessity",
        "response_length",
        "repetition",
        "relationship_register",
        "self_disclosure_balance",
        "topic_flow",
        "human_likeness",
    }


def test_the_reply_path_does_not_import_the_evaluator() -> None:
    """§36: Naturalness Guard を作らない.

    Structural rather than aspirational: if the conversation package ever
    imports this module, a measurement has become a gate.
    """
    package = REPO_ROOT / "app" / "conversation"
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "naturalness" not in node.module, path.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "naturalness" not in alias.name, path.name


def test_the_evaluator_returns_rates_rather_than_a_verdict() -> None:
    report = evaluate(_turns(("うん", "うん。", False)))
    assert not hasattr(report, "accepted")
    assert not hasattr(report, "rejected")
    assert not hasattr(report, "issues")


def test_an_empty_transcript_is_not_an_error() -> None:
    assert evaluate([]).turns == 0
