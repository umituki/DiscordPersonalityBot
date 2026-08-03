"""The autonomous runtime (rebuild spec 21, Phase 6).

    毎秒 LLM を呼ばない。Event-driven + next-due-time sleep とする。

So the loop does not poll. It asks every source when it would next have
something, sleeps until the soonest of those, and wakes early if the USER
appears. A quiet night is a handful of wake-ups, not thirty thousand.

The four rules this module exists to keep:

RUNTIME-001
    The runtime writes no psychological state. It holds no committer, no
    arbitrator and no domain engine. State moves the way it always does —
    an action produces an event, the event goes through the processor, the
    domains propose, the arbitrator decides. The loop is not a shortcut into
    the middle of that.

RUNTIME-002
    A scheduler opportunity is never an action. Collecting is one authority,
    valuing is another, choosing is the Decision Engine's, and executing is the
    handler's. ``for opportunity in due: execute(opportunity)`` is the shape
    this rule forbids, and it is the shape that is easiest to write.

RUNTIME-003
    A USER message outranks anything the loop wanted to do. While a turn is in
    flight the loop still wakes and still records what it saw, but it does not
    act: she is talking to someone. The tick is written as ``deferred``, so the
    thing she nearly did is visible rather than lost.

RUNTIME-004
    Start and stop belong to the application lifecycle. Shutdown cancels the
    task and drains it — a half-finished action at exit is how a plan becomes
    a memory of something that never happened.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator, Sequence

from app import ids
from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.runtime.models import RuntimeTick, TickOutcome, WakeReason
from app.runtime.sources import Registry
from app.world.models import Opportunity

logger = logging.getLogger(__name__)

MODULE = "autonomous_runtime"
TICK = "tick"

#: How long to sleep when no source can say when it would next have something.
#: Spec 21.1 wants next-due-time sleep; this is the floor under "nobody knows",
#: not the normal cadence.
DEFAULT_IDLE_SECONDS = 300

#: Never sleep less than this after a tick, however eager a source is. Without
#: it, a source whose ``next_due`` is always "now" turns the loop into the
#: per-second poll the spec rules out.
MIN_SLEEP_SECONDS = 1.0


class AutonomousRuntime:
    """Wakes up, looks around, and usually goes back to sleep."""

    name = MODULE

    def __init__(
        self,
        registry: Registry,
        *,
        decisions: Any,
        ticks: Any = None,
        clock: Clock | None = None,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        max_opportunities: int = 32,
    ) -> None:
        self._registry = registry
        self._decisions = decisions
        #: Where wake-ups are recorded. Optional so a test can run the loop
        #: without a database, never optional in the wired application.
        self._ticks = ticks
        self._clock = clock or SystemClock()
        self._idle_seconds = float(idle_seconds)
        self._max_opportunities = int(max_opportunities)

        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._pending_reason: WakeReason = "timer"
        #: RUNTIME-003. Incremented for the duration of a USER turn.
        self._user_turns = 0
        self._last_tick: RuntimeTick | None = None

    # --- RUNTIME-003: the USER comes first -----------------------------------
    @contextmanager
    def user_turn(self) -> Iterator[None]:
        """Held for the length of a USER turn.

        Two things happen while this is held: the loop wakes (she has just
        been spoken to, and whatever it was waiting for is less interesting
        than that), and it refuses to execute a background action until the
        turn is done.
        """
        self._user_turns += 1
        self.interrupt()
        try:
            yield
        finally:
            self._user_turns = max(0, self._user_turns - 1)

    @property
    def busy_with_user(self) -> bool:
        return self._user_turns > 0

    def interrupt(self, reason: WakeReason = "user_activity") -> None:
        """Wake the loop now. Safe from any coroutine, cheap when running."""
        self._pending_reason = reason
        self._wake.set()

    # --- lifecycle (RUNTIME-004) ---------------------------------------------
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("the runtime is already started")
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name=MODULE)
        logger.info("autonomous runtime started")

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel and drain. An action half-done at exit is worse than none."""
        self._stopping.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_swallow(task)), timeout=timeout)
        except asyncio.TimeoutError:  # pragma: no cover - a wedged handler
            logger.warning("autonomous runtime did not drain within %.1fs", timeout)
        logger.info("autonomous runtime stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_tick(self) -> RuntimeTick | None:
        return self._last_tick

    # --- the loop ------------------------------------------------------------
    async def _run(self) -> None:
        reason: WakeReason = "startup"
        while not self._stopping.is_set():
            # Cleared *before* the tick, not before the sleep: an interrupt
            # that arrives while she is mid-tick has to survive to the next
            # wait, or a message sent at exactly the wrong moment is lost.
            self._wake.clear()
            try:
                tick = await self.tick(reason=reason)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the life
                logger.exception("autonomous tick failed")
                tick = None

            delay = self._sleep_seconds(tick)
            reason = await self._sleep(delay)

    async def _sleep(self, delay: float) -> WakeReason:
        """Sleep until the next due time, or until something interrupts."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return "timer"
        reason, self._pending_reason = self._pending_reason, "timer"
        return "shutdown" if self._stopping.is_set() else reason

    def _sleep_seconds(self, tick: RuntimeTick | None) -> float:
        now = self._clock.now()
        due = None if tick is None else tick.next_wake_at
        if due is None:
            return self._idle_seconds
        return max(MIN_SLEEP_SECONDS, (due - now).total_seconds())

    # --- one wake-up ---------------------------------------------------------
    async def tick(self, *, reason: WakeReason = "manual", now: datetime | None = None) -> RuntimeTick:
        """Look around once. Records what it saw whether or not it acted."""
        started = self._clock.now()
        moment = now or started

        opportunities = self._collect(moment)
        candidates, unclaimed = self._appraise(opportunities, moment)

        decision = None
        outcome: TickOutcome = "idle"
        deferred_reason = ""
        executed = False

        if candidates:
            if self.busy_with_user:
                # RUNTIME-003. Not dropped, not queued behind her back —
                # recorded, so the thing she nearly did is inspectable.
                outcome, deferred_reason = "deferred", "user_turn_in_flight"
            else:
                # RUNTIME-002. The runtime asks; it does not choose.
                decision = self._decisions.choose(candidates)
                if decision is not None:
                    executed, outcome = await self._execute(decision.chosen, moment)

        next_wake = self._next_wake(moment)
        tick = RuntimeTick(
            tick_id=ids.new_id(TICK),
            woke_at=moment,
            wake_reason=reason,
            opportunities=len(opportunities),
            opportunity_kinds=tuple(item.kind for item in opportunities),
            unclaimed_kinds=tuple(sorted(unclaimed)),
            candidates=len(candidates),
            decision_id=None if decision is None else decision.decision_id,
            chosen_action=None if decision is None else decision.chosen.action,
            executed=executed,
            outcome=outcome,
            deferred_reason=deferred_reason,
            next_wake_at=next_wake,
            duration_ms=max(
                0, int((self._clock.now() - started).total_seconds() * 1000)
            ),
        )
        self._record(tick)
        self._last_tick = tick
        if tick.opportunities or tick.executed:
            logger.info("runtime tick %s", tick.describe())
        return tick

    # --- the four steps ------------------------------------------------------
    def _collect(self, now: datetime) -> list[Opportunity]:
        collected: list[Opportunity] = []
        for source in self._registry.sources:
            if len(collected) >= self._max_opportunities:
                break
            try:
                found = source.collect(now) or ()
            except Exception:  # noqa: BLE001 - a broken source is not a broken life
                logger.exception("opportunity source failed name=%s", source.name)
                continue
            collected.extend(found)
        return collected[: self._max_opportunities]

    def _appraise(
        self, opportunities: Sequence[Opportunity], now: datetime
    ) -> tuple[list[ActionCandidate], set[str]]:
        """Ask each domain what its own opportunity would be worth.

        The runtime has no opinion here, which is deliberate: the moment it
        starts scoring an activity against a nap, every domain's judgement has
        quietly moved into the loop.
        """
        candidates: list[ActionCandidate] = []
        unclaimed: set[str] = set()
        for opportunity in opportunities:
            builder = self._registry.builder_for(opportunity.kind)
            if builder is None:
                unclaimed.add(opportunity.kind)
                continue
            try:
                candidate = builder(opportunity, now)
            except Exception:  # noqa: BLE001
                logger.exception("candidate builder failed kind=%s", opportunity.kind)
                continue
            if candidate is not None:
                candidates.append(candidate)
        if unclaimed:
            # Loud on purpose (§4.5): something is firing that nobody wants.
            logger.warning("opportunity kinds with no builder: %s", sorted(unclaimed))
        return candidates, unclaimed

    async def _execute(
        self, chosen: ActionCandidate, now: datetime
    ) -> tuple[bool, TickOutcome]:
        handler = self._registry.handler_for(chosen.action)
        if handler is None:
            # A decision nobody can carry out. Recorded rather than swallowed:
            # this is precisely the "the class exists and nothing drives it"
            # shape that §0 is about.
            logger.warning("no handler for chosen action=%s", chosen.action)
            return False, "unexecutable"
        try:
            result = handler(chosen, now)
            if asyncio.iscoroutine(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed action is not a failed loop
            logger.exception("runtime action failed action=%s", chosen.action)
            return False, "failed"
        return bool(result), "acted" if result else "idle"

    def _next_wake(self, now: datetime) -> datetime | None:
        """The soonest moment any source says it would have something."""
        soonest: datetime | None = None
        for source in self._registry.sources:
            try:
                due = source.next_due(now)
            except Exception:  # noqa: BLE001
                logger.exception("next_due failed name=%s", source.name)
                continue
            if due is None:
                continue
            if due <= now:
                # Already due and still being offered: do not busy-wait on it.
                due = now + timedelta(seconds=MIN_SLEEP_SECONDS)
            if soonest is None or due < soonest:
                soonest = due
        if soonest is None:
            return now + timedelta(seconds=self._idle_seconds)
        return min(soonest, now + timedelta(seconds=self._idle_seconds))

    def _record(self, tick: RuntimeTick) -> None:
        if self._ticks is None:
            return
        try:
            self._ticks.record(tick)
        except Exception:  # noqa: BLE001 - telemetry never costs a wake-up
            logger.exception("could not record runtime tick")


async def _swallow(task: asyncio.Task[None]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "MIN_SLEEP_SECONDS",
    "AutonomousRuntime",
]
