"""Static identity and context assembly (spec 1.3, 27)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.context.builder import (
    ContextBudget,
    ContextBuilder,
    ContextOverflowError,
    Requirement,
    estimate_tokens,
)
from app.resources.identity import IdentityError, load_identity

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- identity ---------------------------------------------------------------


def test_committed_identity_loads() -> None:
    identity = load_identity(REPO_ROOT / "character")
    assert identity.name
    # identity v2: a person in her own world, not software. The constraint that
    # used to be spelled "no body" is now spelled "nothing reaches the USER's
    # world", which is the boundary it was always protecting.
    assert identity.world == "yui_world"
    assert identity.existence.kind == "person"
    assert identity.speech
    assert {rule.id for rule in identity.must_not} >= {
        "no_cross_world_physical",
        "no_world_lore_invention",
        "no_fabricated_tool_success",
        "no_fabricated_memory",
    }


def test_identity_prompt_block_states_the_constraints() -> None:
    rendered = load_identity(REPO_ROOT / "character").render_for_prompt()
    # First person, not a role brief: 「なりきってください」 asks a model to
    # perform someone, and that is how she ended up narrating her own
    # construction when asked ordinary questions.
    assert "あなたは望月ゆい" in rendered
    assert "なりきって" not in rendered
    # The two halves of the world model: separate lives, and a channel.
    assert "別の世界" in rendered
    assert "話すことはできます" in rendered
    # The old framing is gone, not merely unused.
    assert "デジタル" not in rendered
    assert "肉体" not in rendered
    # Being someone is not a licence to have done things.
    assert "記録にない出来事" in rendered
    # Dynamic personality is not part of static identity (spec 12).
    assert "personality" not in rendered.lower()


def test_identity_version_tag_covers_both_files() -> None:
    identity = load_identity(REPO_ROOT / "character")
    assert identity.version_tag().startswith("identity@v")
    assert "rules@v" in identity.version_tag()


def test_missing_identity_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(IdentityError):
        load_identity(tmp_path)


def test_identity_without_a_name_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "identity.yaml").write_text("existence: {kind: digital}\n", encoding="utf-8")
    (tmp_path / "immutable_rules.yaml").write_text("must_not: []\n", encoding="utf-8")
    with pytest.raises(IdentityError, match="name"):
        load_identity(tmp_path)


# --- context ----------------------------------------------------------------


def test_estimate_tokens_is_monotonic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") >= 1
    assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)


def test_optional_items_are_dropped_first() -> None:
    builder = ContextBuilder()
    builder.add("identity", "i" * 40, requirement=Requirement.REQUIRED)
    builder.add("current_message", "m" * 40, requirement=Requirement.REQUIRED)
    builder.add("recent_conversation", "r" * 40, requirement=Requirement.IMPORTANT)
    builder.add("old_memories", "o" * 40, requirement=Requirement.OPTIONAL)

    built = builder.build(ContextBudget(max_tokens=60, chars_per_token=2.0))

    assert built.includes("identity")
    assert built.includes("current_message")
    assert built.includes("recent_conversation")
    assert not built.includes("old_memories")
    assert built.dropped_keys() == ("old_memories",)


def test_important_items_drop_before_required() -> None:
    builder = ContextBuilder()
    builder.add("identity", "i" * 40, requirement=Requirement.REQUIRED)
    builder.add("current_message", "m" * 40, requirement=Requirement.REQUIRED)
    builder.add("recent_conversation", "r" * 400, requirement=Requirement.IMPORTANT)

    built = builder.build(ContextBudget(max_tokens=45, chars_per_token=2.0))

    assert built.keys() == ("identity", "current_message")
    assert built.dropped_keys() == ("recent_conversation",)


def test_required_overflow_is_an_error_not_a_silent_trim() -> None:
    builder = ContextBuilder()
    builder.add("identity", "i" * 1000, requirement=Requirement.REQUIRED)
    with pytest.raises(ContextOverflowError):
        builder.build(ContextBudget(max_tokens=10, chars_per_token=2.0))


def test_higher_priority_survives_within_a_level() -> None:
    builder = ContextBuilder()
    builder.add("keep", "k" * 20, requirement=Requirement.OPTIONAL, priority=10)
    builder.add("drop", "d" * 20, requirement=Requirement.OPTIONAL, priority=1)

    built = builder.build(ContextBudget(max_tokens=10, chars_per_token=2.0))

    assert built.keys() == ("keep",)


def test_output_preserves_insertion_order() -> None:
    builder = ContextBuilder()
    builder.add("recent_conversation", "r", requirement=Requirement.IMPORTANT)
    builder.add("identity", "i", requirement=Requirement.REQUIRED)

    built = builder.build(ContextBudget(max_tokens=1000))

    assert built.keys() == ("recent_conversation", "identity")


def test_blank_items_are_not_added() -> None:
    built = ContextBuilder().add("empty", "   ").build(ContextBudget())
    assert built.keys() == ()
