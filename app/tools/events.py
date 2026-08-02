"""Tool execution events (spec 26).

``ToolResult → Event``. A failed call is recorded as a failure, in the same
stream, with the same weight — never quietly dropped so that only successes
remain visible.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

TOOL_CALL_SUCCEEDED = "TOOL_CALL_SUCCEEDED"
TOOL_CALL_FAILED = "TOOL_CALL_FAILED"
TOOL_CALL_REFUSED = "TOOL_CALL_REFUSED"


@register_payload(TOOL_CALL_SUCCEEDED)
class ToolCallSucceededPayload(EventPayload):
    call_id: str
    tool_name: str
    source: str = ""
    duration_ms: int = 0
    summary: str = ""


@register_payload(TOOL_CALL_FAILED)
class ToolCallFailedPayload(EventPayload):
    call_id: str
    tool_name: str
    error: str
    retryable: bool = False
    duration_ms: int = 0


@register_payload(TOOL_CALL_REFUSED)
class ToolCallRefusedPayload(EventPayload):
    tool_name: str
    reason_code: str
    detail: str = ""
    requested_by: str = "llm"
