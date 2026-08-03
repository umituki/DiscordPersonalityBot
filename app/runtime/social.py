"""NPCs and groups, connected to the runtime (rebuild spec 29, 30 — Phase 8).

§30 states the reason this phase exists at all:

    USER だけを唯一の relatedness source にしない。

If the only person who can meet a social need is the USER, then every quiet
day is a deficit and every conversation is relief — which is not a life, it is
a dependency. Groups and NPCs are how the need gets met by her own world.

The tiering rule from §29.1 is load-bearing and easy to lose: 全 NPC を完全
Agent にしない. Tier 0 people exist as names and attributes and nothing else;
Tier 1 recur; only Tier 2 carry a subjective ``NPCModel`` YUI can be wrong
about (§29.4). This module respects that by only *contacting* people who are
Tier 1 or above — a background participant is scenery, and scenery does not get
phoned.

§29.5's other half is also structural here: the model may propose an interaction
candidate, but ``Decision / SocietyService が commit した時だけ本当に起きる``.
The sources below produce opportunities and the Society Service commits; nothing
in between can make an interaction real.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Sequence

from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.world.models import Opportunity

logger = logging.getLogger(__name__)

MODULE = "social_runtime"

NPC_CONTACT = "npc_contact"
GROUP_ACTIVITY_DUE = "group_activity_due"

CONTACT_NPC = "contact_npc"
ATTEND_GROUP = "attend_group"

#: §29.1: Tier 0 is background. You do not arrange to meet the scenery.
CONTACTABLE_TIER = 1

#: How long between contacting the same person. Availability is a property of
#: theirs, not a licence to appear every hour.
CONTACT_COOLDOWN_HOURS = 20.0

#: Groups meet on a rhythm rather than continuously.
GROUP_INTERVAL_HOURS = 72.0

GROUP_ACTIVITY_MINUTES = 90.0


class NPCSource:
    """Someone she could get in touch with (§29.1, 29.3). Reads only."""

    name = "npcs"

    def __init__(
        self,
        society: Any,
        world: Any,
        *,
        clock: Clock | None = None,
        cooldown_hours: float = CONTACT_COOLDOWN_HOURS,
    ) -> None:
        self._society = society
        self._world = world
        self._clock = clock or SystemClock()
        self._cooldown = timedelta(hours=cooldown_hours)

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world.current_sleep() is not None:
            return []
        if self._world.current_activity() is not None:
            return []

        recent = {
            interaction.npc_id: interaction.occurred_at
            for interaction in self._society.recent_interactions(limit=50)
        }
        opportunities: list[Opportunity] = []
        for npc in self._society.people(min_tier=CONTACTABLE_TIER):
            if npc.status != "active":
                continue
            last = recent.get(npc.npc_id)
            if last is not None and now - last < self._cooldown:
                continue
            opportunities.append(
                Opportunity(
                    kind=NPC_CONTACT,
                    detail=npc.npc_id,
                    # Their availability, not her wish. Someone who is rarely
                    # around is rarely an opportunity, however much she likes
                    # them — which is what makes them a person rather than a
                    # button.
                    urgency=round(min(1.0, npc.availability), 6),
                    created_at=now,
                )
            )
        return opportunities

    def next_due(self, now: datetime) -> datetime | None:
        return None  # nothing scheduled; the idle interval applies


class GroupSource:
    """A group she belongs to, due to meet (§30). Reads only."""

    name = "groups"

    def __init__(
        self,
        society: Any,
        world: Any,
        *,
        clock: Clock | None = None,
        interval_hours: float = GROUP_INTERVAL_HOURS,
    ) -> None:
        self._society = society
        self._world = world
        self._clock = clock or SystemClock()
        self._interval = timedelta(hours=interval_hours)

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world.current_sleep() is not None:
            return []
        if self._world.current_activity() is not None:
            return []

        last_by_group: dict[str, datetime] = {}
        for interaction in self._society.recent_interactions(limit=100):
            group_id = getattr(interaction, "group_id", None)
            if group_id and group_id not in last_by_group:
                last_by_group[group_id] = interaction.occurred_at

        opportunities: list[Opportunity] = []
        for group in self._society.groups_of_yui():
            if group.status != "active":
                continue
            last = last_by_group.get(group.group_id)
            if last is not None and now - last < self._interval:
                continue
            opportunities.append(
                Opportunity(
                    kind=GROUP_ACTIVITY_DUE,
                    detail=group.group_id,
                    urgency=round(min(1.0, 0.3 + 0.5 * group.social_density), 6),
                    created_at=now,
                )
            )
        return opportunities

    def next_due(self, now: datetime) -> datetime | None:
        return None


class SocialCandidates:
    """What seeing someone would be worth (spec 23).

    Read against her actual social need rather than a constant, so that a
    stretch of solitude makes company matter more — which is the mechanism
    §30 is asking for, and the reason the USER stops being the only source of
    relatedness rather than merely being one of several on paper.
    """

    name = "social_candidates"

    def __init__(self, society: Any, state: Any) -> None:
        self._society = society
        self._state = state

    def npc_contact(
        self, opportunity: Opportunity, now: datetime
    ) -> ActionCandidate | None:
        npc = self._society.npc(opportunity.detail)
        if npc is None or npc.status != "active":
            return None
        warmth = 0.3 + 0.4 * npc.warmth
        return ActionCandidate(
            action=CONTACT_NPC,
            route="reactive",
            expected_value=round(
                max(0.0, min(0.95, warmth * (0.6 + 0.8 * self._relatedness_need()))), 6
            ),
            reason=f"{npc.name}に連絡してみる",
        )

    def group_activity(
        self, opportunity: Opportunity, now: datetime
    ) -> ActionCandidate | None:
        group = next(
            (
                item
                for item in self._society.groups_of_yui()
                if item.group_id == opportunity.detail
            ),
            None,
        )
        if group is None:
            return None
        return ActionCandidate(
            action=ATTEND_GROUP,
            route="goal_directed",
            expected_value=round(
                max(
                    0.0,
                    min(
                        0.95,
                        (0.3 + 0.4 * group.warmth) * (0.6 + 0.8 * self._relatedness_need()),
                    ),
                ),
                6,
            ),
            reason=f"{group.name}の集まり",
        )

    def _relatedness_need(self) -> float:
        entry = self._state.get("needs", "relatedness")
        if entry is None:
            return 0.4
        # The stored value is satisfaction; what makes company attractive is
        # the shortfall.
        return max(0.0, min(1.0, 1.0 - float(entry.numeric or 0.0)))


class SocialActions:
    """Meets people. The Society Service commits; nothing else may (§29.5)."""

    name = "social_actions"

    def __init__(
        self,
        *,
        society: Any,
        world: Any,
        processor: Any,
        candidates: SocialCandidates,
        clock: Clock | None = None,
    ) -> None:
        self._society = society
        self._world = world
        self._processor = processor
        self._candidates = candidates
        self._clock = clock or SystemClock()

    async def contact_npc(self, candidate: ActionCandidate, now: datetime) -> bool:
        npc = self._npc_named(candidate.reason)
        if npc is None:
            return False
        record = self._society.interact(
            npc.npc_id,
            kind="conversation",
            valence=round(0.2 + 0.4 * npc.warmth, 4),
            summary=f"{npc.name}と話した",
        )
        await self._processor.process(record.event)
        # §29.2: repetition promotes. Someone she keeps returning to stops
        # being background, and the tier is the record of that rather than a
        # judgement made once at creation.
        self._maybe_promote(npc)
        return True

    async def attend_group(self, candidate: ActionCandidate, now: datetime) -> bool:
        group = next(
            (
                item
                for item in self._society.groups_of_yui()
                if item.name in candidate.reason
            ),
            None,
        )
        if group is None:
            return False

        # §29.2: a group activity needing background participants is exactly
        # when Tier 0 people come into existence. YUI's own membership has no
        # npc_id, so it is not a participant in the list of *other* people.
        participants = tuple(
            member.npc_id
            for member in self._society.members(group.group_id)
            if getattr(member, "npc_id", None)
        )
        if not participants:
            return False

        event = self._society.group_activity(
            group.group_id,
            activity=f"{group.name}の活動",
            valence=round(0.1 + 0.4 * group.warmth, 4),
            participants=participants,
        )
        await self._processor.process(event)
        # Attending is doing something, so it is an activity like any other and
        # finishes through spec 24.2's lifecycle rather than a private one.
        self._world.start_activity(
            name=f"{group.name}の活動",
            kind="social",
            expected_minutes=GROUP_ACTIVITY_MINUTES,
        )
        return True

    def register(self, registry: Any, *, sources: Sequence[Any] = ()) -> None:
        for source in sources:
            registry.add_source(source)
        registry.add_builder(NPC_CONTACT, self._candidates.npc_contact)
        registry.add_builder(GROUP_ACTIVITY_DUE, self._candidates.group_activity)
        registry.add_handler(CONTACT_NPC, self.contact_npc)
        registry.add_handler(ATTEND_GROUP, self.attend_group)

    # --- internals -----------------------------------------------------------
    def _npc_named(self, reason: str) -> Any | None:
        for npc in self._society.people(min_tier=CONTACTABLE_TIER):
            if npc.name and npc.name in reason:
                return npc
        return None

    def _maybe_promote(self, npc: Any) -> None:
        """§29.2/29.3: 繰り返し関われば promote.

        Counted from the interaction rows rather than a tally kept somewhere
        else, so the promotion survives a restart and can be argued with.
        """
        if npc.tier >= 2:
            return
        history = self._society.interactions_with(npc.npc_id, limit=20)
        if len(history) >= 5:
            self._society.promote(npc.npc_id, tier=2)
            logger.info("npc promoted to tier 2 name=%s", npc.name)


__all__ = [
    "ATTEND_GROUP",
    "CONTACTABLE_TIER",
    "CONTACT_NPC",
    "GROUP_ACTIVITY_DUE",
    "NPC_CONTACT",
    "GroupSource",
    "NPCSource",
    "SocialActions",
    "SocialCandidates",
]
