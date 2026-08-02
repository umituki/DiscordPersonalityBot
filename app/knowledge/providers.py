"""Where period knowledge comes from (patch spec 16.2).

The 2026-08-02 Genesis had zero knowledge sources and zero candidates, because
nothing ever fed the builder — the builder validates and stores candidates, and
no one was producing any.

Patch spec 16.2 is explicit about where they may come from:

    Authorityはpretrained LLM knowledgeではなく外部source。

So a model is never a provider here. What a model knows is not evidence that
something was public in 1998; it is evidence that the sentence is plausible,
which is exactly the difference the temporal guard exists to keep. The
providers below are all external:

``BundleProvider``
    Files the owner supplies — a curated historical dataset, or a bundle
    exported from anywhere else. Offline, reproducible, and what ships.

``ToolKnowledgeProvider``
    A search or reference lookup, executed through the Tool Manager so the
    call has provenance and so a claim is only usable when the Tool Manager
    reports success (spec 26). Absent tool, no candidates — never a guess.

Every provider registers itself as a :class:`KnowledgeSource` and stamps its
``source_id`` on each candidate, so any stored item can be traced back to what
attested it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Sequence

import yaml

from app.clock import from_iso
from app.knowledge.builder import Candidate, KnowledgeBuilder
from app.knowledge.models import COVERAGE_CLASSES, CoverageClass

logger = logging.getLogger(__name__)

MODULE = "knowledge_provider"


class ProviderError(RuntimeError):
    """Raised when a provider's input cannot be read as knowledge candidates."""


@dataclass(frozen=True, slots=True)
class CoverageRequest:
    """One period and one class to find knowledge for (patch spec 16.3)."""

    coverage_class: CoverageClass
    period_start: datetime
    period_end: datetime
    geography: str = "global"
    language: str = "ja"
    topics: tuple[str, ...] = ()
    limit: int = 40

    def covers(self, moment: datetime) -> bool:
        return self.period_start <= moment < self.period_end

    def includes(
        self, available_from: datetime, available_until: datetime | None = None
    ) -> bool:
        """Whether this claim was public *during* the window.

        Not "became public during the window": most of what someone knows was
        already true before they were born. What must never be included is
        something that had not happened yet by the end of the window — that is
        the leak the temporal guard exists to stop, and it is the only
        direction that matters here.
        """
        if available_from >= self.period_end:
            return False
        return available_until is None or available_until > self.period_start


class KnowledgeProvider(Protocol):
    """Produces candidates for one coverage request. Never invents them."""

    name: str
    kind: str
    reliability: float

    def candidates(self, request: CoverageRequest) -> Sequence[Candidate]: ...


# --- owner-supplied bundles -------------------------------------------------
@dataclass(frozen=True, slots=True)
class BundleEntry:
    """One row of a bundle file, before it becomes a candidate."""

    statement: str
    coverage_class: CoverageClass
    available_from: datetime
    topic: str = ""
    geography: str = "global"
    language: str = "ja"
    available_until: datetime | None = None
    stability: str = "CHANGEABLE"
    complexity: float = 0.5
    salience: float = 0.3
    truth_confidence: float | None = None


class BundleProvider:
    """Knowledge from files the owner curated (patch spec 16.2).

    A bundle is a mapping with a ``knowledge`` list. Each entry needs a
    statement, a coverage class and an ``available_from`` — without the last
    one the claim cannot be tested against a past moment, which is the whole
    point of the layer (spec 21.3).

    Reading is strict: a malformed entry raises rather than being skipped. A
    knowledge base that silently drops half its rows is worse than one that
    refuses to load.
    """

    kind = "bundle"

    def __init__(
        self,
        directory: Path | str,
        *,
        name: str = "owner_bundle",
        reliability: float = 0.7,
    ) -> None:
        self.name = name
        self.reliability = reliability
        self._directory = Path(directory)
        self._entries: list[BundleEntry] | None = None

    @property
    def directory(self) -> Path:
        return self._directory

    def entries(self) -> list[BundleEntry]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> list[BundleEntry]:
        if not self._directory.is_dir():
            logger.warning("knowledge bundle directory not found: %s", self._directory)
            return []
        entries: list[BundleEntry] = []
        for path in sorted(self._directory.glob("*.y*ml")) + sorted(
            self._directory.glob("*.json")
        ):
            entries.extend(self._read_file(path))
        return entries

    def _read_file(self, path: Path) -> list[BundleEntry]:
        text = path.read_text(encoding="utf-8")
        raw: Any = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ProviderError(f"knowledge bundle must be a mapping: {path}")
        rows = raw.get("knowledge") or []
        if not isinstance(rows, list):
            raise ProviderError(f"'knowledge' must be a list: {path}")
        return [self._entry(row, path) for row in rows]

    @staticmethod
    def _entry(row: Any, path: Path) -> BundleEntry:
        if not isinstance(row, dict):
            raise ProviderError(f"knowledge entry must be a mapping in {path}: {row!r}")
        try:
            coverage_class = row["coverage_class"]
            if coverage_class not in COVERAGE_CLASSES:
                raise ProviderError(
                    f"unknown coverage class {coverage_class!r} in {path}"
                )
            return BundleEntry(
                statement=str(row["statement"]).strip(),
                coverage_class=coverage_class,
                available_from=_moment(row["available_from"]),
                topic=str(row.get("topic", "")),
                geography=str(row.get("geography", "global")),
                language=str(row.get("language", "ja")),
                available_until=(
                    _moment(row["available_until"]) if row.get("available_until") else None
                ),
                stability=str(row.get("stability", "CHANGEABLE")),
                complexity=float(row.get("complexity", 0.5)),
                salience=float(row.get("salience", 0.3)),
                truth_confidence=(
                    float(row["truth_confidence"])
                    if row.get("truth_confidence") is not None
                    else None
                ),
            )
        except KeyError as exc:
            raise ProviderError(f"knowledge entry in {path} is missing {exc}") from exc

    def candidates(self, request: CoverageRequest) -> Sequence[Candidate]:
        selected = [
            entry
            for entry in self.entries()
            if entry.coverage_class == request.coverage_class
            and request.includes(entry.available_from, entry.available_until)
        ]
        if request.topics:
            wanted = {topic.strip() for topic in request.topics if topic.strip()}
            preferred = [entry for entry in selected if entry.topic in wanted]
            # Interest-driven coverage is *about* the interests; anything else
            # keeps the whole period.
            if request.coverage_class == "interest_driven":
                selected = preferred
            else:
                selected = preferred + [entry for entry in selected if entry not in preferred]
        return [
            Candidate(
                statement=entry.statement,
                coverage_class=entry.coverage_class,
                available_from=entry.available_from,
                topic=entry.topic,
                geography=entry.geography,
                language=entry.language,
                available_until=entry.available_until,
                stability=entry.stability,  # type: ignore[arg-type]
                complexity=entry.complexity,
                salience=entry.salience,
                truth_confidence=entry.truth_confidence,
            )
            for entry in selected[: request.limit]
        ]


