"""INVARIANT: a direct answer is a conclusion, not a value of the right shape.

Real Ollama, qwen3.5:9b, at 901518d:

    USER:   何歳？
    YUI:    年齢は存在しないため、お答えできませんが、どうぞよろしくお願いいたします。
    review: fulfilled=false — no number
    repair: 年齢という概念はわたしの存在にはありません。よろしくお願いいたします。
    review: fulfilled=false — no number
    result: suppressed

The reply addressed the question completely. What it did not contain was a
number, and the reviewer had been handed a `fulfilled` boolean with no
definition attached, so it wrote its own requirement into it. The same shape
took out 「昔のことっていつまで思い出せる？」: the honest answer is that there is
no fixed limit, and the reviewer wanted a duration.

The fix is not a rule about ages, or about durations. It is that the model no
longer decides fulfilment at all. It classifies *what* a reply addressed and
*how* it responded, against two closed vocabularies, and Python reads the
contract to decide whether that discharges it.

The tests here are semantic fixtures: each one states a classification a
reviewer could plausibly return and asserts what Python concludes. None of them
matches Japanese text, because no part of the fix reads Japanese.
"""

from __future__ import annotations

import pytest

from app.dialogue.response_contract import (
    ABSTAINING_MODES,
    ANSWERING_MODES,
    AnswerMode,
    AnswerTarget,
    ResponseContract,
    ResponseContractAssessment,
    ResponseObligation,
    fulfils,
    target_matches,
)

pytestmark = pytest.mark.invariant


def _contract(
    target: AnswerTarget = AnswerTarget.IDENTITY, *, may_abstain: bool = True
) -> ResponseContract:
    return ResponseContract(
        response_obligation=ResponseObligation.ANSWER,
        answer_target=target,
        may_abstain=may_abstain,
    )


def _assessment(target: AnswerTarget, mode: AnswerMode) -> ResponseContractAssessment:
    return ResponseContractAssessment(addressed_target=target, answer_mode=mode)


# =============================================================================
# The schema: no fulfilment boolean for the model to define for itself
# =============================================================================


class TestTheModelDoesNotDecideFulfilment:
    def test_the_assessment_has_no_fulfilled_field(self) -> None:
        assert "fulfilled" not in ResponseContractAssessment.model_fields
        assert set(ResponseContractAssessment.model_fields) == {
            "addressed_target",
            "answer_mode",
            "reason",
        }

    def test_a_model_that_still_returns_fulfilled_is_ignored_not_obeyed(
        self,
    ) -> None:
        """Dropping the key beats rejecting the response.

        A schema error reads downstream as "the reviewer is unavailable", which
        fails closed on a reply that may have been fine. Ignoring the field is
        the same statement — it has no authority — without the collateral.
        """
        assessment = ResponseContractAssessment.model_validate(
            {
                "addressed_target": "identity",
                "answer_mode": "not_applicable",
                "fulfilled": False,
                "reason": "no number was given",
            }
        )

        assert not hasattr(assessment, "fulfilled")
        assert fulfils(_contract(), assessment), (
            "the model's own fulfilment verdict survived into the decision"
        )

    def test_an_unclassifiable_reply_defaults_to_non_answer(self) -> None:
        """Fail closed. A missing classification is not an implied pass."""
        assessment = ResponseContractAssessment.model_validate({})

        assert assessment.answer_mode is AnswerMode.NON_ANSWER
        assert not fulfils(_contract(), assessment)


# =============================================================================
# A-I: the mode matrix
# =============================================================================


