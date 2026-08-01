"""Runtime manifests (spec 29, 2.17)."""

from __future__ import annotations

from pathlib import Path

from app.versioning.manifest import ManifestService, RuntimeManifest, detect_commit_hash


def manifest(**overrides) -> RuntimeManifest:
    base = dict(
        config_version=1,
        event_schema_version=1,
        code_commit_hash="abc123",
        components={"app_version": "2.0.0a1", "db_schema_version": "1"},
    )
    base.update(overrides)
    return RuntimeManifest(**base)


def test_identical_configuration_reuses_the_manifest(manifest_repo, clock) -> None:
    service = ManifestService(manifest_repo, clock=clock)
    first = service.ensure(manifest())
    second = service.ensure(manifest())

    assert first.created is True
    assert second.created is False
    assert first.manifest_id == second.manifest_id
    assert manifest_repo.count() == 1


def test_component_change_creates_a_new_manifest(manifest_repo, clock) -> None:
    service = ManifestService(manifest_repo, clock=clock)
    first = service.ensure(manifest())
    upgraded = manifest().with_components(model_version="qwen3.5:9b-2")
    second = service.ensure(upgraded)

    assert second.manifest_id != first.manifest_id
    assert manifest_repo.count() == 2


def test_commit_change_creates_a_new_manifest(manifest_repo, clock) -> None:
    service = ManifestService(manifest_repo, clock=clock)
    first = service.ensure(manifest())
    second = service.ensure(manifest(code_commit_hash="def456"))
    assert first.manifest_id != second.manifest_id


def test_fingerprint_is_order_independent() -> None:
    left = manifest(components={"a": "1", "b": "2"})
    right = manifest(components={"b": "2", "a": "1"})
    assert left.fingerprint == right.fingerprint


def test_detect_commit_hash_handles_missing_git(tmp_path: Path) -> None:
    assert detect_commit_hash(tmp_path) is None


def test_detect_commit_hash_reads_ref(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git" / "refs" / "heads"
    git_dir.mkdir(parents=True)
    (git_dir / "main").write_text("0123456789abcdef\n", encoding="utf-8")
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert detect_commit_hash(tmp_path) == "0123456789abcdef"


def test_detect_commit_hash_reads_packed_refs(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled\nfeedface refs/heads/main\n", encoding="utf-8"
    )
    assert detect_commit_hash(tmp_path) == "feedface"
