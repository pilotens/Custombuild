"""Deterministic, non-authorizing workshop quality documents."""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, NoReturn

from .model import (
    DFMIssue,
    FeatureKind,
    MachineProfile,
    NestingLayout,
    OperationKind,
    OperationsDocument,
    PartInstance,
    PartSpec,
    Severity,
    Side,
    StockSheet,
    canonical_data,
    canonical_json_bytes,
    expand_part_instances,
    sha256_hex,
    um_to_mm,
)
from .operations import OPERATIONS_SCHEMA_VERSION
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
JOINT_RETENTION_SIGNED_EVIDENCE_PATH = "evidence/joint-retention/signed-evidence.json"
JOINT_RETENTION_SIGNED_EVIDENCE_ROLE = "JOINT_RETENTION_SIGNED_EVIDENCE"
JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE = "application/json"
SUPPLIER_HANDOFF_SCHEMA_VERSION = "custombuild.supplier-handoff.v3"
SUPPLIER_HANDOFF_PATH = "shop/supplier-handoff.json"
SUPPLIER_HANDOFF_ROLE = "CNC_SHOP_HANDOFF"
START_HERE_PATH = "START-HERE.md"
START_HERE_ROLE = "PACKAGE_GUIDE"
MANUFACTURING_INTENT_JSON_SCHEMA_PATH = "schemas/manufacturing-intent.v1.schema.json"
OPERATIONS_JSON_SCHEMA_PATH = "schemas/operations.v2.schema.json"
SUPPLIER_HANDOFF_JSON_SCHEMA_PATH = "schemas/supplier-handoff.v3.schema.json"
JSON_SCHEMA_ROLE = "JSON_SCHEMA"
JSON_SCHEMA_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
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

