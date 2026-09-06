from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import replace
from xml.etree import ElementTree

import ezdxf
import pytest
from custombuild_manufacturing import (
    ArtifactFile,
    DeterministicNester,
    EdgeBandSpec,
    FeatureKind,
    ManifestContext,
    ManufacturingFeature,
    PartSpec,
    Point2D,
    ProductionBlockedError,
    Rect,
    Side,
    StockSheet,
    build_deterministic_zip,
    generate_operations_document,
    linuxcnc_reference_router_1325,
    um_to_mm,
)
from custombuild_manufacturing.errors import ArtifactError
from custombuild_manufacturing.exporters import (
    bom_csv,
    dxf_for_part,
    svg_for_part,
    tool_list_csv,
)
from custombuild_manufacturing.operations import TwoSidedRegistration
from custombuild_manufacturing.package import default_artifacts
from custombuild_manufacturing.profiles import tool_catalog_fingerprint
from ezdxf import bbox, units


def _raw_builder_manifest(payload: bytes) -> dict[str, object]:
    """Inspect builder output without invoking current schema-v5 acceptance policy."""

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        value = json.loads(archive.read("manifest.json"))
    assert isinstance(value, dict)
    return value


def _dxf_json_comment_payload(source: str, label: str) -> dict[str, object]:
    lines = source.splitlines()
    comments = [
        lines[index + 1]
        for index, value in enumerate(lines[:-1])
        if value == "999" and lines[index + 1].startswith(f"{label}:")
    ]
    assert comments
    chunks: dict[int, str] = {}
    expected_count: int | None = None
    for comment in comments:
        sequence, chunk = comment.removeprefix(f"{label}:").split(":", 1)
        raw_index, raw_count = sequence.split("/", 1)
        expected_count = int(raw_count)
        chunks[int(raw_index)] = chunk
    assert expected_count == len(chunks)
    payload = json.loads("".join(chunks[index] for index in range(1, expected_count + 1)))
    assert isinstance(payload, dict)
    return payload


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
        clamp_zones=(
            Rect(16_500, 576_500, 7_000, 7_000),
            Rect(896_500, 576_500, 7_000, 7_000),
        ),
    )
    layout = DeterministicNester().nest((panel,), source_stock)
    machine = linuxcnc_reference_router_1325()
    operations = generate_operations_document(
        design_hash="d" * 64,
        parts=(panel,),
        layout=layout,
        machine=machine,
        two_sided_registration_by_sheet={
            0: TwoSidedRegistration(
                declaration_authority="CLIENT_DECLARED",
                method_id="fixture-registration-v1",
                fixture_method_version="fixture-v1",
                pin_diameter_um=6_000,
                position_tolerance_um=500,
                points=(Point2D(20_000, 580_000), Point2D(900_000, 580_000)),
            )
        },
    )
    context = ManifestContext(
        "project",
        "1",
        "d" * 64,
        "0.1.0",
        "0.1.0",
        "1.0.0",
        "shelving",
        "c" * 64,
        {
            "template_id": "shelving",
            "template_version": "1.0.0",
            "capability_fingerprint": "c" * 64,
        },
        "1.0.0",
        ("mdf@v1",),
        "1.0.0",
        machine.profile_id,
        machine.version,
        "linuxcnc-validation-1.1.0",
        "NOT_REQUESTED",
        "f" * 64,
        {
            "schema_version": "test-production-context.v1",
            "template_capability_registry_version": "test-registry-1.0.0",
        },
    )
    return panel, layout, operations, context


def legacy_full_cam_artifacts(
    *,
    panel: PartSpec,
    layout,
    operations,
) -> tuple[ArtifactFile, ...]:
    return (
        *default_artifacts(parts=(panel,), layout=layout, operations=operations),
        ArtifactFile(
            "cam/validation-backplot.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"/>',
            "image/svg+xml",
            "VALIDATION_BACKPLOT",
        ),
        ArtifactFile(
            "machine-validation/validation-only.ngc",
            b"(VALIDATION ONLY)\nM2\n",
            "text/x-gcode",
            "NON_CUTTING_VALIDATION_PROGRAM",
        ),
    )


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


