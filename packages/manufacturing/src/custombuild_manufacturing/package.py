"""Manifest, checksums and byte-reproducible production ZIP packages."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any, cast

from custombuild_cad import (
    FREECAD_BRIDGE_VERSION,
    FREECAD_PROJECT_CONTRACT_VERSION,
    CADArtifacts,
    CADDependencyUnavailable,
    CADExportError,
    CadQueryAdapter,
)
from custombuild_cam import backplot_svg
from custombuild_postprocessors import LinuxCNCValidationPostprocessor

from . import review_status as review_status_contract
from .artifact_limits import (
    MAX_ARTIFACT_BYTES,
    MAX_PRODUCTION_BUNDLE_BYTES,
    artifact_size_limit,
)
from .dfm import (
    DFM_ENGINE_VERSION,
    STOCK_PROFILE_MISSING_CODE,
    DFMValidator,
    stock_profile_missing_issue,
    validate_stock_profile_missing_issue,
)
from .errors import ArtifactError, ProductionBlockedError
from .exporters import (
    bom_csv,
    cut_list_csv,
    dxf_for_part,
    material_list_csv,
    nesting_svg,
    setup_sheet_svg,
    svg_for_part,
    tool_list_csv,
)
from .grain import (
    DFM_GRAIN_BLOCKER_CODE,
    DFM_GRAIN_STOCK_MATCHED_PHASE,
    DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE,
    stock_grain_binding_issues,
    validate_stock_grain_binding_issue,
)
from .model import (
    DFMIssue,
    DFMReport,
    MachineProfile,
    NestingLayout,
    OperationsDocument,
    PartSpec,
    Point2D,
    Rect,
    Severity,
    Side,
    StockSheet,
    canonical_json_bytes,
    sha256_hex,
)
from .nesting import DeterministicNester
from .operations import (
    OPERATIONS_ENGINE_VERSION,
    OPERATIONS_SCHEMA_VERSION,
    TwoSidedRegistration,
    generate_operations_document,
)
from .procurement import GROUPED_BOM_SCHEMA_VERSION, grouped_bom_json, stock_purchase_csv
from .profiles import (
    linuxcnc_reference_router_1325,
    linuxcnc_reference_router_5125,
    tool_catalog_fingerprint,
)
from .quality import (
    MANUFACTURING_INTENT_PATH,
    MANUFACTURING_INTENT_ROLE,
    SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS,
    SUPPLIER_HANDOFF_PATH,
    SUPPLIER_HANDOFF_ROLE,
    label_index_csv,
    manufacturing_intent_json,
    quality_measurement_plan_json,
    supplier_handoff_json,
)
from .readiness import (
    WorkshopReadinessReport,
    build_workshop_readiness_report,
    normalize_workshop_readiness_report,
    validate_workshop_evidence_binding,
)
from .review_status import (
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
    TWO_SIDED_REGISTRATION_MISSING_BLOCKER_CODE,
    CAMStageStatus,
    DesignReviewPackageStatus,
    normalize_design_review_package_status,
    validate_design_review_status_retention_binding,
)

MAX_PACKAGE_FILES = 10_000
MAX_ARTIFACT_SIZE_BYTES = MAX_ARTIFACT_BYTES
MAX_PACKAGE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000
PACKAGE_BUILDER_VERSION = "deterministic-package-1.6.0"
PRODUCTION_MANIFEST_SCHEMA_VERSION = "custombuild.production-manifest.v4"
ARTIFACT_SCHEMA_VERSION = "custombuild.production-artifacts.v1"
GENERATION_PLAN_SCHEMA_VERSION = "custombuild.generation-plan.v1"
GENERATION_PLAN_PIPELINE_VERSION = "production-pipeline-1.10.0"
NESTING_ALGORITHM_VERSION = "deterministic-bottom-left-v1"
MANIFEST_CONTEXT_HASH_FIELDS = (
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
    "artifacts",
)
if MANIFEST_CONTEXT_HASH_FIELDS[:-1] != SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS:
    raise RuntimeError("manifest and supplier-handoff context fields drifted")
_MANIFEST_CHECKSUM_SCOPE = "all payload files; manifest.json excluded to avoid recursive hashing"
_MANIFEST_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        *MANIFEST_CONTEXT_HASH_FIELDS,
        "production_context_hash",
        "checksum_scope",
    }
)
_MANIFEST_ARTIFACT_ENTRY_KEYS = frozenset({"path", "media_type", "role", "size_bytes", "sha256"})
_PACKAGE_CAD_STATUSES = ("GENERATED", "NOT_REQUESTED")
_BLOCKED_CAM_ALLOWED_FIXED_ARTIFACTS = frozenset(
    {
        ("assembly/assembly-manual.pdf", "ASSEMBLY_REVIEW_MANUAL", "application/pdf"),
        ("assembly/assembly-readiness.json", "ASSEMBLY_READINESS", "application/json"),
        ("bom/bom.csv", "BOM", "text/csv"),
        ("bom/bom.pdf", "BOM_PDF", "application/pdf"),
        ("bom/grouped-bom.json", "GROUPED_BOM", "application/json"),
        ("bom/hardware-list.csv", "HARDWARE_LIST", "text/csv"),
        ("cut-list/cut-list.csv", "CUT_LIST", "text/csv"),
        ("design/design-spec.json", "FROZEN_DESIGN_SPEC", "application/json"),
        ("design/result-summary.json", "DESIGN_RESULT_SUMMARY", "application/json"),
        ("labels/part-labels.pdf", "PART_LABELS", "application/pdf"),
        ("materials/material-list.csv", "MATERIAL_LIST", "text/csv"),
        (
            MANUFACTURING_INTENT_PATH,
            MANUFACTURING_INTENT_ROLE,
            "application/json",
        ),
        ("model/design.fcstd", "NON_AUTHORITATIVE_FREECAD_PROJECT", "application/vnd.freecad"),
        ("model/design.glb", "WEB_PREVIEW_GLB", "model/gltf-binary"),
        ("model/design.step", "AUTHORITATIVE_STEP", "model/step"),
        ("qa/measurement-protocol.pdf", "QA_PROTOCOL", "application/pdf"),
        (
            "validation/cad-interchange-status.json",
            "CAD_INTERCHANGE_STATUS",
            "application/json",
        ),
        (
            "validation/generation-plan.json",
            "GENERATION_PLAN",
            "application/json",
        ),
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
        (
            DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
            DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
            "application/json",
        ),
        (
            "validation/stock-selection.json",
            "STOCK_SELECTION_SNAPSHOT",
            "application/json",
        ),
        ("validation/dfm-report.json", "DFM_VALIDATION_REPORT", "application/json"),
        ("validation/source-provenance.json", "SOURCE_PROVENANCE", "application/json"),
        (
            "validation/workshop-readiness.json",
            "WORKSHOP_READINESS_REPORT",
            "application/json",
        ),
        (SUPPLIER_HANDOFF_PATH, SUPPLIER_HANDOFF_ROLE, "application/json"),
    }
)
_STATUS_REVIEW_REQUIRED_ARTIFACTS = (
    ("bom/bom.csv", "BOM", "text/csv"),
    ("bom/grouped-bom.json", "GROUPED_BOM", "application/json"),
    ("cut-list/cut-list.csv", "CUT_LIST", "text/csv"),
    ("design/design-spec.json", "FROZEN_DESIGN_SPEC", "application/json"),
    ("design/result-summary.json", "DESIGN_RESULT_SUMMARY", "application/json"),
    ("materials/material-list.csv", "MATERIAL_LIST", "text/csv"),
    (
        MANUFACTURING_INTENT_PATH,
        MANUFACTURING_INTENT_ROLE,
        "application/json",
    ),
    ("model/design.glb", "WEB_PREVIEW_GLB", "model/gltf-binary"),
    ("model/design.step", "AUTHORITATIVE_STEP", "model/step"),
    ("validation/cad-interchange-status.json", "CAD_INTERCHANGE_STATUS", "application/json"),
    ("validation/generation-plan.json", "GENERATION_PLAN", "application/json"),
    ("validation/dfm-report.json", "DFM_VALIDATION_REPORT", "application/json"),
    (
        "validation/stock-selection.json",
        "STOCK_SELECTION_SNAPSHOT",
        "application/json",
    ),
    ("validation/workshop-readiness.json", "WORKSHOP_READINESS_REPORT", "application/json"),
    (SUPPLIER_HANDOFF_PATH, SUPPLIER_HANDOFF_ROLE, "application/json"),
)
_WORKSHOP_READINESS_ARTIFACT_PATH = "validation/workshop-readiness.json"
_WORKSHOP_READINESS_ARTIFACT_ROLE = "WORKSHOP_READINESS_REPORT"
_DFM_REPORT_ARTIFACT_PATH = "validation/dfm-report.json"
_DFM_REPORT_ARTIFACT_ROLE = "DFM_VALIDATION_REPORT"
GENERATION_PLAN_ARTIFACT_PATH = "validation/generation-plan.json"
GENERATION_PLAN_ARTIFACT_ROLE = "GENERATION_PLAN"
_GENERATION_PLAN_ARTIFACT_PATH = GENERATION_PLAN_ARTIFACT_PATH
_GENERATION_PLAN_ARTIFACT_ROLE = GENERATION_PLAN_ARTIFACT_ROLE
_PERSISTED_PACKAGE_ARTIFACT_KINDS = {
    "manifest.json": "manifest",
    MANUFACTURING_INTENT_PATH: "manufacturing_intent",
    SUPPLIER_HANDOFF_PATH: "supplier_handoff",
    _WORKSHOP_READINESS_ARTIFACT_PATH: "workshop_readiness",
    _DFM_REPORT_ARTIFACT_PATH: "dfm_report",
    DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH: "design_review_package_status",
    "validation/stock-selection.json": "stock_selection",
    _GENERATION_PLAN_ARTIFACT_PATH: "generation_plan",
    "cam/operations.json": "operations",
    "validation/source-provenance.json": "source_provenance",
    "validation/cad-interchange-status.json": "cad_interchange_status",
    "assembly/assembly-readiness.json": "assembly_readiness",
}
_DFM_REPORT_KEYS = frozenset({"engine_version", "issues"})
_DFM_ISSUE_KEYS = frozenset(
    {
        "code",
        "severity",
        "message",
        "part_id",
        "feature_id",
        "setup_id",
        "inputs",
        "suggestion",
    }
)
STOCK_SELECTION_SCHEMA_VERSION = "custombuild.stock-selection.v1"
FROZEN_DESIGN_SPEC_SCHEMA_VERSION = "custombuild.frozen-design-spec.v1"
DESIGN_RESULT_SUMMARY_SCHEMA_VERSION = "custombuild.design-result-summary.v1"
_SAFE_CALLER_ADDITIONAL_ARTIFACTS = frozenset(
    {
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
    }
)


def stock_selection_artifact(
    stocks: Iterable[StockSheet],
    grouped_parts: Iterable[tuple[StockSheet, tuple[PartSpec, ...]]],
    *,
    unmatched_part_ids: Iterable[str],
) -> ArtifactFile:
    """Freeze the exact stock candidates and deterministic part assignment.

    The review package remains non-authoritative for cutting, but a grain issue
    may not name an opaque or invented stock. This checksum-bound snapshot is
    the package-local fact to which missing-stock and grain findings bind.
    """

    stock_values = tuple(sorted(stocks, key=lambda item: item.stock_id))
    assignment_values = tuple(sorted(grouped_parts, key=lambda item: item[0].stock_id))
    payload = {
        "schema_version": STOCK_SELECTION_SCHEMA_VERSION,
        "stocks": [
            {
                "stock_id": stock.stock_id,
                "material_id": stock.material_id,
                "material_version": stock.material_version,
                "width_um": stock.width_um,
                "height_um": stock.height_um,
                "thickness_um": stock.thickness_um,
                "quantity": stock.quantity,
                "margin_um": stock.margin_um,
                "kerf_um": stock.kerf_um,
                "grain_direction": str(stock.grain_direction).strip().upper(),
                "allow_rotation": stock.allow_rotation,
                "defect_zones": stock.defect_zones,
                "clamp_zones": stock.clamp_zones,
            }
            for stock in stock_values
        ],
        "assignments": [
            {
                "stock_id": stock.stock_id,
                "part_ids": sorted(part.part_id for part in parts),
            }
            for stock, parts in assignment_values
        ],
        "unmatched_part_ids": sorted(set(unmatched_part_ids)),
    }
    return ArtifactFile(
        "validation/stock-selection.json",
        canonical_json_bytes(payload),
        "application/json",
        "STOCK_SELECTION_SNAPSHOT",
    )


def machine_profile_fingerprint(machine: MachineProfile) -> str:
    """Return the canonical fingerprint of every frozen machine/tool value."""

    return sha256_hex(canonical_json_bytes(machine))


def stock_profiles_fingerprint(stocks: Iterable[StockSheet]) -> str:
    """Bind every supplied stock field, including quantity, axis and zones."""

    return sha256_hex(canonical_json_bytes(tuple(sorted(stocks, key=lambda stock: stock.stock_id))))


def generation_plan_artifact(
    *,
    machine: MachineProfile,
    stocks: Iterable[StockSheet],
    two_sided_registration_by_stock: (Mapping[str, Mapping[int, TwoSidedRegistration]] | None),
    validation_program_requested: bool,
) -> ArtifactFile:
    """Freeze only generation inputs that are not derivable from DesignSpec/stock.

    This is deliberately not an output attestation.  The package reader resolves
    the built-in machine profile from its ID/version, verifies its full
    fingerprint and feeds these caller-declared registration coordinates back
    into deterministic nesting/operations/postprocessing.
    """

    if type(validation_program_requested) is not bool:
        raise ValueError("validation_program_requested must be a boolean")
    stock_values = tuple(stocks)
    stock_by_id = {stock.stock_id: stock for stock in stock_values}
    raw_registrations = two_sided_registration_by_stock or {}
    if not isinstance(raw_registrations, Mapping):
        raise ValueError("two-sided registrations must be a mapping")

    registration_rows: list[dict[str, Any]] = []
    for stock_id in sorted(raw_registrations):
        if not isinstance(stock_id, str) or stock_id not in stock_by_id:
            raise ValueError("two-sided registration references unknown stock")
        sheets = raw_registrations[stock_id]
        if not isinstance(sheets, Mapping):
            raise ValueError("two-sided registration sheets must be a mapping")
        stock = stock_by_id[stock_id]
        sheet_rows: list[dict[str, Any]] = []
        for sheet_index in sorted(sheets):
            plan = sheets[sheet_index]
            if (
                type(sheet_index) is not int
                or not 0 <= sheet_index < stock.quantity
                or not isinstance(plan, TwoSidedRegistration)
            ):
                raise ValueError("two-sided registration sheet identity is invalid")
            method_id = plan.method_id
            coordinates = tuple((point.x_um, point.y_um) for point in plan.points)
            if (
                not isinstance(method_id, str)
                or method_id != method_id.strip()
                or not method_id
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
                    for character in method_id
                )
                or len(coordinates) < 2
                or len(set(coordinates)) != len(coordinates)
                or any(
                    type(x_um) is not int
                    or type(y_um) is not int
                    or not 0 <= x_um <= stock.width_um
                    or not 0 <= y_um <= stock.height_um
                    for x_um, y_um in coordinates
                )
            ):
                raise ValueError("two-sided registration plan is invalid")
            sheet_rows.append(
                {
                    "sheet_index": sheet_index,
                    "method_id": method_id,
                    "points": [{"x_um": x_um, "y_um": y_um} for x_um, y_um in coordinates],
                }
            )
        if sheet_rows:
            registration_rows.append({"stock_id": stock_id, "sheets": sheet_rows})

    postprocessor = LinuxCNCValidationPostprocessor()
    payload = {
        "schema_version": GENERATION_PLAN_SCHEMA_VERSION,
        "pipeline_version": GENERATION_PLAN_PIPELINE_VERSION,
        "nesting_algorithm": NESTING_ALGORITHM_VERSION,
        "operations_schema_version": OPERATIONS_SCHEMA_VERSION,
        "operations_engine_version": OPERATIONS_ENGINE_VERSION,
        "machine_profile": {
            "id": machine.profile_id,
            "version": machine.version,
            "fingerprint": machine_profile_fingerprint(machine),
        },
        "stock_profiles_fingerprint": stock_profiles_fingerprint(stock_values),
        "postprocessor": {
            "id": "linuxcnc-validation",
            "version": postprocessor.version,
        },
        "validation_program_requested": validation_program_requested,
        "two_sided_registrations": registration_rows,
    }
    return ArtifactFile(
        _GENERATION_PLAN_ARTIFACT_PATH,
        canonical_json_bytes(payload),
        "application/json",
        _GENERATION_PLAN_ARTIFACT_ROLE,
    )


@dataclass(frozen=True, slots=True)
class _GroupedBOMTruth:
    edge_band_selection_required: bool
    material_grain_binding_required: bool
    part_grain_axis_by_id: Mapping[str, str]
    part_material_by_id: Mapping[str, tuple[str, str]]
    grouped_part_ids_by_id: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _StockSelectionTruth:
    stocks_by_id: Mapping[str, StockSheet]
    assigned_stock_by_part_id: Mapping[str, str]
    unmatched_part_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReviewCoreTruth:
    design: Any
    parts: tuple[PartSpec, ...]


@dataclass(frozen=True, slots=True)
class _GenerationPlanTruth:
    machine: MachineProfile
    validation_program_requested: bool
    registrations_by_stock: Mapping[str, Mapping[int, TwoSidedRegistration]]


_MANIFEST_STRING_CONTEXT_FIELDS = (
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
    "rule_version",
    "joint_version",
    "postprocessor_version",
    "generation_context_hash",
    "artifact_schema_version",
    "cad_status",
    "release_scope",
    "machine_use",
)


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    path: str
    data: bytes
    media_type: str
    role: str

    def __post_init__(self) -> None:
        _validate_artifact_path(self.path)
        if not isinstance(self.data, bytes):
            raise TypeError("artifact data must be bytes")


@dataclass(frozen=True, slots=True)
class ManifestContext:
    project_id: str
    revision: str
    design_hash: str
    app_version: str
    engine_version: str
    template_version: str
    template_id: str
    template_capability_fingerprint: str
    template_capability: Mapping[str, Any]
    rule_version: str
    material_versions: tuple[str, ...]
    joint_version: str
    machine_profile_id: str
    machine_profile_version: str
    postprocessor_version: str
    cad_status: str
    generation_context_hash: str
    production_engine_context: Mapping[str, Any]
    approved_assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    overrides: tuple[Mapping[str, Any], ...] = ()
    external_evidence: tuple[Mapping[str, Any], ...] = ()
    source_provenance: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if len(self.generation_context_hash) != 64:
            raise ValueError("manifest requires a 64-character generation context hash")
        if not self.production_engine_context:
            raise ValueError("manifest requires the frozen production engine context")
        if len(self.template_capability_fingerprint) != 64:
            raise ValueError("manifest requires a 64-character template capability fingerprint")
        if not self.template_capability:
            raise ValueError("manifest requires the frozen template capability snapshot")
        if (
            self.template_capability.get("capability_fingerprint")
            != self.template_capability_fingerprint
        ):
            raise ValueError("template capability snapshot fingerprint mismatch")
        if self.source_provenance is not None:
            source = self.source_provenance
            if source.get("source") != "reference_image":
                raise ValueError("manifest source provenance type is unsupported")
            if not isinstance(source.get("import_id"), str) or len(source["import_id"]) != 36:
                raise ValueError("manifest source provenance requires an import ID")
            for field in ("image_sha256", "verified_model_fingerprint"):
                value = source.get(field)
                if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                    raise ValueError(f"manifest source provenance requires {field}")


def supplier_handoff_manifest_context(context: ManifestContext) -> dict[str, Any]:
    """Project one frozen builder context without recursive artifact identities."""

    capability_version = context.template_capability.get("template_version")
    if not isinstance(capability_version, str) or not capability_version:
        raise ValueError("manifest requires a frozen template capability version")
    capability_registry_version = context.production_engine_context.get(
        "template_capability_registry_version"
    )
    if not isinstance(capability_registry_version, str) or not capability_registry_version:
        raise ValueError("manifest requires a frozen template capability registry version")
    projection = {
        "project_id": context.project_id,
        "revision": context.revision,
        "design_hash": context.design_hash,
        "app_version": context.app_version,
        "engine_version": context.engine_version,
        # ``template_version`` remains as a compatibility alias in schema v4.
        "template_version": context.template_version,
        "domain_template_version": context.template_version,
        "template_capability_version": capability_version,
        "template_capability_registry_version": capability_registry_version,
        "template_id": context.template_id,
        "template_capability_fingerprint": context.template_capability_fingerprint,
        "template_capability": context.template_capability,
        "rule_version": context.rule_version,
        "material_versions": sorted(context.material_versions),
        "joint_version": context.joint_version,
        "machine_profile": {
            "id": context.machine_profile_id,
            "version": context.machine_profile_version,
        },
        "postprocessor_version": context.postprocessor_version,
        "generation_context_hash": context.generation_context_hash,
        "production_engine_context": context.production_engine_context,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "cad_status": context.cad_status,
        "release_scope": "design_review",
        "machine_use": "validation_only",
        "physical_cutting_authorized": False,
        "approved_assumptions": sorted(context.approved_assumptions),
        "warnings": sorted(context.warnings),
        "overrides": list(context.overrides),
        "external_evidence": list(context.external_evidence),
        "source_provenance": context.source_provenance,
    }
    return projection


def default_artifacts(
    *,
    parts: Iterable[PartSpec],
    layout: NestingLayout | Iterable[NestingLayout],
    operations: OperationsDocument,
    project_id: str | None = None,
    revision: str | None = None,
    design_hash: str | None = None,
    additional: Iterable[ArtifactFile] = (),
) -> tuple[ArtifactFile, ...]:
    part_values = tuple(parts)
    layouts = (layout,) if isinstance(layout, NestingLayout) else tuple(layout)
    files = list(
        design_review_artifacts(
            parts=part_values,
            project_id=project_id,
            revision=revision,
            design_hash=design_hash,
        )
    )
    files.extend(
        (
            ArtifactFile(
                "materials/stock-purchase.csv",
                stock_purchase_csv(layouts),
                "text/csv",
                "STOCK_PURCHASE_SCHEDULE",
            ),
            ArtifactFile(
                "cam/operations.json",
                operations.to_json(),
                "application/json",
                "MACHINE_NEUTRAL_OPERATIONS",
            ),
            ArtifactFile("cam/tool-list.csv", tool_list_csv(operations), "text/csv", "TOOL_LIST"),
            ArtifactFile(
                "labels/label-index.csv",
                label_index_csv(parts=part_values, layouts=layouts, operations=operations),
                "text/csv",
                "LABEL_INDEX",
            ),
            ArtifactFile(
                "quality/measurement-plan.json",
                quality_measurement_plan_json(
                    parts=part_values,
                    layouts=layouts,
                    operations=operations,
                ),
                "application/json",
                "QUALITY_MEASUREMENT_PLAN",
            ),
        )
    )
    for setup in sorted(operations.setups, key=lambda item: item.setup_id):
        files.append(
            ArtifactFile(
                f"cam/setups/{safe_component(setup.setup_id)}.svg",
                setup_sheet_svg(setup, operations),
                "image/svg+xml",
                "SETUP_SHEET",
            )
        )
    for current_layout in sorted(layouts, key=lambda item: item.stock.stock_id):
        stock_component = safe_component(current_layout.stock.stock_id)
        for sheet_index in range(current_layout.used_sheet_count):
            files.append(
                ArtifactFile(
                    f"nesting/{stock_component}/sheet-{sheet_index + 1:03d}.svg",
                    nesting_svg(current_layout, sheet_index),
                    "image/svg+xml",
                    "NESTING_MAP",
                )
            )
    files.extend(additional)
    return tuple(sorted(files, key=lambda item: item.path))


def design_review_artifacts(
    *,
    parts: Iterable[PartSpec],
    project_id: str | None = None,
    revision: str | None = None,
    design_hash: str | None = None,
    additional: Iterable[ArtifactFile] = (),
) -> tuple[ArtifactFile, ...]:
    """Build the machine-independent core of a design-review package.

    This deliberately contains no nesting, operations, tool list, setup sheet,
    backplot or controller program.  Those artifacts may only be added by the
    validated CAM branch in :func:`default_artifacts`.
    """

    part_values = tuple(parts)
    identity_values = (project_id, revision, design_hash)
    if any(value is not None for value in identity_values) and not all(
        value is not None for value in identity_values
    ):
        raise ValueError("manufacturing intent identity must be supplied as one complete set")
    files: list[ArtifactFile] = [
        ArtifactFile("bom/bom.csv", bom_csv(part_values), "text/csv", "BOM"),
        ArtifactFile(
            "bom/grouped-bom.json",
            grouped_bom_json(part_values),
            "application/json",
            "GROUPED_BOM",
        ),
        ArtifactFile("cut-list/cut-list.csv", cut_list_csv(part_values), "text/csv", "CUT_LIST"),
        ArtifactFile(
            "materials/material-list.csv",
            material_list_csv(part_values),
            "text/csv",
            "MATERIAL_LIST",
        ),
    ]
    if all(value is not None for value in identity_values):
        assert project_id is not None
        assert revision is not None
        assert design_hash is not None
        files.append(
            ArtifactFile(
                MANUFACTURING_INTENT_PATH,
                manufacturing_intent_json(
                    parts=part_values,
                    project_id=project_id,
                    revision=revision,
                    design_hash=design_hash,
                ),
                "application/json",
                MANUFACTURING_INTENT_ROLE,
            )
        )
    for part in sorted(part_values, key=lambda item: item.part_id):
        component = safe_component(part.part_id)
        for side in (Side.A, Side.B):
            files.append(
                ArtifactFile(
                    f"parts/{component}/{side.value}.dxf",
                    dxf_for_part(part, side),
                    "image/vnd.dxf",
                    "PART_DXF",
                )
            )
            files.append(
                ArtifactFile(
                    f"drawings/{component}/{side.value}.svg",
                    svg_for_part(part, side),
                    "image/svg+xml",
                    "PART_DRAWING",
                )
            )
    files.extend(additional)
    return tuple(sorted(files, key=lambda item: item.path))


def build_manifest(
    context: ManifestContext,
    artifacts: Iterable[ArtifactFile],
) -> bytes:
    files = tuple(sorted(artifacts, key=lambda item: item.path))
    _validate_unique_paths(files)
    artifact_entries = [
        {
            "path": artifact.path,
            "media_type": artifact.media_type,
            "role": artifact.role,
            "size_bytes": len(artifact.data),
            "sha256": sha256_hex(artifact.data),
        }
        for artifact in files
    ]
    production_context = {
        **supplier_handoff_manifest_context(context),
        "artifacts": artifact_entries,
    }
    manifest = {
        "schema_version": PRODUCTION_MANIFEST_SCHEMA_VERSION,
        **production_context,
        "production_context_hash": sha256_hex(canonical_json_bytes(production_context)),
        "checksum_scope": _MANIFEST_CHECKSUM_SCOPE,
    }
    return canonical_json_bytes(manifest)


def build_deterministic_zip(
    context: ManifestContext,
    artifacts: Iterable[ArtifactFile],
    *,
    production_release: bool = False,
) -> bytes:
    files = tuple(sorted(artifacts, key=lambda item: item.path))
    _validate_unique_paths(files)
    if production_release:
        _validate_release_artifacts(context, files)
    manifest = build_manifest(context, files)

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path, payload in [
            ("manifest.json", manifest),
            *((item.path, item.data) for item in files),
        ]:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0x800
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def read_and_verify_package(payload: bytes) -> dict[str, Any]:
    """Re-parse a package, reject unsafe paths and verify every manifest hash."""

    if type(payload) is not bytes or not payload or len(payload) > MAX_PRODUCTION_BUNDLE_BYTES:
        raise ArtifactError("production ZIP is empty or exceeds its canonical size limit")

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), mode="r")
    except zipfile.BadZipFile as exc:
        raise ArtifactError("invalid production ZIP") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PACKAGE_FILES:
            raise ArtifactError("production ZIP contains too many files")
        total_size = 0
        for info in infos:
            if info.is_dir() or info.flag_bits & 0x1:
                raise ArtifactError("production ZIP contains a directory or encrypted entry")
            if info.create_system != 3 or info.external_attr != 0o100644 << 16:
                raise ArtifactError("production ZIP contains a non-regular or non-canonical entry")
            artifact_kind = _PERSISTED_PACKAGE_ARTIFACT_KINDS.get(info.filename, "")
            if re.fullmatch(r"cam/setups/[^/]+\.svg", info.filename):
                artifact_kind = "setup_sheet_001"
            if info.file_size > min(
                MAX_ARTIFACT_SIZE_BYTES,
                artifact_size_limit(artifact_kind),
            ):
                raise ArtifactError(f"production ZIP entry is too large: {info.filename}")
            total_size += info.file_size
            if total_size > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                raise ArtifactError("production ZIP uncompressed size exceeds the safety limit")
            if info.file_size and (
                info.compress_size == 0
                or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise ArtifactError(f"unsafe compression ratio: {info.filename}")

        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ArtifactError("production ZIP contains duplicate paths")
        for name in names:
            _validate_artifact_path(name)
        try:
            manifest_bytes = archive.read("manifest.json")
            manifest_value = json.loads(manifest_bytes)
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ArtifactError("package has no valid manifest.json") from exc
        if not isinstance(manifest_value, dict):
            raise ArtifactError("package manifest must be a JSON object")
        manifest: dict[str, Any] = {str(key): value for key, value in manifest_value.items()}
        if frozenset(manifest) != _MANIFEST_TOP_LEVEL_KEYS:
            raise ArtifactError("production manifest has an unexpected structure")
        if manifest.get("schema_version") != PRODUCTION_MANIFEST_SCHEMA_VERSION:
            raise ArtifactError("unsupported production manifest schema")
        cad_status = manifest.get("cad_status")
        if (
            manifest.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION
            or manifest.get("release_scope") != "design_review"
            or manifest.get("machine_use") != "validation_only"
            or manifest.get("physical_cutting_authorized") is not False
            or not isinstance(cad_status, str)
            or cad_status not in _PACKAGE_CAD_STATUSES
            or manifest.get("checksum_scope") != _MANIFEST_CHECKSUM_SCOPE
        ):
            raise ArtifactError("production manifest has unsafe or unsupported claims")
        validate_manifest_context_contract(manifest)
        entries = manifest.get("artifacts")
        if not isinstance(entries, list):
            raise ArtifactError("manifest artifacts must be an array")
        artifact_entries = list(validate_manifest_artifact_entries(entries))
        artifact_paths: list[str] = []
        for entry in artifact_entries:
            path = entry["path"]
            size_bytes = entry["size_bytes"]
            digest = entry["sha256"]
            artifact_paths.append(path)
            try:
                data = archive.read(path)
            except KeyError as exc:
                raise ArtifactError(f"manifest artifact missing from ZIP: {path}") from exc
            if len(data) != size_bytes or sha256_hex(data) != digest:
                raise ArtifactError(f"artifact checksum mismatch: {path}")
        if set(names) != {"manifest.json", *artifact_paths}:
            raise ArtifactError("production ZIP contains files outside the manifest")
        try:
            context_payload = {field: manifest[field] for field in MANIFEST_CONTEXT_HASH_FIELDS}
        except KeyError as exc:
            raise ArtifactError(f"manifest context field missing: {exc.args[0]}") from exc
        try:
            expected_context_hash = sha256_hex(canonical_json_bytes(context_payload))
            canonical_manifest = canonical_json_bytes(manifest)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ArtifactError("production manifest is not canonical JSON") from exc
        if manifest.get("production_context_hash") != expected_context_hash:
            raise ArtifactError("manifest production_context_hash mismatch")
        if manifest_bytes != canonical_manifest:
            raise ArtifactError("manifest.json is not canonical UTF-8 JSON")
        status_entries = [
            entry
            for entry in artifact_entries
            if entry["path"].casefold() == DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH.casefold()
            or entry["role"].casefold() == DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE.casefold()
        ]
        if len(status_entries) != 1:
            raise ArtifactError(
                "schema-v4 production packages require one canonical design-review status"
            )
        review_core_truth = _validate_review_core_semantics(
            archive,
            artifact_entries,
            manifest=manifest,
        )
        readiness = _validate_workshop_readiness_artifact(archive, artifact_entries)
        grouped_bom_truth = _design_requirements_from_grouped_bom(
            archive,
            artifact_entries,
            required=True,
        )
        assert grouped_bom_truth is not None
        stock_selection_truth = _stock_selection_truth(
            archive,
            artifact_entries,
            canonical_parts=review_core_truth.parts,
        )
        generation_plan_truth = _generation_plan_truth(
            archive,
            artifact_entries,
            manifest=manifest,
            stock_selection_truth=stock_selection_truth,
        )
        _validate_cad_semantics(
            archive,
            artifact_entries,
            design=review_core_truth.design,
        )
        if readiness is not None:
            try:
                edge_band_selection_required, material_grain_binding_required = (
                    grouped_bom_truth.edge_band_selection_required,
                    grouped_bom_truth.material_grain_binding_required,
                )
                validate_workshop_evidence_binding(
                    readiness,
                    expected_edge_band_selection_required=edge_band_selection_required,
                    external_evidence=manifest["external_evidence"],
                    expected_material_grain_binding_required=(material_grain_binding_required),
                )
            except (TypeError, ValueError) as exc:
                raise ArtifactError(
                    "workshop readiness does not match manifest external evidence"
                ) from exc
        _validate_design_review_status_inventory(
            archive,
            artifact_entries,
            cad_status=cad_status,
            readiness=readiness,
            grouped_bom_truth=grouped_bom_truth,
            stock_selection_truth=stock_selection_truth,
            generation_plan_truth=generation_plan_truth,
            canonical_parts=review_core_truth.parts,
            canonical_design=review_core_truth.design,
            manifest=manifest,
        )
        return manifest


def _canonical_artifact_entry(
    entries: Iterable[Mapping[str, Any]],
    *,
    path: str,
    role: str,
    media_type: str,
    role_unique: bool = True,
) -> Mapping[str, Any]:
    candidates = [
        entry
        for entry in entries
        if str(entry["path"]).casefold() == path.casefold()
        or (role_unique and str(entry["role"]).casefold() == role.casefold())
    ]
    if len(candidates) != 1:
        raise ArtifactError(f"{role} artifact entry is not unique")
    entry = candidates[0]
    if (entry["path"], entry["role"], entry["media_type"]) != (
        path,
        role,
        media_type,
    ):
        raise ArtifactError(f"{role} artifact entry is not canonical")
    return entry


def _validate_review_core_semantics(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any],
) -> _ReviewCoreTruth:
    """Rebuild every product projection from the checksum-bound DesignSpec.

    A schema-v4 ZIP is self-contained but unsigned. Rehashing the entire archive
    can therefore create a different package; within one package, however, no
    BOM, drawing or summary may drift from its frozen parametric source.
    """

    entry_values = tuple(entries)
    _canonical_artifact_entry(
        entry_values,
        path="design/design-spec.json",
        role="FROZEN_DESIGN_SPEC",
        media_type="application/json",
    )
    spec_payload = _strict_canonical_json_object(
        archive.read("design/design-spec.json"),
        label="frozen DesignSpec",
    )
    if (
        frozenset(spec_payload) != {"schema_version", "spec"}
        or spec_payload.get("schema_version") != FROZEN_DESIGN_SPEC_SCHEMA_VERSION
        or not isinstance(spec_payload.get("spec"), Mapping)
    ):
        raise ArtifactError("frozen DesignSpec has an unsupported structure")
    try:
        from custombuild_domain import BookcaseDesignSpec, build_bookcase

        from .adapters import adapt_design_result

        design = build_bookcase(BookcaseDesignSpec.model_validate(spec_payload["spec"]))
        adapted = adapt_design_result(cast(Any, design))
    except (TypeError, ValueError) as exc:
        raise ArtifactError("frozen DesignSpec cannot be rebuilt canonically") from exc

    if (
        manifest.get("design_hash") != adapted.design_hash
        or manifest.get("engine_version") != adapted.engine_version
        or manifest.get("template_version") != adapted.template_version
        or manifest.get("domain_template_version") != adapted.template_version
    ):
        raise ArtifactError("manifest identity does not match the rebuilt frozen DesignSpec")

    parts = tuple(adapted.parts)
    expected_core = {
        artifact.path: artifact
        for artifact in design_review_artifacts(
            parts=parts,
            project_id=str(manifest["project_id"]),
            revision=str(manifest["revision"]),
            design_hash=str(manifest["design_hash"]),
        )
    }
    semantic_roles = {
        "BOM",
        "GROUPED_BOM",
        "CUT_LIST",
        "MATERIAL_LIST",
        MANUFACTURING_INTENT_ROLE,
        "PART_DXF",
        "PART_DRAWING",
    }
    actual_semantic_paths = {
        str(entry["path"])
        for entry in entry_values
        if entry["role"] in semantic_roles or str(entry["path"]).startswith(("parts/", "drawings/"))
    }
    if actual_semantic_paths != set(expected_core):
        raise ArtifactError(
            "review-core BOM and A/B part drawing inventory does not match the frozen DesignSpec"
        )
    for path, expected in expected_core.items():
        _canonical_artifact_entry(
            entry_values,
            path=path,
            role=expected.role,
            media_type=expected.media_type,
            role_unique=expected.role not in {"PART_DXF", "PART_DRAWING"},
        )
        if archive.read(path) != expected.data:
            raise ArtifactError(f"review-core artifact differs from frozen DesignSpec: {path}")

    spec_bytes = archive.read("design/design-spec.json")
    part_ids = tuple(sorted(part.part_id for part in design.parts))
    joint_ids = tuple(sorted(joint.joint_id for joint in design.joints))
    expected_summary = {
        "schema_version": DESIGN_RESULT_SUMMARY_SCHEMA_VERSION,
        "design_hash": adapted.design_hash,
        "design_spec": {
            "path": "design/design-spec.json",
            "sha256": sha256_hex(spec_bytes),
            "schema_version": FROZEN_DESIGN_SPEC_SCHEMA_VERSION,
        },
        "engine_version": adapted.engine_version,
        "domain_template_version": adapted.template_version,
        "part_count": len(part_ids),
        "part_ids": list(part_ids),
        "joint_count": len(joint_ids),
        "joint_ids": list(joint_ids),
        "assembly_step_count": len(design.assembly_graph.steps),
        "total_weight_g": design.total_weight_g,
    }
    _canonical_artifact_entry(
        entry_values,
        path="design/result-summary.json",
        role="DESIGN_RESULT_SUMMARY",
        media_type="application/json",
    )
    summary_payload = _strict_canonical_json_object(
        archive.read("design/result-summary.json"),
        label="design result summary",
    )
    if summary_payload != expected_summary:
        raise ArtifactError("design result summary does not match the frozen DesignSpec")

    expected_material_versions = sorted(
        {f"{part.material_id}@{part.material_version}" for part in parts}
    )
    if manifest.get("material_versions") != expected_material_versions:
        raise ArtifactError("manifest material versions do not match the canonical BOM")
    return _ReviewCoreTruth(design=design, parts=parts)


def _validate_cad_semantics(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
    *,
    design: Any,
) -> None:
    entry_values = tuple(entries)
    _canonical_artifact_entry(
        entry_values,
        path="model/design.step",
        role="AUTHORITATIVE_STEP",
        media_type="model/step",
    )
    _canonical_artifact_entry(
        entry_values,
        path="model/design.glb",
        role="WEB_PREVIEW_GLB",
        media_type="model/gltf-binary",
    )
    _canonical_artifact_entry(
        entry_values,
        path="validation/cad-interchange-status.json",
        role="CAD_INTERCHANGE_STATUS",
        media_type="application/json",
    )

    step = archive.read("model/design.step")
    glb = archive.read("model/design.glb")
    try:
        artifacts = CADArtifacts(
            step=step,
            glb=glb,
            kernel="package-reopen",
            adapter_version="package-reopen",
        )
        CadQueryAdapter().validate_design_artifacts(design, artifacts)
    except (CADDependencyUnavailable, CADExportError, TypeError, ValueError) as exc:
        raise ArtifactError("authoritative STEP/GLB do not match the frozen DesignSpec") from exc

    status = _strict_canonical_json_object(
        archive.read("validation/cad-interchange-status.json"),
        label="CAD interchange status",
    )
    common_expected = {
        "requested": False,
        "runtime_probe_performed": False,
        "bridge_version": FREECAD_BRIDGE_VERSION,
        "contract_version": FREECAD_PROJECT_CONTRACT_VERSION,
        "authoritative_geometry": False,
        "authoritative_source": "model/design.step",
        "mode": "optional_downstream_interchange",
        "machine_authorization": False,
    }
    fcstd_candidates = [
        entry
        for entry in entry_values
        if str(entry["path"]).casefold() == "model/design.fcstd"
        or str(entry["role"]).casefold() == "non_authoritative_freecad_project"
    ]
    if fcstd_candidates:
        _canonical_artifact_entry(
            entry_values,
            path="model/design.fcstd",
            role="NON_AUTHORITATIVE_FREECAD_PROJECT",
            media_type="application/vnd.freecad",
        )
        if len(archive.read("model/design.fcstd")) == 0:
            raise ArtifactError("non-authoritative FreeCAD project is empty")
        expected_keys = {*common_expected, "status", "runtime_version", "source_step_sha256"}
        if (
            frozenset(status) != expected_keys
            or status.get("status") != "GENERATED"
            or status.get("requested") is not True
            or status.get("runtime_probe_performed") is not True
            or not isinstance(status.get("runtime_version"), str)
            or not status["runtime_version"]
            or status.get("source_step_sha256") != sha256_hex(step)
            or any(
                status.get(key) != value
                for key, value in common_expected.items()
                if key not in {"requested", "runtime_probe_performed"}
            )
        ):
            raise ArtifactError("CAD interchange status does not match the FreeCAD artifact")
    else:
        expected = {"status": "OPTIONAL_NOT_REQUESTED", **common_expected}
        if status != expected:
            raise ArtifactError("CAD interchange status is not the canonical optional state")


def _stock_selection_truth(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
    *,
    canonical_parts: tuple[PartSpec, ...],
) -> _StockSelectionTruth:
    entry_values = tuple(entries)
    _canonical_artifact_entry(
        entry_values,
        path="validation/stock-selection.json",
        role="STOCK_SELECTION_SNAPSHOT",
        media_type="application/json",
    )
    payload = _strict_canonical_json_object(
        archive.read("validation/stock-selection.json"),
        label="stock selection snapshot",
    )
    if frozenset(payload) != {
        "schema_version",
        "stocks",
        "assignments",
        "unmatched_part_ids",
    }:
        raise ArtifactError("stock selection snapshot has an unexpected structure")
    raw_stocks = payload.get("stocks")
    raw_assignments = payload.get("assignments")
    raw_unmatched = payload.get("unmatched_part_ids")
    if (
        payload.get("schema_version") != STOCK_SELECTION_SCHEMA_VERSION
        or not isinstance(raw_stocks, list)
        or not raw_stocks
        or not isinstance(raw_assignments, list)
        or not isinstance(raw_unmatched, list)
    ):
        raise ArtifactError("stock selection snapshot contract is invalid")

    def parse_zones(value: Any, *, label: str) -> tuple[Rect, ...]:
        if not isinstance(value, list):
            raise ArtifactError(f"stock selection {label} must be an array")
        zones: list[Rect] = []
        for item in value:
            if not isinstance(item, Mapping) or frozenset(item) != {
                "x_um",
                "y_um",
                "width_um",
                "height_um",
            }:
                raise ArtifactError(f"stock selection {label} has an invalid zone")
            if any(type(item[field]) is not int for field in item):
                raise ArtifactError(f"stock selection {label} zone dimensions are invalid")
            if (
                item["x_um"] < 0
                or item["y_um"] < 0
                or item["width_um"] <= 0
                or item["height_um"] <= 0
            ):
                raise ArtifactError(f"stock selection {label} zone dimensions are invalid")
            try:
                zones.append(
                    Rect(
                        int(item["x_um"]),
                        int(item["y_um"]),
                        int(item["width_um"]),
                        int(item["height_um"]),
                    )
                )
            except ValueError as exc:
                raise ArtifactError(f"stock selection {label} zone is invalid") from exc
        return tuple(zones)

    stocks: list[StockSheet] = []
    stock_row_keys = {
        "stock_id",
        "material_id",
        "material_version",
        "width_um",
        "height_um",
        "thickness_um",
        "quantity",
        "margin_um",
        "kerf_um",
        "grain_direction",
        "allow_rotation",
        "defect_zones",
        "clamp_zones",
    }
    supported_axes = {"X", "Y", "UNBOUND", "NONE", "ANY", "UNSPECIFIED", "UNKNOWN"}
    for row in raw_stocks:
        if not isinstance(row, Mapping) or frozenset(row) != stock_row_keys:
            raise ArtifactError("stock selection stock row is invalid")
        string_fields = ("stock_id", "material_id", "material_version", "grain_direction")
        if any(
            not isinstance(row[field], str) or not row[field] or row[field] != row[field].strip()
            for field in string_fields
        ):
            raise ArtifactError("stock selection stock identity is invalid")
        if row["grain_direction"] not in supported_axes:
            raise ArtifactError("stock selection stock grain direction is unsupported")
        integer_fields = (
            "width_um",
            "height_um",
            "thickness_um",
            "quantity",
            "margin_um",
            "kerf_um",
        )
        if (
            any(type(row[field]) is not int for field in integer_fields)
            or type(row["allow_rotation"]) is not bool
        ):
            raise ArtifactError("stock selection stock dimensions are invalid")
        try:
            stock = StockSheet(
                stock_id=str(row["stock_id"]),
                material_id=str(row["material_id"]),
                material_version=str(row["material_version"]),
                width_um=int(row["width_um"]),
                height_um=int(row["height_um"]),
                thickness_um=int(row["thickness_um"]),
                quantity=int(row["quantity"]),
                margin_um=int(row["margin_um"]),
                kerf_um=int(row["kerf_um"]),
                grain_direction=str(row["grain_direction"]),
                allow_rotation=bool(row["allow_rotation"]),
                defect_zones=parse_zones(row["defect_zones"], label="defect zones"),
                clamp_zones=parse_zones(row["clamp_zones"], label="clamp zones"),
            )
            sheet_bounds = Rect(0, 0, stock.width_um, stock.height_um)
            if any(
                not sheet_bounds.contains(zone)
                for zone in (*stock.defect_zones, *stock.clamp_zones)
            ):
                raise ArtifactError("stock selection zone exceeds its stock sheet")
            stocks.append(stock)
        except ValueError as exc:
            raise ArtifactError("stock selection stock row is invalid") from exc
    stock_ids = [stock.stock_id for stock in stocks]
    if stock_ids != sorted(set(stock_ids)):
        raise ArtifactError("stock selection stocks are not canonically ordered and unique")
    stocks_by_id = {stock.stock_id: stock for stock in stocks}

    assigned_stock_by_part_id: dict[str, str] = {}
    assignment_stock_ids: list[str] = []
    for row in raw_assignments:
        if not isinstance(row, Mapping) or frozenset(row) != {"stock_id", "part_ids"}:
            raise ArtifactError("stock selection assignment row is invalid")
        stock_id = row["stock_id"]
        part_ids = row["part_ids"]
        if (
            not isinstance(stock_id, str)
            or stock_id not in stocks_by_id
            or not isinstance(part_ids, list)
            or not part_ids
            or any(
                not isinstance(part_id, str) or not part_id or part_id != part_id.strip()
                for part_id in part_ids
            )
            or part_ids != sorted(set(part_ids))
        ):
            raise ArtifactError("stock selection assignment identity is invalid")
        assignment_stock_ids.append(stock_id)
        for part_id in part_ids:
            if part_id in assigned_stock_by_part_id:
                raise ArtifactError("stock selection assigns one part more than once")
            assigned_stock_by_part_id[part_id] = stock_id
    if assignment_stock_ids != sorted(set(assignment_stock_ids)):
        raise ArtifactError("stock selection assignments are not canonically ordered and unique")
    if any(
        not isinstance(part_id, str) or not part_id or part_id != part_id.strip()
        for part_id in raw_unmatched
    ) or raw_unmatched != sorted(set(raw_unmatched)):
        raise ArtifactError("stock selection unmatched part IDs are not canonical")

    part_by_id = {part.part_id: part for part in canonical_parts}
    represented_ids = set(assigned_stock_by_part_id) | set(raw_unmatched)
    if represented_ids != set(part_by_id) or set(assigned_stock_by_part_id).intersection(
        raw_unmatched
    ):
        raise ArtifactError("stock selection does not partition the canonical BOM")

    nester = DeterministicNester()
    expected_assignment: dict[str, str] = {}
    expected_unmatched: list[str] = []
    for part_id, part in sorted(part_by_id.items()):
        compatible = [
            stock
            for stock in stocks
            if stock.material_id == part.material_id
            and stock.material_version == part.material_version
            and stock.thickness_um == part.thickness_um
            and nester.nest((replace(part, quantity=1, grain_direction="NONE"),), stock).is_complete
        ]
        if compatible:
            expected_assignment[part_id] = min(
                compatible,
                key=lambda stock: (stock.width_um * stock.height_um, stock.stock_id),
            ).stock_id
        else:
            expected_unmatched.append(part_id)
    if assigned_stock_by_part_id != expected_assignment or tuple(raw_unmatched) != tuple(
        expected_unmatched
    ):
        raise ArtifactError("stock selection snapshot does not match deterministic selection")
    return _StockSelectionTruth(
        stocks_by_id=stocks_by_id,
        assigned_stock_by_part_id=assigned_stock_by_part_id,
        unmatched_part_ids=tuple(raw_unmatched),
    )


def _reference_machine_profile(profile_id: str, version: str) -> MachineProfile:
    candidates = (
        linuxcnc_reference_router_1325(),
        linuxcnc_reference_router_5125(),
    )
    matches = tuple(
        machine
        for machine in candidates
        if machine.profile_id == profile_id and machine.version == version
    )
    if len(matches) != 1:
        raise ArtifactError("generation plan references an unsupported machine profile")
    return matches[0]


def _generation_plan_truth(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any],
    stock_selection_truth: _StockSelectionTruth,
) -> _GenerationPlanTruth:
    _canonical_artifact_entry(
        entries,
        path=_GENERATION_PLAN_ARTIFACT_PATH,
        role=_GENERATION_PLAN_ARTIFACT_ROLE,
        media_type="application/json",
    )
    payload = _strict_canonical_json_object(
        archive.read(_GENERATION_PLAN_ARTIFACT_PATH),
        label="generation plan",
    )
    if frozenset(payload) != {
        "schema_version",
        "pipeline_version",
        "nesting_algorithm",
        "operations_schema_version",
        "operations_engine_version",
        "machine_profile",
        "stock_profiles_fingerprint",
        "postprocessor",
        "validation_program_requested",
        "two_sided_registrations",
    }:
        raise ArtifactError("generation plan has an unexpected structure")
    if (
        payload.get("schema_version") != GENERATION_PLAN_SCHEMA_VERSION
        or payload.get("pipeline_version") != GENERATION_PLAN_PIPELINE_VERSION
        or payload.get("nesting_algorithm") != NESTING_ALGORITHM_VERSION
        or payload.get("operations_schema_version") != OPERATIONS_SCHEMA_VERSION
        or payload.get("operations_engine_version") != OPERATIONS_ENGINE_VERSION
        or type(payload.get("validation_program_requested")) is not bool
    ):
        raise ArtifactError("generation plan version contract is invalid")

    machine_payload = payload.get("machine_profile")
    manifest_machine = manifest.get("machine_profile")
    if (
        not isinstance(machine_payload, Mapping)
        or frozenset(machine_payload) != {"id", "version", "fingerprint"}
        or not isinstance(manifest_machine, Mapping)
        or frozenset(manifest_machine) != {"id", "version"}
        or machine_payload.get("id") != manifest_machine.get("id")
        or machine_payload.get("version") != manifest_machine.get("version")
    ):
        raise ArtifactError("generation plan machine identity does not match the manifest")
    profile_id = machine_payload["id"]
    profile_version = machine_payload["version"]
    if not isinstance(profile_id, str) or not isinstance(profile_version, str):
        raise ArtifactError("generation plan machine identity is invalid")
    machine = _reference_machine_profile(profile_id, profile_version)
    if machine_payload.get("fingerprint") != machine_profile_fingerprint(machine):
        raise ArtifactError("generation plan machine fingerprint is not canonical")
    if payload.get("stock_profiles_fingerprint") != stock_profiles_fingerprint(
        stock_selection_truth.stocks_by_id.values()
    ):
        raise ArtifactError("generation plan stock fingerprint does not match stock selection")

    postprocessor_payload = payload.get("postprocessor")
    postprocessor = LinuxCNCValidationPostprocessor()
    if (
        not isinstance(postprocessor_payload, Mapping)
        or frozenset(postprocessor_payload) != {"id", "version"}
        or postprocessor_payload.get("id") != "linuxcnc-validation"
        or postprocessor_payload.get("version") != postprocessor.version
        or manifest.get("postprocessor_version") != postprocessor.version
    ):
        raise ArtifactError("generation plan postprocessor identity is invalid")

    raw_registrations = payload.get("two_sided_registrations")
    if not isinstance(raw_registrations, list):
        raise ArtifactError("generation plan registrations must be an array")
    registrations: dict[str, dict[int, TwoSidedRegistration]] = {}
    stock_ids: list[str] = []
    for stock_row in raw_registrations:
        if not isinstance(stock_row, Mapping) or frozenset(stock_row) != {
            "stock_id",
            "sheets",
        }:
            raise ArtifactError("generation plan registration stock row is invalid")
        stock_id = stock_row.get("stock_id")
        raw_sheets = stock_row.get("sheets")
        if (
            not isinstance(stock_id, str)
            or stock_id not in stock_selection_truth.stocks_by_id
            or not isinstance(raw_sheets, list)
            or not raw_sheets
        ):
            raise ArtifactError("generation plan registration stock identity is invalid")
        stock_ids.append(stock_id)
        stock = stock_selection_truth.stocks_by_id[stock_id]
        sheets: dict[int, TwoSidedRegistration] = {}
        sheet_indices: list[int] = []
        for sheet_row in raw_sheets:
            if not isinstance(sheet_row, Mapping) or frozenset(sheet_row) != {
                "sheet_index",
                "method_id",
                "points",
            }:
                raise ArtifactError("generation plan registration sheet row is invalid")
            sheet_index = sheet_row.get("sheet_index")
            method_id = sheet_row.get("method_id")
            raw_points = sheet_row.get("points")
            if (
                type(sheet_index) is not int
                or not 0 <= sheet_index < stock.quantity
                or not isinstance(method_id, str)
                or not method_id
                or method_id != method_id.strip()
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
                    for character in method_id
                )
                or not isinstance(raw_points, list)
                or len(raw_points) < 2
            ):
                raise ArtifactError("generation plan registration sheet identity is invalid")
            points: list[Point2D] = []
            for raw_point in raw_points:
                if not isinstance(raw_point, Mapping) or frozenset(raw_point) != {"x_um", "y_um"}:
                    raise ArtifactError("generation plan registration point is invalid")
                x_um = raw_point.get("x_um")
                y_um = raw_point.get("y_um")
                if (
                    type(x_um) is not int
                    or type(y_um) is not int
                    or not 0 <= x_um <= stock.width_um
                    or not 0 <= y_um <= stock.height_um
                ):
                    raise ArtifactError("generation plan registration point is outside stock")
                points.append(Point2D(x_um, y_um))
            if len({(point.x_um, point.y_um) for point in points}) != len(points):
                raise ArtifactError("generation plan registration points are not unique")
            sheet_indices.append(sheet_index)
            sheets[sheet_index] = TwoSidedRegistration(method_id, tuple(points))
        if sheet_indices != sorted(set(sheet_indices)):
            raise ArtifactError("generation plan registration sheets are not canonical")
        registrations[stock_id] = sheets
    if stock_ids != sorted(set(stock_ids)):
        raise ArtifactError("generation plan registration stocks are not canonical")

    return _GenerationPlanTruth(
        machine=machine,
        validation_program_requested=payload["validation_program_requested"],
        registrations_by_stock=registrations,
    )


def blocked_cam_artifact_violation(
    path: str,
    role: str,
    media_type: str,
) -> str | None:
    """Reject anything outside the exact CAM-blocked design-review inventory.

    A denylist is unsafe here: a caller can rename a toolpath, setup or operation
    plan and retain the same machine-authoritative meaning. This boundary is
    therefore deliberately case-sensitive and requires one canonical path,
    role and media-type triple. The only dynamic paths are the two review sides
    for a builder-generated, idempotently safe part component.
    """

    if not all(isinstance(value, str) for value in (path, role, media_type)):
        return "artifact path, role and media type must be strings"
    identity = (path, role, media_type)
    if identity in _BLOCKED_CAM_ALLOWED_FIXED_ARTIFACTS:
        return None
    dynamic_match = re.fullmatch(
        r"(?P<namespace>parts|drawings)/(?P<component>[^/]+)/(?P<side>A|B)"
        r"(?P<suffix>\.dxf|\.svg)",
        path,
    )
    if dynamic_match is not None:
        namespace = dynamic_match.group("namespace")
        component = dynamic_match.group("component")
        suffix = dynamic_match.group("suffix")
        expected = (
            ("parts", ".dxf", "PART_DXF", "image/vnd.dxf"),
            ("drawings", ".svg", "PART_DRAWING", "image/svg+xml"),
        )
        if (
            safe_component(component) == component
            and (namespace, suffix, role, media_type) in expected
        ):
            return None
    return (
        "artifact is not allowed in a CAM-blocked design-review package: "
        f"{path} ({role}; {media_type})"
    )


def caller_additional_artifact_violation(
    path: str,
    role: str,
    media_type: str,
) -> str | None:
    """Allow only exact machine-independent document identities from callers."""

    if not all(isinstance(value, str) for value in (path, role, media_type)):
        return "artifact path, role and media type must be strings"
    if (path, role, media_type) in _SAFE_CALLER_ADDITIONAL_ARTIFACTS:
        return None
    return (
        "caller artifact is not in the machine-independent document allowlist: "
        f"{path} ({role}; {media_type})"
    )


def validate_manifest_artifact_entries(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Validate the pure manifest-entry boundary shared by ZIP and API readers."""

    validated: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise ArtifactError("manifest artifact entry must be an object")
        entry = dict(raw_entry)
        if frozenset(entry) != _MANIFEST_ARTIFACT_ENTRY_KEYS:
            raise ArtifactError("manifest artifact entry has an unexpected structure")
        path = entry["path"]
        media_type = entry["media_type"]
        role = entry["role"]
        size_bytes = entry["size_bytes"]
        digest = entry["sha256"]
        if not isinstance(path, str):
            raise ArtifactError("manifest artifact path must be a string")
        _validate_artifact_path(path)
        if (
            not isinstance(media_type, str)
            or not media_type
            or not isinstance(role, str)
            or not role
        ):
            raise ArtifactError("manifest artifact media_type and role must be non-blank strings")
        if type(size_bytes) is not int or size_bytes < 0:
            raise ArtifactError("manifest artifact size_bytes must be a non-negative integer")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ArtifactError("manifest artifact sha256 must be a lowercase digest")
        validated.append(entry)

    paths = [entry["path"] for entry in validated]
    if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
        raise ArtifactError("manifest contains duplicate artifact paths")
    if paths != sorted(paths):
        raise ArtifactError("manifest artifact paths are not in canonical order")
    return tuple(validated)


