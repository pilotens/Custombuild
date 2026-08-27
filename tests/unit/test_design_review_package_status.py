from __future__ import annotations

from dataclasses import replace

import pytest
from custombuild_manufacturing import (
    DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    DFM_GRAIN_BLOCKER_CODE,
    DFM_GRAIN_REQUIRED_ACTION,
    CAMStageStatus,
    DesignReviewPackageStatus,
    blocked_design_review_package_status,
    dado_retention_evidence_missing,
    generated_design_review_package_status,
    normalize_design_review_package_status,
)


@pytest.mark.parametrize(
    "blocker_code",
    (
        "TWO_SIDED_REGISTRATION_MISSING",
        "STOCK_PROFILE_MISSING",
        DFM_GRAIN_BLOCKER_CODE,
        DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,
    ),
)
def test_blocked_cam_status_is_review_ready_but_never_machine_ready(
    blocker_code: str,
) -> None:
    status = blocked_design_review_package_status((blocker_code,))

    parsed = normalize_design_review_package_status(status.as_dict())

    assert parsed.package_status == "READY_FOR_DESIGN_REVIEW"
    assert parsed.cam_status is CAMStageStatus.BLOCKED
    assert parsed.blocker_codes == (blocker_code,)
    assert parsed.operations_included is False
    assert parsed.setup_sheets_included is False
    assert parsed.nesting_included is False
    assert parsed.validation_backplot_included is False
    assert parsed.validation_program_included is False
    assert parsed.physical_cutting_authorized is False


def test_generated_cam_status_keeps_physical_cutting_unauthorized() -> None:
    status = generated_design_review_package_status(validation_program_included=False)

    parsed = normalize_design_review_package_status(status.as_dict())

    assert parsed.cam_status is CAMStageStatus.VALIDATION_GENERATED
    assert parsed.operations_included is True
    assert parsed.setup_sheets_included is True
    assert parsed.nesting_included is True
    assert parsed.validation_backplot_included is True
    assert parsed.validation_program_included is False
    assert parsed.physical_cutting_authorized is False


@pytest.mark.parametrize(
    "blocker_codes",
    (
        ("SOME_OTHER_BLOCKER",),
        ("TWO_SIDED_REGISTRATION_MISSING", "TWO_SIDED_REGISTRATION_MISSING"),
        ("STOCK_PROFILE_MISSING", "STOCK_PROFILE_MISSING"),
        (DFM_GRAIN_BLOCKER_CODE, DFM_GRAIN_BLOCKER_CODE),
        ("STOCK_PROFILE_MISSING", "TWO_SIDED_REGISTRATION_MISSING"),
        ("STOCK_PROFILE_MISSING", DFM_GRAIN_BLOCKER_CODE),
        ("TWO_SIDED_REGISTRATION_MISSING", "SOME_OTHER_BLOCKER"),
    ),
)
def test_blocked_cam_status_v1_rejects_unsupported_blockers(
    blocker_codes: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="only supports"):
        blocked_design_review_package_status(blocker_codes)

    payload = blocked_design_review_package_status(("TWO_SIDED_REGISTRATION_MISSING",)).as_dict()
    payload["blocker_codes"] = sorted(blocker_codes)
    with pytest.raises(ValueError, match="blocker codes|blocked CAM"):
        normalize_design_review_package_status(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("physical_cutting_authorized", True),
        ("operations_included", True),
        ("setup_sheets_included", True),
        ("nesting_included", True),
        ("validation_backplot_included", True),
        ("validation_program_included", True),
        ("blocker_codes", []),
        ("required_action", "   "),
    ),
)
def test_blocked_cam_status_rejects_unsafe_or_inconsistent_claims(
    field: str, value: object
) -> None:
    payload = blocked_design_review_package_status(("TWO_SIDED_REGISTRATION_MISSING",)).as_dict()
    payload[field] = value

    with pytest.raises(ValueError):
        normalize_design_review_package_status(payload)


