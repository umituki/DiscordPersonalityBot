"""Versioned prompt registry (spec 38, `.claude/rules/llm.md`).

``LLM prompt text を business logic ファイルへ散在させない。Prompt は versioned
files / registry で管理する``.

Layout::

    config/prompts/<prompt_id>/v<N>.md

Each file may start with a YAML front matter block::

    ---
    description: what this prompt is for
    variables: [user_message]
    ---
    body with ${user_message} placeholders

``${name}`` substitution is used rather than ``{name}`` so prompt bodies can
contain JSON examples without escaping.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml

logger = logging.getLogger(__name__)

_VERSION_FILE = re.compile(r"^v(\d+)\.(md|txt)$")
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class PromptError(RuntimeError):
    """Raised when a prompt is missing, malformed or misused."""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    prompt_id: str
    version: int
    body: str
    description: str = ""
    variables: tuple[str, ...] = ()

    @property
    def prompt_version(self) -> str:
        """Version string recorded on every call (spec 29)."""
        return f"{self.prompt_id}@v{self.version}"

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]

    def render(self, **values: object) -> str:
        missing = [name for name in self.variables if name not in values]
        if missing:
            raise PromptError(f"{self.prompt_version} is missing variables: {missing}")
        try:
            return Template(self.body).substitute(
                {key: str(value) for key, value in values.items()}
            )
        except KeyError as exc:
            raise PromptError(
                f"{self.prompt_version} references undeclared variable {exc.args[0]!r}"
            ) from exc


class PromptRegistry:
    """Loads prompt files once and serves them by id and version."""

    def __init__(self, templates: dict[str, dict[int, PromptTemplate]]) -> None:
        self._templates = templates

    @classmethod
    def load(cls, directory: Path | str) -> PromptRegistry:
        root = Path(directory)
        if not root.is_dir():
            raise PromptError(f"prompt directory not found: {root}")

        templates: dict[str, dict[int, PromptTemplate]] = {}
        for prompt_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            for file in sorted(prompt_dir.iterdir()):
                match = _VERSION_FILE.match(file.name)
                if match is None:
                    continue
                template = _parse(prompt_dir.name, int(match.group(1)), file)
                templates.setdefault(prompt_dir.name, {})[template.version] = template

        logger.info("prompt registry loaded prompts=%d from %s", len(templates), root)
        return cls(templates)

    def get(self, prompt_id: str, version: int | None = None) -> PromptTemplate:
        versions = self._templates.get(prompt_id)
        if not versions:
            raise PromptError(f"unknown prompt: {prompt_id!r}")
        if version is None:
            return versions[max(versions)]
        template = versions.get(version)
        if template is None:
            raise PromptError(f"prompt {prompt_id!r} has no version {version}")
        return template

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def versions_of(self, prompt_id: str) -> tuple[int, ...]:
        return tuple(sorted(self._templates.get(prompt_id, {})))

    def manifest_components(self) -> dict[str, str]:
        """Prompt versions for the runtime manifest (spec 29)."""
        return {
            f"prompt.{prompt_id}": f"v{max(versions)}+{versions[max(versions)].checksum}"
            for prompt_id, versions in self._templates.items()
        }

    def __len__(self) -> int:
        return len(self._templates)


def _parse(prompt_id: str, version: int, path: Path) -> PromptTemplate:
    raw = path.read_text(encoding="utf-8")
    description = ""
    variables: tuple[str, ...] = ()

    match = _FRONT_MATTER.match(raw)
    body = raw
    if match is not None:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise PromptError(f"invalid front matter in {path}: {exc}") from exc
        if not isinstance(meta, dict):
            raise PromptError(f"front matter must be a mapping in {path}")
        description = str(meta.get("description", ""))
        declared = meta.get("variables") or []
        if not isinstance(declared, list):
            raise PromptError(f"'variables' must be a list in {path}")
        variables = tuple(str(name) for name in declared)
        body = raw[match.end():]

    return PromptTemplate(
        prompt_id=prompt_id,
        version=version,
        body=body.strip() + "\n",
        description=description,
        variables=variables,
    )
