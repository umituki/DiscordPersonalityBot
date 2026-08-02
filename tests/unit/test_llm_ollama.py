"""Ollama transport (spec 3.1, 3.2, 28.1).

The HTTP boundary is mocked; no test contacts a real model.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.llm.errors import (
    LLMProtocolError,
    LLMTimeoutError,
    LLMTransportError,
    LLMUnavailableError,
)
from app.llm.ollama import OllamaClient
from app.llm.types import LLMMessage, LLMRequest


def chat_body(content: str, **overrides) -> dict:
    body = {
        "model": "qwen3.5:9b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 120,
        "eval_count": 40,
    }
    body.update(overrides)
    return body


def client_with(handler, clock, **kwargs) -> OllamaClient:
    return OllamaClient(
        transport=httpx.MockTransport(handler), clock=clock, **kwargs
    )


def request(**kwargs) -> LLMRequest:
    return LLMRequest(
        messages=(LLMMessage(role="user", content="hello"),), purpose="unit_test", **kwargs
    )


async def test_generate_returns_normalised_response(clock) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/chat"
        payload = json.loads(req.content)
        assert payload["stream"] is False
        assert payload["options"]["num_ctx"] == 8192
        return httpx.Response(200, json=chat_body("こんにちは"))

    client = client_with(handler, clock)
    try:
        response = await client.generate(request())
        assert response.text == "こんにちは"
        assert response.prompt_tokens == 120
        assert response.completion_tokens == 40
        assert response.total_tokens == 160
        assert response.created_at == clock.now()
        assert response.truncated is False
    finally:
        await client.aclose()


async def test_structured_request_sends_the_schema(clock) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(200, json=chat_body('{"ok": true}'))

    client = client_with(handler, clock)
    try:
        await client.generate(request(format_schema={"type": "object"}, temperature=0.1))
        assert captured["format"] == {"type": "object"}
        assert captured["options"]["temperature"] == 0.1
    finally:
        await client.aclose()


async def test_truncated_generation_is_flagged(clock) -> None:
    client = client_with(
        lambda req: httpx.Response(200, json=chat_body("half", done_reason="length")), clock
    )
    try:
        response = await client.generate(request())
        assert response.truncated is True
    finally:
        await client.aclose()


async def test_timeout_becomes_llm_timeout_error(clock) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=req)

    client = client_with(handler, clock)
    try:
        with pytest.raises(LLMTimeoutError) as info:
            await client.generate(request())
        assert info.value.retryable is True
    finally:
        await client.aclose()


async def test_connection_error_becomes_transport_error(clock) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    client = client_with(handler, clock)
    try:
        with pytest.raises(LLMTransportError):
            await client.generate(request())
    finally:
        await client.aclose()


async def test_missing_model_is_unavailable(clock) -> None:
    client = client_with(lambda req: httpx.Response(404, text="model not found"), clock)
    try:
        with pytest.raises(LLMUnavailableError) as info:
            await client.generate(request())
        assert info.value.retryable is False
    finally:
        await client.aclose()


async def test_server_error_is_transport_error(clock) -> None:
    client = client_with(lambda req: httpx.Response(500, text="boom"), clock)
    try:
        with pytest.raises(LLMTransportError):
            await client.generate(request())
    finally:
        await client.aclose()


async def test_malformed_body_is_protocol_error(clock) -> None:
    client = client_with(lambda req: httpx.Response(200, text="not json"), clock)
    try:
        with pytest.raises(LLMProtocolError):
            await client.generate(request())
    finally:
        await client.aclose()


async def test_missing_content_is_protocol_error(clock) -> None:
    client = client_with(lambda req: httpx.Response(200, json={"model": "x"}), clock)
    try:
        with pytest.raises(LLMProtocolError):
            await client.generate(request())
    finally:
        await client.aclose()


async def test_calls_are_serialised_to_one_at_a_time(clock) -> None:
    """Spec 3.2: LLM concurrency is 1."""
    active = 0
    peak = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=chat_body("ok"))

    client = client_with(handler, clock)
    try:
        await asyncio.gather(*(client.generate(request()) for _ in range(5)))
        assert peak == 1
        assert client.limiter.concurrency == 1
    finally:
        await client.aclose()


async def test_health_checks_the_model_is_present(clock) -> None:
    listed = {"models": [{"name": "qwen3.5:9b"}, {"name": "other:latest"}]}
    client = client_with(lambda req: httpx.Response(200, json=listed), clock)
    try:
        assert await client.health() is True
    finally:
        await client.aclose()

    missing = client_with(lambda req: httpx.Response(200, json={"models": []}), clock)
    try:
        assert await missing.health() is False
    finally:
        await missing.aclose()


async def test_health_is_false_when_host_is_down(clock) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    client = client_with(handler, clock)
    try:
        assert await client.health() is False
    finally:
        await client.aclose()


def test_building_a_client_opens_no_socket(clock) -> None:
    client = OllamaClient(clock=clock)
    assert client._http is None  # lazily created on first use
    assert client.model == "qwen3.5:9b"
