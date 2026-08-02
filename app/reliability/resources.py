"""Resource Manager (spec 33).

::

    P0 user response
    P1 immediate appraisal
    P2 necessary tool / reasoning
    P3 memory
    P4 life event
    P5 reflection
    P6 diary
    P7 past simulation

    USER input が到着した場合、safe cancellation point で background work を譲る。

One local model, one queue, and a person waiting at the other end. The two
properties that matter:

* **Order by priority, then by arrival.** A diary entry never goes before a
  reply, and two equal-priority jobs stay first-come-first-served rather than
  reshuffling under load.
* **Yielding is cooperative and safe.** When the USER speaks, background work
  is *asked* to stop at its next checkpoint. Nothing is killed mid-write:
  a cancelled job leaves no half-finished state, because the cancellation is
  observed between units of work rather than inside one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

MODULE = "resource_manager"

T = TypeVar("T")

#: Spec 33, most urgent first.
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7")

#: At and below this priority, work is background and may be asked to yield.
BACKGROUND_FROM = "P3"


def rank(priority: str) -> int:
    """Lower is more urgent. An unknown priority sorts last, never first."""
    try:
        return PRIORITIES.index(priority)
    except ValueError:
        return len(PRIORITIES)


def is_background(priority: str) -> bool:
    return rank(priority) >= rank(BACKGROUND_FROM)


class WorkCancelled(RuntimeError):
    """Raised at a checkpoint when background work has been asked to yield."""


@dataclass
class QueuedWork:
    priority: str
    sequence: int
    name: str

    @property
    def sort_key(self) -> tuple[int, int]:
        return (rank(self.priority), self.sequence)


@dataclass
class ResourceStats:
    admitted: int = 0
    yielded: int = 0
    peak_waiting: int = 0
    by_priority: dict[str, int] = field(default_factory=dict)


class ResourceManager:
    """Admission control for model calls and background jobs (spec 33)."""

    name = MODULE

    def __init__(self, *, concurrency: int = 1) -> None:
        if concurrency < 1:
            raise ValueError("resource concurrency must be at least 1")
        self._capacity = concurrency
        self._in_flight = 0
        # A semaphore would hand the slot to whoever waited longest, which is
        # exactly the order spec 33 says not to use. Each waiter therefore
        # holds its own future and the *releasing* side chooses who runs next.
        self._waiting: list[tuple[QueuedWork, asyncio.Future[None]]] = []
        self._sequence = 0
        self._yield_requested = False
        self.stats = ResourceStats()

    # --- admission ----------------------------------------------------------
    async def acquire(self, priority: str, *, name: str = "") -> QueuedWork:
        work = QueuedWork(priority=priority, sequence=self._next(), name=name or priority)

        if self._in_flight < self._capacity and not self._waiting:
            self._admit(work)
            return work

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiting.append((work, future))
        self.stats.peak_waiting = max(self.stats.peak_waiting, len(self._waiting))
        try:
            await future
        except asyncio.CancelledError:
            self._waiting = [item for item in self._waiting if item[0] is not work]
            raise
        self.stats.admitted += 1
        self.stats.by_priority[priority] = self.stats.by_priority.get(priority, 0) + 1
        return work

    def release(self, work: QueuedWork) -> None:
        self._in_flight = max(0, self._in_flight - 1)
        # Foreground work finishing is the natural moment to stop yielding.
        if not is_background(work.priority):
            self._yield_requested = False
        self._admit_next()

    def _admit(self, work: QueuedWork) -> None:
        self._in_flight += 1
        self.stats.admitted += 1
        self.stats.by_priority[work.priority] = (
            self.stats.by_priority.get(work.priority, 0) + 1
        )

    def _admit_next(self) -> None:
        """Give the free slot to the most urgent waiter, oldest first on ties."""
        while self._waiting and self._in_flight < self._capacity:
            self._waiting.sort(key=lambda item: item[0].sort_key)
            _, future = self._waiting.pop(0)
            if future.cancelled():
                continue
            self._in_flight += 1
            future.set_result(None)
            return

    async def run(
        self, priority: str, operation: Callable[[], Awaitable[T]], *, name: str = ""
    ) -> T:
        work = await self.acquire(priority, name=name)
        try:
            return await operation()
        finally:
            self.release(work)

    # --- yielding (spec 33) -------------------------------------------------
    def user_input_arrived(self) -> None:
        """Ask background work to give way at its next checkpoint."""
        self._yield_requested = True
        logger.debug("user input arrived; background work asked to yield")

    def resume_background(self) -> None:
        self._yield_requested = False

    @property
    def yielding(self) -> bool:
        return self._yield_requested

    def should_yield(self, priority: str) -> bool:
        return self._yield_requested and is_background(priority)

    def checkpoint(self, priority: str, *, name: str = "") -> None:
        """A safe cancellation point.

        Call this *between* units of work, never inside a transaction. Raising
        here means the job stops with its state consistent, which is the whole
        difference between yielding and being killed.
        """
        if self.should_yield(priority):
            self.stats.yielded += 1
            raise WorkCancelled(
                f"{name or priority} yielded to the USER at a safe point (spec 33)"
            )

    # --- inspection ---------------------------------------------------------
    def waiting(self) -> tuple[QueuedWork, ...]:
        return tuple(
            work for work, _ in sorted(self._waiting, key=lambda item: item[0].sort_key)
        )

    def next_in_line(self) -> QueuedWork | None:
        queue = self.waiting()
        return queue[0] if queue else None

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence
