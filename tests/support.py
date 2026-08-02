"""Shared test doubles.

Tests must mock the LLM boundary unless they explicitly target Ollama
integration (``.claude/rules/testing.md``). A full Genesis calls the model for
several different purposes, so a scripted queue is the wrong shape: what is
needed is something that answers *any* structured request with a valid answer
of the right schema, forever.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.llm.types import LLMResponse

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

#: One valid answer per structured schema the system asks for.
ANSWERS: dict[str, str] = {
    "Appraisal": (
        '{"self_relevance": 0.5, "goal_congruence": 0.3, "novelty": 0.4, '
        '"certainty": 0.6, "control": 0.5, "agency": 0.5, "social_meaning": 0.2, '
        '"expectation_violation": 0.1, "confidence": 0.6, "reason": "ふつうの出来事"}'
    ),
    # Rebuild spec APP-001: meaning categories, not numbers.
    "AppraisalCandidate": (
        '{"self_relevance": "medium", "goal_congruence": "positive", '
        '"novelty": "medium", "certainty": "medium", "control": "medium", '
        '"agency": "other", "social_meaning": "positive", '
        '"expectation_violation": "low", "confidence": "medium", '
        '"reason": "ふつうの出来事"}'
    ),
    "DialogueAct": (
        '{"acknowledge": true, "goal": "maintain_connection", "mode": "smalltalk", '
        '"question_need": "none"}'
    ),
    "ReplyDraft": '{"text": "うん。"}',
    "EpisodeSummary": (
        '{"summary": "その時期のこと。よく歩いていた。", "topics": ["散歩"], '
        '"novelty": 0.7, "felt_significance": 0.6}'
    ),
    "ExperienceNarration": (
        '{"summary": "川沿いを歩いた", "topics": ["散歩"], '
        '"felt_significance": 0.5, "involves_other_person": false}'
    ),
}


class OfflineModelClient:
    """Answers every structured request with a valid answer of its schema.

    Stands in for a reachable local model. Requests it does not recognise raise,
    so a new purpose cannot silently pass a test by being answered with
    nonsense.
    """

    model = "offline-test-model"

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.requests: list = []
        #: Schema title -> raw answer, for a test that needs one purpose to
        #: answer badly while everything else keeps working.
        self.overrides: dict[str, str] = dict(overrides or {})

    async def generate(self, request):
        self.requests.append(request)
        title = (request.format_schema or {}).get("title", "")
        answer = self.overrides.get(title, ANSWERS.get(title))
        if answer is None:
            raise AssertionError(f"no offline answer for schema {title!r}")
        return LLMResponse(text=answer, model=self.model, created_at=NOW, latency_ms=5)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    # --- convenience --------------------------------------------------------
    def purposes(self) -> list[str]:
        return [request.purpose for request in self.requests]


def use_offline_model(
    application, overrides: dict[str, str] | None = None
) -> OfflineModelClient:
    """Point a built application's structured generator at a local double."""
    client = OfflineModelClient(overrides)
    application.structured._client = client  # noqa: SLF001
    return client


__all__ = ["ANSWERS", "OfflineModelClient", "use_offline_model"]
