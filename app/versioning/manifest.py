"""Runtime manifest (spec 29).

Every processing run references the exact software configuration that produced
it: schema version, engine versions, policy version, code commit. This is what
lets spec 2.17 hold — a model, prompt or engine update is visible as a manifest
change, and is therefore never mistaken for YUI growing as a person.

Components are declared per phase. Phase 1 knows about the event schema, the
arbitration policy and the code itself; later phases add their model and prompt
versions through :meth:`RuntimeManifest.with_components`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app import __version__, ids
from app.clock import Clock, SystemClock
from app.storage.repositories.manifests import ManifestRepository

logger = logging.getLogger(__name__)


class RuntimeManifest(BaseModel):
    """Identity of the running software configuration."""

    model_config = ConfigDict(frozen=True)

    config_version: int
    event_schema_version: int
    code_commit_hash: str | None = None
    components: dict[str, str] = Field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        material = json.dumps(
            {
                "config_version": self.config_version,
                "event_schema_version": self.event_schema_version,
                "code_commit_hash": self.code_commit_hash,
                "components": self.components,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def with_components(self, **components: str) -> RuntimeManifest:
        return self.model_copy(update={"components": {**self.components, **components}})


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    manifest_id: str
    fingerprint: str
    created: bool


class ManifestService:
    """Registers manifests, reusing the id of an identical configuration."""

    def __init__(
        self, repository: ManifestRepository, *, clock: Clock | None = None
    ) -> None:
        self._repository = repository
        self._clock = clock or SystemClock()

    def ensure(self, manifest: RuntimeManifest) -> ManifestRecord:
        fingerprint = manifest.fingerprint
        existing = self._repository.find_by_fingerprint(fingerprint)
        if existing is not None:
            return ManifestRecord(existing["manifest_id"], fingerprint, created=False)

        manifest_id = ids.new_id(ids.MANIFEST)
        self._repository.insert(
            manifest_id=manifest_id,
            fingerprint=fingerprint,
            created_at=self._clock.now(),
            code_commit_hash=manifest.code_commit_hash,
            config_version=manifest.config_version,
            event_schema_version=manifest.event_schema_version,
            components=manifest.components,
        )
        logger.info(
            "runtime manifest registered manifest_id=%s commit=%s",
            manifest_id,
            manifest.code_commit_hash or "unknown",
        )
        return ManifestRecord(manifest_id, fingerprint, created=True)


def detect_commit_hash(root_dir: Path | str) -> str | None:
    """Read the current commit from ``.git`` without invoking git."""
    git_dir = Path(root_dir) / ".git"
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        return None
    try:
        head = head_file.read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        ref_file = git_dir / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip() or None
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or " " not in line:
                    continue
                commit, name = line.split(" ", 1)
                if name.strip() == ref:
                    return commit
    except OSError:
        logger.warning("could not read git HEAD under %s", git_dir)
    return None


def base_components(*, policy_version: int, schema_version: int) -> dict[str, str]:
    """The component versions that exist as of Phase 1."""
    return {
        "app_version": __version__,
        "db_schema_version": str(schema_version),
        "state_arbitration_policy_version": str(policy_version),
        # Engines are added by the phase that introduces them (spec 29).
    }