def test_dxf_declares_millimetres_and_an_independent_parser_confirms_bounds() -> None:
    panel, _, _, _ = manufacturing_values()

    document = ezdxf.read(io.StringIO(dxf_for_part(panel, Side.A).decode("utf-8")))
    outline = tuple(document.modelspace().query('LWPOLYLINE[layer=="OUTLINE"]'))
    bounds = bbox.extents(outline, fast=True)
    audit = document.audit()

    assert document.units == units.MM
    assert document.header["$INSUNITS"] == units.MM
    assert document.header["$MEASUREMENT"] == 1
    assert document.header["$LUNITS"] == 2
    assert document.header["$LUPREC"] == 3
    assert audit.errors == []
    assert audit.fixes == []
    assert tuple(bounds.extmin) == pytest.approx((0.0, 0.0, 0.0))
    assert tuple(bounds.extmax) == pytest.approx((300.0, 200.0, 0.0))


def test_part_drawings_bind_supplier_datums_axes_depth_tolerance_and_blank_size() -> None:
    panel, _, _, _ = manufacturing_values()
    hole = replace(panel.features[0], tolerance_um=50, fit_clearance_um=100)
    panel = replace(
        panel,
        raw_width_um=304_000,
        raw_height_um=204_000,
        features=(hole, panel.features[1]),
        metadata={"domain_a_side": "BOTTOM", "domain_b_side": "TOP"},
    )

    first_dxf = dxf_for_part(panel, Side.A)
    assert first_dxf == dxf_for_part(panel, Side.A)
    dxf = first_dxf.decode("utf-8")
    drawing = _dxf_json_comment_payload(dxf, "CUSTOMBUILD_DRAWING_JSON")
    feature = _dxf_json_comment_payload(dxf, "CUSTOMBUILD_FEATURE_0001_JSON")

    assert drawing["schema_version"] == "custombuild.part-drawing.v2"
    assert drawing["physical_face"] == "BOTTOM"
    assert drawing["coordinate_system"] == "LOCAL_UV_MM_V_UP_NOT_MIRRORED"
    assert drawing["coordinates_mirrored"] is False
    assert drawing["origin"] == "FINISHED_PART_U_MIN_V_MIN"
    assert drawing["material"] == {"id": "mdf", "version": "v1"}
    assert drawing["grain_direction"] == "NONE"
    assert drawing["edge_bands"] == []
    assert drawing["datums"] == {
        "primary": "BOTTOM_FINISHED_SURFACE",
        "secondary": "U_MIN_FINISHED_EDGE",
        "tertiary": "V_MIN_FINISHED_EDGE",
    }
    assert drawing["finished_size_mm"] == {"u": "300", "v": "200", "thickness": "18"}
    assert drawing["raw_blank_size_mm"] == {"u": "304", "v": "204", "thickness": "18"}
    assert drawing["emitted_geometry_extents_mm"] == {
        "u_min": "0",
        "v_min": "0",
        "u_max": "300",
        "v_max": "200",
    }
    assert feature["feature_id"] == "a-hole"
    assert feature["origin_semantics"] == "FIRST_CENTRE"
    assert feature["origin_mm"] == {"u": "50", "v": "50"}
    assert feature["depth_mm"] == "10"
    assert feature["dimensions_mm"] == {"diameter": "8"}
    assert feature["tolerance_mm"] == "0.05"
    assert feature["tolerance_status"] == "DECLARED_IN_DESIGN"
    assert feature["fit_clearance_mm"] == "0.1"

    svg_bytes = svg_for_part(panel, Side.A)
    assert svg_bytes == svg_for_part(panel, Side.A)
    root = ElementTree.fromstring(svg_bytes)  # noqa: S314 -- trusted locally generated SVG
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    metadata = root.find("svg:metadata", namespace)
    geometry = root.find("svg:g[@class='machining-geometry']", namespace)
    circles = root.findall(".//svg:circle[@data-feature-id='a-hole']", namespace)
    assert metadata is not None and json.loads(metadata.text or "") == drawing
    assert geometry is not None and geometry.attrib["transform"] == "translate(0 200) scale(1 -1)"
    assert root.attrib["data-physical-face"] == "BOTTOM"
    assert root.attrib["data-material-id"] == "mdf"
    assert root.attrib["data-material-version"] == "v1"
    assert root.attrib["data-grain-direction"] == "NONE"
    assert root.attrib["data-u-axis"] == "x"
    assert root.attrib["data-v-axis"] == "y"
    assert root.attrib["data-thickness-axis"] == "z"
    assert len(circles) == 1
    assert circles[0].attrib["data-depth-mm"] == "10"
    assert circles[0].attrib["data-tolerance-mm"] == "0.05"
    assert circles[0].attrib["data-tolerance-status"] == "DECLARED_IN_DESIGN"
    assert circles[0].attrib["data-fit-clearance-mm"] == "0.1"


