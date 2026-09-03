from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import custombuild_manufacturing.dfm as manufacturing_dfm
import custombuild_manufacturing.pipeline as manufacturing_pipeline
import custombuild_manufacturing.review_status as review_status_contract
import ezdxf
import pytest
from custombuild_cad import CADArtifacts, CadQueryAdapter, FreeCADProjectArtifacts
from custombuild_domain import (
    BOOKCASE_JOINT_SUPPORT_MATRIX,
    BackPanelType,
    BookcaseDesignSpec,
    BookcaseParameters,
    JointType,
    PartRole,
    build_bookcase,
    screening_birch_plywood_6,
    screening_birch_plywood_18,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import (
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DFM_GRAIN_BLOCKER_CODE,
    DFM_GRAIN_REQUIRED_ACTION,
    ArtifactFile,
    FeatureKind,
    ManifestContext,
    ManufacturingFeature,
    OperationKind,
    PartSpec,
    Point2D,
    ProductionBlockedError,
    Severity,
    Side,
    StockSheet,
    TwoSidedRegistration,
    build_production_bundle,
    canonical_json_bytes,
    linuxcnc_reference_router_1325,
    read_and_verify_package,
)
from custombuild_manufacturing.adapters import AdaptedDesign, adapt_design_result
from custombuild_manufacturing.dfm import (
    FEATURE_TO_OPERATION,
    JOINT_SYSTEM_UNSUPPORTED_CODE,
    joint_type_has_end_to_end_support,
)
from custombuild_postprocessors import validate_validation_program

_REAL_CAD_EXPORT = CadQueryAdapter.export_design
_REAL_DADO_RETENTION_CHECK = manufacturing_pipeline.dado_retention_evidence_missing
_REAL_UNSUPPORTED_JOINT_CHECK = manufacturing_pipeline.unsupported_joint_system_issues
_VALID_CAD_BY_DESIGN_HASH: dict[str, CADArtifacts] = {}


@pytest.fixture(autouse=True)
def _simulate_versioned_retention_for_legacy_cam_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep deeper CAM tests reachable under an explicit future-retention premise."""

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
    monkeypatch.setattr(
        manufacturing_pipeline,
        "unsupported_joint_system_issues",
        lambda _design, **_kwargs: (),
    )


def valid_cad_for(result) -> CADArtifacts:
    cached = _VALID_CAD_BY_DESIGN_HASH.get(result.design_hash)
    if cached is None:
        cached = _REAL_CAD_EXPORT(CadQueryAdapter(), result)
        _VALID_CAD_BY_DESIGN_HASH[result.design_hash] = cached
    return cached


def design_and_request(parameters: BookcaseParameters | None = None):
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="production-bundle-fixture",
            parameters=parameters or BookcaseParameters(),
            material=screening_mdf_18(),
            back_material=screening_mdf_6(),
        )
    )
    machine = linuxcnc_reference_router_1325()
    stock = (
        StockSheet(
            "mdf-18-sheets",
            design.spec.material.material_id,
            design.spec.material.version,
            2_440_000,
            1_220_000,
            18_000,
            quantity=2,
            grain_direction="X",
        ),
        StockSheet(
            "mdf-6-sheets",
            design.spec.back_material.material_id,
            design.spec.back_material.version,
            2_440_000,
            1_220_000,
            6_000,
            quantity=1,
            grain_direction="X",
        ),
    )
    context = ManifestContext(
        project_id="project-fixture",
        revision="1",
        design_hash=design.design_hash,
        app_version="0.1.0",
        engine_version="derived-by-pipeline",
        template_version="derived-by-pipeline",
        template_id="shelving",
        template_capability_fingerprint="c" * 64,
        template_capability={
            "template_id": "shelving",
            "template_version": "1.0.0",
            "capability_fingerprint": "c" * 64,
        },
        rule_version="rules-1.0.0",
        material_versions=(),
        joint_version="joints-1.0.0",
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        postprocessor_version="derived-by-pipeline",
        cad_status="derived-by-pipeline",
        generation_context_hash="f" * 64,
        production_engine_context={
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
            sheet_index: TwoSidedRegistration(
                method_id=f"test-registration:{item.stock_id}:{sheet_index}",
                points=(
                    Point2D(50_000, 50_000),
                    Point2D(item.width_um - 50_000, 50_000),
                ),
            )
            for sheet_index in range(item.quantity)
        }
        for item in stock
    }


def directional_design_and_request(
    *,
    main_stock_axis: str = "X",
    back_stock_axis: str = "NONE",
    external_evidence: tuple[dict[str, str], ...] = (),
):
    _, machine, _, base_context = design_and_request()
    design = build_bookcase(
        BookcaseDesignSpec(
            design_id="directional-production-bundle-fixture",
            parameters=BookcaseParameters(),
            material=screening_birch_plywood_18(),
            back_material=screening_birch_plywood_6(),
        )
    )
    stocks = (
        StockSheet(
            "birch-18-sheets",
            design.spec.material.material_id,
            design.spec.material.version,
            2_440_000,
            1_220_000,
            18_000,
            quantity=2,
            grain_direction=main_stock_axis,
        ),
        StockSheet(
            "birch-6-sheets",
            design.spec.back_material.material_id,
            design.spec.back_material.version,
            2_440_000,
            1_220_000,
            6_000,
            quantity=1,
            grain_direction=back_stock_axis,
        ),
    )
    context = replace(
        base_context,
        design_hash=design.design_hash,
        external_evidence=external_evidence,
    )
    return design, machine, stocks, context


def test_pipeline_requires_explicit_registration_for_each_two_sided_sheet() -> None:
    design, machine, stock, context = design_and_request()

    with pytest.raises(
        ProductionBlockedError,
        match="TWO_SIDED_REGISTRATION_MISSING",
    ) as caught:
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=False,
        )

    assert caught.value.report is not None
    assert {issue.code for issue in caught.value.report.blocking_issues} == {
        "TWO_SIDED_REGISTRATION_MISSING"
    }


def test_plain_dado_yields_review_package_but_never_cam_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    monkeypatch.setattr(
        manufacturing_pipeline,
        "dado_retention_evidence_missing",
        _REAL_DADO_RETENTION_CHECK,
    )
    monkeypatch.setattr(
        review_status_contract,
        "dado_retention_evidence_missing",
        _REAL_DADO_RETENTION_CHECK,
    )
    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        include_validation_program=True,
        allow_blocked_cam=True,
        two_sided_registration_by_stock=explicit_two_sided_registration(stock),
    )

    assert bundle.review_status.blocker_codes == (
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    )
    assert bundle.operations is None
    assert bundle.layouts == ()
    paths = {artifact.path for artifact in bundle.artifacts}
    assert not any(path.startswith(("cam/", "nesting/", "machine-validation/")) for path in paths)
    assert "manufacturing/manufacturing-intent.json" in paths
    assert "shop/supplier-handoff.json" in paths
    by_path = {artifact.path: artifact.data for artifact in bundle.artifacts}
    handoff = json.loads(by_path["shop/supplier-handoff.json"])
    assert handoff["supplier_stages"] == {
        "available_for_quote_review": True,
        "quote_review_scope": "SUPPLIER_ESTIMATION_ONLY_SUBJECT_TO_ALL_NAMED_BLOCKERS",
        "available_for_geometry_review": True,
        "geometry_review_scope": "IMPORT_AND_DIMENSIONAL_REVIEW_ONLY",
        "available_for_cam_intake_review": True,
        "cam_intake_review_scope": (
            "IMPORT_AND_REVIEW_OF_MACHINE_NEUTRAL_GEOMETRY_AND_INTENT_ONLY"
        ),
        "shop_review_required": True,
        "manufacturing_approval_granted": False,
        "cut_authorized": False,
    }
    assert handoff["readiness"]["cam_status"] == "BLOCKED"
    assert handoff["unresolved_inputs_and_decisions"][0]["code"] == (
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    )
    assert handoff["unresolved_inputs_and_decisions"][0]["category"] == (
        "STRUCTURAL_RETENTION"
    )
    assert handoff["unresolved_inputs_and_decisions"][0]["resolved"] is False
    assert [item["code"] for item in handoff["known_unresolved_decisions"]] == [
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    ]
    expected_inventory = [
        entry
        for entry in bundle.manifest["artifacts"]
        if entry["path"] != "shop/supplier-handoff.json"
    ]
    inventory_binding = handoff["payload_inventory_binding"]
    assert inventory_binding["artifact_count"] == len(expected_inventory)
    assert inventory_binding["artifacts"] == expected_inventory
    assert inventory_binding["payload_inventory_sha256"] == hashlib.sha256(
        canonical_json_bytes(expected_inventory)
    ).hexdigest()
    intent = json.loads(by_path["manufacturing/manufacturing-intent.json"])
    assert intent["document_purpose"] == "MACHINE_NEUTRAL_DESIGN_INTENT"
    assert intent["physical_cutting_authorized"] is False
    assert intent["parts"]
    assert bundle.workshop_readiness.physical_cutting_authorized is False


def test_retention_blocker_cannot_hide_invalid_manufacturing_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    adapted = adapt_design_result(design)
    target = adapted.parts[0]
    outside_feature = ManufacturingFeature(
        feature_id="malicious-outside-pocket",
        part_id=target.part_id,
        kind=FeatureKind.POCKET,
        side=Side.A,
        x_um=target.width_um - 1_000,
        y_um=10_000,
        depth_um=1_000,
        width_um=5_000,
        length_um=5_000,
    )
    invalid_target = replace(target, features=(*target.features, outside_feature))
    invalid_adapted = replace(
        adapted,
        parts=tuple(
            invalid_target if part.part_id == target.part_id else part
            for part in adapted.parts
        ),
    )
    monkeypatch.setattr(
        manufacturing_pipeline,
        "dado_retention_evidence_missing",
        _REAL_DADO_RETENTION_CHECK,
    )
    monkeypatch.setattr(
        manufacturing_pipeline,
        "adapt_design_result",
        lambda _design: invalid_adapted,
    )

    with pytest.raises(ProductionBlockedError, match="FEATURE_OUTSIDE_PART") as caught:
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=True,
            allow_blocked_cam=True,
            two_sided_registration_by_stock=explicit_two_sided_registration(stock),
        )

    assert caught.value.report is not None
    assert "FEATURE_OUTSIDE_PART" in {
        issue.code for issue in caught.value.report.blocking_issues
    }


@pytest.mark.parametrize("allow_blocked_cam", (False, True))
def test_surface_back_retention_blocker_cannot_hide_feature_collisions(
    monkeypatch: pytest.MonkeyPatch,
    allow_blocked_cam: bool,
) -> None:
    design, machine, stock, context = design_and_request(
        BookcaseParameters(back_panel=BackPanelType.SURFACE_MOUNTED)
    )
    adapted = adapt_design_result(design)
    rabbet_features = tuple(
        feature
        for part in adapted.parts
        for feature in part.features
        if feature.kind is FeatureKind.RABBET
    )
    assert rabbet_features
    assert all(
        FEATURE_TO_OPERATION[feature.kind] is OperationKind.GROOVE
        for feature in rabbet_features
    )

    monkeypatch.setattr(
        manufacturing_pipeline,
        "unsupported_joint_system_issues",
        _REAL_UNSUPPORTED_JOINT_CHECK,
    )
    monkeypatch.setattr(
        manufacturing_pipeline,
        "generate_operations_document",
        lambda **_kwargs: pytest.fail("unsupported RABBET reached CAM generation"),
    )

    with pytest.raises(ProductionBlockedError, match="FEATURE_COLLISION") as caught:
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=True,
            allow_blocked_cam=allow_blocked_cam,
            two_sided_registration_by_stock=explicit_two_sided_registration(stock),
        )

    assert caught.value.report is not None
    assert {issue.code for issue in caught.value.report.blocking_issues} == {
        "FEATURE_COLLISION"
    }


def test_surface_back_deferral_does_not_hide_an_unrelated_rabbet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, _machine, _stock, _context = design_and_request(
        BookcaseParameters(back_panel=BackPanelType.SURFACE_MOUNTED)
    )
    back_part_ids = {
        part.part_id for part in design.parts if part.role.value == "back"
    }
    carcass_joint = next(
        joint
        for joint in design.joints
        if not back_part_ids & {member.part_id for member in joint.members}
    )
    unrelated_rabbet = carcass_joint.model_copy(
        update={
            "joint_id": "unrelated-carcass-rabbet",
            "joint_type": JointType.RABBET,
            "retention_application_class": None,
            "retention": None,
        }
    )
    canonical_with_extra = design.model_copy(
        update={"joints": (*design.joints, unrelated_rabbet)}
    )
    monkeypatch.setattr(
        manufacturing_dfm,
        "_canonical_bookcase_design",
        lambda _design: canonical_with_extra,
    )

    issues = _REAL_UNSUPPORTED_JOINT_CHECK(
        design,
        defer_surface_back_to_retention=True,
    )

    assert len(issues) == 1
    assert issues[0].code == JOINT_SYSTEM_UNSUPPORTED_CODE
    assert issues[0].inputs["joint_type"] == JointType.RABBET.value
    assert issues[0].inputs["joint_ids"] == (unrelated_rabbet.joint_id,)


@pytest.mark.parametrize(
    "joint_type",
    tuple(
        joint_type
        for joint_type, claim in BOOKCASE_JOINT_SUPPORT_MATRIX.items()
        if claim["status"] == "blocked"
    ),
)
def test_every_matrix_blocked_joint_type_fails_the_manufacturing_support_decision(
    joint_type: JointType,
) -> None:
    assert not joint_type_has_end_to_end_support(joint_type, shelf_mount="fixed")


def test_only_declared_adjustable_shelf_pin_condition_is_accepted() -> None:
    assert joint_type_has_end_to_end_support(JointType.SHELF_PIN, shelf_mount="adjustable")
    assert not joint_type_has_end_to_end_support(JointType.SHELF_PIN, shelf_mount="fixed")


def test_missing_registration_can_yield_only_a_truthful_design_review_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        include_validation_program=True,
        allow_blocked_cam=True,
    )

    paths = {artifact.path for artifact in bundle.artifacts}
    assert bundle.operations is None
    assert bundle.review_status.cam_status.value == "BLOCKED"
    assert bundle.review_status.blocker_codes == ("TWO_SIDED_REGISTRATION_MISSING",)
    assert bundle.dfm_report.status is Severity.PASS
    assert bundle.manifest["release_scope"] == "design_review"
    assert bundle.manifest["machine_use"] == "validation_only"
    assert bundle.manifest["physical_cutting_authorized"] is False
    assert {
        "design/design-spec.json",
        "model/design.step",
        "model/design.glb",
        "validation/design-review-package-status.json",
        "validation/workshop-readiness.json",
    } <= paths
    assert not any(path.startswith("cam/") for path in paths)
    assert not any(path.startswith("nesting/") for path in paths)
    assert not any(path.startswith("machine-validation/") for path in paths)
    assert "materials/stock-purchase.csv" not in paths
    assert bundle.workshop_readiness.design_review_ready is False
    assert bundle.workshop_readiness.physical_cutting_authorized is False
    by_code = {item.code: item.status.value for item in bundle.workshop_readiness.software_evidence}
    assert by_code == {
        "AUTHORITATIVE_CAD": "VERIFIED",
        "DFM_SCREEN": "VERIFIED",
        "SEMANTIC_OPERATIONS": "MISSING",
        "SETUP_SHEETS": "MISSING",
        "VALIDATION_BACKPLOT": "MISSING",
        "NON_CUTTING_PROGRAM": "MISSING",
    }
    material_grain = next(
        item
        for item in bundle.workshop_readiness.workshop_evidence
        if item.code == "MATERIAL_GRAIN"
    )
    assert material_grain.status.value == "VERIFIED"
    assert "catalog-declared non-directional material" in material_grain.evidence


def test_missing_stock_profile_yields_only_a_stockless_design_review_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request(
        BookcaseParameters(back_panel=BackPanelType.SURFACE_MOUNTED)
    )
    monkeypatch.setattr(
        manufacturing_pipeline,
        "dado_retention_evidence_missing",
        _REAL_DADO_RETENTION_CHECK,
    )
    monkeypatch.setattr(
        review_status_contract,
        "dado_retention_evidence_missing",
        _REAL_DADO_RETENTION_CHECK,
    )
    undersized_stock = tuple(
        replace(item, width_um=600_000, height_um=600_000, quantity=24) for item in stock
    )
    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )

    bundle = build_production_bundle(
        design,
        stock=undersized_stock,
        machine=machine,
        context=context,
        include_step=True,
        include_validation_program=True,
        allow_blocked_cam=True,
    )

    paths = {artifact.path for artifact in bundle.artifacts}
    assert bundle.operations is None
    assert bundle.layouts == ()
    assert bundle.review_status.cam_status.value == "BLOCKED"
    assert bundle.review_status.blocker_codes == ("STOCK_PROFILE_MISSING",)
    assert bundle.manifest["postprocessor_version"] == "linuxcnc-validation-1.1.0"
    assert bundle.dfm_report.status is Severity.BLOCK
    assert bundle.dfm_report.engine_version == "dfm-1.3.0"
    assert bundle.dfm_report.blocking_issues
    assert {issue.code for issue in bundle.dfm_report.blocking_issues} == {"STOCK_PROFILE_MISSING"}
    assert {
        "model/design.step",
        "model/design.glb",
        "validation/dfm-report.json",
        "validation/design-review-package-status.json",
        "validation/workshop-readiness.json",
    } <= paths
    assert "materials/stock-purchase.csv" not in paths
    assert "labels/label-index.csv" not in paths
    assert "quality/measurement-plan.json" not in paths
    assert not any(path.startswith(("cam/", "nesting/", "machine-validation/")) for path in paths)
    handoff = json.loads(
        next(
            artifact.data
            for artifact in bundle.artifacts
            if artifact.path == "shop/supplier-handoff.json"
        )
    )
    assert [item["code"] for item in handoff["unresolved_inputs_and_decisions"]] == [
        "STOCK_PROFILE_MISSING"
    ]
    assert [item["code"] for item in handoff["known_unresolved_decisions"]] == [
        BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    ]
    by_code = {item.code: item.status.value for item in bundle.workshop_readiness.software_evidence}
    assert by_code == {
        "AUTHORITATIVE_CAD": "VERIFIED",
        "DFM_SCREEN": "MISSING",
        "SEMANTIC_OPERATIONS": "MISSING",
        "SETUP_SHEETS": "MISSING",
        "VALIDATION_BACKPLOT": "MISSING",
        "NON_CUTTING_PROGRAM": "MISSING",
    }


@pytest.mark.parametrize(
    ("allow_blocked_cam", "include_step"),
    ((False, True), (True, False)),
)
def test_missing_stock_profile_fails_closed_without_explicit_review_route(
    allow_blocked_cam: bool,
    include_step: bool,
) -> None:
    design, machine, stock, context = design_and_request()
    undersized_stock = tuple(
        replace(item, width_um=600_000, height_um=600_000, quantity=24) for item in stock
    )

    with pytest.raises(ProductionBlockedError, match="STOCK_PROFILE_MISSING"):
        build_production_bundle(
            design,
            stock=undersized_stock,
            machine=machine,
            context=context,
            include_step=include_step,
            allow_blocked_cam=allow_blocked_cam,
        )


def test_unbound_directional_stock_is_grain_not_stock_and_stops_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = directional_design_and_request()
    operation_calls = 0

    def forbidden_operations(*args: object, **kwargs: object) -> None:
        nonlocal operation_calls
        operation_calls += 1
        raise AssertionError("operations must not run with an unbound stock axis")

    monkeypatch.setattr(
        manufacturing_pipeline,
        "generate_operations_document",
        forbidden_operations,
    )

    with pytest.raises(ProductionBlockedError, match=DFM_GRAIN_BLOCKER_CODE) as caught:
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=False,
        )

    assert operation_calls == 0
    assert caught.value.report is not None
    assert caught.value.report.blocking_issues
    assert {issue.code for issue in caught.value.report.blocking_issues} == {
        DFM_GRAIN_BLOCKER_CODE
    }
    assert all(
        issue.inputs["assessment_phase"] == "STOCK_MATCHED"
        for issue in caught.value.report.blocking_issues
    )


def test_grain_block_can_yield_only_a_strict_zero_cam_review_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opaque_grain_document = (
        {
            "evidence_type": "material_grain",
            "catalog_id": "uploaded-grain-note",
            "catalog_version": "1.0.0",
            "sha256": "9" * 64,
        },
    )
    design, machine, stock, context = directional_design_and_request(
        external_evidence=opaque_grain_document
    )

    def forbidden_operations(*args: object, **kwargs: object) -> None:
        raise AssertionError("operations must not run with an unbound stock axis")

    monkeypatch.setattr(
        manufacturing_pipeline,
        "generate_operations_document",
        forbidden_operations,
    )
    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        include_validation_program=True,
        allow_blocked_cam=True,
    )

    paths = {artifact.path for artifact in bundle.artifacts}
    assert bundle.layouts == ()
    assert bundle.operations is None
    assert bundle.review_status.blocker_codes == (DFM_GRAIN_BLOCKER_CODE,)
    assert bundle.review_status.required_action == DFM_GRAIN_REQUIRED_ACTION
    assert bundle.review_status.physical_cutting_authorized is False
    assert {issue.code for issue in bundle.dfm_report.blocking_issues} == {
        DFM_GRAIN_BLOCKER_CODE
    }
    assert not any(path.startswith(("cam/", "nesting/", "machine-validation/")) for path in paths)
    assert "materials/stock-purchase.csv" not in paths
    assert "labels/label-index.csv" not in paths
    assert "quality/measurement-plan.json" not in paths
    assert read_and_verify_package(bundle.zip_bytes)["physical_cutting_authorized"] is False
    software = {
        item.code: item.status.value for item in bundle.workshop_readiness.software_evidence
    }
    assert software["DFM_SCREEN"] == "MISSING"
    material_grain = next(
        item
        for item in bundle.workshop_readiness.workshop_evidence
        if item.code == "MATERIAL_GRAIN"
    )
    assert material_grain.status.value == "EXTERNAL_EVIDENCE_REQUIRED"
    assert "not a structured stock-grain axis binding" in material_grain.evidence


def test_stock_profile_precedes_grain_but_retains_canonical_missing_information_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = directional_design_and_request()
    undersized = tuple(
        replace(item, width_um=600_000, height_um=600_000, quantity=24) for item in stock
    )
    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )

    bundle = build_production_bundle(
        design,
        stock=undersized,
        machine=machine,
        context=context,
        include_step=True,
        allow_blocked_cam=True,
    )

    assert bundle.review_status.blocker_codes == ("STOCK_PROFILE_MISSING",)
    assert {issue.code for issue in bundle.dfm_report.blocking_issues} == {
        "STOCK_PROFILE_MISSING"
    }
    grain_warnings = tuple(
        issue for issue in bundle.dfm_report.issues if issue.code == DFM_GRAIN_BLOCKER_CODE
    )
    assert grain_warnings
    assert all(issue.severity is Severity.WARNING for issue in grain_warnings)
    assert all(
        issue.inputs["assessment_phase"] == "STOCK_SELECTION_INCOMPLETE"
        and issue.inputs["binding_status"] == "MISSING_INFORMATION"
        for issue in grain_warnings
    )


def test_bound_directional_stock_advances_to_two_sided_registration_gate() -> None:
    design, machine, stock, context = directional_design_and_request(back_stock_axis="X")

    with pytest.raises(
        ProductionBlockedError,
        match="TWO_SIDED_REGISTRATION_MISSING",
    ) as caught:
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=False,
        )

    assert caught.value.report is not None
    assert {issue.code for issue in caught.value.report.blocking_issues} == {
        "TWO_SIDED_REGISTRATION_MISSING"
    }


def test_blocked_cam_review_package_requires_authoritative_cad() -> None:
    design, machine, stock, context = design_and_request()

    with pytest.raises(ProductionBlockedError, match="requires include_step=true"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=False,
            allow_blocked_cam=True,
        )


@pytest.mark.parametrize(
    "artifact",
    (
        ArtifactFile(
            "machine-validation/injected.validation.ngc",
            b"M2\n",
            "text/plain",
            "WORKER_NOTE",
        ),
        ArtifactFile("cam/operations.json", b"{}", "application/json", "WORKER_NOTE"),
        ArtifactFile("cam/setups/injected.svg", b"<svg/>", "image/svg+xml", "WORKER_NOTE"),
        ArtifactFile("nesting/injected.svg", b"<svg/>", "image/svg+xml", "WORKER_NOTE"),
        ArtifactFile("CAM/rogue.NGC", b"M2\n", "text/plain", "WORKER_NOTE"),
        ArtifactFile("review/rogue.NGC", b"M2\n", "text/plain", "WORKER_NOTE"),
        ArtifactFile(
            "review/backplot.svg",
            b"<svg/>",
            "image/svg+xml",
            "VALIDATION_BACKPLOT",
        ),
        ArtifactFile("toolpaths/finish.nc", b"M2\n", "text/x-gcode", "GCODE"),
        ArtifactFile(
            "review/operations.json",
            b"{}",
            "application/json",
            "WORKER_NOTE",
        ),
        ArtifactFile("review/tool-list.csv", b"tool_id\n", "text/csv", "TOOLING_PLAN"),
        ArtifactFile("setups/sheet.svg", b"<svg/>", "image/svg+xml", "SETUP_PLAN"),
        ArtifactFile(
            "quality/operation-measurements.json",
            b"{}",
            "application/json",
            "OPERATION_QA_PLAN",
        ),
    ),
)
def test_blocked_cam_rejects_caller_manufacturing_artifacts_before_package_build(
    monkeypatch: pytest.MonkeyPatch,
    artifact: ArtifactFile,
) -> None:
    design, machine, stock, context = design_and_request()

    def forbidden_cad(*args: object, **kwargs: object) -> None:
        raise AssertionError("caller artifacts must be rejected before CAD/package generation")

    monkeypatch.setattr(CadQueryAdapter, "export_design", forbidden_cad)

    with pytest.raises(ProductionBlockedError, match="caller-supplied manufacturing"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=True,
            allow_blocked_cam=True,
            additional_artifacts=(artifact,),
        )


def test_blocked_cam_allows_machine_independent_worker_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )
    worker_document = ArtifactFile(
        "assembly/assembly-manual.pdf",
        b"%PDF-1.4\n",
        "application/pdf",
        "ASSEMBLY_REVIEW_MANUAL",
    )

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        allow_blocked_cam=True,
        additional_artifacts=(worker_document,),
    )

    assert worker_document in bundle.artifacts


def test_stock_selection_checks_one_exemplar_before_nesting_quantity() -> None:
    _, machine, _, _ = design_and_request()
    part = PartSpec(
        part_id="repeated-panel",
        name="Repeated panel",
        width_um=600_000,
        height_um=600_000,
        thickness_um=18_000,
        material_id="capacity-material",
        material_version="v1",
        grain_direction="NONE",
        quantity=2,
    )
    stock = StockSheet(
        "one-sheet",
        part.material_id,
        part.material_version,
        700_000,
        700_000,
        part.thickness_um,
        quantity=1,
        grain_direction="NONE",
    )

    groups, selection_issues = manufacturing_pipeline._assign_parts_to_stock(
        (part,),
        (stock,),
    )

    assert selection_issues == ()
    assert len(groups) == 1
    selected_stock, selected_parts = groups[0]
    layout = manufacturing_pipeline.DeterministicNester().nest(selected_parts, selected_stock)
    report = manufacturing_pipeline.DFMValidator().validate(selected_parts, layout, machine)
    assert layout.is_complete is False
    assert {issue.code for issue in report.blocking_issues} >= {"NESTING_UNPLACED"}


def test_stock_selection_defers_aggregate_capacity_shortage_to_nesting() -> None:
    _, machine, _, _ = design_and_request()
    parts = tuple(
        PartSpec(
            part_id=f"panel-{index}",
            name=f"Panel {index}",
            width_um=600_000,
            height_um=600_000,
            thickness_um=18_000,
            material_id="capacity-material",
            material_version="v1",
            grain_direction="NONE",
        )
        for index in range(2)
    )
    stock = StockSheet(
        "one-sheet",
        "capacity-material",
        "v1",
        700_000,
        700_000,
        18_000,
        quantity=1,
        grain_direction="NONE",
    )

    groups, selection_issues = manufacturing_pipeline._assign_parts_to_stock(parts, (stock,))

    assert selection_issues == ()
    assert len(groups) == 1
    selected_stock, selected_parts = groups[0]
    layout = manufacturing_pipeline.DeterministicNester().nest(selected_parts, selected_stock)
    report = manufacturing_pipeline.DFMValidator().validate(selected_parts, layout, machine)
    assert layout.is_complete is False
    assert {issue.code for issue in report.blocking_issues} >= {"NESTING_UNPLACED"}


def test_stock_selection_blocks_before_operations_and_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    back = next(part for part in design.parts if part.actual_thickness_um == 6_000)

    def forbidden_operations(*args: object, **kwargs: object) -> None:
        raise AssertionError("operations must not run after a blocking stock selection issue")

    monkeypatch.setattr(
        manufacturing_pipeline,
        "generate_operations_document",
        forbidden_operations,
    )

    with pytest.raises(ProductionBlockedError, match="STOCK_PROFILE_MISSING") as caught:
        build_production_bundle(
            design,
            stock=(stock[0],),
            machine=machine,
            context=context,
            include_step=False,
        )

    assert caught.value.report is not None
    assert len(caught.value.report.blocking_issues) == 1
    issue = caught.value.report.blocking_issues[0]
    assert issue.code == "STOCK_PROFILE_MISSING"
    assert issue.part_id == back.part_id
    assert issue.inputs["blank_um"] == (back.raw_size.width_um, back.raw_size.height_um)


def test_all_stock_dfm_blocks_before_any_operations_or_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    oversized_back_stock = replace(stock[1], height_um=machine.work_height_um + 1)
    operation_calls = 0

    def forbidden_operations(*args: object, **kwargs: object) -> None:
        nonlocal operation_calls
        operation_calls += 1
        raise AssertionError("operations must not run until every stock layout passes DFM")

    monkeypatch.setattr(
        manufacturing_pipeline,
        "generate_operations_document",
        forbidden_operations,
    )

    with pytest.raises(ProductionBlockedError, match="MACHINE_STOCK_ENVELOPE") as caught:
        build_production_bundle(
            design,
            stock=(stock[0], oversized_back_stock),
            machine=machine,
            context=context,
            include_step=False,
        )

    assert operation_calls == 0
    assert caught.value.report is not None
    assert {issue.code for issue in caught.value.report.blocking_issues} == {
        "MACHINE_STOCK_ENVELOPE"
    }


def test_generic_statusless_single_sided_bundle_is_not_a_current_schema_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, machine, _, original_context = design_and_request()
    design_hash = "e" * 64
    part = PartSpec(
        part_id="single-sided-panel",
        name="Single-sided panel",
        width_um=300_000,
        height_um=200_000,
        thickness_um=18_000,
        material_id="single-sided-material",
        material_version="v1",
        features=(
            ManufacturingFeature(
                feature_id="single-sided-outline",
                part_id="single-sided-panel",
                kind=FeatureKind.OUTER_CONTOUR,
                side=Side.A,
                x_um=0,
                y_um=0,
                depth_um=18_000,
                width_um=300_000,
                length_um=200_000,
                through=True,
            ),
        ),
        grain_direction="NONE",
    )
    adapted = AdaptedDesign(
        design_hash=design_hash,
        engine_version="test-engine",
        template_version="test-template",
        parts=(part,),
    )
    design = SimpleNamespace(
        design_hash=design_hash,
        spec={"design_id": "single-sided-design"},
        parts=(part,),
        joints=(),
        assembly_graph=None,
        total_weight_g=0,
    )
    stock = StockSheet(
        "single-sided-stock",
        part.material_id,
        part.material_version,
        1_000_000,
        600_000,
        part.thickness_um,
        grain_direction="NONE",
    )
    context = replace(original_context, design_hash=design_hash)
    monkeypatch.setattr(manufacturing_pipeline, "adapt_design_result", lambda _: adapted)

    with pytest.raises(ProductionBlockedError, match="statusless generation is disabled"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=False,
        )


def test_dado_dimensions_propagate_to_bom_cam_and_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request(
        BookcaseParameters(shelf_count=1, vertical_divider_count=1)
    )

    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )
    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        two_sided_registration_by_stock=explicit_two_sided_registration(stock),
    )
    artifact_by_path = {artifact.path: artifact.data for artifact in bundle.artifacts}
    bom_rows = {
        row["part_id"]: row
        for row in csv.DictReader(io.StringIO(artifact_by_path["bom/bom.csv"].decode("utf-8")))
    }
    by_key = {part.semantic_key: part for part in design.parts}

    expected_panel_dimensions = {
        "bottom": ("876", "320"),
        "top": ("876", "320"),
        "divider-0": ("308", "1896"),
        "shelf-r0-b0": ("435", "298"),
        "shelf-r0-b1": ("435", "298"),
        "plinth": ("864", "86"),
        # Each outer edge retains the structural 6 mm capture; each
        # divider-facing edge uses 5.95 mm so adjacent back grooves have the
        # versioned 6.1 mm nominal R3/tolerance spacing.
        "back-b0": ("434.95", "1896"),
        "back-b1": ("434.95", "1896"),
    }
    for key, (width_mm, height_mm) in expected_panel_dimensions.items():
        row = bom_rows[by_key[key].part_id]
        assert row["finished_width_mm"] == width_mm
        assert row["finished_height_mm"] == height_mm

    backs = sorted(
        (part for part in design.parts if part.role == PartRole.BACK),
        key=lambda part: part.instance_index,
    )
    assert backs[1].placement.x_um - (
        backs[0].placement.x_um + backs[0].finished_size.width_um
    ) == 6_100

    left_bottom_joint = next(
        joint
        for joint in design.joints
        if {by_key["left-side"].part_id, by_key["bottom"].part_id}
        == {member.part_id for member in joint.members}
    )
    groove_feature_id = left_bottom_joint.members[0].feature_ids[0]
    groove_operation = next(
        operation
        for operation in bundle.operations.operations
        if operation.feature_id == groove_feature_id
    )
    assert groove_operation.kind == OperationKind.GROOVE
    assert groove_operation.depth_um == 6_000
    assert sorted((groove_operation.width_um, groove_operation.length_um)) == [18_500, 320_000]
    assert groove_operation.tolerance_um == 50
    assert groove_operation.fit_clearance_um == 500
    assert groove_operation.corner_strategy == "dogbone-v2"
    assert groove_operation.corner_relief_radius_um == 3_000
    assert groove_operation.open_end_reliefs
    assert groove_operation.cutter_envelope_width_um is not None
    assert groove_operation.cutter_envelope_length_um is not None
    assert bundle.operations.schema_version == "custombuild.operations.v2"

    assembly_step = next(
        step for step in design.assembly_graph.steps if left_bottom_joint.joint_id in step.joint_ids
    )
    assert {
        by_key["left-side"].part_id,
        by_key["bottom"].part_id,
    } <= set(assembly_step.part_ids)
    assert assembly_step == design.assembly_graph.steps[-1]

    left_side_id = by_key["left-side"].part_id
    side_dxf = artifact_by_path[f"parts/{left_side_id}/B.dxf"].decode("utf-8")
    side_svg = artifact_by_path[f"drawings/{left_side_id}/B.svg"].decode("utf-8")
    dxf_document = ezdxf.read(io.StringIO(side_dxf))
    assert len(dxf_document.modelspace().query('CIRCLE[layer=="GROOVE"]')) >= 4
    assert 'data-corner-strategy="dogbone-v2"' in side_svg


def test_domain_to_multistock_bundle_is_safe_complete_and_reproducible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    extra = ArtifactFile(
        "assembly/assembly-manual.pdf",
        b"%PDF-test",
        "application/pdf",
        "ASSEMBLY_REVIEW_MANUAL",
    )

    monkeypatch.setattr(
        CadQueryAdapter,
        "export_design",
        lambda self, result: valid_cad_for(result),
    )
    first = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        two_sided_registration_by_stock=explicit_two_sided_registration(stock),
        additional_artifacts=(extra,),
    )
    second = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        two_sided_registration_by_stock=explicit_two_sided_registration(stock),
        additional_artifacts=(extra,),
    )

    assert first.zip_bytes == second.zip_bytes
    assert first.dfm_report.status == Severity.PASS
    assert {layout.stock.thickness_um for layout in first.layouts} == {6_000, 18_000}
    assert sum(layout.used_sheet_count for layout in first.layouts) == 3
    assert len(first.operations.operations) == 30
    assert {setup.stock_thickness_um for setup in first.operations.setups} == {6_000, 18_000}
    assert any(
        "METHOD=test-registration:mdf-18-sheets:0" in setup.probe_method
        for setup in first.operations.setups
    )
    contour_part_ids = {
        operation.part_id
        for operation in first.operations.operations
        if operation.kind == OperationKind.CONTOUR
    }
    assert contour_part_ids == {part.part_id for part in design.parts}
    back_part_id = next(part.part_id for part in design.parts if part.actual_thickness_um == 6_000)
    back_contour = next(
        operation
        for operation in first.operations.operations
        if operation.part_id == back_part_id and operation.kind == OperationKind.CONTOUR
    )
    setup_by_id = {setup.setup_id: setup for setup in first.operations.setups}
    assert setup_by_id[back_contour.setup_id].stock_thickness_um == 6_000
    assert first.manifest["cad_status"] == "GENERATED"
    assert first.manifest["template_id"] == "shelving"
    assert first.manifest["template_capability_fingerprint"] == "c" * 64
    assert first.manifest["domain_template_version"] == design.template_version
    assert first.manifest["template_version"] == design.template_version
    assert first.manifest["template_capability_version"] == "1.0.0"
    assert first.manifest["template_capability_registry_version"] == "test-registry-1.0.0"
    assert first.manifest["release_scope"] == "design_review"
    assert first.manifest["machine_use"] == "validation_only"
    assert first.manifest["physical_cutting_authorized"] is False
    assert {"mdf@screening-2026.1", "mdf-6@screening-2026.1"} == set(
        first.manifest["material_versions"]
    )

    with zipfile.ZipFile(io.BytesIO(first.zip_bytes)) as archive:
        assert archive.read("assembly/assembly-manual.pdf") == b"%PDF-test"
        frozen_spec = json.loads(archive.read("design/design-spec.json"))
        assert frozen_spec == {
            "schema_version": manufacturing_pipeline.FROZEN_DESIGN_SPEC_SCHEMA_VERSION,
            "spec": design.spec.model_dump(mode="json"),
        }
        result_summary = json.loads(archive.read("design/result-summary.json"))
        assert result_summary["design_hash"] == design.design_hash
        assert result_summary["domain_template_version"] == design.template_version
        assert result_summary["part_ids"] == sorted(part.part_id for part in design.parts)
        assert result_summary["joint_ids"] == sorted(joint.joint_id for joint in design.joints)
        assert result_summary["assembly_step_count"] == len(design.assembly_graph.steps)
        assert result_summary["total_weight_g"] == design.total_weight_g
        assert (
            result_summary["design_spec"]["sha256"]
            == hashlib.sha256(archive.read("design/design-spec.json")).hexdigest()
        )
        freecad_status = json.loads(archive.read("validation/cad-interchange-status.json"))
        assert freecad_status["status"] == "OPTIONAL_NOT_REQUESTED"
        assert freecad_status["requested"] is False
        readiness = json.loads(archive.read("validation/workshop-readiness.json"))
        assert readiness["design_review_ready"] is True
        assert readiness["physical_cutting_authorized"] is False
        assert readiness["missing_evidence_count"] > 0
        handoff = json.loads(archive.read("shop/supplier-handoff.json"))
        assert handoff["package_identity"] == {
            "project_id": "project-fixture",
            "revision": "1",
            "design_hash": design.design_hash,
        }
        assert handoff["operation_binding"]["status"] == (
            "MACHINE_NEUTRAL_VALIDATION_ONLY"
        )
        assert handoff["supplier_stages"]["cut_authorized"] is False
        manifest_inventory = {
            entry["path"]: entry for entry in first.manifest["artifacts"]
        }
        assert set(manifest_inventory) == set(archive.namelist()) - {"manifest.json"}
        assert manifest_inventory["shop/supplier-handoff.json"]["sha256"] == (
            hashlib.sha256(archive.read("shop/supplier-handoff.json")).hexdigest()
        )
        programs = [name for name in archive.namelist() if name.endswith(".validation.ngc")]
        assert programs
        for program in programs:
            content = archive.read(program)
            validate_validation_program(
                content,
                required_safe_z_mm=Decimal("15"),
                maximum_z_mm=Decimal("15"),
                x_bounds_mm=(Decimal(0), Decimal(machine.work_width_um) / 1_000),
                y_bounds_mm=(Decimal(0), Decimal(machine.work_height_um) / 1_000),
                required_wcs="G55" if b"\nG55\n" in content else "G54",
            )


def test_freecad_export_requires_step_and_enters_the_review_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design, machine, stock, context = design_and_request()
    cad = valid_cad_for(design)
    step = cad.step

    def export_design(self: CadQueryAdapter, value: object) -> CADArtifacts:
        return cad

    monkeypatch.setattr(manufacturing_pipeline.CadQueryAdapter, "export_design", export_design)

    fcstd_stream = io.BytesIO()
    with zipfile.ZipFile(fcstd_stream, "w") as archive:
        archive.writestr("Document.xml", "<Document SchemaVersion='4' />")

    class FakeBridge:
        def convert_authoritative_step(
            self,
            source_step: bytes,
            design_hash: str,
            *,
            metadata: dict[str, str],
        ) -> FreeCADProjectArtifacts:
            assert source_step == step
            assert design_hash == design.design_hash
            assert metadata["project_id"] == context.project_id
            return FreeCADProjectArtifacts(
                fcstd=fcstd_stream.getvalue(),
                source_step_sha256=hashlib.sha256(source_step).hexdigest(),
                runtime_version="test-freecad-1.0",
            )

    with pytest.raises(ProductionBlockedError, match="requires include_step"):
        build_production_bundle(
            design,
            stock=stock,
            machine=machine,
            context=context,
            include_step=False,
            include_freecad_project=True,
        )

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        include_freecad_project=True,
        two_sided_registration_by_stock=explicit_two_sided_registration(stock),
        freecad_bridge=FakeBridge(),  # type: ignore[arg-type]
    )
    by_path = {artifact.path: artifact.data for artifact in bundle.artifacts}
    assert by_path["model/design.fcstd"].startswith(b"PK")
    status = json.loads(by_path["validation/cad-interchange-status.json"])
    assert status["status"] == "GENERATED"
    assert status["requested"] is True
    assert status["runtime_probe_performed"] is True
    assert status["runtime_version"] == "test-freecad-1.0"
    assert status["source_step_sha256"] == hashlib.sha256(step).hexdigest()


@pytest.mark.cad
def test_full_domain_bundle_contains_genuine_authoritative_step_and_glb() -> None:
    if not CadQueryAdapter.available():
        pytest.skip("CadQuery/OpenCascade is not installed")
    design, machine, stock, context = design_and_request()

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=True,
        two_sided_registration_by_stock=explicit_two_sided_registration(stock),
    )
    by_path = {artifact.path: artifact.data for artifact in bundle.artifacts}

    assert by_path["model/design.step"].startswith(b"ISO-10303-21")
    assert by_path["model/design.glb"].startswith(b"glTF")
    assert len(by_path["model/design.step"]) > 100_000
    assert len(by_path["model/design.glb"]) > 10_000
    assert bundle.manifest["cad_status"] == "GENERATED"
    assert bundle.workshop_readiness.design_review_ready is True
    assert bundle.workshop_readiness.physical_cutting_authorized is False
