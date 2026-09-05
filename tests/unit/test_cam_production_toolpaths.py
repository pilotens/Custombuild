from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from custombuild_cam import toolpaths as toolpath_module
from custombuild_cam.production_model import (
    EXECUTABLE_CAM_CANDIDATE_MODE,
    FIXTURE_KEEPOUT_POLICY,
    IDENTITY_SOURCE_TO_WCS_XY,
    MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
    STOCK_TOP_Z0_REFERENCE,
    BoundSetup,
    CuttingRecipe,
    ProductionCAMError,
    ProductionExecutionContext,
    ProductionMoveKind,
    ProductionMoveRole,
    ProductionToolBinding,
    ProductionToolGeometry,
)
from custombuild_cam.toolpaths import depth_levels_um, generate_production_toolpaths
from custombuild_manufacturing.model import (
    CAMOperation,
    OperationKind,
    OperationsDocument,
    Point2D,
    Rect,
    Setup,
    Side,
    ToolSpec,
    canonical_json_bytes,
    sha256_hex,
)
from custombuild_manufacturing.profiles import linuxcnc_reference_router_1325

_DESIGN_HASH = "d" * 64
_FIXTURE_HASH = "f" * 64


def _mill() -> ToolSpec:
    return next(tool for tool in linuxcnc_reference_router_1325().tools if tool.tool_id == "T06R")


def _drill() -> ToolSpec:
    return next(tool for tool in linuxcnc_reference_router_1325().tools if tool.tool_id == "T05")


def _source_setup(*, keep_out_zones: tuple[Rect, ...] = ()) -> Setup:
    return Setup(
        setup_id="setup:sheet:001:A",
        stock_id="sheet",
        material_id="birch-ply",
        material_version="2026.1",
        sheet_index=0,
        side=Side.A,
        wcs="G54",
        origin=Point2D(0, 0),
        stock_width_um=600_000,
        stock_height_um=400_000,
        stock_thickness_um=18_000,
        safe_z_um=15_000,
        reference_surface="EXTERNAL_STOCK_TOP_MEASUREMENT_REQUIRED",
        orientation="A_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT",
        fixture="EXTERNAL_FIXTURE_PLAN_REQUIRED; DECLARED_KEEP_OUT_ZONES_ONLY",
        keep_out_zones=keep_out_zones,
        tool_ids=("T05", "T06R"),
        probe_method="EXTERNAL_COORDINATE_REGISTRATION_REQUIRED",
        operator_steps=("Validation only",),
    )


def _bound_setup(*, keep_out_zones: tuple[Rect, ...] = ()) -> BoundSetup:
    source_setup = _source_setup()
    return BoundSetup(
        setup_id="setup:sheet:001:A",
        stock_id="sheet",
        source_material_id="birch-ply",
        source_material_version="2026.1",
        material_id="birch-ply",
        material_version="2026.1",
        material_evidence_id="supplier-declaration-birch-ply",
        material_evidence_version="2026.1",
        material_evidence_sha256="9" * 64,
        sheet_index=0,
        side=Side.A,
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_setup)),
        source_to_wcs_xy_transform=IDENTITY_SOURCE_TO_WCS_XY,
        wcs="G56",
        machine_wcs_origin=Point2D(100_000, 50_000),
        machine_wcs_z0_um=0,
        machine_wcs_xy_rotation_mdeg=0,
        stock_width_um=600_000,
        stock_height_um=400_000,
        stock_thickness_um=18_000,
        safe_z_um=20_000,
        reference_surface=STOCK_TOP_Z0_REFERENCE,
        orientation=source_setup.orientation,
        fixture_id="vacuum-fixture",
        fixture_version="3.1.0",
        fixture_sha256=_FIXTURE_HASH,
        fixture_clearance_z_um=10_000,
        minimum_rapid_clearance_um=5_000,
        keep_out_policy=FIXTURE_KEEPOUT_POLICY,
        probe_method="PROBE_STOCK_TOP_AND_XY_DATUM",
        keep_out_zones=keep_out_zones,
        spoilboard_id="mdf-spoilboard-01",
        spoilboard_version="surfaced-2026.09",
        spoilboard_sha256="e" * 64,
        through_cut_allowance_um=500,
    )


def _operations() -> tuple[CAMOperation, ...]:
    return (
        CAMOperation(
            operation_id="op:part-001:outer",
            setup_id="setup:sheet:001:A",
            part_id="part",
            instance_id="part-001",
            feature_id="outer",
            kind=OperationKind.CONTOUR,
            side=Side.A,
            tool_id="T06R",
            x_um=20_000,
            y_um=20_000,
            depth_um=18_000,
            width_um=560_000,
            length_um=360_000,
            cutter_envelope_x_um=20_000,
            cutter_envelope_y_um=20_000,
            cutter_envelope_width_um=560_000,
            cutter_envelope_length_um=360_000,
            stepdown_um=3_000,
            through=True,
            compensation="OUTSIDE",
            holding_strategy="TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL",
        ),
        CAMOperation(
            operation_id="op:part-001:drill",
            setup_id="setup:sheet:001:A",
            part_id="part",
            instance_id="part-001",
            feature_id="drill",
            kind=OperationKind.DRILL,
            side=Side.A,
            tool_id="T05",
            x_um=50_000,
            y_um=50_000,
            depth_um=8_000,
            diameter_um=5_000,
            stepdown_um=3_000,
        ),
        CAMOperation(
            operation_id="op:part-001:groove",
            setup_id="setup:sheet:001:A",
            part_id="part",
            instance_id="part-001",
            feature_id="groove",
            kind=OperationKind.GROOVE,
            side=Side.A,
            tool_id="T06R",
            x_um=100_000,
            y_um=100_000,
            depth_um=6_000,
            width_um=20_000,
            length_um=80_000,
            cutter_envelope_x_um=100_000,
            cutter_envelope_y_um=100_000,
            cutter_envelope_width_um=20_000,
            cutter_envelope_length_um=80_000,
            stepdown_um=3_000,
            stepover_ppm=400_000,
        ),
        CAMOperation(
            operation_id="op:part-001:pocket",
            setup_id="setup:sheet:001:A",
            part_id="part",
            instance_id="part-001",
            feature_id="pocket",
            kind=OperationKind.POCKET,
            side=Side.A,
            tool_id="T06R",
            x_um=150_000,
            y_um=100_000,
            depth_um=5_000,
            width_um=30_000,
            length_um=40_000,
            cutter_envelope_x_um=150_000,
            cutter_envelope_y_um=100_000,
            cutter_envelope_width_um=30_000,
            cutter_envelope_length_um=40_000,
            stepdown_um=3_000,
            stepover_ppm=400_000,
        ),
    )


def _document(*, operations: tuple[CAMOperation, ...] | None = None) -> OperationsDocument:
    tools = (_drill(), _mill())
    source_machine = linuxcnc_reference_router_1325()
    return OperationsDocument(
        schema_version="custombuild.operations.v2",
        design_hash=_DESIGN_HASH,
        machine_profile_id=source_machine.profile_id,
        machine_profile_version=source_machine.version,
        setups=(_source_setup(),),
        operations=operations or _operations(),
        tool_catalog_version=source_machine.tool_library_version,
        tool_catalog_fingerprint=sha256_hex(canonical_json_bytes(tools)),
        tools=tools,
    )


def _binding(tool: ToolSpec, geometry: ProductionToolGeometry) -> ProductionToolBinding:
    controller_number = 5 if tool.tool_id == "T05" else 6
    return ProductionToolBinding(
        tool_id=f"SHOP-{tool.tool_id}",
        tool_version="measured-2026.09",
        source_tool_id=tool.tool_id,
        source_tool_version=tool.version,
        source_tool_sha256=sha256_hex(canonical_json_bytes(tool)),
        controller_tool_number=controller_number,
        length_offset_number=controller_number,
        expected_length_offset_x_um=0,
        expected_length_offset_y_um=0,
        expected_length_offset_z_um=35_000,
        tool_table_evidence_id="linuxcnc-tool-table",
        tool_table_evidence_version="snapshot-2026.09",
        tool_table_evidence_sha256="a" * 64,
        effective_diameter_um=tool.effective_diameter_um,
        cutting_length_um=28_000,
        measured_stickout_um=35_000,
        minimum_holder_clearance_um=5_000,
        assembly_collision_radius_um=10_000,
        geometry=geometry,
        center_cutting=True,
        drill_point_length_um=0,
    )