SUPPLIER_ACCEPTANCE_QUESTIONS = (
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
        "keep-out zones and any two-sided registration method, including rechecking every "
        "CLIENT_DECLARED value in validation/stock-selection.json and "
        "validation/generation-plan.json?",
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


def start_here_markdown() -> bytes:
    """Return the deterministic, non-authorizing supplier package guide."""

    question_lines = [
        f"{index}. `{question_id}` — {question} Evidence: {evidence}"
        for index, (question_id, question, evidence) in enumerate(
            SUPPLIER_ACCEPTANCE_QUESTIONS,
            start=1,
        )
    ]
    sections = [
        "# START HERE — Custombuild supplier review package",
        "",
        "## Safety and authority boundary",
        "",
        "This ZIP is a **machine-neutral design-review package**. It contains no approved "
        "executable G-code, no approved feeds or speeds, no approved fixture/WCS, and no "
        "permission to cut material. The CNC shop must create, simulate, review and approve "
        "its own CAM and setup before any physical operation.",
        "",
        "The ZIP is **checksummed but unsigned**. SHA-256 can detect an accidental byte change "
        "relative to its contained manifest; it does not prove who published the package or "
        "detect a coordinated rewrite of both files and manifest. Obtain "
        "the ZIP through the authenticated Custombuild download and independently confirm the "
        "project, revision and design hash with the customer.",
        "",
        "## Verify before review",
        "",
        "Treat the received ZIP only as untrusted data; do not extract it and never execute "
        "anything contained in it. Obtain the separately distributed, reviewed standard-library "
        "verifier through a trusted channel, install it outside the download, and run Python "
        "3.11 or newer with the exact filename and independently confirmed order identity:",
        "",
        "```sh",
        'python3 -I /trusted/verify_production_package.py "<downloaded-package>.zip" '
        '--expect-project-id "<project-id>" '
        '--expect-revision "<revision>" --expect-design-hash "<64-char-design-hash>"',
        "```",
        "",
        "On Windows, use `py -3 -I C:\\trusted\\verify_production_package.py` followed by the "
        "package path and options. The trusted verifier needs no Custombuild installation or "
        "third-party Python package. Accept only a JSON result with `status` equal to `PASS` and "
        "process exit code 0. Preserve the JSON result with the shop review record.",
        "",
        "The verifier rejects unsafe, duplicate or case-alias paths before extraction; requires "
        "the exact v5 manifest inventory with no extra or missing files; checks every declared "
        "byte size and SHA-256; recalculates `production_context_hash`; and compares any supplied "
        "project, revision and design hash. Those expected values compare unsigned manifest claims "
        "only; they do not independently reconstruct design semantics or establish authenticity. "
        "The guide is inventoried and checksummed; the verifier is intentionally outside the "
        "untrusted ZIP and must come from a trusted channel.",
        "",
        "A verifier `PASS` proves only internal manifest consistency and can detect accidental "
        "corruption. It cannot detect a malicious coordinated rewrite of both payloads and the "
        "unsigned manifest. It does not authenticate the publisher or evidence issuer. It does "
        "not establish current revocation or expiry status for external signed evidence. It does "
        "not authorize physical cutting, machining or assembly. Obtain the ZIP through the "
        "authenticated Custombuild download and confirm its order identity out of band.",
        "",
        "After the verifier passes, validate `manufacturing/manufacturing-intent.json`, optional "
        "`cam/operations.json`, and `shop/supplier-handoff.json` against their exact "
        "Draft 2020-12 schemas in `schemas/`. Confirm each schema path, version and SHA-256 "
        "against `shop/supplier-handoff.json.package_contract` before using a document.",
        "",
        "## Units, faces and coordinates",
        "",
        "JSON dimensions and coordinates ending in `_um` are integer micrometres: "
        "`1000 um = 1 mm`. STEP, DXF and SVG exchange geometry is expressed in millimetres. "
        "Do not rescale on import.",
        "",
        "Part coordinates use the finished-outline lower-left local datum and the declared local "
        "U/V axes. Face `A` or `B` identifies the source physical face only. It does **not** "
        "define a machine flip, mirror transform, stock origin, fixture, registration method or "
        "work coordinate system. The shop owns and records those decisions.",
        "",
        "Structured stock profiles and two-sided registration records marked "
        "`CLIENT_DECLARED` are unverified caller statements. The 6000 um minimum kerf is the "
        "supported validation contour-tool envelope, not approval of a cutter or toolpath. Pin "
        "diameter, position tolerance, fixture method/version and generated pin keep-outs support "
        "deterministic collision screening only; the shop must measure and approve the physical "
        "fixture, WCS and registration before creating its own CAM.",
        "For each declared pin, the conservative radius is "
        "`r = (pin_diameter_um + 1) // 2 + position_tolerance_um` and its footprint is "
        "`Rect(x_um-r, y_um-r, 2*r, 2*r)`. Every footprint must be fully on-sheet, "
        "disjoint from declared defect and fixture zones, and included in the deterministically "
        "sorted/deduplicated role-wide nesting keep-out union. Every pair of pin centres must be "
        "at least `100000 + 2*r` micrometres apart, leaving a 100000 um usable baseline. These "
        "checks still do not verify a physical pin, fixture or WCS and do not authorize cutting.",
        "",
        "## CUT intent versus REFERENCE material",
        "",
        "- **CUT intent:** `model/design.step`, `parts/<part-id>/A.dxf`, "
        "`parts/<part-id>/B.dxf`, and `manufacturing/manufacturing-intent.json` describe desired "
        "finished geometry and features for CAM interpretation. CUT intent is not a toolpath and "
        "is not cutting authorization.",
        "- **REFERENCE:** files under `drawings/`, the GLB preview, PDFs, labels, nesting images "
        "and validation backplots support visual checking and communication. Never derive an "
        "unreviewed toolpath from reference material.",
        "- **VALIDATION ONLY:** optional files under `cam/` and `machine-validation/` document "
        "machine-neutral operations or non-cutting controller validation. They are not approved "
        "production programs and must never be used to cut.",
        "",
        "## Core artifact map",
        "",
        "- `manifest.json` — authoritative v5 package inventory, identity and SHA-256 digests.",
        "- No executable verifier or `__main__.py` is included. Never execute content from the "
        "ZIP; "
        "use the separately trusted verifier described above.",
        "- `model/design.step` — authoritative assembled 3D geometry for interchange review.",
        "- `parts/` — side-specific A/B DXF geometry; preserve layers and units.",
        "- `drawings/` — side-specific human-readable SVG reference drawings.",
        "- `bom/`, `cut-list/`, `materials/` — quantities and procurement/review schedules.",
        "- `manufacturing/manufacturing-intent.json` — complete part, feature, datum, side, fit "
        "and tolerance intent.",
        "- `cam/operations.json` — optional, strictly schema-bound machine-neutral VALIDATION "
        "operations. It is neither executable CAM nor permission to cut.",
        "- `shop/supplier-handoff.json` — exact v3 package binding, blockers, warnings and the "
        "ten supplier acceptance questions.",
        "- `validation/stock-selection.json` — exact unverified stock declarations, dimensions, "
        "kerf envelope, defects and the role-wide clamp/registration keep-out union.",
        "- `validation/generation-plan.json` — validation machine identity and unverified "
        "CLIENT_DECLARED two-sided registration inputs bound to stock and sheet indexes.",
        "- `validation/` — status and DFM evidence; unresolved warnings and blockers remain open.",
        "- `schemas/` — published Draft 2020-12 JSON Schemas for manufacturing intent, the "
        "optional operations document and the supplier handoff.",
        "",
        "## Supplier acceptance — Q01 to Q10",
        "",
        *question_lines,
        "",
        "Record every answer and its evidence in the shop's controlled system. Only the shop's "
        "named production approver may release its own machine program after all applicable "
        "questions, warnings, blockers, simulation, coupon/air-cut and first-article checks are "
        "closed. This package itself never changes physical authorization.",
        "",
    ]
    return "\n".join(sections).encode("utf-8")


def manufacturing_intent_json_schema() -> bytes:
    """Publish the Draft 2020-12 schema for manufacturing-intent v1."""

    nullable_non_negative_integer = {
        "type": ["integer", "null"],
        "minimum": 0,
    }
    bounds = {
        "type": "object",
        "additionalProperties": False,
        "required": ["x_um", "y_um", "width_um", "height_um"],
        "properties": {
            "x_um": {"type": "integer"},
            "y_um": {"type": "integer"},
            "width_um": {"type": "integer", "minimum": 1},
            "height_um": {"type": "integer", "minimum": 1},
        },
    }
    dimensions = {
        "type": "object",
        "additionalProperties": False,
        "required": ["u", "v", "thickness"],
        "properties": {
            "u": {"type": "integer", "minimum": 1},
            "v": {"type": "integer", "minimum": 1},
            "thickness": {"type": "integer", "minimum": 1},
        },
    }
    feature_properties: dict[str, Any] = {
        "feature_id": {"$ref": "#/$defs/nonEmptyString"},
        "kind": {"enum": sorted(item.value for item in FeatureKind)},
        "side": {"enum": ["A", "B"]},
        "coordinate_reference": {"const": "PART_LOCAL_UV_FROM_FINISHED_OUTLINE_LOWER_LEFT"},
        "x_um": {"type": "integer"},
        "y_um": {"type": "integer"},
        "depth_um": {"type": "integer", "minimum": 0},
        "diameter_um": nullable_non_negative_integer,
        "width_um": nullable_non_negative_integer,
        "length_um": nullable_non_negative_integer,
        "radius_um": nullable_non_negative_integer,
        "pattern_count": {"type": "integer", "minimum": 1},
        "pitch_um": nullable_non_negative_integer,
        "pattern_points_um": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/point"},
        },
        "through": {"type": "boolean"},
        "nominal_bounds_um": bounds,
        "cutter_envelope_um": bounds,
        "corner_strategy": {"type": ["string", "null"]},
        "corner_relief_radius_um": nullable_non_negative_integer,
        "open_end_reliefs": {"type": "array", "items": {"type": "string"}},
        "tolerance_um": nullable_non_negative_integer,
        "tolerance_status": {"enum": ["DECLARED_IN_DESIGN", "EXTERNAL_TOLERANCE_REQUIRED"]},
        "fit_clearance_um": nullable_non_negative_integer,
        "metadata": {"type": "object"},
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:custombuild:schema:manufacturing-intent:v1",
        "title": "Custombuild machine-neutral manufacturing intent v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "document_identity",
            "document_purpose",
            "release_scope",
            "physical_cutting_authorized",
            "units",
            "coordinate_contract",
            "supplier_boundary",
            "external_decisions",
            "parts",
        ],
        "properties": {
            "schema_version": {"const": MANUFACTURING_INTENT_SCHEMA_VERSION},
            "document_identity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project_id", "revision", "design_hash", "parts_sha256"],
                "properties": {
                    "project_id": {"$ref": "#/$defs/nonEmptyString"},
                    "revision": {"$ref": "#/$defs/nonEmptyString"},
                    "design_hash": {"$ref": "#/$defs/sha256"},
                    "parts_sha256": {"$ref": "#/$defs/sha256"},
                },
            },
            "document_purpose": {"const": "MACHINE_NEUTRAL_DESIGN_INTENT"},
            "release_scope": {"const": "DESIGN_REVIEW"},
            "physical_cutting_authorized": {"const": False},
            "units": {
                "const": {
                    "stored_coordinates": "integer_micrometres",
                    "exchange_drawings": "millimetres",
                }
            },
            "coordinate_contract": {
                "const": {
                    "part_datum": "FINISHED_OUTLINE_LOWER_LEFT",
                    "part_axes": "LOCAL_UV_AS_DECLARED_BY_AXIS_MAPPING",
                    "side_semantics": (
                        "A_OR_B_IDENTIFIES_THE_SOURCE_FACE; "
                        "NO_MACHINE_FLIP_OR_MIRROR_TRANSFORM_IS_IMPLIED"
                    ),
                }
            },
            "supplier_boundary": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "executable_toolpaths_included",
                    "machine_coordinates_included",
                    "feeds_speeds_authorized",
                    "fixture_wcs_authorized",
                    "required_action",
                ],
                "properties": {
                    "executable_toolpaths_included": {"const": False},
                    "machine_coordinates_included": {"const": False},
                    "feeds_speeds_authorized": {"const": False},
                    "fixture_wcs_authorized": {"const": False},
                    "required_action": {"$ref": "#/$defs/nonEmptyString"},
                },
            },
            "external_decisions": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "unspecified_tolerance_feature_ids",
                    "unresolved_edge_application_ids",
                    "always_required",
                ],
                "properties": {
                    "unspecified_tolerance_feature_ids": {"$ref": "#/$defs/uniqueStringArray"},
                    "unresolved_edge_application_ids": {"$ref": "#/$defs/uniqueStringArray"},
                    "always_required": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 4,
                        "items": {"$ref": "#/$defs/nonEmptyString"},
                        "uniqueItems": True,
                    },
                },
            },
            "parts": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "part_id",
                        "name",
                        "quantity",
                        "material",
                        "finished_dimensions_um",
                        "raw_blank_dimensions_um",
                        "axis_mapping",
                        "grain_direction",
                        "allow_rotation",
                        "edge_bands",
                        "metadata",
                        "features",
                    ],
                    "properties": {
                        "part_id": {"$ref": "#/$defs/nonEmptyString"},
                        "name": {"$ref": "#/$defs/nonEmptyString"},
                        "quantity": {"type": "integer", "minimum": 1},
                        "material": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["id", "version"],
                            "properties": {
                                "id": {"$ref": "#/$defs/nonEmptyString"},
                                "version": {"$ref": "#/$defs/nonEmptyString"},
                            },
                        },
                        "finished_dimensions_um": dimensions,
                        "raw_blank_dimensions_um": dimensions,
                        "axis_mapping": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["u_axis", "v_axis", "thickness_axis"],
                            "properties": {
                                "u_axis": {"enum": ["x", "y", "z"]},
                                "v_axis": {"enum": ["x", "y", "z"]},
                                "thickness_axis": {"enum": ["x", "y", "z"]},
                            },
                        },
                        "grain_direction": {"type": "string"},
                        "allow_rotation": {"type": "boolean"},
                        "edge_bands": {"type": "array", "items": {"type": "object"}},
                        "metadata": {"type": "object"},
                        "features": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": list(feature_properties),
                                "properties": feature_properties,
                            },
                        },
                    },
                },
            },
        },
        "$defs": {
            "nonEmptyString": {"type": "string", "minLength": 1},
            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "point": {
                "type": "object",
                "additionalProperties": False,
                "required": ["x_um", "y_um"],
                "properties": {
                    "x_um": {"type": "integer"},
                    "y_um": {"type": "integer"},
                },
            },
            "uniqueStringArray": {
                "type": "array",
                "items": {"$ref": "#/$defs/nonEmptyString"},
                "uniqueItems": True,
            },
        },
    }
    return canonical_json_bytes(schema)


