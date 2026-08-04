"""Surface Plan (rebuild spec 14, Phase 3 §8-§11, §19).

How large, and at what distance — never what to say.

The plan is computed in Python from the social reading, the relationship band,
the length of what the USER wrote and the current state. It costs no model call
(§32): a second LLM round to decide "short and casual" would double the USER's
wait for a decision that is a lookup.

**The line Python does not cross.** This module produces ``casual``, ``short``,
``light``. It never produces 「ね」, never prepends 「うん、」, never assembles a
sentence. Grammar rules of that shape always break — they produce text that is
locally correct and globally strange, and every fix adds another condition. The
realizer writes the Japanese; this only says how much room it has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.conversation.social_interpretation import SocialInterpretation
from app.dialogue.response_contract import QuestionPolicy

Length = Literal["very_short", "short", "medium", "long"]
Register = Literal["polite", "neutral", "casual"]
Directness = Literal["soft", "plain", "direct"]
Disclosure = Literal["none", "light", "moderate"]
Fragmentation = Literal["tight", "natural", "loose"]
ExplicitEmotion = Literal["low", "medium", "high"]

#: §15. The relationship reaches the realizer as a band, never as 0.41.
RelationshipBand = Literal["stranger", "acquaintance", "familiar", "close"]
BANDS: tuple[RelationshipBand, ...] = ("stranger", "acquaintance", "familiar", "close")

#: Lower edge of each band, and the width of the overlap that stops a
#: relationship from flickering across a boundary (§16). A band only changes
#: when the value has moved clear of the edge, so 0.39 → 0.40 → 0.39 does not
#: swing her between 丁寧語 and タメ口 twice in three turns.
BAND_FLOORS: tuple[tuple[RelationshipBand, float], ...] = (
    ("close", 0.75),
    ("familiar", 0.45),
    ("acquaintance", 0.15),
    ("stranger", 0.0),
)
BAND_HYSTERESIS = 0.05


def relationship_band(
    familiarity: float, *, current: RelationshipBand | None = None
) -> RelationshipBand:
    """Which band this familiarity is in, with hysteresis (§16).

    Rising out of a band takes crossing its ceiling by the hysteresis margin;
    falling out takes dropping below its floor by the same. Inside the margin
    the band she is already in wins.
    """
    plain: RelationshipBand = "stranger"
    for band, floor in BAND_FLOORS:
        if familiarity >= floor:
            plain = band
            break
    if current is None or current == plain:
        return plain

    floors = dict(BAND_FLOORS)
    if BANDS.index(plain) > BANDS.index(current):
        # Moving up: the new band's floor has to be cleared by the margin.
        return plain if familiarity >= floors[plain] + BAND_HYSTERESIS else current
    # Moving down: the current band's floor has to be lost by the margin.
    return plain if familiarity < floors[current] - BAND_HYSTERESIS else current


@dataclass(frozen=True, slots=True)
class SurfacePlan:
    """How the turn is allowed to surface (§9)."""

    length: Length = "short"
    register: Register = "neutral"
    directness: Directness = "soft"
    self_disclosure: Disclosure = "none"
    #: §19: a budget, not a probability. 0 or 1.
    question_budget: int = 0
    #: The turn-level meaning behind the budget.  ``optional`` means a
    #: question is permitted, not required; it must not collapse to the hard
    #: prohibition represented by ``forbidden``.
    question_policy: str = QuestionPolicy.FORBIDDEN.value
    initiative: str = "balanced"
    fragmentation: Fragmentation = "natural"
    explicit_emotion: ExplicitEmotion = "low"
    relationship_band: RelationshipBand = "acquaintance"

    @property
    def allows_question(self) -> bool:
        return self.question_budget > 0

    def render(self) -> str:
        """What the realizer is told about size and distance."""
        lengths = {
            "very_short": "ひと言で。長くしない",
            "short": "短く。二文までを目安に",
            "medium": "ふつうの長さで",
            "long": "少し長く話してよい",
        }
        registers = {
            "polite": "ていねいな話し方",
            "neutral": "ふつうの話し方",
            "casual": "くだけた話し方",
        }
        directness_lines = {
            "soft": "言い切らず、やわらかく",
            "plain": "そのまま素直に",
            "direct": "はっきりと",
        }
        lines = [
            f"- 長さ: {lengths[self.length]}",
            f"- 口調: {registers[self.register]}",
            f"- 言い方: {directness_lines[self.directness]}",
        ]
        if self.question_policy == QuestionPolicy.FORBIDDEN:
            lines.append("- 質問はしない")
        elif self.question_policy == QuestionPolicy.REQUIRED:
            lines.append("- 質問を一つする")
        elif self.question_policy == QuestionPolicy.ENCOURAGED:
            lines.append("- 必要なら質問を一つして会話を進める")
        else:
            lines.append("- 軽い質問は一つまで可能。ただし質問しなくてもよい")
        if self.self_disclosure == "none":
            lines.append("- 自分の話は今回しなくてよい")
        if self.explicit_emotion == "low":
            lines.append("- 感情を言葉で説明しない。にじむ程度でよい")
        return "\n".join(lines)


#: Roughly how long the USER wrote, in characters.
_VERY_SHORT = 6
_SHORT = 20
_LONG = 120


class SurfacePlanner:
    """Turns a social reading into room to speak. No model call (§10)."""

    name = "surface_planner"

    def plan(
        self,
        interpretation: SocialInterpretation,
        *,
        user_text: str,
        band: RelationshipBand = "acquaintance",
        grounded_experience: bool = False,
        question_policy: QuestionPolicy | None = None,
    ) -> SurfacePlan:
        user_length = len((user_text or "").strip())
        resolved_policy = question_policy or self.question_policy(interpretation)
        question_budget = self.question_budget(
            interpretation, question_policy=question_policy
        )
        effective_policy = (
            resolved_policy if question_budget else QuestionPolicy.FORBIDDEN
        )
        return SurfacePlan(
            length=self._length(interpretation, user_length),
            register=self._register(band, interpretation),
            directness=self._directness(interpretation),
            self_disclosure=self._disclosure(interpretation, grounded_experience),
            question_budget=question_budget,
            question_policy=effective_policy.value,
            initiative=interpretation.initiative,
            fragmentation="natural",
            explicit_emotion=self._explicit_emotion(interpretation),
            relationship_band=band,
        )

    @staticmethod
    def question_budget(
        interpretation: SocialInterpretation,
        *,
        question_policy: QuestionPolicy | None = None,
    ) -> int:
        """§19. A count, never a rate.

        A policy of ``optional`` is permission, never an instruction to ask.
        Whether the realizer spends that one-question budget remains a prose
        decision. Repeated-question and explicit-forbidden guards still apply.
        """
        if question_policy is not None:
            return 0 if question_policy is QuestionPolicy.FORBIDDEN else 1
        policy = SurfacePlanner.question_policy(interpretation)
        if policy is QuestionPolicy.FORBIDDEN:
            return 0
        if policy is QuestionPolicy.OPTIONAL:
            return 1 if interpretation.initiative == "high" else 0
        return 1

    @staticmethod
    def question_policy(interpretation: SocialInterpretation) -> QuestionPolicy:
        return {
            "none": QuestionPolicy.FORBIDDEN,
            "optional": QuestionPolicy.OPTIONAL,
            "useful": QuestionPolicy.ENCOURAGED,
            "necessary": QuestionPolicy.REQUIRED,
        }.get(interpretation.question, QuestionPolicy.FORBIDDEN)

    @staticmethod
    def _length(interpretation: SocialInterpretation, user_length: int) -> Length:
        """A one-word message does not deserve a paragraph (§39 B)."""
        energy = interpretation.response_energy
        if energy == "very_low" or user_length <= _VERY_SHORT:
            return "very_short" if energy in ("very_low", "low") else "short"
        if energy == "low" or user_length <= _SHORT:
            return "short"
        if energy == "high" and user_length >= _LONG:
            return "long"
        return "medium"

    @staticmethod
    def _register(
        band: RelationshipBand, interpretation: SocialInterpretation
    ) -> Register:
        """Distance, not a rule about particles.

        Note what this does *not* do: it does not become more polite as things
        get more serious in a close relationship. Someone close who is having a
        bad day does not want to be addressed formally.
        """
        if band == "stranger":
            return "polite"
        if band == "acquaintance":
            return "polite" if interpretation.tone == "serious" else "neutral"
        return "casual"

    @staticmethod
    def _directness(interpretation: SocialInterpretation) -> Directness:
        if interpretation.is_repair:
            # A retraction is the one place to be plain: hedging it is how a
            # correction turns back into an argument (CORR-002).
            return "direct"
        if interpretation.user_state_hint in ("possibly_negative", "possibly_tired"):
            return "soft"
        if interpretation.primary_move in ("answer", "clarify"):
            return "plain"
        return "soft"

    @staticmethod
    def _disclosure(
        interpretation: SocialInterpretation, grounded_experience: bool
    ) -> Disclosure:
        """§21, §22. Offering an opinion is always available; offering an
        *experience* needs an experience that happened."""
        wanted = interpretation.self_disclosure
        if wanted == "none":
            return "none"
        if not grounded_experience and wanted == "moderate":
            return "light"
        return wanted

    @staticmethod
    def _explicit_emotion(interpretation: SocialInterpretation) -> ExplicitEmotion:
        if interpretation.tone in ("serious", "gentle"):
            return "medium"
        if interpretation.response_energy == "high":
            return "medium"
        return "low"


__all__ = [
    "BANDS",
    "BAND_HYSTERESIS",
    "RelationshipBand",
    "SurfacePlan",
    "SurfacePlanner",
    "relationship_band",
]
