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
    "SocialInterpretation": (
        '{"primary_move": "acknowledge", "secondary_move": null, '
        '"initiative": "low", "question": "none", "tone": "light", '
        '"response_energy": "low", "topic_direction": "stay", '
        '"user_state_hint": "unknown", "self_disclosure": "none", '
        '"wants_to_speak": "speak", "reason": "ordinary acknowledgement"}'
    ),
    "ReplyDraft": '{"text": "うん。"}',
    # Dialogue v2. A deliberately *neutral* reading: no question target, no
    # resolved message and no memory query, so `retrieval_query` falls back to
    # the raw user text and every existing memory test keeps the behaviour it
    # was written against. A test that is about interpretation overrides this.
    "TurnUnderstanding": (
        '{"current_topic": "", "user_intent": "acknowledge", '
        '"question_target": "none", "referenced_action_owner": "unclear", '
        '"referenced_subject": "", "temporal_scope": "unspecified", '
        '"resolved_message": "", "correction_target": "", "memory_query": "", '
        '"wants_memory": false, "self_disclosure_relevant": false, '
        '"memory_query_intent": "none", "reason": "offline default"}'
    ),
    # An ordinary offline reply asserts nothing, so there is nothing to ground.
    "SemanticClaimReview": '{"claims": []}',
    # Response Contract v2. The ordinary direct question is answered. The
    # reviewer no longer returns a fulfilment boolean — it classifies what the
    # reply addressed and in what form, and Python reads the contract — so the
    # default states the ordinary case: the USER's question, answered with the
    # thing they asked for. Tests for an omitted answer override this with
    # `non_answer`, and tests for a specific target override the target.
    "ResponseContractAssessment": (
        '{"addressed_target": "direct_user_question", '
        '"answer_mode": "value_or_proposition", "reason": "offline default"}'
    ),
    # Audit finding 2. The default resolves *nothing*: an offline double that
    # confidently picked a claim would let a broken resolver pass by always
    # guessing, which is the bug the resolver replaced.
    "CorrectionTarget": '{"claim_id": "", "denies": false, "reason": "offline default"}',
    # Semantic Memory grounding. Ordinary offline replies make no memory claim.
    "SemanticMemoryReview": '{"claims": []}',
    # Phase 2 §2E. A batch with no judgements means every candidate was left
    # unjudged, and an unjudged candidate does not pass the gate — so the
    # offline default recalls nothing. That is the conservative direction, and
    # a test that wants recalls has to say so (see ``RelevanceClient``).
    "MemoryRelevanceBatch": '{"judgements": []}',
    "EpisodeSummary": (
        '{"summary": "その時期のこと。よく歩いていた。", "topics": ["散歩"], '
        '"novelty": 0.7, "felt_significance": 0.6}'
    ),
    # Phase 9's 28.2 judgment. "yes" on purpose: a double that always declines
    # would let a broken proactive chain pass every test by never reaching the
    # end of it.
    "ProactiveJudgment": (
        '{"wants_to_say": "yes", "about": "昨日の話のつづき", '
        '"because": "unfinished_conversation"}'
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


class PassingContractReviewer:
    """A dependency double for tests whose subject is not answer fulfilment."""

    async def review(self, *_args, **_kwargs):
        from app.dialogue.response_contract import ContractReviewOutcome

        return ContractReviewOutcome(fulfilled=True, detail="test precondition")


def use_offline_model(
    application, overrides: dict[str, str] | None = None
) -> OfflineModelClient:
    """Point a built application's structured generator at a local double."""
    client = OfflineModelClient(overrides)
    application.structured._client = client  # noqa: SLF001
    return client


def live_shadow(config, **overrides: str):
    """A config with the spec 47 capabilities switched on.

    From Phase 14 the four autonomous capabilities ship in SHADOW: they
    deliberate in full and stop before acting. A test whose subject is one of
    those mechanisms — does contacting an NPC go through the Society Service,
    does a search reach the provider — has to say which mode it is testing,
    the same way it already says who the OWNER is.

    Tests about shadow mode itself must not use this, and neither must tests
    about whether the default is safe.
    """
    modes = {
        "proactive_mode": "LIVE",
        "silence_mode": "LIVE",
        "npc_contact_mode": "LIVE",
        "search_mode": "LIVE",
    }
    modes.update(overrides)
    return config.model_copy(
        update={"runtime": config.runtime.model_copy(update=modes)}
    )


def mark_born(application) -> str:
    """Give a test database a completed FIRST BOOT (rebuild spec 34.20).

    A precondition, not a shortcut. From Phase 13 the application comes up in
    management mode until FIRST BOOT completes — the autonomous runtime stays
    down and the character plane stays shut — so any test whose subject is what
    happens *after* she exists has to say that she does.

    Tests about first boot itself must never use this: they have to reach
    COMPLETE through the orchestrator, which is the entire point of that phase.
    """
    from app import ids

    epoch = application.rebuild.current_epoch()
    if epoch is None:
        epoch_id = ids.new_id("epo")
        application.db.execute(
            "INSERT INTO rebuild_epochs (epoch_id, started_at, reason, genesis_status) "
            "VALUES (?, ?, ?, 'complete')",
            (epoch_id, application.clock.now().isoformat(), "test precondition"),
        )
    else:
        epoch_id = epoch["epoch_id"]
    state = application.first_boot_state.ensure(epoch_id)
    if state["status"] != "COMPLETE":
        application.first_boot_state.transition(
            epoch_id,
            expected=(state["status"],),
            to="COMPLETE",
            now=application.clock.now(),
            completed_at=application.clock.now(),
        )
    return epoch_id


class PassingReranker:
    """A Stage 2 double that judges every candidate relevant.

    For tests whose subject is *not* relevance — suppression, origin filtering,
    forgetting, practice. Holding relevance constant is what makes those tests
    about the thing they claim to be about. Tests that are about the gate use
    the real :class:`~app.memory.relevance.SemanticReranker` with a scripted
    model, or a stub with the labels they need.
    """

    def __init__(self, label: str = "relevant") -> None:
        self.label = label
        self.calls: list[tuple[str, int]] = []

    async def judge(self, query_text, candidates, *, mode, batch_size=16, run_id=None, event_id=None):
        from app.memory.recall_models import RelevanceJudgement

        self.calls.append((query_text, len(candidates)))
        return (
            tuple(
                RelevanceJudgement(
                    memory_id=candidate.memory_id,
                    relevance=self.label,  # type: ignore[arg-type]
                    reason="test double",
                    source="llm",
                )
                for candidate in candidates
            ),
            "call_test",
            "llm",
        )


def relevance_answer(prompt: str, label: str = "strong") -> str:
    """Build a Stage 2 answer for whatever candidates a prompt actually lists.

    Lets a scripted model exercise the real reranker — prompt rendering, schema
    validation, id matching — rather than bypassing it.
    """
    import json
    import re

    ids = re.findall(r"memory_id: (\S+)", prompt)
    return json.dumps(
        {
            "judgements": [
                {"memory_id": memory_id, "relevance": label, "reason": "テスト"}
                for memory_id in ids
            ]
        },
        ensure_ascii=False,
    )


__all__ = [
    "ANSWERS",
    "OfflineModelClient",
    "PassingReranker",
    "live_shadow",
    "mark_born",
    "relevance_answer",
    "use_offline_model",
]
