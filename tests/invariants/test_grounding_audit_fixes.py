"""INVARIANT: the ownership matrix runs in every direction (audit findings 1-9).

The independent audit failed the first cut of Dialogue v2 on nine structural
points. The common thread in the worst of them was *one-sidedness*: a rule was
enforced for the case somebody had been bitten by, and left off for the mirror
image of the same rule.

    claims about YUI were filtered to YUI-owned evidence — and claims about the
    USER were not filtered at all, so her activity row could settle their past;

    ``world`` counted as "YUI's own", so a world event supported a claim that
    she did something;

    hedging was read off the surface, so 「〜のようです」 about her actual
    preference needed no evidence at all;

    a correction took the newest live claim, which is a coin toss the moment
    there are two;

    and a failed repository read became an empty list, which reads downstream
    as "she did nothing today".

Each section here pushes on one of those, in both directions where there are
two.
"""

from __future__ import annotations

import json

import pytest

from app.dialogue.correction import CorrectionResolver, ResolvedTarget, render_claims
from app.dialogue.semantic_claims import (
    COMMITTING_MODALITIES,
    EvidenceResolver,
    SemanticClaimCandidate,
)
from app.dialogue.situation import SituationBuilder
from app.dialogue.understanding import TurnUnderstanding
from app.grounding.context import GroundingContextBuilder
from app.grounding.models import Evidence, GroundingContext
from app.grounding.ownership import (
    SUPPORTING_SUBJECTS,
    may_support,
    refusal_reason,
)

pytestmark = pytest.mark.invariant


def _evidence(subject: str, *, kind: str, reference: str = "e1", relation: str = "actor"):
    return Evidence(
        kind=kind,
        reference=reference,
        summary="詩を詠んだ",
        subject=subject,
        relation=relation,
    )


def _resolve(category: str, evidence: Evidence, *, section: str):
    context = GroundingContext(**{section: (evidence,)})
    candidate = SemanticClaimCandidate(
        proposition="なにかをした",
        trigger="したよ",
        category=category,
        supporting_ids=(evidence.evidence_id,),
    )
    return EvidenceResolver().resolve(candidate, context)


# =============================================================================
# Finding 1: the matrix, in every direction the audit named
# =============================================================================


def test_yui_evidence_cannot_settle_the_users_past() -> None:
    """The mirror image of the bug that was fixed first.

    Her having read a book is not evidence that they did, and until now
    nothing checked: the ownership filter only ran for claims about YUI.
    """
    resolved = _resolve(
        "user_past_fact",
        _evidence("yui", kind="objective_event"),
        section="recent_objective_events",
    )

    assert not resolved.supported
    assert any(reason.startswith("wrong_subject") for reason in resolved.refusals)


@pytest.mark.parametrize(
    "category",
    [
        "yui_completed_action",
        "yui_experience_habit",
        "yui_perception",
        "yui_specific_memory_recall",
    ],
)
def test_user_evidence_cannot_settle_her_own_life(category) -> None:
    resolved = _resolve(
        category,
        _evidence("user", kind="objective_event"),
        section="recent_objective_events",
    )

    assert not resolved.supported
    assert any(reason.startswith("wrong_subject") for reason in resolved.refusals)


def test_a_world_event_is_not_something_she_did() -> None:
    """`world` used to count as "her own", so it rained and she got rained on."""
    resolved = _resolve(
        "yui_completed_action",
        _evidence("world", kind="objective_event"),
        section="recent_objective_events",
    )

    assert not resolved.supported
    assert any(reason.startswith("wrong_subject") for reason in resolved.refusals)


def test_npc_evidence_settles_neither_of_the_two_people() -> None:
    npc = _evidence("other", kind="objective_event")

    for category in ("user_past_fact", "yui_completed_action", "yui_experience_habit"):
        resolved = _resolve(category, npc, section="recent_objective_events")

        assert not resolved.supported, category


def test_her_own_activity_does_settle_her_own_action() -> None:
    """The gate has to pass the true case, or it is a mute button."""
    resolved = _resolve(
        "yui_completed_action",
        _evidence("yui", kind="activity"),
        section="completed_activities_today",
    )

    assert resolved.supported
    assert not resolved.blocking


def test_a_user_record_does_settle_the_users_past() -> None:
    resolved = _resolve(
        "user_past_fact",
        _evidence("user", kind="verified_user_fact"),
        section="verified_user_facts",
    )

    assert resolved.supported