def _recipe(tool: ToolSpec, kind: OperationKind) -> CuttingRecipe:
    contour = kind == OperationKind.CONTOUR
    binding = _binding(
        tool,
        ProductionToolGeometry.DRILL
        if kind == OperationKind.DRILL
        else ProductionToolGeometry.FLAT_END_MILL,
    )
    return CuttingRecipe(
        recipe_id=f"recipe-{binding.tool_id}-{kind.value.lower()}",
        version="1.0.0",
        machine_profile_id="workshop-router-7",
        machine_profile_version="calibration-2026.09",
        material_id="birch-ply",
        material_version="2026.1",
        tool_id=binding.tool_id,
        tool_version=binding.tool_version,
        operation_kind=kind,
        spindle_rpm=16_000 if tool.tool_id == "T06R" else 10_000,
        feed_um_min=1_800_000 if tool.tool_id == "T06R" else 400_000,
        plunge_um_min=400_000 if tool.tool_id == "T06R" else 200_000,
        stepdown_um=5_000,
        stepover_ppm=400_000,
        peck_depth_um=3_000,
        approach_clearance_um=2_000,
        through_overtravel_um=400 if contour else 0,
        tab_width_um=20_000 if contour else 0,
        tab_height_um=3_000 if contour else 0,
        process_accuracy_um=50,
        accepted_tolerance_um=200,
    )


def _context(*, setup: BoundSetup | None = None) -> ProductionExecutionContext:
    source_machine = linuxcnc_reference_router_1325()
    drill = _drill()
    mill = _mill()
    selected_setup = setup or _bound_setup()
    recipes = tuple(
        replace(
            recipe,
            material_id=selected_setup.material_id,
            material_version=selected_setup.material_version,
        )
        for recipe in (
            _recipe(drill, OperationKind.DRILL),
            _recipe(mill, OperationKind.CONTOUR),
            _recipe(mill, OperationKind.GROOVE),
            _recipe(mill, OperationKind.POCKET),
        )
    )
    return ProductionExecutionContext(
        source_machine_profile_id=source_machine.profile_id,
        source_machine_profile_version=source_machine.version,
        source_machine_profile_fingerprint=sha256_hex(canonical_json_bytes(source_machine)),
        machine_profile_id="workshop-router-7",
        machine_profile_version="calibration-2026.09",
        controller_id="linuxcnc",
        controller_version="2.9.3",
        machine_x_min_um=0,
        machine_x_max_um=2_500_000,
        machine_y_min_um=0,
        machine_y_max_um=1_300_000,
        machine_z_min_um=-100_000,
        machine_z_max_um=150_000,
        work_width_um=2_500_000,
        work_height_um=1_300_000,
        work_z_um=250_000,
        min_spindle_rpm=5_000,
        max_spindle_rpm=24_000,
        max_feed_um_min=4_000_000,
        max_plunge_um_min=1_000_000,
        tool_catalog_version="shop-tools-1",
        recipe_catalog_version="birch-recipes-1",
        setups=(selected_setup,),
        tool_bindings=(
            _binding(drill, ProductionToolGeometry.DRILL),
            _binding(mill, ProductionToolGeometry.FLAT_END_MILL),
        ),
        recipes=recipes,
    )


def test_depth_levels_are_exact_and_dense() -> None:
    assert depth_levels_um(8_000, 3_000) == (-3_000, -6_000, -8_000)
    assert depth_levels_um(6_000, 3_000) == (-3_000, -6_000)
    with pytest.raises(ProductionCAMError):
        depth_levels_um(1_000, 0)


def test_candidate_is_deterministic_and_release_contours_are_last() -> None:
    document = _document()
    context = _context()

    first = generate_production_toolpaths(document, context)
    second = generate_production_toolpaths(document, context)

    assert first.to_json() == second.to_json()
    assert first.fingerprint == second.fingerprint
    assert first.mode == EXECUTABLE_CAM_CANDIDATE_MODE
    assert first.physical_cutting_authorized is False
    assert first.workshop_acceptance_required is True
    assert context.source_machine_profile_id != context.machine_profile_id
    assert context.setups[0].wcs != document.setups[0].wcs
    assert context.setups[0].safe_z_um != document.setups[0].safe_z_um
    assert tuple(program.run_order for program in first.programs) == (1, 2, 3)
    assert first.programs[-1].operation_ids[-1] == "op:part-001:outer"
    assert first.programs[-1].release_operation_ids == ("op:part-001:outer",)
    for program in first.programs:
        assert tuple(move.sequence for move in program.moves) == tuple(
            range(1, len(program.moves) + 1)
        )


def test_source_screening_material_is_separate_from_actual_workshop_material() -> None:
    setup = replace(
        _bound_setup(),
        source_material_version="screening-2026.1",
        material_id="supplier-birch-ply-bb",
        material_version="lot-2026.09",
        material_evidence_id="supplier-certificate-4471",
        material_evidence_version="signed-2026.09",
        material_evidence_sha256="7" * 64,
    )
    source_setup = replace(_source_setup(), material_version="screening-2026.1")
    setup = replace(
        setup,
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_setup)),
    )
    source = replace(_document(), setups=(source_setup,))

    candidate = generate_production_toolpaths(source, _context(setup=setup))

    accepted = candidate.execution_context.setups[0]
    assert accepted.source_material_version == "screening-2026.1"
    assert accepted.material_id == "supplier-birch-ply-bb"
    assert accepted.material_version == "lot-2026.09"
    assert accepted.material_evidence_sha256 == "7" * 64
    assert {
        (recipe.material_id, recipe.material_version)
        for recipe in candidate.execution_context.recipes
    } == {("supplier-birch-ply-bb", "lot-2026.09")}


def test_two_sides_of_one_physical_sheet_require_one_material_evidence() -> None:
    context = _context()
    side_a = context.setups[0]
    side_b = replace(
        side_a,
        setup_id="setup:sheet:001:B",
        side=Side.B,
        wcs="G57",
        material_evidence_sha256="6" * 64,
    )

    with pytest.raises(
        ProductionCAMError,
        match="physical sheet disagree on source or actual material",
    ):
        replace(context, setups=(side_a, side_b))


def test_peck_raster_compensation_and_four_tabs_are_explicit_moves() -> None:
    candidate = generate_production_toolpaths(_document(), _context())
    moves = tuple(move for program in candidate.programs for move in program.moves)

    drill_cuts = tuple(
        move
        for move in moves
        if move.operation_id == "op:part-001:drill" and move.kind == ProductionMoveKind.LINEAR
    )
    assert tuple(move.z_um for move in drill_cuts) == (-3_000, -6_000, -8_000)
    assert sum(move.role == ProductionMoveRole.PECK_RETRACT for move in moves) == 2

    groove_cuts = tuple(
        move
        for move in moves
        if move.operation_id == "op:part-001:groove" and move.kind == ProductionMoveKind.LINEAR
    )
    assert {move.x_um for move in groove_cuts} >= {103_000, 117_000}
    assert {move.y_um for move in groove_cuts} >= {103_000, 177_000}

    outer_moves = tuple(move for move in moves if move.operation_id == "op:part-001:outer")
    assert min(move.x_um for move in outer_moves) == 17_000
    assert max(move.x_um for move in outer_moves) == 583_000
    final_tab_bridges = tuple(
        move
        for move in outer_moves
        if move.pass_index == 4 and move.role == ProductionMoveRole.TAB_BRIDGE
    )
    assert len(final_tab_bridges) == 4
    assert {move.z_um for move in final_tab_bridges} == {-15_000}
    final_tab_ramps = tuple(
        move
        for move in outer_moves
        if move.pass_index == 4 and move.role == ProductionMoveRole.TAB_RAMP
    )
    assert len(final_tab_ramps) == 8
    assert {move.feed_um_min for move in final_tab_ramps} == {400_000}
    for bridge in final_tab_bridges:
        bridge_index = outer_moves.index(bridge)
        raised_at = outer_moves[bridge_index - 1]
        centreline_bridge_um = abs(bridge.x_um - raised_at.x_um) + abs(bridge.y_um - raised_at.y_um)
        assert centreline_bridge_um - _mill().effective_diameter_um == 20_000


def test_tab_width_strictly_exceeds_two_sided_process_uncertainty() -> None:
    source = _document()
    context = _context()
    contour_index = next(
        index
        for index, recipe in enumerate(context.recipes)
        if recipe.operation_kind == OperationKind.CONTOUR
    )
    contour_recipe = context.recipes[contour_index]

    for invalid_width_um in (1, 2 * contour_recipe.process_accuracy_um):
        with pytest.raises(ProductionCAMError, match="tab width must exceed twice"):
            replace(contour_recipe, tab_width_um=invalid_width_um)

    recipes = list(context.recipes)
    recipes[contour_index] = replace(
        contour_recipe,
        tab_width_um=2 * contour_recipe.process_accuracy_um + 1,
    )
    boundary_context = replace(context, recipes=tuple(recipes))
    candidate = generate_production_toolpaths(source, boundary_context)
    assert (
        sum(
            move.role == ProductionMoveRole.TAB_BRIDGE
            for program in candidate.programs
            for move in program.moves
        )
        == 4
    )

    for invalid_width_um in (1, 2 * contour_recipe.process_accuracy_um):
        tampered_context = _context()
        tampered_contour = next(
            recipe
            for recipe in tampered_context.recipes
            if recipe.operation_kind == OperationKind.CONTOUR
        )
        object.__setattr__(tampered_contour, "tab_width_um", invalid_width_um)
        with pytest.raises(
            ProductionCAMError,
            match="uncertainty consumes the holding-tab width",
        ):
            generate_production_toolpaths(source, tampered_context)


