import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.release_readiness as release_readiness
from scripts.deploy_descriptor import EXPECTED_REGISTRY_OVERLAY
from scripts.release_readiness import (
    PRODUCTION_SEMANTIC_SOURCE_PATHS,
    build_report,
    compose_hardening_issues,
    production_semantic_contract_issues,
    promotion_contract_issues,
    supply_chain_issues,
    vulnerability_exception_issues,
)


def test_report_cli_creates_the_output_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {"static_controls_ready": True}
    monkeypatch.setattr(release_readiness, "build_report", lambda *_args, **_kwargs: expected)
    output = tmp_path / "nested" / "readiness.json"

    monkeypatch.setattr(
        "sys.argv",
        ["release_readiness.py", "--repo", str(tmp_path), "--output", str(output)],
    )
    assert release_readiness.main() == 0
    assert json.loads(output.read_text(encoding="utf-8")) == expected


def test_checked_in_digest_promotion_contract_is_current() -> None:
    assert promotion_contract_issues(Path.cwd()) == []


def test_digest_promotion_contract_requires_descriptor_and_exact_overlay(
    tmp_path: Path,
) -> None:
    assert set(promotion_contract_issues(tmp_path)) == {
        ".github/workflows/cd.yml is missing",
        "compose.registry.yml is missing or unreadable",
        "scripts/deploy_descriptor.py is missing",
    }

    descriptor = tmp_path / "scripts/deploy_descriptor.py"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text("# verifier\n", encoding="utf-8")
    overlay = tmp_path / "compose.registry.yml"
    overlay.write_text(EXPECTED_REGISTRY_OVERLAY, encoding="utf-8")
    workflow = tmp_path / ".github/workflows/cd.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: build-once release\n", encoding="utf-8")

    assert promotion_contract_issues(tmp_path) == []

    overlay.write_text(
        EXPECTED_REGISTRY_OVERLAY.replace("--no-build", "--build", 1),
        encoding="utf-8",
    )

    assert promotion_contract_issues(tmp_path) == [
        "compose.registry.yml does not exactly bind descriptor images and --no-build"
    ]


