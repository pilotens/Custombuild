from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from custombuild_cam import (
    BoundSetup,
    CuttingRecipe,
    ProductionExecutionContext,
    ProductionMoveRole,
    ProductionToolBinding,
    ProductionToolGeometry,
    ProductionToolpathDocument,
    generate_production_toolpaths,
)
from custombuild_cam.production_model import (
    FIXTURE_KEEPOUT_POLICY,
    IDENTITY_SOURCE_TO_WCS_XY,
    STOCK_TOP_Z0_REFERENCE,
)
from custombuild_manufacturing import (
    CAMOperation,
    OperationKind,
    OperationsDocument,
    Point2D,
    Setup,
    Side,
    canonical_json_bytes,
    linuxcnc_reference_router_1325,
    sha256_hex,
)
from custombuild_manufacturing import cam_candidate_package as candidate_package
from custombuild_manufacturing import cam_software_provenance as provenance_module
from custombuild_manufacturing.cam_candidate_package import (
    CAM_CANDIDATE_BACKPLOT_PATH,
    CAM_CANDIDATE_MACHINE_PROFILE_PATH,
    CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
    CAM_CANDIDATE_PROGRAM_INDEX_PATH,
    CAM_CANDIDATE_REPORT_PATH,
    CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
    CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
    CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
    CAM_CANDIDATE_TOOLPATH_PATH,
    build_cam_candidate_bundle,
    read_operations_document_from_design_review_bundle,
)
from custombuild_manufacturing.cam_candidate_package import (
    read_and_verify_cam_candidate_package as _read_and_verify_cam_candidate_package,
)
from custombuild_manufacturing.cam_software_provenance import (
    CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION,
    CAM_CANDIDATE_PACKAGE_BUILDER_VERSION,
    current_cam_implementation_identity,
    current_cam_implementation_versions,
)
from custombuild_manufacturing.errors import ArtifactError
from custombuild_manufacturing.production_machine_profile import (
    PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    LoadedProductionMachineProfile,
    load_production_machine_profile,
)
from custombuild_postprocessors import (
    CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY,
    EXTERNAL_AXIS_OFFSET_POLICY,
    FEED_SPINDLE_OVERRIDE_POLICY,
    G52_G92_OFFSET_RESET_POLICY,
    G53_TOOL_CHANGE_PATH_COMPLETE,
    HOMING_PREFLIGHT_POLICY,
    M6_TOOL_TABLE_POLICY,
    M6_WCS_TABLE_POLICY,
    METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
    PROGRAM_RESTART_POLICY,
    SPINDLE_AT_SPEED_POLICY,
    SPINDLE_DWELL_ROLE,
    LinuxCNCProductionMachineProfile,
    LinuxCNCProductionPostprocessor,
    LinuxCNCWCSOffset,
    ProductionMachineProgram,
)

_TEST_PRODUCER_SOURCE_MANIFEST_SHA256 = sha256_hex(b"TEST_ONLY_UNATTESTED_SOURCE_MANIFEST")


def read_and_verify_cam_candidate_package(
    payload: bytes,
    *,
    base_design_review_bundle: bytes,
    allow_test_only: bool = False,
    require_current_implementations: bool = True,
) -> dict[str, object]:
    """Keep unit fixtures explicit about their deterministic TEST_ONLY code root."""

    return _read_and_verify_cam_candidate_package(
        payload,
        base_design_review_bundle=base_design_review_bundle,
        expected_producer_source_manifest_sha256=(_TEST_PRODUCER_SOURCE_MANIFEST_SHA256),
        allow_test_only=allow_test_only,
        require_current_implementations=require_current_implementations,
    )


def _source_operations() -> OperationsDocument:
    machine = linuxcnc_reference_router_1325()
    drill = next(item for item in machine.tools if item.tool_id == "T05")
    mill = next(item for item in machine.tools if item.tool_id == "T06R")
    setup = Setup(
        setup_id="setup:sheet:001:A",
        stock_id="sheet",
        material_id="mdf",
        material_version="v1",
        sheet_index=0,
        side=Side.A,
        wcs="G54",
        origin=Point2D(0, 0),
        stock_width_um=500_000,
        stock_height_um=300_000,
        stock_thickness_um=18_000,
        safe_z_um=machine.safe_z_um,
        reference_surface="EXTERNAL_STOCK_TOP_MEASUREMENT_REQUIRED",
        orientation="A_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT",
        fixture="EXTERNAL_FIXTURE_PLAN_REQUIRED; DECLARED_KEEP_OUT_ZONES_ONLY",
        keep_out_zones=(),
        tool_ids=(drill.tool_id, mill.tool_id),
        probe_method="EXTERNAL_COORDINATE_REGISTRATION_REQUIRED",
        operator_steps=("Validation-only source setup",),
    )
    operations = (
        CAMOperation(
            operation_id="op:panel:001:outer",
            setup_id=setup.setup_id,
            part_id="panel",
            instance_id="panel:001",
            feature_id="outer",
            kind=OperationKind.CONTOUR,
            side=Side.A,
            tool_id=mill.tool_id,
            x_um=100_000,
            y_um=80_000,
            depth_um=18_000,
            width_um=200_000,
            length_um=150_000,
            cutter_envelope_x_um=100_000,
            cutter_envelope_y_um=80_000,
            cutter_envelope_width_um=200_000,
            cutter_envelope_length_um=150_000,
            stepdown_um=3_000,
            through=True,
            compensation="OUTSIDE",
            holding_strategy="TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL",
        ),
        CAMOperation(
            operation_id="op:panel:001:drill",
            setup_id=setup.setup_id,
            part_id="panel",
            instance_id="panel:001",
            feature_id="drill",
            kind=OperationKind.DRILL,
            side=Side.A,
            tool_id=drill.tool_id,
            x_um=150_000,
            y_um=120_000,
            depth_um=6_000,
            diameter_um=5_000,
            stepdown_um=2_000,
        ),
    )
    tools = (drill, mill)
    return OperationsDocument(
        schema_version="custombuild.operations.v2",
        design_hash="a" * 64,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        setups=(setup,),
        operations=operations,
        tool_catalog_version=machine.tool_library_version,
        tool_catalog_fingerprint=sha256_hex(canonical_json_bytes(tools)),
        tools=tools,
    )


