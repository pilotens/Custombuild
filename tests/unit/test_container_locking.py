from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml


def test_web_builder_includes_the_walnut_customer_example() -> None:
    dockerfile = Path("apps/web/Dockerfile").read_text(encoding="utf-8")
    before_build = dockerfile.split("FROM dependencies AS builder", 1)[1].split("pnpm --dir", 1)[0]
    example = Path("examples/furniture/walnut-shelving-4340x2540x280.json")
    assert f"COPY {example.as_posix()} examples/furniture/" in before_build.splitlines()
    assert example.is_file()


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
        "python:3.13.18-slim-trixie@sha256:"
        "09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc"
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
    generation_command = [
        "python",
        "-m",
        "custombuild_worker.generation_startup",
        "--loglevel=INFO",
        "--concurrency=2",
        "--queues=generation",
    ]

    assert "--group cad" not in api
    assert "--group cad" in worker
    assert (
        'CMD ["python", "-m", "custombuild_worker.generation_startup", '
        '"--loglevel=INFO", "--concurrency=2", "--queues=generation"]'
        in worker
    )
    assert '"--beat"' not in worker
    assert compose.count('"beat", "--schedule=/tmp/celerybeat-schedule"') == 1
    assert "  scheduler:" in compose
    assert '"--loglevel=WARNING"' in compose
    assert compose_config["services"]["worker"]["command"] == generation_command
    assert (
        compose_config["services"]["worker"]["environment"]["CELERY_EXPECTED_QUEUE"]
        == "generation"
    )
    scheduler_probe = compose_config["services"]["scheduler"]["healthcheck"]["test"]
    assert scheduler_probe[:3] == ["CMD", "/opt/custombuild-venv/bin/python", "-c"]
    assert "celerybeat-schedule*" in scheduler_probe[3]
    assert "now-path.stat().st_mtime <= 300" in scheduler_probe[3]
    assert compose.count("image: custombuild-worker:${VCS_REF:-uncommitted}") == 4
    assert '"--queues=generation"' in compose
    assert '"--queues=maintenance"' in compose
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


def test_web_public_configuration_is_runtime_only() -> None:
    dockerfile = Path("apps/web/Dockerfile").read_text(encoding="utf-8")
    compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
    external = yaml.safe_load(Path("compose.external-production.yml").read_text(encoding="utf-8"))
    runtime_keys = {
        "CUSTOMBUILD_WEB_API_URL",
        "CUSTOMBUILD_WEB_DEMO_TOKEN",
        "CUSTOMBUILD_WEB_OIDC_ISSUER",
        "CUSTOMBUILD_WEB_OIDC_CLIENT_ID",
        "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
    }

    assert "NEXT_PUBLIC_" not in dockerfile
    assert "APP_ENV=production" in dockerfile
    assert runtime_keys.isdisjoint(compose["services"]["web"]["build"]["args"])
    assert runtime_keys <= set(compose["services"]["web"]["environment"])
    assert runtime_keys.isdisjoint(external["services"]["web"]["build"]["args"])
    assert runtime_keys <= set(external["services"]["web"]["environment"])
    assert external["services"]["web"]["environment"]["APP_ENV"] == "production"
    assert external["services"]["web"]["environment"]["CUSTOMBUILD_WEB_DEMO_TOKEN"] == ""
    web_probe = compose["services"]["web"]["healthcheck"]["test"]
    assert "http://127.0.0.1:3000/" in web_probe[-1]
    assert "response.statusCode === 200" in web_probe[-1]


