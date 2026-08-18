"""Deterministic boundary between software review and workshop authorization.

This report intentionally cannot accept browser-supplied attestations. It says
which evidence the software genuinely generated and which physical checks must
still be bound by a future, trusted workshop catalogue before cutting can ever
be authorized.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .grain import DFM_GRAIN_REQUIRED_ACTION

WORKSHOP_READINESS_SCHEMA_VERSION = "custombuild.workshop-readiness.v2"
LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION = "custombuild.workshop-readiness.v1"
DESIGN_REVIEW_RELEASE_SCOPE = "design_review"
VALIDATION_ONLY_MACHINE_USE = "validation_only"

_REPORT_V1_KEYS = frozenset(
    {
        "schema_version",
        "design_review_ready",
        "physical_cutting_authorized",
        "missing_evidence_count",
        "software_evidence",
        "workshop_evidence",
    }
)
_REPORT_V2_KEYS = _REPORT_V1_KEYS | {
    "release_scope",
    "machine_use",
    "edge_band_selection_required",
}
_REQUIREMENT_KEYS = frozenset(
    {
        "code",
        "title",
        "status",
        "evidence",
        "required_action",
    }
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

_SOFTWARE_REQUIREMENTS = (
    ("AUTHORITATIVE_CAD", "Authoritative CAD geometry"),
    ("DFM_SCREEN", "Manufacturing feasibility screen"),
    ("SEMANTIC_OPERATIONS", "Semantic machining operations"),
    ("SETUP_SHEETS", "Setup sheets"),
    ("VALIDATION_BACKPLOT", "Independent review backplot"),
    ("NON_CUTTING_PROGRAM", "Non-cutting controller validation"),
)
_WORKSHOP_REQUIREMENTS = (
    ("WALL_ANCHOR", "Wall substrate and anchor system"),
    ("CABINET_HARDWARE", "Base-cabinet hardware and drill pattern"),
    ("MATERIAL_GRAIN", "Structured sheet-material grain-axis binding"),
    ("MACHINE_CALIBRATION", "Calibrated physical machine"),
    ("WCS_CONVENTION", "Verified WCS and origin convention"),
    ("MEASURED_TOOLING", "Measured tool, holder and runout"),
    ("MATERIAL_BATCH", "Verified material batch"),
    ("JOINT_COUPONS", "Joint coupon and tolerance test"),
    (
        "MATERIAL_REMOVAL_COMPARISON",
        "Independent material-removal comparison",
    ),
    ("SUPERVISED_AIR_CUT", "Supervised air cut"),
    ("REFERENCE_PART", "Measured reference part"),
    ("PROTOTYPE_BUILD", "Complete prototype furniture build"),
    ("CNC_OPERATOR_APPROVAL", "Named CNC operator approval"),
    (
        "FURNITURE_CONSTRUCTOR_APPROVAL",
        "Named furniture constructor approval",
    ),
)
_EDGE_BAND_REQUIREMENT = (
    "EDGE_BAND_SYSTEM",
    "Adhesive-free mechanical edge protection and cut-size compensation",
)
_EXTERNAL_EVIDENCE_TYPE_TO_CODE = {
    "wall_anchor": "WALL_ANCHOR",
    "hardware": "CABINET_HARDWARE",
    "material_grain": "MATERIAL_GRAIN",
}


class ReadinessValidationError(ValueError):
    """The readiness contract or its trusted builder inputs are invalid."""


class ReadinessStatus(StrEnum):
    VERIFIED = "VERIFIED"
    MISSING = "MISSING"
    EXTERNAL_EVIDENCE_REQUIRED = "EXTERNAL_EVIDENCE_REQUIRED"


@dataclass(frozen=True, slots=True)
class ReadinessRequirement:
    code: str
    title: str
    status: ReadinessStatus
    evidence: str
    required_action: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "title": self.title,
            "status": self.status.value,
            "evidence": self.evidence,
            "required_action": self.required_action,
        }


@dataclass(frozen=True, slots=True)
class WorkshopReadinessReport:
    schema_version: str
    release_scope: str
    machine_use: str
    edge_band_selection_required: bool
    design_review_ready: bool
    physical_cutting_authorized: bool
    software_evidence: tuple[ReadinessRequirement, ...]
    workshop_evidence: tuple[ReadinessRequirement, ...]

    @property
    def missing_evidence_count(self) -> int:
        return sum(
            requirement.status is not ReadinessStatus.VERIFIED
            for requirement in (*self.software_evidence, *self.workshop_evidence)
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the one canonical JSON-compatible v2 representation."""

        return {
            "schema_version": self.schema_version,
            "release_scope": self.release_scope,
            "machine_use": self.machine_use,
            "edge_band_selection_required": self.edge_band_selection_required,
            "design_review_ready": self.design_review_ready,
            "physical_cutting_authorized": self.physical_cutting_authorized,
            "missing_evidence_count": self.missing_evidence_count,
            "software_evidence": [item.as_dict() for item in self.software_evidence],
            "workshop_evidence": [item.as_dict() for item in self.workshop_evidence],
        }


