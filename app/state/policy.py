"""Arbitration policy loading (spec 40).

Tuning values are configuration, not code constants. This module turns
``config/policies/state_arbitration.yaml`` into a typed, versioned policy.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class PolicyError(RuntimeError):
    """Raised when the arbitration policy cannot be loaded."""


class DomainPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``None`` means "no bound", used by non-scalar domains such as world.
    value_min: float | None = 0.0
    value_max: float | None = 1.0
    max_delta_per_event: float | None = 0.15
    requires_consolidation: bool = False
    min_evidence_count: int = Field(default=0, ge=0)

    def merged_with(self, override: dict) -> DomainPolicy:
        return DomainPolicy.model_validate({**self.model_dump(), **override})


class ArbitrationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: int = 1
    defaults: DomainPolicy = DomainPolicy()
    domains: dict[str, DomainPolicy] = Field(default_factory=dict)

    def for_domain(self, domain: str) -> DomainPolicy:
        return self.domains.get(domain, self.defaults)

    @classmethod
    def load(cls, path: Path | str) -> ArbitrationPolicy:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise PolicyError(f"arbitration policy not found: {policy_path}")
        with policy_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise PolicyError(f"arbitration policy root must be a mapping: {policy_path}")

        try:
            defaults = DomainPolicy.model_validate(raw.get("defaults") or {})
            domains = {
                name: defaults.merged_with(override or {})
                for name, override in (raw.get("domains") or {}).items()
            }
            return cls(
                policy_version=int(raw.get("policy_version", 1)),
                defaults=defaults,
                domains=domains,
            )
        except Exception as exc:
            raise PolicyError(f"invalid arbitration policy {policy_path}: {exc}") from exc

    @classmethod
    def permissive(cls) -> ArbitrationPolicy:
        """Unbounded policy for unit tests that are not about bounds."""
        return cls(
            defaults=DomainPolicy(
                value_min=None, value_max=None, max_delta_per_event=None,
                requires_consolidation=False, min_evidence_count=0,
            )
        )
