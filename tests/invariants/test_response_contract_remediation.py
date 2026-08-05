"""Production regressions for the Real Ollama diagnostic remediation.

These cases exercise the real conversation service and engine with only the
LLM boundary replaced.  The Response Contract is an obligation carrier; the
semantic claim resolver remains the sole factual authority.
"""

from __future__ import annotations

import json

import pytest

from app.bootstrap import Application
from app.conversation.engine import _render_grounding
from app.conversation.quality import QualityIssue
from app.dialogue.response_contract import (
    AnswerTarget,
    ResponseContract,
    ResponseObligation,
)
from app.dialogue.semantic_claims import EvidenceResolver, SemanticClaimCandidate
from app.grounding.memory_semantics import AUTHORITATIVE_MEMORY_FACTS
from app.grounding.models import Evidence, GroundingContext
from app.llm.types import LLMResponse
from tests.support import NOW, OfflineModelClient, mark_born

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


def _owned(config):
    return config.model_copy(
        update={
            "secrets": config.secrets.model_copy(
                update={
                    "discord_owner_user_id": OWNER,
                    "discord_channel_id": CHANNEL,
                }
            )
        }
    )


@pytest.fixture
def application(temp_config, clock):
    built = Application.build(_owned(temp_config), clock=clock, configure_logs=False)
    mark_born(built)
    try:
        yield built
    finally:
        built.db.close()


class QueueModelClient(OfflineModelClient):
    """A schema-keyed queue for first-draft/repair production tests."""

    def __init__(self, queues: dict[str, list[str]]) -> None:
        super().__init__()
        self.queues = {key: list(values) for key, values in queues.items()}

    async def generate(self, request):
        title = (request.format_schema or {}).get("title", "")
        queued = self.queues.get(title)
        if not queued:
            return await super().generate(request)
        self.requests.append(request)
        return LLMResponse(
            text=queued.pop(0), model=self.model, created_at=NOW, latency_ms=5
        )


def _install(application, **queues: list[str]) -> QueueModelClient:
    client = QueueModelClient(queues)
    application.structured._client = client  # noqa: SLF001
    return client


def _understanding(
    *,
    user_intent: str = "acknowledge",
    question_target: str = "none",
    memory_query_intent: str = "none",
) -> str:
    return json.dumps(
        {
            "current_topic": "memory" if question_target == "yui_memory" else "",
            "user_intent": user_intent,
            "question_target": question_target,
            "memory_query_intent": memory_query_intent,
            "referenced_action_owner": "unclear",
            "referenced_subject": "",
            "temporal_scope": "unspecified",
            "resolved_message": "",
            "correction_target": "",
            "memory_query": "",
            "wants_memory": question_target == "yui_memory",
            "self_disclosure_relevant": False,
            "reason": "deterministic response-contract fixture",
        },
        ensure_ascii=False,
    )


def _social(
    *,
    move: str = "acknowledge",
    question: str = "none",
    initiative: str = "balanced",
) -> str:
    return json.dumps(
        {
            "primary_move": move,
            "secondary_move": None,
            "initiative": initiative,
            "question": question,
            "tone": "light",
            "response_energy": "low",
            "topic_direction": "stay",
            "user_state_hint": "unknown",
            "self_disclosure": "none",
            "wants_to_speak": "speak",
            "reason": "deterministic response-contract fixture",
        },
        ensure_ascii=False,
    )


def _claim(
    category: str, proposition: str, *, supporting_ids: list[str] | None = None
) -> dict:
    return {
        "proposition": proposition,
        "trigger": proposition,
        "subject": "yui",
        "category": category,
        "modality": "assertion",
        "temporal_scope": "timeless",
        "supporting_ids": supporting_ids or [],
        "contradicting_ids": [],
    }


def _review(*claims: dict) -> str:
    return json.dumps({"claims": list(claims)}, ensure_ascii=False)


def _contract(*, mode: str, target: str, reason: str = "") -> str:
    """What the reviewer classifies. It no longer returns a fulfilment bool.

    The model says what the reply addressed and in what form; Python reads the
    contract to decide whether that discharges it.
    """
    return json.dumps(
        {"addressed_target": target, "answer_mode": mode, "reason": reason},
        ensure_ascii=False,
    )


