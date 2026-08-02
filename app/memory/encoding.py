"""The Encoding Gate (spec 10.4).

Not everything that happens is remembered. The gate scores an episode from
signals — novelty, emotion, prediction error, whether it was directed at YUI,
and how much substance it had — and only stores it if the score clears the
policy threshold.

The model may *propose* novelty and felt significance while summarising, but
the resulting importance is computed here, in Python, from policy weights
(spec 2.1, 9.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.memory.policy import EncodingPolicy


class EpisodeSummary(BaseModel):
    """Structured output asked of the model when encoding (spec 28.2)."""

    summary: str = Field(min_length=1)
    topics: list[str] = Field(default_factory=list)
    novelty: float = Field(default=0.0, ge=0.0, le=1.0)
    #: The model's felt significance. A signal, never the final importance.
    felt_significance: float = Field(default=0.0, ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class EncodingSignals:
    """What the gate looks at.

    ``emotional_intensity`` and ``prediction_error`` stay at zero until the
    psychology phase supplies them; the gate is already shaped to receive them.
    """

    novelty: float = 0.0
    emotional_intensity: float = 0.0
    prediction_error: float = 0.0
    user_directed: float = 0.0
    substance: float = 0.0

    def clamped(self) -> EncodingSignals:
        def clamp(value: float) -> float:
            return max(0.0, min(1.0, value))

        return EncodingSignals(
            novelty=clamp(self.novelty),
            emotional_intensity=clamp(self.emotional_intensity),
            prediction_error=clamp(self.prediction_error),
            user_directed=clamp(self.user_directed),
            substance=clamp(self.substance),
        )


@dataclass(frozen=True, slots=True)
class EncodingDecision:
    encode: bool
    importance: float
    components: dict[str, float] = field(default_factory=dict)
    reason: str = ""


class EncodingGate:
    def __init__(self, policy: EncodingPolicy) -> None:
        self._policy = policy

    def evaluate(self, signals: EncodingSignals) -> EncodingDecision:
        signals = signals.clamped()
        weights = self._policy.weights
        components = {
            "novelty": weights.novelty * signals.novelty,
            "emotional_intensity": weights.emotional_intensity * signals.emotional_intensity,
            "prediction_error": weights.prediction_error * signals.prediction_error,
            "user_directed": weights.user_directed * signals.user_directed,
            "substance": weights.substance * signals.substance,
        }
        weighted = sum(components.values())
        base = self._policy.base_importance
        importance = min(1.0, base + weighted * (1.0 - base))

        encode = importance >= self._policy.encode_threshold
        return EncodingDecision(
            encode=encode,
            importance=round(importance, 6),
            components=components,
            reason="above_threshold" if encode else "below_threshold",
        )

    @property
    def threshold(self) -> float:
        return self._policy.encode_threshold


def substance_of(transcript: str, *, full_at_chars: int = 600) -> float:
    """A crude proxy for how much actually happened in an episode."""
    if not transcript:
        return 0.0
    return min(1.0, len(transcript.strip()) / full_at_chars)


def user_directed_ratio(user_turns: int, total_turns: int) -> float:
    if total_turns <= 0:
        return 0.0
    return max(0.0, min(1.0, user_turns / total_turns))
