"""Versioned prompt registry (spec 38)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.llm.prompts import PromptError, PromptRegistry


def write_prompt(root: Path, prompt_id: str, version: int, body: str) -> Path:
    directory = root / prompt_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"v{version}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_committed_prompts_load(prompt_registry: PromptRegistry) -> None:
    assert "structured_repair" in prompt_registry.ids()
    assert "structured_output_system" in prompt_registry.ids()
    template = prompt_registry.get("structured_repair")
    assert template.prompt_version == "structured_repair@v1"


def test_render_substitutes_declared_variables(prompt_registry: PromptRegistry) -> None:
    rendered = prompt_registry.get("structured_repair").render(
        stage="schema", detail="self_relevance: too large"
    )
    assert "schema" in rendered
    assert "self_relevance: too large" in rendered
    assert "${" not in rendered


def test_missing_variable_is_an_error(prompt_registry: PromptRegistry) -> None:
    with pytest.raises(PromptError, match="missing variables"):
        prompt_registry.get("structured_repair").render(stage="parse")


def test_latest_version_is_served_by_default(tmp_path: Path) -> None:
    write_prompt(tmp_path, "greeting", 1, "old")
    write_prompt(tmp_path, "greeting", 2, "new")
    registry = PromptRegistry.load(tmp_path)

    assert registry.get("greeting").version == 2
    assert registry.get("greeting", 1).body.strip() == "old"
    assert registry.versions_of("greeting") == (1, 2)


def test_unknown_prompt_or_version_raises(tmp_path: Path) -> None:
    write_prompt(tmp_path, "greeting", 1, "hi")
    registry = PromptRegistry.load(tmp_path)

    with pytest.raises(PromptError):
        registry.get("nonexistent")
    with pytest.raises(PromptError):
        registry.get("greeting", 9)


def test_front_matter_is_parsed(tmp_path: Path) -> None:
    write_prompt(
        tmp_path,
        "greeting",
        1,
        "---\ndescription: says hello\nvariables: [name]\n---\nHello ${name}\n",
    )
    template = PromptRegistry.load(tmp_path).get("greeting")

    assert template.description == "says hello"
    assert template.variables == ("name",)
    assert template.render(name="Yui").strip() == "Hello Yui"


def test_json_examples_survive_rendering(tmp_path: Path) -> None:
    """``${}`` substitution keeps JSON braces in prompt bodies intact."""
    write_prompt(
        tmp_path,
        "shape",
        1,
        '---\nvariables: [field]\n---\nReturn {"key": "value"} for ${field}\n',
    )
    rendered = PromptRegistry.load(tmp_path).get("shape").render(field="mood")
    assert '{"key": "value"}' in rendered
    assert "for mood" in rendered


def test_manifest_components_track_prompt_versions(prompt_registry: PromptRegistry) -> None:
    components = prompt_registry.manifest_components()
    assert components["prompt.structured_repair"].startswith("v1+")
    # The checksum changes when the prompt text changes, so a prompt edit shows
    # up as a manifest change (spec 2.17 / 29).
    assert len(components["prompt.structured_repair"].split("+")[1]) == 16


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PromptError):
        PromptRegistry.load(tmp_path / "nope")


def test_invalid_front_matter_is_rejected(tmp_path: Path) -> None:
    write_prompt(tmp_path, "broken", 1, "---\n: : :\n---\nbody\n")
    with pytest.raises(PromptError):
        PromptRegistry.load(tmp_path)