def test_a_world_event_reaches_perception_only_as_experiencer() -> None:
    """The one permissive case, and it has to be earned.

    A world record can support "I heard the rain" only when some subsystem has
    said she was its experiencer. Nothing sets that by default.
    """
    topical = _evidence("world", kind="world_state", relation="actor")
    affecting = _evidence("world", kind="world_state", relation="experiencer")

    assert not may_support("yui_perception", subject="world", relation="actor")
    assert may_support("yui_perception", subject="world", relation="experiencer")
    assert not _resolve("yui_perception", topical, section="current_world").supported
    assert _resolve("yui_perception", affecting, section="current_world").supported


def test_the_relation_failure_is_named_separately() -> None:
    """Repair needs to tell "wrong person" from "wrong relationship to it"."""
    assert (
        refusal_reason("yui_perception", subject="world", relation="actor")
        == "wrong_relation"
    )
    assert (
        refusal_reason("yui_completed_action", subject="user") == "wrong_subject"
    )


def test_every_claim_category_has_a_matrix_entry() -> None:
    """A category nobody has thought about must not default to permissive."""
    from app.grounding.models import CLAIM_KINDS

    missing = [kind for kind in CLAIM_KINDS if kind not in SUPPORTING_SUBJECTS]
    assert not missing, f"no ownership rule for: {missing}"
    assert not may_support("a_category_nobody_defined", subject="yui")


# =============================================================================
# Finding 4: memory claims are two categories in one schema
# =============================================================================


def test_an_authority_fact_cannot_make_a_specific_recollection_real() -> None:
    """"Memory is fallible" does not mean she remembers last summer."""
    authority = Evidence(
        kind="memory_authority",
        reference="memory.selective_fallible",
        summary="記憶の想起は選択的で不確実である",
        subject="world",
    )
    resolved = _resolve(
        "yui_specific_memory_recall", authority, section="memory_authority_facts"
    )

    assert not resolved.supported


def test_an_authority_fact_does_settle_a_capability_claim() -> None:
    authority = Evidence(
        kind="memory_authority",
        reference="memory.selective_fallible",
        summary="記憶の想起は選択的で不確実である",
        subject="world",
    )
    resolved = _resolve(
        "yui_general_memory_capability", authority, section="memory_authority_facts"
    )

    assert resolved.supported


def test_a_recalled_memory_cannot_settle_a_capability_claim() -> None:
    """And the reverse: one remembered afternoon says nothing about how
    memory works in general."""
    memory = _evidence("yui", kind="subjective_memory")
    resolved = _resolve(
        "yui_general_memory_capability",
        memory,
        section="recalled_subjective_memories",
    )

    assert not resolved.supported


# =============================================================================
# Finding 5: modality is read from the proposition
# =============================================================================


def test_a_supposition_needs_no_evidence() -> None:
    """「わたしなら好きかも」 — a situation that did not happen."""
    candidate = SemanticClaimCandidate(
        proposition="もしYUIがその立場なら好きだろう",
        trigger="わたしなら好きかも",
        category="yui_experience_habit",
        modality="hypothetical",
    )

    assert not EvidenceResolver().resolve(candidate, GroundingContext()).blocking


def test_a_softened_statement_about_herself_still_needs_evidence() -> None:
    """「静かな時間に読むのが好きなようです」 — softened, and still asserting.

    This is the case the old ``hedged => no evidence`` rule waved through.
    """
    candidate = SemanticClaimCandidate(
        proposition="YUIは静かな時間に読むのが好きである",
        trigger="読むのが好きなようです",
        category="yui_experience_habit",
        modality="hedged_assertion",
    )

    resolved = EvidenceResolver().resolve(candidate, GroundingContext())

    assert resolved.needs_evidence
    assert resolved.blocking


def test_an_uncertain_recollection_still_needs_evidence() -> None:
    """「前に読んだ気がする」 — uncertain about her *actual* past.

    Either there is a record, or the honest reply is that she cannot remember.
    """
    candidate = SemanticClaimCandidate(
        proposition="YUIは以前それを読んだ",
        trigger="前に読んだ気がする",
        category="yui_specific_memory_recall",
        modality="uncertain_recall",
    )

    assert EvidenceResolver().resolve(candidate, GroundingContext()).blocking


def test_the_committing_modalities_are_explicit() -> None:
    """Adding a modality must force a decision, not inherit a default."""
    assert "hedged_assertion" in COMMITTING_MODALITIES
    assert "uncertain_recall" in COMMITTING_MODALITIES
    assert "hypothetical" not in COMMITTING_MODALITIES
    assert "intention" not in COMMITTING_MODALITIES
    assert "question" not in COMMITTING_MODALITIES


# =============================================================================
# Finding 9: a failed read is not a zero
# =============================================================================


class _BrokenActivities:
    def ongoing(self):
        raise RuntimeError("the activity repository is gone")

    def completed(self, **kwargs):
        raise RuntimeError("the activity repository is gone")


