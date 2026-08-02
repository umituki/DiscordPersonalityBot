"""Development conversation evaluation (patch spec 11).

The corpus is run here on every test run, with no model and no network. That
is the point of the structural evaluator: patch spec prohibition 10 forbids
making an external LLM a required dependency, so the check that ships with the
project must work without one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evaluation.conversation import (
    CRITERIA,
    EvaluationError,
    JudgeEvaluator,
    JudgeScore,
    StructuralEvaluator,
    load_scenarios,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "config" / "evaluation" / "conversation_scenarios.yaml"

#: Patch spec 11: ``最低50 scenario、推奨100以上``.
MINIMUM_SCENARIOS = 50


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(CORPUS)


def test_the_corpus_meets_the_required_size(scenarios) -> None:
    assert len(scenarios) >= MINIMUM_SCENARIOS


def test_every_scenario_is_judged_on_named_criteria(scenarios) -> None:
    for scenario in scenarios:
        assert scenario.criteria, f"{scenario.scenario_id} names no criteria"
        assert set(scenario.criteria) <= set(CRITERIA)


def test_the_corpus_covers_every_criterion(scenarios) -> None:
    covered = {name for scenario in scenarios for name in scenario.criteria}
    assert covered == set(CRITERIA)


def test_the_guard_agrees_with_the_whole_corpus(scenarios) -> None:
    report = StructuralEvaluator().evaluate(scenarios)
    assert report.failures == (), report.render()
    assert report.pass_rate == 1.0


def test_the_corpus_keeps_a_false_positive_budget(scenarios) -> None:
    """Half the corpus is replies a person would send, which must be left alone.

    Without this, the corpus could be satisfied by a guard that rejects
    everything.
    """
    accepted = [scenario for scenario in scenarios if scenario.expect == "accept"]
    assert len(accepted) >= len(scenarios) // 2


def test_a_rejection_scenario_names_the_reason(scenarios) -> None:
    for scenario in scenarios:
        if scenario.expect == "reject":
            assert scenario.reason, f"{scenario.scenario_id} does not say why"


def test_a_malformed_scenario_is_refused(tmp_path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "scenarios:\n  - id: x\n    reply: ok\n    criteria: [nonsense]\n", encoding="utf-8"
    )
    with pytest.raises(EvaluationError):
        load_scenarios(path)


def test_duplicate_scenario_ids_are_refused(tmp_path) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text(
        "scenarios:\n  - id: x\n    reply: a\n  - id: x\n    reply: b\n", encoding="utf-8"
    )
    with pytest.raises(EvaluationError):
        load_scenarios(path)


def test_a_missing_corpus_is_an_error(tmp_path) -> None:
    with pytest.raises(EvaluationError):
        load_scenarios(tmp_path / "nothing.yaml")


# --- the optional judge -----------------------------------------------------
class RecordingJudge:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def score(self, prompt: str) -> JudgeScore:
        self.prompts.append(prompt)
        return JudgeScore(scores={name: 0.9 for name in CRITERIA}, comment="fine")


async def test_the_judge_is_provider_neutral(scenarios) -> None:
    """Any object with a ``score`` method works — no vendor is named."""
    judge = RecordingJudge()
    evaluator = JudgeEvaluator(judge=judge)

    results = await evaluator.evaluate(scenarios[:3])

    assert len(results) == 3
    assert len(judge.prompts) == 3
    assert all(name in judge.prompts[0] for name in CRITERIA)


async def test_the_judge_reports_its_worst_criterion() -> None:
    score = JudgeScore(scores={"naturalness": 0.9, "question_overuse": 0.2})
    assert score.worst == ("question_overuse", 0.2)
