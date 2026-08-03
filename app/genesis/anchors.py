"""The things about her life that are decided once (rebuild spec 34.1, 34.2).

    年齢は Python が exact date から計算。LLM に年齢計算させない。

That instruction is the reason this module exists as arithmetic rather than as
a prompt. A model asked how old someone born in March 2007 was in September
2019 will usually say twelve, and will sometimes say thirteen, and nothing
downstream can tell which happened. Every age in the whole nineteen-year run
comes from :func:`age_at` — one function, one definition of a birthday, no
opinions.

34.2 draws the other line. The OWNER's setup answers make a *rough bias* and
nothing more::

    OWNER 初期質問は rough bias のみ作る。
    最終 personality / values / hobbies を直接決めない。

So the temperament seed here is deliberately thin — a handful of leanings — and
the personality she ends up with is whatever nineteen years of replay produce
from it. Writing the final traits into the anchors would make Genesis a very
long way of restating the setup form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clock import ensure_aware


def age_at(birth: datetime, moment: datetime) -> int:
    """Completed years, by the calendar. The only age calculation there is.

    A birthday that has not arrived yet this year does not count, which is the
    part a language model gets wrong at exactly the rate that makes it hard to
    notice.
    """
    birth = ensure_aware(birth)
    moment = ensure_aware(moment)
    years = moment.year - birth.year
    if (moment.month, moment.day) < (birth.month, birth.day):
        years -= 1
    return max(0, years)


def birthday_in(birth: datetime, year: int) -> datetime:
    """That year's birthday, with 29 February handled rather than crashed."""
    birth = ensure_aware(birth)
    try:
        return birth.replace(year=year)
    except ValueError:  # 29 February in a non-leap year
        return birth.replace(year=year, month=3, day=1)


@dataclass(frozen=True, slots=True)
class LifeYearSpan:
    """One year of her life, as dates rather than as a label."""

    year_number: int
    start: datetime
    end: datetime
    age_start: int
    age_end: int

    @property
    def months(self) -> int:
        return 12


def year_spans(birth: datetime, present: datetime) -> tuple[LifeYearSpan, ...]:
    """Every year from birth to now, in order.

    Anchored on birthdays rather than calendar years: "her fourth year" is
    birthday to birthday, because that is the unit ages are counted in and
    mixing the two is how a scaffold ends up describing a five-year-old's
    school year for a four-year-old.
    """
    birth = ensure_aware(birth)
    present = ensure_aware(present)
    if present <= birth:
        return ()
    spans: list[LifeYearSpan] = []
    total = age_at(birth, present)
    for index in range(total):
        start = birthday_in(birth, birth.year + index)
        end = birthday_in(birth, birth.year + index + 1)
        spans.append(
            LifeYearSpan(
                year_number=index + 1,
                start=start,
                end=end,
                age_start=index,
                age_end=index + 1,
            )
        )
    return tuple(spans)


def month_spans(span: LifeYearSpan, birth: datetime) -> Iterator[tuple[int, datetime, datetime, int]]:
    """The twelve months of one life year, with the age in each.

    Yields ``(month_number, start, end, age_start)``. The age is recomputed per
    month rather than assumed constant, because a birthday falls inside the
    first month of every life year and the month either side of it is not the
    same age.
    """
    for index in range(12):
        start = _add_months(span.start, index)
        end = _add_months(span.start, index + 1)
        if end > span.end:
            end = span.end
        yield index + 1, start, end, age_at(birth, start)


class TemperamentSeed(BaseModel):
    """A rough bias, and deliberately no more (34.2).

    Five leanings on a small scale. Not traits, not values, not hobbies —
    those are what nineteen years of replay are *for*, and pre-writing them
    here would make Genesis an expensive way of restating the setup form.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Towards people or away from them, before anything happened to her.
    sociability: float = Field(default=0.0, ge=-1.0, le=1.0)
    #: How strongly things land.
    reactivity: float = Field(default=0.0, ge=-1.0, le=1.0)
    #: Drawn to the new, or to the familiar.
    openness: float = Field(default=0.0, ge=-1.0, le=1.0)
    #: How steadily she stays with something.
    persistence: float = Field(default=0.0, ge=-1.0, le=1.0)
    #: Baseline mood tilt.
    positivity: float = Field(default=0.0, ge=-1.0, le=1.0)

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class LifeAnchors(BaseModel):
    """The immutable frame of a life (34.1).

    Everything here is settled before generation starts and is never
    renegotiated by anything downstream. A scaffold that contradicts an anchor
    is wrong; the anchor is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    birth_datetime: datetime
    present_datetime: datetime
    gender_identity: str = ""
    embodiment: str = ""
    language: str = "ja"
    culture: str = ""
    home: str = ""
    family: str = ""
    social: str = ""
    education: str = ""
    immutable_rules: str = ""
    temperament: TemperamentSeed = TemperamentSeed()

    @field_validator("birth_datetime", "present_datetime", mode="after")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return ensure_aware(value)

    @property
    def developmental_age(self) -> int:
        """Her age now. Computed, never stated (34.1)."""
        return age_at(self.birth_datetime, self.present_datetime)

    @property
    def years(self) -> tuple[LifeYearSpan, ...]:
        return year_spans(self.birth_datetime, self.present_datetime)

    def age_on(self, moment: datetime) -> int:
        return age_at(self.birth_datetime, moment)

    def describe(self) -> str:
        """The block every generation prompt gets. Facts, not narrative."""
        lines = [
            f"生年月日: {self.birth_datetime.date().isoformat()}",
            f"現在: {self.present_datetime.date().isoformat()}（{self.developmental_age}歳）",
        ]
        for label, value in (
            ("言語/文化", f"{self.language} / {self.culture}"),
            ("身体", self.embodiment),
            ("家", self.home),
            ("家族", self.family),
            ("社会環境", self.social),
            ("学びの環境", self.education),
        ):
            if value and value.strip(" /"):
                lines.append(f"{label}: {value}")
        if self.immutable_rules:
            lines.append(f"変わらないこと: {self.immutable_rules}")
        return "\n".join(lines)


def _add_months(moment: datetime, count: int) -> datetime:
    total = moment.month - 1 + count
    year = moment.year + total // 12
    month = total % 12 + 1
    day = moment.day
    while True:
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
            if day < 28:  # pragma: no cover - unreachable for real dates
                raise


__all__ = [
    "LifeAnchors",
    "LifeYearSpan",
    "TemperamentSeed",
    "age_at",
    "birthday_in",
    "month_spans",
    "year_spans",
]
