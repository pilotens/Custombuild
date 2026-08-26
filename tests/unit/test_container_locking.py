from __future__ import annotations

import hashlib
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
    assert (
        "python:3.13.15-slim-trixie@sha256:"
        "7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4"
    ) in source
    assert "pip==26.2.1" in source
    assert "uv sync --locked" in source
    assert "--no-dev" in source
    assert "--no-install-project" in source
    assert "pip install --no-cache-dir -r" not in source


def test_worker_image_installs_locked_cad_group_but_api_does_not() -> None:
    api = Path("services/api/Dockerfile").read_text(encoding="utf-8")
    worker = Path("services/worker/Dockerfile").read_text(encoding="utf-8")
    compose = Path("compose.yml").read_text(encoding="utf-8")
    compose_config = yaml.safe_load(compose)

    assert "--group cad" not in api
    assert "--group cad" in worker
    assert '"worker", "--loglevel=INFO"' in worker
    assert '"--beat"' not in worker
    assert compose.count('"beat", "--schedule=/tmp/celerybeat-schedule"') == 1
    assert "  scheduler:" in compose
    assert '"--loglevel=WARNING"' in compose
    scheduler_probe = compose_config["services"]["scheduler"]["healthcheck"]["test"]
    assert scheduler_probe[:3] == ["CMD", "/opt/custombuild-venv/bin/python", "-c"]
    assert "celerybeat-schedule*" in scheduler_probe[3]
    assert "now-path.stat().st_mtime <= 300" in scheduler_probe[3]
    assert compose.count("image: custombuild-worker:${VCS_REF:-uncommitted}") == 2
    assert "INSTALL_FREECAD" not in compose
    assert "freecad-python3" not in worker
    assert 'io.custombuild.freecad.runtime="not-installed-fail-closed"' in worker


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
    assert web.count("@sha256:") == 3
    assert "ARG NODE_VERSION=24.19.0" in web
    assert (
        "node:24.19.0-trixie-slim@sha256:"
        "ab3eebe934147fee049b5eb83c570f68c849a13c930bdfa482de99fcdfa3b3de"
    ) in web
    assert (
        "cgr.dev/chainguard/glibc-dynamic:latest@sha256:"
        "205572d5e48117e14b44b42627890fa8d3e8e65bb37a80abb3317e5151e7f35b"
    ) in web
    assert (
        "cgr.dev/chainguard/python:latest-dev@sha256:"
        "f6d6485f11a65ca81d8a2d01eae564fa88937e7d19c1cf216cdb1142980c51bd"
    ) in web
    for package in (
        "c-ares=1.34.8-r1",
        "libbrotlicommon1=1.2.0-r3",
        "libbrotlidec1=1.2.0-r3",
        "libbrotlienc1=1.2.0-r3",
        "libcrypto3=3.6.4-r0",
        "icu78-data-full=78.3-r2",
        "libicu78=78.3-r2",
        "libnghttp2-14=1.70.0-r2",
        "libssl3=3.6.4-r0",
        "libuv=1.52.1-r1",
        "zlib=1.3.2-r4",
        "nodejs-24-minimal=24.19.0-r0",
    ):
        assert package in web
    assert "COPY --from=base /usr/local/bin/node" not in web
    assert "COPY --link --from=runtime-assembler /base-chroot /" in web
    assert 'ENTRYPOINT ["/usr/bin/node"]' in web
    assert 'CMD ["/usr/bin/node", "-e"' in web
    assert "process.version !== 'v24.19.0'" in web
    assert "apt-get" not in web


