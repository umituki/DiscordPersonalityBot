"""Searching, as one option among several (rebuild spec 32 — Phase 11).

This module is the formal entrance for information from outside. Three
distinctions hold it together, and collapsing any of them produces a bot that
googles everything and believes what it finds.

**Search request ≠ search success ≠ knowledge acquisition.**
A query being sent, a provider answering, and YUI knowing something are three
events with three different truth conditions. The model writing 「調べたら〜
だった」 establishes none of them; only ``ToolManager`` reporting success
establishes the second, and only the exposure funnel establishes the third.

**External information ≠ what she permanently knows.**
Ten results arriving is not ten facts learned. Every result goes through
attention, comprehension and retention, and most of them stop somewhere before
the end — which is what makes "I read about it once" different from "I know
it".

**No results ≠ it does not exist.**
The failure taxonomy below is not decoration. A timeout, a provider outage and
an honest empty answer are three different states of the world, and the one
thing none of them means is that the thing being asked about is not real.

The temporal gate is Python, not prose. ``published_at > effective_now`` is
unusable, checked here, on every result, before anything downstream sees it.
A prompt asking the model not to use the future is a request; this is a rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.clock import ensure_aware

logger = logging.getLogger(__name__)

MODULE = "search"

#: Every way a search can end. Kept apart on purpose: collapsing these into a
#: boolean is how "the network was down" turns into "there is nothing to find".
SearchOutcome = Literal[
    "success",
    "no_results",
    "ambiguous_results",
    "timeout",
    "network_error",
    "provider_error",
    "refused",
]

#: The outcomes that produced something to look at. Everything else produced
#: information about the *search*, which is not information about the world.
PRODUCTIVE: frozenset[str] = frozenset({"success", "ambiguous_results"})


class SearchQuery(BaseModel):
    """What to look for, and when she is looking from.

    ``effective_now`` is required and has no default. That is the whole
    anti-leakage design: a caller cannot forget it, and a Genesis run in 2012
    cannot silently inherit today's clock and read about 2025.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    #: The moment she is searching *from*. In ordinary running this is now; in
    #: a past simulation it is the simulated date, and results published after
    #: it are unusable however relevant they look.
    effective_now: datetime
    topic: str = ""
    max_results: int = Field(default=5, ge=1, le=20)