def operations_json_schema() -> bytes:
    """Publish the strict Draft 2020-12 contract for emitted operations v2.

    The contract describes machine-neutral validation intent only.  It does
    not turn the selected validation profile, tools, WCS or setup prose into
    verified workshop facts or physical cutting authorization.
    """

    nullable_positive_integer = {
        "type": ["integer", "null"],
        "minimum": 1,
    }
    nullable_non_negative_integer = {
        "type": ["integer", "null"],
        "minimum": 0,
    }
    rectangle = {
        "type": "object",
        "additionalProperties": False,
        "required": ["x_um", "y_um", "width_um", "height_um"],
        "properties": {
            "x_um": {"type": "integer"},
            "y_um": {"type": "integer"},
            "width_um": {"type": "integer", "minimum": 1},
            "height_um": {"type": "integer", "minimum": 1},
        },
    }
    setup_properties: dict[str, Any] = {
        "setup_id": {"$ref": "#/$defs/canonicalId"},
        "stock_id": {"$ref": "#/$defs/canonicalId"},
        "material_id": {"$ref": "#/$defs/canonicalId"},
        "material_version": {"$ref": "#/$defs/nonEmptyString"},
        "sheet_index": {"type": "integer", "minimum": 0},
        "side": {"enum": [Side.A.value, Side.B.value]},
        "wcs": {"pattern": "^G5[4-9]$", "type": "string"},
        "origin": {"$ref": "#/$defs/point"},
        "stock_width_um": {"type": "integer", "minimum": 1},
        "stock_height_um": {"type": "integer", "minimum": 1},
        "stock_thickness_um": {"type": "integer", "minimum": 1},
        "safe_z_um": {"type": "integer", "minimum": 1},
        "reference_surface": {"const": "EXTERNAL_STOCK_TOP_MEASUREMENT_REQUIRED"},
        "orientation": {
            "enum": [
                "A_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT",
                "FLIP_STOCK_ABOUT_X_AXIS; MACHINE_Y=STOCK_HEIGHT-DESIGN_Y",
            ]
        },
        "fixture": {"const": "EXTERNAL_FIXTURE_PLAN_REQUIRED; DECLARED_KEEP_OUT_ZONES_ONLY"},
        "keep_out_zones": {
            "type": "array",
            "items": rectangle,
        },
        "tool_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/canonicalId"},
            "uniqueItems": True,
        },
        "probe_method": {
            "type": "string",
            "maxLength": 4096,
            "pattern": (
                "^(?:EXTERNAL_COORDINATE_REGISTRATION_REQUIRED|"
                "DECLARED_COORDINATE_REGISTRATION;"
                "DECLARATION_AUTHORITY=CLIENT_DECLARED;"
                "METHOD=[A-Za-z0-9][A-Za-z0-9._:-]{0,63};"
                "METHOD_VERSION=[A-Za-z0-9][A-Za-z0-9._:-]{0,63};"
                "PIN_DIAMETER_UM=[0-9]+;POSITION_TOLERANCE_UM=[0-9]+;"
                "STOCK_XY_UM=[0-9]+,[0-9]+(?:\\|[0-9]+,[0-9]+)+;"
                "EXTERNAL_SETUP_VERIFICATION_REQUIRED)$"
            ),
        },
        "operator_steps": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {"$ref": "#/$defs/nonEmptyString"},
        },
    }
    operation_properties: dict[str, Any] = {
        "operation_id": {"$ref": "#/$defs/canonicalId"},
        "setup_id": {"$ref": "#/$defs/canonicalId"},
        "part_id": {"$ref": "#/$defs/canonicalId"},
        "instance_id": {"$ref": "#/$defs/canonicalId"},
        "feature_id": {"$ref": "#/$defs/canonicalId"},
        "kind": {"enum": sorted(item.value for item in OperationKind)},
        "side": {"enum": [Side.A.value, Side.B.value]},
        "tool_id": {"$ref": "#/$defs/canonicalId"},
        "x_um": {"type": "integer", "minimum": 0},
        "y_um": {"type": "integer", "minimum": 0},
        "depth_um": {"type": "integer", "minimum": 1},
        "diameter_um": nullable_positive_integer,
        "width_um": nullable_positive_integer,
        "length_um": nullable_positive_integer,
        "cutter_envelope_x_um": nullable_non_negative_integer,
        "cutter_envelope_y_um": nullable_non_negative_integer,
        "cutter_envelope_width_um": nullable_positive_integer,
        "cutter_envelope_length_um": nullable_positive_integer,
        "stepdown_um": {"type": "integer", "minimum": 1},
        "stepover_ppm": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 1_000_000,
        },
        "through": {"type": "boolean"},
        "source_rotation_90": {"type": "boolean"},
        "compensation": {"enum": [None, "CENTER", "INSIDE", "OUTSIDE"]},
        "holding_strategy": {"enum": [None, "TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL"]},
        "corner_strategy": {"enum": [None, "dogbone-v1", "dogbone-v2"]},
        "corner_relief_radius_um": nullable_positive_integer,
        "open_end_reliefs": {
            "type": "array",
            "items": {"enum": ["u_min", "u_max", "v_min", "v_max"]},
            "uniqueItems": True,
        },
        "tolerance_um": {"type": "integer", "minimum": 0},
        "fit_clearance_um": {"type": "integer", "minimum": 0},
    }
    tool_properties: dict[str, Any] = {
        "tool_id": {"$ref": "#/$defs/canonicalId"},
        "name": {"$ref": "#/$defs/nonEmptyString"},
        "diameter_um": {"type": "integer", "minimum": 1},
        "cutting_length_um": {"type": "integer", "minimum": 1},
        "supported_operations": {
            "type": "array",
            "minItems": 1,
            "items": {"enum": sorted(item.value for item in OperationKind)},
            "uniqueItems": True,
        },
        "spindle_rpm": {"type": "integer", "minimum": 1},
        "feed_um_min": {"type": "integer", "minimum": 1},
        "plunge_um_min": {"type": "integer", "minimum": 1},
        "measured_diameter_um": nullable_positive_integer,
        "runout_um": {"type": "integer", "minimum": 0},
        "version": {"$ref": "#/$defs/nonEmptyString"},
    }
    schema = {
        "$schema": JSON_SCHEMA_DRAFT_2020_12,
        "$id": "urn:custombuild:schema:operations:v2",
        "title": "Custombuild machine-neutral validation operations v2",
        "description": (
            "Strict shape of validation-only operation intent; never executable CAM or "
            "physical cutting authorization."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "design_hash",
            "machine_profile_id",
            "machine_profile_version",
            "setups",
            "operations",
            "mode",
            "tool_catalog_version",
            "tool_catalog_fingerprint",
            "tools",
        ],
        "properties": {
            "schema_version": {"const": OPERATIONS_SCHEMA_VERSION},
            "design_hash": {"$ref": "#/$defs/sha256"},
            "machine_profile_id": {"$ref": "#/$defs/canonicalId"},
            "machine_profile_version": {"$ref": "#/$defs/nonEmptyString"},
            "setups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(setup_properties),
                    "properties": setup_properties,
                },
            },
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(operation_properties),
                    "properties": operation_properties,
                },
            },
            "mode": {"const": "VALIDATION"},
            "tool_catalog_version": {"$ref": "#/$defs/nonEmptyString"},
            "tool_catalog_fingerprint": {"$ref": "#/$defs/sha256"},
            "tools": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(tool_properties),
                    "properties": tool_properties,
                },
            },
        },
        "$defs": {
            "nonEmptyString": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
            },
            "canonicalId": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$",
            },
            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "point": {
                "type": "object",
                "additionalProperties": False,
                "required": ["x_um", "y_um"],
                "properties": {
                    "x_um": {"type": "integer"},
                    "y_um": {"type": "integer"},
                },
            },
        },
    }
    return canonical_json_bytes(schema)


