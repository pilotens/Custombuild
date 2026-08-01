from __future__ import annotations

import csv
import io
import json
from dataclasses import replace

import pytest
from custombuild_manufacturing import (
    ArtifactFile,
    DeterministicNester,
    FeatureKind,
    ManifestContext,
    ManufacturingFeature,
    PartSpec,
    ProductionBlockedError,
    Side,
    StockSheet,
    build_deterministic_zip,
    generate_operations_document,
    linuxcnc_reference_router_1325,
    read_and_verify_package,
    um_to_mm,
)
from custombuild_manufacturing.errors import ArtifactError
from custombuild_manufacturing.exporters import dxf_for_part, svg_for_part, tool_list_csv
from custombuild_manufacturing.package import default_artifacts
from custombuild_manufacturing.profiles import tool_catalog_fingerprint


def manufacturing_values():
    features = (
        ManufacturingFeature(
            "a-hole",
            "panel",
            FeatureKind.DRILL,
            Side.A,
            50_000,
            50_000,
            10_000,
            diameter_um=8_000,
        ),
        ManufacturingFeature(
            "b-groove",
            "panel",
            FeatureKind.GROOVE,
            Side.B,
            100_000,
            80_000,
            6_000,
            width_um=8_000,
            length_um=50_000,
        ),
    )
    panel = PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        features=features,
        grain_direction="NONE",
    )
    source_stock = StockSheet(
        "sheet",
        "mdf",
        "v1",
        1_000_000,
        600_000,
        18_000,
        grain_direction="NONE",
    )
    layout = DeterministicNester().nest((panel,), source_stock)
    machine = linuxcnc_reference_router_1325()
    operations = generate_operations_document(
        design_hash="d" * 64,
        parts=(panel,),
        layout=layout,
        machine=machine,
    )
    context = ManifestContext(
        "project",
        "1",
        "d" * 64,
        "0.1.0",
        "0.1.0",
        "1.0.0",
        "1.0.0",
        ("mdf@v1",),
        "1.0.0",
        machine.profile_id,
        machine.version,
        "linuxcnc-validation-1.0.0",
        "NOT_REQUESTED",
        "f" * 64,
        {"schema_version": "test-production-context.v1"},
    )
    return panel, layout, operations, context


def test_dxf_files_are_strictly_side_separated() -> None:
    panel, _, _, _ = manufacturing_values()

    side_a = dxf_for_part(panel, Side.A).decode("utf-8")
    side_b = dxf_for_part(panel, Side.B).decode("utf-8")

    assert "FEATURE:a-hole" in side_a
    assert "FEATURE:b-groove" not in side_a
    assert "FEATURE:b-groove" in side_b
    assert "FEATURE:a-hole" not in side_b
    assert "CUSTOMBUILD_SIDE:A" in side_a
    assert "CUSTOMBUILD_SIDE:B" in side_b


def test_outer_contour_replaces_fallback_outline_in_dxf_and_svg_on_each_side() -> None:
    panel, _, _, _ = manufacturing_values()
    contour = ManufacturingFeature(
        "outline:panel",
        "panel",
        FeatureKind.OUTER_CONTOUR,
        Side.A,
        0,
        0,
        panel.thickness_um,
        width_um=panel.width_um,
        length_um=panel.height_um,
        through=True,
        metadata={"derived": True, "is_part_outline": True},
    )
    panel = replace(panel, features=(*panel.features, contour))

    side_a_dxf = dxf_for_part(panel, Side.A).decode("utf-8")
    side_b_dxf = dxf_for_part(panel, Side.B).decode("utf-8")
    side_a_svg = svg_for_part(panel, Side.A).decode("utf-8")
    side_b_svg = svg_for_part(panel, Side.B).decode("utf-8")

    dxf_outline_entity = "0\nLWPOLYLINE\n8\nOUTLINE\n"
    assert side_a_dxf.count(dxf_outline_entity) == 1
    assert side_b_dxf.count(dxf_outline_entity) == 1
    assert side_a_dxf.count("FEATURE:outline:panel") == 1
    assert "FEATURE:outline:panel" not in side_b_dxf
    assert side_a_svg.count('class="outline"') == 1
    assert side_b_svg.count('class="outline"') == 1
    assert 'class="outline" data-feature-id="outline:panel"' in side_a_svg
    assert 'class="pocket" data-feature-id="outline:panel"' not in side_a_svg
    assert 'data-feature-id="outline:panel"' not in side_b_svg


def test_export_rejects_ambiguous_multiple_outer_contours_per_side() -> None:
    panel, _, _, _ = manufacturing_values()
    contours = tuple(
        ManufacturingFeature(
            f"outline:{index}",
            "panel",
            FeatureKind.OUTER_CONTOUR,
            Side.A,
            0,
            0,
            panel.thickness_um,
            width_um=panel.width_um,
            length_um=panel.height_um,
            through=True,
        )
        for index in range(2)
    )
    panel = replace(panel, features=(*panel.features, *contours))

    with pytest.raises(ValueError, match="multiple outer contours"):
        dxf_for_part(panel, Side.A)
    with pytest.raises(ValueError, match="multiple outer contours"):
        svg_for_part(panel, Side.A)


