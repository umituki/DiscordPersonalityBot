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
    claims_about,
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


#: The claim vocabulary's word for each evidence-subject. `_declared_subject`
#: is the inverse of `ownership.normalize_subject`, so a helper can name the
#: subject a category is necessarily about without hard-coding eleven cases.
_CLAIM_WORD = {"other": "npc"}


def _declared_subject(category: str) -> str:
    return _CLAIM_WORD.get(claims_about(category), claims_about(category))


def _resolve(
    category: str, evidence: Evidence, *, section: str, subject: str | None = None
):
    """Resolve one citation, with the subject the category is about.

    The subject defaults rather than being omitted because a committing claim
    now has to declare one — these tests are about which *evidence* may stand
    behind a category, so they state the subject and let the evidence be the
    variable.
    """
    context = GroundingContext(**{section: (evidence,)})
    candidate = SemanticClaimCandidate(
        proposition="なにかをした",
        trigger="したよ",
        subject=subject if subject is not None else _declared_subject(category),
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
    ("category", "reason"),
    [
        ("yui_completed_action", "wrong_subject"),
        ("yui_experience_habit", "wrong_subject"),
        ("yui_perception", "wrong_subject"),
        # Round 2 made this one stricter still: a recollection accepts only
        # `subjective_memory`, so a USER event is refused on kind before the
        # subject is even considered. Unsupported either way.
        ("yui_specific_memory_recall", "wrong_kind"),
    ],
)
def test_user_evidence_cannot_settle_her_own_life(category, reason) -> None:
    resolved = _resolve(
        category,
        _evidence("user", kind="objective_event"),
        section="recent_objective_events",
    )

    assert not resolved.supported
    assert resolved.blocking
    assert any(item.startswith(reason) for item in resolved.refusals), resolved.refusals


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


# =============================================================================
# Round 2. Codex's reproductions, as fixtures.
# =============================================================================


