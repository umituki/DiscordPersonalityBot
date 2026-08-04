"""INVARIANT: it works against the real repositories, not only against fakes
(audit findings 6, 7, 8).

Three of the audit's findings were the same mistake in different places: code
that was only ever exercised through a purpose-built double.

    ``SituationBuilder`` called ``top()`` and ``strongest()``. Neither method
    exists on any repository in this project. Every test passed, because every
    test supplied an object that had them — so the Values and Self sections
    could never have appeared in production, and nothing said so.

    Six trace stages were marked and silently dropped, because
    ``ConversationTrace.mark`` ignores unregistered names. The latency of the
    three most expensive new steps was unmeasurable and no test noticed.

    The prompt budget reported ``fraction=None`` and ``over_budget=False``
    always, because no budget was ever passed in. A measurement that cannot
    fail is not a measurement.

So this file builds a real ``Application`` on a fresh SQLite database, runs
real turns through it, and reads the results back out of the database.
"""

from __future__ import annotations

import json

import pytest

from app.bootstrap import Application
from app.storage.repositories import CommonGroundRepository
from tests.support import mark_born, use_offline_model

pytestmark = pytest.mark.invariant

OWNER = "111111111111111111"
CHANNEL = "222222222222222222"


def _owned(config, **runtime):
    return config.model_copy(
        update={
            "secrets": config.secrets.model_copy(
                update={
                    "discord_owner_user_id": OWNER,
                    "discord_channel_id": CHANNEL,
                }
            ),
            "runtime": config.runtime.model_copy(update=runtime),
        }
    )


@pytest.fixture
def application(temp_config, clock):
    built = Application.build(_owned(temp_config), clock=clock, configure_logs=False)
    use_offline_model(built)
    mark_born(built)
    try:
        yield built
    finally:
        built.db.close()


_MESSAGE = {"count": 0}


async def _turn(application, clock, text: str):
    """One complete turn, including the send confirmation.

    The trace is written when the turn *finishes* — on confirmation or on
    suppression — so a helper that stopped at `handle_inbound` would leave
    nothing in the table to assert against.
    """
    from app.interfaces.discord.dto import InboundMessage

    _MESSAGE["count"] += 1
    result = await application.conversation.handle_inbound(
        InboundMessage(
            message_id=f"prod-{_MESSAGE['count']}",
            channel_id=CHANNEL,
            channel_type="direct_message",
            author_id=OWNER,
            text=text,
            created_at=clock.now(),
        )
    )
    if result.should_send:
        await application.conversation.confirm_sent(
            result, message_id=f"sent-{_MESSAGE['count']}"
        )
    return result


# =============================================================================
# Finding 6: the real repository API
# =============================================================================


def test_values_self_and_goals_are_reachable_in_production(application, clock) -> None:
    """The section that could never have appeared, against real rows.

    `ValueRepository.all()`, `SelfRepository.active()` and
    `GoalRepository.active()` are the published APIs. The builder used to call
    two methods that exist nowhere, so this section was permanently
    ``unavailable`` in production and permanently ``present`` under a fake.
    """
    application.value_repo.seed({"誠実さ": 0.9, "好奇心": 0.7}, now=clock.now())
    application.self_repo.ensure(
        name="reader", statement="本を読むのが好き", strength=0.8, now=clock.now()
    )
    application.goal_repo.upsert(
        description="短歌を続ける",
        source="interest",
        reason="続けたいから",
        importance=0.7,
        autonomy=0.8,
        obligation=0.1,
        expected_reward=0.6,
        identity_relevance=0.7,
        value_alignment=0.6,
        origin="virtual_life",
        now=clock.now(),
    )

    situation = application.situation.build(
        understanding=_disclosing(), now=clock.now()
    )

    values = situation.get("self_and_values")
    goals = situation.get("goals")
    assert values.availability == "present", values.render()
    assert goals.availability == "present", goals.render()
    assert any("誠実さ" in line for line in values.lines)
    assert any("本を読むのが好き" in line for line in values.lines)
    assert any("短歌" in line for line in goals.lines)


