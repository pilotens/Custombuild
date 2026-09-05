from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
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
from custombuild_manufacturing import OperationKind, Point2D, Side
from custombuild_postprocessors import (
    CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY,
    EXTERNAL_AXIS_OFFSET_POLICY,
    FEED_SPINDLE_OVERRIDE_POLICY,
    G52_G92_OFFSET_RESET_POLICY,
    G53_TOOL_CHANGE_PATH_COMPLETE,
    M6_WCS_TABLE_POLICY,
    METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
    PROGRAM_RESTART_POLICY,
    GCodeSafetyError,
    LinuxCNCProductionMachineProfile,
    LinuxCNCProductionPostprocessor,
    LinuxCNCWCSOffset,
    ProductionMachineProgram,
    parse_production_program,
    production_parser,
    validate_production_program,
)


def production_machine_profile() -> LinuxCNCProductionMachineProfile:
    return LinuxCNCProductionMachineProfile(
        profile_id="shop-router-01-linuxcnc",
        version="1.0.0",
        machine_profile_id="shop-router-01",
        machine_profile_version="1.0.0",
        controller_id="LinuxCNC",
        controller_version="2.9.4",
        supported_wcs=("G54", "G55"),
        wcs_offsets=(
            LinuxCNCWCSOffset("G54", 0, 0, -60_000, 0),
            LinuxCNCWCSOffset("G55", 0, 700_000, -60_000, 0),
        ),
        machine_x_min_um=0,
        machine_x_max_um=1_300_000,
        machine_y_min_um=0,
        machine_y_max_um=2_500_000,
        machine_z_min_um=-100_000,
        machine_z_max_um=0,
        tool_change_x_um=100_000,
        tool_change_y_um=2_400_000,
        tool_change_z_um=-5_000,
        spindle_spinup_ms=2_500,
        g53_tool_change_path=G53_TOOL_CHANGE_PATH_COMPLETE,
        g53_tool_change_path_clearance_evidence_id="shop-clearance-aircut",
        g53_tool_change_path_clearance_evidence_version="1.0.0",
        g53_tool_change_path_clearance_evidence_sha256="4" * 64,
        wcs_offsets_evidence_id="shop-wcs-probe-report",
        wcs_offsets_evidence_version="1.0.0",
        wcs_offsets_evidence_sha256="5" * 64,
        g52_g92_offset_reset_policy=G52_G92_OFFSET_RESET_POLICY,
        g52_g92_offset_reset_evidence_id="shop-offset-reset-validation",
        g52_g92_offset_reset_evidence_version="1.0.0",
        g52_g92_offset_reset_evidence_sha256="6" * 64,
        feed_spindle_override_policy=FEED_SPINDLE_OVERRIDE_POLICY,
        feed_spindle_override_evidence_id="shop-disabled-overrides",
        feed_spindle_override_evidence_version="1.0.0",
        feed_spindle_override_evidence_sha256="7" * 64,
        external_axis_offset_policy=EXTERNAL_AXIS_OFFSET_POLICY,
        external_axis_offset_evidence_id="shop-disabled-external-axis-offsets",
        external_axis_offset_evidence_version="1.0.0",
        external_axis_offset_evidence_sha256="8" * 64,
        homing_preflight_policy="NO_FORCE_HOMING_0_ALL_XYZ_HOMED_BEFORE_AUTO",
        homing_preflight_evidence_id="shop-homing-interlock",
        homing_preflight_evidence_version="1.0.0",
        homing_preflight_evidence_sha256="9" * 64,
        program_restart_policy=PROGRAM_RESTART_POLICY,
        program_restart_evidence_id="shop-program-restart-interlock",
        program_restart_evidence_version="1.0.0",
        program_restart_evidence_sha256="b" * 64,
        m6_tool_table_policy="M6_PRESERVES_EXACT_BOUND_TOOL_TABLE",
        m6_tool_table_evidence_id="shop-m6-tool-table-invariance",
        m6_tool_table_evidence_version="1.0.0",
        m6_tool_table_evidence_sha256="c" * 64,
        m6_wcs_table_policy=M6_WCS_TABLE_POLICY,
        m6_wcs_table_evidence_id="shop-m6-wcs-table-invariance",
        m6_wcs_table_evidence_version="1.0.0",
        m6_wcs_table_evidence_sha256="d" * 64,
        metric_xyz_identity_kinematics_policy=METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
        metric_xyz_identity_kinematics_evidence_id="shop-metric-xyz-kinematics",
        metric_xyz_identity_kinematics_evidence_version="1.0.0",
        metric_xyz_identity_kinematics_evidence_sha256="f" * 64,
        spindle_at_speed_policy="ACTUAL_RPM_GT_0_WITHIN_TOLERANCE_BEFORE_FEED",
        spindle_feedback_source="spindle-encoder-rpm",
        spindle_at_speed_evidence_id="shop-spindle-at-speed-interlock",
        spindle_at_speed_evidence_version="1.0.0",
        spindle_at_speed_evidence_sha256="a" * 64,
        spindle_at_speed_tolerance_ppm=50_000,
        continuous_spindle_speed_interlock_policy=(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY),
        continuous_spindle_speed_interlock_evidence_id="shop-continuous-spindle-interlock",
        continuous_spindle_speed_interlock_evidence_version="1.0.0",
        continuous_spindle_speed_interlock_evidence_sha256="e" * 64,
        g53_machine_coordinates_verified=True,
        g53_tool_change_path_clearance_verified=True,
        wcs_offsets_verified=True,
        g92_1_clears_g52_g92_offsets_verified=True,
        m6_tool_change_verified=True,
        m6_preserves_axis_position=True,
        m6_preserves_bound_tool_table_verified=True,
        m6_preserves_bound_wcs_table_verified=True,
        linear_units_mm_verified=True,
        coordinates_xyz_verified=True,
        identity_trivkins_verified=True,
        exactly_three_joints_verified=True,
        joint_0_x_1_y_2_z_verified=True,
        no_extra_controlled_axes_verified=True,
        g43_h_length_offset_verified=True,
        g8_radius_mode_verified=True,
        g97_rpm_mode_verified=True,
        m9_coolant_off_verified=True,
        m49_feed_and_spindle_overrides_disabled_verified=True,
        m52_p0_adaptive_feed_disabled_verified=True,
        m53_p1_feed_hold_enabled_verified=True,
        external_xyz_offsets_disabled_verified=True,
        all_xyz_homed_before_auto_verified=True,
        no_force_homing_disabled_verified=True,
        run_from_line_disabled_verified=True,
        full_restart_after_abort_required=True,
        real_spindle_feedback_verified=True,
        spindle_at_speed_motion_interlock_verified=True,
        continuous_spindle_speed_feed_inhibit_verified=True,
        vfd_fault_motion_inhibit_verified=True,
        vfd_fault_spindle_stop_verified=True,
        m3_clockwise_spindle_verified=True,
        g4_p_seconds_dwell_verified=True,
    )


