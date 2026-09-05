from __future__ import annotations

from dataclasses import replace

import pytest
from custombuild_cam import production_verification as verification_module
from custombuild_cam.production_model import (
    FIXTURE_KEEPOUT_POLICY,
    IDENTITY_SOURCE_TO_WCS_XY,
    STOCK_TOP_Z0_REFERENCE,
    BoundSetup,
    CuttingRecipe,
    ProductionExecutionContext,
    ProductionMove,
    ProductionMoveKind,
    ProductionMoveRole,
    ProductionProgram,
    ProductionToolBinding,
    ProductionToolGeometry,
    ProductionToolpathDocument,
)
from custombuild_cam.production_verification import (
    CuttingProgramStatus,
    cutting_backplot_svg,
    cutting_program_report_json,
    verify_production_toolpaths,
)
from custombuild_cam.toolpaths import generate_production_toolpaths
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
_TOOL_TABLE_HASH = "a" * 64


def _source_tools() -> tuple[ToolSpec, ToolSpec]:
    tools = {tool.tool_id: tool for tool in linuxcnc_reference_router_1325().tools}
    return tools["T05"], tools["T06R"]


def _source_setup() -> Setup:
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
        keep_out_zones=(),
        tool_ids=("T05", "T06R"),
        probe_method="EXTERNAL_COORDINATE_REGISTRATION_REQUIRED",
        operator_steps=("Validation only",),
    )


def _source_document() -> OperationsDocument:
    drill, mill = _source_tools()
    machine = linuxcnc_reference_router_1325()
    operations = (
        CAMOperation(
            operation_id="op:part-001:outer",
            setup_id="setup:sheet:001:A",
            part_id="part",
            instance_id="part-001",
            feature_id="outer",
            kind=OperationKind.CONTOUR,
            side=Side.A,
            tool_id=mill.tool_id,
            x_um=20_000,
            y_um=20_000,
            depth_um=18_000,
            width_um=400_000,
            length_um=300_000,
            cutter_envelope_x_um=20_000,
            cutter_envelope_y_um=20_000,
            cutter_envelope_width_um=400_000,
            cutter_envelope_length_um=300_000,
            stepdown_um=3_000,
            through=True,
            compensation="OUTSIDE",
            holding_strategy="TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL",
        ),
        CAMOperation(
            operation_id="op:part-001:drill-a",
            setup_id="setup:sheet:001:A",
            part_id="part",
            instance_id="part-001",
            feature_id="drill-a",
            kind=OperationKind.DRILL,
            side=Side.A,
            tool_id=drill.tool_id,
            x_um=50_000,
            y_um=50_000,
            depth_um=8_000,
            diameter_um=5_000,
            stepdown_um=3_000,
        ),
        CAMOperation(
            operation_id="op:part-001:drill-b",
            setup_id="setup:sheet:001:A",
            part_id="part",
            instance_id="part-001",
            feature_id="drill-b",
            kind=OperationKind.DRILL,
            side=Side.A,
            tool_id=drill.tool_id,
            x_um=100_000,
            y_um=50_000,
            depth_um=6_000,
            diameter_um=5_000,
            stepdown_um=3_000,
        ),
    )
    tools = (drill, mill)
    return OperationsDocument(
        schema_version="custombuild.operations.v2",
        design_hash=_DESIGN_HASH,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        setups=(_source_setup(),),
        operations=operations,
        tool_catalog_version=machine.tool_library_version,
        tool_catalog_fingerprint=sha256_hex(canonical_json_bytes(tools)),
        tools=tools,
    )


def _binding(
    source_tool: ToolSpec,
    geometry: ProductionToolGeometry,
) -> ProductionToolBinding:
    controller_number = 5 if source_tool.tool_id == "T05" else 6
    return ProductionToolBinding(
        tool_id=f"SHOP-{source_tool.tool_id}",
        tool_version="measured-2026.09",
        source_tool_id=source_tool.tool_id,
        source_tool_version=source_tool.version,
        source_tool_sha256=sha256_hex(canonical_json_bytes(source_tool)),
        controller_tool_number=controller_number,
        length_offset_number=controller_number,
        expected_length_offset_x_um=0,
        expected_length_offset_y_um=0,
        expected_length_offset_z_um=35_000,
        tool_table_evidence_id="linuxcnc-tool-table-7",
        tool_table_evidence_version="snapshot-2026.09",
        tool_table_evidence_sha256=_TOOL_TABLE_HASH,
        effective_diameter_um=source_tool.effective_diameter_um,
        cutting_length_um=28_000,
        measured_stickout_um=35_000,
        minimum_holder_clearance_um=5_000,
        assembly_collision_radius_um=10_000,
        geometry=geometry,
        center_cutting=True,
        drill_point_length_um=0,
    )


def _recipe(
    binding: ProductionToolBinding,
    kind: OperationKind,
) -> CuttingRecipe:
    contour = kind == OperationKind.CONTOUR
    drill = kind == OperationKind.DRILL
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
        spindle_rpm=10_000 if drill else 16_000,
        feed_um_min=400_000 if drill else 1_800_000,
        plunge_um_min=200_000 if drill else 400_000,
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