def test_web_image_smoke_covers_valid_and_blank_runtime_configuration() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    positive = workflow.index("- name: Smoke production web image")
    negative = workflow.index("- name: Reject blank production web runtime")

    assert positive < negative
    positive_smoke = workflow[positive:negative]
    for required in (
        "--env APP_ENV=production",
        "--env CUSTOMBUILD_WEB_API_URL=https://api.ci.invalid",
        "--env CUSTOMBUILD_WEB_DEMO_TOKEN=",
        "--env CUSTOMBUILD_WEB_OIDC_ISSUER=https://identity.ci.invalid/",
        "--env CUSTOMBUILD_WEB_OIDC_CLIENT_ID=custombuild-web",
        "--env CUSTOMBUILD_WEB_OIDC_REDIRECT_URI=https://app.ci.invalid/",
    ):
        assert required in positive_smoke

    negative_smoke = workflow[negative:]
    assert "--env APP_ENV=" in negative_smoke
    assert ".State.Health.Status" in negative_smoke
    assert 'if [ "$health" != unhealthy ]' in negative_smoke


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
    assert compose.count("mem_limit:") == 13
    assert compose.count("cpus:") == 13
    assert compose.count("*python-release-build-args") == 8
    assert compose.count("*release-build-args") == 4
    assert "POSTGRES_RECIPE_SHA256:-unknown" in compose
    assert "REDIS_RECIPE_SHA256:-unknown" in compose


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
    assert "ARG NODE_VERSION=24.22.2" in web
    assert (
        "node:24.22.2-trixie-slim@sha256:"
        "0820eaa85e07d0490446dd01d14707e17e6108a36988cf374899cd057eb3812d"
    ) in web
    assert (
        "cgr.dev/chainguard/glibc-dynamic:latest@sha256:"
        "aab5cd0b4cc5edd9c87b6e7aa9ddaa3fb6ffcec559b9e5473c55bbc937b65399"
    ) in web
    assert (
        "cgr.dev/chainguard/python:latest-dev@sha256:"
        "64f414bffa25ca9ff518f35975ad153a921b2e4850c4a84c31a96885fdb86a02"
    ) in web
    for package in (
        "c-ares=1.34.9-r0",
        "libbrotlicommon1=1.2.1-r3",
        "libbrotlidec1=1.2.1-r3",
        "libbrotlienc1=1.2.1-r3",
        "openssl-4.0-libcrypto=4.0.3-r8",
        "icu79-data-full=79.1-r1",
        "libicu79=79.1-r1",
        "libnghttp2-14=1.71.0-r0",
        "openssl-4.0-libssl=4.0.3-r8",
        "libuv=1.53.0-r0",
        "zlib=1.3.2.1_rc20260921-r2",
        "nodejs-24=24.22.2-r2",
    ):
        assert package in web
    assert "COPY --from=base /usr/local/bin/node" not in web
    assert "COPY --link --from=runtime-assembler /base-chroot /" in web
    assert 'ENTRYPOINT ["/usr/bin/node"]' in web
    assert 'CMD ["/usr/bin/node", "-e"' in web
    assert "require('node:inspector')" in web
    assert "process.version !== 'v24.22.2'" in web
    assert "apt-get" not in web