class SearchResult(BaseModel):
    """One thing a provider returned, with where it came from.

    Provenance is mandatory rather than nice to have. Genesis asks "when could
    this have been known, and on whose word?", and a result that cannot answer
    is a leak waiting to happen twelve phases later.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    statement: str = Field(min_length=1, max_length=1000)
    source_name: str = ""
    source_url: str | None = None
    #: When this was published. ``None`` means unknown, which the gate treats
    #: as unusable for a past-dated search — not knowing when something was
    #: written is not evidence that it is old enough.
    published_at: datetime | None = None
    #: When the fact itself became public, if the provider can say. Falls back
    #: to ``published_at``.
    available_from: datetime | None = None
    topic: str = ""
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    complexity: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @property
    def known_from(self) -> datetime | None:
        return self.available_from or self.published_at


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """What the provider said, including how it failed."""

    outcome: SearchOutcome
    results: tuple[SearchResult, ...] = ()
    detail: str = ""
    provider: str = ""
    latency_ms: int = 0

    @property
    def productive(self) -> bool:
        return self.outcome in PRODUCTIVE and bool(self.results)

    @property
    def found_nothing(self) -> bool:
        """True for an honest empty answer.

        Emphatically not "the thing does not exist" — see the module note.
        """
        return self.outcome == "no_results"


@runtime_checkable
class SearchProvider(Protocol):
    """Somewhere to look. Every implementation must honour ``effective_now``."""

    name: str

    async def search(self, query: SearchQuery) -> SearchResponse: ...


class NullSearchProvider:
    """No provider configured. Says so, rather than returning nothing found.

    The distinction matters: "we have nowhere to look" and "we looked and
    found nothing" are different, and only the second is about the world.
    """

    name = "null"

    async def search(self, query: SearchQuery) -> SearchResponse:
        return SearchResponse(
            outcome="provider_error",
            detail="no search provider is configured",
            provider=self.name,
        )


class FixtureSearchProvider:
    """A provider backed by a YAML file, for tests and offline running.

    Deliberately a real provider rather than a mock: it honours
    ``effective_now``, it can return every failure mode on demand, and the
    end-to-end test therefore exercises the same code path a live provider
    would.
    """

    name = "fixture"

    def __init__(self, entries: Sequence[SearchResult], *, behaviour: str = "success") -> None:
        self._entries = tuple(entries)
        #: Lets a test ask for a specific failure without a mock.
        self.behaviour: str = behaviour

    @classmethod
    def load(cls, path: Path | str) -> FixtureSearchProvider:
        file = Path(path)
        if not file.is_file():
            return cls(())
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        entries = [
            SearchResult(
                statement=row["statement"],
                source_name=row.get("source_name", str(file.name)),
                source_url=row.get("source_url"),
                published_at=_moment(row.get("published_at")),
                available_from=_moment(row.get("available_from")),
                topic=row.get("topic", ""),
                salience=float(row.get("salience", 0.5)),
                complexity=float(row.get("complexity", 0.5)),
                confidence=float(row.get("confidence", 0.5)),
            )
            for row in raw.get("entries", [])
        ]
        return cls(entries)

    async def search(self, query: SearchQuery) -> SearchResponse:
        if self.behaviour != "success":
            return SearchResponse(
                outcome=self.behaviour,  # type: ignore[arg-type]
                detail=f"fixture provider set to {self.behaviour}",
                provider=self.name,
            )

        needle = query.text.strip()
        hits = [
            entry
            for entry in self._entries
            if needle in entry.statement
            or (entry.topic and (entry.topic in needle or needle in entry.topic))
        ]
        if not hits:
            return SearchResponse(
                outcome="no_results", provider=self.name, detail="nothing matched"
            )
        return SearchResponse(
            outcome="success",
            results=tuple(hits[: query.max_results]),
            provider=self.name,
        )


@dataclass(frozen=True, slots=True)
class TemporalVerdict:
    """What the gate let through, and what it refused."""

    usable: tuple[SearchResult, ...] = ()
    rejected: tuple[SearchResult, ...] = field(default=())

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


def reject_the_future(
    results: Sequence[SearchResult], effective_now: datetime
) -> TemporalVerdict:
    """The hard gate. ``published_at > effective_now`` is unusable.

    In Python, on every result, before anything downstream sees one. Telling
    the model not to use the future is a request that a good model will mostly
    honour and a degraded one will not; this is the part that does not depend
    on the model behaving.

    A result with no date at all is also refused. Not knowing when something
    was written is not evidence that it is old enough — and for a simulated
    past, "probably fine" is exactly the reasoning that leaks a decade.
    """
    moment = ensure_aware(effective_now)
    usable: list[SearchResult] = []
    rejected: list[SearchResult] = []
    for result in results:
        known_from = result.known_from
        if known_from is None or ensure_aware(known_from) > moment:
            rejected.append(result)
            continue
        usable.append(result)
    if rejected:
        logger.info(
            "temporal gate refused %d result(s) as newer than %s",
            len(rejected),
            moment.isoformat(),
        )
    return TemporalVerdict(usable=tuple(usable), rejected=tuple(rejected))


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    from app.clock import from_iso

    try:
        return from_iso(str(value))
    except Exception:  # noqa: BLE001 - an unparseable date is an unknown date
        return None


__all__ = [
    "MODULE",
    "PRODUCTIVE",
    "FixtureSearchProvider",
    "NullSearchProvider",
    "SearchOutcome",
    "SearchProvider",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "TemporalVerdict",
    "reject_the_future",
]
