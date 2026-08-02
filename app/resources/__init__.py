"""Static, human-editable resources (spec 31.1).

Markdown for narrative text, YAML for structured facts. Nothing here is dynamic
state — dynamic state lives in SQLite and is owned by its engine.
"""

from app.resources.identity import Identity, IdentityError, ImmutableRule, load_identity

__all__ = ["Identity", "IdentityError", "ImmutableRule", "load_identity"]
