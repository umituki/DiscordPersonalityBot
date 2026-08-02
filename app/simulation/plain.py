"""Describing an ordinary day without a model (patch spec 18.3).

Routine and minor experiences never reach the model — that is what makes
simulating decades affordable (spec 22.4). The cost of that was a life whose
every block read:

    そのころの暮らしでのよかったこと（routine）
    そのころの暮らしでのつらかったこと（routine）

Two sentences, sixty blocks. Patch spec 18.3 asks for low-cost variation drawn
from ``activity / context / sociality / topic / life-stage`` instead.

So the summary is composed rather than templated: where she was, what she was
doing, whether anyone else was there, and what it was about — each drawn from
the scaffold and the seed she actually has. Nothing here invents a *fact* the
simulation did not already have; it renders what the block already carries into
a sentence a person could have written.

Deterministic: the RNG is seeded from the moment and the scaffold, so a replay
of the same simulation produces the same life (spec 29).
"""

from __future__ import annotations

from datetime import datetime
from random import Random
from typing import Sequence

MODULE = "plain_summary"

#: Where an ordinary stretch of time is spent. Kept generic on purpose — the
#: specific place comes from the scaffold's environment.
_PLACES: tuple[str, ...] = (
    "家のなか",
    "近所",
    "行きなれた道",
    "窓ぎわ",
    "学校の帰り道",
    "少し遠くまで歩いた先",
)

_ACTIVITIES_POSITIVE: tuple[str, ...] = (
    "ぼんやりしていた",
    "手を動かしていた",
    "同じ道を歩いた",
    "本をめくっていた",
    "音を聞いていた",
    "何かを見つけた",
    "気がついたら時間が過ぎていた",
)

_ACTIVITIES_NEGATIVE: tuple[str, ...] = (
    "うまくいかなかった",
    "気が重かった",
    "やめておけばよかったと思った",
    "うまく言えなかった",
    "うわの空だった",
    "途中でやめた",
)

_SOCIALITY_ALONE: tuple[str, ...] = ("ひとりで", "だれもいないところで", "静かなまま")
_SOCIALITY_TOGETHER: tuple[str, ...] = ("だれかといて", "そばに人がいて", "話しながら")

_SEASONS: tuple[str, ...] = ("冬", "冬", "春", "春", "春", "夏", "夏", "夏", "秋", "秋", "秋", "冬")

#: How much of a mark it left, in the words a plain summary may use.
_WEIGHT: dict[str, str] = {
    "routine": "",
    "minor": "少し心に残った",
    "meaningful": "しばらく考えていた",
    "major": "長く残った",
    "turning_point": "あとから思えば区切りだった",
}


def _rng_for(moment: datetime, salt: str) -> Random:
    """Deterministic per moment, so replaying a simulation replays the life."""
    return Random(f"{moment.isoformat()}|{salt}".encode("utf-8").hex())


def compose_plain_summary(
    *,
    experience_class: str,
    valence: float,
    occurred_at: datetime,
    life_stage: str = "",
    environment: str = "",
    interests: Sequence[str] = (),
    social: bool | None = None,
) -> str:
    """One ordinary day, in a sentence, without a model call.

    ``social`` is left to the RNG when unknown: whether anyone else was there
    is part of what the day was like, and a life where every ordinary day is
    solitary is as flat as one where every day is the same sentence.
    """
    rng = _rng_for(occurred_at, f"{experience_class}|{valence:.3f}|{environment}")

    season = _SEASONS[occurred_at.month - 1]
    place = environment.strip() or rng.choice(_PLACES)
    stage = life_stage.strip()
    alone = rng.random() < 0.6 if social is None else not social
    company = rng.choice(_SOCIALITY_ALONE if alone else _SOCIALITY_TOGETHER)
    activity = rng.choice(
        _ACTIVITIES_POSITIVE if valence >= 0 else _ACTIVITIES_NEGATIVE
    )
    weight = _WEIGHT.get(experience_class, "")

    parts: list[str] = []
    opening = f"{season}の{place}"
    if stage and rng.random() < 0.5:
        opening = f"{stage}のころ、{opening}"
    parts.append(opening)
    parts.append(f"{company}{activity}")

    topic = _topic(rng, interests)
    if topic:
        parts.append(f"{topic}のことが頭にあった")
    if weight:
        parts.append(weight)

    return "。".join(parts) + "。"


def _topic(rng: Random, interests: Sequence[str]) -> str:
    """An interest, sometimes. Not every ordinary day is about a hobby."""
    usable = [topic.strip() for topic in interests if topic.strip()]
    if not usable or rng.random() < 0.45:
        return ""
    return rng.choice(usable)


__all__ = ["MODULE", "compose_plain_summary"]