SEMANTIC_FIXTURE_SOURCES = {
    PRODUCTION_SEMANTIC_SOURCE_PATHS[0]: """
PRODUCTION_MANIFEST_SCHEMA_VERSION = "custombuild.production-manifest.v4"
MANIFEST_CONTEXT_HASH_FIELDS = (
    "domain_template_version",
    "template_capability_version",
    "template_capability_registry_version",
    "release_scope",
    "machine_use",
    "physical_cutting_authorized",
)

def build_manifest():
    production_context = {
        "release_scope": "design_review",
        "machine_use": "validation_only",
        "physical_cutting_authorized": False,
    }
    manifest = {
        "schema_version": PRODUCTION_MANIFEST_SCHEMA_VERSION,
        **production_context,
        "production_context_hash": sha256_hex(canonical_json_bytes(production_context)),
    }
    return canonical_json_bytes(manifest)
""",
    PRODUCTION_SEMANTIC_SOURCE_PATHS[1]: """
WORKSHOP_READINESS_SCHEMA_VERSION = "custombuild.workshop-readiness.v2"
DESIGN_REVIEW_RELEASE_SCOPE = "design_review"
VALIDATION_ONLY_MACHINE_USE = "validation_only"

def build_workshop_readiness_report():
    return WorkshopReadinessReport(
        schema_version=WORKSHOP_READINESS_SCHEMA_VERSION,
        release_scope=DESIGN_REVIEW_RELEASE_SCOPE,
        machine_use=VALIDATION_ONLY_MACHINE_USE,
        physical_cutting_authorized=False,
    )
""",
    PRODUCTION_SEMANTIC_SOURCE_PATHS[2]: """
def _stock_id(*, role, material_id, material_version, thickness_um, width_um, height_um):
    return (
        f"stock-{role}-{material_id}-{material_version}-{thickness_um}um-"
        f"{width_um}x{height_um}um"
    )

def _generate():
    carcass_stock = StockSheet(
        stock_id=_stock_id(
            role="carcass",
            material_id="material",
            material_version="version",
            thickness_um=18000,
            width_um=2440000,
            height_um=1220000,
        ),
        grain_direction="UNBOUND",
    )
    stocks = [carcass_stock]
    stocks.append(StockSheet(
        stock_id=_stock_id(
            role="back",
            material_id="material",
            material_version="version",
            thickness_um=6000,
            width_um=2440000,
            height_um=1220000,
        ),
        grain_direction="UNBOUND",
    ))
    evidence_candidates = [
        artifact
        for artifact in bundle.artifacts
        if artifact.path
        in {"validation/stock-selection.json", "validation/generation-plan.json"}
    ]
    evidence_artifacts = []
    for artifact in sorted(evidence_candidates, key=lambda item: item.path):
        kind = {
            "validation/stock-selection.json": "stock_selection",
            "validation/generation-plan.json": "generation_plan",
        }.get(artifact.path)
        evidence_artifacts.append({"kind": kind})
    cam_blocked = bundle.review_status.cam_status is CAMStageStatus.BLOCKED
    return {
        "bundle_sha256": "bundle",
        "manifest_sha256": "manifest",
        "evidence_artifacts": evidence_artifacts,
        "generation_context_hash": "context",
        "design_review_package_status": bundle.review_status.as_dict(),
        "machine_program_mode": "CAM_BLOCKED" if cam_blocked else "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
""",
    PRODUCTION_SEMANTIC_SOURCE_PATHS[3]: """
def _generation_result_claims_are_safe(result_json):
    dfm_status = result_json.get("dfm_status")
    return (
        result_json.get("authoritative_geometry") is True
        and isinstance(dfm_status, str)
        and dfm_status in ("PASS", "WARNING")
    )

def release_version(job):
    if job.result_json.get("authoritative_geometry") is not True:
        raise RuntimeError("unsafe geometry")
    if not _generation_result_claims_are_safe(job.result_json):
        raise RuntimeError("unsafe result")
""",
    PRODUCTION_SEMANTIC_SOURCE_PATHS[4]: """
CONTEXT_HASH_FIELDS = ("generation_context_hash",)
PRODUCTION_MANIFEST_SCHEMA_VERSION = "custombuild.production-manifest.v4"
WORKSHOP_READINESS_SCHEMA_VERSION = "custombuild.workshop-readiness.v2"
DFM_ENGINE_VERSION = "dfm-1.3.0"
STOCK_SELECTION_PATH = "validation/stock-selection.json"
STOCK_SELECTION_ROLE = "STOCK_SELECTION_SNAPSHOT"
STOCK_SELECTION_SCHEMA_VERSION = "custombuild.stock-selection.v1"
GENERATION_PLAN_PATH = "validation/generation-plan.json"
GENERATION_PLAN_ROLE = "GENERATION_PLAN"
GENERATION_PLAN_SCHEMA_VERSION = "custombuild.generation-plan.v1"
PRODUCTION_PIPELINE_VERSION = "production-pipeline-1.10.0"
OPERATIONS_SCHEMA_VERSION = "custombuild.operations.v2"
OPERATIONS_ENGINE_VERSION = "semantic-operations-1.2.0"
REQUIRED_REVIEW_PACKAGE_PATHS = frozenset({STOCK_SELECTION_PATH, GENERATION_PLAN_PATH})
STOCK_PROFILE_MISSING = "STOCK_PROFILE_MISSING"
DFM_GRAIN_MISSING = "DFM-GRAIN-001"
DADO_RETENTION_EVIDENCE_MISSING = "DADO_RETENTION_EVIDENCE_MISSING"
BLOCKED_CAM_REQUIRED_ACTIONS = {
    DADO_RETENTION_EVIDENCE_MISSING: (
        "The current MVP cannot resolve this blocker because it has no authenticated "
        "catalogue/evidence boundary. Such a server-side boundary must bind a versioned, "
        "checksum-addressed mechanical retention contract to every DADO joint, including "
        "exact geometry, hardware quantity, material/thickness applicability and separate "
        "shear/withdrawal capacity data; a review acknowledgement, adhesive or geometric "
        "bearing check is not retention evidence."
    ),
}

def verify_workshop_readiness(payload):
    require(payload["physical_cutting_authorized"] is False, "unsafe readiness")

def verify_generation_result_safety(job_result):
    package_status = job_result["design_review_package_status"]
    workshop_status = {"MATERIAL_GRAIN": "EXTERNAL_EVIDENCE_REQUIRED"}
    dfm_blocked = package_status["blocker_codes"] in (
        [STOCK_PROFILE_MISSING],
        [DFM_GRAIN_MISSING],
    )
    if dfm_blocked:
        require(job_result.get("dfm_status") == "BLOCK", "unsafe blocked DFM")
        if package_status["blocker_codes"] == [DFM_GRAIN_MISSING]:
            require(
                workshop_status.get("MATERIAL_GRAIN") == "EXTERNAL_EVIDENCE_REQUIRED",
                "grain readiness remains unresolved",
            )
    else:
        require(job_result.get("dfm_status") in {"PASS", "WARNING"}, "unsafe DFM")

def verify_generation_context_hash(completed_job, job_result):
    job_context_hash = completed_job.get("production_context_hash")
    result_context_hash = job_result.get("generation_context_hash")
    require(job_context_hash == result_context_hash, "context mismatch")
    return job_context_hash

def verify_package(
    bundle,
    manifest,
    standalone_stock_selection,
    standalone_generation_plan,
    *,
    generation_context_hash,
):
    archived_stock_selection = archive.read(STOCK_SELECTION_PATH)
    require(
        archived_stock_selection == standalone_stock_selection,
        "stock selection mismatch",
    )
    archived_generation_plan = archive.read(GENERATION_PLAN_PATH)
    require(
        archived_generation_plan == standalone_generation_plan,
        "generation plan mismatch",
    )
    require(manifest.get("physical_cutting_authorized") is False, "unsafe manifest")
    require(
        manifest.get("generation_context_hash") == generation_context_hash,
        "manifest context mismatch",
    )

def run_acceptance(completed_job, job_result, manifest):
    cam_blocked = True
    if cam_blocked:
        nordic.request("POST", f"{base}/approve", expected=(409,))
        nordic.request("POST", f"{base}/release", expected=(409,))
    generation_context_hash = verify_generation_context_hash(completed_job, job_result)
    downloaded = {
        "stock_selection": b"selection",
        "generation_plan": b"plan",
    }
    verify_package(
        b"bundle",
        manifest,
        downloaded["stock_selection"],
        downloaded["generation_plan"],
        generation_context_hash=generation_context_hash,
    )
""",
}


