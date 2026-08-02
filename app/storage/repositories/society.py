"""Virtual society persistence (spec 20, 31.8)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Sequence

from app import ids
from app.clock import from_iso, to_iso
from app.society.models import (
    NPC,
    Group,
    GroupMembership,
    NPCInteraction,
    NPCModel,
    NPCRelationship,
    SocialLink,
)
from app.storage.database import Database

NPC_ID = "npc"
MODEL = "nmd"
RELATIONSHIP = "nrl"
GROUP = "grp"
MEMBERSHIP = "gmb"
LINK = "lnk"
INTERACTION = "nit"


class NPCRepository:
    """The objective profile of every person in YUI's virtual life."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        *,
        name: str,
        tier: int,
        role: str = "",
        traits: dict[str, Any] | None = None,
        availability: float = 0.5,
        warmth: float = 0.5,
        reliability: float = 0.5,
        now: datetime,
    ) -> NPC:
        npc_id = ids.new_id(NPC_ID)
        self._db.execute(
            """
            INSERT INTO npcs
                (npc_id, name, tier, role, traits_json, availability, warmth, reliability,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                npc_id, name, tier, role,
                json.dumps(traits or {}, ensure_ascii=False),
                availability, warmth, reliability, to_iso(now), to_iso(now),
            ),
        )
        return self.get(npc_id)  # type: ignore[return-value]

    def set_tier(self, npc_id: str, tier: int, *, now: datetime) -> NPC:
        self._db.execute(
            "UPDATE npcs SET tier = ?, updated_at = ? WHERE npc_id = ?",
            (tier, to_iso(now), npc_id),
        )
        return self.get(npc_id)  # type: ignore[return-value]

    def set_status(self, npc_id: str, status: str, *, now: datetime) -> NPC:
        self._db.execute(
            "UPDATE npcs SET status = ?, updated_at = ? WHERE npc_id = ?",
            (status, to_iso(now), npc_id),
        )
        return self.get(npc_id)  # type: ignore[return-value]

    def get(self, npc_id: str) -> NPC | None:
        row = self._db.query_one("SELECT * FROM npcs WHERE npc_id = ?", (npc_id,))
        return None if row is None else _to_npc(row)

    def by_name(self, name: str) -> NPC | None:
        row = self._db.query_one("SELECT * FROM npcs WHERE name = ?", (name,))
        return None if row is None else _to_npc(row)

    def all(self, *, min_tier: int = 0, limit: int = 200) -> list[NPC]:
        rows = self._db.query_all(
            "SELECT * FROM npcs WHERE tier >= ? AND status = 'active' "
            "ORDER BY tier DESC, name LIMIT ?",
            (min_tier, limit),
        )
        return [_to_npc(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM npcs") or 0)


class NPCModelRepository:
    """What YUI believes about an NPC. Never the same table as the truth."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def ensure(self, npc_id: str, *, now: datetime) -> NPCModel:
        self._db.execute(
            "INSERT OR IGNORE INTO npc_models (model_id, npc_id, updated_at) VALUES (?, ?, ?)",
            (ids.new_id(MODEL), npc_id, to_iso(now)),
        )
        return self.for_npc(npc_id)  # type: ignore[return-value]

    def update(
        self,
        npc_id: str,
        *,
        perceived_warmth: float,
        perceived_reliability: float,
        perceived_availability: float,
        observation_count: int,
        confidence: float,
        now: datetime,
    ) -> NPCModel:
        self._db.execute(
            """
            UPDATE npc_models
               SET perceived_warmth = ?, perceived_reliability = ?,
                   perceived_availability = ?, observation_count = ?, confidence = ?,
                   updated_at = ?
             WHERE npc_id = ?
            """,
            (
                perceived_warmth, perceived_reliability, perceived_availability,
                observation_count, confidence, to_iso(now), npc_id,
            ),
        )
        return self.for_npc(npc_id)  # type: ignore[return-value]

    def for_npc(self, npc_id: str) -> NPCModel | None:
        row = self._db.query_one("SELECT * FROM npc_models WHERE npc_id = ?", (npc_id,))
        return None if row is None else _to_model(row)

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM npc_models") or 0)


class NPCRelationshipRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def ensure(self, npc_id: str, *, now: datetime) -> NPCRelationship:
        self._db.execute(
            "INSERT OR IGNORE INTO npc_relationships (relationship_id, npc_id, updated_at) "
            "VALUES (?, ?, ?)",
            (ids.new_id(RELATIONSHIP), npc_id, to_iso(now)),
        )
        return self.for_npc(npc_id)  # type: ignore[return-value]

    def update(
        self,
        npc_id: str,
        *,
        stage: str,
        previous_stage: str,
        familiarity: float,
        closeness: float,
        conflict: float,
        interaction_count: int,
        last_contact_at: datetime | None,
        now: datetime,
        first_met_at: datetime | None = None,
    ) -> NPCRelationship:
        self._db.execute(
            """
            UPDATE npc_relationships
               SET stage = ?, previous_stage = ?, familiarity = ?, closeness = ?,
                   conflict = ?, interaction_count = ?, last_contact_at = ?,
                   first_met_at = COALESCE(first_met_at, ?), updated_at = ?
             WHERE npc_id = ?
            """,
            (
                stage, previous_stage, familiarity, closeness, conflict, interaction_count,
                None if last_contact_at is None else to_iso(last_contact_at),
                None if first_met_at is None else to_iso(first_met_at),
                to_iso(now), npc_id,
            ),
        )
        return self.for_npc(npc_id)  # type: ignore[return-value]

    def end(self, npc_id: str, *, reason: str, now: datetime) -> NPCRelationship:
        """Ending is a deliberate act, so it has its own method (spec 20.4)."""
        current = self.for_npc(npc_id)
        self._db.execute(
            "UPDATE npc_relationships SET stage = 'ended', previous_stage = ?, "
            "ended_at = ?, ended_reason = ?, updated_at = ? WHERE npc_id = ?",
            (
                "" if current is None else current.stage,
                to_iso(now), reason, to_iso(now), npc_id,
            ),
        )
        return self.for_npc(npc_id)  # type: ignore[return-value]

    def reopen(self, npc_id: str, *, now: datetime) -> NPCRelationship:
        self._db.execute(
            "UPDATE npc_relationships SET stage = 'reconnected', previous_stage = 'ended', "
            "ended_at = NULL, ended_reason = '', updated_at = ? WHERE npc_id = ?",
            (to_iso(now), npc_id),
        )
        return self.for_npc(npc_id)  # type: ignore[return-value]

    def for_npc(self, npc_id: str) -> NPCRelationship | None:
        row = self._db.query_one("SELECT * FROM npc_relationships WHERE npc_id = ?", (npc_id,))
        return None if row is None else _to_relationship(row)

    def all(self, *, limit: int = 200) -> list[NPCRelationship]:
        rows = self._db.query_all(
            "SELECT * FROM npc_relationships ORDER BY closeness DESC LIMIT ?", (limit,)
        )
        return [_to_relationship(row) for row in rows]

    def in_stage(self, stage: str, *, limit: int = 100) -> list[NPCRelationship]:
        rows = self._db.query_all(
            "SELECT * FROM npc_relationships WHERE stage = ? LIMIT ?", (stage, limit)
        )
        return [_to_relationship(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM npc_relationships") or 0)


class GroupRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        *,
        name: str,
        activity_type: str = "",
        norms: Sequence[str] = (),
        social_density: float = 0.5,
        competitiveness: float = 0.5,
        warmth: float = 0.5,
        stability: float = 0.5,
        now: datetime,
    ) -> Group:
        group_id = ids.new_id(GROUP)
        self._db.execute(
            """
            INSERT INTO npc_groups
                (group_id, name, activity_type, norms_json, social_density, competitiveness,
                 warmth, stability, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id, name, activity_type, json.dumps(list(norms), ensure_ascii=False),
                social_density, competitiveness, warmth, stability, to_iso(now), to_iso(now),
            ),
        )
        return self.get(group_id)  # type: ignore[return-value]

    def get(self, group_id: str) -> Group | None:
        row = self._db.query_one("SELECT * FROM npc_groups WHERE group_id = ?", (group_id,))
        return None if row is None else _to_group(row)

    def by_name(self, name: str) -> Group | None:
        row = self._db.query_one("SELECT * FROM npc_groups WHERE name = ?", (name,))
        return None if row is None else _to_group(row)

    def all(self, *, limit: int = 50) -> list[Group]:
        rows = self._db.query_all(
            "SELECT * FROM npc_groups WHERE status = 'active' ORDER BY name LIMIT ?", (limit,)
        )
        return [_to_group(row) for row in rows]

    def join(
        self,
        group_id: str,
        *,
        member_type: str,
        npc_id: str | None,
        role: str = "member",
        now: datetime,
    ) -> GroupMembership:
        membership_id = ids.new_id(MEMBERSHIP)
        self._db.execute(
            """
            INSERT OR IGNORE INTO group_memberships
                (membership_id, group_id, member_type, npc_id, role, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (membership_id, group_id, member_type, npc_id, role, to_iso(now)),
        )
        return self.membership(group_id, member_type=member_type, npc_id=npc_id)  # type: ignore

    def leave(
        self, group_id: str, *, member_type: str, npc_id: str | None, now: datetime
    ) -> None:
        self._db.execute(
            "UPDATE group_memberships SET status = 'left', left_at = ? "
            "WHERE group_id = ? AND member_type = ? AND npc_id IS ?",
            (to_iso(now), group_id, member_type, npc_id),
        )

    def membership(
        self, group_id: str, *, member_type: str, npc_id: str | None
    ) -> GroupMembership | None:
        row = self._db.query_one(
            "SELECT * FROM group_memberships WHERE group_id = ? AND member_type = ? "
            "AND npc_id IS ?",
            (group_id, member_type, npc_id),
        )
        return None if row is None else _to_membership(row)

    def members(self, group_id: str) -> list[GroupMembership]:
        rows = self._db.query_all(
            "SELECT * FROM group_memberships WHERE group_id = ? AND status = 'active'",
            (group_id,),
        )
        return [_to_membership(row) for row in rows]

    def groups_of_yui(self) -> list[Group]:
        rows = self._db.query_all(
            "SELECT g.* FROM npc_groups g JOIN group_memberships m ON m.group_id = g.group_id "
            "WHERE m.member_type = 'yui' AND m.status = 'active' AND g.status = 'active'"
        )
        return [_to_group(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM npc_groups") or 0)


class SocialLinkRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def link(
        self,
        *,
        from_npc_id: str,
        to_npc_id: str,
        kind: str = "acquaintance",
        strength: float = 0.3,
        now: datetime,
    ) -> SocialLink:
        self._db.execute(
            """
            INSERT INTO npc_social_links
                (link_id, from_npc_id, to_npc_id, kind, strength, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (from_npc_id, to_npc_id)
            DO UPDATE SET kind = excluded.kind, strength = excluded.strength,
                          updated_at = excluded.updated_at
            """,
            (
                ids.new_id(LINK), from_npc_id, to_npc_id, kind, strength,
                to_iso(now), to_iso(now),
            ),
        )
        return self.between(from_npc_id, to_npc_id)  # type: ignore[return-value]

    def between(self, from_npc_id: str, to_npc_id: str) -> SocialLink | None:
        row = self._db.query_one(
            "SELECT * FROM npc_social_links WHERE from_npc_id = ? AND to_npc_id = ?",
            (from_npc_id, to_npc_id),
        )
        return None if row is None else _to_link(row)

    def neighbours(self, npc_id: str) -> list[SocialLink]:
        rows = self._db.query_all(
            "SELECT * FROM npc_social_links WHERE from_npc_id = ? OR to_npc_id = ?",
            (npc_id, npc_id),
        )
        return [_to_link(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM npc_social_links") or 0)


class NPCInteractionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        npc_id: str,
        group_id: str | None,
        kind: str,
        valence: float,
        summary: str,
        occurred_at: datetime,
        event_id: str | None = None,
    ) -> NPCInteraction:
        interaction_id = ids.new_id(INTERACTION)
        self._db.execute(
            """
            INSERT INTO npc_interactions
                (interaction_id, npc_id, group_id, kind, valence, summary, occurred_at, event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id, npc_id, group_id, kind, valence, summary,
                to_iso(occurred_at), event_id,
            ),
        )
        return self.get(interaction_id)  # type: ignore[return-value]

    def get(self, interaction_id: str) -> NPCInteraction | None:
        row = self._db.query_one(
            "SELECT * FROM npc_interactions WHERE interaction_id = ?", (interaction_id,)
        )
        return None if row is None else _to_interaction(row)

    def for_npc(self, npc_id: str, *, limit: int = 50) -> list[NPCInteraction]:
        rows = self._db.query_all(
            "SELECT * FROM npc_interactions WHERE npc_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (npc_id, limit),
        )
        return [_to_interaction(row) for row in rows]

    def recent(self, *, limit: int = 50) -> list[NPCInteraction]:
        rows = self._db.query_all(
            "SELECT * FROM npc_interactions ORDER BY occurred_at DESC LIMIT ?", (limit,)
        )
        return [_to_interaction(row) for row in rows]

    def count(self) -> int:
        return int(self._db.scalar("SELECT COUNT(*) FROM npc_interactions") or 0)


# --- row mapping -----------------------------------------------------------
def _optional_dt(value: str | None) -> datetime | None:
    return None if value is None else from_iso(value)


def _to_npc(row: sqlite3.Row) -> NPC:
    return NPC(
        npc_id=row["npc_id"],
        name=row["name"],
        tier=int(row["tier"]),
        role=row["role"],
        traits=json.loads(row["traits_json"] or "{}"),
        availability=row["availability"],
        warmth=row["warmth"],
        reliability=row["reliability"],
        status=row["status"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        origin=row["origin"],
    )


def _to_model(row: sqlite3.Row) -> NPCModel:
    return NPCModel(
        model_id=row["model_id"],
        npc_id=row["npc_id"],
        perceived_warmth=row["perceived_warmth"],
        perceived_reliability=row["perceived_reliability"],
        perceived_availability=row["perceived_availability"],
        observation_count=int(row["observation_count"]),
        confidence=row["confidence"],
        updated_at=from_iso(row["updated_at"]),
    )


def _to_relationship(row: sqlite3.Row) -> NPCRelationship:
    return NPCRelationship(
        relationship_id=row["relationship_id"],
        npc_id=row["npc_id"],
        stage=row["stage"],
        previous_stage=row["previous_stage"],
        familiarity=row["familiarity"],
        closeness=row["closeness"],
        conflict=row["conflict"],
        interaction_count=int(row["interaction_count"]),
        first_met_at=_optional_dt(row["first_met_at"]),
        last_contact_at=_optional_dt(row["last_contact_at"]),
        ended_at=_optional_dt(row["ended_at"]),
        ended_reason=row["ended_reason"],
        updated_at=from_iso(row["updated_at"]),
    )


def _to_group(row: sqlite3.Row) -> Group:
    return Group(
        group_id=row["group_id"],
        name=row["name"],
        activity_type=row["activity_type"],
        norms=tuple(json.loads(row["norms_json"] or "[]")),
        social_density=row["social_density"],
        competitiveness=row["competitiveness"],
        warmth=row["warmth"],
        stability=row["stability"],
        status=row["status"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _to_membership(row: sqlite3.Row) -> GroupMembership:
    return GroupMembership(
        membership_id=row["membership_id"],
        group_id=row["group_id"],
        member_type=row["member_type"],
        npc_id=row["npc_id"],
        role=row["role"],
        joined_at=from_iso(row["joined_at"]),
        left_at=_optional_dt(row["left_at"]),
        status=row["status"],
    )


def _to_link(row: sqlite3.Row) -> SocialLink:
    return SocialLink(
        link_id=row["link_id"],
        from_npc_id=row["from_npc_id"],
        to_npc_id=row["to_npc_id"],
        kind=row["kind"],
        strength=row["strength"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _to_interaction(row: sqlite3.Row) -> NPCInteraction:
    return NPCInteraction(
        interaction_id=row["interaction_id"],
        npc_id=row["npc_id"],
        group_id=row["group_id"],
        kind=row["kind"],
        valence=row["valence"],
        summary=row["summary"],
        occurred_at=from_iso(row["occurred_at"]),
        event_id=row["event_id"],
        origin=row["origin"],
    )
