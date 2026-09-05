from pathlib import Path

import yaml

PROD_CI = Path(".github/workflows/prod-ci.yml")
REVIEWED_GRYPE_SCAN_ACTION = "anchore/scan-action@e49c028b8f5d4ac63b87309b024ea6faceb6bac3"
REVIEWED_GRYPE_VERSION = "v0.110.0"


def _workflow() -> str:
    return PROD_CI.read_text(encoding="utf-8")


def test_release_workflows_use_identical_sha_bound_linuxcnc_oracle() -> None:
    paths = (
        Path(".github/workflows/ci.yml"),
        Path(".github/workflows/cd.yml"),
        PROD_CI,
    )
    jobs = {
        path: yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"] for path in paths
    }
    oracle_jobs = [jobs[path]["linuxcnc-interpreter-oracle"] for path in paths]

    assert all(job == oracle_jobs[0] for job in oracle_jobs[1:])
    oracle = oracle_jobs[0]
    assert oracle["container"]["image"] == (
        "debian:trixie-slim@sha256:"
        "abc9cb88a5587630d7f915f47b23b0668fe250fbfc6457aa4d52b534c1bbf73f"
    )
    assert oracle["timeout-minutes"] == 20
    assert oracle["env"] == {"DEBIAN_FRONTEND": "noninteractive"}
    assert len(oracle["steps"]) == 4
    install = oracle["steps"][0]["run"]
    snapshot = "20260824T000000Z"
    assert install.count(snapshot) == 2
    assert f"snapshot.debian.org/archive/debian/{snapshot}" in install
    assert f"snapshot.debian.org/archive/debian-security/{snapshot}" in install
    assert 'Acquire::Check-Valid-Until "false";' in install
    assert "linuxcnc-uspace=1:2.9.4-2+deb13u1" in install
    assert oracle["steps"][1]["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    assert oracle["steps"][-1]["run"] == (
        "python3 scripts/verify_linuxcnc_interpreter_oracle.py"
    )
    assert oracle["steps"][2] == {
        "name": "Verify immutable repository content root",
        "run": "python3 scripts/source_manifest.py --repo . --check-production-semantic-root",
    }
    ci_python = jobs[paths[0]]["python"]
    assert ci_python["needs"] == "linuxcnc-interpreter-oracle"
    assert ci_python["if"] == "${{ always() }}"
    assert ci_python["steps"][0] == {
        "name": "Require LinuxCNC interpreter oracle",
        "if": "${{ needs.linuxcnc-interpreter-oracle.result != 'success' }}",
        "run": (
            'echo "LinuxCNC interpreter oracle did not pass for this exact commit"\n'
            "exit 1\n"
        ),
    }
    assert jobs[paths[1]]["test-release-bundle"]["needs"] == [
        "quality-evidence",
        "build-images",
        "linuxcnc-interpreter-oracle",
    ]
    assert jobs[PROD_CI]["prod-compose-acceptance"]["needs"] == [
        "prod-quality",
        "linuxcnc-interpreter-oracle",
    ]