def write_production_semantic_fixture(root: Path, *, sources: dict[str, str] | None = None) -> None:
    for relative, source in (sources or SEMANTIC_FIXTURE_SOURCES).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.strip() + "\n", encoding="utf-8")


def test_checked_in_production_semantic_contract_is_current() -> None:
    assert production_semantic_contract_issues(Path.cwd()) == []


def test_minimal_exact_production_semantic_contract_passes(tmp_path: Path) -> None:
    write_production_semantic_fixture(tmp_path)

    assert production_semantic_contract_issues(tmp_path) == []


@pytest.mark.parametrize("relative", PRODUCTION_SEMANTIC_SOURCE_PATHS)
def test_production_semantic_contract_requires_every_source(tmp_path: Path, relative: str) -> None:
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    del sources[relative]
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert f"{relative} is missing" in production_semantic_contract_issues(tmp_path)


@pytest.mark.parametrize(
    ("relative", "old", "new", "expected_issue"),
    [
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[0],
            "custombuild.production-manifest.v4",
            "custombuild.production-manifest.v3",
            "manifest v4 schema",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[0],
            '    "template_capability_registry_version",\n',
            "",
            "v4 safety fields",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[0],
            '"release_scope": "design_review"',
            '"release_scope": "production"',
            "build_manifest emitter",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[0],
            '"machine_use": "validation_only"',
            '"machine_use": "cutting"',
            "build_manifest emitter",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[0],
            '"physical_cutting_authorized": False',
            '"physical_cutting_authorized": True',
            "build_manifest emitter",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[1],
            "custombuild.workshop-readiness.v2",
            "custombuild.workshop-readiness.v1",
            "WORKSHOP_READINESS_SCHEMA_VERSION",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[1],
            "physical_cutting_authorized=False",
            "physical_cutting_authorized=True",
            "builder can emit unsafe",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[2],
            '"machine_program_mode": "CAM_BLOCKED" if cam_blocked else "VALIDATION_DRY_RUN"',
            '"machine_program_mode": "CUTTING"',
            "not strictly bound",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[2],
            '"production_machine_program": False',
            '"production_machine_program": True',
            "can claim a production machine program",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[2],
            'grain_direction="UNBOUND"',
            'grain_direction="X"',
            "unique role/thickness stock IDs with UNBOUND grain",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[2],
            '            role="back",',
            '            role="carcass",',
            "unique role/thickness stock IDs with UNBOUND grain",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[2],
            '"validation/stock-selection.json": "stock_selection"',
            '"validation/stock-selection.json": "worker_note"',
            "checksum-bound stock-selection snapshot",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[2],
            '"validation/generation-plan.json": "generation_plan"',
            '"validation/generation-plan.json": "worker_note"',
            "checksum-bound generation plan",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[3],
            "and isinstance(dfm_status, str)",
            "and True",
            "safety predicate is incomplete",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[3],
            "if not _generation_result_claims_are_safe(job.result_json):",
            "if False:",
            "release path does not fail closed",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[3],
            'if job.result_json.get("authoritative_geometry") is not True:',
            'if job.result_json.get("authoritative_geometry") is not True and False:',
            "release path does not fail closed",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[3],
            "if not _generation_result_claims_are_safe(job.result_json):",
            "if not _generation_result_claims_are_safe(job.result_json) and False:",
            "release path does not fail closed",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[4],
            'job_result.get("dfm_status") in {"PASS", "WARNING"}',
            'job_result.get("dfm_status") in {"PASS"}',
            "bind live DFM acceptance",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[4],
            'job_result.get("dfm_status") == "BLOCK"',
            'job_result.get("dfm_status") == "PASS"',
            "bind live DFM acceptance",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[4],
            "        [DFM_GRAIN_MISSING],\n",
            "",
            "bind live DFM acceptance",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[4],
            'workshop_status.get("MATERIAL_GRAIN") == "EXTERNAL_EVIDENCE_REQUIRED"',
            'workshop_status.get("MATERIAL_GRAIN") == "VERIFIED"',
            "keep grain-blocked MATERIAL_GRAIN readiness unresolved",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[4],
            "generation_context_hash=generation_context_hash",
            'generation_context_hash="unbound"',
            "bind generation context",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[4],
            "archived_stock_selection == standalone_stock_selection",
            "archived_stock_selection is not None",
            "byte-bind the standalone semantic review documents",
        ),
        (
            PRODUCTION_SEMANTIC_SOURCE_PATHS[4],
            "archived_generation_plan == standalone_generation_plan",
            "archived_generation_plan is not None",
            "byte-bind the standalone semantic review documents",
        ),
    ],
)
def test_production_semantic_contract_rejects_stale_or_unsafe_mutations(
    tmp_path: Path,
    relative: str,
    old: str,
    new: str,
    expected_issue: str,
) -> None:
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    assert old in sources[relative]
    sources[relative] = sources[relative].replace(old, new, 1)
    write_production_semantic_fixture(tmp_path, sources=sources)

    issues = production_semantic_contract_issues(tmp_path)

    assert any(relative in issue and expected_issue in issue for issue in issues)


