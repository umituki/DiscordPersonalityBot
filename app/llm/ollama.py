"""Ollama transport.

The only module that knows Ollama's HTTP shape. Everything above it sees
:class:`LLMRequest` / :class:`LLMResponse` (spec 3.1).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.clock import Clock, SystemClock
from app.llm.client import ConcurrencyLimiter
from app.llm.errors import (
    LLMProtocolError,
    LLMTimeoutError,
    LLMTransportError,
    LLMUnavailableError,
)
from app.llm.types import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:9b"


class OllamaClient:
    """Talks to a local Ollama server over its chat API."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        request_timeout_s: float = 120.0,
        connect_timeout_s: float = 10.0,
        concurrency: int = 1,
        num_ctx: int = 8192,
        temperature: float = 0.7,
        keep_alive: str = "10m",
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._request_timeout_s = request_timeout_s
        self._num_ctx = num_ctx
        self._temperature = temperature
        self._keep_alive = keep_alive
        self._clock = clock or SystemClock()
        self._limiter = ConcurrencyLimiter(concurrency)
        self._connect_timeout_s = connect_timeout_s
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def limiter(self) -> ConcurrencyLimiter:
        return self._limiter

    @property
    def http(self) -> httpx.AsyncClient:
        """The HTTP client, created on first use so building costs no socket."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(
                    self._request_timeout_s, connect=self._connect_timeout_s
                ),
                transport=self._transport,
            )
        return self._http

    # --- generation --------------------------------------------------------
    async def generate(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request)
        timeout = request.timeout_s or self._request_timeout_s

        async with self._limiter:
            started = time.perf_counter()
            try:
                response = await self.http.post("/api/chat", json=payload, timeout=timeout)
            except httpx.TimeoutException as exc:
                raise LLMTimeoutError(
                    f"ollama did not respond within {timeout}s for purpose={request.purpose}"
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMTransportError(f"ollama transport failure: {exc}") from exc
            latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code == 404:
            raise LLMUnavailableError(
                f"model {payload['model']!r} is not available on {self._base_url}"
            )
        if response.status_code >= 400:
            raise LLMTransportError(
                f"ollama returned HTTP {response.status_code}: {response.text[:300]}"
            )

        try:
            body: Any = response.json()
        except ValueError as exc:
            raise LLMProtocolError(f"ollama returned a non-JSON body: {response.text[:300]}") from exc
        if not isinstance(body, dict):
            raise LLMProtocolError(f"unexpected ollama body type: {type(body).__name__}")

        message = body.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LLMProtocolError("ollama response is missing message.content")

        done_reason = body.get("done_reason")
        return LLMResponse(
            text=message["content"],
            model=str(body.get("model") or payload["model"]),
            created_at=self._clock.now(),
            latency_ms=latency_ms,
            prompt_tokens=_optional_int(body.get("prompt_eval_count")),
            completion_tokens=_optional_int(body.get("eval_count")),
            done_reason=done_reason if isinstance(done_reason, str) else None,
            truncated=done_reason == "length",
        )

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": (
                self._temperature if request.temperature is None else request.temperature
            ),
            "num_ctx": request.num_ctx or self._num_ctx,
        }
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if request.stop:
            options["stop"] = list(request.stop)

        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": [message.model_dump() for message in request.messages],
            "stream": False,
            "keep_alive": self._keep_alive,
            "options": options,
        }
        if request.format_schema is not None:
            payload["format"] = request.format_schema
        return payload

    # --- health ------------------------------------------------------------
    async def health(self) -> bool:
        """Spec 32: Ollama health is checked before readiness."""
        try:
            response = await self.http.get("/api/tags", timeout=5.0)
        except httpx.HTTPError as exc:
            logger.warning("ollama health check failed: %s", exc)
            return False
        if response.status_code >= 400:
            logger.warning("ollama health check returned HTTP %s", response.status_code)
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(models, list):
            return False
        available = {str(entry.get("name", "")) for entry in models if isinstance(entry, dict)}
        if self._model in available:
            return True
        # Ollama reports "name:tag"; accept an untagged configuration too.
        return any(name.split(":", 1)[0] == self._model.split(":", 1)[0] for name in available)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