def _execution_context(source: OperationsDocument) -> ProductionExecutionContext:
    machine = linuxcnc_reference_router_1325()
    bindings = tuple(
        _binding(
            tool,
            ProductionToolGeometry.DRILL
            if tool.tool_id == "T05"
            else ProductionToolGeometry.FLAT_END_MILL,
        )
        for tool in source.tools
    )
    binding_by_source_id = {binding.source_tool_id: binding for binding in bindings}
    bound_setups = []
    for source_setup in sorted(source.setups, key=lambda item: item.setup_id):
        has_through = any(
            operation.through and operation.setup_id == source_setup.setup_id
            for operation in source.operations
        )
        bound_setups.append(
            BoundSetup(
                setup_id=source_setup.setup_id,
                stock_id=source_setup.stock_id,
                source_material_id=source_setup.material_id,
                source_material_version=source_setup.material_version,
                material_id=source_setup.material_id,
                material_version=source_setup.material_version,
                material_evidence_id="supplier-declaration-birch-ply",
                material_evidence_version="2026.1",
                material_evidence_sha256="9" * 64,
                sheet_index=source_setup.sheet_index,
                side=source_setup.side,
                source_setup_sha256=sha256_hex(canonical_json_bytes(source_setup)),
                source_to_wcs_xy_transform=IDENTITY_SOURCE_TO_WCS_XY,
                wcs="G56" if source_setup.side == Side.A else "G57",
                machine_wcs_origin=Point2D(100_000, 50_000),
                machine_wcs_z0_um=0,
                machine_wcs_xy_rotation_mdeg=0,
                stock_width_um=source_setup.stock_width_um,
                stock_height_um=source_setup.stock_height_um,
                stock_thickness_um=source_setup.stock_thickness_um,
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
                keep_out_zones=source_setup.keep_out_zones,
                spoilboard_id="spoilboard-7" if has_through else None,
                spoilboard_version="measured-2026.09" if has_through else None,
                spoilboard_sha256="e" * 64 if has_through else None,
                through_cut_allowance_um=500 if has_through else 0,
            )
        )
    recipe_keys = sorted(
        {(operation.tool_id, operation.kind) for operation in source.operations},
        key=lambda item: (item[0], item[1].value),
    )
    recipes = tuple(_recipe(binding_by_source_id[tool_id], kind) for tool_id, kind in recipe_keys)
    return ProductionExecutionContext(
        source_machine_profile_id=machine.profile_id,
        source_machine_profile_version=machine.version,
        source_machine_profile_fingerprint=sha256_hex(canonical_json_bytes(machine)),
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
        setups=tuple(bound_setups),
        tool_bindings=bindings,
        recipes=recipes,
    )


def _candidate() -> tuple[OperationsDocument, ProductionToolpathDocument]:
    source = _source_document()
    return source, generate_production_toolpaths(source, _execution_context(source))


def _two_instance_source_document() -> OperationsDocument:
    source = _source_document()
    first_outline = replace(
        source.operations[0],
        width_um=200_000,
        length_um=200_000,
        cutter_envelope_width_um=200_000,
        cutter_envelope_length_um=200_000,
    )
    second_outline = replace(
        first_outline,
        operation_id="op:part-002:outer",
        part_id="part-2",
        instance_id="part-002",
        x_um=300_000,
        cutter_envelope_x_um=300_000,
    )
    return replace(
        source,
        operations=(first_outline, *source.operations[1:], second_outline),
    )


def _two_sided_source_document() -> OperationsDocument:
    source = _source_document()
    side_a = replace(_source_setup(), tool_ids=("T06R",))
    side_b = replace(
        _source_setup(),
        setup_id="setup:sheet:001:B",
        side=Side.B,
        orientation="FLIP_STOCK_ABOUT_X_AXIS; MACHINE_Y=STOCK_HEIGHT-DESIGN_Y",
        tool_ids=("T05",),
    )
    side_b_drill = replace(
        source.operations[1],
        setup_id=side_b.setup_id,
        side=Side.B,
        x_um=100_000,
        y_um=320_000,
    )
    side_a_outline = replace(
        source.operations[0],
        width_um=200_000,
        length_um=200_000,
        cutter_envelope_width_um=200_000,
        cutter_envelope_length_um=200_000,
    )
    return replace(
        source,
        setups=(side_b, side_a),
        operations=(side_b_drill, side_a_outline),
    )


def _dogbone_source_document() -> OperationsDocument:
    source = _source_document()
    groove = CAMOperation(
        operation_id="op:part-001:groove",
        setup_id=source.setups[0].setup_id,
        part_id="part",
        instance_id="part-001",
        feature_id="groove",
        kind=OperationKind.GROOVE,
        side=Side.A,
        tool_id="T06R",
        x_um=150_000,
        y_um=100_000,
        depth_um=6_000,
        width_um=30_000,
        length_um=80_000,
        cutter_envelope_x_um=147_000,
        cutter_envelope_y_um=97_000,
        cutter_envelope_width_um=36_000,
        cutter_envelope_length_um=86_000,
        stepdown_um=3_000,
        stepover_ppm=400_000,
        corner_strategy="dogbone-v2",
        corner_relief_radius_um=3_000,
    )
    return replace(source, operations=(*source.operations, groove))


def _pocket_source_document() -> OperationsDocument:
    source = _source_document()
    pocket = CAMOperation(
        operation_id="op:part-001:pocket",
        setup_id=source.setups[0].setup_id,
        part_id="part",
        instance_id="part-001",
        feature_id="pocket",
        kind=OperationKind.POCKET,
        side=Side.A,
        tool_id="T06R",
        x_um=150_000,
        y_um=100_000,
        depth_um=6_000,
        width_um=30_000,
        length_um=40_000,
        cutter_envelope_x_um=150_000,
        cutter_envelope_y_um=100_000,
        cutter_envelope_width_um=30_000,
        cutter_envelope_length_um=40_000,
        stepdown_um=3_000,
        stepover_ppm=400_000,
    )
    return replace(source, operations=(*source.operations, pocket))


def _open_edge_groove_source_document() -> OperationsDocument:
    source = _source_document()
    groove = CAMOperation(
        operation_id="op:part-001:open-groove",
        setup_id=source.setups[0].setup_id,
        part_id="part",
        instance_id="part-001",
        feature_id="open-groove",
        kind=OperationKind.GROOVE,
        side=Side.A,
        tool_id="T06R",
        x_um=20_000,
        y_um=100_000,
        depth_um=6_000,
        width_um=30_000,
        length_um=80_000,
        cutter_envelope_x_um=20_000,
        cutter_envelope_y_um=97_000,
        cutter_envelope_width_um=33_000,
        cutter_envelope_length_um=86_000,
        stepdown_um=3_000,
        stepover_ppm=400_000,
        corner_strategy="dogbone-v2",
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_min",),
    )
    return replace(source, operations=(*source.operations, groove))


def _rotated_open_edge_groove_source_document() -> OperationsDocument:
    source = _source_document()
    rotated_operations = tuple(
        replace(operation, source_rotation_90=True) for operation in source.operations
    )
    groove = CAMOperation(
        operation_id="op:part-001:rotated-open-groove",
        setup_id=source.setups[0].setup_id,
        part_id="part",
        instance_id="part-001",
        feature_id="rotated-open-groove",
        kind=OperationKind.GROOVE,
        side=Side.A,
        tool_id="T06R",
        x_um=100_000,
        y_um=20_000,
        depth_um=6_000,
        width_um=80_000,
        length_um=30_000,
        cutter_envelope_x_um=97_000,
        cutter_envelope_y_um=20_000,
        cutter_envelope_width_um=86_000,
        cutter_envelope_length_um=33_000,
        stepdown_um=3_000,
        stepover_ppm=400_000,
        source_rotation_90=True,
        corner_strategy="dogbone-v2",
        corner_relief_radius_um=3_000,
        open_end_reliefs=("u_min",),
    )
    return replace(source, operations=(*rotated_operations, groove))


