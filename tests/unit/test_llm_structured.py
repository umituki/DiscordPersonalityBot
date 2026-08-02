"""Structured generation and validation (spec 28.2, 35 Phase 2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, Field

from app.llm.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage, LLMResponse
from app.llm.validation import (
    MaxLength,
    NonEmptyText,
    Stage,
    ValidationContext,
    ValidationFailure,
    ValidationPipeline,
)

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


class Appraisal(BaseModel):
    """Stand-in schema; the real one arrives with Phase 5."""

    self_relevance: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    note: str = ""


class ScriptedClient:
    """Returns queued texts or raises queued errors, one per call."""

    model = "test-model"

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.requests: list = []

    async def generate(self, request):
        self.requests.append(request)
        if not self._script:
            raise AssertionError("client called more times than scripted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(text=item, model=self.model, created_at=NOW, latency_ms=12)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def messages() -> tuple[LLMMessage, ...]:
    return (LLMMessage(role="user", content="how do you feel?"),)


async def test_valid_output_is_accepted(clock, prompt_registry) -> None:
    client = ScriptedClient(['{"self_relevance": 0.6, "novelty": 0.2, "note": "ok"}'])
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock)

    outcome = await generator.generate(Appraisal, messages(), purpose="appraisal")

    assert outcome.accepted
    assert outcome.value.self_relevance == 0.6
    assert outcome.attempts == 1
    assert client.requests[0].format_schema["title"] == "Appraisal"


async def test_code_fenced_json_is_parsed(clock, prompt_registry) -> None:
    client = ScriptedClient(['```json\n{"self_relevance": 0.1, "novelty": 0.1}\n```'])
    outcome = await StructuredGenerator(client, prompts=prompt_registry, clock=clock).generate(
        Appraisal, messages(), purpose="appraisal"
    )
    assert outcome.accepted


async def test_invalid_json_is_retried_with_a_repair_message(clock, prompt_registry) -> None:
    client = ScriptedClient(
        ["sorry, I can't do that", '{"self_relevance": 0.4, "novelty": 0.3}']
    )
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock, max_attempts=2)

    outcome = await generator.generate(Appraisal, messages(), purpose="appraisal")

    assert outcome.accepted
    assert outcome.attempts == 2
    repair = client.requests[1].messages
    assert repair[-2].role == "assistant"
    assert "parse" in repair[-1].content


async def test_schema_violation_is_retried_then_rejected(clock, failures, prompt_registry) -> None:
    client = ScriptedClient(
        ['{"self_relevance": 5.0, "novelty": 0.2}', '{"self_relevance": 9.0, "novelty": 0.2}']
    )
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock, max_attempts=2, failures=failures)

    outcome = await generator.generate(Appraisal, messages(), purpose="appraisal")

    assert outcome.accepted is False
    assert outcome.value is None
    assert outcome.rejected_stage is Stage.SCHEMA
    assert outcome.attempts == 2
    recorded = [row["reason_code"] for row in failures.recent()]
    assert recorded.count("schema_violation") == 2


async def test_transport_error_is_retried_then_reported(clock, prompt_registry) -> None:
    client = ScriptedClient(
        [LLMTimeoutError("slow"), '{"self_relevance": 0.2, "novelty": 0.2}']
    )
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock, max_attempts=2, transport_backoff_s=0)

    outcome = await generator.generate(Appraisal, messages(), purpose="appraisal")
    assert outcome.accepted


async def test_non_retryable_transport_error_stops_immediately(clock, prompt_registry) -> None:
    client = ScriptedClient([LLMUnavailableError("no model")])
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock, max_attempts=3, transport_backoff_s=0)

    outcome = await generator.generate(Appraisal, messages(), purpose="appraisal")

    assert outcome.accepted is False
    assert outcome.rejected_stage is Stage.TRANSPORT
    assert outcome.attempts == 1


async def test_semantic_rejection_is_not_retried(clock, prompt_registry) -> None:
    """The caller owns what to do about a semantically bad answer."""
    client = ScriptedClient(['{"self_relevance": 0.5, "novelty": 0.5, "note": ""}'])
    pipeline = ValidationPipeline([NonEmptyText("note")])
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock, max_attempts=3)

    outcome = await generator.generate(
        Appraisal, messages(), purpose="appraisal", pipeline=pipeline
    )

    assert outcome.accepted is False
    assert outcome.rejected_stage is Stage.SEMANTIC
    assert outcome.attempts == 1
    assert outcome.failure.reason_code == "empty_field"


async def test_rejected_outcome_refuses_to_hand_over_a_value(clock, prompt_registry) -> None:
    client = ScriptedClient(["not json"])
    outcome = await StructuredGenerator(client, prompts=prompt_registry, clock=clock, max_attempts=1).generate(
        Appraisal, messages(), purpose="appraisal"
    )
    assert outcome.value is None
    with pytest.raises(LLMError):
        outcome.require()


async def test_calls_are_traced_with_model_and_prompt_version(
    clock, db, llm_calls_repo, tracer, prompt_registry
) -> None:
    client = ScriptedClient(['{"self_relevance": 0.3, "novelty": 0.3}'])
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock, tracer=tracer)

    outcome = await generator.generate(
        Appraisal,
        messages(),
        purpose="appraisal",
        run_id="run_1",
        event_id="evt_1",
        prompt_id="appraisal",
        prompt_version="appraisal@v1",
    )

    row = llm_calls_repo.get(outcome.call_ids[0])
    assert row["status"] == "succeeded"
    assert row["purpose"] == "appraisal"
    assert row["model"] == "test-model"
    assert row["prompt_version"] == "appraisal@v1"
    assert row["structured"] == 1
    assert row["run_id"] == "run_1"
    assert row["response_text"]


async def test_failed_calls_are_traced_too(clock, llm_calls_repo, tracer, prompt_registry) -> None:
    client = ScriptedClient([LLMTimeoutError("slow")])
    generator = StructuredGenerator(
        client, prompts=prompt_registry, clock=clock, tracer=tracer, max_attempts=1, transport_backoff_s=0
    )

    outcome = await generator.generate(Appraisal, messages(), purpose="appraisal")

    row = llm_calls_repo.get(outcome.call_ids[0])
    assert row["status"] == "failed"
    assert row["error_type"] == "llm_timeout"


# --- validation pipeline ---------------------------------------------------


def test_pipeline_runs_stages_in_order() -> None:
    order: list[str] = []

    class Recorder:
        def __init__(self, name: str, stage: Stage) -> None:
            self.name = name
            self.stage = stage

        def check(self, candidate, context):
            order.append(self.name)
            return None

    pipeline = ValidationPipeline(
        [
            Recorder("identity", Stage.IDENTITY),
            Recorder("semantic", Stage.SEMANTIC),
            Recorder("state", Stage.STATE_CONSISTENCY),
        ]
    )
    outcome = pipeline.validate(object(), ValidationContext(purpose="test"))

    assert outcome.accepted
    assert order == ["semantic", "state", "identity"]


def test_pipeline_stops_at_the_first_rejection() -> None:
    calls: list[str] = []

    class Reject:
        name = "rejector"
        stage = Stage.SEMANTIC

        def check(self, candidate, context):
            calls.append(self.name)
            return ValidationFailure(self.stage, "nope", "rejected on purpose")

    class Later:
        name = "later"
        stage = Stage.IDENTITY

        def check(self, candidate, context):
            calls.append(self.name)
            return None

    outcome = ValidationPipeline([Reject(), Later()]).validate(
        object(), ValidationContext(purpose="test")
    )

    assert outcome.accepted is False
    assert outcome.value is None
    assert outcome.failure.validator == "rejector"
    assert calls == ["rejector"]


def test_max_length_validator() -> None:
    class Reply(BaseModel):
        text: str

    pipeline = ValidationPipeline([MaxLength("text", 10)])
    context = ValidationContext(purpose="test")

    assert pipeline.validate(Reply(text="short"), context).accepted
    rejected = pipeline.validate(Reply(text="x" * 50), context)
    assert rejected.failure.reason_code == "output_too_long"