# --- tool-backed lookup -----------------------------------------------------
class ToolKnowledgeProvider:
    """Knowledge from a registered lookup tool (patch spec 16.2).

    Spec 26: a tool result is only usable when the Tool Manager reports
    success. If the tool is not registered, fails, or returns something that
    is not a list of entries, this provider yields nothing — a gap that the
    knowledge health audit will report, which is the intended outcome. It
    never falls back to a model.
    """

    kind = "tool"

    def __init__(
        self,
        tools: Any,
        *,
        tool_name: str,
        name: str | None = None,
        reliability: float = 0.5,
    ) -> None:
        self._tools = tools
        self._tool_name = tool_name
        self.name = name or f"tool:{tool_name}"
        self.reliability = reliability

    def candidates(self, request: CoverageRequest) -> Sequence[Candidate]:
        call = getattr(self._tools, "call_sync", None)
        if call is None:
            logger.info("tool manager has no synchronous call; skipping %s", self._tool_name)
            return ()
        try:
            result = call(
                self._tool_name,
                {
                    "coverage_class": request.coverage_class,
                    "period_start": request.period_start.isoformat(),
                    "period_end": request.period_end.isoformat(),
                    "topics": list(request.topics),
                    "limit": request.limit,
                },
            )
        except Exception:  # noqa: BLE001 - a missing tool is a gap, not a crash
            logger.warning("knowledge tool %s failed", self._tool_name, exc_info=True)
            return ()

        if not getattr(result, "success", False):
            logger.info("knowledge tool %s did not report success", self._tool_name)
            return ()
        rows = (getattr(result, "output", None) or {}).get("knowledge")
        if not isinstance(rows, list):
            return ()
        return [
            Candidate(
                statement=str(row["statement"]).strip(),
                coverage_class=request.coverage_class,
                available_from=_moment(row["available_from"]),
                topic=str(row.get("topic", "")),
                geography=str(row.get("geography", request.geography)),
                language=str(row.get("language", request.language)),
                complexity=float(row.get("complexity", 0.5)),
                salience=float(row.get("salience", 0.3)),
            )
            for row in rows[: request.limit]
            if isinstance(row, dict) and row.get("statement") and row.get("available_from")
        ]


# --- registry ---------------------------------------------------------------
@dataclass
class ProviderRegistry:
    """The providers in use, each registered as a traceable source."""

    builder: KnowledgeBuilder
    providers: list[KnowledgeProvider] = field(default_factory=list)
    _source_ids: dict[str, str] = field(default_factory=dict, init=False)

    def register(self, provider: KnowledgeProvider) -> str:
        """Add a provider and record where its claims will come from."""
        source = self.builder.add_source(
            name=provider.name,
            kind=provider.kind,
            reliability=provider.reliability,
        )
        self._source_ids[provider.name] = source.source_id
        self.providers.append(provider)
        return source.source_id

    def source_id_of(self, provider: KnowledgeProvider) -> str | None:
        return self._source_ids.get(provider.name)

    def candidates_for(self, request: CoverageRequest) -> list[Candidate]:
        """Everything the providers offer for this request, with provenance."""
        offered: list[Candidate] = []
        seen: set[str] = set()
        for provider in self.providers:
            source_id = self._source_ids.get(provider.name)
            try:
                produced = provider.candidates(request)
            except Exception:  # noqa: BLE001 - one bad provider is not the run
                logger.exception("knowledge provider %s failed", provider.name)
                continue
            for candidate in produced:
                # Patch spec 16.4: the same claim from the same provider twice
                # is one claim. Deduplicated here rather than counted twice as
                # corroboration.
                key = candidate.statement.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                offered.append(
                    replace(candidate, source_id=source_id) if source_id else candidate
                )
        return offered


def _moment(value: Any) -> datetime:
    if isinstance(value, datetime):
        from app.clock import ensure_aware

        return ensure_aware(value)
    if isinstance(value, str):
        return from_iso(value if "T" in value else f"{value}T00:00:00+00:00")
    raise ProviderError(f"cannot read {value!r} as a date")


__all__ = [
    "BundleEntry",
    "BundleProvider",
    "CoverageRequest",
    "KnowledgeProvider",
    "ProviderError",
    "ProviderRegistry",
    "ToolKnowledgeProvider",
]