def test_web_runtime_uid_owns_its_only_writable_application_cache() -> None:
    web = Path("apps/web/Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))

    assert "USER 65532:65532" in web
    assert (
        "/app/apps/web/.next/cache:size=128m,uid=65532,gid=65532,mode=0750"
        in compose["services"]["web"]["tmpfs"]
    )


@pytest.mark.parametrize("dockerfile", ("services/api/Dockerfile", "services/worker/Dockerfile"))
def test_python_runtimes_are_minimal_pinned_and_keep_python_313(dockerfile: str) -> None:
    source = Path(dockerfile).read_text(encoding="utf-8")

    assert (
        "cgr.dev/chainguard/glibc-dynamic:latest@sha256:"
        "205572d5e48117e14b44b42627890fa8d3e8e65bb37a80abb3317e5151e7f35b"
    ) in source
    assert (
        "cgr.dev/chainguard/python:latest-dev@sha256:"
        "f6d6485f11a65ca81d8a2d01eae564fa88937e7d19c1cf216cdb1142980c51bd"
    ) in source
    for package in (
        "py3-pip-wheel=26.2.1-r0",
        "libbz2-1=1.0.8-r23",
        "libcrypto3=3.6.4-r0",
        "libexpat1=2.8.3-r0",
        "libffi=3.8.0-r0",
        "gdbm=1.26-r5",
        "xz=5.8.3-r2",
        "mpdecimal=4.0.1-r3",
        "ncurses-terminfo-base=6.6.20260822-r0",
        "ncurses=6.6.20260822-r0",
        "readline=8.3-r2",
        "sqlite-libs=3.53.4-r0",
        "libssl3=3.6.4-r0",
        "libuuid=2.42.2-r3",
        "zlib=1.3.2-r4",
        "python-3.13-base=3.13.15-r2",
        "python-3.13=3.13.15-r2",
    ):
        assert package in source
    builder_copy_lines = [
        line
        for line in source.splitlines()
        if line.startswith("COPY ") and "--from=builder" in line
    ]
    assert builder_copy_lines
    assert all("/usr/local" not in line for line in builder_copy_lines)
    assert "COPY --from=builder /opt/custombuild-venv /base-chroot/opt/custombuild-venv" in source
    assert "COPY --from=builder /app/uv.lock /base-chroot/app/uv.lock" in source
    assert "ln -s /usr/bin/python3.13 /base-chroot/opt/custombuild-venv/bin/python" in source
    assert "sed -i 's|^home = .*|home = /usr/bin|'" in source
    assert "COPY --link --from=runtime-assembler /base-chroot /" in source
    assert "FROM scratch AS runtime" in source
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in source
    assert 'io.custombuild.python.version="3.13.15"' in source
    assert "ENTRYPOINT []" in source
    assert "USER 65532:65532" in source
    assert "assert sys.version_info[:3] == (3, 13, 15)" in source
    assert "assert ssl.create_default_context().get_ca_certs()" in source
    assert "assert importlib.util.find_spec('pip') is None" in source
    assert "assert importlib.util.find_spec('uv') is None" in source
    assert "assert shutil.which('FreeCAD') is None" in source
    assert "test ! -e /base-chroot/opt/custombuild-venv/bin/pip" in source
    assert "test ! -e /base-chroot/opt/custombuild-venv/bin/uv" in source
    assert "test ! -e /base-chroot/usr/bin/FreeCAD" in source
    assert "/base-chroot/usr/local" in source
    assert "assert not pathlib.Path('/usr/local').exists()" in source
    assert "PATH=/opt/custombuild-venv/bin:/usr/bin:/usr/sbin:/sbin:/bin" in source


def test_python_runtime_build_probes_import_the_real_entrypoint_dependencies() -> None:
    api = Path("services/api/Dockerfile").read_text(encoding="utf-8")
    worker = Path("services/worker/Dockerfile").read_text(encoding="utf-8")

    assert "import uvicorn; import app.main" in api
    assert "import celery, cadquery, OCP, vtkmodules.vtkCommonCore" in worker
    assert "import custombuild_worker.tasks" in worker
    assert "cadquery.Workplane('XY').box(10, 20, 30)" in worker
    assert "solid.val().Volume() > 0" in worker
    assert "assert importlib.util.find_spec('FreeCAD') is None" in worker


@pytest.mark.parametrize(
    ("dockerfile", "package_count", "closure_sha256"),
    (
        (
            "apps/web/Dockerfile",
            19,
            "c1dd3f34a4e609705a82320975cb9bbac035fd9969f592d52fe58158689d4b4c",
        ),
        (
            "services/api/Dockerfile",
            24,
            "b877ad764a4b02b376bf61c2b62ebfc5044c1b1b70fb4a3a5ffca089be06bffb",
        ),
        (
            "services/worker/Dockerfile",
            49,
            "7d48b979148c77fbd4383671b6f0a79156fd85518840f217ef564091230f596e",
        ),
    ),
)
def test_wolfi_runtime_rejects_unreviewed_transitive_package_changes(
    dockerfile: str,
    package_count: int,
    closure_sha256: str,
) -> None:
    source = Path(dockerfile).read_text(encoding="utf-8")

    assert "/base-chroot/lib/apk/db/installed | sort" in source
    assert f'wc -l < /tmp/runtime-package-closure)" = "{package_count}"' in source
    assert closure_sha256 in source


def test_worker_runtime_packages_are_scanner_visible_and_exact() -> None:
    worker = Path("services/worker/Dockerfile").read_text(encoding="utf-8")

    assert (
        "cgr.dev/chainguard/python:latest-dev@sha256:"
        "f6d6485f11a65ca81d8a2d01eae564fa88937e7d19c1cf216cdb1142980c51bd"
    ) in worker
    assert "COPY --from=runtime-base / /base-chroot" in worker
    assert "apk add --no-cache --no-scripts --root /base-chroot" in worker
    assert "COPY --link --from=runtime-assembler /base-chroot /" in worker
    for package in (
        "mesa-gl=26.2.1-r0",
        "libx11=1.8.13-r5",
        "libxau=1.0.12-r7",
        "libxdmcp=1.1.5-r9",
        "libbsd=0.12.2-r7",
        "libmd=1.2.0-r2",
        "libgomp=16.2.0-r1",
        "libxcb=1.17.0-r15",
        "libglvnd=1.7.0-r10",
        "libxml2=2.15.0-r0",
        "libLLVM-22=22.1.8-r2",
        "libpciaccess=0.19-r2",
        "libdrm=2.4.134-r0",
        "libzstd1=1.5.7-r8",
        "libelf=0.195-r2",
        "libxshmfence=1.3.3-r2",
        "mesa-libgallium=26.2.1-r0",
        "mesa-gbm=26.2.1-r0",
        "libudev=261.2-r1",
        "wayland-libs-client=1.26.0-r0",
        "wayland-protocols=1.49-r0",
        "mesa=26.2.1-r0",
        "libxext=1.3.7-r0",
        "libxxf86vm=1.1.7-r2",
        "mesa-glx=26.2.1-r0",
    ):
        assert package in worker


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
    overrides = Path("infra/seaweedfs/security-overrides.sum").read_bytes()
    compose = Path("compose.yml").read_text(encoding="utf-8")
    compose_config = yaml.safe_load(compose)
    workflow = Path(".github/workflows/supply-chain.yml").read_text(encoding="utf-8")

    assert (
        "golang:1.26.6-alpine3.24@sha256:"
        "3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83"
    ) in dockerfile
    assert "ENV GOTOOLCHAIN=local" in dockerfile
    assert (
        "ADD --checksum=sha256:6928236b4703abd0fcb3d1391eeef3045277927ca3e501f4c69adc3306955fbd"
    ) in dockerfile
    overrides_sha256 = hashlib.sha256(overrides).hexdigest()
    assert overrides_sha256 == "1a96843ba71c16cee5c7e396a3082ab3ae0327ab429956db51d0d1b07f6508e5"
    assert f"ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256={overrides_sha256}" in dockerfile
    assert "go.etcd.io/etcd/client/pkg/v3@v3.6.14" in dockerfile
    assert "golang.org/x/image@v0.45.0" in dockerfile
    assert "golang.org/x/text@v0.41.0" in dockerfile
    assert 'go.etcd.io/etcd/client/pkg/v3)" = "v3.6.12"' in dockerfile
    assert 'golang.org/x/image)" = "v0.44.0"' in dockerfile
    assert dockerfile.count("-mod=readonly") == 7
    assert "go mod verify" in dockerfile
    assert "FROM scratch AS runtime" in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert "COPY --from=builder --chown=1000:1000 /out/tmp /tmp" in dockerfile
    assert "ENV TMPDIR=/tmp" in dockerfile
    assert 'test: ["CMD", "/usr/local/bin/healthcheck"]' in compose
    assert "object-storage-init:" in compose
    runtime_tmp = Path("/") / "tmp"
    assert (
        f"{runtime_tmp}:size=64m,mode=1777" in compose_config["services"]["object-storage"]["tmpfs"]
    )
    for expected in (
        'test "$go_version" = "1.26.6"',
        'test "$etcd_client_pkg_version" = "3.6.14"',
        'test "$x_image_version" = "0.45.0"',
        'test "$x_text_version" = "0.41.0"',
        f'test "$security_overrides_sha256" = "{overrides_sha256}"',
        "custombuild.seaweedfs-release.v2",
    ):
        assert expected in workflow


@pytest.mark.parametrize(
    ("dockerfile", "snapshot"),
    (
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