def _software_requirement(
    code: str,
    title: str,
    verified: bool,
    evidence: str,
    required_action: str,
) -> ReadinessRequirement:
    return ReadinessRequirement(
        code=code,
        title=title,
        status=ReadinessStatus.VERIFIED if verified else ReadinessStatus.MISSING,
        evidence=evidence if verified else "No bound evidence in this generation job.",
        required_action="None for design review." if verified else required_action,
    )


def _external_requirement(
    code: str,
    title: str,
    required_action: str,
    evidence: Mapping[str, Any] | None = None,
) -> ReadinessRequirement:
    verified = evidence is not None
    return ReadinessRequirement(
        code=code,
        title=title,
        status=(
            ReadinessStatus.VERIFIED if verified else ReadinessStatus.EXTERNAL_EVIDENCE_REQUIRED
        ),
        evidence=(
            (
                f"Server-bound {evidence['catalog_id']}@"
                f"{evidence['catalog_version']} / sha256:{evidence['sha256']}"
            )
            if evidence is not None
            else "No checksum-bound external evidence is present in this generation job."
        ),
        required_action="None for this recorded check." if verified else required_action,
    )


def _material_grain_requirement(
    evidence: Mapping[str, Any] | None,
    *,
    binding_required: bool,
) -> ReadinessRequirement:
    """Record opaque documents without promoting them to a semantic axis binding."""

    if not binding_required:
        return ReadinessRequirement(
            code="MATERIAL_GRAIN",
            title="Structured sheet-material grain-axis binding",
            status=ReadinessStatus.VERIFIED,
            evidence=(
                "Not applicable: every effective part uses a catalog-declared "
                "non-directional material."
            ),
            required_action="None for this design.",
        )
    documentary_evidence = (
        (
            f"Checksum-bound documentary upload {evidence['catalog_id']}@"
            f"{evidence['catalog_version']} / sha256:{evidence['sha256']}; "
            "it is not a structured stock-grain axis binding."
        )
        if evidence is not None
        else "No structured stock-grain axis binding is present in this generation job."
    )
    return ReadinessRequirement(
        code="MATERIAL_GRAIN",
        title="Structured sheet-material grain-axis binding",
        status=ReadinessStatus.EXTERNAL_EVIDENCE_REQUIRED,
        evidence=documentary_evidence,
        required_action=DFM_GRAIN_REQUIRED_ACTION,
    )


