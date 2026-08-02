"""Society events (spec 8.1, 20).

Every event here is ``virtual_life`` origin and ``actor_type="npc"``. That is
not decoration: it is how an NPC conversation stays permanently distinguishable
from a real Discord message from the USER (spec 2.10, 2.16, 34.2-4).
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

NPC_INTERACTION = "NPC_INTERACTION"
NPC_RELATIONSHIP_STAGE_CHANGED = "NPC_RELATIONSHIP_STAGE_CHANGED"
GROUP_ACTIVITY = "GROUP_ACTIVITY"
GROUP_JOINED = "GROUP_JOINED"
GROUP_LEFT = "GROUP_LEFT"


@register_payload(NPC_INTERACTION)
class NPCInteractionPayload(EventPayload):
    npc_id: str
    npc_name: str
    tier: int = 0
    kind: str = "conversation"
    valence: float = 0.0
    summary: str = ""
    group_id: str | None = None


@register_payload(NPC_RELATIONSHIP_STAGE_CHANGED)
class StageChangedPayload(EventPayload):
    """Spec 20.4: which way it moved, and whether contact or trouble did it."""

    npc_id: str
    previous_stage: str
    stage: str
    reason: str = ""
    days_since_contact: float | None = None


@register_payload(GROUP_ACTIVITY)
class GroupActivityPayload(EventPayload):
    group_id: str
    group_name: str
    activity: str = ""
    participants: tuple[str, ...] = ()
    valence: float = 0.0


@register_payload(GROUP_JOINED)
class GroupJoinedPayload(EventPayload):
    group_id: str
    group_name: str
    role: str = "member"


@register_payload(GROUP_LEFT)
class GroupLeftPayload(EventPayload):
    group_id: str
    group_name: str
    reason: str = ""