def validate_design_review_status_inventory_entries(
    status: DesignReviewPackageStatus | None,
    entries: Iterable[Mapping[str, Any]],
) -> None:
    """Validate a mandatory review status against authenticated inventory."""

    entry_values = tuple(entries)
    for entry in entry_values:
        if any(
            not isinstance(entry.get(field), str) or not entry[field]
            for field in ("path", "role", "media_type")
        ):
            raise ArtifactError("design-review inventory entry identity is invalid")

    if status is None:
        raise ArtifactError("schema-v4 production package status is mandatory")
    _validate_status_review_core(entry_values)
    if status.cam_status is CAMStageStatus.BLOCKED:
        violations = [
            blocked_cam_artifact_violation(
                str(entry["path"]),
                str(entry["role"]),
                str(entry["media_type"]),
            )
            for entry in entry_values
        ]
        present = tuple(reason for reason in violations if reason is not None)
        if present:
            raise ArtifactError(
                "blocked CAM package contains manufacturing artifacts: " + "; ".join(present)
            )
        return
    _validate_generated_cam_inventory(status, entry_values)


def _validate_design_review_status_inventory(
    archive: zipfile.ZipFile,
    entries: list[dict[str, Any]],
    *,
    cad_status: str,
    readiness: WorkshopReadinessReport | None,
    grouped_bom_truth: _GroupedBOMTruth | None,
    stock_selection_truth: _StockSelectionTruth,
    generation_plan_truth: _GenerationPlanTruth,
    canonical_parts: tuple[PartSpec, ...],
    canonical_design: Any,
    manifest: Mapping[str, Any],
) -> None:
    """Bind an optional versioned review-status document to the ZIP inventory."""

    status_path = DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH
    status_role = DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE
    status_candidates = [
        entry
        for entry in entries
        if entry["path"].casefold() == status_path.casefold()
        or entry["role"].casefold() == status_role.casefold()
    ]
    if not status_candidates:
        raise ArtifactError("schema-v4 production package status is mandatory")
    if len(status_candidates) != 1:
        raise ArtifactError("design-review package status entry is not unique")
    status_entry = status_candidates[0]
    if (
        status_entry["path"] != status_path
        or status_entry["role"] != status_role
        or status_entry["media_type"] != "application/json"
    ):
        raise ArtifactError("design-review package status entry is not canonical")
    if cad_status != "GENERATED":
        raise ArtifactError("design-review package status requires generated authoritative CAD")
    status_payload = archive.read(status_path)
    status_value = _strict_canonical_json_object(
        status_payload,
        label="design-review package status",
    )
    try:
        status = normalize_design_review_package_status(status_value)
        validate_design_review_status_retention_binding(status, canonical_design)
    except ValueError as exc:
        raise ArtifactError(
            "design-review package status is invalid or contradicts frozen retention"
        ) from exc

    validate_design_review_status_inventory_entries(status, entries)
    dfm_report = _validate_dfm_report_artifact(archive, entries)
    validate_design_review_status_dfm_report(status, dfm_report)
    _validate_stock_and_grain_report_binding(
        status,
        dfm_report,
        stock_selection_truth=stock_selection_truth,
        canonical_parts=canonical_parts,
    )
    if status.blocker_codes in {
        (DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,),
        (BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,),
        (TWO_SIDED_REGISTRATION_MISSING_BLOCKER_CODE,),
    }:
        rebuilt_report, _ = _rebuild_complete_stock_dfm(
            canonical_parts=canonical_parts,
            stock_selection_truth=stock_selection_truth,
            machine=generation_plan_truth.machine,
        )
        if canonical_json_bytes(rebuilt_report) != canonical_json_bytes(dfm_report):
            raise ArtifactError(
                "blocked CAM DFM report differs from deterministic reconstruction"
            )
    if grouped_bom_truth is not None:
        _validate_grain_issues_against_grouped_bom(dfm_report, grouped_bom_truth)
    if (
        grouped_bom_truth is not None
        and not grouped_bom_truth.material_grain_binding_required
        and (
            status.blocker_codes == (DFM_GRAIN_BLOCKER_CODE,)
            or any(issue.code == DFM_GRAIN_BLOCKER_CODE for issue in dfm_report.issues)
        )
    ):
        raise ArtifactError("grain DFM issue or status contradicts the non-directional grouped BOM")
    if readiness is None:
        raise ArtifactError("design-review package status requires workshop readiness")
    _validate_status_readiness(status, readiness, authoritative_cad_verified=True)

    operations: OperationsDocument | None = None
    generated_artifacts: tuple[ArtifactFile, ...] = ()
    if status.cam_status is CAMStageStatus.VALIDATION_GENERATED:
        operations, generated_artifacts = _validate_generated_package_semantics(
            archive,
            status=status,
            dfm_report=dfm_report,
            canonical_parts=canonical_parts,
            stock_selection_truth=stock_selection_truth,
            generation_plan_truth=generation_plan_truth,
            entries=entries,
        )
    _validate_complete_package_inventory(
        archive,
        entries,
        canonical_parts=canonical_parts,
        generated_artifacts=generated_artifacts,
        manifest=manifest,
    )

    assert grouped_bom_truth is not None
    expected_readiness = build_workshop_readiness_report(
        authoritative_cad=True,
        dfm_passed=not dfm_report.blocking_issues,
        operation_count=len(operations.operations) if operations is not None else 0,
        setup_count=len(operations.setups) if operations is not None else 0,
        validation_backplot=operations is not None,
        validation_program=(
            operations is not None and generation_plan_truth.validation_program_requested
        ),
        edge_band_selection_required=grouped_bom_truth.edge_band_selection_required,
        material_grain_binding_required=grouped_bom_truth.material_grain_binding_required,
        external_evidence=cast(list[Mapping[str, Any]], manifest["external_evidence"]),
    )
    if readiness.as_dict() != expected_readiness.as_dict():
        raise ArtifactError(
            "workshop readiness text and evidence do not match deterministic reconstruction"
        )
    _validate_supplier_handoff_artifact(
        archive,
        entries,
        manifest=manifest,
        machine=generation_plan_truth.machine,
        stocks=stock_selection_truth.stocks_by_id.values(),
        operations=operations,
        status=status,
        readiness=readiness,
        canonical_design=canonical_design,
        dfm_report=dfm_report,
    )


