"""Expression Context (patch spec 9).

The reply prompt needs to know how YUI currently is. It must not be told in
numbers: a model handed ``relationship.familiarity=0.12`` writes about a
familiarity score, and a model handed the whole state table writes about
whichever number is largest. Patch spec 9 is explicit — ``DB数値全量dumpは禁止``,
use qualitative bands:

    喜び: 強い
    好意: 強い
    関係: まだ初対面に近い
    親しさ: 低い

So this module does three things and nothing else:

1. reads a small, fixed set of keys — never "everything in the snapshot",
2. converts each to a band, in words,
3. drops anything that is not currently worth saying.

It is a pure read. Selecting context must not mutate state
(``.claude/rules/architecture.md``), and nothing here writes.

The bands are deliberately coarse. Expression is
``Emotion × Personality × Relationship × Conversation Context`` (patch spec 9),
and a difference of 0.03 in one of those is not something a person could
express differently anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.state.snapshot import StateSnapshot

MODULE = "expression_context"

#: Below this, a value is not part of how she comes across right now.
FLOOR = 0.25

#: At most this many emotions reach the prompt. Everything a person feels at
#: once does not reach their sentences either.
MAX_EMOTIONS = 3
MAX_NEEDS = 2
MAX_ADAPTATIONS = 2

NOTHING = "(いまとくに強い状態はない)"

_EMOTION_LABELS: dict[str, str] = {
    "joy": "喜び",
    "sadness": "悲しさ",
    "anger": "怒り",
    "fear": "不安",
    "surprise": "驚き",
    "interest": "興味",
    "affection": "好意",
}

_NEED_LABELS: dict[str, str] = {
    "loneliness": "さみしさ",
    "connection_desire": "誰かと話したい気持ち",
    "solitude_desire": "ひとりでいたい気持ち",
}

_TRAIT_LABELS: dict[str, str] = {
    "openness": "新しいことへの関心",
    "conscientiousness": "きちんとしていたい気持ち",
    "extraversion": "人と関わる元気",
    "agreeableness": "人あたりのやわらかさ",
    "neuroticism": "揺れやすさ",
}

#: 0.0-1.0 intensity, from "not worth mentioning" upward.
_INTENSITY_BANDS: tuple[tuple[float, str], ...] = (
    (0.75, "とても強い"),
    (0.55, "強い"),
    (0.4, "ややある"),
    (FLOOR, "少しある"),
)

#: Relationship dimensions read differently from raw intensity: the interesting
#: thing about low familiarity is that it means "we have only just met", not
#: "familiarity is weak".
_FAMILIARITY_BANDS: tuple[tuple[float, str], ...] = (
    (0.7, "長いつきあい"),
    (0.45, "何度も話した相手"),
    (0.2, "少しずつ知りはじめた相手"),
    (0.0, "まだ初対面に近い"),
)

_CLOSENESS_BANDS: tuple[tuple[float, str], ...] = (
    (0.7, "高い"),
    (0.45, "ふつう"),
    (0.2, "まだ低い"),
    (0.0, "低い"),
)


def _band(value: float, bands: Sequence[tuple[float, str]]) -> str | None:
    for threshold, label in bands:
        if value >= threshold:
            return label
    return None


@dataclass(frozen=True, slots=True)
class ExpressionContext:
    """How YUI currently is, in the words a prompt can use."""

    lines: tuple[str, ...] = ()

    def render(self) -> str:
        return "\n".join(self.lines) if self.lines else NOTHING

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @classmethod
    def from_snapshot(cls, snapshot: StateSnapshot | None) -> ExpressionContext:
        """How she is, in words. The turn's intention is rendered separately by
        the social interpretation, so it does not belong here too."""
        if snapshot is None:
            return cls()

        lines: list[str] = []
        lines.extend(_emotion_lines(snapshot))
        lines.extend(_mood_lines(snapshot))
        lines.extend(_need_lines(snapshot))
        lines.extend(_relationship_lines(snapshot))
        lines.extend(_attachment_lines(snapshot))
        lines.extend(_personality_lines(snapshot))
        lines.extend(_adaptation_lines(snapshot))
        lines.extend(_world_lines(snapshot))
        lines.extend(_user_model_lines(snapshot))
        return cls(lines=tuple(lines))


# --- sections ---------------------------------------------------------------
def _ranked(snapshot: StateSnapshot, domain: str, labels: dict[str, str], limit: int):
    """The strongest labelled keys of one domain, above the floor."""
    scored: list[tuple[float, str]] = []
    for key, value in snapshot.domain(domain).items():
        label = labels.get(key)
        number = value.numeric
        if label is None or number is None or number < FLOOR:
            continue
        scored.append((number, label))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[:limit]


def _emotion_lines(snapshot: StateSnapshot) -> Iterable[str]:
    for number, label in _ranked(snapshot, "emotion", _EMOTION_LABELS, MAX_EMOTIONS):
        band = _band(number, _INTENSITY_BANDS)
        if band:
            yield f"{label}: {band}"


def _mood_lines(snapshot: StateSnapshot) -> Iterable[str]:
    valence = snapshot.number_of("mood", "valence")
    if valence is not None:
        if valence >= 0.25:
            yield "気分: よい"
        elif valence <= -0.25:
            yield "気分: 沈んでいる"
    arousal = snapshot.number_of("mood", "arousal")
    if arousal is not None:
        if arousal >= 0.6:
            yield "落ち着き: 高ぶっている"
        elif arousal <= 0.25:
            yield "落ち着き: 静か"


def _need_lines(snapshot: StateSnapshot) -> Iterable[str]:
    for number, label in _ranked(snapshot, "needs", _NEED_LABELS, MAX_NEEDS):
        band = _band(number, _INTENSITY_BANDS)
        if band:
            yield f"{label}: {band}"


def _relationship_lines(snapshot: StateSnapshot) -> Iterable[str]:
    familiarity = snapshot.number_of("relationship", "familiarity")
    if familiarity is not None:
        band = _band(familiarity, _FAMILIARITY_BANDS)
        if band:
            yield f"相手との関係: {band}"
    closeness = snapshot.number_of("relationship", "emotional_closeness")
    if closeness is not None:
        band = _band(closeness, _CLOSENESS_BANDS)
        if band:
            yield f"親しさ: {band}"
    security = snapshot.number_of("relationship", "security")
    if security is not None and security < 0.3:
        yield "安心感: まだ確かめている途中"


def _attachment_lines(snapshot: StateSnapshot) -> Iterable[str]:
    """Only when actually activated — otherwise it is not part of this turn."""
    activation = snapshot.number_of("attachment", "activation")
    if activation is not None and activation >= 0.5:
        yield "そばにいてほしい気持ちが強くなっている"


def _personality_lines(snapshot: StateSnapshot) -> Iterable[str]:
    """Traits only where they are far enough from the middle to show."""
    for key, label in _TRAIT_LABELS.items():
        number = snapshot.number_of("personality", key)
        if number is None:
            continue
        if number >= 0.65:
            yield f"{label}: 強いほう"
        elif number <= 0.35:
            yield f"{label}: 弱いほう"


def _adaptation_lines(snapshot: StateSnapshot) -> Iterable[str]:
    scored: list[tuple[float, str]] = []
    for key, value in snapshot.domain("characteristic_adaptations").items():
        number = value.numeric
        if number is None or number < 0.5:
            continue
        scored.append((number, key))
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _, key in scored[:MAX_ADAPTATIONS]:
        yield f"いまの自分の傾向: {key}"


def _world_lines(snapshot: StateSnapshot) -> Iterable[str]:
    """What she is in the middle of, when the world says she is in something."""
    activity = snapshot.value_of("world", "current_activity")
    if isinstance(activity, str) and activity.strip():
        yield f"いましていること: {activity.strip()}"


def _user_model_lines(snapshot: StateSnapshot) -> Iterable[str]:
    """Only high-confidence estimates (patch spec 9).

    A guess about how the USER feels, stated as fact, is worse than saying
    nothing — so a low-confidence estimate does not reach the prompt at all.
    """
    for key, value in sorted(snapshot.domain("user_model").items()):
        if not isinstance(value.value, str) or not value.value.strip():
            continue
        if (value.confidence or 0.0) < 0.7:
            continue
        yield f"相手について（推測）: {value.value.strip()}"
        return


__all__ = ["ExpressionContext", "MODULE"]
