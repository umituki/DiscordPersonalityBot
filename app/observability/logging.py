"""Logging setup.

Spec 38: ``print()`` is never operational logging. Everything goes through the
standard logging module, to stderr and to a rotating file under ``logs/``.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

_configured = False


def configure_logging(
    *,
    level: str = "INFO",
    log_file: Path | None = None,
    fmt: str = "%(asctime)s %(levelname)s %(name)s %(message)s",
    force: bool = False,
) -> None:
    """Configure root logging once per process."""
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=DEFAULT_MAX_BYTES,
            backupCount=DEFAULT_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    _configured = True