def test_unspecified_outer_contour_tolerance_is_explicit_and_never_numeric_zero() -> None:
    panel, _, _, _ = manufacturing_values()
    outline = ManufacturingFeature(
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
    )
    panel = replace(panel, features=(*panel.features, outline))

    for side, source in (
        (Side.A, "SEMANTIC_OUTER_CONTOUR"),
        (Side.B, "FINISHED_PART_RECTANGULAR_FALLBACK"),
    ):
        dxf = dxf_for_part(panel, side).decode("utf-8")
        drawing = _dxf_json_comment_payload(dxf, "CUSTOMBUILD_DRAWING_JSON")
        finished_outline = drawing["finished_outline"]
        assert finished_outline["source"] == source
        assert finished_outline["tolerance_mm"] is None
        assert finished_outline["tolerance_status"] == "EXTERNAL_TOLERANCE_REQUIRED"
        if side is Side.A:
            assert finished_outline["feature"]["feature_id"] == "outline:panel"
        else:
            assert finished_outline["feature"] is None

        root = ElementTree.fromstring(  # noqa: S314 -- trusted locally generated SVG
            svg_for_part(panel, side)
        )
        svg_outline = next(
            (
                element
                for element in root.iter()
                if element.attrib.get("class") == "outline"
            ),
            None,
        )
        assert svg_outline is not None
        assert svg_outline.attrib["data-outline-source"] == source
        assert svg_outline.attrib["data-tolerance-status"] == (
            "EXTERNAL_TOLERANCE_REQUIRED"
        )
        assert "data-tolerance-mm" not in svg_outline.attrib


@pytest.mark.parametrize("exporter", (dxf_for_part, svg_for_part))
@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda feature: replace(feature, side=Side.EDGE), "edge-machining features"),
        (lambda feature: replace(feature, kind=FeatureKind.COUNTERSINK), "no versioned angle"),
        (lambda feature: replace(feature, diameter_um=None), "has no diameter"),
        (lambda feature: replace(feature, through=True), "does not reach the full"),
        (lambda feature: replace(feature, depth_um=18_000), "reaches or exceeds"),
    ),
)
def test_part_drawings_fail_closed_instead_of_omitting_or_guessing_geometry(
    exporter,
    mutate,
    message: str,
) -> None:
    panel, _, _, _ = manufacturing_values()
    panel = replace(panel, features=(mutate(panel.features[0]), panel.features[1]))

    with pytest.raises(ValueError, match=message):
        exporter(panel, Side.A)


@pytest.mark.parametrize("exporter", (dxf_for_part, svg_for_part))
def test_part_drawings_reject_a_misstated_outer_perimeter(exporter) -> None:
    panel, _, _, _ = manufacturing_values()
    outline = ManufacturingFeature(
        "outline:wrong",
        panel.part_id,
        FeatureKind.OUTER_CONTOUR,
        Side.A,
        10_000,
        0,
        panel.thickness_um,
        width_um=panel.width_um - 10_000,
        length_um=panel.height_um,
        through=True,
    )
    panel = replace(panel, features=(*panel.features, outline))

    with pytest.raises(ValueError, match="does not exactly match"):
        exporter(panel, Side.A)


def test_dxf_extents_include_declared_dogbone_cutter_overhang() -> None:
    panel, _, _, _ = manufacturing_values()
    open_groove = ManufacturingFeature(
        "edge-groove",
        panel.part_id,
        FeatureKind.GROOVE,
        Side.A,
        0,
        50_000,
        6_000,
        width_um=20_000,
        length_um=80_000,
        corner_strategy="dogbone-v1",
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_min",),
    )
    panel = replace(panel, features=(open_groove, panel.features[1]))

    dxf = dxf_for_part(panel, Side.A).decode("utf-8")
    document = ezdxf.read(io.StringIO(dxf))
    drawing = _dxf_json_comment_payload(dxf, "CUSTOMBUILD_DRAWING_JSON")

    assert tuple(document.header["$EXTMIN"]) == pytest.approx((-3.0, 0.0, 0.0))
    assert tuple(document.header["$EXTMAX"]) == pytest.approx((300.0, 200.0, 0.0))
    assert drawing["emitted_geometry_extents_mm"] == {
        "u_min": "-3",
        "v_min": "0",
        "u_max": "300",
        "v_max": "200",
    }


