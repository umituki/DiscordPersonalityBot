"""INVARIANT: when the USER says she is wrong, she checks rather than argues
(rebuild spec 11, CORR-001..CORR-003).

The shape of the failure: YUI infers something — 「詩を書いたんだね」 — the USER
answers 「詩？」, and the system reads that as curiosity about poetry. So she
elaborates. The inference is now twice-stated and, as far as the conversation is
concerned, established. Nothing ever asked whether it was true.

Three rules close it. Notice the pushback (CORR-001). With no evidence, take it
back instead of explaining (CORR-002) — a model asked to justify itself will
always find words, and that is exactly the failure. And once taken back, it is
out of the conversation for good (CORR-003).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.conversation.common_ground import (
    CommonGroundTracker,
    detect_correction,
)
from app.grounding.claims import ClaimExtractor, ClaimGroundingGuard
from app.grounding.models import Evidence, GroundingContext
from app.grounding.policy import GroundingPolicy
from app.storage.repositories.common_ground import CommonGroundRepository
from tests.unit.test_conversation import conversations  # noqa: F401 - shared fixture

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

CLAIMED = "今日は本を読んだよ。"
SUPPORTING = GroundingContext(
    completed_activities_today=(
        Evidence(
            kind="activity",
            reference="act_1",
            summary="本を読む",
            occurred_at=NOW,
            subject="yui",
        ),
    )
)


@pytest.fixture
def tracker(db, conversations):
    policy = GroundingPolicy.load(REPO_ROOT / "config" / "policies" / "grounding.yaml")
    extractor = ClaimExtractor(policy)
    return CommonGroundTracker(
        CommonGroundRepository(db),
        extractor=extractor,
        guard=ClaimGroundingGuard(extractor),
    )


@pytest.fixture
def conversation(conversations):
    return conversations.ensure_conversation(
        channel_id="chan_1", channel_type="direct_message", now=NOW
    )


# --- CORR-001: noticing ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["違うよ", "そんなこと言ってない", "書いてないよ", "え？", "詩？", "いつそんな話した？"],
)
def test_pushback_is_detected(text: str) -> None:
    assert detect_correction(text).detected, text


@pytest.mark.parametrize(
    "text",
    ["うん、そうだね", "今日はいい天気だね", "本は好き？ おすすめある？", "ありがとう"],
)
def test_ordinary_talk_is_not_pushback(text: str) -> None:
    assert not detect_correction(text).detected, text


def test_a_flat_denial_is_stronger_than_a_question() -> None:
    assert detect_correction("違うよ").strength == "denied"
    assert detect_correction("詩？").strength == "questioned"


def test_a_short_question_with_nothing_outstanding_is_just_a_question(
    tracker, conversation
) -> None:
    """CONV-002 cuts both ways. 「詩？」 is a challenge *when there is a claim to
    challenge*; with an empty common ground it is curiosity, and treating it as
    a retraction would be its own fabrication."""
    outcome = tracker.review_correction(
        "詩？",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )
    assert outcome.happened is False


def test_a_challenge_re_checks_yuis_own_claim(tracker, conversation) -> None:
    """CORR-001: USER が否定・疑問を示した場合、YUI は自分の直前 claim を再検証する."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )

    outcome = tracker.review_correction(
        "え？ 読んでないでしょ",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )
    assert outcome.happened
    assert outcome.claim is not None
    assert outcome.claim.statement == CLAIMED


# --- CORR-002: retract rather than explain -----------------------------------


def test_without_evidence_the_claim_is_retracted(tracker, conversation) -> None:
    """CORR-002: Evidence が無ければ、説明で押し切らず retraction 側へ倒す."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )

    outcome = tracker.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )
    assert outcome.retracted
    assert outcome.claim is not None
    assert outcome.claim.status == "retracted"
    assert "取り消して" in outcome.render()


def test_with_evidence_the_claim_stands_but_is_contested(tracker, conversation) -> None:
    """Backing down is not the same as having no spine. A claim the record
    supports is not retracted merely because the USER disagrees — it is marked
    contested, which is the honest state of the conversation."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=SUPPORTING,
        now=NOW,
    )

    outcome = tracker.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=SUPPORTING,
        now=NOW,
    )
    assert outcome.retracted is False
    assert outcome.claim is not None
    assert outcome.claim.status == "contested"


