"""Stage 2 — Semantic Relevance Judgment (rebuild spec 17.3, Phase 2 §2E, §2F).

The hard gate. A candidate passes only if something judged it to be *about* the
query — and accessibility is not consulted here at all, which is the point:

    semantic relevance < threshold  →  Recall 禁止, たとえ accessibility が 1.0 でも

That single rule breaks the loop the old system was in. A memory that had been
recalled often scored highly on accessibility, scored highly overall, surfaced
again, and was strengthened again. Nothing in that circuit ever asked whether it
had anything to do with what the USER had just said.

The model answers in categories (``irrelevant`` / ``weak`` / ``relevant`` /
``strong``) rather than decimals, for the same reason the appraisal does: a
category is a judgement it can defend, and there is no 0.31 to argue about.

**Failure policy.** When the model times out or answers unusably, Python falls
back to a deterministic lexical check that can only produce ``relevant`` or
``irrelevant`` on hard evidence of shared content. If that finds nothing, the
recall is empty. What must never happen is falling back to accessibility order:
a broken reranker is a reason to remember nothing, not a reason to say whatever
is nearest to hand. Recalling nothing is always safer than fabricating.
"""

from __future__ import annotations

import logging
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage
from app.memory.recall_mode import RecallMode
from app.memory.recall_models import (
    Candidate,
    RelevanceJudgement,
    RelevanceLabel,
)

logger = logging.getLogger(__name__)

PROMPT_ID = "memory_relevance"
PURPOSE = "memory_relevance"

#: How many characters of a memory the reranker sees. Enough to judge topic,
#: not so much that sixteen candidates blow the context budget.
SUMMARY_CHARS = 160


