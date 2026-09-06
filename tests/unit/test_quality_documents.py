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
    sha256_hex,
    supplier_handoff_json,
)
from custombuild_manufacturing.package import default_artifacts
from custombuild_manufacturing.quality import (
    MANUFACTURING_INTENT_JSON_SCHEMA_PATH,
    OPERATIONS_JSON_SCHEMA_PATH,
    START_HERE_PATH,
    SUPPLIER_HANDOFF_JSON_SCHEMA_PATH,
    SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS,
    manufacturing_intent_json_schema,
    operations_json_schema,
    start_here_markdown,
    supplier_handoff_json_schema,
    validate_json_schema_instance,
)


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


def test_start_here_exposes_registration_math_and_unverified_authority_boundary() -> None:
    guide = start_here_markdown().decode("utf-8")

    assert "checksummed but unsigned" in guide
    assert '--expect-bundle-sha256 "<64-char-bundle-sha256>"' in guide
    assert "authenticated out-of-band order record" in guide
    assert "validation/stock-selection.json" in guide
    assert "validation/generation-plan.json" in guide
    assert "CLIENT_DECLARED" in guide
    assert "r = (pin_diameter_um + 1) // 2 + position_tolerance_um" in guide
    assert "Rect(x_um-r, y_um-r, 2*r, 2*r)" in guide
    assert "100000 + 2*r" in guide
    assert "do not authorize cutting" in guide


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


def test_start_here_explains_supplier_boundary_and_all_acceptance_questions() -> None:
    first = start_here_markdown()
    second = start_here_markdown()
    guide = first.decode("utf-8")

    assert first == second
    assert guide.startswith("# START HERE")
    assert "checksummed but unsigned" in guide
    assert 'python3 -I /trusted/verify_production_package.py "<downloaded-package>.zip"' in guide
    assert '--expect-bundle-sha256 "<64-char-bundle-sha256>"' in guide
    assert "no Custombuild installation" in guide
    assert "status` equal to `PASS`" in guide
    assert "details.external_bundle_sha256_match" in guide
    assert "details.bundle_sha256" in guide
    assert "digest source must be authenticated" in guide
    assert "not a publisher signature" in guide
    assert "does not authenticate the publisher" in guide
    assert "does not authorize physical cutting" in guide
    assert "current revocation or expiry status" in guide
    assert "No executable verifier or `__main__.py` is included" in guide
    assert "never execute anything contained in it" in guide
    assert "malicious coordinated internal rewrite" in guide
    assert "authenticate the publisher or evidence issuer" in guide
    assert "expected identity values compare unsigned manifest claims only" in guide
    assert "do not independently reconstruct design semantics" in guide
    assert "no approved executable G-code" in guide
    assert "CUT intent versus REFERENCE material" in guide
    assert "1000 um = 1 mm" in guide
    assert "does **not** define a machine flip" in guide
    assert [guide.count(f"`Q{index:02d}_") for index in range(1, 11)] == [1] * 10


def test_published_manufacturing_intent_schema_rejects_cut_authorization() -> None:
    part, _, _ = _quality_values()
    schema_bytes = manufacturing_intent_json_schema()
    schema = json.loads(schema_bytes)
    intent = json.loads(
        manufacturing_intent_json(
            parts=(part,),
            project_id="project-123",
            revision="revision-7",
            design_hash="d" * 64,
        )
    )

    assert schema_bytes == manufacturing_intent_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:custombuild:schema:manufacturing-intent:v1"
    validate_json_schema_instance(intent, schema)

    intent["physical_cutting_authorized"] = True
    with pytest.raises(ValueError, match="physical_cutting_authorized"):
        validate_json_schema_instance(intent, schema)


def test_published_operations_schema_is_exact_validation_only_contract() -> None:
    _, _, operations = _quality_values()
    schema_bytes = operations_json_schema()
    schema = json.loads(schema_bytes)
    document = json.loads(operations.to_json())

    assert schema_bytes == operations_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:custombuild:schema:operations:v2"
    assert schema["properties"]["schema_version"] == {
        "const": "custombuild.operations.v2"
    }
    assert schema["properties"]["mode"] == {"const": "VALIDATION"}
    validate_json_schema_instance(document, schema)

    document["physical_cutting_authorized"] = True
    with pytest.raises(ValueError, match="unexpected properties"):
        validate_json_schema_instance(document, schema)

    del document["physical_cutting_authorized"]
    document["operations"][0]["stepover_ppm"] = 1_000_001
    with pytest.raises(ValueError, match="integer is above maximum"):
        validate_json_schema_instance(document, schema)


