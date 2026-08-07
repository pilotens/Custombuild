from __future__ import annotations

import csv
import io
import zipfile
from decimal import Decimal

import pytest
from custombuild_cad import CadQueryAdapter
from custombuild_domain import (
    BookcaseDesignSpec,
    BookcaseParameters,
    build_bookcase,
    screening_mdf_6,
    screening_mdf_18,
)
from custombuild_manufacturing import (
    ArtifactFile,
    ManifestContext,
    OperationKind,
    Severity,
    StockSheet,
    build_production_bundle,
    linuxcnc_reference_router_1325,
)
from custombuild_postprocessors import validate_validation_program


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
        rule_version="rules-1.0.0",
        material_versions=(),
        joint_version="joints-1.0.0",
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        postprocessor_version="derived-by-pipeline",
        cad_status="derived-by-pipeline",
        generation_context_hash="f" * 64,
        production_engine_context={"schema_version": "test-production-context.v1"},
    )
    return design, machine, stock, context


def test_dado_dimensions_propagate_to_bom_cam_and_assembly() -> None:
    design, machine, stock, context = design_and_request(
        BookcaseParameters(shelf_count=1, vertical_divider_count=1)
    )

    bundle = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=False,
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
        "divider-0": ("300", "1896"),
        "shelf-r0-b0": ("435", "300"),
        "shelf-r0-b1": ("435", "300"),
        "plinth": ("864", "86"),
        "back": ("876", "1896"),
    }
    for key, (width_mm, height_mm) in expected_panel_dimensions.items():
        row = bom_rows[by_key[key].part_id]
        assert row["finished_width_mm"] == width_mm
        assert row["finished_height_mm"] == height_mm

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
    assert groove_operation.corner_strategy == "dogbone-v1"
    assert groove_operation.corner_relief_radius_um == 3_000

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
    assert side_dxf.count("\nCIRCLE\n8\nGROOVE\n") >= 4
    assert 'data-corner-strategy="dogbone-v1"' in side_svg


def test_domain_to_multistock_bundle_is_safe_complete_and_reproducible() -> None:
    design, machine, stock, context = design_and_request()
    extra = ArtifactFile("documents/manual.pdf", b"%PDF-test", "application/pdf", "MANUAL")

    first = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=False,
        additional_artifacts=(extra,),
    )
    second = build_production_bundle(
        design,
        stock=stock,
        machine=machine,
        context=context,
        include_step=False,
        additional_artifacts=(extra,),
    )

    assert first.zip_bytes == second.zip_bytes
    assert first.dfm_report.status == Severity.PASS
    assert {layout.stock.thickness_um for layout in first.layouts} == {6_000, 18_000}
    assert sum(layout.used_sheet_count for layout in first.layouts) == 3
    assert len(first.operations.operations) == 30
    assert {setup.stock_thickness_um for setup in first.operations.setups} == {6_000, 18_000}
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
    assert first.manifest["cad_status"] == "NOT_REQUESTED"
    assert {"mdf@screening-2026.1", "mdf-6@screening-2026.1"} == set(
        first.manifest["material_versions"]
    )

    with zipfile.ZipFile(io.BytesIO(first.zip_bytes)) as archive:
        assert archive.read("documents/manual.pdf") == b"%PDF-test"
        programs = [name for name in archive.namelist() if name.endswith(".validation.ngc")]
        assert programs
        for program in programs:
            validate_validation_program(
                archive.read(program),
                required_safe_z_mm=Decimal("15"),
            )


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
    )
    by_path = {artifact.path: artifact.data for artifact in bundle.artifacts}

    assert by_path["model/design.step"].startswith(b"ISO-10303-21")
    assert by_path["model/design.glb"].startswith(b"glTF")
    assert len(by_path["model/design.step"]) > 100_000
    assert len(by_path["model/design.glb"]) > 10_000
    assert bundle.manifest["cad_status"] == "GENERATED"