def _validate_supplier_handoff_artifact(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any],
    machine: MachineProfile,
    stocks: Iterable[StockSheet],
    operations: OperationsDocument | None,
    status: DesignReviewPackageStatus,
    readiness: WorkshopReadinessReport,
    canonical_design: Any,
    dfm_report: DFMReport,
) -> None:
    """Rebuild the supplier cover sheet from already verified package truth."""

    entry_values = tuple(entries)
    _canonical_artifact_entry(
        entry_values,
        path=SUPPLIER_HANDOFF_PATH,
        role=SUPPLIER_HANDOFF_ROLE,
        media_type="application/json",
    )
    expected = supplier_handoff_json(
        project_id=str(manifest["project_id"]),
        revision=str(manifest["revision"]),
        design_hash=str(manifest["design_hash"]),
        machine=machine,
        stocks=stocks,
        operations=operations,
        cam_status=status.cam_status.value,
        blocker_codes=status.blocker_codes,
        cam_required_action=status.required_action,
        design_review_ready=readiness.design_review_ready,
        manifest_context_projection={
            field: manifest[field]
            for field in SUPPLIER_HANDOFF_MANIFEST_CONTEXT_FIELDS
        },
        payload_inventory_entries=(
            entry for entry in entry_values if entry["path"] != SUPPLIER_HANDOFF_PATH
        ),
        known_unresolved_decision_codes=tuple(
            sorted(
                code
                for code, unresolved in (
                    (
                        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
                        review_status_contract.dado_retention_evidence_missing(
                            canonical_design
                        ),
                    ),
                    (
                        BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
                        review_status_contract.back_panel_retention_evidence_missing(
                            canonical_design
                        ),
                    ),
                )
                if unresolved
            )
        ),
        dfm_warning_issues=(
            issue for issue in dfm_report.issues if issue.severity is Severity.WARNING
        ),
    )
    if archive.read(SUPPLIER_HANDOFF_PATH) != expected:
        raise ArtifactError(
            "supplier handoff does not match package identity, assumptions and inventory"
        )