def test_external_runtime_pins_match_scans_backups_and_release_descriptors() -> None:
    from scripts.compose_backup import VOLUME_INIT_IMAGE
    from scripts.deploy_descriptor import (
        PINNED_RUNTIME_IMAGES,
        PINNED_RUNTIME_SCAN_REFERENCES,
    )
    from scripts.release_evidence_gate import EXTERNAL_IMAGE_REFERENCES

    compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
    registry = yaml.safe_load(Path("compose.registry.yml").read_text(encoding="utf-8"))
    workflow_path = Path(".github/workflows/supply-chain.yml")
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    scan_matrix = workflow["jobs"]["pinned-runtime-images"]["strategy"]["matrix"]["include"]
    scan_images = {entry["component"]: entry["image"] for entry in scan_matrix}

    assert scan_images == PINNED_RUNTIME_SCAN_REFERENCES == EXTERNAL_IMAGE_REFERENCES
    assert compose["services"]["postgres"]["build"]["dockerfile"] == "infra/postgres/Dockerfile"
    assert scan_images["volume-init"] == VOLUME_INIT_IMAGE
    for component, scan_reference in scan_images.items():
        service = "object-storage-init" if component == "volume-init" else component
        assert compose["services"][service]["image"] == scan_reference
        assert registry["services"][service]["image"] == PINNED_RUNTIME_IMAGES[component]
        release_digest = PINNED_RUNTIME_IMAGES[component].partition("@")[2]
        assert scan_reference.partition("@")[2] == release_digest


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
        "aab5cd0b4cc5edd9c87b6e7aa9ddaa3fb6ffcec559b9e5473c55bbc937b65399"
    ) in source
    assert (
        "cgr.dev/chainguard/python:latest-dev@sha256:"
        "64f414bffa25ca9ff518f35975ad153a921b2e4850c4a84c31a96885fdb86a02"
    ) in source
    for package in (
        "py3-pip-wheel=26.2.2-r2",
        "libbz2-1=1.0.8-r30",
        "openssl-4.0-libcrypto=4.0.3-r8",
        "libexpat1=2.10.0-r1",
        "libffi=3.8.0-r2",
        "gdbm=1.26-r9",
        "xz=5.8.4-r1",
        "mpdecimal=4.0.1-r6",
        "ncurses-terminfo-base=6.6.20261031-r0",
        "ncurses=6.6.20261031-r0",
        "readline=8.3-r3",
        "sqlite-libs=3.54.1-r0",
        "openssl-4.0-libssl=4.0.3-r8",
        "libuuid=2.42.4-r2",
        "zlib=1.3.2.1_rc20260921-r2",
        "python-3.13-base=3.13.18_git20261030-r0",
        "python-3.13=3.13.18_git20261030-r0",
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
    assert 'io.custombuild.python.version="3.13.18"' in source
    assert "ENTRYPOINT []" in source
    assert "USER 65532:65532" in source
    assert "assert sys.version_info[:3] == (3, 13, 18)" in source
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
            20,
            "c63f4ad041b38a0f30df9f4a18e8dfac4ad2411840ff114d32dc57ce2d8ce4ec",
        ),
        (
            "services/api/Dockerfile",
            28,
            "7c3ee343ab7b5ce429bf3e4261742aee04b8e80151a459ed76dd30cc25ea6dc2",
        ),
        (
            "services/worker/Dockerfile",
            53,
            "aefade11386262b51c3f6b9a2f7129b2ba0a3e6cc04ee8b2a0a9828cbfa226b7",
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
        "64f414bffa25ca9ff518f35975ad153a921b2e4850c4a84c31a96885fdb86a02"
    ) in worker
    assert "COPY --from=runtime-base / /base-chroot" in worker
    assert "apk add --no-cache --no-scripts --root /base-chroot" in worker
    assert "COPY --link --from=runtime-assembler /base-chroot /" in worker
    for package in (
        "mesa-gl=26.2.6-r0",
        "libx11=1.8.14-r3",
        "libxau=1.0.12-r11",
        "libxdmcp=1.1.5-r14",
        "libbsd=0.12.2-r11",
        "libmd=1.3.0-r2",
        "libgomp=16.2.0-r2",
        "libxcb=1.17.0-r19",
        "libglvnd=1.7.0-r12",
        "libxml2=2.15.0-r0",
        "libLLVM-23=23.1.5-r0",
        "libpciaccess=0.19-r4",
        "libdrm=2.4.134-r0",
        "libzstd1=1.5.7-r13",
        "libelf=0.197-r0",
        "libxshmfence=1.3.3-r7",
        "mesa-libgallium=26.2.6-r0",
        "mesa-gbm=26.2.6-r0",
        "libudev=262.2-r1",
        "wayland-libs-client=1.26.0-r3",
        "wayland-protocols=1.50-r0",
        "mesa=26.2.6-r0",
        "libxext=1.3.7-r4",
        "libxxf86vm=1.1.7-r5",
        "mesa-glx=26.2.6-r0",
    ):
        assert package in worker