def production_document() -> ProductionToolpathDocument:
    setup = BoundSetup(
        setup_id="setup:sheet:001:A",
        stock_id="sheet",
        source_material_id="mdf",
        source_material_version="v1",
        material_id="mdf",
        material_version="v1",
        material_evidence_id="supplier-declaration-mdf",
        material_evidence_version="v1",
        material_evidence_sha256="9" * 64,
        sheet_index=0,
        side=Side.A,
        source_setup_sha256="0" * 64,
        source_to_wcs_xy_transform=IDENTITY_SOURCE_TO_WCS_XY,
        wcs="G54",
        machine_wcs_origin=Point2D(0, 0),
        machine_wcs_z0_um=-60_000,
        machine_wcs_xy_rotation_mdeg=0,
        stock_width_um=1_000_000,
        stock_height_um=600_000,
        stock_thickness_um=18_000,
        safe_z_um=15_000,
        reference_surface=STOCK_TOP_Z0_REFERENCE,
        orientation="A_SIDE_UP_LOWER_LEFT",
        fixture_id="vacuum-table-01",
        fixture_version="1.0.0",
        fixture_sha256="1" * 64,
        fixture_clearance_z_um=5_000,
        minimum_rapid_clearance_um=2_000,
        keep_out_policy=FIXTURE_KEEPOUT_POLICY,
        probe_method="TOUCH_PROBE_V1",
        keep_out_zones=(),
        spoilboard_id=None,
        spoilboard_version=None,
        spoilboard_sha256=None,
        through_cut_allowance_um=0,
    )
    tool = ProductionToolBinding(
        tool_id="T06R",
        tool_version="1.0.0",
        source_tool_id="validation-t06",
        source_tool_version="1.0.0",
        source_tool_sha256="2" * 64,
        controller_tool_number=7,
        length_offset_number=17,
        expected_length_offset_x_um=0,
        expected_length_offset_y_um=0,
        expected_length_offset_z_um=40_000,
        tool_table_evidence_id="shop-tool-table-snapshot",
        tool_table_evidence_version="1.0.0",
        tool_table_evidence_sha256="8" * 64,
        effective_diameter_um=6_000,
        cutting_length_um=30_000,
        measured_stickout_um=40_000,
        assembly_collision_radius_um=10_000,
        minimum_holder_clearance_um=5_000,
        geometry=ProductionToolGeometry.FLAT_END_MILL,
        center_cutting=True,
        drill_point_length_um=0,
    )
    recipe = CuttingRecipe(
        recipe_id="mdf-pocket-t06r",
        version="1.0.0",
        machine_profile_id="shop-router-01",
        machine_profile_version="1.0.0",
        material_id="mdf",
        material_version="v1",
        tool_id="T06R",
        tool_version="1.0.0",
        operation_kind=OperationKind.POCKET,
        spindle_rpm=18_000,
        feed_um_min=2_000_000,
        plunge_um_min=500_000,
        stepdown_um=3_000,
        stepover_ppm=400_000,
        peck_depth_um=2_000,
        approach_clearance_um=2_000,
        through_overtravel_um=0,
        tab_width_um=0,
        tab_height_um=0,
        process_accuracy_um=100,
        accepted_tolerance_um=500,
    )
    context = ProductionExecutionContext(
        source_machine_profile_id="validation-router-1325",
        source_machine_profile_version="1.0.0",
        source_machine_profile_fingerprint="3" * 64,
        machine_profile_id="shop-router-01",
        machine_profile_version="1.0.0",
        controller_id="LinuxCNC",
        controller_version="2.9.4",
        work_width_um=1_300_000,
        work_height_um=2_500_000,
        work_z_um=100_000,
        machine_x_min_um=0,
        machine_x_max_um=1_300_000,
        machine_y_min_um=0,
        machine_y_max_um=2_500_000,
        machine_z_min_um=-100_000,
        machine_z_max_um=0,
        min_spindle_rpm=6_000,
        max_spindle_rpm=24_000,
        max_feed_um_min=5_000_000,
        max_plunge_um_min=1_000_000,
        tool_catalog_version="shop-tools-1",
        recipe_catalog_version="shop-recipes-1",
        setups=(setup,),
        tool_bindings=(tool,),
        recipes=(recipe,),
    )
    moves = (
        ProductionMove(
            1,
            "op:panel:001:pocket",
            1,
            ProductionMoveKind.RAPID,
            ProductionMoveRole.POSITION,
            10_000,
            20_000,
            15_000,
        ),
        ProductionMove(
            2,
            "op:panel:001:pocket",
            1,
            ProductionMoveKind.RAPID,
            ProductionMoveRole.APPROACH,
            10_000,
            20_000,
            2_000,
        ),
        ProductionMove(
            3,
            "op:panel:001:pocket",
            1,
            ProductionMoveKind.LINEAR,
            ProductionMoveRole.CUT,
            10_000,
            20_000,
            -3_000,
            500_000,
        ),
        ProductionMove(
            4,
            "op:panel:001:pocket",
            1,
            ProductionMoveKind.LINEAR,
            ProductionMoveRole.CUT,
            50_000,
            20_000,
            -3_000,
            2_000_000,
        ),
        ProductionMove(
            5,
            "op:panel:001:pocket",
            1,
            ProductionMoveKind.RAPID,
            ProductionMoveRole.RETRACT,
            50_000,
            20_000,
            15_000,
        ),
    )
    program = ProductionProgram(
        program_id="program:001:setup:sheet:001:A:T06R",
        run_order=1,
        setup_id=setup.setup_id,
        tool_id=tool.tool_id,
        tool_version=tool.tool_version,
        recipe_ids=(recipe.recipe_id,),
        operation_ids=("op:panel:001:pocket",),
        release_operation_ids=(),
        moves=moves,
    )
    return ProductionToolpathDocument(
        design_hash="a" * 64,
        operations_sha256="b" * 64,
        execution_context=context,
        machine_profile_fingerprint=context.machine_profile_fingerprint,
        tool_catalog_fingerprint=context.tool_catalog_fingerprint,
        recipe_catalog_fingerprint=context.recipe_catalog_fingerprint,
        programs=(program,),
    )


