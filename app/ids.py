"""Identifier generation.

Spec 38: ``ID は domain prefix + UUID/ULID 等、一意性と追跡性を確保する``.

IDs are ``<prefix>_<ulid>``. The ULID keeps creation order sortable, which makes
event / run / change streams debuggable without an extra index.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Final

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODED_LEN: Final = 26

# Known domain prefixes. Keeping them here prevents ad-hoc prefixes drifting
# through the codebase.
EVENT: Final = "evt"
RUN: Final = "run"
SNAPSHOT: Final = "snap"
PROPOSAL: Final = "prop"
CHANGE: Final = "chg"
DELIVERY: Final = "dlv"
MANIFEST: Final = "man"
FAILURE: Final = "fail"
EVIDENCE: Final = "ev"

_lock = threading.Lock()
_last_ms = 0
_last_entropy = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def ulid(now_ms: int | None = None) -> str:
    """Return a monotonic ULID string (48-bit time + 80-bit entropy)."""
    global _last_ms, _last_entropy
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    with _lock:
        if timestamp < _last_ms:
            # Clock moved backwards: keep monotonicity of generated ids.
            timestamp = _last_ms
        if timestamp == _last_ms:
            _last_entropy += 1
            if _last_entropy >= 1 << 80:
                timestamp += 1
                _last_entropy = int.from_bytes(os.urandom(10), "big")
        else:
            _last_entropy = int.from_bytes(os.urandom(10), "big")
        _last_ms = timestamp
        entropy = _last_entropy
    return _encode(timestamp, 10) + _encode(entropy, 16)


def new_id(prefix: str) -> str:
    """Return a prefixed identifier, e.g. ``evt_01J...``."""
    if not prefix or "_" in prefix:
        raise ValueError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}_{ulid()}"


def prefix_of(identifier: str) -> str:
    """Return the domain prefix of an identifier."""
    head, _, rest = identifier.partition("_")
    if not rest:
        raise ValueError(f"identifier has no domain prefix: {identifier!r}")
    return head


def has_prefix(identifier: str, prefix: str) -> bool:
    return identifier.startswith(f"{prefix}_") and len(identifier) > len(prefix) + 1
