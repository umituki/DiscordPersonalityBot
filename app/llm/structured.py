"""Structured generation with schema validation (spec 35 Phase 2).

The generator asks the model for JSON matching a pydantic schema, then runs the
spec 28.2 pipeline over the result. A rejected candidate is never returned as a
usable value — the caller gets an outcome describing the failing stage, so it
can degrade instead of committing something invalid (spec 2.12).

Retries are bounded and only for the stages where retrying is meaningful:
a malformed or schema-violating answer may be repaired by telling the model
what was wrong. A semantically rejected answer is returned to the caller, which
owns the policy for what to do next.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Generic, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from app.clock import Clock, SystemClock
from app.llm.errors import LLMError
from app.llm.prompts import PromptRegistry
from app.llm.tracing import LLMCallTracer, NullTracer
from app.llm.types import LLMMessage, LLMRequest, LLMResponse
from app.llm.validation import (
    Stage,
    ValidationContext,
    ValidationFailure,
    ValidationPipeline,
)
from app.storage.repositories.failures import FailureRecord, FailureRepository

logger = logging.getLogger(__name__)

COMPONENT = "structured_llm"

T = TypeVar("T", bound=BaseModel)

_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

#: Stages where asking the model again, with the error explained, can help.
DEFAULT_RETRY_STAGES = frozenset({Stage.TRANSPORT, Stage.PARSE, Stage.SCHEMA})

REPAIR_PROMPT_ID = "structured_repair"
SYSTEM_PROMPT_ID = "structured_output_system"


@dataclass(frozen=True, slots=True)
class StructuredOutcome(Generic[T]):
    """Result of a structured generation. ``value`` is set only when accepted."""

    accepted: bool
    value: T | None = None
    failure: ValidationFailure | None = None
    attempts: int = 0
    call_ids: tuple[str, ...] = ()
    response: LLMResponse | None = None

    @property
    def rejected_stage(self) -> Stage | None:
        return None if self.failure is None else self.failure.stage

    def require(self) -> T:
        """Return the value or raise. Use only where degradation is impossible."""
        if not self.accepted or self.value is None:
            detail = "unknown" if self.failure is None else self.failure.detail
            raise LLMError(f"structured generation was rejected: {detail}")
        return self.value


class StructuredGenerator:
    def __init__(
        self,
        client,
        *,
        prompts: PromptRegistry,
        failures: FailureRepository | None = None,
        tracer: LLMCallTracer | None = None,
        clock: Clock | None = None,
        max_attempts: int = 3,
        retry_stages: frozenset[Stage] = DEFAULT_RETRY_STAGES,
        transport_backoff_s: float = 0.5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._client = client
        self._prompts = prompts
        self._failures = failures
        self._tracer = tracer or NullTracer()
        self._clock = clock or SystemClock()
        self._max_attempts = max_attempts
        self._retry_stages = retry_stages
        self._backoff = transport_backoff_s

    async def generate(
        self,
        schema: type[T],
        messages: Sequence[LLMMessage],
        *,
        purpose: str,
        pipeline: ValidationPipeline | None = None,
        context: ValidationContext | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        num_ctx: int | None = None,
        timeout_s: float | None = None,
        priority: str = "P3",
        prompt_id: str | None = None,
        prompt_version: str | None = None,
    ) -> StructuredOutcome[T]:
        validation_context = context or ValidationContext(
            purpose=purpose, run_id=run_id, event_id=event_id
        )
        request = LLMRequest(
            messages=tuple(messages),
            purpose=purpose,
            model=getattr(self._client, "model", None),
            format_schema=schema.model_json_schema(),
            temperature=temperature,
            max_tokens=max_tokens,
            num_ctx=num_ctx,
            timeout_s=timeout_s,
            priority=priority,  # type: ignore[arg-type]
            prompt_id=prompt_id,
            prompt_version=prompt_version,
        )

        call_ids: list[str] = []
        last_failure: ValidationFailure | None = None
        last_response: LLMResponse | None = None

        for attempt in range(1, self._max_attempts + 1):
            call_id = await self._tracer.start(request, run_id=run_id, event_id=event_id)
            call_ids.append(call_id)

            try:
                response = await self._client.generate(request)
            except LLMError as exc:
                await self._tracer.finish_failure(call_id, error=exc)
                last_failure = ValidationFailure(
                    stage=Stage.TRANSPORT,
                    reason_code=exc.reason_code,
                    detail=str(exc)[:500],
                    validator="transport",
                )
                self._record_failure(purpose, last_failure, run_id, event_id, call_id, attempt)
                if not exc.retryable or attempt >= self._max_attempts:
                    break
                await asyncio.sleep(self._backoff * attempt)
                continue

            await self._tracer.finish_success(call_id, response=response)
            last_response = response

            parsed, failure = self._parse(response.text, schema)
            if failure is None and parsed is not None:
                outcome = (pipeline or ValidationPipeline()).validate(
                    parsed, validation_context
                )
                if outcome.accepted:
                    return StructuredOutcome(
                        accepted=True,
                        value=outcome.value,
                        attempts=attempt,
                        call_ids=tuple(call_ids),
                        response=response,
                    )
                failure = outcome.failure

            assert failure is not None
            last_failure = failure
            self._record_failure(purpose, failure, run_id, event_id, call_id, attempt)

            if failure.stage not in self._retry_stages or attempt >= self._max_attempts:
                break
            request = self._repair_request(request, response, failure)

        return StructuredOutcome(
            accepted=False,
            failure=last_failure,
            attempts=len(call_ids),
            call_ids=tuple(call_ids),
            response=last_response,
        )

    # --- parsing -----------------------------------------------------------
    @staticmethod
    def _parse(text: str, schema: type[T]) -> tuple[T | None, ValidationFailure | None]:
        candidate = text.strip()
        fenced = _CODE_FENCE.match(candidate)
        if fenced is not None:
            candidate = fenced.group(1)

        try:
            data: Any = json.loads(candidate)
        except json.JSONDecodeError as exc:
            return None, ValidationFailure(
                stage=Stage.PARSE,
                reason_code="invalid_json",
                detail=f"{exc.msg} at position {exc.pos}",
                validator="json_parser",
            )
        if not isinstance(data, dict):
            return None, ValidationFailure(
                stage=Stage.PARSE,
                reason_code="not_an_object",
                detail=f"expected a JSON object, got {type(data).__name__}",
                validator="json_parser",
            )
        try:
            return schema.model_validate(data), None
        except ValidationError as exc:
            return None, ValidationFailure(
                stage=Stage.SCHEMA,
                reason_code="schema_violation",
                detail="; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()[:5]
                ),
                validator=schema.__name__,
            )

    # --- retry -------------------------------------------------------------
    def _repair_request(
        self, request: LLMRequest, response: LLMResponse, failure: ValidationFailure
    ) -> LLMRequest:
        instruction = self._repair_instruction(failure)
        return request.append(
            LLMMessage(role="assistant", content=response.text[:2000]),
            LLMMessage(role="user", content=instruction),
        )

    def _repair_instruction(self, failure: ValidationFailure) -> str:
        """Repair text comes from the versioned registry, never from code."""
        template = self._prompts.get(REPAIR_PROMPT_ID)
        return template.render(stage=failure.stage.label, detail=failure.detail)

    def system_message(self, schema: type[BaseModel]) -> LLMMessage:
        """Standard system instruction for a structured call (spec 38)."""
        template = self._prompts.get(SYSTEM_PROMPT_ID)
        return LLMMessage(role="system", content=template.render(schema_name=schema.__name__))

    # --- failure recording -------------------------------------------------
    def _record_failure(
        self,
        purpose: str,
        failure: ValidationFailure,
        run_id: str | None,
        event_id: str | None,
        call_id: str,
        attempt: int,
    ) -> None:
        if self._failures is None:
            return
        failure_type = {
            Stage.TRANSPORT: "transport",
            Stage.PARSE: "parse",
            Stage.SCHEMA: "schema",
            Stage.SEMANTIC: "semantic",
            Stage.STATE_CONSISTENCY: "consistency",
            Stage.IDENTITY: "behavior",
        }[failure.stage]
        self._failures.record(
            FailureRecord(
                failure_type=failure_type,  # type: ignore[arg-type]
                component=COMPONENT,
                reason_code=failure.reason_code,
                severity="warning",
                run_id=run_id,
                event_id=event_id,
                reference_id=call_id,
                detail={"purpose": purpose, "attempt": attempt, **failure.as_dict()},
            ),
            now=self._clock.now(),
        )
