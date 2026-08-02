"""Tool Manager — the authority on what actually ran (spec 26).

::

    LLM Tool Request → permission / validation → execute
    → normalized ToolResult → Event → optional interpretation

Rules kept here:

* A request is not an execution. Anything the LLM asks for is checked against
  the registry, its permission class and its argument schema first.
* YUI never gets admin tools (spec 30).
* Every attempt is recorded, successful or not, and a failure is stored as a
  failure. Nothing downstream may describe a failed call as having worked
  (spec 17.3, 26) — the Output Guard reads exactly this record.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, Sequence

from app import ids
from app.clock import Clock, SystemClock
from app.events.model import Event
from app.tools.events import (
    TOOL_CALL_FAILED,
    TOOL_CALL_REFUSED,
    TOOL_CALL_SUCCEEDED,
    ToolCallFailedPayload,
    ToolCallRefusedPayload,
    ToolCallSucceededPayload,
)
from app.tools.models import Permission, ToolRequest, ToolResult, ToolSpec
from app.storage.repositories.tools import ToolCallRepository

logger = logging.getLogger(__name__)

COMPONENT = "tool_manager"
TOOL_CALL = "tool"


class ToolExecutionError(RuntimeError):
    """Raised by a tool implementation when a call cannot be completed."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ToolImplementation(Protocol):
    """A callable that performs the work and returns ``(data, source)``."""

    async def __call__(self, arguments: dict[str, Any]) -> tuple[Any, str]: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    implementation: ToolImplementation


@dataclass(frozen=True, slots=True)
class ToolRefusal:
    """A request that never ran, and why."""

    tool_name: str
    reason_code: str
    detail: str

    @property
    def executed(self) -> bool:
        return False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        spec: ToolSpec,
        implementation: ToolImplementation | Callable[..., Awaitable[tuple[Any, str]]],
    ) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = RegisteredTool(spec=spec, implementation=implementation)

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def specs(self, *, available_to_yui: bool | None = None) -> tuple[ToolSpec, ...]:
        entries = [entry.spec for entry in self._tools.values()]
        if available_to_yui is not None:
            entries = [spec for spec in entries if spec.available_to_yui == available_to_yui]
        return tuple(sorted(entries, key=lambda spec: spec.name))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)


