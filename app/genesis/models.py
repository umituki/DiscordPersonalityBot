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


class MonthNarrative(BaseModel):
    """Stage B output for one month."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    narrative: str = Field(default="", max_length=4000)
    importance: ImportanceClass = "routine"
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


class ExperienceCandidate(BaseModel):
    """34.11. Something that happened, extracted from a finished month.

    ``全 narrative sentence を Event にしない`` — repetitive daily life is one
    compressed experience, not thirty. What survives extraction is what would
    still be worth mentioning a year later.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    occurred_at: datetime
    ends_at: datetime | None = None
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