def test_raster_stepover_preserves_accuracy_adjusted_material_coverage() -> None:
    source = _document()
    context = _context()
    pocket_index = next(
        index
        for index, recipe in enumerate(context.recipes)
        if recipe.operation_kind == OperationKind.POCKET
    )
    pocket_recipe = context.recipes[pocket_index]
    mill_binding = next(
        binding for binding in context.tool_bindings if binding.tool_id == pocket_recipe.tool_id
    )

    recipes = list(context.recipes)
    recipes[pocket_index] = replace(pocket_recipe, stepover_ppm=1_000_000)
    with pytest.raises(ProductionCAMError, match="process-accuracy-adjusted cutter coverage"):
        replace(context, recipes=tuple(recipes))

    guaranteed_width_um = mill_binding.effective_diameter_um - 2 * pocket_recipe.process_accuracy_um
    boundary_ppm = (
        guaranteed_width_um * 1_000_000 + mill_binding.effective_diameter_um - 1
    ) // mill_binding.effective_diameter_um
    assert mill_binding.effective_diameter_um * boundary_ppm // 1_000_000 == guaranteed_width_um
    recipes[pocket_index] = replace(pocket_recipe, stepover_ppm=boundary_ppm)
    generate_production_toolpaths(source, replace(context, recipes=tuple(recipes)))

    tampered_context = _context()
    tampered_pocket = next(
        recipe
        for recipe in tampered_context.recipes
        if recipe.operation_kind == OperationKind.POCKET
    )
    object.__setattr__(tampered_pocket, "stepover_ppm", 1_000_000)
    with pytest.raises(ProductionCAMError, match="process-accuracy-adjusted cutter coverage"):
        generate_production_toolpaths(source, tampered_context)


def test_placeholder_setup_raw_allowance_and_excess_overtravel_fail_closed() -> None:
    with pytest.raises(ProductionCAMError, match="stock-top"):
        replace(_bound_setup(), reference_surface="EXTERNAL_MEASUREMENT_REQUIRED")
    with pytest.raises(ProductionCAMError, match="raw allowance"):
        replace(_bound_setup(), raw_allowance_um=100)
    contour_recipe = _recipe(_mill(), OperationKind.CONTOUR)
    with pytest.raises(ProductionCAMError, match="overtravel"):
        replace(contour_recipe, through_overtravel_um=501)
    with pytest.raises(ProductionCAMError, match="minimum rapid clearance"):
        replace(_bound_setup(), fixture_clearance_z_um=16_000)
    with pytest.raises(ProductionCAMError, match="spoilboard binding"):
        replace(_bound_setup(), spoilboard_sha256=None)
    with pytest.raises(ProductionCAMError, match="through-cut allowance"):
        replace(_bound_setup(), through_cut_allowance_um=501)
    with pytest.raises(ProductionCAMError, match="identity source-to-WCS"):
        replace(_bound_setup(), source_to_wcs_xy_transform="ROTATE_90")
    with pytest.raises(ProductionCAMError, match="zero WCS XY rotation"):
        replace(_bound_setup(), machine_wcs_xy_rotation_mdeg=1)


def test_live_tool_table_values_and_atomic_evidence_are_bound_exactly() -> None:
    context = _context()
    drill_binding, mill_binding = context.tool_bindings

    with pytest.raises(ProductionCAMError, match="zero expected X/Y"):
        replace(drill_binding, expected_length_offset_x_um=1)
    with pytest.raises(ProductionCAMError, match="zero expected X/Y"):
        replace(drill_binding, expected_length_offset_y_um=-1)
    with pytest.raises(ProductionCAMError, match="must be an integer"):
        replace(drill_binding, expected_length_offset_z_um=True)
    with pytest.raises(ProductionCAMError, match="placeholder"):
        replace(drill_binding, tool_table_evidence_id="TBD")
    with pytest.raises(ProductionCAMError, match="SHA-256"):
        replace(drill_binding, tool_table_evidence_sha256="not-a-hash")

    changed_z = replace(drill_binding, expected_length_offset_z_um=-12_345)
    rebound = replace(context, tool_bindings=(changed_z, mill_binding))
    assert rebound.tool_catalog_fingerprint != context.tool_catalog_fingerprint
    assert rebound.fingerprint != context.fingerprint

    mixed_snapshot = replace(mill_binding, tool_table_evidence_sha256="b" * 64)
    with pytest.raises(ProductionCAMError, match="one atomic tool-table evidence"):
        replace(context, tool_bindings=(drill_binding, mixed_snapshot))


def test_source_provenance_and_production_setup_are_separate_and_exact() -> None:
    document = _document()
    context = _context()
    independently_safer_setup = replace(context.setups[0], safe_z_um=25_000, wcs="G57")
    candidate = generate_production_toolpaths(
        document,
        replace(context, setups=(independently_safer_setup,)),
    )
    assert max(move.z_um for program in candidate.programs for move in program.moves) == 25_000

    with pytest.raises(ProductionCAMError, match="profile fingerprint"):
        generate_production_toolpaths(
            document,
            replace(context, source_machine_profile_fingerprint="0" * 64),
        )
    with pytest.raises(ProductionCAMError, match="setup source fingerprint"):
        generate_production_toolpaths(
            document,
            replace(
                context,
                setups=(replace(context.setups[0], source_setup_sha256="0" * 64),),
            ),
        )
    with pytest.raises(ProductionCAMError, match="orientation differs"):
        generate_production_toolpaths(
            document,
            replace(
                context,
                setups=(
                    replace(
                        context.setups[0],
                        orientation="A_SIDE_UP; STOCK_ORIGIN_AT_UPPER_RIGHT",
                    ),
                ),
            ),
        )


def test_actual_tool_measurement_and_machine_limits_are_enforced() -> None:
    document = _document()
    context = _context()
    drill_binding, mill_binding = context.tool_bindings
    drill_recipe = next(
        recipe for recipe in context.recipes if recipe.operation_kind == OperationKind.DRILL
    )
    measured_drill = replace(drill_binding, effective_diameter_um=5_100)
    tolerant_drill_recipe = replace(drill_recipe, diameter_tolerance_um=100)
    recipes = tuple(
        tolerant_drill_recipe if recipe is drill_recipe else recipe for recipe in context.recipes
    )
    candidate = generate_production_toolpaths(
        document,
        replace(
            context,
            tool_bindings=(measured_drill, mill_binding),
            recipes=recipes,
        ),
    )
    assert candidate.execution_context.tool_bindings[0].effective_diameter_um == 5_100
    assert measured_drill.source_tool_version != measured_drill.tool_version
    assert measured_drill.cutting_length_um != _drill().cutting_length_um

    too_tight = tuple(
        replace(tolerant_drill_recipe, diameter_tolerance_um=99)
        if recipe is tolerant_drill_recipe
        else recipe
        for recipe in recipes
    )
    with pytest.raises(ProductionCAMError, match="diameter/tool mismatch"):
        generate_production_toolpaths(
            document,
            replace(
                context,
                tool_bindings=(measured_drill, mill_binding),
                recipes=too_tight,
            ),
        )

    with pytest.raises(ProductionCAMError, match="controller tool number"):
        replace(
            context,
            tool_bindings=(
                drill_binding,
                replace(
                    mill_binding,
                    controller_tool_number=drill_binding.controller_tool_number,
                ),
            ),
        )
    with pytest.raises(ProductionCAMError, match="length-offset"):
        replace(
            context,
            tool_bindings=(
                drill_binding,
                replace(
                    mill_binding,
                    length_offset_number=drill_binding.length_offset_number,
                ),
            ),
        )
    assert (
        replace(
            drill_binding,
            controller_tool_number=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
            length_offset_number=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER,
        ).controller_tool_number
        == MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
    )
    with pytest.raises(ProductionCAMError, match="controller_tool_number must be at most"):
        replace(
            drill_binding,
            controller_tool_number=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER + 1,
        )
    with pytest.raises(ProductionCAMError, match="length_offset_number must be at most"):
        replace(
            drill_binding,
            length_offset_number=MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER + 1,
        )
    with pytest.raises(ProductionCAMError, match="measured stickout"):
        replace(mill_binding, measured_stickout_um=20_000)
    with pytest.raises(ProductionCAMError, match="collision radius"):
        replace(mill_binding, assembly_collision_radius_um=2_000)
    with pytest.raises(ProductionCAMError, match="exactly zero drill point length"):
        replace(drill_binding, drill_point_length_um=1)
    with pytest.raises(ProductionCAMError, match="center-cutting"):
        replace(drill_binding, center_cutting=False)

    mutated_point_length = replace(drill_binding)
    object.__setattr__(mutated_point_length, "drill_point_length_um", 1)
    with pytest.raises(ProductionCAMError, match="zero-point-length"):
        generate_production_toolpaths(
            document,
            replace(context, tool_bindings=(mutated_point_length, mill_binding)),
        )

    shallow_stickout = replace(mill_binding, minimum_holder_clearance_um=17_000)
    with pytest.raises(ProductionCAMError, match="tool-holder clearance"):
        generate_production_toolpaths(
            document,
            replace(context, tool_bindings=(drill_binding, shallow_stickout)),
        )

    pocket_recipe = next(
        recipe for recipe in context.recipes if recipe.operation_kind == OperationKind.POCKET
    )
    with pytest.raises(ProductionCAMError, match="feed limit"):
        replace(
            context,
            recipes=tuple(
                replace(recipe, feed_um_min=context.max_feed_um_min + 1)
                if recipe is pocket_recipe
                else recipe
                for recipe in context.recipes
            ),
        )
    with pytest.raises(ProductionCAMError, match="spindle limits"):
        replace(
            context,
            recipes=tuple(
                replace(recipe, spindle_rpm=context.min_spindle_rpm - 1)
                if recipe is pocket_recipe
                else recipe
                for recipe in context.recipes
            ),
        )