def test_the_sections_are_empty_not_broken_on_a_fresh_database(
    application, clock
) -> None:
    """Nothing seeded: ``empty``, which is a fact. Never ``unavailable``,
    which would mean the API call failed."""
    situation = application.situation.build(
        understanding=_disclosing(), now=clock.now()
    )

    assert situation.get("self_and_values").availability == "empty"
    assert situation.get("goals").availability == "empty"
    assert "self_and_values" not in situation.unavailable


def _disclosing():
    from app.dialogue.understanding import TurnUnderstanding

    return TurnUnderstanding(self_disclosure_relevant=True, current_topic="短歌")


# =============================================================================
# Finding 7: the trace actually records the latencies
# =============================================================================


async def test_the_new_stages_reach_the_trace_database(application, clock) -> None:
    """Marked *and* stored. They used to be marked and dropped."""
    await _turn(application, clock, "やっほー")

    row = application.db.query_one(
        "SELECT * FROM conversation_traces ORDER BY received_at DESC LIMIT 1"
    )
    assert row is not None, "no trace was written"
    assert row["understanding_started_at"], "turn understanding was not timed"
    assert row["understanding_ended_at"]
    assert row["memory_recall_started_at"]
    assert row["realization_started_at"]
    assert row["semantic_grounding_started_at"], "semantic grounding was not timed"
    assert row["semantic_grounding_ended_at"]


async def test_every_required_latency_is_measurable(application, clock) -> None:
    """The six the audit named, from one row."""
    await _turn(application, clock, "やっほー")

    row = application.db.query_one(
        "SELECT detail_json, total_ms FROM conversation_traces "
        "ORDER BY received_at DESC LIMIT 1"
    )
    latencies = json.loads(row["detail_json"])["latencies_ms"]

    for name in (
        "understanding_ms",
        "memory_recall_ms",
        "realization_ms",
        "semantic_grounding_ms",
        "total_ms",
    ):
        assert name in latencies, f"{name} is not measurable; got {sorted(latencies)}"
        assert latencies[name] >= 0
    assert row["total_ms"] is not None


async def test_repair_latency_is_recorded_when_repair_runs(application, clock) -> None:
    """Absent when no repair happened — a turn that did not repair did not take
    zero milliseconds repairing."""
    use_offline_model(
        application,
        overrides={
            "ReplyDraft": '{"text": "今日は図書館で本を読んだよ。"}',
            "SemanticClaimReview": (
                '{"claims": [{"proposition": "YUIは今日図書館で本を読んだ", '
                '"trigger": "今日は図書館で本を読んだよ", "subject": "yui", '
                '"category": "yui_completed_action", "modality": "assertion", '
                '"temporal_scope": "today", "supporting_ids": [], '
                '"contradicting_ids": []}]}'
            ),
        },
    )

    await _turn(application, clock, "今日は何してた？")

    row = application.db.query_one(
        "SELECT repair_started_at, repair_ended_at, detail_json "
        "FROM conversation_traces ORDER BY received_at DESC LIMIT 1"
    )
    assert row["repair_started_at"], "repair ran and was not timed"
    assert row["repair_ended_at"]
    assert "repair_ms" in json.loads(row["detail_json"])["latencies_ms"]


# =============================================================================
# Finding 8: the budget is measured against a real budget
# =============================================================================


async def test_the_prompt_budget_is_measured_and_stored(application, clock) -> None:
    await _turn(application, clock, "やっほー")

    row = application.db.query_one(
        "SELECT prompt_tokens, prompt_budget_tokens, prompt_over_budget, detail_json "
        "FROM conversation_traces ORDER BY received_at DESC LIMIT 1"
    )
    budget = json.loads(row["detail_json"])["prompt_budget"]

    assert row["prompt_tokens"] > 0, "the prompt measured as empty"
    assert row["prompt_budget_tokens"] > 0, "no budget was compared against"
    assert budget["fraction"] is not None, "fraction was None; nothing was compared"
    assert 0 < budget["fraction"]
    assert budget["over_budget"] is False
    assert row["prompt_over_budget"] == 0


