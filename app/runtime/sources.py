"""Where opportunities come from, and what may be done about them (spec 21, 22).

Three registries, deliberately separate, because they are three different
authorities:

``OpportunitySource``
    notices that something *could* be done. Spec 22: an opportunity is a
    possibility, not an event, and producing one is not deciding anything.

``CandidateBuilder``
    says what that possibility would be worth. Owned by the domain the
    opportunity belongs to — the runtime has no opinion about how much an
    activity is worth compared to a nap.

``ActionHandler``
    actually carries it out, and owns the event it produces. Spec 22 again:
    only a *selected and executed* opportunity becomes an event.

Phases 7-11 register into these. None of them edits the loop, which is the
whole reason the seams exist: a runtime that has to be modified to gain a
capability grows a branch per subsystem and ends up as the place where every
domain's judgement quietly lives.

A kind with a source but no builder is not an error and is not silently
dropped — it is counted as *unclaimed* and written to the tick row, because
"the scheduler has been firing this for a week and nothing has ever picked it
up" is exactly the finding §0 exists to make visible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable, Protocol, Sequence, runtime_checkable

from app.agency.models import ActionCandidate
from app.world.models import Opportunity


@runtime_checkable
class OpportunitySource(Protocol):
    """Something that notices moments. It must not act on them."""

    name: str

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        """Opportunities available at this moment. May be empty, usually is."""

    def next_due(self, now: datetime) -> datetime | None:
        """When this source would next have something, if it can say.

        Returning ``None`` means "I cannot predict it" — the loop falls back to
        its idle interval rather than sleeping forever. Spec 21.1 wants
        next-due-time sleep, not a poll every second; a source that knows its
        own schedule is what makes that possible.
        """


#: Turns an opportunity into a weighted candidate, or declines it. Declining is
#: normal: a habit cue encountered while she is asleep is not a candidate.
CandidateBuilder = Callable[[Opportunity, datetime], ActionCandidate | None]

#: Carries out a chosen action and owns whatever event that produces. Returns
#: True when something actually happened.
ActionHandler = Callable[[ActionCandidate, datetime], Awaitable[bool] | bool]


class Registry:
    """The runtime's wiring, in one object so it can be inspected."""

    def __init__(self) -> None:
        self._sources: list[OpportunitySource] = []
        self._builders: dict[str, CandidateBuilder] = {}
        self._handlers: dict[str, ActionHandler] = {}

    # --- registration --------------------------------------------------------
    def add_source(self, source: OpportunitySource) -> None:
        self._sources.append(source)

    def add_builder(self, kind: str, builder: CandidateBuilder) -> None:
        if kind in self._builders:
            raise ValueError(f"a candidate builder for {kind!r} is already registered")
        self._builders[kind] = builder

    def add_handler(self, action: str, handler: ActionHandler) -> None:
        if action in self._handlers:
            raise ValueError(f"a handler for {action!r} is already registered")
        self._handlers[action] = handler

    # --- reading -------------------------------------------------------------
    @property
    def sources(self) -> tuple[OpportunitySource, ...]:
        return tuple(self._sources)

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._builders)

    @property
    def actions(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def builder_for(self, kind: str) -> CandidateBuilder | None:
        return self._builders.get(kind)

    def handler_for(self, action: str) -> ActionHandler | None:
        return self._handlers.get(action)


class SchedulerSource:
    """The scheduler as an opportunity source (spec 19, 22).

    RUNTIME-002 lives here as much as in the loop: this hands over
    opportunities and nothing else. It does not know what any of them would be
    worth and cannot execute one.
    """

    name = "scheduler"

    def __init__(self, scheduler) -> None:
        self._scheduler = scheduler

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        return self._scheduler.tick(now=now)

    def next_due(self, now: datetime) -> datetime | None:
        """The soonest pending job. Condition jobs cannot say, so they do not."""
        soonest: datetime | None = None
        for job in self._scheduler.pending(limit=200):
            due = job.due_at
            if due is None:
                continue
            if soonest is None or due < soonest:
                soonest = due
        return soonest


__all__ = [
    "ActionHandler",
    "CandidateBuilder",
    "OpportunitySource",
    "Registry",
    "SchedulerSource",
]
