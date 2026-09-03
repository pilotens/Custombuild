from __future__ import annotations

import csv
import io
import json

import pytest
from custombuild_manufacturing import (
    DeterministicNester,
    FeatureKind,
    ManufacturingFeature,
    PartSpec,
    Side,
    StockSheet,
    generate_operations_document,
    label_index_csv,
    linuxcnc_reference_router_1325,
    manufacturing_intent_json,
    quality_measurement_plan_json,
    supplier_handoff_json,
)
from custombuild_manufacturing.package import default_artifacts
from custombuild_manufacturing.quality import SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS


def _quality_values():
    part = PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        quantity=2,
        features=(
            ManufacturingFeature(
                "hole",
                "panel",
                FeatureKind.DRILL,
                Side.A,
                50_000,
                50_000,
                10_000,
                diameter_um=8_000,
                tolerance_um=150,
            ),
        ),
    )
    stock = StockSheet("sheet", "mdf", "v1", 1_000_000, 600_000, 18_000)
    layout = DeterministicNester().nest((part,), stock)
    operations = generate_operations_document(
        design_hash="d" * 64,
        parts=(part,),
        layout=layout,
        machine=linuxcnc_reference_router_1325(),
    )
    return part, layout, operations


def test_label_index_covers_every_placement_once_with_non_authorizing_qr() -> None:
    part, layout, operations = _quality_values()

    first = label_index_csv(parts=(part,), layouts=layout, operations=operations)
    second = label_index_csv(parts=(part,), layouts=(layout,), operations=operations)
    rows = list(csv.DictReader(io.StringIO(first.decode("utf-8"))))

    assert first == second
    assert {row["instance_id"] for row in rows} == {
        placement.instance_id for placement in layout.placements
    }
    assert len(rows) == len(layout.placements)
    assert len({row["qr_payload"] for row in rows}) == len(rows)
    assert all(row["qr_payload"].startswith(f"custombuild:part:{'d' * 64}:") for row in rows)
    assert all(row["physical_release_authorized"] == "false" for row in rows)


def test_measurement_plan_covers_every_part_dimension_and_operation_without_pass() -> None:
    part, layout, operations = _quality_values()

    payload = json.loads(
        quality_measurement_plan_json(
            parts=(part,),
            layouts=layout,
            operations=operations,
        )
    )

    instance_ids = {placement.instance_id for placement in layout.placements}
    assert payload["physical_release_authorized"] is False
    assert payload["approval_state"] == "PENDING_EXTERNAL_MEASUREMENT"
    assert payload["coverage"]["part_instance_count"] == 2
    assert payload["coverage"]["dimension_check_count"] == 6
    assert payload["coverage"]["operation_check_count"] == len(operations.operations)
    assert {item["instance_id"] for item in payload["dimension_checks"]} == instance_ids
    assert {item["operation_id"] for item in payload["operation_checks"]} == {
        operation.operation_id for operation in operations.operations
    }
    assert all(item["measured_um"] is None for item in payload["dimension_checks"])
    assert all(item["result"] is None for item in payload["dimension_checks"])
    assert all(item["measured"] is None for item in payload["operation_checks"])
    assert all(item["result"] is None for item in payload["operation_checks"])
    assert any(
        item["tolerance_um"] == 150
        and item["tolerance_status"] == "DECLARED_IN_DESIGN"
        for item in payload["operation_checks"]
    )
    assert b'"result":"PASS"' not in quality_measurement_plan_json(
        parts=(part,), layouts=layout, operations=operations
    )


def test_machine_neutral_intent_freezes_every_feature_without_authorizing_motion() -> None:
    part, _, _ = _quality_values()

    first = manufacturing_intent_json(
        parts=(part,), project_id="project-123", revision="revision-7", design_hash="d" * 64
    )
    second = manufacturing_intent_json(
        parts=(part,), project_id="project-123", revision="revision-7", design_hash="d" * 64
    )
    payload = json.loads(first)
    feature = payload["parts"][0]["features"][0]

    assert first == second
    assert payload["schema_version"] == "custombuild.manufacturing-intent.v1"
    assert payload["document_purpose"] == "MACHINE_NEUTRAL_DESIGN_INTENT"
    assert payload["document_identity"]["project_id"] == "project-123"
    assert payload["document_identity"]["revision"] == "revision-7"
    assert payload["document_identity"]["design_hash"] == "d" * 64
    assert len(payload["document_identity"]["parts_sha256"]) == 64
    assert payload["physical_cutting_authorized"] is False
    assert payload["supplier_boundary"] == {
        "executable_toolpaths_included": False,
        "machine_coordinates_included": False,
        "feeds_speeds_authorized": False,
        "fixture_wcs_authorized": False,
        "required_action": (
            "Import and verify this intent, resolve every external decision, and generate "
            "shop-approved toolpaths in the supplier's controlled CAM workflow."
        ),
    }
    assert payload["parts"][0]["finished_dimensions_um"] == {
        "u": 300_000,
        "v": 200_000,
        "thickness": 18_000,
    }
    assert feature["feature_id"] == "hole"
    assert feature["kind"] == "DRILL"
    assert feature["side"] == "A"
    assert feature["x_um"] == 50_000
    assert feature["y_um"] == 50_000
    assert feature["depth_um"] == 10_000
    assert feature["diameter_um"] == 8_000
    assert feature["tolerance_um"] == 150
    assert feature["tolerance_status"] == "DECLARED_IN_DESIGN"
    assert feature["pattern_points_um"] == [{"x_um": 50_000, "y_um": 50_000}]


