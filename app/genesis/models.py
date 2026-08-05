"""Genesis domain models (rebuild spec 34, 35 — Phase 12)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: 34.5. Most months are routine, and a generator that cannot say so invents
#: a crisis every four weeks. Only `meaningful` and above earn a detail call.
ImportanceClass = Literal["routine", "minor", "meaningful", "major", "turning_point"]

#: The classes worth spending a second model call on (34.5).
WORTH_DETAIL: frozenset[str] = frozenset({"meaningful", "major", "turning_point"})

#: 34.7. What the continuity ledger tracks between months.
EntityType = Literal[
    "NPC",
    "GROUP",
    "LOCATION",
    "POSSESSION",
    "INTEREST",
    "ONGOING_THREAD",
    "COMMITMENT",
    "LIFE_FACT",
]

#: 34.10. Eight critics, each with one job. One giant critic asked to check
#: everything checks nothing in particular, and its verdict cannot be acted on
#: because it never says which part was wrong.
CriticType = Literal[
    "chronology",
    "continuity",
    "development",
    "psychology",
    "historical_reality",
    "narrative_realism",
    "identity",
    "memory_plausibility",
]

Severity = Literal["low", "medium", "high", "fatal"]

#: high and fatal force regeneration of the offending part (34.10).
BLOCKING: frozenset[str] = frozenset({"high", "fatal"})


class AnnualScaffold(BaseModel):
    """Stage A output. 仮設計 — provisional, and known to be (GEN-ANNUAL-002).

    Not a memory, and not an objective fact about her past. It is the sketch
    the months are drawn from, and Stage C rewrites it from what the months
    actually said.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    summary: str = Field(default="", max_length=4000)
    routine: str = Field(default="", max_length=1500)
    people: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()
    threads: tuple[str, ...] = ()
    #: GEN-ANNUAL-001: 毎年 major event を強制しない. A year with nothing
    #: remarkable in it is a normal year, not a generation failure.
    notable: tuple[str, ...] = ()


#: The closed subject vocabulary a generated month may name, shared with
#: `app.world.scope` so there is one word for "the USER" in the whole system.
WorldParticipant = Literal["yui", "npc", "user", "world", "unknown"]

#: Where a generated month's contact took place. The same three scopes the
#: conversation path uses; a generated past is checked by the same rule.
GenesisInteractionScope = Literal[
    "local_to_subject_world",
    "shared_communication",
    "cross_world_physical",
]


class MonthNarrative(BaseModel):
    """Stage B output for one month.

    ``participants`` and ``interaction_scope`` are the world metadata, produced
    by the same call that writes the prose rather than by a second review — a
    nineteen-year Genesis cannot afford an extra model call per month, and the
    generator is the thing that already knows who was in the story.

    Both are required. The safety they carry used to be a list of five
    substrings; a probe of eight paraphrases got seven of them past it, and the
    fix is not fifty substrings. `people` is free text and stays free text —
    that is what the continuity ledger reads. These are the closed vocabulary
    Python can actually check.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    narrative: str = Field(default="", max_length=4000)
    importance: ImportanceClass = "routine"
    #: Whose life this month is — always hers, stated so the validator has a
    #: subject to compare participants against rather than an assumption.
    subject: WorldParticipant = "yui"
    #: Who else the month involved, in the closed subject vocabulary. Empty for
    #: a month spent alone. The USER must never appear: her nineteen years
    #: happened somewhere they were not.
    participants: tuple[WorldParticipant, ...]
    #: Where the month's contact took place.
    interaction_scope: GenesisInteractionScope
    people: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()
    threads: tuple[str, ...] = ()
    #: What happened that might become an experience. Not every sentence:
    #: 34.11 is explicit that narrative prose is not a queue of events.
    episodes: tuple[str, ...] = ()


class AnnualSynthesis(BaseModel):
    """Stage C output: the year as its months turned out (34.6)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    summary: str = Field(default="", max_length=4000)
    #: Where the finished year contradicts the original scaffold. Recorded
    #: rather than smoothed over: the months win, and the disagreement is
    #: evidence that they did.
    revisions: tuple[str, ...] = ()


class CriticIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    severity: Severity = "low"
    target_id: str = ""
    code: str = ""
    reason: str = Field(default="", max_length=500)
    repair_scope: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING


class CriticVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    passed: bool = True
    issues: tuple[CriticIssue, ...] = ()

    @property
    def blocking_issues(self) -> tuple[CriticIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def worst(self) -> str:
        order = ["low", "medium", "high", "fatal"]
        found = [issue.severity for issue in self.issues]
        return max(found, key=order.index) if found else ""


class ExperienceActor(BaseModel):
    """Somebody in an experience, with their name kept apart from their role.

    The name is free text and stays free text — 「ミカ」, 「先生」, 「母」 are
    how a life reads, and no rule should be inferring anything from them. The
    `subject` is the closed value the world model checks. Splitting them is the
    point: `actors=["USER"]` used to be an ordinary string that no validator
    could see was a person from another world.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str = Field(default="", max_length=80)
    subject: WorldParticipant


class ExperienceCandidate(BaseModel):
    """34.11. Something that happened, extracted from a finished month.

    ``全 narrative sentence を Event にしない`` — repetitive daily life is one
    compressed experience, not thirty. What survives extraction is what would
    still be worth mentioning a year later.

    An experience is a *derivative* of a month that already passed world
    validation, not a second generation stage — but a second model call
    produced it, and that call could introduce a person the month never had.
    The audit found `actors=["USER"]` accepted by this schema and staged
    without anything looking at it. So the world metadata is required here for
    the same reason it is required of a claim, and checked against the month it
    came from before the row exists.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    occurred_at: datetime
    ends_at: datetime | None = None
    #: Who was there, with a checkable role each. Required, and empty for
    #: something she did alone.
    actor_refs: tuple[ExperienceActor, ...]
    #: Who the experience involves besides her, in the closed vocabulary.
    #: Must be a subset of the month's — extraction compresses what happened,
    #: it does not add to it.
    participants: tuple[WorldParticipant, ...]
    #: Where the contact took place.
    interaction_scope: GenesisInteractionScope
    #: Legacy free-text actor list, kept so migration-29 rows still read.
    #: Never consulted for world safety.
    actors: tuple[str, ...] = ()
    context: str = Field(default="", max_length=400)
    action: str = Field(default="", max_length=400)
    outcome: str = Field(default="", max_length=400)
    social_significance: float = Field(default=0.3, ge=0.0, le=1.0)
    importance: ImportanceClass = "routine"
    source_month_id: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    #: True when this stands for a repeated routine rather than one occasion.
    compressed: bool = False


__all__ = [
    "BLOCKING",
    "WORTH_DETAIL",
    "AnnualScaffold",
    "AnnualSynthesis",
    "CriticIssue",
    "CriticType",
    "CriticVerdict",
    "EntityType",
    "ExperienceCandidate",
    "ImportanceClass",
    "MonthNarrative",
    "Severity",
]