class MemoryRelevance(BaseModel):
    """One candidate's verdict (§2E)."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    relevance: RelevanceLabel
    reason: str = ""


class MemoryRelevanceBatch(BaseModel):
    """Every candidate judged in one call, to keep a live turn to one call."""

    model_config = ConfigDict(extra="forbid")

    judgements: list[MemoryRelevance] = Field(default_factory=list)


class SemanticReranker:
    """Stage 2. Decides aboutness, and nothing about reachability."""

    name = "semantic_reranker"

    def __init__(
        self,
        *,
        structured: StructuredGenerator,
        prompts: PromptRegistry,
    ) -> None:
        self._structured = structured
        self._prompts = prompts

    async def judge(
        self,
        query_text: str,
        candidates: Sequence[Candidate],
        *,
        mode: RecallMode,
        batch_size: int = 16,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> tuple[tuple[RelevanceJudgement, ...], str | None, str]:
        """Judge every candidate.

        Returns the judgements, the LLM call id when there was one, and which
        path was taken (``llm`` or ``fallback``) — all three are needed by the
        observability record, and the third is what makes a silent degradation
        impossible to mistake for a working reranker.
        """
        if not candidates:
            return (), None, "llm"

        batch = list(candidates)[:batch_size]
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            query=query_text or "(質問なし)",
            mode=mode.value,
            candidates=_render(batch),
        )
        outcome = await self._structured.generate(
            MemoryRelevanceBatch,
            (LLMMessage(role="user", content=content),),
            purpose=PURPOSE,
            run_id=run_id,
            event_id=event_id,
            # Behind the reply itself: the USER is waiting on the sentence, and
            # this is the work that decides what goes into it.
            priority="P1",
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        call_id = outcome.call_ids[-1] if outcome.call_ids else None

        if not outcome.accepted or outcome.value is None:
            logger.info(
                "memory rerank unavailable; falling back to a conservative check "
                "(candidates=%d)",
                len(batch),
            )
            return fallback_judgements(query_text, batch), call_id, "fallback"

        known = {candidate.memory_id for candidate in batch}
        judged: dict[str, RelevanceJudgement] = {}
        for item in outcome.value.judgements:
            if item.memory_id not in known:
                # A memory_id the model invented is not a judgement about
                # anything. Dropping it is the only safe reading.
                continue
            judged[item.memory_id] = RelevanceJudgement(
                memory_id=item.memory_id,
                relevance=item.relevance,
                reason=item.reason[:200],
                source="llm",
            )

        # A candidate the model did not mention was not judged relevant. It
        # does not get the benefit of the doubt (§2F).
        for candidate in batch:
            judged.setdefault(
                candidate.memory_id,
                RelevanceJudgement(
                    memory_id=candidate.memory_id,
                    relevance="irrelevant",
                    reason="モデルが判定を返さなかった",
                    source="llm",
                ),
            )
        return tuple(judged[c.memory_id] for c in batch), call_id, "llm"


def fallback_judgements(
    query_text: str, candidates: Sequence[Candidate]
) -> tuple[RelevanceJudgement, ...]:
    """Deterministic, conservative, and deliberately not very good.

    It passes a candidate only on hard lexical evidence — content characters the
    query and the memory actually share. It cannot see paraphrase, so most
    things it does not pass are things it simply could not judge, and the right
    consequence of "could not judge" is not remembering.
    """
    query_tokens = _query_tokens(query_text)
    judgements: list[RelevanceJudgement] = []
    for candidate in candidates:
        if not query_tokens:
            judgements.append(
                RelevanceJudgement(
                    memory_id=candidate.memory_id,
                    relevance="irrelevant",
                    reason="rerank 不能、query に手がかりなし",
                    source="fallback",
                )
            )
            continue
        haystack = _haystack_tokens(
            candidate.memory.summary + " " + " ".join(candidate.memory.topics)
        )
        shared = query_tokens & haystack
        overlap = len(shared) / max(1, len(query_tokens))
        if overlap >= 0.34:
            label: RelevanceLabel = "relevant"
            reason = f"rerank 不能、語の重なり {overlap:.0%}"
        else:
            label = "irrelevant"
            reason = f"rerank 不能、語の重なり {overlap:.0%} で判定できず"
        judgements.append(
            RelevanceJudgement(
                memory_id=candidate.memory_id,
                relevance=label,
                reason=reason,
                source="fallback",
            )
        )
    return tuple(judgements)


def _render(candidates: Sequence[Candidate]) -> str:
    lines = []
    for candidate in candidates:
        topics = "、".join(candidate.memory.topics) or "なし"
        lines.append(
            f"- memory_id: {candidate.memory_id}\n"
            f"  内容: {candidate.memory.summary[:SUMMARY_CHARS]}\n"
            f"  話題: {topics}"
        )
    return "\n".join(lines)


#: Grammatical scaffolding. Japanese has no spaces, so overlap is measured on
#: what is left after the particles are dropped.
_PARTICLES = frozenset("はがをにでとへもやのねよなかだですますましたたるらしいうくっ、。！？!?　 ")


def _kept(text: str) -> list[str]:
    return [char for char in (text or "") if char not in _PARTICLES]


def _query_tokens(text: str) -> frozenset[str]:
    """What the query is about.

    A one- or two-character query — 「海」「本」 — has no bigrams, and treating it
    as having no content meant the fallback could never judge the shortest and
    most common Japanese questions. Below three characters the characters
    themselves are the content.
    """
    kept = _kept(text)
    if len(kept) <= 2:
        return frozenset(kept)
    return frozenset("".join(kept[i : i + 2]) for i in range(len(kept) - 1))


def _haystack_tokens(text: str) -> frozenset[str]:
    """What a memory could match against — single characters and bigrams both,
    so a short query and a long one are both answerable."""
    kept = _kept(text)
    tokens = set(kept)
    tokens.update("".join(kept[i : i + 2]) for i in range(max(0, len(kept) - 1)))
    return frozenset(tokens)


__all__ = [
    "MemoryRelevance",
    "MemoryRelevanceBatch",
    "SemanticReranker",
    "fallback_judgements",
]
