from __future__ import annotations

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
    Severity,
    Side,
    StockSheet,
    generate_operations_document,
    linuxcnc_reference_router_1325,
)
from custombuild_manufacturing.operations import TwoSidedRegistration


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
        kerf_um=5_000,
        grain_direction="NONE",
    )
    layout = DeterministicNester().nest((panel,), source_stock)
    document = generate_operations_document(
        design_hash="a" * 64,
        parts=(panel,),
        layout=layout,
        machine=linuxcnc_reference_router_1325(),
        two_sided_registration_by_sheet={
            0: TwoSidedRegistration(
                method_id="fixture-registration-v1",
                points=(Point2D(20_000, 20_000), Point2D(900_000, 20_000)),
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