def _validate_workshop_readiness_artifact(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
) -> WorkshopReadinessReport | None:
    candidates = [
        entry
        for entry in entries
        if str(entry["path"]).casefold() == _WORKSHOP_READINESS_ARTIFACT_PATH.casefold()
        or str(entry["role"]).casefold() == _WORKSHOP_READINESS_ARTIFACT_ROLE.casefold()
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ArtifactError("workshop readiness artifact entry is not unique")
    entry = candidates[0]
    if (
        entry["path"] != _WORKSHOP_READINESS_ARTIFACT_PATH
        or entry["role"] != _WORKSHOP_READINESS_ARTIFACT_ROLE
        or entry["media_type"] != "application/json"
    ):
        raise ArtifactError("workshop readiness artifact entry is not canonical")
    value = _strict_canonical_json_object(
        archive.read(_WORKSHOP_READINESS_ARTIFACT_PATH),
        label="workshop readiness",
    )
    try:
        return normalize_workshop_readiness_report(value)
    except ValueError as exc:
        raise ArtifactError("workshop readiness artifact is invalid") from exc


def normalize_design_review_dfm_report(payload: Mapping[str, Any]) -> DFMReport:
    """Strictly parse the checksum-bound DFM report used by review packages."""

    if not isinstance(payload, Mapping) or frozenset(payload) != _DFM_REPORT_KEYS:
        raise ValueError("design-review DFM report has an unexpected structure")
    if payload.get("engine_version") != DFM_ENGINE_VERSION:
        raise ValueError("design-review DFM report engine version is unsupported")
    raw_issues = payload.get("issues")
    if not isinstance(raw_issues, list):
        raise ValueError("design-review DFM report issues must be an array")
    issues: list[DFMIssue] = []
    for raw_issue in raw_issues:
        if not isinstance(raw_issue, Mapping) or frozenset(raw_issue) != _DFM_ISSUE_KEYS:
            raise ValueError("design-review DFM issue has an unexpected structure")
        code = raw_issue.get("code")
        message = raw_issue.get("message")
        if not isinstance(code, str) or not code or not isinstance(message, str) or not message:
            raise ValueError("design-review DFM issue identity is invalid")
        severity_value = raw_issue.get("severity")
        if not isinstance(severity_value, str):
            raise ValueError("design-review DFM issue severity is invalid")
        try:
            severity = Severity(severity_value)
        except ValueError as exc:
            raise ValueError("design-review DFM issue severity is invalid") from exc
        optional_ids: dict[str, str | None] = {}
        for field in ("part_id", "feature_id", "setup_id"):
            value = raw_issue.get(field)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("design-review DFM issue reference is invalid")
            optional_ids[field] = value
        inputs = raw_issue.get("inputs")
        if not isinstance(inputs, Mapping):
            raise ValueError("design-review DFM issue inputs must be an object")
        suggestion = raw_issue.get("suggestion")
        if suggestion is not None and (not isinstance(suggestion, str) or not suggestion):
            raise ValueError("design-review DFM issue suggestion is invalid")
        issues.append(
            DFMIssue(
                code=code,
                severity=severity,
                message=message,
                part_id=optional_ids["part_id"],
                feature_id=optional_ids["feature_id"],
                setup_id=optional_ids["setup_id"],
                inputs=dict(inputs),
                suggestion=suggestion,
            )
        )
    return DFMReport(tuple(issues), engine_version=DFM_ENGINE_VERSION)


def validate_design_review_status_dfm_report(
    status: DesignReviewPackageStatus,
    report: DFMReport,
) -> None:
    """Bind the blocker profile to the exact DFM result without inventing stock."""

    if status.blocker_codes == ("STOCK_PROFILE_MISSING",):
        stock_issues = tuple(
            issue
            for issue in report.issues
            if issue.code == "STOCK_PROFILE_MISSING" and issue.severity is Severity.BLOCK
        )
        invalid: list[DFMIssue] = []
        for issue in report.issues:
            if issue.code == "STOCK_PROFILE_MISSING" and issue.severity is Severity.BLOCK:
                try:
                    validate_stock_profile_missing_issue(issue)
                except ValueError:
                    invalid.append(issue)
                continue
            if issue.code == DFM_GRAIN_BLOCKER_CODE:
                try:
                    validate_stock_grain_binding_issue(
                        issue,
                        expected_severity=Severity.WARNING,
                        expected_phase=DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE,
                    )
                except ValueError:
                    invalid.append(issue)
                continue
            invalid.append(issue)
        if not stock_issues or invalid:
            raise ArtifactError(
                "stock-blocked review status requires raw STOCK_PROFILE_MISSING blockers and "
                "permits only canonical missing-information grain warnings"
            )
        return
    if status.blocker_codes == (DFM_GRAIN_BLOCKER_CODE,):
        invalid_grain_issue = False
        for issue in report.issues:
            try:
                validate_stock_grain_binding_issue(
                    issue,
                    expected_severity=Severity.BLOCK,
                    expected_phase=DFM_GRAIN_STOCK_MATCHED_PHASE,
                )
            except ValueError:
                invalid_grain_issue = True
                break
        if not report.issues or invalid_grain_issue:
            raise ArtifactError(
                "grain-blocked review status requires only raw DFM-GRAIN-001 blockers"
            )
        return
    if report.blocking_issues:
        raise ArtifactError("non-DFM-blocked review status cannot carry a blocking DFM report")


def _validate_stock_and_grain_report_binding(
    status: DesignReviewPackageStatus,
    report: DFMReport,
    *,
    stock_selection_truth: _StockSelectionTruth,
    canonical_parts: tuple[PartSpec, ...],
) -> None:
    """Bind selection/grain facts to the frozen parts and selected stock snapshot."""

    part_by_id = {part.part_id: part for part in canonical_parts}
    if status.blocker_codes == (STOCK_PROFILE_MISSING_CODE,):
        expected_stock_issues = tuple(
            stock_profile_missing_issue(part_by_id[part_id])
            for part_id in stock_selection_truth.unmatched_part_ids
        )
        expected_grain_warnings = stock_grain_binding_issues(
            canonical_parts,
            None,
            severity=Severity.WARNING,
        )
        expected_issues = (*expected_stock_issues, *expected_grain_warnings)
        if not expected_stock_issues or canonical_json_bytes(report.issues) != canonical_json_bytes(
            expected_issues
        ):
            raise ArtifactError(
                "stock-blocked DFM report does not exactly cover canonical unmatched BOM parts"
            )
        return

    if stock_selection_truth.unmatched_part_ids:
        raise ArtifactError("non-stock status contradicts unmatched canonical BOM parts")

    parts_by_stock: dict[str, list[PartSpec]] = {}
    for part_id, stock_id in stock_selection_truth.assigned_stock_by_part_id.items():
        parts_by_stock.setdefault(stock_id, []).append(part_by_id[part_id])
    expected_grain_issues = tuple(
        issue
        for stock_id in sorted(parts_by_stock)
        for issue in stock_grain_binding_issues(
            tuple(sorted(parts_by_stock[stock_id], key=lambda part: part.part_id)),
            stock_selection_truth.stocks_by_id[stock_id],
        )
    )
    if status.blocker_codes == (DFM_GRAIN_BLOCKER_CODE,):
        if not expected_grain_issues or canonical_json_bytes(report.issues) != canonical_json_bytes(
            expected_grain_issues
        ):
            raise ArtifactError(
                "grain-blocked DFM report does not exactly cover canonical unbound stock groups"
            )
        return
    if expected_grain_issues:
        raise ArtifactError("non-grain status contradicts an unbound directional stock group")
    if any(
        issue.code in {STOCK_PROFILE_MISSING_CODE, DFM_GRAIN_BLOCKER_CODE}
        for issue in report.issues
    ):
        raise ArtifactError("non-selection status carries a stock or grain DFM issue")


def _validate_dfm_report_artifact(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
) -> DFMReport:
    candidates = [
        entry
        for entry in entries
        if str(entry["path"]).casefold() == _DFM_REPORT_ARTIFACT_PATH.casefold()
        or str(entry["role"]).casefold() == _DFM_REPORT_ARTIFACT_ROLE.casefold()
    ]
    if len(candidates) != 1:
        raise ArtifactError("design-review DFM report artifact entry is not unique")
    entry = candidates[0]
    if (
        entry["path"] != _DFM_REPORT_ARTIFACT_PATH
        or entry["role"] != _DFM_REPORT_ARTIFACT_ROLE
        or entry["media_type"] != "application/json"
    ):
        raise ArtifactError("design-review DFM report artifact entry is not canonical")
    value = _strict_canonical_json_object(
        archive.read(_DFM_REPORT_ARTIFACT_PATH),
        label="design-review DFM report",
    )
    try:
        return normalize_design_review_dfm_report(value)
    except ValueError as exc:
        raise ArtifactError("design-review DFM report artifact is invalid") from exc


def _validate_status_readiness(
    status: DesignReviewPackageStatus,
    readiness: WorkshopReadinessReport,
    *,
    authoritative_cad_verified: bool,
) -> None:
    software = {item.code: item.status.value for item in readiness.software_evidence}
    expected = {
        "AUTHORITATIVE_CAD": ("VERIFIED" if authoritative_cad_verified else "MISSING"),
        "DFM_SCREEN": (
            "MISSING"
            if status.blocker_codes in {("STOCK_PROFILE_MISSING",), (DFM_GRAIN_BLOCKER_CODE,)}
            else "VERIFIED"
        ),
        "SEMANTIC_OPERATIONS": ("VERIFIED" if status.operations_included else "MISSING"),
        "SETUP_SHEETS": "VERIFIED" if status.setup_sheets_included else "MISSING",
        "VALIDATION_BACKPLOT": ("VERIFIED" if status.validation_backplot_included else "MISSING"),
        "NON_CUTTING_PROGRAM": ("VERIFIED" if status.validation_program_included else "MISSING"),
    }
    if software != expected:
        raise ArtifactError("design-review package status and workshop readiness disagree")


def _design_requirements_from_grouped_bom(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
    *,
    required: bool,
) -> _GroupedBOMTruth | None:
    candidates = [
        entry
        for entry in entries
        if str(entry["path"]).casefold() == "bom/grouped-bom.json"
        or str(entry["role"]).casefold() == "grouped_bom"
    ]
    if not candidates and not required:
        return None
    if len(candidates) != 1:
        raise ArtifactError("grouped BOM artifact entry is not unique")
    entry = candidates[0]
    if (
        entry["path"] != "bom/grouped-bom.json"
        or entry["role"] != "GROUPED_BOM"
        or entry["media_type"] != "application/json"
    ):
        raise ArtifactError("grouped BOM artifact entry is not canonical")
    payload = _strict_canonical_json_object(
        archive.read("bom/grouped-bom.json"),
        label="grouped BOM",
    )
    if frozenset(payload) != {
        "schema_version",
        "group_fingerprint",
        "release_scope",
        "physical_release_authorized",
        "group_count",
        "part_instance_count",
        "groups",
    }:
        raise ArtifactError("grouped BOM has an unexpected structure")
    groups = payload["groups"]
    if (
        payload["schema_version"] != GROUPED_BOM_SCHEMA_VERSION
        or payload["release_scope"] != "DESIGN_REVIEW"
        or payload["physical_release_authorized"] is not False
        or not isinstance(groups, list)
        or type(payload["group_count"]) is not int
        or payload["group_count"] != len(groups)
        or type(payload["part_instance_count"]) is not int
        or payload["part_instance_count"] < 0
        or payload["group_fingerprint"] != sha256_hex(canonical_json_bytes(groups))
    ):
        raise ArtifactError("grouped BOM contract is invalid")

    edge_band_selection_required = False
    material_grain_binding_required = False
    part_grain_axis_by_id: dict[str, str] = {}
    part_material_by_id: dict[str, tuple[str, str]] = {}
    grouped_part_ids_by_id: dict[str, tuple[str, ...]] = {}
    group_ids: list[str] = []
    signature_bytes_seen: set[bytes] = set()
    instance_count = 0
    for group in groups:
        if not isinstance(group, Mapping) or frozenset(group) != {
            "group_id",
            "signature",
            "part_ids",
            "quantity",
            "conservative_total_weight_g",
            "finished_area_um2",
            "raw_area_um2",
        }:
            raise ArtifactError("grouped BOM row is invalid")
        group_id = group["group_id"]
        signature = group["signature"]
        part_ids = group["part_ids"]
        quantity = group["quantity"]
        if (
            not isinstance(group_id, str)
            or not group_id
            or not isinstance(signature, Mapping)
            or frozenset(signature)
            != {
                "name",
                "finished_um",
                "raw_um",
                "material_id",
                "material_version",
                "grain_direction",
                "edge_bands",
            }
            or not isinstance(part_ids, list)
            or not part_ids
            or any(
                not isinstance(part_id, str) or not part_id or part_id != part_id.strip()
                for part_id in part_ids
            )
            or part_ids != sorted(set(part_ids))
            or type(quantity) is not int
            or quantity <= 0
        ):
            raise ArtifactError("grouped BOM row identity is invalid")
        signature_bytes = canonical_json_bytes(signature)
        expected_group_id = f"bom-group:{sha256_hex(signature_bytes)[:16]}"
        if group_id != expected_group_id:
            raise ArtifactError("grouped BOM group ID does not match its canonical signature")
        if signature_bytes in signature_bytes_seen:
            raise ArtifactError("grouped BOM contains a duplicate canonical signature")
        signature_bytes_seen.add(signature_bytes)
        group_ids.append(group_id)
        instance_count += quantity
        grain_direction = signature["grain_direction"]
        material_id = signature["material_id"]
        material_version = signature["material_version"]
        if (
            not isinstance(grain_direction, str)
            or not grain_direction
            or grain_direction != grain_direction.strip().upper()
            or not isinstance(material_id, str)
            or not material_id
            or material_id != material_id.strip()
            or not isinstance(material_version, str)
            or not material_version
            or material_version != material_version.strip()
        ):
            raise ArtifactError("grouped BOM material or grain identity is not canonical")
        canonical_grain_axis = (
            grain_direction if grain_direction in {"NONE", "X", "Y"} else "UNKNOWN"
        )
        material_grain_binding_required |= canonical_grain_axis != "NONE"
        canonical_group_part_ids = tuple(part_ids)
        for part_id in part_ids:
            if part_id in part_grain_axis_by_id:
                raise ArtifactError("grouped BOM contains a duplicate part ID")
            part_grain_axis_by_id[part_id] = canonical_grain_axis
            part_material_by_id[part_id] = (material_id, material_version)
            grouped_part_ids_by_id[part_id] = canonical_group_part_ids
        edge_bands = signature["edge_bands"]
        if not isinstance(edge_bands, list):
            raise ArtifactError("grouped BOM edge bands must be an array")
        for detail in edge_bands:
            if not isinstance(detail, Mapping) or frozenset(detail) != {
                "edge",
                "thickness_um",
                "source_face",
                "catalog_id",
                "catalog_version",
                "attachment_method",
            }:
                raise ArtifactError("grouped BOM edge-band declaration is invalid")
            method = detail["attachment_method"]
            catalog_id = detail["catalog_id"]
            catalog_version = detail["catalog_version"]
            if method == "UNRESOLVED":
                if catalog_id is not None or catalog_version is not None:
                    raise ArtifactError("unresolved grouped BOM edge band has a catalog identity")
                edge_band_selection_required = True
            elif method == "MECHANICAL":
                if (
                    not isinstance(catalog_id, str)
                    or not catalog_id
                    or not isinstance(catalog_version, str)
                    or not catalog_version
                ):
                    raise ArtifactError("mechanical grouped BOM edge band lacks catalog identity")
            else:
                raise ArtifactError("grouped BOM edge band is not adhesive-free")
    if group_ids != sorted(set(group_ids)) or instance_count != payload["part_instance_count"]:
        raise ArtifactError("grouped BOM ordering or quantity total is invalid")
    return _GroupedBOMTruth(
        edge_band_selection_required=edge_band_selection_required,
        material_grain_binding_required=material_grain_binding_required,
        part_grain_axis_by_id=part_grain_axis_by_id,
        part_material_by_id=part_material_by_id,
        grouped_part_ids_by_id=grouped_part_ids_by_id,
    )


def _validate_grain_issues_against_grouped_bom(
    report: DFMReport,
    truth: _GroupedBOMTruth,
) -> None:
    grain_issues = tuple(issue for issue in report.issues if issue.code == DFM_GRAIN_BLOCKER_CODE)
    seen_part_ids: set[str] = set()
    for issue in grain_issues:
        try:
            validate_stock_grain_binding_issue(issue)
        except ValueError as exc:
            raise ArtifactError("DFM grain issue is not canonical") from exc
        affected_part_ids = tuple(issue.inputs["affected_part_ids"])
        if seen_part_ids.intersection(affected_part_ids):
            raise ArtifactError("DFM grain issues contain duplicate affected part IDs")
        seen_part_ids.update(affected_part_ids)
        if any(part_id not in truth.part_grain_axis_by_id for part_id in affected_part_ids):
            raise ArtifactError("DFM grain issue references a part missing from grouped BOM")
        affected_part_id_set = set(affected_part_ids)
        if any(
            not set(truth.grouped_part_ids_by_id[part_id]) <= affected_part_id_set
            for part_id in affected_part_ids
        ):
            raise ArtifactError("DFM grain issue omits an identical grouped-BOM part")

        expected_axes = tuple(
            sorted({truth.part_grain_axis_by_id[part_id] for part_id in affected_part_ids})
        )
        if "NONE" in expected_axes:
            raise ArtifactError("DFM grain issue references a non-directional grouped-BOM part")
        if tuple(issue.inputs["required_part_grain_directions"]) != expected_axes:
            raise ArtifactError("DFM grain issue axes do not match grouped BOM")

        expected_material = (
            str(issue.inputs["material_id"]),
            str(issue.inputs["material_version"]),
        )
        if any(
            truth.part_material_by_id[part_id] != expected_material for part_id in affected_part_ids
        ):
            raise ArtifactError("DFM grain issue material does not match grouped BOM")

        if issue.inputs["assessment_phase"] == DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE:
            expected_affected = tuple(
                sorted(
                    part_id
                    for part_id, material in truth.part_material_by_id.items()
                    if material == expected_material
                    and truth.part_grain_axis_by_id[part_id] != "NONE"
                )
            )
            if affected_part_ids != expected_affected:
                raise ArtifactError(
                    "stock-selection grain warning does not cover its grouped-BOM material"
                )


def _validate_status_review_core(entries: Iterable[Mapping[str, Any]]) -> None:
    entry_values = tuple(entries)
    by_identity = {
        (str(entry["path"]), str(entry["role"]), str(entry["media_type"])): entry
        for entry in entry_values
    }
    missing = [
        path
        for path, role, media_type in _STATUS_REVIEW_REQUIRED_ARTIFACTS
        if (path, role, media_type) not in by_identity
    ]
    if missing:
        raise ArtifactError(
            "design-review package status requires complete review artifacts: " + ", ".join(missing)
        )
    non_positive = [
        path
        for path, role, media_type in _STATUS_REVIEW_REQUIRED_ARTIFACTS
        if type(by_identity[(path, role, media_type)].get("size_bytes")) is not int
        or by_identity[(path, role, media_type)]["size_bytes"] <= 0
    ]
    if non_positive:
        raise ArtifactError(
            "design-review package status requires non-empty review artifacts: "
            + ", ".join(non_positive)
        )
    part_dxfs = [
        entry
        for entry in entry_values
        if re.fullmatch(r"parts/[^/]+/[AB]\.dxf", str(entry["path"]))
        and entry["role"] == "PART_DXF"
        and entry["media_type"] == "image/vnd.dxf"
    ]
    if not part_dxfs:
        raise ArtifactError("design-review package status requires at least one part DXF")
    part_drawings = [
        entry
        for entry in entry_values
        if re.fullmatch(r"drawings/[^/]+/[AB]\.svg", str(entry["path"]))
        and entry["role"] == "PART_DRAWING"
        and entry["media_type"] == "image/svg+xml"
    ]
    if not part_drawings:
        raise ArtifactError("design-review package status requires at least one part drawing")
    if any(
        type(entry.get("size_bytes")) is not int or entry["size_bytes"] <= 0
        for entry in (*part_dxfs, *part_drawings)
    ):
        raise ArtifactError("design-review package status requires non-empty part review files")


def _strict_canonical_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ArtifactError(f"{label} is not canonical UTF-8 JSON")

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    try:
        decoded = payload.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, parse_constant=reject_nonfinite)
        if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != payload:
            raise ValueError(f"{label} is not a canonical JSON object")
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ArtifactError(f"{label} is not canonical UTF-8 JSON") from exc
    return {str(key): value for key, value in parsed.items()}


