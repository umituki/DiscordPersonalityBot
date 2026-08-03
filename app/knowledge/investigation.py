"""From not knowing to knowing (rebuild spec 17, 32 — Phase 11).

The chain, in full, with the authority for each step::

    knowledge gap          situation, not a trigger
    → EpistemicActionSelector  recall / infer / ask_user / web_search /
                               defer / ignore / avoid
    → (only if web_search)
    → ActionCandidate → DecisionEngine        chooses
    → ToolManager                             executes, and is the only
                                              authority on whether it ran
    → provider result
    → temporal gate                           Python, not prose
    → ExposureFunnel                          attention → comprehension →
                                              retention
    → KnowledgeRepository                     commits what survived
    → Evidence → BeliefEngine                 never a direct overwrite

Two of those deserve saying twice.

**The selector is not a formality.** A gap arrives and most of the time the
answer is not to search: it is already in memory, or it can be reasoned out, or
the person who knows is right there, or it does not matter enough to bother.
The gap row records which of the seven was chosen, so "she searches for
everything" is a claim the database can refute or confirm.

**A search result is not a belief.** What comes back is *evidence*, and it goes
to the Belief Engine as evidence — where it meets whatever she already thought
and updates it through the ordinary support/contradiction model. Overwriting a
belief because a search said otherwise would make her a search cache with a
personality attached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from app import ids
from app.clock import Clock, SystemClock
from app.epistemics.actions import EpistemicDecision, KnowledgeGap
from app.knowledge.events import (
    KNOWLEDGE_ACQUIRED,
    SEARCH_PERFORMED,
    KnowledgeAcquiredPayload,
    SearchPerformedPayload,
)
from app.knowledge.models import KnowledgeItem
from app.knowledge.search import (
    SearchProvider,
    SearchQuery,
    SearchResponse,
    SearchResult,
    reject_the_future,
)
from app.events.model import Event
from app.tools.models import ToolRequest

logger = logging.getLogger(__name__)

MODULE = "investigation_service"
SEARCH = "sch"
KNOWLEDGE = "kno"

#: The tool the search goes through. It is a tool rather than a direct call so
#: that ToolManager remains the single authority on whether anything ran
#: (spec 32.2) — the same rule that stops the model claiming it looked
#: something up.
SEARCH_TOOL = "web_search"


@dataclass(frozen=True, slots=True)
class InvestigationOutcome:
    """One full attempt, however far it got."""

    search_id: str
    query: str
    outcome: str
    provider: str = ""
    detail: str = ""
    results: int = 0
    future_rejected: int = 0
    exposed: int = 0
    acquired: int = 0
    tool_call_id: str | None = None
    event_id: str | None = None
    acquired_ids: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        """The *search* succeeded. Not the same as having learned anything."""
        return self.outcome == "success"

    @property
    def learned_anything(self) -> bool:
        return self.acquired > 0

    def describe(self) -> str:
        return (
            f"{self.outcome} results={self.results} "
            f"future_rejected={self.future_rejected} exposed={self.exposed} "
            f"acquired={self.acquired}"
        )


class InvestigationService:
    """Runs the chain above, and records every stage of it."""

    name = MODULE

    def __init__(
        self,
        *,
        selector: Any,
        provider: SearchProvider,
        tools: Any,
        knowledge: Any,
        knowledge_repo: Any,
        searches: Any,
        gaps: Any,
        beliefs: Any = None,
        processor: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._selector = selector
        self._provider = provider
        self._tools = tools
        self._knowledge = knowledge
        self._knowledge_repo = knowledge_repo
        self._searches = searches
        self._gaps = gaps
        self._beliefs = beliefs
        self._processor = processor
        self._clock = clock or SystemClock()

    # --- the decision, before any searching ---------------------------------
    def decide(self, gap: KnowledgeGap, *, gap_id: str | None = None) -> EpistemicDecision:
        """Which of the seven responses this gap gets (spec 17.1).

        Recorded on the gap row whichever way it goes, so a claim that she
        searches for everything is one query away from being settled.
        """
        decision = self._selector.select(gap)
        if gap_id is not None:
            self._gaps.decide(
                gap_id, action=decision.action, now=self._clock.now()
            )
        logger.info(
            "epistemic action topic=%s action=%s reason=%s",
            gap.topic,
            decision.action,
            decision.reason,
        )
        return decision

    # --- the search itself ---------------------------------------------------
    async def investigate(
        self,
        gap: KnowledgeGap,
        *,
        effective_now: datetime,
        gap_id: str | None = None,
        run_id: str | None = None,
        event_id: str | None = None,
        interest: float = 0.5,
        curiosity: float = 0.5,
    ) -> InvestigationOutcome:
        """Look something up, and see how much of it she ends up knowing.

        ``effective_now`` is required rather than defaulted to the clock: a
        past simulation searches from its own date, and a parameter that
        quietly falls back to today is the leak this phase exists to prevent.
        """
        search_id = ids.new_id(SEARCH)
        started = self._clock.now()
        query = SearchQuery(
            text=gap.topic or gap.unknown_part or "?",
            effective_now=effective_now,
            topic=gap.topic,
        )

        response, call_id = await self._run_tool(query, run_id=run_id, event_id=event_id)

        verdict = reject_the_future(response.results, effective_now)
        exposed = 0
        acquired: list[str] = []

        if verdict.usable:
            exposed, acquired = self._absorb(
                verdict.usable,
                effective_now=effective_now,
                search_id=search_id,
                interest=interest,
                curiosity=curiosity,
            )

        outcome = InvestigationOutcome(
            search_id=search_id,
            query=query.text,
            outcome=response.outcome,
            provider=response.provider or getattr(self._provider, "name", ""),
            detail=response.detail,
            results=len(response.results),
            future_rejected=verdict.rejected_count,
            exposed=exposed,
            acquired=len(acquired),
            tool_call_id=call_id,
            acquired_ids=tuple(acquired),
        )
        event = await self._announce(outcome, gap)
        self._searches.record(
            outcome.model_copy(update={"event_id": event})
            if hasattr(outcome, "model_copy")
            else outcome,
            gap_id=gap_id,
            requested_at=started,
            effective_now=effective_now,
            latency_ms=max(
                0, int((self._clock.now() - started).total_seconds() * 1000)
            ),
            event_id=event,
        )
        if acquired and gap_id is not None:
            self._gaps.close(gap_id, now=self._clock.now(), status="answered")
        logger.info("investigation %s: %s", query.text, outcome.describe())
        return outcome

    # --- stages --------------------------------------------------------------
    async def _run_tool(
        self, query: SearchQuery, *, run_id: str | None, event_id: str | None
    ) -> tuple[SearchResponse, str | None]:
        """Spec 32.2: ToolManager success is the only authority.

        The provider is reached *through* the tool, not beside it, so that a
        search which never ran cannot be described as one that did.
        """
        if self._tools is None:
            response = await self._provider.search(query)
            return response, None

        request = ToolRequest(
            tool_name=SEARCH_TOOL,
            arguments={
                "query": query.text,
                "effective_now": query.effective_now.isoformat(),
                "max_results": query.max_results,
            },
            requested_by="engine",
            reason=f"knowledge gap: {query.topic or query.text}",
            run_id=run_id,
            event_id=event_id,
        )
        outcome = await self._tools.execute(request)
        if not getattr(outcome, "success", False):
            return (
                _failure_from(outcome, provider=getattr(self._provider, "name", "")),
                getattr(outcome, "call_id", None),
            )
        return _response_from(outcome.data), outcome.call_id

    def _absorb(
        self,
        results: Sequence[SearchResult],
        *,
        effective_now: datetime,
        search_id: str,
        interest: float,
        curiosity: float,
    ) -> tuple[int, list[str]]:
        """Exposure, not import.

        Every usable result is *put in front of her*; the funnel decides what
        she noticed, what she understood and what she kept. Ten results is not
        ten facts learned, and a pipeline that wrote them all straight to the
        knowledge table would be claiming otherwise.
        """
        exposed = 0
        acquired: list[str] = []
        for result in results:
            item = self._store(result, effective_now=effective_now, search_id=search_id)
            if item is None:
                continue
            try:
                outcome = self._knowledge.expose(
                    item,
                    moment=effective_now,
                    interest=interest,
                    curiosity=curiosity,
                    channel="search",
                    origin="real_discord",
                )
            except Exception:  # noqa: BLE001 - one bad result is not a failed search
                logger.exception("could not expose a search result")
                continue
            exposed += 1
            if outcome.learned:
                acquired.append(item.knowledge_id)
                self._offer_as_evidence(item, result)
        return exposed, acquired

    def _store(
        self, result: SearchResult, *, effective_now: datetime, search_id: str
    ) -> KnowledgeItem | None:
        """Record the world's claim, with where it came from.

        This is the *world's* fact, not hers. Writing it here does not mean she
        knows it — that is what the acquisition row is for.
        """
        existing = self._knowledge_repo.by_statement(result.statement)
        if existing is not None:
            return existing
        known_from = result.known_from
        if known_from is None:  # pragma: no cover - the gate refuses these first
            return None
        item = KnowledgeItem(
            knowledge_id=ids.new_id(KNOWLEDGE),
            statement=result.statement,
            coverage_class="interest_driven",
            topic=result.topic,
            available_from=known_from,
            source_published_at=result.published_at,
            truth_confidence=result.confidence,
            complexity=result.complexity,
            salience=result.salience,
            created_at=self._clock.now(),
        )
        try:
            return self._knowledge_repo.add_knowledge(
                item,
                source_type="web_search",
                source_url=result.source_url,
                retrieved_at=self._clock.now(),
                search_id=search_id,
            )
        except TypeError:  # pragma: no cover - older repository signature
            return self._knowledge_repo.add_knowledge(item)

    def _offer_as_evidence(self, item: KnowledgeItem, result: SearchResult) -> None:
        """A search result argues; it does not decree.

        It goes to the Belief Engine as evidence and meets whatever she already
        thought. Overwriting a belief because a search disagreed would make her
        a search cache with a personality attached, and would also mean she can
        never be wrong about anything for longer than one query.
        """
        if self._beliefs is None:
            return
        try:
            self._beliefs.record_evidence(
                statement=item.statement,
                subject="world",
                supports=True,
                strength=min(0.8, 0.3 + 0.5 * result.confidence),
                source="web_search",
                origin="real_discord",
            )
        except TypeError:
            logger.debug("belief engine does not take search evidence in this shape")
        except Exception:  # noqa: BLE001
            logger.exception("could not offer a search result as evidence")

    async def _announce(
        self, outcome: InvestigationOutcome, gap: KnowledgeGap
    ) -> str | None:
        if self._processor is None:
            return None
        event = Event.create(
            event_type=SEARCH_PERFORMED,
            category="knowledge",
            actor_type="yui",
            source_type=MODULE,
            origin="real_discord",
            priority="P4",
            payload=SearchPerformedPayload(
                search_id=outcome.search_id,
                query=outcome.query,
                outcome=outcome.outcome,
                provider=outcome.provider,
                results=outcome.results,
                future_rejected=outcome.future_rejected,
                acquired=outcome.acquired,
                topic=gap.topic,
            ),
            clock=self._clock,
        )
        await self._processor.process(event)
        for knowledge_id in outcome.acquired_ids:
            await self._processor.process(
                Event.create(
                    event_type=KNOWLEDGE_ACQUIRED,
                    category="knowledge",
                    actor_type="yui",
                    source_type=MODULE,
                    origin="real_discord",
                    priority="P4",
                    payload=KnowledgeAcquiredPayload(
                        knowledge_id=knowledge_id,
                        search_id=outcome.search_id,
                        topic=gap.topic,
                    ),
                    clock=self._clock,
                )
            )
        return event.event_id


#: The tool reports failure as a string, because a tool does not know about
#: search taxonomies. This maps it back, so "the network was down" survives the
#: round trip instead of flattening into a generic error.
_FAILURE_PREFIXES: tuple[str, ...] = (
    "timeout",
    "network_error",
    "provider_error",
)


def _failure_from(outcome: Any, *, provider: str) -> SearchResponse:
    """A tool call that did not succeed, classified as precisely as possible."""
    if hasattr(outcome, "reason_code"):
        # A refusal: the tool was not allowed to run at all. Not a search that
        # found nothing, and not a provider that broke.
        return SearchResponse(
            outcome="refused",
            detail=str(outcome.reason_code),
            provider=provider,
        )
    error = str(getattr(outcome, "error", "") or "")
    for prefix in _FAILURE_PREFIXES:
        if error.startswith(prefix):
            return SearchResponse(
                outcome=prefix,  # type: ignore[arg-type]
                detail=error[len(prefix) :].lstrip(": "),
                provider=provider,
            )
    if "timed out" in error:
        return SearchResponse(outcome="timeout", detail=error, provider=provider)
    return SearchResponse(outcome="provider_error", detail=error, provider=provider)


def _response_from(data: Any) -> SearchResponse:
    """Rebuild the response from what the tool returned."""
    if isinstance(data, SearchResponse):
        return data
    if isinstance(data, dict):
        return SearchResponse(
            outcome=data.get("outcome", "provider_error"),
            results=tuple(
                item if isinstance(item, SearchResult) else SearchResult(**item)
                for item in data.get("results", ())
            ),
            detail=data.get("detail", ""),
            provider=data.get("provider", ""),
        )
    return SearchResponse(outcome="provider_error", detail="unreadable tool result")


__all__ = ["SEARCH_TOOL", "InvestigationOutcome", "InvestigationService"]
