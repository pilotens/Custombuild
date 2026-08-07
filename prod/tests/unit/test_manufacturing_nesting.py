from __future__ import annotations

from custombuild_manufacturing import (
    DeterministicNester,
    NestingLayout,
    PartSpec,
    Placement,
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


def test_nesting_accounts_for_every_unplaceable_instance() -> None:
    oversized = part("oversized", 2_000_000, 2_000_000)

    layout = DeterministicNester().nest((oversized,), stock())
    issues = validate_layout(layout, expand_part_instances((oversized,)))

    assert layout.unplaced_instance_ids == ("oversized:001",)
    assert {issue.code for issue in issues} == {"NESTING_UNPLACED"}
