"""Which claim the USER is taking issue with (audit findings 2 and 3).

``review_correction`` used to take ``live[0]`` — the most recent live claim —
whenever the USER pushed back. With one outstanding claim that is right by
accident. With two it is a coin toss, and the failure is silent and wrong in
the worst direction: she retracts something she can support and keeps
something she cannot.

The resolution is the same cite-and-resolve shape used everywhere else in this
subsystem. The model is shown the live claims *with their identifiers* and the
USER's message, and returns one identifier. Python then checks that the
identifier is real and live. The model never edits a claim, never invents an
id, and cannot retract anything by saying so.

**No re-parsing.** The claims shown here are the verified Semantic Claim
representations stored when the reply was delivered — the same objects the
pre-send review produced. Nothing re-reads the sent sentence. That was audit
finding 3: two parsers, one before the send and a weaker one after, disagreeing
about what she had actually claimed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.llm.prompts import PromptRegistry
from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage

logger = logging.getLogger(__name__)

PROMPT_ID = "correction_target"
PURPOSE = "correction_target"


class CorrectionTarget(BaseModel):
    """Which stored claim the USER is correcting, as the model reads it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    #: The identifier of the claim being corrected, from the list supplied.
    #: Empty when the USER is not correcting any of them.
    claim_id: str = Field(default="", max_length=64)
    #: Whether the USER is denying the claim outright or merely questioning it.
    denies: bool = False
    reason: str = Field(default="", max_length=200)


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """What Python concluded, after checking the model's answer."""

    claim_id: str = ""
    denies: bool = False
    #: Set when the model named something that is not a live claim.
    refused: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.claim_id)


class CorrectionResolver:
    """Picks the corrected claim by identity, not by recency."""

    name = "correction_resolver"

    def __init__(
        self, *, prompts: PromptRegistry, structured: StructuredGenerator
    ) -> None:
        self._prompts = prompts
        self._structured = structured

    async def resolve(
        self,
        user_text: str,
        claims: Sequence[Any],
        *,
        recent_conversation: str = "",
        understanding: Any = None,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> ResolvedTarget:
        """Ask which claim, then verify the answer names a real one.

        With no claims there is nothing to resolve. With exactly one there is
        still a question worth asking — the USER may be correcting something
        else entirely — but an unresolved answer falls back to that one claim,
        because "the only outstanding claim" is a defensible reading and
        refusing to act on a clear denial is its own failure.
        """
        live = list(claims)
        if not live:
            return ResolvedTarget()

        allowed = {claim.claim_id for claim in live}
        template = self._prompts.get(PROMPT_ID)
        content = template.render(
            user_message=user_text,
            recent_conversation=recent_conversation or "(なし)",
            turn_understanding=(
                understanding.render() if understanding is not None else "(なし)"
            ),
            live_claims=render_claims(live),
        )
        outcome = await self._structured.generate(
            CorrectionTarget,
            (LLMMessage(role="user", content=content),),
            purpose=PURPOSE,
            run_id=run_id,
            event_id=event_id,
            temperature=0.0,
            max_tokens=300,
            priority="P0",
            prompt_id=PROMPT_ID,
            prompt_version=template.prompt_version,
        )
        if not outcome.accepted or outcome.value is None:
            return self._fallback(live, "resolver_unavailable")

        answer = outcome.value
        if not answer.claim_id:
            return ResolvedTarget(denies=answer.denies)
        if answer.claim_id not in allowed:
            # An invented identifier retracts nothing. The same rule as
            # evidence citation: Python owns whether a name refers.
            logger.info(
                "correction target %r is not a live claim", answer.claim_id[:40]
            )
            return self._fallback(live, f"unknown_claim:{answer.claim_id[:24]}")
        return ResolvedTarget(claim_id=answer.claim_id, denies=answer.denies)

    @staticmethod
    def _fallback(live: Sequence[Any], refused: str) -> ResolvedTarget:
        """Unresolved, and honest about it.

        One outstanding claim is unambiguous enough to act on. Several are not,
        and picking the newest is exactly the guess this class exists to
        remove — so nothing is retracted and the refusal is recorded.
        """
        if len(live) == 1:
            return ResolvedTarget(claim_id=live[0].claim_id, refused=refused)
        return ResolvedTarget(refused=refused)


def render_claims(claims: Sequence[Any]) -> str:
    """The live claims, with the identifiers the model may name.

    Rendered from the stored semantic representation where there is one, so
    what the model sees is what was verified before the send.
    """
    lines: list[str] = []
    for claim in claims:
        semantic = _semantic(claim)
        subject = semantic.get("subject") or getattr(claim, "subject", "") or "不明"
        category = semantic.get("category") or claim.kind
        proposition = semantic.get("proposition") or claim.statement
        lines.append(
            f"- [{claim.claim_id}] ({subject} / {category} / {claim.status}) {proposition}"
        )
    return "\n".join(lines) or "(未解決の主張はない)"


def _semantic(claim: Any) -> dict:
    raw = getattr(claim, "semantic_json", "") or ""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001 - a malformed blob is simply absent
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "CorrectionResolver",
    "CorrectionTarget",
    "PROMPT_ID",
    "PURPOSE",
    "ResolvedTarget",
    "render_claims",
]