@pytest.mark.parametrize(
    ("anchor", "replacement"),
    [
        (
            "    manifest = {",
            '    production_context["physical_cutting_authorized"] = True\n    manifest = {',
        ),
        (
            "    return canonical_json_bytes(manifest)",
            '    manifest["physical_cutting_authorized"] = True\n'
            "    return canonical_json_bytes(manifest)",
        ),
    ],
)
def test_package_semantic_contract_rejects_post_dict_mutation(
    tmp_path: Path, anchor: str, replacement: str
) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[0]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    sources[relative] = sources[relative].replace(anchor, replacement, 1)
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "build_manifest emitter" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


def test_package_semantic_contract_ignores_a_safe_dead_decoy(tmp_path: Path) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[0]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    unsafe_emitter = sources[relative].replace(
        '"machine_use": "validation_only"',
        '"machine_use": "cutting"',
        1,
    )
    sources[relative] = (
        unsafe_emitter
        + """

def dead_safe_decoy():
    return {
        "release_scope": "design_review",
        "machine_use": "validation_only",
        "physical_cutting_authorized": False,
    }
"""
    )
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "build_manifest emitter" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


@pytest.mark.parametrize("unsafe_field", ["machine_program_mode", "production_machine_program"])
def test_worker_semantic_contract_rejects_post_result_mutation(
    tmp_path: Path, unsafe_field: str
) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[2]
    unsafe_value = '"CUTTING"' if unsafe_field == "machine_program_mode" else "True"
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    sources[relative] = f'''
def _generate():
    cam_blocked = bundle.review_status.cam_status is CAMStageStatus.BLOCKED
    result = {{
        "bundle_sha256": "bundle",
        "manifest_sha256": "manifest",
        "evidence_artifacts": [],
        "generation_context_hash": "context",
        "design_review_package_status": bundle.review_status.as_dict(),
        "machine_program_mode": "CAM_BLOCKED" if cam_blocked else "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }}
    result["{unsafe_field}"] = {unsafe_value}
    return result
'''
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "unique immutable _generate result" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


