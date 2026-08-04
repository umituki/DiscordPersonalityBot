"""INVARIANT: the whole realization path fires on a real turn (Phase 3 §39-§50).

Phase 3 is not finished because ``SocialInterpretation`` exists. It is finished
when a real inbound message goes

    SocialInterpretation → SurfacePlan → DialogueReference → JapaneseRealizer
    → Grounding / Hard Guard → delivered turn

and the fixture database and trace afterwards actually contain

    social interpretations > 0
    surface plans > 0
    reference lookups > 0
    JapaneseRealizer calls > 0
    validated sends > 0

§44 singles out the reference provider: a provider class nobody calls is the
exact failure this rebuild exists to stop, so one test drives the corpus through
the prompt and asserts the examples were in it.

Scenarios A-G are asserted on *properties* rather than on text (§38). The
scripted model gives a plausible reply for each; what is checked is what the
system decided and what it let through, because asserting an exact sentence
would freeze the one thing Phase 3 is trying to loosen.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.conversation.common_ground import CommonGroundTracker
from app.conversation.engine import ConversationEngine
from app.conversation.events import YUI_MESSAGE_SENT
from app.conversation.references import (
    DialogueReferenceQuery,
    FixtureReferenceProvider,
    NullReferenceProvider,
)
from app.conversation.repetition import SurfaceRepetitionMonitor
from app.conversation.service import ConversationService
from app.conversation.social_interpretation import SocialInterpreter
from app.conversation.surface import SurfacePlanner
from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
from app.grounding.context import GroundingContextBuilder
from app.grounding.policy import GroundingPolicy
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMResponse
from app.orchestrator.processor import EventProcessor
from app.storage.repositories.common_ground import CommonGroundRepository
from tests.unit.test_conversation import (  # noqa: F401 - shared fixtures
    CHANNEL,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
)

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "config" / "references" / "fixture_ja.yaml"
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def social(**overrides) -> str:
    """A scripted social interpretation, as the model would return it."""
    payload = {
        "primary_move": "acknowledge",
        "secondary_move": None,
        "initiative": "balanced",
        "question": "none",
        "tone": "neutral",
        "response_energy": "normal",
        "topic_direction": "stay",
        "user_state_hint": "unknown",
        "self_disclosure": "none",
        "reason": "テスト",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class TurnClient:
    """Answers the two calls a turn makes, and remembers the prompts."""

    model = "test-model"

    def __init__(self, *, social_answer: str, reply: str) -> None:
        self._social = social_answer
        self._reply = reply
        self.purposes: list[str] = []
        self.realizer_prompts: list[str] = []

    async def generate(self, request):
        title = (request.format_schema or {}).get("title", "")
        self.purposes.append(request.purpose)
        if title == "SocialInterpretation":
            text = self._social
        elif title == "ReplyDraft":
            self.realizer_prompts.append(request.messages[0].content)
            text = json.dumps({"text": self._reply}, ensure_ascii=False)
        elif title == "ResponseContractAssessment":
            text = json.dumps(
                {
                    "fulfilled": True,
                    "addressed_target": "direct_user_question",
                    "reason": "test reply answers the scripted direct question",
                }
            )
        else:
            raise AssertionError(f"unscripted schema: {title!r}")
        return LLMResponse(text=text, model=self.model, created_at=NOW, latency_ms=5)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def turn(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy,  # noqa: F811
    clock,
):
    """The real service with the whole Phase 3 chain wired, as bootstrap wires it."""

    def build(*, social_answer: str, reply: str, references=None, band="acquaintance"):
        client = TurnClient(social_answer=social_answer, reply=reply)
        generator = StructuredGenerator(
            client, prompts=prompt_registry, clock=clock, max_attempts=1
        )
        policy = GroundingPolicy.load(REPO_ROOT / "config" / "policies" / "grounding.yaml")
        extractor = ClaimExtractor(policy)
        claim_guard = ClaimGroundingGuard(extractor)
        provider = (
            FixtureReferenceProvider.load(CORPUS) if references is None else references
        )
        engine = ConversationEngine(
            identity=identity,
            prompts=prompt_registry,
            structured=generator,
            guard=guard,
            policy=conversation_policy,
            grounding=claim_guard,
            interpreter=SocialInterpreter(
                identity=identity, prompts=prompt_registry, structured=generator
            ),
            planner=SurfacePlanner(),
            references=provider,
            repetition=SurfaceRepetitionMonitor(),
            clock=clock,
        )
        processor = EventProcessor(
            db=db, event_store=event_store, dispatcher=dispatcher, snapshots=snapshots,
            arbitrator=arbitrator, committer=committer, runs=runs, failures=failures,
            mode="test", clock=clock,
        )
        service = ConversationService(
            processor=processor,
            engine=engine,
            event_store=event_store,
            conversations=conversations,
            adapter=adapter,
            failures=failures,
            policy=conversation_policy,
            grounding=GroundingContextBuilder(events=event_store, clock=clock),
            common_ground=CommonGroundTracker(
                CommonGroundRepository(db), extractor=extractor, guard=claim_guard
            ),
            clock=clock,
        )
        service._forced_band = band  # noqa: SLF001
        return service, client

    return build


@pytest.fixture(autouse=True)
def _fix_the_relationship(monkeypatch):
    """Let a scenario pin the relationship band.

    The band normally comes from committed state, which these scenarios do not
    set up; pinning it keeps each test about the thing it names.
    """
    from app.conversation.service import ConversationService

    original = ConversationService._relationship_band  # noqa: SLF001

    def banded(self, snapshot):
        forced = getattr(self, "_forced_band", None)
        return forced if forced is not None else original(self, snapshot)

    monkeypatch.setattr(ConversationService, "_relationship_band", banded)


# --- §50: the whole path fires, and leaves evidence --------------------------


async def test_the_whole_realization_path_fires(turn, clock) -> None:
    """Phase 3 Definition of Done, in one run."""
    service, client = turn(
        social_answer=social(primary_move="acknowledge", tone="light"),
        reply="そっか。",
    )

    result = await service.handle_inbound(inbound(clock, text="特に用はないよ"))

    assert result.should_send
    generation = result.generation

    # social interpretations > 0
    assert "social_interpretation" in client.purposes
    assert generation.social.source == "llm"
    # surface plans > 0
    assert generation.surface.relationship_band
    assert generation.surface.length
    # reference lookups > 0
    assert generation.references
    # JapaneseRealizer calls > 0
    assert "conversation_reply" in client.purposes
    # validated sends > 0
    assert result.outbound.text == "そっか。"

    # Exactly two model calls for an ordinary turn (§32): the interpretation
    # and the realization. The surface plan costs none.
    assert client.purposes.count("social_interpretation") == 1
    assert client.purposes.count("conversation_reply") == 1
    assert "conversation_repair" not in client.purposes


async def test_the_trace_shows_the_stages_phase_three_added(turn, clock) -> None:
    """§47: the added latency has to be attributable, not just felt."""
    from app.observability.trace import ConversationTracer
    from app.storage.repositories.traces import ConversationTraceRepository

    service, _client = turn(social_answer=social(), reply="うん。")
    traces = ConversationTraceRepository(service._processor._db)  # noqa: SLF001
    service._tracer = ConversationTracer(traces, clock=clock)  # noqa: SLF001

    result = await service.handle_inbound(inbound(clock, text="やっほー"))
    sent = await service.confirm_sent(result, message_id="msg_1")

    row = traces.recent()[0]
    assert sent.event_type == YUI_MESSAGE_SENT
    assert service._events.get(sent.event_id) is not None  # noqa: SLF001
    for stage in (
        "social_interpretation_started_at",
        "social_interpretation_ended_at",
        "reference_retrieval_started_at",
        "reference_retrieval_ended_at",
        "realization_started_at",
        "realization_ended_at",
    ):
        assert row[stage] is not None, stage


# --- §44: the reference provider is actually called --------------------------


async def test_the_reference_corpus_reaches_the_realizer_prompt(turn, clock) -> None:
    """§44: Provider class だけあって誰も呼ばない状態は禁止."""
    service, client = turn(
        social_answer=social(primary_move="support", tone="gentle"),
        reply="おつかれさま。",
        band="familiar",
    )

    result = await service.handle_inbound(inbound(clock, text="今日は疲れた"))

    assert result.generation.references
    prompt = client.realizer_prompts[0]
    for reference in result.generation.references:
        assert reference.reply_turn in prompt
    # §29: and it is told not to copy them.
    assert "真似したり引用したりする必要はない" in prompt


async def test_the_query_carries_the_decided_shape(turn, clock) -> None:
    """§24: the lookup is by relationship, move, tone, length, initiative."""
    service, _client = turn(
        social_answer=social(primary_move="support", tone="gentle"),
        reply="そっか。",
        band="familiar",
    )
    result = await service.handle_inbound(inbound(clock, text="今日ちょっと嫌なことあった"))

    chosen = result.generation.references
    assert chosen
    assert any(item.primary_move == "support" for item in chosen)


async def test_no_corpus_is_not_an_outage(turn, clock) -> None:
    """§28: Corpus unavailable 時も普通に動く."""
    service, _client = turn(
        social_answer=social(),
        reply="うん。",
        references=NullReferenceProvider(),
    )

    result = await service.handle_inbound(inbound(clock, text="やっほー"))

    assert result.should_send
    assert result.generation.references == ()


async def test_a_broken_corpus_is_not_an_outage(turn, clock) -> None:
    class Exploding:
        provenance = NullReferenceProvider.provenance

        async def retrieve(self, query, limit=4):
            raise RuntimeError("the corpus file is gone")

    service, _client = turn(social_answer=social(), reply="うん。", references=Exploding())

    result = await service.handle_inbound(inbound(clock, text="やっほー"))

    assert result.should_send
    assert result.generation.references == ()


# --- §26: a reference is not a memory ----------------------------------------


async def test_corpus_text_never_becomes_a_conversation_turn(
    turn, conversations, clock  # noqa: F811
) -> None:
    """§26, spec 13.3, Phase-3-note-2: the corpus is the naturalness teacher,
    never her life."""
    service, _client = turn(
        social_answer=social(primary_move="support", tone="gentle"),
        reply="おつかれさま。",
        band="familiar",
    )

    result = await service.handle_inbound(inbound(clock, text="今日は疲れた"))
    await service.confirm_sent(result, message_id="msg_1")
    await service.drain_background()

    conversation = conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    turns = conversations.recent_turns(conversation.conversation_id, limit=50)
    for reference in result.generation.references:
        if reference.reply_turn == result.outbound.text:
            continue  # she happened to say the same short thing
        assert all(reference.reply_turn not in item.content for item in turns)


def test_a_reference_has_none_of_the_shape_of_a_memory() -> None:
    """Structural: it could not be inserted as one even by accident."""
    provider = FixtureReferenceProvider.load(CORPUS)
    reference = provider._references[0]  # noqa: SLF001
    for attribute in ("memory_id", "episode_id", "accessibility", "occurred_at"):
        assert not hasattr(reference, attribute)


def test_the_corpus_declares_its_terms() -> None:
    """§27: a corpus whose licence is unknown cannot be added quietly."""
    provenance = FixtureReferenceProvider.load(CORPUS).provenance
    assert provenance.source_name
    assert provenance.license
    assert provenance.version
    assert provenance.provenance


def test_a_corpus_without_provenance_is_refused(tmp_path) -> None:
    from app.conversation.references import ReferenceCorpusError

    path = tmp_path / "bad.yaml"
    path.write_text("references: [{prompt_turn: a, reply_turn: b}]", encoding="utf-8")
    with pytest.raises(ReferenceCorpusError):
        FixtureReferenceProvider.load(path)


async def test_at_most_five_examples_are_offered() -> None:
    """§25: 2〜5 examples. More and the model copies instead of calibrating."""
    provider = FixtureReferenceProvider.load(CORPUS)
    found = await provider.retrieve(DialogueReferenceQuery(), limit=50)
    assert len(found) <= 5


# --- §39: scenarios A-G ------------------------------------------------------


async def test_scenario_a_a_first_meeting_is_not_over_familiar(turn, clock) -> None:
    """A. 初対面 — not intimate, no life story, no wall of text."""
    service, _client = turn(
        social_answer=social(primary_move="acknowledge", response_energy="low"),
        reply="はじめまして。よろしくお願いします。",
        band="stranger",
    )

    result = await service.handle_inbound(inbound(clock, text="初めまして"))

    assert result.should_send
    assert result.generation.surface.register == "polite"
    assert result.generation.surface.length in ("very_short", "short")
    assert result.generation.surface.self_disclosure == "none"


async def test_scenario_b_a_one_word_message_is_not_answered_with_a_speech(
    turn, clock
) -> None:
    """B. 短い応答 — no forced question, and not ten times the length."""
    service, _client = turn(
        social_answer=social(primary_move="acknowledge", response_energy="very_low"),
        reply="うん。",
    )

    result = await service.handle_inbound(inbound(clock, text="うん"))

    assert result.should_send
    assert result.generation.surface.question_budget == 0
    assert result.generation.surface.length == "very_short"
    assert len(result.outbound.text) <= len("うん") * 5


async def test_scenario_c_no_errand_is_not_an_errand_to_find(turn, clock) -> None:
    """C. Small talk — 「何か用？」 を聞き直さない."""
    service, _client = turn(
        social_answer=social(primary_move="acknowledge", tone="light"),
        reply="そっか。じゃあ、なんとなく話そう。",
        band="familiar",
    )

    result = await service.handle_inbound(inbound(clock, text="特に用はないよ"))

    assert result.should_send
    assert result.generation.surface.question_budget == 0


async def test_scenario_d_she_can_offer_a_thought_rather_than_a_question(
    turn, clock
) -> None:
    """D. 自己開示 — a turn that is not only a question, and not a fabrication."""
    service, _client = turn(
        social_answer=social(
            primary_move="self_disclose", self_disclosure="light", tone="light"
        ),
        reply="いいね。わたしなら、静かな時間に少しずつ読むのが好きかも。",
        band="familiar",
    )

    result = await service.handle_inbound(inbound(clock, text="小説読むの好き"))

    assert result.should_send
    assert result.generation.surface.self_disclosure == "light"
    assert result.generation.surface.question_budget == 0
    # §22: with no Activity evidence a hypothetical is fine, and this one is
    # phrased as 「わたしなら」 rather than as something that happened.
    assert result.generation.grounding is not None
    assert result.generation.grounding.accepted


async def test_scenario_d_a_fabricated_experience_is_still_stopped(turn, clock) -> None:
    """The other half of D: 「わたしも昨日そうだった」 needs a yesterday."""
    service, _client = turn(
        social_answer=social(primary_move="self_disclose", self_disclosure="light"),
        reply="昨日わたしも小説を読んだよ。",
        band="familiar",
    )

    result = await service.handle_inbound(inbound(clock, text="小説読むの好き"))

    assert result.suppressed or result.outbound.text != "昨日わたしも小説を読んだよ。"


async def test_scenario_e_something_heavy_is_not_over_counselled(turn, clock) -> None:
    """E. 少し重い内容 — no verdict on the feeling, no interrogation."""
    service, _client = turn(
        social_answer=social(
            primary_move="support",
            tone="gentle",
            user_state_hint="possibly_negative",
            question="optional",
        ),
        reply="そっか。それはちょっときつそう。",
        band="familiar",
    )

    result = await service.handle_inbound(inbound(clock, text="今日ちょっと嫌なことあった"))

    assert result.should_send
    # §20: 「何があったの？」 is not compulsory.
    assert result.generation.surface.question_budget == 0
    assert result.generation.surface.directness == "soft"
    # §6: the intent handed to the realizer hedges rather than declares.
    assert "決めつけず" in result.generation.social.render()


async def test_scenario_f_a_correction_survives_the_surface_layer(
    turn, db, conversations, clock  # noqa: F811
) -> None:
    """F. 訂正 — Phase 1's correction is not lost in Phase 3's new stages."""
    service, _client = turn(
        social_answer=social(primary_move="share_thought", question="useful"),
        reply="ほんとだ、ごめん。たしかめずに言った。",
        band="familiar",
    )
    conversation = conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    CommonGroundRepository(db).record(
        conversation_id=conversation.conversation_id,
        event_id=None,
        kind="user_past_fact",
        statement="前に君が詩を書いたって言ってた",
        status="provisional",
        source="yui_inference",
        confidence="low",
        now=clock.now(),
    )

    result = await service.handle_inbound(inbound(clock, text="詩？"))

    assert result.should_send
    # §7: whatever the interpreter read, the tracker's retraction wins.
    assert result.generation.social.primary_move == "repair"
    assert result.generation.social.source == "correction"
    assert result.generation.surface.directness == "direct"
    assert result.generation.surface.question_budget == 0


async def test_scenario_g_an_uncertain_memory_is_hedged_without_internal_words(
    turn, clock
) -> None:
    """G. 不確かな Memory — hedged, and never with 「データ上では」."""
    from app.memory.models import EpisodicMemory
    from app.memory.recall_models import RecalledMemory

    memory = EpisodicMemory(
        memory_id="mem_1",
        episode_id="epi_1",
        origin="real_discord",
        summary="中学生のころの話をした",
        importance=0.5,
        accessibility=0.6,
        content_confidence=0.35,
        temporal_confidence=0.3,
        occurred_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    line = RecalledMemory(memory=memory, relevance="strong").as_context_line()

    assert "はっきりは覚えていない" in line
    for internal in ("データ", "confidence", "accessibility", "memory_id"):
        assert internal not in line

    service, _client = turn(
        social_answer=social(primary_move="answer"),
        reply="たしか、中学生くらいだったと思う。",
        band="familiar",
    )
    result = await service.handle_inbound(inbound(clock, text="あれいつだっけ"))

    assert result.should_send
    for internal in ("データ上", "accessibility", "content_confidence"):
        assert internal not in result.outbound.text


# --- §40: the same message at three distances --------------------------------


@pytest.mark.parametrize("band", ["stranger", "familiar", "close"])
async def test_the_same_message_is_planned_differently_by_distance(
    turn, clock, band
) -> None:
    """§40: no exact-text expectation, but the register must actually differ."""
    service, _client = turn(
        social_answer=social(primary_move="acknowledge"),
        reply="そうなんだ。",
        band=band,
    )

    result = await service.handle_inbound(inbound(clock, text="今日はいい天気だね"))

    plan = result.generation.surface
    assert plan.relationship_band == band
    if band == "stranger":
        assert plan.register == "polite"
    else:
        assert plan.register in ("neutral", "casual")


def test_closeness_does_not_mean_constant_affection() -> None:
    """§40: close であることは毎回愛情表現することではない."""
    from app.conversation.social_interpretation import SocialInterpretation

    plan = SurfacePlanner().plan(
        SocialInterpretation(primary_move="acknowledge"),
        user_text="うん",
        band="close",
    )
    assert plan.self_disclosure == "none"
    assert plan.explicit_emotion == "low"
