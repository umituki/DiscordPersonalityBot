"""Reaching out first (rebuild spec 28 — Phase 9).

The one thing she does that the USER cannot undo. A reply is invited; a message
that arrives unasked is not, and the cost of getting it wrong is asymmetric —
a missed impulse is invisible, an unwanted message is not.

So the chain in 28.5 is long on purpose, and every stage can stop it::

    Opportunity → hard gate → LLM judgment → DecisionEngine
    → draft → grounding/guard → send → success only: event + record

**28.1 is the rule that matters most**, and it is a structural one rather than a
tuning one:

    USER が返さないほど送信頻度が増える構造は禁止。

A system that reaches out more when nobody answers is not lonely, it is broken:
silence becomes an input that produces more messages, which produces more
silence. The backoff multiplies with the unanswered count and there is a hard
ceiling on top of it, so the feedback runs the only direction it may.

**28.4** gives three modes, and ``初期運用は SHADOW`` — deliberate fully, record
what she would have said, send nothing. Shadow is worthless without the record,
which is why every deliberation writes a row in every mode, and why
``would_send`` and ``sent`` are separate columns: "she wanted to and the mode
forbade it" and "she decided not to" are different facts about her.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app import ids
from app.agency.models import ActionCandidate
from app.clock import Clock, SystemClock
from app.conversation.events import YUI_MESSAGE_SENT, YuiMessageSentPayload
from app.events.model import Event
from app.llm.types import LLMMessage
from app.llm.validation import ValidationContext
from app.world.models import Opportunity

logger = logging.getLogger(__name__)

MODULE = "proactive_runtime"
DELIBERATION = "pdl"

PROACTIVE_CONTACT = "proactive_contact"
REACH_OUT = "reach_out"

JUDGMENT_PROMPT = "proactive_judgment"
MESSAGE_PROMPT = "proactive_message"

ProactiveMode = Literal["OFF", "SHADOW", "LIVE"]

#: Spec 28.3: 原則最低 1 つ. A proactive message needs something to be about.
#: "She felt like it" is not on this list, and that is deliberate — an urge with
#: no referent produces the message nobody wants to receive.
TriggerKind = Literal[
    "activity_completion",
    "spontaneous_memory",
    "npc_event",
    "goal_event",
    "knowledge_discovery",
    "emotion",
    "connection_desire",
    "unfinished_conversation",
]

#: Event types that count as each trigger, so 28.3 is a lookup rather than a
#: pile of conditionals that drift apart.
TRIGGER_EVENTS: dict[str, TriggerKind] = {
    "ACTIVITY_FINISHED": "activity_completion",
    "NPC_INTERACTION": "npc_event",
    "GROUP_ACTIVITY": "npc_event",
    "GOAL_PURSUED": "goal_event",
    "KNOWLEDGE_ACQUIRED": "knowledge_discovery",
    "EMOTION_ACTIVATED": "emotion",
}

#: How far back a trigger stays fresh. Something that happened yesterday is not
#: a reason to message someone today.
TRIGGER_WINDOW_HOURS = 6.0


@dataclass(frozen=True, slots=True)
class Trigger:
    """What this would be about (28.3)."""

    kind: TriggerKind
    detail: str = ""
    event_id: str | None = None


class ProactiveJudgment(BaseModel):
    """The model's answer to 「今、本当にUSERへ伝えたいことがあるか？」 (28.2).

    Categorical, like every other judgment in this system (APP-001, §2E). A
    float here would invite exactly the thing 28.1 forbids: a threshold that
    can be nudged until she talks more.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    wants_to_say: Literal["yes", "no", "not_really"]
    #: One short line about what, in her own words. Not the message itself.
    about: str = Field(default="", max_length=200)
    #: Why this is worth interrupting someone for.
    because: Literal[
        "something_happened",
        "thought_of_them",
        "unfinished_conversation",
        "wanted_to_share",
        "nothing_in_particular",
    ] = "nothing_in_particular"

    @property
    def affirmative(self) -> bool:
        return self.wants_to_say == "yes"


class ProactiveOutcome(BaseModel):
    """One full deliberation, whatever came of it."""

    model_config = ConfigDict(frozen=True)

    deliberation_id: str
    considered_at: datetime
    mode: ProactiveMode
    trigger_kind: str = ""
    trigger_detail: str = ""
    gate_passed: bool = False
    gate_reason: str = ""
    desire: float = 0.0
    unanswered: int = 0
    required_wait_hours: float = 0.0
    judged: bool = False
    judgment: str = ""
    judgment_reason: str = ""
    draft: str = ""
    guard_verdict: str = ""
    would_send: bool = False
    sent: bool = False
    event_id: str | None = None
    contact_id: str | None = None

    def describe(self) -> str:
        if not self.gate_passed:
            return f"blocked: {self.gate_reason}"
        if not self.would_send:
            return f"decided against: {self.judgment_reason or self.judgment}"
        return "sent" if self.sent else f"would send (mode={self.mode})"