def generated() -> tuple[ProductionToolpathDocument, ProductionMachineProgram]:
    document = production_document()
    return document, LinuxCNCProductionPostprocessor(production_machine_profile()).generate(
        document
    )[0]


def test_linuxcnc_interpreter_oracle_fixture_matches_generated_program_byte_for_byte() -> None:
    repo = Path(__file__).resolve().parents[2]
    oracle_fixture = repo / "tests" / "linuxcnc-oracle" / "production-output.ngc"
    _document, machine_program = generated()

    assert oracle_fixture.read_bytes() == machine_program.content


def _replace_occurrence(
    value: str,
    source: str,
    replacement: str,
    *,
    occurrence: int,
) -> str:
    start = 0
    for _ in range(occurrence):
        index = value.index(source, start)
        start = index + len(source)
    return value[:index] + replacement + value[index + len(source) :]


def test_production_postprocessor_emits_bound_candidate_and_round_trips_moves() -> None:
    document, machine_program = generated()
    planned = document.programs[0]

    parsed = validate_production_program(
        machine_program.content,
        document=document,
        program=planned,
        machine_profile=production_machine_profile(),
    )

    assert machine_program.mode == "EXECUTABLE_CAM_CANDIDATE"
    assert machine_program.machine_executable is True
    assert machine_program.physical_cutting_authorized is False
    assert machine_program.workshop_acceptance_required is True
    assert machine_program.postprocessor_id == "linuxcnc-3axis-production"
    assert machine_program.postprocessor_version == "1.1.0"
    assert machine_program.source_toolpaths_sha256 == document.fingerprint
    assert (
        machine_program.production_machine_profile_sha256
        == production_machine_profile().config_sha256
    )
    assert machine_program.filename.startswith("001.")
    assert machine_program.filename.endswith(".T06R.production.ngc")
    assert b"(SOURCE_MATERIAL_ID=mdf)" in machine_program.content
    assert b"(ACTUAL_MATERIAL_ID=mdf)" in machine_program.content
    assert b"(MATERIAL_EVIDENCE_SHA256=" + (b"9" * 64) + b")" in machine_program.content
    assert b"(STOCK_THICKNESS_UM=18000)" in machine_program.content
    assert parsed.wcs == "G54"
    assert parsed.controller_tool_number == 7
    assert parsed.length_offset_number == 17
    assert parsed.spindle_rpm == 18_000
    assert tuple(
        (move.kind, move.x_um, move.y_um, move.z_um, move.feed_um_min) for move in parsed.moves
    ) == tuple(
        (move.kind, move.x_um, move.y_um, move.z_um, move.feed_um_min) for move in planned.moves
    )