def test_ci_external_overlay_render_binds_its_profile_file_and_digest() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["compose-acceptance"]["steps"]
    step = next(
        item
        for item in steps
        if item.get("name") == "Validate fail-closed external production overlay"
    )

    assert step["env"]["PRODUCTION_CAM_PROFILE_HOST_PATH"] == (
        "/tmp/custombuild-production-cam-profile.json"  # noqa: S108 - fixed CI fixture path
    )
    assert step["env"]["PRODUCTION_CAM_PROFILE_SHA256"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert 'touch "$PRODUCTION_CAM_PROFILE_HOST_PATH"' in step["run"]
    assert 'sha256sum "$PRODUCTION_CAM_PROFILE_HOST_PATH"' in step["run"]
    assert '"$PRODUCTION_CAM_PROFILE_SHA256"' in step["run"]


def test_release_browser_evidence_uses_direct_playwright_cli() -> None:
    release_workflows = (
        Path(".github/workflows/cd.yml"),
        Path(".github/workflows/prod-ci.yml"),
    )
    chromium_command = (
        "pnpm --dir apps/web exec playwright test --project=chromium-desktop"
    )
    wcag_command = (
        "pnpm --dir apps/web exec playwright test e2e/accessibility.spec.ts "
        "--project=chromium-desktop --output=wcag-test-results"
    )

    for path in release_workflows:
        source = path.read_text(encoding="utf-8")
        assert source.count(chromium_command) == 1
        assert source.count(wcag_command) == 1
        assert "test:e2e -- --project" not in source
        assert "test:e2e:a11y -- --output" not in source


def test_prod_ci_probes_the_exact_executed_native_runtimes() -> None:
    workflow = _workflow()

    build = workflow.index("- name: Build and start prod stack")
    probe = workflow.index("- name: Verify exact native runtimes and excluded tooling")
    acceptance = workflow.index("- name: Prod design-review and blocked CAM/release acceptance")
    assert build < probe < acceptance

    for required in (
        "exec -T web /usr/bin/node --version",
        '"v24.19.0"',
        "process.execPath !== '/usr/bin/node'",
        "exec -T api /opt/custombuild-venv/bin/python -",
        "exec -T worker /opt/custombuild-venv/bin/python -",
        "assert sys.version_info[:3] == (3, 13, 15)",
        'Path("/usr/bin/python3.13").resolve()',
        "import uvicorn",
        "from app.main import app",
        "import cadquery as cq",
        "import OCP",
        "import vtkmodules.vtkCommonCore",
        "from custombuild_worker.tasks import celery_app",
        'cq.Workplane("XY").box(10, 20, 30).val()',
        'importlib.util.find_spec("FreeCAD") is None',
        '"pip3.13"',
        '"uvx"',
        '"FreeCADCmd"',
        '"freecadcmd"',
        "assert shutil.which(executable) is None",
    ):
        assert required in workflow


def test_prod_ci_sboms_prove_native_interpreter_package_versions() -> None:
    workflow = _workflow()

    generated = workflow.index("- name: Generate exact volume-init runtime SPDX SBOM")
    verified = workflow.index("- name: Verify native interpreter packages in exact runtime SBOMs")
    scanned = workflow.index("- name: Scan exact API runtime image")
    assert generated < verified < scanned

    for required in (
        "require_exact_package",
        "reject_package_prefix",
        "sbom-web.spdx.json",
        "nodejs-24 24.19.0-r0",
        "nodejs-26",
        "sbom-${component}.spdx.json",
        "python-3.13 3.13.15-r2",
        "python-3.13-base 3.13.15-r2",
        "python-3.14",
    ):
        assert required in workflow


def test_prod_ci_pins_every_scan_to_the_reviewed_grype_toolchain() -> None:
    workflow = _workflow()

    assert workflow.count("uses: anchore/scan-action@") == 7
    assert workflow.count(f"uses: {REVIEWED_GRYPE_SCAN_ACTION}") == 7
    assert workflow.count(f"grype-version: {REVIEWED_GRYPE_VERSION}") == 7
    assert "e1165082ffb1fe366ebaf02d8526e7c4989ea9d2" not in workflow


def test_prod_ci_worker_sbom_proves_the_exact_native_cad_runtime() -> None:
    workflow = _workflow()
    worker = Path("services/worker/Dockerfile").read_text(encoding="utf-8")
    native_packages = (
        "mesa-gl 26.2.1-r0",
        "libx11 1.8.13-r5",
        "libxau 1.0.12-r7",
        "libxdmcp 1.1.5-r9",
        "libbsd 0.12.2-r7",
        "libmd 1.2.0-r2",
        "libgomp 16.2.0-r1",
        "libxcb 1.17.0-r15",
        "libglvnd 1.7.0-r10",
        "libxml2 2.15.0-r0",
        "libLLVM-22 22.1.8-r2",
        "libpciaccess 0.19-r2",
        "libdrm 2.4.134-r0",
        "libzstd1 1.5.7-r8",
        "libelf 0.195-r2",
        "libxshmfence 1.3.3-r2",
        "mesa-libgallium 26.2.1-r0",
        "mesa-gbm 26.2.1-r0",
        "libudev 261.2-r1",
        "wayland-libs-client 1.26.0-r0",
        "wayland-protocols 1.49-r0",
        "mesa 26.2.1-r0",
        "libxext 1.3.7-r0",
        "libxxf86vm 1.1.7-r2",
        "mesa-glx 26.2.1-r0",
    )

    assert '"$RELEASE_EVIDENCE_DIR/sbom-worker.spdx.json"' in workflow
    assert "done <<'PACKAGES'" in workflow
    assert len(native_packages) == 25
    for package in native_packages:
        assert f"          {package}\n" in workflow
        assert package.replace(" ", "=", 1) in worker


def test_operations_requires_logical_postgres_17_to_18_migration() -> None:
    operations = Path("docs/OPERATIONS.md").read_text(encoding="utf-8")

    assert "Never attach it to a\nPostgreSQL 17 data volume" in operations
    assert "logical custom-format backup" in operations
    assert "fresh PostgreSQL 18 volume" in operations
    assert "complete restore drill" in operations
