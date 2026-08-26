"""Strict status contract for checksum-bound design-review packages.

The package can be complete for design review while the downstream CAM stage is
deliberately blocked.  This contract keeps those two claims separate so neither
the API nor the UI has to infer machine readiness from the presence of a ZIP.
"""

from __future__ import annotations

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
BLOCKED_CAM_SUPPORTED_BLOCKER_CODES = (
    STOCK_PROFILE_MISSING_BLOCKER_CODE,
    DFM_GRAIN_BLOCKER_CODE,
    TWO_SIDED_REGISTRATION_MISSING_BLOCKER_CODE,
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
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
        "Bind a versioned, checksum-addressed dry self-locking joint or mechanical "
        "retention system for every DADO joint; a review acknowledgement, adhesive or "
        "geometric bearing check is not retention evidence."
    ),
}


def dado_retention_evidence_missing(design_result: Any) -> bool:
    """Return true while any plain DADO lacks a structured retention contract.

    The current domain joint schema proves the groove geometry and local bearing
    path, but it deliberately has no field that can identify a versioned dry or
    mechanical retention system.  Treat every DADO as unresolved until that
    domain contract exists; caller-supplied review text must never substitute for
    a product-modelled retention declaration.
    """

    for joint in getattr(design_result, "joints", ()):
        joint_type = getattr(joint, "joint_type", None)
        value = getattr(joint_type, "value", joint_type)
        if isinstance(value, str) and value.casefold() == "dado":
            return True
    return False


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