def _validate_generated_package_semantics(
    archive: zipfile.ZipFile,
    *,
    status: DesignReviewPackageStatus,
    dfm_report: DFMReport,
    canonical_parts: tuple[PartSpec, ...],
    stock_selection_truth: _StockSelectionTruth,
    generation_plan_truth: _GenerationPlanTruth,
    entries: Iterable[Mapping[str, Any]],
) -> tuple[OperationsDocument, tuple[ArtifactFile, ...]]:
    """Re-run every machine-neutral GENERATED projection from frozen inputs."""

    machine = generation_plan_truth.machine
    rebuilt_report, validated_groups = _rebuild_complete_stock_dfm(
        canonical_parts=canonical_parts,
        stock_selection_truth=stock_selection_truth,
        machine=machine,
    )
    if rebuilt_report.blocking_issues:
        raise ArtifactError("generated CAM package rebuild has a blocking DFM issue")
    if canonical_json_bytes(rebuilt_report) != canonical_json_bytes(dfm_report):
        raise ArtifactError("generated CAM DFM report differs from deterministic reconstruction")
    layouts = [layout for _, _, layout in validated_groups]

    design_hash = _archive_design_hash(archive)
    try:
        documents = [
            generate_operations_document(
                design_hash=design_hash,
                parts=selected_parts,
                layout=layout,
                machine=machine,
                validate=False,
                two_sided_registration_by_sheet=(
                    generation_plan_truth.registrations_by_stock.get(stock.stock_id)
                ),
            )
            for stock, selected_parts, layout in validated_groups
        ]
    except ProductionBlockedError as exc:
        raise ArtifactError("generated CAM operations cannot be deterministically rebuilt") from exc

    selected_tool_ids = {
        operation.tool_id for document in documents for operation in document.operations
    }
    selected_tools = tuple(
        sorted(
            (tool for tool in machine.tools if tool.tool_id in selected_tool_ids),
            key=lambda item: item.tool_id,
        )
    )
    operations = OperationsDocument(
        schema_version=OPERATIONS_SCHEMA_VERSION,
        design_hash=design_hash,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        setups=tuple(setup for document in documents for setup in document.setups),
        operations=tuple(operation for document in documents for operation in document.operations),
        mode="VALIDATION",
        tool_catalog_version=machine.tool_library_version,
        tool_catalog_fingerprint=tool_catalog_fingerprint(selected_tools),
        tools=selected_tools,
    )
    if not operations.operations or not operations.setups:
        raise ArtifactError("generated CAM reconstruction produced no operations or setups")
    if status.validation_program_included is not (
        generation_plan_truth.validation_program_requested
    ):
        raise ArtifactError("generated CAM status disagrees with the generation plan")

    generated_roles = {
        "STOCK_PURCHASE_SCHEDULE",
        "MACHINE_NEUTRAL_OPERATIONS",
        "TOOL_LIST",
        "LABEL_INDEX",
        "QUALITY_MEASUREMENT_PLAN",
        "SETUP_SHEET",
        "NESTING_MAP",
    }
    expected = tuple(
        artifact
        for artifact in default_artifacts(
            parts=canonical_parts,
            layout=tuple(layouts),
            operations=operations,
        )
        if artifact.role in generated_roles
    )
    expected = (
        *expected,
        ArtifactFile(
            "cam/validation-backplot.svg",
            backplot_svg(operations),
            "image/svg+xml",
            "VALIDATION_BACKPLOT",
        ),
    )
    if generation_plan_truth.validation_program_requested:
        programs = LinuxCNCValidationPostprocessor().generate(operations)
        if not programs:
            raise ArtifactError("generated CAM reconstruction produced no validation program")
        expected = (
            *expected,
            *(
                ArtifactFile(
                    f"machine-validation/{program.filename}",
                    program.content,
                    "text/x-gcode",
                    "NON_CUTTING_VALIDATION_PROGRAM",
                )
                for program in programs
            ),
        )

    entry_values = tuple(entries)
    for artifact in expected:
        _canonical_artifact_entry(
            entry_values,
            path=artifact.path,
            role=artifact.role,
            media_type=artifact.media_type,
            role_unique=artifact.role
            not in {"SETUP_SHEET", "NESTING_MAP", "NON_CUTTING_VALIDATION_PROGRAM"},
        )
        if archive.read(artifact.path) != artifact.data:
            raise ArtifactError(
                f"generated CAM artifact differs from deterministic reconstruction: {artifact.path}"
            )
    return operations, tuple(sorted(expected, key=lambda item: item.path))