def test_dogbone_v2_omits_open_slot_mouth_scallops_but_v1_remains_stable() -> None:
    panel, _, _, _ = manufacturing_values()
    legacy = ManufacturingFeature(
        "legacy-edge-groove",
        panel.part_id,
        FeatureKind.GROOVE,
        Side.A,
        0,
        50_000,
        6_000,
        width_um=20_000,
        length_um=80_000,
        corner_strategy="dogbone-v1",
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_min",),
    )
    current = replace(
        legacy,
        feature_id="current-edge-groove",
        corner_strategy="dogbone-v2",
    )
    assert len(legacy.relief_circles()) == 4
    assert len(current.relief_circles()) == 2

    current_panel = replace(panel, features=(current, panel.features[1]))
    dxf = dxf_for_part(current_panel, Side.A).decode("utf-8")
    document = ezdxf.read(io.StringIO(dxf))
    drawing = _dxf_json_comment_payload(dxf, "CUSTOMBUILD_DRAWING_JSON")
    svg = svg_for_part(current_panel, Side.A).decode("utf-8")

    assert len(document.modelspace().query('CIRCLE[layer=="GROOVE"]')) == 2
    assert tuple(document.header["$EXTMIN"]) == pytest.approx((0.0, 0.0, 0.0))
    assert drawing["emitted_geometry_extents_mm"]["u_min"] == "0"
    assert svg.count('class="groove corner-relief"') == 2
    assert 'data-corner-strategy="dogbone-v2"' in svg


def test_bom_exposes_edge_band_thickness_and_unresolved_procurement() -> None:
    panel, _, _, _ = manufacturing_values()
    detail = EdgeBandSpec(
        edge="U_MIN",
        thickness_um=1_000,
        source_face="FRONT",
    )
    panel = replace(
        panel,
        edge_bands=(detail.edge,),
        edge_band_details=(detail,),
    )

    row = next(csv.DictReader(io.StringIO(bom_csv((panel,)).decode("utf-8"))))

    assert row["edge_bands"] == "U_MIN"
    assert row["edge_band_thicknesses_mm"] == "1"
    assert row["edge_band_source_faces"] == "FRONT"
    assert row["edge_band_catalog_refs"] == "UNRESOLVED"
    assert row["edge_band_attachment_methods"] == "UNRESOLVED"
    assert row["edge_band_procurement_statuses"] == "EXTERNAL_SELECTION_REQUIRED"


def test_edge_protection_rejects_adhesive_attachment() -> None:
    with pytest.raises(ValueError, match="dry mechanical attachment"):
        EdgeBandSpec(
            edge="U_MIN",
            thickness_um=1_000,
            source_face="FRONT",
            attachment_method="ADHESIVE",
        )
    with pytest.raises(TypeError, match="adhesive_id"):
        EdgeBandSpec(
            **{
                "edge": "U_MIN",
                "thickness_um": 1_000,
                "source_face": "FRONT",
                "adhesive_id": "hot-melt",
            }
        )


def test_catalog_identified_edge_protection_must_be_mechanically_retained() -> None:
    with pytest.raises(ValueError, match="must declare dry mechanical attachment"):
        EdgeBandSpec(
            edge="U_MIN",
            thickness_um=1_000,
            source_face="FRONT",
            catalog_id="edge-profile-a",
            catalog_version="1.0.0",
        )

    detail = EdgeBandSpec(
        edge="U_MIN",
        thickness_um=1_000,
        source_face="FRONT",
        catalog_id="edge-profile-a",
        catalog_version="1.0.0",
        attachment_method="MECHANICAL",
    )

    assert detail.procurement_status == "CATALOG_IDENTIFIED"