def test_through_overtravel_is_included_in_machine_z_travel() -> None:
    context = _context()
    exact_without_overtravel = context.setups[0].safe_z_um + context.setups[0].stock_thickness_um
    with pytest.raises(ProductionCAMError, match="machine Z bounds"):
        constrained = replace(
            context,
            machine_z_min_um=-context.setups[0].stock_thickness_um,
            machine_z_max_um=context.setups[0].safe_z_um,
            work_z_um=exact_without_overtravel,
        )
        generate_production_toolpaths(_document(), constrained)


def test_absolute_machine_bounds_and_signed_wcs_origins_are_enforced() -> None:
    context = _context()
    setup = context.setups[0]
    with pytest.raises(ProductionCAMError, match="machine Z bounds"):
        shifted_below_limit = replace(
            context,
            setups=(replace(setup, machine_wcs_z0_um=-120_000),),
        )
        generate_production_toolpaths(_document(), shifted_below_limit)
    with pytest.raises(ProductionCAMError, match="machine XY travel"):
        replace(
            context,
            setups=(
                replace(
                    setup,
                    machine_wcs_origin=Point2D(context.machine_x_max_um - 100, 50_000),
                ),
            ),
        )
    with pytest.raises(ProductionCAMError, match="absolute bounds"):
        replace(context, machine_z_min_um=context.machine_z_min_um + 1)

    shifted_setup = replace(
        setup,
        machine_wcs_origin=Point2D(-900_000, -450_000),
        machine_wcs_z0_um=-60_000,
    )
    shifted = replace(
        context,
        machine_x_min_um=-1_000_000,
        machine_x_max_um=1_500_000,
        machine_y_min_um=-500_000,
        machine_y_max_um=800_000,
        machine_z_min_um=-250_000,
        machine_z_max_um=0,
        setups=(shifted_setup,),
    )
    assert generate_production_toolpaths(_document(), shifted).execution_context == shifted


def test_g43_tool_offset_is_applied_to_every_programmed_machine_endpoint() -> None:
    context = _context()
    drill_binding, mill_binding = context.tool_bindings

    # LinuxCNC adds the signed H value to the programmed point and literal G5x
    # offset.  A +35 mm H row therefore puts WCS Z100 + safe Z20 at machine
    # Z155, which is outside this profile's +150 mm maximum.
    with pytest.raises(ProductionCAMError, match="machine Z bounds"):
        generate_production_toolpaths(
            _document(),
            replace(
                context,
                setups=(replace(context.setups[0], machine_wcs_z0_um=100_000),),
            ),
        )

    compensated_setup = replace(context.setups[0], machine_wcs_z0_um=90_000)
    compensated = generate_production_toolpaths(
        _document(),
        replace(context, setups=(compensated_setup,)),
    )
    assert compensated.execution_context.setups[0] == compensated_setup

    below_z_limit = replace(
        drill_binding,
        expected_length_offset_z_um=200_000,
    )
    with pytest.raises(ProductionCAMError, match="machine Z bounds"):
        generate_production_toolpaths(
            _document(),
            replace(context, tool_bindings=(below_z_limit, mill_binding)),
        )

    above_z_limit = replace(
        drill_binding,
        expected_length_offset_z_um=-200_000,
    )
    with pytest.raises(ProductionCAMError, match="machine Z bounds"):
        generate_production_toolpaths(
            _document(),
            replace(context, tool_bindings=(above_z_limit, mill_binding)),
        )


def test_spoilboard_allowance_and_polygonal_accuracy_are_recipe_bound() -> None:
    context = _context()
    with pytest.raises(ProductionCAMError, match="spoilboard allowance"):
        generate_production_toolpaths(
            _document(),
            replace(
                context,
                setups=(replace(context.setups[0], through_cut_allowance_um=449),),
            ),
        )

    inaccurate_recipes = tuple(
        replace(recipe, process_accuracy_um=1)
        if recipe.operation_kind == OperationKind.CONTOUR
        else recipe
        for recipe in context.recipes
    )
    with pytest.raises(ProductionCAMError, match="interpolation error"):
        generate_production_toolpaths(
            _document(),
            replace(context, recipes=inaccurate_recipes),
        )

    consumed_tabs = tuple(
        replace(recipe, tab_height_um=recipe.process_accuracy_um)
        if recipe.operation_kind == OperationKind.CONTOUR
        else recipe
        for recipe in context.recipes
    )
    with pytest.raises(ProductionCAMError, match="holding-tab height"):
        generate_production_toolpaths(
            _document(),
            replace(context, recipes=consumed_tabs),
        )

    pocket = next(
        operation for operation in _operations() if operation.kind == OperationKind.POCKET
    )
    uncertain_bottom = replace(pocket, depth_um=17_975)
    operations = tuple(
        uncertain_bottom if operation.operation_id == pocket.operation_id else operation
        for operation in _operations()
    )
    with pytest.raises(ProductionCAMError, match="uncertainty reaches stock bottom"):
        generate_production_toolpaths(_document(operations=operations), context)


def test_through_overtravel_strictly_exceeds_worst_case_process_uncertainty() -> None:
    context = _context()
    equal_boundary = tuple(
        replace(
            recipe,
            through_overtravel_um=recipe.process_accuracy_um,
        )
        if recipe.operation_kind == OperationKind.CONTOUR
        else recipe
        for recipe in context.recipes
    )
    with pytest.raises(ProductionCAMError, match="exceed worst-case process uncertainty"):
        generate_production_toolpaths(
            _document(),
            replace(context, recipes=equal_boundary),
        )

    strict_boundary = tuple(
        replace(
            recipe,
            through_overtravel_um=recipe.process_accuracy_um + 1,
        )
        if recipe.operation_kind == OperationKind.CONTOUR
        else recipe
        for recipe in context.recipes
    )
    generated = generate_production_toolpaths(
        _document(),
        replace(context, recipes=strict_boundary),
    )
    contour_recipe = next(
        recipe
        for recipe in generated.execution_context.recipes
        if recipe.operation_kind == OperationKind.CONTOUR
    )
    assert contour_recipe.through_overtravel_um == contour_recipe.process_accuracy_um + 1


