"""Built-in tools.

Only two exist at this phase, and one of them deliberately fails.

Spec 17.3: ``Search failure は失敗として保持し、pretraining knowledge で「検索成功」
を捏造しない``. Until a real search backend is configured, the search tool
reports an honest failure rather than answering from the model's own weights —
which is exactly the behaviour the specification demands.
"""

from __future__ import annotations

from typing import Any

from app.clock import Clock, SystemClock, to_iso
from app.tools.manager import ToolExecutionError, ToolRegistry
from app.tools.models import ToolSpec

CURRENT_TIME = "current_time"
WEB_SEARCH = "web_search"


def register_builtin_tools(registry: ToolRegistry, *, clock: Clock | None = None) -> None:
    resolved_clock = clock or SystemClock()

    async def current_time(arguments: dict[str, Any]) -> tuple[Any, str]:
        now = resolved_clock.now()
        return {"utc": to_iso(now)}, "system_clock"

    registry.register(
        ToolSpec(
            name=CURRENT_TIME,
            description="いまの時刻 (UTC) を返す。",
            permission="read_only",
            required_arguments=(),
            timeout_s=1.0,
        ),
        current_time,
    )

    async def unavailable_search(arguments: dict[str, Any]) -> tuple[Any, str]:
        raise ToolExecutionError(
            "no search backend is configured; the call did not happen",
            retryable=False,
        )

    registry.register(
        ToolSpec(
            name=WEB_SEARCH,
            description="ウェブ検索。バックエンド未設定のうちは必ず失敗を返す。",
            permission="external_side_effect",
            required_arguments=("query",),
            timeout_s=20.0,
        ),
        unavailable_search,
    )