def test_zip_manifest_and_bytes_are_deterministic() -> None:
    panel, layout, operations, context = manufacturing_values()
    artifacts = default_artifacts(parts=(panel,), layout=layout, operations=operations)
    by_path = {artifact.path: artifact for artifact in artifacts}

    first = build_deterministic_zip(context, artifacts)
    second = build_deterministic_zip(context, tuple(reversed(artifacts)))
    manifest = read_and_verify_package(first)

    assert first == second
    assert manifest["design_hash"] == "d" * 64
    assert manifest["production_context_hash"]
    assert all(len(item["sha256"]) == 64 for item in manifest["artifacts"])
    material_list = by_path["materials/material-list.csv"].data.decode("utf-8")
    assert "material_id,material_version,thickness_mm" in material_list
    assert "mdf,v1,18,1,0.060000,0.060000,0.000" in material_list
    setup_sheets = [artifact for artifact in artifacts if artifact.role == "SETUP_SHEET"]
    assert len(setup_sheets) == len(operations.setups)
    assert all(b"Custombuild setupblad" in artifact.data for artifact in setup_sheets)
    assert all(b"VALIDERINGSL" in artifact.data for artifact in setup_sheets)

    operations_payload = json.loads(by_path["cam/operations.json"].data)
    assert operations_payload["tool_catalog_version"] == operations.tool_catalog_version
    assert operations_payload["tool_catalog_fingerprint"] == operations.tool_catalog_fingerprint
    assert operations_payload["tools"] == operations.as_dict()["tools"]

    tool_rows = list(csv.DictReader(io.StringIO(by_path["cam/tool-list.csv"].data.decode("utf-8"))))
    assert tool_rows
    assert list(tool_rows[0]) == [
        "setup_id",
        "tool_id",
        "tool_version",
        "nominal_diameter_mm",
        "measured_diameter_mm",
        "effective_diameter_mm",
        "runout_mm",
        "cutting_length_mm",
        "spindle_rpm",
        "feed_mm_min",
        "plunge_mm_min",
        "operation_count",
    ]
    tools_by_id = {tool.tool_id: tool for tool in operations.tools}
    for row in tool_rows:
        tool = tools_by_id[row["tool_id"]]
        assert row["tool_version"] == tool.version
        assert row["nominal_diameter_mm"] == str(tool.diameter_um // 1_000)
        assert row["measured_diameter_mm"] == ""
        assert row["effective_diameter_mm"] == str(tool.effective_diameter_um // 1_000)
        assert row["runout_mm"] == "0"
        assert row["cutting_length_mm"] == str(tool.cutting_length_um // 1_000)
        assert row["spindle_rpm"] == str(tool.spindle_rpm)
        assert row["feed_mm_min"] == str(tool.feed_um_min // 1_000)
        assert row["plunge_mm_min"] == str(tool.plunge_um_min // 1_000)
        expected_count = sum(
            operation.setup_id == row["setup_id"] and operation.tool_id == row["tool_id"]
            for operation in operations.operations
        )
        assert row["operation_count"] == str(expected_count)


def test_validation_postprocessor_cannot_be_marked_as_production_release() -> None:
    panel, layout, operations, context = manufacturing_values()
    artifacts = list(default_artifacts(parts=(panel,), layout=layout, operations=operations))
    artifacts.extend(
        (
            ArtifactFile(
                "model/design.step", b"ISO-10303-21;\nEND-ISO-10303-21;", "model/step", "TEST"
            ),
            ArtifactFile("model/design.glb", b"glTF\x02\x00\x00\x00", "model/gltf-binary", "TEST"),
        )
    )
    with pytest.raises(ProductionBlockedError, match="release is disabled"):
        build_deterministic_zip(
            replace(context, cad_status="GENERATED"),
            artifacts,
            production_release=True,
        )


def test_tool_list_exposes_calibration_and_cutting_parameters() -> None:
    _, _, operations, _ = manufacturing_values()
    source = operations.tools[0]
    calibrated = replace(
        source,
        version="calibration-2026-08-01",
        measured_diameter_um=source.diameter_um - 11,
        runout_um=17,
    )
    tools = tuple(
        calibrated if tool.tool_id == source.tool_id else tool for tool in operations.tools
    )
    calibrated_document = replace(
        operations,
        tools=tools,
        tool_catalog_fingerprint=tool_catalog_fingerprint(tools),
    )

    rows = list(csv.DictReader(io.StringIO(tool_list_csv(calibrated_document).decode("utf-8"))))
    row = next(item for item in rows if item["tool_id"] == calibrated.tool_id)
    assert calibrated.measured_diameter_um is not None
    assert row == {
        "setup_id": row["setup_id"],
        "tool_id": calibrated.tool_id,
        "tool_version": "calibration-2026-08-01",
        "nominal_diameter_mm": um_to_mm(calibrated.diameter_um),
        "measured_diameter_mm": um_to_mm(calibrated.measured_diameter_um),
        "effective_diameter_mm": um_to_mm(calibrated.measured_diameter_um),
        "runout_mm": "0.017",
        "cutting_length_mm": str(calibrated.cutting_length_um // 1_000),
        "spindle_rpm": str(calibrated.spindle_rpm),
        "feed_mm_min": str(calibrated.feed_um_min // 1_000),
        "plunge_mm_min": str(calibrated.plunge_um_min // 1_000),
        "operation_count": row["operation_count"],
    }


def test_artifact_paths_cannot_escape_package() -> None:
    with pytest.raises(ArtifactError):
        ArtifactFile("../escape.txt", b"bad", "text/plain", "TEST")