def test_drill_diameter_deviation_and_process_accuracy_share_acceptance_budget() -> None:
    context = _context()
    drill_binding, mill_binding = context.tool_bindings
    drill_recipe = next(
        recipe for recipe in context.recipes if recipe.operation_kind == OperationKind.DRILL
    )
    accepting_recipe = replace(drill_recipe, diameter_tolerance_um=200)
    recipes = tuple(
        accepting_recipe if recipe is drill_recipe else recipe for recipe in context.recipes
    )

    exact_boundary = replace(drill_binding, effective_diameter_um=5_150)
    generate_production_toolpaths(
        _document(),
        replace(
            context,
            tool_bindings=(exact_boundary, mill_binding),
            recipes=recipes,
        ),
    )

    recipe_tolerated_but_budget_exceeded = replace(
        drill_binding,
        effective_diameter_um=5_152,
    )
    with pytest.raises(
        ProductionCAMError,
        match="mismatch plus process uncertainty exceeds accepted tolerance",
    ):
        generate_production_toolpaths(
            _document(),
            replace(
                context,
                tool_bindings=(recipe_tolerated_but_budget_exceeded, mill_binding),
                recipes=recipes,
            ),
        )

    drill_operation = next(
        operation for operation in _operations() if operation.kind == OperationKind.DRILL
    )
    design_toleranced_drill = replace(drill_operation, tolerance_um=120)
    design_operations = tuple(
        design_toleranced_drill
        if operation.operation_id == drill_operation.operation_id
        else operation
        for operation in _operations()
    )
    design_recipe = replace(
        drill_recipe,
        accepted_tolerance_um=120,
        diameter_tolerance_um=200,
    )
    design_recipes = tuple(
        design_recipe if recipe is drill_recipe else recipe for recipe in context.recipes
    )
    design_boundary = replace(drill_binding, effective_diameter_um=5_070)
    generate_production_toolpaths(
        _document(operations=design_operations),
        replace(
            context,
            tool_bindings=(design_boundary, mill_binding),
            recipes=design_recipes,
        ),
    )
    beyond_design_tolerance = replace(drill_binding, effective_diameter_um=5_072)
    with pytest.raises(ProductionCAMError, match="exceeds accepted tolerance"):
        generate_production_toolpaths(
            _document(operations=design_operations),
            replace(
                context,
                tool_bindings=(beyond_design_tolerance, mill_binding),
                recipes=design_recipes,
            ),
        )


def test_through_drill_is_blocked_without_point_geometry_breakthrough_model() -> None:
    context = _context()
    drill_operation = next(
        operation for operation in _operations() if operation.kind == OperationKind.DRILL
    )
    through_drill = replace(
        drill_operation,
        depth_um=context.setups[0].stock_thickness_um,
        through=True,
    )
    operations = tuple(
        through_drill if operation.operation_id == drill_operation.operation_id else operation
        for operation in _operations()
    )
    recipes = tuple(
        replace(recipe, through_overtravel_um=recipe.process_accuracy_um + 1)
        if recipe.operation_kind == OperationKind.DRILL
        else recipe
        for recipe in context.recipes
    )

    with pytest.raises(
        ProductionCAMError,
        match="through drill requires an exact point-geometry",
    ):
        generate_production_toolpaths(
            _document(operations=operations),
            replace(context, recipes=recipes),
        )


def test_every_instance_is_bound_to_one_exact_finished_outline() -> None:
    operations = _operations()
    outer = operations[0]
    non_release = replace(
        outer,
        depth_um=10_000,
        through=False,
        holding_strategy=None,
    )
    without_outline = (non_release, *operations[1:])
    context = _context()
    no_spoilboard = replace(
        context.setups[0],
        spoilboard_id=None,
        spoilboard_version=None,
        spoilboard_sha256=None,
        through_cut_allowance_um=0,
    )
    with pytest.raises(ProductionCAMError, match="finished-outline contour"):
        generate_production_toolpaths(
            _document(operations=without_outline),
            replace(context, setups=(no_spoilboard,)),
        )

    rebound_drill = replace(operations[1], part_id="another-part")
    mismatched = (outer, rebound_drill, *operations[2:])
    with pytest.raises(ProductionCAMError, match="changes part"):
        generate_production_toolpaths(
            _document(operations=mismatched),
            _context(),
        )

    mutated_envelope = replace(outer, cutter_envelope_width_um=560_001)
    mismatched = (mutated_envelope, *operations[1:])
    with pytest.raises(ProductionCAMError, match="declared cutter envelope"):
        generate_production_toolpaths(
            _document(operations=mismatched),
            _context(),
        )


def test_accuracy_expanded_own_part_sweep_uses_physical_b_side_coordinates() -> None:
    source_a = replace(_source_setup(), tool_ids=("T06R",))
    source_b = replace(
        source_a,
        setup_id="setup:sheet:001:B",
        side=Side.B,
        wcs="G55",
        orientation="FLIP_STOCK_ABOUT_X_AXIS; MACHINE_Y=STOCK_HEIGHT-DESIGN_Y",
        tool_ids=("T05",),
    )
    template_outer = _operations()[0]
    first_outer = replace(
        template_outer,
        operation_id="op:first-001:outer",
        part_id="first",
        instance_id="first-001",
        x_um=20_000,
        y_um=20_000,
        width_um=200_000,
        length_um=200_000,
        cutter_envelope_x_um=20_000,
        cutter_envelope_y_um=20_000,
        cutter_envelope_width_um=200_000,
        cutter_envelope_length_um=200_000,
    )
    second_outer = replace(
        first_outer,
        operation_id="op:second-001:outer",
        part_id="second",
        instance_id="second-001",
        x_um=230_000,
        cutter_envelope_x_um=230_000,
    )
    b_drill = replace(
        _operations()[1],
        operation_id="op:first-001:b-drill",
        setup_id=source_b.setup_id,
        part_id="first",
        instance_id="first-001",
        feature_id="b-drill",
        side=Side.B,
        x_um=217_500,
        y_um=300_000,
        depth_um=6_000,
    )
    tools = (_drill(), _mill())
    source_machine = linuxcnc_reference_router_1325()
    document = OperationsDocument(
        schema_version="custombuild.operations.v2",
        design_hash=_DESIGN_HASH,
        machine_profile_id=source_machine.profile_id,
        machine_profile_version=source_machine.version,
        setups=(source_b, source_a),
        operations=(b_drill, first_outer, second_outer),
        tool_catalog_version=source_machine.tool_library_version,
        tool_catalog_fingerprint=sha256_hex(canonical_json_bytes(tools)),
        tools=tools,
    )
    base_bound = replace(
        _bound_setup(),
        fixture_clearance_z_um=0,
        minimum_rapid_clearance_um=15_000,
    )
    bound_a = replace(
        base_bound,
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_a)),
    )
    bound_b = replace(
        base_bound,
        setup_id=source_b.setup_id,
        side=Side.B,
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_b)),
        wcs="G57",
        orientation=source_b.orientation,
        spoilboard_id=None,
        spoilboard_version=None,
        spoilboard_sha256=None,
        through_cut_allowance_um=0,
    )
    context = _context()
    collision_recipes = tuple(
        replace(recipe, process_accuracy_um=10_000, accepted_tolerance_um=10_000)
        if recipe.operation_kind == OperationKind.DRILL
        else recipe
        for recipe in context.recipes
        if recipe.operation_kind in {OperationKind.DRILL, OperationKind.CONTOUR}
    )
    collision_context = replace(
        context,
        setups=(bound_a, bound_b),
        recipes=collision_recipes,
    )
    with pytest.raises(ProductionCAMError, match="own finished part outline"):
        generate_production_toolpaths(document, collision_context)


def test_missing_recipe_bounds_and_keepout_collisions_fail_closed() -> None:
    context = _context()
    without_pocket = replace(
        context,
        recipes=tuple(
            recipe for recipe in context.recipes if recipe.operation_kind != OperationKind.POCKET
        ),
    )
    with pytest.raises(ProductionCAMError, match="no exact"):
        generate_production_toolpaths(_document(), without_pocket)

    keepout = Rect(145_000, 95_000, 45_000, 55_000)
    colliding = _context(setup=_bound_setup(keep_out_zones=(keepout,)))
    with pytest.raises(ProductionCAMError, match="keep-out"):
        generate_production_toolpaths(_document(), colliding)

    assembly_only_keepout = Rect(100_000, 8_000, 2_000, 2_000)
    assembly_collision = _context(setup=_bound_setup(keep_out_zones=(assembly_only_keepout,)))
    with pytest.raises(ProductionCAMError, match="tool assembly"):
        generate_production_toolpaths(_document(), assembly_collision)

    operations = tuple(
        replace(operation, x_um=1_000, cutter_envelope_x_um=1_000)
        if operation.operation_id == "op:part-001:outer"
        else operation
        for operation in _operations()
    )
    with pytest.raises(ProductionCAMError, match="stock bounds"):
        generate_production_toolpaths(_document(operations=operations), context)


def test_accuracy_expanded_drill_must_stay_inside_its_own_finished_part() -> None:
    drill = next(operation for operation in _operations() if operation.kind == OperationKind.DRILL)
    edge_drill = replace(drill, x_um=22_500)
    operations = tuple(
        edge_drill if operation.operation_id == drill.operation_id else operation
        for operation in _operations()
    )
    with pytest.raises(ProductionCAMError, match="own finished part outline"):
        generate_production_toolpaths(_document(operations=operations), _context())


def test_unsupported_and_ambiguous_operation_shapes_fail_closed() -> None:
    pocket = next(
        operation for operation in _operations() if operation.kind == OperationKind.POCKET
    )
    circular = replace(pocket, diameter_um=6_000)
    remaining = tuple(
        circular if operation.operation_id == pocket.operation_id else operation
        for operation in _operations()
    )
    with pytest.raises(ProductionCAMError, match="circular"):
        generate_production_toolpaths(_document(operations=remaining), _context())

    engraved = replace(pocket, kind=OperationKind.ENGRAVE)
    remaining = tuple(
        engraved if operation.operation_id == pocket.operation_id else operation
        for operation in _operations()
    )
    with pytest.raises(ProductionCAMError, match="unsupported"):
        generate_production_toolpaths(_document(operations=remaining), _context())


