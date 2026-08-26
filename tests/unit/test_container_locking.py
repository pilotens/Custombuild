from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def test_docker_context_excludes_generated_typescript_build_state() -> None:
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8")

    assert "**/*.tsbuildinfo" in dockerignore.splitlines()
    assert "**/.next-*" in dockerignore.splitlines()


def test_web_production_context_excludes_live_e2e_harnesses() -> None:
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8")

    assert "apps/web/e2e" in dockerignore.splitlines()


@pytest.mark.parametrize("dockerfile", ("services/api/Dockerfile", "services/worker/Dockerfile"))
def test_python_images_install_the_exact_uv_lock(dockerfile: str) -> None:
    source = Path(dockerfile).read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock ./" in source
    assert "uv==0.12.0" in source
    assert "python:3.13.15-slim-trixie@sha256:" in source
    assert "pip==26.2.1" in source
    assert "uv sync --locked" in source
    assert "--no-dev" in source
    assert "--no-install-project" in source
    assert "pip install --no-cache-dir -r" not in source


def test_worker_image_installs_locked_cad_group_but_api_does_not() -> None:
    api = Path("services/api/Dockerfile").read_text(encoding="utf-8")
    worker = Path("services/worker/Dockerfile").read_text(encoding="utf-8")
    compose = Path("compose.yml").read_text(encoding="utf-8")

    assert "--group cad" not in api
    assert "--group cad" in worker
    assert '"worker", "--loglevel=INFO"' in worker
    assert '"--beat"' not in worker
    assert compose.count('"beat", "--schedule=/tmp/celerybeat-schedule"') == 1
    assert "  scheduler:" in compose
    assert '"--loglevel=WARNING"' in compose
    assert "-name 'celerybeat-schedule*' -mmin -5" in compose
    assert compose.count("image: custombuild-worker:${VCS_REF:-uncommitted}") == 2
    assert compose.count("INSTALL_FREECAD: ${INSTALL_FREECAD:-true}") == 2
    assert 'INSTALL_FREECAD: "false"' not in compose


@pytest.mark.parametrize(
    "dockerfile",
    ("services/api/Dockerfile", "services/worker/Dockerfile", "apps/web/Dockerfile"),
)
def test_application_images_embed_oci_release_provenance(dockerfile: str) -> None:
    source = Path(dockerfile).read_text(encoding="utf-8")

    for argument in (
        "APP_VERSION",
        "VCS_REF",
        "BUILD_DATE",
        "SOURCE_URL",
        "SOURCE_MANIFEST_SHA256",
    ):
        assert f"ARG {argument}=" in source
        assert f"{argument}=${{{argument}}}" in source
    for label in (
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.created",
        "org.opencontainers.image.source",
        "io.custombuild.source-manifest.sha256",
    ):
        assert label in source


@pytest.mark.parametrize("dockerfile", ("services/api/Dockerfile", "services/worker/Dockerfile"))
def test_python_images_bind_the_verified_uv_lock(dockerfile: str) -> None:
    source = Path(dockerfile).read_text(encoding="utf-8")

    assert "ARG DEPENDENCY_LOCK_SHA256=unknown" in source
    assert "DEPENDENCY_LOCK_SHA256=${DEPENDENCY_LOCK_SHA256}" in source
    assert "io.custombuild.dependency-lock.sha256" in source
    assert "sha256sum uv.lock" in source
    assert 'DEPENDENCY_LOCK_SHA256" != "unknown' in source


def test_web_image_binds_the_verified_frontend_lock() -> None:
    source = Path("apps/web/Dockerfile").read_text(encoding="utf-8")

    assert source.count("ARG FRONTEND_LOCK_SHA256=unknown") == 2
    assert "FRONTEND_LOCK_SHA256=${FRONTEND_LOCK_SHA256}" in source
    assert "io.custombuild.frontend-lock.sha256" in source
    assert "sha256sum pnpm-lock.yaml" in source
    assert "sha256sum uv.lock" not in source


def test_web_typecheck_regenerates_canonical_next_types() -> None:
    package = Path("apps/web/package.json").read_text(encoding="utf-8")

    assert '"typecheck": "next typegen && tsc --noEmit"' in package


