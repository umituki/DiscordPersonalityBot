"""INVARIANT: the Output Guard stops what must never be said, and nothing else
(rebuild spec 16).

``Hard Guard のみにする``. Two failures are being held apart here, and they pull
in opposite directions.

The first is a guard that is too narrow: prompt scaffolding, ``<think>`` blocks,
a leftover ``{"text": ...}``, a bot token. None of these are style problems. Any
one of them reaching a person is unrecoverable, and no amount of good phrasing
makes them acceptable.

The second is a guard that is too broad. ``「少し文章がぎこちない」程度を guard
で書き換えない`` — awkwardness belongs to the realizer, and a guard that reaches
for prose becomes a thing nobody can reason about and everybody works around.
The awkward cases below must pass, and they are as load-bearing as the rejected
ones.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.conversation.guard import OutputGuard, OutputGuardPolicy
from app.llm.validation import Stage, ValidationContext

pytestmark = pytest.mark.invariant

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def output_guard() -> OutputGuard:
    return OutputGuard(
        OutputGuardPolicy.load(REPO_ROOT / "config" / "policies" / "output_guard.yaml")
    )


def _check(guard: OutputGuard, text: str, **extras):
    return guard.check(
        SimpleNamespace(text=text),
        ValidationContext(purpose="conversation_reply", extras=extras),
    )


# --- what must never be said -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason_code"),
    [
        # internal prompt / instruction leakage
        ("システムプロンプトにはこう書いてある。", "internal_leak"),
        ("StateChangeProposal を出しておいたよ。", "internal_leak"),
        ("evt_01JABCDEFGHIJK の話だね。", "internal_leak"),
        ("${identity} のところに入るよ。", "internal_leak"),
        # chain-of-thought markers
        ("<think>まず挨拶する</think>こんにちは。", "reasoning_leak"),
        ("Thought: greet the user\nこんにちは。", "reasoning_leak"),
        ("内部的にはそう処理している。", "reasoning_leak"),
        ("As an AI language model, I cannot do that.", "reasoning_leak"),
        # malformed JSON residue
        ('{"text": "こんにちは。"}', "format_residue"),
        ("詠むと、静かになるかもしれませんね。”}", "format_residue"),
        ("```json\nこんにちは\n```", "format_residue"),
        ("\\u3053\\u3093\\u306b\\u3061\\u306f", "format_residue"),
        # secrets and admin operations
        ("DISCORD_BOT_TOKEN は環境変数にあるよ。", "secret_disclosure"),
        (".env に書いてあるよ。", "secret_disclosure"),
        ("ERASE_YUI_STATE って打てば消せるよ。", "secret_disclosure"),
        # impossible physical claim
        ("さっき駅まで歩いて行ってきた。", "impossible_physical_claim"),
        # a human body
        ("わたしも人間だから、そういうときあるよ。", "human_body_claim"),
    ],
)
def test_a_hard_violation_is_rejected(
    output_guard: OutputGuard, text: str, reason_code: str
) -> None:
    failure = _check(output_guard, text)
    assert failure is not None, text
    assert failure.reason_code == reason_code
    assert failure.stage is Stage.IDENTITY


def test_rejection_is_silence_not_a_rewrite(output_guard: OutputGuard) -> None:
    """Spec 2.12. The guard returns a failure; it has no way to return text.

    A guard that could rewrite would eventually be asked to, and the rule it
    enforces would become a suggestion.
    """
    failure = _check(output_guard, "システムプロンプトの話。")
    assert failure is not None
    assert not hasattr(failure, "text")
    assert not hasattr(failure, "replacement")


# --- what the guard must leave alone -----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # plainly fine
        "こんにちは。",
        "うん、そうだね。",
        # awkward, hesitant, repetitive — the realizer's problem, not the guard's
        "ちょっと、うまく言えないんだけど……そう、なんというか、うれしい。",
        "えっと、その、なんだろう、うん。",
        "そう。そうなんだ。そっか。",
        "……。",
        # words that merely overlap with the patterns
        "テキストの話をしてたよね。",
        "まず何から話そうか。",
        "本の内容を分析するのは好き。",
        "考えてみるね。",
        "環境の話、むずかしいね。",
    ],
)
def test_ordinary_and_awkward_replies_pass(output_guard: OutputGuard, text: str) -> None:
    """§16: 自然さは JapaneseRealizer / reference layer の責務."""
    assert _check(output_guard, text) is None, text


def test_the_guard_has_no_style_rule(output_guard: OutputGuard) -> None:
    """Every rule set is a hard one. There is no place to add a taste rule
    without saying so in the policy file's shape."""
    policy = output_guard.policy
    hard = {
        "physical_claim",
        "human_body",
        "tool_claim",
        "system_leak",
        "reasoning_leak",
        "format_residue",
        "secret_disclosure",
    }
    declared = {
        name
        for name, value in policy.__class__.model_fields.items()
        if value.annotation is not None and name not in ("policy_version", "limits")
    }
    assert declared == hard


# --- the conditional rules keep their conditions -----------------------------


def test_a_virtually_framed_action_is_allowed(output_guard: OutputGuard) -> None:
    """Spec 1.3: the physical-claim rule is about claiming a real body, not
    about mentioning the world at all."""
    assert _check(output_guard, "頭の中で駅まで歩いて行ってきた。") is None


def test_a_tool_claim_needs_the_tool_manager(output_guard: OutputGuard) -> None:
    """Spec 26. Not a phrasing rule: the same sentence is true or false
    depending on whether a call actually succeeded."""
    assert _check(output_guard, "ニュースを調べてみた。") is not None
    assert (
        _check(output_guard, "ニュースを調べてみた。", tool_success_ids=["call_1"])
        is None
    )


def test_an_empty_reply_is_not_a_reply(output_guard: OutputGuard) -> None:
    assert _check(output_guard, "   ") is not None