def _replace_program(
    candidate: ProductionToolpathDocument,
    index: int,
    program: ProductionProgram,
) -> ProductionToolpathDocument:
    programs = list(candidate.programs)
    programs[index] = program
    return replace(candidate, programs=tuple(programs))


def _resequence(moves: tuple[ProductionMove, ...]) -> tuple[ProductionMove, ...]:
    return tuple(replace(move, sequence=index) for index, move in enumerate(moves, start=1))


def _issue_codes(
    candidate: ProductionToolpathDocument,
    source: OperationsDocument,
) -> set[str]:
    return {issue.code for issue in verify_production_toolpaths(candidate, source).report.issues}


def test_valid_candidate_report_envelopes_and_backplot_are_deterministic() -> None:
    source, candidate = _candidate()

    first = verify_production_toolpaths(candidate, source)
    second = verify_production_toolpaths(candidate, source)
    report_json = cutting_program_report_json(candidate, source)
    first_svg = cutting_backplot_svg(candidate, source)
    second_svg = cutting_backplot_svg(candidate, source)

    assert first == second
    assert first.report.status == CuttingProgramStatus.PASS
    assert first.report.issue_count == 0
    assert first.report.toolpath_sha256 == candidate.fingerprint
    assert first.report.operations_sha256 == sha256_hex(source.to_json())
    assert first.report.physical_cutting_authorized is False
    assert first.report.workshop_acceptance_required is True
    assert report_json == first.report.to_json()
    assert first_svg == second_svg
    assert b'data-verification-status="PASS"' in first_svg
    assert f'data-toolpath-sha256="{candidate.fingerprint}"'.encode() in first_svg
    assert b'data-physical-cutting-authorized="false"' in first_svg
    assert b'data-workshop-acceptance-required="true"' in first_svg
    assert b'data-program-id="program:' in first_svg
    assert b'data-tool-id="SHOP-T05"' in first_svg
    assert b'data-pass-index="1"' in first_svg
    assert b'data-depth-um="-3000"' in first_svg
    assert b"<circle" in first_svg
    assert b"<script" not in first_svg
    assert b"href=" not in first_svg

    first_drill_cut = next(
        envelope
        for envelope in first.swept_envelopes
        if envelope.operation_id == "op:part-001:drill-a" and envelope.material_removal
    )
    assert first_drill_cut.x_min_um == 47_450
    assert first_drill_cut.x_max_um == 52_550
    assert first_drill_cut.assembly_x_min_um == 39_950
    assert first_drill_cut.assembly_x_max_um == 60_050
    assert first_drill_cut.material_z_min_um == -3_000
    assert first_drill_cut.material_z_max_um == 0
    assert first_drill_cut.machine_x_min_um == 149_950
    assert first_drill_cut.machine_x_max_um == 150_050
    assert first_drill_cut.machine_z_min_um == 31_950
    assert first_drill_cut.machine_z_max_um == 37_050
    assert b'data-h-offset-z-um="35000"' in first_svg
    assert b'data-machine-z-um="32000"' in first_svg


def test_independent_verifier_rejects_unresolved_or_unbound_actual_material() -> None:
    source, unresolved = _candidate()
    object.__setattr__(
        unresolved.execution_context.setups[0],
        "material_version",
        "screening-2026.1",
    )
    assert "MATERIAL_BINDING_INVALID" in _issue_codes(unresolved, source)

    source, detached = _candidate()
    object.__setattr__(
        detached.execution_context.setups[0],
        "material_evidence_sha256",
        "not-a-sha256",
    )
    assert "MATERIAL_BINDING_INVALID" in _issue_codes(detached, source)


def test_changed_cutting_move_blocks_geometry_and_removal_coverage() -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    move_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == "op:part-001:drill-a" and move.kind == ProductionMoveKind.LINEAR
    )
    moves = list(program.moves)
    moves[move_index] = replace(moves[move_index], x_um=moves[move_index].x_um + 100)
    mutated = _replace_program(candidate, 0, replace(program, moves=tuple(moves)))

    assert {
        "OPERATION_GEOMETRY_MISMATCH",
        "MATERIAL_REMOVAL_COVERAGE_INVALID",
    } <= _issue_codes(mutated, source)


def test_diagonal_entry_cannot_cut_a_trench_into_an_otherwise_valid_pocket() -> None:
    source = _pocket_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))
    program_index = next(
        index
        for index, program in enumerate(candidate.programs)
        if "op:part-001:pocket" in program.operation_ids
    )
    program = candidate.programs[program_index]
    cut_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == "op:part-001:pocket"
        and move.pass_index == 1
        and move.kind == ProductionMoveKind.LINEAR
    )
    recipe = next(
        item
        for item in candidate.execution_context.recipes
        if item.operation_kind == OperationKind.POCKET
    )
    moves = list(program.moves)
    for index in (cut_index - 2, cut_index - 1):
        moves[index] = replace(moves[index], x_um=50_000, y_um=300_000)
    moves[cut_index] = replace(moves[cut_index], feed_um_min=recipe.feed_um_min)
    mutated = _replace_program(candidate, program_index, replace(program, moves=tuple(moves)))

    codes = _issue_codes(mutated, source)
    assert codes == {"MATERIAL_REMOVAL_SEGMENT_INVALID"}


def test_accuracy_expanded_cut_must_stay_inside_its_own_finished_outline() -> None:
    source, candidate = _candidate()
    operations = list(source.operations)
    operations[1] = replace(operations[1], x_um=22_500)
    edge_tangent_source = replace(source, operations=tuple(operations))
    program = candidate.programs[0]
    moves = tuple(
        replace(move, x_um=22_500) if move.operation_id == "op:part-001:drill-a" else move
        for move in program.moves
    )
    candidate = _replace_program(candidate, 0, replace(program, moves=moves))
    candidate = replace(
        candidate,
        operations_sha256=sha256_hex(edge_tangent_source.to_json()),
    )

    codes = _issue_codes(candidate, edge_tangent_source)
    assert codes == {"OWN_PART_CUTTER_BREAKOUT"}