def test_production_program_is_byte_deterministic_and_has_canonical_safety_envelope() -> None:
    document = production_document()
    first = LinuxCNCProductionPostprocessor(production_machine_profile()).generate(document)[0]
    second = LinuxCNCProductionPostprocessor(production_machine_profile()).generate(document)[0]

    assert first.content == second.content
    assert first.sha256 == second.sha256
    lines = first.content.decode("ascii").splitlines()
    assert lines[:5] == [
        "%",
        "(CUSTOMBUILD LINUXCNC 3-AXIS PRODUCTION CANDIDATE)",
        "(CUSTOMBUILD_MACHINE_PROGRAM_MODE=EXECUTABLE_CAM_CANDIDATE)",
        "(PHYSICAL_CUTTING_AUTHORIZED=FALSE)",
        "(WORKSHOP_ACCEPTANCE_REQUIRED=TRUE)",
    ]
    assert "G21" in lines
    assert "G8" in lines
    assert "G17 G40 G49 G80 G90 G94 G97" in lines
    assert "G61" in lines
    assert "M9" in lines
    assert "M49" in lines
    assert "M52 P0" in lines
    assert "M53 P1" in lines
    assert "(EXACTLY_THREE_JOINTS_VERIFIED=TRUE)" in lines
    assert lines.count("G92.1") == 2
    assert "G53 G0 Z-5.000" in lines
    assert "G53 G0 X100.000 Y2400.000" in lines
    assert "G53 G0 X10.000 Y20.000" in lines
    assert "(WCS_MACHINE_ORIGIN_Z_UM=-60000)" in lines
    assert "(WCS_MACHINE_XY_ROTATION_MDEG=0)" in lines
    assert (
        "(G53_TOOL_CHANGE_PATH=G53_Z_TOOLCHANGE_XY_M6_G53_Z_THEN_ENTRY_XY_AT_GLOBAL_CLEARANCE)"
    ) in lines
    assert f"(G53_TOOL_CHANGE_CLEARANCE_EVIDENCE_SHA256={'4' * 64})" in lines
    assert f"(G52_G92_OFFSET_RESET_EVIDENCE_SHA256={'6' * 64})" in lines
    assert f"(TOOL_TABLE_EVIDENCE_SHA256={'8' * 64})" in lines
    assert "(EXPECTED_LENGTH_OFFSET_Z_UM=40000)" in lines
    assert "(HOMING_PREFLIGHT_POLICY=NO_FORCE_HOMING_0_ALL_XYZ_HOMED_BEFORE_AUTO)" in lines
    assert "(SPINDLE_AT_SPEED_TOLERANCE_PPM=50000)" in lines
    assert "(SPINDLE_FEEDBACK_SOURCE=spindle-encoder-rpm)" in lines
    assert "(SPINDLE_DWELL_ROLE=MINIMUM_DWELL_NOT_SPEED_PROOF)" in lines
    assert (
        "(PROGRAM_RESTART_POLICY="
        "PROGRAM_START_ONLY_RUN_FROM_LINE_DISABLED_FULL_RESTART_AFTER_ABORT)"
    ) in lines
    assert "(M6_TOOL_TABLE_POLICY=M6_PRESERVES_EXACT_BOUND_TOOL_TABLE)" in lines
    assert "(M6_WCS_TABLE_POLICY=M6_PRESERVES_EXACT_BOUND_WCS_TABLE)" in lines
    assert f"(M6_WCS_TABLE_EVIDENCE_SHA256={'d' * 64})" in lines
    assert "(EXTERNAL_AXIS_OFFSET_POLICY=XYZ_OFFSET_AV_RATIO_ZERO_EOFFSETS_DISABLED)" in lines
    assert f"(EXTERNAL_AXIS_OFFSET_EVIDENCE_SHA256={'8' * 64})" in lines
    assert (
        "(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY="
        "ACTUAL_RPM_OUT_OF_TOLERANCE_INHIBITS_CUTTING_FEED_CONTINUOUSLY)" in lines
    )
    assert f"(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_EVIDENCE_SHA256={'e' * 64})" in lines
    assert "G54" in lines
    assert "T7 M6" in lines
    assert "G43 H17" in lines
    assert "S18000 M3" in lines
    assert "G4 P2.500" in lines
    assert "G1 X10.000 Y20.000 Z-3.000 F500.000" in lines
    assert lines[-7:] == [
        "G0 Z15.000",
        "M5",
        "M9",
        "G49",
        "G53 G0 Z-5.000",
        "M2",
        "%",
    ]
    assert parse_production_program(first.content)