def _rebuild_complete_stock_dfm(
    *,
    canonical_parts: tuple[PartSpec, ...],
    stock_selection_truth: _StockSelectionTruth,
    machine: MachineProfile,
) -> tuple[
    DFMReport,
    tuple[tuple[StockSheet, tuple[PartSpec, ...], NestingLayout], ...],
]:
    """Rebuild DFM from authenticated parts, stock selection and machine identity."""

    if stock_selection_truth.unmatched_part_ids:
        raise ArtifactError("complete-stock DFM reconstruction has unmatched canonical parts")
    part_by_id = {part.part_id: part for part in canonical_parts}
    grouped: dict[str, list[PartSpec]] = {}
    for part_id, stock_id in sorted(stock_selection_truth.assigned_stock_by_part_id.items()):
        grouped.setdefault(stock_id, []).append(part_by_id[part_id])
    if not grouped:
        raise ArtifactError("complete-stock DFM reconstruction has no stock assignment")

    validator = DFMValidator()
    report_issues: list[DFMIssue] = []
    validated_groups: list[tuple[StockSheet, tuple[PartSpec, ...], NestingLayout]] = []
    for stock_id in sorted(grouped):
        stock = stock_selection_truth.stocks_by_id[stock_id]
        selected_parts = tuple(grouped[stock_id])
        layout = DeterministicNester().nest(selected_parts, stock)
        if not layout.is_complete or layout.used_sheet_count <= 0:
            raise ArtifactError("complete-stock DFM reconstruction has an incomplete layout")
        validated_groups.append((stock, selected_parts, layout))
        report_issues.extend(validator.validate(selected_parts, layout, machine).issues)
    return (
        DFMReport(tuple(report_issues), engine_version=validator.engine_version),
        tuple(validated_groups),
    )