def test_compose_sets_resource_limits_and_passes_release_identity() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")

    assert "x-release-build-args: &release-build-args" in compose
    assert "x-python-release-build-args: &python-release-build-args" in compose
    assert "DEPENDENCY_LOCK_SHA256: ${DEPENDENCY_LOCK_SHA256:-unknown}" in compose
    assert "SOURCE_MANIFEST_SHA256: ${SOURCE_MANIFEST_SHA256:-unknown}" in compose
    assert "FRONTEND_LOCK_SHA256: ${FRONTEND_LOCK_SHA256:-unknown}" in compose
    assert compose.count("mem_limit:") == 9
    assert compose.count("cpus:") == 9
    assert compose.count("*python-release-build-args") == 4
    assert compose.count("*release-build-args") == 2


def test_one_image_tag_never_has_conflicting_build_definitions() -> None:
    config = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
    definitions_by_image: dict[str, list[object]] = {}
    for service in config["services"].values():
        image = service.get("image")
        build = service.get("build")
        if isinstance(image, str) and build is not None:
            definitions_by_image.setdefault(image, []).append(build)

    for image, definitions in definitions_by_image.items():
        assert all(definition == definitions[0] for definition in definitions), image


def test_postgres_healthcheck_proves_the_runtime_role_contract() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")
    healthcheck = Path("infra/postgres/healthcheck.sh").read_text(encoding="utf-8")

    assert "custombuild-postgres-healthcheck" in compose
    assert "start_period: 5s" in compose
    assert "pg_isready" in healthcheck
    assert "custombuild_bootstrap" in healthcheck
    assert "custombuild_migrator" in healthcheck
    assert "rolbypassrls" in healthcheck
    assert "pg_has_role" in healthcheck


def test_all_external_container_images_are_digest_pinned() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")
    web = Path("apps/web/Dockerfile").read_text(encoding="utf-8")

    external_images = [
        line.strip().removeprefix("image:").strip()
        for line in compose.splitlines()
        if line.strip().startswith("image:") and "custombuild-" not in line
    ]
    assert external_images
    assert all("@sha256:" in image for image in external_images)
    assert web.count("@sha256:") == 2
    assert "ARG NODE_VERSION=24.19.0" in web
    assert "/usr/local/lib/node_modules/npm" in web
    assert "apt-get upgrade --yes" in web


def test_ci_uses_the_same_node_release_as_the_web_image() -> None:
    web = Path("apps/web/Dockerfile").read_text(encoding="utf-8")
    expected = "24.19.0"

    assert f"ARG NODE_VERSION={expected}" in web
    for workflow in (".github/workflows/ci.yml", ".github/workflows/prod-ci.yml"):
        source = Path(workflow).read_text(encoding="utf-8")
        assert "node-version: 24.18.1" not in source
        assert f"node-version: {expected}" in source


def test_seaweedfs_runtime_is_source_verified_and_shell_free() -> None:
    dockerfile = Path("infra/seaweedfs/Dockerfile").read_text(encoding="utf-8")
    compose = Path("compose.yml").read_text(encoding="utf-8")

    assert "golang:1.25.14-alpine3.24@sha256:" in dockerfile
    assert "ENV GOTOOLCHAIN=local" in dockerfile
    assert (
        "ADD --checksum=sha256:"
        "6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd"
    ) in dockerfile
    assert dockerfile.count("-mod=readonly") == 2
    assert "FROM scratch AS runtime" in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert 'test: ["CMD", "/usr/local/bin/healthcheck"]' in compose
    assert "object-storage-init:" in compose


@pytest.mark.parametrize(
    ("dockerfile", "snapshot"),
    (
        ("apps/web/Dockerfile", "20260825T000000Z"),
        ("services/api/Dockerfile", "20260825T000000Z"),
        ("services/worker/Dockerfile", "20260825T000000Z"),
    ),
)
def test_apt_layers_use_a_dated_debian_snapshot(dockerfile: str, snapshot: str) -> None:
    source = Path(dockerfile).read_text(encoding="utf-8")

    assert f"ARG DEBIAN_SNAPSHOT={snapshot}" in source
    assert "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" in source
    assert "snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" in source
    assert "Acquire::Check-Valid-Until" in source
    assert "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" in source
    assert "apt-get update" in source
