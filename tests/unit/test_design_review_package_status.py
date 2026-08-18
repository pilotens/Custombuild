from __future__ import annotations

from dataclasses import replace

import pytest
from custombuild_manufacturing import (
    DFM_GRAIN_BLOCKER_CODE,
    DFM_GRAIN_REQUIRED_ACTION,
    CAMStageStatus,
    DesignReviewPackageStatus,
    blocked_design_review_package_status,
    generated_design_review_package_status,
    normalize_design_review_package_status,
)


@pytest.mark.parametrize(
    "blocker_code",
    (
        "TWO_SIDED_REGISTRATION_MISSING",
        "STOCK_PROFILE_MISSING",
        DFM_GRAIN_BLOCKER_CODE,
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