def test_exactly_declared_open_edge_allows_only_its_bound_cutter_sweep() -> None:
    source = _open_edge_groove_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))

    report = verify_production_toolpaths(candidate, source).report

    assert report.status == CuttingProgramStatus.PASS
    assert report.issue_count == 0


def test_rotated_source_open_edge_maps_to_the_exact_physical_boundary() -> None:
    source = _rotated_open_edge_groove_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))

    report = verify_production_toolpaths(candidate, source).report

    assert report.status == CuttingProgramStatus.PASS
    assert report.issue_count == 0


def test_xy_rapid_below_safe_z_blocks() -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    move_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == "op:part-001:drill-b" and move.role == ProductionMoveRole.POSITION
    )
    moves = list(program.moves)
    moves[move_index] = replace(moves[move_index], z_um=2_000)
    mutated = _replace_program(candidate, 0, replace(program, moves=tuple(moves)))

    assert "RAPID_XY_BELOW_SAFE_Z" in _issue_codes(mutated, source)


def test_program_entry_must_be_exact_safe_absolute_return_target() -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    moves = list(program.moves)
    moves[0] = replace(moves[0], z_um=moves[0].z_um - 1)
    mutated = _replace_program(candidate, 0, replace(program, moves=tuple(moves)))

    assert "PROGRAM_ENTRY_INVALID" in _issue_codes(mutated, source)


def test_tool_assembly_keepout_collision_blocks() -> None:
    source, candidate = _candidate()
    setup = candidate.execution_context.setups[0]
    blocked_setup = replace(
        setup,
        keep_out_zones=(Rect(45_000, 45_000, 10_000, 10_000),),
    )
    context = replace(candidate.execution_context, setups=(blocked_setup,))
    mutated = replace(
        candidate,
        execution_context=context,
        machine_profile_fingerprint=context.machine_profile_fingerprint,
        tool_catalog_fingerprint=context.tool_catalog_fingerprint,
        recipe_catalog_fingerprint=context.recipe_catalog_fingerprint,
    )

    codes = _issue_codes(mutated, source)
    assert "KEEP_OUT_COLLISION" in codes
    assert "TOOL_ASSEMBLY_KEEP_OUT_COLLISION" in codes


def test_excessive_pass_depth_blocks() -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    move_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == "op:part-001:drill-a" and move.kind == ProductionMoveKind.LINEAR
    )
    moves = list(program.moves)
    moves[move_index] = replace(moves[move_index], z_um=-9_000)
    mutated = _replace_program(candidate, 0, replace(program, moves=tuple(moves)))

    assert "CUT_DEPTH_INVALID" in _issue_codes(mutated, source)


def test_missing_operation_moves_block() -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    remaining = tuple(move for move in program.moves if move.operation_id != "op:part-001:drill-b")
    mutated_program = replace(program)
    object.__setattr__(mutated_program, "moves", _resequence(remaining))
    mutated = _replace_program(candidate, 0, mutated_program)

    assert "OPERATION_MOVE_COVERAGE_INVALID" in _issue_codes(mutated, source)


def test_wrong_operation_block_order_blocks() -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    first_id, second_id = program.operation_ids
    reordered = tuple(move for move in program.moves if move.operation_id == second_id) + tuple(
        move for move in program.moves if move.operation_id == first_id
    )
    mutated = _replace_program(candidate, 0, replace(program, moves=_resequence(reordered)))

    assert "MOVE_OPERATION_ORDER_INVALID" in _issue_codes(mutated, source)


def test_physical_authority_tampering_blocks_but_report_never_authorizes() -> None:
    source, candidate = _candidate()
    object.__setattr__(candidate, "physical_cutting_authorized", True)
    object.__setattr__(candidate, "workshop_acceptance_required", False)

    report = verify_production_toolpaths(candidate, source).report

    assert report.status == CuttingProgramStatus.BLOCK
    assert {"PHYSICAL_AUTHORITY_INVALID", "WORKSHOP_ACCEPTANCE_BYPASSED"} <= {
        issue.code for issue in report.issues
    }
    assert report.physical_cutting_authorized is False
    assert report.workshop_acceptance_required is True


def test_contour_diagonal_with_valid_boundary_endpoints_blocks() -> None:
    source, candidate = _candidate()
    operation = source.operations[0]
    contour_index = next(
        index for index, program in enumerate(candidate.programs) if program.release_operation_ids
    )
    program = candidate.programs[contour_index]
    pass_cut_indexes = [
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == operation.operation_id
        and move.pass_index == 1
        and move.kind == ProductionMoveKind.LINEAR
    ]
    assert operation.width_um is not None
    assert operation.length_um is not None
    radius_um = _source_tools()[1].effective_diameter_um // 2
    moves = list(program.moves)
    moves[pass_cut_indexes[1]] = replace(
        moves[pass_cut_indexes[1]],
        x_um=operation.x_um + operation.width_um,
        y_um=operation.y_um + operation.length_um + radius_um,
    )
    mutated = _replace_program(
        candidate,
        contour_index,
        replace(program, moves=tuple(moves)),
    )

    codes = _issue_codes(mutated, source)
    assert "MATERIAL_REMOVAL_COVERAGE_INVALID" in codes
    assert "OPERATION_GEOMETRY_MISMATCH" not in codes