class OutboundSender(Protocol):
    """Somewhere to send an unprompted message.

    A protocol rather than the gateway itself, so that ``app/runtime/`` never
    imports Discord and SHADOW mode has something honest to be given: the null
    sender is not a stub standing in for a real one, it *is* the correct
    collaborator when nothing should leave the process.
    """

    async def send(self, text: str) -> str | None:
        """Return the delivered message id, or ``None`` if nothing was sent."""


class NullSender:
    """Sends nothing, and says so. The default everywhere but LIVE."""

    name = "null_sender"

    async def send(self, text: str) -> str | None:
        return None


class ProactiveSource:
    """Something worth mentioning (28.3). Reads only.

    Deliberately does *not* consult the hard gate: a source's job is to notice
    that there is something to say, and mixing "is there a reason" with "is it
    allowed" would make the gate's decisions invisible in the tick record.
    """

    name = "proactive"

    def __init__(
        self,
        events: Any,
        state: Any,
        conversations: Any = None,
        *,
        world: Any = None,
        clock: Clock | None = None,
        window_hours: float = TRIGGER_WINDOW_HOURS,
    ) -> None:
        self._events = events
        self._state = state
        self._conversations = conversations
        self._world = world
        self._clock = clock or SystemClock()
        self._window = timedelta(hours=window_hours)

    def collect(self, now: datetime) -> Sequence[Opportunity]:
        if self._world is not None and self._world.current_sleep() is not None:
            return []  # she is asleep, and does not compose messages in it
        trigger = self.find_trigger(now)
        if trigger is None:
            return []
        return [
            Opportunity(
                kind=PROACTIVE_CONTACT,
                detail=f"{trigger.kind}:{trigger.detail}",
                urgency=0.5,
                created_at=now,
            )
        ]

    def find_trigger(self, now: datetime) -> Trigger | None:
        """The most recent thing that would give a message a subject."""
        for event in self._events.recent(limit=30):
            if event.actor_type == "user":
                continue
            kind = TRIGGER_EVENTS.get(event.event_type)
            if kind is None:
                continue
            if now - event.occurred_at > self._window:
                break  # `recent` is newest first, so everything older follows
            return Trigger(
                kind=kind, detail=event.event_type, event_id=event.event_id
            )

        # No external subject. Wanting company is a legitimate trigger in its
        # own right (28.3), but only when it is actually strong — otherwise
        # every quiet evening becomes a reason to write.
        entry = self._state.get("needs", "connection_desire")
        if entry is not None and float(entry.numeric or 0.0) >= 0.7:
            return Trigger(kind="connection_desire", detail="connection_desire")
        return None

    def next_due(self, now: datetime) -> datetime | None:
        return None


