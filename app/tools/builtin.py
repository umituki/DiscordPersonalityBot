"""Built-in tools.

Only two exist at this phase, and one of them deliberately fails.

Spec 17.3: ``Search failure は失敗として保持し、pretraining knowledge で「検索成功」
を捏造しない``. Until a real search backend is configured, the search tool
reports an honest failure rather than answering from the model's own weights —
which is exactly the behaviour the specification demands.
"""

from __future__ import annotations

from typing import Any

from app.clock import Clock, SystemClock, from_iso, to_iso
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
            required_arguments=("query", "effective_now"),
            timeout_s=20.0,
        ),
        unavailable_search,
    )


def register_search_provider(
    registry: ToolRegistry, provider: Any, *, clock: Clock | None = None
) -> None:
    """Give ``web_search`` a real backend (Phase 11).

    Replaces the always-failing stub. The provider is reached through the tool
    rather than beside it, so that spec 32.2 keeps holding: ToolManager is the
    only authority on whether a search actually ran, and a search that never
    happened cannot be described as one that did.

    ``effective_now`` is a required argument of the tool, not a default filled
    in here. A caller that forgets it gets a refusal, which is the behaviour a
    past simulation needs — silently substituting today's date is the leak.
    """
    from app.knowledge.search import SearchQuery

    resolved_clock = clock or SystemClock()

    async def run_search(arguments: dict[str, Any]) -> tuple[Any, str]:
        try:
            query = SearchQuery(
                text=str(arguments["query"]),
                effective_now=from_iso(str(arguments["effective_now"])),
                topic=str(arguments.get("topic", "")),
                max_results=int(arguments.get("max_results", 5)),
            )
        except Exception as exc:  # noqa: BLE001 - a bad request is a failed call
            raise ToolExecutionError(f"invalid search request: {exc}", retryable=False)

        response = await provider.search(query)
        if response.outcome in ("timeout", "network_error", "provider_error"):
            # A provider that could not answer is a *failed call*, not an empty
            # result. Reporting it as success with zero results would make "the
            # network was down" indistinguishable from "there is nothing there".
            raise ToolExecutionError(
                f"{response.outcome}: {response.detail}",
                retryable=response.outcome in ("timeout", "network_error"),
            )
        return (
            {
                "outcome": response.outcome,
                "provider": response.provider,
                "detail": response.detail,
                "results": [result.model_dump() for result in response.results],
            },
            response.provider or getattr(provider, "name", "search"),
        )

    registry.replace(
        ToolSpec(
            name=WEB_SEARCH,
            description="ウェブ検索。effective_now より後に公開された結果は使えない。",
            permission="external_side_effect",
            required_arguments=("query", "effective_now"),
            timeout_s=20.0,
        ),
        run_search,
    )