def _archive_design_hash(archive: zipfile.ZipFile) -> str:
    """Read the already-validated design hash from the canonical result summary."""

    summary = _strict_canonical_json_object(
        archive.read("design/result-summary.json"),
        label="design result summary",
    )
    value = summary.get("design_hash")
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ArtifactError("design result summary has an invalid design hash")
    return value


def _validate_complete_package_inventory(
    archive: zipfile.ZipFile,
    entries: Iterable[Mapping[str, Any]],
    *,
    canonical_parts: tuple[PartSpec, ...],
    generated_artifacts: tuple[ArtifactFile, ...],
    manifest: Mapping[str, Any],
) -> None:
    """Reject aliases and extras by matching one complete case-sensitive inventory."""

    review_core = design_review_artifacts(
        parts=canonical_parts,
        project_id=str(manifest["project_id"]),
        revision=str(manifest["revision"]),
        design_hash=str(manifest["design_hash"]),
    )
    fixed = {
        ("design/design-spec.json", "FROZEN_DESIGN_SPEC", "application/json"),
        ("design/result-summary.json", "DESIGN_RESULT_SUMMARY", "application/json"),
        ("model/design.step", "AUTHORITATIVE_STEP", "model/step"),
        ("model/design.glb", "WEB_PREVIEW_GLB", "model/gltf-binary"),
        (
            "validation/cad-interchange-status.json",
            "CAD_INTERCHANGE_STATUS",
            "application/json",
        ),
        (
            DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH,
            DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE,
            "application/json",
        ),
        (_DFM_REPORT_ARTIFACT_PATH, _DFM_REPORT_ARTIFACT_ROLE, "application/json"),
        (_GENERATION_PLAN_ARTIFACT_PATH, _GENERATION_PLAN_ARTIFACT_ROLE, "application/json"),
        (
            "validation/stock-selection.json",
            "STOCK_SELECTION_SNAPSHOT",
            "application/json",
        ),
        (
            _WORKSHOP_READINESS_ARTIFACT_PATH,
            _WORKSHOP_READINESS_ARTIFACT_ROLE,
            "application/json",
        ),
        (SUPPLIER_HANDOFF_PATH, SUPPLIER_HANDOFF_ROLE, "application/json"),
        ("model/design.fcstd", "NON_AUTHORITATIVE_FREECAD_PROJECT", "application/vnd.freecad"),
    }
    allowed = {
        *((artifact.path, artifact.role, artifact.media_type) for artifact in review_core),
        *fixed,
        *_SAFE_CALLER_ADDITIONAL_ARTIFACTS,
        *((artifact.path, artifact.role, artifact.media_type) for artifact in generated_artifacts),
    }
    unexpected = [
        (str(entry["path"]), str(entry["role"]), str(entry["media_type"]))
        for entry in entries
        if (str(entry["path"]), str(entry["role"]), str(entry["media_type"])) not in allowed
    ]
    if unexpected:
        rendered = "; ".join(f"{path} ({role}; {media})" for path, role, media in unexpected)
        raise ArtifactError(
            "production package contains an unapproved artifact identity: " + rendered
        )

    source = manifest.get("source_provenance")
    source_entries = [
        entry for entry in entries if entry["path"] == "validation/source-provenance.json"
    ]
    if source is None:
        if source_entries:
            raise ArtifactError("source provenance artifact has no matching manifest context")
    else:
        if len(source_entries) != 1 or archive.read(
            "validation/source-provenance.json"
        ) != canonical_json_bytes(source):
            raise ArtifactError("source provenance artifact differs from manifest context")