class ProactiveDeliberation:
    """The 28.5 chain, in one place, with every stage able to stop it."""

    name = "proactive_deliberation"

    def __init__(
        self,
        *,
        engine: Any,
        source: ProactiveSource,
        deliberations: Any,
        mode: ProactiveMode = "SHADOW",
        structured: Any = None,
        prompts: Any = None,
        guard: Any = None,
        sender: OutboundSender | None = None,
        processor: Any = None,
        channel_id: str = "",
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._source = source
        self._deliberations = deliberations
        self._mode: ProactiveMode = mode
        self._structured = structured
        self._prompts = prompts
        self._guard = guard
        self._sender = sender or NullSender()
        self._processor = processor
        self._channel_id = channel_id
        self._clock = clock or SystemClock()

    @property
    def mode(self) -> ProactiveMode:
        return self._mode

    async def deliberate(
        self, opportunity: Opportunity, view: Any, *, now: datetime | None = None
    ) -> ProactiveOutcome:
        moment = now or self._clock.now()
        kind, _, detail = opportunity.detail.partition(":")

        outcome = ProactiveOutcome(
            deliberation_id=ids.new_id(DELIBERATION),
            considered_at=moment,
            mode=self._mode,
            trigger_kind=kind,
            trigger_detail=detail,
        )

        if self._mode == "OFF":
            outcome = outcome.model_copy(
                update={"gate_reason": "proactive contact is switched off"}
            )
            self._record(outcome)
            return outcome

        # --- 28.1, first and unconditionally ---------------------------------
        assessment = self._engine.assess(opportunity, view, now=moment)
        outcome = outcome.model_copy(
            update={
                "gate_passed": assessment.allowed,
                "gate_reason": assessment.reason,
                "desire": assessment.desire,
                "unanswered": assessment.unanswered,
                "required_wait_hours": assessment.required_wait_hours,
            }
        )
        if not assessment.allowed:
            self._record(outcome)
            return outcome

        # --- 28.2, only after the gate ---------------------------------------
        judgment = await self._judge(kind, detail, view)
        outcome = outcome.model_copy(
            update={
                "judged": judgment is not None,
                "judgment": "" if judgment is None else judgment.wants_to_say,
                "judgment_reason": "" if judgment is None else judgment.because,
            }
        )
        if judgment is None or not judgment.affirmative:
            self._record(outcome)
            return outcome

        draft = await self._draft(judgment, kind)
        verdict = self._check(draft)
        outcome = outcome.model_copy(
            update={
                "draft": draft,
                "guard_verdict": verdict,
                # She got all the way here: she wanted to, she was allowed to,
                # and what she wrote was clean. Whether it *leaves* is the
                # mode's business, not hers.
                "would_send": bool(draft) and verdict == "clean",
            }
        )
        if not outcome.would_send:
            self._record(outcome)
            return outcome

        if self._mode != "LIVE":
            # SHADOW: recorded in full, and nothing left the process.
            self._record(outcome)
            return outcome

        outcome = await self._send(outcome)
        self._record(outcome)
        return outcome

    # --- stages --------------------------------------------------------------
    async def _judge(self, kind: str, detail: str, view: Any) -> ProactiveJudgment | None:
        if self._structured is None or self._prompts is None:
            # No model available. Silence is the conservative answer: failing
            # towards "send something" is how a degraded system becomes a
            # talkative one.
            return None
        try:
            template = self._prompts.get(JUDGMENT_PROMPT)
            content = template.render(
                trigger=kind,
                detail=detail or "-",
                needs=_summarise(view, "needs") or "-",
                relationship=_summarise(view, "relationship") or "-",
            )
            outcome = await self._structured.generate(
                ProactiveJudgment,
                (LLMMessage(role="user", content=content),),
                purpose=JUDGMENT_PROMPT,
                temperature=0.3,
                max_tokens=200,
                prompt_id=JUDGMENT_PROMPT,
                prompt_version=template.prompt_version,
            )
        except Exception:  # noqa: BLE001 - a model failure is not a reason to talk
            logger.exception("proactive judgment failed")
            return None
        return outcome.value if getattr(outcome, "ok", True) else None

    async def _draft(self, judgment: ProactiveJudgment, kind: str) -> str:
        if self._structured is None or self._prompts is None:
            return ""
        try:
            template = self._prompts.get(MESSAGE_PROMPT)
            content = template.render(trigger=kind, about=judgment.about or "-")
            outcome = await self._structured.generate(
                _ProactiveDraft,
                (LLMMessage(role="user", content=content),),
                purpose=MESSAGE_PROMPT,
                temperature=0.7,
                max_tokens=200,
                prompt_id=MESSAGE_PROMPT,
                prompt_version=template.prompt_version,
            )
        except Exception:  # noqa: BLE001
            logger.exception("proactive draft failed")
            return ""
        if not getattr(outcome, "ok", True) or outcome.value is None:
            return ""
        return (outcome.value.text or "").strip()

    def _check(self, draft: str) -> str:
        """The same output guard the conversation uses, not a second one.

        §16's rules about physical claims, reasoning leaks and secret
        disclosure do not become optional because nobody asked her a question.
        A second, gentler guard for unprompted messages would be a hole with a
        justification attached.
        """
        if not draft:
            return "empty"
        if self._guard is None:
            return "clean"
        failure = self._guard.check(
            _ProactiveDraft(text=draft), ValidationContext(purpose=MESSAGE_PROMPT)
        )
        return "clean" if failure is None else failure.reason_code

    async def _send(self, outcome: ProactiveOutcome) -> ProactiveOutcome:
        """28.5: success only: YUI_MESSAGE_SENT + proactive record.

        The order matters. Nothing is recorded as contact until Discord has
        confirmed it, because a contact row for a message that never arrived
        would make the backoff count a silence that was never hers.
        """
        try:
            message_id = await self._sender.send(outcome.draft)
        except Exception:  # noqa: BLE001
            logger.exception("proactive send failed")
            return outcome.model_copy(update={"sent": False})
        if not message_id:
            return outcome.model_copy(update={"sent": False})

        event = Event.create(
            event_type=YUI_MESSAGE_SENT,
            category="social",
            actor_type="yui",
            source_type=MODULE,
            origin="real_discord",
            priority="P2",
            payload=YuiMessageSentPayload(
                message_id=str(message_id),
                channel_id=self._channel_id,
                text=outcome.draft,
                # Nothing prompted it — that is the whole point of this phase.
                in_reply_to_event_id=None,
            ),
            clock=self._clock,
        )
        if self._processor is not None:
            await self._processor.process(event)
        contact_id = self._engine.record_sent(
            Opportunity(
                kind=outcome.trigger_kind or PROACTIVE_CONTACT,
                created_at=outcome.considered_at,
            ),
            event_id=event.event_id,
        )
        return outcome.model_copy(
            update={"sent": True, "event_id": event.event_id, "contact_id": contact_id}
        )

    def _record(self, outcome: ProactiveOutcome) -> None:
        try:
            self._deliberations.record(outcome)
        except Exception:  # noqa: BLE001 - telemetry never costs a decision
            logger.exception("could not record a proactive deliberation")


class ProactiveCandidates:
    """What reaching out is worth. The gate has not run yet at this point."""

    name = "proactive_candidates"

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def contact(self, opportunity: Opportunity, now: datetime) -> ActionCandidate | None:
        # Deliberately modest. A proactive message competes with reading a book
        # and usually loses, which is the correct shape: the bar for
        # interrupting someone is higher than the bar for entertaining herself.
        return ActionCandidate(
            action=REACH_OUT,
            route="goal_directed",
            expected_value=round(min(0.6, 0.25 + opportunity.urgency / 3), 6),
            reason=opportunity.detail,
        )


class ProactiveActions:
    """The handler. Everything real happens inside the deliberation."""

    name = "proactive_actions"

    def __init__(
        self,
        deliberation: ProactiveDeliberation,
        *,
        state: Any = None,
        candidates: ProactiveCandidates | None = None,
        engine: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._deliberation = deliberation
        #: The state repository, read directly rather than through a captured
        #: snapshot: taking a snapshot writes a row, and deciding whether to
        #: say something is not an event in her timeline.
        self._state = state
        self._candidates = candidates or ProactiveCandidates(engine)
        self._clock = clock or SystemClock()

    async def reach_out(self, candidate: ActionCandidate, now: datetime) -> bool:
        opportunity = Opportunity(
            kind=PROACTIVE_CONTACT, detail=candidate.reason, created_at=now
        )
        view = self._view()
        outcome = await self._deliberation.deliberate(opportunity, view, now=now)
        logger.info("proactive deliberation: %s", outcome.describe())
        # Whether the *action* happened is whether anything left the process.
        # A shadow deliberation is real work and a real row, and it is not a
        # contact — reporting it as one would make every shadow run look like
        # she had messaged someone.
        return outcome.sent

    def register(self, registry: Any, *, sources: Sequence[Any] = ()) -> None:
        for source in sources:
            registry.add_source(source)
        registry.add_builder(PROACTIVE_CONTACT, self._candidates.contact)
        registry.add_handler(REACH_OUT, self.reach_out)

    def _view(self) -> Any:
        return _EmptyView() if self._state is None else _StateView(self._state)


class _ProactiveDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str = Field(default="", max_length=400)


class _EmptyView:
    """Everything at its default. Used only when state is unreadable."""

    def number(self, domain: str, key: str, default: float = 0.0) -> float:
        return default


class _StateView:
    """A read-only view over the state repository, in the shape the gate wants.

    Not a captured snapshot: capturing one writes a row, and wondering whether
    to say something is not a moment in her timeline.
    """

    def __init__(self, state: Any) -> None:
        self._state = state

    def number(self, domain: str, key: str, default: float = 0.0) -> float:
        try:
            entry = self._state.get(domain, key)
        except Exception:  # noqa: BLE001
            logger.exception("could not read %s.%s", domain, key)
            return default
        if entry is None or entry.numeric is None:
            return default
        return float(entry.numeric)


def _summarise(view: Any, domain: str) -> str:
    """A short, readable line for the prompt. Never the whole snapshot."""
    getter = getattr(view, "numbers_in", None)
    if getter is None:
        return ""
    try:
        values = getter(domain) or {}
    except Exception:  # noqa: BLE001
        return ""
    return ", ".join(f"{key}={value:.2f}" for key, value in sorted(values.items()))


__all__ = [
    "PROACTIVE_CONTACT",
    "REACH_OUT",
    "TRIGGER_EVENTS",
    "NullSender",
    "OutboundSender",
    "ProactiveActions",
    "ProactiveCandidates",
    "ProactiveDeliberation",
    "ProactiveJudgment",
    "ProactiveMode",
    "ProactiveOutcome",
    "ProactiveSource",
    "Trigger",
    "TriggerKind",
]
