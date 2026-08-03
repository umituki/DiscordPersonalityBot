"""Knowledge events (rebuild spec 32 — Phase 11).

Two events, deliberately, because they are two different facts:

``SEARCH_PERFORMED``
    she looked something up. True whatever came back — including nothing, and
    including a provider outage. It says a search happened, not that it worked.

``KNOWLEDGE_ACQUIRED``
    something got past attention, comprehension and retention and is now
    hers. One of these per thing learned, which is normally far fewer than the
    number of results.

Collapsing them into one event would make "she searched" and "she now knows"
indistinguishable in the timeline, which is precisely the confusion this phase
exists to prevent.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

SEARCH_PERFORMED = "SEARCH_PERFORMED"
KNOWLEDGE_ACQUIRED = "KNOWLEDGE_ACQUIRED"


@register_payload(SEARCH_PERFORMED)
class SearchPerformedPayload(EventPayload):
    search_id: str
    query: str
    #: success / no_results / ambiguous_results / timeout / network_error /
    #: provider_error / refused. `no_results` is not a claim about the world.
    outcome: str
    provider: str = ""
    results: int = 0
    #: How many were refused for being newer than the moment she searched from.
    future_rejected: int = 0
    acquired: int = 0
    topic: str = ""


@register_payload(KNOWLEDGE_ACQUIRED)
class KnowledgeAcquiredPayload(EventPayload):
    knowledge_id: str
    search_id: str | None = None
    topic: str = ""


__all__ = [
    "KNOWLEDGE_ACQUIRED",
    "SEARCH_PERFORMED",
    "KnowledgeAcquiredPayload",
    "SearchPerformedPayload",
]
