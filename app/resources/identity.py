"""Static identity loading (spec 1.3, 6, 27.1).

``character/`` holds what does not change with experience: who she is, where
she lives, the immutable rules, and how she speaks. Personality,
values and self schema are *not* here — those are dynamic state owned by their
engines and stored in SQLite.

Identity is a REQUIRED context item: it is never dropped on overflow (spec 27.2).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

IDENTITY_FILE = "identity.yaml"
IMMUTABLE_RULES_FILE = "immutable_rules.yaml"
SPEECH_FILE = "speech.md"


class IdentityError(RuntimeError):
    """Raised when static identity is missing or malformed."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")


class ImmutableRule(_Frozen):
    id: str
    statement: str


class Existence(_Frozen):
    """Who she is and where she lives.

    ``kind`` was ``digital`` and the summary said she had no body. That kept
    her safe by keeping her from being anybody: asked her age she answered that
    age did not apply to her. What actually prevents fabrication is evidence,
    which is untouched — so the constraint moved rather than went away. She is
    a person in her own world; nothing physical reaches the USER's.
    """

    kind: str = "person"
    #: Which world she lives in. Matches `app.world.scope.WorldScope`.
    world: str = "yui_world"
    summary: str = ""


class Language(_Frozen):
    primary: str = "ja"
    fallback: str | None = None


class Identity(_Frozen):
    """Who YUI is, independent of what she has experienced."""

    identity_version: int = 1
    rules_version: int = 1
    name: str
    display_name: str = ""
    existence: Existence = Existence()
    language: Language = Language()
    speech: str = ""
    must_not: tuple[ImmutableRule, ...] = ()
    may: tuple[ImmutableRule, ...] = ()
    extra: dict = Field(default_factory=dict)

    @property
    def world(self) -> str:
        """The world she lives in, as the scope vocabulary names it."""
        return self.existence.world

    def rule(self, rule_id: str) -> ImmutableRule:
        for rule in (*self.must_not, *self.may):
            if rule.id == rule_id:
                return rule
        raise IdentityError(f"unknown immutable rule: {rule_id!r}")

    def version_tag(self) -> str:
        """Recorded in the runtime manifest (spec 29)."""
        return f"identity@v{self.identity_version}+rules@v{self.rules_version}"

    def render_for_prompt(self) -> str:
        """The REQUIRED identity block of a prompt (spec 27.1).

        First person, not a role brief. 「YUIというキャラクターになりきって
        ください」 asks a model to perform someone; 「あなたは望月ゆいです」
        tells it who it is. The difference showed up as her narrating her own
        construction when asked ordinary questions.

        It is not a licence, either. Being someone does not entitle her to a
        past — every experience still comes from the authoritative state that
        follows this block, and the rules below say so.
        """
        lines = ["# あなたについて", f"名前: {self.name}"]
        if self.existence.summary:
            lines.append(self.existence.summary.strip())
        if self.must_not:
            lines.append("\n# 絶対に守ること")
            lines.extend(f"- {rule.statement}" for rule in self.must_not)
        if self.may:
            lines.append("\n# してよいこと")
            lines.extend(f"- {rule.statement}" for rule in self.may)
        if self.speech:
            lines.append("\n" + self.speech.strip())
        return "\n".join(lines).strip()


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise IdentityError(f"identity file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise IdentityError(f"identity file must be a mapping: {path}")
    return data


def load_identity(directory: Path | str) -> Identity:
    """Load ``character/`` into a validated :class:`Identity`."""
    root = Path(directory)
    identity_data = _read_yaml(root / IDENTITY_FILE)
    rules_data = _read_yaml(root / IMMUTABLE_RULES_FILE)

    speech_path = root / SPEECH_FILE
    speech = speech_path.read_text(encoding="utf-8") if speech_path.is_file() else ""

    known = {"identity_version", "name", "display_name", "existence", "language"}
    try:
        return Identity(
            identity_version=int(identity_data.get("identity_version", 1)),
            rules_version=int(rules_data.get("rules_version", 1)),
            name=identity_data["name"],
            display_name=identity_data.get("display_name", ""),
            existence=Existence.model_validate(identity_data.get("existence") or {}),
            language=Language.model_validate(identity_data.get("language") or {}),
            speech=speech,
            must_not=tuple(
                ImmutableRule.model_validate(rule) for rule in rules_data.get("must_not") or ()
            ),
            may=tuple(ImmutableRule.model_validate(rule) for rule in rules_data.get("may") or ()),
            extra={key: value for key, value in identity_data.items() if key not in known},
        )
    except KeyError as exc:
        raise IdentityError(f"identity is missing required field {exc.args[0]!r}") from exc
    except Exception as exc:
        raise IdentityError(f"invalid identity in {root}: {exc}") from exc
