"""Strict status contract for checksum-bound design-review packages.

The package can be complete for design review while the downstream CAM stage is
deliberately blocked.  This contract keeps those two claims separate so neither
the API nor the UI has to infer machine readiness from the presence of a ZIP.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .dfm import STOCK_PROFILE_MISSING_CODE
from .grain import DFM_GRAIN_BLOCKER_CODE, DFM_GRAIN_REQUIRED_ACTION

DESIGN_REVIEW_PACKAGE_STATUS_SCHEMA_VERSION = "custombuild.design-review-package-status.v1"
DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_PATH = "validation/design-review-package-status.json"
DESIGN_REVIEW_PACKAGE_STATUS_ARTIFACT_ROLE = "DESIGN_REVIEW_PACKAGE_STATUS"
STOCK_PROFILE_MISSING_BLOCKER_CODE = STOCK_PROFILE_MISSING_CODE
TWO_SIDED_REGISTRATION_MISSING_BLOCKER_CODE = "TWO_SIDED_REGISTRATION_MISSING"
DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE = "DADO_RETENTION_EVIDENCE_MISSING"
BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE = (
    "BACK_PANEL_RETENTION_EVIDENCE_MISSING"
)
_CATALOG_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
BLOCKED_CAM_SUPPORTED_BLOCKER_CODES = (
    STOCK_PROFILE_MISSING_BLOCKER_CODE,
    DFM_GRAIN_BLOCKER_CODE,
    TWO_SIDED_REGISTRATION_MISSING_BLOCKER_CODE,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
)
GENERATED_REVIEW_REQUIRED_ACTION = (
    "None for design review; physical workshop evidence remains required."
)
BLOCKED_CAM_REQUIRED_ACTIONS = {
    STOCK_PROFILE_MISSING_BLOCKER_CODE: (
        "Select and server-bind an exact stock profile for every part material, version, "
        "thickness, blank size and quantity; do not infer sheet size, stock identity or "
        "machine capacity."
    ),
    DFM_GRAIN_BLOCKER_CODE: DFM_GRAIN_REQUIRED_ACTION,
    TWO_SIDED_REGISTRATION_MISSING_BLOCKER_CODE: (
        "Bind an externally specified two-sided registration and fixture plan; "
        "do not infer WCS, pins, fixtures or registration coordinates."
    ),
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE: (
        "Bind current certifier-signed, checksum-addressed mechanical retention evidence "
        "to every load-bearing carcass DADO application, including exact geometry, compiler, "
        "hardware quantity, material/thickness and shear/withdrawal capacity; a review "
        "acknowledgement, adhesive or geometric bearing check cannot replace that evidence."
    ),
    BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE: (
        "Use only the canonical inset back whose four boundary grooves and multi-direction "
        "closing sequence prove mechanical capture, or bind independently authenticated "
        "back-panel retention evidence when that application class is implemented."
    ),
}


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _canonical_identity(value: Any) -> bool:
    return isinstance(value, str) and _CATALOG_IDENTITY_PATTERN.fullmatch(value) is not None


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def joint_retention_contract_is_structurally_complete(
    joint: Any,
    *,
    parts: Sequence[Any] = (),
    expected_contract: Any = None,
    required_design_load_n: Mapping[str, int] | None = None,
    required_safety_factor_permille: int | None = None,
) -> bool:
    """Check structural completeness after, but not replace, source authentication.

    The current public API has no ingestion path for this object, so production
    designs remain blocked. This checker makes the future internal path testable;
    it does not establish that a catalogue or evidence issuer is trustworthy.
    Physical cutting remains a separate, permanently false claim.
    """

    joint_type = _enum_value(getattr(joint, "joint_type", None))
    if not isinstance(joint_type, str) or joint_type.casefold() != "dado":
        return False
    retention = getattr(joint, "retention", None)
    if retention is None or expected_contract is None or retention != expected_contract:
        return False
    retention_joint_type = _enum_value(getattr(retention, "joint_type", None))
    if (
        not isinstance(retention_joint_type, str)
        or retention_joint_type.casefold() != joint_type.casefold()
    ):
        return False
    application_class = _enum_value(
        getattr(joint, "retention_application_class", None)
    )
    if application_class != "load_bearing_carcass_dado" or _enum_value(
        getattr(retention, "application_class", None)
    ) != application_class:
        return False
    if any(
        not _canonical_identity(getattr(retention, field, None))
        for field in (
            "system_id",
            "system_version",
            "evidence_id",
            "installation_instruction_id",
            "installation_instruction_version",
            "hardware_sku",
        )
    ):
        return False
    if any(
        not _sha256(getattr(retention, field, None))
        for field in (
            "catalog_entry_sha256",
            "evidence_sha256",
            "installation_instruction_sha256",
            "joint_geometry_sha256",
        )
    ):
        return False

    # This first foundation slice models only a mechanical catalogue system
    # whose installation needs no additional CNC geometry. Feature-bound and
    # dry/self-locking systems remain fail-closed until the template can bind
    # their per-joint manufacturing features.
    if _enum_value(getattr(retention, "method", None)) != "mechanical":
        return False
    if _enum_value(getattr(retention, "machining_scope", None)) != "no_additional_cnc":
        return False
    raw_feature_ids = getattr(retention, "bound_feature_ids", None)
    if not isinstance(raw_feature_ids, tuple | list):
        return False
    feature_ids = tuple(raw_feature_ids)
    if len(feature_ids) != len(set(feature_ids)) or any(
        not isinstance(item, str) or not item for item in feature_ids
    ):
        return False
    if feature_ids:
        return False

    raw_materials = getattr(retention, "applicable_materials", None)
    if not isinstance(raw_materials, tuple | list) or not raw_materials:
        return False
    material_keys = tuple(
        (
            getattr(material, "material_id", None),
            getattr(material, "material_version", None),
        )
        for material in raw_materials
    )
    if any(
        not _canonical_identity(material_id) or not _canonical_identity(material_version)
        for material_id, material_version in material_keys
    ):
        return False
    if material_keys != tuple(sorted(set(material_keys))):
        return False

    numeric_fields = (
        "hardware_count_per_joint",
        "minimum_applicable_thickness_um",
        "maximum_applicable_thickness_um",
        "safety_factor_permille",
    )
    if any(
        not _positive_integer(getattr(retention, field, None)) for field in numeric_fields
    ):
        return False
    minimum_thickness_um = retention.minimum_applicable_thickness_um
    maximum_thickness_um = retention.maximum_applicable_thickness_um
    if minimum_thickness_um > maximum_thickness_um:
        return False
    if retention.safety_factor_permille < 1_000:
        return False
    raw_load_cases = getattr(retention, "load_cases", None)
    if not isinstance(raw_load_cases, tuple | list) or len(raw_load_cases) != 2:
        return False
    load_modes = tuple(_enum_value(getattr(item, "mode", None)) for item in raw_load_cases)
    if load_modes != ("shear", "withdrawal"):
        return False
    for load_case in raw_load_cases:
        rated_load_n = getattr(load_case, "rated_design_load_n", None)
        capacity_n = getattr(load_case, "verified_capacity_n", None)
        if (
            not isinstance(rated_load_n, int)
            or isinstance(rated_load_n, bool)
            or rated_load_n <= 0
            or not isinstance(capacity_n, int)
            or isinstance(capacity_n, bool)
            or capacity_n <= 0
        ):
            return False
        if capacity_n * 1_000 < rated_load_n * retention.safety_factor_permille:
            return False
    if (
        not isinstance(required_design_load_n, Mapping)
        or frozenset(required_design_load_n) != {"shear", "withdrawal"}
        or any(not _positive_integer(item) for item in required_design_load_n.values())
        or not _positive_integer(required_safety_factor_permille)
    ):
        return False
    for load_case in raw_load_cases:
        mode = _enum_value(load_case.mode)
        if load_case.rated_design_load_n < required_design_load_n[mode]:
            return False
    if retention.safety_factor_permille < required_safety_factor_permille:
        return False

    if getattr(joint, "hardware_sku", None) != retention.hardware_sku:
        return False
    if getattr(joint, "hardware_count", None) != retention.hardware_count_per_joint:
        return False
    part_by_id = {
        getattr(part, "part_id", None): part
        for part in parts
        if isinstance(getattr(part, "part_id", None), str)
    }
    members = getattr(joint, "members", ())
    if not isinstance(members, tuple | list) or len(members) != 2:
        return False
    for member in members:
        member_part = part_by_id.get(getattr(member, "part_id", None))
        thickness_um = getattr(member_part, "actual_thickness_um", None)
        if not _positive_integer(thickness_um):
            return False
        if not minimum_thickness_um <= thickness_um <= maximum_thickness_um:
            return False
        if (
            getattr(member_part, "material_id", None),
            getattr(member_part, "material_version", None),
        ) not in set(material_keys):
            return False
    return True


def _canonical_design_for_retention_check(design_result: Any) -> Any | None:
    """Rebuild and compare exact topology so removed joints fail closed."""

    try:
        from custombuild_domain import BookcaseDesignSpec, build_bookcase

        spec = BookcaseDesignSpec.model_validate(getattr(design_result, "spec", None))
        canonical = build_bookcase(spec)
    except (TypeError, ValueError):
        return None
    if any(
        getattr(design_result, field, None) != getattr(canonical, field)
        for field in (
            "design_hash",
            "engine_version",
            "template_version",
            "spec",
            "parts",
            "joints",
            "assembly_graph",
            "total_weight_g",
        )
    ):
        return None
    return canonical


def dado_retention_evidence_missing(design_result: Any) -> bool:
    """Return true unless every retention-required carcass DADO is covered."""

    canonical = _canonical_design_for_retention_check(design_result)
    if canonical is None:
        return True
    dado_joints = tuple(
        joint
        for joint in canonical.joints
        if isinstance(value := _enum_value(getattr(joint, "joint_type", None)), str)
        and value.casefold() == "dado"
        and _enum_value(getattr(joint, "retention_application_class", None))
        != "captive_inset_back_groove"
    )
    if not dado_joints:
        return False
    spec = canonical.spec
    parameters = getattr(spec, "parameters", None)
    expected_contract = getattr(spec, "joint_retention", None)
    shelf_load_n = getattr(parameters, "shelf_load_n", None)
    horizontal_force_n = getattr(parameters, "assumed_horizontal_force_n", None)
    safety_factor_permille = getattr(parameters, "structural_safety_factor_permille", None)
    if (
        not isinstance(shelf_load_n, int)
        or isinstance(shelf_load_n, bool)
        or shelf_load_n < 0
        or not isinstance(horizontal_force_n, int)
        or isinstance(horizontal_force_n, bool)
        or horizontal_force_n <= 0
        or not isinstance(safety_factor_permille, int)
        or isinstance(safety_factor_permille, bool)
        or safety_factor_permille <= 0
    ):
        return True
    required_design_load_n = {
        "shear": max(shelf_load_n, 1),
        "withdrawal": horizontal_force_n,
    }
    return any(
        not joint_retention_contract_is_structurally_complete(
            joint,
            parts=canonical.parts,
            expected_contract=expected_contract,
            required_design_load_n=required_design_load_n,
            required_safety_factor_permille=safety_factor_permille,
        )
        for joint in dado_joints
    )


def back_panel_retention_evidence_missing(design_result: Any) -> bool:
    """Fail closed unless a back is absent or its canonical capture is proven.

    This is not a type-name waiver.  Only the domain-validated four-edge inset
    topology with multiple closing movements is accepted without a separate
    back-panel retention contract.  Surface-mounted and unknown applications
    remain blocked.
    """

    # This checker owns bookcase back applications only.  A payload that is not
    # even a BookcaseDesignSpec belongs to the generic unsupported/statusless
    # gates; claiming a back-panel fact for it would mask those stronger errors.
    try:
        from custombuild_domain import BookcaseDesignSpec

        BookcaseDesignSpec.model_validate(getattr(design_result, "spec", None))
    except (ImportError, TypeError, ValueError):
        return False
    canonical = _canonical_design_for_retention_check(design_result)
    if canonical is None:
        return True
    back_panel = _enum_value(getattr(canonical.spec.parameters, "back_panel", None))
    if back_panel == "none":
        return False
    if back_panel != "inset_groove":
        return True
    try:
        from custombuild_domain.models import captive_inset_back_topology_is_complete

        return not captive_inset_back_topology_is_complete(
            canonical.parts,
            canonical.joints,
            canonical.assembly_graph,
        )
    except (ImportError, TypeError, ValueError):
        return True


def retention_evidence_blocker_code(design_result: Any) -> str | None:
    """Return one deterministic retention blocker in prerequisite order."""

    if dado_retention_evidence_missing(design_result):
        return DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    if back_panel_retention_evidence_missing(design_result):
        return BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE
    return None


class CAMStageStatus(StrEnum):
    VALIDATION_GENERATED = "VALIDATION_GENERATED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class DesignReviewPackageStatus:
    schema_version: str
    package_status: str
    cam_status: CAMStageStatus
    blocker_codes: tuple[str, ...]
    operations_included: bool
    setup_sheets_included: bool
    nesting_included: bool
    validation_backplot_included: bool
    validation_program_included: bool
    physical_cutting_authorized: bool
    required_action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "package_status": self.package_status,
            "cam_status": self.cam_status.value,
            "blocker_codes": list(self.blocker_codes),
            "operations_included": self.operations_included,
            "setup_sheets_included": self.setup_sheets_included,
            "nesting_included": self.nesting_included,
            "validation_backplot_included": self.validation_backplot_included,
            "validation_program_included": self.validation_program_included,
            "physical_cutting_authorized": self.physical_cutting_authorized,
            "required_action": self.required_action,
        }


_STATUS_KEYS = frozenset(DesignReviewPackageStatus.__dataclass_fields__)


def generated_design_review_package_status(
    *, validation_program_included: bool
) -> DesignReviewPackageStatus:
    if type(validation_program_included) is not bool:
        raise ValueError("validation_program_included must be a boolean")
    return DesignReviewPackageStatus(
        schema_version=DESIGN_REVIEW_PACKAGE_STATUS_SCHEMA_VERSION,
        package_status="READY_FOR_DESIGN_REVIEW",
        cam_status=CAMStageStatus.VALIDATION_GENERATED,
        blocker_codes=(),
        operations_included=True,
        setup_sheets_included=True,
        nesting_included=True,
        validation_backplot_included=True,
        validation_program_included=validation_program_included,
        physical_cutting_authorized=False,
        required_action=GENERATED_REVIEW_REQUIRED_ACTION,
    )


def blocked_design_review_package_status(
    blocker_codes: Sequence[str],
) -> DesignReviewPackageStatus:
    codes = tuple(blocker_codes)
    if not codes or any(not isinstance(code, str) or not code for code in codes):
        raise ValueError("blocked CAM status requires non-empty blocker codes")
    if len(codes) != 1 or codes[0] not in BLOCKED_CAM_SUPPORTED_BLOCKER_CODES:
        raise ValueError(
            "design-review status v1 only supports exactly one canonical stock, grain, "
            "registration or retention blocker"
        )
    return DesignReviewPackageStatus(
        schema_version=DESIGN_REVIEW_PACKAGE_STATUS_SCHEMA_VERSION,
        package_status="READY_FOR_DESIGN_REVIEW",
        cam_status=CAMStageStatus.BLOCKED,
        blocker_codes=codes,
        operations_included=False,
        setup_sheets_included=False,
        nesting_included=False,
        validation_backplot_included=False,
        validation_program_included=False,
        physical_cutting_authorized=False,
        required_action=BLOCKED_CAM_REQUIRED_ACTIONS[codes[0]],
    )


def validate_design_review_status_retention_binding(
    status: DesignReviewPackageStatus,
    frozen_design: Any,
) -> None:
    """Bind generated/retention-blocked status to canonical frozen topology both ways."""

    retention_blocker = retention_evidence_blocker_code(frozen_design)
    if status.cam_status is CAMStageStatus.VALIDATION_GENERATED and retention_blocker:
        raise ValueError(
            "generated CAM status contradicts unresolved frozen joint retention"
        )
    retention_status_codes = {
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
        BACK_PANEL_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    }
    if len(status.blocker_codes) == 1 and status.blocker_codes[0] in retention_status_codes and (
        retention_blocker != status.blocker_codes[0]
    ):
        raise ValueError(
            "retention blocker contradicts the canonical frozen design"
        )


def normalize_design_review_package_status(
    payload: Mapping[str, Any],
) -> DesignReviewPackageStatus:
    """Strictly parse and re-derive all status invariants without mutating input."""

    if not isinstance(payload, Mapping) or set(payload) != _STATUS_KEYS:
        raise ValueError("design-review package status has an unexpected structure")
    if payload.get("schema_version") != DESIGN_REVIEW_PACKAGE_STATUS_SCHEMA_VERSION:
        raise ValueError("design-review package status schema is unsupported")
    if payload.get("package_status") != "READY_FOR_DESIGN_REVIEW":
        raise ValueError("design-review package is not ready for review")
    if payload.get("physical_cutting_authorized") is not False:
        raise ValueError("design-review package must never authorize physical cutting")

    blocker_codes = payload.get("blocker_codes")
    if (
        not isinstance(blocker_codes, list)
        or any(not isinstance(code, str) or not code for code in blocker_codes)
        or blocker_codes != sorted(set(blocker_codes))
    ):
        raise ValueError("design-review package blocker codes are invalid")
    required_action = payload.get("required_action")
    if not isinstance(required_action, str) or not required_action.strip():
        raise ValueError("design-review package status requires an action")

    flag_names = (
        "operations_included",
        "setup_sheets_included",
        "nesting_included",
        "validation_backplot_included",
        "validation_program_included",
    )
    if any(type(payload.get(name)) is not bool for name in flag_names):
        raise ValueError("design-review package artifact flags must be booleans")
    raw_cam_status = payload.get("cam_status")
    if not isinstance(raw_cam_status, str):
        raise ValueError("design-review package CAM status is unsupported")
    try:
        cam_status = CAMStageStatus(raw_cam_status)
    except (TypeError, ValueError) as exc:
        raise ValueError("design-review package CAM status is unsupported") from exc

    if cam_status is CAMStageStatus.BLOCKED:
        if (
            len(blocker_codes) != 1
            or blocker_codes[0] not in BLOCKED_CAM_SUPPORTED_BLOCKER_CODES
            or any(payload[name] is not False for name in flag_names)
        ):
            raise ValueError("blocked CAM must omit every manufacturing-validation artifact")
        expected_action = BLOCKED_CAM_REQUIRED_ACTIONS[blocker_codes[0]]
    elif (
        blocker_codes
        or payload["operations_included"] is not True
        or payload["setup_sheets_included"] is not True
        or payload["nesting_included"] is not True
        or payload["validation_backplot_included"] is not True
    ):
        raise ValueError("generated CAM status does not match its artifact claims")
    else:
        expected_action = GENERATED_REVIEW_REQUIRED_ACTION
    if required_action != expected_action:
        raise ValueError("design-review package status required action is not canonical")

    return DesignReviewPackageStatus(
        schema_version=DESIGN_REVIEW_PACKAGE_STATUS_SCHEMA_VERSION,
        package_status="READY_FOR_DESIGN_REVIEW",
        cam_status=cam_status,
        blocker_codes=tuple(blocker_codes),
        operations_included=payload["operations_included"],
        setup_sheets_included=payload["setup_sheets_included"],
        nesting_included=payload["nesting_included"],
        validation_backplot_included=payload["validation_backplot_included"],
        validation_program_included=payload["validation_program_included"],
        physical_cutting_authorized=False,
        required_action=required_action,
    )
