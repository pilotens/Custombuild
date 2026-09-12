from __future__ import annotations

from dataclasses import replace

import pytest
from custombuild_cam import (
    build_validation_backplot,
    theoretical_removal_envelopes,
    validate_operations_document,
)
from custombuild_manufacturing import (
    DeterministicNester,
    DFMValidator,
    FeatureKind,
    ManufacturingFeature,
    PartSpec,
    Point2D,
    Rect,
    Severity,
    Side,
    StockSheet,
    generate_operations_document,
    linuxcnc_reference_router_1325,
)
from custombuild_manufacturing.operations import TwoSidedRegistration


@pytest.mark.parametrize("corner_strategy", ("dogbone-v1", "dogbone-v2"))
@pytest.mark.parametrize(
    ("rotated", "side", "expected"),
    (
        (False, Side.A, Rect(7_000, 57_000, 26_000, 86_000)),
        (False, Side.B, Rect(7_000, 457_000, 26_000, 86_000)),
        (True, Side.A, Rect(777_000, 7_000, 86_000, 26_000)),
        (True, Side.B, Rect(777_000, 567_000, 86_000, 26_000)),
    ),
)
def test_open_slot_cutter_exit_is_preserved_through_source_export_and_validation(
    corner_strategy: str, rotated: bool, side: Side, expected: Rect
) -> None:
    groove = ManufacturingFeature(
        "open-slot", "panel", FeatureKind.GROOVE, side, 0, 50_000, 5_000,
        width_um=20_000,
        length_um=80_000,
        corner_strategy=corner_strategy,
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_min",),
    )
    panel = PartSpec(
        "panel", "Panel", 550_000 if rotated else 300_000,
        900_000 if rotated else 200_000, 18_000, "mdf", "v1", features=(groove,),
    )
    stock = StockSheet(
        "sheet", "mdf", "v1", 1_000_000, 600_000, 18_000, margin_um=10_000,
    )
    layout = DeterministicNester().nest((panel,), stock)
    machine = linuxcnc_reference_router_1325()
    document = generate_operations_document(
        design_hash="c" * 64,
        parts=(panel,),
        layout=layout,
        machine=machine,
        two_sided_registration_by_sheet={
            0: TwoSidedRegistration(
                declaration_authority="CLIENT_DECLARED",
                method_id="fixture-registration-v1",
                fixture_method_version="fixture-v1",
                pin_diameter_um=6_000,
                position_tolerance_um=500,
                points=(Point2D(20_000, 580_000), Point2D(900_000, 580_000)),
            )
        } if side == Side.B else None,
    )

    operation = next(item for item in document.operations if item.feature_id == "open-slot")
    assert layout.placements[0].rotated_90 is rotated
    assert Rect(
        operation.cutter_envelope_x_um,
        operation.cutter_envelope_y_um,
        operation.cutter_envelope_width_um,
        operation.cutter_envelope_length_um,
    ) == expected
    assert validate_operations_document(document, machine=machine).valid
    removal = next(
        item for item in theoretical_removal_envelopes(document, machine=machine)
        if item.operation_id == operation.operation_id
    )
    assert (removal.x_min_um, removal.y_min_um, removal.x_max_um, removal.y_max_um) == (
        expected.x_um, expected.y_um, expected.right_um, expected.top_um,
    )

    # Old envelopes clipped the cutter at the nominal opening.  Reject them
    # after the same source rotation/flip, including the B-side upper boundary.
    if not rotated:
        clipped = replace(
            operation, cutter_envelope_x_um=expected.x_um + 3_000,
            cutter_envelope_width_um=expected.width_um - 3_000,
        )
    else:
        clipped = replace(
            operation,
            cutter_envelope_y_um=expected.y_um + (3_000 if side == Side.A else 0),
            cutter_envelope_length_um=expected.height_um - 3_000,
        )
    forged = replace(
        document,
        operations=tuple(clipped if item == operation else item for item in document.operations),
    )
    assert any(
        "cutter envelope does not match versioned corner semantics" in error
        for error in validate_operations_document(forged, machine=machine).errors
    )


@pytest.mark.parametrize(
    ("strategy", "open_edges", "expected", "relief_count"),
    (
        ("dogbone-v1", ("u_min", "u_max"), Rect(-3_000, -3_000, 26_000, 86_000), 4),
        ("dogbone-v2", ("u_min", "u_max"), Rect(-3_000, 0, 26_000, 80_000), 0),
        ("dogbone-v2", ("v_min", "v_max"), Rect(0, -3_000, 20_000, 86_000), 0),
        ("dogbone-v2", ("u_min", "v_min"), Rect(-3_000, -3_000, 26_000, 86_000), 1),
        (
            "dogbone-v2", ("u_min", "u_max", "v_min"),
            Rect(-3_000, -3_000, 26_000, 83_000), 0,
        ),
        (
            "dogbone-v1", ("u_min", "u_max", "v_min", "v_max"),
            Rect(-3_000, -3_000, 26_000, 86_000), 0,
        ),
        (
            "dogbone-v2", ("u_min", "u_max", "v_min", "v_max"),
            Rect(-3_000, -3_000, 26_000, 86_000), 0,
        ),
    ),
)
def test_open_slot_envelope_includes_exits_when_corner_reliefs_are_suppressed(
    strategy: str, open_edges: tuple[str, ...], expected: Rect, relief_count: int
) -> None:
    feature = ManufacturingFeature(
        "open-slot", "panel", FeatureKind.GROOVE, Side.A, 0, 0, 5_000,
        width_um=20_000,
        length_um=80_000,
        corner_strategy=strategy,
        corner_relief_radius_um=3_000,
        open_end_reliefs=open_edges,
    )
    assert len(feature.relief_circles()) == relief_count
    assert feature.machining_bounds() == expected