def test_ci_uses_the_same_node_release_as_the_web_image() -> None:
    web = Path("apps/web/Dockerfile").read_text(encoding="utf-8")
    expected = "24.22.2"

    assert f"ARG NODE_VERSION={expected}" in web
    for workflow in (".github/workflows/ci.yml", ".github/workflows/prod-ci.yml"):
        source = Path(workflow).read_text(encoding="utf-8")
        assert "node-version: 24.18.1" not in source
        assert f"node-version: {expected}" in source


def test_supply_chain_scans_pin_the_reviewed_grype_toolchain() -> None:
    workflow = Path(".github/workflows/supply-chain.yml").read_text(encoding="utf-8")
    reviewed_action = "anchore/scan-action@e49c028b8f5d4ac63b87309b024ea6faceb6bac3"

    assert workflow.count("uses: anchore/scan-action@") == 3
    assert workflow.count(f"uses: {reviewed_action}") == 3
    assert workflow.count("grype-version: v0.110.0") == 3
    assert "e1165082ffb1fe366ebaf02d8526e7c4989ea9d2" not in workflow


def test_seaweedfs_runtime_is_source_verified_and_shell_free() -> None:
    dockerfile = Path("infra/seaweedfs/Dockerfile").read_text(encoding="utf-8")
    overrides = Path("infra/seaweedfs/security-overrides.sum").read_bytes()
    compose = Path("compose.yml").read_text(encoding="utf-8")
    compose_config = yaml.safe_load(compose)
    workflow = Path(".github/workflows/supply-chain.yml").read_text(encoding="utf-8")

    assert (
        "golang:1.26.9-alpine3.24@sha256:"
        "3082400e369fa24d5fc60bca20edab3f6d604e0c5a690ec66b295eff4dd87ade"
    ) in dockerfile
    assert "ENV GOTOOLCHAIN=local" in dockerfile
    assert (
        "ADD --checksum=sha256:800a91693e2a4e974ef0e2a157b6a04eff9f4df507303b1cff31b0601081fc30"
    ) in dockerfile
    overrides_sha256 = hashlib.sha256(overrides).hexdigest()
    assert overrides_sha256 == "b03ac9c9a2b22047f2bc0ed6d2a8a6945af8118cca0bf5237740b3e11609cac1"
    assert f"ARG SEAWEEDFS_SECURITY_OVERRIDES_SHA256={overrides_sha256}" in dockerfile
    assert "github.com/apache/thrift@v0.24.0" in dockerfile
    assert "go.etcd.io/etcd/client/pkg/v3@v3.7.1" in dockerfile
    assert "golang.org/x/crypto@v0.58.0" in dockerfile
    assert "golang.org/x/image@v0.45.0" in dockerfile
    assert "golang.org/x/text@v0.43.0" in dockerfile
    assert "google.golang.org/grpc@v1.85.0-dev.0.20260825072537-93e31b48545e" in dockerfile
    assert "go mod edit -dropreplace=github.com/apache/thrift" in dockerfile
    assert 'go.etcd.io/etcd/client/pkg/v3)" = "v3.7.1"' in dockerfile
    assert 'golang.org/x/crypto)" = "v0.55.0"' in dockerfile
    assert 'golang.org/x/image)" = "v0.44.0"' in dockerfile
    assert dockerfile.count("-mod=readonly") == 14
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
        'test "$go_version" = "1.26.9"',
        'test "$apache_thrift_version" = "0.24.0"',
        'test "$etcd_client_pkg_version" = "3.7.1"',
        'test "$x_crypto_version" = "0.58.0"',
        'test "$x_image_version" = "0.45.0"',
        'test "$x_text_version" = "0.43.0"',
        'test "$grpc_version" = "v1.85.0-dev.0.20260825072537-93e31b48545e"',
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
    assert "https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" in source
    assert "https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" in source
    assert "Acquire::Check-Valid-Until" in source
    assert "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" in source
    assert "apt-get update" in source
