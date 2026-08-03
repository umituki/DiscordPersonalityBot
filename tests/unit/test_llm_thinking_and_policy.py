"""Patch A: thinking policy, per-purpose retry, telemetry (patch spec 3, 4, 19).

The measured failure this protects against: on the target machine an appraisal
averaged 104.8s and timed out at 120s, because a thinking model was reasoning
its way through a structured scoring task while the USER waited.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.clock import FixedClock
from app.conversation.social_interpretation import SocialInterpretation
from app.conversation.text import looks_like_question
from app.llm.errors import LLMTimeoutError
from app.llm.ollama import OllamaClient
from app.llm.policy import LLMPolicy, LLMPolicyError, PurposeRules, plan_for
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage, LLMRequest
from app.reliability.resources import ResourceManager
from tests.unit.test_llm_structured import ScriptedClient

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def llm_policy() -> LLMPolicy:
    return LLMPolicy.load(REPO_ROOT / "config" / "policies" / "llm.yaml")


def request_for(purpose: str, thinking: str = "disabled") -> LLMRequest:
    return LLMRequest(
        messages=(LLMMessage(role="user", content="こんにちは"),),
        purpose=purpose,
        thinking=thinking,  # type: ignore[arg-type]
    )


def client_with(handler, clock: FixedClock) -> OllamaClient:
    return OllamaClient(
        model="test-model", transport=httpx.MockTransport(handler), clock=clock
    )


def ok_body(content: str = '{"ok": true}', **extra) -> dict:
    return {
        "model": "test-model",
        "message": {"role": "assistant", "content": content, **extra},
        "done_reason": "stop",
        "prompt_eval_count": 12,
        "eval_count": 34,
    }


# --- think=false is actually sent (patch spec 3.1, 3.2) ---------------------
async def test_a_disabled_thinking_policy_sends_think_false(clock) -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    client = client_with(handler, clock)
    await client.generate(request_for("appraisal"))
    await client.aclose()

    assert sent[0]["think"] is False


async def test_an_enabled_thinking_policy_sends_think_true(clock) -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    client = client_with(handler, clock)
    await client.generate(request_for("reflection", thinking="enabled"))
    await client.aclose()

    assert sent[0]["think"] is True


async def test_provider_default_sends_nothing_at_all(clock) -> None:
    """It is the only way to ask for the server's own behaviour."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    client = client_with(handler, clock)
    await client.generate(request_for("reflection", thinking="provider_default"))
    await client.aclose()

    assert "think" not in sent[0]


def test_a_request_does_not_get_reasoning_by_accident() -> None:
    assert request_for("appraisal").thinking == "disabled"


# --- thinking and content stay separate (patch spec 3.3) --------------------
async def test_reasoning_never_reaches_the_answer(clock) -> None:
    """A thinking model puts reasoning in `thinking`; it must not be parsed."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=ok_body(
                content='{"score": 1}',
                thinking="ええと、まずこれを考えて、次にあれを考えて…",
            ),
        )

    client = client_with(handler, clock)
    response = await client.generate(request_for("appraisal"))
    await client.aclose()

    assert response.text == '{"score": 1}'
    assert "ええと" not in response.text


async def test_only_metadata_about_reasoning_is_kept(clock) -> None:
    """Patch spec 3.3: the count, never the text."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=ok_body(thinking="長い推論のテキスト", **{})
        )

    client = client_with(handler, clock)
    response = await client.generate(request_for("reflection", thinking="enabled"))
    await client.aclose()

    assert response.thinking_present is True
    assert response.thinking_char_count == len("長い推論のテキスト")
    assert response.thinking_enabled is True
    # There is nowhere on the response to put the reasoning itself.
    assert "thinking_text" not in response.model_dump()


