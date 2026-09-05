"""Audit the complete export chain for a genuinely custom shelving design.

This deliberately enters through the API's strict request model and canonical
design service.  It must not be replaced with a hand-built domain fixture: the
regression exists to catch defaults or custom-layout fields being lost at any
service boundary before the supplier package is produced.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import pytest
from app.design_service import bind_joint_retention, canonical_preview
from app.schemas import BookcasePreviewInput
from custombuild_cad import CADArtifacts, CadQueryAdapter
from custombuild_domain import (
    JointRetentionContract,
    JointRetentionLoadCase,
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMaterialIdentity,
    JointRetentionMethod,
    PartRole,
    dado_joint_geometry_fingerprint,
    mm,
)
from custombuild_manufacturing import (
    ADJACENT_RELIEF_CLEARANCE_WARNING_CODE,
    JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
    JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
    JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
    MANUFACTURING_INTENT_SCHEMA_VERSION,
    SUPPLIER_HANDOFF_PATH,
    SUPPLIER_HANDOFF_SCHEMA_VERSION,
    ArtifactFile,
    ManifestContext,
    Point2D,
    Side,
    StockSheet,
    TwoSidedRegistration,
    build_production_bundle,
    canonical_json_bytes,
    linuxcnc_reference_router_1325,
    read_and_verify_package,
    sha256_hex,
)
from custombuild_manufacturing.adapters import adapt_design_result
from custombuild_manufacturing.quality import (
    OPERATIONS_JSON_SCHEMA_PATH,
    operations_json_schema,
)
from ezdxf.filemanagement import read as read_dxf

PROJECT_ID = "audit-custom-shelving"
REVISION = 7
_TEST_ONLY_SIGNED_RETENTION_EVIDENCE_BYTES = canonical_json_bytes(
    {
        "evidence_id": "test-only.signed-evidence",
        "fixture_scope": "integration-test-only",
    }
)


def _custom_request() -> BookcasePreviewInput:
    """Return a valid request whose dimensions and layout are all non-default."""

    return BookcasePreviewInput.model_validate(
        {
            "width_mm": 1_437,
            "height_mm": 2_187,
            "depth_mm": 347,
            "furniture_type": "bookcase",
            "material_id": "mdf",
            "back_material_id": "mdf-6",
            "nominal_thickness_mm": 18,
            # 17.6 mm is inside the production catalogue's declared 17--19 mm
            # range and must remain manufacturable for a multi-bay inset back.
            "measured_thickness_mm": 17.6,
            "shelf_count": 4,
            "shelf_mount": "fixed",
            "load_per_shelf_kg": 20,
            "back_panel": "inset_groove",
            "plinth": True,
            "plinth_height_mm": 93,
            "divider_count": 2,
            "bay_width_ratios": [0.21, 0.47, 0.32],
            "shelf_height_ratios": [0.14, 0.37, 0.63, 0.86],
            "edge_band_mm": 0,
            "joint_system": "dado",
            "reinforcement_mode": "manual",
        }
    )


def _test_only_retention_contract(
    spec: Any,
    design: Any,
    *,
    evidence_bytes: bytes,
) -> JointRetentionContract:
    """Create structurally complete *test-only* input for the server binder.

    The synthetic hashes are not presented as supplier evidence.  This unit
    boundary exercises the already-authenticated input accepted by
    ``bind_joint_retention`` so the test can reach every downstream export.
    Authentication/revocation of real catalogue evidence is covered by the
    retention trust-boundary tests.
    """

    return JointRetentionContract(
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
                material_id=spec.material.material_id,
                material_version=spec.material.version,
            ),
        ),
        joint_geometry_sha256=dado_joint_geometry_fingerprint(
            design.parts,
            design.joints,
        ),
        minimum_applicable_thickness_um=mm(17),
        maximum_applicable_thickness_um=mm(19),
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


def _stock_and_registration(
    spec: Any,
) -> tuple[tuple[StockSheet, ...], dict[str, dict[int, TwoSidedRegistration]]]:
    assert spec.back_material is not None
    stocks = (
        StockSheet(
            "custom-mdf-18",
            spec.material.material_id,
            spec.material.version,
            2_440_000,
            1_220_000,
            spec.parameters.actual_thickness_um,
            quantity=12,
            grain_direction="X",
        ),
        StockSheet(
            "custom-mdf-6",
            spec.back_material.material_id,
            spec.back_material.version,
            2_440_000,
            1_220_000,
            spec.parameters.back_thickness_um,
            quantity=5,
            grain_direction="X",
        ),
    )
    registrations = {
        stock.stock_id: {
            sheet_index: TwoSidedRegistration(
                declaration_authority="CLIENT_DECLARED",
                method_id=f"audit:{stock.stock_id}:{sheet_index}",
                fixture_method_version="fixture-v1",
                pin_diameter_um=6_000,
                position_tolerance_um=500,
                points=(
                    Point2D(50_000, 50_000),
                    Point2D(stock.width_um - 50_000, 50_000),
                ),
            )
            for sheet_index in range(stock.quantity)
        }
        for stock in stocks
    }
    return stocks, registrations


def _manifest_context(design: Any, machine: Any) -> ManifestContext:
    return ManifestContext(
        project_id=PROJECT_ID,
        revision=str(REVISION),
        design_hash=design.design_hash,
        app_version="0.1.0",
        engine_version="derived-by-pipeline",
        template_version="derived-by-pipeline",
        template_id="shelving",
        template_capability_fingerprint="c" * 64,
        template_capability={
            "template_id": "shelving",
            "template_version": design.template_version,
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


def _csv_rows(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def _dxf_json_comment_payload(source: str, label: str) -> dict[str, Any]:
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


def _feature_projection(features: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {
            "feature_id": feature.feature_id,
            "kind": feature.kind.value,
            "side": feature.side.value,
            "x_um": feature.x_um,
            "y_um": feature.y_um,
            "depth_um": feature.depth_um,
            "diameter_um": feature.diameter_um,
            "width_um": feature.width_um,
            "length_um": feature.length_um,
        }
        for feature in sorted(features, key=lambda item: item.feature_id)
    ]


@pytest.mark.integration
@pytest.mark.cad
def test_non_default_custom_shelving_flows_to_complete_supplier_package() -> None:
    request = _custom_request()
    defaults = BookcasePreviewInput()
    assert (
        request.width_mm,
        request.height_mm,
        request.depth_mm,
        request.measured_thickness_mm,
        request.shelf_count,
        request.divider_count,
    ) != (
        defaults.width_mm,
        defaults.height_mm,
        defaults.depth_mm,
        defaults.measured_thickness_mm,
        defaults.shelf_count,
        defaults.divider_count,
    )
    assert request.bay_width_ratios != defaults.bay_width_ratios
    assert request.shelf_height_ratios != defaults.shelf_height_ratios

    spec, unbound_design, _ = canonical_preview(
        request.model_dump(exclude_none=True),
        design_id=PROJECT_ID,
        revision=REVISION,
    )
    assert spec.parameters.width_um == 1_437_000
    assert spec.parameters.height_um == 2_187_000
    assert spec.parameters.depth_um == 347_000
    assert spec.parameters.actual_thickness_um == 17_600
    assert spec.parameters.bay_width_ratios_ppm == (210_000, 470_000, 320_000)
    assert spec.parameters.shelf_height_ratios_ppm == (140_000, 370_000, 630_000, 860_000)

    evidence_bytes = _TEST_ONLY_SIGNED_RETENTION_EVIDENCE_BYTES
    retention = _test_only_retention_contract(
        spec,
        unbound_design,
        evidence_bytes=evidence_bytes,
    )
    bound_spec, design, _ = bind_joint_retention(spec, retention)
    assert unbound_design.parts == design.parts
    assert bound_spec.joint_retention == retention

    shelves = sorted(
        (part for part in design.parts if part.role == PartRole.SHELF),
        key=lambda part: part.instance_index,
    )
    dividers = sorted(
        (part for part in design.parts if part.role == PartRole.DIVIDER),
        key=lambda part: part.instance_index,
    )
    backs = sorted(
        (part for part in design.parts if part.role == PartRole.BACK),
        key=lambda part: part.instance_index,
    )
    assert len(shelves) == 12
    assert len(dividers) == 2
    assert len(backs) == 3
    assert sorted({part.finished_size.width_um for part in shelves}) == [
        298_718,
        449_044,
        654_034,
    ]
    assert sorted({part.placement.z_um for part in shelves}) == [
        390_032,
        863_556,
        1_398_844,
        1_872_368,
    ]
    assert [part.placement.x_um for part in dividers] == [304_586, 964_488]
    assert [part.placement.x_um for part in backs] == [11_734, 316_436, 976_338]
    assert [part.finished_size.width_um for part in backs] == [298_602, 653_802, 448_928]
    assert [
        backs[index + 1].placement.x_um
        - (backs[index].placement.x_um + backs[index].finished_size.width_um)
        for index in range(len(backs) - 1)
    ] == [6_100, 6_100]

    # Outer side and vertical capture remains the structural t//3 depth. Only
    # divider-facing capture is reduced by 116 µm per edge so the existing R3 /
    # Ø6 mm cutter leaves 0.1 mm nominal relief-envelope clearance, consumed at
    # both 0.05 mm worst-case feature limits. Machine/runout and physical web
    # acceptance remain explicitly outside this digital review package.
    back_ids = {part.part_id for part in backs}
    parts_by_id = {part.part_id: part for part in design.parts}
    features_by_id = {
        feature.feature_id: feature for part in design.parts for feature in part.features
    }
    back_joint_cut_depths = []
    for joint in design.joints:
        if not (back_ids & {member.part_id for member in joint.members}):
            continue
        cut_member = next(member for member in joint.members if member.feature_ids)
        owner = parts_by_id[cut_member.part_id]
        feature = features_by_id[cut_member.feature_ids[0]]
        back_joint_cut_depths.append((owner.role, feature.dimensions.depth_um))
    assert sorted(
        depth for role, depth in back_joint_cut_depths if role == PartRole.DIVIDER
    ) == [5_750] * 4
    assert sorted(
        depth for role, depth in back_joint_cut_depths if role != PartRole.DIVIDER
    ) == [5_866] * 8

    adapted = adapt_design_result(design)
    machine = linuxcnc_reference_router_1325()
    stocks, registrations = _stock_and_registration(bound_spec)
    bundle = build_production_bundle(
        design,
        stock=stocks,
        machine=machine,
        context=_manifest_context(design, machine),
        include_step=True,
        include_validation_program=False,
        two_sided_registration_by_stock=registrations,
        additional_artifacts=(
            ArtifactFile(
                JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
                evidence_bytes,
                JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
                JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
            ),
        ),
    )

    assert bundle.operations is not None
    assert bundle.layouts
    assert not bundle.dfm_report.blocking_issues
    assert bundle.manifest["design_hash"] == design.design_hash
    assert bundle.manifest["release_scope"] == "design_review"
    assert bundle.manifest["machine_use"] == "validation_only"
    assert bundle.manifest["physical_cutting_authorized"] is False
    assert read_and_verify_package(bundle.zip_bytes) == bundle.manifest

    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        payloads = {
            name: archive.read(name) for name in archive.namelist() if name != "manifest.json"
        }
    assert payloads[JOINT_RETENTION_SIGNED_EVIDENCE_PATH] == evidence_bytes

    frozen_spec = json.loads(payloads["design/design-spec.json"])
    assert frozen_spec["spec"] == bound_spec.model_dump(mode="json")
    assert frozen_spec["spec"]["parameters"]["bay_width_ratios_ppm"] == [
        210_000,
        470_000,
        320_000,
    ]
    assert frozen_spec["spec"]["parameters"]["shelf_height_ratios_ppm"] == [
        140_000,
        370_000,
        630_000,
        860_000,
    ]

    adapted_by_id = {part.part_id: part for part in adapted.parts}
    bom = _csv_rows(payloads["bom/bom.csv"])
    cut_list = _csv_rows(payloads["cut-list/cut-list.csv"])
    material_list = _csv_rows(payloads["materials/material-list.csv"])
    assert {row["part_id"] for row in bom} == set(adapted_by_id)
    assert {row["part_id"] for row in cut_list} == set(adapted_by_id)
    assert len(cut_list) == sum(part.quantity for part in adapted.parts)
    for row in bom:
        part = adapted_by_id[row["part_id"]]
        assert Decimal(row["finished_width_mm"]) * 1_000 == part.width_um
        assert Decimal(row["finished_height_mm"]) * 1_000 == part.height_um
        assert Decimal(row["thickness_mm"]) * 1_000 == part.thickness_um
        assert row["material_id"] == part.material_id
        assert row["material_version"] == part.material_version
    assert sum(int(row["part_instances"]) for row in material_list) == len(cut_list)
    assert {row["thickness_mm"] for row in material_list} == {"6", "17.6"}

    grouped_bom = json.loads(payloads["bom/grouped-bom.json"])
    assert grouped_bom["part_instance_count"] == len(cut_list)
    assert grouped_bom["physical_release_authorized"] is False
    assert {part_id for group in grouped_bom["groups"] for part_id in group["part_ids"]} == set(
        adapted_by_id
    )

    intent = json.loads(payloads["manufacturing/manufacturing-intent.json"])
    assert intent["schema_version"] == MANUFACTURING_INTENT_SCHEMA_VERSION
    assert intent["document_identity"] == {
        "project_id": PROJECT_ID,
        "revision": str(REVISION),
        "design_hash": design.design_hash,
        "parts_sha256": sha256_hex(
            canonical_json_bytes(tuple(sorted(adapted.parts, key=lambda part: part.part_id)))
        ),
    }
    assert intent["document_purpose"] == "MACHINE_NEUTRAL_DESIGN_INTENT"
    assert intent["physical_cutting_authorized"] is False
    assert intent["supplier_boundary"]["executable_toolpaths_included"] is False
    intent_by_id = {part["part_id"]: part for part in intent["parts"]}
    assert set(intent_by_id) == set(adapted_by_id)
    expected_physical_faces = {
        "LEFT_SIDE": ("LEFT", "RIGHT"),
        "RIGHT_SIDE": ("LEFT", "RIGHT"),
        "DIVIDER": ("LEFT", "RIGHT"),
        "BASE_SIDE": ("LEFT", "RIGHT"),
        "TOP": ("BOTTOM", "TOP"),
        "BOTTOM": ("BOTTOM", "TOP"),
        "SHELF": ("BOTTOM", "TOP"),
        "BASE_BOTTOM": ("BOTTOM", "TOP"),
        "BASE_TOP": ("BOTTOM", "TOP"),
        "BACK": ("FRONT", "BACK"),
        "PLINTH": ("FRONT", "BACK"),
        "CABINET_FRONT": ("FRONT", "BACK"),
    }
    for part_id, part in adapted_by_id.items():
        row = intent_by_id[part_id]
        expected_a, expected_b = expected_physical_faces[part.name]
        assert part.metadata["domain_a_side"] == expected_a
        assert part.metadata["domain_b_side"] == expected_b
        assert row["finished_dimensions_um"] == {
            "u": part.width_um,
            "v": part.height_um,
            "thickness": part.thickness_um,
        }
        assert _feature_projection(part.features) == [
            {
                key: feature[key]
                for key in (
                    "feature_id",
                    "kind",
                    "side",
                    "x_um",
                    "y_um",
                    "depth_um",
                    "diameter_um",
                    "width_um",
                    "length_um",
                )
            }
            for feature in row["features"]
        ]

        for side in (Side.A, Side.B):
            dxf_path = f"parts/{part_id}/{side.value}.dxf"
            svg_path = f"drawings/{part_id}/{side.value}.svg"
            assert dxf_path in payloads
            assert svg_path in payloads
            dxf_text = payloads[dxf_path].decode("utf-8")
            svg_text = payloads[svg_path].decode("utf-8")
            expected_face = expected_a if side is Side.A else expected_b
            drawing_metadata = _dxf_json_comment_payload(
                dxf_text,
                "CUSTOMBUILD_DRAWING_JSON",
            )
            assert drawing_metadata["physical_face"] == expected_face
            assert drawing_metadata["datums"]["primary"] == (
                f"{expected_face}_FINISHED_SURFACE"
            )
            assert drawing_metadata["finished_outline"]["tolerance_mm"] is None
            assert drawing_metadata["finished_outline"]["tolerance_status"] == (
                "EXTERNAL_TOLERANCE_REQUIRED"
            )
            assert f'data-physical-face="{expected_face}"' in svg_text
            assert 'data-tolerance-status="EXTERNAL_TOLERANCE_REQUIRED"' in svg_text
            document = read_dxf(io.StringIO(dxf_text))
            audit = document.audit()
            assert document.units == 4  # millimetres in the DXF standard
            assert audit.errors == []
            assert audit.fixes == []
            assert f"CUSTOMBUILD_SIDE:{side.value}" in dxf_text
            side_features = tuple(feature for feature in part.features if feature.side == side)
            for feature in side_features:
                assert f"FEATURE:{feature.feature_id}" in dxf_text
                assert f'data-feature-id="{feature.feature_id}"' in svg_text
            for feature in part.features:
                if feature.side != side:
                    assert f"FEATURE:{feature.feature_id}" not in dxf_text

    assert payloads["model/design.step"].startswith(b"ISO-10303-21")
    assert payloads["model/design.glb"].startswith(b"glTF")
    CadQueryAdapter().validate_design_artifacts(
        design,
        CADArtifacts(
            step=payloads["model/design.step"],
            glb=payloads["model/design.glb"],
            kernel="package-roundtrip",
            adapter_version=CadQueryAdapter.version,
        ),
    )

    assert "cam/operations.json" in payloads
    operations = json.loads(payloads["cam/operations.json"])
    assert operations["design_hash"] == design.design_hash
    assert operations["mode"] == "VALIDATION"
    assert payloads[OPERATIONS_JSON_SCHEMA_PATH] == operations_json_schema()

    handoff = json.loads(payloads[SUPPLIER_HANDOFF_PATH])
    assert handoff["schema_version"] == SUPPLIER_HANDOFF_SCHEMA_VERSION
    assert handoff["package_identity"] == {
        "project_id": PROJECT_ID,
        "revision": str(REVISION),
        "design_hash": design.design_hash,
    }
    assert handoff["package_contract"]["authoritative_inventory"] == ("manifest.json.artifacts")
    assert handoff["package_contract"]["physical_cutting_authorized"] is False
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
    assert handoff["operation_binding"]["document_path"] == "cam/operations.json"
    assert handoff["operation_binding"]["status"] == "MACHINE_NEUTRAL_VALIDATION_ONLY"
    assert handoff["operation_binding"]["document_sha256"] == sha256_hex(
        payloads["cam/operations.json"]
    )
    assert handoff["operation_binding"]["json_schema_path"] == (
        OPERATIONS_JSON_SCHEMA_PATH
    )
    assert handoff["operation_binding"]["json_schema_sha256"] == sha256_hex(
        payloads[OPERATIONS_JSON_SCHEMA_PATH]
    )
    handoff_warnings = handoff["dfm_review_warnings"]
    assert len(handoff_warnings) == 4
    assert {
        row["issue"]["code"] for row in handoff_warnings
    } == {ADJACENT_RELIEF_CLEARANCE_WARNING_CODE}
    assert [
        (row["issue"]["part_id"], row["issue"]["feature_id"])
        for row in handoff_warnings
    ] == sorted(
        (row["issue"]["part_id"], row["issue"]["feature_id"])
        for row in handoff_warnings
    )
    for row in handoff_warnings:
        assert row["source"] == "validation/dfm-report.json"
        assert row["status"] == "UNRESOLVED_SUPPLIER_REVIEW_WARNING"
        assert row["resolved"] is False
        assert row["issue"]["inputs"]["other_feature_id"]
        assert row["issue"]["inputs"]["nominal_relief_clearance_um"] == 100
        assert row["issue"]["inputs"]["combined_feature_tolerance_um"] == 100
        assert row["issue"]["inputs"]["combined_machine_accuracy_allowance_um"] == 200
        assert row["issue"]["inputs"]["combined_tool_runout_um"] == 0
        assert row["issue"]["inputs"]["remaining_conservative_margin_um"] == -200

    inventory = {entry["path"]: entry for entry in bundle.manifest["artifacts"]}
    expected_handoff_inventory = tuple(
        sorted(
            (
                entry
                for entry in bundle.manifest["artifacts"]
                if entry["path"] != SUPPLIER_HANDOFF_PATH
            ),
            key=lambda entry: entry["path"],
        )
    )
    handoff_inventory = handoff["payload_inventory_binding"]
    assert handoff_inventory["excluded_paths"] == [
        "manifest.json",
        SUPPLIER_HANDOFF_PATH,
    ]
    assert handoff_inventory["artifact_count"] == len(expected_handoff_inventory)
    assert handoff_inventory["artifacts"] == list(expected_handoff_inventory)
    assert handoff_inventory["payload_inventory_sha256"] == sha256_hex(
        canonical_json_bytes(expected_handoff_inventory)
    )
    assert set(inventory) == set(payloads)
    for path, payload in payloads.items():
        assert inventory[path]["size_bytes"] == len(payload)
        assert inventory[path]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert inventory["manufacturing/manufacturing-intent.json"]["role"] == (
        "MACHINE_NEUTRAL_MANUFACTURING_INTENT"
    )
    assert inventory[SUPPLIER_HANDOFF_PATH]["role"] == "CNC_SHOP_HANDOFF"
