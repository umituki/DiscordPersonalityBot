"""Eight critics, each with one job (rebuild spec 34.10 — Phase 12).

    一つの巨大 critic に全部やらせない。

A single critic asked to check chronology, continuity, development, psychology,
historical reality, repetition, identity and memory plausibility at once checks
none of them in particular. Worse, its verdict cannot be acted on: it says the
year is wrong without saying which part, so the only available repair is to
regenerate everything, which changes the parts that were fine.

Eight purposes, eight prompts, eight rows. High and fatal issues regenerate the
part they name and nothing else.

GEN-CRITIC-001 is the rule that makes the rest of it worth having::

    Audit が失敗したのに silent pass して次へ進まない。

So a failed audit is a row *and* a stop. :class:`CriticBoard` returns whether
the stage may proceed, and the caller that ignores it is the bug.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Sequence

from app import ids
from app.clock import Clock, SystemClock
from app.genesis.models import BLOCKING, CriticIssue, CriticType, CriticVerdict
from app.llm.types import LLMMessage

logger = logging.getLogger(__name__)

MODULE = "genesis_critics"
AUDIT = "aud"

#: 34.10's list, in the order they are cheapest to check.
CRITICS: tuple[CriticType, ...] = (
    "chronology",
    "continuity",
    "identity",
    "historical_reality",
    "development",
    "psychology",
    "narrative_realism",
    "memory_plausibility",
)

#: Critics that Python can run without a model, because they check arithmetic
#: and declared facts rather than judgement. Running these locally means a
#: model outage cannot turn an age error into a silent pass.
DETERMINISTIC: frozenset[str] = frozenset({"chronology", "identity"})

#: Critics that must actually run before a year may advance. Being unable to
#: reach the model is not the same as the model approving, and treating it as
#: approval is the silent pass GEN-CRITIC-001 forbids — so an unavailable
#: required critic blocks, retryably.
REQUIRED: frozenset[str] = frozenset(
    {"chronology", "identity", "continuity", "historical_reality"}
)

PROMPT_PREFIX = "genesis_critic"


#: What a critic actually concluded. Three states, not two: a critic that
#: could not run has neither approved nor objected, and collapsing that into
#: either is a lie in one direction or the other.
Outcome = Literal["pass", "fail", "unavailable"]


@dataclass(frozen=True, slots=True)
class Review:
    """The board's verdict on one target."""

    ok: bool
    blocking: tuple[CriticIssue, ...] = ()
    unavailable: tuple[str, ...] = ()

    @property
    def retryable(self) -> bool:
        """True when the block is "we could not check", not "this is wrong".

        The difference decides what a resume does: an unreachable model is
        worth trying again, and a fatal continuity break is not.
        """
        return bool(self.unavailable) and not self.blocking


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    """What is being checked, with enough context to check it."""

    target_type: str
    target_id: str
    text: str
    age_start: int = 0
    age_end: int = 0
    period_start: datetime | None = None
    period_end: datetime | None = None
    anchors: str = ""
    continuity: str = ""
    previous: str = ""


def check_chronology(target: ReviewTarget) -> CriticVerdict:
    """Ages and dates, in Python (34.1).

    The one critic that must never depend on a model: it exists to catch the
    arithmetic a model gets wrong, and asking a model to check it would put the
    same failure on both sides of the check.
    """
    issues: list[CriticIssue] = []
    if target.age_end < target.age_start:
        issues.append(
            CriticIssue(
                severity="fatal",
                target_id=target.target_id,
                code="AGE_REVERSED",
                reason=f"age runs {target.age_start} → {target.age_end}",
                repair_scope=target.target_type,
            )
        )
    if (
        target.period_start is not None
        and target.period_end is not None
        and target.period_end < target.period_start
    ):
        issues.append(
            CriticIssue(
                severity="fatal",
                target_id=target.target_id,
                code="PERIOD_REVERSED",
                reason="the period ends before it starts",
                repair_scope=target.target_type,
            )
        )
    for wrong_age in _stated_ages(target.text):
        if not (target.age_start - 1 <= wrong_age <= target.age_end + 1):
            issues.append(
                CriticIssue(
                    severity="high",
                    target_id=target.target_id,
                    code="AGE_MISMATCH",
                    reason=f"the text says {wrong_age} in a period covering "
                    f"{target.age_start}-{target.age_end}",
                    repair_scope=target.target_type,
                )
            )
    return CriticVerdict(passed=not issues, issues=tuple(issues))


def check_identity(target: ReviewTarget) -> CriticVerdict:
    """The immutable rules, in Python (spec 1.3, 34.1).

    She is a digital being. A scaffold that has her eating breakfast is not a
    style problem to be nudged in a prompt; it is a contradiction of an anchor,
    and it stops the year.
    """
    issues: list[CriticIssue] = []
    for phrase in _PHYSICAL_MARKERS:
        if phrase in target.text:
            issues.append(
                CriticIssue(
                    severity="high",
                    target_id=target.target_id,
                    code="EMBODIMENT_CONTRADICTION",
                    reason=f"{phrase!r} contradicts the embodiment anchor",
                    repair_scope=target.target_type,
                )
            )
    return CriticVerdict(passed=not issues, issues=tuple(issues))


