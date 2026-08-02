"""Temporal Leakage Guard (spec 21.4, 2.11, 34.2-6).

    過去時点より後にしか存在しない情報を Exposure Candidate に入れてはならない。

    現在の資料を「当時既に公表されていたことの証拠」として使うことは可能だが、
    後知恵の内容を過去の本人に与えない。

Those two sentences pull in opposite directions and the distinction between
them is the whole module:

* **Availability** is about the fact. If a fact was not public until 2011, a
  simulated 2003 cannot be exposed to it. Full stop.
* **Sources** are about our evidence for the fact. A book published in 2024 is
  a perfectly good witness that something was already public in 1998. Refusing
  modern sources would leave the past unresearchable; accepting their *content*
  as period knowledge is the leak.

So the guard checks ``available_from`` and deliberately does not check
``source_published_at``. Everything that could put knowledge in front of YUI
goes through :func:`guard` first, and a violation raises rather than filters —
a leak is a bug in the caller, not a candidate to quietly drop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from app.knowledge.models import KnowledgeItem


class TemporalLeakageError(ValueError):
    """Raised when hindsight was about to be handed to a past self."""


@dataclass(frozen=True, slots=True)
class LeakageReport:
    """What a batch check found, for audits rather than for control flow."""

    moment: datetime
    checked: int
    leaked: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.leaked


def existed_at(item: KnowledgeItem, moment: datetime) -> bool:
    return item.existed_at(moment)


def guard(item: KnowledgeItem, moment: datetime) -> KnowledgeItem:
    """Return ``item`` if it could have been known at ``moment``, else raise."""
    if item.existed_at(moment):
        return item
    if moment < item.available_from:
        raise TemporalLeakageError(
            f"{item.statement!r} was not available until {item.available_from.isoformat()}; "
            f"it cannot be exposed at {moment.isoformat()} (spec 21.4)"
        )
    raise TemporalLeakageError(
        f"{item.statement!r} stopped being available at "
        f"{item.available_until.isoformat() if item.available_until else '?'}; "
        f"it cannot be exposed at {moment.isoformat()} (spec 21.4)"
    )


def available(items: Iterable[KnowledgeItem], moment: datetime) -> list[KnowledgeItem]:
    """Filter, for building candidate sets. Use :func:`guard` to assert."""
    return [item for item in items if item.existed_at(moment)]


def audit(items: Sequence[KnowledgeItem], moment: datetime) -> LeakageReport:
    """Check a whole exposure set at once (spec 22.7 knowledge chronology audit)."""
    leaked = tuple(
        item.knowledge_id for item in items if not item.existed_at(moment)
    )
    return LeakageReport(moment=moment, checked=len(items), leaked=leaked)


def source_is_usable(item: KnowledgeItem, moment: datetime) -> bool:
    """A later-published source is fine; later-available content is not.

    This exists to be explicit about the asymmetry, and to give the audit
    something to assert rather than leaving it as a comment.
    """
    return item.existed_at(moment)
