"""Society Service — the objective record of YUI's virtual social world.

Spec 20.2 separates two things that are easy to conflate::

    NPC Objective Profile   ≠   YUI's Model of NPC

This service owns the first one: who exists, what tier they are, which groups
exist, who knows whom, and what actually happened. It is the society's
equivalent of the World Service — facts, not feelings.

It never touches YUI's psychology. What an interaction *means* is decided by
:mod:`app.society.relationships` and the normal psychology pipeline, from the
event this service produces.

Two rules it enforces on the way in:

* An NPC is never the USER (spec 1.2, 2.10). Nothing here accepts a Discord
  identity, and every record it writes is ``virtual_life`` origin.
* Not every NPC is a full agent (spec 20.1). Tier decides what may exist:
  tier 0 people have no relationship state and no model at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from app.clock import Clock, SystemClock
from app.events.model import Event
from app.society.events import (
    GROUP_ACTIVITY,
    GROUP_JOINED,
    GROUP_LEFT,
    NPC_INTERACTION,
    GroupActivityPayload,
    GroupJoinedPayload,
    GroupLeftPayload,
    NPCInteractionPayload,
)
from app.society.models import NPC, Group, NPCInteraction, SocialLink
from app.society.policy import SocietyPolicy
from app.storage.repositories.society import (
    GroupRepository,
    NPCInteractionRepository,
    NPCRelationshipRepository,
    NPCRepository,
    SocialLinkRepository,
)

logger = logging.getLogger(__name__)

MODULE = "society_service"

YUI_MEMBER = "yui"
NPC_MEMBER = "npc"


class SocietyError(ValueError):
    """Raised when a society operation would break a separation rule."""


@dataclass(frozen=True, slots=True)
class InteractionRecord:
    """What happened, and the event that says so."""

    interaction: NPCInteraction
    npc: NPC
    event: Event


class SocietyService:
    name = MODULE

    def __init__(
        self,
        *,
        npcs: NPCRepository,
        relationships: NPCRelationshipRepository,
        groups: GroupRepository,
        links: SocialLinkRepository,
        interactions: NPCInteractionRepository,
        policy: SocietyPolicy,
        clock: Clock | None = None,
    ) -> None:
        self._npcs = npcs
        self._relationships = relationships
        self._groups = groups
        self._links = links
        self._interactions = interactions
        self._policy = policy
        self._clock = clock or SystemClock()

    # --- people (spec 20.1, 20.2) ------------------------------------------
    def introduce(
        self,
        *,
        name: str,
        tier: int = 0,
        role: str = "",
        traits: dict[str, Any] | None = None,
        availability: float = 0.5,
        warmth: float = 0.5,
        reliability: float = 0.5,
    ) -> NPC:
        """Add a person to the virtual world. Never the USER (spec 1.2)."""
        if not name.strip():
            raise SocietyError("an NPC needs a name")
        existing = self._npcs.by_name(name)
        if existing is not None:
            return existing
        now = self._clock.now()
        npc = self._npcs.create(
            name=name,
            tier=max(0, min(2, tier)),
            role=role,
            traits=traits,
            availability=availability,
            warmth=warmth,
            reliability=reliability,
            now=now,
        )
        if npc.keeps_relationship_state:
            self._relationships.ensure(npc.npc_id, now=now)
        logger.info("npc introduced name=%s tier=%d", npc.name, npc.tier)
        return npc

    def promote(self, npc_id: str, *, tier: int) -> NPC:
        """A background presence can become someone who matters (spec 20.1)."""
        now = self._clock.now()
        npc = self._npcs.set_tier(npc_id, max(0, min(2, tier)), now=now)
        if npc.keeps_relationship_state:
            self._relationships.ensure(npc.npc_id, now=now)
        return npc

    def npc(self, npc_id: str) -> NPC | None:
        return self._npcs.get(npc_id)

    def by_name(self, name: str) -> NPC | None:
        return self._npcs.by_name(name)

    def people(self, *, min_tier: int = 0) -> list[NPC]:
        return self._npcs.all(min_tier=min_tier)

    def tracked_people(self) -> list[NPC]:
        return self._npcs.all(min_tier=self._policy.npc.min_tier_for_relationship)

    # --- what happened (spec 20, 18.2) --------------------------------------
    def interact(
        self,
        npc_id: str,
        *,
        kind: str = "conversation",
        valence: float = 0.0,
        summary: str = "",
        group_id: str | None = None,
        parent_event: Event | None = None,
    ) -> InteractionRecord:
        """Record an NPC interaction and the event that reports it."""
        npc = self._npcs.get(npc_id)
        if npc is None:
            raise SocietyError(f"unknown npc: {npc_id!r}")

        now = self._clock.now()
        interaction = self._interactions.record(
            npc_id=npc_id,
            group_id=group_id,
            kind=kind,
            valence=max(-1.0, min(1.0, valence)),
            summary=summary,
            occurred_at=now,
        )
        payload = NPCInteractionPayload(
            npc_id=npc.npc_id,
            npc_name=npc.name,
            tier=npc.tier,
            kind=kind,
            valence=interaction.valence,
            summary=summary,
            group_id=group_id,
        )
        event = self._event(NPC_INTERACTION, payload, parent=parent_event)
        return InteractionRecord(interaction, npc, event)

    # --- groups (spec 20.3) -------------------------------------------------
    def found_group(
        self,
        *,
        name: str,
        activity_type: str = "",
        norms: Sequence[str] = (),
        social_density: float = 0.5,
        competitiveness: float = 0.5,
        warmth: float = 0.5,
        stability: float = 0.5,
    ) -> Group:
        existing = self._groups.by_name(name)
        if existing is not None:
            return existing
        return self._groups.create(
            name=name,
            activity_type=activity_type,
            norms=norms,
            social_density=social_density,
            competitiveness=competitiveness,
            warmth=warmth,
            stability=stability,
            now=self._clock.now(),
        )

    def join_group(
        self, group_id: str, *, npc_id: str | None = None, role: str = "member",
        parent_event: Event | None = None,
    ) -> Event | None:
        """YUI (``npc_id=None``) or an NPC joins a group."""
        group = self._groups.get(group_id)
        if group is None:
            raise SocietyError(f"unknown group: {group_id!r}")
        member_type = NPC_MEMBER if npc_id else YUI_MEMBER
        self._groups.join(
            group_id,
            member_type=member_type,
            npc_id=npc_id,
            role=role,
            now=self._clock.now(),
        )
        if member_type != YUI_MEMBER:
            return None
        return self._event(
            GROUP_JOINED,
            GroupJoinedPayload(group_id=group.group_id, group_name=group.name, role=role),
            parent=parent_event,
        )

    def leave_group(
        self, group_id: str, *, npc_id: str | None = None, reason: str = "",
        parent_event: Event | None = None,
    ) -> Event | None:
        group = self._groups.get(group_id)
        if group is None:
            raise SocietyError(f"unknown group: {group_id!r}")
        member_type = NPC_MEMBER if npc_id else YUI_MEMBER
        self._groups.leave(
            group_id, member_type=member_type, npc_id=npc_id, now=self._clock.now()
        )
        if member_type != YUI_MEMBER:
            return None
        return self._event(
            GROUP_LEFT,
            GroupLeftPayload(group_id=group.group_id, group_name=group.name, reason=reason),
            parent=parent_event,
        )

    def group_activity(
        self,
        group_id: str,
        *,
        activity: str,
        valence: float = 0.0,
        participants: Sequence[str] = (),
        parent_event: Event | None = None,
    ) -> Event:
        group = self._groups.get(group_id)
        if group is None:
            raise SocietyError(f"unknown group: {group_id!r}")
        return self._event(
            GROUP_ACTIVITY,
            GroupActivityPayload(
                group_id=group.group_id,
                group_name=group.name,
                activity=activity,
                participants=tuple(participants),
                valence=max(-1.0, min(1.0, valence)),
            ),
            parent=parent_event,
        )

    def groups(self) -> list[Group]:
        return self._groups.all()

    def groups_of_yui(self) -> list[Group]:
        return self._groups.groups_of_yui()

    def members(self, group_id: str):
        return self._groups.members(group_id)

    # --- the network (spec 20) ---------------------------------------------
    def connect(
        self, from_npc_id: str, to_npc_id: str, *, kind: str = "acquaintance",
        strength: float = 0.3,
    ) -> SocialLink:
        """Two NPCs know each other. This has nothing to do with YUI."""
        if from_npc_id == to_npc_id:
            raise SocietyError("an NPC cannot be linked to itself")
        for npc_id in (from_npc_id, to_npc_id):
            if self._npcs.get(npc_id) is None:
                raise SocietyError(f"unknown npc: {npc_id!r}")
        return self._links.link(
            from_npc_id=from_npc_id,
            to_npc_id=to_npc_id,
            kind=kind,
            strength=max(0.0, min(1.0, strength)),
            now=self._clock.now(),
        )

    def neighbours(self, npc_id: str) -> list[SocialLink]:
        return self._links.neighbours(npc_id)

    # --- helpers ------------------------------------------------------------
    def _event(self, event_type: str, payload, *, parent: Event | None) -> Event:
        """Always ``npc`` actor and ``virtual_life`` origin (spec 2.10)."""
        if parent is not None:
            return parent.child(
                event_type=event_type,
                category="social",
                actor_type="npc",
                source_type=MODULE,
                clock=self._clock,
                priority="P3",
                payload=payload,
            )
        return Event.create(
            event_type=event_type,
            category="social",
            actor_type="npc",
            source_type=MODULE,
            origin="virtual_life",
            priority="P3",
            payload=payload,
            clock=self._clock,
        )

    def interactions_with(self, npc_id: str, *, limit: int = 20) -> list[NPCInteraction]:
        return self._interactions.for_npc(npc_id, limit=limit)

    def recent_interactions(self, *, limit: int = 20) -> list[NPCInteraction]:
        return self._interactions.recent(limit=limit)