async def test_a_very_long_turn_is_detected_as_over_budget(
    application, clock
) -> None:
    """A ~4000-token input against a 3000-token budget must be *detected*.

    The old measurement could not detect anything: no budget was passed, so
    `over_budget` was a constant False.
    """
    from app.context.builder import measure_messages
    from app.llm.types import LLMMessage

    budget = application.conversation_engine._policy.context.budget()  # noqa: SLF001
    huge = "あ" * (budget.max_tokens * 2 * 2)  # ~2x the budget in tokens

    measured = measure_messages([LLMMessage(role="system", content=huge)], budget)

    assert measured.tokens > budget.max_tokens
    assert measured.over_budget
    assert measured.fraction > 1.0
    assert measured.budget_tokens == budget.max_tokens
    assert "budget" in measured.describe()


async def test_a_user_turn_too_large_to_fit_is_an_explicit_failure(
    application, clock
) -> None:
    """§27.2 in production. The current message is REQUIRED, so a message that
    cannot fit alongside identity is a configuration failure — surfaced as a
    named suppression rather than as an exception out of the reply path."""
    long_turn = "きょうのことを話すね。" * 400

    result = await _turn(application, clock, long_turn)

    assert result.suppressed
    assert result.outbound is None
    failure = result.generation.outcome.failure
    assert failure is not None
    assert failure.reason_code == "context_overflow"


def test_required_context_alone_over_budget_is_a_configuration_failure() -> None:
    """§27.2. Identity and the current message are never dropped, so if they
    alone do not fit, that is a configuration error rather than something to
    trim silently."""
    from app.context.builder import (
        ContextBudget,
        ContextBuilder,
        ContextOverflowError,
        Requirement,
    )

    builder = ContextBuilder()
    builder.add("identity", "あ" * 8000, requirement=Requirement.REQUIRED)

    with pytest.raises(ContextOverflowError):
        builder.build(ContextBudget(max_tokens=1000))


def test_the_drop_policy_governs_the_sections_the_template_renders() -> None:
    """Audit finding 8's other half.

    Grounding, common ground, correction, the situation and the references used
    to be rendered *after* the builder had finished accounting, so the drop
    policy covered about half the prompt. They are candidates now, and OPTIONAL
    ones go first under pressure.
    """
    from app.context.builder import ContextBudget, ContextBuilder, Requirement

    builder = ContextBuilder()
    builder.add("identity", "あ" * 200, requirement=Requirement.REQUIRED, priority=100)
    builder.add("grounding_facts", "い" * 200, requirement=Requirement.REQUIRED, priority=85)
    builder.add("common_ground", "う" * 400, requirement=Requirement.IMPORTANT, priority=60)
    builder.add("situation", "え" * 400, requirement=Requirement.OPTIONAL, priority=45)
    builder.add("references", "お" * 400, requirement=Requirement.OPTIONAL, priority=10)

    built = builder.build(ContextBudget(max_tokens=260))

    kept = set(built.keys())
    assert "identity" in kept
    assert "grounding_facts" in kept, "what she may state as fact was dropped"
    assert "references" not in kept, "an OPTIONAL section survived the squeeze"


# =============================================================================
# Findings 2 and 3, end to end on real rows
# =============================================================================


