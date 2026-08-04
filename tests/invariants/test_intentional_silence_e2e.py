"""INVARIANT: choosing not to speak runs the whole path (Phase 4).

Trigger → runtime entry → decision → hard gate → actual action → event →
persistence → recovery → debug, for the case where the actual action is *not
sending anything*.

The half of this that is easy to get wrong is the negative space. A silence has
to leave: an event of its own type, no conversation turn, no failure record, no
typing indicator, and a trace that says she chose it. If any of those is missing
or borrowed from the suppression path, then "she is quiet" and "she is broken"
become the same observation, and nobody can debug either.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.conversation.engine import ConversationEngine
from app.conversation.events import YUI_INTENTIONAL_SILENCE, YUI_MESSAGE_SENT
from app.conversation.response_intent import ResponseIntent
from app.conversation.service import ConversationService
from app.conversation.social_interpretation import SocialInterpreter
from app.interfaces.discord.gateway import DiscordGateway
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMResponse
from app.observability.trace import ConversationTracer
from app.orchestrator.processor import EventProcessor
from app.storage.repositories.traces import ConversationTraceRepository
from tests.unit.test_conversation import (  # noqa: F401 - shared fixtures
    CHANNEL,
    adapter,
    conversation_policy,
    conversations,
    guard,
    identity,
    inbound,
)
from tests.unit.test_discord_gateway import (  # noqa: F401 - shared fixtures
    FakeAuthor,
    FakeMessage,
    TypingChannel,
)
from tests.support import PassingContractReviewer

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def social(**overrides) -> str:
    payload = {
        "primary_move": "acknowledge",
        "secondary_move": None,
        "initiative": "low",
        "question": "none",
        "tone": "light",
        "response_energy": "very_low",
        "topic_direction": "close",
        "user_state_hint": "unknown",
        "self_disclosure": "none",
        "wants_to_speak": "silence",
        "reason": "会話が自然に閉じている",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class TurnClient:
    model = "test-model"

    def __init__(self, *, social_answer: str, reply: str = "うん。") -> None:
        self._social = social_answer
        self._reply = reply
        self.purposes: list[str] = []

    async def generate(self, request):
        title = (request.format_schema or {}).get("title", "")
        self.purposes.append(request.purpose)
        if title == "SocialInterpretation":
            text = self._social
        elif title == "ReplyDraft":
            text = json.dumps({"text": self._reply}, ensure_ascii=False)
        else:
            raise AssertionError(f"unscripted schema: {title!r}")
        return LLMResponse(text=text, model=self.model, created_at=NOW, latency_ms=5)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def silent_turn(
    db, event_store, dispatcher, snapshots, arbitrator, committer, runs, failures,
    conversations, adapter, identity, prompt_registry, guard, conversation_policy,  # noqa: F811
    clock,
):
    traces = ConversationTraceRepository(db)

    def build(*, social_answer: str, reply: str = "うん。"):
        client = TurnClient(social_answer=social_answer, reply=reply)
        generator = StructuredGenerator(
            client, prompts=prompt_registry, clock=clock, max_attempts=1
        )
        engine = ConversationEngine(
            identity=identity,
            prompts=prompt_registry,
            structured=generator,
            guard=guard,
            policy=conversation_policy,
            interpreter=SocialInterpreter(
                identity=identity, prompts=prompt_registry, structured=generator
            ),
            contract_reviewer=PassingContractReviewer(),
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
            tracer=ConversationTracer(traces, clock=clock),
            clock=clock,
        )
        return service, client, traces

    return build


# --- the whole path, for the case where nothing is sent ----------------------


async def test_a_chosen_silence_runs_the_whole_path(
    silent_turn, event_store, conversations, failures, clock  # noqa: F811
) -> None:
    service, client, traces = silent_turn(social_answer=social())

    result = await service.handle_inbound(inbound(clock, text="うん"))

    # Decision
    assert result.silent
    assert result.intent is not None
    assert result.intent.intent is ResponseIntent.INTENTIONAL_SILENCE
    assert result.should_send is False
    assert result.suppressed is False

    # Event — its own type, and no message event
    types = [event.event_type for event in event_store.recent(limit=20)]
    assert YUI_INTENTIONAL_SILENCE in types
    assert YUI_MESSAGE_SENT not in types

    # Persistence: no turn, because she said nothing
    conversation = conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    turns = conversations.recent_turns(conversation.conversation_id, limit=20)
    assert all(turn.speaker == "user" for turn in turns)

    # Nothing failed, so nothing is recorded as a failure
    assert failures.recent() == []

    # No realizer call was spent on a message that was never going to be sent
    assert "conversation_reply" not in client.purposes

    # Debug
    row = traces.recent()[0]
    assert row["outcome"] == "intentional_silence"
    assert row["response_intent_started_at"] is not None
    assert row["response_intent_ended_at"] is not None
    assert row["realization_started_at"] is None


async def test_the_silence_event_records_who_decided(
    silent_turn, event_store, clock  # noqa: F811
) -> None:
    service, _client, _traces = silent_turn(social_answer=social())

    await service.handle_inbound(inbound(clock, text="うん"))

    silence = next(
        event
        for event in event_store.recent(limit=20)
        if event.event_type == YUI_INTENTIONAL_SILENCE
    )
    assert silence.actor_type == "yui"
    assert silence.payload.intent == "INTENTIONAL_SILENCE"
    assert silence.payload.reason_code == "chose_not_to_speak"
    assert silence.payload.in_reply_to_event_id


async def test_a_vetoed_turn_answers_and_records_no_silence(
    silent_turn, event_store, clock  # noqa: F811
) -> None:
    """The model wanted quiet; the USER asked a question."""
    service, client, traces = silent_turn(social_answer=social(), reply="十九だよ。")

    result = await service.handle_inbound(inbound(clock, text="ゆいは何歳？"))

    assert result.should_send
    assert result.silent is False
    assert result.intent.was_vetoed
    assert result.intent.veto.value == "direct_question"
    types = [event.event_type for event in event_store.recent(limit=20)]
    assert YUI_INTENTIONAL_SILENCE not in types
    assert "conversation_reply" in client.purposes


# --- 12.4: typing follows the decision, not the message ----------------------


async def test_silence_shows_no_typing_indicator(silent_turn, clock) -> None:  # noqa: F811
    """Spec 12.4. Three dots are a promise that a reply is coming."""
    service, _client, _traces = silent_turn(social_answer=social())
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="うん", created_at=clock.now()
    )

    result = await gateway.handle_message(message)

    assert result.silent
    assert channel.events == []
    assert channel.sent == []


async def test_speaking_still_shows_typing(silent_turn, clock) -> None:  # noqa: F811
    service, _client, _traces = silent_turn(
        social_answer=social(wants_to_speak="speak"), reply="そうだね。"
    )
    gateway = DiscordGateway(service, token="fake-token", clock=clock)
    channel = TypingChannel()
    message = FakeMessage(
        channel=channel, author=FakeAuthor(), content="やっほー", created_at=clock.now()
    )

    await gateway.handle_message(message)

    # The send happens inside the indicator, which is the existing contract.
    assert channel.events == ["start", "send", "stop"]
    assert channel.sent


# --- recovery ----------------------------------------------------------------


async def test_a_silence_survives_a_restart(
    silent_turn, db, event_store, clock  # noqa: F811
) -> None:
    """The decision is a row, not a process memory: after a restart it is still
    true that she chose to leave that turn."""
    service, _client, _traces = silent_turn(social_answer=social())
    await service.handle_inbound(inbound(clock, text="うん"))

    from app.events.store import EventStore
    from app.storage.repositories.events import EventRepository

    reopened = EventStore(db, EventRepository(db), clock=clock)
    stored = [
        event
        for event in reopened.recent(limit=20)
        if event.event_type == YUI_INTENTIONAL_SILENCE
    ]
    assert len(stored) == 1
    assert stored[0].payload.intent == "INTENTIONAL_SILENCE"

    reopened_traces = ConversationTraceRepository(db)
    assert reopened_traces.recent()[0]["outcome"] == "intentional_silence"


async def test_her_own_silence_is_not_appraised(silent_turn, clock) -> None:  # noqa: F811
    """Patch spec 5.3 carried forward: her own act is not something that
    happened *to* her, so it does not spend a model call being read."""
    from app.psychology.appraisal import AppraisalEngine

    service, _client, _traces = silent_turn(social_answer=social())
    result = await service.handle_inbound(inbound(clock, text="うん"))
    assert result.silent

    silence = next(
        event
        for event in service._events.recent(limit=20)  # noqa: SLF001
        if event.event_type == YUI_INTENTIONAL_SILENCE
    )
    assert AppraisalEngine.is_self_authored(silence)
