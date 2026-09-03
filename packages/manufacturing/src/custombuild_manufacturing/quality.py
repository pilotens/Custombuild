"""Deterministic, non-authorizing workshop quality documents."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .model import (
    DFMIssue,
    MachineProfile,
    NestingLayout,
    OperationsDocument,
    PartInstance,
    PartSpec,
    Severity,
    StockSheet,
    canonical_data,
    canonical_json_bytes,
    expand_part_instances,
    sha256_hex,
    um_to_mm,
)
from .review_status import (
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    BLOCKED_CAM_REQUIRED_ACTIONS,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
)

LABEL_INDEX_SCHEMA_VERSION = "custombuild.label-index.v1"
QUALITY_MEASUREMENT_PLAN_SCHEMA_VERSION = "custombuild.quality-measurement-plan.v1"
MANUFACTURING_INTENT_SCHEMA_VERSION = "custombuild.manufacturing-intent.v1"
MANUFACTURING_INTENT_PATH = "manufacturing/manufacturing-intent.json"
MANUFACTURING_INTENT_ROLE = "MACHINE_NEUTRAL_MANUFACTURING_INTENT"
SUPPLIER_HANDOFF_SCHEMA_VERSION = "custombuild.supplier-handoff.v2"
SUPPLIER_HANDOFF_PATH = "shop/supplier-handoff.json"
SUPPLIER_HANDOFF_ROLE = "CNC_SHOP_HANDOFF"
SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS = (
    "project_id",
    "revision",
    "design_hash",
    "app_version",
    "engine_version",
    "template_version",
    "domain_template_version",
    "template_capability_version",
    "template_capability_registry_version",
    "template_id",
    "template_capability_fingerprint",
    "template_capability",
    "rule_version",
    "material_versions",
    "joint_version",
    "machine_profile",
    "postprocessor_version",
    "generation_context_hash",
    "production_engine_context",
    "artifact_schema_version",
    "cad_status",
    "release_scope",
    "machine_use",
    "physical_cutting_authorized",
    "approved_assumptions",
    "warnings",
    "overrides",
    "external_evidence",
    "source_provenance",
)


def manufacturing_intent_json(
    *,
    parts: Iterable[PartSpec],
    project_id: str,
    revision: str,
    design_hash: str,
) -> bytes:
    """Freeze machine-neutral part and feature intent for an external shop.

    DXF consumers do not consistently preserve custom metadata.  This document
    therefore carries the complete semantic feature declaration beside the
    drawings, including the source side, local datum, depths, tolerances and
    cutter-envelope geometry.  It is design intent only: no coordinate
    transform, tooling choice or executable motion is implied.
    """

    if (
        not isinstance(project_id, str)
        or not project_id.strip()
        or not isinstance(revision, str)
        or not revision.strip()
        or not isinstance(design_hash, str)
        or re.fullmatch(r"[a-f0-9]{64}", design_hash) is None
    ):
        raise ValueError("manufacturing intent requires canonical package identity")
    part_values = tuple(sorted(parts, key=lambda item: item.part_id))
    unspecified_tolerance_feature_ids: list[str] = []
    unresolved_edge_ids: list[str] = []
    part_rows: list[dict[str, Any]] = []
    for part in part_values:
        feature_rows: list[dict[str, Any]] = []
        for feature in sorted(part.features, key=lambda item: item.feature_id):
            if feature.tolerance_um == 0:
                unspecified_tolerance_feature_ids.append(feature.feature_id)
            bounds = feature.bounds()
            machining_bounds = feature.machining_bounds()
            feature_rows.append(
                {
                    "feature_id": feature.feature_id,
                    "kind": feature.kind.value,
                    "side": feature.side.value,
                    "coordinate_reference": "PART_LOCAL_UV_FROM_FINISHED_OUTLINE_LOWER_LEFT",
                    "x_um": feature.x_um,
                    "y_um": feature.y_um,
                    "depth_um": feature.depth_um,
                    "diameter_um": feature.diameter_um,
                    "width_um": feature.width_um,
                    "length_um": feature.length_um,
                    "radius_um": feature.radius_um,
                    "pattern_count": feature.pattern_count,
                    "pitch_um": feature.pitch_um,
                    "pattern_points_um": [
                        {"x_um": point.x_um, "y_um": point.y_um}
                        for point in feature.points()
                    ],
                    "through": feature.through,
                    "nominal_bounds_um": canonical_data(bounds),
                    "cutter_envelope_um": canonical_data(machining_bounds),
                    "corner_strategy": feature.corner_strategy,
                    "corner_relief_radius_um": feature.corner_relief_radius_um,
                    "open_end_reliefs": list(feature.open_end_reliefs),
                    "tolerance_um": feature.tolerance_um or None,
                    "tolerance_status": (
                        "DECLARED_IN_DESIGN"
                        if feature.tolerance_um > 0
                        else "EXTERNAL_TOLERANCE_REQUIRED"
                    ),
                    "fit_clearance_um": feature.fit_clearance_um or None,
                    "metadata": feature.metadata,
                }
            )
        for detail in part.edge_band_details:
            if detail.procurement_status != "CATALOG_IDENTIFIED":
                unresolved_edge_ids.append(f"{part.part_id}:{detail.edge}")
        part_rows.append(
            {
                "part_id": part.part_id,
                "name": part.name,
                "quantity": part.quantity,
                "material": {"id": part.material_id, "version": part.material_version},
                "finished_dimensions_um": {
                    "u": part.width_um,
                    "v": part.height_um,
                    "thickness": part.thickness_um,
                },
                "raw_blank_dimensions_um": {
                    "u": part.blank_width_um,
                    "v": part.blank_height_um,
                    "thickness": part.thickness_um,
                },
                "axis_mapping": canonical_data(part.axis_mapping),
                "grain_direction": part.grain_direction,
                "allow_rotation": part.allow_rotation,
                "edge_bands": canonical_data(part.edge_band_details),
                "metadata": part.metadata,
                "features": feature_rows,
            }
        )

    payload = {
        "schema_version": MANUFACTURING_INTENT_SCHEMA_VERSION,
        "document_identity": {
            "project_id": project_id,
            "revision": revision,
            "design_hash": design_hash,
            "parts_sha256": sha256_hex(canonical_json_bytes(part_values)),
        },
        "document_purpose": "MACHINE_NEUTRAL_DESIGN_INTENT",
        "release_scope": "DESIGN_REVIEW",
        "physical_cutting_authorized": False,
        "units": {
            "stored_coordinates": "integer_micrometres",
            "exchange_drawings": "millimetres",
        },
        "coordinate_contract": {
            "part_datum": "FINISHED_OUTLINE_LOWER_LEFT",
            "part_axes": "LOCAL_UV_AS_DECLARED_BY_AXIS_MAPPING",
            "side_semantics": (
                "A_OR_B_IDENTIFIES_THE_SOURCE_FACE; NO_MACHINE_FLIP_OR_MIRROR_TRANSFORM_IS_IMPLIED"
            ),
        },
        "supplier_boundary": {
            "executable_toolpaths_included": False,
            "machine_coordinates_included": False,
            "feeds_speeds_authorized": False,
            "fixture_wcs_authorized": False,
            "required_action": (
                "Import and verify this intent, resolve every external decision, and generate "
                "shop-approved toolpaths in the supplier's controlled CAM workflow."
            ),
        },
        "external_decisions": {
            "unspecified_tolerance_feature_ids": sorted(
                set(unspecified_tolerance_feature_ids)
            ),
            "unresolved_edge_application_ids": sorted(set(unresolved_edge_ids)),
            "always_required": [
                "MATERIAL_BATCH_AND_ACTUAL_THICKNESS_ACCEPTANCE",
                "MACHINE_FIXTURE_WCS_AND_KEEP_OUT_ACCEPTANCE",
                "TOOLPATH_STRATEGY_AND_CUTTING_PARAMETERS",
                "FIRST_ARTICLE_MEASUREMENT_AND_RELEASE",
            ],
        },
        "parts": part_rows,
    }
    return canonical_json_bytes(payload)


def supplier_handoff_json(
    *,
    project_id: str,
    revision: str,
    design_hash: str,
    machine: MachineProfile,
    stocks: Iterable[StockSheet],
    operations: OperationsDocument | None,
    cam_status: str,
    blocker_codes: Iterable[str],
    cam_required_action: str,
    design_review_ready: bool,
    manifest_context_projection: Mapping[str, Any],
    payload_inventory_entries: Iterable[Mapping[str, Any]],
    known_unresolved_decision_codes: Iterable[str],
    dfm_warning_issues: Iterable[DFMIssue] = (),
) -> bytes:
    """Create the checksum-bound cover sheet for an external CNC quotation.

    The manifest remains the authoritative inventory because it includes this
    handoff file. The handoff binds the final manifest's non-artifact context
    plus every payload created before itself, avoiding a recursive self-hash
    while still selecting one exact accepted manifest contract.
    """

    stock_values = tuple(sorted(stocks, key=lambda item: item.stock_id))
    manifest_context = _canonical_supplier_manifest_context_projection(
        manifest_context_projection
    )
    payload_inventory = _canonical_supplier_payload_inventory(payload_inventory_entries)
    known_decision_codes = tuple(sorted(set(known_unresolved_decision_codes)))
    warning_rows = _canonical_supplier_dfm_warnings(dfm_warning_issues)
    supported_known_decisions = {
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
        BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    }
    if any(code not in supported_known_decisions for code in known_decision_codes):
        raise ValueError("supplier handoff contains an unsupported known decision")
    operation_binding: dict[str, Any]
    if operations is None:
        operation_binding = {
            "status": "NOT_GENERATED",
            "document_path": None,
            "schema_version": None,
            "mode": None,
            "tool_catalog_version": None,
            "tool_catalog_fingerprint": None,
            "setups": [],
            "selected_tools": [],
        }
    else:
        operation_binding = {
            "status": "MACHINE_NEUTRAL_VALIDATION_ONLY",
            "document_path": "cam/operations.json",
            "schema_version": operations.schema_version,
            "mode": operations.mode,
            "tool_catalog_version": operations.tool_catalog_version,
            "tool_catalog_fingerprint": operations.tool_catalog_fingerprint,
            "setups": canonical_data(operations.setups),
            "selected_tools": canonical_data(operations.tools),
        }

    questions = (
        (
            "Q01_IMPORT_AND_UNITS",
            "Can the shop import the supplied STEP and side-specific DXF files in millimetres "
            "and reproduce every part, feature side and local datum without repair?",
            "Import report with measured bounding boxes and a list of any repaired entities.",
        ),
        (
            "Q02_MATERIAL_AND_STOCK",
            "Will the supplied material grade, batch, actual thickness, grain direction and "
            "sheet condition match the bound design and stock assumptions?",
            "Supplier SKU, batch certificate, measured thickness and grain/face mapping.",
        ),
        (
            "Q03_MACHINE_AND_TRAVEL",
            "Has the shop selected a calibrated machine whose usable travel and controller "
            "semantics cover every bound stock, setup and feature?",
            "Machine identity, calibration status and usable-envelope check.",
        ),
        (
            "Q04_FIXTURE_WCS_AND_KEEP_OUT",
            "Has the shop independently approved fixture, clamp, spoilboard, WCS/origin, safe Z, "
            "keep-out zones and any two-sided registration method?",
            "Signed setup plan and collision-reviewed machine simulation.",
        ),
        (
            "Q05_TOOLS_AND_CUTTING_DATA",
            "Has every selected cutter been matched by version, measured diameter, runout, "
            "cutting length and compatible shop-approved feeds, speeds and entry strategy?",
            "Tool preset report and approved cutting-data record.",
        ),
        (
            "Q06_TOLERANCE_AND_FIT",
            "Are every declared tolerance and fit clearance manufacturable, and have all fields "
            "marked EXTERNAL_TOLERANCE_REQUIRED been resolved in writing?",
            "Marked-up drawing or signed tolerance matrix.",
        ),
        (
            "Q07_EXECUTABLE_CAM",
            "Has the shop generated, simulated and independently reviewed its own executable CAM "
            "from this machine-neutral intent rather than treating validation artifacts as code?",
            "Shop CAM revision, simulation evidence and independent reviewer approval.",
        ),
        (
            "Q08_FIRST_ARTICLE_AND_RELEASE",
            "Will an air-cut/coupon and measured first article pass before batch production, with "
            "nonconformities stopping release?",
            "Completed measurement plan and named production-release approval.",
        ),
        (
            "Q09_CONSTRUCTION_DECISIONS",
            "Has a qualified furniture constructor resolved and approved every named structural "
            "or retention decision independently of the CNC shop's manufacturability review?",
            "Revision-bound construction decision and structural/retention evidence.",
        ),
        (
            "Q10_ADJACENT_RELIEF_AND_MATERIAL_WEB",
            "For every pair of adjacent grooves, pockets or corner reliefs, has the shop "
            "checked the exact cutter-envelope clearance using actual cutter diameter and "
            "runout, calibrated machine accuracy, chip-out allowance and the residual material "
            "web rather than treating nominal geometry as robust clearance?",
            "CAM interference report plus coupon/first-article measurements of the residual "
            "material web; zero or tolerance-consumed clearance requires a reviewed strategy "
            "change and is never accepted by this handoff.",
        ),
    )
    blocker_values = sorted(set(blocker_codes))
    blocker_categories = {
        "STOCK_PROFILE_MISSING": "STOCK_SELECTION",
        "DFM-GRAIN-001": "MATERIAL_AND_GRAIN_BINDING",
        "TWO_SIDED_REGISTRATION_MISSING": "SETUP_AND_REGISTRATION",
        "DADO_RETENTION_EVIDENCE_MISSING": "STRUCTURAL_RETENTION",
        "BACK_PANEL_RETENTION_EVIDENCE_MISSING": "STRUCTURAL_RETENTION",
    }
    payload = {
        "schema_version": SUPPLIER_HANDOFF_SCHEMA_VERSION,
        "package_identity": {
            "project_id": project_id,
            "revision": revision,
            "design_hash": design_hash,
        },
        "package_contract": {
            "release_scope": "DESIGN_REVIEW",
            "machine_use": "VALIDATION_ONLY",
            "physical_cutting_authorized": False,
            "manifest_path": "manifest.json",
            "checksum_algorithm": "SHA-256",
            "authoritative_inventory": "manifest.json.artifacts",
            "inventory_fields": ["path", "media_type", "role", "size_bytes", "sha256"],
            "inventory_scope": (
                "ALL_PAYLOAD_FILES; MANIFEST_JSON_EXCLUDED_TO_AVOID_RECURSIVE_HASHING"
            ),
        },
        "manifest_context_binding": {
            "scope": (
                "FINAL_MANIFEST_CONTEXT_EXCLUDING_ARTIFACT_INVENTORY_AND_"
                "DERIVED_MANIFEST_HASH"
            ),
            "excluded_fields": [
                "schema_version",
                "artifacts",
                "production_context_hash",
                "checksum_scope",
            ],
            "field_names": list(SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS),
            "manifest_context_sha256": sha256_hex(
                canonical_json_bytes(manifest_context)
            ),
            "context": manifest_context,
        },
        "payload_inventory_binding": {
            "scope": (
                "CANONICAL_MANIFEST_ENTRY_PROJECTION_FOR_ALL_PAYLOADS_CREATED_BEFORE_"
                "SUPPLIER_HANDOFF; EXCLUDES_MANIFEST_JSON_AND_SUPPLIER_HANDOFF"
            ),
            "excluded_paths": ["manifest.json", SUPPLIER_HANDOFF_PATH],
            "artifact_count": len(payload_inventory),
            "payload_inventory_sha256": sha256_hex(
                canonical_json_bytes(payload_inventory)
            ),
            "artifacts": list(payload_inventory),
        },
        "readiness": {
            "package_review_availability": "AVAILABLE_FOR_BOUNDED_DESIGN_REVIEW",
            "complete_validation_evidence_ready": design_review_ready,
            "complete_validation_evidence_scope": (
                "ALL_WORKSHOP_READINESS_SOFTWARE_EVIDENCE_INCLUDING_"
                "NON_CUTTING_CONTROLLER_VALIDATION"
            ),
            "cam_status": cam_status,
            "blocker_codes": blocker_values,
            "cam_required_action": cam_required_action,
            "supplier_acceptance_complete": False,
        },
        "dfm_review_warnings": [
            {
                "issue": warning,
                "source": "validation/dfm-report.json",
                "status": "UNRESOLVED_SUPPLIER_REVIEW_WARNING",
                "resolved": False,
                "boundary": (
                    "Review the referenced structured DFM issue and close it with supplier "
                    "evidence before physical release. This warning is not cutting approval."
                ),
            }
            for warning in warning_rows
        ],
        "supplier_stages": {
            "available_for_quote_review": True,
            "quote_review_scope": (
                "SUPPLIER_ESTIMATION_ONLY_SUBJECT_TO_ALL_NAMED_BLOCKERS"
            ),
            "available_for_geometry_review": True,
            "geometry_review_scope": "IMPORT_AND_DIMENSIONAL_REVIEW_ONLY",
            "available_for_cam_intake_review": True,
            "cam_intake_review_scope": (
                "IMPORT_AND_REVIEW_OF_MACHINE_NEUTRAL_GEOMETRY_AND_INTENT_ONLY"
            ),
            "shop_review_required": True,
            "manufacturing_approval_granted": False,
            "cut_authorized": False,
        },
        "unresolved_inputs_and_decisions": [
            {
                "code": code,
                "category": blocker_categories.get(code, "UNCLASSIFIED_BLOCKER"),
                "required_action": cam_required_action,
                "resolved": False,
                "boundary": (
                    "Does not prevent supplier estimation or geometry review, but blocks the "
                    "named downstream stage and never establishes structural or cutting approval."
                ),
            }
            for code in blocker_values
        ],
        "known_unresolved_decisions": [
            {
                "code": code,
                "category": "STRUCTURAL_RETENTION",
                "source": "FROZEN_CANONICAL_DESIGN",
                "required_action": BLOCKED_CAM_REQUIRED_ACTIONS[code],
                "resolved": False,
                "boundary": (
                    "Independent construction evidence is still required even when another "
                    "earlier-stage blocker currently determines CAM status."
                ),
            }
            for code in known_decision_codes
        ],
        "selected_validation_machine_profile": {
            "profile": canonical_data(machine),
            "sha256": sha256_hex(canonical_json_bytes(machine)),
            "authority": "VALIDATION_ASSUMPTION_NOT_SHOP_APPROVAL",
        },
        "stock_assumptions": {
            "profiles": canonical_data(stock_values),
            "sha256": sha256_hex(canonical_json_bytes(stock_values)),
            "authority": "DESIGN_AND_NESTING_ASSUMPTIONS_NOT_SUPPLIER_BATCH_EVIDENCE",
        },
        "operation_binding": operation_binding,
        "shop_acceptance_questions": [
            {
                "question_id": question_id,
                "question": question,
                "required_evidence": evidence,
                "answer": None,
                "answered_by": None,
                "answered_at": None,
                "evidence_reference": None,
                "status": "UNANSWERED",
            }
            for question_id, question, evidence in questions
        ],
        "acceptance_rule": (
            "Every question requires a recorded affirmative answer and referenced evidence in "
            "the shop's controlled system. This document never changes physical authorization."
        ),
    }
    return canonical_json_bytes(payload)


def _canonical_supplier_dfm_warnings(
    issues: Iterable[DFMIssue],
) -> tuple[dict[str, Any], ...]:
    """Project every structured DFM warning into the standalone shop handoff."""

    rows: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, DFMIssue) or issue.severity is not Severity.WARNING:
            raise ValueError("supplier handoff DFM projection accepts warning issues only")
        row = {
            "code": issue.code,
            "message": issue.message,
            "part_id": issue.part_id,
            "feature_id": issue.feature_id,
            "setup_id": issue.setup_id,
            "inputs": canonical_data(issue.inputs),
            "suggestion": issue.suggestion,
        }
        canonical_json_bytes(row)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row["code"]),
            str(row["part_id"] or ""),
            str(row["feature_id"] or ""),
            canonical_json_bytes(row),
        )
    )
    if len({canonical_json_bytes(row) for row in rows}) != len(rows):
        raise ValueError("supplier handoff contains duplicate DFM warning issues")
    return tuple(rows)


def _canonical_supplier_manifest_context_projection(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Require the complete non-recursive manifest context used by the builder."""

    required_fields = frozenset(SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS)
    if frozenset(projection) != required_fields:
        raise ValueError("supplier handoff manifest context projection is incomplete")
    normalized = canonical_data(projection)
    if not isinstance(normalized, dict):
        raise ValueError("supplier handoff manifest context projection is invalid")
    try:
        canonical_json_bytes(normalized)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("supplier handoff manifest context projection is invalid") from exc
    return normalized


