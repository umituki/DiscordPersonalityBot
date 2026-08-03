"""Memory claims are resolved by semantic category and authoritative IDs."""

from __future__ import annotations

import pytest

from app.grounding.memory_semantics import (
    AUTHORITATIVE_MEMORY_FACTS,
    SemanticMemoryClaim,
    SemanticMemoryReview,
    resolve_semantic_memory_review,
)
from app.grounding.models import Evidence, GroundingContext

pytestmark = pytest.mark.invariant


def _context(*, recalled: tuple[Evidence, ...] = ()) -> GroundingContext:
    return GroundingContext(
        recalled_subjective_memories=recalled,
        memory_authority_facts=AUTHORITATIVE_MEMORY_FACTS,
    )


@pytest.mark.parametrize(
    "assertion",
    [
        "記録に残っていればいつでも思い出せる",
        "残っているものは忘れずに続いている",
        "保存された記憶なら大丈夫だ",
    ],
)
def test_paraphrases_share_one_general_capability_resolution(assertion: str) -> None:
    review = SemanticMemoryReview(
        claims=(
            SemanticMemoryClaim(
                category="general_memory_capability",
                assertion=assertion,
                contradicting_ids=("memory.dynamic_accessibility",),
            ),
        )
    )

    verdict = resolve_semantic_memory_review(review, _context())

    assert verdict.blocking
    assert verdict.reason_code == "unsupported_yui_memory_claim"


def test_true_general_capability_uses_authoritative_memory_fact() -> None:
    review = SemanticMemoryReview(
        claims=(
            SemanticMemoryClaim(
                category="general_memory_capability",
                assertion="保存されていても思い出せないことがある",
                supporting_ids=("memory.dynamic_accessibility",),
            ),
        )
    )

    assert resolve_semantic_memory_review(review, _context()).accepted


def test_specific_recall_requires_a_memory_recalled_on_this_turn() -> None:
    review = SemanticMemoryReview(
        claims=(
            SemanticMemoryClaim(
                category="specific_recall_affirmation",
                assertion="前に好きだと言った食べ物を覚えている",
            ),
        )
    )

    assert resolve_semantic_memory_review(review, _context()).blocking


def test_specific_recall_can_cite_only_a_current_recalled_memory() -> None:
    memory = Evidence(
        kind="subjective_memory",
        reference="mem_food",
        summary="USERは梨が好きだと話した",
        subject="yui",
    )
    review = SemanticMemoryReview(
        claims=(
            SemanticMemoryClaim(
                category="specific_recall_affirmation",
                assertion="梨が好きだと言っていたことを覚えている",
                supporting_ids=("mem_food",),
            ),
        )
    )

    assert resolve_semantic_memory_review(review, _context(recalled=(memory,))).accepted


def test_reviewer_cannot_manufacture_an_evidence_identifier() -> None:
    review = SemanticMemoryReview(
        claims=(
            SemanticMemoryClaim(
                category="specific_recall_affirmation",
                assertion="覚えている",
                supporting_ids=("mem_invented",),
            ),
        )
    )

    assert resolve_semantic_memory_review(review, _context()).blocking


def test_specific_no_recall_or_uncertainty_is_not_a_positive_claim() -> None:
    review = SemanticMemoryReview(claims=())

    assert resolve_semantic_memory_review(review, _context()).accepted