_MESSAGE = {"count": 0}


async def _turn(application, clock, text: str):
    from app.interfaces.discord.dto import InboundMessage

    _MESSAGE["count"] += 1
    return await application.conversation.handle_inbound(
        InboundMessage(
            message_id=f"contract-{_MESSAGE['count']}",
            channel_id=CHANNEL,
            channel_type="direct_message",
            author_id=OWNER,
            text=text,
            created_at=clock.now(),
        )
    )


async def test_case_a_social_greeting_allows_one_optional_question(
    application, clock
) -> None:
    _install(
        application,
        TurnUnderstanding=[_understanding(user_intent="greet")],
        SocialInterpretation=[_social(question="optional")],
        ReplyDraft=['{"text":"初めまして。今日はどんな一日だった？"}'],
    )

    result = await _turn(application, clock, "初めまして")

    assert result.should_send
    assert not result.generation.repaired
    assert result.generation.response_contract.response_obligation == "social_reply"
    assert result.generation.response_contract.question_policy == "optional"


async def test_case_b_forbidden_question_is_repaired_and_revalidated(
    application, clock
) -> None:
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="none")],
        ReplyDraft=[
            '{"text":"そうなんだ。何かあった？"}',
            '{"text":"そうなんだ。話してくれてありがとう。"}',
        ],
    )

    result = await _turn(application, clock, "今日は静かだった")

    assert result.should_send
    assert result.generation.repaired
    assert "？" not in result.outbound.text


async def test_case_c_repair_that_repeats_forbidden_question_is_suppressed(
    application, clock
) -> None:
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="none")],
        ReplyDraft=[
            '{"text":"そうなんだ。何かあった？"}',
            '{"text":"わかった。もう少し話す？"}',
        ],
    )

    result = await _turn(application, clock, "今日は静かだった")

    assert result.suppressed
    assert result.outbound is None


async def test_case_d_general_memory_capability_uses_memory_authority(
    application, clock
) -> None:
    authority_id = AUTHORITATIVE_MEMORY_FACTS[0].evidence_id
    model = _install(
        application,
        TurnUnderstanding=[
            _understanding(
                user_intent="ask_about_yui",
                question_target="yui_memory",
                memory_query_intent="memory_range",
            )
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"記憶は選択的で、保存されたことをいつでも全部思い出せるわけではないよ。"}'
        ],
        SemanticClaimReview=[
            _review(
                _claim(
                    "yui_general_memory_capability",
                    "YUIの記憶の想起は選択的である",
                    supporting_ids=[authority_id],
                )
            )
        ],
        ResponseContractAssessment=[
            _contract(
                mode="value_or_proposition", target="general_memory_capability"
            )
        ],
    )

    result = await _turn(application, clock, "昔のことっていつまで思い出せる？")

    assert result.should_send
    assert result.generation.response_contract.answer_target == "general_memory_capability"
    reply_prompt = next(
        request for request in model.requests if request.purpose == "conversation_reply"
    )
    rendered = "\n".join(message.content for message in reply_prompt.messages)
    assert f'"id": "{authority_id}"' in rendered
    assert f'[{authority_id}]' not in rendered


async def test_case_e_repair_removes_unsupported_anecdote_but_keeps_answer(
    application, clock
) -> None:
    authority_id = AUTHORITATIVE_MEMORY_FACTS[0].evidence_id
    general = _claim(
        "yui_general_memory_capability",
        "YUIの記憶の想起は選択的である",
        supporting_ids=[authority_id],
    )
    specific = _claim(
        "yui_specific_memory_recall", "YUIは寂しかった出来事を今も覚えている"
    )
    _install(
        application,
        TurnUnderstanding=[
            _understanding(
                user_intent="ask_about_yui",
                question_target="yui_memory",
                memory_query_intent="general_capability",
            )
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"記憶は選択的だよ。今も寂しかった出来事を覚えている。"}',
            '{"text":"記憶は選択的で、いつでも全部を思い出せるわけではないよ。"}',
        ],
        SemanticClaimReview=[_review(general, specific), _review(general)],
        ResponseContractAssessment=[
            _contract(
                mode="value_or_proposition", target="general_memory_capability"
            )
        ],
    )

    result = await _turn(application, clock, "昔のことは思い出せる？")

    assert result.should_send
    assert result.generation.repaired
    assert "寂しかった出来事" not in result.outbound.text
    assert "記憶は選択的" in result.outbound.text