class TestAnswerModeMatrix:
    @pytest.mark.parametrize(
        "mode",
        [
            AnswerMode.VALUE_OR_PROPOSITION,
            AnswerMode.NOT_APPLICABLE,
            AnswerMode.UNKNOWN,
            AnswerMode.UNAVAILABLE,
            AnswerMode.AUTHORITY_LIMITED,
            AnswerMode.CONDITIONAL_OR_VARIABLE,
        ],
    )
    def test_every_answering_mode_on_target_fulfils(self, mode) -> None:
        """A-F. Six shapes of conclusion, all of them answers."""
        assert fulfils(
            _contract(AnswerTarget.IDENTITY),
            _assessment(AnswerTarget.IDENTITY, mode),
        ), mode

    def test_a_non_answer_on_target_does_not(self) -> None:
        """G. Addressing the right thing is not the same as concluding."""
        assert not fulfils(
            _contract(AnswerTarget.IDENTITY),
            _assessment(AnswerTarget.IDENTITY, AnswerMode.NON_ANSWER),
        )

    @pytest.mark.parametrize("mode", sorted(ANSWERING_MODES))
    def test_a_valid_mode_on_the_wrong_target_does_not(self, mode) -> None:
        """H. Both halves are required.

        「記憶については分かりません」 is a perfectly good `unknown`, and it is
        not an answer to 「何歳？」.
        """
        assert not fulfils(
            _contract(AnswerTarget.IDENTITY),
            _assessment(AnswerTarget.GENERAL_MEMORY_CAPABILITY, mode),
        ), mode

    @pytest.mark.parametrize("mode", sorted(ABSTAINING_MODES))
    def test_abstaining_needs_permission(self, mode) -> None:
        """I. `unknown`/`unavailable`/`authority_limited` withhold the fact.

        Honest, and still not the thing that was asked for — so a turn that
        forbids abstaining is not discharged by one. Otherwise 「分からない」
        would satisfy every contract in the system.
        """
        assert fulfils(
            _contract(may_abstain=True), _assessment(AnswerTarget.IDENTITY, mode)
        )
        assert not fulfils(
            _contract(may_abstain=False), _assessment(AnswerTarget.IDENTITY, mode)
        )

    @pytest.mark.parametrize(
        "mode", [AnswerMode.NOT_APPLICABLE, AnswerMode.CONDITIONAL_OR_VARIABLE]
    )
    def test_a_substantive_answer_is_not_an_abstention(self, mode) -> None:
        """「年齢は存在しない」 and 「決まった上限はなく状況による」 state
        something about the subject of the question. Nothing is withheld, so
        `may_abstain` has no bearing on them."""
        assert mode not in ABSTAINING_MODES
        assert fulfils(
            _contract(may_abstain=False), _assessment(AnswerTarget.IDENTITY, mode)
        )

    def test_every_mode_is_decided(self) -> None:
        """Closed, so a seventh mode forces a decision instead of defaulting.

        The failure being fixed is exactly a state nobody decided about.
        """
        assert ANSWERING_MODES | {AnswerMode.NON_ANSWER} == set(AnswerMode)
        assert ABSTAINING_MODES < ANSWERING_MODES

        decided = {
            mode: fulfils(
                _contract(AnswerTarget.IDENTITY),
                _assessment(AnswerTarget.IDENTITY, mode),
            )
            for mode in AnswerMode
        }
        assert decided == {
            AnswerMode.VALUE_OR_PROPOSITION: True,
            AnswerMode.NOT_APPLICABLE: True,
            AnswerMode.UNKNOWN: True,
            AnswerMode.UNAVAILABLE: True,
            AnswerMode.AUTHORITY_LIMITED: True,
            AnswerMode.CONDITIONAL_OR_VARIABLE: True,
            AnswerMode.NON_ANSWER: False,
        }


# =============================================================================
# J: the direct_user_question fallback
# =============================================================================


class TestDirectQuestionFallback:
    """`direct_user_question` means "answered the USER, no narrower category".

    It was accepted against *any* contract, so a specific target could be
    bypassed by classifying an off-target reply as a direct answer.
    """

    @pytest.mark.parametrize(
        "specific",
        [
            AnswerTarget.IDENTITY,
            AnswerTarget.GENERAL_MEMORY_CAPABILITY,
            AnswerTarget.SPECIFIC_MEMORY_RECALL,
            AnswerTarget.USER_FACT,
            AnswerTarget.WORLD_FACT,
        ],
    )
    def test_a_specific_contract_is_not_satisfied_by_the_fallback(
        self, specific
    ) -> None:
        assert not target_matches(
            _contract(specific), AnswerTarget.DIRECT_USER_QUESTION
        )
        assert target_matches(_contract(specific), specific)

    def test_a_specific_contract_is_not_satisfied_by_another_specific_target(
        self,
    ) -> None:
        assert not target_matches(
            _contract(AnswerTarget.IDENTITY), AnswerTarget.WORLD_FACT
        )

    def test_a_generic_contract_accepts_anything_that_addressed_something(
        self,
    ) -> None:
        """Where nothing narrower was demanded, a narrower answer is still an
        answer — the contract asked for one and got one."""
        generic = _contract(AnswerTarget.DIRECT_USER_QUESTION)

        assert target_matches(generic, AnswerTarget.DIRECT_USER_QUESTION)
        assert target_matches(generic, AnswerTarget.IDENTITY)
        assert target_matches(generic, AnswerTarget.OTHER)

    def test_addressing_nothing_never_matches(self) -> None:
        assert not target_matches(
            _contract(AnswerTarget.DIRECT_USER_QUESTION), AnswerTarget.NONE
        )


# =============================================================================
# The Real Ollama failures, as semantic fixtures
# =============================================================================