def test_contour_cannot_hide_a_missing_cut_behind_safe_rapid_repositioning() -> None:
    source, candidate = _candidate()
    contour_index = next(
        index for index, program in enumerate(candidate.programs) if program.release_operation_ids
    )
    program = candidate.programs[contour_index]
    setup = candidate.execution_context.setups[0]
    recipe = next(
        item
        for item in candidate.execution_context.recipes
        if item.operation_kind == OperationKind.CONTOUR
    )
    cutting_indexes = [
        index
        for index, move in enumerate(program.moves)
        if move.pass_index == 1 and move.kind == ProductionMoveKind.LINEAR
    ]
    destination_index = cutting_indexes[2]
    start = program.moves[destination_index - 1]
    destination = program.moves[destination_index]
    interruption = (
        replace(
            destination,
            kind=ProductionMoveKind.RAPID,
            role=ProductionMoveRole.RETRACT,
            x_um=start.x_um,
            y_um=start.y_um,
            z_um=setup.safe_z_um,
            feed_um_min=None,
        ),
        replace(
            destination,
            kind=ProductionMoveKind.RAPID,
            role=ProductionMoveRole.POSITION,
            z_um=setup.safe_z_um,
            feed_um_min=None,
        ),
        replace(
            destination,
            kind=ProductionMoveKind.RAPID,
            role=ProductionMoveRole.APPROACH,
            z_um=recipe.approach_clearance_um,
            feed_um_min=None,
        ),
    )
    moves = program.moves[:destination_index] + interruption + program.moves[destination_index:]
    mutated = _replace_program(
        candidate,
        contour_index,
        replace(program, moves=_resequence(moves)),
    )

    assert "MATERIAL_REMOVAL_COVERAGE_INVALID" in _issue_codes(mutated, source)


def test_shifted_tab_bridge_blocks_even_when_contour_remains_closed() -> None:
    source, candidate = _candidate()
    contour_index = next(
        index for index, program in enumerate(candidate.programs) if program.release_operation_ids
    )
    program = candidate.programs[contour_index]
    deepest_pass = max(move.pass_index for move in program.moves)
    bridge_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.pass_index == deepest_pass and move.role == ProductionMoveRole.TAB_BRIDGE
    )
    moves = list(program.moves)
    for index in (bridge_index - 2, bridge_index - 1, bridge_index, bridge_index + 1):
        moves[index] = replace(moves[index], x_um=moves[index].x_um + 1_000)
    mutated = _replace_program(
        candidate,
        contour_index,
        replace(program, moves=tuple(moves)),
    )

    assert "TAB_CONTRACT_INVALID" in _issue_codes(mutated, source)


@pytest.mark.parametrize("tab_width_um", (1, 100))
def test_tab_width_uncertainty_is_independently_blocked(tab_width_um: int) -> None:
    source, candidate = _candidate()
    contour_recipe = next(
        recipe
        for recipe in candidate.execution_context.recipes
        if recipe.operation_kind == OperationKind.CONTOUR
    )
    object.__setattr__(
        contour_recipe,
        "tab_width_um",
        tab_width_um,
    )

    verification = verify_production_toolpaths(candidate, source)

    assert any(
        issue.code == "TAB_CONTRACT_INVALID"
        and issue.message == "recipe uncertainty consumes the holding-tab width"
        for issue in verification.report.issues
    )


def test_raster_stepover_uncertainty_is_independently_blocked() -> None:
    source = _pocket_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))
    pocket_recipe = next(
        recipe
        for recipe in candidate.execution_context.recipes
        if recipe.operation_kind == OperationKind.POCKET
    )
    object.__setattr__(pocket_recipe, "stepover_ppm", 1_000_000)

    verification = verify_production_toolpaths(candidate, source)

    assert any(
        issue.code == "STEPOVER_COVERAGE_INVALID"
        and issue.message == "recipe stepover exceeds process-accuracy-adjusted cutter coverage"
        for issue in verification.report.issues
    )


def test_part_and_instance_provenance_is_bound_to_finished_outline() -> None:
    source, candidate = _candidate()
    operations = list(source.operations)
    operations[1] = replace(operations[1], part_id="different-part")
    tampered_source = replace(source, operations=tuple(operations))

    assert "PART_INSTANCE_BINDING_INVALID" in _issue_codes(candidate, tampered_source)


def test_each_instance_requires_exactly_one_finished_outline() -> None:
    source, candidate = _candidate()
    duplicate = replace(
        source.operations[0],
        operation_id="op:part-001:outer-copy",
        feature_id="outer-copy",
    )
    tampered_source = replace(source, operations=(*source.operations, duplicate))

    assert "PART_OUTLINE_BINDING_INVALID" in _issue_codes(candidate, tampered_source)


def test_b_side_coordinates_are_unflipped_into_physical_outline_frame() -> None:
    source = _two_sided_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))

    report = verify_production_toolpaths(candidate, source).report

    assert report.status == CuttingProgramStatus.PASS
    assert tuple(program.setup_id for program in candidate.programs) == (
        "setup:sheet:001:B",
        "setup:sheet:001:A",
    )


def test_accuracy_expanded_segment_capsule_blocks_inter_part_contact() -> None:
    source = _two_instance_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))
    program = candidate.programs[0]
    move_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == "op:part-001:drill-a" and move.kind == ProductionMoveKind.LINEAR
    )
    moves = list(program.moves)
    moves[move_index] = replace(moves[move_index], x_um=297_450)
    mutated = _replace_program(candidate, 0, replace(program, moves=tuple(moves)))

    assert "INTER_PART_CUTTER_COLLISION" in _issue_codes(mutated, source)


def test_missing_dogbone_relief_cycle_blocks_exact_material_coverage() -> None:
    source = _dogbone_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))
    program_index = next(
        index
        for index, program in enumerate(candidate.programs)
        if "op:part-001:groove" in program.operation_ids
    )
    program = candidate.programs[program_index]
    position_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == "op:part-001:groove"
        and move.pass_index == 1
        and move.role == ProductionMoveRole.POSITION
        and (move.x_um, move.y_um) == (150_000, 100_000)
    )
    moves = program.moves[:position_index] + program.moves[position_index + 4 :]
    mutated = _replace_program(
        candidate,
        program_index,
        replace(program, moves=_resequence(moves)),
    )

    assert "MATERIAL_REMOVAL_COVERAGE_INVALID" in _issue_codes(mutated, source)


def test_absolute_wcs_z_bounds_include_the_bound_stock_top_offset() -> None:
    source, candidate = _candidate()
    setup = candidate.execution_context.setups[0]
    object.__setattr__(setup, "machine_wcs_z0_um", 170_000)

    codes = _issue_codes(candidate, source)
    assert "SETUP_Z_LIMIT" in codes
    assert "MOVE_Z_LIMIT" in codes