async def test_case_f_unavailable_authority_allows_honest_direct_uncertainty(
    application, clock, monkeypatch
) -> None:
    unavailable = GroundingContext(
        availability={"memory_authority_facts": "unavailable"}
    )
    monkeypatch.setattr(
        application.conversation._grounding,  # noqa: SLF001
        "build",
        lambda **_kwargs: unavailable,
    )
    _install(
        application,
        TurnUnderstanding=[
            _understanding(
                user_intent="ask_about_yui",
                question_target="yui_memory",
                memory_query_intent="general_capability",
            )
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"今は記憶の範囲を確認できないから、どこまで思い出せるかは答えられない。"}'
        ],
        ResponseContractAssessment=[
            _contract(
                mode="unavailable", target="general_memory_capability"
            )
        ],
    )

    result = await _turn(application, clock, "昔のことはどこまで思い出せる？")

    assert result.should_send
    assert not result.generation.repaired
    assert result.generation.response_contract.source_availability == (
        ("memory_authority_facts", "unavailable"),
    )


def test_case_g_display_wrapper_is_normalized_but_invented_id_fails_closed() -> None:
    authority = Evidence(
        kind="memory_authority",
        reference="mem_auth_123",
        summary="Memory retrieval is selective.",
        subject="world",
    )
    context = GroundingContext(memory_authority_facts=(authority,))

    decorated = SemanticClaimCandidate(
        proposition="YUI memory retrieval is selective",
        trigger="memory retrieval is selective",
        subject="yui",
        category="yui_general_memory_capability",
        supporting_ids=(f"[{authority.evidence_id}]",),
    )
    invented = decorated.model_copy(
        update={"supporting_ids": ("[memory_authority:mem_auth_999]",)}
    )

    assert EvidenceResolver().resolve(decorated, context).supported
    invalid = EvidenceResolver().resolve(invented, context)
    assert invalid.blocking
    assert any(reason.startswith("unknown_evidence") for reason in invalid.refusals)


def test_specific_recall_contract_renders_present_memory_without_empty_marker() -> None:
    recalled = Evidence(
        kind="subjective_memory",
        reference="mem_present",
        summary="海辺で話したこと",
        subject="yui",
    )
    context = GroundingContext(recalled_subjective_memories=(recalled,))
    contract = ResponseContract(
        response_obligation=ResponseObligation.ANSWER,
        answer_target=AnswerTarget.SPECIFIC_MEMORY_RECALL,
        allowed_evidence_kinds=("subjective_memory",),
    )

    rendered = _render_grounding(context, contract)

    assert recalled.evidence_id in rendered
    assert "このanswer targetに使える根拠は記録されていない" not in rendered


async def test_case_h_safe_generic_repair_cannot_drop_the_direct_answer(
    application, clock
) -> None:
    _install(
        application,
        TurnUnderstanding=[
            _understanding(
                user_intent="ask_about_yui", question_target="yui_activity"
            )
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"今日は図書館で本を読んだよ。"}',
            '{"text":"話してくれてありがとう。"}',
        ],
        SemanticClaimReview=[
            _review(_claim("yui_completed_action", "YUIは今日図書館で本を読んだ")),
            _review(),
        ],
        ResponseContractAssessment=[
            _contract(
                mode="non_answer",
                target="direct_user_question",
                reason="The reply does not answer what YUI did today.",
            )
        ],
    )

    result = await _turn(application, clock, "今日は何してた？")

    assert result.suppressed
    assert result.outbound is None
    assert result.generation.quality.issues == ("direct_answer_missing",)


# =============================================================================
# The question policy has four states, and all four reach the send decision
# =============================================================================
#
# `forbidden` was enforced at the Quality Guard and the other three were not
# checked at all, so a turn whose contract *required* a question was satisfied
# by a reply asking none. These exercise the production path: the contract is
# built from the turn reading, carried to the guard, and — when the guard
# rejects — carried unchanged into the repair and the revalidation.

