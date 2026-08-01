"""Typed configuration loading.

Layering (spec 31.1):

* ``config/settings.yaml`` — structured static settings, committed.
* ``.env`` / process environment — secrets and machine-local overrides, never committed.

Secrets are wrapped in ``SecretStr`` so they cannot be logged by accident.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

RuntimeMode = Literal["normal", "degraded", "minimal", "offline", "test", "simulation", "admin"]

DEFAULT_CONFIG_FILE = Path("config/settings.yaml")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AppSection(_Section):
    name: str = "yui"
    environment: Literal["development", "test", "production"] = "development"


class PathsSection(_Section):
    data_dir: Path = Path("data")
    logs_dir: Path = Path("logs")
    backups_dir: Path = Path("backups")
    policies_dir: Path = Path("config/policies")


class DatabaseSection(_Section):
    filename: str = "yui.db"
    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"] = "WAL"
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "FULL"
    busy_timeout_ms: int = Field(default=5000, ge=0)
    foreign_keys: bool = True


class RuntimeSection(_Section):
    mode: RuntimeMode = "normal"
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _utc_only(cls, value: str) -> str:
        # Spec 38: one consistent internal reference. Display conversion is an
        # interface concern, not a storage concern.
        if value.upper() != "UTC":
            raise ValueError("internal reference timezone must be UTC")
        return value.upper()


class LoggingSection(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    file: str = "yui.log"
    format: str = "%(asctime)s %(levelname)s %(name)s %(message)s"


class PoliciesSection(_Section):
    state_arbitration: str = "state_arbitration.yaml"


class Secrets(_Section):
    """Values sourced from the environment only."""

    discord_bot_token: SecretStr | None = None
    discord_owner_user_id: str | None = None
    discord_channel_id: str | None = None

    def require_discord_token(self) -> str:
        if self.discord_bot_token is None:
            raise ConfigError("DISCORD_BOT_TOKEN is not set")
        return self.discord_bot_token.get_secret_value()


class AppConfig(_Section):
    """Fully resolved configuration."""

    config_version: int = 1
    root_dir: Path
    config_file: Path
    app: AppSection = AppSection()
    paths: PathsSection = PathsSection()
    database: DatabaseSection = DatabaseSection()
    runtime: RuntimeSection = RuntimeSection()
    logging: LoggingSection = LoggingSection()
    policies: PoliciesSection = PoliciesSection()
    secrets: Secrets = Secrets()

    # --- resolved locations -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self._resolve(self.paths.data_dir)

    @property
    def logs_dir(self) -> Path:
        return self._resolve(self.paths.logs_dir)

    @property
    def backups_dir(self) -> Path:
        return self._resolve(self.paths.backups_dir)

    @property
    def policies_dir(self) -> Path:
        return self._resolve(self.paths.policies_dir)

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database.filename

    @property
    def log_path(self) -> Path:
        return self.logs_dir / self.logging.file

    @property
    def state_arbitration_policy_path(self) -> Path:
        return self.policies_dir / self.policies.state_arbitration

    def _resolve(self, value: Path) -> Path:
        return value if value.is_absolute() else (self.root_dir / value)

    def ensure_directories(self) -> None:
        """Create runtime directories. Never touches committed source paths."""
        for directory in (self.data_dir, self.logs_dir, self.backups_dir):
            directory.mkdir(parents=True, exist_ok=True)


_ENV_OVERRIDES: Mapping[str, tuple[str, str]] = {
    "YUI_ENVIRONMENT": ("app", "environment"),
    "YUI_DATA_DIR": ("paths", "data_dir"),
    "YUI_LOG_DIR": ("paths", "logs_dir"),
    "YUI_BACKUP_DIR": ("paths", "backups_dir"),
    "YUI_LOG_LEVEL": ("logging", "level"),
    "YUI_RUNTIME_MODE": ("runtime", "mode"),
    "YUI_DB_FILENAME": ("database", "filename"),
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    return loaded


def _environment(root_dir: Path, env: Mapping[str, str] | None, use_dotenv: bool) -> dict[str, str]:
    merged: dict[str, str] = {}
    if use_dotenv:
        dotenv_path = root_dir / ".env"
        if dotenv_path.is_file():
            merged.update({k: v for k, v in dotenv_values(dotenv_path).items() if v is not None})
    merged.update(dict(os.environ if env is None else env))
    return merged


def load_config(
    config_file: Path | str | None = None,
    *,
    root_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    use_dotenv: bool = True,
) -> AppConfig:
    """Load settings.yaml, apply environment overrides, and validate."""
    resolved_root = Path(root_dir).resolve() if root_dir is not None else Path.cwd().resolve()
    environ = _environment(resolved_root, env, use_dotenv)

    candidate = config_file or environ.get("YUI_CONFIG_FILE") or DEFAULT_CONFIG_FILE
    resolved_config = Path(candidate)
    if not resolved_config.is_absolute():
        resolved_config = resolved_root / resolved_config

    raw = _read_yaml(resolved_config)
    for env_key, (section, field) in _ENV_OVERRIDES.items():
        if env_key in environ and environ[env_key] != "":
            raw.setdefault(section, {})
            if not isinstance(raw[section], dict):
                raise ConfigError(f"configuration section '{section}' must be a mapping")
            raw[section][field] = environ[env_key]

    def _optional(name: str) -> str | None:
        value = environ.get(name, "").strip()
        return value or None

    token = _optional("DISCORD_BOT_TOKEN")
    raw["secrets"] = {
        "discord_bot_token": SecretStr(token) if token else None,
        "discord_owner_user_id": _optional("DISCORD_OWNER_USER_ID"),
        "discord_channel_id": _optional("DISCORD_CHANNEL_ID"),
    }
    raw["root_dir"] = resolved_root
    raw["config_file"] = resolved_config

    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError and friends
        raise ConfigError(f"invalid configuration in {resolved_config}: {exc}") from exc
