"""Context assembly with a required/important/optional budget (spec 27).

Every candidate piece of context declares how essential it is::

    REQUIRED   identity, digital existence constraints, the current message
    IMPORTANT  recent conversation, current world state, critical emotion/goals
    OPTIONAL   relevant memories, beliefs, user model, common ground

When the budget overflows, OPTIONAL items are dropped first, then IMPORTANT.
REQUIRED items are never dropped: if they alone exceed the budget, that is a
configuration error, not something to silently trim (spec 27.2).

Later phases add sources (memory in Phase 4, psychology in Phase 5); the
classification and the drop order stay here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)


class ContextOverflowError(RuntimeError):
    """Raised when REQUIRED context alone does not fit the budget."""


class Requirement(IntEnum):
    REQUIRED = 0
    IMPORTANT = 1
    OPTIONAL = 2

    @property
    def label(self) -> str:
        return self.name.lower()


#: Rough characters-per-token for mixed Japanese/English text. Tuned by
#: measurement, not treated as a constant of nature (spec 40).
DEFAULT_CHARS_PER_TOKEN = 2.0


def estimate_tokens(text: str, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / chars_per_token))


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One labelled piece of context."""

    key: str
    content: str
    requirement: Requirement = Requirement.OPTIONAL
    #: Within a requirement level, higher priority survives longer.
    priority: int = 0
    #: Where it came from, for provenance in the trace (spec 25).
    source: str = ""

    def tokens(self, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
        return estimate_tokens(self.content, chars_per_token)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_tokens: int = 3000
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN

    def tokens_of(self, text: str) -> int:
        return estimate_tokens(text, self.chars_per_token)


@dataclass(frozen=True, slots=True)
class PromptMeasurement:
    """The size of what was actually sent to the model.

    The builder's own accounting covers the items it assembled. The realizer
    then renders grounding, common ground, correction, references, style hints
    and the situation into the template around them, so the builder's number
    has never been the number that left the process. This measures the final
    string, which is the only figure a context budget can be checked against.
    """

    chars: int
    tokens: int
    #: How much of a budget this used, when one was supplied.
    fraction: float | None = None

    @property
    def over_budget(self) -> bool:
        return self.fraction is not None and self.fraction > 1.0

    def describe(self) -> str:
        head = f"{self.chars} chars / ~{self.tokens} tokens"
        if self.fraction is None:
            return head
        return f"{head} ({self.fraction:.0%} of budget)"


def measure_prompt(
    rendered: str, budget: "ContextBudget | None" = None
) -> PromptMeasurement:
    """Measure a fully rendered model input.

    Deliberately takes the finished string rather than the parts: measuring the
    parts is what produced a number that did not match reality.
    """
    chars_per_token = (
        budget.chars_per_token if budget is not None else DEFAULT_CHARS_PER_TOKEN
    )
    tokens = estimate_tokens(rendered, chars_per_token)
    fraction = None
    if budget is not None and budget.max_tokens:
        fraction = tokens / budget.max_tokens
    return PromptMeasurement(chars=len(rendered), tokens=tokens, fraction=fraction)


@dataclass(frozen=True, slots=True)
class BuiltContext:
    items: tuple[ContextItem, ...]
    dropped: tuple[ContextItem, ...]
    used_tokens: int
    budget: ContextBudget

    def text(self, separator: str = "\n\n") -> str:
        return separator.join(item.content for item in self.items if item.content)

    def keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.items)

    def dropped_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.dropped)

    def get(self, key: str) -> ContextItem | None:
        for item in self.items:
            if item.key == key:
                return item
        return None

    def includes(self, key: str) -> bool:
        return self.get(key) is not None


@dataclass
class ContextBuilder:
    """Collects candidates, then fits them to a budget."""

    _items: list[ContextItem] = field(default_factory=list)

    def add(
        self,
        key: str,
        content: str,
        *,
        requirement: Requirement = Requirement.OPTIONAL,
        priority: int = 0,
        source: str = "",
    ) -> ContextBuilder:
        if content and content.strip():
            self._items.append(
                ContextItem(
                    key=key,
                    content=content.strip(),
                    requirement=requirement,
                    priority=priority,
                    source=source,
                )
            )
        return self

    def extend(self, items: Iterable[ContextItem]) -> ContextBuilder:
        self._items.extend(items)
        return self

    @property
    def candidates(self) -> tuple[ContextItem, ...]:
        return tuple(self._items)

    def build(self, budget: ContextBudget) -> BuiltContext:
        ordered = self._ordered()
        required_tokens = sum(
            item.tokens(budget.chars_per_token)
            for item in ordered
            if item.requirement is Requirement.REQUIRED
        )
        if required_tokens > budget.max_tokens:
            raise ContextOverflowError(
                f"required context needs {required_tokens} tokens but the budget is "
                f"{budget.max_tokens}; identity and the current message are never dropped "
                "(spec 27.2)"
            )

        kept: list[ContextItem] = []
        dropped: list[ContextItem] = []
        used = 0
        for item in ordered:
            cost = item.tokens(budget.chars_per_token)
            if item.requirement is Requirement.REQUIRED or used + cost <= budget.max_tokens:
                kept.append(item)
                used += cost
            else:
                dropped.append(item)

        if dropped:
            logger.debug(
                "context overflow dropped=%s used=%d budget=%d",
                [item.key for item in dropped],
                used,
                budget.max_tokens,
            )
        # Preserve the caller's insertion order in the output; only the drop
        # decision used the requirement ordering.
        kept_keys = {id(item) for item in kept}
        return BuiltContext(
            items=tuple(item for item in self._items if id(item) in kept_keys),
            dropped=tuple(dropped),
            used_tokens=used,
            budget=budget,
        )

    def _ordered(self) -> Sequence[ContextItem]:
        """Most essential first: requirement level, then priority."""
        return sorted(
            self._items,
            key=lambda item: (item.requirement, -item.priority, self._items.index(item)),
        )
