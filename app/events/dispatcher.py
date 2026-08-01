"""Event dispatch with idempotent delivery (spec 28.4, storage rules).

Delivery is reserved before a subscriber runs and only marked *completed*
inside the commit transaction. So:

* a redelivered event that was already committed is skipped — no double
  psychology,
* a crash between handling and commit leaves the delivery unconsumed, and the
  event is processed again after restart.

A subscriber that raises is recorded as a failure and contributes nothing. The
run continues in degraded form rather than corrupting state (spec 2.12, 28.3).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.clock import Clock, SystemClock
from app.events.bus import EventBus, SubscriberResult
from app.events.model import Event
from app.state.proposal import StateChangeProposal
from app.state.snapshot import StateSnapshot
from app.storage.repositories.deliveries import DeliveryRepository
from app.storage.repositories.failures import FailureRecord, FailureRepository

logger = logging.getLogger(__name__)

COMPONENT = "event_dispatcher"

DeliveryStatus = Literal["handled", "skipped", "failed", "timeout"]

#: A single subscriber must not be able to stall a user-facing run forever.
DEFAULT_SUBSCRIBER_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    subscriber: str
    status: DeliveryStatus
    delivery_id: str | None
    proposal_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    event_id: str
    proposals: tuple[StateChangeProposal, ...]
    derived_events: tuple[Event, ...]
    outcomes: tuple[DeliveryOutcome, ...]
    #: Deliveries the committer must mark completed in the commit transaction.
    delivery_ids: tuple[str, ...]

    @property
    def failed(self) -> tuple[DeliveryOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.status in ("failed", "timeout"))

    @property
    def degraded(self) -> bool:
        return bool(self.failed)


class EventDispatcher:
    def __init__(
        self,
        bus: EventBus,
        delivery_repository: DeliveryRepository,
        failure_repository: FailureRepository,
        *,
        clock: Clock | None = None,
        subscriber_timeout_s: float = DEFAULT_SUBSCRIBER_TIMEOUT_S,
    ) -> None:
        self._bus = bus
        self._deliveries = delivery_repository
        self._failures = failure_repository
        self._clock = clock or SystemClock()
        self._timeout = subscriber_timeout_s

    async def dispatch(
        self, event: Event, snapshot: StateSnapshot, *, run_id: str | None = None
    ) -> DispatchResult:
        proposals: list[StateChangeProposal] = []
        derived: list[Event] = []
        outcomes: list[DeliveryOutcome] = []
        delivery_ids: list[str] = []

        for subscription in self._bus.subscriptions_for(event):
            name = subscription.subscriber.name
            decision = await asyncio.to_thread(
                self._deliveries.begin_attempt, event.event_id, name, now=self._clock.now()
            )
            if not decision.should_process:
                logger.debug(
                    "delivery skipped event_id=%s subscriber=%s status=%s",
                    event.event_id,
                    name,
                    decision.status,
                )
                outcomes.append(DeliveryOutcome(name, "skipped", decision.delivery_id))
                continue

            try:
                result = await asyncio.wait_for(
                    subscription.subscriber.handle(event, snapshot), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                await self._record_failure(
                    event, name, decision.delivery_id, run_id, "subscriber_timeout",
                    f"subscriber exceeded {self._timeout}s",
                )
                outcomes.append(
                    DeliveryOutcome(name, "timeout", decision.delivery_id, error="timeout")
                )
                continue
            except Exception as exc:  # noqa: BLE001 - one subscriber must not kill the run
                logger.exception("subscriber failed name=%s event_id=%s", name, event.event_id)
                await self._record_failure(
                    event, name, decision.delivery_id, run_id, "subscriber_exception", repr(exc)
                )
                outcomes.append(
                    DeliveryOutcome(name, "failed", decision.delivery_id, error=repr(exc))
                )
                continue

            if not isinstance(result, SubscriberResult):
                await self._record_failure(
                    event, name, decision.delivery_id, run_id, "invalid_subscriber_result",
                    f"expected SubscriberResult, got {type(result).__name__}",
                )
                outcomes.append(
                    DeliveryOutcome(name, "failed", decision.delivery_id, error="invalid result")
                )
                continue

            proposals.extend(result.proposals)
            derived.extend(result.events)
            delivery_ids.append(decision.delivery_id)
            outcomes.append(
                DeliveryOutcome(name, "handled", decision.delivery_id, len(result.proposals))
            )

        return DispatchResult(
            event_id=event.event_id,
            proposals=tuple(proposals),
            derived_events=tuple(derived),
            outcomes=tuple(outcomes),
            delivery_ids=tuple(delivery_ids),
        )

    async def _record_failure(
        self,
        event: Event,
        subscriber: str,
        delivery_id: str,
        run_id: str | None,
        reason_code: str,
        error: str,
    ) -> None:
        now: datetime = self._clock.now()
        await asyncio.to_thread(
            self._deliveries.mark_failed, delivery_id, now=now, error=error
        )
        await asyncio.to_thread(
            self._failures.record,
            FailureRecord(
                failure_type="behavior",
                component=COMPONENT,
                reason_code=reason_code,
                severity="error",
                run_id=run_id,
                event_id=event.event_id,
                reference_id=delivery_id,
                detail={"subscriber": subscriber, "error": error[:1000]},
            ),
            now=now,
        )