def _execution_context(source: OperationsDocument) -> ProductionExecutionContext:
    machine = linuxcnc_reference_router_1325()
    source_drill, source_mill = source.tools
    source_setup = source.setups[0]
    bound = BoundSetup(
        setup_id=source_setup.setup_id,
        stock_id=source_setup.stock_id,
        source_material_id=source_setup.material_id,
        source_material_version=source_setup.material_version,
        material_id="shop-mdf-18-fsc",
        material_version="lot-2026-09-04",
        material_evidence_id="accepted-material-certificate-2026-09-04",
        material_evidence_version="1.0.0",
        material_evidence_sha256="7" * 64,
        sheet_index=source_setup.sheet_index,
        side=source_setup.side,
        source_setup_sha256=sha256_hex(canonical_json_bytes(source_setup)),
        source_to_wcs_xy_transform=IDENTITY_SOURCE_TO_WCS_XY,
        wcs=source_setup.wcs,
        machine_wcs_origin=Point2D(0, 0),
        machine_wcs_z0_um=0,
        machine_wcs_xy_rotation_mdeg=0,
        stock_width_um=source_setup.stock_width_um,
        stock_height_um=source_setup.stock_height_um,
        stock_thickness_um=source_setup.stock_thickness_um,
        safe_z_um=source_setup.safe_z_um,
        reference_surface=STOCK_TOP_Z0_REFERENCE,
        orientation=source_setup.orientation,
        fixture_id="vacuum-table-01",
        fixture_version="1.0.0",
        fixture_sha256="1" * 64,
        fixture_clearance_z_um=5_000,
        minimum_rapid_clearance_um=5_000,
        keep_out_policy=FIXTURE_KEEPOUT_POLICY,
        probe_method="TOUCH_PROBE_V1",
        keep_out_zones=(),
        spoilboard_id="spoilboard-01",
        spoilboard_version="2026.1",
        spoilboard_sha256="2" * 64,
        through_cut_allowance_um=500,
    )
    drill_binding = ProductionToolBinding(
        tool_id="SHOP-T05",
        tool_version="2026.1",
        source_tool_id=source_drill.tool_id,
        source_tool_version=source_drill.version,
        source_tool_sha256=sha256_hex(canonical_json_bytes(source_drill)),
        controller_tool_number=5,
        length_offset_number=5,
        expected_length_offset_x_um=0,
        expected_length_offset_y_um=0,
        expected_length_offset_z_um=30_000,
        tool_table_evidence_id="accepted-tool-table-snapshot-2026",
        tool_table_evidence_version="2026.1",
        tool_table_evidence_sha256="6" * 64,
        effective_diameter_um=5_000,
        cutting_length_um=30_000,
        measured_stickout_um=40_000,
        minimum_holder_clearance_um=5_000,
        assembly_collision_radius_um=8_000,
        geometry=ProductionToolGeometry.DRILL,
        center_cutting=True,
        drill_point_length_um=0,
    )
    mill_binding = ProductionToolBinding(
        tool_id="SHOP-T06R",
        tool_version="2026.1",
        source_tool_id=source_mill.tool_id,
        source_tool_version=source_mill.version,
        source_tool_sha256=sha256_hex(canonical_json_bytes(source_mill)),
        controller_tool_number=6,
        length_offset_number=6,
        expected_length_offset_x_um=0,
        expected_length_offset_y_um=0,
        expected_length_offset_z_um=30_000,
        tool_table_evidence_id="accepted-tool-table-snapshot-2026",
        tool_table_evidence_version="2026.1",
        tool_table_evidence_sha256="6" * 64,
        effective_diameter_um=6_000,
        cutting_length_um=30_000,
        measured_stickout_um=40_000,
        minimum_holder_clearance_um=5_000,
        assembly_collision_radius_um=10_000,
        geometry=ProductionToolGeometry.FLAT_END_MILL,
        center_cutting=True,
        drill_point_length_um=0,
    )
    drill_recipe = CuttingRecipe(
        recipe_id="mdf-drill-shop-t05",
        version="1.0.0",
        machine_profile_id="shop-router-01",
        machine_profile_version="1.0.0",
        material_id=bound.material_id,
        material_version=bound.material_version,
        tool_id=drill_binding.tool_id,
        tool_version=drill_binding.tool_version,
        operation_kind=OperationKind.DRILL,
        spindle_rpm=12_000,
        feed_um_min=500_000,
        plunge_um_min=250_000,
        stepdown_um=2_000,
        stepover_ppm=400_000,
        peck_depth_um=2_000,
        approach_clearance_um=2_000,
        through_overtravel_um=0,
        tab_width_um=0,
        tab_height_um=0,
        process_accuracy_um=100,
        accepted_tolerance_um=200,
        diameter_tolerance_um=100,
    )
    contour_recipe = CuttingRecipe(
        recipe_id="mdf-contour-shop-t06r",
        version="1.0.0",
        machine_profile_id="shop-router-01",
        machine_profile_version="1.0.0",
        material_id=bound.material_id,
        material_version=bound.material_version,
        tool_id=mill_binding.tool_id,
        tool_version=mill_binding.tool_version,
        operation_kind=OperationKind.CONTOUR,
        spindle_rpm=18_000,
        feed_um_min=1_800_000,
        plunge_um_min=400_000,
        stepdown_um=3_000,
        stepover_ppm=400_000,
        peck_depth_um=2_000,
        approach_clearance_um=2_000,
        through_overtravel_um=300,
        tab_width_um=20_000,
        tab_height_um=3_000,
        process_accuracy_um=100,
        accepted_tolerance_um=200,
    )
    return ProductionExecutionContext(
        source_machine_profile_id=machine.profile_id,
        source_machine_profile_version=machine.version,
        source_machine_profile_fingerprint=sha256_hex(canonical_json_bytes(machine)),
        machine_profile_id="shop-router-01",
        machine_profile_version="1.0.0",
        controller_id="linuxcnc",
        controller_version="2.9.4",
        machine_x_min_um=0,
        machine_x_max_um=2_500_000,
        machine_y_min_um=0,
        machine_y_max_um=1_300_000,
        machine_z_min_um=-50_000,
        machine_z_max_um=100_000,
        work_width_um=2_500_000,
        work_height_um=1_300_000,
        work_z_um=150_000,
        min_spindle_rpm=6_000,
        max_spindle_rpm=24_000,
        max_feed_um_min=5_000_000,
        max_plunge_um_min=1_000_000,
        tool_catalog_version="shop-tools-2026.1",
        recipe_catalog_version="shop-recipes-2026.1",
        setups=(bound,),
        tool_bindings=(drill_binding, mill_binding),
        recipes=(drill_recipe, contour_recipe),
    )


def _production_profile() -> LinuxCNCProductionMachineProfile:
    return LinuxCNCProductionMachineProfile(
        profile_id="shop-router-01-linuxcnc",
        version="1.0.0",
        machine_profile_id="shop-router-01",
        machine_profile_version="1.0.0",
        controller_id="linuxcnc",
        controller_version="2.9.4",
        supported_wcs=("G54",),
        wcs_offsets=(LinuxCNCWCSOffset("G54", 0, 0, 0, 0),),
        machine_x_min_um=0,
        machine_x_max_um=2_500_000,
        machine_y_min_um=0,
        machine_y_max_um=1_300_000,
        machine_z_min_um=-50_000,
        machine_z_max_um=100_000,
        tool_change_x_um=100_000,
        tool_change_y_um=1_200_000,
        tool_change_z_um=100_000,
        spindle_spinup_ms=2_500,
        g53_tool_change_path=G53_TOOL_CHANGE_PATH_COMPLETE,
        g53_tool_change_path_clearance_evidence_id="TEST_ONLY_G53_CLEARANCE",
        g53_tool_change_path_clearance_evidence_version="TEST_ONLY_V1",
        g53_tool_change_path_clearance_evidence_sha256="3" * 64,
        wcs_offsets_evidence_id="TEST_ONLY_WCS_OFFSETS",
        wcs_offsets_evidence_version="TEST_ONLY_V1",
        wcs_offsets_evidence_sha256="4" * 64,
        g52_g92_offset_reset_policy=G52_G92_OFFSET_RESET_POLICY,
        g52_g92_offset_reset_evidence_id="TEST_ONLY_G52_G92_RESET",
        g52_g92_offset_reset_evidence_version="TEST_ONLY_V1",
        g52_g92_offset_reset_evidence_sha256="5" * 64,
        external_axis_offset_policy=EXTERNAL_AXIS_OFFSET_POLICY,
        external_axis_offset_evidence_id="TEST_ONLY_EXTERNAL_AXIS_OFFSETS",
        external_axis_offset_evidence_version="TEST_ONLY_V1",
        external_axis_offset_evidence_sha256="d" * 64,
        feed_spindle_override_policy=FEED_SPINDLE_OVERRIDE_POLICY,
        feed_spindle_override_evidence_id="TEST_ONLY_OVERRIDE_PREFLIGHT",
        feed_spindle_override_evidence_version="TEST_ONLY_V1",
        feed_spindle_override_evidence_sha256="7" * 64,
        homing_preflight_policy=HOMING_PREFLIGHT_POLICY,
        homing_preflight_evidence_id="TEST_ONLY_HOMING_PREFLIGHT",
        homing_preflight_evidence_version="TEST_ONLY_V1",
        homing_preflight_evidence_sha256="8" * 64,
        program_restart_policy=PROGRAM_RESTART_POLICY,
        program_restart_evidence_id="TEST_ONLY_PROGRAM_RESTART",
        program_restart_evidence_version="TEST_ONLY_V1",
        program_restart_evidence_sha256="a" * 64,
        m6_tool_table_policy=M6_TOOL_TABLE_POLICY,
        m6_tool_table_evidence_id="TEST_ONLY_M6_TOOL_TABLE",
        m6_tool_table_evidence_version="TEST_ONLY_V1",
        m6_tool_table_evidence_sha256="b" * 64,
        m6_wcs_table_policy=M6_WCS_TABLE_POLICY,
        m6_wcs_table_evidence_id="TEST_ONLY_M6_WCS_TABLE",
        m6_wcs_table_evidence_version="TEST_ONLY_V1",
        m6_wcs_table_evidence_sha256="c" * 64,
        metric_xyz_identity_kinematics_policy=METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
        metric_xyz_identity_kinematics_evidence_id="TEST_ONLY_METRIC_XYZ_KINEMATICS",
        metric_xyz_identity_kinematics_evidence_version="TEST_ONLY_V1",
        metric_xyz_identity_kinematics_evidence_sha256="f" * 64,
        spindle_at_speed_policy=SPINDLE_AT_SPEED_POLICY,
        spindle_feedback_source="TEST_VFD_ENCODER_RPM_FEEDBACK",
        spindle_at_speed_evidence_id="TEST_ONLY_SPINDLE_AT_SPEED",
        spindle_at_speed_evidence_version="TEST_ONLY_V1",
        spindle_at_speed_evidence_sha256="9" * 64,
        spindle_at_speed_tolerance_ppm=50_000,
        continuous_spindle_speed_interlock_policy=(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY),
        continuous_spindle_speed_interlock_evidence_id=("TEST_ONLY_CONTINUOUS_SPINDLE_INTERLOCK"),
        continuous_spindle_speed_interlock_evidence_version="TEST_ONLY_V1",
        continuous_spindle_speed_interlock_evidence_sha256="e" * 64,
        g53_machine_coordinates_verified=True,
        g53_tool_change_path_clearance_verified=True,
        wcs_offsets_verified=True,
        g92_1_clears_g52_g92_offsets_verified=True,
        external_xyz_offsets_disabled_verified=True,
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
        all_xyz_homed_before_auto_verified=True,
        no_force_homing_disabled_verified=True,
        run_from_line_disabled_verified=True,
        full_restart_after_abort_required=True,
        real_spindle_feedback_verified=True,
        spindle_at_speed_motion_interlock_verified=True,
        vfd_fault_motion_inhibit_verified=True,
        vfd_fault_spindle_stop_verified=True,
        continuous_spindle_speed_feed_inhibit_verified=True,
        m3_clockwise_spindle_verified=True,
        g4_p_seconds_dwell_verified=True,
    )