def test_production_machine_profile_round_trips_canonical_bytes() -> None:
    profile = production_machine_profile()

    assert LinuxCNCProductionMachineProfile.from_json(profile.to_json()) == profile
    assert profile.config_sha256 == profile.fingerprint

    with pytest.raises(ValueError, match="canonical JSON"):
        LinuxCNCProductionMachineProfile.from_json(profile.to_json() + b"\n")


def test_production_machine_profile_requires_every_controller_attestation() -> None:
    profile = production_machine_profile()

    with pytest.raises(ValueError, match="explicitly verified"):
        replace(profile, m6_preserves_axis_position=False)

    with pytest.raises(ValueError, match="outside the declared machine bounds"):
        replace(profile, tool_change_x_um=-1)

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(profile, g53_tool_change_path_clearance_evidence_sha256="bad")

    with pytest.raises(ValueError, match="unsupported G53 tool-change path"):
        replace(profile, g53_tool_change_path="XY_THEN_Z")

    with pytest.raises(ValueError, match="explicitly verified"):
        replace(profile, g92_1_clears_g52_g92_offsets_verified=False)

    with pytest.raises(ValueError, match="unsupported G52/G92 offset-reset policy"):
        replace(profile, g52_g92_offset_reset_policy="PERSIST_OFFSETS")

    with pytest.raises(ValueError, match="unsupported feed/spindle-override policy"):
        replace(profile, feed_spindle_override_policy="ALLOW_LIVE_OVERRIDE")

    with pytest.raises(ValueError, match="unsupported external-axis-offset policy"):
        replace(profile, external_axis_offset_policy="ALLOW_DYNAMIC_EXTERNAL_OFFSETS")

    with pytest.raises(ValueError, match="unsupported program-restart policy"):
        replace(profile, program_restart_policy="RUN_FROM_SELECTED_LINE")

    with pytest.raises(ValueError, match="unsupported M6 tool-table policy"):
        replace(profile, m6_tool_table_policy="M6_MUTATES_H_TABLE")

    with pytest.raises(ValueError, match="unsupported M6 WCS-table policy"):
        replace(profile, m6_wcs_table_policy="M6_MUTATES_G5X_TABLE")

    with pytest.raises(ValueError, match="unsupported homing-preflight policy"):
        replace(profile, homing_preflight_policy="SKIP_HOMING")

    with pytest.raises(ValueError, match="unsupported spindle-at-speed policy"):
        replace(profile, spindle_at_speed_policy="DWELL_ONLY")

    with pytest.raises(ValueError, match="unsupported continuous spindle-speed-interlock policy"):
        replace(profile, continuous_spindle_speed_interlock_policy="CHECK_ONCE_BEFORE_FEED")

    with pytest.raises(ValueError, match="spindle_at_speed_tolerance_ppm"):
        replace(profile, spindle_at_speed_tolerance_ppm=0)

    with pytest.raises(ValueError, match="canonical identity"):
        replace(profile, spindle_feedback_source="")

    with pytest.raises(ValueError, match="exactly zero"):
        replace(
            profile,
            wcs_offsets=(
                replace(profile.wcs_offsets[0], machine_xy_rotation_mdeg=1),
                profile.wcs_offsets[1],
            ),
        )


@pytest.mark.parametrize(
    "attestation",
    (
        "g97_rpm_mode_verified",
        "g8_radius_mode_verified",
        "m9_coolant_off_verified",
        "m49_feed_and_spindle_overrides_disabled_verified",
        "m52_p0_adaptive_feed_disabled_verified",
        "m53_p1_feed_hold_enabled_verified",
        "external_xyz_offsets_disabled_verified",
        "all_xyz_homed_before_auto_verified",
        "no_force_homing_disabled_verified",
        "run_from_line_disabled_verified",
        "full_restart_after_abort_required",
        "m6_preserves_bound_tool_table_verified",
        "m6_preserves_bound_wcs_table_verified",
        "real_spindle_feedback_verified",
        "spindle_at_speed_motion_interlock_verified",
        "continuous_spindle_speed_feed_inhibit_verified",
        "vfd_fault_motion_inhibit_verified",
        "vfd_fault_spindle_stop_verified",
    ),
)
def test_production_machine_profile_requires_live_state_attestations(
    attestation: str,
) -> None:
    with pytest.raises(ValueError, match="explicitly verified"):
        replace(production_machine_profile(), **{attestation: False})