async def test_server_timings_are_captured_in_milliseconds(clock) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = ok_body()
        body.update(
            total_duration=5_000_000_000,
            load_duration=1_000_000_000,
            prompt_eval_duration=2_000_000_000,
            eval_duration=2_000_000_000,
        )
        return httpx.Response(200, json=body)

    client = client_with(handler, clock)
    response = await client.generate(request_for("appraisal"))
    await client.aclose()

    assert response.total_duration_ms == 5000
    assert response.load_duration_ms == 1000
    assert response.prompt_eval_duration_ms == 2000
    assert response.eval_duration_ms == 2000


# --- per-purpose retry and timeout (patch spec 3.4) -------------------------
def test_the_shipped_policy_matches_the_patch_spec(llm_policy) -> None:
    appraisal = plan_for(llm_policy, "appraisal")
    assert appraisal.max_attempts == 1
    assert appraisal.timeout_s == 15.0
    assert appraisal.thinking == "disabled"

    dialogue = plan_for(llm_policy, "dialogue_act")
    assert dialogue.max_attempts == 1
    assert dialogue.timeout_s == 15.0

    reply = plan_for(llm_policy, "conversation_reply")
    assert reply.max_attempts == 2
    assert reply.timeout_s == 30.0


def test_every_conversation_purpose_disables_thinking(llm_policy) -> None:
    """Patch spec 3.2: these MUST be think=false."""
    for purpose in ("appraisal", "dialogue_act", "conversation_reply", "conversation_repair"):
        assert llm_policy.for_purpose(purpose).thinking == "disabled"
        assert llm_policy.is_conversation_purpose(purpose)


def test_background_reflection_may_think(llm_policy) -> None:
    assert llm_policy.for_purpose("reflection").thinking == "enabled"
    assert not llm_policy.is_conversation_purpose("reflection")


def test_a_conversation_purpose_may_not_defer_to_the_provider() -> None:
    """Patch spec 3.2: deferring is how the regression went unnoticed."""
    with pytest.raises(ValueError):
        LLMPolicy(
            purposes={"appraisal": PurposeRules(thinking="provider_default")},
            conversation_purposes=("appraisal",),
        )


def test_a_missing_policy_file_is_an_error() -> None:
    with pytest.raises(LLMPolicyError):
        LLMPolicy.load(REPO_ROOT / "config" / "policies" / "does_not_exist.yaml")


async def test_appraisal_does_not_retry_a_timeout(clock, prompt_registry) -> None:
    """One 113-second failure is enough; the USER does not wait for a second."""
    from pydantic import BaseModel

    class Schema(BaseModel):
        score: float = 0.0

    calls = 0

    class TimingOut:
        model = "test-model"

        async def generate(self, request):
            nonlocal calls
            calls += 1
            raise LLMTimeoutError("too slow")

    generator = StructuredGenerator(
        TimingOut(),
        prompts=prompt_registry,
        clock=clock,
        policy=LLMPolicy.load(REPO_ROOT / "config" / "policies" / "llm.yaml"),
    )
    outcome = await generator.generate(
        Schema, (LLMMessage(role="user", content="x"),), purpose="appraisal"
    )

    assert outcome.accepted is False
    assert calls == 1


async def test_a_reply_is_allowed_one_retry(clock, prompt_registry) -> None:
    from pydantic import BaseModel

    class Schema(BaseModel):
        text: str = ""

    client = ScriptedClient(["not json", '{"text": "こんにちは"}'])
    generator = StructuredGenerator(
        client,
        prompts=prompt_registry,
        clock=clock,
        policy=LLMPolicy.load(REPO_ROOT / "config" / "policies" / "llm.yaml"),
    )
    outcome = await generator.generate(
        Schema, (LLMMessage(role="user", content="x"),), purpose="conversation_reply"
    )

    assert outcome.accepted is True
    assert outcome.attempts == 2