def test_rotated_a_and_b_features_are_transformed_into_machine_coordinates() -> None:
    features = (
        ManufacturingFeature(
            "hole-a",
            "panel",
            FeatureKind.DRILL,
            Side.A,
            100_000,
            50_000,
            5_000,
            diameter_um=8_000,
        ),
        ManufacturingFeature(
            "hole-b",
            "panel",
            FeatureKind.DRILL,
            Side.B,
            100_000,
            50_000,
            5_000,
            diameter_um=8_000,
        ),
    )
    panel = PartSpec(
        "panel",
        "Panel",
        550_000,
        900_000,
        18_000,
        "mdf",
        "v1",
        features=features,
        grain_direction="NONE",
    )
    source_stock = StockSheet(
        "sheet",
        "mdf",
        "v1",
        1_000_000,
        600_000,
        18_000,
        margin_um=10_000,
        kerf_um=6_000,
        grain_direction="NONE",
        clamp_zones=(
            Rect(16_500, 576_500, 7_000, 7_000),
            Rect(896_500, 576_500, 7_000, 7_000),
        ),
    )
    layout = DeterministicNester().nest((panel,), source_stock)
    document = generate_operations_document(
        design_hash="a" * 64,
        parts=(panel,),
        layout=layout,
        machine=linuxcnc_reference_router_1325(),
        two_sided_registration_by_sheet={
            0: TwoSidedRegistration(
                declaration_authority="CLIENT_DECLARED",
                method_id="fixture-registration-v1",
                fixture_method_version="fixture-v1",
                pin_diameter_um=6_000,
                position_tolerance_um=500,
                points=(Point2D(20_000, 580_000), Point2D(900_000, 580_000)),
            )
        },
    )

    operations = {operation.feature_id: operation for operation in document.operations}
    assert layout.placements[0].rotated_90 is True
    assert (operations["hole-a"].x_um, operations["hole-a"].y_um) == (860_000, 110_000)
    assert (operations["hole-b"].x_um, operations["hole-b"].y_um) == (860_000, 490_000)
    assert {setup.side for setup in document.setups} == {Side.A, Side.B}
    assert "FLIP_STOCK_ABOUT_X_AXIS" in next(
        setup.orientation for setup in document.setups if setup.side == Side.B
    )
    assert validate_operations_document(document).valid

    backplot = build_validation_backplot(document)
    safe_z_by_setup = {setup.setup_id: setup.safe_z_um for setup in document.setups}
    assert all(move.z_um == safe_z_by_setup[move.setup_id] for move in backplot.moves)
    assert set(backplot.omitted_cutting_operation_ids) == {
        operation.operation_id for operation in document.operations
    }


def test_dfm_blocks_real_edge_machining_without_edge_aggregate() -> None:
    feature = ManufacturingFeature(
        "edge-hole",
        "panel",
        FeatureKind.DRILL,
        Side.EDGE,
        50_000,
        50_000,
        10_000,
        diameter_um=8_000,
    )
    panel = PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        features=(feature,),
        grain_direction="NONE",
    )
    source_stock = StockSheet(
        "sheet",
        "mdf",
        "v1",
        1_000_000,
        600_000,
        18_000,
        grain_direction="NONE",
    )
    layout = DeterministicNester().nest((panel,), source_stock)

    report = DFMValidator().validate((panel,), layout, linuxcnc_reference_router_1325())

    assert report.status == Severity.BLOCK
    assert "EDGE_ACCESS_UNAVAILABLE" in {issue.code for issue in report.blocking_issues}


def test_theoretical_removal_is_explicitly_non_physical() -> None:
    feature = ManufacturingFeature(
        "hole",
        "panel",
        FeatureKind.DRILL,
        Side.A,
        50_000,
        50_000,
        10_000,
        diameter_um=8_000,
    )
    panel = PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        features=(feature,),
        grain_direction="NONE",
    )
    source_stock = StockSheet(
        "sheet",
        "mdf",
        "v1",
        1_000_000,
        600_000,
        18_000,
        grain_direction="NONE",
    )
    layout = DeterministicNester().nest((panel,), source_stock)
    document = generate_operations_document(
        design_hash="b" * 64,
        parts=(panel,),
        layout=layout,
        machine=linuxcnc_reference_router_1325(),
    )

    envelopes = theoretical_removal_envelopes(document)

    assert len(envelopes) == 1
    assert envelopes[0].theoretical_only is True
    assert envelopes[0].z_min_um == -10_000