def test_postprocessor_rejects_unattested_or_low_tool_change_traverse() -> None:
    document = production_document()
    profile = production_machine_profile()

    with pytest.raises(ValueError, match="explicitly verified"):
        replace(profile, g53_tool_change_path_clearance_verified=False)

    low_traverse = replace(profile, tool_change_z_um=-46_000)
    with pytest.raises(GCodeSafetyError, match="below the setup safe plane"):
        LinuxCNCProductionPostprocessor(low_traverse).generate(document)


@pytest.mark.parametrize(
    ("source", "replacement", "message"),
    (
        ("G53 G0 Z-5.000", "G53 G0 Z-4.000", "wrong outbound G53 tool-change Z"),
        (
            "G53 G0 X100.000 Y2400.000",
            "G53 G0 X101.000 Y2400.000",
            "wrong G53 tool-change XY",
        ),
        ("G4 P2.500", "G4 P2.000", "wrong spindle spin-up dwell"),
    ),
)
def test_validator_rejects_wrong_tool_change_or_spinup_profile_values(
    source: str,
    replacement: str,
    message: str,
) -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace(source, replacement, 1)

    with pytest.raises(GCodeSafetyError, match=message):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


@pytest.mark.parametrize(
    ("source", "replacement"),
    (
        ("G17 G40 G49 G80 G90 G94 G97", "G17 G40 G49 G80 G90 G94 G96"),
        ("\nG8\n", "\nG7\n"),
        ("\nM9\nM49\n", "\nM8\nM49\n"),
        ("\nM49\nM52 P0\n", "\nM48\nM52 P0\n"),
        ("\nM52 P0\n", "\nM52 P1\n"),
        ("\nM53 P1\n", "\nM53 P0\n"),
        ("\nG92.1\n", "\nG92.2\n"),
    ),
)
def test_validator_rejects_dirty_or_weakened_live_state_preamble(
    source: str,
    replacement: str,
) -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace(source, replacement, 1)

    with pytest.raises(GCodeSafetyError):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


@pytest.mark.parametrize(
    ("source", "replacement", "occurrence", "message"),
    (
        (
            "G53 G0 Z-5.000",
            "G53 G0 Z-6.000",
            2,
            "wrong post-M6 G53 return Z",
        ),
        (
            "G53 G0 X10.000 Y20.000\nG43 H17",
            "G53 G0 X11.000 Y20.000\nG43 H17",
            1,
            "wrong post-M6 G53 program-entry XY",
        ),
    ),
)
def test_validator_rejects_tampered_post_m6_return_path(
    source: str,
    replacement: str,
    occurrence: int,
    message: str,
) -> None:
    document, machine_program = generated()
    mutated = _replace_occurrence(
        machine_program.content.decode("ascii"),
        source,
        replacement,
        occurrence=occurrence,
    )

    with pytest.raises(GCodeSafetyError, match=message):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_validator_requires_complete_post_m6_state_reassertion() -> None:
    document, machine_program = generated()
    mutated = _replace_occurrence(
        machine_program.content.decode("ascii"),
        "\nG92.1\n",
        "\nG61\n",
        occurrence=2,
    )

    with pytest.raises(GCodeSafetyError, match="post-M6 canonical state reassertion"):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


@pytest.mark.parametrize(
    ("source", "replacement"),
    (
        (
            "(M6_WCS_TABLE_POLICY=M6_PRESERVES_EXACT_BOUND_WCS_TABLE)",
            "(M6_WCS_TABLE_POLICY=M6_MUTATES_G5X_TABLE)",
        ),
        (
            f"(M6_WCS_TABLE_EVIDENCE_SHA256={'d' * 64})",
            f"(M6_WCS_TABLE_EVIDENCE_SHA256={'e' * 64})",
        ),
        (
            f"(METRIC_XYZ_IDENTITY_KINEMATICS_POLICY={METRIC_XYZ_IDENTITY_KINEMATICS_POLICY})",
            "(METRIC_XYZ_IDENTITY_KINEMATICS_POLICY=ALLOW_EXTRA_AXES)",
        ),
        (
            f"(METRIC_XYZ_IDENTITY_KINEMATICS_EVIDENCE_SHA256={'f' * 64})",
            f"(METRIC_XYZ_IDENTITY_KINEMATICS_EVIDENCE_SHA256={'0' * 64})",
        ),
        (
            "(EXACTLY_THREE_JOINTS_VERIFIED=TRUE)",
            "(EXACTLY_THREE_JOINTS_VERIFIED=FALSE)",
        ),
    ),
)
def test_validator_rejects_tampered_m6_wcs_table_binding(
    source: str,
    replacement: str,
) -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace(source, replacement, 1)

    with pytest.raises(GCodeSafetyError, match="identity header"):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