def test_an_unverifiable_claim_leans_to_retraction(tracker, conversation) -> None:
    """With no context to check against, CORR-002's direction decides."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=None,
        now=NOW,
    )

    outcome = tracker.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=None,
        now=NOW,
    )
    assert outcome.retracted


def test_the_correction_note_never_supplies_a_fact(tracker, conversation) -> None:
    """What the reply prompt is told is that the claim is unsupported — not a
    replacement fact, because the system does not have one."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )
    outcome = tracker.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )
    note = outcome.render()
    assert "裏づけがない" in note
    assert "取り消して" in note


# --- CORR-003: a retracted claim is gone -------------------------------------


def test_a_retracted_claim_leaves_the_common_ground(tracker, conversation) -> None:
    """CORR-003: 訂正後は Common Ground から誤 claim を除外し、
    後続 turn で再利用しない."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )
    assert tracker.live_claims(conversation.conversation_id)

    tracker.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )

    assert tracker.live_claims(conversation.conversation_id) == ()
    assert CLAIMED not in tracker.render(conversation.conversation_id)


def test_a_retraction_is_kept_as_history_not_as_common_ground(
    tracker, conversation, db
) -> None:
    """Removed from the conversation, not deleted from the record: 「なぜ取り消し
    たのか」 has to have an answer."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )
    tracker.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )

    repository = CommonGroundRepository(db)
    everything = repository.all_for(conversation.conversation_id)
    assert len(everything) == 1
    assert everything[0].status == "retracted"
    assert everything[0].resolved_reason.startswith("unsupported_after_challenge")


def test_a_second_challenge_does_not_re_retract_the_same_claim(
    tracker, conversation
) -> None:
    """Once it is out, it is out — it cannot come back to be retracted again."""
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )
    tracker.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )

    again = tracker.review_correction(
        "違うって",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )
    assert again.happened is False


# --- what enters the common ground at all ------------------------------------


def test_only_a_claim_enters_the_common_ground(tracker, conversation) -> None:
    recorded = tracker.record_reply(
        "うん、そうだね。",
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )
    assert recorded == ()
    assert tracker.live_claims(conversation.conversation_id) == ()


def test_a_supported_claim_is_recorded_as_supported(tracker, conversation) -> None:
    recorded = tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=SUPPORTING,
        now=NOW,
    )
    assert recorded[0].status == "supported"
    assert recorded[0].confidence == "high"


def test_an_unsupported_claim_is_recorded_as_provisional(tracker, conversation) -> None:
    """It was said, so it is in the conversation — but it is not established,
    and the reply prompt is shown that difference."""
    recorded = tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )
    assert recorded[0].status == "provisional"
    assert "status: provisional" in tracker.render(conversation.conversation_id)


def test_claims_survive_a_restart(tracker, conversation, db) -> None:
    """A claim the USER heard before a restart is still a claim she made.

    Keeping this in memory would mean 「違うよ」 after a restart lands on nothing
    and CORR-001 silently does nothing at all.
    """
    tracker.record_reply(
        CLAIMED,
        conversation_id=conversation.conversation_id,
        event_id="evt_1",
        context=GroundingContext(),
        now=NOW,
    )

    policy = GroundingPolicy.load(REPO_ROOT / "config" / "policies" / "grounding.yaml")
    extractor = ClaimExtractor(policy)
    reopened = CommonGroundTracker(
        CommonGroundRepository(db),
        extractor=extractor,
        guard=ClaimGroundingGuard(extractor),
    )

    outcome = reopened.review_correction(
        "違うよ",
        conversation_id=conversation.conversation_id,
        context=GroundingContext(),
        now=NOW,
    )
    assert outcome.retracted