def test_depth_uncertainty_preserves_holder_and_spoilboard_clearances() -> None:
    source, holder_candidate = _candidate()
    mill_binding = next(
        binding
        for binding in holder_candidate.execution_context.tool_bindings
        if binding.source_tool_id == "T06R"
    )
    object.__setattr__(mill_binding, "minimum_holder_clearance_um", 17_000)
    assert "TOOL_HOLDER_CLEARANCE_EXCEEDED" in _issue_codes(holder_candidate, source)

    source, spoilboard_candidate = _candidate()
    setup = spoilboard_candidate.execution_context.setups[0]
    object.__setattr__(setup, "through_cut_allowance_um", 400)
    assert "THROUGH_CUT_ALLOWANCE_EXCEEDED" in _issue_codes(
        spoilboard_candidate,
        source,
    )


def test_through_overtravel_must_exceed_process_uncertainty() -> None:
    source, candidate = _candidate()
    contour_recipe = next(
        recipe
        for recipe in candidate.execution_context.recipes
        if recipe.operation_kind == OperationKind.CONTOUR
    )
    object.__setattr__(contour_recipe, "through_overtravel_um", 50)

    assert "THROUGH_CUT_UNCERTAINTY_INVALID" in _issue_codes(candidate, source)


def test_through_drill_is_blocked_without_tip_breakthrough_geometry() -> None:
    source, candidate = _candidate()
    operations = list(source.operations)
    operations[1] = replace(
        operations[1],
        through=True,
        depth_um=source.setups[0].stock_thickness_um,
    )
    through_drill_source = replace(source, operations=tuple(operations))

    assert "THROUGH_DRILL_UNSUPPORTED" in _issue_codes(
        candidate,
        through_drill_source,
    )


def test_nonzero_blind_drill_point_length_is_independently_blocked() -> None:
    source, candidate = _candidate()
    drill_binding = next(
        binding
        for binding in candidate.execution_context.tool_bindings
        if binding.source_tool_id == "T05"
    )
    object.__setattr__(drill_binding, "drill_point_length_um", 1)

    codes = _issue_codes(candidate, source)
    assert "DRILL_POINT_GEOMETRY_INVALID" in codes
    assert "OPERATION_CONTRACT_INVALID" in codes


def test_drill_diameter_mismatch_and_process_uncertainty_share_one_budget() -> None:
    source, candidate = _candidate()
    drill_recipe = next(
        recipe
        for recipe in candidate.execution_context.recipes
        if recipe.operation_kind == OperationKind.DRILL
    )
    drill_binding = next(
        binding
        for binding in candidate.execution_context.tool_bindings
        if binding.source_tool_id == "T05"
    )
    object.__setattr__(drill_recipe, "diameter_tolerance_um", 200)
    object.__setattr__(drill_binding, "effective_diameter_um", 5_152)

    assert "DRILL_DIAMETER_BUDGET_INVALID" in _issue_codes(candidate, source)


def test_cutter_sweep_uncertainty_cannot_leave_stock() -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    move_index = next(
        index
        for index, move in enumerate(program.moves)
        if move.operation_id == "op:part-001:drill-a" and move.kind == ProductionMoveKind.LINEAR
    )
    moves = list(program.moves)
    moves[move_index] = replace(moves[move_index], x_um=2_450)
    mutated = _replace_program(candidate, 0, replace(program, moves=tuple(moves)))

    assert "CUTTER_SWEEP_OUTSIDE_STOCK" in _issue_codes(mutated, source)


def test_setup_spoilboard_rapid_margin_and_contour_accuracy_are_fail_closed() -> None:
    source, spoilboard_candidate = _candidate()
    object.__setattr__(
        spoilboard_candidate.execution_context.setups[0],
        "spoilboard_sha256",
        "not-a-sha256",
    )
    assert "SPOILBOARD_BINDING_INVALID" in _issue_codes(
        spoilboard_candidate,
        source,
    )

    source, rapid_candidate = _candidate()
    object.__setattr__(
        rapid_candidate.execution_context.setups[0],
        "minimum_rapid_clearance_um",
        50,
    )
    assert "RECIPE_CLEARANCE_INVALID" in _issue_codes(rapid_candidate, source)

    source, interpolation_candidate = _candidate()
    contour_recipe = next(
        recipe
        for recipe in interpolation_candidate.execution_context.recipes
        if recipe.operation_kind == OperationKind.CONTOUR
    )
    object.__setattr__(contour_recipe, "process_accuracy_um", 15)
    assert "CONTOUR_INTERPOLATION_ACCURACY_INVALID" in _issue_codes(
        interpolation_candidate,
        source,
    )


def test_tool_table_snapshot_offsets_and_wcs_rotation_are_exactly_bound() -> None:
    source, offset_candidate = _candidate()
    object.__setattr__(
        offset_candidate.execution_context.tool_bindings[0],
        "expected_length_offset_x_um",
        1,
    )
    assert "TOOL_TABLE_BINDING_INVALID" in _issue_codes(offset_candidate, source)

    source, snapshot_candidate = _candidate()
    object.__setattr__(
        snapshot_candidate.execution_context.tool_bindings[1],
        "tool_table_evidence_sha256",
        "b" * 64,
    )
    assert "TOOL_TABLE_BINDING_INVALID" in _issue_codes(snapshot_candidate, source)

    source, rotation_candidate = _candidate()
    object.__setattr__(
        rotation_candidate.execution_context.setups[0],
        "machine_wcs_xy_rotation_mdeg",
        1,
    )
    assert "WCS_ROTATION_INVALID" in _issue_codes(rotation_candidate, source)


def test_g43_length_offset_is_applied_to_absolute_machine_move_bounds() -> None:
    source, candidate = _candidate()
    mill_binding = next(
        binding
        for binding in candidate.execution_context.tool_bindings
        if binding.source_tool_id == "T06R"
    )
    object.__setattr__(mill_binding, "expected_length_offset_z_um", 140_000)

    codes = _issue_codes(candidate, source)
    assert "MOVE_Z_LIMIT" in codes
    assert "SETUP_Z_LIMIT" in codes

    source, entry_candidate = _candidate()
    drill_binding = next(
        binding
        for binding in entry_candidate.execution_context.tool_bindings
        if binding.source_tool_id == "T05"
    )
    object.__setattr__(drill_binding, "expected_length_offset_z_um", 131_000)
    assert "PROGRAM_ENTRY_INVALID" in _issue_codes(entry_candidate, source)


