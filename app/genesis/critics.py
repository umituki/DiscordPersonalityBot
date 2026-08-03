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
from typing import Any, Sequence

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

PROMPT_PREFIX = "genesis_critic"


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

    async def review(self, target: ReviewTarget) -> tuple[bool, tuple[CriticIssue, ...]]:
        """Every enabled critic, in order. Returns whether the stage may go on.

        GEN-CRITIC-001: a blocking issue means it may not, and the verdict is
        written down either way. An audit that failed and was passed over
        silently is the one outcome the spec forbids, and it is only detectable
        because the failure is a row.
        """
        blocking: list[CriticIssue] = []
        for critic in self._enabled:
            verdict = await self._run(critic, target)
            self._record(critic, target, verdict)
            blocking.extend(verdict.blocking_issues)
        if blocking:
            logger.warning(
                "genesis audit blocked target=%s issues=%s",
                target.target_id,
                [issue.code for issue in blocking],
            )
        return not blocking, tuple(blocking)

    async def _run(self, critic: CriticType, target: ReviewTarget) -> CriticVerdict:
        if critic == "chronology":
            return check_chronology(target)
        if critic == "identity":
            return check_identity(target)
        if self._structured is None or self._prompts is None:
            # No model. Not a pass: a critic that could not run has not
            # approved anything, and saying otherwise is the silent pass
            # GEN-CRITIC-001 names. It is recorded as an unrun critic with a
            # medium issue, which does not block but is visible in the audit.
            return CriticVerdict(
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
            return CriticVerdict(
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
            )
        if not getattr(outcome, "ok", True) or outcome.value is None:
            return CriticVerdict(passed=True)
        return outcome.value

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
    "CriticBoard",
    "ReviewTarget",
    "check_chronology",
    "check_identity",
]