def test_source_operation_traceability_and_tolerance_contract_fail_closed() -> None:
    outer = _operations()[0]
    untraceable = replace(
        outer,
        part_id="",
        instance_id="different-instance",
        feature_id="different-feature",
    )
    operations = (untraceable, *_operations()[1:])
    with pytest.raises(ProductionCAMError, match="non-canonical part_id"):
        generate_production_toolpaths(_document(operations=operations), _context())

    groove = _operations()[2]
    toleranced_groove = replace(groove, tolerance_um=50, fit_clearance_um=200)
    operations = tuple(
        toleranced_groove if operation.operation_id == groove.operation_id else operation
        for operation in _operations()
    )
    context = _context()
    with pytest.raises(ProductionCAMError, match="accept the operation tolerance"):
        generate_production_toolpaths(_document(operations=operations), context)

    accepted_recipes = tuple(
        replace(recipe, accepted_tolerance_um=50, process_accuracy_um=49)
        if recipe.operation_kind == OperationKind.GROOVE
        else recipe
        for recipe in context.recipes
    )
    generate_production_toolpaths(
        _document(operations=operations),
        replace(context, recipes=accepted_recipes),
    )
    exhausted_recipes = tuple(
        replace(recipe, process_accuracy_um=50)
        if recipe.operation_kind == OperationKind.GROOVE
        else recipe
        for recipe in accepted_recipes
    )
    with pytest.raises(ProductionCAMError, match="fit-clearance budget"):
        generate_production_toolpaths(
            _document(operations=operations),
            replace(context, recipes=exhausted_recipes),
        )


def test_programs_split_deterministically_by_spindle_speed() -> None:
    context = _context()
    recipes = tuple(
        replace(recipe, spindle_rpm=15_000)
        if recipe.operation_kind == OperationKind.GROOVE
        else recipe
        for recipe in context.recipes
    )
    candidate = generate_production_toolpaths(
        _document(),
        replace(context, recipes=recipes),
    )
    assert len(candidate.programs) == 4
    recipe_by_id = {recipe.recipe_id: recipe for recipe in recipes}
    for program in candidate.programs:
        assert len({recipe_by_id[recipe_id].spindle_rpm for recipe_id in program.recipe_ids}) == 1
    assert len({program.program_id for program in candidate.programs}) == 4


def test_no_program_may_follow_release_on_the_same_physical_sheet() -> None:
    source_a = replace(_source_setup(), tool_ids=("T06R",))
    source_b = replace(
        source_a,
        setup_id="setup:sheet:001:B",
        side=Side.B,
        wcs="G55",
        orientation="FLIP_STOCK_ABOUT_X_AXIS; MACHINE_Y=STOCK_HEIGHT-DESIGN_Y",
    )
    release_b = replace(
        _operations()[0],
        setup_id=source_b.setup_id,
        side=Side.B,
    )
    work_a = _operations()[3]
    mill = _mill()
    source_machine = linuxcnc_reference_router_1325()
    document = OperationsDocument(
        schema_version="custombuild.operations.v2",
        design_hash=_DESIGN_HASH,
        machine_profile_id=source_machine.profile_id,
        machine_profile_version=source_machine.version,
        setups=(source_b, source_a),
        operations=(release_b, work_a),
        tool_catalog_version=source_machine.tool_library_version,
        tool_catalog_fingerprint=sha256_hex(canonical_json_bytes((mill,))),
        tools=(mill,),
    )
    base_bound = _bound_setup()
    bound_a = replace(
        base_bound,
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_a)),
        spoilboard_id=None,
        spoilboard_version=None,
        spoilboard_sha256=None,
        through_cut_allowance_um=0,
    )
    bound_b = replace(
        base_bound,
        setup_id=source_b.setup_id,
        side=Side.B,
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_b)),
        wcs="G57",
        orientation=source_b.orientation,
    )
    context = _context()
    mill_binding = context.tool_bindings[1]
    recipes = tuple(
        recipe
        for recipe in context.recipes
        if recipe.operation_kind in {OperationKind.CONTOUR, OperationKind.POCKET}
    )
    context = replace(
        context,
        setups=(bound_a, bound_b),
        tool_bindings=(mill_binding,),
        recipes=recipes,
    )
    with pytest.raises(ProductionCAMError, match="follows a release contour"):
        generate_production_toolpaths(document, context)


def test_versioned_dogbone_centres_are_cut_without_unwanted_connecting_slots() -> None:
    groove = next(
        operation for operation in _operations() if operation.kind == OperationKind.GROOVE
    )
    dogbone = replace(
        groove,
        cutter_envelope_x_um=100_000,
        cutter_envelope_y_um=97_000,
        cutter_envelope_width_um=23_000,
        cutter_envelope_length_um=86_000,
        corner_strategy="dogbone-v2",
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_min",),
    )
    operations = tuple(
        dogbone if operation.operation_id == groove.operation_id else operation
        for operation in _operations()
    )

    candidate = generate_production_toolpaths(_document(operations=operations), _context())
    groove_moves = tuple(
        move
        for program in candidate.programs
        for move in program.moves
        if move.operation_id == groove.operation_id
    )
    relief_positions = {
        (move.x_um, move.y_um)
        for move in groove_moves
        if move.role == ProductionMoveRole.POSITION and move.x_um == 120_000
    }
    assert relief_positions == {(120_000, 100_000), (120_000, 180_000)}
    for position in relief_positions:
        index = next(
            move_index
            for move_index, move in enumerate(groove_moves)
            if move.role == ProductionMoveRole.POSITION and (move.x_um, move.y_um) == position
        )
        assert groove_moves[index - 1].role == ProductionMoveRole.RETRACT


def test_countersink_fails_closed_until_tip_depth_geometry_is_bound() -> None:
    source_machine = linuxcnc_reference_router_1325()
    countersink_tool = next(tool for tool in source_machine.tools if tool.tool_id == "T08D")
    operation = CAMOperation(
        operation_id="op:part-001:countersink",
        setup_id="setup:sheet:001:A",
        part_id="part",
        instance_id="part-001",
        feature_id="countersink",
        kind=OperationKind.COUNTERSINK,
        side=Side.A,
        tool_id=countersink_tool.tool_id,
        x_um=75_000,
        y_um=75_000,
        depth_um=2_000,
        diameter_um=8_000,
        stepdown_um=1_000,
    )
    source_setup = replace(_source_setup(), tool_ids=(countersink_tool.tool_id,))
    document = OperationsDocument(
        schema_version="custombuild.operations.v2",
        design_hash=_DESIGN_HASH,
        machine_profile_id=source_machine.profile_id,
        machine_profile_version=source_machine.version,
        setups=(source_setup,),
        operations=(operation,),
        tool_catalog_version=source_machine.tool_library_version,
        tool_catalog_fingerprint=sha256_hex(canonical_json_bytes((countersink_tool,))),
        tools=(countersink_tool,),
    )
    binding = _binding(countersink_tool, ProductionToolGeometry.COUNTERSINK)
    recipe = CuttingRecipe(
        recipe_id="recipe-SHOP-T08D-countersink",
        version="1.0.0",
        machine_profile_id="workshop-router-7",
        machine_profile_version="calibration-2026.09",
        material_id="birch-ply",
        material_version="2026.1",
        tool_id=binding.tool_id,
        tool_version=binding.tool_version,
        operation_kind=OperationKind.COUNTERSINK,
        spindle_rpm=8_000,
        feed_um_min=300_000,
        plunge_um_min=150_000,
        stepdown_um=1_000,
        stepover_ppm=400_000,
        peck_depth_um=1_000,
        approach_clearance_um=2_000,
        through_overtravel_um=0,
        tab_width_um=0,
        tab_height_um=0,
        process_accuracy_um=50,
        accepted_tolerance_um=200,
        countersink_top_diameter_um=8_000,
        countersink_included_angle_mdeg=90_000,
    )
    bound_setup = replace(
        _bound_setup(),
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_setup)),
        spoilboard_id=None,
        spoilboard_version=None,
        spoilboard_sha256=None,
        through_cut_allowance_um=0,
    )
    context = replace(
        _context(),
        setups=(bound_setup,),
        tool_bindings=(binding,),
        recipes=(recipe,),
    )

    with pytest.raises(ProductionCAMError, match="unsupported production operation"):
        generate_production_toolpaths(document, context)
    with pytest.raises(ProductionCAMError, match="top diameter and included angle"):
        replace(recipe, countersink_included_angle_mdeg=None)