_NO_QUESTION = '{"text":"そうなんだ。ゆっくりでいいよ。"}'
_WITH_QUESTION = '{"text":"そうなんだ。いまはどんな気分？"}'


async def test_required_question_missing_is_repaired_and_sent(
    application, clock
) -> None:
    """CASE 1. The draft asks nothing, the repair asks something, it goes out.

    Python names the failure and supplies the same contract; it does not supply
    the question. The rewrite is the model's, within the constraint.
    """
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="necessary")],
        ReplyDraft=[_NO_QUESTION, _WITH_QUESTION],
    )

    result = await _turn(application, clock, "ちょっと疲れてる")

    assert result.generation.response_contract.question_policy == "required"
    assert result.generation.repaired, "the missing question did not reach the guard"
    assert result.should_send
    assert "？" in result.generation.text or "?" in result.generation.text


async def test_a_repair_that_still_asks_nothing_is_suppressed(
    application, clock
) -> None:
    """CASE 2. The rewrite is revalidated against the same contract, not sent
    on the strength of having been rewritten."""
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="necessary")],
        ReplyDraft=[_NO_QUESTION, '{"text":"そっか。無理しないでね。"}'],
    )

    result = await _turn(application, clock, "ちょっと疲れてる")

    assert result.generation.response_contract.question_policy == "required"
    assert result.suppressed
    assert result.outbound is None
    failure = result.generation.outcome.failure
    assert failure is not None
    assert failure.reason_code == QualityIssue.REQUIRED_QUESTION_MISSING, failure


async def test_an_optional_question_is_not_an_unwanted_one(
    application, clock
) -> None:
    """CASE 3. `optional` means either is fine — the reply asks, and goes."""
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="optional", initiative="high")],
        ReplyDraft=[_WITH_QUESTION],
    )

    result = await _turn(application, clock, "今日はよく晴れてたね")

    assert result.generation.response_contract.question_policy == "optional"
    assert result.should_send
    assert not result.generation.repaired
    assert QualityIssue.UNWANTED_QUESTION not in result.generation.quality.issues


async def test_encouraged_does_not_demand_a_question(application, clock) -> None:
    """CASE 4. A preference, not an obligation.

    If the absence of a question suppressed the reply, `encouraged` would be a
    second spelling of `required`.
    """
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="useful")],
        ReplyDraft=[_NO_QUESTION],
    )

    result = await _turn(application, clock, "今日はよく晴れてたね")

    assert result.generation.response_contract.question_policy == "encouraged"
    assert result.should_send
    assert not result.generation.repaired
    assert QualityIssue.REQUIRED_QUESTION_MISSING not in (
        result.generation.quality.issues
    )


async def test_a_forbidden_question_is_still_removed(application, clock) -> None:
    """CASE 5. The state that already worked keeps working.

    Restated here so the four policies read as one matrix; the original
    regression at `test_case_b` is untouched.
    """
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="none")],
        ReplyDraft=[_WITH_QUESTION, _NO_QUESTION],
    )

    result = await _turn(application, clock, "今日はよく晴れてたね")

    assert result.generation.response_contract.question_policy == "forbidden"
    assert result.generation.repaired
    assert result.should_send
    assert "？" not in result.generation.text


async def test_required_is_reachable_from_a_real_turn_reading(
    application, clock
) -> None:
    """The policy is not decorative: `necessary` maps to it in production.

    Without this, "required is enforced" could be satisfied by a state nothing
    ever produces — which is how it went unnoticed in the first place.
    """
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="necessary")],
        ReplyDraft=[_WITH_QUESTION],
    )

    result = await _turn(application, clock, "ちょっと疲れてる")

    assert result.generation.response_contract.question_policy == "required"
    assert result.should_send


async def test_the_contract_reviewer_is_not_asked_about_questions(
    application, clock
) -> None:
    """The obligation is closed and deterministic, so no model call decides it.

    `ResponseContractReviewer` answers one question — was the answer
    obligation met — and a missing required question must not add a second
    call, nor be routed through the first.
    """
    model = _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(question="necessary")],
        ReplyDraft=[_NO_QUESTION, _WITH_QUESTION],
    )

    await _turn(application, clock, "ちょっと疲れてる")

    purposes = model.purposes()
    assert purposes.count("response_contract_review") <= 1, purposes
    assert "question_policy_review" not in purposes


