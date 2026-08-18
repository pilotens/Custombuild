from __future__ import annotations

import copy
import io
from dataclasses import replace

import ezdxf
import pytest
from custombuild_manufacturing import (
    DeterministicNester,
    DFMIssue,
    DFMReport,
    DFMValidator,
    EdgeBandSpec,
    FeatureKind,
    MachineProfile,
    ManufacturingFeature,
    NestingLayout,
    OperationKind,
    OperationsDocument,
    PanelAxisMapping,
    PartInstance,
    PartSpec,
    Placement,
    Rect,
    Severity,
    Side,
    StockSheet,
    ToolSpec,
    canonical_json_bytes,
    linuxcnc_reference_router_1325,
    um_to_mm,
    validate_layout,
)
from custombuild_manufacturing.dfm import (
    select_tool,
    transform_point_to_machine,
    transform_rect_to_machine,
)
from custombuild_manufacturing.exporters import (
    bom_csv,
    cut_list_csv,
    dxf_for_part,
    nesting_svg,
    svg_for_part,
)
from custombuild_manufacturing.model import coerce_part_instances


def base_stock(**changes) -> StockSheet:
    values = {
        "stock_id": "sheet",
        "material_id": "mdf",
        "material_version": "v1",
        "width_um": 1_000_000,
        "height_um": 600_000,
        "thickness_um": 18_000,
        "margin_um": 10_000,
        "kerf_um": 5_000,
        "grain_direction": "NONE",
    }
    values.update(changes)
    return StockSheet(**values)


def base_part(*features: ManufacturingFeature) -> PartSpec:
    return PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        features=features,
        grain_direction="NONE",
    )


