from __future__ import annotations

from dataclasses import replace

import pytest
from custombuild_manufacturing import (
    DeterministicNester,
    NestingLayout,
    PartSpec,
    Placement,
    Severity,
    StockSheet,
    expand_part_instances,
    validate_layout,
)


def stock(*, width_um: int = 1_000_000, height_um: int = 600_000) -> StockSheet:
    return StockSheet(
        "sheet",
        "mdf",
        "v1",
        width_um,
        height_um,
        18_000,
        margin_um=10_000,
        kerf_um=5_000,
        grain_direction="NONE",
    )


def part(part_id: str, width_um: int, height_um: int) -> PartSpec:
    return PartSpec(
        part_id,
        part_id,
        width_um,
        height_um,
        18_000,
        "mdf",
        "v1",
        grain_direction="NONE",
    )


def test_nesting_rotates_only_when_required_and_is_reproducible() -> None:
    source = part("wide-panel", 550_000, 900_000)
    nester = DeterministicNester()

    first = nester.nest((source,), stock())
    second = nester.nest((source,), stock())

    assert first == second
    assert first.is_complete
    assert len(first.placements) == 1
    placement = first.placements[0]
    assert placement.rotated_90 is True
    assert (placement.width_um, placement.height_um) == (900_000, 550_000)
    assert (placement.x_um, placement.y_um) == (10_000, 10_000)


def test_nesting_does_not_rotate_a_grain_bound_part_onto_same_grain_stock() -> None:
    source = PartSpec(
        "grain-bound-panel",
        "grain-bound-panel",
        550_000,
        900_000,
        18_000,
        "mdf",
        "v1",
        grain_direction="X",
    )
    source_stock = StockSheet(
        "grain-bound-sheet",
        "mdf",
        "v1",
        1_000_000,
        600_000,
        18_000,
        margin_um=10_000,
        kerf_um=5_000,
        grain_direction="X",
    )

    layout = DeterministicNester().nest((source,), source_stock)
    issues = validate_layout(layout, expand_part_instances((source,)))

    assert layout.placements == ()
    assert layout.unplaced_instance_ids == ("grain-bound-panel:001",)
    assert {issue.code for issue in issues} == {"NESTING_UNPLACED"}


@pytest.mark.parametrize("part_grain,stock_grain", (("X", "Y"), ("Y", "X")))
def test_square_panel_rotates_to_align_grain(part_grain: str, stock_grain: str) -> None:
    source = replace(part("square-panel", 300_000, 300_000), grain_direction=part_grain)
    source_stock = replace(stock(), grain_direction=stock_grain)

    layout = DeterministicNester().nest((source,), source_stock)

    assert layout.is_complete
    assert len(layout.placements) == 1
    placement = layout.placements[0]
    assert placement.rotated_90 is True
    assert (placement.width_um, placement.height_um) == (300_000, 300_000)
    assert validate_layout(layout, expand_part_instances((source,))) == ()
    assert DeterministicNester().nest((source,), source_stock) == layout


@pytest.mark.parametrize("grain", ("X", "Y", "NONE"))
def test_square_panel_keeps_local_axes_when_rotation_is_unnecessary(grain: str) -> None:
    source = replace(part("square-panel", 300_000, 300_000), grain_direction=grain)
    source_stock = replace(stock(), grain_direction=grain)

    layout = DeterministicNester().nest((source,), source_stock)

    assert layout.is_complete
    assert layout.placements[0].rotated_90 is False


@pytest.mark.parametrize("part_rotation,stock_rotation", ((False, True), (True, False)))
def test_square_panel_grain_alignment_respects_rotation_permissions(
    part_rotation: bool, stock_rotation: bool
) -> None:
    source = replace(
        part("square-panel", 300_000, 300_000), grain_direction="X", allow_rotation=part_rotation
    )
    source_stock = replace(stock(), grain_direction="Y", allow_rotation=stock_rotation)

    layout = DeterministicNester().nest((source,), source_stock)

    assert layout.placements == ()
    assert layout.unplaced_instance_ids == ("square-panel:001",)


@pytest.mark.parametrize(
    "unbound_stock_axis",
    ("NONE", "ANY", "UNSPECIFIED", "UNKNOWN", "", "LENGTH"),
)
def test_directional_part_never_uses_an_unbound_stock_axis(
    unbound_stock_axis: str,
) -> None:
    source = PartSpec(
        "directional-panel",
        "directional-panel",
        550_000,
        900_000,
        18_000,
        "mdf",
        "v1",
        grain_direction="X",
    )
    source_stock = replace(stock(), grain_direction=unbound_stock_axis)

    layout = DeterministicNester().nest((source,), source_stock)

    assert layout.placements == ()
    assert layout.unplaced_instance_ids == ("directional-panel:001",)


def test_layout_validation_detects_overlap_including_kerf_clearance() -> None:
    first = part("first", 300_000, 200_000)
    second = part("second", 300_000, 200_000)
    source_stock = stock()
    placements = (
        Placement("first:001", "first", "sheet", 0, 10_000, 10_000, 300_000, 200_000, False),
        Placement(
            "second:001",
            "second",
            "sheet",
            0,
            312_000,
            10_000,
            300_000,
            200_000,
            False,
        ),
    )
    layout = NestingLayout(source_stock, placements, (), 1, 300_000, "manual-v1")

    issues = validate_layout(layout, expand_part_instances((first, second)))

    assert "NESTING_OVERLAP" in {issue.code for issue in issues}


def test_layout_validation_rejects_mutated_part_identity_binding() -> None:
    source = part("panel", 300_000, 200_000)
    instances = expand_part_instances((source,))
    layout = DeterministicNester().nest(instances, stock())
    mutated_placement = replace(layout.placements[0], part_id="substituted-panel")
    mutated_layout = replace(layout, placements=(mutated_placement,))

    issues = validate_layout(mutated_layout, instances)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "NESTING_PART_BINDING_MISMATCH"
    assert issue.message == ("Placement part identity does not match the referenced part instance.")
    assert issue.severity is Severity.BLOCK
    assert issue.part_id == "panel"
    assert issue.inputs == {
        "instance_id": "panel:001",
        "expected_part_id": "panel",
        "actual_part_id": "substituted-panel",
    }


def test_layout_validation_rejects_mutated_stock_identity_binding() -> None:
    source = part("panel", 300_000, 200_000)
    instances = expand_part_instances((source,))
    layout = DeterministicNester().nest(instances, stock())
    mutated_placement = replace(layout.placements[0], stock_id="substituted-sheet")
    mutated_layout = replace(layout, placements=(mutated_placement,))

    issues = validate_layout(mutated_layout, instances)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "NESTING_STOCK_BINDING_MISMATCH"
    assert issue.message == ("Placement stock identity does not match the nesting layout stock.")
    assert issue.severity is Severity.BLOCK
    assert issue.part_id == "panel"
    assert issue.inputs == {
        "instance_id": "panel:001",
        "expected_stock_id": "sheet",
        "actual_stock_id": "substituted-sheet",
    }


def test_nesting_accounts_for_every_unplaceable_instance() -> None:
    oversized = part("oversized", 2_000_000, 2_000_000)

    layout = DeterministicNester().nest((oversized,), stock())
    issues = validate_layout(layout, expand_part_instances((oversized,)))

    assert layout.unplaced_instance_ids == ("oversized:001",)
    assert {issue.code for issue in issues} == {"NESTING_UNPLACED"}