async def test_the_policy_thinking_reaches_the_request(clock, prompt_registry) -> None:
    from pydantic import BaseModel

    class Schema(BaseModel):
        text: str = ""

    client = ScriptedClient(['{"text": "ok"}'])
    generator = StructuredGenerator(
        client,
        prompts=prompt_registry,
        clock=clock,
        policy=LLMPolicy.load(REPO_ROOT / "config" / "policies" / "llm.yaml"),
    )
    await generator.generate(
        Schema, (LLMMessage(role="user", content="x"),), purpose="appraisal"
    )

    assert client.requests[0].thinking == "disabled"
    assert client.requests[0].timeout_s == 15.0
    assert client.requests[0].logical_call_id


async def test_retries_share_one_logical_call_id(clock, prompt_registry) -> None:
    """Patch spec 19.1: attempts of one decision are summable."""
    from pydantic import BaseModel

    class Schema(BaseModel):
        text: str = ""

    client = ScriptedClient(["nope", '{"text": "ok"}'])
    generator = StructuredGenerator(
        client,
        prompts=prompt_registry,
        clock=clock,
        policy=LLMPolicy.load(REPO_ROOT / "config" / "policies" / "llm.yaml"),
    )
    await generator.generate(
        Schema, (LLMMessage(role="user", content="x"),), purpose="conversation_reply"
    )

    ids = {request.logical_call_id for request in client.requests}
    assert len(ids) == 1
    assert [request.attempt for request in client.requests] == [1, 2]


# --- one scheduler, not two (patch spec 4) ----------------------------------
async def test_generation_goes_through_the_resource_manager(clock, prompt_registry) -> None:
    from pydantic import BaseModel

    class Schema(BaseModel):
        text: str = ""

    resources = ResourceManager(concurrency=1)
    generator = StructuredGenerator(
        ScriptedClient(['{"text": "ok"}']),
        prompts=prompt_registry,
        clock=clock,
        policy=LLMPolicy.permissive(),
        resources=resources,
    )
    outcome = await generator.generate(
        Schema, (LLMMessage(role="user", content="x"),), purpose="conversation_reply",
        priority="P0",
    )

    assert outcome.accepted is True
    assert resources.stats.admitted == 1
    assert resources.stats.by_priority["P0"] == 1
    # The slot was handed back, so the next call is not blocked.
    assert resources.in_flight == 0


async def test_queue_wait_is_measured_separately_from_inference(
    clock, prompt_registry
) -> None:
    """Patch spec 19.1: a slow reply must be attributable, not guessed at."""
    from pydantic import BaseModel

    class Schema(BaseModel):
        text: str = ""

    resources = ResourceManager(concurrency=1)
    generator = StructuredGenerator(
        ScriptedClient(['{"text": "ok"}']),
        prompts=prompt_registry,
        clock=clock,
        policy=LLMPolicy.permissive(),
        resources=resources,
    )
    held = await resources.acquire("P7", name="simulation")

    task = asyncio.create_task(
        generator.generate(
            Schema, (LLMMessage(role="user", content="x"),), purpose="conversation_reply",
            priority="P0",
        )
    )
    await asyncio.sleep(0.02)
    resources.release(held)
    outcome = await task

    assert outcome.response is not None
    assert outcome.response.queue_wait_ms is not None
    assert outcome.response.queue_wait_ms > 0


# --- dialogue fallback (patch spec 3.5) -------------------------------------
def test_the_social_fallback_does_not_invent_an_intention() -> None:
    fallback = SocialInterpretation.minimal()

    assert fallback.primary_move == "acknowledge"
    assert fallback.question == "none"
    assert fallback.topic_direction == "stay"
    assert fallback.self_disclosure == "none"
    assert fallback.source == "default"


def test_a_direct_question_is_answered_even_by_the_fallback() -> None:
    fallback = SocialInterpretation.minimal(direct_question=True)

    assert fallback.primary_move == "answer"
    assert fallback.question == "none"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("元気？", True),
        ("どうしたの", True),
        ("何時に行くの？", True),
        ("そうなんだ", False),
        ("どうも", False),
        ("いや、特に用はないんだ", False),
        ("", False),
    ],
)
def test_question_detection_is_conservative(text: str, expected: bool) -> None:
    assert looks_like_question(text) is expected
