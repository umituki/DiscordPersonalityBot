"""Epistemic Action Selector (spec 17.1, 17.2).

``未知 = 自動 Web Search にしてはならない``. Meeting something YUI does not know
is not a trigger; it is a situation with several possible responses::

    recall  infer  ask_user  web_search  defer  ignore  avoid

Which one fits depends on how much of the gap matters, how curious she is, how
expensive the option is, and whether the USER is right there to ask. A knowledge
gap is also not a boolean: it has a known part, an unknown part and an
uncertainty (spec 17.2), and curiosity is tracked separately from the decision
to actually go looking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from app.agency.policy import EpistemicPolicy
from app.clock import Clock, SystemClock

logger = logging.getLogger(__name__)

MODULE = "epistemic_selector"

EpistemicAction = Literal[
    "recall", "infer", "ask_user", "web_search", "defer", "ignore", "avoid"
]


@dataclass(frozen=True, slots=True)
class KnowledgeGap:
    """What is missing, not merely *that* something is (spec 17.2)."""

    topic: str
    known_part: str = ""
    unknown_part: str = ""
    #: How unsure YUI is about the part she thinks she knows.
    uncertainty: float = 0.5
    #: How much this matters to what is happening now.
    relevance: float = 0.5
    #: Curiosity is a feeling; it is not itself a decision to search.
    curiosity: float = 0.0
    #: True when the answer is likely already in memory.
    likely_in_memory: bool = False
    #: True when the answer changes with time and cannot be reasoned out.
    time_sensitive: bool = False

    @property
    def is_total(self) -> bool:
        return not self.known_part.strip()


@dataclass(frozen=True, slots=True)
class EpistemicDecision:
    action: EpistemicAction
    reason: str
    gap: KnowledgeGap
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def searches(self) -> bool:
        return self.action == "web_search"


class EpistemicActionSelector:
    name = MODULE

    def __init__(
        self,
        policy: EpistemicPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._policy = policy
        self._clock = clock or SystemClock()

    def select(
        self,
        gap: KnowledgeGap,
        *,
        user_present: bool = False,
        search_available: bool = True,
        last_search_failure_at: datetime | None = None,
        busy: bool = False,
    ) -> EpistemicDecision:
        """Choose how to respond to not knowing something."""
        now = self._clock.now()
        scores: dict[str, float] = {}

        # Cheapest first: is it already hers?
        if gap.likely_in_memory:
            scores["recall"] = 0.9
        # Something reasoned from what she has, when the gap is partial and not
        # time-sensitive.
        if not gap.is_total and not gap.time_sensitive:
            scores["infer"] = 0.6 * (1.0 - gap.uncertainty) + 0.2 * gap.relevance

        want = max(gap.curiosity, gap.relevance)
        if want >= self._policy.curiosity_threshold:
            if user_present:
                scores["ask_user"] = want - self._policy.ask_user_cost
                if self._policy.prefer_ask_when_present:
                    scores["ask_user"] += 0.15
            if search_available and self._search_allowed(last_search_failure_at, now):
                scores["web_search"] = want - self._policy.search_cost
        elif want > 0:
            # Mildly interesting: hold on to it rather than act on it.
            scores["defer"] = 0.35 + 0.2 * want

        if busy:
            scores["defer"] = max(scores.get("defer", 0.0), 0.5)
        if gap.relevance < 0.2 and gap.curiosity < 0.2:
            scores["ignore"] = 0.4

        if not scores:
            return EpistemicDecision(
                action="ignore",
                reason="nothing about this gap calls for action",
                gap=gap,
                scores=scores,
            )

        action = max(scores.items(), key=lambda item: item[1])[0]
        logger.debug("epistemic action=%s topic=%s scores=%s", action, gap.topic, scores)
        return EpistemicDecision(
            action=action,  # type: ignore[arg-type]
            reason=self._reason_for(action, gap, user_present),
            gap=gap,
            scores=scores,
        )

    # --- helpers -----------------------------------------------------------
    def _search_allowed(self, last_failure: datetime | None, now: datetime) -> bool:
        """A search that just failed is not retried immediately (spec 17.3)."""
        if last_failure is None:
            return True
        return now - last_failure >= timedelta(hours=self._policy.retry_after_failure_hours)

    @staticmethod
    def _reason_for(action: str, gap: KnowledgeGap, user_present: bool) -> str:
        return {
            "recall": "たぶん覚えている",
            "infer": "知っていることから考えられる",
            "ask_user": "目の前にいる相手に聞くのがいちばん早い",
            "web_search": "自分では埋められないので調べる",
            "defer": "いま追いかけるほどではない",
            "ignore": "気にならない",
            "avoid": "触れたくない",
        }.get(action, "")


def unresolved_gap(gap: KnowledgeGap, *, attempted: EpistemicAction) -> KnowledgeGap:
    """Mark a gap that an attempt failed to close (spec 17.3).

    The gap stays open and honest rather than being quietly filled in from the
    model's own weights.
    """
    return KnowledgeGap(
        topic=gap.topic,
        known_part=gap.known_part,
        unknown_part=gap.unknown_part or f"{attempted} did not answer it",
        uncertainty=min(1.0, gap.uncertainty + 0.1),
        relevance=gap.relevance,
        curiosity=gap.curiosity,
        likely_in_memory=False,
        time_sensitive=gap.time_sensitive,
    )
