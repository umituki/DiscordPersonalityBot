"""INVARIANT: only the Tool Manager makes a tool call true (spec 26, 17.3).

* A request is a proposal; execution is a separate, authorised act.
* A failed call is recorded as failed and never described as a success.
* YUI is never granted an owner-only tool (spec 30).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.tools.builtin import CURRENT_TIME, WEB_SEARCH, register_builtin_tools
from app.tools.events import (
    TOOL_CALL_FAILED,
    TOOL_CALL_REFUSED,
    TOOL_CALL_SUCCEEDED,
)
from app.tools.manager import (
    ToolExecutionError,
    ToolManager,
    ToolRefusal,
    ToolRegistry,
)
from app.tools.models import ToolRequest, ToolSpec
from app.storage.repositories.tools import ToolCallRepository

pytestmark = pytest.mark.invariant

#: Phase 11 made ``effective_now`` a required argument of ``web_search``: a
#: caller that omits it is refused rather than quietly searching from today.
#: These tests are about what happens when a search *runs* and fails, so they
#: supply it — except `test_missing_arguments_are_refused`, which is about
#: omitting arguments and now has one more to omit.
WHEN = "2026-01-01T09:00:00+00:00"


@pytest.fixture
def tool_calls(db) -> ToolCallRepository:
    return ToolCallRepository(db)


@pytest.fixture
def registry(clock) -> ToolRegistry:
    registry = ToolRegistry()
    register_builtin_tools(registry, clock=clock)
    return registry


@pytest.fixture
def tools(registry, tool_calls, clock) -> ToolManager:
    return ToolManager(registry, tool_calls, clock=clock)


def request_for(name: str, **arguments: Any) -> ToolRequest:
    return ToolRequest(tool_name=name, arguments=arguments, requested_by="llm")


async def test_a_successful_call_is_recorded_and_usable(tools, tool_calls) -> None:
    result = await tools.execute(request_for(CURRENT_TIME))

    assert result.success
    assert result.is_usable
    assert result.source == "system_clock"
    row = tool_calls.get(result.call_id)
    assert row["status"] == "succeeded"
    assert row["success"] == 1
    assert result.call_id in tools.successful_call_ids()


async def test_a_failed_call_is_never_a_success(tools, tool_calls) -> None:
    """Spec 17.3: an unavailable search stays a failure."""
    result = await tools.execute(request_for(WEB_SEARCH, query="明日の天気", effective_now=WHEN))

    assert result.success is False
    assert result.is_usable is False
    assert result.error
    row = tool_calls.get(result.call_id)
    assert row["status"] == "failed"
    assert row["success"] == 0
    # And it never appears in the list the Output Guard trusts.
    assert result.call_id not in tools.successful_call_ids()
    assert tool_calls.failures()


async def test_unknown_tools_are_refused_not_invented(tools, tool_calls) -> None:
    outcome = await tools.execute(request_for("teleport"))

    assert isinstance(outcome, ToolRefusal)
    assert outcome.reason_code == "unknown_tool"
    assert tool_calls.count() == 0  # nothing was even started


async def test_missing_arguments_are_refused(tools, tool_calls) -> None:
    outcome = await tools.execute(request_for(WEB_SEARCH))

    assert isinstance(outcome, ToolRefusal)
    assert outcome.reason_code == "invalid_arguments"
    assert tool_calls.count() == 0


async def test_yui_cannot_call_an_owner_only_tool(registry, tools, tool_calls) -> None:
    """Spec 30: ``YUI 自身に Admin Tool 権限を与えない``."""

    async def destroy(arguments: dict[str, Any]) -> tuple[Any, str]:
        raise AssertionError("an owner-only tool must never be executed for YUI")

    registry.register(
        ToolSpec(
            name="delete_all_memories",
            description="admin only",
            permission="owner_only",
            available_to_yui=False,
        ),
        destroy,
    )

    outcome = await tools.execute(request_for("delete_all_memories"))

    assert isinstance(outcome, ToolRefusal)
    assert outcome.reason_code == "permission_denied"
    assert tool_calls.count() == 0


async def test_a_disabled_permission_class_blocks_execution(registry, tool_calls, clock) -> None:
    read_only_manager = ToolManager(
        registry, tool_calls, clock=clock, allowed_permissions=("read_only",)
    )

    refused = await read_only_manager.execute(request_for(WEB_SEARCH, query="x", effective_now=WHEN))
    allowed = await read_only_manager.execute(request_for(CURRENT_TIME))

    assert isinstance(refused, ToolRefusal)
    assert refused.reason_code == "permission_not_enabled"
    assert allowed.success


async def test_a_raising_tool_becomes_a_recorded_failure(registry, tools, tool_calls) -> None:
    async def broken(arguments: dict[str, Any]) -> tuple[Any, str]:
        raise ToolExecutionError("upstream is down", retryable=True)

    registry.register(
        ToolSpec(name="flaky", description="fails", permission="read_only"), broken
    )

    result = await tools.execute(request_for("flaky"))

    assert result.success is False
    assert result.retryable is True
    assert tool_calls.get(result.call_id)["status"] == "failed"


async def test_a_hanging_tool_times_out_as_a_failure(registry, tools) -> None:
    async def slow(arguments: dict[str, Any]) -> tuple[Any, str]:
        await asyncio.sleep(5)
        return None, "never"

    registry.register(
        ToolSpec(name="slow", description="hangs", permission="read_only", timeout_s=0.05),
        slow,
    )

    result = await tools.execute(request_for("slow"))

    assert result.success is False
    assert "timed out" in (result.error or "")
    assert result.retryable is True


async def test_every_outcome_becomes_an_event(tools, make_event) -> None:
    parent = make_event()

    success = tools.result_event(parent, await tools.execute(request_for(CURRENT_TIME)))
    failure = tools.result_event(
        parent, await tools.execute(request_for(WEB_SEARCH, query="x", effective_now=WHEN))
    )
    refusal = tools.result_event(parent, await tools.execute(request_for("nope")))

    assert success.event_type == TOOL_CALL_SUCCEEDED
    assert failure.event_type == TOOL_CALL_FAILED
    assert refusal.event_type == TOOL_CALL_REFUSED
    # A failure is as visible as a success in the same stream.
    assert failure.category == "action"
    assert failure.payload.error


async def test_the_guard_only_believes_executed_calls(
    tools, prompt_registry, clock, make_event
) -> None:
    """End to end: the guard's tool evidence comes from the execution record."""
    from app.conversation.guard import OutputGuard, OutputGuardPolicy
    from app.llm.validation import ValidationContext
    from pathlib import Path

    guard = OutputGuard(
        OutputGuardPolicy.load(
            Path(__file__).resolve().parents[2] / "config" / "policies" / "output_guard.yaml"
        )
    )

    class Draft:
        text = "調べてみたら、そう書いてあった。"

    failed = await tools.execute(request_for(WEB_SEARCH, query="x", effective_now=WHEN))
    assert failed.success is False

    rejected = guard.check(
        Draft(),
        ValidationContext(
            purpose="conversation_reply",
            extras={"tool_success_ids": tools.successful_call_ids()},
        ),
    )
    assert rejected is not None
    assert rejected.reason_code == "unverified_tool_claim"

    succeeded = await tools.execute(request_for(CURRENT_TIME))
    accepted = guard.check(
        Draft(),
        ValidationContext(
            purpose="conversation_reply",
            extras={"tool_success_ids": tools.successful_call_ids()},
        ),
    )
    assert succeeded.success
    assert accepted is None
