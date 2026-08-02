"""Tool request and result types (spec 26).

The LLM may *ask* for a tool. Only the Tool Manager can run one, and only its
record makes a call true: ``Tool failure を成功として表現しない``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

#: Spec 26 permission classes.
Permission = Literal["read_only", "external_side_effect", "owner_only"]


class ToolRequest(BaseModel):
    """A proposed tool call. Proposing is not running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Who asked. ``llm`` requests are the untrusted case (spec 2.1).
    requested_by: Literal["llm", "engine", "admin"] = "llm"
    reason: str = ""
    run_id: str | None = None
    event_id: str | None = None


class ToolResult(BaseModel):
    """The normalised outcome of an execution attempt (spec 26)."""

    model_config = ConfigDict(frozen=True)

    call_id: str
    tool_name: str
    success: bool
    data: Any = None
    source: str = ""
    started_at: datetime
    finished_at: datetime
    error: str | None = None
    retryable: bool = False

    @field_validator("started_at", "finished_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def is_usable(self) -> bool:
        """Only a successful call may be spoken about as having happened."""
        return self.success and self.error is None


class ToolSpec(BaseModel):
    """What a tool is and what it is allowed to do."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    permission: Permission
    #: JSON schema for the arguments, validated before execution.
    argument_schema: dict[str, Any] = Field(default_factory=dict)
    #: Required arguments, checked without pulling in a JSON-schema library.
    required_arguments: tuple[str, ...] = ()
    timeout_s: float = Field(default=20.0, gt=0)
    #: Tools YUI herself may request. Admin tools are never in this set
    #: (spec 30: ``YUI 自身に Admin Tool 権限を与えない``).
    available_to_yui: bool = True
