"""Internal psychology events (spec 8.1 INTERNAL category).

Emotion episodes and mood shifts are recorded as events, appended inside the
same transaction as the state they describe. That keeps spec 11.2's required
metadata — trigger, target, cause, unresolved — attached to the moment it
happened, with full provenance, and without a second source of truth.
"""

from __future__ import annotations

from app.events.model import EventPayload, register_payload

EMOTION_ACTIVATED = "EMOTION_ACTIVATED"
MOOD_SHIFTED = "MOOD_SHIFTED"
NEED_CHANGED = "NEED_CHANGED"


@register_payload(EMOTION_ACTIVATED)
class EmotionActivatedPayload(EventPayload):
    name: str
    intensity: float
    previous_intensity: float
    #: Spec 11.2: emotion leads to an action tendency, not straight to behaviour.
    action_tendency: str
    trigger_event_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    cause: str = ""
    unresolved: bool = False
    appraisal_source: str = "default"
    appraisal_confidence: float = 0.0


@register_payload(MOOD_SHIFTED)
class MoodShiftedPayload(EventPayload):
    valence: float
    arousal: float
    previous_valence: float
    previous_arousal: float
    reason_code: str = ""


@register_payload(NEED_CHANGED)
class NeedChangedPayload(EventPayload):
    key: str
    value: float
    previous_value: float
    reason_code: str = ""
