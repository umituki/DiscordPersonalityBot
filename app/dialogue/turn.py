"""One turn's state, carried as one object (Dialogue v2).

This type exists because of a specific bug, and its shape is the fix for the
whole class of it.

``ConversationService`` built the common ground and the correction outcome,
passed both to ``plan_turn``, and then called ``draft_reply`` without them.
Both parameters had ``""`` defaults, so nothing failed: the realizer simply
rendered 「(この会話でまだ前提になっていることはない)」 into every prompt, and the
correction the USER had just made vanished between the plan and the sentence.
A retraction that the planner knew about did not reach the writer.

Defaults that mean "absent" are what made that silent. So the turn's state is
one object, assembled once, handed to every stage that needs any of it. A stage
that forgets it gets a type error rather than an empty string, and adding a new
field does not create eight new call sites that can each omit it.

Nothing here is authoritative. It is a *carrier*: `grounding` is the authority,
`understanding` and `situation` are readings, and the two are kept in separate
fields precisely so that no consumer can mistake one for the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.dialogue.understanding import UNREAD, TurnUnderstanding


@dataclass(frozen=True, slots=True)
class TurnState:
    """Everything one turn knows about itself."""

    #: What the USER actually typed. Never replaced by the resolved form —
    #: the resolution is for retrieval and interpretation, and answering the
    #: rewritten question rather than the asked one is its own failure.
    user_text: str = ""
    #: The conversation so far, already rendered.
    recent_conversation: str = ""
    #: The model's reading of this turn. Interpretation, never evidence.
    understanding: TurnUnderstanding = UNREAD
    #: The working picture. Read-only, non-authoritative, turn-local.
    situation: Any = None
    #: The authority. The only thing that can make a claim true.
    grounding: Any = None
    #: Rebuild spec 11: what this conversation has already established.
    common_ground: str = ""
    #: CORR-002: what was just taken back, if anything.
    correction: str = ""

    @property
    def memory_query(self) -> str:
        """What to search memory for, with ellipsis resolved where possible."""
        return self.understanding.retrieval_query(self.user_text)

    def render_understanding(self) -> str:
        return self.understanding.render()

    def render_situation(self) -> str:
        if self.situation is None:
            return "(状況を組み立てられなかった)"
        return self.situation.render()


__all__ = ["TurnState"]