def supplier_handoff_json_schema() -> bytes:
    """Publish the Draft 2020-12 schema for supplier-handoff v3."""

    operations_schema_sha256 = sha256_hex(operations_json_schema())
    artifact_entry = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "media_type", "role", "size_bytes", "sha256"],
        "properties": {
            "path": {"$ref": "#/$defs/nonEmptyString"},
            "media_type": {"$ref": "#/$defs/nonEmptyString"},
            "role": {"$ref": "#/$defs/nonEmptyString"},
            "size_bytes": {"type": "integer", "minimum": 0},
            "sha256": {"$ref": "#/$defs/sha256"},
        },
    }
    question_properties: dict[str, Any] = {
        "question_id": {"enum": [item[0] for item in SUPPLIER_ACCEPTANCE_QUESTIONS]},
        "question": {"$ref": "#/$defs/nonEmptyString"},
        "required_evidence": {"$ref": "#/$defs/nonEmptyString"},
        "answer": {"type": ["string", "null"]},
        "answered_by": {"type": ["string", "null"]},
        "answered_at": {"type": ["string", "null"]},
        "evidence_reference": {"type": ["string", "null"]},
        "status": {"const": "UNANSWERED"},
    }
    question_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": list(question_properties),
        "properties": question_properties,
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:custombuild:schema:supplier-handoff:v3",
        "title": "Custombuild CNC supplier handoff v3",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "package_identity",
            "package_contract",
            "manifest_context_binding",
            "payload_inventory_binding",
            "readiness",
            "dfm_review_warnings",
            "supplier_stages",
            "unresolved_inputs_and_decisions",
            "known_unresolved_decisions",
            "selected_validation_machine_profile",
            "stock_assumptions",
            "workshop_declaration_boundary",
            "operation_binding",
            "shop_acceptance_questions",
            "acceptance_rule",
        ],
        "properties": {
            "schema_version": {"const": SUPPLIER_HANDOFF_SCHEMA_VERSION},
            "package_identity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project_id", "revision", "design_hash"],
                "properties": {
                    "project_id": {"$ref": "#/$defs/nonEmptyString"},
                    "revision": {"$ref": "#/$defs/nonEmptyString"},
                    "design_hash": {"$ref": "#/$defs/sha256"},
                },
            },
            "package_contract": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "release_scope",
                    "machine_use",
                    "physical_cutting_authorized",
                    "signature_status",
                    "publisher_authenticity_provided",
                    "authenticity_boundary",
                    "manifest_path",
                    "checksum_algorithm",
                    "authoritative_inventory",
                    "inventory_fields",
                    "inventory_scope",
                    "machine_neutral_operations_contract",
                ],
                "properties": {
                    "release_scope": {"const": "DESIGN_REVIEW"},
                    "machine_use": {"const": "VALIDATION_ONLY"},
                    "physical_cutting_authorized": {"const": False},
                    "signature_status": {"const": "UNSIGNED"},
                    "publisher_authenticity_provided": {"const": False},
                    "authenticity_boundary": {"$ref": "#/$defs/nonEmptyString"},
                    "manifest_path": {"const": "manifest.json"},
                    "checksum_algorithm": {"const": "SHA-256"},
                    "authoritative_inventory": {"const": "manifest.json.artifacts"},
                    "inventory_fields": {
                        "const": ["path", "media_type", "role", "size_bytes", "sha256"]
                    },
                    "inventory_scope": {
                        "const": (
                            "ALL_PAYLOAD_FILES; MANIFEST_JSON_EXCLUDED_TO_AVOID_RECURSIVE_HASHING"
                        )
                    },
                    "machine_neutral_operations_contract": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "document_path",
                            "document_schema_version",
                            "json_schema_path",
                            "json_schema_draft",
                            "json_schema_sha256",
                            "purpose",
                            "executable_cam_provided",
                            "physical_cutting_authorized",
                        ],
                        "properties": {
                            "document_path": {"const": "cam/operations.json"},
                            "document_schema_version": {"const": OPERATIONS_SCHEMA_VERSION},
                            "json_schema_path": {"const": OPERATIONS_JSON_SCHEMA_PATH},
                            "json_schema_draft": {"const": JSON_SCHEMA_DRAFT_2020_12},
                            "json_schema_sha256": {"const": operations_schema_sha256},
                            "purpose": {"const": "MACHINE_NEUTRAL_VALIDATION_ONLY"},
                            "executable_cam_provided": {"const": False},
                            "physical_cutting_authorized": {"const": False},
                        },
                    },
                },
            },
            "manifest_context_binding": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "scope",
                    "excluded_fields",
                    "field_names",
                    "manifest_context_sha256",
                    "context",
                ],
                "properties": {
                    "scope": {"$ref": "#/$defs/nonEmptyString"},
                    "excluded_fields": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/nonEmptyString"},
                        "uniqueItems": True,
                    },
                    "field_names": {"const": list(SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS)},
                    "manifest_context_sha256": {"$ref": "#/$defs/sha256"},
                    "context": {"type": "object"},
                },
            },
            "payload_inventory_binding": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "scope",
                    "excluded_paths",
                    "artifact_count",
                    "payload_inventory_sha256",
                    "artifacts",
                ],
                "properties": {
                    "scope": {"$ref": "#/$defs/nonEmptyString"},
                    "excluded_paths": {"const": ["manifest.json", SUPPLIER_HANDOFF_PATH]},
                    "artifact_count": {"type": "integer", "minimum": 1},
                    "payload_inventory_sha256": {"$ref": "#/$defs/sha256"},
                    "artifacts": {"type": "array", "minItems": 1, "items": artifact_entry},
                },
            },
            "readiness": {"$ref": "#/$defs/object"},
            "dfm_review_warnings": {"type": "array", "items": {"type": "object"}},
            "supplier_stages": {
                "type": "object",
                "required": [
                    "available_for_quote_review",
                    "available_for_geometry_review",
                    "available_for_cam_intake_review",
                    "shop_review_required",
                    "manufacturing_approval_granted",
                    "cut_authorized",
                ],
                "properties": {
                    "available_for_quote_review": {"const": True},
                    "available_for_geometry_review": {"const": True},
                    "available_for_cam_intake_review": {"const": True},
                    "shop_review_required": {"const": True},
                    "manufacturing_approval_granted": {"const": False},
                    "cut_authorized": {"const": False},
                },
            },
            "unresolved_inputs_and_decisions": {
                "type": "array",
                "items": {"type": "object"},
            },
            "known_unresolved_decisions": {
                "type": "array",
                "items": {"type": "object"},
            },
            "selected_validation_machine_profile": {"$ref": "#/$defs/object"},
            "stock_assumptions": {"$ref": "#/$defs/object"},
            "workshop_declaration_boundary": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "stock_declaration_authorities",
                    "registration_authorities_in_operations",
                    "physical_verification_provided",
                    "cut_authorization_granted",
                    "purpose",
                ],
                "properties": {
                    "stock_declaration_authorities": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/nonEmptyString"},
                        "uniqueItems": True,
                    },
                    "registration_authorities_in_operations": {
                        "type": "array",
                        "items": {"const": "CLIENT_DECLARED"},
                        "uniqueItems": True,
                    },
                    "physical_verification_provided": {"const": False},
                    "cut_authorization_granted": {"const": False},
                    "purpose": {"const": "DETERMINISTIC_VALIDATION_INPUT_ONLY"},
                },
            },
            "operation_binding": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "status",
                    "document_path",
                    "document_sha256",
                    "schema_version",
                    "json_schema_path",
                    "json_schema_draft",
                    "json_schema_sha256",
                    "mode",
                    "tool_catalog_version",
                    "tool_catalog_fingerprint",
                    "setups",
                    "selected_tools",
                ],
                "properties": {
                    "status": {
                        "enum": [
                            "NOT_GENERATED",
                            "MACHINE_NEUTRAL_VALIDATION_ONLY",
                        ]
                    },
                    "document_path": {"enum": [None, "cam/operations.json"]},
                    "document_sha256": {
                        "type": ["string", "null"],
                        "pattern": "^[a-f0-9]{64}$",
                    },
                    "schema_version": {"enum": [None, OPERATIONS_SCHEMA_VERSION]},
                    "json_schema_path": {"const": OPERATIONS_JSON_SCHEMA_PATH},
                    "json_schema_draft": {"const": JSON_SCHEMA_DRAFT_2020_12},
                    "json_schema_sha256": {"const": operations_schema_sha256},
                    "mode": {"enum": [None, "VALIDATION"]},
                    "tool_catalog_version": {"type": ["string", "null"]},
                    "tool_catalog_fingerprint": {
                        "type": ["string", "null"],
                        "pattern": "^[a-f0-9]{64}$",
                    },
                    "setups": {"type": "array", "items": {"type": "object"}},
                    "selected_tools": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
            },
            "shop_acceptance_questions": {
                "type": "array",
                "minItems": 10,
                "maxItems": 10,
                "prefixItems": [
                    {
                        **question_schema,
                        "properties": {
                            **question_properties,
                            "question_id": {"const": question_id},
                            "question": {"const": question},
                            "required_evidence": {"const": evidence},
                        },
                    }
                    for question_id, question, evidence in SUPPLIER_ACCEPTANCE_QUESTIONS
                ],
                "items": False,
            },
            "acceptance_rule": {"$ref": "#/$defs/nonEmptyString"},
        },
        "$defs": {
            "nonEmptyString": {"type": "string", "minLength": 1},
            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "object": {"type": "object"},
        },
    }
    return canonical_json_bytes(schema)


