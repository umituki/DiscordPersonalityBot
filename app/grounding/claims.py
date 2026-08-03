"""Claim extraction and evidence resolution (rebuild spec 15.2, 15.3)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.grounding.models import (
    SELF_CLAIM_KINDS,
    Claim,
    Evidence,
    GroundedClaim,
    GroundingContext,
)
from app.grounding.policy import GroundingPolicy

logger = logging.getLogger(__name__)

#: Sentences, with their terminator kept. Splitting on 「。」 would lose the
#: mark that tells an assertion from a question, and a question is not a claim.
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]*")


class ClaimExtractor:
    """Finds the dangerous factual assertions in a draft (spec 15.2)."""

    def __init__(self, policy: GroundingPolicy) -> None:
        self._policy = policy
        self._rules = tuple(
            (rule, rule.compiled()) for rule in policy.rules if rule.patterns
        )

    def extract(self, text: str) -> tuple[Claim, ...]:
        claims: list[Claim] = []
        for sentence in _sentences(text):
            if _is_question(sentence):
                # 「今日は本を読んだ？」 asks; it does not assert.
                continue
            if _is_hedged(sentence):
                # 「読みたいな」「読んだ気がする」 does not claim it happened.
                continue
            for rule, patterns in self._rules:
                for pattern in patterns:
                    found = pattern.search(sentence)
                    if found is None:
                        continue
                    claims.append(
                        Claim(
                            kind=rule.kind,  # type: ignore[arg-type]
                            text=sentence.strip(),
                            trigger=found.group(0),
                            severity=rule.severity,  # type: ignore[arg-type]
                        )
                    )
                    break
        return tuple(claims[: self._policy.limits.max_claims])


@dataclass(frozen=True, slots=True)
class GroundingVerdict:
    """What the guard concluded about one draft."""

    claims: tuple[GroundedClaim, ...] = ()

    @property
    def unsupported(self) -> tuple[GroundedClaim, ...]:
        return tuple(item for item in self.claims if not item.supported)

    @property
    def blocking(self) -> tuple[GroundedClaim, ...]:
        """Unsupported claims that are hard enough to hold the send (spec 16)."""
        return tuple(
            item for item in self.unsupported if item.claim.severity == "hard"
        )

    @property
    def accepted(self) -> bool:
        return not self.blocking

    @property
    def reason_code(self) -> str:
        if self.accepted:
            return ""
        return f"unsupported_{self.blocking[0].claim.kind}"

    def describe(self) -> str:
        """What to tell the repair call. Never what to write instead."""
        return "\n".join(f"- {item.describe()}" for item in self.blocking)


class ClaimGroundingGuard:
    """Resolves each claim against what is actually known (spec 15.3).

    GROUND-001 lives in the signature: the only things it reads are the draft
    and a :class:`GroundingContext` assembled from other subsystems' rows. It
    has no access to the conversation history, so YUI repeating herself cannot
    turn into evidence, and it cannot consult the model, so the model cannot
    vouch for itself.
    """

    name = "claim_grounding_guard"

    def __init__(self, extractor: ClaimExtractor) -> None:
        self._extractor = extractor

    def review(self, text: str, context: GroundingContext) -> GroundingVerdict:
        claims = self._extractor.extract(text)
        if not claims:
            return GroundingVerdict()
        resolved = tuple(
            GroundedClaim(claim=claim, evidence=self._resolve(claim, context))
            for claim in claims
        )
        for item in resolved:
            if not item.supported and item.claim.severity == "hard":
                logger.info(
                    "unsupported claim kind=%s trigger=%r",
                    item.claim.kind,
                    item.claim.trigger,
                )
        return GroundingVerdict(claims=resolved)

    @staticmethod
    def _resolve(claim: Claim, context: GroundingContext) -> tuple[Evidence, ...]:
        """Spec 15.3. Evidence must be of an accepted kind, about the thing
        being claimed, and — for a claim about YUI's own doing — about *her*.

        The last of those was learned the hard way: with only the first two,
        the USER saying 「小説読むの好き」 was enough to support 「昨日わたしも小説
        を読んだよ」, because the two sentences share the word. Sharing a topic
        with something the USER said is not having done it.
        """
        candidates = context.of_kinds(claim.accepted_evidence)
        if claim.kind in SELF_CLAIM_KINDS:
            candidates = tuple(item for item in candidates if item.is_yuis_own)
        return tuple(item for item in candidates if item.matches(claim.text))


def _sentences(text: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _SENTENCE.finditer(text))


def _is_question(sentence: str) -> bool:
    return sentence.rstrip().endswith(("？", "?")) or "でしょうか" in sentence


#: Phrasings that turn an assertion into a wish, a guess or a plan. None of
#: these claim that anything happened, so none of them need evidence.
_HEDGES = (
    "たい",
    "かな",
    "かも",
    "気がする",
    "ような気",
    "だろう",
    "でしょう",
    "つもり",
    "予定",
    "みたい",
)


def _is_hedged(sentence: str) -> bool:
    return any(hedge in sentence for hedge in _HEDGES)


__all__ = ["ClaimExtractor", "ClaimGroundingGuard", "GroundingVerdict"]