def _loaded_profile(source: OperationsDocument) -> LoadedProductionMachineProfile:
    context = _execution_context(source)
    postprocessor = _production_profile()
    payload = {
        "acceptance": {
            "evidence_id": "TEST_ONLY_CAM_CANDIDATE_ACCEPTANCE",
            "evidence_sha256": "e" * 64,
            "evidence_version": "TEST_ONLY_V1",
            "status": "TEST_ONLY",
        },
        "machine": {
            "source_machine_profile_id": context.source_machine_profile_id,
            "source_machine_profile_version": context.source_machine_profile_version,
            "source_machine_profile_fingerprint": (context.source_machine_profile_fingerprint),
            "machine_profile_id": context.machine_profile_id,
            "machine_profile_version": context.machine_profile_version,
            "controller_id": context.controller_id,
            "controller_version": context.controller_version,
            "work_width_um": context.work_width_um,
            "work_height_um": context.work_height_um,
            "work_z_um": context.work_z_um,
            "machine_x_min_um": context.machine_x_min_um,
            "machine_x_max_um": context.machine_x_max_um,
            "machine_y_min_um": context.machine_y_min_um,
            "machine_y_max_um": context.machine_y_max_um,
            "machine_z_min_um": context.machine_z_min_um,
            "machine_z_max_um": context.machine_z_max_um,
            "min_spindle_rpm": context.min_spindle_rpm,
            "max_spindle_rpm": context.max_spindle_rpm,
            "max_feed_um_min": context.max_feed_um_min,
            "max_plunge_um_min": context.max_plunge_um_min,
            "tool_catalog_version": context.tool_catalog_version,
            "recipe_catalog_version": context.recipe_catalog_version,
            "postprocessor_profile_id": postprocessor.profile_id,
            "postprocessor_profile_version": postprocessor.version,
            "postprocessor_profile_sha256": postprocessor.config_sha256,
        },
        "postprocessor_profile": json.loads(postprocessor.to_json()),
        "profile_class": "TEST_ONLY",
        "setups": [
            {
                "setup_id": setup.setup_id,
                "stock_id": setup.stock_id,
                "source_material_id": setup.source_material_id,
                "source_material_version": setup.source_material_version,
                "material_id": setup.material_id,
                "material_version": setup.material_version,
                "material_evidence_id": setup.material_evidence_id,
                "material_evidence_version": setup.material_evidence_version,
                "material_evidence_sha256": setup.material_evidence_sha256,
                "sheet_index": setup.sheet_index,
                "side": setup.side.value,
                "source_setup_sha256": setup.source_setup_sha256,
                "source_to_wcs_xy_transform": setup.source_to_wcs_xy_transform,
                "wcs": setup.wcs,
                "machine_wcs_origin": {
                    "x_um": setup.machine_wcs_origin.x_um,
                    "y_um": setup.machine_wcs_origin.y_um,
                },
                "machine_wcs_z0_um": setup.machine_wcs_z0_um,
                "machine_wcs_xy_rotation_mdeg": setup.machine_wcs_xy_rotation_mdeg,
                "stock_width_um": setup.stock_width_um,
                "stock_height_um": setup.stock_height_um,
                "stock_thickness_um": setup.stock_thickness_um,
                "safe_z_um": setup.safe_z_um,
                "minimum_rapid_clearance_um": setup.minimum_rapid_clearance_um,
                "reference_surface": setup.reference_surface,
                "orientation": setup.orientation,
                "fixture": {
                    "fixture_id": setup.fixture_id,
                    "fixture_version": setup.fixture_version,
                    "fixture_sha256": setup.fixture_sha256,
                    "clearance_z_um": setup.fixture_clearance_z_um,
                    "keep_out_policy": setup.keep_out_policy,
                },
                "probe_method": setup.probe_method,
                "keep_out_zones": [
                    {
                        "x_um": zone.x_um,
                        "y_um": zone.y_um,
                        "width_um": zone.width_um,
                        "height_um": zone.height_um,
                    }
                    for zone in setup.keep_out_zones
                ],
                "raw_allowance_um": setup.raw_allowance_um,
                "spoilboard_id": setup.spoilboard_id,
                "spoilboard_version": setup.spoilboard_version,
                "spoilboard_sha256": setup.spoilboard_sha256,
                "through_cut_allowance_um": setup.through_cut_allowance_um,
            }
            for setup in context.setups
        ],
        "tools": [
            {
                "tool_id": tool.tool_id,
                "tool_version": tool.tool_version,
                "source_tool_id": tool.source_tool_id,
                "source_tool_version": tool.source_tool_version,
                "source_tool_sha256": tool.source_tool_sha256,
                "controller_tool_number": tool.controller_tool_number,
                "length_offset_number": tool.length_offset_number,
                "expected_length_offset_x_um": tool.expected_length_offset_x_um,
                "expected_length_offset_y_um": tool.expected_length_offset_y_um,
                "expected_length_offset_z_um": tool.expected_length_offset_z_um,
                "tool_table_evidence_id": tool.tool_table_evidence_id,
                "tool_table_evidence_version": tool.tool_table_evidence_version,
                "tool_table_evidence_sha256": tool.tool_table_evidence_sha256,
                "effective_diameter_um": tool.effective_diameter_um,
                "drill_point_length_um": tool.drill_point_length_um,
                "cutting_length_um": tool.cutting_length_um,
                "measured_stickout_um": tool.measured_stickout_um,
                "minimum_holder_clearance_um": tool.minimum_holder_clearance_um,
                "assembly_collision_radius_um": tool.assembly_collision_radius_um,
                "geometry": tool.geometry.value,
                "center_cutting": tool.center_cutting,
                "spindle_direction": tool.spindle_direction,
            }
            for tool in context.tool_bindings
        ],
        "recipes": [
            {
                "recipe_id": recipe.recipe_id,
                "version": recipe.version,
                "machine_profile_id": recipe.machine_profile_id,
                "machine_profile_version": recipe.machine_profile_version,
                "material_id": recipe.material_id,
                "material_version": recipe.material_version,
                "tool_id": recipe.tool_id,
                "tool_version": recipe.tool_version,
                "operation_kind": recipe.operation_kind.value,
                "spindle_rpm": recipe.spindle_rpm,
                "feed_um_min": recipe.feed_um_min,
                "plunge_um_min": recipe.plunge_um_min,
                "stepdown_um": recipe.stepdown_um,
                "stepover_ppm": recipe.stepover_ppm,
                "peck_depth_um": recipe.peck_depth_um,
                "approach_clearance_um": recipe.approach_clearance_um,
                "through_overtravel_um": recipe.through_overtravel_um,
                "tab_width_um": recipe.tab_width_um,
                "tab_height_um": recipe.tab_height_um,
                "process_accuracy_um": recipe.process_accuracy_um,
                "accepted_tolerance_um": recipe.accepted_tolerance_um,
                "entry_strategy": recipe.entry_strategy,
                "diameter_tolerance_um": recipe.diameter_tolerance_um,
                "countersink_top_diameter_um": recipe.countersink_top_diameter_um,
                "countersink_included_angle_mdeg": (recipe.countersink_included_angle_mdeg),
            }
            for recipe in context.recipes
        ],
    }
    document = canonical_json_bytes(
        {
            "payload": payload,
            "payload_sha256": sha256_hex(canonical_json_bytes(payload)),
            "schema_version": PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
        }
    )
    loaded = load_production_machine_profile(document, allow_test_only=True)
    assert loaded.execution_context == context
    assert loaded.postprocessor_profile == postprocessor
    return loaded


def _fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    ProductionToolpathDocument,
    LoadedProductionMachineProfile,
    tuple[ProductionMachineProgram, ...],
]:
    source = _source_operations()
    profile = _loaded_profile(source)
    context = profile.execution_context
    toolpaths = generate_production_toolpaths(source, context)
    programs = LinuxCNCProductionPostprocessor(profile.postprocessor_profile).generate(toolpaths)
    base = candidate_package._BaseDesignReviewBinding(
        bundle_sha256=sha256_hex(b"verified-review-bundle"),
        bundle_size_bytes=len(b"verified-review-bundle"),
        manifest_sha256="1" * 64,
        operations_sha256=sha256_hex(source.to_json()),
        project_id="project-1",
        revision="7",
        design_hash=source.design_hash,
        machine_profile_id=source.machine_profile_id,
        machine_profile_version=source.machine_profile_version,
        machine_profile_fingerprint=context.source_machine_profile_fingerprint,
        operations_bytes=source.to_json(),
    )
    monkeypatch.setattr(candidate_package, "_verified_base_binding", lambda _payload: base)
    return toolpaths, profile, programs


