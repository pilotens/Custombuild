from pathlib import Path

PROD_CI = Path(".github/workflows/prod-ci.yml")


def _workflow() -> str:
    return PROD_CI.read_text(encoding="utf-8")


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
