from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any
from unittest.mock import patch

import custombuild_manufacturing.offline_package_verifier as offline_package_verifier
import custombuild_manufacturing.pipeline as manufacturing_pipeline
import custombuild_manufacturing.review_status as review_status_contract
import pytest
from custombuild_cad import CADArtifacts, CADDependencyUnavailable
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    JointRetentionContract,
    JointRetentionLoadCase,
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMaterialIdentity,
    JointRetentionMethod,
    build_bookcase,
    dado_joint_geometry_fingerprint,
    screening_birch_plywood_6,
    screening_birch_plywood_18,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import (
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    DFM_GRAIN_BLOCKER_CODE,
    JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
    JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
    JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
    MANIFEST_CONTEXT_HASH_FIELDS,
    MANUFACTURING_INTENT_PATH,
    MAX_CORE_DOCUMENT_BYTES,
    MAX_PRODUCTION_BUNDLE_BYTES,
    SUPPLIER_HANDOFF_PATH,
    ArtifactFile,
    DFMIssue,
    DFMReport,
    ManifestContext,
    PartSpec,
    Point2D,
    ProductionBlockedError,
    Severity,
    StockSheet,
    TwoSidedRegistration,
    blocked_cam_artifact_violation,
    blocked_design_review_package_status,
    build_deterministic_zip,
    build_production_bundle,
    canonical_json_bytes,
    generated_design_review_package_status,
    generation_plan_artifact,
    linuxcnc_reference_router_1325,
    read_and_verify_package,
    registration_pin_keep_out_rectangles,
    sha256_hex,
    stock_grain_binding_issues,
    validate_design_review_status_inventory_entries,
    validate_manifest_context_contract,
)
from custombuild_manufacturing.errors import ArtifactError
from custombuild_manufacturing.offline_package_verifier import (
    REPORT_SCHEMA_VERSION as OFFLINE_REPORT_SCHEMA_VERSION,
)
from custombuild_manufacturing.package import (
    PACKAGE_BUILDER_VERSION,
    PRODUCTION_MANIFEST_SCHEMA_VERSION,
)
from custombuild_manufacturing.pipeline import CadQueryAdapter
from custombuild_manufacturing.quality import (
    MANUFACTURING_INTENT_JSON_SCHEMA_PATH,
    OPERATIONS_JSON_SCHEMA_PATH,
    START_HERE_PATH,
    SUPPLIER_HANDOFF_JSON_SCHEMA_PATH,
)
from custombuild_manufacturing.readiness import build_workshop_readiness_report