def test_worker_semantic_contract_rejects_result_aliasing(tmp_path: Path) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[2]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    sources[relative] = """
def _generate():
    cam_blocked = bundle.review_status.cam_status is CAMStageStatus.BLOCKED
    result = {
        "bundle_sha256": "bundle",
        "manifest_sha256": "manifest",
        "evidence_artifacts": [],
        "generation_context_hash": "context",
        "design_review_package_status": bundle.review_status.as_dict(),
        "machine_program_mode": "CAM_BLOCKED" if cam_blocked else "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    alias = result
    return alias
"""
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "unique immutable _generate result" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "cam_blocked = bundle.review_status.cam_status is CAMStageStatus.BLOCKED",
            "cam_blocked = True",
        ),
        (
            '"design_review_package_status": bundle.review_status.as_dict()',
            '"design_review_package_status": {}',
        ),
        (
            '"CAM_BLOCKED" if cam_blocked else "VALIDATION_DRY_RUN"',
            '"VALIDATION_DRY_RUN" if cam_blocked else "CAM_BLOCKED"',
        ),
    ],
)
def test_worker_semantic_contract_binds_both_modes_to_review_status(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[2]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    assert old in sources[relative]
    sources[relative] = sources[relative].replace(old, new, 1)
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "not strictly bound" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


def test_worker_semantic_contract_ignores_a_safe_dead_decoy(tmp_path: Path) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[2]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    sources[relative] = """
def _generate():
    dead_safe_decoy = {
        "bundle_sha256": "bundle-decoy",
        "manifest_sha256": "manifest-decoy",
        "evidence_artifacts": [],
        "generation_context_hash": "context-decoy",
        "machine_program_mode": "VALIDATION_DRY_RUN",
        "production_machine_program": False,
    }
    cam_blocked = bundle.review_status.cam_status is CAMStageStatus.BLOCKED
    return {
        "bundle_sha256": "bundle",
        "manifest_sha256": "manifest",
        "evidence_artifacts": [],
        "generation_context_hash": "context",
        "design_review_package_status": bundle.review_status.as_dict(),
        "machine_program_mode": "CUTTING",
        "production_machine_program": True,
    }
"""
    write_production_semantic_fixture(tmp_path, sources=sources)

    issues = production_semantic_contract_issues(tmp_path)

    assert any(relative in issue and "not strictly bound" in issue for issue in issues)
    assert any(relative in issue and "production machine program" in issue for issue in issues)


def test_api_release_guard_rejects_a_nested_dead_raise(tmp_path: Path) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[3]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    sources[relative] = sources[relative].replace(
        """    if job.result_json.get("authoritative_geometry") is not True:
        raise RuntimeError("unsafe geometry")""",
        """    if job.result_json.get("authoritative_geometry") is not True:
        if False:
            raise RuntimeError("unsafe geometry")""",
        1,
    )
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "release path does not fail closed" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


def test_api_release_guard_rejects_a_caught_raise(tmp_path: Path) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[3]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    sources[relative] = sources[relative].replace(
        """    if not _generation_result_claims_are_safe(job.result_json):
        raise RuntimeError("unsafe result")""",
        """    try:
        if not _generation_result_claims_are_safe(job.result_json):
            raise RuntimeError("unsafe result")
    except RuntimeError:
        pass""",
        1,
    )
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "release path does not fail closed" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


def test_api_release_guard_rejects_a_prior_conditional_return(tmp_path: Path) -> None:
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[3]
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    sources[relative] = sources[relative].replace(
        """def release_version(job):
    if job.result_json.get("authoritative_geometry") is not True:""",
        """def release_version(job):
    if True:
        return {"released": True}
    if job.result_json.get("authoritative_geometry") is not True:""",
        1,
    )
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "release path does not fail closed" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


def test_production_semantic_contract_fails_closed_on_python_parse_error(
    tmp_path: Path,
) -> None:
    sources = dict(SEMANTIC_FIXTURE_SOURCES)
    relative = PRODUCTION_SEMANTIC_SOURCE_PATHS[2]
    sources[relative] = "def broken(:\n"
    write_production_semantic_fixture(tmp_path, sources=sources)

    assert any(
        relative in issue and "not valid Python" in issue
        for issue in production_semantic_contract_issues(tmp_path)
    )


def test_build_report_blocks_software_release_on_semantic_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git(_repo: Path, *arguments: str) -> str:
        if arguments == ("rev-parse", "HEAD"):
            return "a" * 40
        if arguments == ("branch", "--show-current"):
            return "main"
        return ""

    monkeypatch.setattr(
        release_readiness,
        "load_surface",
        lambda _path: SimpleNamespace(project="custombuild-prod", published_ports=frozenset()),
    )
    monkeypatch.setattr(release_readiness, "run_git", fake_git)
    monkeypatch.setattr(
        release_readiness,
        "build_source_manifest",
        lambda _repo: ([], b"", "b" * 64),
    )
    monkeypatch.setattr(
        release_readiness,
        "verify_production_semantic_root",
        lambda _repo: SimpleNamespace(digest="c" * 64),
    )
    monkeypatch.setattr(release_readiness, "resolved_compose", lambda _repo: {})
    monkeypatch.setattr(release_readiness, "compose_hardening_issues", lambda _config: [])
    monkeypatch.setattr(release_readiness, "supply_chain_issues", lambda _repo: [])
    monkeypatch.setattr(
        release_readiness,
        "production_semantic_contract_issues",
        lambda _repo: ["worker result can claim production cutting"],
    )

    report = build_report(Path.cwd(), require_clean=True)
    semantic_check = next(
        check for check in report["checks"] if check["code"] == "PRODUCTION_SEMANTIC_CONTRACT"
    )

    assert semantic_check["status"] == "BLOCK"
    assert "worker result can claim production cutting" in semantic_check["detail"]
    assert report["static_controls_ready"] is False
    assert report["software_release_ready"] is False
    assert report["repository_content_root_sha256"] == "c" * 64
    assert report["external_semantic_approval_required"] is True
    assert report["schema_version"] == "custombuild.release-readiness-static.v3"

    def stale_root(_repo: Path) -> object:
        raise release_readiness.SourceManifestError("semantic root is stale")

    monkeypatch.setattr(release_readiness, "verify_production_semantic_root", stale_root)
    stale_report = build_report(Path.cwd(), require_clean=True)
    root_check = next(
        check for check in stale_report["checks"] if check["code"] == "REPOSITORY_CONTENT_ROOT"
    )
    assert root_check["status"] == "BLOCK"
    assert stale_report["repository_content_root_sha256"] is None
    assert stale_report["static_controls_ready"] is False


def write_empty_vulnerability_policy(root: Path) -> None:
    (root / ".grype.yaml").write_text("ignore: []\n")
    security = root / "security"
    security.mkdir(parents=True, exist_ok=True)
    (security / "vulnerability-exceptions.json").write_text(
        json.dumps(
            {
                "schema_version": "custombuild.vulnerability-exceptions.v2",
                "exceptions": [],
            }
        )
    )


def exact_grype_policy(
    vulnerability: str = "CVE-2099-0001",
    package: str = "python",
    version: str = "3.13.14",
    package_type: str = "binary",
) -> str:
    return (
        "ignore:\n"
        f"  - vulnerability: {vulnerability}\n"
        "    package:\n"
        f"      name: {package}\n"
        f"      version: {version}\n"
        f"      type: {package_type}\n"
    )


def vulnerability_record(
    vulnerability: str = "CVE-2099-0001",
    package: str = "python",
    version: str = "3.13.14",
    package_type: str = "binary",
    **overrides: str,
) -> dict[str, str]:
    record = {
        "vulnerability": vulnerability,
        "package": package,
        "version": version,
        "type": package_type,
        "severity": "Critical",
        "owner": "security-release-owner",
        "rationale": "Exact temporary exception for a reviewed scanner finding.",
        "mitigation": "Keep the digest pinned and re-scan before the review deadline.",
        "source": "https://example.test/advisory",
        "review_by": "2026-09-01",
    }
    record.update(overrides)
    return record


def write_exception_ledger(root: Path, records: list[dict[str, str]]) -> None:
    security = root / "security"
    security.mkdir(parents=True, exist_ok=True)
    (security / "vulnerability-exceptions.json").write_text(
        json.dumps(
            {
                "schema_version": "custombuild.vulnerability-exceptions.v2",
                "exceptions": records,
            }
        )
    )


def hardened_service(*, networks: list[str]) -> dict[str, object]:
    return {
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "pids_limit": 64,
        "networks": networks,
    }


def test_hardened_compose_contract_passes() -> None:
    api = hardened_service(networks=["edge", "backend"]) | {
        "healthcheck": {"test": ["CMD", "probe"]},
        "environment": {
            "REDIS_URL": "redis://:strong-secret@redis:6379/0",
            "RATE_LIMIT_REQUESTS": "180",
            "RATE_LIMIT_WINDOW_SECONDS": "60",
        },
        "depends_on": {"redis": {"condition": "service_healthy"}},
    }
    worker = hardened_service(networks=["backend"]) | {
        "healthcheck": {"test": ["CMD", "worker-probe"]},
        "environment": {"REDIS_URL": "redis://:strong-secret@redis:6379/0"},
    }
    web = hardened_service(networks=["edge"]) | {
        "depends_on": {"api": {"condition": "service_healthy"}}
    }
    datastore = {
        "restart": "unless-stopped",
        "pids_limit": 128,
        "networks": ["backend"],
    }
    object_storage = {
        "restart": "unless-stopped",
        "pids_limit": 128,
        "networks": ["backend", "artifact-ingress"],
    }
    redis = datastore | {
        "environment": {"REDIS_PASSWORD": "strong-secret"},
        "command": ["redis-server", "--requirepass", "strong-secret"],
    }
    config = {
        "services": {
            "api": api,
            "worker": worker,
            "scheduler": hardened_service(networks=["backend"]),
            "web": web,
            "postgres": datastore,
            "redis": redis,
            "object-storage": object_storage,
        },
        "networks": {
            "edge": {},
            "backend": {"internal": True},
            "artifact-ingress": {},
        },
    }

    assert compose_hardening_issues(config) == []


def test_hardening_reports_each_missing_boundary() -> None:
    issues = compose_hardening_issues(
        {"services": {"api": {}, "worker": {}, "web": {"depends_on": ["api"]}}}
    )

    assert "api is not read-only" in issues
    assert "worker does not drop all Linux capabilities" in issues
    assert "web lacks no-new-privileges" in issues
    assert "api has no dependency-backed readiness healthcheck" in issues
    assert "api does not configure REDIS_URL" in issues
    assert "api does not wait for the shared rate-limit store" in issues
    assert "web does not wait for API readiness" in issues
    assert "object-storage has no isolated host-publish ingress network" in issues
    assert "postgres has no positive PID limit" in issues


def test_supply_chain_gate_accepts_immutable_actions_and_evidence(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    sha = "a" * 40
    (workflows / "ci.yml").write_text(f"steps:\n  - uses: actions/checkout@{sha}\n")
    (workflows / "supply-chain.yml").write_text(
        f"steps:\n  - uses: anchore/sbom-action@{sha}\n"
        f"  - uses: anchore/scan-action@{sha}\n    config: .grype.yaml\n"
        "    fail-build: true\n    severity-cutoff: high\n"
        f"  - uses: actions/upload-artifact@{sha}\n    path: image.release.json\n"
    )
    write_empty_vulnerability_policy(tmp_path)
    (tmp_path / "compose.yml").write_text(f"services:\n  db:\n    image: db:1@sha256:{'b' * 64}\n")
    for relative in (
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
        "apps/web/Dockerfile",
    ):
        dockerfile = tmp_path / relative
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        lock_argument = (
            "FRONTEND_LOCK_SHA256"
            if relative == "apps/web/Dockerfile"
            else "DEPENDENCY_LOCK_SHA256"
        )
        lock_label = (
            "io.custombuild.frontend-lock.sha256"
            if relative == "apps/web/Dockerfile"
            else "io.custombuild.dependency-lock.sha256"
        )
        dockerfile.write_text(
            f"FROM example:1@sha256:{'c' * 64}\n"
            "ARG APP_VERSION=0.1.0-local\n"
            "ARG VCS_REF=uncommitted\n"
            "ARG BUILD_DATE=unknown\n"
            "ARG SOURCE_URL=unknown\n"
            "ARG SOURCE_MANIFEST_SHA256=unknown\n"
            f"ARG {lock_argument}=unknown\n"
            "ENV APP_VERSION=${APP_VERSION} VCS_REF=${VCS_REF} "
            "BUILD_DATE=${BUILD_DATE} SOURCE_URL=${SOURCE_URL} "
            "SOURCE_MANIFEST_SHA256=${SOURCE_MANIFEST_SHA256} "
            f"{lock_argument}=${{{lock_argument}}}\n"
            "LABEL org.opencontainers.image.version=${APP_VERSION} "
            "org.opencontainers.image.revision=${VCS_REF} "
            "org.opencontainers.image.created=${BUILD_DATE} "
            "org.opencontainers.image.source=${SOURCE_URL} "
            "io.custombuild.source-manifest.sha256=${SOURCE_MANIFEST_SHA256} "
            f"{lock_label}=${{{lock_argument}}}\n"
        )

    assert supply_chain_issues(tmp_path) == []


def test_supply_chain_gate_reports_floating_actions_and_missing_scan(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("steps:\n  - uses: actions/checkout@v6\n")
    (workflows / "supply-chain.yml").write_text("steps:\n  - uses: anchore/sbom-action@v1\n")
    write_empty_vulnerability_policy(tmp_path)

    issues = supply_chain_issues(tmp_path)

    assert any("uses an unpinned action" in issue for issue in issues)
    assert "supply-chain workflow does not enforce vulnerability scanning" in issues
    assert "compose.yml is missing for container provenance checks" in issues


def test_supply_chain_gate_rejects_filter_that_would_hide_an_unfixed_critical(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    sha = "a" * 40
    (workflows / "supply-chain.yml").write_text(
        f"steps:\n  - uses: anchore/sbom-action@{sha}\n"
        f"  - uses: anchore/scan-action@{sha}\n"
        "    config: .grype.yaml\n"
        "    fail-build: true\n"
        "    severity-cutoff: high\n"
        "    only-fixed: true\n"
    )
    write_empty_vulnerability_policy(tmp_path)

    issues = supply_chain_issues(tmp_path)

    assert "supply-chain workflow filters out unfixed High/Critical vulnerabilities" in issues


def test_supply_chain_gate_reports_missing_oci_release_provenance(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    sha = "a" * 40
    (workflows / "supply-chain.yml").write_text(
        f"steps:\n  - uses: anchore/sbom-action@{sha}\n"
        f"  - uses: anchore/scan-action@{sha}\n    config: .grype.yaml\n"
        f"  - uses: actions/upload-artifact@{sha}\n    path: image.release.json\n"
    )
    write_empty_vulnerability_policy(tmp_path)
    (tmp_path / "compose.yml").write_text(f"services:\n  db:\n    image: db:1@sha256:{'b' * 64}\n")
    for relative in (
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
        "apps/web/Dockerfile",
    ):
        dockerfile = tmp_path / relative
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text(f"FROM example:1@sha256:{'c' * 64}\n")

    issues = supply_chain_issues(tmp_path)

    assert sum("does not embed complete OCI release provenance" in issue for issue in issues) == 3


def test_supply_chain_gate_reports_floating_container_references(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    sha = "a" * 40
    (workflows / "supply-chain.yml").write_text(
        f"steps:\n  - uses: anchore/sbom-action@{sha}\n"
        f"  - uses: anchore/scan-action@{sha}\n    config: .grype.yaml\n"
        "  - image: postgres:latest\n"
    )
    write_empty_vulnerability_policy(tmp_path)
    (tmp_path / "compose.yml").write_text("services:\n  db:\n    image: db:latest\n")
    for relative in ("services/api/Dockerfile", "services/worker/Dockerfile"):
        dockerfile = tmp_path / relative
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text(f"FROM example:1@sha256:{'b' * 64}\n")
    web = tmp_path / "apps/web/Dockerfile"
    web.parent.mkdir(parents=True, exist_ok=True)
    web.write_text("FROM node:latest\n")

    issues = supply_chain_issues(tmp_path)

    assert any("unpinned container image" in issue for issue in issues)
    assert any("unpinned base image" in issue for issue in issues)


def _write_supply_chain_dockerfile_fixture(tmp_path: Path, source: str) -> None:
    workflow = tmp_path / ".github/workflows/supply-chain.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("steps: []\n", encoding="utf-8")
    for relative in (
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
        "apps/web/Dockerfile",
    ):
        dockerfile = tmp_path / relative
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text(source, encoding="utf-8")


def test_supply_chain_gate_accepts_only_reserved_scratch_as_an_unpinned_base(
    tmp_path: Path,
) -> None:
    _write_supply_chain_dockerfile_fixture(tmp_path, "FROM scratch AS runtime\n")

    issues = supply_chain_issues(tmp_path)

    assert not any("uses an unpinned base image" in issue for issue in issues)


@pytest.mark.parametrize(
    "image",
    (
        "scratch:latest",
        "registry.example/scratch",
        "scratch-runtime",
        "Scratch",
        "${RUNTIME_IMAGE}",
    ),
)
def test_supply_chain_gate_rejects_deceptive_scratch_references(
    tmp_path: Path,
    image: str,
) -> None:
    _write_supply_chain_dockerfile_fixture(tmp_path, f"FROM {image} AS runtime\n")

    issues = supply_chain_issues(tmp_path)

    assert sum("uses an unpinned base image" in issue for issue in issues) == 3


def test_supply_chain_gate_rejects_mutable_base_hidden_behind_scratch_alias(
    tmp_path: Path,
) -> None:
    _write_supply_chain_dockerfile_fixture(
        tmp_path,
        "FROM example:latest AS scratch\nFROM scratch AS runtime\n",
    )

    issues = supply_chain_issues(tmp_path)

    assert sum("uses an unpinned base image" in issue for issue in issues) == 3


def test_supply_chain_gate_checks_case_insensitive_from_instructions(tmp_path: Path) -> None:
    _write_supply_chain_dockerfile_fixture(tmp_path, "from example:latest AS runtime\n")

    issues = supply_chain_issues(tmp_path)

    assert sum("uses an unpinned base image" in issue for issue in issues) == 3


def test_supply_chain_gate_reports_mutable_apt_indexes(tmp_path: Path) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    sha = "a" * 40
    (workflows / "supply-chain.yml").write_text(
        f"steps:\n  - uses: anchore/sbom-action@{sha}\n"
        f"  - uses: anchore/scan-action@{sha}\n    config: .grype.yaml\n"
    )
    write_empty_vulnerability_policy(tmp_path)
    (tmp_path / "compose.yml").write_text(f"services:\n  db:\n    image: db:1@sha256:{'b' * 64}\n")
    for relative in (
        "services/api/Dockerfile",
        "services/worker/Dockerfile",
        "apps/web/Dockerfile",
    ):
        dockerfile = tmp_path / relative
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text(
            f"FROM example:1@sha256:{'c' * 64}\nRUN apt-get update && apt-get install -y tool\n"
        )

    issues = supply_chain_issues(tmp_path)

    assert sum("uses a mutable APT package index" in issue for issue in issues) == 3


def test_vulnerability_exception_gate_rejects_expired_or_untracked_entries(tmp_path: Path) -> None:
    (tmp_path / ".grype.yaml").write_text(exact_grype_policy())
    write_exception_ledger(
        tmp_path,
        [
            vulnerability_record(review_by="2026-08-09"),
            vulnerability_record(vulnerability="CVE-2099-0002"),
        ],
    )

    issues = vulnerability_exception_issues(tmp_path, today=date(2026, 8, 10))

    assert any("CVE-2099-0001" in issue and "expired on 2026-08-09" in issue for issue in issues)
    assert any(
        "ledger record CVE-2099-0002" in issue and "not present in the Grype policy" in issue
        for issue in issues
    )


@pytest.mark.parametrize(
    ("field", "mismatched"),
    [
        ("package", "python-other"),
        ("version", "3.13.15"),
        ("type", "library"),
    ],
)
def test_vulnerability_exception_gate_rejects_tuple_mismatch(
    tmp_path: Path,
    field: str,
    mismatched: str,
) -> None:
    (tmp_path / ".grype.yaml").write_text(exact_grype_policy())
    record = vulnerability_record()
    record[field] = mismatched
    write_exception_ledger(tmp_path, [record])

    issues = vulnerability_exception_issues(tmp_path, today=date(2026, 8, 10))

    assert any("has no exact ledger record" in issue for issue in issues)
    assert any("is not present in the Grype policy" in issue for issue in issues)


@pytest.mark.parametrize("field", ["severity", "mitigation"])
def test_vulnerability_exception_gate_requires_release_metadata(
    tmp_path: Path,
    field: str,
) -> None:
    (tmp_path / ".grype.yaml").write_text(exact_grype_policy())
    record = vulnerability_record()
    del record[field]
    write_exception_ledger(tmp_path, [record])

    issues = vulnerability_exception_issues(tmp_path, today=date(2026, 8, 10))

    assert any(f"is missing {field}" in issue for issue in issues)


def test_checked_in_vulnerability_policy_has_exact_current_ledger_records() -> None:
    assert vulnerability_exception_issues(Path.cwd(), today=date(2026, 8, 12)) == []