def test_status_dataclass_cannot_be_normalized_after_unknown_schema() -> None:
    status = generated_design_review_package_status(validation_program_included=True)
    payload = replace(status, schema_version="custombuild.design-review-package-status.v999")

    with pytest.raises(ValueError, match="schema"):
        normalize_design_review_package_status(payload.as_dict())


@pytest.mark.parametrize(
    "status",
    (
        blocked_design_review_package_status(("TWO_SIDED_REGISTRATION_MISSING",)),
        blocked_design_review_package_status(("STOCK_PROFILE_MISSING",)),
        blocked_design_review_package_status((DFM_GRAIN_BLOCKER_CODE,)),
        blocked_design_review_package_status(
            (DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,)
        ),
        generated_design_review_package_status(validation_program_included=True),
    ),
)
def test_status_rejects_checksum_consistent_required_action_drift(
    status: DesignReviewPackageStatus,
) -> None:
    payload = status.as_dict()
    payload["required_action"] = "Use an invented large-format profile."

    with pytest.raises(ValueError, match="required action"):
        normalize_design_review_package_status(payload)


def test_grain_blocker_status_uses_the_single_canonical_required_action() -> None:
    status = blocked_design_review_package_status((DFM_GRAIN_BLOCKER_CODE,))

    assert status.blocker_codes == (DFM_GRAIN_BLOCKER_CODE,)
    assert status.required_action == DFM_GRAIN_REQUIRED_ACTION


def test_dado_retention_blocker_cannot_be_replaced_by_review_text() -> None:
    status = blocked_design_review_package_status(
        (DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,)
    )

    assert status.blocker_codes == (DADO_RETENTION_EVIDENCE_MISSING_BLOCKER_CODE,)
    assert "checksum-addressed" in status.required_action
    assert "review acknowledgement" in status.required_action


class _JointType:
    def __init__(self, value: object) -> None:
        self.value = value


class _Joint:
    def __init__(self, joint_type: object) -> None:
        self.joint_type = joint_type


class _DesignResult:
    def __init__(self, joints: tuple[object, ...]) -> None:
        self.joints = joints


def test_retention_detection_fails_closed_without_canonical_spec_and_topology() -> None:
    assert dado_retention_evidence_missing(_DesignResult((_Joint(_JointType("DaDo")),)))
    assert dado_retention_evidence_missing(_DesignResult((_Joint("RABBET"),)))
    assert dado_retention_evidence_missing(object())


@pytest.mark.parametrize("value", (None, 0, 1, "false"))
def test_generated_status_requires_an_actual_boolean(value: object) -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        generated_design_review_package_status(validation_program_included=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("blocker_codes", ((), ("",), (None,)))
def test_blocked_status_rejects_empty_or_non_string_codes(
    blocker_codes: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError, match="non-empty blocker codes"):
        blocked_design_review_package_status(blocker_codes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", "custombuild.design-review-package-status.v2", "schema"),
        ("package_status", "RELEASED", "not ready"),
        ("operations_included", 1, "must be booleans"),
        ("cam_status", None, "CAM status"),
        ("cam_status", "RELEASED", "CAM status"),
    ),
)
def test_status_parser_rejects_unsupported_identity_and_typed_claims(
    field: str,
    value: object,
    message: str,
) -> None:
    payload = generated_design_review_package_status(
        validation_program_included=True
    ).as_dict()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        normalize_design_review_package_status(payload)


@pytest.mark.parametrize("mutation", ({"unexpected": True}, {"schema_version": None}))
def test_status_parser_rejects_missing_or_extra_fields(
    mutation: dict[str, object],
) -> None:
    payload = generated_design_review_package_status(
        validation_program_included=True
    ).as_dict()
    if mutation == {"schema_version": None}:
        del payload["schema_version"]
    else:
        payload.update(mutation)

    with pytest.raises(ValueError, match="unexpected structure"):
        normalize_design_review_package_status(payload)


def test_generated_status_cannot_omit_a_manufacturing_artifact() -> None:
    payload = generated_design_review_package_status(
        validation_program_included=True
    ).as_dict()
    payload["nesting_included"] = False

    with pytest.raises(ValueError, match="does not match"):
        normalize_design_review_package_status(payload)
