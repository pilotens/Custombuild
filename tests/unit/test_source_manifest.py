from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.source_manifest import (
    SCHEMA_VERSION,
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


def test_manifest_identity_includes_docker_relevant_executable_mode(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX executable mode changes")
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    source = repo / "scripts" / "startup.sh"
    source.write_text("#!/bin/sh\n", encoding="utf-8")
    source.chmod(0o644)
    regular_manifest, _canonical, regular_digest = build_source_manifest(repo)

    source.chmod(0o755)
    executable_manifest, _canonical, executable_digest = build_source_manifest(repo)

    regular = next(
        entry for entry in regular_manifest["entries"] if entry["path"] == "scripts/startup.sh"
    )
    executable = next(
        entry
        for entry in executable_manifest["entries"]
        if entry["path"] == "scripts/startup.sh"
    )
    assert regular["mode"] == 0o644
    assert executable["mode"] == 0o755
    assert executable_digest != regular_digest


def test_manifest_does_not_depend_on_absolute_repository_location(tmp_path: Path) -> None:
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second")
    for repo in (first, second):
        (repo / "scripts").mkdir()
        (repo / "scripts" / "app.py").write_text("print('same')\n", encoding="utf-8")

    assert build_source_manifest(first)[1:] == build_source_manifest(second)[1:]


def test_deployment_contract_is_bound_to_application_source_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "build.py").write_text("source\n", encoding="utf-8")
    (repo / "compose.yml").write_text("name: prod\n", encoding="utf-8")
    before = build_source_manifest(repo)[2]

    (repo / "compose.yml").write_text("name: test\n", encoding="utf-8")

    assert build_source_manifest(repo)[2] != before


def test_release_workflow_is_hashed_even_when_dockerignore_excludes_github(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, ".github\nartifacts\n")
    workflow = repo / ".github" / "workflows" / "release.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: first\n", encoding="utf-8")
    before_manifest, _canonical, before_digest = build_source_manifest(repo)

    workflow.write_text("name: second\n", encoding="utf-8")
    after_manifest, _canonical, after_digest = build_source_manifest(repo)

    assert before_manifest["schema_version"] == SCHEMA_VERSION
    assert ".github/workflows" in before_manifest["release_control_roots"]
    assert ".github/workflows/release.yml" in {
        entry["path"] for entry in before_manifest["entries"]
    }
    assert ".github/workflows/release.yml" in {
        entry["path"] for entry in after_manifest["entries"]
    }
    assert after_digest != before_digest


def test_current_repository_manifest_contains_release_workflows() -> None:
    paths = {entry["path"] for entry in build_source_manifest(Path.cwd())[0]["entries"]}

    assert ".github/workflows/ci.yml" in paths
    assert ".github/workflows/prod-ci.yml" in paths
    assert ".github/workflows/supply-chain.yml" in paths
    # These remain bound by VCS_REF plus the final clean-tree gate, not by the
    # narrower image/workflow source manifest.
    assert "README.md" not in paths
    assert "docs/SECURITY.md" not in paths
    assert "tests/unit/test_source_manifest.py" not in paths
    assert "apps/web/e2e/workspace.spec.ts" not in paths


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
        "infra/seaweedfs/Dockerfile",
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FROM example\n", encoding="utf-8")
    (repo / "services/api/Dockerfile").write_text(
        "FROM example\nCOPY deployment-secrets /app/deployment-secrets\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceManifestError, match="outside the manifest contract"):
        build_source_manifest(repo)


def test_manifest_rejects_the_retired_duplicate_prod_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "prod").mkdir()
    (repo / "prod" / "compose.yml").write_text("name: stale\n", encoding="utf-8")

    with pytest.raises(SourceManifestError, match="repository root must be the only"):
        build_source_manifest(repo)


@pytest.mark.parametrize(
    "relative",
    (
        "compose.yml",
        "compose.external-production.yml",
        "infra/postgres/init-roles.sh",
        "infra/seaweedfs/Dockerfile",
        ".github/workflows/prod-ci.yml",
        ".grype.yaml",
        "security/vulnerability-exceptions.json",
    ),
)
def test_release_runtime_inputs_change_source_identity(
    tmp_path: Path,
    relative: str,
) -> None:
    repo = _repo(tmp_path)
    for dockerfile_relative in (
        "apps/web/Dockerfile",
        "infra/seaweedfs/Dockerfile",
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
    ):
        dockerfile = repo / dockerfile_relative
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("first\n", encoding="utf-8")
    before = build_source_manifest(repo)[2]

    target.write_text("second\n", encoding="utf-8")

    assert build_source_manifest(repo)[2] != before


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

    with pytest.raises(SourceManifestError, match="would change the build/control source set"):
        from scripts.source_manifest import _ensure_output_is_not_a_build_input

        _ensure_output_is_not_a_build_input(
            repo=repo,
            output=repo / "manifest.json",
            dockerignore=DockerIgnore("artifacts\n"),
        )

    with pytest.raises(SourceManifestError, match="would change the build/control source set"):
        _ensure_output_is_not_a_build_input(
            repo=repo,
            output=repo / ".github" / "workflows" / "manifest.json",
            dockerignore=DockerIgnore(".github\nartifacts\n"),
        )
