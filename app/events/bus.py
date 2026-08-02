"""Event bus (spec 8.1).

Subscribers receive events and return *evidence about what should change* —
proposals and follow-up events. A subscriber never writes state itself; that is
the arbitrator's and committer's job (spec 2.7).

Delivery filtering also enforces spec 8.1: ``ADMIN / SYSTEM event は原則
Psychology subscriber に配送しない``. A subscriber declares its kind, and the bus
refuses to hand a psychology subscriber an admin or system event.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence, runtime_checkable

from app.events.model import NON_PSYCHOLOGICAL_CATEGORIES, Event
from app.orchestrator.run_view import RunView
from app.state.proposal import StateChangeProposal

logger = logging.getLogger(__name__)

#: ``psychology`` subscribers form YUI's inner life and must not see admin or
#: system plumbing. ``system`` subscribers exist for infrastructure.
#: ``observer`` subscribers are read-only (logging, metrics) and may see all.
SubscriberKind = Literal["psychology", "system", "observer"]


class SubscriptionError(ValueError):
    """Raised on an invalid or duplicate subscription."""


@dataclass(frozen=True, slots=True)
class SubscriberResult:
    """What a subscriber returns. Never a state write."""

    proposals: tuple[StateChangeProposal, ...] = ()
    events: tuple[Event, ...] = ()
    notes: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> SubscriberResult:
        return cls()


@runtime_checkable
class Subscriber(Protocol):
    """Anything that reacts to an event."""

    name: str

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        """React to ``event``, reading state only from ``view`` (spec 9.2)."""


@dataclass(frozen=True, slots=True)
class Subscription:
    subscriber: Subscriber
    kind: SubscriberKind
    event_types: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    order: int = 100

    def accepts(self, event: Event) -> bool:
        if self.kind == "psychology" and event.category in NON_PSYCHOLOGICAL_CATEGORIES:
            return False
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.categories and event.category not in self.categories:
            return False
        return True


@dataclass
class EventBus:
    """Routing table from events to subscribers. Holds no state of its own."""

    _subscriptions: list[Subscription] = field(default_factory=list)

    def register(
        self,
        subscriber: Subscriber,
        *,
        kind: SubscriberKind = "psychology",
        event_types: Sequence[str] = (),
        categories: Sequence[str] = (),
        order: int = 100,
    ) -> Subscription:
        name = getattr(subscriber, "name", "")
        if not name:
            raise SubscriptionError("a subscriber must expose a non-empty 'name'")
        if any(existing.subscriber.name == name for existing in self._subscriptions):
            raise SubscriptionError(f"subscriber {name!r} is already registered")
        subscription = Subscription(
            subscriber=subscriber,
            kind=kind,
            event_types=frozenset(event_types),
            categories=frozenset(categories),
            order=order,
        )
        self._subscriptions.append(subscription)
        self._subscriptions.sort(key=lambda item: (item.order, item.subscriber.name))
        logger.debug("subscriber registered name=%s kind=%s", name, kind)
        return subscription

    def unregister(self, name: str) -> bool:
        before = len(self._subscriptions)
        self._subscriptions = [
            item for item in self._subscriptions if item.subscriber.name != name
        ]
        return len(self._subscriptions) != before

    def subscriptions_for(self, event: Event) -> tuple[Subscription, ...]:
        return tuple(item for item in self._subscriptions if item.accepts(event))

    def names(self) -> tuple[str, ...]:
        return tuple(item.subscriber.name for item in self._subscriptions)

    def __len__(self) -> int:
        return len(self._subscriptions)
