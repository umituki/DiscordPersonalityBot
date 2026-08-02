"""INVARIANT: the LLM is not an authority (spec 2.1, 2.11, 28.2, `.claude/rules/llm.md`).

The model proposes; Python decides. This suite pins the structural half of that
rule: the LLM package cannot reach state, invalid output cannot become a value,
and prompt text is not embedded in code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.llm.structured import StructuredGenerator
from app.llm.types import LLMMessage, LLMResponse
from app.llm.validation import Stage, ValidationFailure, ValidationPipeline
from tests.unit.test_llm_structured import NOW, ScriptedClient

pytestmark = pytest.mark.invariant

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
LLM_ROOT = APP_ROOT / "llm"

FORBIDDEN_LLM_IMPORTS = re.compile(
    r"^\s*(from|import)\s+app\.(state\.committer|state\.arbitrator|orchestrator)", re.MULTILINE
)


class Reply(BaseModel):
    text: str


def test_llm_package_cannot_reach_the_state_writer() -> None:
    offenders = [
        str(path.relative_to(APP_ROOT))
        for path in LLM_ROOT.rglob("*.py")
        if FORBIDDEN_LLM_IMPORTS.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        f"{offenders} import the state writer; LLM output must travel as proposals "
        "through the owning engine (spec 2.1, 2.7)"
    )


def test_llm_package_does_not_write_state_values() -> None:
    offenders = [
        str(path.relative_to(APP_ROOT))
        for path in LLM_ROOT.rglob("*.py")
        if "write_value(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


async def test_invalid_output_never_yields_a_value(clock, prompt_registry) -> None:
    """Spec 28.2: ``不正出力から State を更新しない``."""
    client = ScriptedClient(["{ this is not json", "still not json", "nope"])
    generator = StructuredGenerator(
        client, prompts=prompt_registry, clock=clock, max_attempts=3
    )

    outcome = await generator.generate(Reply, (LLMMessage(role="user", content="hi"),), purpose="t")

    assert outcome.accepted is False
    assert outcome.value is None
    assert outcome.rejected_stage is Stage.PARSE


async def test_semantically_rejected_output_never_yields_a_value(clock, prompt_registry) -> None:
    class AlwaysReject:
        name = "identity_guard"
        stage = Stage.IDENTITY

        def check(self, candidate, context):
            return ValidationFailure(self.stage, "impossible_physical_claim", "cannot be true")

    client = ScriptedClient(['{"text": "I went outside and touched the rain"}'])
    generator = StructuredGenerator(client, prompts=prompt_registry, clock=clock)

    outcome = await generator.generate(
        Reply,
        (LLMMessage(role="user", content="hi"),),
        purpose="conversation",
        pipeline=ValidationPipeline([AlwaysReject()]),
    )

    assert outcome.accepted is False
    assert outcome.value is None
    assert outcome.failure.reason_code == "impossible_physical_claim"


async def test_llm_self_reported_confidence_is_not_authoritative(clock, prompt_registry) -> None:
    """A model claiming certainty does not bypass validation."""

    class Confident(BaseModel):
        text: str
        confidence: float

    class RejectAnyway:
        name = "policy"
        stage = Stage.SEMANTIC

        def check(self, candidate, context):
            return ValidationFailure(self.stage, "policy_rejected", "confidence is only a signal")

    client = ScriptedClient(['{"text": "certain", "confidence": 1.0}'])
    outcome = await StructuredGenerator(
        client, prompts=prompt_registry, clock=clock
    ).generate(
        Confident,
        (LLMMessage(role="user", content="hi"),),
        purpose="t",
        pipeline=ValidationPipeline([RejectAnyway()]),
    )

    assert outcome.accepted is False


def test_prompt_text_is_not_embedded_in_code() -> None:
    """Spec 38: prompts live in versioned files, not in business logic."""
    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or path.parts[-2:-1] == ("storage",):
            continue
        if path.relative_to(APP_ROOT).parts[0] == "storage":
            continue  # SQL statements are long by nature and live here legitimately
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and len(node.value) > 200
                and id(node) not in docstrings
            ):
                offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}")
    assert offenders == [], (
        f"long inline strings look like prompts: {offenders}. Move them to config/prompts/."
    )


async def test_traced_client_records_every_call(clock, llm_calls_repo, tracer) -> None:
    """Spec 29: model and prompt versions stay attributable."""
    from app.llm.client import TracedLLMClient
    from app.llm.types import LLMRequest

    class Echo:
        model = "test-model"

        async def generate(self, request):
            return LLMResponse(text="ok", model=self.model, created_at=NOW, latency_ms=5)

        async def health(self):
            return True

        async def aclose(self):
            return None

    client = TracedLLMClient(Echo(), tracer, clock=clock)
    await client.generate(
        LLMRequest(
            messages=(LLMMessage(role="user", content="hi"),),
            purpose="conversation_reply",
            prompt_id="reply",
            prompt_version="reply@v1",
        )
    )

    row = llm_calls_repo.recent()[0]
    assert row["purpose"] == "conversation_reply"
    assert row["prompt_version"] == "reply@v1"
    assert row["status"] == "succeeded"