def _rewrite_zip(payload: bytes, files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0x800
            archive.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def _rehash_artifact_and_manifest(files: dict[str, bytes], path: str) -> None:
    manifest = json.loads(files["manifest.json"])
    entry = next(item for item in manifest["artifacts"] if item["path"] == path)
    entry["sha256"] = sha256_hex(files[path])
    entry["size_bytes"] = len(files[path])
    context = {
        key: value
        for key, value in manifest.items()
        if key not in {"candidate_context_hash", "checksum_scope"}
    }
    manifest["candidate_context_hash"] = sha256_hex(canonical_json_bytes(context))
    files["manifest.json"] = canonical_json_bytes(manifest)


def _rehash_manifest_context(files: dict[str, bytes], manifest: dict[str, object]) -> None:
    context = {
        key: value
        for key, value in manifest.items()
        if key not in {"candidate_context_hash", "checksum_scope"}
    }
    manifest["candidate_context_hash"] = sha256_hex(canonical_json_bytes(context))
    files["manifest.json"] = canonical_json_bytes(manifest)


def test_sidecar_is_deterministic_complete_and_strictly_verifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    assert (
        read_operations_document_from_design_review_bundle(b"verified-review-bundle")
        == _source_operations()
    )

    first = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    second = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )

    assert first.zip_bytes == second.zip_bytes
    assert first.manifest["status"] == "CUTTING_CANDIDATE_GENERATED"
    assert first.manifest["mode"] == "EXECUTABLE_CAM_CANDIDATE"
    assert first.manifest["physical_cutting_authorized"] is False
    assert first.manifest["workshop_acceptance_required"] is True
    assert first.manifest["schema_version"] == CAM_CANDIDATE_MANIFEST_SCHEMA_VERSION
    assert first.manifest["builder_version"] == CAM_CANDIDATE_PACKAGE_BUILDER_VERSION
    provenance = first.manifest["software_provenance"]
    assert provenance["code_root"] == {
        "kind": "SOURCE_MANIFEST_SHA256",
        "sha256": _TEST_PRODUCER_SOURCE_MANIFEST_SHA256,
    }
    assert provenance["implementations"] == current_cam_implementation_versions()
    assert first.production_profile == profile
    assert first.production_machine_profile == profile.postprocessor_profile
    paths = {artifact.path for artifact in first.artifacts}
    assert {
        CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
        CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
        CAM_CANDIDATE_TOOLPATH_PATH,
        CAM_CANDIDATE_MACHINE_PROFILE_PATH,
        CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
        CAM_CANDIDATE_PROGRAM_INDEX_PATH,
        CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
        CAM_CANDIDATE_REPORT_PATH,
        CAM_CANDIDATE_BACKPLOT_PATH,
    } <= paths
    assert first.program_index["programs"][0]["execution_order"] == 1
    expected_materials = [
        {
            "stock_id": "sheet",
            "sheet_index": 0,
            "source_material": {"id": "mdf", "version": "v1"},
            "actual_material": {
                "id": "shop-mdf-18-fsc",
                "version": "lot-2026-09-04",
                "evidence": {
                    "id": "accepted-material-certificate-2026-09-04",
                    "version": "1.0.0",
                    "sha256": "7" * 64,
                },
            },
        }
    ]
    assert first.manifest["materials"] == expected_materials
    assert first.program_index["materials"] == expected_materials
    assert first.setup_instructions["materials"] == expected_materials
    assert first.cutting_program_report["materials"] == expected_materials
    expected_material_binding = {
        "source_material": expected_materials[0]["source_material"],
        "actual_material": expected_materials[0]["actual_material"],
    }
    program_stock = first.program_index["programs"][0]["setup"]["stock"]
    assert program_stock["source_material"] == expected_material_binding["source_material"]
    assert program_stock["actual_material"] == expected_material_binding["actual_material"]
    assert (
        first.setup_instructions["setups"][0]["stock"]["source_material"]
        == (expected_material_binding["source_material"])
    )
    assert (
        first.setup_instructions["setups"][0]["stock"]["actual_material"]
        == (expected_material_binding["actual_material"])
    )
    assert (
        first.cutting_program_report["postprocessor_round_trip"]["programs"][0]["material_binding"]
        == expected_material_binding
    )
    assert first.manifest["production_machine_profile"]["path"] == (
        CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH
    )
    runtime_safety = first.manifest["production_machine_profile"]["runtime_safety"]
    assert runtime_safety["modal_preflight"]["required_program_states"] == [
        "G8",
        "G97",
        "M9",
        "M49",
        "M52 P0",
        "M53 P1",
    ]
    metric_kinematics = runtime_safety["metric_xyz_identity_kinematics"]
    assert metric_kinematics["policy"] == METRIC_XYZ_IDENTITY_KINEMATICS_POLICY
    assert metric_kinematics["evidence"]["sha256"] == "f" * 64
    assert metric_kinematics["linear_units_mm_verified"] is True
    assert metric_kinematics["coordinates_xyz_verified"] is True
    assert metric_kinematics["identity_trivkins_verified"] is True
    assert metric_kinematics["required_joint_count"] == 3
    assert metric_kinematics["exactly_three_joints_verified"] is True
    assert metric_kinematics["joint_0_x_1_y_2_z_verified"] is True
    assert metric_kinematics["no_extra_controlled_axes_verified"] is True
    assert runtime_safety["feed_spindle_overrides"]["policy"] == (FEED_SPINDLE_OVERRIDE_POLICY)
    assert runtime_safety["homing"]["policy"] == HOMING_PREFLIGHT_POLICY
    assert runtime_safety["program_restart"]["policy"] == PROGRAM_RESTART_POLICY
    assert runtime_safety["program_restart"]["run_from_line_disabled_verified"] is True
    assert runtime_safety["program_restart"]["full_restart_after_abort_required"] is True
    assert runtime_safety["tool_change"]["policy"] == M6_TOOL_TABLE_POLICY
    assert runtime_safety["tool_change"]["m6_preserves_bound_tool_table_verified"] is True
    wcs_preservation = runtime_safety["wcs_table_preservation"]
    assert wcs_preservation["policy"] == M6_WCS_TABLE_POLICY
    assert wcs_preservation["m6_preserves_bound_wcs_table_verified"] is True
    assert wcs_preservation["exact_raw_g5x_xyz_r_preservation_required"] is True
    assert wcs_preservation["required_continuity"] == (
        "EXACT_RAW_G5X_XYZ_AND_R_FROM_PREFLIGHT_THROUGH_POST_M6_WCS_SELECTION"
    )
    assert runtime_safety["spindle_at_speed"]["dwell_role"] == SPINDLE_DWELL_ROLE
    assert first.program_index["production_machine_profile"]["runtime_safety"] == runtime_safety
    assert (
        first.program_index["production_machine_profile"]["runtime_safety"]["tool_change"]["policy"]
        == M6_TOOL_TABLE_POLICY
    )
    assert first.manifest["source_machine_profile"]["path"] == (
        CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH
    )
    assert first.setup_instructions["required_preflight_checks"][-1] == (
        "OBTAIN_WORKSHOP_ACCEPTANCE_BEFORE_MACHINE_START"
    )
    assert (
        "VERIFY_PHYSICAL_BARRIERS_GUARDS_ESTOP_AND_SAFE_WORK_ZONE"
        in first.setup_instructions["required_preflight_checks"]
    )
    assert (
        "VERIFY_CURRENT_SPINDLE_TOOL_EMPTY_OR_PROFILE_BOUND"
        in first.setup_instructions["required_preflight_checks"]
    )
    live_state = first.setup_instructions["expected_live_controller_state"]
    assert live_state["observations_embedded"] is False
    assert live_state["metric_xyz_identity_kinematics"] == {
        "additional_controlled_axes_permitted": False,
        "coordinates_xyz_verified": True,
        "evidence": {
            "id": "TEST_ONLY_METRIC_XYZ_KINEMATICS",
            "sha256": "f" * 64,
            "version": "TEST_ONLY_V1",
        },
        "exactly_three_joints_verified": True,
        "identity_trivkins_verified": True,
        "joint_0_x_1_y_2_z_verified": True,
        "linear_units_mm_verified": True,
        "no_extra_controlled_axes_verified": True,
        "policy": METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
        "required_coordinates": "XYZ",
        "required_joint_count": 3,
        "required_joint_axis_mapping": ["0:X", "1:Y", "2:Z"],
        "required_kinematics": "IDENTITY_TRIVKINS",
        "required_native_linear_units": "mm",
    }
    assert live_state["home_axes"]["required_axes"] == ["X", "Y", "Z"]
    assert live_state["home_axes"]["policy"] == HOMING_PREFLIGHT_POLICY
    assert live_state["initial_spindle_tool"]["required_state"] == (
        "EMPTY_OR_EXACT_PROFILE_BOUND_TOOL_ASSEMBLY"
    )
    assert live_state["initial_spindle_tool"]["unsafe_tool_number_override_permitted"] is False
    tool_change_state = live_state["tool_change_and_tool_table"]
    assert tool_change_state["policy"] == M6_TOOL_TABLE_POLICY
    assert tool_change_state["m6_preserves_bound_tool_table_verified"] is True
    assert tool_change_state["automatic_probe_or_remap_may_mutate_bound_h_row"] is False
    assert live_state["wcs_table"]["expected_offsets"] == [
        {
            "wcs": "G54",
            "machine_x0_um": 0,
            "machine_y0_um": 0,
            "machine_z0_um": 0,
            "machine_xy_rotation_mdeg": 0,
        }
    ]
    wcs_m6 = live_state["wcs_table"]["m6_preservation"]
    assert (
        live_state["wcs_table"][
            "live_values_must_remain_equal_expected_through_m6_and_wcs_selection"
        ]
        is True
    )
    assert wcs_m6["policy"] == M6_WCS_TABLE_POLICY
    assert wcs_m6["m6_preserves_bound_wcs_table_verified"] is True
    assert wcs_m6["exact_raw_g5x_xyz_r_preservation_required"] is True
    assert wcs_m6["automatic_probe_or_remap_may_mutate_bound_g5x_row"] is False
    assert wcs_m6["required_continuity"] == (
        "EXACT_RAW_G5X_XYZ_AND_R_FROM_PREFLIGHT_THROUGH_POST_M6_WCS_SELECTION"
    )
    assert live_state["spindle_and_overrides"]["policy"] == FEED_SPINDLE_OVERRIDE_POLICY
    assert live_state["spindle_and_overrides"]["required_program_states"] == [
        "G8",
        "G97",
        "M9",
        "M49",
        "M52 P0",
        "M53 P1",
    ]
    assert (
        live_state["spindle_and_overrides"]["m49_feed_and_spindle_overrides_disabled_verified"]
        is True
    )
    assert live_state["program_execution"]["policy"] == PROGRAM_RESTART_POLICY
    assert live_state["program_execution"]["allowed_entry_point"] == "PROGRAM_START_ONLY"
    assert live_state["program_execution"]["run_from_line_disabled_verified"] is True
    assert live_state["program_execution"]["full_restart_after_abort_required"] is True
    spindle_state = live_state["spindle_and_overrides"]["spindle_at_speed"]
    assert spindle_state["policy"] == SPINDLE_AT_SPEED_POLICY
    assert spindle_state["dwell_role"] == SPINDLE_DWELL_ROLE
    assert spindle_state["g4_is_speed_proof"] is False
    assert spindle_state["actual_rpm_must_be_nonzero"] is True
    first_program = first.programs[0].content.decode("ascii")
    assert f"(FEED_SPINDLE_OVERRIDE_POLICY={FEED_SPINDLE_OVERRIDE_POLICY})" in first_program
    assert f"(PROGRAM_RESTART_POLICY={PROGRAM_RESTART_POLICY})" in first_program
    assert f"(M6_TOOL_TABLE_POLICY={M6_TOOL_TABLE_POLICY})" in first_program
    assert f"(M6_WCS_TABLE_POLICY={M6_WCS_TABLE_POLICY})" in first_program
    setup_tool = first.setup_instructions["setups"][0]["tools"][0]
    assert setup_tool["expected_length_offset_um"] == {
        "x": 0,
        "y": 0,
        "z": 30_000,
    }
    assert setup_tool["length_offset_semantics"] == ("SIGNED_LINUXCNC_TOOL_TABLE_XYZ_VALUES")
    assert setup_tool["live_controller_h_offset_must_equal_expected"] is True
    assert setup_tool["drill_point_length_um"] == 0
    assert first.program_index["programs"][0]["tool"]["drill_point_length_um"] == 0
    assert (
        first.setup_instructions["setups"][0]["coordinate_registration"][
            "machine_wcs_origin_semantics"
        ]
        == "RAW_LINUXCNC_G5X_OFFSET_FROM_MACHINE_ORIGIN"
    )
    assert (
        first.setup_instructions["setups"][0]["coordinate_registration"][
            "programmed_coordinates_semantics"
        ]
        == "CONTROLLED_POINT_WCS"
    )
    assert (
        first.setup_instructions["setups"][0]["coordinate_registration"][
            "g43_axis_endpoint_formula"
        ]
        == "MACHINE_WCS_PLUS_PROGRAMMED_PLUS_EXPECTED_H"
    )
    assert (
        first.setup_instructions["program_sequence"][-1]["sheet_release_state_after_program"]
        == "SHEET_RELEASED_NO_FURTHER_PROGRAMS"
    )
    contour_recipe = next(
        recipe
        for row in first.setup_instructions["program_sequence"]
        for recipe in row["recipes"]
        if recipe["operation_kind"] == "CONTOUR"
    )
    assert contour_recipe["entry_strategy"] == "PLUNGE"
    assert contour_recipe["through_overtravel_um"] == 300
    assert contour_recipe["tab_width_um"] == 20_000
    assert contour_recipe["tab_height_um"] == 3_000
    assert contour_recipe["approach_clearance_um"] == 2_000
    assert first.cutting_program_report["result"] == "PASS"
    assert first.cutting_program_report["independent_source_to_removal"]["status"] == "PASS"
    assert first.cutting_program_report["postprocessor_round_trip"]["result"] == "PASS"
    assert (
        read_and_verify_cam_candidate_package(
            first.zip_bytes,
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )
        == first.manifest
    )

    with pytest.raises(ArtifactError, match="software provenance is invalid"):
        read_and_verify_cam_candidate_package(
            first.zip_bytes,
            base_design_review_bundle=b"verified-review-bundle",
        )


