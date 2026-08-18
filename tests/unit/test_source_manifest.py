from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.source_manifest import (
    DockerIgnore,
    SourceManifestError,
    _dockerfile_local_copy_sources,
    _validate_dockerfile_copy_contract,
    build_source_manifest,
    main,
)


def _repo(tmp_path: Path, dockerignore: str = "artifacts\n") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".dockerignore").write_text(dockerignore, encoding="utf-8")
    return tmp_path


def test_manifest_is_canonical_stable_and_content_sensitive(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    source = repo / "scripts" / "design.py"
    source.write_text("first\n", encoding="utf-8")

    manifest, canonical, digest = build_source_manifest(repo)
    _, repeated, repeated_digest = build_source_manifest(repo)

    assert repeated == canonical
    assert repeated_digest == digest == hashlib.sha256(canonical).hexdigest()
    assert json.loads(canonical) == manifest
    assert canonical == json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert ".dockerignore" in {entry["path"] for entry in manifest["entries"]}
    assert "scripts/design.py" in {entry["path"] for entry in manifest["entries"]}
    file_entry = next(
        entry for entry in manifest["entries"] if entry["path"] == "scripts/design.py"
    )
    assert isinstance(file_entry["mode"], int)

    source.write_text("second\n", encoding="utf-8")
    assert build_source_manifest(repo)[2] != digest


def test_manifest_does_not_depend_on_absolute_repository_location(tmp_path: Path) -> None:
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second")
    for repo in (first, second):
        (repo / "scripts").mkdir()
        (repo / "scripts" / "app.py").write_text("print('same')\n", encoding="utf-8")

    assert build_source_manifest(first)[1:] == build_source_manifest(second)[1:]


def test_deployment_only_files_are_not_shared_application_image_sources(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "build.py").write_text("source\n", encoding="utf-8")
    (repo / "compose.yml").write_text("name: prod\n", encoding="utf-8")
    before = build_source_manifest(repo)[2]

    (repo / "compose.yml").write_text("name: test\n", encoding="utf-8")

    assert build_source_manifest(repo)[2] == before


def test_unused_dockerignore_rule_still_changes_source_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "build.py").write_text("source\n", encoding="utf-8")
    before = build_source_manifest(repo)[2]

    (repo / ".dockerignore").write_text("artifacts\nfuture-generated\n", encoding="utf-8")

    assert build_source_manifest(repo)[2] != before


def test_repository_dockerfile_copy_sources_are_inside_the_manifest_contract() -> None:
    _validate_dockerfile_copy_contract(Path("."))


def test_copy_contract_rejects_a_new_unhashed_source_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for relative in (
        "apps/web/Dockerfile",
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FROM example\n", encoding="utf-8")
    (repo / "services/api/Dockerfile").write_text(
        "FROM example\nCOPY infra /app/infra\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceManifestError, match="outside the manifest contract"):
        build_source_manifest(repo)


def test_copy_parser_ignores_prior_build_stages(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "COPY --from=builder --chown=1001:1001 /workspace/output /app\n"
        "COPY scripts /app/scripts\n",
        encoding="utf-8",
    )

    assert _dockerfile_local_copy_sources(dockerfile) == ("scripts",)


def test_current_dockerignore_features_and_e2e_exclusion_are_applied(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "# comment\n**/__pycache__\n**/*.pyc\napps/web/e2e\n*.db\n",
    )
    for relative in (
        "apps/web/page.tsx",
        "apps/web/e2e/live.spec.ts",
        "services/api/__pycache__/config.pyc",
        "scratch.db",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")

    manifest = build_source_manifest(repo)[0]
    paths = {entry["path"] for entry in manifest["entries"]}

    assert "apps/web/page.tsx" in paths
    assert "apps/web/e2e" not in paths
    assert "apps/web/e2e/live.spec.ts" not in paths
    assert "services/api/__pycache__" not in paths
    assert "services/api/__pycache__/config.pyc" not in paths
    assert "scratch.db" not in paths


def test_globstar_and_ordered_negation_match_platform_neutrally() -> None:
    matcher = DockerIgnore("**/*.log\n!audit/keep.log\n")

    assert matcher.is_ignored("other/error.log", is_dir=False)
    assert not matcher.is_ignored("audit/keep.log", is_dir=False)
    assert not matcher.is_ignored("audit/notes.txt", is_dir=False)


def test_negation_can_reinclude_a_child_of_an_ignored_directory() -> None:
    matcher = DockerIgnore("generated\n!generated/release.json\n")

    assert matcher.is_ignored("generated/temporary.json", is_dir=False)
    assert not matcher.is_ignored("generated/release.json", is_dir=False)


def test_leading_slash_anchors_a_pattern_to_the_context_root() -> None:
    matcher = DockerIgnore("/cache\n")

    assert matcher.is_ignored("cache", is_dir=True)
    assert not matcher.is_ignored("nested/cache", is_dir=True)


def test_escaped_comment_and_negation_prefixes_are_literal() -> None:
    matcher = DockerIgnore("\\#notes\n\\!keep\n")

    assert matcher.is_ignored("#notes", is_dir=False)
    assert matcher.is_ignored("!keep", is_dir=False)


def test_symlink_identity_uses_link_target_not_target_contents(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("creating symbolic links requires optional Windows privileges")
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    target = repo / "scripts" / "target.txt"
    target.write_text("payload", encoding="utf-8")
    (repo / "scripts" / "alias.txt").symlink_to("target.txt")

    manifest = build_source_manifest(repo)[0]
    alias = next(entry for entry in manifest["entries"] if entry["path"] == "scripts/alias.txt")

    assert alias["path"] == "scripts/alias.txt"
    assert alias["target"] == "target.txt"
    assert alias["type"] == "symlink"
    assert isinstance(alias["mode"], int)


def test_cli_writes_exact_manifest_and_digest_only_to_ignored_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "source.txt").write_text("source", encoding="utf-8")
    output = repo / "artifacts" / "source-manifest.json"
    sha_output = repo / "artifacts" / "source-manifest.sha256"

    assert main(
        [
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--sha256-output",
            str(sha_output),
        ]
    ) == 0
    digest = capsys.readouterr().out.strip()
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
    assert sha_output.read_text(encoding="ascii") == f"{digest}  source-manifest.json\n"
    assert build_source_manifest(repo)[2] == digest

    with pytest.raises(SourceManifestError, match="would change the Docker build context"):
        from scripts.source_manifest import _ensure_output_is_not_a_build_input

        _ensure_output_is_not_a_build_input(
            repo=repo,
            output=repo / "manifest.json",
            dockerignore=DockerIgnore("artifacts\n"),
        )
