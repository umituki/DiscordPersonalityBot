"""Dialogue references (rebuild spec 13.1-13.3, Phase 3 §23-§29).

A few short examples of how people actually talk, handed to the realizer as a
sense of register — nothing more.

Three rules make this safe, and all three are structural rather than advisory:

**A reference is not a memory (§26, spec 13.3).** There is no path from here
into ``episodic_memories``, ``semantic_memories`` or the common ground. A
:class:`DialogueReference` has no ``memory_id``, is never returned by recall,
and the realizer is the only thing that ever sees one. Corpus text entering
YUI's life would be a fabricated past with a clean provenance chain, which is
exactly what Phase 1 exists to prevent.

**References are optional (§28).** With no provider, or a provider that finds
nothing, the realizer runs on an empty tuple. A corpus must never become a
runtime dependency of being able to speak.

**References are not templates (§29).** The prompt says so explicitly. Without
that, a handful of examples turns into a handful of sentence moulds and every
reply comes out the same shape.

Licensing (§27): shipped here are the protocol, a null provider and a small
developer-written fixture. Real corpora — CEJC, BTSJ and the like — are not
vendored, because their terms have not been checked. Every provider must state
``source_name``, ``license``, ``version`` and ``provenance``, so a corpus whose
terms are unknown cannot be added without someone noticing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

import yaml

logger = logging.getLogger(__name__)

#: §25. Two to five. More than this and the model starts copying rather than
#: calibrating, and the context budget goes to other people's conversations.
MIN_REFERENCES = 2
MAX_REFERENCES = 5


class ReferenceCorpusError(RuntimeError):
    """Raised when a reference corpus is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ReferenceProvenance:
    """Where a corpus came from, and on what terms (§27)."""

    source_name: str
    license: str
    version: str
    provenance: str

    def render(self) -> str:
        return f"{self.source_name} ({self.license}, {self.version})"


@dataclass(frozen=True, slots=True)
class DialogueReference:
    """One short example of a turn pair.

    Deliberately not an ``EpisodicMemory``: no id that memory would accept, no
    ``occurred_at``, no accessibility. It cannot be mistaken for something that
    happened to her, because it has none of the shape of something that did.
    """

    situation: str
    prompt_turn: str
    reply_turn: str
    relationship_band: str = "acquaintance"
    conversation_type: str = "smalltalk"
    primary_move: str = "acknowledge"
    tone: str = "neutral"
    length: str = "short"
    initiative: str = "balanced"
    source: str = "fixture"

    def render(self) -> str:
        return f"({self.situation})\nA: {self.prompt_turn}\nB: {self.reply_turn}"


@dataclass(frozen=True, slots=True)
class DialogueReferenceQuery:
    """What kind of exchange to look for (§24)."""

    relationship_band: str = "acquaintance"
    conversation_type: str = "smalltalk"
    primary_move: str = "acknowledge"
    tone: str = "neutral"
    length: str = "short"
    initiative: str = "balanced"

    def score(self, reference: DialogueReference) -> int:
        """How well an example matches. Plain field agreement — a cleverer
        similarity here would be a second retrieval system to reason about."""
        return sum(
            (
                reference.relationship_band == self.relationship_band,
                reference.conversation_type == self.conversation_type,
                reference.primary_move == self.primary_move,
                reference.tone == self.tone,
                reference.length == self.length,
                reference.initiative == self.initiative,
            )
        )


@runtime_checkable
class DialogueReferenceProvider(Protocol):
    """Where examples come from (§23)."""

    provenance: ReferenceProvenance

    async def retrieve(
        self, query: DialogueReferenceQuery, limit: int = 4
    ) -> tuple[DialogueReference, ...]:
        ...


class NullReferenceProvider:
    """No corpus. The realizer runs on nothing, which must stay ordinary (§28)."""

    provenance = ReferenceProvenance(
        source_name="none",
        license="n/a",
        version="0",
        provenance="no dialogue corpus is configured",
    )

    async def retrieve(
        self, query: DialogueReferenceQuery, limit: int = 4
    ) -> tuple[DialogueReference, ...]:
        return ()


class FixtureReferenceProvider:
    """A small corpus written by the developers of this project (§27).

    Not a linguistic resource and not pretending to be one: it exists so the
    provider path is genuinely exercised, and so the licensing question stays
    open rather than being answered by vendoring something.
    """

    def __init__(
        self,
        references: Sequence[DialogueReference],
        provenance: ReferenceProvenance,
    ) -> None:
        self._references = tuple(references)
        self.provenance = provenance

    @classmethod
    def load(cls, path: Path | str) -> FixtureReferenceProvider:
        corpus_path = Path(path)
        if not corpus_path.is_file():
            raise ReferenceCorpusError(f"reference corpus not found: {corpus_path}")
        raw: Any = yaml.safe_load(corpus_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ReferenceCorpusError(f"reference corpus must be a mapping: {corpus_path}")

        meta = raw.get("provenance") or {}
        missing = [
            field
            for field in ("source_name", "license", "version", "provenance")
            if not str(meta.get(field, "")).strip()
        ]
        if missing:
            raise ReferenceCorpusError(
                f"{corpus_path.name} is missing provenance: {', '.join(missing)}"
            )
        entries = raw.get("references") or []
        if not isinstance(entries, list):
            raise ReferenceCorpusError(f"references must be a list: {corpus_path}")

        references = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ReferenceCorpusError(f"each reference must be a mapping: {corpus_path}")
            references.append(
                DialogueReference(
                    situation=str(entry.get("situation", "")),
                    prompt_turn=str(entry["prompt_turn"]),
                    reply_turn=str(entry["reply_turn"]),
                    relationship_band=str(entry.get("relationship_band", "acquaintance")),
                    conversation_type=str(entry.get("conversation_type", "smalltalk")),
                    primary_move=str(entry.get("primary_move", "acknowledge")),
                    tone=str(entry.get("tone", "neutral")),
                    length=str(entry.get("length", "short")),
                    initiative=str(entry.get("initiative", "balanced")),
                    source=str(meta["source_name"]),
                )
            )
        return cls(
            references,
            ReferenceProvenance(
                source_name=str(meta["source_name"]),
                license=str(meta["license"]),
                version=str(meta["version"]),
                provenance=str(meta["provenance"]),
            ),
        )

    async def retrieve(
        self, query: DialogueReferenceQuery, limit: int = 4
    ) -> tuple[DialogueReference, ...]:
        wanted = max(0, min(limit, MAX_REFERENCES))
        if wanted == 0 or not self._references:
            return ()
        ranked = sorted(
            self._references, key=lambda item: query.score(item), reverse=True
        )
        # An example that matches nothing is worse than no example: it teaches
        # the wrong register. Below one matching field, return nothing.
        return tuple(item for item in ranked[:wanted] if query.score(item) >= 1)


def render_references(references: Sequence[DialogueReference]) -> str:
    """The block handed to the realizer. Empty when there is nothing (§28)."""
    if not references:
        return ""
    return "\n\n".join(reference.render() for reference in references)


__all__ = [
    "MAX_REFERENCES",
    "MIN_REFERENCES",
    "DialogueReference",
    "DialogueReferenceProvider",
    "DialogueReferenceQuery",
    "FixtureReferenceProvider",
    "NullReferenceProvider",
    "ReferenceCorpusError",
    "ReferenceProvenance",
    "render_references",
]
