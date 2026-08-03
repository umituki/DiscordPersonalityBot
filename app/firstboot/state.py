"""The FIRST BOOT state machine (rebuild spec 34.20 — Phase 13).

A boolean cannot express what an operator needs to know. "Not complete" covers
a run that has never been started, a run that is halfway through year nine, a
run that stopped because the model went away, and a run that stopped because
her chronology contradicts itself — and the last two need opposite responses.

So::

    PENDING            nothing has been asked for yet
    READY              preflight passed; a start would be accepted
    RUNNING            a process holds the lease and is generating
    PAUSED             stopped politely at a checkpoint boundary
    BLOCKED_RETRYABLE  stopped by something that might work next time
    BLOCKED_FATAL      stopped by something that will not
    AUDITING           generation done, finalisation under way
    COMPLETE           she exists; the character plane may open

There is no ``CANCELLED``. Genesis generates a life, and the useful response to
wanting to stop one is to stop and resume rather than to throw it away — a
cancel would mean discarding nineteen years of generation for a decision that
could have been "later".

``RECOVERY_REQUIRED`` is not a stored status. It is what ``RUNNING`` *means*
when no process holds the lease: the machine died mid-run. Storing it would
need something alive to write it, which is exactly what is missing.
"""

from __future__ import annotations

from typing import Literal

FirstBootStatus = Literal[
    "PENDING",
    "READY",
    "RUNNING",
    "PAUSED",
    "BLOCKED_RETRYABLE",
    "BLOCKED_FATAL",
    "AUDITING",
    "COMPLETE",
]

#: Why it stopped. Retryable means "we could not", fatal means "it is wrong".
BlockKind = Literal[
    "",
    "model_unavailable",
    "critic_unavailable",
    "incomplete",
    "landing_failed",
    "contradiction",
    "audit_failed",
    "retry_exhausted",
]

#: Block kinds that a resume may sensibly retry.
RETRYABLE_BLOCKS: frozenset[str] = frozenset(
    {"model_unavailable", "critic_unavailable", "incomplete", "landing_failed"}
)

#: Block kinds that will produce the same answer next time. A resume is
#: refused; the OWNER has to look, and repair or reset.
#:
#: ``audit_failed`` is deliberately *not* here. An audit reads state that a
#: further resume can still fill in — a run that stopped before the last month
#: fails ``coverage`` without anything being wrong with what it did generate.
#: ``retry_exhausted`` is the budget of point 23: enough retryable attempts in
#: a row stops being "we could not" and becomes "this does not work".
FATAL_BLOCKS: frozenset[str] = frozenset({"contradiction", "retry_exhausted"})

#: How many attempts a retryable block gets before it is treated as fatal
#: (point 23). Without a budget, `model_unavailable` is an infinite loop that
#: burns a nineteen-year generation's worth of calls against a dead Ollama.
MAX_ATTEMPTS: int = 8

#: Statuses a `first-boot start` may be issued from.
STARTABLE: frozenset[str] = frozenset({"PENDING", "READY"})

#: Statuses a `first-boot resume` may be issued from. RUNNING is included on
#: purpose: a crashed run leaves RUNNING behind, and refusing to resume it
#: would be a deadlock only a database edit could break (26).
RESUMABLE: frozenset[str] = frozenset(
    {"RUNNING", "PAUSED", "BLOCKED_RETRYABLE", "AUDITING"}
)

#: Statuses in which the character plane stays shut. Everything except one.
CHARACTER_PLANE_OPEN: frozenset[str] = frozenset({"COMPLETE"})


def is_terminal(status: str) -> bool:
    return status in ("COMPLETE", "BLOCKED_FATAL")


def block_status(kind: str) -> FirstBootStatus:
    """Which blocked status a block kind produces."""
    return "BLOCKED_FATAL" if kind in FATAL_BLOCKS else "BLOCKED_RETRYABLE"


def character_plane_open(status: str) -> bool:
    """Whether YUI may receive a message from the USER.

    The hard gate of point 9 and 11, in one function so there is one answer.
    """
    return status in CHARACTER_PLANE_OPEN


__all__ = [
    "CHARACTER_PLANE_OPEN",
    "FATAL_BLOCKS",
    "MAX_ATTEMPTS",
    "RESUMABLE",
    "RETRYABLE_BLOCKS",
    "STARTABLE",
    "BlockKind",
    "FirstBootStatus",
    "block_status",
    "character_plane_open",
    "is_terminal",
]
