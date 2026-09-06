from __future__ import annotations

import pytest
from custombuild_manufacturing import (
    DeterministicNester,
    DFMReport,
    FeatureKind,
    ManufacturingFeature,
    NestingLayout,
    PartSpec,
    Point2D,
    ProductionBlockedError,
    Rect,
    Severity,
    Side,
    StockSheet,
    generate_operations_document,
    linuxcnc_reference_router_1325,
)
from custombuild_manufacturing.operations import TwoSidedRegistration


def _two_sided_values(
    *,
    b_outer_contour: bool = False,
) -> tuple[PartSpec, NestingLayout]:
    b_feature = ManufacturingFeature(
        feature_id="b-release" if b_outer_contour else "b-pocket",
        part_id="panel",
        kind=FeatureKind.OUTER_CONTOUR if b_outer_contour else FeatureKind.POCKET,
        side=Side.B,
        x_um=0 if b_outer_contour else 30_000,
        y_um=0 if b_outer_contour else 30_000,
        depth_um=18_000 if b_outer_contour else 6_000,
        width_um=300_000 if b_outer_contour else 40_000,
        length_um=200_000 if b_outer_contour else 50_000,
        through=b_outer_contour,
    )
    features = (
        ManufacturingFeature(
            feature_id="a-hole",
            part_id="panel",
            kind=FeatureKind.DRILL,
            side=Side.A,
            x_um=90_000,
            y_um=80_000,
            depth_um=6_000,
            diameter_um=8_000,
        ),
        ManufacturingFeature(
            feature_id="a-release",
            part_id="panel",
            kind=FeatureKind.OUTER_CONTOUR,
            side=Side.A,
            x_um=0,
            y_um=0,
            depth_um=18_000,
            width_um=300_000,
            length_um=200_000,
            through=True,
        ),
        b_feature,
    )
    panel = PartSpec(
        part_id="panel",
        name="Panel",
        width_um=300_000,
        height_um=200_000,
        thickness_um=18_000,
        material_id="mdf",
        material_version="v1",
        features=features,
        grain_direction="NONE",
    )
    stock = StockSheet(
        stock_id="sheet",
        material_id="mdf",
        material_version="v1",
        width_um=1_000_000,
        height_um=600_000,
        thickness_um=18_000,
        grain_direction="NONE",
        clamp_zones=(
            Rect(16_500, 16_500, 7_000, 7_000),
            Rect(896_500, 16_500, 7_000, 7_000),
        ),
    )
    return panel, DeterministicNester().nest((panel,), stock)


def _registration() -> TwoSidedRegistration:
    return TwoSidedRegistration(
        declaration_authority="CLIENT_DECLARED",
        method_id="fixture-registration-v1",
        fixture_method_version="fixture-v1",
        pin_diameter_um=6_000,
        position_tolerance_um=500,
        points=(Point2D(20_000, 20_000), Point2D(900_000, 20_000)),
    )


def test_two_sided_sheet_without_coordinate_registration_is_blocked() -> None:
    panel, layout = _two_sided_values()

    with pytest.raises(
        ProductionBlockedError,
        match="TWO_SIDED_REGISTRATION_MISSING",
    ) as caught:
        generate_operations_document(
            design_hash="a" * 64,
            parts=(panel,),
            layout=layout,
            machine=linuxcnc_reference_router_1325(),
            validate=False,
        )

    assert isinstance(caught.value.report, DFMReport)
    assert caught.value.report.status == Severity.BLOCK
    assert {issue.code for issue in caught.value.report.blocking_issues} == {
        "TWO_SIDED_REGISTRATION_MISSING"
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"method_id": ""}, "method ID"),
        (
            {"points": (Point2D(20_000, 20_000), Point2D(20_000, 20_000))},
            "unique",
        ),
        (
            {"points": (Point2D(20_000, 20_000), Point2D(21_000, 20_000))},
            "usable baseline",
        ),
        (
            {"points": (Point2D(True, 20_000), Point2D(900_000, 20_000))},
            "coordinates must be integers",
        ),
    ),
)
def test_invalid_two_sided_registration_declaration_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "declaration_authority": "CLIENT_DECLARED",
        "method_id": "fixture-registration-v1",
        "fixture_method_version": "fixture-v1",
        "pin_diameter_um": 6_000,
        "position_tolerance_um": 500,
        "points": (Point2D(20_000, 20_000), Point2D(900_000, 20_000)),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        TwoSidedRegistration(**values)  # type: ignore[arg-type]


def test_registration_pin_outside_stock_is_blocked() -> None:
    panel, layout = _two_sided_values()
    registration = TwoSidedRegistration(
        declaration_authority="CLIENT_DECLARED",
        method_id="fixture-registration-v1",
        fixture_method_version="fixture-v1",
        pin_diameter_um=6_000,
        position_tolerance_um=500,
        points=(Point2D(20_000, 20_000), Point2D(1_100_000, 20_000)),
    )

    with pytest.raises(ProductionBlockedError, match="TWO_SIDED_REGISTRATION_INVALID"):
        generate_operations_document(
            design_hash="b" * 64,
            parts=(panel,),
            layout=layout,
            machine=linuxcnc_reference_router_1325(),
            validate=False,
            two_sided_registration_by_sheet={0: registration},
        )


def test_side_b_precedes_side_a_and_release_contours_are_globally_last() -> None:
    panel, layout = _two_sided_values()
    machine = linuxcnc_reference_router_1325()

    document = generate_operations_document(
        design_hash="c" * 64,
        parts=(panel,),
        layout=layout,
        machine=machine,
        validate=False,
        two_sided_registration_by_sheet={0: _registration()},
    )

    assert [setup.side for setup in document.setups] == [Side.B, Side.A]
    assert [operation.feature_id for operation in document.operations] == [
        "b-pocket",
        "a-hole",
        "a-release",
    ]
    assert document.operations[-1].through is True
    assert document.mode == "VALIDATION"
    for setup in document.setups:
        assert setup.wcs in machine.wcs_codes
        assert setup.fixture.startswith("EXTERNAL_FIXTURE_PLAN_REQUIRED")
        assert "METHOD=fixture-registration-v1" in setup.probe_method
        assert "STOCK_XY_UM=20000,20000|900000,20000" in setup.probe_method


def test_conflicting_b_release_and_remaining_a_work_is_blocked() -> None:
    panel, layout = _two_sided_values(b_outer_contour=True)

    with pytest.raises(ProductionBlockedError, match="SETUP_SEQUENCE_CONFLICT") as caught:
        generate_operations_document(
            design_hash="d" * 64,
            parts=(panel,),
            layout=layout,
            machine=linuxcnc_reference_router_1325(),
            validate=False,
            two_sided_registration_by_sheet={0: _registration()},
        )

    assert isinstance(caught.value.report, DFMReport)
    assert caught.value.report.status == Severity.BLOCK