@pytest.mark.parametrize(
    ("catalog_id", "catalog_version"),
    (
        ("", "1.0.0"),
        ("edge-profile-a", ""),
        (" edge-profile-a", "1.0.0"),
        ("edge-profile-a", "1.0.0 "),
        ("edge@profile", "1.0.0"),
    ),
)
def test_edge_protection_rejects_noncanonical_catalog_identity(
    catalog_id: str,
    catalog_version: str,
) -> None:
    with pytest.raises(ValueError, match="canonical non-blank identity"):
        EdgeBandSpec(
            edge="U_MIN",
            thickness_um=1_000,
            source_face="FRONT",
            catalog_id=catalog_id,
            catalog_version=catalog_version,
            attachment_method="MECHANICAL",
        )


def test_mechanical_edge_protection_requires_a_versioned_catalog_identity() -> None:
    with pytest.raises(ValueError, match="requires a canonical catalog ID and version"):
        EdgeBandSpec(
            edge="U_MIN",
            thickness_um=1_000,
            source_face="FRONT",
            attachment_method="MECHANICAL",
        )


def test_part_rejects_edge_summary_without_exact_details_and_vice_versa() -> None:
    panel, _, _, _ = manufacturing_values()
    detail = EdgeBandSpec(
        edge="U_MIN",
        thickness_um=1_000,
        source_face="FRONT",
    )

    with pytest.raises(ValueError, match="must match exactly"):
        replace(panel, edge_bands=(detail.edge,), edge_band_details=())
    with pytest.raises(ValueError, match="must match exactly"):
        replace(panel, edge_bands=(), edge_band_details=(detail,))


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

    dxf_outline_entity = "0\nLWPOLYLINE\n100\nAcDbEntity\n8\nOUTLINE\n"
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
    artifacts = legacy_full_cam_artifacts(
        panel=panel,
        layout=layout,
        operations=operations,
    )
    by_path = {artifact.path: artifact for artifact in artifacts}

    first = build_deterministic_zip(context, artifacts)
    second = build_deterministic_zip(context, tuple(reversed(artifacts)))
    manifest = _raw_builder_manifest(first)

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


def test_manifest_hash_binds_the_exact_runtime_build_identity() -> None:
    panel, layout, operations, context = manufacturing_values()
    artifacts = legacy_full_cam_artifacts(
        panel=panel,
        layout=layout,
        operations=operations,
    )
    identity = {
        "schema_version": "custombuild.production-engine-context.v5",
        "app_version": "1.4.0",
        "vcs_ref": "a" * 40,
        "build_date": "2026-08-11T12:00:00+02:00",
        "source_url": "https://github.com/pilotens/Custombuild",
        "source_manifest_sha256": "d" * 64,
        "dependency_lock_sha256": "b" * 64,
        "template_capability_registry_version": "test-registry-1.0.0",
    }
    changed_identity = {**identity, "source_manifest_sha256": "c" * 64}

    first = _raw_builder_manifest(
        build_deterministic_zip(
            replace(context, production_engine_context=identity),
            artifacts,
        )
    )
    changed = _raw_builder_manifest(
        build_deterministic_zip(
            replace(context, production_engine_context=changed_identity),
            artifacts,
        )
    )

    assert first["production_engine_context"] == identity
    assert changed["production_engine_context"] == changed_identity
    assert first["production_context_hash"] != changed["production_context_hash"]


def test_manifest_binds_reference_asset_identity_for_offline_verification() -> None:
    panel, layout, operations, context = manufacturing_values()
    provenance = {
        "source": "reference_image",
        "import_id": "11111111-1111-4111-8111-111111111111",
        "image_sha256": "a" * 64,
        "verified_model_fingerprint": "d" * 64,
    }
    context = replace(context, source_provenance=provenance)
    artifacts = legacy_full_cam_artifacts(
        panel=panel,
        layout=layout,
        operations=operations,
    )

    payload = build_deterministic_zip(context, artifacts)
    manifest = _raw_builder_manifest(payload)

    assert manifest["source_provenance"] == provenance
    assert manifest["source_provenance"]["image_sha256"] == "a" * 64


def test_manifest_rejects_unverifiable_reference_provenance() -> None:
    _, _, _, context = manufacturing_values()

    with pytest.raises(ValueError, match="image_sha256"):
        replace(
            context,
            source_provenance={
                "source": "reference_image",
                "import_id": "11111111-1111-4111-8111-111111111111",
                "image_sha256": "not-a-digest",
                "verified_model_fingerprint": "d" * 64,
            },
        )


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
