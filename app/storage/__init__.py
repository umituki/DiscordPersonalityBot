"""Persistence layer.

Spec architecture rule: ``app/storage/`` and its repositories are the only
normal SQL boundary. No other layer writes SQL.
"""

from app.storage.database import Database, DatabaseError

__all__ = ["Database", "DatabaseError"]