async def test_the_second_of_two_claims_is_the_one_retracted(
    application, clock
) -> None:
    """The audit's named regression, on the production path.

    Two live claims; the USER corrects the second. `live[0]` would have
    retracted the first — a claim she can support — and left the second
    standing.
    """
    conversation = application.conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    repository = CommonGroundRepository(application.db)
    first = repository.record(
        conversation_id=conversation.conversation_id,
        event_id=None,
        kind="yui_completed_action",
        statement="YUIは今日本を読んだ",
        status="supported",
        source="yui_inference",
        confidence="high",
        semantic={"proposition": "YUIは今日本を読んだ", "subject": "yui"},
        now=clock.now(),
    )
    second = repository.record(
        conversation_id=conversation.conversation_id,
        event_id=None,
        kind="yui_completed_action",
        statement="YUIは今日詩を書いた",
        status="supported",
        source="yui_inference",
        confidence="high",
        semantic={"proposition": "YUIは今日詩を書いた", "subject": "yui"},
        now=clock.now(),
    )
    use_offline_model(
        application,
        overrides={
            "CorrectionTarget": json.dumps(
                {"claim_id": second.claim_id, "denies": True, "reason": "詩を否定"}
            )
        },
    )

    await _turn(application, clock, "詩は書いてないよ")

    assert repository.get(first.claim_id).status == "supported", (
        "the wrong claim was retracted"
    )
    assert repository.get(second.claim_id).status != "supported"


async def test_an_unresolved_correction_retracts_nothing(application, clock) -> None:
    """Ambiguity is not a licence to guess."""
    conversation = application.conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    repository = CommonGroundRepository(application.db)
    claims = [
        repository.record(
            conversation_id=conversation.conversation_id,
            event_id=None,
            kind="yui_completed_action",
            statement=statement,
            status="supported",
            source="yui_inference",
            confidence="high",
            now=clock.now(),
        )
        for statement in ("YUIは今日本を読んだ", "YUIは今日詩を書いた")
    ]
    # The offline default resolves nothing.
    use_offline_model(application)

    await _turn(application, clock, "ちがうよ")

    assert [repository.get(claim.claim_id).status for claim in claims] == [
        "supported",
        "supported",
    ]


async def test_the_stored_claim_is_what_a_correction_reads(
    application, clock
) -> None:
    """Finding 3. The verified representation is on the row, so nothing has to
    re-parse the delivered sentence to find out what was claimed."""
    conversation = application.conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )
    repository = CommonGroundRepository(application.db)
    repository.record(
        conversation_id=conversation.conversation_id,
        event_id=None,
        kind="yui_completed_action",
        statement="YUIは今日詩を書いた",
        status="supported",
        source="yui_inference",
        confidence="high",
        semantic={
            "proposition": "YUIは今日詩を書いた",
            "subject": "yui",
            "category": "yui_completed_action",
            "modality": "assertion",
        },
        subject="yui",
        modality="assertion",
        now=clock.now(),
    )
    model = use_offline_model(application)

    await _turn(application, clock, "詩？ちがうよ")

    prompt = next(
        "\n".join(m.content for m in request.messages)
        for request in reversed(model.requests)
        if request.purpose == "correction_target"
    )
    assert "YUIは今日詩を書いた" in prompt
    assert "yui_completed_action" in prompt
    assert "一覧に無いIDを書かない" in prompt