class TestCategorySubjectConsistency:
    """BLOCKING 1. The claim has to agree with itself before evidence is read.

    Codex's reproduction: a claim declaring ``subject="yui"`` under
    ``category="user_past_fact"``, cited against a USER-owned
    ``verified_user_fact``, resolved as *supported*. The evidence matched the
    category, nothing checked it matched the declared subject, and the
    contradiction between the claim's own two fields went unnoticed.
    """

    def test_the_codex_reproduction_is_refused(self) -> None:
        evidence = Evidence(
            kind="verified_user_fact",
            reference="uf_1",
            summary="本を読んだ",
            subject="user",
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは本を読んだ",
            trigger="読んだよ",
            subject="yui",
            category="user_past_fact",
            supporting_ids=(evidence.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(verified_user_facts=(evidence,))
        )

        assert not resolved.supported
        assert resolved.blocking
        assert any(
            reason.startswith("category_subject_mismatch")
            for reason in resolved.refusals
        )

    def test_the_mirror_image_is_refused_too(self) -> None:
        evidence = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        candidate = SemanticClaimCandidate(
            proposition="USERは本を読んだ",
            trigger="読んだんだね",
            subject="user",
            category="yui_completed_action",
            supporting_ids=(evidence.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(completed_activities_today=(evidence,))
        )

        assert not resolved.supported
        assert any(
            reason.startswith("category_subject_mismatch")
            for reason in resolved.refusals
        )

    def test_a_consistent_claim_still_resolves(self) -> None:
        """The gate must pass the true case, or it is a mute button."""
        evidence = Evidence(
            kind="verified_user_fact",
            reference="uf_1",
            summary="本を読んだ",
            subject="user",
        )
        candidate = SemanticClaimCandidate(
            proposition="USERは本を読んだ",
            trigger="読んだんだね",
            subject="user",
            category="user_past_fact",
            supporting_ids=(evidence.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(verified_user_facts=(evidence,))
        )

        assert resolved.supported

    def test_an_uncommitted_subject_leaves_the_category_in_charge(self) -> None:
        """A reviewer that declared no subject has contradicted nothing.

        True only where nothing is being asserted. A *committing* claim owes a
        subject — see `TestSubjectRequiredForCommittingClaims`.
        """
        evidence = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIなら本を読むかもしれない",
            trigger="わたしなら読むかも",
            category="yui_completed_action",
            modality="hypothetical",
            supporting_ids=(evidence.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(completed_activities_today=(evidence,))
        )

        assert resolved.supported
        assert not resolved.blocking

    def test_an_unknown_category_supports_nothing(self) -> None:
        evidence = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        candidate = SemanticClaimCandidate(
            proposition="なにか",
            trigger="なにか",
            subject="yui",
            category="yui_completed_action",
            supporting_ids=(evidence.evidence_id,),
        ).model_copy(update={"category": "a_category_nobody_defined"})

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(completed_activities_today=(evidence,))
        )

        assert not resolved.supported

    def test_there_is_one_ownership_table(self) -> None:
        """`CATEGORY_SUBJECT` and the matrix were two hand-maintained copies of
        the same rule, and only one of them was consulted."""
        from app.grounding.ownership import (
            CATEGORY_SUBJECT,
            OWNERSHIP,
            SUPPORTING_SUBJECTS,
        )

        assert CATEGORY_SUBJECT == {
            category: rule.claims_about for category, rule in OWNERSHIP.items()
        }
        assert SUPPORTING_SUBJECTS == {
            category: rule.subjects for category, rule in OWNERSHIP.items()
        }


class TestSpecificRecallAuthority:
    """BLOCKING 2. "It happened" does not mean "she is remembering it".

    `yui_specific_memory_recall` accepted ``objective_event`` and
    ``diary_entry``, so a recorded event supported 「覚えている」 with nothing
    recalled — which is exactly what `memory.current_recall_required` forbids.
    """

    def test_an_event_alone_does_not_support_remembering(self) -> None:
        event = Evidence(
            kind="objective_event", reference="e1", summary="海に行った", subject="yui"
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは海に行ったことを覚えている",
            trigger="覚えているよ",
            subject="yui",
            category="yui_specific_memory_recall",
            supporting_ids=(event.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(recent_objective_events=(event,))
        )

        assert not resolved.supported
        assert resolved.blocking

    def test_a_memory_recalled_this_turn_does(self) -> None:
        memory = Evidence(
            kind="subjective_memory",
            reference="m1",
            summary="海に行った",
            subject="yui",
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは海に行ったことを覚えている",
            trigger="覚えているよ",
            subject="yui",
            category="yui_specific_memory_recall",
            supporting_ids=(memory.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(recalled_subjective_memories=(memory,))
        )

        assert resolved.supported

    def test_a_stored_memory_nobody_retrieved_does_not(self) -> None:
        """The half the accepted-kind list cannot express: the row exists, and
        it is not one of *this turn's* recalls."""
        memory = Evidence(
            kind="subjective_memory",
            reference="m1",
            summary="海に行った",
            subject="yui",
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは海に行ったことを覚えている",
            trigger="覚えているよ",
            subject="yui",
            category="yui_specific_memory_recall",
            supporting_ids=(memory.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(known_semantic_memories=(memory,))
        )

        assert not resolved.supported
        assert any(
            reason.startswith("not_recalled_this_turn") for reason in resolved.refusals
        )

    def test_the_accepted_kinds_match_the_authority_fact(self) -> None:
        """`memory.current_recall_required` says a concrete recollection needs
        a current recall. The table has to say the same thing."""
        from app.grounding.models import ACCEPTED_EVIDENCE

        assert ACCEPTED_EVIDENCE["yui_specific_memory_recall"] == ("subjective_memory",)
        for kind in ("objective_event", "diary_entry", "semantic_memory"):
            assert kind not in ACCEPTED_EVIDENCE["yui_specific_memory_recall"]


class TestContradictionVerdict:
    """BLOCKING 3. A contradiction is a verdict, not a note.

    Supporting and contradicting evidence both present returned
    ``supported=True``: the contradiction was collected, displayed and ignored.
    """

    def test_a_contradiction_beats_supporting_evidence(self) -> None:
        good = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        bad = Evidence(
            kind="activity", reference="a2", summary="読んでいない", subject="yui"
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは本を読んだ",
            trigger="読んだよ",
            subject="yui",
            category="yui_completed_action",
            supporting_ids=(good.evidence_id,),
            contradicting_ids=(bad.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(completed_activities_today=(good, bad))
        )

        assert not resolved.supported
        assert resolved.blocking
        assert resolved.contradicted
        assert resolved.verdict == "contradicted"

    def test_the_three_verdicts_are_distinguishable(self) -> None:
        """"Nothing found" and "the opposite found" call for different repairs."""
        good = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        base = SemanticClaimCandidate(
            proposition="YUIは本を読んだ",
            trigger="読んだよ",
            subject="yui",
            category="yui_completed_action",
        )
        context = GroundingContext(completed_activities_today=(good,))

        supported = EvidenceResolver().resolve(
            base.model_copy(update={"supporting_ids": (good.evidence_id,)}), context
        )
        unsupported = EvidenceResolver().resolve(base, context)
        contradicted = EvidenceResolver().resolve(
            base.model_copy(
                update={
                    "supporting_ids": (good.evidence_id,),
                    "contradicting_ids": (good.evidence_id,),
                }
            ),
            context,
        )

        assert supported.verdict == "supported"
        assert unsupported.verdict == "unsupported"
        assert contradicted.verdict == "contradicted"

    def test_an_invented_contradiction_is_ignored(self) -> None:
        """Python owns whether a cited name refers — in both directions. A
        contradiction that does not exist must not block a good claim."""
        good = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは本を読んだ",
            trigger="読んだよ",
            subject="yui",
            category="yui_completed_action",
            supporting_ids=(good.evidence_id,),
            contradicting_ids=("activity:does_not_exist",),
        )

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(completed_activities_today=(good,))
        )

        assert resolved.supported
        assert not resolved.contradicted

    def test_a_contradiction_from_the_wrong_owner_is_ignored(self) -> None:
        """The USER disagreeing is not the record disagreeing."""
        good = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        theirs = Evidence(
            kind="objective_event",
            reference="e1",
            summary="読んでないでしょ",
            subject="user",
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは本を読んだ",
            trigger="読んだよ",
            subject="yui",
            category="yui_completed_action",
            supporting_ids=(good.evidence_id,),
            contradicting_ids=(theirs.evidence_id,),
        )

        resolved = EvidenceResolver().resolve(
            candidate,
            GroundingContext(
                completed_activities_today=(good,), recent_objective_events=(theirs,)
            ),
        )

        assert resolved.supported


class TestCorrectionInvalidId:
    """BLOCKING 5. An invented identifier retracts nothing.

    With one live claim, an invented id was replaced by the real one and *that*
    was retracted — a wrong answer converted into a confident wrong action.
    """

    async def test_an_invented_id_with_one_live_claim_retracts_nothing(
        self, prompt_registry
    ) -> None:
        resolved = await _resolver(
            {"claim_id": "cgc_invented", "denies": True}, prompt_registry
        ).resolve("ちがうよ", [_Claim("cgc_1", "YUIは本を読んだ")])

        assert not resolved.resolved
        assert resolved.claim_id == ""
        assert "unknown_claim" in resolved.refused

    async def test_a_valid_exact_id_is_acted_on(self, prompt_registry) -> None:
        resolved = await _resolver(
            {"claim_id": "cgc_1", "denies": True}, prompt_registry
        ).resolve("ちがうよ", [_Claim("cgc_1", "YUIは本を読んだ")])

        assert resolved.claim_id == "cgc_1"
        assert resolved.denies

    async def test_abstaining_with_one_live_claim_is_still_actionable(
        self, prompt_registry
    ) -> None:
        """Naming nothing is different from naming something that is not there.

        The USER denied something and there is exactly one thing it can be.
        """
        resolved = await _resolver({"claim_id": ""}, prompt_registry).resolve(
            "ちがうよ", [_Claim("cgc_1", "YUIは本を読んだ")]
        )

        assert resolved.claim_id == "cgc_1"
        assert resolved.refused == "no_target"

    async def test_abstaining_with_several_retracts_nothing(
        self, prompt_registry
    ) -> None:
        resolved = await _resolver({"claim_id": ""}, prompt_registry).resolve(
            "ちがうよ", [_Claim("cgc_1", "A"), _Claim("cgc_2", "B")]
        )

        assert not resolved.resolved

    async def test_an_invented_id_with_several_live_claims_retracts_nothing(
        self, prompt_registry
    ) -> None:
        resolved = await _resolver(
            {"claim_id": "cgc_nope"}, prompt_registry
        ).resolve("ちがうよ", [_Claim("cgc_1", "A"), _Claim("cgc_2", "B")])

        assert not resolved.resolved
        assert "unknown_claim" in resolved.refused


# =============================================================================
# Round 3: the retired memory alias is read-only
# =============================================================================


class TestLegacyMemoryCategoryIsolation:
    """`yui_memory_claim` may be deserialized. It may not be grounded.

    It predates the split into "she is recalling this particular thing" and
    "this is how memory works", and it accepted the union of both categories'
    evidence — including `objective_event` and `diary_entry`. So a reviewer that
    chose the alias got a *looser* rule than either category that replaced it,
    and 「去年の夏のこと覚えている」 could be settled by a row proving only that
    last summer happened.
    """

    def test_the_reviewer_schema_cannot_produce_it(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SemanticClaimCandidate(
                proposition="YUIは去年の夏のことを覚えている",
                trigger="覚えているよ",
                subject="yui",
                category="yui_memory_claim",
            )

    def test_the_resolver_refuses_it_even_if_the_schema_is_bypassed(self) -> None:
        """The second layer. A widened Literal, a row deserialized into a
        candidate, `model_construct` — none of them gets evidence."""
        memory = Evidence(
            kind="subjective_memory",
            reference="m1",
            summary="去年の夏の花火",
            subject="yui",
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは去年の夏のことを覚えている",
            trigger="覚えているよ",
            subject="yui",
            category="yui_specific_memory_recall",
            supporting_ids=(memory.evidence_id,),
        ).model_copy(update={"category": "yui_memory_claim"})

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(recalled_subjective_memories=(memory,))
        )

        assert not resolved.supported
        assert resolved.blocking
        assert resolved.refusals == ("legacy_category:yui_memory_claim",)

    def test_an_objective_event_cannot_reach_it_either(self) -> None:
        """The specific path the audit named: a YUI-owned objective event under
        the legacy memory category. Refused before the evidence is looked at,
        and refused again by the accepted kinds if it ever were."""
        from app.grounding.models import ACCEPTED_EVIDENCE

        event = Evidence(
            kind="objective_event",
            reference="e1",
            summary="去年の夏に花火を見た",
            subject="yui",
        )
        candidate = SemanticClaimCandidate(
            proposition="YUIは去年の夏のことを覚えている",
            trigger="覚えているよ",
            subject="yui",
            category="yui_specific_memory_recall",
            supporting_ids=(event.evidence_id,),
        ).model_copy(update={"category": "yui_memory_claim"})

        resolved = EvidenceResolver().resolve(
            candidate, GroundingContext(recent_objective_events=(event,))
        )

        assert not resolved.supported
        assert resolved.blocking
        assert "objective_event" not in ACCEPTED_EVIDENCE["yui_memory_claim"]
        assert "diary_entry" not in ACCEPTED_EVIDENCE["yui_memory_claim"]

    def test_the_two_runtime_memory_categories_are_the_only_ones(self) -> None:
        from app.dialogue.semantic_claims import RuntimeClaimKind
        from app.grounding.models import (
            CLAIM_KINDS,
            LEGACY_CLAIM_KINDS,
            RUNTIME_CLAIM_KINDS,
        )

        runtime = set(RuntimeClaimKind.__args__)
        assert runtime == set(RUNTIME_CLAIM_KINDS), "schema and table drifted"
        assert set(CLAIM_KINDS) - runtime == LEGACY_CLAIM_KINDS
        memory = {kind for kind in runtime if "memory" in kind}
        assert memory == {
            "yui_specific_memory_recall",
            "yui_general_memory_capability",
        }

    def test_specific_recall_takes_this_turns_memories_and_nothing_else(
        self,
    ) -> None:
        from app.grounding.models import ACCEPTED_EVIDENCE

        assert ACCEPTED_EVIDENCE["yui_specific_memory_recall"] == (
            "subjective_memory",
        )

        recalled = Evidence(
            kind="subjective_memory", reference="m1", summary="花火", subject="yui"
        )
        stored = Evidence(
            kind="subjective_memory", reference="m2", summary="海", subject="yui"
        )

        def resolve(evidence, context):
            return EvidenceResolver().resolve(
                SemanticClaimCandidate(
                    proposition="YUIは覚えている",
                    trigger="覚えているよ",
                    subject="yui",
                    category="yui_specific_memory_recall",
                    supporting_ids=(evidence.evidence_id,),
                ),
                context,
            )

        assert resolve(
            recalled, GroundingContext(recalled_subjective_memories=(recalled,))
        ).supported
        # Present in the context as a known semantic row, absent from this
        # turn's recall. Storage is not remembering.
        refused = resolve(stored, GroundingContext(known_semantic_memories=(stored,)))
        assert not refused.supported
        assert refused.blocking

    def test_general_capability_takes_authority_facts_and_nothing_else(self) -> None:
        from app.grounding.memory_semantics import AUTHORITATIVE_MEMORY_FACTS
        from app.grounding.models import ACCEPTED_EVIDENCE

        assert ACCEPTED_EVIDENCE["yui_general_memory_capability"] == (
            "memory_authority",
        )

        fact = AUTHORITATIVE_MEMORY_FACTS[0]
        memory = Evidence(
            kind="subjective_memory", reference="m1", summary="花火", subject="yui"
        )

        def resolve(evidence, context):
            return EvidenceResolver().resolve(
                SemanticClaimCandidate(
                    proposition="YUIにも忘れることがある",
                    trigger="忘れることもあるよ",
                    subject="yui",
                    category="yui_general_memory_capability",
                    supporting_ids=(evidence.evidence_id,),
                ),
                context,
            )

        assert resolve(
            fact, GroundingContext(memory_authority_facts=(fact,))
        ).supported
        assert not resolve(
            memory, GroundingContext(recalled_subjective_memories=(memory,))
        ).supported

    def test_a_pre_migration_row_still_reads(self) -> None:
        """The compatibility that is actually needed: an old Common Ground row
        deserializes, renders for the correction resolver, and keeps its kind.

        Reading is the whole requirement. Nothing about serving a stored row
        needs a *new* draft to be classifiable as the old category.
        """
        from app.grounding.models import Claim

        claim = Claim(kind="yui_memory_claim", text="去年の夏のこと", trigger="覚えてる")
        assert claim.kind == "yui_memory_claim"
        assert claim.accepted_evidence == ("subjective_memory",)

        rendered = render_claims([_LegacyRow()])
        assert "yui_memory_claim" in rendered
        assert "cg_legacy" in rendered


class _LegacyRow:
    """A Common Ground row written before the memory categories split."""

    claim_id = "cg_legacy"
    kind = "yui_memory_claim"
    statement = "去年の夏のことを覚えている"
    status = "live"
    subject = "yui"
    semantic_json = ""


# =============================================================================
# Round 3: a committing claim has to say whose life it is about
# =============================================================================


class TestSubjectRequiredForCommittingClaims:
    """`unknown` is not a neutral subject — it is the one that fits every
    category, so leaving it open lets the category decide alone."""

    @staticmethod
    def _yui_activity():
        return Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )

    def _resolve(self, subject, *, modality="assertion", category="yui_completed_action"):
        evidence = self._yui_activity()
        candidate = SemanticClaimCandidate(
            proposition="YUIは本を読んだ",
            trigger="読んだよ",
            subject="yui",
            category=category,
            modality=modality,
            supporting_ids=(evidence.evidence_id,),
        ).model_copy(update={"subject": subject})
        return EvidenceResolver().resolve(
            candidate, GroundingContext(completed_activities_today=(evidence,))
        )

    @pytest.mark.parametrize("modality", sorted(COMMITTING_MODALITIES))
    def test_unknown_blocks_every_committing_modality(self, modality) -> None:
        resolved = self._resolve("unknown", modality=modality)

        assert not resolved.supported
        assert resolved.blocking
        assert resolved.refusals == ("subject_required:yui_completed_action",)

    @pytest.mark.parametrize("modality", sorted(COMMITTING_MODALITIES))
    def test_an_empty_subject_blocks_too(self, modality) -> None:
        """The schema's default and a model that omitted the field arrive as
        different values; both are the same non-answer."""
        resolved = self._resolve("", modality=modality)

        assert not resolved.supported
        assert resolved.blocking
        assert resolved.refusals == ("subject_required:yui_completed_action",)

    def test_a_declared_subject_resolves_normally(self) -> None:
        resolved = self._resolve("yui")

        assert resolved.supported
        assert not resolved.blocking

    @pytest.mark.parametrize("modality", ["hypothetical", "intention", "question"])
    def test_a_non_committing_claim_may_leave_it_open(self, modality) -> None:
        """Nothing is being asserted about anyone, so there is no owner to get
        wrong. Blocking these would be the guard refusing to let her speculate."""
        resolved = self._resolve("unknown", modality=modality)

        assert not resolved.blocking

    def test_unknown_does_not_route_around_ownership(self) -> None:
        """The inversion the audit found, attempted through the open subject.

        USER-owned evidence under a claim about her life. Declaring `yui` is
        caught by the matrix; declaring nothing must not be a way past it.
        """
        evidence = Evidence(
            kind="objective_event",
            reference="e1",
            summary="詩を詠んだ",
            subject="user",
        )
        context = GroundingContext(recent_objective_events=(evidence,))

        for subject in ("unknown", "", "yui"):
            candidate = SemanticClaimCandidate(
                proposition="YUIも詩を詠んだ",
                trigger="わたしも詠んだよ",
                subject="yui",
                category="yui_completed_action",
                supporting_ids=(evidence.evidence_id,),
            ).model_copy(update={"subject": subject})

            resolved = EvidenceResolver().resolve(candidate, context)

            assert not resolved.supported, f"subject={subject!r} got through"
            assert resolved.blocking

    def test_the_mirror_inversion_is_closed_too(self) -> None:
        """And YUI-owned evidence under a claim about the USER."""
        evidence = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        context = GroundingContext(completed_activities_today=(evidence,))

        for subject in ("unknown", "", "user"):
            candidate = SemanticClaimCandidate(
                proposition="USERは本を読んだ",
                trigger="読んだんだよね",
                subject="user",
                category="user_past_fact",
                supporting_ids=(evidence.evidence_id,),
            ).model_copy(update={"subject": subject})

            resolved = EvidenceResolver().resolve(candidate, context)

            assert not resolved.supported, f"subject={subject!r} got through"
            assert resolved.blocking


# =============================================================================
# Round 3, finding 1: the verdict survives the adapter
# =============================================================================


def _supported_and_contradicted():
    """One claim with valid support and a valid authoritative contradiction."""
    support = Evidence(
        kind="activity", reference="a1", summary="図書館へ行った", subject="yui"
    )
    against = Evidence(
        kind="objective_event",
        reference="e1",
        summary="今日は一日家にいた",
        subject="yui",
    )
    context = GroundingContext(
        completed_activities_today=(support,), recent_objective_events=(against,)
    )
    candidate = SemanticClaimCandidate(
        proposition="YUIは今日図書館へ行った",
        trigger="図書館に行ったよ",
        subject="yui",
        category="yui_completed_action",
        supporting_ids=(support.evidence_id,),
        contradicting_ids=(against.evidence_id,),
    )
    return EvidenceResolver().resolve(candidate, context)


class TestVerdictReachesTheVerdict:
    """`GroundedClaim` used to recompute the answer from the evidence tuple.

    The resolver weighed positive evidence against an authoritative
    contradiction and concluded `contradicted`; the adapter then looked at the
    surviving positive evidence alone and reported `supported`. The claim went
    out because the last object to touch it re-decided a question it was not
    the authority for.
    """

    def test_the_resolver_says_contradicted(self) -> None:
        resolved = _supported_and_contradicted()

        assert resolved.evidence, "the support was not admitted; wrong fixture"
        assert resolved.contradictions
        assert resolved.verdict == "contradicted"
        assert not resolved.supported
        assert resolved.blocking

    def test_the_grounded_claim_does_not_reverse_it(self) -> None:
        grounded = _supported_and_contradicted().as_grounded()

        assert grounded.evidence, "positive evidence is present, as in the bug"
        assert grounded.verdict == "contradicted"
        assert grounded.contradicted
        assert not grounded.supported, (
            "the adapter recomputed `supported` from the evidence tuple"
        )

    def test_the_verdict_holds_the_send(self) -> None:
        from app.grounding.claims import GroundingVerdict

        verdict = GroundingVerdict(claims=(_supported_and_contradicted().as_grounded(),))

        assert not verdict.accepted
        assert verdict.blocking
        assert "矛盾" in verdict.describe(), verdict.describe()

    def test_a_supported_claim_still_passes(self) -> None:
        """The gate has to let the true case through."""
        from app.grounding.claims import GroundingVerdict

        support = Evidence(
            kind="activity", reference="a1", summary="図書館へ行った", subject="yui"
        )
        resolved = EvidenceResolver().resolve(
            SemanticClaimCandidate(
                proposition="YUIは今日図書館へ行った",
                trigger="図書館に行ったよ",
                subject="yui",
                category="yui_completed_action",
                supporting_ids=(support.evidence_id,),
            ),
            GroundingContext(completed_activities_today=(support,)),
        )

        assert resolved.verdict == "supported"
        assert GroundingVerdict(claims=(resolved.as_grounded(),)).accepted

    def test_blocking_crosses_the_boundary_in_both_directions(self) -> None:
        """Not only "contradicted must block" but "a hypothetical must not".

        `ResolvedClaim.blocking` is `needs_evidence and not supported`;
        `GroundingVerdict.blocking` is `not supported and severity == hard`.
        The two are the same question, so the claim carries the answer across
        as its severity instead of the boundary approximating it.
        """
        from app.grounding.claims import GroundingVerdict

        speculation = EvidenceResolver().resolve(
            SemanticClaimCandidate(
                proposition="YUIなら図書館が好きかもしれない",
                trigger="わたしなら好きかも",
                subject="yui",
                category="yui_completed_action",
                modality="hypothetical",
            ),
            GroundingContext(),
        )

        assert not speculation.supported
        assert not speculation.blocking
        assert speculation.as_claim().severity == "soft"
        assert GroundingVerdict(claims=(speculation.as_grounded(),)).accepted

        asserted = EvidenceResolver().resolve(
            SemanticClaimCandidate(
                proposition="YUIは今日図書館へ行った",
                trigger="図書館に行ったよ",
                subject="yui",
                category="yui_completed_action",
                modality="assertion",
            ),
            GroundingContext(),
        )

        assert asserted.blocking
        assert asserted.as_claim().severity == "hard"
        assert not GroundingVerdict(claims=(asserted.as_grounded(),)).accepted

    def test_the_legacy_path_keeps_its_own_reading(self) -> None:
        """A `GroundedClaim` nobody resolved has no verdict to preserve, so the
        evidence-derived reading still applies — that path has no better
        answer, and inventing one would be the same mistake pointed the other
        way."""
        from app.grounding.models import Claim, GroundedClaim

        claim = Claim(kind="yui_completed_action", text="読んだ", trigger="読んだよ")
        evidence = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )

        assert GroundedClaim(claim=claim, evidence=(evidence,)).supported
        assert not GroundedClaim(claim=claim).supported


# =============================================================================
# Round 3, finding 2: a contradiction needs the same standing as support
# =============================================================================


class TestContradictionAdmissibility:
    """Authority to overturn a claim cannot exceed authority to establish it.

    Contradicting identifiers were checked for ownership only, so a YUI-owned
    `tool_call` — a kind that cannot support 「本を読んだ」 at all — could
    refute it.
    """

    SUPPORT = Evidence(
        kind="activity", reference="a1", summary="本を読んだ", subject="yui"
    )

    def _resolve(self, contradiction, *, extra_section=None):
        sections = {"completed_activities_today": (self.SUPPORT,)}
        if extra_section:
            sections.update(extra_section)
        candidate = SemanticClaimCandidate(
            proposition="YUIは今日本を読んだ",
            trigger="読んだよ",
            subject="yui",
            category="yui_completed_action",
            supporting_ids=(self.SUPPORT.evidence_id,),
            contradicting_ids=(contradiction,),
        )
        return EvidenceResolver().resolve(candidate, GroundingContext(**sections))

    def test_a_valid_contradiction_still_wins(self) -> None:
        against = Evidence(
            kind="objective_event",
            reference="e1",
            summary="一日中寝ていた",
            subject="yui",
        )
        resolved = self._resolve(
            against.evidence_id,
            extra_section={"recent_objective_events": (against,)},
        )

        assert resolved.verdict == "contradicted"
        assert resolved.blocking

    def test_a_wrong_kind_contradiction_has_no_authority(self) -> None:
        """The audit's example. `tool_call` is not admissible evidence *for* a
        completed action, so it is not an opinion *against* one either."""
        against = Evidence(
            kind="tool_call", reference="t1", summary="検索した", subject="yui"
        )
        resolved = self._resolve(
            against.evidence_id, extra_section={"successful_tool_calls": (against,)}
        )

        assert resolved.contradictions == ()
        assert resolved.verdict == "supported"
        assert not resolved.blocking

    def test_an_invented_contradiction_has_no_authority(self) -> None:
        resolved = self._resolve("objective_event:does_not_exist")

        assert resolved.contradictions == ()
        assert resolved.supported

    def test_a_wrong_subject_contradiction_has_no_authority(self) -> None:
        """The USER's day is not a rebuttal of hers."""
        against = Evidence(
            kind="objective_event",
            reference="e1",
            summary="一日中寝ていた",
            subject="user",
        )
        resolved = self._resolve(
            against.evidence_id,
            extra_section={"recent_objective_events": (against,)},
        )

        assert resolved.contradictions == ()
        assert resolved.supported

    def test_a_wrong_relation_contradiction_has_no_authority(self) -> None:
        """It rained; that is not a rebuttal of what she did."""
        against = Evidence(
            kind="objective_event",
            reference="e1",
            summary="雨が降った",
            subject="world",
            relation="experiencer",
        )
        resolved = self._resolve(
            against.evidence_id,
            extra_section={"recent_objective_events": (against,)},
        )

        assert resolved.contradictions == ()
        assert resolved.supported

    def test_both_directions_run_the_same_function(self) -> None:
        """Structural: one admissibility test, not two that can drift."""
        import inspect

        from app.dialogue.semantic_claims import EvidenceResolver as Resolver

        source = inspect.getsource(Resolver.resolve)
        assert source.count("admit(") == 2, (
            "supporting and contradicting citations must both go through `admit`"
        )
        assert "may_support(" not in source, (
            "the ownership check was re-implemented outside `admit`"
        )