def test_release_program_is_terminal_for_the_physical_sheet() -> None:
    source, candidate = _candidate()
    non_release, release = candidate.programs
    object.__setattr__(
        candidate,
        "programs",
        (
            replace(release, run_order=1),
            replace(non_release, run_order=2),
        ),
    )

    assert "RELEASE_ORDER_INVALID" in _issue_codes(candidate, source)


def test_backplot_escapes_untrusted_metadata_and_contains_no_active_content() -> None:
    source, candidate = _candidate()
    object.__setattr__(
        candidate.programs[0],
        "program_id",
        '"><script>alert(1)</script>\x00',
    )

    svg = cutting_backplot_svg(candidate, source)

    assert b"<script" not in svg
    assert b"href=" not in svg
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in svg
    assert b"\x00" not in svg
    assert b"\xef\xbf\xbd" in svg


@pytest.mark.parametrize(
    ("target", "field", "value", "expected_code"),
    (
        ("document", "design_hash", "0" * 64, "DOCUMENT_BINDING_MISMATCH"),
        ("document", "mode", "VALIDATION_ONLY", "CANDIDATE_MODE_INVALID"),
        (
            "document",
            "machine_profile_fingerprint",
            "0" * 64,
            "DOCUMENT_BINDING_MISMATCH",
        ),
        (
            "context",
            "source_machine_profile_version",
            "tampered-version",
            "SOURCE_MACHINE_BINDING_INVALID",
        ),
        ("context", "work_width_um", 2_499_999, "MACHINE_ABSOLUTE_BOUNDS_INVALID"),
        ("context", "machine_x_min_um", 1, "MACHINE_ABSOLUTE_BOUNDS_INVALID"),
        ("binding", "effective_diameter_um", 5_099, "TOOL_BINDING_INVALID"),
        ("binding", "cutting_length_um", 7_000, "TOOL_CUTTING_LENGTH_EXCEEDED"),
        ("binding", "source_tool_version", "tampered", "SOURCE_TOOL_BINDING_INVALID"),
        ("binding", "controller_tool_number", 0, "TOOL_TABLE_BINDING_INVALID"),
        ("recipe", "machine_profile_version", "tampered", "RECIPE_MACHINE_LIMIT_INVALID"),
        ("recipe", "approach_clearance_um", 20_000, "RECIPE_CLEARANCE_INVALID"),
        ("recipe", "process_accuracy_um", 8_000, "RECIPE_MACHINE_LIMIT_INVALID"),
        ("setup", "stock_width_um", 599_999, "SETUP_BINDING_INVALID"),
        ("setup", "source_setup_sha256", "0" * 64, "SETUP_BINDING_INVALID"),
        ("setup", "orientation", "TAMPERED", "SETUP_BINDING_INVALID"),
        ("setup", "source_to_wcs_xy_transform", "TAMPERED", "SETUP_BINDING_INVALID"),
        ("setup", "fixture_id", "", "FIXTURE_BINDING_INVALID"),
        ("setup", "reference_surface", "MACHINE_BED_Z0", "SETUP_Z_REFERENCE_INVALID"),
        ("setup", "fixture_clearance_z_um", 19_000, "SETUP_Z_LIMIT"),
    ),
)
def test_tampered_execution_bindings_fail_closed(
    target: str,
    field: str,
    value: object,
    expected_code: str,
) -> None:
    source, candidate = _candidate()
    subject: object
    if target == "document":
        subject = candidate
    elif target == "context":
        subject = candidate.execution_context
    elif target == "binding":
        subject = candidate.execution_context.tool_bindings[0]
    elif target == "recipe":
        subject = candidate.execution_context.recipes[0]
    else:
        subject = candidate.execution_context.setups[0]
    object.__setattr__(subject, field, value)

    assert expected_code in _issue_codes(candidate, source)


def test_duplicate_catalog_claims_and_controller_slots_fail_closed() -> None:
    source, candidate = _candidate()
    first, second = candidate.execution_context.tool_bindings
    object.__setattr__(second, "controller_tool_number", first.controller_tool_number)
    object.__setattr__(second, "length_offset_number", first.length_offset_number)
    duplicate_recipe = replace(
        candidate.execution_context.recipes[0],
        recipe_id="duplicate-material-tool-operation-recipe",
    )
    object.__setattr__(
        candidate.execution_context,
        "recipes",
        (*candidate.execution_context.recipes, duplicate_recipe),
    )

    codes = _issue_codes(candidate, source)
    assert {"TOOL_BINDING_DUPLICATE", "RECIPE_DUPLICATE"} <= codes


def test_duplicate_document_identities_fail_closed() -> None:
    source, candidate = _candidate()
    object.__setattr__(
        candidate.execution_context,
        "setups",
        (*candidate.execution_context.setups, candidate.execution_context.setups[0]),
    )
    object.__setattr__(
        candidate.execution_context,
        "tool_bindings",
        (*candidate.execution_context.tool_bindings, candidate.execution_context.tool_bindings[0]),
    )
    object.__setattr__(
        candidate.execution_context,
        "recipes",
        (*candidate.execution_context.recipes, candidate.execution_context.recipes[0]),
    )

    assert {
        "BOUND_SETUP_DUPLICATE",
        "TOOL_BINDING_DUPLICATE",
        "SOURCE_TOOL_BINDING_DUPLICATE",
        "RECIPE_DUPLICATE",
    } <= _issue_codes(candidate, source)


@pytest.mark.parametrize(
    ("mutation", "expected_codes"),
    (
        ("run-order", {"PROGRAM_ORDER_INVALID"}),
        ("duplicate-program-id", {"PROGRAM_ORDER_INVALID"}),
        ("unknown-setup", {"PROGRAM_SETUP_UNKNOWN"}),
        ("unknown-tool", {"PROGRAM_TOOL_UNKNOWN"}),
        ("unknown-operation", {"PROGRAM_OPERATION_UNKNOWN"}),
        ("release-inventory", {"RELEASE_ORDER_INVALID"}),
        ("release-not-terminal", {"RELEASE_ORDER_INVALID"}),
    ),
)
def test_program_manifest_tampering_fails_closed(
    mutation: str,
    expected_codes: set[str],
) -> None:
    source, candidate = _candidate()
    programs = list(candidate.programs)
    if mutation == "run-order":
        programs[0] = replace(programs[0], run_order=9)
    elif mutation == "duplicate-program-id":
        programs[1] = replace(programs[1], program_id=programs[0].program_id)
    elif mutation == "unknown-setup":
        programs[0] = replace(programs[0], setup_id="setup:unknown")
    elif mutation == "unknown-tool":
        programs[0] = replace(programs[0], tool_id="SHOP-UNKNOWN")
    elif mutation == "unknown-operation":
        programs[0] = replace(programs[0])
        object.__setattr__(
            programs[0],
            "operation_ids",
            (*programs[0].operation_ids, "op:unknown"),
        )
    elif mutation == "release-inventory":
        programs[1] = replace(programs[1], release_operation_ids=())
    else:
        programs[1] = replace(programs[1])
        object.__setattr__(
            programs[1],
            "operation_ids",
            (programs[1].operation_ids[0], programs[0].operation_ids[0]),
        )
    object.__setattr__(candidate, "programs", tuple(programs))

    assert expected_codes <= _issue_codes(candidate, source)