def test_a_delivered_claim_stores_its_verified_representation(
    application, clock
) -> None:
    """What makes finding 3 structural rather than a promise."""
    from app.conversation.common_ground import CommonGroundTracker
    from app.dialogue.semantic_claims import (
        EvidenceResolver,
        SemanticClaimCandidate,
        SemanticReviewOutcome,
    )
    from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
    from app.grounding.models import Evidence, GroundingContext
    from app.grounding.policy import GroundingPolicy

    evidence = Evidence(
        kind="activity", reference="act_1", summary="本を読んだ", subject="yui"
    )
    reviewed = SemanticReviewOutcome(
        claims=(
            EvidenceResolver().resolve(
                SemanticClaimCandidate(
                    proposition="YUIは今日本を読んだ",
                    trigger="読んだよ",
                    subject="yui",
                    category="yui_completed_action",
                    supporting_ids=(evidence.evidence_id,),
                ),
                GroundingContext(completed_activities_today=(evidence,)),
            ),
        )
    )
    policy = GroundingPolicy.load("config/policies/grounding.yaml")
    extractor = ClaimExtractor(policy)
    repository = CommonGroundRepository(application.db)
    conversation = application.conversations.ensure_conversation(
        channel_id=CHANNEL, channel_type="direct_message", now=clock.now()
    )

    CommonGroundTracker(
        repository, extractor=extractor, guard=ClaimGroundingGuard(extractor)
    ).record_reply(
        "読んだよ",
        conversation_id=conversation.conversation_id,
        event_id=None,
        context=None,
        reviewed=reviewed,
        now=clock.now(),
    )

    stored = repository.live(conversation.conversation_id)[0]
    semantic = json.loads(stored.semantic_json)
    assert semantic["proposition"] == "YUIは今日本を読んだ"
    assert semantic["category"] == "yui_completed_action"
    assert stored.subject == "yui"
    assert stored.modality == "assertion"
    assert stored.evidence == ("activity:act_1",)


# =============================================================================
# Finding 4 (round 2): one semantic authority on the production path
# =============================================================================


def _count_legacy_reviews(application) -> list[int]:
    """Wrap the legacy regex guard so the test can see whether it was asked.

    Returns a one-element list used as a counter, because the wrapper has to
    stay a plain function on the engine's own attribute — replacing the guard
    with a double would test the double.
    """
    engine = application.conversation_engine
    guard = engine._grounding  # noqa: SLF001
    assert guard is not None, "the legacy guard is not wired; the test proves nothing"
    calls = [0]
    original = guard.review

    def counting(text, context):
        calls[0] += 1
        return original(text, context)

    guard.review = counting  # type: ignore[method-assign]
    return calls


async def test_a_normal_draft_is_judged_by_exactly_one_authority(
    application, clock
) -> None:
    """Audit finding 4 (round 2).

    Both readers used to run on every production draft: the semantic reviewer
    resolved the claims by identity, and then the regex extractor re-read the
    same sentence and merged a second verdict. Two authorities answering one
    question is the defect — the weaker one could veto a sentence the stronger
    one had cleared, and neither was accountable for the result.
    """
    legacy = _count_legacy_reviews(application)
    model = use_offline_model(application)

    result = await _turn(application, clock, "やっほー")

    assert not result.suppressed, "the draft did not survive; count the reviews of what did"
    assert model.purposes().count("semantic_claim_review") == 1, (
        f"the draft was reviewed {model.purposes().count('semantic_claim_review')} "
        "times; got " + repr(model.purposes())
    )
    assert legacy[0] == 0, (
        "the legacy regex guard judged a draft the semantic reviewer had already "
        "settled"
    )
    assert "memory_claim_review" not in model.purposes(), (
        "the retired memory reviewer is still reachable in production"
    )


async def test_the_legacy_guard_speaks_only_where_there_is_no_reviewer(
    application, clock
) -> None:
    """The other half of one-authority: not two, but never zero.

    With the semantic reviewer unwired — an older deployment, or a draft it
    could not run on — the regex extractor is the only thing between a
    fabricated claim and the USER. That is a fallback, not a second opinion,
    and it has to still happen.
    """
    legacy = _count_legacy_reviews(application)
    application.conversation_engine._semantic_claims = None  # noqa: SLF001
    model = use_offline_model(application)

    await _turn(application, clock, "やっほー")

    assert legacy[0] == 1, "nothing reviewed the draft at all"
    assert "semantic_claim_review" not in model.purposes()


def test_the_retired_memory_reviewer_is_constructed_nowhere() -> None:
    """`SemanticMemoryGroundingGuard` is reference material, not a code path.

    Asserted against the application source rather than against a run, because
    "it did not fire on this turn" is a weaker statement than "nothing builds
    it".
    """
    import pathlib

    hits = [
        path
        for path in pathlib.Path("app").rglob("*.py")
        if path.name != "memory_semantics.py"
        and "SemanticMemoryGroundingGuard(" in path.read_text(encoding="utf-8")
    ]
    assert hits == [], f"the retired memory guard is constructed in {hits}"


