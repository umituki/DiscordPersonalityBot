"""Conversation quality evaluation (patch spec 11).

Two evaluators over one scenario corpus:

``StructuralEvaluator``
    No model, no network, deterministic. It runs the production Conversation
    Quality Guard over each scenario and compares the verdict against what the
    scenario says should happen. This is what CI runs, and it is why the
    corpus is useful even with no provider configured.

``JudgeEvaluator``
    Optional, development only. It asks *some* model — any object with the
    ``LLMClient`` shape, local or remote — to score a reply on the criteria of
    patch spec 11. Provider-neutral by construction: this module names no
    vendor and reads no vendor-specific setting.

The corpus lives in ``config/evaluation/conversation_scenarios.yaml`` so
scenarios can be added without touching code (patch spec 11 asks for at least
50, and more over time).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.conversation.models import ConversationTurn, DialogueAct
from app.conversation.quality import ConversationQualityGuard, QualityVerdict

logger = logging.getLogger(__name__)

MODULE = "conversation_evaluation"

DEFAULT_CORPUS = Path("config/evaluation/conversation_scenarios.yaml")

#: The criteria of patch spec 11, in the order they are listed there.
CRITERIA: tuple[str, ...] = (
    "naturalness",
    "question_overuse",
    "echoing",
    "reciprocity",
    "relationship_distance",
    "personality_consistency",
    "emotion_expression_consistency",
    "topic_continuity",
    "smalltalk_handling",
    "repetition",
)

Expectation = Literal["accept", "reject"]


class EvaluationError(RuntimeError):
    """Raised when the scenario corpus cannot be loaded."""


@dataclass(frozen=True, slots=True)
class Scenario:
    """One reply, in context, with what should happen to it."""

    scenario_id: str
    user_message: str
    reply: str
    expect: Expectation
    acts: DialogueAct
    history: tuple[tuple[str, str], ...] = ()
    reason: str | None = None
    criteria: tuple[str, ...] = ()
    note: str = ""

    def turns(self) -> tuple[ConversationTurn, ...]:
        """The history as the guard sees it.

        Timestamps are synthetic and only have to be ordered: nothing in the
        guard reads them, and a scenario file should not carry clock detail.
        """
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return tuple(
            ConversationTurn(
                turn_id=f"{self.scenario_id}_{index}",
                conversation_id=self.scenario_id,
                event_id=f"{self.scenario_id}_evt_{index}",
                speaker=speaker,  # type: ignore[arg-type]
                author_id="user" if speaker == "user" else None,
                content=content,
                occurred_at=base + timedelta(minutes=index),
            )
            for index, (speaker, content) in enumerate(self.history)
        )


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: Scenario
    verdict: QualityVerdict

    @property
    def passed(self) -> bool:
        expected_rejection = self.scenario.expect == "reject"
        if self.verdict.rejected != expected_rejection:
            return False
        if self.scenario.reason is None:
            return True
        return self.scenario.reason in self.verdict.issues

    @property
    def detail(self) -> str:
        want = self.scenario.expect
        if self.scenario.reason:
            want = f"{want} ({self.scenario.reason})"
        got = "reject" if self.verdict.rejected else "accept"
        if self.verdict.issues:
            got = f"{got} ({', '.join(self.verdict.issues)})"
        return f"{self.scenario.scenario_id}: expected {want}, got {got}"


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    results: tuple[ScenarioResult, ...] = ()

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def failures(self) -> tuple[ScenarioResult, ...]:
        return tuple(result for result in self.results if not result.passed)

    @property
    def pass_rate(self) -> float:
        return 1.0 if not self.results else 1.0 - len(self.failures) / len(self.results)

    def render(self) -> str:
        lines = [f"scenarios={self.total} passed={self.total - len(self.failures)}"]
        lines.extend(result.detail for result in self.failures)
        return "\n".join(lines)


# --- corpus -----------------------------------------------------------------
def load_scenarios(path: Path | str = DEFAULT_CORPUS) -> tuple[Scenario, ...]:
    corpus_path = Path(path)
    if not corpus_path.is_file():
        raise EvaluationError(f"scenario corpus not found: {corpus_path}")
    raw: Any = yaml.safe_load(corpus_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("scenarios") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise EvaluationError(f"scenario corpus has no scenarios: {corpus_path}")

    scenarios: list[Scenario] = []
    seen: set[str] = set()
    for entry in entries:
        scenario = _parse_scenario(entry, corpus_path)
        if scenario.scenario_id in seen:
            raise EvaluationError(f"duplicate scenario id: {scenario.scenario_id}")
        seen.add(scenario.scenario_id)
        scenarios.append(scenario)
    return tuple(scenarios)


def _parse_scenario(entry: Any, path: Path) -> Scenario:
    if not isinstance(entry, dict):
        raise EvaluationError(f"scenario must be a mapping in {path}: {entry!r}")
    try:
        acts = DialogueAct.model_validate(entry.get("acts") or {})
    except Exception as exc:  # noqa: BLE001 - the file is hand-written
        raise EvaluationError(f"invalid acts in {entry.get('id')!r}: {exc}") from exc

    unknown = set(entry.get("criteria") or ()) - set(CRITERIA)
    if unknown:
        raise EvaluationError(f"unknown criteria in {entry.get('id')!r}: {sorted(unknown)}")

    history = tuple(
        (str(turn[0]), str(turn[1])) for turn in (entry.get("history") or ()) if len(turn) == 2
    )
    expect = entry.get("expect", "accept")
    if expect not in ("accept", "reject"):
        raise EvaluationError(f"expect must be accept or reject in {entry.get('id')!r}")

    return Scenario(
        scenario_id=str(entry["id"]),
        user_message=str(entry.get("user_message", "")),
        reply=str(entry["reply"]),
        expect=expect,
        acts=acts,
        history=history,
        reason=entry.get("reason"),
        criteria=tuple(entry.get("criteria") or ()),
        note=str(entry.get("note", "")),
    )


# --- structural evaluation (no model) ---------------------------------------
class StructuralEvaluator:
    """Runs the production guard over the corpus. Deterministic and offline."""

    def __init__(self, guard: ConversationQualityGuard | None = None) -> None:
        self._guard = guard or ConversationQualityGuard()

    def evaluate(self, scenarios: Sequence[Scenario]) -> EvaluationReport:
        results = []
        for scenario in scenarios:
            verdict = self._guard.review(
                scenario.reply,
                acts=scenario.acts,
                user_text=scenario.user_message,
                recent_turns=scenario.turns(),
            )
            results.append(ScenarioResult(scenario=scenario, verdict=verdict))
        return EvaluationReport(results=tuple(results))


# --- judge evaluation (optional, development only) --------------------------
class JudgeScore(BaseModel):
    """One judged reply. Scores are 0-1 with 1 meaning "no problem here"."""

    model_config = ConfigDict(extra="forbid")

    scores: dict[str, float] = Field(default_factory=dict)
    comment: str = ""

    @property
    def worst(self) -> tuple[str, float] | None:
        if not self.scores:
            return None
        criterion = min(self.scores, key=lambda key: self.scores[key])
        return criterion, self.scores[criterion]


class StructuredJudge(Protocol):
    """Anything that can turn a prompt into a validated :class:`JudgeScore`."""

    async def score(self, prompt: str) -> JudgeScore: ...


@dataclass
class JudgeEvaluator:
    """Scores replies with a model. Never wired into the running system.

    Provider-neutral: it holds a ``StructuredJudge``, which is satisfied by the
    project's own :class:`~app.llm.structured.StructuredGenerator` against any
    client — the local Ollama one included. Nothing here assumes a vendor.
    """

    judge: StructuredJudge
    criteria: tuple[str, ...] = CRITERIA
    scores: list[tuple[str, JudgeScore]] = field(default_factory=list)

    async def evaluate(self, scenarios: Sequence[Scenario]) -> dict[str, JudgeScore]:
        results: dict[str, JudgeScore] = {}
        for scenario in scenarios:
            score = await self.judge.score(self.render_prompt(scenario))
            results[scenario.scenario_id] = score
            self.scores.append((scenario.scenario_id, score))
        return results

    def render_prompt(self, scenario: Scenario) -> str:
        history = "\n".join(
            f"{'USER' if speaker == 'user' else 'YUI'}: {content}"
            for speaker, content in scenario.history
        )
        criteria = "\n".join(f"- {name}" for name in self.criteria)
        return (
            "次の会話の最後の返事を評価してください。\n\n"
            f"# 直近のやりとり\n{history or '(なし)'}\n\n"
            f"# 相手の最後のメッセージ\n{scenario.user_message}\n\n"
            f"# 評価する返事\n{scenario.reply}\n\n"
            f"# 評価項目 (0.0〜1.0、1.0 が問題なし)\n{criteria}\n\n"
            '{"scores": {"naturalness": 0.0}, "comment": ""} の形式の JSON を1つだけ出力する。'
        )


__all__ = [
    "CRITERIA",
    "EvaluationError",
    "EvaluationReport",
    "JudgeEvaluator",
    "JudgeScore",
    "Scenario",
    "ScenarioResult",
    "StructuralEvaluator",
    "StructuredJudge",
    "load_scenarios",
]
