"""LLM client interface and tracing decorator.

Spec 3.2: LLM concurrency is 1 by default — a local 9B model serving one user
gains nothing from parallel requests and loses latency on the P0 reply. The
limit lives in the client so no caller can accidentally bypass it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from app.clock import Clock, SystemClock
from app.llm.errors import LLMError
from app.llm.tracing import LLMCallTracer
from app.llm.types import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMClient(Protocol):
    """Every model access in the system goes through this interface."""

    @property
    def model(self) -> str:
        """Identifier of the model this client talks to (spec 29)."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Run one completion. Raises :class:`LLMError` subclasses on failure."""

    async def health(self) -> bool:
        """``True`` when the model host is reachable and the model is loadable."""

    async def aclose(self) -> None:
        """Release transport resources."""


class ConcurrencyLimiter:
    """Serialises model calls (spec 3.2)."""

    def __init__(self, concurrency: int = 1) -> None:
        if concurrency < 1:
            raise ValueError("LLM concurrency must be at least 1")
        self._semaphore = asyncio.Semaphore(concurrency)
        self.concurrency = concurrency
        self.peak_waiters = 0
        self._waiters = 0

    async def __aenter__(self) -> None:
        self._waiters += 1
        self.peak_waiters = max(self.peak_waiters, self._waiters)
        await self._semaphore.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self._waiters -= 1
        self._semaphore.release()


class TracedLLMClient:
    """Wraps a client so every call is recorded for spec 29 traceability."""

    def __init__(
        self,
        inner: LLMClient,
        tracer: LLMCallTracer,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._inner = inner
        self._tracer = tracer
        self._clock = clock or SystemClock()

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def inner(self) -> LLMClient:
        return self._inner

    async def generate(
        self,
        request: LLMRequest,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> LLMResponse:
        call_id = await self._tracer.start(request, run_id=run_id, event_id=event_id)
        try:
            response = await self._inner.generate(request)
        except LLMError as exc:
            await self._tracer.finish_failure(call_id, error=exc)
            raise
        await self._tracer.finish_success(call_id, response=response)
        return response

    async def health(self) -> bool:
        return await self._inner.health()

    async def aclose(self) -> None:
        await self._inner.aclose()