def _index_external_evidence(
    external_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(external_evidence, str | bytes | bytearray) or not isinstance(
        external_evidence, Sequence
    ):
        raise ReadinessValidationError("external_evidence must be a sequence of mappings")

    by_type: dict[str, Mapping[str, Any]] = {}
    for item in external_evidence:
        if not isinstance(item, Mapping):
            raise ReadinessValidationError("Every external evidence item must be a mapping")
        evidence_type = item.get("evidence_type")
        if (
            not isinstance(evidence_type, str)
            or evidence_type != evidence_type.strip()
            or evidence_type not in _EXTERNAL_EVIDENCE_TYPE_TO_CODE
        ):
            raise ReadinessValidationError("External evidence has an unknown evidence_type")
        if evidence_type in by_type:
            raise ReadinessValidationError(f"Duplicate external evidence type: {evidence_type}")
        for field in ("catalog_id", "catalog_version"):
            value = item.get(field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ReadinessValidationError(
                    f"External evidence {field} must be a canonical non-blank string"
                )
        digest = item.get("sha256")
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise ReadinessValidationError(
                "External evidence sha256 must be 64 lowercase hexadecimal digits"
            )
        by_type[evidence_type] = item
    return by_type


def build_workshop_readiness_report(
    *,
    authoritative_cad: bool,
    dfm_passed: bool,
    operation_count: int,
    setup_count: int,
    validation_backplot: bool,
    validation_program: bool,
    edge_band_selection_required: bool = False,
    material_grain_binding_required: bool = True,
    external_evidence: Sequence[Mapping[str, Any]] = (),
) -> WorkshopReadinessReport:
    boolean_inputs = {
        "authoritative_cad": authoritative_cad,
        "dfm_passed": dfm_passed,
        "validation_backplot": validation_backplot,
        "validation_program": validation_program,
        "edge_band_selection_required": edge_band_selection_required,
        "material_grain_binding_required": material_grain_binding_required,
    }
    for field, boolean_value in boolean_inputs.items():
        if type(boolean_value) is not bool:
            raise ReadinessValidationError(f"{field} must be a boolean")
    count_inputs = {
        "operation_count": operation_count,
        "setup_count": setup_count,
    }
    for field, count_value in count_inputs.items():
        if type(count_value) is not int or count_value < 0:
            raise ReadinessValidationError(f"{field} must be a non-negative integer")

    by_type = _index_external_evidence(external_evidence)
    software = (
        _software_requirement(
            "AUTHORITATIVE_CAD",
            "Authoritative CAD geometry",
            authoritative_cad,
            "Checksum-bound STEP and GLB generated by the server.",
            "Generate the job with authoritative STEP enabled.",
        ),
        _software_requirement(
            "DFM_SCREEN",
            "Manufacturing feasibility screen",
            dfm_passed,
            "The versioned DFM report has no blocking issue.",
            "Resolve every blocking stock, tool, setup and operation finding.",
        ),
        _software_requirement(
            "SEMANTIC_OPERATIONS",
            "Semantic machining operations",
            operation_count > 0,
            f"{operation_count} checksum-bound operations generated.",
            "Generate a non-empty operations document for the exact design.",
        ),
        _software_requirement(
            "SETUP_SHEETS",
            "Setup sheets",
            setup_count > 0,
            f"{setup_count} versioned setups generated.",
            "Generate at least one setup sheet for the exact stock and machine profile.",
        ),
        _software_requirement(
            "VALIDATION_BACKPLOT",
            "Independent review backplot",
            validation_backplot,
            "A checksum-bound validation backplot is included.",
            "Generate the validation backplot for the exact operations document.",
        ),
        _software_requirement(
            "NON_CUTTING_PROGRAM",
            "Non-cutting controller validation",
            validation_program,
            "A safe-Z-only validation program is included; it is not cutting code.",
            "Generate and parse the non-cutting validation program.",
        ),
    )
    workshop_requirements = [
        _external_requirement(
            "WALL_ANCHOR",
            "Wall substrate and anchor system",
            "Upload a dated anchor specification for this exact design and substrate.",
            by_type.get("wall_anchor"),
        ),
        _external_requirement(
            "CABINET_HARDWARE",
            "Base-cabinet hardware and drill pattern",
            "Upload the exact hardware SKU, version and approved boring pattern.",
            by_type.get("hardware"),
        ),
        _material_grain_requirement(
            by_type.get("material_grain"),
            binding_required=material_grain_binding_required,
        ),
        _external_requirement(
            "MACHINE_CALIBRATION",
            "Calibrated physical machine",
            "Bind a dated calibration record with travel, spindle and accuracy results.",
        ),
        _external_requirement(
            "WCS_CONVENTION",
            "Verified WCS and origin convention",
            "Record the physical fixture origin, axes and safe limits for this machine.",
        ),
        _external_requirement(
            "MEASURED_TOOLING",
            "Measured tool, holder and runout",
            "Bind measured diameter, runout, holder, stick-out and usable flute length.",
        ),
        _external_requirement(
            "MATERIAL_BATCH",
            "Verified material batch",
            "Measure thickness and record batch-specific machining and moisture evidence.",
        ),
        _external_requirement(
            "JOINT_COUPONS",
            "Joint coupon and tolerance test",
            "Machine and approve representative joint coupons for this material/tool pair.",
        ),
        _external_requirement(
            "MATERIAL_REMOVAL_COMPARISON",
            "Independent material-removal comparison",
            ("Compare the final trusted toolpath against CAD and stock outside the postprocessor."),
        ),
        _external_requirement(
            "SUPERVISED_AIR_CUT",
            "Supervised air cut",
            "Run a guarded air cut on the exact physical setup and record the result.",
        ),
        _external_requirement(
            "REFERENCE_PART",
            "Measured reference part",
            "Cut and inspect a representative reference part before a full sheet.",
        ),
        _external_requirement(
            "PROTOTYPE_BUILD",
            "Complete prototype furniture build",
            "Build, load-test and document a complete prototype for this construction system.",
        ),
        _external_requirement(
            "CNC_OPERATOR_APPROVAL",
            "Named CNC operator approval",
            "Bind approval from an authorized operator for this exact job and setup.",
        ),
        _external_requirement(
            "FURNITURE_CONSTRUCTOR_APPROVAL",
            "Named furniture constructor approval",
            ("Bind approval of construction, assembly and intended use from a qualified reviewer."),
        ),
    ]
    if edge_band_selection_required:
        workshop_requirements.append(
            _external_requirement(
                "EDGE_BAND_SYSTEM",
                "Adhesive-free mechanical edge protection and cut-size compensation",
                (
                    "Bind the exact edge-protection SKU/version and its verified mechanical "
                    "retention method, then approve substrate cut-size compensation for every "
                    "declared local panel edge. Adhesively bonded edging is prohibited."
                ),
            )
        )
    workshop = tuple(workshop_requirements)
    return WorkshopReadinessReport(
        schema_version=WORKSHOP_READINESS_SCHEMA_VERSION,
        release_scope=DESIGN_REVIEW_RELEASE_SCOPE,
        machine_use=VALIDATION_ONLY_MACHINE_USE,
        edge_band_selection_required=edge_band_selection_required,
        design_review_ready=all(item.status is ReadinessStatus.VERIFIED for item in software),
        physical_cutting_authorized=False,
        software_evidence=software,
        workshop_evidence=workshop,
    )


def validate_workshop_evidence_binding(
    report: WorkshopReadinessReport,
    *,
    expected_edge_band_selection_required: bool,
    external_evidence: Sequence[Mapping[str, Any]],
    expected_material_grain_binding_required: bool = True,
) -> None:
    """Bind every workshop claim to the manifest's checksum-bound evidence.

    Software evidence is validated separately against the generated package
    inventory.  Workshop evidence is rebuilt from the immutable manifest
    snapshots so a self-consistent readiness document cannot invent a verified
    wall anchor, hardware selection or material-grain record.
    """

    if type(expected_edge_band_selection_required) is not bool:
        raise ReadinessValidationError("expected_edge_band_selection_required must be a boolean")
    if type(expected_material_grain_binding_required) is not bool:
        raise ReadinessValidationError(
            "expected_material_grain_binding_required must be a boolean"
        )
    if report.edge_band_selection_required is not expected_edge_band_selection_required:
        raise ReadinessValidationError(
            "Workshop readiness edge-band requirement does not match the frozen design"
        )

    expected = build_workshop_readiness_report(
        authoritative_cad=False,
        dfm_passed=False,
        operation_count=0,
        setup_count=0,
        validation_backplot=False,
        validation_program=False,
        edge_band_selection_required=expected_edge_band_selection_required,
        material_grain_binding_required=expected_material_grain_binding_required,
        external_evidence=external_evidence,
    )
    if report.workshop_evidence != expected.workshop_evidence:
        raise ReadinessValidationError(
            "Workshop readiness evidence does not match manifest external evidence"
        )


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    *,
    location: str,
) -> None:
    if set(payload) != expected:
        raise ReadinessValidationError(f"{location} must contain exactly the canonical keys")


def _parse_requirement(
    payload: Any,
    *,
    expected_code: str,
    expected_title: str,
    allowed_statuses: frozenset[ReadinessStatus],
) -> ReadinessRequirement:
    if not isinstance(payload, Mapping):
        raise ReadinessValidationError("Readiness requirements must be mappings")
    _require_exact_keys(payload, _REQUIREMENT_KEYS, location="Readiness requirement")
    for field in _REQUIREMENT_KEYS:
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise ReadinessValidationError(
                f"Readiness requirement {field} must be a non-empty string"
            )
    if payload["code"] != expected_code or payload["title"] != expected_title:
        raise ReadinessValidationError(
            "Readiness requirement order, code or title is not canonical"
        )
    try:
        status = ReadinessStatus(payload["status"])
    except ValueError as exc:
        raise ReadinessValidationError("Readiness requirement status is unknown") from exc
    if status not in allowed_statuses:
        raise ReadinessValidationError(
            "Readiness requirement status is invalid for its evidence scope"
        )
    return ReadinessRequirement(
        code=payload["code"],
        title=payload["title"],
        status=status,
        evidence=payload["evidence"],
        required_action=payload["required_action"],
    )


def _parse_requirement_list(
    payload: Any,
    *,
    expected: tuple[tuple[str, str], ...],
    allowed_statuses: frozenset[ReadinessStatus],
    location: str,
) -> tuple[ReadinessRequirement, ...]:
    if not isinstance(payload, list):
        raise ReadinessValidationError(f"{location} must be a list")
    if len(payload) != len(expected):
        raise ReadinessValidationError(
            f"{location} must contain every canonical requirement exactly once"
        )
    return tuple(
        _parse_requirement(
            item,
            expected_code=code,
            expected_title=title,
            allowed_statuses=allowed_statuses,
        )
        for item, (code, title) in zip(payload, expected, strict=True)
    )


def normalize_workshop_readiness_report(
    payload: Mapping[str, Any],
) -> WorkshopReadinessReport:
    """Validate a complete v1/v2 payload and return an immutable canonical v2 report.

    Legacy v1 is accepted only when its full arrays and every derived invariant are
    present and exact. No input mapping or nested list is mutated.
    """

    if not isinstance(payload, Mapping):
        raise ReadinessValidationError("Workshop readiness payload must be a mapping")
    schema_version = payload.get("schema_version")
    if schema_version == WORKSHOP_READINESS_SCHEMA_VERSION:
        _require_exact_keys(payload, _REPORT_V2_KEYS, location="Workshop readiness v2")
        if payload["release_scope"] != DESIGN_REVIEW_RELEASE_SCOPE:
            raise ReadinessValidationError(
                "Workshop readiness release_scope is not design-review safe"
            )
        if payload["machine_use"] != VALIDATION_ONLY_MACHINE_USE:
            raise ReadinessValidationError("Workshop readiness machine_use is not validation-only")
        edge_required = payload["edge_band_selection_required"]
        if type(edge_required) is not bool:
            raise ReadinessValidationError("edge_band_selection_required must be a boolean")
    elif schema_version == LEGACY_WORKSHOP_READINESS_SCHEMA_VERSION:
        _require_exact_keys(payload, _REPORT_V1_KEYS, location="Workshop readiness v1")
        workshop_payload = payload["workshop_evidence"]
        if not isinstance(workshop_payload, list):
            raise ReadinessValidationError("workshop_evidence must be a list")
        edge_required = len(workshop_payload) == len(_WORKSHOP_REQUIREMENTS) + 1
    else:
        raise ReadinessValidationError("Workshop readiness schema_version is unsupported")

    design_review_ready = payload["design_review_ready"]
    if type(design_review_ready) is not bool:
        raise ReadinessValidationError("design_review_ready must be a boolean")
    if payload["physical_cutting_authorized"] is not False:
        raise ReadinessValidationError("Physical cutting must remain unauthorized")
    missing_count = payload["missing_evidence_count"]
    if type(missing_count) is not int or missing_count < 0:
        raise ReadinessValidationError("missing_evidence_count must be a non-negative integer")

    software = _parse_requirement_list(
        payload["software_evidence"],
        expected=_SOFTWARE_REQUIREMENTS,
        allowed_statuses=frozenset({ReadinessStatus.VERIFIED, ReadinessStatus.MISSING}),
        location="software_evidence",
    )
    workshop_expected = (
        _WORKSHOP_REQUIREMENTS + (_EDGE_BAND_REQUIREMENT,)
        if edge_required
        else _WORKSHOP_REQUIREMENTS
    )
    workshop = _parse_requirement_list(
        payload["workshop_evidence"],
        expected=workshop_expected,
        allowed_statuses=frozenset(
            {
                ReadinessStatus.VERIFIED,
                ReadinessStatus.EXTERNAL_EVIDENCE_REQUIRED,
            }
        ),
        location="workshop_evidence",
    )

    derived_ready = all(item.status is ReadinessStatus.VERIFIED for item in software)
    if design_review_ready is not derived_ready:
        raise ReadinessValidationError("design_review_ready does not match software evidence")
    derived_missing = sum(
        item.status is not ReadinessStatus.VERIFIED for item in (*software, *workshop)
    )
    if missing_count != derived_missing:
        raise ReadinessValidationError("missing_evidence_count does not match the evidence arrays")

    return WorkshopReadinessReport(
        schema_version=WORKSHOP_READINESS_SCHEMA_VERSION,
        release_scope=DESIGN_REVIEW_RELEASE_SCOPE,
        machine_use=VALIDATION_ONLY_MACHINE_USE,
        edge_band_selection_required=edge_required,
        design_review_ready=design_review_ready,
        physical_cutting_authorized=False,
        software_evidence=software,
        workshop_evidence=workshop,
    )
