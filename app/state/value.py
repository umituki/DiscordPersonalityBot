"""Dynamic state values (spec 9, 31.1).

A state value is one owned key inside one domain, e.g. ``relationship.trust``.
Values are typed and versioned; ``version`` increments on every committed
change so lost updates are detectable.

Spec 24: ``0`` and ``unknown`` are different. A key that has never been written
is absent, not zero, and ``StateSnapshot.get`` returns ``None`` for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware

ValueType = Literal["number", "text", "boolean", "json", "null"]

JSONValue = Union[float, int, str, bool, None, dict, list]


class UnknownValueError(KeyError):
    """Raised when code demands a state value that has never been written."""


def value_type_of(value: JSONValue) -> ValueType:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (dict, list)):
        return "json"
    raise TypeError(f"unsupported state value type: {type(value)!r}")


class StateValue(BaseModel):
    """The current value of one state key."""

    model_config = ConfigDict(frozen=True)

    domain: str
    key: str
    value: JSONValue = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime
    updated_by_run_id: str | None = None
    updated_by_event_id: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @property
    def value_type(self) -> ValueType:
        return value_type_of(self.value)

    @property
    def numeric(self) -> float | None:
        """Numeric view of the value, or ``None`` if it is not a number."""
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            return None
        return float(self.value)

    def as_number(self) -> float:
        number = self.numeric
        if number is None:
            raise TypeError(f"{self.domain}.{self.key} is not numeric: {self.value!r}")
        return number