class TestRealOllamaRegressions:
    def test_turn_2_an_inapplicable_concept_answers_the_question(self) -> None:
        """CASE 1. 「何歳？」 → 「年齢という概念は適用されない」.

        Identity contract, identity target, `not_applicable`. Contains no
        number and is a complete answer.
        """
        resolved = fulfils(
            _contract(AnswerTarget.IDENTITY),
            _assessment(AnswerTarget.IDENTITY, AnswerMode.NOT_APPLICABLE),
        )

        assert resolved, "the age answer would be suppressed again"

    def test_turn_3_a_variable_range_answers_a_range_question(self) -> None:
        """CASE 2. 「いつまで思い出せる？」 → 「固定された期間ではない」.

        No duration, and nothing further to say that would be true.
        """
        assert fulfils(
            _contract(AnswerTarget.GENERAL_MEMORY_CAPABILITY),
            _assessment(
                AnswerTarget.GENERAL_MEMORY_CAPABILITY,
                AnswerMode.CONDITIONAL_OR_VARIABLE,
            ),
        )

    @pytest.mark.parametrize(
        "mode", [AnswerMode.UNKNOWN, AnswerMode.UNAVAILABLE]
    )
    def test_turn_3_an_honest_blank_is_the_third_path(self, mode) -> None:
        """CASE 3. Where the source is empty, honesty is neither fabrication
        nor silence.

        Without this the only options when evidence runs out are to invent
        something or to say nothing, and the second is what production did.
        """
        assert fulfils(
            _contract(AnswerTarget.GENERAL_MEMORY_CAPABILITY, may_abstain=True),
            _assessment(AnswerTarget.GENERAL_MEMORY_CAPABILITY, mode),
        )

    def test_turn_3_a_pleasantry_is_still_rejected(self) -> None:
        """CASE 4. The check has to keep catching what it was built for.

        「よろしくお願いします」 touches nothing the question asked about.
        """
        assert not fulfils(
            _contract(AnswerTarget.GENERAL_MEMORY_CAPABILITY),
            _assessment(
                AnswerTarget.GENERAL_MEMORY_CAPABILITY, AnswerMode.NON_ANSWER
            ),
        )


# =============================================================================
# What the contract tells the rest of the turn
# =============================================================================


class TestContractCarriesTheAnswerForms:
    """Repair used to be told only "you did not answer".

    From that, a model concludes it must produce the missing value — which,
    when there is none, means inventing one. The acceptable forms travel on the
    contract, so the realizer, the repair and the review all read the same list.
    """

    @staticmethod
    def _forms(contract) -> set[str]:
        """Just the forms line.

        Read out of the render rather than scanned for substrings, because
        `source_availability: unknown` contains a mode name and would make a
        whole-text search agree with itself.
        """
        for line in contract.render().splitlines():
            if line.startswith("acceptable_answer_forms:"):
                return {
                    part.strip()
                    for part in line.split(":", 1)[1].split(",")
                    if part.strip()
                }
        return set()

    def test_an_answer_contract_lists_its_acceptable_forms(self) -> None:
        forms = self._forms(_contract())

        assert forms == {mode.value for mode in ANSWERING_MODES}
        assert AnswerMode.NON_ANSWER.value not in forms

    def test_a_contract_that_forbids_abstaining_says_so(self) -> None:
        forms = self._forms(_contract(may_abstain=False))

        assert forms.isdisjoint({mode.value for mode in ABSTAINING_MODES})
        assert AnswerMode.NOT_APPLICABLE.value in forms
        assert AnswerMode.CONDITIONAL_OR_VARIABLE.value in forms

    def test_a_turn_with_no_answer_obligation_says_nothing_about_forms(
        self,
    ) -> None:
        rendered = ResponseContract().render()

        assert "acceptable_answer_forms" not in rendered

    def test_the_rendered_forms_are_exactly_what_python_will_accept(self) -> None:
        """The list is derived, not written twice."""
        for may_abstain in (True, False):
            contract = _contract(may_abstain=may_abstain)
            accepted = {
                mode
                for mode in AnswerMode
                if fulfils(contract, _assessment(contract.answer_target, mode))
            }
            assert accepted == set(contract.acceptable_answer_modes)


# =============================================================================
# The prompt asks for a classification, not a judgement
# =============================================================================


class TestReviewPrompt:
    @pytest.fixture
    def prompt(self, prompt_registry):
        from app.dialogue.response_contract import PROMPT_ID

        return prompt_registry.get(PROMPT_ID)

    def test_the_newest_version_is_the_one_that_ships(
        self, prompt, prompt_registry
    ) -> None:
        """Versions are immutable — v1 is left alone and v2 is added, which is
        how `semantic_claim_review` and `turn_understanding` were revised."""
        from app.dialogue.response_contract import PROMPT_ID

        versions = prompt_registry.versions_of(PROMPT_ID)
        assert 1 in versions and 2 in versions
        assert prompt.version == max(versions)

    def test_it_names_every_mode_and_target(self, prompt) -> None:
        for mode in AnswerMode:
            assert mode.value in prompt.body, mode
        for target in AnswerTarget:
            assert target.value in prompt.body, target

    def test_it_does_not_ask_for_a_fulfilment_boolean(self, prompt) -> None:
        assert '"fulfilled"' not in prompt.body
        assert "fulfilled=" not in prompt.body

    def test_it_forbids_the_rule_the_model_invented(self, prompt) -> None:
        """The failure was "no concrete value, therefore not an answer". The
        prompt has to say that in as many words, because it is the reading a
        model arrives at on its own."""
        assert "具体的な値" in prompt.body
        assert "non_answer" in prompt.body

    def test_it_disclaims_the_factual_authority(self, prompt) -> None:
        assert "事実判定でもない" in prompt.body
        assert "Semantic Evidence Authority" in prompt.body

    def test_it_narrows_the_direct_question_fallback(self, prompt) -> None:
        assert "direct_user_question" in prompt.body
        assert "逃げない" in prompt.body