def validate_json_schema_instance(instance: Any, schema: Mapping[str, Any]) -> None:
    """Validate one document with the deterministic JSON-Schema subset we publish."""

    root = schema

    def fail(path: str, message: str) -> NoReturn:
        raise ValueError(f"JSON Schema validation failed at {path}: {message}")

    def matches_type(value: Any, expected: str) -> bool:
        return {
            "object": isinstance(value, Mapping),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": type(value) is int,
            "boolean": type(value) is bool,
            "null": value is None,
        }.get(expected, False)

    def visit(value: Any, rule: Any, path: str, depth: int) -> None:
        if depth > 64:
            fail(path, "maximum validation depth exceeded")
        if rule is False:
            fail(path, "value is prohibited")
        if rule is True:
            return
        if not isinstance(rule, Mapping):
            fail(path, "schema rule is invalid")
        ref = rule.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                fail(path, "schema reference is unsupported")
            definition_name = ref.removeprefix("#/$defs/")
            definitions = root.get("$defs")
            if not isinstance(definitions, Mapping) or definition_name not in definitions:
                fail(path, "schema reference is unresolved")
            visit(value, definitions[definition_name], path, depth + 1)
            return
        if "const" in rule and value != rule["const"]:
            fail(path, "value does not match const")
        enum = rule.get("enum")
        if enum is not None and (not isinstance(enum, list) or value not in enum):
            fail(path, "value is outside enum")
        expected_type = rule.get("type")
        if expected_type is not None:
            accepted = [expected_type] if isinstance(expected_type, str) else expected_type
            if (
                not isinstance(accepted, list)
                or not accepted
                or any(not isinstance(item, str) for item in accepted)
                or not any(matches_type(value, item) for item in accepted)
            ):
                fail(path, "value has the wrong type")
        if isinstance(value, str):
            min_length = rule.get("minLength")
            if type(min_length) is int and len(value) < min_length:
                fail(path, "string is too short")
            max_length = rule.get("maxLength")
            if type(max_length) is int and len(value) > max_length:
                fail(path, "string is too long")
            pattern = rule.get("pattern")
            if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
                fail(path, "string does not match pattern")
        if type(value) is int:
            minimum = rule.get("minimum")
            if type(minimum) is int and value < minimum:
                fail(path, "integer is below minimum")
            maximum = rule.get("maximum")
            if type(maximum) is int and value > maximum:
                fail(path, "integer is above maximum")
        if isinstance(value, Mapping):
            required = rule.get("required", [])
            if not isinstance(required, list) or any(
                not isinstance(item, str) for item in required
            ):
                fail(path, "required declaration is invalid")
            missing = [item for item in required if item not in value]
            if missing:
                fail(path, f"required properties missing: {', '.join(missing)}")
            properties = rule.get("properties", {})
            if not isinstance(properties, Mapping):
                fail(path, "properties declaration is invalid")
            additional = rule.get("additionalProperties", True)
            extras = set(value) - set(properties)
            if additional is False and extras:
                fail(path, f"unexpected properties: {', '.join(sorted(map(str, extras)))}")
            for key, child in value.items():
                child_rule = properties.get(key, additional)
                if child_rule is not True:
                    visit(child, child_rule, f"{path}.{key}", depth + 1)
        if isinstance(value, list):
            min_items = rule.get("minItems")
            max_items = rule.get("maxItems")
            if type(min_items) is int and len(value) < min_items:
                fail(path, "array has too few items")
            if type(max_items) is int and len(value) > max_items:
                fail(path, "array has too many items")
            if rule.get("uniqueItems") is True:
                serialized = [canonical_json_bytes(item) for item in value]
                if len(serialized) != len(set(serialized)):
                    fail(path, "array items are not unique")
            prefix_items = rule.get("prefixItems", [])
            if not isinstance(prefix_items, list):
                fail(path, "prefixItems declaration is invalid")
            for index, child_rule in enumerate(prefix_items[: len(value)]):
                visit(value[index], child_rule, f"{path}[{index}]", depth + 1)
            items_rule = rule.get("items", True)
            for index in range(len(prefix_items), len(value)):
                if items_rule is not True:
                    visit(value[index], items_rule, f"{path}[{index}]", depth + 1)

    visit(instance, root, "$", 0)


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
                        {"x_um": point.x_um, "y_um": point.y_um} for point in feature.points()
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
            "unspecified_tolerance_feature_ids": sorted(set(unspecified_tolerance_feature_ids)),
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
    manifest_context = _canonical_supplier_manifest_context_projection(manifest_context_projection)
    payload_inventory = _canonical_supplier_payload_inventory(payload_inventory_entries)
    known_decision_codes = tuple(sorted(set(known_unresolved_decision_codes)))
    warning_rows = _canonical_supplier_dfm_warnings(dfm_warning_issues)
    operation_schema_bytes = operations_json_schema()
    operation_schema_sha256 = sha256_hex(operation_schema_bytes)
    operation_schema_entries = tuple(
        entry
        for entry in payload_inventory
        if str(entry["path"]).casefold() == OPERATIONS_JSON_SCHEMA_PATH.casefold()
        or str(entry["role"]).casefold() == JSON_SCHEMA_ROLE.casefold()
        and str(entry["path"]).casefold().endswith("operations.v2.schema.json")
    )
    if len(operation_schema_entries) != 1 or operation_schema_entries[0] != {
        "path": OPERATIONS_JSON_SCHEMA_PATH,
        "media_type": "application/schema+json",
        "role": JSON_SCHEMA_ROLE,
        "size_bytes": len(operation_schema_bytes),
        "sha256": operation_schema_sha256,
    }:
        raise ValueError("supplier handoff requires the canonical operations JSON Schema")

    operation_entries = tuple(
        entry
        for entry in payload_inventory
        if str(entry["path"]).casefold() == "cam/operations.json"
        or str(entry["role"]).casefold() == "machine_neutral_operations"
    )
    expected_operation_count = 0 if operations is None else 1
    if len(operation_entries) != expected_operation_count:
        raise ValueError("supplier handoff operation inventory does not match generation state")
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
            "document_sha256": None,
            "schema_version": None,
            "json_schema_path": OPERATIONS_JSON_SCHEMA_PATH,
            "json_schema_draft": JSON_SCHEMA_DRAFT_2020_12,
            "json_schema_sha256": operation_schema_sha256,
            "mode": None,
            "tool_catalog_version": None,
            "tool_catalog_fingerprint": None,
            "setups": [],
            "selected_tools": [],
        }
    else:
        operation_bytes = operations.to_json()
        try:
            validate_json_schema_instance(
                operations.as_dict(),
                json.loads(operation_schema_bytes),
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("supplier handoff operations do not conform to schema") from exc
        if operation_entries[0] != {
            "path": "cam/operations.json",
            "media_type": "application/json",
            "role": "MACHINE_NEUTRAL_OPERATIONS",
            "size_bytes": len(operation_bytes),
            "sha256": sha256_hex(operation_bytes),
        }:
            raise ValueError("supplier handoff operation artifact binding is invalid")
        operation_binding = {
            "status": "MACHINE_NEUTRAL_VALIDATION_ONLY",
            "document_path": "cam/operations.json",
            "document_sha256": sha256_hex(operation_bytes),
            "schema_version": operations.schema_version,
            "json_schema_path": OPERATIONS_JSON_SCHEMA_PATH,
            "json_schema_draft": JSON_SCHEMA_DRAFT_2020_12,
            "json_schema_sha256": operation_schema_sha256,
            "mode": operations.mode,
            "tool_catalog_version": operations.tool_catalog_version,
            "tool_catalog_fingerprint": operations.tool_catalog_fingerprint,
            "setups": canonical_data(operations.setups),
            "selected_tools": canonical_data(operations.tools),
        }

    registration_authorities = sorted(
        {
            match.group(1)
            for setup in (() if operations is None else operations.setups)
            if (
                match := re.search(
                    r"(?:^|;)DECLARATION_AUTHORITY=([^;]+)(?:;|$)",
                    setup.probe_method,
                )
            )
            is not None
        }
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
            "signature_status": "UNSIGNED",
            "publisher_authenticity_provided": False,
            "authenticity_boundary": (
                "SHA-256 detects in-package corruption but does not authenticate the publisher. "
                "Obtain the ZIP through the authenticated Custombuild download and verify its "
                "project, revision and design hash before relying on it."
            ),
            "manifest_path": "manifest.json",
            "checksum_algorithm": "SHA-256",
            "authoritative_inventory": "manifest.json.artifacts",
            "inventory_fields": ["path", "media_type", "role", "size_bytes", "sha256"],
            "inventory_scope": (
                "ALL_PAYLOAD_FILES; MANIFEST_JSON_EXCLUDED_TO_AVOID_RECURSIVE_HASHING"
            ),
            "machine_neutral_operations_contract": {
                "document_path": "cam/operations.json",
                "document_schema_version": OPERATIONS_SCHEMA_VERSION,
                "json_schema_path": OPERATIONS_JSON_SCHEMA_PATH,
                "json_schema_draft": JSON_SCHEMA_DRAFT_2020_12,
                "json_schema_sha256": operation_schema_sha256,
                "purpose": "MACHINE_NEUTRAL_VALIDATION_ONLY",
                "executable_cam_provided": False,
                "physical_cutting_authorized": False,
            },
        },
        "manifest_context_binding": {
            "scope": (
                "FINAL_MANIFEST_CONTEXT_EXCLUDING_ARTIFACT_INVENTORY_AND_DERIVED_MANIFEST_HASH"
            ),
            "excluded_fields": [
                "schema_version",
                "artifacts",
                "production_context_hash",
                "checksum_scope",
            ],
            "field_names": list(SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS),
            "manifest_context_sha256": sha256_hex(canonical_json_bytes(manifest_context)),
            "context": manifest_context,
        },
        "payload_inventory_binding": {
            "scope": (
                "CANONICAL_MANIFEST_ENTRY_PROJECTION_FOR_ALL_PAYLOADS_CREATED_BEFORE_"
                "SUPPLIER_HANDOFF; EXCLUDES_MANIFEST_JSON_AND_SUPPLIER_HANDOFF"
            ),
            "excluded_paths": ["manifest.json", SUPPLIER_HANDOFF_PATH],
            "artifact_count": len(payload_inventory),
            "payload_inventory_sha256": sha256_hex(canonical_json_bytes(payload_inventory)),
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
            "quote_review_scope": ("SUPPLIER_ESTIMATION_ONLY_SUBJECT_TO_ALL_NAMED_BLOCKERS"),
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
        "workshop_declaration_boundary": {
            "stock_declaration_authorities": sorted(
                {stock.declaration_authority for stock in stock_values}
            ),
            "registration_authorities_in_operations": registration_authorities,
            "physical_verification_provided": False,
            "cut_authorization_granted": False,
            "purpose": "DETERMINISTIC_VALIDATION_INPUT_ONLY",
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
            for question_id, question, evidence in SUPPLIER_ACCEPTANCE_QUESTIONS
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