@pytest.mark.parametrize(
    ("source", "replacement"),
    (
        (
            "(EXTERNAL_AXIS_OFFSET_POLICY=XYZ_OFFSET_AV_RATIO_ZERO_EOFFSETS_DISABLED)",
            "(EXTERNAL_AXIS_OFFSET_POLICY=XYZ_EOFFSETS_ALLOWED)",
        ),
        (
            f"(EXTERNAL_AXIS_OFFSET_EVIDENCE_SHA256={'8' * 64})",
            f"(EXTERNAL_AXIS_OFFSET_EVIDENCE_SHA256={'9' * 64})",
        ),
        (
            "(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY="
            "ACTUAL_RPM_OUT_OF_TOLERANCE_INHIBITS_CUTTING_FEED_CONTINUOUSLY)",
            "(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY=CHECK_ONCE_BEFORE_FEED)",
        ),
        (
            f"(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_EVIDENCE_SHA256={'e' * 64})",
            f"(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_EVIDENCE_SHA256={'f' * 64})",
        ),
    ),
)
def test_validator_rejects_tampered_external_offset_or_continuous_spindle_binding(
    source: str,
    replacement: str,
) -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace(source, replacement, 1)

    with pytest.raises(GCodeSafetyError, match="identity header"):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_postprocessor_rejects_production_machine_profile_context_drift() -> None:
    document = production_document()
    drifted_profile = replace(production_machine_profile(), controller_version="2.9.5")

    with pytest.raises(GCodeSafetyError, match="does not exactly match"):
        LinuxCNCProductionPostprocessor(drifted_profile).generate(document)


def test_postprocessor_rejects_machine_axis_bound_drift() -> None:
    document = production_document()
    drifted_profile = replace(production_machine_profile(), machine_x_max_um=1_200_000)

    with pytest.raises(GCodeSafetyError, match="does not exactly match"):
        LinuxCNCProductionPostprocessor(drifted_profile).generate(document)


def test_postprocessor_rejects_setup_wcs_offset_drift() -> None:
    document = production_document()
    profile = production_machine_profile()
    shifted_profile = replace(
        profile,
        wcs_offsets=(
            replace(profile.wcs_offsets[0], machine_x0_um=1),
            profile.wcs_offsets[1],
        ),
    )

    with pytest.raises(GCodeSafetyError, match="differs from its attested"):
        LinuxCNCProductionPostprocessor(shifted_profile).generate(document)


