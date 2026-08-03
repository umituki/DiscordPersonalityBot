"""INVARIANT: the decision is made once, in categories, and Python never
writes Japanese (rebuild spec 10, 14, Phase 3).

Two failures are being held apart, and they pull in opposite directions.

One is the schema that grows: ``empathy=0.63, warmth=0.72,
question_probability=0.28``. That is the appraisal failure again — a model
inventing precision it has no basis for, on a scale nobody can defend.

The other is Python growing grammar: ``if casual: sentence += "ね"``. Rules of
that shape produce text that is locally correct and globally strange, and each
fix needs another condition. Python stops at ``casual``, ``short``, ``light``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.conversation.social_interpretation import (
    SocialInterpretation,
    SocialInterpreter,
)
from app.conversation.repetition import SurfaceRepetitionMonitor
from tests.unit.test_conversation import identity  # noqa: F401
from app.conversation.surface import (
    BANDS,
    RelationshipBand,
    SurfacePlan,
    SurfacePlanner,
    relationship_band,
)

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]


def _social(**overrides) -> SocialInterpretation:
    return SocialInterpretation(**overrides)


# --- §2, §3: categories, not numbers -----------------------------------------


def test_the_interpretation_has_no_numeric_fields() -> None:
    """§2: the schema that ruined the appraisal must not come back here."""
    for name, field in SocialInterpretation.model_fields.items():
        assert field.annotation not in (float, int), name


def test_an_invented_move_is_refused() -> None:
    with pytest.raises(ValidationError):
        _social(primary_move="be_charming")


def test_a_probability_is_not_a_question_need() -> None:
    """§19: 「質問率30%」 は禁止. The field cannot express one."""
    with pytest.raises(ValidationError):
        _social(question="0.3")


def test_the_schema_stays_small() -> None:
    """§2, §Phase-3-notes-1: not a hundred enums and fifty if-statements."""
    assert len(SocialInterpretation.model_fields) <= 12


# --- §6: the USER's feelings are never decided -------------------------------


def test_there_is_no_field_that_declares_how_the_user_feels() -> None:
    """§6: ``user_is_sad = true`` のような確定値を持たせない."""
    fields = set(SocialInterpretation.model_fields)
    assert "user_is_sad" not in fields
    assert "user_emotion" not in fields
    assert "user_state_hint" in fields


def test_every_user_state_value_is_hedged() -> None:
    hints = SocialInterpretation.model_fields["user_state_hint"].annotation
    values = set(hints.__args__)  # type: ignore[attr-defined]
    assert values == {
        "unknown",
        "possibly_positive",
        "possibly_negative",
        "possibly_tired",
        "mixed",
    }
    # Every one of them either says "possibly" or admits it does not know.
    assert all(
        value.startswith("possibly_") or value in ("unknown", "mixed")
        for value in values
    )


def test_the_hint_reaches_the_prompt_as_a_hint() -> None:
    rendered = _social(user_state_hint="possibly_negative").render()
    assert "possibly_negative" in rendered
    assert "決めつけず" in rendered


# --- §7: Common Ground is the correction authority ---------------------------


def test_a_retraction_overrides_whatever_the_model_read() -> None:
    """§7: the tracker has the evidence; the interpreter has an opinion."""
    read_as_chat = _social(primary_move="share_thought", question="useful", source="llm")

    corrected = read_as_chat.with_correction("直前に自分が言った「…」には裏づけがない")

    assert corrected.primary_move == "repair"
    assert corrected.question == "none"
    assert corrected.source == "correction"
    # The original reading is kept as the secondary move rather than discarded.
    assert corrected.secondary_move == "share_thought"


def test_no_correction_leaves_the_reading_alone() -> None:
    original = _social(primary_move="acknowledge", source="llm")
    assert original.with_correction("") is original


# --- §10, §11: Python decides size, never grammar ----------------------------


SURFACE_MODULES = ("surface.py", "repetition.py", "social_interpretation.py")

#: Particles and fillers. If one of these appears as a string literal in the
#: planning layer, something is assembling Japanese where it should not be.
FORBIDDEN_FRAGMENTS = ("ね", "よ", "うん、", "だね", "ですね", "なるほど", "ふむ")


def test_python_does_not_assemble_japanese() -> None:
    """§11. The rule that is easiest to break and worst to have broken.

    Checked over string literals in the *code* rather than the docstrings,
    which necessarily quote the very thing they forbid.
    """
    for module in SURFACE_MODULES:
        tree = ast.parse((REPO_ROOT / "app" / "conversation" / module).read_text("utf-8"))
        docstrings = {
            ast.get_docstring(node)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings or len(node.value) > 60:
                continue  # a docstring or a prose comment, not an assembled string
            for fragment in FORBIDDEN_FRAGMENTS:
                assert node.value.strip() != fragment, (module, node.value)


def test_the_planner_never_calls_a_model() -> None:
    """§10, §32: no second LLM round to decide "short and casual"."""
    source = (REPO_ROOT / "app" / "conversation" / "surface.py").read_text("utf-8")
    assert "structured" not in source
    assert "LLMMessage" not in source
    assert "async def" not in source


# --- §19, §20: a question budget, never a rate -------------------------------


@pytest.mark.parametrize(
    ("need", "initiative", "expected"),
    [
        ("none", "balanced", 0),
        ("none", "high", 0),
        ("optional", "balanced", 0),
        ("optional", "low", 0),
        ("optional", "high", 1),
        ("useful", "balanced", 1),
        ("necessary", "low", 1),
    ],
)
def test_the_question_budget_is_a_count(need, initiative, expected) -> None:
    social = _social(question=need, initiative=initiative)
    assert SurfacePlanner.question_budget(social) == expected


def test_a_tired_user_is_not_interrogated() -> None:
    """§20: 「今日は疲れた」 に毎回 「何があったの？」 と聞く必要はない."""
    social = _social(
        primary_move="support",
        question="optional",
        initiative="balanced",
        user_state_hint="possibly_tired",
    )
    plan = SurfacePlanner().plan(social, user_text="今日は疲れた")

    assert plan.question_budget == 0
    assert plan.allows_question is False
    assert "質問はしない" in plan.render()


def test_a_necessary_question_is_allowed_exactly_one() -> None:
    plan = SurfacePlanner().plan(
        _social(primary_move="clarify", question="necessary"), user_text="あれってどう？"
    )
    assert plan.question_budget == 1


# --- §39 B: a one-word message does not get a paragraph ----------------------


def test_a_short_message_gets_a_short_reply_plan() -> None:
    plan = SurfacePlanner().plan(
        _social(primary_move="acknowledge", response_energy="low"), user_text="うん"
    )
    assert plan.length in ("very_short", "short")


def test_a_long_message_may_get_a_longer_reply_plan() -> None:
    plan = SurfacePlanner().plan(
        _social(primary_move="continue_story", response_energy="high"),
        user_text="あ" * 200,
    )
    assert plan.length in ("medium", "long")


# --- §15, §16: relationship as a band, with hysteresis -----------------------


def test_familiarity_becomes_a_band() -> None:
    assert relationship_band(0.0) == "stranger"
    assert relationship_band(0.2) == "acquaintance"
    assert relationship_band(0.5) == "familiar"
    assert relationship_band(0.9) == "close"


def test_a_band_does_not_flicker_across_its_edge() -> None:
    """§16: familiarity 0.39 → 0.40 → 0.39 must not swing her twice."""
    band: RelationshipBand = "acquaintance"
    for value in (0.44, 0.46, 0.44, 0.46):
        band = relationship_band(value, current=band)
        assert band == "acquaintance"

    # Clear of the edge by the margin, it does move.
    assert relationship_band(0.52, current=band) == "familiar"


def test_a_band_does_not_fall_back_on_a_twitch() -> None:
    band = relationship_band(0.60, current="familiar")
    assert band == "familiar"
    assert relationship_band(0.43, current=band) == "familiar"
    assert relationship_band(0.30, current=band) == "acquaintance"


def test_a_stranger_is_addressed_politely_and_a_friend_is_not() -> None:
    planner = SurfacePlanner()
    social = _social(primary_move="acknowledge")
    assert planner.plan(social, user_text="こんにちは", band="stranger").register == "polite"
    assert planner.plan(social, user_text="やっほー", band="close").register == "casual"


def test_a_close_friend_having_a_bad_day_is_not_suddenly_addressed_formally() -> None:
    """A serious tone is not a reason to put distance back in."""
    plan = SurfacePlanner().plan(
        _social(primary_move="support", tone="serious"),
        user_text="今日ちょっと嫌なことあった",
        band="close",
    )
    assert plan.register == "casual"


def test_every_band_is_reachable() -> None:
    produced = {relationship_band(value) for value in (0.0, 0.2, 0.5, 0.9)}
    assert produced == set(BANDS)


# --- §21, §22: disclosure needs an experience --------------------------------


def test_an_ungrounded_experience_is_capped_at_an_opinion() -> None:
    """§22: 「わたしも経験した」 needs evidence; 「わたしならこう感じそう」 does not."""
    social = _social(primary_move="self_disclose", self_disclosure="moderate")
    plan = SurfacePlanner().plan(social, user_text="小説読むの好き", grounded_experience=False)
    assert plan.self_disclosure == "light"


def test_a_grounded_experience_may_be_disclosed_fully() -> None:
    social = _social(primary_move="self_disclose", self_disclosure="moderate")
    plan = SurfacePlanner().plan(social, user_text="小説読むの好き", grounded_experience=True)
    assert plan.self_disclosure == "moderate"


# --- §17, §18: repetition is a hint, not a ban -------------------------------


def _turn(speaker: str, content: str, index: int):
    from datetime import datetime, timedelta, timezone

    from app.conversation.models import ConversationTurn

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ConversationTurn(
        turn_id=f"t{index}",
        conversation_id="c",
        event_id=f"e{index}",
        speaker=speaker,  # type: ignore[arg-type]
        author_id=None,
        content=content,
        occurred_at=base + timedelta(minutes=index),
    )


def test_an_overused_opening_is_reported() -> None:
    """§17: the 「ふむ」 problem. Reported, not forbidden."""
    turns = [_turn("yui", "ふむ、そうなんだ。", index) for index in range(4)]
    hints = SurfaceRepetitionMonitor().review(turns)

    assert "ふむ" in hints.openings
    assert "避けろという意味ではない" in hints.render()


def test_using_the_same_backchannel_twice_is_not_reported() -> None:
    """People repeat themselves. Two is a coincidence, not a pattern."""
    turns = [_turn("yui", "うん。そうだね。", index) for index in range(2)]
    assert SurfaceRepetitionMonitor().review(turns).is_empty


def test_a_run_of_questions_is_reported() -> None:
    turns = [_turn("yui", f"どうだった{index}？", index) for index in range(4)]
    hints = SurfaceRepetitionMonitor().review(turns)
    assert hints.question_streak >= 3


def test_the_monitor_reads_only_her_own_turns() -> None:
    turns = [_turn("user", "ふむ、なるほど。", index) for index in range(5)]
    assert SurfaceRepetitionMonitor().review(turns).is_empty


def test_the_monitor_rejects_nothing() -> None:
    """§18: style hints only. It has no verdict to return."""
    import inspect

    source = inspect.getsource(SurfaceRepetitionMonitor)
    for word in ("reject", "Verdict", "raise ", "accepted"):
        assert word not in source


# --- the fallback ------------------------------------------------------------


async def test_an_unavailable_model_produces_the_minimal_reading(
    identity, prompt_registry, clock
) -> None:
    """A degraded reading invents no intention (§3.5 carried forward)."""
    from app.llm.structured import StructuredGenerator
    from tests.unit.test_llm_structured import ScriptedClient

    interpreter = SocialInterpreter(
        identity=identity,
        prompts=prompt_registry,
        structured=StructuredGenerator(
            ScriptedClient(["not json at all"], social_response=None),
            prompts=prompt_registry,
            clock=clock,
            max_attempts=1,
        ),
    )
    # The scripted client answers social calls from its default, so force the
    # failure path by asking it to answer from the (bad) script instead.
    interpreter._structured._client._social_response = None  # noqa: SLF001

    social = await interpreter.interpret(user_text="うん", recent_conversation="")

    assert social.source == "default"
    assert social.primary_move == "acknowledge"
    assert social.question == "none"
    assert social.self_disclosure == "none"


async def test_a_direct_question_is_still_answered_when_the_model_fails(
    identity, prompt_registry, clock
) -> None:
    from app.llm.structured import StructuredGenerator
    from tests.unit.test_llm_structured import ScriptedClient

    interpreter = SocialInterpreter(
        identity=identity,
        prompts=prompt_registry,
        structured=StructuredGenerator(
            ScriptedClient(["nonsense"]), prompts=prompt_registry, clock=clock, max_attempts=1
        ),
    )
    interpreter._structured._client._social_response = None  # noqa: SLF001

    social = await interpreter.interpret(user_text="ゆいは何歳？", recent_conversation="")

    assert social.primary_move == "answer"
    assert social.question == "none"


# --- the plan renders as intentions, never as sentences ----------------------


def test_the_rendered_plan_contains_no_reply_text() -> None:
    plan = SurfacePlan(length="short", register="casual", question_budget=0)
    rendered = plan.render()
    assert "-" in rendered
    # Nothing that could be pasted into a reply.
    assert "「" not in rendered
