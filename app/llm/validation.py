"""LLM output validation pipeline (spec 28.2).

::

    Transport → Parse → Schema → Semantic → State consistency
    → Identity / World invariants → ACCEPT

``不正出力から State を更新しない``. The pipeline never repairs or "best-effort
fixes" a candidate: it accepts or it rejects with a stage and a reason code.

Phase 2 owns the framework and the transport/parse/schema stages. The semantic,
state-consistency and identity validators are registered by the phases that own
those meanings (conversation Output Guard in Phase 3, psychology in Phase 5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Generic, Protocol, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Stage(IntEnum):
    """Ordered validation stages. Lower stages run first."""

    TRANSPORT = 1
    PARSE = 2
    SCHEMA = 3
    SEMANTIC = 4
    STATE_CONSISTENCY = 5
    IDENTITY = 6

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """What a validator may look at besides the candidate itself."""

    purpose: str
    run_id: str | None = None
    event_id: str | None = None
    #: Read-only state view (S0). Validators must not mutate anything.
    snapshot: Any = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    stage: Stage
    reason_code: str
    detail: str
    validator: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "stage": self.stage.label,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "validator": self.validator,
        }


@dataclass(frozen=True, slots=True)
class ValidationOutcome(Generic[T]):
    accepted: bool
    value: T | None = None
    failure: ValidationFailure | None = None

    @classmethod
    def accept(cls, value: T) -> ValidationOutcome[T]:
        return cls(accepted=True, value=value)

    @classmethod
    def reject(cls, failure: ValidationFailure) -> ValidationOutcome[T]:
        return cls(accepted=False, failure=failure)


class Validator(Protocol):
    """A single check. Returns ``None`` when the candidate is acceptable."""

    name: str
    stage: Stage

    def check(self, candidate: Any, context: ValidationContext) -> ValidationFailure | None: ...


class ValidationPipeline:
    """Runs validators in stage order and stops at the first rejection."""

    def __init__(self, validators: Sequence[Validator] = ()) -> None:
        self._validators = sorted(validators, key=lambda item: (item.stage, item.name))

    def register(self, validator: Validator) -> ValidationPipeline:
        self._validators = sorted(
            [*self._validators, validator], key=lambda item: (item.stage, item.name)
        )
        return self

    @property
    def validators(self) -> tuple[Validator, ...]:
        return tuple(self._validators)

    def stages(self) -> tuple[Stage, ...]:
        return tuple(dict.fromkeys(validator.stage for validator in self._validators))

    def validate(self, candidate: T, context: ValidationContext) -> ValidationOutcome[T]:
        for validator in self._validators:
            failure = validator.check(candidate, context)
            if failure is not None:
                logger.warning(
                    "llm output rejected purpose=%s stage=%s reason=%s validator=%s",
                    context.purpose,
                    failure.stage.label,
                    failure.reason_code,
                    failure.validator or validator.name,
                )
                return ValidationOutcome.reject(
                    ValidationFailure(
                        stage=failure.stage,
                        reason_code=failure.reason_code,
                        detail=failure.detail,
                        validator=failure.validator or validator.name,
                    )
                )
        return ValidationOutcome.accept(candidate)


# --- generally useful validators -------------------------------------------


@dataclass(frozen=True, slots=True)
class NonEmptyText:
    """Rejects a field that came back blank."""

    field_name: str
    name: str = "non_empty_text"
    stage: Stage = Stage.SEMANTIC

    def check(self, candidate: Any, context: ValidationContext) -> ValidationFailure | None:
        value = getattr(candidate, self.field_name, None)
        if isinstance(value, str) and value.strip():
            return None
        return ValidationFailure(
            stage=self.stage,
            reason_code="empty_field",
            detail=f"{self.field_name} is empty",
            validator=self.name,
        )


@dataclass(frozen=True, slots=True)
class MaxLength:
    """Rejects output far longer than the channel can carry."""

    field_name: str
    limit: int
    name: str = "max_length"
    stage: Stage = Stage.SEMANTIC

    def check(self, candidate: Any, context: ValidationContext) -> ValidationFailure | None:
        value = getattr(candidate, self.field_name, None)
        if not isinstance(value, str) or len(value) <= self.limit:
            return None
        return ValidationFailure(
            stage=self.stage,
            reason_code="output_too_long",
            detail=f"{self.field_name} is {len(value)} characters, limit {self.limit}",
            validator=self.name,
        )