def test_supplier_handoff_binds_assumptions_and_requires_explicit_shop_answers() -> None:
    _, layout, operations = _quality_values()
    machine = linuxcnc_reference_router_1325()

    payload = json.loads(
        supplier_handoff_json(
            project_id="project-123",
            revision="revision-7",
            design_hash="d" * 64,
            machine=machine,
            stocks=(layout.stock,),
            operations=operations,
            cam_status="VALIDATION_GENERATED",
            blocker_codes=(),
            cam_required_action=(
                "None for design review; physical workshop evidence remains required."
            ),
            design_review_ready=True,
            manifest_context_projection={
                field: f"context:{field}"
                for field in SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS
            },
            payload_inventory_entries=(
                {
                    "path": "model/design.step",
                    "media_type": "model/step",
                    "role": "AUTHORITATIVE_STEP",
                    "size_bytes": 123,
                    "sha256": "a" * 64,
                },
            ),
            known_unresolved_decision_codes=(),
        )
    )

    assert payload["package_identity"] == {
        "project_id": "project-123",
        "revision": "revision-7",
        "design_hash": "d" * 64,
    }
    assert payload["package_contract"]["authoritative_inventory"] == (
        "manifest.json.artifacts"
    )
    assert payload["supplier_stages"] == {
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
    inventory = payload["payload_inventory_binding"]
    assert inventory["artifact_count"] == 1
    assert inventory["artifacts"][0]["path"] == "model/design.step"
    assert len(inventory["payload_inventory_sha256"]) == 64
    assert inventory["excluded_paths"] == ["manifest.json", "shop/supplier-handoff.json"]
    manifest_context = payload["manifest_context_binding"]
    assert manifest_context["field_names"] == list(
        SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS
    )
    assert manifest_context["context"]["app_version"] == "context:app_version"
    assert len(manifest_context["manifest_context_sha256"]) == 64
    assert payload["known_unresolved_decisions"] == []
    assert payload["readiness"]["package_review_availability"] == (
        "AVAILABLE_FOR_BOUNDED_DESIGN_REVIEW"
    )
    assert payload["readiness"]["complete_validation_evidence_ready"] is True
    assert "NON_CUTTING_CONTROLLER_VALIDATION" in payload["readiness"][
        "complete_validation_evidence_scope"
    ]
    assert "design_review_ready" not in payload["readiness"]
    assert payload["operation_binding"]["status"] == (
        "MACHINE_NEUTRAL_VALIDATION_ONLY"
    )
    assert payload["operation_binding"]["document_path"] == "cam/operations.json"
    assert payload["operation_binding"]["setups"]
    assert payload["operation_binding"]["selected_tools"]
    assert payload["selected_validation_machine_profile"]["profile"]["profile_id"] == (
        machine.profile_id
    )
    assert payload["stock_assumptions"]["profiles"][0]["stock_id"] == "sheet"
    questions = payload["shop_acceptance_questions"]
    assert [item["question_id"] for item in questions] == [
        f"Q{index:02d}_{suffix}"
        for index, suffix in enumerate(
            (
                "IMPORT_AND_UNITS",
                "MATERIAL_AND_STOCK",
                "MACHINE_AND_TRAVEL",
                "FIXTURE_WCS_AND_KEEP_OUT",
                "TOOLS_AND_CUTTING_DATA",
                "TOLERANCE_AND_FIT",
                "EXECUTABLE_CAM",
                "FIRST_ARTICLE_AND_RELEASE",
                "CONSTRUCTION_DECISIONS",
                "ADJACENT_RELIEF_AND_MATERIAL_WEB",
            ),
            start=1,
        )
    ]
    assert all(item["status"] == "UNANSWERED" for item in questions)
    assert all(item["answer"] is None for item in questions)
    relief_question = questions[-1]
    assert "actual cutter diameter and runout" in relief_question["question"]
    assert "zero or tolerance-consumed clearance" in relief_question["required_evidence"]


def test_label_index_rejects_duplicate_instance_placement() -> None:
    part, layout, operations = _quality_values()
    duplicate_layout = type(layout)(
        layout.stock,
        (*layout.placements, layout.placements[0]),
        layout.unplaced_instance_ids,
        layout.used_sheet_count,
        layout.utilization_ppm,
        layout.algorithm,
    )

    with pytest.raises(ValueError, match="placed more than once"):
        label_index_csv(parts=(part,), layouts=duplicate_layout, operations=operations)


def test_default_package_includes_traceability_quality_and_procurement_documents() -> None:
    part, layout, operations = _quality_values()

    artifacts = {
        artifact.path: artifact
        for artifact in default_artifacts(
            parts=(part,),
            layout=layout,
            operations=operations,
            project_id="project-123",
            revision="revision-7",
            design_hash="d" * 64,
        )
    }

    assert artifacts["labels/label-index.csv"].role == "LABEL_INDEX"
    assert artifacts["quality/measurement-plan.json"].role == "QUALITY_MEASUREMENT_PLAN"
    assert artifacts["bom/grouped-bom.json"].role == "GROUPED_BOM"
    assert artifacts["materials/stock-purchase.csv"].role == "STOCK_PURCHASE_SCHEDULE"
    assert artifacts["manufacturing/manufacturing-intent.json"].role == (
        "MACHINE_NEUTRAL_MANUFACTURING_INTENT"
    )