def _canonical_supplier_payload_inventory(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Normalize the non-recursive payload inventory bound by the handoff."""

    required_fields = {"path", "media_type", "role", "size_bytes", "sha256"}
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if set(entry) != required_fields:
            raise ValueError("supplier payload inventory entry has an unexpected structure")
        path = entry.get("path")
        media_type = entry.get("media_type")
        role = entry.get("role")
        size_bytes = entry.get("size_bytes")
        digest = entry.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path in {"manifest.json", SUPPLIER_HANDOFF_PATH}
            or not isinstance(media_type, str)
            or not media_type
            or not isinstance(role, str)
            or not role
            or type(size_bytes) is not int
            or size_bytes < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            raise ValueError("supplier payload inventory entry is invalid")
        normalized.append(
            {
                "path": path,
                "media_type": media_type,
                "role": role,
                "size_bytes": size_bytes,
                "sha256": digest,
            }
        )
    normalized.sort(key=lambda entry: str(entry["path"]))
    paths = [str(entry["path"]) for entry in normalized]
    if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
        raise ValueError("supplier payload inventory paths must be unique")
    return tuple(normalized)


def label_index_csv(
    *,
    parts: Iterable[PartSpec],
    layouts: NestingLayout | Iterable[NestingLayout],
    operations: OperationsDocument,
) -> bytes:
    """Return one traceable label row for every placed part instance.

    The QR payload is an identifier, not an approval token.  It binds the
    physical label to the immutable design hash and exact expanded instance.
    """

    part_values = tuple(parts)
    layout_values = _coerce_layouts(layouts)
    instances = _instances_by_id(part_values)
    placements = _validated_placements(layout_values, instances)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "schema_version",
            "design_hash",
            "instance_id",
            "part_id",
            "part_name",
            "material_id",
            "material_version",
            "finished_width_mm",
            "finished_height_mm",
            "finished_thickness_mm",
            "stock_id",
            "sheet_number",
            "placement_x_mm",
            "placement_y_mm",
            "placement_width_mm",
            "placement_height_mm",
            "rotated_90",
            "qr_payload",
            "physical_release_authorized",
        )
    )
    for placement in placements:
        instance = instances[placement.instance_id]
        part = instance.part
        writer.writerow(
            (
                LABEL_INDEX_SCHEMA_VERSION,
                operations.design_hash,
                instance.instance_id,
                part.part_id,
                part.name,
                part.material_id,
                part.material_version,
                um_to_mm(part.width_um),
                um_to_mm(part.height_um),
                um_to_mm(part.thickness_um),
                placement.stock_id,
                placement.sheet_index + 1,
                um_to_mm(placement.x_um),
                um_to_mm(placement.y_um),
                um_to_mm(placement.width_um),
                um_to_mm(placement.height_um),
                str(placement.rotated_90).lower(),
                f"custombuild:part:{operations.design_hash}:{instance.instance_id}",
                "false",
            )
        )
    return stream.getvalue().encode("utf-8")


def quality_measurement_plan_json(
    *,
    parts: Iterable[PartSpec],
    layouts: NestingLayout | Iterable[NestingLayout],
    operations: OperationsDocument,
) -> bytes:
    """Return a complete inspection plan with deliberately blank results."""

    part_values = tuple(parts)
    layout_values = _coerce_layouts(layouts)
    instances = _instances_by_id(part_values)
    placements = _validated_placements(layout_values, instances)
    placed_ids = {placement.instance_id for placement in placements}
    unplaced_ids = tuple(sorted(set(instances) - placed_ids))

    dimension_checks: list[dict[str, Any]] = []
    for instance in sorted(instances.values(), key=lambda item: item.instance_id):
        for axis, nominal_um in (
            ("width", instance.part.width_um),
            ("height", instance.part.height_um),
            ("thickness", instance.part.thickness_um),
        ):
            dimension_checks.append(
                {
                    "check_id": f"dimension:{instance.instance_id}:{axis}",
                    "instance_id": instance.instance_id,
                    "part_id": instance.part.part_id,
                    "kind": "FINISHED_DIMENSION",
                    "axis": axis,
                    "nominal_um": nominal_um,
                    "tolerance_um": None,
                    "tolerance_status": "EXTERNAL_TOLERANCE_REQUIRED",
                    "measured_um": None,
                    "result": None,
                    "measured_by": None,
                    "measured_at": None,
                }
            )

    operation_checks: list[dict[str, Any]] = []
    for operation in sorted(operations.operations, key=lambda item: item.operation_id):
        declared_tolerance = operation.tolerance_um or None
        operation_checks.append(
            {
                "check_id": f"operation:{operation.operation_id}",
                "operation_id": operation.operation_id,
                "setup_id": operation.setup_id,
                "instance_id": operation.instance_id,
                "part_id": operation.part_id,
                "feature_id": operation.feature_id,
                "kind": operation.kind.value,
                "side": operation.side.value,
                "nominal": {
                    "x_um": operation.x_um,
                    "y_um": operation.y_um,
                    "depth_um": operation.depth_um,
                    "diameter_um": operation.diameter_um,
                    "width_um": operation.width_um,
                    "length_um": operation.length_um,
                },
                "tolerance_um": declared_tolerance,
                "tolerance_status": (
                    "DECLARED_IN_DESIGN"
                    if declared_tolerance is not None
                    else "EXTERNAL_TOLERANCE_REQUIRED"
                ),
                "fit_clearance_um": operation.fit_clearance_um or None,
                "measured": None,
                "result": None,
                "measured_by": None,
                "measured_at": None,
            }
        )

    payload = {
        "schema_version": QUALITY_MEASUREMENT_PLAN_SCHEMA_VERSION,
        "design_hash": operations.design_hash,
        "release_scope": "DESIGN_REVIEW",
        "physical_release_authorized": False,
        "approval_state": "PENDING_EXTERNAL_MEASUREMENT",
        "instructions": (
            "Record actual measurements and an authorized result externally; "
            "blank fields are intentional and never imply approval."
        ),
        "coverage": {
            "part_instance_count": len(instances),
            "placed_instance_count": len(placed_ids),
            "unplaced_instance_ids": unplaced_ids,
            "dimension_check_count": len(dimension_checks),
            "operation_count": len(operations.operations),
            "operation_check_count": len(operation_checks),
        },
        "dimension_checks": dimension_checks,
        "operation_checks": operation_checks,
    }
    return canonical_json_bytes(payload)


def _coerce_layouts(
    layouts: NestingLayout | Iterable[NestingLayout],
) -> tuple[NestingLayout, ...]:
    return (layouts,) if isinstance(layouts, NestingLayout) else tuple(layouts)


def _instances_by_id(parts: tuple[PartSpec, ...]) -> dict[str, PartInstance]:
    instances = expand_part_instances(parts)
    by_id = {instance.instance_id: instance for instance in instances}
    if len(by_id) != len(instances):
        raise ValueError("expanded part instances must have unique IDs")
    return by_id


def _validated_placements(
    layouts: tuple[NestingLayout, ...],
    instances: dict[str, PartInstance],
) -> tuple[Any, ...]:
    placements = tuple(
        sorted(
            (placement for layout in layouts for placement in layout.placements),
            key=lambda item: (item.stock_id, item.sheet_index, item.instance_id),
        )
    )
    seen: set[str] = set()
    for placement in placements:
        instance = instances.get(placement.instance_id)
        if instance is None:
            raise ValueError(f"placement references unknown instance {placement.instance_id}")
        if placement.part_id != instance.part.part_id:
            raise ValueError(
                f"placement {placement.instance_id} does not match part {instance.part.part_id}"
            )
        if placement.instance_id in seen:
            raise ValueError(f"part instance {placement.instance_id} is placed more than once")
        seen.add(placement.instance_id)
    return placements