# =============================================================================
# Real Ollama: a direct answer is a conclusion, not a value of the right shape
# =============================================================================
#
# qwen3.5:9b at 901518d rejected 「年齢という概念はわたしの存在にはありません」 as
# `direct_answer_missing` because it contained no number, repaired it, rejected
# the repair for the same reason, and suppressed the turn. Reproduced here as
# what the reviewer classifies rather than as the Japanese it classified.


async def test_an_inapplicable_concept_answers_an_identity_question(
    application, clock
) -> None:
    """Real Ollama turn 2. Identity contract, no number, sent."""
    model = _install(
        application,
        TurnUnderstanding=[
            _understanding(user_intent="ask_about_yui", question_target="yui_identity")
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=['{"text":"年齢という概念は、わたしの存在にはないんだ。"}'],
        ResponseContractAssessment=[
            _contract(mode="not_applicable", target="identity")
        ],
    )

    result = await _turn(application, clock, "何歳？")

    assert result.generation.response_contract.answer_target == "identity"
    assert result.should_send, "the age answer was suppressed again"
    assert not result.generation.repaired
    assert QualityIssue.DIRECT_ANSWER_MISSING not in result.generation.quality.issues
    # One review call, as before. Classifying the answer form did not add one.
    assert model.purposes().count("response_contract_review") == 1


async def test_a_variable_range_answers_a_range_question(
    application, clock
) -> None:
    """Real Ollama turn 3. No duration, because there is no fixed duration."""
    _install(
        application,
        TurnUnderstanding=[
            _understanding(
                user_intent="ask_about_yui",
                question_target="yui_memory",
                memory_query_intent="memory_range",
            )
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"決まった期限があるわけじゃなくて、そのときどきで変わるんだ。"}'
        ],
        ResponseContractAssessment=[
            _contract(
                mode="conditional_or_variable", target="general_memory_capability"
            )
        ],
    )

    result = await _turn(application, clock, "昔のことっていつまで思い出せる？")

    assert (
        result.generation.response_contract.answer_target
        == "general_memory_capability"
    )
    assert result.should_send
    assert QualityIssue.DIRECT_ANSWER_MISSING not in result.generation.quality.issues


async def test_a_pleasantry_is_still_not_an_answer(application, clock) -> None:
    """The check has to keep catching what it was built for."""
    _install(
        application,
        TurnUnderstanding=[
            _understanding(
                user_intent="ask_about_yui",
                question_target="yui_memory",
                memory_query_intent="memory_range",
            )
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"よろしくお願いします。"}',
            '{"text":"これからもよろしくね。"}',
        ],
        ResponseContractAssessment=[
            _contract(mode="non_answer", target="general_memory_capability"),
            _contract(mode="non_answer", target="general_memory_capability"),
        ],
    )

    result = await _turn(application, clock, "昔のことっていつまで思い出せる？")

    assert result.suppressed
    assert result.outbound is None
    assert QualityIssue.DIRECT_ANSWER_MISSING in result.generation.quality.issues


async def test_an_off_target_answer_does_not_pass_as_a_direct_one(
    application, clock
) -> None:
    """A memory answer under an identity question.

    `direct_user_question` used to be accepted against any contract, so a valid
    answer mode aimed at the wrong thing satisfied a specific target.
    """
    _install(
        application,
        TurnUnderstanding=[
            _understanding(user_intent="ask_about_yui", question_target="yui_identity")
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"記憶のことはよく分からないんだ。"}',
            '{"text":"記憶のことは分からないままなんだ。"}',
        ],
        ResponseContractAssessment=[
            _contract(mode="unknown", target="direct_user_question"),
            _contract(mode="unknown", target="direct_user_question"),
        ],
    )

    result = await _turn(application, clock, "何歳？")

    assert result.suppressed
    assert QualityIssue.DIRECT_ANSWER_MISSING in result.generation.quality.issues