class ToolManager:
    def __init__(
        self,
        registry: ToolRegistry,
        repository: ToolCallRepository,
        *,
        clock: Clock | None = None,
        allowed_permissions: Sequence[Permission] = ("read_only", "external_side_effect"),
    ) -> None:
        self._registry = registry
        self._repository = repository
        self._clock = clock or SystemClock()
        self._allowed = frozenset(allowed_permissions)

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def execute(self, request: ToolRequest) -> ToolResult | ToolRefusal:
        """Run a requested tool, or refuse it. Never pretend either way."""
        entry = self._registry.get(request.tool_name)
        if entry is None:
            return self._refuse(request, "unknown_tool", f"no tool named {request.tool_name!r}")

        spec = entry.spec
        if request.requested_by != "admin":
            if spec.permission == "owner_only" or not spec.available_to_yui:
                # Spec 30: YUI is never given the admin control plane.
                return self._refuse(
                    request, "permission_denied", f"{spec.name} is owner-only"
                )
            if spec.permission not in self._allowed:
                return self._refuse(
                    request, "permission_not_enabled", f"{spec.permission} is disabled"
                )

        missing = [
            name for name in spec.required_arguments if name not in request.arguments
        ]
        if missing:
            return self._refuse(
                request, "invalid_arguments", f"missing arguments: {missing}"
            )

        call_id = ids.new_id(TOOL_CALL)
        started_at = self._clock.now()
        await asyncio.to_thread(
            self._repository.start,
            call_id=call_id,
            tool_name=spec.name,
            permission=spec.permission,
            requested_by=request.requested_by,
            run_id=request.run_id,
            event_id=request.event_id,
            arguments=request.arguments,
            reason=request.reason,
            now=started_at,
        )

        try:
            data, source = await asyncio.wait_for(
                entry.implementation(dict(request.arguments)), timeout=spec.timeout_s
            )
        except asyncio.TimeoutError:
            return await self._record_failure(
                call_id, spec, started_at, f"timed out after {spec.timeout_s}s", retryable=True
            )
        except ToolExecutionError as exc:
            return await self._record_failure(
                call_id, spec, started_at, str(exc), retryable=exc.retryable
            )
        except Exception as exc:  # noqa: BLE001 - a broken tool is a failed call
            logger.exception("tool raised name=%s", spec.name)
            return await self._record_failure(
                call_id, spec, started_at, repr(exc), retryable=False
            )

        finished_at = self._clock.now()
        result = ToolResult(
            call_id=call_id,
            tool_name=spec.name,
            success=True,
            data=data,
            source=source,
            started_at=started_at,
            finished_at=finished_at,
        )
        await asyncio.to_thread(
            self._repository.finish,
            call_id=call_id,
            success=True,
            source=source,
            data=data,
            error=None,
            retryable=False,
            now=finished_at,
        )
        logger.info("tool call succeeded name=%s call_id=%s", spec.name, call_id)
        return result

    # --- events -------------------------------------------------------------
    def result_event(self, parent: Event, outcome: ToolResult | ToolRefusal) -> Event:
        """Turn an execution record into an event (spec 26)."""
        if isinstance(outcome, ToolRefusal):
            return parent.child(
                event_type=TOOL_CALL_REFUSED,
                category="action",
                actor_type="system",
                source_type=COMPONENT,
                clock=self._clock,
                priority="P3",
                payload=ToolCallRefusedPayload(
                    tool_name=outcome.tool_name,
                    reason_code=outcome.reason_code,
                    detail=outcome.detail[:500],
                ),
            )
        if outcome.success:
            return parent.child(
                event_type=TOOL_CALL_SUCCEEDED,
                category="action",
                actor_type="yui",
                source_type=COMPONENT,
                clock=self._clock,
                priority="P2",
                payload=ToolCallSucceededPayload(
                    call_id=outcome.call_id,
                    tool_name=outcome.tool_name,
                    source=outcome.source,
                    duration_ms=outcome.duration_ms,
                    summary=_summarise(outcome.data),
                ),
            )
        return parent.child(
            event_type=TOOL_CALL_FAILED,
            category="action",
            actor_type="yui",
            source_type=COMPONENT,
            clock=self._clock,
            priority="P2",
            payload=ToolCallFailedPayload(
                call_id=outcome.call_id,
                tool_name=outcome.tool_name,
                error=(outcome.error or "unknown error")[:500],
                retryable=outcome.retryable,
                duration_ms=outcome.duration_ms,
            ),
        )

    # --- truth about what ran ----------------------------------------------
    def successful_call_ids(self, *, run_id: str | None = None, limit: int = 20) -> list[str]:
        """The only source the Output Guard trusts (spec 17.3, 26)."""
        return self._repository.successful_call_ids(run_id=run_id, limit=limit)

    # --- helpers -----------------------------------------------------------
    def _refuse(self, request: ToolRequest, reason_code: str, detail: str) -> ToolRefusal:
        logger.info(
            "tool request refused name=%s reason=%s", request.tool_name, reason_code
        )
        return ToolRefusal(tool_name=request.tool_name, reason_code=reason_code, detail=detail)

    async def _record_failure(
        self,
        call_id: str,
        spec: ToolSpec,
        started_at,
        error: str,
        *,
        retryable: bool,
    ) -> ToolResult:
        finished_at = self._clock.now()
        await asyncio.to_thread(
            self._repository.finish,
            call_id=call_id,
            success=False,
            source="",
            data=None,
            error=error,
            retryable=retryable,
            now=finished_at,
        )
        logger.warning("tool call failed name=%s error=%s", spec.name, error[:200])
        return ToolResult(
            call_id=call_id,
            tool_name=spec.name,
            success=False,
            data=None,
            source="",
            started_at=started_at,
            finished_at=finished_at,
            error=error,
            retryable=retryable,
        )


def _summarise(data: Any, limit: int = 200) -> str:
    if data is None:
        return ""
    try:
        rendered = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(data)
    return rendered[:limit]
