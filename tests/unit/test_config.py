"""Phase 0: configuration loads, overrides apply, secrets stay out of logs."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_loads_committed_settings(temp_config: AppConfig) -> None:
    assert temp_config.app.name == "yui"
    assert temp_config.runtime.timezone == "UTC"
    assert temp_config.database.journal_mode == "WAL"
    assert temp_config.database_path.name == "yui.db"


def test_paths_resolve_under_root(temp_config: AppConfig, tmp_path: Path) -> None:
    assert temp_config.data_dir == tmp_path / "data"
    assert temp_config.log_path == tmp_path / "logs" / "yui.log"
    assert temp_config.state_arbitration_policy_path.is_file()


def test_ensure_directories_creates_runtime_paths(temp_config: AppConfig) -> None:
    temp_config.ensure_directories()
    assert temp_config.data_dir.is_dir()
    assert temp_config.logs_dir.is_dir()
    assert temp_config.backups_dir.is_dir()


def test_environment_overrides_settings(tmp_path: Path, temp_config: AppConfig) -> None:
    config = load_config(
        root_dir=tmp_path,
        env={"YUI_LOG_LEVEL": "DEBUG", "YUI_DATA_DIR": "elsewhere"},
        use_dotenv=False,
    )
    assert config.logging.level == "DEBUG"
    assert config.data_dir == tmp_path / "elsewhere"


def test_secrets_come_from_environment_only(tmp_path: Path, temp_config: AppConfig) -> None:
    config = load_config(
        root_dir=tmp_path, env={"DISCORD_BOT_TOKEN": "s3cret"}, use_dotenv=False
    )
    assert config.secrets.require_discord_token() == "s3cret"
    # The token must never appear in a repr / log line.
    assert "s3cret" not in repr(config)
    assert "s3cret" not in str(config.secrets.discord_bot_token)


def test_missing_token_raises_only_when_required(temp_config: AppConfig) -> None:
    assert temp_config.secrets.discord_bot_token is None
    with pytest.raises(ConfigError):
        temp_config.secrets.require_discord_token()


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(root_dir=tmp_path, env={}, use_dotenv=False)


def test_non_utc_reference_timezone_is_rejected(tmp_path: Path, temp_config: AppConfig) -> None:
    settings = tmp_path / "config" / "settings.yaml"
    settings.write_text(
        settings.read_text(encoding="utf-8").replace("timezone: UTC", "timezone: Asia/Tokyo"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(root_dir=tmp_path, env={}, use_dotenv=False)


def test_repository_settings_are_valid() -> None:
    """The committed settings.yaml itself must always validate."""
    config = load_config(root_dir=REPO_ROOT, env={}, use_dotenv=False)
    assert config.config_version >= 1
