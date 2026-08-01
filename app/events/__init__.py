"""Event system.

Spec 8: events are the immutable record of what happened. This layer stores and
routes them; it never decides psychology.
"""

from app.events.bus import EventBus, Subscriber, SubscriptionError
from app.events.dispatcher import DeliveryOutcome, DeliveryStatus, EventDispatcher
from app.events.model import (
    ActorType,
    Event,
    EventCategory,
    EventOrigin,
    EventPayload,
    Priority,
    register_payload,
)
from app.events.store import EventStore, ImmutableEventError

__all__ = [
    "ActorType",
    "DeliveryOutcome",
    "DeliveryStatus",
    "Event",
    "EventBus",
    "EventCategory",
    "EventDispatcher",
    "EventOrigin",
    "EventPayload",
    "EventStore",
    "ImmutableEventError",
    "Priority",
    "Subscriber",
    "SubscriptionError",
    "register_payload",
]