def test_production_model_value_objects_reject_ambiguous_shop_state() -> None:
    context = _context()
    drill_binding, _mill_binding = context.tool_bindings

    with pytest.raises(ProductionCAMError, match="canonical non-blank identity"):
        replace(drill_binding, tool_id="")
    with pytest.raises(ProductionCAMError, match="effective_diameter_um must be at least 1"):
        replace(drill_binding, effective_diameter_um=0)
    with pytest.raises(ProductionCAMError, match="smaller than measured stickout"):
        replace(
            drill_binding,
            minimum_holder_clearance_um=drill_binding.measured_stickout_um,
        )
    with pytest.raises(ProductionCAMError, match="production-tool enum"):
        replace(drill_binding, geometry=cast(ProductionToolGeometry, "DRILL"))
    with pytest.raises(ProductionCAMError, match="center_cutting must be a boolean"):
        replace(drill_binding, center_cutting=cast(bool, 1))
    with pytest.raises(ProductionCAMError, match="clockwise spindle"):
        replace(drill_binding, spindle_direction="CCW")

    pocket_recipe = _recipe(_mill(), OperationKind.POCKET)
    with pytest.raises(ProductionCAMError, match="process accuracy cannot exceed"):
        replace(
            pocket_recipe,
            process_accuracy_um=pocket_recipe.accepted_tolerance_um + 1,
        )
    with pytest.raises(ProductionCAMError, match="cannot exceed one tool diameter"):
        replace(pocket_recipe, stepover_ppm=1_000_001)
    with pytest.raises(ProductionCAMError, match="explicit PLUNGE entry"):
        replace(pocket_recipe, entry_strategy="RAMP")
    with pytest.raises(ProductionCAMError, match="only valid for drill recipes"):
        replace(pocket_recipe, diameter_tolerance_um=1)

    contour_recipe = _recipe(_mill(), OperationKind.CONTOUR)
    with pytest.raises(ProductionCAMError, match="explicit tab width and height"):
        replace(contour_recipe, tab_width_um=0)

    countersink_recipe = replace(
        pocket_recipe,
        recipe_id="recipe-SHOP-T06R-countersink",
        operation_kind=OperationKind.COUNTERSINK,
        countersink_top_diameter_um=12_000,
        countersink_included_angle_mdeg=90_000,
    )
    with pytest.raises(ProductionCAMError, match="at least 60000"):
        replace(countersink_recipe, countersink_included_angle_mdeg=59_999)
    with pytest.raises(ProductionCAMError, match="exceeds 120 degrees"):
        replace(countersink_recipe, countersink_included_angle_mdeg=120_001)
    with pytest.raises(ProductionCAMError, match="only valid for countersink recipes"):
        replace(pocket_recipe, countersink_top_diameter_um=12_000)

    with pytest.raises(ProductionCAMError, match="canonical non-blank setup fact"):
        replace(_bound_setup(), orientation=" A_SIDE_UP")
    with pytest.raises(ProductionCAMError, match="does not support edge setups"):
        replace(_bound_setup(), side=Side.EDGE)
    with pytest.raises(ProductionCAMError, match="keep-out policy"):
        replace(_bound_setup(), keep_out_policy="INFER_CLEARANCE")
    with pytest.raises(ProductionCAMError, match="requires a positive through-cut allowance"):
        replace(_bound_setup(), through_cut_allowance_um=0)

    first_zone = Rect(20_000, 20_000, 10_000, 10_000)
    second_zone = Rect(40_000, 40_000, 10_000, 10_000)
    with pytest.raises(ProductionCAMError, match="canonical order"):
        _bound_setup(keep_out_zones=(second_zone, first_zone))
    with pytest.raises(ProductionCAMError, match="must be unique"):
        _bound_setup(keep_out_zones=(first_zone, first_zone))


def test_execution_context_rejects_noncanonical_and_unbound_catalogs() -> None:
    context = _context()

    with pytest.raises(ProductionCAMError, match="X minimum must be below"):
        replace(
            context,
            machine_x_min_um=context.machine_x_max_um,
            work_width_um=1,
        )
    with pytest.raises(ProductionCAMError, match="requires setups, tool bindings"):
        replace(context, setups=())

    side_b = replace(
        context.setups[0],
        setup_id="setup:sheet:001:B",
        side=Side.B,
        wcs="G57",
    )
    with pytest.raises(ProductionCAMError, match="canonical setup_id order"):
        replace(context, setups=(side_b, context.setups[0]))
    with pytest.raises(ProductionCAMError, match="canonical source-tool order"):
        replace(context, tool_bindings=tuple(reversed(context.tool_bindings)))
    with pytest.raises(ProductionCAMError, match="canonical binding order"):
        replace(context, recipes=tuple(reversed(context.recipes)))

    wrong_machine_recipe = replace(
        context.recipes[0],
        machine_profile_id="different-router",
    )
    with pytest.raises(ProductionCAMError, match="recipe machine binding mismatch"):
        replace(context, recipes=(wrong_machine_recipe, *context.recipes[1:]))

    renamed_binding = replace(context.tool_bindings[0], tool_id="SHOP-T05-RENAMED")
    with pytest.raises(ProductionCAMError, match="recipe references an unbound tool"):
        replace(context, tool_bindings=(renamed_binding, context.tool_bindings[1]))


def test_move_program_and_document_invariants_fail_closed() -> None:
    candidate = generate_production_toolpaths(_document(), _context())
    first_program, area_program, release_program = candidate.programs
    rapid = first_program.moves[0]
    linear = next(move for move in first_program.moves if move.kind == ProductionMoveKind.LINEAR)

    with pytest.raises(ProductionCAMError, match="rapid moves cannot carry"):
        replace(rapid, feed_um_min=1)
    with pytest.raises(ProductionCAMError, match="rapid move has a cutting-only role"):
        replace(rapid, role=ProductionMoveRole.CUT)
    with pytest.raises(ProductionCAMError, match="linear moves require"):
        replace(linear, feed_um_min=None)
    with pytest.raises(ProductionCAMError, match="linear move has a rapid-only role"):
        replace(linear, role=ProductionMoveRole.RETRACT)

    with pytest.raises(ProductionCAMError, match="recipe IDs must be non-empty"):
        replace(first_program, recipe_ids=())
    with pytest.raises(ProductionCAMError, match="operation IDs must be non-empty"):
        replace(first_program, operation_ids=())
    with pytest.raises(ProductionCAMError, match="release operation IDs must belong"):
        replace(first_program, release_operation_ids=("op:not-in-program",))
    with pytest.raises(ProductionCAMError, match="release contours must be the final"):
        replace(area_program, release_operation_ids=(area_program.operation_ids[0],))
    with pytest.raises(ProductionCAMError, match="program cannot be empty"):
        replace(first_program, moves=())
    with pytest.raises(ProductionCAMError, match="move sequences must be dense"):
        replace(
            first_program,
            moves=(replace(first_program.moves[0], sequence=2), *first_program.moves[1:]),
        )
    with pytest.raises(ProductionCAMError, match="moves must exactly cover"):
        replace(
            first_program,
            moves=tuple(
                replace(move, operation_id="op:undeclared") for move in first_program.moves
            ),
        )

    for field, value, message in (
        ("schema_version", "custombuild.toolpaths.v0", "unsupported production toolpath schema"),
        ("engine_version", "unknown-engine", "unsupported production toolpath engine"),
        ("mode", "VALIDATION", "must remain an executable candidate"),
        ("physical_cutting_authorized", True, "cannot authorize physical cutting"),
        ("workshop_acceptance_required", False, "cannot be bypassed"),
    ):
        with pytest.raises(ProductionCAMError, match=message):
            replace(candidate, **{field: value})

    with pytest.raises(ProductionCAMError, match="requires programs"):
        replace(candidate, programs=())
    with pytest.raises(ProductionCAMError, match="run order must be dense"):
        replace(candidate, programs=(replace(first_program, run_order=2), *candidate.programs[1:]))
    with pytest.raises(ProductionCAMError, match="duplicate production program ID"):
        replace(
            candidate,
            programs=(
                first_program,
                replace(area_program, program_id=first_program.program_id),
                release_program,
            ),
        )

    duplicate_operation = replace(
        first_program,
        program_id="program:duplicate-operation",
        run_order=2,
    )
    with pytest.raises(ProductionCAMError, match="cannot belong to multiple programs"):
        replace(candidate, programs=(first_program, duplicate_operation))

    with pytest.raises(ProductionCAMError, match="unknown setup or tool"):
        replace(
            candidate,
            programs=(
                replace(first_program, setup_id="setup:missing"),
                *candidate.programs[1:],
            ),
        )
    with pytest.raises(ProductionCAMError, match="tool version differs"):
        replace(
            candidate,
            programs=(
                replace(first_program, tool_version="unbound-version"),
                *candidate.programs[1:],
            ),
        )
    with pytest.raises(ProductionCAMError, match="unknown recipe"):
        replace(
            candidate,
            programs=(
                replace(first_program, recipe_ids=("recipe:missing",)),
                *candidate.programs[1:],
            ),
        )
    with pytest.raises(ProductionCAMError, match="recipe binding differs"):
        replace(
            candidate,
            programs=(
                replace(first_program, recipe_ids=area_program.recipe_ids),
                *candidate.programs[1:],
            ),
        )

    release_first = replace(release_program, run_order=1)
    work_after_release = replace(first_program, run_order=2)
    with pytest.raises(ProductionCAMError, match="program follows release contour"):
        replace(candidate, programs=(release_first, work_after_release))

    for field in (
        "machine_profile_fingerprint",
        "tool_catalog_fingerprint",
        "recipe_catalog_fingerprint",
    ):
        with pytest.raises(ProductionCAMError, match="fingerprint mismatch"):
            replace(candidate, **{field: "0" * 64})