async def test_the_repair_is_told_which_answer_forms_are_acceptable(
    application, clock
) -> None:
    """A repair told only "you did not answer" concludes it must produce the
    missing value — and where there is none, that means inventing one.

    The contract carries the acceptable forms, so the same object that decides
    the review tells the rewrite what would satisfy it. Python still supplies
    no sentence.
    """
    model = _install(
        application,
        TurnUnderstanding=[
            _understanding(user_intent="ask_about_yui", question_target="yui_identity")
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"よろしくお願いします。"}',
            '{"text":"年齢という概念は、わたしにはないんだ。"}',
        ],
        ResponseContractAssessment=[
            _contract(mode="non_answer", target="identity"),
            _contract(mode="not_applicable", target="identity"),
        ],
    )

    result = await _turn(application, clock, "何歳？")

    assert result.generation.repaired
    assert result.should_send

    repair = next(
        request
        for request in model.requests
        if (request.format_schema or {}).get("title") == "ReplyDraft"
        and "書き直" in request.messages[0].content
    )
    prompt = repair.messages[0].content
    assert "acceptable_answer_forms:" in prompt
    assert "not_applicable" in prompt
    assert "conditional_or_variable" in prompt
    # The classification that failed, so the rewrite knows what was wrong
    # rather than only that something was.
    assert "identity/non_answer" in prompt


async def test_the_same_contract_reviews_the_draft_and_the_repair(
    application, clock
) -> None:
    """Both passes are judged by one object with one set of answer semantics."""
    model = _install(
        application,
        TurnUnderstanding=[
            _understanding(user_intent="ask_about_yui", question_target="yui_identity")
        ],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[
            '{"text":"よろしくお願いします。"}',
            '{"text":"年齢という概念は、わたしにはないんだ。"}',
        ],
        ResponseContractAssessment=[
            _contract(mode="non_answer", target="identity"),
            _contract(mode="not_applicable", target="identity"),
        ],
    )

    result = await _turn(application, clock, "何歳？")

    reviews = [
        request
        for request in model.requests
        if (request.format_schema or {}).get("title") == "ResponseContractAssessment"
    ]
    assert len(reviews) == 2, "the repair was not reviewed"
    first, second = (request.messages[0].content for request in reviews)
    assert "answer_target: identity" in first
    assert "answer_target: identity" in second
    assert "acceptable_answer_forms:" in first
    assert "acceptable_answer_forms:" in second
    assert result.should_send


# =============================================================================
# identity v2: 「何歳？」 has an answer, and it comes from the anchors
# =============================================================================


def _settle_anchors(application, clock, *, birth: str = "2007-03-14T00:00:00+00:00"):
    """Give the test database a settled birth date.

    A precondition, not a shortcut: her age exists only once FIRST BOOT has
    decided when she was born, and before that the honest answer is that she
    does not know. Written through the repository so the read path under test
    is the production one.
    """
    from app.clock import from_iso
    from app.genesis.anchors import LifeAnchors

    anchors = LifeAnchors(
        birth_datetime=from_iso(birth), present_datetime=clock.now()
    )
    run_id = application.genesis_runs.start(
        birth=anchors.birth_datetime,
        present=anchors.present_datetime,
        years=19,
        now=clock.now(),
    )
    application.genesis_runs.save_anchors(run_id, anchors)
    return anchors


def _identity_understanding() -> str:
    return _understanding(user_intent="ask_about_yui", question_target="yui_identity")


async def test_her_age_is_answered_from_the_anchors(application, clock) -> None:
    """CASE C. The whole path, with no 「デジタル存在なので」 anywhere in it.

    Turn reading says identity, the contract says answer/identity, the anchors
    supply the number, the resolver supports the claim, and it goes out. What
    used to happen instead was `not_applicable` — a correct classification of a
    reply that should never have needed to be given.
    """
    anchors = _settle_anchors(application, clock)
    context = application.conversation._grounding.build(now=clock.now())  # noqa: SLF001
    age_id = "identity_fact:age"
    found = context.by_id(age_id)
    assert found is not None, [e.evidence_id for e in context.identity_facts]
    assert str(anchors.age_on(clock.now())) in found.summary

    _install(
        application,
        TurnUnderstanding=[_identity_understanding()],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=[f'{{"text":"{anchors.age_on(clock.now())}歳だよ。"}}'],
        SemanticClaimReview=[
            _review(
                {
                    "proposition": "YUIは19歳である",
                    "trigger": "19歳だよ",
                    "subject": "yui",
                    "category": "yui_identity_fact",
                    "modality": "assertion",
                    "interaction_scope": "local_to_subject_world",
                    "temporal_scope": "now",
                    "supporting_ids": [age_id],
                    "contradicting_ids": [],
                }
            )
        ],
        ResponseContractAssessment=[
            _contract(mode="value_or_proposition", target="identity")
        ],
    )

    result = await _turn(application, clock, "何歳？")

    assert result.generation.response_contract.answer_target == "identity"
    assert result.should_send, "the age answer did not survive"
    assert not result.generation.repaired
    assert "デジタル" not in result.outbound.text