def test_supplier_handoff_binds_assumptions_and_requires_explicit_shop_answers() -> None:
    _, layout, operations = _quality_values()
    machine = linuxcnc_reference_router_1325()
    operations_bytes = operations.to_json()
    operations_schema_bytes = operations_json_schema()

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
                {
                    "path": "cam/operations.json",
                    "media_type": "application/json",
                    "role": "MACHINE_NEUTRAL_OPERATIONS",
                    "size_bytes": len(operations_bytes),
                    "sha256": sha256_hex(operations_bytes),
                },
                {
                    "path": OPERATIONS_JSON_SCHEMA_PATH,
                    "media_type": "application/schema+json",
                    "role": "JSON_SCHEMA",
                    "size_bytes": len(operations_schema_bytes),
                    "sha256": sha256_hex(operations_schema_bytes),
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
    assert payload["schema_version"] == "custombuild.supplier-handoff.v3"
    assert payload["package_contract"]["authoritative_inventory"] == (
        "manifest.json.artifacts"
    )
    assert payload["package_contract"]["signature_status"] == "UNSIGNED"
    assert payload["package_contract"]["publisher_authenticity_provided"] is False
    assert "does not authenticate the publisher" in payload["package_contract"][
        "authenticity_boundary"
    ]
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
    assert inventory["artifact_count"] == 3
    assert {item["path"] for item in inventory["artifacts"]} == {
        "cam/operations.json",
        "model/design.step",
        OPERATIONS_JSON_SCHEMA_PATH,
    }
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
    assert payload["operation_binding"]["document_sha256"] == sha256_hex(
        operations.to_json()
    )
    assert payload["operation_binding"]["json_schema_path"] == (
        OPERATIONS_JSON_SCHEMA_PATH
    )
    assert payload["operation_binding"]["json_schema_sha256"] == sha256_hex(
        operations_json_schema()
    )
    assert payload["operation_binding"]["setups"]
    assert payload["operation_binding"]["selected_tools"]
    assert payload["selected_validation_machine_profile"]["profile"]["profile_id"] == (
        machine.profile_id
    )
    assert payload["stock_assumptions"]["profiles"][0]["stock_id"] == "sheet"
    operations_contract = payload["package_contract"][
        "machine_neutral_operations_contract"
    ]
    assert operations_contract == {
        "document_path": "cam/operations.json",
        "document_schema_version": "custombuild.operations.v2",
        "json_schema_path": OPERATIONS_JSON_SCHEMA_PATH,
        "json_schema_draft": "https://json-schema.org/draft/2020-12/schema",
        "json_schema_sha256": sha256_hex(operations_json_schema()),
        "purpose": "MACHINE_NEUTRAL_VALIDATION_ONLY",
        "executable_cam_provided": False,
        "physical_cutting_authorized": False,
    }
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

    schema_bytes = supplier_handoff_json_schema()
    schema = json.loads(schema_bytes)
    assert schema_bytes == supplier_handoff_json_schema()
    assert schema["$id"] == "urn:custombuild:schema:supplier-handoff:v3"
    validate_json_schema_instance(payload, schema)
    payload["shop_acceptance_questions"][0]["question_id"] = "Q10_WRONG_ORDER"
    with pytest.raises(ValueError, match=r"shop_acceptance_questions\[0\]\.question_id"):
        validate_json_schema_instance(payload, schema)


def test_default_artifacts_publish_guide_and_all_json_schemas() -> None:
    part, layout, operations = _quality_values()

    artifacts = default_artifacts(
        parts=(part,),
        layout=layout,
        operations=operations,
    )
    by_path = {artifact.path: artifact for artifact in artifacts}

    assert by_path[START_HERE_PATH].data == start_here_markdown()
    assert by_path[START_HERE_PATH].media_type == "text/markdown"
    assert (
        by_path[MANUFACTURING_INTENT_JSON_SCHEMA_PATH].data
        == manufacturing_intent_json_schema()
    )
    assert (
        by_path[SUPPLIER_HANDOFF_JSON_SCHEMA_PATH].data
        == supplier_handoff_json_schema()
    )
    assert by_path[OPERATIONS_JSON_SCHEMA_PATH].data == operations_json_schema()
    assert by_path[MANUFACTURING_INTENT_JSON_SCHEMA_PATH].media_type == (
        "application/schema+json"
    )


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