@pytest.fixture(autouse=True)
def _simulate_versioned_retention_for_legacy_package_security_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep package-tamper tests focused past the independent DADO gate."""

    monkeypatch.setattr(
        manufacturing_pipeline,
        "dado_retention_evidence_missing",
        lambda _design: False,
    )
    monkeypatch.setattr(
        review_status_contract,
        "dado_retention_evidence_missing",
        lambda _design: False,
    )


def _review_design(*, edge_band_selection_required: bool = False):
    return build_bookcase(
        BookcaseDesignSpec(
            design_id=(
                "package-security-edge" if edge_band_selection_required else "package-security"
            ),
            parameters=BookcaseParameters(
                edge_band_thickness_um=1_000 if edge_band_selection_required else 0,
            ),
            material=screening_birch_plywood_18(),
            back_material=screening_birch_plywood_6(),
        )
    )


def _review_context(design, *, external_evidence: tuple[dict[str, str], ...] = ()):
    machine = linuxcnc_reference_router_1325()
    return ManifestContext(
        "project",
        "1",
        design.design_hash,
        "app-1",
        design.engine_version,
        design.template_version,
        "shelving",
        "c" * 64,
        {
            "template_id": "shelving",
            "template_version": "1.0.0",
            "capability_fingerprint": "c" * 64,
        },
        "rules-1",
        (),
        "joints-1",
        machine.profile_id,
        machine.version,
        "derived-by-pipeline",
        "GENERATED",
        "f" * 64,
        {
            "schema_version": "test-production-context.v1",
            "template_capability_registry_version": "test-registry-1.0.0",
        },
        external_evidence=external_evidence,
    )


def _review_stocks(design, *, mode: str) -> tuple[StockSheet, ...]:
    if mode == "STOCK_PROFILE_MISSING":
        return (
            StockSheet(
                "incompatible-stock",
                "different-material",
                "v1",
                500_000,
                500_000,
                18_000,
                grain_direction="UNBOUND",
            ),
        )
    axis = "UNBOUND" if mode == DFM_GRAIN_BLOCKER_CODE else "X"
    return (
        StockSheet(
            "birch-main-stock",
            design.spec.material.material_id,
            design.spec.material.version,
            2_440_000,
            1_220_000,
            design.spec.parameters.actual_thickness_um,
            quantity=4,
            grain_direction=axis,
        ),
        StockSheet(
            "birch-back-stock",
            design.spec.back_material.material_id,
            design.spec.back_material.version,
            2_440_000,
            1_220_000,
            design.spec.parameters.back_thickness_um,
            quantity=2,
            grain_direction=axis,
        ),
    )


@lru_cache(maxsize=2)
def _review_cad(edge_band_selection_required: bool) -> CADArtifacts:
    return CadQueryAdapter().export_design(
        _review_design(edge_band_selection_required=edge_band_selection_required)
    )


def _registration(method_id: str, points: tuple[Point2D, ...]) -> TwoSidedRegistration:
    return TwoSidedRegistration(
        declaration_authority="CLIENT_DECLARED",
        method_id=method_id,
        fixture_method_version="fixture-v1",
        pin_diameter_um=6_000,
        position_tolerance_um=500,
        points=points,
    )


def _registrations(stocks: tuple[StockSheet, ...]):
    return {
        stock.stock_id: {
            sheet_index: _registration(
                f"fixture:{stock.stock_id}:{sheet_index}",
                (Point2D(50_000, 50_000), Point2D(stock.width_um - 50_000, 50_000)),
            )
            for sheet_index in range(stock.quantity)
        }
        for stock in stocks
    }


@lru_cache(maxsize=32)
def _review_case_cached(
    mode: str,
    edge_band_selection_required: bool,
    external_evidence_json: str,
    include_worker_note: bool,
):
    external_evidence = tuple(json.loads(external_evidence_json))
    design = _review_design(edge_band_selection_required=edge_band_selection_required)
    machine = linuxcnc_reference_router_1325()
    stocks = _review_stocks(design, mode=mode)
    context = _review_context(design, external_evidence=external_evidence)
    registrations = _registrations(stocks) if mode == "GENERATED" else None
    additional = (
        (
            ArtifactFile(
                "assembly/assembly-manual.pdf",
                b"%PDF-1.4\n% test review manual\n",
                "application/pdf",
                "ASSEMBLY_REVIEW_MANUAL",
            ),
        )
        if include_worker_note
        else ()
    )
    with patch.object(
        CadQueryAdapter,
        "export_design",
        return_value=_review_cad(edge_band_selection_required),
    ):
        bundle = build_production_bundle(
            design,
            stock=stocks,
            machine=machine,
            context=context,
            include_step=True,
            include_validation_program=True,
            allow_blocked_cam=True,
            two_sided_registration_by_stock=registrations,
            additional_artifacts=additional,
        )
    frozen_context = replace(
        context,
        engine_version=bundle.manifest["engine_version"],
        template_version=bundle.manifest["template_version"],
        material_versions=tuple(bundle.manifest["material_versions"]),
        postprocessor_version=bundle.manifest["postprocessor_version"],
        cad_status=bundle.manifest["cad_status"],
    )
    return frozen_context, bundle.artifacts, bundle.zip_bytes


def _review_case(
    mode: str,
    *,
    edge_band_selection_required: bool = False,
    external_evidence: tuple[dict[str, str], ...] = (),
    include_worker_note: bool = False,
):
    return _review_case_cached(
        mode,
        edge_band_selection_required,
        json.dumps(external_evidence, sort_keys=True),
        include_worker_note,
    )


@lru_cache(maxsize=1)
def _dado_review_case():
    design = _review_design()
    machine = linuxcnc_reference_router_1325()
    stocks = _review_stocks(design, mode=DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE)
    with (
        patch.object(
            manufacturing_pipeline,
            "dado_retention_evidence_missing",
            lambda _design: True,
        ),
        patch.object(
            review_status_contract,
            "dado_retention_evidence_missing",
            lambda _design: True,
        ),
        patch.object(CadQueryAdapter, "export_design", return_value=_review_cad(False)),
    ):
        return build_production_bundle(
            design,
            stock=stocks,
            machine=machine,
            context=_review_context(design),
            include_step=True,
            include_validation_program=True,
            allow_blocked_cam=True,
        )


def package_fixture():
    return _review_case("GENERATED", include_worker_note=True)


@lru_cache(maxsize=1)
def _retention_package_components():
    evidence_bytes = canonical_json_bytes(
        {
            "schema_version": "test-only.signed-retention.v1",
            "signature": "test-only-package-integrity-fixture",
        }
    )
    base_spec = BookcaseDesignSpec(
        design_id="retention-package-security",
        parameters=BookcaseParameters(),
        material=screening_birch_plywood_18(),
        back_material=screening_birch_plywood_6(),
    )
    base_design = build_bookcase(base_spec)
    retention = JointRetentionContract(
        system_id="test-only.custom-retention",
        system_version="v1",
        method=JointRetentionMethod.MECHANICAL,
        catalog_entry_sha256="1" * 64,
        evidence_id="test-only.signed-evidence",
        evidence_sha256=sha256_hex(evidence_bytes),
        installation_instruction_id="test-only.instructions",
        installation_instruction_version="v1",
        installation_instruction_sha256="3" * 64,
        machining_scope=JointRetentionMachiningScope.NO_ADDITIONAL_CNC,
        hardware_sku="test-only.fastener",
        hardware_count_per_joint=2,
        applicable_materials=(
            JointRetentionMaterialIdentity(
                material_id=base_spec.material.material_id,
                material_version=base_spec.material.version,
            ),
        ),
        joint_geometry_sha256=dado_joint_geometry_fingerprint(
            base_design.parts,
            base_design.joints,
        ),
        minimum_applicable_thickness_um=17_000,
        maximum_applicable_thickness_um=19_000,
        load_cases=(
            JointRetentionLoadCase(
                mode=JointRetentionLoadMode.SHEAR,
                rated_design_load_n=300,
                verified_capacity_n=600,
            ),
            JointRetentionLoadCase(
                mode=JointRetentionLoadMode.WITHDRAWAL,
                rated_design_load_n=50,
                verified_capacity_n=100,
            ),
        ),
        safety_factor_permille=1_800,
    )
    design = build_bookcase(base_spec.model_copy(update={"joint_retention": retention}))
    return design, evidence_bytes, CadQueryAdapter().export_design(design)


@lru_cache(maxsize=2)
def retention_package_fixture(mode: str = "GENERATED"):
    design, evidence_bytes, cad = _retention_package_components()
    machine = linuxcnc_reference_router_1325()
    stocks = _review_stocks(design, mode=mode)
    with patch.object(CadQueryAdapter, "export_design", return_value=cad):
        bundle = build_production_bundle(
            design,
            stock=stocks,
            machine=machine,
            context=_review_context(design),
            include_step=True,
            include_validation_program=True,
            allow_blocked_cam=True,
            two_sided_registration_by_stock=(
                _registrations(stocks) if mode == "GENERATED" else None
            ),
            additional_artifacts=(
                ArtifactFile(
                    JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
                    evidence_bytes,
                    JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
                    JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
                ),
            ),
        )
    return bundle.zip_bytes, evidence_bytes


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"generation_context_hash": "short"}, "generation context hash"),
        ({"production_engine_context": {}}, "production engine context"),
        ({"template_capability_fingerprint": "short"}, "capability fingerprint"),
        ({"template_capability": {}}, "capability snapshot"),
        (
            {
                "template_capability": {
                    "template_id": "shelving",
                    "template_version": "1.0.0",
                    "capability_fingerprint": "d" * 64,
                }
            },
            "snapshot fingerprint mismatch",
        ),
        ({"source_provenance": {"source": "manual"}}, "type is unsupported"),
        (
            {
                "source_provenance": {
                    "source": "reference_image",
                    "import_id": "short",
                }
            },
            "requires an import ID",
        ),
        (
            {
                "source_provenance": {
                    "source": "reference_image",
                    "import_id": "i" * 36,
                    "image_sha256": "not-a-digest",
                    "verified_model_fingerprint": "b" * 64,
                }
            },
            "requires image_sha256",
        ),
        (
            {
                "source_provenance": {
                    "source": "reference_image",
                    "import_id": "i" * 36,
                    "image_sha256": "a" * 64,
                    "verified_model_fingerprint": "B" * 64,
                }
            },
            "requires verified_model_fingerprint",
        ),
    ),
)
def test_manifest_context_constructor_fails_closed_on_unbound_identity(
    overrides: dict[str, object],
    message: str,
) -> None:
    context = _review_context(_review_design())

    with pytest.raises(ValueError, match=message):
        replace(context, **overrides)


def test_artifact_file_requires_immutable_bytes() -> None:
    with pytest.raises(TypeError, match="artifact data must be bytes"):
        ArtifactFile("data/value.txt", "mutable text", "text/plain", "TEST")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "case",
    (
        "program-not-boolean",
        "registrations-not-mapping",
        "unknown-stock",
        "sheets-not-mapping",
        "sheet-index-not-integer",
        "sheet-index-out-of-range",
        "plan-wrong-type",
        "point-outside-sheet",
    ),
)
def test_generation_plan_rejects_unbound_registration_inputs(case: str) -> None:
    machine = linuxcnc_reference_router_1325()
    stock = StockSheet(
        "registration-stock",
        "birch-plywood",
        "screening-1.0.0",
        1_000_000,
        500_000,
        18_000,
    )
    valid_plan = _registration(
        "fixture:two-pin",
        (Point2D(50_000, 50_000), Point2D(950_000, 50_000)),
    )
    stock = replace(stock, clamp_zones=registration_pin_keep_out_rectangles(valid_plan))
    validation_program_requested: object = True
    registrations: object = {stock.stock_id: {0: valid_plan}}
    if case == "program-not-boolean":
        validation_program_requested = 1
    elif case == "registrations-not-mapping":
        registrations = [stock.stock_id]
    elif case == "unknown-stock":
        registrations = {"unknown-stock": {0: valid_plan}}
    elif case == "sheets-not-mapping":
        registrations = {stock.stock_id: [valid_plan]}
    elif case == "sheet-index-not-integer":
        registrations = {stock.stock_id: {"0": valid_plan}}
    elif case == "sheet-index-out-of-range":
        registrations = {stock.stock_id: {stock.quantity: valid_plan}}
    elif case == "plan-wrong-type":
        registrations = {stock.stock_id: {0: object()}}
    elif case == "point-outside-sheet":
        registrations = {
            stock.stock_id: {
                0: _registration(
                    "fixture:outside",
                    (Point2D(50_000, 50_000), Point2D(stock.width_um + 1, 50_000)),
                )
            }
        }

    with pytest.raises(ValueError, match="boolean|mapping|stock|sheet|plan"):
        generation_plan_artifact(
            machine=machine,
            stocks=(stock,),
            two_sided_registration_by_stock=registrations,  # type: ignore[arg-type]
            validation_program_requested=validation_program_requested,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("method_id", "points", "message"),
    (
        (
            "fixture/unsafe",
            (Point2D(50_000, 50_000), Point2D(950_000, 50_000)),
            "method ID",
        ),
        (
            "fixture:duplicate",
            (Point2D(50_000, 50_000), Point2D(50_000, 50_000)),
            "unique",
        ),
    ),
)
def test_registration_value_object_rejects_invalid_identity_or_points(
    method_id: str, points: tuple[Point2D, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _registration(method_id, points)


def test_generation_plan_omits_empty_registration_groups_canonically() -> None:
    machine = linuxcnc_reference_router_1325()
    stock = StockSheet(
        "registration-stock",
        "birch-plywood",
        "screening-1.0.0",
        1_000_000,
        500_000,
        18_000,
    )

    artifact = generation_plan_artifact(
        machine=machine,
        stocks=(stock,),
        two_sided_registration_by_stock={stock.stock_id: {}},
        validation_program_requested=False,
    )

    assert json.loads(artifact.data)["two_sided_registrations"] == []


def review_status_artifact(
    *,
    blocked: bool = True,
    blocker_code: str = "TWO_SIDED_REGISTRATION_MISSING",
) -> ArtifactFile:
    status = (
        blocked_design_review_package_status((blocker_code,))
        if blocked
        else generated_design_review_package_status(validation_program_included=False)
    )
    return ArtifactFile(
        DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
        canonical_json_bytes(status.as_dict()),
        "application/json",
        DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    )


def grouped_bom_group_id(signature: object) -> str:
    return f"bom-group:{sha256_hex(canonical_json_bytes(signature))[:16]}"


def status_review_core_artifacts(
    *,
    blocked: bool = True,
    blocker_code: str = "TWO_SIDED_REGISTRATION_MISSING",
    edge_band_selection_required: bool = False,
    external_evidence: tuple[dict[str, str], ...] = (),
) -> tuple[ArtifactFile, ...]:
    mode = blocker_code if blocked else "GENERATED"
    _, artifacts, _ = _review_case(
        mode,
        edge_band_selection_required=edge_band_selection_required,
        external_evidence=external_evidence,
    )
    without_status = tuple(
        artifact
        for artifact in artifacts
        if artifact.path != DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH
    )
    if blocked:
        return without_status
    return tuple(
        artifact
        for artifact in without_status
        if blocked_cam_artifact_violation(
            artifact.path,
            artifact.role,
            artifact.media_type,
        )
        is None
    )


def rebind_supplier_handoff_inventory(
    artifacts: Iterable[ArtifactFile],
) -> tuple[ArtifactFile, ...]:
    """Rebuild the handoff inventory after a test appends safe worker evidence."""

    values = tuple(artifacts)
    handoffs = tuple(artifact for artifact in values if artifact.path == SUPPLIER_HANDOFF_PATH)
    assert len(handoffs) == 1
    handoff = handoffs[0]
    payload = json.loads(handoff.data)
    entries = sorted(
        (
            {
                "path": artifact.path,
                "media_type": artifact.media_type,
                "role": artifact.role,
                "size_bytes": len(artifact.data),
                "sha256": sha256_hex(artifact.data),
            }
            for artifact in values
            if artifact.path != SUPPLIER_HANDOFF_PATH
        ),
        key=lambda entry: entry["path"],
    )
    binding = payload["payload_inventory_binding"]
    binding["artifact_count"] = len(entries)
    binding["artifacts"] = entries
    binding["payload_inventory_sha256"] = sha256_hex(canonical_json_bytes(entries))
    rebound = replace(handoff, data=canonical_json_bytes(payload))
    return tuple(
        sorted(
            (
                rebound if artifact.path == SUPPLIER_HANDOFF_PATH else artifact
                for artifact in values
            ),
            key=lambda artifact: artifact.path,
        )
    )


def test_reader_rejects_statusless_review_only_downgrade() -> None:
    context, _, _ = package_fixture()
    payload = build_deterministic_zip(
        context,
        (ArtifactFile("data/file.txt", b"payload", "text/plain", "WORKER_NOTE"),),
    )

    with pytest.raises(ArtifactError, match="require one canonical design-review status"):
        read_and_verify_package(payload)


def test_inventory_validator_rejects_implicit_statusless_compatibility() -> None:
    _, artifacts, _ = package_fixture()
    entries = tuple(
        {
            "path": artifact.path,
            "role": artifact.role,
            "media_type": artifact.media_type,
        }
        for artifact in artifacts
    )

    with pytest.raises(ArtifactError, match="status is mandatory"):
        validate_design_review_status_inventory_entries(None, entries)


def test_reader_accepts_statused_full_cam_inventory() -> None:
    _, _, payload = package_fixture()

    manifest = read_and_verify_package(payload)

    assert manifest["schema_version"] == "custombuild.production-manifest.v5"
    assert manifest["schema_version"] == PRODUCTION_MANIFEST_SCHEMA_VERSION
    assert "cam/operations.json" in {entry["path"] for entry in manifest["artifacts"]}


def test_reader_rejects_status_stripped_full_cam_without_requested_cad() -> None:
    context, artifacts, _ = package_fixture()
    artifacts = tuple(
        artifact
        for artifact in artifacts
        if artifact.path != DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH
    )
    payload = build_deterministic_zip(
        replace(context, cad_status="NOT_REQUESTED"),
        artifacts,
    )

    with pytest.raises(ArtifactError, match="require one canonical design-review status"):
        read_and_verify_package(payload)


def test_reader_rejects_statusless_cam_inventory_without_validation_program() -> None:
    context, artifacts, _ = package_fixture()
    without_program = tuple(
        artifact for artifact in artifacts if not artifact.path.startswith("machine-validation/")
    )

    with pytest.raises(ArtifactError, match="validation_program_included"):
        read_and_verify_package(build_deterministic_zip(context, without_program))


def test_reader_rejects_statusless_cam_with_contradictory_readiness() -> None:
    context, artifacts, _ = package_fixture()
    blocked_readiness = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
    )
    contradictory = tuple(
        replace(artifact, data=canonical_json_bytes(blocked_readiness.as_dict()))
        if artifact.path == "validation/workshop-readiness.json"
        else artifact
        for artifact in artifacts
    )

    with pytest.raises(ArtifactError, match="status and workshop readiness disagree"):
        read_and_verify_package(build_deterministic_zip(context, contradictory))


@pytest.mark.parametrize(
    ("path", "role", "media_type"),
    (
        ("machine-validation/injected.validation.ngc", "WORKER_NOTE", "text/plain"),
        ("cam/operations.json", "WORKER_NOTE", "application/json"),
        ("cam/setups/injected.svg", "WORKER_NOTE", "image/svg+xml"),
        ("nesting/injected.svg", "WORKER_NOTE", "image/svg+xml"),
        ("cam/rogue.ngc", "WORKER_NOTE", "text/plain"),
        ("CAM/rogue.NGC", "WORKER_NOTE", "text/plain"),
        ("review/rogue.NGC", "WORKER_NOTE", "text/plain"),
        ("stock/rogue.json", "WORKER_NOTE", "application/json"),
        ("PLACEMENTS/rogue.json", "WORKER_NOTE", "application/json"),
        ("materials/STOCK-identity.json", "WORKER_NOTE", "application/json"),
        ("review/rogue.json", "STOCK_PROFILE", "application/json"),
        ("review/rogue.json", "PLACEMENT_MAP", "application/json"),
        ("review/backplot.svg", "VALIDATION_BACKPLOT", "image/svg+xml"),
        ("toolpaths/finish.nc", "GCODE", "text/x-gcode"),
        ("review/operations.json", "WORKER_NOTE", "application/json"),
        ("review/tool-list.csv", "TOOLING_PLAN", "text/csv"),
        ("setups/sheet.svg", "SETUP_PLAN", "image/svg+xml"),
        (
            "quality/operation-measurements.json",
            "OPERATION_QA_PLAN",
            "application/json",
        ),
    ),
)
def test_reader_rejects_self_consistent_blocked_package_with_cam_artifact(
    path: str,
    role: str,
    media_type: str,
) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    artifacts = (
        *status_review_core_artifacts(),
        review_status_artifact(),
        ArtifactFile(path, b"payload", media_type, role),
    )
    payload = build_deterministic_zip(context, artifacts)

    with pytest.raises(ArtifactError, match="blocked CAM package"):
        read_and_verify_package(payload)


@pytest.mark.parametrize(
    ("path", "role", "media_type"),
    (
        ("assembly/assembly-manual.pdf", "ASSEMBLY_REVIEW_MANUAL", "application/pdf"),
        ("assembly/assembly-readiness.json", "ASSEMBLY_READINESS", "application/json"),
        ("bom/bom.pdf", "BOM_PDF", "application/pdf"),
        ("bom/hardware-list.csv", "HARDWARE_LIST", "text/csv"),
        ("labels/part-labels.pdf", "PART_LABELS", "application/pdf"),
        ("qa/measurement-protocol.pdf", "QA_PROTOCOL", "application/pdf"),
        (
            "validation/construction-report.json",
            "CONSTRUCTION_VALIDATION_REPORT",
            "application/json",
        ),
        (
            "validation/construction-report.pdf",
            "CONSTRUCTION_VALIDATION_REPORT",
            "application/pdf",
        ),
        ("validation/source-provenance.json", "SOURCE_PROVENANCE", "application/json"),
        ("parts/example/A.dxf", "PART_DXF", "image/vnd.dxf"),
        ("parts/example/B.dxf", "PART_DXF", "image/vnd.dxf"),
        ("drawings/example/A.svg", "PART_DRAWING", "image/svg+xml"),
        ("drawings/example/B.svg", "PART_DRAWING", "image/svg+xml"),
    ),
)
def test_blocked_cam_allowlist_accepts_only_canonical_review_artifacts(
    path: str,
    role: str,
    media_type: str,
) -> None:
    assert blocked_cam_artifact_violation(path, role, media_type) is None


@pytest.mark.parametrize(
    ("path", "role", "media_type"),
    (
        ("parts/example/C.dxf", "PART_DXF", "image/vnd.dxf"),
        ("parts/example/A.dxf", "PART_DRAWING", "image/vnd.dxf"),
        ("parts/example/A.dxf", "PART_DXF", "application/octet-stream"),
        ("parts/_unsafe/A.dxf", "PART_DXF", "image/vnd.dxf"),
        ("drawings/example/a.svg", "PART_DRAWING", "image/svg+xml"),
        ("Assembly/assembly-manual.pdf", "ASSEMBLY_REVIEW_MANUAL", "application/pdf"),
    ),
)
def test_blocked_cam_allowlist_rejects_noncanonical_review_aliases(
    path: str,
    role: str,
    media_type: str,
) -> None:
    assert blocked_cam_artifact_violation(path, role, media_type) is not None


def test_reader_accepts_blocked_package_with_machine_independent_worker_document() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    artifacts = rebind_supplier_handoff_inventory(
        (
            *status_review_core_artifacts(),
            review_status_artifact(),
            ArtifactFile(
                "assembly/assembly-manual.pdf",
                b"%PDF-1.4\n",
                "application/pdf",
                "ASSEMBLY_REVIEW_MANUAL",
            ),
        )
    )
    payload = build_deterministic_zip(context, artifacts)

    manifest = read_and_verify_package(payload)

    assert "assembly/assembly-manual.pdf" in {entry["path"] for entry in manifest["artifacts"]}


def test_reader_accepts_stockless_review_with_safe_labels_and_empty_qa_protocol() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    artifacts = rebind_supplier_handoff_inventory(
        (
            *status_review_core_artifacts(blocker_code="STOCK_PROFILE_MISSING"),
            review_status_artifact(blocker_code="STOCK_PROFILE_MISSING"),
            ArtifactFile(
                "labels/part-labels.pdf",
                b"%PDF-1.4\n",
                "application/pdf",
                "PART_LABELS",
            ),
            ArtifactFile(
                "qa/measurement-protocol.pdf",
                b"%PDF-1.4\n",
                "application/pdf",
                "QA_PROTOCOL",
            ),
        )
    )

    manifest = read_and_verify_package(build_deterministic_zip(context, artifacts))

    assert {entry["path"] for entry in manifest["artifacts"]} >= {
        "labels/part-labels.pdf",
        "qa/measurement-protocol.pdf",
    }


def test_reader_accepts_canonical_grain_blocked_zero_cam_review() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    artifacts = (
        *status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE),
        review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE),
    )

    manifest = read_and_verify_package(build_deterministic_zip(context, artifacts))

    paths = {entry["path"] for entry in manifest["artifacts"]}
    assert "validation/dfm-report.json" in paths
    assert not any(path.startswith(("cam/", "nesting/", "machine-validation/")) for path in paths)
    assert manifest["physical_cutting_authorized"] is False


def test_reader_rejects_checksum_consistent_noncanonical_grain_issue() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    canonical_core = status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE)
    canonical_report = json.loads(
        next(
            artifact.data
            for artifact in canonical_core
            if artifact.path == "validation/dfm-report.json"
        )
    )
    mutated_reports: list[dict[str, Any]] = []
    for field, value in (
        ("message", "Plausible invented grain wording."),
        ("suggestion", "Acknowledge this warning."),
    ):
        payload = json.loads(json.dumps(canonical_report))
        payload["issues"][0][field] = value
        mutated_reports.append(payload)
    input_mutations: tuple[tuple[str, Any], ...] = (
        ("assessment_phase", "STOCK_SELECTION_INCOMPLETE"),
        ("stock_id", None),
        ("affected_part_ids", ["z", "a"]),
        ("required_part_grain_directions", ["Z"]),
    )
    for key, value in input_mutations:
        payload = json.loads(json.dumps(canonical_report))
        payload["issues"][0]["inputs"][key] = value
        mutated_reports.append(payload)
    unexpected_input = json.loads(json.dumps(canonical_report))
    unexpected_input["issues"][0]["inputs"]["acknowledged"] = True
    mutated_reports.append(unexpected_input)

    for report in mutated_reports:
        core = tuple(
            replace(artifact, data=canonical_json_bytes(report))
            if artifact.path == "validation/dfm-report.json"
            else artifact
            for artifact in canonical_core
        )
        with pytest.raises(ArtifactError):
            read_and_verify_package(
                build_deterministic_zip(
                    context,
                    (*core, review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE)),
                )
            )


@pytest.mark.parametrize(
    ("blocker_code", "claimed_grain_binding_required"),
    (
        ("STOCK_PROFILE_MISSING", False),
        (DFM_GRAIN_BLOCKER_CODE, False),
    ),
)
def test_reader_rejects_readiness_grain_applicability_drift_from_grouped_bom(
    blocker_code: str,
    claimed_grain_binding_required: bool,
) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    forged_readiness = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=False,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        material_grain_binding_required=claimed_grain_binding_required,
    )
    core = tuple(
        replace(artifact, data=canonical_json_bytes(forged_readiness.as_dict()))
        if artifact.path == "validation/workshop-readiness.json"
        else artifact
        for artifact in status_review_core_artifacts(blocker_code=blocker_code)
    )

    with pytest.raises(ArtifactError, match="manifest external evidence"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*core, review_status_artifact(blocker_code=blocker_code)),
            )
        )


def test_reader_rejects_grain_blocker_contradicting_nondirectional_grouped_bom() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE)
    grouped_bom = json.loads(
        next(artifact.data for artifact in core if artifact.path == "bom/grouped-bom.json")
    )
    grouped_bom["groups"][0]["signature"]["grain_direction"] = "NONE"
    grouped_bom["groups"][0]["group_id"] = grouped_bom_group_id(
        grouped_bom["groups"][0]["signature"]
    )
    grouped_bom["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped_bom["groups"]))
    nondirectional_readiness = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=False,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        material_grain_binding_required=True,
    )
    forged_core = tuple(
        replace(artifact, data=canonical_json_bytes(grouped_bom))
        if artifact.path == "bom/grouped-bom.json"
        else replace(artifact, data=canonical_json_bytes(nondirectional_readiness.as_dict()))
        if artifact.path == "validation/workshop-readiness.json"
        else artifact
        for artifact in core
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *forged_core,
                    review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE),
                ),
            )
        )


def test_reader_rejects_rehashed_grouped_bom_axis_drift_from_grain_issue() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE)
    grouped_bom = json.loads(
        next(artifact.data for artifact in core if artifact.path == "bom/grouped-bom.json")
    )
    assert grouped_bom["groups"][0]["signature"]["grain_direction"] == "X"
    grouped_bom["groups"][0]["signature"]["grain_direction"] = "Y"
    grouped_bom["groups"][0]["group_id"] = grouped_bom_group_id(
        grouped_bom["groups"][0]["signature"]
    )
    grouped_bom["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped_bom["groups"]))
    forged_core = tuple(
        replace(artifact, data=canonical_json_bytes(grouped_bom))
        if artifact.path == "bom/grouped-bom.json"
        else artifact
        for artifact in core
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *forged_core,
                    review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE),
                ),
            )
        )


def test_reader_rejects_grain_issue_omitting_an_identical_grouped_bom_part() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE)
    grouped_bom = json.loads(
        next(artifact.data for artifact in core if artifact.path == "bom/grouped-bom.json")
    )
    grouped_bom["groups"][0]["part_ids"] = ["example", "example-2"]
    grouped_bom["groups"][0]["quantity"] = 2
    grouped_bom["part_instance_count"] = 2
    grouped_bom["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped_bom["groups"]))
    forged_core = tuple(
        replace(artifact, data=canonical_json_bytes(grouped_bom))
        if artifact.path == "bom/grouped-bom.json"
        else artifact
        for artifact in core
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *forged_core,
                    review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE),
                ),
            )
        )


def test_reader_rejects_rehashed_split_identical_grouped_bom_rows() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE)
    grouped_bom = json.loads(
        next(artifact.data for artifact in core if artifact.path == "bom/grouped-bom.json")
    )
    split_row = json.loads(json.dumps(grouped_bom["groups"][0]))
    split_row["group_id"] = "bom-group:ffffffffffffffff"
    split_row["part_ids"] = ["example-2"]
    grouped_bom["groups"].append(split_row)
    grouped_bom["group_count"] = 2
    grouped_bom["part_instance_count"] = 2
    grouped_bom["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped_bom["groups"]))
    forged_core = tuple(
        replace(artifact, data=canonical_json_bytes(grouped_bom))
        if artifact.path == "bom/grouped-bom.json"
        else artifact
        for artifact in core
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *forged_core,
                    review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE),
                ),
            )
        )


def test_reader_rejects_rehashed_noncanonical_grouped_bom_group_id() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE)
    grouped_bom = json.loads(
        next(artifact.data for artifact in core if artifact.path == "bom/grouped-bom.json")
    )
    grouped_bom["groups"][0]["group_id"] = "bom-group:0000000000000000"
    grouped_bom["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped_bom["groups"]))
    forged_core = tuple(
        replace(artifact, data=canonical_json_bytes(grouped_bom))
        if artifact.path == "bom/grouped-bom.json"
        else artifact
        for artifact in core
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *forged_core,
                    review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE),
                ),
            )
        )


def test_reader_rejects_rehashed_noncanonical_grouped_bom_row_order() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = status_review_core_artifacts(blocker_code=DFM_GRAIN_BLOCKER_CODE)
    grouped_bom = json.loads(
        next(artifact.data for artifact in core if artifact.path == "bom/grouped-bom.json")
    )
    second_row = json.loads(json.dumps(grouped_bom["groups"][0]))
    second_row["signature"]["name"] = "SECOND DISTINCT PANEL"
    second_row["group_id"] = grouped_bom_group_id(second_row["signature"])
    second_row["part_ids"] = ["other-bound-part"]
    grouped_bom["groups"] = sorted(
        [grouped_bom["groups"][0], second_row],
        key=lambda row: row["group_id"],
        reverse=True,
    )
    grouped_bom["group_count"] = 2
    grouped_bom["part_instance_count"] = 2
    grouped_bom["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped_bom["groups"]))
    forged_core = tuple(
        replace(artifact, data=canonical_json_bytes(grouped_bom))
        if artifact.path == "bom/grouped-bom.json"
        else artifact
        for artifact in core
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *forged_core,
                    review_status_artifact(blocker_code=DFM_GRAIN_BLOCKER_CODE),
                ),
            )
        )


def test_reader_rejects_stock_status_grain_warning_for_nondirectional_grouped_bom() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = status_review_core_artifacts(blocker_code="STOCK_PROFILE_MISSING")
    report = json.loads(
        next(artifact.data for artifact in core if artifact.path == "validation/dfm-report.json")
    )
    directional_part = PartSpec(
        "invented-directional-part",
        "Invented directional part",
        500_000,
        300_000,
        18_000,
        "birch-plywood",
        "screening-1.0.0",
        grain_direction="X",
    )
    grain_warning = stock_grain_binding_issues(
        (directional_part,),
        None,
        severity=Severity.WARNING,
    )[0]
    report["issues"].append(json.loads(canonical_json_bytes(grain_warning)))
    forged_core = tuple(
        replace(artifact, data=canonical_json_bytes(report))
        if artifact.path == "validation/dfm-report.json"
        else artifact
        for artifact in core
    )

    with pytest.raises(ArtifactError, match="exactly cover|grouped BOM"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *forged_core,
                    review_status_artifact(blocker_code="STOCK_PROFILE_MISSING"),
                ),
            )
        )


@pytest.mark.parametrize(
    "report",
    (
        DFMReport((), engine_version="dfm-1.3.0"),
        DFMReport(
            (
                DFMIssue(
                    "OTHER_BLOCKER",
                    Severity.BLOCK,
                    "A different blocker.",
                ),
            ),
            engine_version="dfm-1.3.0",
        ),
        DFMReport(
            (
                DFMIssue(
                    "STOCK_PROFILE_MISSING",
                    Severity.WARNING,
                    "Stock warning is not the canonical blocker.",
                ),
            ),
            engine_version="dfm-1.3.0",
        ),
    ),
)
def test_reader_rejects_stock_status_without_exact_raw_blocking_dfm(
    report: DFMReport,
) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    core = tuple(
        replace(artifact, data=canonical_json_bytes(report))
        if artifact.path == "validation/dfm-report.json"
        else artifact
        for artifact in status_review_core_artifacts(blocker_code="STOCK_PROFILE_MISSING")
    )

    with pytest.raises(ArtifactError, match="STOCK_PROFILE_MISSING"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*core, review_status_artifact(blocker_code="STOCK_PROFILE_MISSING")),
            )
        )


@pytest.mark.parametrize(
    "status_artifact",
    (
        review_status_artifact(),
        review_status_artifact(blocked=False),
    ),
)
def test_reader_rejects_raw_stock_dfm_with_non_stock_status(
    status_artifact: ArtifactFile,
) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")

    with pytest.raises(ArtifactError):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *status_review_core_artifacts(blocker_code="STOCK_PROFILE_MISSING"),
                    status_artifact,
                ),
            )
        )


def test_reader_rejects_stock_status_with_dfm_verified_readiness() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    verified_readiness = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=True,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        material_grain_binding_required=True,
    )
    core = tuple(
        replace(artifact, data=canonical_json_bytes(verified_readiness.as_dict()))
        if artifact.path == "validation/workshop-readiness.json"
        else artifact
        for artifact in status_review_core_artifacts(blocker_code="STOCK_PROFILE_MISSING")
    )

    with pytest.raises(ArtifactError, match="status and workshop readiness disagree"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*core, review_status_artifact(blocker_code="STOCK_PROFILE_MISSING")),
            )
        )


@pytest.mark.parametrize("evidence_type", ("wall_anchor", "hardware", "material_grain"))
def test_reader_rejects_workshop_verification_missing_from_manifest(
    evidence_type: str,
) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    fabricated_evidence = {
        "evidence_type": evidence_type,
        "catalog_id": f"catalog-{evidence_type}",
        "catalog_version": "1.0.0",
        "sha256": "9" * 64,
    }
    fabricated_readiness = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=False,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        external_evidence=(fabricated_evidence,),
    )
    core = tuple(
        replace(artifact, data=canonical_json_bytes(fabricated_readiness.as_dict()))
        if artifact.path == "validation/workshop-readiness.json"
        else artifact
        for artifact in status_review_core_artifacts(blocker_code="STOCK_PROFILE_MISSING")
    )

    with pytest.raises(ArtifactError, match="manifest external evidence"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*core, review_status_artifact(blocker_code="STOCK_PROFILE_MISSING")),
            )
        )


def test_reader_accepts_workshop_verification_bound_to_manifest_evidence() -> None:
    bound_evidence = {
        "evidence_type": "wall_anchor",
        "catalog_id": "anchor-system",
        "catalog_version": "1.0.0",
        "sha256": "8" * 64,
    }
    context, _, _ = _review_case(
        "STOCK_PROFILE_MISSING",
        external_evidence=(bound_evidence,),
    )
    core = status_review_core_artifacts(
        blocker_code="STOCK_PROFILE_MISSING",
        external_evidence=(bound_evidence,),
    )

    manifest = read_and_verify_package(
        build_deterministic_zip(
            context,
            (*core, review_status_artifact(blocker_code="STOCK_PROFILE_MISSING")),
        )
    )

    assert manifest["external_evidence"] == [bound_evidence]


@pytest.mark.parametrize(
    "requirement_code",
    (
        "MACHINE_CALIBRATION",
        "WCS_CONVENTION",
        "MEASURED_TOOLING",
        "MATERIAL_BATCH",
        "JOINT_COUPONS",
        "MATERIAL_REMOVAL_COMPARISON",
        "SUPERVISED_AIR_CUT",
        "REFERENCE_PART",
        "PROTOTYPE_BUILD",
        "CNC_OPERATOR_APPROVAL",
        "FURNITURE_CONSTRUCTOR_APPROVAL",
        "EDGE_BAND_SYSTEM",
    ),
)
def test_reader_rejects_fabricated_physical_workshop_claim(
    requirement_code: str,
) -> None:
    context, _, _ = _review_case(
        "STOCK_PROFILE_MISSING",
        edge_band_selection_required=True,
    )
    readiness = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=False,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        edge_band_selection_required=True,
    ).as_dict()
    requirements = readiness["workshop_evidence"]
    assert isinstance(requirements, list)
    requirement = next(item for item in requirements if item["code"] == requirement_code)
    requirement.update(
        status="VERIFIED",
        evidence="Invented physical verification.",
        required_action="None for this invented check.",
    )
    readiness["missing_evidence_count"] -= 1
    core = tuple(
        replace(artifact, data=canonical_json_bytes(readiness))
        if artifact.path == "validation/workshop-readiness.json"
        else artifact
        for artifact in status_review_core_artifacts(
            blocker_code="STOCK_PROFILE_MISSING",
            edge_band_selection_required=True,
        )
    )

    with pytest.raises(ArtifactError, match="manifest external evidence"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*core, review_status_artifact(blocker_code="STOCK_PROFILE_MISSING")),
            )
        )


def test_reader_accepts_edge_band_requirement_bound_to_grouped_bom() -> None:
    context, _, _ = _review_case(
        "STOCK_PROFILE_MISSING",
        edge_band_selection_required=True,
    )

    manifest = read_and_verify_package(
        build_deterministic_zip(
            context,
            (
                *status_review_core_artifacts(
                    blocker_code="STOCK_PROFILE_MISSING",
                    edge_band_selection_required=True,
                ),
                review_status_artifact(blocker_code="STOCK_PROFILE_MISSING"),
            ),
        )
    )

    assert "bom/grouped-bom.json" in {entry["path"] for entry in manifest["artifacts"]}


def test_reader_rejects_suppressed_edge_band_requirement() -> None:
    context, _, _ = _review_case(
        "STOCK_PROFILE_MISSING",
        edge_band_selection_required=True,
    )
    suppressed = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=False,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        edge_band_selection_required=False,
    )
    core = tuple(
        replace(artifact, data=canonical_json_bytes(suppressed.as_dict()))
        if artifact.path == "validation/workshop-readiness.json"
        else artifact
        for artifact in status_review_core_artifacts(
            blocker_code="STOCK_PROFILE_MISSING",
            edge_band_selection_required=True,
        )
    )

    with pytest.raises(ArtifactError, match="manifest external evidence"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*core, review_status_artifact(blocker_code="STOCK_PROFILE_MISSING")),
            )
        )


@pytest.mark.parametrize(
    ("media_type", "role"),
    (
        ("text/plain", DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE),
        ("application/json", "WORKER_NOTE"),
    ),
)
def test_reader_requires_canonical_status_entry_identity(
    media_type: str,
    role: str,
) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    canonical_status = review_status_artifact()
    payload = build_deterministic_zip(
        context,
        (
            *status_review_core_artifacts(),
            ArtifactFile(
                canonical_status.path,
                canonical_status.data,
                media_type,
                role,
            ),
        ),
    )

    with pytest.raises(ArtifactError, match="status entry is not canonical"):
        read_and_verify_package(payload)


def test_reader_rejects_noncanonical_status_bytes_and_duplicate_status_role() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    canonical_status = review_status_artifact()
    noncanonical = ArtifactFile(
        canonical_status.path,
        canonical_status.data + b"\n",
        canonical_status.media_type,
        canonical_status.role,
    )
    with pytest.raises(ArtifactError, match="not canonical UTF-8 JSON"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*status_review_core_artifacts(), noncanonical),
            )
        )

    duplicate_role = ArtifactFile(
        "validation/status-copy.json",
        canonical_status.data,
        "application/json",
        DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    )
    with pytest.raises(ArtifactError, match="one canonical design-review status|not unique"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*status_review_core_artifacts(), canonical_status, duplicate_role),
            )
        )


def test_reader_rejects_duplicate_or_aliased_workshop_readiness_role() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    rogue_readiness = ArtifactFile(
        "review/rogue-readiness.json",
        canonical_json_bytes({"physical_cutting_authorized": True}),
        "application/json",
        "WORKSHOP_READINESS_REPORT",
    )

    with pytest.raises(ArtifactError, match="readiness artifact entry is not unique"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *status_review_core_artifacts(),
                    review_status_artifact(),
                    rogue_readiness,
                ),
            )
        )


def test_reader_rejects_duplicate_or_aliased_dfm_report_role() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    rogue_report = ArtifactFile(
        "review/rogue-dfm.json",
        canonical_json_bytes(DFMReport((), engine_version="dfm-1.3.0")),
        "application/json",
        "DFM_VALIDATION_REPORT",
    )

    with pytest.raises(ArtifactError, match="blocked CAM package"):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (
                    *status_review_core_artifacts(),
                    review_status_artifact(),
                    rogue_report,
                ),
            )
        )


def test_reader_rejects_generated_status_without_claimed_cam_inventory() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    payload = build_deterministic_zip(
        context,
        (
            *status_review_core_artifacts(blocked=False),
            review_status_artifact(blocked=False),
        ),
    )

    with pytest.raises(ArtifactError, match="does not match operations_included"):
        read_and_verify_package(payload)


def test_reader_rejects_status_package_without_authoritative_cad_claim_and_files() -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="NOT_REQUESTED")
    complete = (*status_review_core_artifacts(), review_status_artifact())
    with pytest.raises(ArtifactError, match="requires generated authoritative CAD"):
        read_and_verify_package(build_deterministic_zip(context, complete))

    without_step_and_glb = tuple(
        artifact
        for artifact in complete
        if artifact.path not in {"model/design.step", "model/design.glb"}
    )
    with pytest.raises(ArtifactError, match="complete review artifacts|AUTHORITATIVE_STEP"):
        read_and_verify_package(
            build_deterministic_zip(
                replace(context, cad_status="GENERATED"),
                without_step_and_glb,
            )
        )


def zip_entries(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def make_zip(
    entries: list[tuple[str, bytes]],
    *,
    unix_modes: dict[str, int] | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (unix_modes or {}).get(name, 0o100644) << 16
            info.flag_bits = 0x800
            archive.writestr(
                info,
                data,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    return output.getvalue()


def run_offline_verifier(
    tmp_path: Path,
    payload: bytes,
    *arguments: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    package_path = tmp_path / "custombuild-test-package.zip"
    package_path.write_bytes(payload)
    verifier_file = (
        Path(__file__).resolve().parents[2] / "scripts" / "verify_production_package.py"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned package path
        [sys.executable, "-I", verifier_file, str(package_path), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)

    canonical_stdout = io.StringIO()
    canonical_exit_code = offline_package_verifier.main(
        [str(package_path), *arguments],
        stdout=canonical_stdout,
    )
    canonical_output = canonical_stdout.getvalue()
    canonical_result = json.loads(canonical_output)

    assert canonical_exit_code == completed.returncode
    assert canonical_output.encode("utf-8") == completed.stdout.encode("utf-8")
    assert canonical_result == result
    return completed, result


def test_distributed_trusted_verifier_is_byte_identical_to_canonical_source() -> None:
    canonical_file = offline_package_verifier.__file__
    assert canonical_file is not None
    distributed_file = (
        Path(__file__).resolve().parents[2] / "scripts" / "verify_production_package.py"
    )

    assert distributed_file.read_bytes() == Path(canonical_file).read_bytes()


@pytest.mark.parametrize(
    ("payload", "error_code"),
    (
        (b"", "PACKAGE_SIZE_INVALID"),
        (b"not-a-zip", "INVALID_ZIP"),
        (make_zip([("unlisted.txt", b"payload")]), "MISSING_MANIFEST"),
        (
            make_zip([("nested/", b"")]),
            "UNSAFE_ZIP_PATH",
        ),
        (
            make_zip(
                [("symlink", b"target")],
                unix_modes={"symlink": 0o120777},
            ),
            "UNSAFE_ZIP_ENTRY",
        ),
    ),
)
def test_offline_verifier_rejects_invalid_package_and_zip_metadata(
    tmp_path: Path,
    payload: bytes,
    error_code: str,
) -> None:
    completed, result = run_offline_verifier(tmp_path, payload)

    assert completed.returncode == 3
    assert result["status"] == "FAIL"
    assert result["error"]["code"] == error_code


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "/absolute.txt",
        "windows\\separator.txt",
        "drive:prefix.txt",
        "double//separator.txt",
    ),
)
def test_offline_verifier_rejects_additional_unsafe_zip_path_forms(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    completed, result = run_offline_verifier(
        tmp_path,
        make_zip([(unsafe_path, b"untrusted")]),
    )

    assert completed.returncode == 3
    assert result["error"]["code"] == "UNSAFE_ZIP_PATH"


def test_offline_verifier_enforces_zip_entry_count_limit(tmp_path: Path) -> None:
    payload = make_zip(
        [
            (f"untrusted/{index:05d}.txt", b"")
            for index in range(offline_package_verifier.MAX_FILES + 1)
        ]
    )

    completed, result = run_offline_verifier(tmp_path, payload)

    assert completed.returncode == 3
    assert result["error"]["code"] == "TOO_MANY_ZIP_ENTRIES"


def test_offline_verifier_enforces_per_entry_size_limit(tmp_path: Path) -> None:
    payload = make_zip(
        [
            (
                "oversized.bin",
                b"x" * (offline_package_verifier.MAX_ENTRY_BYTES + 1),
            )
        ]
    )

    completed, result = run_offline_verifier(tmp_path, payload)

    assert completed.returncode == 3
    assert result["error"]["code"] == "ZIP_ENTRY_TOO_LARGE"


def test_offline_verifier_rejects_unsafe_compression_ratio(tmp_path: Path) -> None:
    payload = make_zip([("compression-bomb.bin", b"x" * (4 * 1024 * 1024))])

    completed, result = run_offline_verifier(tmp_path, payload)

    assert completed.returncode == 3
    assert result["error"]["code"] == "UNSAFE_COMPRESSION_RATIO"


@pytest.mark.parametrize(
    ("manifest_bytes", "error_code"),
    (
        (b"\xff", "INVALID_MANIFEST_JSON"),
        (b"{", "INVALID_MANIFEST_JSON"),
        (b"[]", "INVALID_MANIFEST_STRUCTURE"),
    ),
)
def test_offline_verifier_rejects_non_strict_manifest_json(
    tmp_path: Path,
    manifest_bytes: bytes,
    error_code: str,
) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    files["manifest.json"] = manifest_bytes

    completed, result = run_offline_verifier(tmp_path, make_zip(sorted(files.items())))

    assert completed.returncode == 3
    assert result["error"]["code"] == error_code


def test_offline_verifier_rejects_duplicate_manifest_json_key(tmp_path: Path) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    files["manifest.json"] = b'{"schema_version":"duplicate",' + files["manifest.json"][1:]

    completed, result = run_offline_verifier(tmp_path, make_zip(sorted(files.items())))

    assert completed.returncode == 3
    assert result["error"]["code"] == "DUPLICATE_MANIFEST_KEY"


def test_offline_verifier_rejects_non_finite_manifest_json(tmp_path: Path) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    files["manifest.json"] = files["manifest.json"].replace(
        b'"physical_cutting_authorized":false',
        b'"physical_cutting_authorized":NaN',
    )

    completed, result = run_offline_verifier(tmp_path, make_zip(sorted(files.items())))

    assert completed.returncode == 3
    assert result["error"]["code"] == "INVALID_MANIFEST_JSON"


@pytest.mark.parametrize("mutation", ("extra-field", "pretty-printed"))
def test_offline_verifier_rejects_noncanonical_manifest_structure_or_encoding(
    tmp_path: Path,
    mutation: str,
) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    manifest = json.loads(files["manifest.json"])
    if mutation == "extra-field":
        manifest["attacker_claim"] = True
        expected_code = "INVALID_MANIFEST_STRUCTURE"
        manifest_bytes = canonical_json_bytes(manifest)
    else:
        expected_code = "NON_CANONICAL_MANIFEST"
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    files["manifest.json"] = manifest_bytes

    completed, result = run_offline_verifier(tmp_path, make_zip(sorted(files.items())))

    assert completed.returncode == 3
    assert result["error"]["code"] == expected_code


def test_v5_zip_contains_no_executable_verifier_and_separate_trusted_verifier_passes(
    tmp_path: Path,
) -> None:
    context, _, payload = package_fixture()
    manifest = read_and_verify_package(payload)
    entries = zip_entries(payload)

    assert PACKAGE_BUILDER_VERSION == "deterministic-package-1.11.0"
    assert "__main__.py" not in entries
    assert all(entry["path"].casefold() != "__main__.py" for entry in manifest["artifacts"])

    completed, result = run_offline_verifier(
        tmp_path,
        payload,
        "--expect-project-id",
        context.project_id,
        "--expect-revision",
        context.revision,
        "--expect-design-hash",
        context.design_hash,
    )

    assert completed.returncode == 0
    assert result == {
        "authenticity": "NOT_AUTHENTICATED",
        "checksums_verified": True,
        "details": {
            "identity": {
                "design_hash": context.design_hash,
                "project_id": context.project_id,
                "revision": context.revision,
            },
            "manifest_schema_version": PRODUCTION_MANIFEST_SCHEMA_VERSION,
            "verified_artifact_count": len(manifest["artifacts"]),
        },
        "error": None,
        "exit_code": 0,
        "package": "custombuild-test-package.zip",
        "physical_cutting_authorized": False,
        "schema_version": OFFLINE_REPORT_SCHEMA_VERSION,
        "status": "PASS",
        "verifier_version": "custombuild-offline-package-verifier-1.1.0",
        "warnings": [
            (
                "SHA-256 verifies internal consistency relative to this unsigned manifest and "
                "can detect accidental corruption; it does not authenticate the publisher or "
                "issuer."
            ),
            (
                "A malicious party able to rewrite both payloads and the unsigned manifest can "
                "create a new internally consistent ZIP; this verifier cannot detect that "
                "coordinated rewrite."
            ),
            (
                "Expected project, revision and design-hash options compare unsigned manifest "
                "claims only; they do not independently reconstruct design semantics or establish "
                "authenticity."
            ),
            "A PASS does not authorize physical cutting, machining, or assembly.",
            (
                "This verifier does not establish current revocation or expiry status for "
                "external signed evidence."
            ),
            (
                "No registry embedded in a ZIP is trusted; use the authenticated server "
                "to recheck the certifier, signature, registry high-water, revocation and "
                "expiry before use."
            ),
        ],
    }


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (
        (
            lambda files: files.__setitem__(
                START_HERE_PATH,
                files[START_HERE_PATH] + b"\ntampered\n",
            ),
            "ARTIFACT_SIZE_MISMATCH",
        ),
        (
            lambda files: files.__setitem__("unlisted.txt", b"extra"),
            "UNLISTED_ARTIFACT",
        ),
        (
            lambda files: files.pop(START_HERE_PATH),
            "MISSING_ARTIFACT",
        ),
        (
            lambda files: files.__setitem__("../outside.txt", b"unsafe"),
            "UNSAFE_ZIP_PATH",
        ),
    ),
)
def test_offline_verifier_rejects_tamper_unsafe_extra_and_missing_files(
    tmp_path: Path,
    mutation,
    error_code: str,
) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    mutation(files)

    completed, result = run_offline_verifier(tmp_path, make_zip(list(files.items())))

    assert completed.returncode == 3
    assert result["status"] == "FAIL"
    assert result["checksums_verified"] is False
    assert result["physical_cutting_authorized"] is False
    assert result["authenticity"] == "NOT_AUTHENTICATED"
    assert result["error"]["code"] == error_code


def test_offline_verifier_rejects_duplicate_case_alias_paths(tmp_path: Path) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    duplicate = ("start-here.md", files[START_HERE_PATH])

    completed, result = run_offline_verifier(
        tmp_path,
        make_zip([*files.items(), duplicate]),
    )

    assert completed.returncode == 3
    assert result["error"]["code"] == "DUPLICATE_ZIP_PATH"


def test_offline_verifier_rejects_non_v5_manifest(tmp_path: Path) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    manifest = json.loads(files["manifest.json"])
    manifest["schema_version"] = "custombuild.production-manifest.v4"
    files["manifest.json"] = canonical_json_bytes(manifest)

    completed, result = run_offline_verifier(tmp_path, make_zip(list(files.items())))

    assert completed.returncode == 3
    assert result["error"]["code"] == "UNSUPPORTED_MANIFEST_SCHEMA"


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("artifact_schema_version", "custombuild.production-artifacts.v999"),
        ("release_scope", "physical_production"),
        ("machine_use", "cutting"),
        ("physical_cutting_authorized", True),
        ("checksum_scope", "selected files only"),
    ),
)
def test_offline_verifier_rejects_unsafe_manifest_claims(
    tmp_path: Path,
    field: str,
    unsafe_value: object,
) -> None:
    _, _, payload = package_fixture()
    forged = mutate_manifest(
        payload,
        lambda manifest: manifest.__setitem__(field, unsafe_value),
    )

    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == "UNSAFE_MANIFEST_CLAIM"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("project_id", ""),
        ("revision", ""),
        ("design_hash", "not-a-lowercase-sha256"),
    ),
)
def test_offline_verifier_rejects_invalid_manifest_identity(
    tmp_path: Path,
    field: str,
    invalid_value: str,
) -> None:
    _, _, payload = package_fixture()
    forged = mutate_manifest(
        payload,
        lambda manifest: manifest.__setitem__(field, invalid_value),
    )

    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == "INVALID_PACKAGE_IDENTITY"


@pytest.mark.parametrize("mutation", ("invalid-format", "context-mismatch"))
def test_offline_verifier_rejects_invalid_or_mismatched_context_hash(
    tmp_path: Path,
    mutation: str,
) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    manifest = json.loads(files["manifest.json"])
    if mutation == "invalid-format":
        manifest["production_context_hash"] = "not-a-digest"
        expected_code = "INVALID_CONTEXT_HASH"
    else:
        manifest["warnings"] = [*manifest["warnings"], "attacker-authored warning"]
        expected_code = "CONTEXT_HASH_MISMATCH"
    files["manifest.json"] = canonical_json_bytes(manifest)

    completed, result = run_offline_verifier(tmp_path, make_zip(sorted(files.items())))

    assert completed.returncode == 3
    assert result["error"]["code"] == expected_code


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (
        ("not-array", "INVALID_ARTIFACT_INVENTORY"),
        ("entry-not-object", "INVALID_ARTIFACT_INVENTORY"),
        ("unexpected-entry-field", "INVALID_ARTIFACT_INVENTORY"),
        ("manifest-inventories-itself", "INVALID_ARTIFACT_INVENTORY"),
        ("empty-media-type", "INVALID_ARTIFACT_INVENTORY"),
        ("empty-role", "INVALID_ARTIFACT_INVENTORY"),
        ("boolean-size", "INVALID_ARTIFACT_INVENTORY"),
        ("invalid-digest", "INVALID_ARTIFACT_INVENTORY"),
        ("duplicate-path", "DUPLICATE_MANIFEST_PATH"),
        ("case-alias-path", "DUPLICATE_MANIFEST_PATH"),
        ("unsorted-paths", "NON_CANONICAL_ARTIFACT_INVENTORY"),
    ),
)
def test_offline_verifier_rejects_malformed_artifact_inventory(
    tmp_path: Path,
    mutation: str,
    error_code: str,
) -> None:
    _, _, payload = package_fixture()

    def mutate_inventory(manifest: dict[str, Any]) -> None:
        artifacts = manifest["artifacts"]
        assert isinstance(artifacts, list)
        if mutation == "not-array":
            manifest["artifacts"] = {}
        elif mutation == "entry-not-object":
            artifacts[0] = "not-an-object"
        elif mutation == "unexpected-entry-field":
            artifacts[0]["attacker_claim"] = True
        elif mutation == "manifest-inventories-itself":
            artifacts[0]["path"] = "manifest.json"
        elif mutation == "empty-media-type":
            artifacts[0]["media_type"] = ""
        elif mutation == "empty-role":
            artifacts[0]["role"] = ""
        elif mutation == "boolean-size":
            artifacts[0]["size_bytes"] = True
        elif mutation == "invalid-digest":
            artifacts[0]["sha256"] = "ABC"
        elif mutation == "duplicate-path":
            artifacts.insert(1, dict(artifacts[0]))
        elif mutation == "case-alias-path":
            alias = dict(artifacts[0])
            alias["path"] = alias["path"].upper()
            artifacts.insert(1, alias)
        else:
            artifacts.reverse()

    forged = mutate_manifest(payload, mutate_inventory)
    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == error_code


def test_offline_verifier_rejects_equal_size_artifact_digest_tamper(tmp_path: Path) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    original = files[START_HERE_PATH]
    replacement = bytes([original[0] ^ 1]) + original[1:]
    assert len(replacement) == len(original)
    files[START_HERE_PATH] = replacement

    completed, result = run_offline_verifier(tmp_path, make_zip(sorted(files.items())))

    assert completed.returncode == 3
    assert result["error"]["code"] == "ARTIFACT_SHA256_MISMATCH"


@pytest.mark.parametrize(
    ("arguments", "error_code", "exit_code"),
    (
        (("--expect-project-id", "wrong-project"), "EXPECTED_IDENTITY_MISMATCH", 3),
        (("--expect-revision", "999"), "EXPECTED_IDENTITY_MISMATCH", 3),
        (("--expect-design-hash", "0" * 64), "EXPECTED_IDENTITY_MISMATCH", 3),
        (("--expect-design-hash", "not-a-digest"), "INVALID_ARGUMENTS", 2),
        (("--expect-project-id", ""), "INVALID_ARGUMENTS", 2),
        (("--expect-revision", ""), "INVALID_ARGUMENTS", 2),
        (("--unknown-option",), "INVALID_ARGUMENTS", 2),
    ),
)
def test_offline_verifier_fails_closed_on_wrong_or_invalid_expected_identity(
    tmp_path: Path,
    arguments: tuple[str, ...],
    error_code: str,
    exit_code: int,
) -> None:
    _, _, payload = package_fixture()

    completed, result = run_offline_verifier(tmp_path, payload, *arguments)

    assert completed.returncode == exit_code
    assert result["exit_code"] == exit_code
    assert result["status"] == "FAIL"
    assert result["error"]["code"] == error_code


def test_trusted_verifier_and_reader_reject_forged_zip_main_without_executing_it(
    tmp_path: Path,
) -> None:
    _, _, payload = package_fixture()
    execution_marker = tmp_path / "forged-main-executed"
    forged_source = (
        "from pathlib import Path\n"
        f"Path({str(execution_marker)!r}).write_text('executed', encoding='utf-8')\n"
    ).encode()
    forged = rehash_package_artifacts(
        payload,
        additions=(
            ArtifactFile(
                "__main__.py",
                forged_source,
                "text/x-python",
                "FORGED_EXECUTABLE",
            ),
        ),
    )

    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == "EXECUTABLE_ZIP_ENTRY_FORBIDDEN"
    assert not execution_marker.exists()
    with pytest.raises(ArtifactError, match="must not contain executable __main__.py"):
        read_and_verify_package(forged)
    assert not execution_marker.exists()


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (
        ("invalid-json", "INVALID_ARTIFACT_JSON"),
        ("non-object", "INVALID_ARTIFACT_JSON"),
        ("noncanonical-json", "NON_CANONICAL_ARTIFACT_JSON"),
        ("unsupported-structure", "INVALID_FROZEN_DESIGN_BINDING"),
    ),
)
def test_offline_verifier_rejects_invalid_frozen_design_artifact(
    tmp_path: Path,
    mutation: str,
    error_code: str,
) -> None:
    _, _, payload = package_fixture()
    frozen = json.loads(zip_entries(payload)["design/design-spec.json"])
    if mutation == "invalid-json":
        replacement = b"{"
    elif mutation == "non-object":
        replacement = b"[]"
    elif mutation == "noncanonical-json":
        replacement = json.dumps(frozen, indent=2, sort_keys=True).encode("utf-8")
    else:
        frozen["schema_version"] = "custombuild.frozen-design-spec.v999"
        replacement = canonical_json_bytes(frozen)
    forged = rehash_package_artifacts(
        payload,
        replacements={"design/design-spec.json": replacement},
    )

    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == error_code


def test_offline_verifier_rejects_duplicate_frozen_design_role(tmp_path: Path) -> None:
    _, _, payload = package_fixture()
    forged = rehash_package_artifacts(
        payload,
        additions=(
            ArtifactFile(
                "design/attacker-spec.json",
                b"{}",
                "application/json",
                "FROZEN_DESIGN_SPEC",
            ),
        ),
    )

    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == "INVALID_FROZEN_DESIGN_BINDING"


@pytest.mark.parametrize("mutation", ("non-object", "invalid-evidence-digest"))
def test_offline_verifier_rejects_invalid_frozen_retention_contract(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload, _ = retention_package_fixture()
    frozen = json.loads(zip_entries(payload)["design/design-spec.json"])
    retention = frozen["spec"]["joint_retention"]
    if mutation == "non-object":
        frozen["spec"]["joint_retention"] = "attacker-authored-contract"
    else:
        assert isinstance(retention, dict)
        retention["evidence_sha256"] = "not-a-digest"
    forged = rehash_package_artifacts(
        payload,
        replacements={"design/design-spec.json": canonical_json_bytes(frozen)},
    )

    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == "INVALID_RETENTION_CONTRACT"


def test_offline_verifier_rejects_noncanonical_retention_evidence_role(
    tmp_path: Path,
) -> None:
    payload, _ = retention_package_fixture()

    def mutate_evidence_role(manifest: dict[str, Any]) -> None:
        evidence = next(
            entry
            for entry in manifest["artifacts"]
            if entry["path"] == JOINT_RETENTION_SIGNED_EVIDENCE_PATH
        )
        evidence["role"] = "ATTACKER_RELABELED_EVIDENCE"

    forged = mutate_manifest(payload, mutate_evidence_role)
    completed, result = run_offline_verifier(tmp_path, forged)

    assert completed.returncode == 3
    assert result["error"]["code"] == "INVALID_RETENTION_EVIDENCE_BINDING"


def mutate_manifest(
    payload: bytes,
    mutation,
    *,
    rehash_context: bool = True,
) -> bytes:
    entries = zip_entries(payload)
    manifest = json.loads(entries["manifest.json"])
    mutation(manifest)
    if rehash_context:
        context = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
        manifest["production_context_hash"] = sha256_hex(canonical_json_bytes(context))
    entries["manifest.json"] = canonical_json_bytes(manifest)
    return make_zip(list(entries.items()))


def rehash_package_artifacts(
    payload: bytes,
    *,
    replacements: dict[str, bytes] | None = None,
    removals: tuple[str, ...] = (),
    additions: tuple[ArtifactFile, ...] = (),
) -> bytes:
    """Rebuild an attacker-controlled, internally rehashed unsigned package."""

    files = zip_entries(payload)
    manifest = json.loads(files.pop("manifest.json"))
    by_path = {entry["path"]: entry for entry in manifest["artifacts"]}
    for path in removals:
        files.pop(path, None)
        by_path.pop(path, None)
    for path, data in (replacements or {}).items():
        assert path in by_path
        files[path] = data
        by_path[path]["size_bytes"] = len(data)
        by_path[path]["sha256"] = sha256_hex(data)
    for artifact in additions:
        files[artifact.path] = artifact.data
        by_path[artifact.path] = {
            "path": artifact.path,
            "media_type": artifact.media_type,
            "role": artifact.role,
            "size_bytes": len(artifact.data),
            "sha256": sha256_hex(artifact.data),
        }
    manifest["artifacts"] = [by_path[path] for path in sorted(by_path)]
    context = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
    manifest["production_context_hash"] = sha256_hex(canonical_json_bytes(context))
    files["manifest.json"] = canonical_json_bytes(manifest)
    return make_zip(sorted(files.items()))


def rehash_supplier_bound_artifact(payload: bytes, *, path: str, data: bytes) -> bytes:
    """Model an attacker who updates ZIP, manifest and handoff inventory hashes."""

    files = zip_entries(payload)
    handoff = json.loads(files[SUPPLIER_HANDOFF_PATH])
    binding = handoff["payload_inventory_binding"]
    entry = next(item for item in binding["artifacts"] if item["path"] == path)
    entry["size_bytes"] = len(data)
    entry["sha256"] = sha256_hex(data)
    binding["payload_inventory_sha256"] = sha256_hex(canonical_json_bytes(binding["artifacts"]))
    return rehash_package_artifacts(
        payload,
        replacements={
            path: data,
            SUPPLIER_HANDOFF_PATH: canonical_json_bytes(handoff),
        },
    )


def test_retention_bound_package_requires_exact_historical_statement_bytes(
    tmp_path: Path,
) -> None:
    payload, evidence_bytes = retention_package_fixture()
    manifest = read_and_verify_package(payload)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.read(JOINT_RETENTION_SIGNED_EVIDENCE_PATH) == evidence_bytes
    evidence_entry = next(
        entry
        for entry in manifest["artifacts"]
        if entry["path"] == JOINT_RETENTION_SIGNED_EVIDENCE_PATH
    )
    assert evidence_entry == {
        "path": JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
        "media_type": JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
        "role": JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
        "size_bytes": len(evidence_bytes),
        "sha256": sha256_hex(evidence_bytes),
    }
    completed, report = run_offline_verifier(tmp_path, payload)
    assert completed.returncode == 0
    assert report["status"] == "PASS"
    assert report["authenticity"] == "NOT_AUTHENTICATED"
    assert report["physical_cutting_authorized"] is False
    assert any("No registry embedded in a ZIP is trusted" in item for item in report["warnings"])

    without_evidence = rehash_package_artifacts(
        payload,
        removals=(JOINT_RETENTION_SIGNED_EVIDENCE_PATH,),
    )
    with pytest.raises(ArtifactError, match="requires one signed evidence artifact"):
        read_and_verify_package(without_evidence)
    completed, report = run_offline_verifier(tmp_path, without_evidence)
    assert completed.returncode == 3
    assert report["error"]["code"] == "INVALID_RETENTION_EVIDENCE_BINDING"

    tampered = rehash_package_artifacts(
        payload,
        replacements={JOINT_RETENTION_SIGNED_EVIDENCE_PATH: b"forged-signed-evidence"},
    )
    with pytest.raises(ArtifactError, match="frozen retention contract"):
        read_and_verify_package(tampered)
    completed, report = run_offline_verifier(tmp_path, tampered)
    assert completed.returncode == 3
    assert report["error"]["code"] == "RETENTION_EVIDENCE_SHA256_MISMATCH"


def test_unbound_package_rejects_retention_evidence_even_when_manifest_is_rehashed(
    tmp_path: Path,
) -> None:
    _, _, payload = package_fixture()
    forged = rehash_package_artifacts(
        payload,
        additions=(
            ArtifactFile(
                JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
                b"unbound-signed-evidence",
                JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
                JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
            ),
        ),
    )

    with pytest.raises(ArtifactError, match="unbound frozen DesignSpec"):
        read_and_verify_package(forged)
    completed, report = run_offline_verifier(tmp_path, forged)
    assert completed.returncode == 3
    assert report["error"]["code"] == "UNEXPECTED_RETENTION_EVIDENCE"


def test_cam_blocked_review_package_retains_bound_signed_evidence(
    tmp_path: Path,
) -> None:
    payload, evidence_bytes = retention_package_fixture("STOCK_PROFILE_MISSING")

    manifest = read_and_verify_package(payload)
    assert manifest["physical_cutting_authorized"] is False
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.read(JOINT_RETENTION_SIGNED_EVIDENCE_PATH) == evidence_bytes
    completed, report = run_offline_verifier(tmp_path, payload)
    assert completed.returncode == 0
    assert report["status"] == "PASS"
    assert report["physical_cutting_authorized"] is False


def test_reader_never_reinterprets_legacy_manifest_v4_as_v5() -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    manifest = json.loads(files["manifest.json"])
    manifest["schema_version"] = "custombuild.production-manifest.v4"
    files["manifest.json"] = canonical_json_bytes(manifest)

    with pytest.raises(
        ArtifactError,
        match="legacy production manifest v4 requires its exact archived v4 verifier",
    ):
        read_and_verify_package(make_zip(sorted(files.items())))


def test_reader_never_reinterprets_legacy_supplier_handoff_v2_as_v3() -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    handoff = json.loads(files[SUPPLIER_HANDOFF_PATH])
    handoff["schema_version"] = "custombuild.supplier-handoff.v2"
    forged = rehash_package_artifacts(
        payload,
        replacements={SUPPLIER_HANDOFF_PATH: canonical_json_bytes(handoff)},
    )

    with pytest.raises(
        ArtifactError,
        match="legacy supplier handoff v2 requires its exact archived v4 verifier",
    ):
        read_and_verify_package(forged)


def test_supplier_handoff_inventory_digest_is_semantically_rebuilt() -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    handoff = json.loads(files[SUPPLIER_HANDOFF_PATH])
    handoff["payload_inventory_binding"]["payload_inventory_sha256"] = "0" * 64
    forged = rehash_package_artifacts(
        payload,
        replacements={SUPPLIER_HANDOFF_PATH: canonical_json_bytes(handoff)},
    )

    with pytest.raises(ArtifactError, match="supplier handoff does not match"):
        read_and_verify_package(forged)


def test_reader_rejects_rehashed_start_here_authorization_drift() -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    forged_guide = files[START_HERE_PATH].replace(
        b"no permission to cut material",
        b"permission to cut is granted",
    )
    forged = rehash_supplier_bound_artifact(
        payload,
        path=START_HERE_PATH,
        data=forged_guide,
    )

    with pytest.raises(ArtifactError, match="START-HERE guide differs"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    "schema_path",
    (
        MANUFACTURING_INTENT_JSON_SCHEMA_PATH,
        OPERATIONS_JSON_SCHEMA_PATH,
        SUPPLIER_HANDOFF_JSON_SCHEMA_PATH,
    ),
)
def test_reader_rejects_rehashed_published_schema_drift(schema_path: str) -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    schema = json.loads(files[schema_path])
    schema["title"] = "Attacker-authored permissive contract"
    forged = rehash_supplier_bound_artifact(
        payload,
        path=schema_path,
        data=canonical_json_bytes(schema),
    )

    with pytest.raises(ArtifactError, match="JSON Schema differs from the canonical schema"):
        read_and_verify_package(forged)


def test_reader_rejects_rehashed_operations_outside_published_schema() -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    operations = json.loads(files["cam/operations.json"])
    operations["physical_cutting_authorized"] = True
    forged = rehash_supplier_bound_artifact(
        payload,
        path="cam/operations.json",
        data=canonical_json_bytes(operations),
    )

    with pytest.raises(
        ArtifactError,
        match="operations document does not conform to its published JSON Schema",
    ):
        read_and_verify_package(forged)


def test_reader_rejects_rehashed_operations_binding_digest_drift() -> None:
    _, _, payload = package_fixture()
    files = zip_entries(payload)
    handoff = json.loads(files[SUPPLIER_HANDOFF_PATH])
    handoff["operation_binding"]["document_sha256"] = "0" * 64
    forged = rehash_package_artifacts(
        payload,
        replacements={SUPPLIER_HANDOFF_PATH: canonical_json_bytes(handoff)},
    )

    with pytest.raises(ArtifactError, match="supplier handoff does not match"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    "mode",
    (
        "GENERATED",
        "TWO_SIDED_REGISTRATION_MISSING",
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    ),
)
def test_reader_rejects_rehashed_coordinated_dfm_and_handoff_warning_drift(
    mode: str,
) -> None:
    if mode == DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE:
        payload = _dado_review_case().zip_bytes
    else:
        _, _, payload = _review_case(mode)
    files = zip_entries(payload)
    report = json.loads(files["validation/dfm-report.json"])
    forged_issue = {
        "code": "DFM-FORGED-WARNING",
        "severity": "WARNING",
        "message": "Attacker-authored review warning.",
        "part_id": None,
        "feature_id": None,
        "setup_id": None,
        "inputs": {"attacker_authored": True},
        "suggestion": "Do not trust this forged warning.",
    }
    report["issues"].append(forged_issue)
    report_bytes = canonical_json_bytes(report)

    handoff = json.loads(files[SUPPLIER_HANDOFF_PATH])
    handoff["dfm_review_warnings"].append(
        {
            "issue": {key: value for key, value in forged_issue.items() if key != "severity"},
            "source": "validation/dfm-report.json",
            "status": "UNRESOLVED_SUPPLIER_REVIEW_WARNING",
            "resolved": False,
            "boundary": (
                "Review the referenced structured DFM issue and close it with supplier "
                "evidence before physical release. This warning is not cutting approval."
            ),
        }
    )
    binding = handoff["payload_inventory_binding"]
    dfm_entry = next(
        entry for entry in binding["artifacts"] if entry["path"] == "validation/dfm-report.json"
    )
    dfm_entry["size_bytes"] = len(report_bytes)
    dfm_entry["sha256"] = sha256_hex(report_bytes)
    binding["payload_inventory_sha256"] = sha256_hex(canonical_json_bytes(binding["artifacts"]))
    forged = rehash_package_artifacts(
        payload,
        replacements={
            "validation/dfm-report.json": report_bytes,
            SUPPLIER_HANDOFF_PATH: canonical_json_bytes(handoff),
        },
    )

    retention_check = (
        patch.object(
            review_status_contract,
            "dado_retention_evidence_missing",
            lambda _design: True,
        )
        if mode == DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
        else patch.object(
            review_status_contract,
            "dado_retention_evidence_missing",
            lambda _design: False,
        )
    )
    with (
        retention_check,
        pytest.raises(
            ArtifactError,
            match="DFM report differs from deterministic reconstruction",
        ),
    ):
        read_and_verify_package(forged)


def _render_bom_rows(fieldnames: list[str], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def test_reader_rejects_rehashed_coordinated_bom_grouped_dfm_axis_drift() -> None:
    _, _, payload = _review_case(DFM_GRAIN_BLOCKER_CODE)
    files = zip_entries(payload)
    reader = csv.DictReader(io.StringIO(files["bom/bom.csv"].decode("utf-8")))
    rows = list(reader)
    assert reader.fieldnames is not None
    grouped = json.loads(files["bom/grouped-bom.json"])
    target_group = next(
        group for group in grouped["groups"] if group["signature"]["grain_direction"] in {"X", "Y"}
    )
    target_ids = set(target_group["part_ids"])
    replacement_axis = "Y" if target_group["signature"]["grain_direction"] == "X" else "X"
    for row in rows:
        if row["part_id"] in target_ids:
            row["grain_direction"] = replacement_axis
    target_group["signature"]["grain_direction"] = replacement_axis
    target_group["group_id"] = grouped_bom_group_id(target_group["signature"])
    grouped["groups"].sort(key=lambda group: group["group_id"])
    grouped["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped["groups"]))
    grain_by_part = {row["part_id"]: row["grain_direction"] for row in rows}
    report = json.loads(files["validation/dfm-report.json"])
    for issue in report["issues"]:
        if issue["code"] == DFM_GRAIN_BLOCKER_CODE:
            issue["inputs"]["required_part_grain_directions"] = sorted(
                {grain_by_part[part_id] for part_id in issue["inputs"]["affected_part_ids"]}
            )
    forged = rehash_package_artifacts(
        payload,
        replacements={
            "bom/bom.csv": _render_bom_rows(reader.fieldnames, rows),
            "bom/grouped-bom.json": canonical_json_bytes(grouped),
            "validation/dfm-report.json": canonical_json_bytes(report),
        },
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(forged)


def test_reader_rejects_rehashed_grouped_dfm_ghost_part() -> None:
    _, _, payload = _review_case(DFM_GRAIN_BLOCKER_CODE)
    files = zip_entries(payload)
    grouped = json.loads(files["bom/grouped-bom.json"])
    report = json.loads(files["validation/dfm-report.json"])
    original = report["issues"][0]["inputs"]["affected_part_ids"][0]
    ghost = "ghost-part-id"
    for group in grouped["groups"]:
        if original in group["part_ids"]:
            group["part_ids"] = sorted(
                ghost if part_id == original else part_id for part_id in group["part_ids"]
            )
    grouped["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped["groups"]))
    for issue in report["issues"]:
        issue["inputs"]["affected_part_ids"] = sorted(
            ghost if part_id == original else part_id
            for part_id in issue["inputs"]["affected_part_ids"]
        )
    forged = rehash_package_artifacts(
        payload,
        replacements={
            "bom/grouped-bom.json": canonical_json_bytes(grouped),
            "validation/dfm-report.json": canonical_json_bytes(report),
        },
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(forged)


def test_reader_rejects_rehashed_invented_bom_material_identity() -> None:
    _, _, payload = _review_case(DFM_GRAIN_BLOCKER_CODE)
    files = zip_entries(payload)
    reader = csv.DictReader(io.StringIO(files["bom/bom.csv"].decode("utf-8")))
    rows = list(reader)
    assert reader.fieldnames is not None
    report = json.loads(files["validation/dfm-report.json"])
    original_material = report["issues"][0]["inputs"]["material_id"]
    invented_material = "invented-sheet-material"
    for row in rows:
        if row["material_id"] == original_material:
            row["material_id"] = invented_material
    grouped = json.loads(files["bom/grouped-bom.json"])
    for group in grouped["groups"]:
        if group["signature"]["material_id"] == original_material:
            group["signature"]["material_id"] = invented_material
            group["group_id"] = grouped_bom_group_id(group["signature"])
    grouped["groups"].sort(key=lambda group: group["group_id"])
    grouped["group_fingerprint"] = sha256_hex(canonical_json_bytes(grouped["groups"]))
    for issue in report["issues"]:
        if issue["inputs"]["material_id"] == original_material:
            issue["inputs"]["material_id"] = invented_material
    forged = rehash_package_artifacts(
        payload,
        replacements={
            "bom/bom.csv": _render_bom_rows(reader.fieldnames, rows),
            "bom/grouped-bom.json": canonical_json_bytes(grouped),
            "validation/dfm-report.json": canonical_json_bytes(report),
        },
    )

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(forged)


def test_reader_rejects_stock_report_with_all_grain_warnings_removed() -> None:
    _, _, payload = _review_case("STOCK_PROFILE_MISSING")
    files = zip_entries(payload)
    report = json.loads(files["validation/dfm-report.json"])
    report["issues"] = [
        issue for issue in report["issues"] if issue["code"] != DFM_GRAIN_BLOCKER_CODE
    ]
    forged = rehash_package_artifacts(
        payload,
        replacements={"validation/dfm-report.json": canonical_json_bytes(report)},
    )

    with pytest.raises(ArtifactError, match="exactly cover canonical unmatched BOM parts"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    ("field", "value"),
    (("stock_id", "ghost-stock"), ("stock_grain_direction", "ARBITRARY")),
)
def test_reader_rejects_matched_grain_issue_with_false_stock_fact(
    field: str,
    value: str,
) -> None:
    _, _, payload = _review_case(DFM_GRAIN_BLOCKER_CODE)
    files = zip_entries(payload)
    report = json.loads(files["validation/dfm-report.json"])
    report["issues"][0]["inputs"][field] = value
    forged = rehash_package_artifacts(
        payload,
        replacements={"validation/dfm-report.json": canonical_json_bytes(report)},
    )

    with pytest.raises(ArtifactError, match="grain|canonical|stock"):
        read_and_verify_package(forged)


def test_reader_rejects_stock_report_with_ghost_part_issue() -> None:
    _, _, payload = _review_case("STOCK_PROFILE_MISSING")
    files = zip_entries(payload)
    report = json.loads(files["validation/dfm-report.json"])
    stock_issue = next(
        issue for issue in report["issues"] if issue["code"] == "STOCK_PROFILE_MISSING"
    )
    stock_issue["part_id"] = "ghost-part-id"
    forged = rehash_package_artifacts(
        payload,
        replacements={"validation/dfm-report.json": canonical_json_bytes(report)},
    )

    with pytest.raises(ArtifactError, match="exactly cover canonical unmatched BOM parts"):
        read_and_verify_package(forged)


@pytest.mark.parametrize("mode", ("missing-b-sides", "extra-ghost"))
def test_reader_rejects_rehashed_noncanonical_part_drawing_inventory(mode: str) -> None:
    _, _, payload = _review_case(DFM_GRAIN_BLOCKER_CODE)
    files = zip_entries(payload)
    if mode == "missing-b-sides":
        removals = tuple(
            path
            for path in files
            if (path.startswith("parts/") or path.startswith("drawings/"))
            and (path.endswith("/B.dxf") or path.endswith("/B.svg"))
        )
        forged = rehash_package_artifacts(payload, removals=removals)
    else:
        forged = rehash_package_artifacts(
            payload,
            additions=(
                ArtifactFile("parts/ghost/A.dxf", b"0\nEOF\n", "image/vnd.dxf", "PART_DXF"),
                ArtifactFile(
                    "drawings/ghost/A.svg",
                    b"<svg/>",
                    "image/svg+xml",
                    "PART_DRAWING",
                ),
            ),
        )
    with pytest.raises(ArtifactError, match="A/B part drawing inventory"):
        read_and_verify_package(forged)


@pytest.mark.parametrize("mutation", ("invalid-utf8", "malformed-csv", "duplicate-row"))
def test_reader_rejects_rehashed_noncanonical_bom_csv(mutation: str) -> None:
    _, _, payload = _review_case(DFM_GRAIN_BLOCKER_CODE)
    bom = zip_entries(payload)["bom/bom.csv"]
    if mutation == "invalid-utf8":
        replacement = b"\xff" + bom
    elif mutation == "malformed-csv":
        replacement = bom.replace(b"part_id,", b'"part_id,', 1)
    else:
        lines = bom.splitlines(keepends=True)
        replacement = b"".join((*lines, lines[1]))
    forged = rehash_package_artifacts(payload, replacements={"bom/bom.csv": replacement})

    with pytest.raises(ArtifactError, match="review-core artifact differs from frozen DesignSpec"):
        read_and_verify_package(forged)


def test_reader_rejects_status_stripping_even_with_injected_full_cam_inventory() -> None:
    _, _, blocked_payload = _review_case(DFM_GRAIN_BLOCKER_CODE)
    _, generated_artifacts, _ = _review_case("GENERATED")
    blocked_paths = set(zip_entries(blocked_payload))
    injected_roles = {
        "MACHINE_NEUTRAL_OPERATIONS",
        "SETUP_SHEET",
        "VALIDATION_BACKPLOT",
        "NON_CUTTING_VALIDATION_PROGRAM",
        "NESTING_MAP",
        "STOCK_PURCHASE_SCHEDULE",
    }
    additions = tuple(
        artifact
        for artifact in generated_artifacts
        if artifact.role in injected_roles and artifact.path not in blocked_paths
    )
    forged = rehash_package_artifacts(
        blocked_payload,
        removals=(DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,),
        additions=additions,
    )

    with pytest.raises(ArtifactError, match="require one canonical design-review status"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    ("path", "role", "media_type", "data"),
    (
        ("toolpaths/finish.nc", "GCODE", "text/x-gcode", b"M3\nG1 Z-18\n"),
        ("machine/finish.tap", "CUTTING_PROGRAM", "text/x-gcode", b"M3\nG1 Z-18\n"),
        ("machine/finish.gcode", "WORKER_NOTE", "text/plain", b"M3\nG1 Z-18\n"),
    ),
)
def test_reader_rejects_rehashed_generated_cutting_alias(
    path: str,
    role: str,
    media_type: str,
    data: bytes,
) -> None:
    _, _, payload = _review_case("GENERATED")
    forged = rehash_package_artifacts(
        payload,
        additions=(ArtifactFile(path, data, media_type, role),),
    )

    with pytest.raises(ArtifactError, match="unapproved artifact|invalid .*artifacts"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    "target_kind",
    ("operations", "setup", "nesting", "backplot", "program"),
)
def test_reader_rejects_empty_rehashed_generated_cam_artifact(target_kind: str) -> None:
    _, artifacts, payload = _review_case("GENERATED")
    predicates = {
        "operations": lambda artifact: artifact.path == "cam/operations.json",
        "setup": lambda artifact: artifact.role == "SETUP_SHEET",
        "nesting": lambda artifact: artifact.role == "NESTING_MAP",
        "backplot": lambda artifact: artifact.role == "VALIDATION_BACKPLOT",
        "program": lambda artifact: artifact.role == "NON_CUTTING_VALIDATION_PROGRAM",
    }
    target = next(artifact.path for artifact in artifacts if predicates[target_kind](artifact))
    forged = rehash_package_artifacts(payload, replacements={target: b""})

    with pytest.raises(ArtifactError, match="generated CAM artifact|canonical|non-empty"):
        read_and_verify_package(forged)


@pytest.mark.parametrize("role", ("SETUP_SHEET", "NESTING_MAP", "NON_CUTTING_VALIDATION_PROGRAM"))
def test_reader_rejects_reduced_rehashed_generated_cam_inventory(role: str) -> None:
    _, artifacts, payload = _review_case("GENERATED")
    paths = [artifact.path for artifact in artifacts if artifact.role == role]
    assert len(paths) > 1
    forged = rehash_package_artifacts(payload, removals=tuple(paths[1:]))

    with pytest.raises(ArtifactError, match="generated CAM artifact|not unique|inventory"):
        read_and_verify_package(forged)


@pytest.mark.parametrize("mutation", ("stock-id", "grain-axis", "quantity"))
def test_reader_rejects_stock_selection_drift_from_generated_outputs(mutation: str) -> None:
    _, _, payload = _review_case("GENERATED")
    snapshot = json.loads(zip_entries(payload)["validation/stock-selection.json"])
    stock = snapshot["stocks"][0]
    if mutation == "stock-id":
        original = stock["stock_id"]
        stock["stock_id"] = "ghost-stock"
        for assignment in snapshot["assignments"]:
            if assignment["stock_id"] == original:
                assignment["stock_id"] = "ghost-stock"
        snapshot["stocks"].sort(key=lambda row: row["stock_id"])
        snapshot["assignments"].sort(key=lambda row: row["stock_id"])
    elif mutation == "grain-axis":
        stock["grain_direction"] = "Y" if stock["grain_direction"] == "X" else "X"
    else:
        stock["quantity"] = 99
    forged = rehash_package_artifacts(
        payload,
        replacements={"validation/stock-selection.json": canonical_json_bytes(snapshot)},
    )

    with pytest.raises(ArtifactError):
        read_and_verify_package(forged)


def test_reader_rejects_rehashed_stock_authority_upgrade() -> None:
    _, _, payload = _review_case("GENERATED")
    snapshot = json.loads(zip_entries(payload)["validation/stock-selection.json"])
    snapshot["stocks"][0]["declaration_authority"] = "SERVER_VERIFIED"
    forged = rehash_package_artifacts(
        payload,
        replacements={"validation/stock-selection.json": canonical_json_bytes(snapshot)},
    )

    with pytest.raises(ArtifactError, match="declaration authority"):
        read_and_verify_package(forged)


@pytest.mark.parametrize("mutation", ("authority", "baseline", "keep-out-binding"))
def test_reader_rejects_rehashed_registration_contract_tamper(mutation: str) -> None:
    _, _, payload = _review_case("GENERATED")
    entries = zip_entries(payload)
    plan = json.loads(entries["validation/generation-plan.json"])
    sheet = plan["two_sided_registrations"][0]["sheets"][0]
    replacements: dict[str, bytes] = {}
    if mutation == "authority":
        sheet["declaration_authority"] = "SERVER_VERIFIED"
    elif mutation == "baseline":
        sheet["points"][1] = {
            "x_um": sheet["points"][0]["x_um"] + 1,
            "y_um": sheet["points"][0]["y_um"],
        }
    else:
        sheet["position_tolerance_um"] += 1
    replacements["validation/generation-plan.json"] = canonical_json_bytes(plan)
    forged = rehash_package_artifacts(payload, replacements=replacements)

    with pytest.raises(ArtifactError, match="generation plan|registration"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        ("model/design.step", b"x"),
        ("model/design.glb", b"x"),
        ("validation/cad-interchange-status.json", b"{}"),
    ),
)
def test_reader_rejects_rehashed_unbound_cad_artifacts(path: str, replacement: bytes) -> None:
    _, _, payload = _review_case("GENERATED")
    forged = rehash_package_artifacts(payload, replacements={path: replacement})

    with pytest.raises(ArtifactError, match="STEP/GLB|CAD interchange"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    ("path", "role", "media_type"),
    (
        ("Parts/ghost/A.dxf", "WORKER_NOTE", "text/plain"),
        ("Drawings/ghost/A.svg", "WORKER_NOTE", "text/plain"),
        ("BOM/ghost.csv", "WORKER_NOTE", "text/plain"),
        ("cut-list/ghost.csv", "WORKER_NOTE", "text/plain"),
        ("materials/ghost.csv", "WORKER_NOTE", "text/plain"),
        ("design/ghost.json", "WORKER_NOTE", "application/json"),
        ("validation/ghost.json", "WORKER_NOTE", "application/json"),
        ("review/step.bin", "AUTHORITATIVE_STEP", "application/octet-stream"),
        ("review/preview.bin", "WEB_PREVIEW_GLB", "application/octet-stream"),
        ("review/cad.json", "CAD_INTERCHANGE_STATUS", "application/json"),
    ),
)
def test_reader_rejects_generated_reserved_namespace_or_role_alias(
    path: str,
    role: str,
    media_type: str,
) -> None:
    _, _, payload = _review_case("GENERATED")
    forged = rehash_package_artifacts(
        payload,
        additions=(ArtifactFile(path, b"payload", media_type, role),),
    )

    with pytest.raises(ArtifactError, match="unapproved artifact|not unique"):
        read_and_verify_package(forged)


def test_reader_rejects_rehashed_readiness_software_evidence_text() -> None:
    _, _, payload = _review_case("GENERATED")
    readiness = json.loads(zip_entries(payload)["validation/workshop-readiness.json"])
    readiness["software_evidence"][0]["evidence"] = "nonsense"
    forged = rehash_package_artifacts(
        payload,
        replacements={"validation/workshop-readiness.json": canonical_json_bytes(readiness)},
    )

    with pytest.raises(ArtifactError, match="readiness text and evidence"):
        read_and_verify_package(forged)


@pytest.mark.parametrize(
    "path",
    ("", "../x", "/abs", "C:/x", "file:stream", "a\\b", "a/./b", "a//b", "x\x00y"),
)
def test_artifact_paths_reject_ambiguous_or_escaping_names(path: str) -> None:
    with pytest.raises(ArtifactError):
        ArtifactFile(path, b"x", "text/plain", "TEST")


def test_duplicate_artifact_paths_and_missing_release_files_are_blocked() -> None:
    context, artifacts, _ = package_fixture()
    with pytest.raises(ArtifactError, match="duplicate"):
        build_deterministic_zip(context, artifacts + artifacts)
    with pytest.raises(ArtifactError, match="duplicate"):
        build_deterministic_zip(
            context,
            (
                ArtifactFile("data/file.txt", b"a", "text/plain", "TEST"),
                ArtifactFile("DATA/FILE.TXT", b"b", "text/plain", "TEST"),
            ),
        )
    with pytest.raises(ProductionBlockedError, match="production machine release is disabled"):
        build_deterministic_zip(context, artifacts, production_release=True)


def test_package_reader_rejects_corruption_missing_manifest_and_unsafe_entries() -> None:
    with pytest.raises(ArtifactError, match="invalid production ZIP"):
        read_and_verify_package(b"not-a-zip")
    with pytest.raises(ArtifactError, match="manifest"):
        read_and_verify_package(make_zip([("file.txt", b"x")]))
    with pytest.raises(ArtifactError, match="unsafe artifact path"):
        read_and_verify_package(make_zip([("../manifest.json", b"{}")]))
    with pytest.raises(ArtifactError, match="directory"):
        read_and_verify_package(make_zip([("folder/", b""), ("manifest.json", b"{}")]))
    bomb = make_zip([("manifest.json", b"0" * 2_000_000)])
    with pytest.raises(ArtifactError, match="compression ratio"):
        read_and_verify_package(bomb)


@pytest.mark.parametrize("payload", (b"", bytearray(b"not-exact-bytes")))
def test_package_reader_rejects_empty_or_non_bytes_payload_before_zip_parse(
    payload: object,
) -> None:
    with pytest.raises(ArtifactError, match="canonical size limit"):
        read_and_verify_package(payload)  # type: ignore[arg-type]


def test_package_reader_rejects_oversize_bundle_before_zip_parse() -> None:
    payload = b"x" * (MAX_PRODUCTION_BUNDLE_BYTES + 1)

    with pytest.raises(ArtifactError, match="canonical size limit"):
        read_and_verify_package(payload)


def test_package_reader_applies_canonical_manifest_limit_before_reading_entry() -> None:
    payload = make_zip([("manifest.json", b"x" * (MAX_CORE_DOCUMENT_BYTES + 1))])

    with pytest.raises(ArtifactError, match="entry is too large: manifest.json"):
        read_and_verify_package(payload)


@pytest.mark.parametrize(
    "path",
    (MANUFACTURING_INTENT_PATH, SUPPLIER_HANDOFF_PATH),
)
def test_package_reader_applies_core_document_limit_to_supplier_json(path: str) -> None:
    payload = make_zip([(path, b"x" * (MAX_CORE_DOCUMENT_BYTES + 1))])

    with pytest.raises(ArtifactError, match=f"entry is too large: {path}"):
        read_and_verify_package(payload)


@pytest.mark.parametrize(
    "symlink_path",
    ("model/design.step", "assembly/assembly-manual.pdf"),
)
def test_package_reader_rejects_symlink_for_required_or_optional_review_artifact(
    symlink_path: str,
) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    payload = build_deterministic_zip(
        context,
        (
            *status_review_core_artifacts(),
            review_status_artifact(),
            ArtifactFile(
                "assembly/assembly-manual.pdf",
                b"%PDF-1.4\n",
                "application/pdf",
                "ASSEMBLY_REVIEW_MANUAL",
            ),
        ),
    )
    entries = zip_entries(payload)
    symlink_payload = make_zip(
        list(entries.items()),
        unix_modes={symlink_path: 0o120777},
    )

    with pytest.raises(ArtifactError, match="non-regular or non-canonical"):
        read_and_verify_package(symlink_payload)


@pytest.mark.parametrize(
    "target",
    ("model/design.step", "PART_DXF", "PART_DRAWING"),
)
def test_reader_rejects_empty_required_review_core_artifacts(target: str) -> None:
    context, _, _ = package_fixture()
    context = replace(context, cad_status="GENERATED")
    source = status_review_core_artifacts()
    empty_path = (
        target
        if target == "model/design.step"
        else next(artifact.path for artifact in source if artifact.role == target)
    )
    artifacts = tuple(
        replace(artifact, data=b"") if artifact.path == empty_path else artifact
        for artifact in source
    )

    with pytest.raises(
        ArtifactError,
        match="non-empty|review-core artifact|authoritative STEP/GLB",
    ):
        read_and_verify_package(
            build_deterministic_zip(
                context,
                (*artifacts, review_status_artifact()),
            )
        )


def test_public_manifest_context_validator_accepts_builder_manifest() -> None:
    _, _, payload = package_fixture()
    manifest = read_and_verify_package(payload)

    validate_manifest_context_contract(manifest)

    incomplete = dict(manifest)
    incomplete.pop("template_capability")
    with pytest.raises(ArtifactError, match="context field missing"):
        validate_manifest_context_contract(incomplete)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda manifest: b"not-json", "valid manifest"),
        (lambda manifest: b"[]", "JSON object"),
        (
            lambda manifest: json.dumps({**manifest, "schema_version": "wrong"}).encode(),
            "schema",
        ),
        (
            lambda manifest: json.dumps({**manifest, "artifacts": {}}).encode(),
            "must be an array",
        ),
        (
            lambda manifest: json.dumps({**manifest, "artifacts": ["bad"]}).encode(),
            "must be an object",
        ),
        (
            lambda manifest: json.dumps(
                {**manifest, "artifacts": [{**manifest["artifacts"][0], "path": 1}]}
            ).encode(),
            "path must be a string",
        ),
        (
            lambda manifest: json.dumps({**manifest, "project_id": "tampered"}).encode(),
            "context_hash mismatch",
        ),
        (
            lambda manifest: json.dumps(
                {**manifest, "artifacts": manifest["artifacts"] * 2}
            ).encode(),
            "duplicate artifact paths",
        ),
    ),
)
def test_package_reader_validates_manifest_structure_and_context(mutation, message: str) -> None:
    _, _, payload = package_fixture()
    entries = zip_entries(payload)
    manifest = json.loads(entries["manifest.json"])
    entries["manifest.json"] = mutation(manifest)

    with pytest.raises(ArtifactError, match=message):
        read_and_verify_package(make_zip(list(entries.items())))


def test_package_reader_rejects_checksum_missing_and_unlisted_files() -> None:
    _, _, payload = package_fixture()
    entries = zip_entries(payload)
    tampered = {**entries, "assembly/assembly-manual.pdf": b"changed"}
    with pytest.raises(ArtifactError, match="checksum mismatch"):
        read_and_verify_package(make_zip(list(tampered.items())))

    missing = {
        name: data for name, data in entries.items() if name != "assembly/assembly-manual.pdf"
    }
    with pytest.raises(ArtifactError, match="missing from ZIP"):
        read_and_verify_package(make_zip(list(missing.items())))

    unlisted = {**entries, "extra.txt": b"extra"}
    with pytest.raises(ArtifactError, match="outside the manifest"):
        read_and_verify_package(make_zip(list(unlisted.items())))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artifact_schema_version", "custombuild.production-artifacts.v0"),
        ("release_scope", "production"),
        ("machine_use", "physical_cutting"),
        ("physical_cutting_authorized", True),
        ("physical_cutting_authorized", 0),
        ("cad_status", "FAILED"),
        ("cad_status", []),
        ("checksum_scope", "payload files"),
    ),
)
def test_package_reader_rejects_rehashed_unsafe_manifest_claims(field, value) -> None:
    _, _, payload = package_fixture()

    def mutate(manifest):
        manifest[field] = value

    with pytest.raises(ArtifactError, match="unsafe or unsupported claims"):
        read_and_verify_package(mutate_manifest(payload, mutate))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(extra=True),
        lambda value: value.pop("checksum_scope"),
    ),
)
def test_package_reader_requires_exact_manifest_top_level(mutation) -> None:
    _, _, payload = package_fixture()

    with pytest.raises(ArtifactError, match="unexpected structure"):
        read_and_verify_package(mutate_manifest(payload, mutation, rehash_context=False))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda manifest: manifest.update(project_id=1),
        lambda manifest: manifest.update(domain_template_version="drifted"),
        lambda manifest: manifest["template_capability"].update(template_version="drifted"),
        lambda manifest: manifest["production_engine_context"].update(
            template_capability_registry_version="drifted"
        ),
        lambda manifest: manifest["machine_profile"].update(extra="unsafe"),
        lambda manifest: manifest.update(material_versions=["z", "a"]),
        lambda manifest: manifest.update(overrides=["not-an-object"]),
        lambda manifest: manifest.update(
            source_provenance={"source": "reference_image", "import_id": "invalid"}
        ),
    ),
)
def test_package_reader_rejects_rehashed_invalid_context_fields(mutation) -> None:
    _, _, payload = package_fixture()

    with pytest.raises(ArtifactError, match="context|capability|engine|profile|provenance"):
        read_and_verify_package(mutate_manifest(payload, mutation))


def test_package_reader_rejects_rehashed_manifest_context_with_stale_handoff() -> None:
    """A detached handoff must select one exact accepted manifest context."""

    _, _, payload = package_fixture()

    def change_builder_identity(manifest: dict[str, Any]) -> None:
        manifest["app_version"] = "attacker-rehashed-app-version"

    with pytest.raises(ArtifactError, match="supplier handoff does not match"):
        read_and_verify_package(mutate_manifest(payload, change_builder_identity))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda entry: entry.update(extra=True),
        lambda entry: entry.pop("role"),
        lambda entry: entry.update(media_type=1),
        lambda entry: entry.update(role=1),
        lambda entry: entry.update(size_bytes=True),
        lambda entry: entry.update(sha256="A" * 64),
    ),
)
def test_package_reader_requires_exact_artifact_entry_contract(mutation) -> None:
    context, _, _ = package_fixture()
    payload = build_deterministic_zip(
        context,
        (ArtifactFile("data/file.txt", b"x", "text/plain", "TEST"),),
    )

    def mutate(manifest):
        mutation(manifest["artifacts"][0])

    with pytest.raises(ArtifactError, match="manifest artifact"):
        read_and_verify_package(mutate_manifest(payload, mutate))


def test_package_reader_requires_unique_sorted_artifact_paths() -> None:
    context, artifacts, _ = package_fixture()
    payload = build_deterministic_zip(
        context,
        (*artifacts, ArtifactFile("data/another.txt", b"other", "text/plain", "TEST")),
    )

    def reverse_artifacts(manifest):
        manifest["artifacts"].reverse()

    with pytest.raises(ArtifactError, match="canonical order"):
        read_and_verify_package(mutate_manifest(payload, reverse_artifacts))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload + b"\n",
        lambda payload: b"\xef\xbb\xbf" + payload,
        lambda payload: payload.decode("utf-8").encode("utf-16"),
        lambda payload: payload.replace(
            b'"production_engine_context":{',
            b'"production_engine_context":{"nonfinite":NaN,',
        ),
    ),
)
def test_package_reader_requires_canonical_manifest_bytes(mutation) -> None:
    _, _, payload = package_fixture()
    entries = zip_entries(payload)
    entries["manifest.json"] = mutation(entries["manifest.json"])

    with pytest.raises(ArtifactError, match="canonical"):
        read_and_verify_package(make_zip(list(entries.items())))


@pytest.mark.parametrize("cad_status", ("NOT_REQUESTED", "GENERATED"))
def test_package_reader_requires_generated_cad_for_current_status(cad_status: str) -> None:
    context, artifacts, _ = package_fixture()
    payload = build_deterministic_zip(replace(context, cad_status=cad_status), artifacts)

    if cad_status == "NOT_REQUESTED":
        with pytest.raises(ArtifactError, match="requires generated authoritative CAD"):
            read_and_verify_package(payload)
    else:
        assert read_and_verify_package(payload)["cad_status"] == "GENERATED"


def production_request():
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="pipeline-errors",
            parameters=BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    machine = linuxcnc_reference_router_1325()
    stock = (
        StockSheet(
            "mdf18",
            design.spec.material.material_id,
            design.spec.material.version,
            2_440_000,
            1_220_000,
            18_000,
            quantity=2,
            grain_direction="X",
        ),
        StockSheet(
            "mdf6",
            design.spec.back_material.material_id,
            design.spec.back_material.version,
            2_440_000,
            1_220_000,
            6_000,
            grain_direction="X",
        ),
    )
    context = ManifestContext(
        "project",
        "1",
        design.design_hash,
        "app",
        "engine",
        "template",
        "shelving",
        "c" * 64,
        {
            "template_id": "shelving",
            "template_version": "1.0.0",
            "capability_fingerprint": "c" * 64,
        },
        "rules",
        (),
        "joints",
        machine.profile_id,
        machine.version,
        "none",
        "NOT_REQUESTED",
        "f" * 64,
        {
            "schema_version": "test-production-context.v1",
            "template_capability_registry_version": "test-registry-1.0.0",
        },
    )
    return design, machine, stock, context


def explicit_two_sided_registration(
    stock: tuple[StockSheet, ...],
) -> dict[str, dict[int, TwoSidedRegistration]]:
    return {
        item.stock_id: {
            sheet_index: _registration(
                f"test-registration:{item.stock_id}:{sheet_index}",
                (
                    Point2D(50_000, 50_000),
                    Point2D(item.width_um - 50_000, 50_000),
                ),
            )
            for sheet_index in range(item.quantity)
        }
        for item in stock
    }


def test_pipeline_rejects_context_stock_and_capacity_mismatches() -> None:
    design, machine, stock, context = production_request()
    with pytest.raises(ProductionBlockedError, match="design_hash"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=replace(context, design_hash="b" * 64),
            include_step=False,
        )
    with pytest.raises(ProductionBlockedError, match="machine profile"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=replace(context, machine_profile_version="wrong"),
            include_step=False,
        )
    with pytest.raises(ProductionBlockedError, match="at least one stock"):
        build_production_bundle(
            design, stock=(), machine=machine, context=context, include_step=False
        )
    with pytest.raises(ProductionBlockedError, match="stock_id"):
        build_production_bundle(
            design,
            stock=(stock[0], stock[0]),
            machine=machine,
            context=context,
            include_step=False,
        )
    with pytest.raises(ProductionBlockedError, match="STOCK_PROFILE_MISSING"):
        build_production_bundle(
            design,
            stock=(stock[0],),
            machine=machine,
            context=context,
            include_step=False,
            two_sided_registration_by_stock=explicit_two_sided_registration((stock[0],)),
        )
    with pytest.raises(ProductionBlockedError, match="NESTING_UNPLACED"):
        build_production_bundle(
            design,
            stock=(replace(stock[0], quantity=1), stock[1]),
            machine=machine,
            context=context,
            include_step=False,
        )


def test_pipeline_cad_failure_and_no_program_branch_are_explicit(monkeypatch) -> None:
    design, machine, stock, context = production_request()

    def fail_export(self, value):
        raise CADDependencyUnavailable("missing kernel")

    monkeypatch.setattr(CadQueryAdapter, "export_design", fail_export)
    with pytest.raises(ProductionBlockedError, match="authoritative CAD"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=True,
            two_sided_registration_by_stock=explicit_two_sided_registration(stock),
        )

    with pytest.raises(ProductionBlockedError, match="statusless generation is disabled"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=False,
            include_validation_program=False,
            two_sided_registration_by_stock=explicit_two_sided_registration(stock),
        )