class _EmptyActivities:
    def ongoing(self):
        return None

    def completed(self, **kwargs):
        return []


def test_the_authority_distinguishes_failure_from_zero() -> None:
    """`_safe` flattened both into an empty tuple, which is where the
    information was lost — and it cannot be recovered downstream."""
    broken = GroundingContextBuilder(activities=_BrokenActivities()).build()
    empty = GroundingContextBuilder(activities=_EmptyActivities()).build()

    assert broken.status("completed_activities_today") == "unavailable"
    assert empty.status("completed_activities_today") == "empty"
    assert broken.degraded
    assert not empty.degraded
    assert "completed_activities_today" in broken.unavailable_sections


def test_an_unreadable_source_is_named_in_the_prompt() -> None:
    from app.conversation.engine import _render_grounding

    broken = GroundingContextBuilder(activities=_BrokenActivities()).build()

    rendered = _render_grounding(broken)

    assert "読めなかった情報源" in rendered
    assert "無かったことにして話さない" in rendered


def test_an_unreadable_source_is_named_to_the_reviewer() -> None:
    from app.dialogue.semantic_claims import render_evidence

    broken = GroundingContextBuilder(activities=_BrokenActivities()).build()

    rendered = render_evidence(broken)

    assert "読めなかった情報源" in rendered
    assert "「記録が無い」という意味ではない" in rendered


def test_an_unreadable_source_reaches_the_situation() -> None:
    broken = GroundingContextBuilder(activities=_BrokenActivities()).build()

    situation = SituationBuilder().build(grounding=broken)

    assert "completed_today" in situation.unavailable
    assert "読めなかった" in situation.render()


def test_a_real_zero_still_reads_as_a_real_zero() -> None:
    empty = GroundingContextBuilder(activities=_EmptyActivities()).build()

    situation = SituationBuilder().build(grounding=empty)

    assert "completed_today" not in situation.unavailable
    assert "何もしていないという意味ではない" in situation.render()


# =============================================================================
# Finding 2: the correction target is resolved, not guessed
# =============================================================================


class _Claim:
    def __init__(self, claim_id: str, statement: str, **semantic) -> None:
        self.claim_id = claim_id
        self.statement = statement
        self.kind = semantic.get("category", "yui_completed_action")
        self.status = "supported"
        self.semantic_json = json.dumps(semantic, ensure_ascii=False)
        self.subject = semantic.get("subject", "")


class _Resolver:
    """A structured generator that answers with whatever it was told to."""

    def __init__(self, answer: dict | None) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def generate(self, schema, messages, **kwargs):
        self.prompts.append("\n".join(m.content for m in messages))
        if self.answer is None:
            return type("Outcome", (), {"accepted": False, "value": None})()
        return type(
            "Outcome", (), {"accepted": True, "value": schema(**self.answer)}
        )()


def _resolver(answer, prompts):
    return CorrectionResolver(prompts=prompts, structured=_Resolver(answer))


async def test_the_second_claim_can_be_the_corrected_one(prompt_registry) -> None:
    """The whole point. `live[0]` would always have taken the first."""
    claims = [
        _Claim("cgc_1", "YUIは本を読んだ", subject="yui"),
        _Claim("cgc_2", "YUIは詩を書いた", subject="yui"),
    ]

    resolved = await _resolver({"claim_id": "cgc_2", "denies": True}, prompt_registry).resolve(
        "詩は書いてないよ", claims
    )

    assert resolved.claim_id == "cgc_2"
    assert resolved.denies


async def test_an_invented_claim_id_retracts_nothing(prompt_registry) -> None:
    """Same rule as evidence citation: Python owns whether a name refers."""
    claims = [
        _Claim("cgc_1", "YUIは本を読んだ"),
        _Claim("cgc_2", "YUIは詩を書いた"),
    ]

    resolved = await _resolver({"claim_id": "cgc_99"}, prompt_registry).resolve(
        "ちがうよ", claims
    )

    assert not resolved.resolved
    assert "unknown_claim" in resolved.refused


async def test_ambiguity_retracts_nothing(prompt_registry) -> None:
    """Two outstanding claims and no resolution is exactly the case that used
    to silently retract the newest one."""
    claims = [_Claim("cgc_1", "A"), _Claim("cgc_2", "B")]

    resolved = await _resolver(None, prompt_registry).resolve("ちがうよ", claims)

    assert not resolved.resolved
    assert resolved.refused == "resolver_unavailable"


async def test_one_outstanding_claim_is_unambiguous(prompt_registry) -> None:
    """Refusing to act on a clear denial is its own failure."""
    resolved = await _resolver(None, prompt_registry).resolve(
        "ちがうよ", [_Claim("cgc_1", "A")]
    )

    assert resolved.claim_id == "cgc_1"