def test_postprocessor_rejects_first_planned_move_below_safe_plane() -> None:
    document = production_document()
    first_move = replace(
        document.programs[0].moves[0],
        role=ProductionMoveRole.APPROACH,
        z_um=2_000,
    )
    program = replace(
        document.programs[0],
        moves=(first_move, *document.programs[0].moves[1:]),
    )
    changed = replace(document, programs=(program,))

    with pytest.raises(GCodeSafetyError, match="first planned move"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(changed)


@pytest.mark.parametrize(
    ("source", "replacement", "message"),
    (
        ("\nG54\n", "\nG55\n", "unexpected WCS"),
        ("\nT7 M6\n", "\nT8 M6\n", "wrong T/M6"),
        ("\nG43 H17\n", "\nG43 H18\n", "wrong G43 H"),
        ("\nS18000 M3\n", "\nS17000 M3\n", "wrong spindle"),
        (
            "G1 X10.000 Y20.000 Z-3.000 F500.000",
            "G1 X10.000 Y20.000 Z-3.000 F501.000",
            "differs from immutable plan",
        ),
        (
            "G1 X50.000 Y20.000 Z-3.000 F2000.000",
            "G1 X1000.001 Y20.000 Z-3.000 F2000.000",
            "leaves setup XY bounds",
        ),
    ),
)
def test_validator_rejects_wrong_bound_controller_values(
    source: str,
    replacement: str,
    message: str,
) -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace(source, replacement, 1)

    with pytest.raises(GCodeSafetyError, match=message):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_validator_rejects_rapid_xy_below_safe_z() -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace(
        "G0 X10.000 Y20.000 Z15.000",
        "G0 X10.000 Y20.000 Z2.000",
        1,
    )

    with pytest.raises(GCodeSafetyError, match="rapid XY motion below safe Z"):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_nominal_move_round_trip_checks_absolute_machine_z() -> None:
    document, machine_program = generated()
    program = document.programs[0]
    executable = parse_production_program(machine_program.content)
    body_lines = executable[30:-6]

    with pytest.raises(GCodeSafetyError, match="G43-transformed machine Z bounds"):
        production_parser._validate_and_round_trip_moves(
            body_lines,
            program=program,
            setup_width_um=1_000_000,
            setup_height_um=600_000,
            stock_thickness_um=18_000,
            through_cut_allowance_um=0,
            safe_z_um=15_000,
            work_z_um=100_000,
            machine_z_min_um=-70_000,
            machine_z_max_um=30_000,
            machine_x_min_um=0,
            machine_x_max_um=1_300_000,
            machine_y_min_um=0,
            machine_y_max_um=2_500_000,
            machine_wcs_x0_um=0,
            machine_wcs_y0_um=0,
            machine_wcs_z0_um=-20_000,
            expected_length_offset_x_um=0,
            expected_length_offset_y_um=0,
            expected_length_offset_z_um=40_000,
        )


def test_postprocessor_applies_linuxcnc_g43_offset_sign_to_actual_move_bounds() -> None:
    document = production_document()
    changed_tool = replace(
        document.execution_context.tool_bindings[0],
        expected_length_offset_z_um=-50_000,
    )
    changed_context = replace(document.execution_context, tool_bindings=(changed_tool,))
    changed = replace(
        document,
        execution_context=changed_context,
        machine_profile_fingerprint=changed_context.machine_profile_fingerprint,
        tool_catalog_fingerprint=changed_context.tool_catalog_fingerprint,
        recipe_catalog_fingerprint=changed_context.recipe_catalog_fingerprint,
    )

    with pytest.raises(GCodeSafetyError, match="G43-transformed machine Z bounds"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(changed)


@pytest.mark.parametrize(
    "injected",
    (
        "G20",
        "G91",
        "G41",
        "G10 L2 P1 X0",
        "G52 X0",
        "G92 X0",
        "G92.2",
        "G96",
        "M4",
        "M30",
        "O100",
        "G1 X#1",
    ),
)
def test_production_parser_rejects_forbidden_dialect(injected: str) -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace(
        "\nG54\n",
        f"\n{injected}\nG54\n",
        1,
    )

    with pytest.raises((GCodeSafetyError, ValueError)):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.replace("\nG54\n", "\ng54\n", 1),
        lambda value: value.replace("\nG54\n", "\nG54 ; ignored code\n", 1),
        lambda value: value.replace(
            "\nG54\n",
            "\n(MSG,misleading operator message)\nG54\n",
            1,
        ),
        lambda value: value.replace(
            "\nG54\n",
            "\n(MACHINE_PROFILE=misleading@9.9.9)\nG54\n",
            1,
        ),
        lambda value: value.replace("\nG54\n", "\n\nG54\n", 1),
    ),
)
def test_validator_rejects_noncanonical_or_misleading_text(
    mutation: Callable[[str], str],
) -> None:
    document, machine_program = generated()
    changed = mutation(machine_program.content.decode("ascii"))

    with pytest.raises(GCodeSafetyError):
        validate_production_program(
            changed,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_production_parser_rejects_linuxcnc_file_line_overflow() -> None:
    payload = "%\n" + ("G" * 253) + "\n%\n"

    with pytest.raises(GCodeSafetyError, match="LinuxCNC file line limit at line 2"):
        parse_production_program(payload)


def test_postprocessor_cannot_emit_an_identity_header_linuxcnc_will_truncate() -> None:
    document = production_document()
    long_program = replace(document.programs[0], program_id="p" * 240)
    changed = replace(document, programs=(long_program,))

    with pytest.raises(GCodeSafetyError, match="LinuxCNC file line limit"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(changed)


def test_validator_rejects_negative_z_when_spindle_start_is_removed() -> None:
    document, machine_program = generated()
    mutated = machine_program.content.decode("ascii").replace("S18000 M3", "S18000 M5", 1)

    with pytest.raises(GCodeSafetyError):
        validate_production_program(
            mutated,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_postprocessor_rejects_non_linuxcnc_context() -> None:
    document = production_document()
    changed_context = replace(document.execution_context, controller_id="OtherController")
    changed = replace(
        document,
        execution_context=changed_context,
        machine_profile_fingerprint=changed_context.machine_profile_fingerprint,
        tool_catalog_fingerprint=changed_context.tool_catalog_fingerprint,
        recipe_catalog_fingerprint=changed_context.recipe_catalog_fingerprint,
    )

    with pytest.raises(GCodeSafetyError, match="exact LinuxCNC controller"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(changed)


def test_postprocessor_rejects_mixed_spindle_speeds_within_one_tool_program() -> None:
    document = production_document()
    source_recipe = document.execution_context.recipes[0]
    second_recipe = replace(
        source_recipe,
        recipe_id="mdf-groove-t06r",
        operation_kind=OperationKind.GROOVE,
        spindle_rpm=17_000,
    )
    context = replace(
        document.execution_context,
        recipes=(second_recipe, source_recipe),
    )
    program = replace(
        document.programs[0],
        recipe_ids=(source_recipe.recipe_id, second_recipe.recipe_id),
    )
    changed = replace(
        document,
        execution_context=context,
        machine_profile_fingerprint=context.machine_profile_fingerprint,
        tool_catalog_fingerprint=context.tool_catalog_fingerprint,
        recipe_catalog_fingerprint=context.recipe_catalog_fingerprint,
        programs=(program,),
    )

    with pytest.raises(GCodeSafetyError, match="one exact spindle speed"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(changed)


def test_production_machine_program_cannot_claim_physical_authorization() -> None:
    _document, machine_program = generated()

    with pytest.raises(ValueError, match="cannot authorize physical cutting"):
        replace(machine_program, physical_cutting_authorized=True)

    with pytest.raises(ValueError, match="filename must bind its run_order"):
        replace(machine_program, run_order=2)