def test_source_binding_defense_in_depth_rejects_mutated_inputs() -> None:
    for field, value, message in (
        ("schema_version", "custombuild.operations.v1", "unsupported operations schema"),
        ("mode", "PRODUCTION", "only accepts the validation source contract"),
        ("design_hash", "not-a-hash", "design_hash must be"),
    ):
        document = _document()
        object.__setattr__(document, field, value)
        with pytest.raises(ProductionCAMError, match=message):
            generate_production_toolpaths(document, _context())

    with pytest.raises(ProductionCAMError, match="profile binding differs"):
        generate_production_toolpaths(
            _document(),
            replace(_context(), source_machine_profile_version="other-version"),
        )

    untrusted_document = _document()
    object.__setattr__(untrusted_document, "machine_profile_id", "untrusted-router")
    with pytest.raises(ProductionCAMError, match="profile is not trusted"):
        generate_production_toolpaths(
            untrusted_document,
            replace(_context(), source_machine_profile_id="untrusted-router"),
        )

    invalid_source = _document()
    object.__setattr__(invalid_source, "tool_catalog_fingerprint", "0" * 64)
    with pytest.raises(ProductionCAMError, match="source operations document is invalid"):
        generate_production_toolpaths(invalid_source, _context())

    uncovered_context = _context()
    object.__setattr__(uncovered_context, "setups", ())
    with pytest.raises(ProductionCAMError, match="must exactly cover source setups"):
        generate_production_toolpaths(_document(), uncovered_context)

    context = _context()
    changed_setup = replace(
        context.setups[0],
        stock_width_um=context.setups[0].stock_width_um + 1,
    )
    with pytest.raises(ProductionCAMError, match="differs from source geometry"):
        generate_production_toolpaths(
            _document(),
            replace(context, setups=(changed_setup,)),
        )

    source_keepout = Rect(0, 0, 5_000, 5_000)
    source_setup = _source_setup(keep_out_zones=(source_keepout,))
    keepout_document = replace(_document(), setups=(source_setup,))
    rebound_setup = replace(
        context.setups[0],
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_setup)),
    )
    with pytest.raises(ProductionCAMError, match="omits a source keep-out zone"):
        generate_production_toolpaths(
            keepout_document,
            replace(context, setups=(rebound_setup,)),
        )

    no_allowance = replace(
        context.setups[0],
        spoilboard_id=None,
        spoilboard_version=None,
        spoilboard_sha256=None,
        through_cut_allowance_um=0,
    )
    with pytest.raises(ProductionCAMError, match="lacks an exact spoilboard allowance"):
        generate_production_toolpaths(
            _document(),
            replace(context, setups=(no_allowance,)),
        )

    non_through_outer = replace(
        _operations()[0],
        depth_um=10_000,
        through=False,
        holding_strategy=None,
    )
    non_through_operations = (non_through_outer, *_operations()[1:])
    with pytest.raises(ProductionCAMError, match="without through cuts declares"):
        generate_production_toolpaths(
            _document(operations=non_through_operations),
            context,
        )

    incomplete_tools = _context()
    object.__setattr__(incomplete_tools, "tool_bindings", incomplete_tools.tool_bindings[:1])
    with pytest.raises(ProductionCAMError, match="must exactly cover every operation tool"):
        generate_production_toolpaths(_document(), incomplete_tools)

    changed_version = replace(
        context.tool_bindings[0],
        source_tool_version="other-version",
    )
    with pytest.raises(ProductionCAMError, match="source tool version mismatch"):
        generate_production_toolpaths(
            _document(),
            replace(context, tool_bindings=(changed_version, context.tool_bindings[1])),
        )

    changed_source_hash = replace(
        context.tool_bindings[0],
        source_tool_sha256="0" * 64,
    )
    with pytest.raises(ProductionCAMError, match="source fingerprint mismatch"):
        generate_production_toolpaths(
            _document(),
            replace(context, tool_bindings=(changed_source_hash, context.tool_bindings[1])),
        )

    odd_diameter = replace(
        context.tool_bindings[0],
        effective_diameter_um=context.tool_bindings[0].effective_diameter_um + 1,
    )
    with pytest.raises(ProductionCAMError, match="even cutter diameter"):
        generate_production_toolpaths(
            _document(),
            replace(context, tool_bindings=(odd_diameter, context.tool_bindings[1])),
        )


def test_collision_geometry_kernel_covers_exact_touch_and_clear_boundaries() -> None:
    rectangle = Rect(10, 10, 10, 10)

    assert toolpath_module._segment_radius_touches_rect((0, 15), (30, 15), 0, rectangle)
    assert toolpath_module._segment_radius_touches_rect((5, 5), (5, 6), 8, rectangle)
    assert toolpath_module._segment_radius_touches_rect((0, 0), (8, 8), 3, rectangle)
    assert not toolpath_module._segment_radius_touches_rect((0, 0), (0, 5), 2, rectangle)

    assert toolpath_module._segment_intersects_rect((0, 15), (30, 15), rectangle)
    assert toolpath_module._segment_intersects_rect((10, 10), (5, 5), rectangle)
    assert not toolpath_module._segment_intersects_rect((0, 0), (5, 5), rectangle)

    touching_segments = (
        ((0, 0), (10, 0), (5, 0), (5, 5)),
        ((0, 0), (10, 0), (5, 5), (5, 0)),
        ((5, 0), (5, 5), (0, 0), (10, 0)),
        ((5, 5), (5, 0), (0, 0), (10, 0)),
        ((0, 0), (10, 10), (0, 10), (10, 0)),
    )
    for first_start, first_end, second_start, second_end in touching_segments:
        assert toolpath_module._segments_touch_or_cross(
            first_start,
            first_end,
            second_start,
            second_end,
        )
    assert not toolpath_module._segments_touch_or_cross((0, 0), (2, 0), (3, 1), (4, 1))

    point_segment_cases = (
        ((0, 0), (1, 1), (1, 1), 0, False),
        ((1, 1), (1, 1), (1, 1), 0, True),
        ((0, 0), (1, 0), (3, 0), 1, True),
        ((0, 2), (1, 0), (3, 0), 1, False),
        ((4, 0), (1, 0), (3, 0), 1, True),
        ((4, 2), (1, 0), (3, 0), 1, False),
        ((2, 1), (1, 0), (3, 0), 1, True),
        ((2, 2), (1, 0), (3, 0), 1, False),
    )
    for point, start, end, radius_um, expected in point_segment_cases:
        assert (
            toolpath_module._point_segment_within_radius(point, start, end, radius_um) is expected
        )

    assert toolpath_module._point_rect_distance_squared((0, 0), rectangle) == 200
    assert toolpath_module._point_rect_distance_squared((15, 15), rectangle) == 0
    assert toolpath_module._orientation((0, 0), (10, 0), (5, 5)) > 0
    assert toolpath_module._point_on_segment((5, 0), (0, 0), (10, 0))


def test_source_edge_mapping_is_explicit_for_rotation_and_side() -> None:
    assert (
        toolpath_module._source_edge_machine_boundary("u_min", rotated_90=False, side=Side.A)
        == "x_min"
    )
    assert (
        toolpath_module._source_edge_machine_boundary("u_max", rotated_90=True, side=Side.A)
        == "y_max"
    )
    assert (
        toolpath_module._source_edge_machine_boundary("v_min", rotated_90=True, side=Side.A)
        == "x_max"
    )
    assert (
        toolpath_module._source_edge_machine_boundary("v_max", rotated_90=False, side=Side.B)
        == "y_min"
    )
    with pytest.raises(ProductionCAMError, match="unsupported open-end edge"):
        toolpath_module._source_edge_machine_boundary(
            "diagonal",
            rotated_90=False,
            side=Side.A,
        )
