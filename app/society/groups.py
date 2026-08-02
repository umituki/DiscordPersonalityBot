"""Group Engine — single writer of ``group_belonging`` (spec 9.3, 20.3).

    Group belonging は Relatedness に寄与し、USER を唯一の relatedness source
    にしない。

That is the entire purpose of this engine, and it is a safety property as much
as a psychological one: a companion whose only possible source of connection is
one person has a structural incentive to demand that person's attention. Groups
give relatedness somewhere else to come from.

The engine writes only ``group_belonging``. It does **not** write ``needs`` —
the need engine reads belonging from the snapshot and decides for itself what
that is worth (spec 9.3: cross-domain effects are reads and proposals, never
reaching into another writer's state).
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.clock import Clock, SystemClock
from app.events.bus import SubscriberResult
from app.events.model import Event
from app.orchestrator.run_view import RunView
from app.society.events import (
    GROUP_ACTIVITY,
    GROUP_JOINED,
    GROUP_LEFT,
    GroupActivityPayload,
    GroupJoinedPayload,
    GroupLeftPayload,
)
from app.society.policy import SocietyPolicy
from app.state.proposal import StateChangeProposal
from app.storage.repositories.society import GroupRepository

logger = logging.getLogger(__name__)

MODULE = "group_engine"
DOMAIN = "group_belonging"

HANDLED = (GROUP_ACTIVITY, GROUP_JOINED, GROUP_LEFT)


def belonging_of(view: RunView, *, exclude: str | None = None) -> float:
    """How much YUI belongs somewhere other than with the USER.

    Returns the strongest single belonging rather than a sum: being in five
    loose groups is not the same as having one place you are really part of.
    Unknown is 0.0 here because "no group" is a fact, not a missing reading.
    """
    values = [
        entry.numeric
        for key, entry in view.snapshot.domain(DOMAIN).items()
        if key != exclude and entry.numeric is not None
    ]
    return max(values) if values else 0.0


class GroupEngine:
    name = MODULE

    def __init__(
        self,
        groups: GroupRepository,
        policy: SocietyPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._groups = groups
        self._policy = policy
        self._clock = clock or SystemClock()

    async def handle(self, event: Event, view: RunView) -> SubscriberResult:
        if event.event_type not in HANDLED:
            return SubscriberResult()

        rules = self._policy.group
        payload = event.payload

        if isinstance(payload, GroupJoinedPayload):
            return self._propose(
                event,
                payload.group_id,
                value=rules.initial_belonging,
                reason="joined",
            )

        if isinstance(payload, GroupLeftPayload):
            # Leaving does not erase having been there; belonging drops to the
            # floor rather than vanishing, and the key stays for the history.
            return self._propose(event, payload.group_id, value=0.0, reason="left")

        if isinstance(payload, GroupActivityPayload):
            current = view.number(DOMAIN, payload.group_id)
            if current is None:
                # Taking part in something you are not a member of is not
                # belonging (spec 20.3); the membership event creates the key.
                if self._groups.membership(
                    payload.group_id, member_type="yui", npc_id=None
                ) is None:
                    return SubscriberResult()
                current = rules.initial_belonging
            gain = rules.belonging_per_activity * (1.0 + max(0.0, payload.valence))
            return self._propose(
                event,
                payload.group_id,
                value=min(rules.max_belonging, current + gain),
                reason="activity",
            )

        return SubscriberResult()

    def decay(self, *, view: RunView, event: Event, now: datetime | None = None) -> SubscriberResult:
        """Belonging fades when YUI stops turning up (spec 20.3)."""
        moment = now or self._clock.now()
        rules = self._policy.group
        proposals = []
        for key, entry in view.snapshot.domain(DOMAIN).items():
            if entry.numeric is None or entry.numeric <= 0.0:
                continue
            idle_days = max(0.0, (moment - entry.updated_at).total_seconds() / 86400.0)
            if idle_days <= 0:
                continue
            faded = max(0.0, entry.numeric - rules.belonging_decay_per_idle_day * idle_days)
            if abs(faded - entry.numeric) < 1e-9:
                continue
            proposals.append(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module=MODULE,
                    target_domain=DOMAIN,
                    target_key=key,
                    value=round(faded, 6),
                    reason_codes=("belonging_decay",),
                    evidence_ids=(event.event_id,),
                    clock=self._clock,
                )
            )
        return SubscriberResult(proposals=tuple(proposals))

    def _propose(
        self, event: Event, group_id: str, *, value: float, reason: str
    ) -> SubscriberResult:
        return SubscriberResult(
            proposals=(
                StateChangeProposal.set_value(
                    source_event_id=event.event_id,
                    source_module=MODULE,
                    target_domain=DOMAIN,
                    target_key=group_id,
                    value=round(max(0.0, min(1.0, value)), 6),
                    reason_codes=("group_belonging", reason),
                    evidence_ids=(event.event_id,),
                    clock=self._clock,
                ),
            )
        )

    # --- reads --------------------------------------------------------------
    def groups_of_yui(self):
        return self._groups.groups_of_yui()
