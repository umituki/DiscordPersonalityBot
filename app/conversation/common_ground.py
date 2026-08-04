"""Common Ground and correction (rebuild spec 11).

What a conversation is currently treating as true is not the same as what is
true. YUI infers things — 「詩を書いたんだね」 — and the inference then sits in
the conversation as though it were established. When the USER pushes back, the
system needs three things it did not have: a record of what she asserted, a way
to notice the pushback, and a rule for what to do when she cannot back it up.

    supported    evidence stands behind it
    provisional  she said it; nothing contradicts it and nothing supports it
    contested    the USER has questioned it
    retracted    she took it back, and it is out of the conversation for good

CORR-002 is the one that matters and the one that is easy to get wrong: with no
evidence, the correct move is to *retract*, not to explain. A model asked to
justify itself will always find words; the whole failure mode is that it is
good at that.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
from app.grounding.models import GroundingContext

logger = logging.getLogger(__name__)

ClaimStatus = Literal["supported", "provisional", "contested", "retracted"]
CLAIM_STATUSES: tuple[ClaimStatus, ...] = (
    "supported",
    "provisional",
    "contested",
    "retracted",
)

#: Statuses a claim is still part of the conversation under. A retracted claim
#: is gone (CORR-003).
LIVE_STATUSES: tuple[ClaimStatus, ...] = ("supported", "provisional", "contested")


@dataclass(frozen=True, slots=True)
class CommonGroundClaim:
    """One thing this conversation is treating as true (spec 11.1)."""

    claim_id: str
    conversation_id: str
    kind: str
    statement: str
    status: ClaimStatus = "provisional"
    source: str = "yui_inference"
    confidence: str = "low"
    evidence: tuple[str, ...] = ()
    asserted_at: datetime | None = None
    updated_at: datetime | None = None
    event_id: str | None = None
    resolved_reason: str = ""

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    def render(self) -> str:
        return f"claim: {self.statement}\nstatus: {self.status}\nsource: {self.source}\nconfidence: {self.confidence}"


# --- CORR-001: noticing that the USER is pushing back ------------------------

#: Flat denial. 「違う」「そんなこと言ってない」「してないよ」
_DENIAL = re.compile(
    r"(?:違う|ちがう|そんなこと(?:は)?(?:言って|してい?)ない|"
    r"(?:言って|して|書いて|やって)ない|嘘|うそ|そうじゃない|じゃないよ)"
)

#: A short question that is doing the work of a denial. Spec CONV-002: 「詩？」
#: after an unsupported claim is a challenge, not curiosity, and reading it as
#: curiosity is how the real system dug in instead of backing down.
_SHORT_CHALLENGE = re.compile(r"^[^。！？!?\n]{0,12}[？?]$")

#: Explicit surprise at being told something about oneself.
_SURPRISE = re.compile(r"(?:え[？?！!]|えっ|いつ(?:そんな|の話)|なんで(?:そう|そんな))")


@dataclass(frozen=True, slots=True)
class CorrectionSignal:
    """What the USER's message says about YUI's previous claim (CORR-001)."""

    detected: bool = False
    trigger: str = ""
    strength: Literal["none", "questioned", "denied"] = "none"

    @property
    def is_denial(self) -> bool:
        return self.strength == "denied"


def detect_correction(user_text: str) -> CorrectionSignal:
    """Whether this message challenges what YUI just claimed.

    CONV-001: the reading is of the message *as an answer to the previous
    turn*, which is why the caller only consults this when a live claim exists.
    A bare 「詩？」 with nothing outstanding is a question about poetry.
    """
    text = (user_text or "").strip()
    if not text:
        return CorrectionSignal()

    found = _DENIAL.search(text)
    if found is not None:
        return CorrectionSignal(detected=True, trigger=found.group(0), strength="denied")

    found = _SURPRISE.search(text)
    if found is not None:
        return CorrectionSignal(
            detected=True, trigger=found.group(0), strength="questioned"
        )

    if _SHORT_CHALLENGE.match(text):
        return CorrectionSignal(detected=True, trigger=text, strength="questioned")

    return CorrectionSignal()


# --- the read model ---------------------------------------------------------


class CommonGroundTracker:
    """Records what YUI asserted, and re-checks it when challenged.

    It owns no psychology and decides no reply. It answers two questions: what
    is this conversation treating as true, and does the thing the USER just
    pushed back on still stand.
    """

    name = "common_ground_tracker"

    def __init__(
        self,
        repository,
        *,
        extractor: ClaimExtractor,
        guard: ClaimGroundingGuard,
    ) -> None:
        self._repository = repository
        self._extractor = extractor
        self._guard = guard

    # --- writing ---------------------------------------------------------
    def record_reply(
        self,
        text: str,
        *,
        conversation_id: str,
        event_id: str | None,
        context: GroundingContext | None,
        reviewed: object | None = None,
        now: datetime,
    ) -> tuple[CommonGroundClaim, ...]:
        """Enter the claims a delivered reply made into the common ground.

        Only a *delivered* reply: a suppressed draft asserted nothing, so it
        enters nothing (the same rule as GROUND-004).

        ``reviewed`` is the semantic review that was already run *before* the
        send, and passing it is the point. Re-extracting and re-resolving the
        same sentence afterwards meant two different authorities decided what
        the reply had claimed: the pre-send reviewer read the proposition with
        the turn's context, and the post-send extractor read the surface
        patterns without it. They disagreed, and the common ground recorded the
        second answer. Now the verified claims are carried forward, and the
        extractor is only the fallback for callers that have none.
        """
        if reviewed is not None and not getattr(reviewed, "unavailable", False):
            return self._record_reviewed(
                reviewed,
                conversation_id=conversation_id,
                event_id=event_id,
                now=now,
            )
        claims = self._extractor.extract(text)
        if not claims:
            return ()
        verdict = (
            self._guard.review(text, context) if context is not None else None
        )
        supported = {
            item.claim.trigger for item in (verdict.claims if verdict else ()) if item.supported
        }
        recorded: list[CommonGroundClaim] = []
        for claim in claims:
            recorded.append(
                self._repository.record(
                    conversation_id=conversation_id,
                    event_id=event_id,
                    kind=claim.kind,
                    statement=claim.text,
                    status="supported" if claim.trigger in supported else "provisional",
                    source="yui_inference",
                    confidence="high" if claim.trigger in supported else "low",
                    now=now,
                )
            )
        return tuple(recorded)

    def _record_reviewed(
        self,
        reviewed,
        *,
        conversation_id: str,
        event_id: str | None,
        now: datetime,
    ) -> tuple[CommonGroundClaim, ...]:
        """Record exactly what was verified before the send, unchanged.

        The proposition is stored rather than the surface sentence, because the
        proposition is what a later correction has to be matched against.
        """
        recorded: list[CommonGroundClaim] = []
        for claim in reviewed.claims:
            if not claim.needs_evidence:
                # A wish or a question asserted nothing, so the conversation is
                # not now treating anything as true.
                continue
            recorded.append(
                self._repository.record(
                    conversation_id=conversation_id,
                    event_id=event_id,
                    kind=claim.candidate.category,
                    statement=claim.candidate.proposition or claim.candidate.trigger,
                    status="supported" if claim.supported else "provisional",
                    source="yui_inference",
                    confidence="high" if claim.supported else "low",
                    now=now,
                )
            )
        return tuple(recorded)

    # --- reading ---------------------------------------------------------
    def live_claims(self, conversation_id: str, *, limit: int = 10) -> tuple[CommonGroundClaim, ...]:
        """What the conversation still treats as true (CORR-003 excludes the rest)."""
        return tuple(self._repository.live(conversation_id, limit=limit))

    def render(self, conversation_id: str, *, limit: int = 5) -> str:
        claims = self.live_claims(conversation_id, limit=limit)
        if not claims:
            return ""
        return "\n\n".join(claim.render() for claim in claims)

    # --- CORR-001..003 ---------------------------------------------------
    def review_correction(
        self,
        user_text: str,
        *,
        conversation_id: str,
        context: GroundingContext | None,
        now: datetime,
    ) -> "CorrectionOutcome":
        """Re-verify YUI's own previous claim when the USER pushes back.

        CORR-001 is the trigger, CORR-002 is the decision, CORR-003 is the
        consequence. The claim is re-resolved against evidence *now* — not
        argued about, not explained, not re-justified by another model call.
        """
        signal = detect_correction(user_text)
        if not signal.detected:
            return CorrectionOutcome()

        live = self.live_claims(conversation_id, limit=5)
        if not live:
            # A short challenge with nothing outstanding (「詩？」) is just a
            # question.  An explicit denial is different: the USER has said
            # our explanation is wrong even when that explanation did not
            # contain one of Grounding's dangerous factual claim shapes.
            # Dropping that signal made the real model argue back.
            if signal.is_denial:
                return CorrectionOutcome(signal=signal, retracted=True)
            return CorrectionOutcome()

        target = live[0]
        supported = self._still_supported(target, context)
        if supported:
            # It stands. She may say so, and it stays in the common ground —
            # marked contested, because the USER does not accept it.
            updated = self._repository.set_status(
                target.claim_id,
                status="contested",
                reason=f"user_challenged:{signal.trigger}"[:200],
                now=now,
            )
            return CorrectionOutcome(signal=signal, claim=updated, retracted=False)

        # CORR-002: no evidence, so she takes it back rather than defending it.
        logger.info(
            "retracting unsupported claim claim_id=%s kind=%s",
            target.claim_id,
            target.kind,
        )
        updated = self._repository.set_status(
            target.claim_id,
            status="retracted",
            reason=f"unsupported_after_challenge:{signal.trigger}"[:200],
            now=now,
        )
        return CorrectionOutcome(signal=signal, claim=updated, retracted=True)

    def _still_supported(
        self, claim: CommonGroundClaim, context: GroundingContext | None
    ) -> bool:
        if context is None:
            # Nothing to check against. CORR-002 leans to retraction, so an
            # unverifiable claim is not treated as verified.
            return False
        verdict = self._guard.review(claim.statement, context)
        if not verdict.claims:
            # The sentence carries no factual assertion any more — nothing to
            # retract, and nothing to defend either.
            return True
        return all(item.supported for item in verdict.claims)


@dataclass(frozen=True, slots=True)
class CorrectionOutcome:
    """What the correction review concluded."""

    signal: CorrectionSignal = CorrectionSignal()
    claim: CommonGroundClaim | None = None
    retracted: bool = False

    @property
    def happened(self) -> bool:
        return self.signal.detected and (self.claim is not None or self.retracted)

    def render(self) -> str:
        """What the reply prompt is told. Never a sentence to say."""
        if not self.happened:
            return ""
        if self.claim is None:
            return (
                "相手が直前の説明を明示的に訂正した。事実関係を争わず、"
                "短く認めて謝り、訂正を受け入れること。相手が誤解したとは言わない。"
            )
        if self.retracted:
            return (
                "直前に自分が言った「"
                f"{self.claim.statement}"
                "」には裏づけがない。説明して押し切らず、取り消して認めること。"
            )
        return (
            "直前に自分が言った「"
            f"{self.claim.statement}"
            "」は裏づけがある。ただし相手は納得していない。"
        )


__all__ = [
    "CLAIM_STATUSES",
    "LIVE_STATUSES",
    "ClaimStatus",
    "CommonGroundClaim",
    "CommonGroundTracker",
    "CorrectionOutcome",
    "CorrectionSignal",
    "detect_correction",
]
