"""Stage 1 — Candidate Generation (rebuild spec 17.3, Phase 2 §2B, §2C).

Finding memories that *might* be about the query, and nothing else.

Two rules make this stage what it is. It does not rank by accessibility (§2B):
a memory is a candidate because there is a reason to think it relates to the
query, never because it is easy to reach. And it has no side effects at all
(§2C) — no ``recall_count``, no ``last_recalled_at``, no practice. Searching is
not remembering, and the old code's failure to say that is what pinned memories
at accessibility 1.0.

Ways in:

* full-text match on the summary,
* the autobiographical bridge — 「何歳」「生まれ」「最初の記憶」 reach the Genesis
  memories that hold those facts (§2G, kept, but demoted to *finding*
  candidates rather than deciding recall),
* shared topics,
* time hints in the query,
* recency, for modes where "lately" is the question.

Importance can raise a memory into the pool for reflective modes, because 「一番
印象に残っているのは」 is a question about importance. It never does so for
ordinary conversation.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Sequence

from app.memory.models import EpisodicMemory
from app.memory.policy import CandidatePolicy
from app.memory.recall_mode import RecallMode
from app.memory.recall_models import Candidate
from app.storage.repositories.memory import MemoryRepository

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_NON_WORD = re.compile(r"[^\w\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+")

MAX_MATCH_TERMS = 12

#: Grammatical scaffolding, dropped before a query is turned into topic probes.
_PARTICLES = frozenset("はがをにでとへもやのねよなかだですますましたたるらしいうくっ、。！？!?　 ")

#: Query shapes that point at facts about her own life, and the topics that
#: hold them. Kept from the conceptual-bridge fix (§2G).
AUTOBIOGRAPHICAL_BRIDGE: dict[str, tuple[str, ...]] = {
    r"何歳|なんさい|歳は|年齢|いくつ": ("誕生", "生まれ", "年齢", "こども"),
    r"生まれ|誕生日|出身": ("誕生", "生まれ", "家", "こども"),
    r"最初の記憶|一番古い記憶": ("こども", "はじめて", "最初"),
    r"子供の頃|子どもの頃|昔の(?:こと|話)": ("こども", "学校", "家", "むかし"),
}

#: Rough time words. They narrow *where* to look, not what is relevant.
_TIME_HINTS: dict[str, int] = {
    "今日": 1,
    "きょう": 1,
    "昨日": 2,
    "きのう": 2,
    "最近": 14,
    "この前": 30,
    "こないだ": 30,
    "先週": 14,
    "先月": 60,
}


def build_match_query(
    text: str, *, min_chars: int = 3, max_terms: int = MAX_MATCH_TERMS
) -> str | None:
    """Turn free text into an FTS5 trigram query.

    Japanese has no word boundaries, so long runs are sliced into overlapping
    fragments; Latin words are used whole.
    """
    cleaned = _NON_WORD.sub(" ", text or "").strip()
    if not cleaned:
        return None

    terms: list[str] = []
    for token in cleaned.split():
        if len(token) < min_chars:
            continue
        if _CJK.search(token) and len(token) > 4:
            for start in range(0, len(token) - 2, 2):
                fragment = token[start : start + 3]
                if len(fragment) >= min_chars:
                    terms.append(fragment)
        else:
            terms.append(token)

    unique = list(dict.fromkeys(terms))[:max_terms]
    if not unique:
        return None
    return " OR ".join(f'"{term}"' for term in unique)


def bridge_topics(text: str) -> tuple[str, ...]:
    """Topics an autobiographical question reaches for (§2G)."""
    found: list[str] = []
    for pattern, topics in AUTOBIOGRAPHICAL_BRIDGE.items():
        if re.search(pattern, text or ""):
            found.extend(topics)
    return tuple(dict.fromkeys(found))


def query_topic_probes(text: str, *, max_probes: int = 8) -> tuple[str, ...]:
    """Topic probes taken from the query itself.

    FTS needs three characters to build a term, so 「海」 and 「本」 — which are
    perfectly ordinary things to ask about in Japanese — matched nothing at all.
    The old code hid that behind a blanket "return recent memories" fallback,
    which is the very thing §2B removes: recency is not aboutness. Probing the
    topic index instead keeps the short query reachable *by its content*.

    Over-matching here is cheap. Stage 2 still has to find the candidate
    relevant, so a loose probe costs a judgement, not a false memory.
    """
    cleaned = _NON_WORD.sub("", text or "").strip()
    if not cleaned:
        return ()
    probes = [cleaned] if len(cleaned) <= 6 else []
    # Topics are single words — 「海」「花火」 — so the probes have to be words
    # too. Dropping the grammatical scaffolding first is what turns 「海の話」
    # into 「海」 and 「話」 rather than into a phrase that matches no topic at
    # all. Over-matching is cheap here: Stage 2 still has to pass it.
    content = [char for char in cleaned if char not in _PARTICLES]
    probes.extend(char for char in content if _CJK.match(char))
    probes.extend(
        "".join(content[index : index + 2]) for index in range(max(0, len(content) - 1))
    )
    return tuple(dict.fromkeys(probe for probe in probes if probe))[:max_probes]


def time_hint_days(text: str) -> int | None:
    """How far back a time word in the query points, if any."""
    windows = [days for word, days in _TIME_HINTS.items() if word in (text or "")]
    return min(windows) if windows else None


class CandidateGenerator:
    """Stage 1. Reads only; writes nothing (§2C)."""

    name = "candidate_generator"

    def __init__(self, repository: MemoryRepository, policy: CandidatePolicy) -> None:
        self._repository = repository
        self._policy = policy

    def generate(
        self,
        query_text: str,
        *,
        mode: RecallMode,
        now: datetime,
        origins: Sequence[str] | None = None,
        cues: Sequence[str] = (),
    ) -> tuple[Candidate, ...]:
        limits = self._policy.for_mode(mode.value)
        found: dict[str, list[str]] = {}
        memories: dict[str, EpisodicMemory] = {}

        def add(memory: EpisodicMemory, reason: str) -> None:
            # Suppressed and invalidated memories are unreachable in every
            # mode (test 13). The repository filters by status, and this is the
            # belt to that pair of braces.
            if not memory.is_recallable:
                return
            memories.setdefault(memory.memory_id, memory)
            reasons = found.setdefault(memory.memory_id, [])
            if reason not in reasons:
                reasons.append(reason)

        pool = limits.pool
        for memory in self._by_text(query_text, pool, origins):
            add(memory, "fts")
        for memory in self._by_topics(query_topic_probes(query_text), pool, origins):
            add(memory, "topic")
        for memory in self._by_topics(bridge_topics(query_text), pool, origins):
            add(memory, "autobiographical_bridge")
        for memory in self._by_topics(tuple(cues), pool, origins):
            add(memory, "cue")
        for memory in self._by_time_hint(query_text, now, pool, origins):
            add(memory, "time_hint")
        if limits.include_recent:
            for memory in self._recent(pool, origins):
                add(memory, "recent")
        if limits.include_important:
            for memory in self._important(pool, origins):
                add(memory, "importance")

        candidates = [
            Candidate(memory=memories[memory_id], reasons=tuple(reasons))
            for memory_id, reasons in found.items()
        ]
        # Ordered by how many independent ways led here, then by how long ago
        # it happened. Not by accessibility: that is Stage 3's variable, and
        # letting it in here is precisely the feedback loop being removed.
        candidates.sort(
            key=lambda item: (len(item.reasons), item.memory.occurred_at),
            reverse=True,
        )
        return tuple(candidates[: limits.max_candidates])

    # --- ways in ------------------------------------------------------------
    def _by_text(
        self, query_text: str, pool: int, origins: Sequence[str] | None
    ) -> list[EpisodicMemory]:
        match_query = build_match_query(
            query_text, min_chars=self._policy.min_query_chars
        )
        if not match_query:
            return []
        try:
            return self._repository.search(
                match_query=match_query, limit=pool, origins=origins
            )
        except Exception:  # noqa: BLE001 - a bad query must not break a reply
            logger.exception("full-text search failed query=%r", match_query)
            return []

    def _by_topics(
        self, topics: Sequence[str], pool: int, origins: Sequence[str] | None
    ) -> list[EpisodicMemory]:
        if not topics:
            return []
        try:
            return self._repository.by_topics(topics, limit=pool, origins=origins)
        except Exception:  # noqa: BLE001
            logger.exception("topic search failed topics=%r", topics)
            return []

    def _by_time_hint(
        self,
        query_text: str,
        now: datetime,
        pool: int,
        origins: Sequence[str] | None,
    ) -> list[EpisodicMemory]:
        days = time_hint_days(query_text)
        if days is None:
            return []
        try:
            return self._repository.occurred_between(
                start=now - timedelta(days=days), end=now, limit=pool, origins=origins
            )
        except Exception:  # noqa: BLE001
            logger.exception("time-hint search failed days=%s", days)
            return []

    def _recent(self, pool: int, origins: Sequence[str] | None) -> list[EpisodicMemory]:
        try:
            return self._repository.search(match_query=None, limit=pool, origins=origins)
        except Exception:  # noqa: BLE001
            logger.exception("recent search failed")
            return []

    def _important(
        self, pool: int, origins: Sequence[str] | None
    ) -> list[EpisodicMemory]:
        try:
            return self._repository.most_important(
                limit=pool, origins=origins, min_importance=self._policy.min_importance
            )
        except Exception:  # noqa: BLE001
            logger.exception("importance search failed")
            return []


__all__ = [
    "AUTOBIOGRAPHICAL_BRIDGE",
    "CandidateGenerator",
    "bridge_topics",
    "build_match_query",
    "time_hint_days",
]
