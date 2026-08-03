"""FIRST BOOT lifecycle events (rebuild spec 34.20 — Phase 13).

System events, not experiences. They describe the *machine* getting her ready
to exist, and none of them goes near the psychological pipeline — being born is
not something she remembers happening to her, and encoding it would put the
installation procedure in her autobiography.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

FIRST_BOOT_STARTED = "FIRST_BOOT_STARTED"
FIRST_BOOT_RESUMED = "FIRST_BOOT_RESUMED"
FIRST_BOOT_BLOCKED = "FIRST_BOOT_BLOCKED"
FIRST_BOOT_COMPLETED = "FIRST_BOOT_COMPLETED"

#: None of these is an experience. The lifecycle of the process that made her
#: is not part of the life it made.
LIFECYCLE_EVENTS: frozenset[str] = frozenset(
    {FIRST_BOOT_STARTED, FIRST_BOOT_RESUMED, FIRST_BOOT_BLOCKED, FIRST_BOOT_COMPLETED}
)


@register_payload(FIRST_BOOT_STARTED)
class FirstBootStartedPayload(EventPayload):
    epoch_id: str
    genesis_run_id: str
    birth: str = ""
    present: str = ""
    life_years: int = 0


@register_payload(FIRST_BOOT_RESUMED)
class FirstBootResumedPayload(EventPayload):
    epoch_id: str
    genesis_run_id: str
    attempt: int = 1
    from_checkpoint: str = ""


@register_payload(FIRST_BOOT_BLOCKED)
class FirstBootBlockedPayload(EventPayload):
    epoch_id: str
    genesis_run_id: str = ""
    kind: str = ""
    retryable: bool = True
    #: Short. A blocked run's reason is a sentence, not a stack trace.
    reason: str = ""


@register_payload(FIRST_BOOT_COMPLETED)
class FirstBootCompletedPayload(EventPayload):
    """Deliberately counts, not contents (point 38: no huge payloads)."""

    epoch_id: str
    genesis_run_id: str
    birth: str = ""
    present: str = ""
    life_years: int = 0
    months: int = 0
    experiences: int = 0
    memories: int = 0
    surviving_npcs: int = 0
    audits_passed: int = 0


__all__ = [
    "FIRST_BOOT_BLOCKED",
    "FIRST_BOOT_COMPLETED",
    "FIRST_BOOT_RESUMED",
    "FIRST_BOOT_STARTED",
    "LIFECYCLE_EVENTS",
    "FirstBootBlockedPayload",
    "FirstBootCompletedPayload",
    "FirstBootResumedPayload",
    "FirstBootStartedPayload",
]