@pytest.mark.parametrize(
    ("move_index", "changes", "expected_code"),
    (
        (0, {"sequence": 2}, "MOVE_SEQUENCE_INVALID"),
        (0, {"kind": ProductionMoveKind.LINEAR}, "OPERATION_SEQUENCE_INVALID"),
        (1, {"z_um": -1}, "RAPID_BELOW_STOCK_TOP"),
        (2, {"role": ProductionMoveRole.APPROACH}, "MOVE_ROLE_INVALID"),
        (3, {"role": ProductionMoveRole.POSITION}, "OPERATION_SEQUENCE_INVALID"),
        (3, {"z_um": 1_999}, "OPERATION_SEQUENCE_INVALID"),
        (7, {"role": ProductionMoveRole.POSITION}, "OPERATION_SEQUENCE_INVALID"),
        (8, {"x_um": -1}, "MOVE_OUTSIDE_STOCK"),
    ),
)
def test_motion_state_machine_tampering_fails_closed(
    move_index: int,
    changes: dict[str, object],
    expected_code: str,
) -> None:
    source, candidate = _candidate()
    program = candidate.programs[0]
    moves = list(program.moves)
    moves[move_index] = replace(moves[move_index])
    for field, value in changes.items():
        object.__setattr__(moves[move_index], field, value)
    mutated_program = replace(program)
    object.__setattr__(mutated_program, "moves", tuple(moves))
    mutated = _replace_program(candidate, 0, mutated_program)

    assert expected_code in _issue_codes(mutated, source)


def test_source_and_bound_inventory_gaps_fail_closed() -> None:
    source, candidate = _candidate()
    object.__setattr__(source, "tools", source.tools[:1])
    object.__setattr__(candidate.execution_context, "tool_bindings", ())
    object.__setattr__(candidate.execution_context, "setups", ())

    codes = _issue_codes(candidate, source)
    assert {
        "SOURCE_TOOL_COVERAGE_INVALID",
        "SOURCE_TOOL_BINDING_INVALID",
        "SETUP_BINDING_INVALID",
        "PROGRAM_SETUP_UNKNOWN",
    } <= codes


def test_part_outline_overlap_and_operation_escape_fail_closed() -> None:
    source = _two_instance_source_document()
    candidate = generate_production_toolpaths(source, _execution_context(source))
    operations = list(source.operations)
    operations[-1] = replace(operations[-1], x_um=200_000, cutter_envelope_x_um=200_000)
    operations[1] = replace(operations[1], x_um=450_000)
    tampered = replace(source, operations=tuple(operations))

    codes = _issue_codes(candidate, tampered)
    assert {"PART_OUTLINE_COLLISION", "PART_OPERATION_OUTSIDE_OUTLINE"} <= codes


@pytest.mark.parametrize(
    ("start", "end", "radius_um", "rectangle", "expected"),
    (
        ((-5, 5), (15, 5), 0, Rect(0, 0, 10, 10), True),
        ((-5, -5), (-5, -5), 8, Rect(0, 0, 10, 10), True),
        ((-5, 12), (15, 12), 2, Rect(0, 0, 10, 10), True),
        ((-5, 13), (15, 13), 2, Rect(0, 0, 10, 10), False),
    ),
)
def test_segment_capsule_contact_is_conservative_at_edges_and_corners(
    start: tuple[int, int],
    end: tuple[int, int],
    radius_um: int,
    rectangle: Rect,
    expected: bool,
) -> None:
    assert (
        verification_module._segment_capsule_touches_rect(start, end, radius_um, rectangle)
        is expected
    )


@pytest.mark.parametrize(
    ("first_start", "first_end", "second_start", "second_end"),
    (
        ((0, 0), (10, 0), (5, 0), (15, 0)),
        ((0, 0), (10, 0), (10, 0), (10, 10)),
        ((5, -5), (5, 5), (0, 0), (10, 0)),
        ((0, 0), (10, 10), (0, 10), (10, 0)),
    ),
)
def test_segment_intersection_accepts_collinear_endpoints_and_crossings(
    first_start: tuple[int, int],
    first_end: tuple[int, int],
    second_start: tuple[int, int],
    second_end: tuple[int, int],
) -> None:
    assert verification_module._segments_touch(
        first_start,
        first_end,
        second_start,
        second_end,
    )


def test_corner_chord_guards_reject_wrong_quadrant_radius_and_oversized_step() -> None:
    common = {
        "centre": (0, 0),
        "radius_um": 10_000,
        "x_sign": 1,
        "y_sign": 1,
        "x_direction": -1,
        "y_direction": 1,
        "process_accuracy_um": 100,
    }

    assert not verification_module._corner_chord_is_valid((-1, 10_000), (0, 10_000), **common)
    assert not verification_module._corner_chord_is_valid(
        (10_000, 0),
        (9_000, 1_000),
        **common,
    )
    assert not verification_module._corner_chord_is_valid(
        (10_000, 0),
        (0, 10_000),
        **common,
    )


def test_inside_contour_and_degenerate_depth_helpers_are_fail_closed() -> None:
    assert (
        verification_module._inside_contour_segment_allowed(
            (5, 0),
            (5, 10),
            left=0,
            right=10,
            bottom=0,
            top=10,
        )
        is False
    )
    assert verification_module._depth_levels(0, 1) == ()
    assert verification_module._depth_levels(1, 0) == ()
    assert verification_module._canonical_contour_interpolation_error_um(0) == 0