def test_reader_rejects_rehashed_software_provenance_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        original_files = {name: archive.read(name) for name in archive.namelist()}

    def rejects(mutate: Callable[[dict[str, Any]], None]) -> None:
        files = dict(original_files)
        manifest = json.loads(files["manifest.json"])
        mutate(manifest["software_provenance"])
        _rehash_manifest_context(files, manifest)
        with pytest.raises(ArtifactError):
            read_and_verify_cam_candidate_package(
                _rewrite_zip(result.zip_bytes, files),
                base_design_review_bundle=b"verified-review-bundle",
                allow_test_only=True,
            )

    rejects(lambda value: value["producer_build"].update({"unexpected": "forged"}))
    rejects(
        lambda value: value["implementations"].update(
            {"candidate_package_builder_version": "forged-builder-9.9.9"}
        )
    )

    def replace_producer_code_root(value: dict[str, Any]) -> None:
        replacement = "0" * 64
        value["code_root"]["sha256"] = replacement
        value["producer_build"]["source_manifest_sha256"] = replacement

    rejects(replace_producer_code_root)


def test_historical_reader_uses_the_frozen_supported_implementation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    frozen_identity = provenance_module.parse_supported_cam_implementation_identity(
        result.manifest["software_provenance"]["implementations"]
    )
    frozen_dispatch = candidate_package._resolve_cam_candidate_verification_dispatch(
        frozen_identity
    )
    assert frozen_dispatch.key == frozen_identity.dispatch_key
    assert frozen_dispatch.verify is candidate_package._verify_cam_candidate_v1
    monkeypatch.setattr(
        provenance_module,
        "PRODUCTION_TOOLPATH_ENGINE_VERSION",
        "production-toolpaths-future",
    )

    with pytest.raises(ArtifactError, match="software provenance is invalid"):
        read_and_verify_cam_candidate_package(
            result.zip_bytes,
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    assert (
        read_and_verify_cam_candidate_package(
            result.zip_bytes,
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
            require_current_implementations=False,
        )
        == result.manifest
    )


def test_historical_reader_dispatches_full_second_identity_before_v1_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    current = current_cam_implementation_identity()
    second = replace(
        current,
        support_id="custombuild.cam-implementation-stack.test-v2",
        verification_dispatch="custombuild.cam-candidate-verifier.test-v2",
        toolpath_engine_version="production-toolpaths-test-v2",
    )
    monkeypatch.setattr(
        provenance_module,
        "_SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES",
        {current.support_id: current, second.support_id: second},
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    manifest["software_provenance"]["implementations"] = second.as_dict()
    _rehash_manifest_context(files, manifest)
    second_payload = _rewrite_zip(result.zip_bytes, files)

    invoked: list[object] = []

    def verify_second(request: object) -> dict[str, Any]:
        invoked.append(request)
        return {"dispatch": "second"}

    second_dispatch = candidate_package._dispatch_entry(second, verify_second)
    monkeypatch.setattr(
        candidate_package,
        "_CAM_CANDIDATE_VERIFICATION_DISPATCHES",
        {
            candidate_package._V1_VERIFICATION_DISPATCH.key: (
                candidate_package._V1_VERIFICATION_DISPATCH
            ),
            second_dispatch.key: second_dispatch,
        },
    )
    monkeypatch.setattr(
        candidate_package,
        "_parse_toolpath_document",
        lambda _payload: pytest.fail("v1 parser must not run for the second stack"),
    )

    assert read_and_verify_cam_candidate_package(
        second_payload,
        base_design_review_bundle=b"verified-review-bundle",
        allow_test_only=True,
        require_current_implementations=False,
    ) == {"dispatch": "second"}
    assert len(invoked) == 1


def test_historical_reader_fails_closed_without_an_exact_verifier_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    current = current_cam_implementation_identity()
    second = replace(
        current,
        support_id="custombuild.cam-implementation-stack.test-v2",
        verification_dispatch="custombuild.cam-candidate-verifier.test-v2",
        toolpath_engine_version="production-toolpaths-test-v2",
    )
    monkeypatch.setattr(
        provenance_module,
        "_SUPPORTED_CAM_IMPLEMENTATION_IDENTITIES",
        {current.support_id: current, second.support_id: second},
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    manifest["software_provenance"]["implementations"] = second.as_dict()
    _rehash_manifest_context(files, manifest)
    second_payload = _rewrite_zip(result.zip_bytes, files)

    with pytest.raises(ArtifactError, match="software provenance is invalid"):
        read_and_verify_cam_candidate_package(
            second_payload,
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
            require_current_implementations=False,
        )

    monkeypatch.setattr(
        candidate_package,
        "_CAM_CANDIDATE_VERIFICATION_DISPATCHES",
        {second.dispatch_key: candidate_package._V1_VERIFICATION_DISPATCH},
    )
    with pytest.raises(ArtifactError, match="software provenance is invalid"):
        read_and_verify_cam_candidate_package(
            second_payload,
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
            require_current_implementations=False,
        )


def test_reader_rejects_program_tamper_even_when_manifest_is_rehashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    program_path = next(name for name in files if name.endswith(".production.ngc"))
    original_program = files[program_path]
    files[program_path] = original_program.replace(b"X150.000", b"X151.000", 1)
    assert files[program_path] != original_program
    _rehash_artifact_and_manifest(files, program_path)
    tampered = _rewrite_zip(result.zip_bytes, files)

    with pytest.raises(ArtifactError):
        read_and_verify_cam_candidate_package(
            tampered,
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )


def test_runtime_safety_contract_is_bound_across_manifest_index_setup_and_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        original_files = {name: archive.read(name) for name in archive.namelist()}

    manifest_files = dict(original_files)
    manifest = json.loads(manifest_files["manifest.json"])
    manifest["production_machine_profile"]["runtime_safety"]["feed_spindle_overrides"][
        "m49_feed_and_spindle_overrides_disabled_verified"
    ] = False
    context = {
        key: value
        for key, value in manifest.items()
        if key not in {"candidate_context_hash", "checksum_scope"}
    }
    manifest["candidate_context_hash"] = sha256_hex(canonical_json_bytes(context))
    manifest_files["manifest.json"] = canonical_json_bytes(manifest)
    with pytest.raises(ArtifactError, match="manifest differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, manifest_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    wcs_manifest_files = dict(original_files)
    wcs_manifest = json.loads(wcs_manifest_files["manifest.json"])
    wcs_manifest["production_machine_profile"]["runtime_safety"]["wcs_table_preservation"][
        "m6_preserves_bound_wcs_table_verified"
    ] = False
    wcs_context = {
        key: value
        for key, value in wcs_manifest.items()
        if key not in {"candidate_context_hash", "checksum_scope"}
    }
    wcs_manifest["candidate_context_hash"] = sha256_hex(canonical_json_bytes(wcs_context))
    wcs_manifest_files["manifest.json"] = canonical_json_bytes(wcs_manifest)
    with pytest.raises(ArtifactError, match="manifest differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, wcs_manifest_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    index_files = dict(original_files)
    index = json.loads(index_files[CAM_CANDIDATE_PROGRAM_INDEX_PATH])
    index["production_machine_profile"]["runtime_safety"]["program_restart"][
        "run_from_line_disabled_verified"
    ] = False
    index_files[CAM_CANDIDATE_PROGRAM_INDEX_PATH] = canonical_json_bytes(index)
    _rehash_artifact_and_manifest(index_files, CAM_CANDIDATE_PROGRAM_INDEX_PATH)
    with pytest.raises(ArtifactError, match="program index differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, index_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    wcs_index_files = dict(original_files)
    wcs_index = json.loads(wcs_index_files[CAM_CANDIDATE_PROGRAM_INDEX_PATH])
    wcs_index["production_machine_profile"]["runtime_safety"]["wcs_table_preservation"][
        "exact_raw_g5x_xyz_r_preservation_required"
    ] = False
    wcs_index_files[CAM_CANDIDATE_PROGRAM_INDEX_PATH] = canonical_json_bytes(wcs_index)
    _rehash_artifact_and_manifest(wcs_index_files, CAM_CANDIDATE_PROGRAM_INDEX_PATH)
    with pytest.raises(ArtifactError, match="program index differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, wcs_index_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    setup_files = dict(original_files)
    setup = json.loads(setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH])
    setup["expected_live_controller_state"]["spindle_and_overrides"]["required_program_states"][
        3
    ] = "M48"
    setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH] = canonical_json_bytes(setup)
    _rehash_artifact_and_manifest(setup_files, CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH)
    with pytest.raises(ArtifactError, match="setup instructions"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, setup_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    m6_setup_files = dict(original_files)
    m6_setup = json.loads(m6_setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH])
    m6_setup["expected_live_controller_state"]["tool_change_and_tool_table"][
        "automatic_probe_or_remap_may_mutate_bound_h_row"
    ] = True
    m6_setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH] = canonical_json_bytes(m6_setup)
    _rehash_artifact_and_manifest(m6_setup_files, CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH)
    with pytest.raises(ArtifactError, match="setup instructions"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, m6_setup_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    wcs_setup_files = dict(original_files)
    wcs_setup = json.loads(wcs_setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH])
    wcs_setup["expected_live_controller_state"]["wcs_table"]["m6_preservation"][
        "automatic_probe_or_remap_may_mutate_bound_g5x_row"
    ] = True
    wcs_setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH] = canonical_json_bytes(wcs_setup)
    _rehash_artifact_and_manifest(wcs_setup_files, CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH)
    with pytest.raises(ArtifactError, match="setup instructions"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, wcs_setup_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    header_files = dict(original_files)
    program_path = next(name for name in header_files if name.endswith(".production.ngc"))
    original_program = header_files[program_path]
    header_files[program_path] = original_program.replace(
        f"(PROGRAM_RESTART_POLICY={PROGRAM_RESTART_POLICY})".encode(),
        b"(PROGRAM_RESTART_POLICY=RUN_FROM_LINE_PERMITTED)",
        1,
    )
    assert header_files[program_path] != original_program
    _rehash_artifact_and_manifest(header_files, program_path)
    with pytest.raises(ArtifactError, match="canonical LinuxCNC postprocessor output"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, header_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    m6_header_files = dict(original_files)
    program_path = next(name for name in m6_header_files if name.endswith(".production.ngc"))
    original_program = m6_header_files[program_path]
    m6_header_files[program_path] = original_program.replace(
        f"(M6_TOOL_TABLE_POLICY={M6_TOOL_TABLE_POLICY})".encode(),
        b"(M6_TOOL_TABLE_POLICY=M6_MAY_REWRITE_BOUND_TOOL_TABLE)",
        1,
    )
    assert m6_header_files[program_path] != original_program
    _rehash_artifact_and_manifest(m6_header_files, program_path)
    with pytest.raises(ArtifactError, match="canonical LinuxCNC postprocessor output"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, m6_header_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    wcs_header_files = dict(original_files)
    program_path = next(name for name in wcs_header_files if name.endswith(".production.ngc"))
    original_program = wcs_header_files[program_path]
    wcs_header_files[program_path] = original_program.replace(
        f"(M6_WCS_TABLE_POLICY={M6_WCS_TABLE_POLICY})".encode(),
        b"(M6_WCS_TABLE_POLICY=M6_MAY_REWRITE_BOUND_WCS_TABLE)",
        1,
    )
    assert wcs_header_files[program_path] != original_program
    _rehash_artifact_and_manifest(wcs_header_files, program_path)
    with pytest.raises(ArtifactError, match="canonical LinuxCNC postprocessor output"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, wcs_header_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )


def test_material_provenance_is_fail_closed_across_candidate_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        original_files = {name: archive.read(name) for name in archive.namelist()}

    manifest_files = dict(original_files)
    manifest = json.loads(manifest_files["manifest.json"])
    manifest["materials"][0]["actual_material"]["evidence"]["sha256"] = "8" * 64
    context = {
        key: value
        for key, value in manifest.items()
        if key not in {"candidate_context_hash", "checksum_scope"}
    }
    manifest["candidate_context_hash"] = sha256_hex(canonical_json_bytes(context))
    manifest_files["manifest.json"] = canonical_json_bytes(manifest)
    with pytest.raises(ArtifactError, match="manifest differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, manifest_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    index_files = dict(original_files)
    index = json.loads(index_files[CAM_CANDIDATE_PROGRAM_INDEX_PATH])
    index["materials"][0]["source_material"]["version"] = "attacker-source-v2"
    index_files[CAM_CANDIDATE_PROGRAM_INDEX_PATH] = canonical_json_bytes(index)
    _rehash_artifact_and_manifest(index_files, CAM_CANDIDATE_PROGRAM_INDEX_PATH)
    with pytest.raises(ArtifactError, match="program index differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, index_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    setup_files = dict(original_files)
    instructions = json.loads(setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH])
    instructions["setups"][0]["stock"]["actual_material"]["id"] = "substitute-board"
    setup_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH] = canonical_json_bytes(instructions)
    _rehash_artifact_and_manifest(setup_files, CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH)
    with pytest.raises(ArtifactError, match="setup instructions"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, setup_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    report_files = dict(original_files)
    report = json.loads(report_files[CAM_CANDIDATE_REPORT_PATH])
    report["postprocessor_round_trip"]["programs"][0]["material_binding"]["actual_material"][
        "version"
    ] = "substitute-lot"
    report_files[CAM_CANDIDATE_REPORT_PATH] = canonical_json_bytes(report)
    _rehash_artifact_and_manifest(report_files, CAM_CANDIDATE_REPORT_PATH)
    with pytest.raises(ArtifactError, match="cutting program report differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, report_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    toolpath_files = dict(original_files)
    toolpath = json.loads(toolpath_files[CAM_CANDIDATE_TOOLPATH_PATH])
    del toolpath["execution_context"]["setups"][0]["material_evidence_sha256"]
    toolpath_files[CAM_CANDIDATE_TOOLPATH_PATH] = canonical_json_bytes(toolpath)
    _rehash_artifact_and_manifest(toolpath_files, CAM_CANDIDATE_TOOLPATH_PATH)
    with pytest.raises(ArtifactError, match="bound setup has an unexpected structure"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, toolpath_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )


def test_reader_rejects_rehashed_toolpath_schema_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The toolpath JSON trust boundary is closed, even for canonical JSON."""

    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    toolpath_payload = json.loads(files[CAM_CANDIDATE_TOOLPATH_PATH])
    toolpath_payload["unreviewed_extension"] = {"machine_start_authorized": True}
    files[CAM_CANDIDATE_TOOLPATH_PATH] = canonical_json_bytes(toolpath_payload)
    _rehash_artifact_and_manifest(files, CAM_CANDIDATE_TOOLPATH_PATH)

    with pytest.raises(ArtifactError, match="unexpected structure"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )


def test_reader_rejects_nonzero_drill_point_length_in_toolpath_and_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        original_files = {name: archive.read(name) for name in archive.namelist()}

    toolpath_files = dict(original_files)
    toolpath_payload = json.loads(toolpath_files[CAM_CANDIDATE_TOOLPATH_PATH])
    toolpath_payload["execution_context"]["tool_bindings"][0]["drill_point_length_um"] = 1
    toolpath_files[CAM_CANDIDATE_TOOLPATH_PATH] = canonical_json_bytes(toolpath_payload)
    _rehash_artifact_and_manifest(toolpath_files, CAM_CANDIDATE_TOOLPATH_PATH)
    with pytest.raises(ArtifactError, match="toolpath document has an invalid contract"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, toolpath_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    profile_files = dict(original_files)
    production_profile = json.loads(profile_files[CAM_CANDIDATE_MACHINE_PROFILE_PATH])
    production_profile["payload"]["tools"][0]["drill_point_length_um"] = 1
    production_profile["payload_sha256"] = sha256_hex(
        canonical_json_bytes(production_profile["payload"])
    )
    profile_files[CAM_CANDIDATE_MACHINE_PROFILE_PATH] = canonical_json_bytes(production_profile)
    _rehash_artifact_and_manifest(profile_files, CAM_CANDIDATE_MACHINE_PROFILE_PATH)
    with pytest.raises(ArtifactError, match="embedded production machine profile is invalid"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, profile_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )


def test_setup_instructions_follow_program_order_not_canonical_setup_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A two-sided B-to-A run must not be relabelled A-to-B in the handoff."""

    toolpaths, profile, _ = _fixture(monkeypatch)
    assert len(toolpaths.programs) == 2
    original_setup = toolpaths.execution_context.setups[0]
    setup_a = replace(
        original_setup,
        setup_id="setup:01:A",
        stock_id="sheet-a",
        side=Side.A,
        source_setup_sha256="a" * 64,
        orientation="A_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT",
    )
    setup_b = replace(
        original_setup,
        setup_id="setup:02:B",
        stock_id="sheet-b",
        side=Side.B,
        source_setup_sha256="b" * 64,
        orientation="B_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT",
    )
    context = replace(
        toolpaths.execution_context,
        # Context serialization is canonical A-to-B, while execution is B-to-A.
        setups=(setup_a, setup_b),
    )
    planned_programs = (
        replace(toolpaths.programs[0], setup_id=setup_b.setup_id),
        replace(toolpaths.programs[1], setup_id=setup_a.setup_id),
    )
    reordered_toolpaths = replace(
        toolpaths,
        execution_context=context,
        programs=planned_programs,
    )
    machine_programs = LinuxCNCProductionPostprocessor(profile.postprocessor_profile).generate(
        reordered_toolpaths
    )

    instructions = candidate_package._build_setup_instructions(
        reordered_toolpaths,
        machine_programs,
        profile,
        profile.postprocessor_profile,
    )
    assert [row["setup_id"] for row in instructions["setups"]] == [
        setup_b.setup_id,
        setup_a.setup_id,
    ]
    assert [row["setup_order"] for row in instructions["setups"]] == [1, 2]
    assert [
        order for row in instructions["setups"] for order in row["program_execution_orders"]
    ] == [1, 2]
    assert [row["setup_id"] for row in instructions["program_sequence"]] == [
        setup_b.setup_id,
        setup_a.setup_id,
    ]

    with pytest.raises(ArtifactError, match="has no executable program"):
        candidate_package._build_setup_instructions(
            reordered_toolpaths,
            machine_programs[:1],
            profile,
            profile.postprocessor_profile,
        )


def test_reader_rejects_rehashed_source_profile_setup_instructions_and_path_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        original_files = {name: archive.read(name) for name in archive.namelist()}

    source_files = dict(original_files)
    source_profile = json.loads(source_files[CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH])
    source_profile["name"] = "Attacker rewritten validation profile"
    source_files[CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH] = canonical_json_bytes(source_profile)
    manifest = json.loads(source_files["manifest.json"])
    manifest["source_machine_profile"]["sha256"] = sha256_hex(
        source_files[CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH]
    )
    manifest["source_machine_profile"]["size_bytes"] = len(
        source_files[CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH]
    )
    source_files["manifest.json"] = canonical_json_bytes(manifest)
    _rehash_artifact_and_manifest(
        source_files,
        CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
    )
    with pytest.raises(ArtifactError, match="trusted reviewed profile"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, source_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    instruction_files = dict(original_files)
    instructions = json.loads(instruction_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH])
    instructions["required_preflight_checks"][0] = "SKIP_CHECKSUM_VERIFICATION"
    instruction_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH] = canonical_json_bytes(instructions)
    manifest = json.loads(instruction_files["manifest.json"])
    manifest["setup_instructions"]["sha256"] = sha256_hex(
        instruction_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH]
    )
    manifest["setup_instructions"]["size_bytes"] = len(
        instruction_files[CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH]
    )
    instruction_files["manifest.json"] = canonical_json_bytes(manifest)
    _rehash_artifact_and_manifest(
        instruction_files,
        CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
    )
    with pytest.raises(ArtifactError, match="setup instructions"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, instruction_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )

    runtime_mutations = (
        ("homing_preflight_policy", None),
        ("g8_radius_mode_verified", False),
        ("m49_feed_and_spindle_overrides_disabled_verified", False),
        ("run_from_line_disabled_verified", False),
        ("full_restart_after_abort_required", False),
        ("m6_preserves_bound_tool_table_verified", False),
        ("m6_preserves_bound_wcs_table_verified", False),
        ("feed_spindle_override_evidence_sha256", "b" * 64),
        ("program_restart_evidence_sha256", "c" * 64),
        ("m6_tool_table_evidence_sha256", "d" * 64),
        ("m6_wcs_table_evidence_sha256", "e" * 64),
    )
    for field, value in runtime_mutations:
        runtime_files = dict(original_files)
        runtime_profile = json.loads(runtime_files[CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH])
        if value is None:
            del runtime_profile[field]
        else:
            runtime_profile[field] = value
        runtime_files[CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH] = canonical_json_bytes(
            runtime_profile
        )
        _rehash_artifact_and_manifest(
            runtime_files,
            CAM_CANDIDATE_POSTPROCESSOR_PROFILE_PATH,
        )
        with pytest.raises(
            ArtifactError,
            match="production machine profile is invalid|postprocessor profile differs",
        ):
            read_and_verify_cam_candidate_package(
                _rewrite_zip(result.zip_bytes, runtime_files),
                base_design_review_bundle=b"verified-review-bundle",
                allow_test_only=True,
            )

    alias_files = dict(original_files)
    manifest = json.loads(alias_files["manifest.json"])
    manifest["production_machine_profile"]["path"] = CAM_CANDIDATE_MACHINE_PROFILE_PATH
    context = {
        key: value
        for key, value in manifest.items()
        if key not in {"candidate_context_hash", "checksum_scope"}
    }
    manifest["candidate_context_hash"] = sha256_hex(canonical_json_bytes(context))
    alias_files["manifest.json"] = canonical_json_bytes(manifest)
    with pytest.raises(ArtifactError, match="manifest differs"):
        read_and_verify_cam_candidate_package(
            _rewrite_zip(result.zip_bytes, alias_files),
            base_design_review_bundle=b"verified-review-bundle",
            allow_test_only=True,
        )


def test_toolpath_json_uses_artifact_capacity_not_small_core_document_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dense shelving toolpaths may legitimately exceed small metadata JSON."""

    monkeypatch.setattr(candidate_package, "MAX_CORE_DOCUMENT_BYTES", 256)
    dense_payload = bytes(range(256)) * 4

    toolpath_zip = _rewrite_zip(
        b"unused",
        {CAM_CANDIDATE_TOOLPATH_PATH: dense_payload},
    )
    with zipfile.ZipFile(io.BytesIO(toolpath_zip)) as archive:
        candidate_package._validate_zip_envelope(archive, archive.infolist())

    metadata_zip = _rewrite_zip(
        b"unused",
        {CAM_CANDIDATE_PROGRAM_INDEX_PATH: dense_payload},
    )
    with (
        zipfile.ZipFile(io.BytesIO(metadata_zip)) as archive,
        pytest.raises(ArtifactError, match="invalid size"),
    ):
        candidate_package._validate_zip_envelope(archive, archive.infolist())


def test_builder_requires_an_intact_loaded_profile_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    forged_receipt = replace(profile, payload_sha256="0" * 64)

    with pytest.raises(ArtifactError, match="receipt differs"):
        build_cam_candidate_bundle(
            b"verified-review-bundle",
            toolpaths=toolpaths,
            programs=programs,
            production_profile=forged_receipt,
        )


def test_reader_rejects_unbound_base_and_builder_blocks_failed_independent_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )
    original = candidate_package._verified_base_binding(b"verified-review-bundle")
    wrong = replace(original, bundle_sha256="f" * 64)
    monkeypatch.setattr(candidate_package, "_verified_base_binding", lambda _payload: wrong)
    with pytest.raises(ArtifactError, match="not bound"):
        read_and_verify_cam_candidate_package(
            result.zip_bytes,
            base_design_review_bundle=b"different-review-bundle",
            allow_test_only=True,
        )

    original_move = next(
        move for move in toolpaths.programs[0].moves if move.role is ProductionMoveRole.CUT
    )
    bad_move = replace(original_move, role=ProductionMoveRole.TAB_BRIDGE)
    bad_move_index = toolpaths.programs[0].moves.index(original_move)
    bad_program = replace(
        toolpaths.programs[0],
        moves=(
            *toolpaths.programs[0].moves[:bad_move_index],
            bad_move,
            *toolpaths.programs[0].moves[bad_move_index + 1 :],
        ),
    )
    bad_toolpaths = replace(toolpaths, programs=(bad_program, *toolpaths.programs[1:]))
    bad_machine_programs = LinuxCNCProductionPostprocessor(profile.postprocessor_profile).generate(
        bad_toolpaths
    )
    monkeypatch.setattr(candidate_package, "_verified_base_binding", lambda _payload: original)
    with pytest.raises(ArtifactError, match="independent cutting verification"):
        build_cam_candidate_bundle(
            b"verified-review-bundle",
            toolpaths=bad_toolpaths,
            programs=bad_machine_programs,
            production_profile=profile,
        )


def test_reader_public_envelope_guards_fail_before_versioned_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture(monkeypatch)
    valid_digest = _TEST_PRODUCER_SOURCE_MANIFEST_SHA256

    for kwargs in (
        {"allow_test_only": 1},
        {"require_current_implementations": 1},
    ):
        with pytest.raises(ArtifactError, match="explicit boolean"):
            _read_and_verify_cam_candidate_package(
                b"payload",
                base_design_review_bundle=b"verified-review-bundle",
                expected_producer_source_manifest_sha256=valid_digest,
                **kwargs,  # type: ignore[arg-type]
            )
    with pytest.raises(ArtifactError, match="expected producer"):
        _read_and_verify_cam_candidate_package(
            b"payload",
            base_design_review_bundle=b"verified-review-bundle",
            expected_producer_source_manifest_sha256="A" * 64,
        )
    for payload, message in ((b"", "empty"), (b"not-a-zip", "invalid")):
        with pytest.raises(ArtifactError, match=message):
            _read_and_verify_cam_candidate_package(
                payload,
                base_design_review_bundle=b"verified-review-bundle",
                expected_producer_source_manifest_sha256=valid_digest,
            )

    no_manifest = _rewrite_zip(b"unused", {"other.json": b"{}"})
    with pytest.raises(ArtifactError, match="no manifest"):
        _read_and_verify_cam_candidate_package(
            no_manifest,
            base_design_review_bundle=b"verified-review-bundle",
            expected_producer_source_manifest_sha256=valid_digest,
        )
    wrong_provenance = _rewrite_zip(
        b"unused",
        {"manifest.json": canonical_json_bytes({"software_provenance": []})},
    )
    with pytest.raises(ArtifactError, match="software provenance"):
        _read_and_verify_cam_candidate_package(
            wrong_provenance,
            base_design_review_bundle=b"verified-review-bundle",
            expected_producer_source_manifest_sha256=valid_digest,
        )


@pytest.mark.parametrize(
    "payload",
    (
        b"\xef\xbb\xbf{}",
        b'{"value":NaN}',
        b'{"value": 1}',
        b"[]",
        b"\xff",
    ),
)
def test_canonical_json_reader_rejects_every_noncanonical_encoding(payload: bytes) -> None:
    with pytest.raises(ArtifactError, match="not canonical UTF-8 JSON"):
        candidate_package._canonical_json_object(payload, label="fixture")


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        "/absolute",
        "back\\slash",
        "nul\x00byte",
        "drive:colon",
        "parent/../escape",
        "space not allowed",
        "x" * 256,
    ),
)
def test_candidate_path_validation_rejects_unsafe_aliases(value: object) -> None:
    with pytest.raises(ArtifactError, match="unsafe CAM candidate artifact path"):
        candidate_package._validate_candidate_path(value)


def test_zip_envelope_rejects_empty_comments_noncanonical_entries_and_resource_abuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_buffer = io.BytesIO()
    with zipfile.ZipFile(empty_buffer, "w"):
        pass
    with (
        zipfile.ZipFile(io.BytesIO(empty_buffer.getvalue())) as archive,
        pytest.raises(ArtifactError, match="invalid file count"),
    ):
        candidate_package._validate_zip_envelope(archive, archive.infolist())

    comment_buffer = io.BytesIO()
    with zipfile.ZipFile(comment_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b"comment"
        archive.writestr("one.json", b"{}")
    with (
        zipfile.ZipFile(io.BytesIO(comment_buffer.getvalue())) as archive,
        pytest.raises(ArtifactError, match="comments are not canonical"),
    ):
        candidate_package._validate_zip_envelope(archive, archive.infolist())

    stored_buffer = io.BytesIO()
    with zipfile.ZipFile(stored_buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo("one.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, b"{}")
    with (
        zipfile.ZipFile(io.BytesIO(stored_buffer.getvalue())) as archive,
        pytest.raises(ArtifactError, match="non-canonical entry"),
    ):
        candidate_package._validate_zip_envelope(archive, archive.infolist())

    zero_zip = _rewrite_zip(b"unused", {"empty.json": b""})
    with (
        zipfile.ZipFile(io.BytesIO(zero_zip)) as archive,
        pytest.raises(ArtifactError, match="invalid size"),
    ):
        candidate_package._validate_zip_envelope(archive, archive.infolist())

    tiny_zip = _rewrite_zip(b"unused", {"one.json": b"{}"})
    with zipfile.ZipFile(io.BytesIO(tiny_zip)) as archive:
        monkeypatch.setattr(candidate_package, "MAX_CAM_CANDIDATE_UNCOMPRESSED_BYTES", 1)
        with pytest.raises(ArtifactError, match="uncompressed size"):
            candidate_package._validate_zip_envelope(archive, archive.infolist())
    with zipfile.ZipFile(io.BytesIO(tiny_zip)) as archive:
        monkeypatch.setattr(candidate_package, "MAX_CAM_CANDIDATE_UNCOMPRESSED_BYTES", 64)
        monkeypatch.setattr(candidate_package, "MAX_CAM_CANDIDATE_COMPRESSION_RATIO", 0.1)
        with pytest.raises(ArtifactError, match="compression ratio"):
            candidate_package._validate_zip_envelope(archive, archive.infolist())


def test_manifest_and_artifact_inventory_guards_reject_rehashed_shape_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)
    result = build_cam_candidate_bundle(
        b"verified-review-bundle",
        toolpaths=toolpaths,
        programs=programs,
        production_profile=profile,
    )

    missing = dict(result.manifest)
    missing.pop("status")
    with pytest.raises(ArtifactError, match="unexpected structure"):
        candidate_package._validate_manifest_shape(missing)

    for key, value, message in (
        ("software_provenance", [], "implementation is unsupported"),
        ("status", "FORGED", "unsafe or unsupported claims"),
        ("controller", [], "controller must be an object"),
        ("candidate_context_hash", "0" * 64, "context hash mismatch"),
    ):
        mutated = json.loads(canonical_json_bytes(result.manifest))
        mutated[key] = value
        if key != "candidate_context_hash":
            _rehash_manifest_context({"manifest.json": b""}, mutated)
        with pytest.raises(ArtifactError, match=message):
            candidate_package._validate_manifest_shape(mutated)

    entries = result.manifest["artifacts"]
    with pytest.raises(ArtifactError, match="non-empty array"):
        candidate_package._validate_artifact_entries(None)
    with pytest.raises(ArtifactError, match="entry is invalid"):
        candidate_package._validate_artifact_entries([None])

    bad_metadata = json.loads(canonical_json_bytes(entries))
    bad_metadata[0]["size_bytes"] = 0
    with pytest.raises(ArtifactError, match="metadata is invalid"):
        candidate_package._validate_artifact_entries(bad_metadata)

    with pytest.raises(ArtifactError, match="paths are not canonical"):
        candidate_package._validate_artifact_entries(list(reversed(entries)))

    duplicate = json.loads(canonical_json_bytes(entries))
    duplicate.insert(1, dict(duplicate[0]))
    with pytest.raises(ArtifactError, match="duplicate path aliases"):
        candidate_package._validate_artifact_entries(duplicate)

    incomplete = [entry for entry in entries if not str(entry["path"]).endswith(".ngc")]
    with pytest.raises(ArtifactError, match="inventory is incomplete"):
        candidate_package._validate_artifact_entries(incomplete)

    with pytest.raises(ArtifactError, match="identity is unsupported"):
        candidate_package._validate_artifact_identity(
            {
                "path": "unknown.bin",
                "media_type": "application/octet-stream",
                "role": "UNKNOWN",
            }
        )


def test_low_level_canonical_guards_and_svg_active_content_fail_closed() -> None:
    artifact = candidate_package.ArtifactFile("b", b"1", "text/plain", "TEST")
    other = candidate_package.ArtifactFile("a", b"1", "text/plain", "TEST")
    for values in (
        (artifact, artifact),
        (artifact, replace(artifact, path="B")),
        (artifact, other),
    ):
        with pytest.raises(ArtifactError):
            candidate_package._require_unique_paths(values)

    for payload, message in (
        (b"", "non-empty bounded bytes"),
        (b"\xff", "UTF-8 SVG"),
        (b"<svg><script/></svg>", "active SVG content"),
    ):
        with pytest.raises(ArtifactError, match=message):
            candidate_package._validate_backplot(payload)

    guard_calls = (
        lambda: candidate_package._exact_object([], {"x"}, "value"),
        lambda: candidate_package._required_list({}, "value"),
        lambda: candidate_package._required_string(" whitespace ", "value"),
        lambda: candidate_package._required_hash("A" * 64, "value"),
        lambda: candidate_package._required_int(True, "value"),
        lambda: candidate_package._required_bool(1, "value"),
    )
    for call in guard_calls:
        with pytest.raises(ArtifactError):
            call()


def test_builder_rejects_a_caller_supplied_backplot_that_is_not_independent_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolpaths, profile, programs = _fixture(monkeypatch)

    with pytest.raises(ArtifactError, match="backplot differs"):
        build_cam_candidate_bundle(
            b"verified-review-bundle",
            toolpaths=toolpaths,
            programs=programs,
            production_profile=profile,
            cutting_backplot_svg=b"<svg/>",
        )
