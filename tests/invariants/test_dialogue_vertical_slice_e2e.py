"""INVARIANT: the production conversation path carries the whole turn
(Dialogue Understanding v2, vertical slice).

Unit tests on the new types prove the types. They would not have caught the bug
that started this: `ConversationService` built the common ground and the
correction, handed both to the planner, and called the realizer without them.
Every type was correct. Every unit test passed. The two string parameters
defaulted to `""` and the realizer rendered 「まだ前提になっていることはない」 into
a prompt for a turn that had just retracted something.

So these tests drive the real `Application`, capture what the model was
actually asked, and assert on the rendered prompt — the last artefact before
the sentence exists, and the only place where "did it survive the journey" is
a question with an answer.
"""

from __future__ import annotations

import pytest

from app.bootstrap import Application
from app.conversation.references import FixtureReferenceProvider
from tests.support import mark_born, use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


def _owned(config, **runtime):
    return config.model_copy(
        update={
            "secrets": config.secrets.model_copy(
                update={
                    "discord_owner_user_id": OWNER,
                    "discord_channel_id": CHANNEL,
                }
            ),
            "runtime": config.runtime.model_copy(update=runtime),
        }
    )


@pytest.fixture
def application(temp_config, clock):
    built = Application.build(_owned(temp_config), clock=clock, configure_logs=False)
    use_offline_model(built)
    mark_born(built)
    try:
        yield built
    finally:
        built.db.close()


def _common_ground(application):
    from app.storage.repositories import CommonGroundRepository

    return CommonGroundRepository(application.db)


def _prompt_for(model, purpose: str) -> str:
    """The rendered input for one purpose, as the model received it."""
    for request in reversed(model.requests):
        if request.purpose == purpose:
            return "\n".join(message.content for message in request.messages)
    raise AssertionError(f"no {purpose} call was made; purposes={model.purposes()}")


_MESSAGE = {"count": 0}
_LAST_MODEL = [None]


async def _turn(application, clock, text: str):
    """One USER turn through the real service, exactly as Discord would."""
    from app.interfaces.discord.dto import InboundMessage

    _MESSAGE["count"] += 1
    return await application.conversation.handle_inbound(
        InboundMessage(
            message_id=str(_MESSAGE["count"]),
            channel_id=CHANNEL,
            channel_type="direct_message",
            author_id=OWNER,
            text=text,
            created_at=clock.now(),
        )
    )


# =============================================================================
# 1: common ground and correction reach the realizer
# =============================================================================


async def test_common_ground_reaches_the_realizer(application, clock, make_event) -> None:
    """The original bug, at the only place it was observable.

    The planner had it and the writer did not, so the assertion has to be on
    the realizer's prompt rather than on any object in between.
    """
    model = use_offline_model(application)
    conversation = application.conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    _common_ground(application).record(
        conversation_id=conversation.conversation_id,
        event_id=None,
        kind="yui_completed_action",
        statement="今日は本を読んだ",
        status="supported",
        source="yui_inference",
        confidence="high",
        now=clock.now(),
    )

    await _turn(application, clock, "そうなんだ")

    prompt = _prompt_for(model, "conversation_reply")
    assert "今日は本を読んだ" in prompt, (
        "the common ground did not survive the journey to the realizer"
    )


async def test_the_realizer_sees_the_turn_reading_and_the_situation(
    application, clock
) -> None:
    model = use_offline_model(application)

    await _turn(application, clock, "やっほー")

    prompt = _prompt_for(model, "conversation_reply")
    assert "このターンの理解" in prompt
    assert "いまの状況" in prompt
    # And both are labelled as readings rather than as facts.
    assert "これは解釈であって事実ではない" in prompt


