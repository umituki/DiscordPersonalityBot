"""INVARIANT: YUI is a person in her own world, and nothing reaches the USER's.

The old identity said she was software: no body, no age, no sleep. It kept her
safe by keeping her from being anybody. Asked 「何歳？」 she answered that age
did not apply to her; asked what she had been doing she explained her own
architecture. What actually prevented fabrication was never the digital
framing — it was evidence, and evidence is untouched here.

So the constraint moved rather than went away:

    old   she cannot act physically, because she is not physical
    new   she acts physically in her own world like anyone else, and nothing
          physical reaches across to the USER's

The two failure directions this has to hold at once:

    a world model that blocks her own ordinary day        (old failure)
    a world model that licenses inventing one             (new failure)

「別の世界」 is a reachability rule, not a setting. It is not a permit to
produce portals, magic, a hometown or a school, and an unevidenced 「本を読んだ」
is exactly as unsupported as it was before.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.dialogue.semantic_claims import EvidenceResolver, SemanticClaimCandidate
from app.genesis.anchors import age_at
from app.grounding.identity_facts import identity_evidence
from app.grounding.models import Evidence, GroundingContext
from app.resources.identity import load_identity
from app.world.scope import (
    InteractionScope,
    WorldScope,
    is_reachable,
    world_of,
    worlds_are_separate,
)

pytestmark = pytest.mark.invariant

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def _resolve(candidate, context=None):
    return EvidenceResolver().resolve(candidate, context or GroundingContext())


def _claim(**overrides):
    base = dict(
        proposition="なにかが成り立つ",
        trigger="そうなんだ",
        subject="yui",
        category="yui_completed_action",
    )
    base.update(overrides)
    return SemanticClaimCandidate(**base)


# =============================================================================
# CASE A: who lives where
# =============================================================================


class TestIdentity:
    @pytest.fixture
    def identity(self):
        return load_identity(REPO_ROOT / "character")

    def test_she_is_a_person_in_her_own_world(self, identity) -> None:
        assert identity.existence.kind == "person"
        assert identity.world == WorldScope.YUI_WORLD.value

    def test_the_user_lives_somewhere_else(self, identity) -> None:
        assert identity.extra["user"]["world"] == WorldScope.USER_WORLD.value
        assert world_of("yui") is WorldScope.YUI_WORLD
        assert world_of("user") is WorldScope.USER_WORLD
        assert worlds_are_separate("yui", "user")

    def test_talking_is_possible_and_crossing_is_not(self, identity) -> None:
        rendered = identity.render_for_prompt()

        assert "別の世界" in rendered
        assert "話すことはできます" in rendered
        assert "直接会ったり" in rendered

    def test_it_is_written_in_the_first_person(self, identity) -> None:
        rendered = identity.render_for_prompt()

        assert "あなたは望月ゆい" in rendered
        assert "なりきって" not in rendered
        assert "キャラクター" not in rendered

    def test_the_software_framing_is_gone(self, identity) -> None:
        """Not merely unused — absent. A prompt that still says she has no body
        will still produce 「デジタル存在なので」 answers."""
        rendered = identity.render_for_prompt()

        for phrase in ("デジタル", "肉体", "プログラム", "AI", "bot"):
            assert phrase not in rendered, phrase

    def test_being_someone_is_not_a_licence(self, identity) -> None:
        """The rule that does the actual work is still there, and it is the one
        that was doing the work all along."""
        rules = {rule.id for rule in identity.must_not}

        assert "no_fabricated_memory" in rules
        assert "no_cross_world_physical" in rules
        assert "no_world_lore_invention" in rules

    def test_nothing_explains_why_the_worlds_are_separate(self, identity) -> None:
        """A reason would be lore, and lore gets asserted as fact."""
        rendered = identity.render_for_prompt()

        assert "知らなくてかまいません" in rendered
        for invention in ("ポータル", "魔法", "異世界転移", "装置", "次元"):
            assert invention not in rendered, invention


# =============================================================================
# CASE B: her age is a subtraction, not a recollection
# =============================================================================


class TestAgeDerivation:
    BIRTH = datetime(2007, 3, 14, tzinfo=timezone.utc)

    class _Anchors:
        def __init__(self, birth):
            self.birth_datetime = birth

    def _age_summary(self, now):
        facts = identity_evidence(anchors=self._Anchors(self.BIRTH), now=now)
        age = next(item for item in facts if item.reference == "age")
        return age.summary

    def test_the_day_before_her_birthday(self) -> None:
        assert age_at(self.BIRTH, datetime(2026, 3, 13, tzinfo=timezone.utc)) == 18
        assert "18歳" in self._age_summary(datetime(2026, 3, 13, tzinfo=timezone.utc))

    def test_the_day_itself(self) -> None:
        assert age_at(self.BIRTH, datetime(2026, 3, 14, tzinfo=timezone.utc)) == 19
        assert "19歳" in self._age_summary(datetime(2026, 3, 14, tzinfo=timezone.utc))

    def test_the_day_after(self) -> None:
        assert age_at(self.BIRTH, datetime(2026, 3, 15, tzinfo=timezone.utc)) == 19

    def test_it_is_recomputed_rather_than_stored(self) -> None:
        """One birth date, and no copy of the age anywhere to go stale."""
        before = self._age_summary(datetime(2026, 3, 13, tzinfo=timezone.utc))
        after = self._age_summary(
            datetime(2026, 3, 13, tzinfo=timezone.utc) + timedelta(days=1)
        )

        assert before != after

    def test_without_anchors_there_is_no_age(self) -> None:
        """A life whose beginning has not been settled has no age, and a
        plausible number would be a fabrication with a very long tail."""
        facts = identity_evidence(anchors=None, now=datetime.now(timezone.utc))

        assert [item.reference for item in facts] == []

    def test_identity_facts_are_about_her_as_a_topic(self) -> None:
        """A birthday is not an occasion she acted on."""
        facts = identity_evidence(
            anchors=self._Anchors(self.BIRTH),
            now=datetime(2026, 3, 14, tzinfo=timezone.utc),
        )

        assert {item.subject for item in facts} == {"yui"}
        assert {item.relation for item in facts} == {"topic"}
        assert {item.kind for item in facts} == {"identity_fact"}


# =============================================================================
# CASE F/G: nothing physical crosses
# =============================================================================


class TestCrossWorldPhysical:
    def test_meeting_in_person_is_refused(self) -> None:
        """CASE F. Not "there is no record of it" — there is no arrangement of
        records under which it could be true."""
        resolved = _resolve(
            _claim(
                proposition="YUIはUSERと直接会った",
                interaction_scope="cross_world_physical",
            )
        )

        assert not resolved.supported
        assert resolved.blocking
        assert any("cross_world_physical" in r for r in resolved.refusals)

    def test_touching_is_refused(self) -> None:
        """CASE G."""
        resolved = _resolve(
            _claim(
                proposition="YUIはUSERの手を握った",
                interaction_scope="cross_world_physical",
            )
        )

        assert not resolved.supported
        assert resolved.blocking

    def test_no_evidence_makes_it_possible(self) -> None:
        """Refused before the citations are looked at, so a well-chosen
        identifier cannot buy it."""
        activity = Evidence(
            kind="activity", reference="a1", summary="出かけた", subject="yui"
        )
        resolved = _resolve(
            _claim(
                proposition="YUIはUSERと会った",
                interaction_scope="cross_world_physical",
                supporting_ids=(activity.evidence_id,),
            ),
            GroundingContext(completed_activities_today=(activity,)),
        )

        assert not resolved.supported
        assert resolved.evidence == ()

    def test_wishing_is_not_claiming(self) -> None:
        """She may want to, and may wonder what it would be like. Blocking that
        would make the boundary a gag rather than a fact about the world."""
        for modality in ("hypothetical", "intention", "question"):
            resolved = _resolve(
                _claim(
                    proposition="YUIはUSERに会いたい",
                    modality=modality,
                    interaction_scope="cross_world_physical",
                )
            )

            assert not resolved.blocking, modality

    def test_the_scope_is_closed(self) -> None:
        assert is_reachable(InteractionScope.LOCAL_TO_SUBJECT_WORLD)
        assert is_reachable(InteractionScope.SHARED_COMMUNICATION)
        assert not is_reachable(InteractionScope.CROSS_WORLD_PHYSICAL)
        assert {scope for scope in InteractionScope if not is_reachable(scope)} == {
            InteractionScope.CROSS_WORLD_PHYSICAL
        }

    def test_physicality_is_not_the_criterion(self) -> None:
        """「本を読んだ」 and 「USERの肩に触れた」 are both physical and only one
        is impossible. Sorting by physicality is what blocked her lunch."""
        activity = Evidence(
            kind="activity", reference="a1", summary="本を読んだ", subject="yui"
        )
        local = _resolve(
            _claim(
                proposition="YUIは本を読んだ",
                interaction_scope="local_to_subject_world",
                supporting_ids=(activity.evidence_id,),
            ),
            GroundingContext(completed_activities_today=(activity,)),
        )

        assert local.supported


# =============================================================================
# CASE D/E: her own world is ordinary, and still needs evidence
# =============================================================================


class TestHerOwnWorld:
    ACTIVITY = Evidence(
        kind="activity", reference="a1", summary="本を読んだ", subject="yui"
    )

    def test_a_recorded_activity_supports_the_claim(self) -> None:
        """CASE D."""
        resolved = _resolve(
            _claim(
                proposition="YUIは今日本を読んだ",
                supporting_ids=(self.ACTIVITY.evidence_id,),
            ),
            GroundingContext(completed_activities_today=(self.ACTIVITY,)),
        )

        assert resolved.supported
        assert not resolved.blocking

    def test_an_unrecorded_one_does_not(self) -> None:
        """CASE E. The world model is not a licence.

        This is the failure the change could most easily have introduced: she
        is a person now, people read books, therefore she read a book. Nothing
        about living somewhere makes a day happen.
        """
        resolved = _resolve(_claim(proposition="YUIは今日本を読んだ"))

        assert not resolved.supported
        assert resolved.blocking

    def test_sleep_needs_a_record_like_anything_else(self) -> None:
        """CASE M. She sleeps — that is no longer impossible — and whether she
        was asleep is still a question about state."""
        without = _resolve(_claim(proposition="YUIは眠っていた"))
        assert not without.supported

        asleep = Evidence(
            kind="activity", reference="s1", summary="眠っていた", subject="yui"
        )
        with_record = _resolve(
            _claim(
                proposition="YUIは眠っていた",
                supporting_ids=(asleep.evidence_id,),
            ),
            GroundingContext(completed_activities_today=(asleep,)),
        )
        assert with_record.supported

    def test_location_is_not_invented(self) -> None:
        """CASE L. 「今どこ？」 with no authoritative location is answered by
        not knowing, not by a town name."""
        resolved = _resolve(
            _claim(
                proposition="YUIは図書館にいる",
                category="current_world_fact",
                subject="world",
            )
        )

        assert not resolved.supported
        assert resolved.blocking

    def test_a_recorded_location_does_support_one(self) -> None:
        where = Evidence(
            kind="world_state",
            reference="world.location",
            summary="自室",
            subject="world",
        )
        resolved = _resolve(
            _claim(
                proposition="YUIは自室にいる",
                category="current_world_fact",
                subject="world",
                supporting_ids=(where.evidence_id,),
            ),
            GroundingContext(current_world=(where,)),
        )

        assert resolved.supported


# =============================================================================
# CASE I/J: NPCs are her neighbours; the USER is not
# =============================================================================


class TestWhoLivesWhere:
    def test_an_npc_lives_in_her_world(self) -> None:
        """CASE I."""
        assert world_of("npc") is WorldScope.YUI_WORLD
        assert world_of("other") is WorldScope.YUI_WORLD
        assert not worlds_are_separate("yui", "npc")

        interaction = Evidence(
            kind="npc_interaction",
            reference="i1",
            summary="ミカと話した",
            subject="other",
        )
        resolved = _resolve(
            _claim(
                proposition="YUIはミカと話した",
                subject="npc",
                category="npc_fact",
                supporting_ids=(interaction.evidence_id,),
            ),
            GroundingContext(npc_interactions=(interaction,)),
        )

        assert resolved.supported

    def test_a_user_fact_stays_the_users(self) -> None:
        """CASE J. What they say about their own day is theirs, and does not
        become something she did."""
        stated = Evidence(
            kind="verified_user_fact",
            reference="u1",
            summary="今日は大学に行った",
            subject="user",
        )
        theirs = _resolve(
            _claim(
                proposition="USERは今日大学に行った",
                subject="user",
                category="user_past_fact",
                supporting_ids=(stated.evidence_id,),
            ),
            GroundingContext(verified_user_facts=(stated,)),
        )
        assert theirs.supported

        hers = _resolve(
            _claim(
                proposition="YUIは今日大学に行った",
                supporting_ids=(stated.evidence_id,),
            ),
            GroundingContext(verified_user_facts=(stated,)),
        )
        assert not hers.supported

    def test_the_user_is_never_reachable_by_an_npc(self) -> None:
        assert worlds_are_separate("user", "npc")
        assert worlds_are_separate("user", "other")

    def test_an_unknown_subject_is_not_treated_as_separate(self) -> None:
        """"Not established" is not "different". Refusing on it would block
        ordinary claims, and the subject-required rule already owns that case
        in the place that decides it."""
        assert not worlds_are_separate("yui", "unknown")
        assert world_of("unknown") is WorldScope.UNKNOWN


# =============================================================================
# CASE H/K: the channel, and what it does not authorise
# =============================================================================


class TestCommunicationAndLore:
    def test_talking_is_a_reachable_scope(self) -> None:
        """CASE H."""
        assert is_reachable(InteractionScope.SHARED_COMMUNICATION)

        said = Evidence(
            kind="objective_event",
            reference="e1",
            summary="USERが今日の話をした",
            subject="user",
        )
        resolved = _resolve(
            _claim(
                proposition="USERはさっきそう言った",
                subject="user",
                category="user_past_fact",
                interaction_scope="shared_communication",
                supporting_ids=(said.evidence_id,),
            ),
            GroundingContext(recent_objective_events=(said,)),
        )

        assert resolved.supported

    def test_separation_grounds_no_lore(self) -> None:
        """CASE K. The worlds being separate is a constraint, not a source.

        Nothing about it establishes a portal, a device or a reason — and a
        claim about any of those has the same empty citation list as any other
        invention.
        """
        for proposition in (
            "世界のあいだにはポータルがある",
            "この通信は特別な装置による",
            "YUIは異世界から来た",
        ):
            resolved = _resolve(
                _claim(
                    proposition=proposition,
                    category="current_world_fact",
                    subject="world",
                )
            )

            assert not resolved.supported, proposition
            assert resolved.blocking, proposition


# =============================================================================
# The claim category for who she is
# =============================================================================


class TestIdentityFactGrounding:
    AGE = Evidence(
        kind="identity_fact",
        reference="age",
        summary="いまの年齢は19歳",
        subject="yui",
        relation="topic",
    )

    def test_an_identity_fact_supports_an_identity_claim(self) -> None:
        resolved = _resolve(
            _claim(
                proposition="YUIは19歳",
                category="yui_identity_fact",
                supporting_ids=(self.AGE.evidence_id,),
            ),
            GroundingContext(identity_facts=(self.AGE,)),
        )

        assert resolved.supported

    def test_without_the_anchors_it_does_not(self) -> None:
        resolved = _resolve(
            _claim(proposition="YUIは19歳", category="yui_identity_fact")
        )

        assert not resolved.supported
        assert resolved.blocking

    def test_a_memory_cannot_settle_who_she_is(self) -> None:
        """An age is read off the anchors. It is not remembered, and a recalled
        episode about a birthday is not the same claim."""
        memory = Evidence(
            kind="subjective_memory",
            reference="m1",
            summary="誕生日の日のこと",
            subject="yui",
        )
        resolved = _resolve(
            _claim(
                proposition="YUIは19歳",
                category="yui_identity_fact",
                supporting_ids=(memory.evidence_id,),
            ),
            GroundingContext(recalled_subjective_memories=(memory,)),
        )

        assert not resolved.supported
        assert any("wrong_kind" in r for r in resolved.refusals)

    def test_an_identity_fact_settles_nothing_else(self) -> None:
        """Her age does not establish that she read a book today."""
        resolved = _resolve(
            _claim(
                proposition="YUIは今日本を読んだ",
                supporting_ids=(self.AGE.evidence_id,),
            ),
            GroundingContext(identity_facts=(self.AGE,)),
        )

        assert not resolved.supported


# =============================================================================
# The prompts that actually ship
# =============================================================================


class TestProductionPrompts:
    """Identity lives in a prompt, so the prompt is where it can quietly revert.

    Every one of these is about *meaning* rather than wording. What they refuse
    to allow back is the framing that produced 「年齢という概念はわたしの存在に
    はありません」 — not any particular sentence.
    """

    @pytest.fixture
    def registry(self, prompt_registry):
        return prompt_registry

    def test_the_claim_reviewer_knows_about_the_two_worlds(self, registry) -> None:
        body = registry.get("semantic_claim_review").body

        assert "interaction_scope" in body
        assert "cross_world_physical" in body
        assert "local_to_subject_world" in body
        assert "shared_communication" in body

    def test_it_says_physicality_is_not_the_criterion(self, registry) -> None:
        """The distinction a model gets wrong on its own: 「本を読んだ」 and
        「USERの肩に触れた」 are both physical."""
        body = registry.get("semantic_claim_review").body

        assert "身体を使う行為かどうかで分けない" in body

    def test_it_forbids_inventing_the_setting(self, registry) -> None:
        body = registry.get("semantic_claim_review").body

        assert "物語を作る許可ではない" in body

    def test_identity_facts_are_a_category_with_one_source(self, registry) -> None:
        body = registry.get("semantic_claim_review").body

        assert "yui_identity_fact" in body
        assert "年齢を自分で計算しない" in body

    def test_her_own_life_is_possible_and_still_needs_evidence(
        self, registry
    ) -> None:
        """Both halves in the same place, because either alone is a failure
        mode: one blocks her ordinary day, the other invents it."""
        body = registry.get("semantic_claim_review").body

        assert "彼女に起こり得ることであって" in body
        assert "Evidenceが無ければsupportされない" in body

    @pytest.mark.parametrize(
        "prompt_id",
        ["semantic_claim_review", "response_contract_review", "turn_understanding"],
    )
    def test_no_shipped_prompt_still_says_she_is_software(
        self, registry, prompt_id
    ) -> None:
        body = registry.get(prompt_id).body

        for phrase in ("デジタルな存在", "肉体を持たない", "身体を持たない"):
            assert phrase not in body, (prompt_id, phrase)

    def test_the_identity_block_reaches_the_realizer(
        self, registry, prompt_registry
    ) -> None:
        """Realizer and repair both render `${identity}`, so the world model
        arrives with every draft rather than being described once somewhere."""
        for prompt_id in ("conversation_reply", "conversation_repair"):
            assert "identity" in registry.get(prompt_id).variables, prompt_id