def _validate_generated_cam_inventory(
    status: DesignReviewPackageStatus,
    entries: Iterable[Mapping[str, Any]],
) -> None:
    entry_values = tuple(entries)
    fixed_generated = {
        ("materials/stock-purchase.csv", "STOCK_PURCHASE_SCHEDULE", "text/csv"),
        ("cam/operations.json", "MACHINE_NEUTRAL_OPERATIONS", "application/json"),
        ("cam/tool-list.csv", "TOOL_LIST", "text/csv"),
        ("labels/label-index.csv", "LABEL_INDEX", "text/csv"),
        (
            "quality/measurement-plan.json",
            "QUALITY_MEASUREMENT_PLAN",
            "application/json",
        ),
        ("cam/validation-backplot.svg", "VALIDATION_BACKPLOT", "image/svg+xml"),
    }

    def is_dynamic_generated(path: str, role: str, media_type: str) -> bool:
        if (
            re.fullmatch(r"cam/setups/[^/]+\.svg", path)
            and role == "SETUP_SHEET"
            and media_type == "image/svg+xml"
        ):
            return True
        if (
            re.fullmatch(r"nesting/[^/]+/sheet-[0-9]{3}\.svg", path)
            and role == "NESTING_MAP"
            and media_type == "image/svg+xml"
        ):
            return True
        return bool(
            re.fullmatch(r"machine-validation/[^/]+\.validation\.ngc", path)
            and role == "NON_CUTTING_VALIDATION_PROGRAM"
            and media_type == "text/x-gcode"
        )

    unexpected = [
        entry
        for entry in entry_values
        if blocked_cam_artifact_violation(
            str(entry["path"]),
            str(entry["role"]),
            str(entry["media_type"]),
        )
        is not None
        and (
            str(entry["path"]),
            str(entry["role"]),
            str(entry["media_type"]),
        )
        not in fixed_generated
        and not is_dynamic_generated(
            str(entry["path"]),
            str(entry["role"]),
            str(entry["media_type"]),
        )
    ]
    if unexpected:
        raise ArtifactError("generated CAM package has an unapproved artifact identity")

    categories: tuple[
        tuple[
            str,
            Callable[[str, str], bool],
            Callable[[str, str, str], bool],
        ],
        ...,
    ] = (
        (
            "operations_included",
            lambda path, role: path.casefold() == "cam/operations.json"
            or role.casefold() == "machine_neutral_operations",
            lambda path, role, media: path == "cam/operations.json"
            and role == "MACHINE_NEUTRAL_OPERATIONS"
            and media == "application/json",
        ),
        (
            "setup_sheets_included",
            lambda path, role: path.casefold().startswith("cam/setups/")
            or role.casefold() == "setup_sheet",
            lambda path, role, media: path.startswith("cam/setups/")
            and role == "SETUP_SHEET"
            and media == "image/svg+xml",
        ),
        (
            "nesting_included",
            lambda path, role: path.casefold().startswith("nesting/")
            or role.casefold() == "nesting_map",
            lambda path, role, media: path.startswith("nesting/")
            and role == "NESTING_MAP"
            and media == "image/svg+xml",
        ),
        (
            "validation_backplot_included",
            lambda path, role: path.casefold() == "cam/validation-backplot.svg"
            or role.casefold() == "validation_backplot",
            lambda path, role, media: path == "cam/validation-backplot.svg"
            and role == "VALIDATION_BACKPLOT"
            and media == "image/svg+xml",
        ),
        (
            "validation_program_included",
            lambda path, role: path.casefold().startswith("machine-validation/")
            or path.casefold().endswith(".ngc")
            or role.casefold() == "non_cutting_validation_program",
            lambda path, role, media: path.startswith("machine-validation/")
            and path.endswith(".ngc")
            and role == "NON_CUTTING_VALIDATION_PROGRAM"
            and media == "text/x-gcode",
        ),
    )
    for flag_name, selects, is_canonical in categories:
        matches = [entry for entry in entry_values if selects(entry["path"], entry["role"])]
        if any(
            not is_canonical(entry["path"], entry["role"], entry["media_type"]) for entry in matches
        ):
            raise ArtifactError(f"generated CAM package has invalid {flag_name} artifacts")
        if bool(matches) is not getattr(status, flag_name):
            raise ArtifactError(f"generated CAM package status does not match {flag_name}")


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "part"
    if cleaned == value and len(cleaned) <= 80:
        return cleaned
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:64]}-{digest}"


def _validate_release_artifacts(
    context: ManifestContext,
    files: tuple[ArtifactFile, ...],
) -> None:
    paths = {item.path for item in files}
    required = {"model/design.step", "model/design.glb", "cam/operations.json"}
    missing = sorted(required - paths)
    if context.cad_status != "GENERATED" or missing:
        raise ProductionBlockedError(
            "production release requires genuine STEP and GLB CAD artifacts; "
            f"cad_status={context.cad_status}, missing={missing}"
        )
    raise ProductionBlockedError(
        "production machine release is disabled until a server-bound calibration and "
        "operator-approval catalogue exists; client-supplied evidence is not accepted"
    )


def _validate_unique_paths(files: tuple[ArtifactFile, ...]) -> None:
    paths = [item.path for item in files]
    if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
        raise ArtifactError("duplicate artifact paths")


def validate_manifest_context_contract(manifest: Mapping[str, Any]) -> None:
    """Validate schema-v4 manifest context shape and internal field bindings."""

    try:
        _validate_manifest_context_contract(manifest)
    except KeyError as exc:
        raise ArtifactError(f"production manifest context field missing: {exc.args[0]}") from exc


def _validate_manifest_context_contract(manifest: Mapping[str, Any]) -> None:
    if any(not isinstance(manifest.get(field), str) for field in _MANIFEST_STRING_CONTEXT_FIELDS):
        raise ArtifactError("production manifest context fields have invalid types")
    if manifest["domain_template_version"] != manifest["template_version"]:
        raise ArtifactError("production manifest context fields do not match the builder contract")

    capability = manifest["template_capability"]
    capability_fingerprint = manifest["template_capability_fingerprint"]
    if (
        not isinstance(capability, dict)
        or not capability
        or not isinstance(capability.get("template_version"), str)
        or not capability["template_version"]
        or capability.get("template_version") != manifest["template_capability_version"]
        or capability.get("capability_fingerprint") != capability_fingerprint
        or not isinstance(capability_fingerprint, str)
        or len(capability_fingerprint) != 64
    ):
        raise ArtifactError("production manifest template capability context is invalid")

    engine_context = manifest["production_engine_context"]
    if (
        not isinstance(engine_context, dict)
        or not engine_context
        or not isinstance(engine_context.get("template_capability_registry_version"), str)
        or not engine_context["template_capability_registry_version"]
        or engine_context.get("template_capability_registry_version")
        != manifest["template_capability_registry_version"]
    ):
        raise ArtifactError("production manifest engine context is invalid")

    generation_context_hash = manifest["generation_context_hash"]
    if not isinstance(generation_context_hash, str) or len(generation_context_hash) != 64:
        raise ArtifactError("production manifest generation context hash is invalid")

    machine_profile = manifest["machine_profile"]
    if (
        not isinstance(machine_profile, dict)
        or frozenset(machine_profile) != {"id", "version"}
        or not isinstance(machine_profile.get("id"), str)
        or not isinstance(machine_profile.get("version"), str)
    ):
        raise ArtifactError("production manifest machine profile context is invalid")

    for field in ("material_versions", "approved_assumptions", "warnings"):
        values = manifest[field]
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)
            or values != sorted(values)
        ):
            raise ArtifactError(f"production manifest {field} context is invalid")
    for field in ("overrides", "external_evidence"):
        values = manifest[field]
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise ArtifactError(f"production manifest {field} context is invalid")

    source = manifest["source_provenance"]
    if source is None:
        return
    if not isinstance(source, dict) or source.get("source") != "reference_image":
        raise ArtifactError("production manifest source provenance is invalid")
    import_id = source.get("import_id")
    if not isinstance(import_id, str) or len(import_id) != 36:
        raise ArtifactError("production manifest source provenance import ID is invalid")
    for field in ("image_sha256", "verified_model_fingerprint"):
        value = source.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ArtifactError(f"production manifest source provenance {field} is invalid")


def _validate_artifact_path(path: str) -> None:
    if not path or "\\" in path or "\x00" in path or ":" in path:
        raise ArtifactError("invalid artifact path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ArtifactError(f"unsafe artifact path: {path}")
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        raise ArtifactError(f"unsafe artifact path: {path}")
