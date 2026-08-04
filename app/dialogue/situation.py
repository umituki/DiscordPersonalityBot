"""What is going on right now, as one readable thing (Dialogue v2).

The old build had one virtue worth recovering: the model saw the situation as a
single coherent picture rather than as eight unrelated prompt variables. The
cost was that the picture was also treated as true. This keeps the first and
refuses the second.

**Not an authority.** Turn-local, read-only, assembled from rows other
subsystems own, thrown away at the end of the turn. Nothing here is written
back, and nothing here decides whether a claim is supported — that is
`GroundingContext`'s job, and the two are separate types so no call site can
quietly use one for the other.

**Zero is not the same as unknown.** ``completed_activities_today = []`` means
"no completed Activity is on record". It does not mean "she did nothing today",
and it especially does not mean the same thing as "the activity repository
could not be read". A model shown an empty list will happily narrate an empty
day; a model shown 「記録を確認できなかった」 will not. Every section therefore
carries its availability, and the renderer says which it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Sequence

logger = logging.getLogger(__name__)

MODULE = "situation_context"

#: Why a section has nothing in it.
Availability = Literal["present", "empty", "unavailable"]


@dataclass(frozen=True, slots=True)
class Section:
    """One part of the picture, and whether it could be read at all."""

    name: str
    heading: str
    lines: tuple[str, ...] = ()
    availability: Availability = "empty"
    #: What to say when there is nothing. Never a bare "なし" — the difference
    #: between "none recorded" and "could not check" is the whole point.
    empty_text: str = "記録がない"

    @property
    def has_content(self) -> bool:
        return self.availability == "present" and bool(self.lines)

    def render(self) -> str:
        if self.availability == "unavailable":
            return f"## {self.heading}\n- (この情報源を読めなかった。無いという意味ではない)"
        if not self.lines:
            return f"## {self.heading}\n- ({self.empty_text})"
        return "\n".join([f"## {self.heading}", *(f"- {line}" for line in self.lines)])


def _section(
    name: str,
    heading: str,
    reader,
    *,
    empty_text: str = "記録がない",
    limit: int = 8,
) -> Section:
    """Run a reader, and record honestly what happened.

    An exception becomes ``unavailable`` rather than an empty list. That
    distinction cannot be recovered later, so it is captured at the only point
    where it is known.
    """
    try:
        values = [str(item) for item in (reader() or []) if str(item).strip()]
    except Exception:  # noqa: BLE001 - a broken source is information
        logger.exception("situation section %s could not be read", name)
        return Section(name=name, heading=heading, availability="unavailable")
    return Section(
        name=name,
        heading=heading,
        lines=tuple(values[:limit]),
        availability="present" if values else "empty",
        empty_text=empty_text,
    )


@dataclass(frozen=True, slots=True)
class SituationContext:
    """The turn's working picture. Read-only, non-authoritative, turn-local."""

    sections: tuple[Section, ...] = ()
    built_at: datetime | None = None

    def get(self, name: str) -> Section | None:
        for section in self.sections:
            if section.name == name:
                return section
        return None

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Sections that could not be read. Worth logging, worth showing."""
        return tuple(
            section.name
            for section in self.sections
            if section.availability == "unavailable"
        )

    def render(self) -> str:
        return "\n\n".join(section.render() for section in self.sections)


class SituationBuilder:
    """Assembles the picture from whatever is readable.

    Every collaborator is optional and every read is guarded, because this runs
    on the USER-facing path: a situation section failing must degrade the reply,
    never lose it.
    """

    name = MODULE

    def __init__(
        self,
        *,
        world: Any = None,
        state: Any = None,
        society: Any = None,
        goals: Any = None,
        personality: Any = None,
        values: Any = None,
        self_model: Any = None,
        beliefs: Any = None,
        clock: Any = None,
    ) -> None:
        self._world = world
        self._state = state
        self._society = society
        self._goals = goals
        self._personality = personality
        self._values = values
        self._self_model = self_model
        self._beliefs = beliefs
        self._clock = clock

    def build(
        self,
        *,
        understanding: Any = None,
        recalled: Sequence[Any] = (),
        grounding: Any = None,
        now: datetime | None = None,
    ) -> SituationContext:
        moment = now or (self._clock.now() if self._clock is not None else None)
        sections: list[Section] = [
            _section(
                "current_activity",
                "いま していること",
                lambda: self._current_activity(),
                empty_text="いま進行中のActivityは記録されていない",
            ),
            _section(
                "completed_today",
                "今日 終えたこと",
                lambda: self._completed_today(moment),
                # The wording matters. "何もしていない" is a claim about her
                # day; "記録がない" is a claim about the database.
                empty_text="今日の完了Activityは記録されていない（何もしていないという意味ではない）",
            ),
            _section(
                "recalled_memories",
                "いま思い出していること",
                lambda: [self._describe_memory(item) for item in recalled],
                empty_text="このターンで思い出せた具体的な記憶はない",
            ),
            _section(
                "user_facts",
                "USERについて確かなこと（USER所有）",
                lambda: self._evidence_lines(grounding, "verified_user_facts"),
                empty_text="USERについて確認済みの事実は記録されていない",
            ),
            _section(
                "npc_facts",
                "他者について確かなこと（NPC所有）",
                lambda: self._evidence_lines(grounding, "npc_interactions"),
                empty_text="他者とのやりとりは記録されていない",
            ),
            _section(
                "world_facts",
                "いまの世界の状態",
                lambda: self._evidence_lines(grounding, "current_world"),
                empty_text="世界状態を読めていない",
            ),
            _section(
                "feeling",
                "いまの気分",
                lambda: self._feeling(),
                empty_text="感情状態は記録されていない",
            ),
            _section(
                "goals",
                "いま関係しそうな目標",
                lambda: self._relevant_goals(understanding),
                empty_text="進行中の目標はない",
            ),
            _section(
                "self_and_values",
                "自己理解と価値観（このターンに関係する範囲）",
                lambda: self._self_and_values(understanding),
                empty_text="このターンに関係する自己理解・価値観はない",
            ),
        ]
        return SituationContext(sections=tuple(sections), built_at=moment)

    # --- readers -------------------------------------------------------------
    def _current_activity(self) -> list[str]:
        if self._world is None:
            raise RuntimeError("no world service")
        ongoing = self._world.current_activity()
        if ongoing is None:
            return []
        return [f"{ongoing.name}（{ongoing.kind}）"]

    def _completed_today(self, moment: datetime | None) -> list[str]:
        if self._world is None:
            raise RuntimeError("no world service")
        rows = self._world.completed_activities(limit=20)
        if moment is None:
            return [item.name for item in rows]
        return [
            item.name
            for item in rows
            if item.ended_at is not None and item.ended_at.date() == moment.date()
        ]

    @staticmethod
    def _describe_memory(item: Any) -> str:
        summary = getattr(item, "summary", "") or getattr(item, "content", "")
        return str(summary)

    @staticmethod
    def _evidence_lines(grounding: Any, section: str) -> list[str]:
        if grounding is None:
            raise RuntimeError("no grounding context")
        return [item.describe() for item in getattr(grounding, section, ())]

    def _feeling(self) -> list[str]:
        if self._state is None:
            raise RuntimeError("no state repository")
        lines: list[str] = []
        for domain in ("emotion", "mood"):
            for value in self._state.list_domain(domain):
                if value.numeric is not None and float(value.numeric) >= 0.4:
                    lines.append(f"{value.key}: {float(value.numeric):.2f}")
        return lines

    def _relevant_goals(self, understanding: Any) -> list[str]:
        """Only what this turn could plausibly touch.

        Dumping every goal into every reply is how a character starts sounding
        like a status page. The selector is deliberately crude — topic overlap —
        because being crude and small beats being clever and long.
        """
        if self._goals is None:
            raise RuntimeError("no goal repository")
        rows = self._goals.active(limit=10)
        topic = _topic_of(understanding)
        described = [str(getattr(row, "description", "") or row["description"]) for row in rows]
        if not topic:
            return described[:2]
        relevant = [item for item in described if _shares_content(item, topic)]
        return relevant or described[:1]

    def _self_and_values(self, understanding: Any) -> list[str]:
        """Values and self-schema, when the turn is actually about her.

        Gated on `self_disclosure_relevant` rather than always present: these
        are the lines most likely to be padded into every reply, and a reply
        that recites her values unprompted is worse than one that omits them.
        """
        if not getattr(understanding, "self_disclosure_relevant", False):
            return []
        lines: list[str] = []
        if self._values is not None:
            for row in self._values.top(limit=3):
                lines.append(f"価値観: {getattr(row, 'name', row)}")
        if self._self_model is not None:
            for row in self._self_model.strongest(limit=2):
                lines.append(f"自己理解: {getattr(row, 'statement', row)}")
        if not lines:
            raise RuntimeError("no values or self model wired")
        return lines


def _topic_of(understanding: Any) -> str:
    for attribute in ("referenced_subject", "current_topic"):
        value = getattr(understanding, attribute, "")
        if value:
            return str(value)
    return ""


def _shares_content(left: str, right: str) -> bool:
    from app.grounding.models import _content_tokens

    return bool(_content_tokens(left) & _content_tokens(right))


__all__ = [
    "Availability",
    "MODULE",
    "Section",
    "SituationBuilder",
    "SituationContext",
]
