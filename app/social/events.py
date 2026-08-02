"""Internal social events (spec 8.1).

Relationship and attachment changes are recorded alongside the state they
describe, inside the same commit, so a later question of "when did trust drop,
and why" has an answer (spec 2.9, 25).
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

RELATIONSHIP_UPDATED = "RELATIONSHIP_UPDATED"
ATTACHMENT_UPDATED = "ATTACHMENT_UPDATED"


@register_payload(RELATIONSHIP_UPDATED)
class RelationshipUpdatedPayload(EventPayload):
    changes: dict[str, float]
    conflict_kind: str = "none"
    apology: bool = False
    separation_days: float = 0.0


@register_payload(ATTACHMENT_UPDATED)
class AttachmentUpdatedPayload(EventPayload):
    changes: dict[str, float]
    reason_code: str = ""
    separation_days: float = 0.0