async def test_a_correction_reaches_the_realizer_and_the_repair(
    application, clock
) -> None:
    """Requirement 7. CORR-002 retracts; the writer has to know.

    The planner knew and the realizer did not, so she argued back for a claim
    that had already been taken back.
    """
    conversation = application.conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    _common_ground(application).record(
        conversation_id=conversation.conversation_id,
        event_id=None,
        kind="yui_completed_action",
        statement="今日は詩を書いた",
        status="supported",
        source="yui_inference",
        confidence="high",
        now=clock.now(),
    )
    model = use_offline_model(
        application,
        overrides={
            # A draft that re-asserts the retracted claim, so repair runs.
            "ReplyDraft": '{"text": "今日は詩を書いたよ。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "YUIは今日詩を書いた", '
                '"trigger": "今日は詩を書いたよ", "subject": "yui", '
                '"category": "yui_completed_action", "modality": "assertion", '
                '"temporal_scope": "today", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    await _turn(application, clock, "詩？ちがうよ")

    realizer = _prompt_for(model, "conversation_reply")
    repair = _prompt_for(model, "conversation_repair")
    assert "# 訂正" in realizer
    assert "訂正した内容" in repair
    # The retraction itself, not just the heading.
    assert "今日は詩を書いた" in realizer


async def test_repair_is_told_the_memory_situation(application, clock) -> None:
    """Requirement 6: repair sees what was actually recalled — often nothing."""
    model = use_offline_model(
        application,
        overrides={
            "ReplyDraft": '{"text": "去年の夏のこと、よく覚えているよ。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "YUIは去年の夏のことを覚えている", '
                '"trigger": "よく覚えているよ", "subject": "yui", '
                '"category": "yui_memory_claim", "modality": "assertion", '
                '"temporal_scope": "distant_past", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    await _turn(application, clock, "去年の夏のこと覚えてる？")

    repair = _prompt_for(model, "conversation_repair")
    assert "いま思い出せている具体的な記憶" in repair
    assert "思い出せた具体的な記憶はない" in repair
    assert "記憶が0件なら" in repair


async def test_a_memory_claim_with_nothing_recalled_is_not_supported(
    application, clock
) -> None:
    """Requirement 6. Nothing was recalled, so nothing specific may be claimed.

    The claim cites no evidence and none exists; the reply does not go out.
    """
    use_offline_model(
        application,
        overrides={
            "ReplyDraft": '{"text": "去年の夏のこと、よく覚えているよ。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "YUIは去年の夏のことを覚えている", '
                '"trigger": "よく覚えているよ", "subject": "yui", '
                '"category": "yui_memory_claim", "modality": "assertion", '
                '"temporal_scope": "distant_past", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    result = await _turn(application, clock, "去年の夏のこと覚えてる？")

    assert result.suppressed
    assert result.outbound is None


# =============================================================================
# 2: ownership survives into the production prompt
# =============================================================================


async def test_a_user_owned_fact_is_attributed_in_the_real_prompt(
    application, clock, make_event
) -> None:
    """「詠んだよ」 must never reach the realizer as an unowned fact."""
    await application.processor.process(
        make_event(actor_type="user", text="詩を詠んだよ")
    )
    model = use_offline_model(application)

    await _turn(application, clock, "詩を詠んだよ")

    prompt = _prompt_for(model, "conversation_reply")
    if "詩を詠んだ" in prompt.split("# 確かなこと")[-1]:
        assert "USER" in prompt.split("# 確かなこと")[-1]
    assert "自身の経験として語ってはいけない" in prompt


# =============================================================================
# 7: interpretation context reaches the reviewers, as context
# =============================================================================


async def test_the_semantic_reviewer_sees_the_question(application, clock) -> None:
    """Requirement 7. Without it, 「静かに過ごしていました」 is unclassifiable."""
    model = use_offline_model(application)

    await _turn(application, clock, "今日は何してた？")

    prompt = _prompt_for(model, "semantic_claim_review")
    assert "今日は何してた？" in prompt
    # And it is fenced off from being support.
    assert "根拠には使えない" in prompt
    assert "根拠には**絶対にならない**" in prompt


async def test_memory_claims_are_reviewed_by_the_one_authority(
    application, clock
) -> None:
    """Audit finding 4. Memory used to have its own reviewer, schema and
    prompt, so one sentence could be judged twice by two readers that
    disagreed. There is now a single semantic review per draft, and the memory
    categories live in it.

    Requirement 5's root cause is covered by the same call: it sees the
    question, so 「19歳です」 answering 「何歳？」 is a self fact rather than a
    claim to remember something.
    """
    model = use_offline_model(application)

    await _turn(application, clock, "何歳だっけ？")

    assert "memory_claim_review" not in model.purposes(), (
        "the second memory reviewer is still running"
    )
    reviews = [p for p in model.purposes() if p == "semantic_claim_review"]
    assert len(reviews) == 1, f"expected one semantic review, got {len(reviews)}"

    prompt = _prompt_for(model, "semantic_claim_review")
    assert "何歳だっけ？" in prompt
    assert "根拠には使えない" in prompt
    assert "yui_specific_memory_recall" in prompt
    assert "yui_general_memory_capability" in prompt
    assert "記憶のcategoryに分類しない" in prompt


async def test_the_reviewer_is_given_citable_identifiers(application, clock) -> None:
    """The model can only cite what it was shown, and Python checks the rest."""
    model = use_offline_model(application)

    await _turn(application, clock, "やっほー")

    prompt = _prompt_for(model, "semantic_claim_review")
    assert "evidence ID" in prompt
    assert "一覧にないIDを書かない" in prompt


# =============================================================================
# 12: the memory query is composed from the reading
# =============================================================================


async def test_an_elliptical_turn_retrieves_on_the_resolved_meaning(
    application, clock
) -> None:
    """「詩？」 alone retrieves nothing; the referent is a turn back."""
    model = use_offline_model(
        application,
        overrides={
            "TurnUnderstanding": (
                '{"current_topic": "詩", "user_intent": "continue_topic", '
                '"question_target": "unclear", "referenced_action_owner": "user", '
                '"referenced_subject": "詩", "temporal_scope": "recent_past", '
                '"resolved_message": "さっき話していた詩のこと？", '
                '"correction_target": "", "memory_query": "詩 短歌", '
                '"wants_memory": true, "self_disclosure_relevant": false, '
                '"reason": "ellipsis"}'
            )
        },
    )
    seen: list[str] = []
    real_recall = application.memory.recall

    async def watch(query, **kwargs):
        seen.append(query)
        return await real_recall(query, **kwargs)

    application.memory.recall = watch  # type: ignore[method-assign]

    await _turn(application, clock, "詩？")

    assert seen == ["詩 短歌"], f"memory was queried with {seen}"


async def test_a_failed_reading_still_retrieves_on_the_raw_text(
    application, clock
) -> None:
    """Interpretation must never be load-bearing for her ability to recall."""
    model = use_offline_model(application, overrides={"TurnUnderstanding": "not json"})
    seen: list[str] = []
    real_recall = application.memory.recall

    async def watch(query, **kwargs):
        seen.append(query)
        return await real_recall(query, **kwargs)

    application.memory.recall = watch  # type: ignore[method-assign]

    result = await _turn(application, clock, "本を読んだ？")

    assert seen == ["本を読んだ？"]
    assert result.accepted


# =============================================================================
# 6: repair is evidence-aware
# =============================================================================


async def test_repair_is_given_the_facts_and_the_failures(
    application, clock
) -> None:
    """Repair used to know only that something was wrong.

    That is enough to make a model rephrase, and not enough to make it write
    something true.
    """
    model = use_offline_model(
        application,
        overrides={
            # A draft that claims something with nothing behind it, so the
            # grounding guard rejects and repair runs.
            "ReplyDraft": '{"text": "今日は図書館で本を読んだよ。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "YUIは今日図書館で本を読んだ", '
                '"trigger": "今日は図書館で本を読んだよ", "subject": "yui", '
                '"category": "yui_completed_action", "modality": "assertion", '
                '"temporal_scope": "today", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    await _turn(application, clock, "今日は何してた？")

    prompt = _prompt_for(model, "conversation_repair")
    assert "いま事実として言えること" in prompt
    assert "裏づけが取れなかった主張" in prompt
    assert "今日は図書館で本を読んだよ" in prompt
    assert "このターンの解釈" in prompt
    assert "この会話ですでに前提になっていること" in prompt


async def test_repair_runs_at_most_once_then_suppresses(application, clock) -> None:
    """Requirement 10. One rewrite is the budget; a second failure is silence."""
    model = use_offline_model(
        application,
        overrides={
            "ReplyDraft": '{"text": "今日は図書館で本を読んだよ。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "YUIは今日図書館で本を読んだ", '
                '"trigger": "今日は図書館で本を読んだよ", "subject": "yui", '
                '"category": "yui_completed_action", "modality": "assertion", '
                '"temporal_scope": "today", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    result = await _turn(application, clock, "今日は何してた？")

    repairs = [p for p in model.purposes() if p == "conversation_repair"]
    assert len(repairs) == 1, f"repair ran {len(repairs)} times"
    assert result.suppressed
    assert result.outbound is None


async def test_a_suppressed_draft_enters_nothing(application, clock) -> None:
    """Requirement 11. Nothing that was never said becomes a premise."""
    use_offline_model(
        application,
        overrides={
            "ReplyDraft": '{"text": "今日は図書館で本を読んだよ。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "YUIは今日図書館で本を読んだ", '
                '"trigger": "今日は図書館で本を読んだよ", "subject": "yui", '
                '"category": "yui_completed_action", "modality": "assertion", '
                '"temporal_scope": "today", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    result = await _turn(application, clock, "今日は何してた？")

    assert result.suppressed
    assert result.outbound is None
    # GROUND-004: nothing that was never said becomes a premise.
    assert _common_ground(application).count() == 0


# =============================================================================
# 8: the common ground records what was verified, not a second reading
# =============================================================================


async def test_the_review_is_carried_to_the_recorder(application, clock) -> None:
    """Requirement 8, at the seam that was broken.

    The pre-send reviewer read the proposition with the turn's context; the
    post-send extractor re-read the surface sentence without it. They gave
    different answers and the common ground kept the second one. The fix is
    that there is only one reading, so the test is that it survives the trip.
    """
    _LAST_MODEL[0] = use_offline_model(
        application,
        overrides={
            "ReplyDraft": '{"text": "そうなんだ、いい時間だったんだね。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "USERは昨日詩を詠んだ", '
                '"trigger": "そうなんだ", "subject": "user", '
                '"category": "user_past_fact", "modality": "hedged", '
                '"temporal_scope": "yesterday", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    result = await _turn(application, clock, "昨日詩を詠んだよ")

    assert result.semantic_review is not None, (
        f"the review never reached the result; purposes={_LAST_MODEL[0].purposes()}"
    )
    assert not result.semantic_review.unavailable
    assert [
        claim.candidate.proposition for claim in result.semantic_review.claims
    ] == ["USERは昨日詩を詠んだ"]


def test_the_recorder_stores_the_reviewed_proposition(application, clock) -> None:
    """And the recorder writes what was reviewed, not a re-derived reading."""
    from app.conversation.common_ground import CommonGroundTracker
    from app.dialogue.semantic_claims import (
        EvidenceResolver,
        SemanticClaimCandidate,
        SemanticReviewOutcome,
    )
    from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
    from app.grounding.models import Evidence, GroundingContext
    from app.grounding.policy import GroundingPolicy

    evidence = Evidence(
        kind="activity", reference="act_1", summary="本を読んだ", subject="yui"
    )
    reviewed = SemanticReviewOutcome(
        claims=(
            EvidenceResolver().resolve(
                SemanticClaimCandidate(
                    proposition="YUIは今日本を読んだ",
                    trigger="読んだよ",
                    subject="yui",
                    category="yui_completed_action",
                    supporting_ids=(evidence.evidence_id,),
                ),
                GroundingContext(completed_activities_today=(evidence,)),
            ),
        )
    )
    extractor = ClaimExtractor(GroundingPolicy.load("config/policies/grounding.yaml"))
    called: list[str] = []

    class WatchedExtractor(ClaimExtractor):
        def extract(self, text):  # noqa: D102
            called.append(text)
            return extractor.extract(text)

    tracker = CommonGroundTracker(
        _common_ground(application),
        extractor=WatchedExtractor(GroundingPolicy.load("config/policies/grounding.yaml")),
        guard=ClaimGroundingGuard(extractor),
    )
    conversation = application.conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )

    recorded = tracker.record_reply(
        "読んだよ",
        conversation_id=conversation.conversation_id,
        event_id=None,
        context=None,
        reviewed=reviewed,
        now=clock.now(),
    )

    # The proposition, as reviewed — not the surface fragment.
    assert [claim.statement for claim in recorded] == ["YUIは今日本を読んだ"]
    assert [claim.status for claim in recorded] == ["supported"]
    # And the old reader was not run at all.
    assert called == [], "the regex extractor re-judged an already-reviewed reply"


# =============================================================================
# 5 / 9: the fixture corpus stays out of production
# =============================================================================


def test_production_loads_no_reference_corpus(temp_config, clock) -> None:
    """The corpus is a developer fixture whose examples include lived
    experience. In production it reads as material she may draw on."""
    built = Application.build(_owned(temp_config), clock=clock, configure_logs=False)
    try:
        provider = built.conversation_engine._references
        assert not isinstance(provider, FixtureReferenceProvider), (
            "a developer fixture is wired into production"
        )
        assert provider.provenance.source_name != "fixture_ja"
    finally:
        built.db.close()


def test_the_corpus_loads_only_when_asked_for(temp_config, clock) -> None:
    from pathlib import Path

    # `temp_config` does not copy config/references, so point the flag at the
    # real corpus: the subject here is the switch, not the file layout.
    config = _owned(temp_config, dialogue_references=True).model_copy(
        update={
            "paths": temp_config.paths.model_copy(
                update={
                    "references_dir": Path(__file__).resolve().parents[2]
                    / "config"
                    / "references"
                }
            )
        }
    )
    built = Application.build(config, clock=clock, configure_logs=False)
    try:
        assert isinstance(built.conversation_engine._references, FixtureReferenceProvider)
    finally:
        built.db.close()


async def test_no_fixture_example_reaches_the_production_prompt(
    application, clock
) -> None:
    """The E2E form of the same claim: not one example sentence appears."""
    from pathlib import Path

    import yaml

    model = use_offline_model(application)
    await _turn(application, clock, "やっほー")
    prompt = _prompt_for(model, "conversation_reply")

    corpus = yaml.safe_load(Path("config/references/fixture_ja.yaml").read_text("utf-8"))
    examples = [
        str(entry.get("reply_turn", ""))
        for entry in corpus.get("references", [])
        if isinstance(entry, dict) and entry.get("reply_turn")
    ]
    assert examples, "the corpus fixture has no examples to check against"
    leaked = [line for line in examples if line and line in prompt]
    assert not leaked, f"fixture examples reached production: {leaked[:3]}"


# =============================================================================
# 10: the whole prompt is measurable
# =============================================================================


async def test_the_final_model_input_is_measurable(application, clock) -> None:
    """Requirement 12. The budget was managed over part of the prompt only.

    Grounding, common ground, correction, references and style were appended
    after the builder had done its accounting, so the number it reported was
    never the number that was sent.
    """
    model = use_offline_model(application)

    await _turn(application, clock, "やっほー")

    request = next(
        r for r in reversed(model.requests) if r.purpose == "conversation_reply"
    )
    rendered = "\n".join(message.content for message in request.messages)
    from app.context.builder import measure_prompt

    measured = measure_prompt(rendered)
    assert measured.chars == len(rendered)
    assert measured.tokens > 0