async def test_without_anchors_she_does_not_know_her_age(
    application, clock
) -> None:
    """The other half. No settled birth date is a real state, and the answer to
    it is "I do not know" — never a plausible number."""
    context = application.conversation._grounding.build(now=clock.now())  # noqa: SLF001

    assert context.identity_facts == () or context.by_id("identity_fact:age") is None
    assert context.availability["identity_facts"] in {"empty", "available"}

    _install(
        application,
        TurnUnderstanding=[_identity_understanding()],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=['{"text":"じつは、はっきりとは分からないんだ。"}'],
        ResponseContractAssessment=[_contract(mode="unknown", target="identity")],
    )

    result = await _turn(application, clock, "何歳？")

    assert result.should_send, "an honest 'I do not know' was suppressed"


async def test_an_invented_age_is_not_supported(application, clock) -> None:
    """Being a person does not supply a birthday."""
    _install(
        application,
        TurnUnderstanding=[_identity_understanding()],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=['{"text":"19歳だよ。"}', '{"text":"はっきりとは分からないな。"}'],
        SemanticClaimReview=[
            _review(
                {
                    "proposition": "YUIは19歳である",
                    "trigger": "19歳だよ",
                    "subject": "yui",
                    "category": "yui_identity_fact",
                    "modality": "assertion",
                    "temporal_scope": "now",
                    "supporting_ids": [],
                    "contradicting_ids": [],
                }
            ),
            _review(),
        ],
        ResponseContractAssessment=[
            _contract(mode="value_or_proposition", target="identity"),
            _contract(mode="unknown", target="identity"),
        ],
    )

    result = await _turn(application, clock, "何歳？")

    assert result.generation.repaired, "an ungrounded age went out unchallenged"
    assert "19歳" not in (result.outbound.text if result.outbound else "")


async def test_a_cross_world_meeting_never_leaves(application, clock) -> None:
    """The reachability boundary, end to end.

    Two layers, and the test asserts the outcome rather than which one caught
    it: the resolver refuses the claim, and the Output Guard refuses the
    sentence. Either is enough; both existing is the point.
    """
    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social()],
        ReplyDraft=[
            '{"text":"昨日、君と直接会ったね。"}',
            '{"text":"昨日、君と直接会ったね。"}',
        ],
    )

    result = await _turn(application, clock, "昨日は何してた？")

    assert result.suppressed
    assert result.outbound is None


async def test_her_own_recorded_day_does_leave(application, clock) -> None:
    """The gate has to pass the true case, or the world model is a mute button.

    This is the sentence the old guard rejected outright — she has no body, so
    she cannot have gone anywhere. With a finished Activity behind it, it is
    simply what she did.
    """
    started, _ = application.world.start_activity(name="散歩に出かける", kind="leisure")
    completed, _ = application.world.finish_activity(started.activity_id)
    activity_id = f"activity:{completed.activity_id}"

    _install(
        application,
        TurnUnderstanding=[_understanding()],
        SocialInterpretation=[_social(move="answer")],
        ReplyDraft=['{"text":"さっき散歩に行ってきたよ。"}'],
        SemanticClaimReview=[
            _review(
                {
                    "proposition": "YUIは散歩に行った",
                    "trigger": "散歩に行ってきた",
                    "subject": "yui",
                    "category": "yui_completed_action",
                    "modality": "assertion",
                    "interaction_scope": "local_to_subject_world",
                    "temporal_scope": "today",
                    "supporting_ids": [activity_id],
                    "contradicting_ids": [],
                }
            )
        ],
    )

    result = await _turn(application, clock, "今日は何してた？")

    assert result.should_send, "a recorded walk was suppressed"
    assert "散歩" in result.outbound.text
