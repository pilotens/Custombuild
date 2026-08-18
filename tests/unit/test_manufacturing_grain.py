from __future__ import annotations

from dataclasses import replace

import pytest
from custombuild_manufacturing import (
    DFM_GRAIN_BLOCKER_CODE,
    DFM_GRAIN_REQUIRED_ACTION,
    DFM_GRAIN_RULE_MESSAGE,
    DFM_GRAIN_RULE_TITLE,
    DFM_GRAIN_RULE_VERSION,
    DFM_GRAIN_STOCK_MATCHED_PHASE,
    DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE,
    DeterministicNester,
    DFMIssue,
    DFMValidator,
    PartSpec,
    Severity,
    StockSheet,
    grain_control_projection,
    linuxcnc_reference_router_1325,
    stock_grain_binding_issues,
    validate_stock_grain_binding_issue,
)


def part(*, grain_direction: str = "X") -> PartSpec:
    return PartSpec(
        "directional-panel",
        "Directional panel",
        900_000,
        500_000,
        18_000,
        "birch-plywood",
        "screening-1.0.0",
        grain_direction=grain_direction,
    )


def stock(*, grain_direction: str = "NONE") -> StockSheet:
    return StockSheet(
        "birch-stock",
        "birch-plywood",
        "screening-1.0.0",
        2_440_000,
        1_220_000,
        18_000,
        quantity=1,
        grain_direction=grain_direction,
    )


@pytest.mark.parametrize(
    "unbound_axis",
    ("NONE", "ANY", "UNSPECIFIED", "UNKNOWN", "LENGTH"),
)
def test_matched_directional_stock_without_exact_axis_is_one_canonical_blocker(
    unbound_axis: str,
) -> None:
    issues = stock_grain_binding_issues((part(),), stock(grain_direction=unbound_axis))

    assert len(issues) == 1
    issue = issues[0]
    validate_stock_grain_binding_issue(
        issue,
        expected_severity=Severity.BLOCK,
        expected_phase=DFM_GRAIN_STOCK_MATCHED_PHASE,
    )
    assert issue.code == DFM_GRAIN_BLOCKER_CODE
    assert issue.message == DFM_GRAIN_RULE_MESSAGE
    assert issue.suggestion == DFM_GRAIN_REQUIRED_ACTION
    assert issue.inputs == {
        "binding_status": "MISSING_INFORMATION",
        "assessment_phase": DFM_GRAIN_STOCK_MATCHED_PHASE,
        "stock_id": "birch-stock",
        "material_id": "birch-plywood",
        "material_version": "screening-1.0.0",
        "stock_grain_direction": "UNBOUND",
        "required_part_grain_directions": ("X",),
        "affected_part_ids": ("directional-panel",),
    }


@pytest.mark.parametrize("bound_axis", ("X", "Y", " x ", "y"))
def test_exact_structured_stock_axis_clears_the_binding_issue(bound_axis: str) -> None:
    assert stock_grain_binding_issues((part(),), stock(grain_direction=bound_axis)) == ()


def test_catalog_effective_non_directional_part_needs_no_stock_axis() -> None:
    source = part(grain_direction="NONE")
    source_stock = stock(grain_direction="UNSPECIFIED")

    assert stock_grain_binding_issues((source,), source_stock) == ()
    assert DeterministicNester().nest((source,), source_stock).is_complete


def test_incomplete_stock_selection_projects_one_canonical_warning() -> None:
    issues = stock_grain_binding_issues((part(),), None, severity=Severity.WARNING)

    assert len(issues) == 1
    validate_stock_grain_binding_issue(
        issues[0],
        expected_severity=Severity.WARNING,
        expected_phase=DFM_GRAIN_STOCK_SELECTION_INCOMPLETE_PHASE,
    )
    projection = grain_control_projection(issues)
    assert projection is not None
    assert projection["rule_id"] == DFM_GRAIN_BLOCKER_CODE
    assert projection["rule_version"] == DFM_GRAIN_RULE_VERSION
    assert projection["title"] == DFM_GRAIN_RULE_TITLE
    assert projection["status"] == Severity.WARNING.value
    assert projection["affected_part_ids"] == ("directional-panel",)


def test_strict_validator_rejects_checksum_consistent_truth_drift() -> None:
    canonical = stock_grain_binding_issues((part(),), stock())[0]
    mutations = (
        replace(canonical, message="Invented message"),
        replace(canonical, suggestion="Acknowledge the warning."),
        replace(canonical, part_id="directional-panel"),
        replace(canonical, inputs={**canonical.inputs, "stock_id": None}),
        replace(
            canonical,
            inputs={**canonical.inputs, "affected_part_ids": ("z", "a")},
        ),
        replace(
            canonical,
            inputs={**canonical.inputs, "required_part_grain_directions": ("Z",)},
        ),
    )

    for issue in mutations:
        with pytest.raises(ValueError):
            validate_stock_grain_binding_issue(issue)


def test_dfm_validator_keeps_grain_blocker_when_direct_nesting_is_called() -> None:
    source = part()
    source_stock = stock()
    layout = DeterministicNester().nest((source,), source_stock)

    report = DFMValidator().validate(
        (source,),
        layout,
        linuxcnc_reference_router_1325(),
    )

    grain_issues = tuple(
        issue for issue in report.blocking_issues if issue.code == DFM_GRAIN_BLOCKER_CODE
    )
    assert len(grain_issues) == 1
    assert layout.placements == ()
    assert "NESTING_UNPLACED" in {issue.code for issue in report.blocking_issues}


def test_projection_rejects_noncanonical_grain_issue() -> None:
    forged = DFMIssue(
        DFM_GRAIN_BLOCKER_CODE,
        Severity.WARNING,
        "Plausible but non-canonical text",
    )

    with pytest.raises(ValueError):
        grain_control_projection((forged,))