@pytest.mark.parametrize(
    "factory",
    (
        lambda: PanelAxisMapping("x", "x", "z"),
        lambda: ManufacturingFeature("", "p", FeatureKind.DRILL, Side.A, 1, 1, 1),
        lambda: ManufacturingFeature("f", "p", FeatureKind.DRILL, Side.A, 1, 1, 0),
        lambda: ManufacturingFeature(
            "f", "p", FeatureKind.DRILL_PATTERN, Side.A, 1, 1, 1, pattern_count=2
        ),
        lambda: ManufacturingFeature("f", "p", FeatureKind.DRILL, Side.A, 1, 1, 1, diameter_um=-1),
        lambda: ManufacturingFeature(
            "f",
            "p",
            FeatureKind.GROOVE,
            Side.A,
            1,
            1,
            1,
            width_um=1,
            length_um=1,
            open_end_reliefs=("u_min",),
        ),
        lambda: PartSpec("p", "P", 0, 1, 1, "m", "v"),
        lambda: PartSpec("p", "P", 1, 1, 1, "m", "v", quantity=0),
        lambda: PartSpec(
            "p",
            "P",
            1,
            1,
            1,
            "m",
            "v",
            features=(ManufacturingFeature("f", "other", FeatureKind.DRILL, Side.A, 1, 1, 1),),
        ),
        lambda: ToolSpec("t", "T", 0, 1, (OperationKind.DRILL,), 1, 1, 1),
        lambda: ToolSpec("t", "T", 1, 1, (OperationKind.DRILL,), 1, 0, 1),
        lambda: MachineProfile("m", "M", "1", "c", 0, 1, 1, 1, 1, (), ()),
        lambda: MachineProfile("m", "M", "1", "c", 1, 1, 1, 2, 1, (), ()),
        lambda: StockSheet("s", "m", "v", 1, 1, 1, quantity=0),
        lambda: StockSheet("s", "m", "v", 100, 100, 1, margin_um=50),
        lambda: OperationsDocument("v", "h", "m", "1", (), (), "PRODUCTION"),
    ),
)
def test_models_reject_invalid_manufacturing_state(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_canonical_units_status_and_instance_coercion() -> None:
    part = replace(base_part(), quantity=2)
    instances = coerce_part_instances((part,))

    assert [item.instance_id for item in instances] == ["panel:001", "panel:002"]
    assert coerce_part_instances(instances) == instances
    with pytest.raises(TypeError):
        coerce_part_instances((part, instances[0]))
    assert um_to_mm(-1_250) == "-1.25"
    assert um_to_mm(2_000) == "2"
    assert canonical_json_bytes({"set": {"b", "a"}, "status": Severity.WARNING}) == (
        b'{"set":["a","b"],"status":"WARNING"}'
    )
    assert DFMReport(()).status == Severity.PASS
    warning = DFMIssue("W", Severity.WARNING, "warning")
    blocking = DFMIssue("B", Severity.BLOCK, "blocking")
    assert DFMReport((warning,)).status == Severity.WARNING
    assert DFMReport((warning, blocking)).blocking_issues == (blocking,)


def test_dfm_deduplication_preserves_distinct_instances_and_collapses_exact_issues() -> None:
    missing_diameter = ManufacturingFeature(
        "missing-diameter",
        "panel",
        FeatureKind.DRILL,
        Side.A,
        50_000,
        50_000,
        5_000,
    )
    part = replace(base_part(missing_diameter), quantity=2)
    stock = base_stock(width_um=250_000, height_um=150_000)
    layout = DeterministicNester().nest((part,), stock)

    report = DFMValidator().validate((part,), layout, linuxcnc_reference_router_1325())

    unplaced = [issue for issue in report.issues if issue.code == "NESTING_UNPLACED"]
    assert [issue.inputs["instance_id"] for issue in unplaced] == [
        "panel:001",
        "panel:002",
    ]
    assert sum(issue.code == "DRILL_DIAMETER_MISSING" for issue in report.issues) == 1
    assert sum(issue.code == "COMPATIBLE_TOOL_MISSING" for issue in report.issues) == 1
    assert report.engine_version == "dfm-1.3.0"


def test_dogbone_actual_envelope_drives_boundary_and_collision_validation() -> None:
    edge_near = ManufacturingFeature(
        "edge-near",
        "panel",
        FeatureKind.GROOVE,
        Side.A,
        2_000,
        50_000,
        5_000,
        width_um=20_000,
        length_um=80_000,
        corner_strategy="dogbone-v1",
        corner_relief_radius_um=3_000,
    )
    assert edge_near.bounds() == Rect(2_000, 50_000, 20_000, 80_000)
    assert edge_near.machining_bounds() == Rect(-1_000, 47_000, 26_000, 86_000)

    edge_part = base_part(edge_near)
    edge_layout = DeterministicNester().nest((edge_part,), base_stock())
    edge_report = DFMValidator().validate(
        (edge_part,), edge_layout, linuxcnc_reference_router_1325()
    )
    assert "FEATURE_OUTSIDE_PART" in {issue.code for issue in edge_report.blocking_issues}

    exact_open = replace(edge_near, x_um=0, open_end_reliefs=("u_min",))
    open_part = base_part(exact_open)
    open_layout = DeterministicNester().nest((open_part,), base_stock())
    open_report = DFMValidator().validate(
        (open_part,), open_layout, linuxcnc_reference_router_1325()
    )
    assert "FEATURE_OUTSIDE_PART" not in {issue.code for issue in open_report.blocking_issues}

    nearby = ManufacturingFeature(
        "nearby",
        "panel",
        FeatureKind.POCKET,
        Side.A,
        42_000,
        49_000,
        5_000,
        width_um=10_000,
        length_um=10_000,
    )
    collision_part = base_part(
        replace(edge_near, x_um=20_000, y_um=50_000),
        nearby,
    )
    collision_layout = DeterministicNester().nest((collision_part,), base_stock())
    collision_report = DFMValidator().validate(
        (collision_part,), collision_layout, linuxcnc_reference_router_1325()
    )
    assert "FEATURE_COLLISION" in {issue.code for issue in collision_report.blocking_issues}

    broad_bounds_only = base_part(
        replace(edge_near, x_um=20_000, y_um=50_000),
        replace(nearby, y_um=60_000),
    )
    broad_bounds_layout = DeterministicNester().nest((broad_bounds_only,), base_stock())
    broad_bounds_report = DFMValidator().validate(
        (broad_bounds_only,), broad_bounds_layout, linuxcnc_reference_router_1325()
    )
    assert "FEATURE_COLLISION" not in {issue.code for issue in broad_bounds_report.blocking_issues}


def test_open_end_relief_envelope_blocks_insufficient_nesting_spacing() -> None:
    exit_feature = ManufacturingFeature(
        "open-exit",
        "a-exit",
        FeatureKind.GROOVE,
        Side.A,
        290_000,
        50_000,
        5_000,
        width_um=10_000,
        length_um=80_000,
        corner_strategy="dogbone-v1",
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_max",),
    )
    first = PartSpec(
        "a-exit",
        "Exit panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        features=(exit_feature,),
    )
    second = PartSpec("b-neighbor", "Neighbor", 300_000, 200_000, 18_000, "mdf", "v1")
    stock = base_stock(kerf_um=1_000)
    layout = DeterministicNester().nest((first, second), stock)

    report = DFMValidator().validate((first, second), layout, linuxcnc_reference_router_1325())

    clearance_issue = next(
        issue for issue in report.blocking_issues if issue.code == "OPEN_END_RELIEF_PART_CLEARANCE"
    )
    assert clearance_issue.feature_id == "open-exit"
    assert clearance_issue.inputs["other_instance_id"] == "b-neighbor:001"


def test_dfm_reports_machine_feature_tool_depth_keepout_and_collision_failures() -> None:
    features = (
        ManufacturingFeature(
            "outside-hole",
            "panel",
            FeatureKind.DRILL,
            Side.A,
            2_000,
            2_000,
            10_000,
            diameter_um=8_000,
        ),
        ManufacturingFeature(
            "missing-diameter",
            "panel",
            FeatureKind.DRILL,
            Side.B,
            50_000,
            50_000,
            10_000,
        ),
        ManufacturingFeature(
            "through-short",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            80_000,
            20_000,
            17_000,
            width_um=10_000,
            length_um=10_000,
            through=True,
        ),
        ManufacturingFeature(
            "through-deep",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            100_000,
            20_000,
            20_000,
            width_um=10_000,
            length_um=10_000,
            through=True,
        ),
        ManufacturingFeature(
            "non-through",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            120_000,
            20_000,
            18_000,
            width_um=10_000,
            length_um=10_000,
        ),
        ManufacturingFeature(
            "thin-wall",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            140_000,
            20_000,
            16_000,
            width_um=10_000,
            length_um=10_000,
        ),
        ManufacturingFeature(
            "square-pocket",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            160_000,
            20_000,
            5_000,
            width_um=10_000,
            length_um=10_000,
            metadata={"requires_square_corners": True},
        ),
        ManufacturingFeature(
            "collision-a",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            200_000,
            100_000,
            5_000,
            width_um=20_000,
            length_um=20_000,
        ),
        ManufacturingFeature(
            "collision-b",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            210_000,
            110_000,
            5_000,
            width_um=20_000,
            length_um=20_000,
        ),
    )
    part = base_part(*features)
    stock = base_stock(clamp_zones=(Rect(205_000, 105_000, 100_000, 100_000),))
    layout = DeterministicNester().nest((part,), stock)
    machine = replace(
        linuxcnc_reference_router_1325(),
        work_width_um=900_000,
        work_z_um=20_000,
        can_flip_stock=False,
        supported_operations=(),
        supported_sides=(),
        tools=(),
        keep_out_zones=(Rect(0, 0, 1_000_000, 600_000),),
    )

    report = DFMValidator().validate((part,), layout, machine)
    codes = {issue.code for issue in report.blocking_issues}

    assert {
        "MACHINE_STOCK_ENVELOPE",
        "MACHINE_Z_ENVELOPE",
        "FEATURE_OUTSIDE_PART",
        "HOLE_EDGE_DISTANCE",
        "B_SIDE_ACCESS_UNAVAILABLE",
        "FEATURE_SIDE_UNSUPPORTED",
        "OPERATION_UNSUPPORTED",
        "DRILL_DIAMETER_MISSING",
        "THROUGH_DEPTH_INCOMPLETE",
        "THROUGH_DEPTH_EXCESSIVE",
        "NONTHROUGH_DEPTH",
        "REMAINING_WALL_TOO_THIN",
        "INNER_CORNER_STRATEGY_MISSING",
        "COMPATIBLE_TOOL_MISSING",
        "FEATURE_KEEPOUT_COLLISION",
        "FEATURE_COLLISION",
    } <= codes


def test_dfm_spindle_limit_allowed_overlap_and_defensive_invalid_part() -> None:
    first = ManufacturingFeature(
        "first",
        "panel",
        FeatureKind.POCKET,
        Side.A,
        50_000,
        50_000,
        5_000,
        width_um=20_000,
        length_um=20_000,
        metadata={"allow_overlap_with": ("second",)},
    )
    second = replace(first, feature_id="second", x_um=55_000, y_um=55_000, metadata={})
    part = base_part(first, second)
    stock = base_stock()
    layout = DeterministicNester().nest((part,), stock)
    low_rpm_machine = replace(linuxcnc_reference_router_1325(), max_spindle_rpm=10_000)
    report = DFMValidator().validate((part,), layout, low_rpm_machine)

    assert "TOOL_SPINDLE_LIMIT" in {issue.code for issue in report.issues}
    assert "FEATURE_COLLISION" not in {issue.code for issue in report.issues}

    invalid = copy.copy(part)
    object.__setattr__(invalid, "width_um", 0)
    invalid_report = DFMValidator().validate((invalid,), layout, low_rpm_machine)
    assert "PART_INVALID_DIMENSIONS" in {issue.code for issue in invalid_report.issues}


def test_dfm_sums_opposing_depths_and_requires_a_positive_fit_budget() -> None:
    opposing = tuple(
        ManufacturingFeature(
            f"groove-{side.value}",
            "thin-panel",
            FeatureKind.GROOVE,
            side,
            20_000,
            20_000,
            2_000,
            width_um=20_000,
            length_um=60_000,
        )
        for side in (Side.A, Side.B)
    )
    thin_panel = PartSpec(
        "thin-panel",
        "Thin panel",
        300_000,
        200_000,
        6_000,
        "mdf-6",
        "v1",
        features=opposing,
        grain_direction="NONE",
    )
    thin_stock = replace(
        base_stock(),
        material_id="mdf-6",
        thickness_um=6_000,
    )
    thin_layout = DeterministicNester().nest((thin_panel,), thin_stock)
    thin_report = DFMValidator().validate(
        (thin_panel,), thin_layout, linuxcnc_reference_router_1325()
    )
    wall_issue = next(
        issue
        for issue in thin_report.blocking_issues
        if issue.code == "OPPOSING_FEATURE_WALL_TOO_THIN"
    )
    assert wall_issue.inputs["remaining_um"] == 2_000
    assert "REMAINING_WALL_TOO_THIN" not in {issue.code for issue in thin_report.blocking_issues}

    exhausted_fit = ManufacturingFeature(
        "fit",
        "panel",
        FeatureKind.GROOVE,
        Side.A,
        20_000,
        20_000,
        5_000,
        width_um=10_000,
        length_um=60_000,
        tolerance_um=50,
        fit_clearance_um=200,
    )
    fit_part = base_part(exhausted_fit)
    fit_layout = DeterministicNester().nest((fit_part,), base_stock())
    fit_report = DFMValidator().validate((fit_part,), fit_layout, linuxcnc_reference_router_1325())
    fit_issue = next(
        issue
        for issue in fit_report.blocking_issues
        if issue.code == "FIT_TOLERANCE_BUDGET_EXHAUSTED"
    )
    assert fit_issue.inputs["remaining_margin_um"] == -100


def test_tool_selection_and_coordinate_transforms_cover_rejection_paths() -> None:
    machine = linuxcnc_reference_router_1325()
    wrong_diameter = ManufacturingFeature(
        "wrong",
        "panel",
        FeatureKind.DRILL,
        Side.A,
        10_000,
        10_000,
        40_000,
        diameter_um=7_000,
    )
    narrow = ManufacturingFeature(
        "narrow",
        "panel",
        FeatureKind.GROOVE,
        Side.A,
        10_000,
        10_000,
        5_000,
        width_um=2_000,
        length_um=20_000,
    )
    assert select_tool(wrong_diameter, machine) is None
    assert select_tool(narrow, machine) is None

    placement = Placement("panel:001", "panel", "sheet", 0, 10, 20, 300, 200, False)
    assert transform_point_to_machine(30, 40, placement, 1_000, Side.A) == (40, 60)
    assert transform_point_to_machine(30, 40, placement, 1_000, Side.B) == (40, 940)
    assert transform_rect_to_machine(Rect(30, 40, 10, 20), placement, 1_000, Side.A) == Rect(
        40, 60, 10, 20
    )


def test_manual_layout_validation_catches_reference_and_boundary_failures() -> None:
    part = replace(base_part(), grain_direction="Y")
    instances = (PartInstance("panel:001", part),)
    stock = base_stock(
        quantity=1,
        grain_direction="X",
        defect_zones=(Rect(20_000, 20_000, 20_000, 20_000),),
        clamp_zones=(Rect(50_000, 50_000, 20_000, 20_000),),
    )
    placements = (
        Placement("unknown", "ghost", "sheet", 2, 0, 0, 1, 1, False),
        Placement("panel:001", "panel", "sheet", 0, 20_000, 20_000, 1, 1, False),
        Placement("panel:001", "panel", "sheet", 0, 50_000, 50_000, 1, 1, False),
        Placement("panel:001", "panel", "sheet", 2, 0, 0, 300_000, 200_000, False),
    )
    layout = NestingLayout(stock, placements, ("panel:001",), 2, 1, "manual")

    codes = {issue.code for issue in validate_layout(layout, instances)}

    assert {
        "NESTING_SHEET_COUNT",
        "NESTING_UNKNOWN_INSTANCE",
        "NESTING_DUPLICATE_INSTANCE",
        "NESTING_DIMENSION_MISMATCH",
        "NESTING_GRAIN_MISMATCH",
        "NESTING_INVALID_SHEET",
        "NESTING_OUT_OF_BOUNDS",
        "NESTING_DEFECT_COLLISION",
        "NESTING_CLAMP_COLLISION",
        "NESTING_ACCOUNTING",
    } <= codes
    assert DeterministicNester().nest((), stock).placements == ()


def test_exporters_cover_all_semantic_layers_edges_and_nesting_zones() -> None:
    features = (
        ManufacturingFeature(
            "pattern",
            "panel",
            FeatureKind.DRILL_PATTERN,
            Side.A,
            20_000,
            20_000,
            5_000,
            diameter_um=5_000,
            pattern_count=2,
            pitch_um=32_000,
        ),
        ManufacturingFeature(
            "counter",
            "panel",
            FeatureKind.COUNTERSINK,
            Side.A,
            80_000,
            20_000,
            5_000,
            diameter_um=8_000,
        ),
        ManufacturingFeature(
            "pocket",
            "panel",
            FeatureKind.POCKET,
            Side.A,
            20_000,
            60_000,
            5_000,
            width_um=20_000,
            length_um=20_000,
        ),
        ManufacturingFeature(
            "inner",
            "panel",
            FeatureKind.INNER_CONTOUR,
            Side.A,
            60_000,
            60_000,
            5_000,
            width_um=20_000,
            length_um=20_000,
        ),
        ManufacturingFeature(
            "rabbet",
            "panel",
            FeatureKind.RABBET,
            Side.A,
            100_000,
            60_000,
            5_000,
            width_um=20_000,
            length_um=20_000,
            corner_strategy="dogbone-v1",
            corner_relief_radius_um=3_000,
        ),
        ManufacturingFeature(
            "outer",
            "panel",
            FeatureKind.OUTER_CONTOUR,
            Side.A,
            140_000,
            60_000,
            18_000,
            width_um=20_000,
            length_um=20_000,
            through=True,
        ),
        ManufacturingFeature(
            "label",
            "panel",
            FeatureKind.LABEL,
            Side.A,
            180_000,
            60_000,
            1_000,
            width_um=10_000,
            length_um=5_000,
        ),
    )
    edge_band_details = tuple(
        EdgeBandSpec(edge=edge, thickness_um=1_000, source_face=source_face)
        for edge, source_face in (
            ("V_MAX", "TOP"),
            ("V_MIN", "BOTTOM"),
            ("U_MIN", "LEFT"),
            ("U_MAX", "RIGHT"),
        )
    )
    part = replace(
        base_part(*features),
        edge_bands=tuple(detail.edge for detail in edge_band_details),
        edge_band_details=edge_band_details,
    )
    dxf = dxf_for_part(part, Side.A).decode("utf-8")
    svg = svg_for_part(part, Side.A).decode("utf-8")

    assert all(layer in dxf for layer in ("DRILL", "POCKET", "GROOVE", "EDGE_BAND", "LABEL"))
    assert 'data-feature-id="pattern"' in svg
    dxf_document = ezdxf.read(io.StringIO(dxf))
    assert len(dxf_document.modelspace().query('CIRCLE[layer=="GROOVE"]')) == 4
    assert 'data-corner-strategy="dogbone-v1"' in svg
    assert b"finished_width_mm" in bom_csv((part,))
    assert b"instance_id" in cut_list_csv((part,))
    with pytest.raises(ValueError):
        dxf_for_part(part, Side.EDGE)
    with pytest.raises(ValueError):
        svg_for_part(part, Side.EDGE)

    stock = base_stock(
        defect_zones=(Rect(500_000, 400_000, 10_000, 10_000),),
        clamp_zones=(Rect(600_000, 400_000, 10_000, 10_000),),
    )
    layout = DeterministicNester().nest((replace(part, features=()),), stock)
    nesting = nesting_svg(layout, 0).decode("utf-8")
    assert 'class="defect"' in nesting and 'class="clamp"' in nesting
    with pytest.raises(ValueError):
        nesting_svg(layout, 1)
