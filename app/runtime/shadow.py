"""Shadow modes for the autonomous capabilities (rebuild spec 47 — Phase 14).

    危険な自律機能は first live から直接送信しない。

Four things she does without being asked, and all four are hard to take back:

    proactive_contact     a message the USER did not prompt
    intentional_silence   deciding not to answer one they did
    npc_contact           living a social life while nobody is watching
    web_search            a request that leaves this machine

Each runs in one of three modes::

    OFF      the capability does not run at all
    SHADOW   it deliberates fully, records what it would have done, and stops
    LIVE     it acts

The point of SHADOW is that it is not a dry run bolted on beside the real path.
The deliberation is the same deliberation — same gates, same model calls, same
decision — and the mode only decides whether the last step happens. A shadow
implementation that took a different route would be evaluating a system the
OWNER is not about to switch on.

This module owns the mode and the record. It does not own any of the four
decisions: each capability decides for itself whether it *wants* to act, and
asks here only whether it may. That split is what keeps "she chose not to" and
"she was not allowed to" from collapsing into the same row.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping

from app.clock import Clock, SystemClock

logger = logging.getLogger(__name__)

MODULE = "shadow_controller"

ShadowMode = Literal["OFF", "SHADOW", "LIVE"]

#: The four of spec 47, and nothing else. A capability that is not on this list
#: must not be asking for permission here — either it is not dangerous in the
#: sense the section means, or the list is wrong and the spec has to change.
SHADOW_CAPABILITIES: tuple[str, ...] = (
    "proactive_contact",
    "intentional_silence",
    "npc_contact",
    "web_search",
)

MODES: tuple[ShadowMode, ...] = ("OFF", "SHADOW", "LIVE")


class UnknownCapability(KeyError):
    """Asked about something spec 47 does not cover."""


@dataclass(frozen=True, slots=True)
class ShadowVerdict:
    """May this happen, and what was recorded about the asking."""

    capability: str
    mode: ShadowMode
    #: What the capability decided on its own, before the mode was consulted.
    would_act: bool
    #: Whether the real effect may now happen. Only ever true in LIVE.
    acts: bool
    reason: str = ""
    subject: str = ""
    shadow_id: str = ""

    @property
    def suppressed(self) -> bool:
        """She wanted to, and the mode is why she did not."""
        return self.would_act and not self.acts

    def describe(self) -> str:
        if self.acts:
            return f"{self.capability}: LIVE"
        if self.suppressed:
            return f"{self.capability}: would have — {self.mode}"
        return f"{self.capability}: no"


class ShadowController:
    """The authority on what may actually happen (spec 47).

    One object, asked by all four capabilities, so that "what mode is proactive
    contact in" has exactly one answer. The modes are read once at construction
    from configuration: a mode that could change under a running deliberation
    would let a capability pass its gate in SHADOW and send in LIVE.
    """

    name = MODULE

    def __init__(
        self,
        *,
        modes: Mapping[str, str],
        repository: Any = None,
        clock: Clock | None = None,
    ) -> None:
        unknown = set(modes) - set(SHADOW_CAPABILITIES)
        if unknown:
            raise UnknownCapability(f"not a spec 47 capability: {sorted(unknown)}")
        resolved: dict[str, ShadowMode] = {}
        for capability in SHADOW_CAPABILITIES:
            mode = str(modes.get(capability, "SHADOW")).upper()
            if mode not in MODES:
                raise ValueError(f"{capability}: {mode!r} is not one of {MODES}")
            resolved[capability] = mode  # type: ignore[assignment]
        self._modes = resolved
        self._repository = repository
        self._clock = clock or SystemClock()

    def mode(self, capability: str) -> ShadowMode:
        try:
            return self._modes[capability]
        except KeyError as exc:  # noqa: PERF203 - the message is the point
            raise UnknownCapability(
                f"{capability!r} is not one of {SHADOW_CAPABILITIES}"
            ) from exc

    def runs(self, capability: str) -> bool:
        """Whether the capability deliberates at all.

        Spec 28.4: OFF sends nothing *and deliberates nothing*. A capability
        that is OFF should not be spending model calls on a decision whose
        answer is already known.
        """
        return self.mode(capability) != "OFF"

    def live(self, capability: str) -> bool:
        return self.mode(capability) == "LIVE"

    def decide(
        self,
        capability: str,
        *,
        would_act: bool,
        reason: str = "",
        subject: str = "",
        detail: Any = None,
        gates: Any = None,
        run_id: str | None = None,
        event_id: str | None = None,
        now: datetime | None = None,
    ) -> ShadowVerdict:
        """Ask, and be recorded asking.

        Called at the moment the capability is about to commit its external
        effect — not before its gates, not after the effect. That position is
        what makes the shadow record honest: everything upstream really
        happened, and the only difference between this row in SHADOW and the
        same row in LIVE is the last step.
        """
        mode = self.mode(capability)
        acts = would_act and mode == "LIVE"
        moment = now or self._clock.now()
        shadow_id = ""
        if self._repository is not None:
            shadow_id = self._repository.record(
                capability=capability,
                mode=mode,
                would_act=would_act,
                acted=acts,
                subject=subject[:200],
                reason=reason[:400],
                detail_json=_encode(detail),
                gates_json=_encode(gates),
                run_id=run_id,
                event_id=event_id,
                now=moment,
            )
        if would_act and not acts:
            logger.info(
                "shadow suppressed capability=%s mode=%s subject=%s",
                capability, mode, subject[:80],
            )
        return ShadowVerdict(
            capability=capability,
            mode=mode,
            would_act=would_act,
            acts=acts,
            reason=reason,
            subject=subject,
            shadow_id=shadow_id,
        )

    def as_dict(self) -> dict[str, str]:
        return dict(self._modes)


def _encode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


__all__ = [
    "MODES",
    "MODULE",
    "SHADOW_CAPABILITIES",
    "ShadowController",
    "ShadowMode",
    "ShadowVerdict",
    "UnknownCapability",
]