async def test_nothing_outstanding_needs_no_model_call(prompt_registry) -> None:
    generator = _Resolver({"claim_id": "cgc_1"})
    resolver = CorrectionResolver(prompts=prompt_registry, structured=generator)

    resolved = await resolver.resolve("ちがうよ", [])

    assert not resolved.resolved
    assert generator.prompts == []


# =============================================================================
# Finding 3: correction reads the stored claim, never the sentence again
# =============================================================================


def test_the_resolver_is_shown_the_stored_semantic_claim() -> None:
    """Not the delivered prose. The claim that was verified before the send."""
    claims = [
        _Claim(
            "cgc_1",
            "surface sentence that nobody should re-parse",
            proposition="YUIは今日詩を書いた",
            subject="yui",
            category="yui_completed_action",
        )
    ]

    rendered = render_claims(claims)

    assert "cgc_1" in rendered
    assert "YUIは今日詩を書いた" in rendered
    assert "yui_completed_action" in rendered
    assert "surface sentence" not in rendered


def test_a_row_without_a_stored_claim_still_renders() -> None:
    """Legacy rows predate the stored representation and must not crash."""
    legacy = _Claim("cgc_old", "むかしの主張")
    legacy.semantic_json = ""

    assert "むかしの主張" in render_claims([legacy])


def test_reverification_reresolves_the_stored_claim_not_the_sentence(
    temp_config, clock
) -> None:
    """Finding 3's other half, and the one that survived the first pass.

    ``_still_supported`` re-ran the regex guard over the delivered sentence —
    a second reader, on prose the first reader had already approved, treated as
    authoritative. It now re-resolves the stored Semantic Claim against
    evidence as it stands now.
    """
    from app.conversation.common_ground import CommonGroundTracker
    from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
    from app.grounding.policy import GroundingPolicy
    from app.storage.database import Database
    from app.storage.migrations import migrate
    from app.storage.repositories import CommonGroundRepository, ConversationRepository

    database = Database(temp_config.database_path)
    database.connect()
    migrate(database, clock=clock)
    try:
        conversations = ConversationRepository(database)
        conversation = conversations.ensure_conversation(
            channel_id="c", channel_type="direct_message", now=clock.now()
        )
        repository = CommonGroundRepository(database)
        evidence = Evidence(
            kind="activity", reference="act_1", summary="本を読んだ", subject="yui"
        )
        repository.record(
            conversation_id=conversation.conversation_id,
            event_id=None,
            kind="yui_completed_action",
            statement="YUIは今日本を読んだ",
            status="supported",
            source="yui_inference",
            confidence="high",
            semantic={
                "proposition": "YUIは今日本を読んだ",
                "trigger": "読んだよ",
                "subject": "yui",
                "category": "yui_completed_action",
                "modality": "assertion",
                "supporting_ids": [evidence.evidence_id],
            },
            now=clock.now(),
        )
        policy = GroundingPolicy.load("config/policies/grounding.yaml")
        extractor = ClaimExtractor(policy)
        calls: list[str] = []

        class WatchingGuard(ClaimGroundingGuard):
            def review(self, text, context):  # noqa: D102
                calls.append(text)
                return super().review(text, context)

        tracker = CommonGroundTracker(
            repository, extractor=extractor, guard=WatchingGuard(extractor)
        )

        # The activity is still on record, so the claim still stands.
        standing = tracker.review_correction(
            "違うよ",
            conversation_id=conversation.conversation_id,
            context=GroundingContext(completed_activities_today=(evidence,)),
            now=clock.now(),
        )
        # The activity is gone, so it does not.
        repository.record(
            conversation_id=conversation.conversation_id,
            event_id=None,
            kind="yui_completed_action",
            statement="YUIは昨日詩を書いた",
            status="supported",
            source="yui_inference",
            confidence="high",
            semantic={
                "proposition": "YUIは昨日詩を書いた",
                "subject": "yui",
                "category": "yui_completed_action",
                "modality": "assertion",
                "supporting_ids": ["activity:act_missing"],
            },
            now=clock.now(),
        )
        gone = tracker.review_correction(
            "違うよ",
            conversation_id=conversation.conversation_id,
            context=GroundingContext(),
            target_claim_id=repository.live(conversation.conversation_id)[0].claim_id,
            now=clock.now(),
        )

        assert standing.retracted is False
        assert standing.claim.status == "contested"
        assert gone.retracted is True
        assert calls == [], "the legacy parser re-read a claim that had a stored one"
    finally:
        database.close()