# =============================================================================
# Finding 6 (round 2): NPC evidence is interactions, not definitions
# =============================================================================


def _npc_claim(evidence_id: str):
    from app.dialogue.semantic_claims import SemanticClaimCandidate

    return SemanticClaimCandidate(
        proposition="ミカと昨日話した",
        trigger="ミカと話したんだ",
        # The reviewer's word for an NPC. The evidence rows say `other`;
        # translating between the two is `ownership.normalize_subject`, and
        # comparing them directly is what used to refuse every valid npc_fact.
        subject="npc",
        category="npc_fact",
        supporting_ids=(evidence_id,),
    )


def _production_grounding(application, clock):
    """The builder the running application actually uses."""
    builder = application.conversation._grounding  # noqa: SLF001
    assert builder is not None
    return builder.build(now=clock.now())


def test_an_npc_that_merely_exists_grounds_nothing(application, clock) -> None:
    """Audit finding 6 (round 2), against real rows.

    The builder read `NPCRepository.all()` — the *definitions* — and emitted one
    ``npc_interaction`` per NPC, whose summary was the NPC's name. So writing a
    person into the world made "I talked to her yesterday" supportable, with the
    row proving her existence standing in for the conversation.
    """
    from app.dialogue.semantic_claims import EvidenceResolver

    npc = application.society.introduce(name="ミカ", tier=2, role="友人")

    context = _production_grounding(application, clock)

    assert context.npc_interactions == (), (
        "a defined NPC produced interaction evidence: "
        f"{[e.evidence_id for e in context.npc_interactions]}"
    )
    assert context.availability["npc_interactions"] == "empty"

    resolved = EvidenceResolver().resolve(
        _npc_claim(f"npc_interaction:{npc.npc_id}"), context
    )
    assert not resolved.supported, "an NPC's existence settled what she did"
    assert resolved.blocking
    assert any("unknown_evidence" in reason for reason in resolved.refusals), (
        resolved.refusals
    )


def test_a_recorded_interaction_is_what_supports_an_npc_fact(
    application, clock
) -> None:
    """And when it did happen, the claim stands — on the interaction row."""
    from app.dialogue.semantic_claims import EvidenceResolver

    npc = application.society.introduce(name="ミカ", tier=2, role="友人")
    record = application.society.interact(
        npc.npc_id, kind="conversation", valence=0.4, summary="短歌の話をした"
    )

    context = _production_grounding(application, clock)

    assert context.availability["npc_interactions"] == "available"
    evidence = context.npc_interactions[0]
    assert evidence.evidence_id == f"npc_interaction:{record.interaction.interaction_id}"
    assert evidence.subject == "other", "an NPC's action was filed as somebody else's"
    assert "ミカ" in evidence.summary and "短歌の話をした" in evidence.summary

    resolved = EvidenceResolver().resolve(_npc_claim(evidence.evidence_id), context)
    assert resolved.supported, resolved.refusals
    assert not resolved.blocking


def test_the_builder_reads_the_interaction_repository_not_the_roster(
    application,
) -> None:
    """Structural, so the wiring cannot quietly revert.

    The two repositories are different objects with different tables; passing
    the roster where the evidence belongs is the whole finding, and a test that
    only checks the output would pass again the moment somebody re-pointed the
    parameter.
    """
    from app.storage.repositories.society import (
        NPCInteractionRepository,
        NPCRepository,
    )

    builder = application.conversation._grounding  # noqa: SLF001
    assert isinstance(builder._npc_interactions_repo, NPCInteractionRepository)  # noqa: SLF001
    assert isinstance(builder._npcs, NPCRepository)  # noqa: SLF001
