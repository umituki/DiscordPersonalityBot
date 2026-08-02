"""LLM call tracing (spec 29).

``重要 LLM call は model/prompt/context snapshot/structured output を追跡可能に
する``. Every traced call records which model and prompt version produced which
output, so a later behaviour change can be attributed to a software change
rather than mistaken for YUI's own growth (spec 2.17).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from app import ids
from app.clock import Clock, SystemClock
from app.llm.errors import LLMError
from app.llm.types import LLMRequest, LLMResponse
from app.storage.repositories.llm_calls import LLMCallRepository

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRACED_CHARS = 4000


class LLMCallTracer(Protocol):
    async def start(
        self, request: LLMRequest, *, run_id: str | None = None, event_id: str | None = None
    ) -> str: ...

    async def finish_success(self, call_id: str, *, response: LLMResponse) -> None: ...

    async def finish_failure(self, call_id: str, *, error: BaseException) -> None: ...


class NullTracer:
    """Used where persistence is not available (unit tests, dry runs)."""

    async def start(
        self, request: LLMRequest, *, run_id: str | None = None, event_id: str | None = None
    ) -> str:
        return ids.new_id(ids.LLM_CALL)

    async def finish_success(self, call_id: str, *, response: LLMResponse) -> None:
        return None

    async def finish_failure(self, call_id: str, *, error: BaseException) -> None:
        return None


class DatabaseTracer:
    """Persists call traces to ``llm_calls``."""

    def __init__(
        self,
        repository: LLMCallRepository,
        *,
        clock: Clock | None = None,
        manifest_id: str | None = None,
        trace_payloads: bool = True,
        max_traced_chars: int = DEFAULT_MAX_TRACED_CHARS,
    ) -> None:
        self._repository = repository
        self._clock = clock or SystemClock()
        self._manifest_id = manifest_id
        self._trace_payloads = trace_payloads
        self._max_chars = max_traced_chars

    async def start(
        self, request: LLMRequest, *, run_id: str | None = None, event_id: str | None = None
    ) -> str:
        call_id = ids.new_id(ids.LLM_CALL)
        await asyncio.to_thread(
            self._repository.start,
            call_id=call_id,
            run_id=run_id,
            event_id=event_id,
            manifest_id=self._manifest_id,
            purpose=request.purpose,
            priority=request.priority,
            model=request.model or "unspecified",
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            structured=request.structured,
            request_fingerprint=request.fingerprint(),
            request_transcript=(
                request.transcript(max_chars=self._max_chars) if self._trace_payloads else None
            ),
            now=self._clock.now(),
        )
        return call_id

    async def finish_success(self, call_id: str, *, response: LLMResponse) -> None:
        await asyncio.to_thread(
            self._repository.finish,
            call_id=call_id,
            status="succeeded",
            now=self._clock.now(),
            latency_ms=response.latency_ms,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            response_text=(
                response.text[: self._max_chars] if self._trace_payloads else None
            ),
            error_type=None,
            error_detail=None,
        )

    async def finish_failure(self, call_id: str, *, error: BaseException) -> None:
        reason = getattr(error, "reason_code", type(error).__name__)
        await asyncio.to_thread(
            self._repository.finish,
            call_id=call_id,
            status="failed",
            now=self._clock.now(),
            latency_ms=None,
            prompt_tokens=None,
            completion_tokens=None,
            response_text=None,
            error_type=reason if isinstance(error, LLMError) else type(error).__name__,
            error_detail=str(error)[:1000],
        )