class CriticBoard:
    """Runs the critics and records every verdict, including the failures."""

    name = MODULE

    def __init__(
        self,
        *,
        audits: Any,
        structured: Any = None,
        prompts: Any = None,
        genesis_run_id: str | None = None,
        clock: Clock | None = None,
        enabled: Sequence[CriticType] = CRITICS,
    ) -> None:
        self._audits = audits
        self._structured = structured
        self._prompts = prompts
        self._run_id = genesis_run_id
        self._clock = clock or SystemClock()
        self._enabled = tuple(enabled)

    async def review(self, target: ReviewTarget) -> Review:
        """Every enabled critic, in order. Says whether the stage may go on.

        GEN-CRITIC-001: a blocking issue means it may not, and the verdict is
        written down either way. An audit that failed and was passed over
        silently is the one outcome the spec forbids, and it is only detectable
        because the failure is a row.

        A *required* critic that could not run also stops the stage. It has not
        approved anything — it has not looked — and letting the year proceed on
        that basis is the same silent pass wearing a different hat.
        """
        blocking: list[CriticIssue] = []
        unavailable: list[str] = []
        for critic in self._enabled:
            verdict, outcome = await self._run(critic, target)
            self._record(critic, target, verdict)
            blocking.extend(verdict.blocking_issues)
            if outcome == "unavailable" and critic in REQUIRED:
                unavailable.append(critic)
        if blocking or unavailable:
            logger.warning(
                "genesis audit blocked target=%s issues=%s unavailable=%s",
                target.target_id,
                [issue.code for issue in blocking],
                unavailable,
            )
        return Review(
            ok=not blocking and not unavailable,
            blocking=tuple(blocking),
            unavailable=tuple(unavailable),
        )

    async def _run(
        self, critic: CriticType, target: ReviewTarget
    ) -> tuple[CriticVerdict, Outcome]:
        if critic == "chronology":
            verdict = check_chronology(target)
            return verdict, ("pass" if verdict.passed else "fail")
        if critic == "identity":
            verdict = check_identity(target)
            return verdict, ("pass" if verdict.passed else "fail")
        if self._structured is None or self._prompts is None:
            # No model. Not a pass: a critic that could not run has not
            # approved anything, and saying otherwise is the silent pass
            # GEN-CRITIC-001 names. It is recorded as an unrun critic with a
            # medium issue, which does not block but is visible in the audit.
            return (
                CriticVerdict(
                    passed=False,
                    issues=(
                        CriticIssue(
                            severity="medium",
                            target_id=target.target_id,
                            code="CRITIC_UNAVAILABLE",
                            reason=f"{critic} could not run: no model",
                            repair_scope="none",
                        ),
                    ),
                ),
                "unavailable",
            )
        try:
            template = self._prompts.get(f"{PROMPT_PREFIX}_{critic}")
            content = template.render(
                target=target.text,
                anchors=target.anchors or "-",
                continuity=target.continuity or "-",
                previous=target.previous or "-",
                age_start=target.age_start,
                age_end=target.age_end,
            )
            outcome = await self._structured.generate(
                CriticVerdict,
                (LLMMessage(role="user", content=content),),
                purpose=f"{PROMPT_PREFIX}_{critic}",
                temperature=0.2,
                max_tokens=600,
                prompt_id=f"{PROMPT_PREFIX}_{critic}",
                prompt_version=template.prompt_version,
            )
        except Exception:  # noqa: BLE001
            logger.exception("critic %s failed to run", critic)
            return (
                CriticVerdict(
                    passed=False,
                    issues=(
                        CriticIssue(
                            severity="medium",
                            target_id=target.target_id,
                            code="CRITIC_ERROR",
                            reason=f"{critic} raised",
                            repair_scope="none",
                        ),
                    ),
                ),
                "unavailable",
            )
        if not getattr(outcome, "ok", True) or outcome.value is None:
            # Unparseable output is not approval either. The critic produced
            # nothing usable, which is exactly what "unavailable" means.
            return (
                CriticVerdict(
                    passed=False,
                    issues=(
                        CriticIssue(
                            severity="medium",
                            target_id=target.target_id,
                            code="CRITIC_UNREADABLE",
                            reason=f"{critic} returned nothing usable",
                            repair_scope="none",
                        ),
                    ),
                ),
                "unavailable",
            )
        verdict = outcome.value
        return verdict, ("pass" if verdict.passed else "fail")

    def _record(
        self, critic: CriticType, target: ReviewTarget, verdict: CriticVerdict
    ) -> None:
        try:
            self._audits.record(
                audit_id=ids.new_id(AUDIT),
                genesis_run_id=self._run_id,
                target_type=target.target_type,
                target_id=target.target_id,
                critic_type=critic,
                passed=verdict.passed,
                severity=verdict.worst,
                issues_json=json.dumps(
                    [issue.model_dump() for issue in verdict.issues],
                    ensure_ascii=False,
                ),
                now=self._clock.now(),
            )
        except Exception:  # noqa: BLE001 - never lose the run over telemetry
            logger.exception("could not record a critic verdict")


#: Phrases that contradict the embodiment anchor (spec 1.3).
_PHYSICAL_MARKERS: tuple[str, ...] = (
    "朝ごはんを食べ",
    "ごはんを食べた",
    "電車に乗った",
    "手をつないだ",
    "熱を出して寝込",
)


def _stated_ages(text: str) -> tuple[int, ...]:
    """Ages the text claims, so they can be checked against the real ones."""
    import re

    return tuple(
        int(match)
        for match in re.findall(r"(\d{1,2})\s*(?:歳|才)", text)
        if match.isdigit()
    )


__all__ = [
    "CRITICS",
    "DETERMINISTIC",
    "REQUIRED",
    "CriticBoard",
    "Outcome",
    "Review",
    "ReviewTarget",
    "check_chronology",
    "check_identity",
]
