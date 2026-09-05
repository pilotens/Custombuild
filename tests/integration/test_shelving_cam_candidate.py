"""Prove the supported shelving path reaches a verified CAM-candidate ZIP.

The machine facts in this test are conspicuously TEST_ONLY.  They exercise the
complete compiler contract but make no claim about a physical router or shop.
The production loader rejects this document unless the test-only opt-in is
supplied explicitly.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from typing import Any

import pytest
from app.design_service import bind_joint_retention, canonical_preview
from custombuild_manufacturing import (
    JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
    JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
    JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
    ArtifactFile,
    ManufacturingError,
    OperationsDocument,
    Point2D,
    TwoSidedRegistration,
    build_production_bundle,
    canonical_json_bytes,
    linuxcnc_reference_router_1325,
    sha256_hex,
)
from custombuild_manufacturing.cam_candidate_package import (
    CAM_CANDIDATE_BACKPLOT_PATH,
    CAM_CANDIDATE_REPORT_PATH,
    CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH,
    CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH,
    CAM_CANDIDATE_SOURCE_OPERATIONS_PATH,
    CAM_CANDIDATE_TOOLPATH_PATH,
)
from custombuild_manufacturing.production_machine_profile import (
    PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    ProductionMachineProfileError,
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
    GCodeSafetyError,
    validate_production_program,
)

from scripts.compile_cam_candidate import compile_cam_candidate
from scripts.verify_cam_candidate import (
    CAMCandidateVerificationError,
    _current_verifier_source_manifest_sha256,
    verify_cam_candidate,
)
from tests.integration.test_custom_shelving_export_chain import (
    PROJECT_ID,
    REVISION,
    _custom_request,
    _manifest_context,
    _stock_and_registration,
    _test_only_retention_contract,
)

_TEST_RETENTION_EVIDENCE = canonical_json_bytes(
    {
        "evidence_id": "test-only.shelving-cam.retention",
        "fixture_scope": "integration-test-only",
    }
)
_TEST_TOOL_TABLE_EVIDENCE_ID = "ci-linuxcnc-tool-table-snapshot"
_TEST_TOOL_TABLE_EVIDENCE_VERSION = "ci-snapshot-v1"
_TEST_TOOL_TABLE_EVIDENCE_SHA256 = sha256_hex(b"test LinuxCNC tool table")


def _digest(label: str) -> str:
    return sha256_hex(label.encode("utf-8"))


def _tamper_first_program(payload: bytes) -> bytes:
    """Repack one changed program with the otherwise canonical ZIP envelope."""

    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        files = [(info.filename, source.read(info.filename)) for info in source.infolist()]
    target = next(name for name, _ in files if name.endswith(".production.ngc"))
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for name, data in files:
            if name == target:
                data += b"(TRANSFER_TAMPER)\n"
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0x800
            archive.writestr(
                info,
                data,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    return output.getvalue()


def _review_bundle_with_production_clearance() -> Any:
    request = _custom_request()
    spec, unbound_design, _ = canonical_preview(
        request.model_dump(exclude_none=True),
        design_id=PROJECT_ID,
        revision=REVISION,
    )
    retention = _test_only_retention_contract(
        spec,
        unbound_design,
        evidence_bytes=_TEST_RETENTION_EVIDENCE,
    )
    bound_spec, design, _ = bind_joint_retention(spec, retention)
    source_stocks, _ = _stock_and_registration(bound_spec)

    # The production cutter verifier expands each outside tool-centre path by
    # cutter radius plus process accuracy.  An 8 mm nesting gap therefore has
    # 1.9 mm margin beyond this fixture's 6 mm cutter + 0.1 mm accuracy bound.
    # A 25 mm sheet margin also separates the holder from the registration pins.
    stocks = tuple(replace(stock, kerf_um=8_000, margin_um=25_000) for stock in source_stocks)
    registrations = {
        stock.stock_id: {
            sheet_index: TwoSidedRegistration(
                declaration_authority="CLIENT_DECLARED",
                method_id=f"test-safe-registration:{stock.stock_id}:{sheet_index}",
                fixture_method_version="test-fixture-v1",
                pin_diameter_um=6_000,
                position_tolerance_um=500,
                points=(
                    Point2D(5_000, 5_000),
                    Point2D(stock.width_um - 5_000, 5_000),
                ),
            )
            for sheet_index in range(stock.quantity)
        }
        for stock in stocks
    }
    machine = linuxcnc_reference_router_1325()
    return build_production_bundle(
        design,
        stock=stocks,
        machine=machine,
        context=_manifest_context(design, machine),
        include_step=True,
        include_validation_program=False,
        two_sided_registration_by_stock=registrations,
        additional_artifacts=(
            ArtifactFile(
                JOINT_RETENTION_SIGNED_EVIDENCE_PATH,
                _TEST_RETENTION_EVIDENCE,
                JOINT_RETENTION_SIGNED_EVIDENCE_MEDIA_TYPE,
                JOINT_RETENTION_SIGNED_EVIDENCE_ROLE,
            ),
        ),
    )


def _test_only_profile(source: OperationsDocument) -> bytes:
    source_machine = linuxcnc_reference_router_1325()
    assert source.machine_profile_id == source_machine.profile_id
    assert source.machine_profile_version == source_machine.version
    machine_id = "test-shop-router-1325"
    machine_version = "test-fixture-v1"
    controller_id = "linuxcnc"
    controller_version = "2.9.4"
    postprocessor_id = "test-shop-router-linuxcnc"
    postprocessor_version = "test-fixture-v1"
    # With LinuxCNC G43 active the machine endpoint is P + G5x + H. Keep the
    # 35-37 mm positive test H rows inside the router's negative-Z work range.
    machine_z0_um = -60_000
    supported_wcs = sorted({setup.wcs for setup in source.setups})

    postprocessor_profile = {
        "controller_id": controller_id,
        "controller_version": controller_version,
        "g4_p_seconds_dwell_verified": True,
        "g43_h_length_offset_verified": True,
        "g52_g92_offset_reset_evidence_id": "test-only-offset-reset",
        "g52_g92_offset_reset_evidence_sha256": _digest("test offset reset"),
        "g52_g92_offset_reset_evidence_version": "test-v1",
        "g52_g92_offset_reset_policy": G52_G92_OFFSET_RESET_POLICY,
        "g53_machine_coordinates_verified": True,
        "g53_tool_change_path": G53_TOOL_CHANGE_PATH_COMPLETE,
        "g53_tool_change_path_clearance_evidence_id": "test-only-clearance",
        "g53_tool_change_path_clearance_evidence_sha256": _digest("test clearance"),
        "g53_tool_change_path_clearance_evidence_version": "test-v1",
        "g53_tool_change_path_clearance_verified": True,
        "g92_1_clears_g52_g92_offsets_verified": True,
        "external_axis_offset_policy": EXTERNAL_AXIS_OFFSET_POLICY,
        "external_axis_offset_evidence_id": "test-only-external-axis-offsets",
        "external_axis_offset_evidence_sha256": _digest("test external axis offsets"),
        "external_axis_offset_evidence_version": "test-v1",
        "external_xyz_offsets_disabled_verified": True,
        "g8_radius_mode_verified": True,
        "g97_rpm_mode_verified": True,
        "feed_spindle_override_evidence_id": "test-only-disabled-overrides",
        "feed_spindle_override_evidence_sha256": _digest(
            "test disabled feed and spindle overrides"
        ),
        "feed_spindle_override_evidence_version": "test-v1",
        "feed_spindle_override_policy": FEED_SPINDLE_OVERRIDE_POLICY,
        "homing_preflight_evidence_id": "test-only-homing-interlock",
        "homing_preflight_evidence_sha256": _digest("test homing interlock"),
        "homing_preflight_evidence_version": "test-v1",
        "homing_preflight_policy": HOMING_PREFLIGHT_POLICY,
        "m3_clockwise_spindle_verified": True,
        "m49_feed_and_spindle_overrides_disabled_verified": True,
        "m52_p0_adaptive_feed_disabled_verified": True,
        "m53_p1_feed_hold_enabled_verified": True,
        "m6_preserves_axis_position": True,
        "m6_preserves_bound_tool_table_verified": True,
        "m6_tool_table_evidence_id": "test-only-m6-tool-table-preservation",
        "m6_tool_table_evidence_sha256": _digest("test M6 tool table preservation"),
        "m6_tool_table_evidence_version": "test-v1",
        "m6_tool_table_policy": M6_TOOL_TABLE_POLICY,
        "m6_tool_change_verified": True,
        "m6_wcs_table_evidence_id": "test-only-m6-wcs-table-preservation",
        "m6_wcs_table_evidence_sha256": _digest("test M6 WCS table preservation"),
        "m6_wcs_table_evidence_version": "test-v1",
        "m6_wcs_table_policy": M6_WCS_TABLE_POLICY,
        "m6_preserves_bound_wcs_table_verified": True,
        "metric_xyz_identity_kinematics_evidence_id": "test-only-metric-xyz-kinematics",
        "metric_xyz_identity_kinematics_evidence_sha256": _digest(
            "test metric XYZ identity kinematics"
        ),
        "metric_xyz_identity_kinematics_evidence_version": "test-v1",
        "metric_xyz_identity_kinematics_policy": METRIC_XYZ_IDENTITY_KINEMATICS_POLICY,
        "linear_units_mm_verified": True,
        "coordinates_xyz_verified": True,
        "identity_trivkins_verified": True,
        "exactly_three_joints_verified": True,
        "joint_0_x_1_y_2_z_verified": True,
        "no_extra_controlled_axes_verified": True,
        "m9_coolant_off_verified": True,
        "machine_profile_id": machine_id,
        "machine_profile_version": machine_version,
        "machine_x_max_um": source_machine.work_width_um,
        "machine_x_min_um": 0,
        "machine_y_max_um": source_machine.work_height_um,
        "machine_y_min_um": 0,
        "machine_z_max_um": 0,
        "machine_z_min_um": -source_machine.work_z_um,
        "profile_id": postprocessor_id,
        "program_restart_evidence_id": "test-only-program-restart-interlock",
        "program_restart_evidence_sha256": _digest("test program restart interlock"),
        "program_restart_evidence_version": "test-v1",
        "program_restart_policy": PROGRAM_RESTART_POLICY,
        "run_from_line_disabled_verified": True,
        "schema_version": "custombuild.linuxcnc-production-machine-profile.v1",
        "spindle_at_speed_evidence_id": "test-only-spindle-at-speed-interlock",
        "spindle_at_speed_evidence_sha256": _digest("test spindle at speed interlock"),
        "spindle_at_speed_evidence_version": "test-v1",
        "spindle_at_speed_motion_interlock_verified": True,
        "spindle_at_speed_policy": SPINDLE_AT_SPEED_POLICY,
        "spindle_at_speed_tolerance_ppm": 50_000,
        "continuous_spindle_speed_interlock_policy": (CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY),
        "continuous_spindle_speed_interlock_evidence_id": (
            "test-only-continuous-spindle-speed-interlock"
        ),
        "continuous_spindle_speed_interlock_evidence_sha256": _digest(
            "test continuous spindle speed interlock"
        ),
        "continuous_spindle_speed_interlock_evidence_version": "test-v1",
        "continuous_spindle_speed_feed_inhibit_verified": True,
        "spindle_feedback_source": "test-vfd-encoder-rpm-feedback",
        "spindle_spinup_ms": 2_500,
        "supported_wcs": supported_wcs,
        "tool_change_x_um": source_machine.work_width_um - 100_000,
        "tool_change_y_um": source_machine.work_height_um - 100_000,
        "tool_change_z_um": -1_000,
        "version": postprocessor_version,
        "all_xyz_homed_before_auto_verified": True,
        "full_restart_after_abort_required": True,
        "no_force_homing_disabled_verified": True,
        "real_spindle_feedback_verified": True,
        "vfd_fault_motion_inhibit_verified": True,
        "vfd_fault_spindle_stop_verified": True,
        "wcs_offsets": [
            {
                "machine_x0_um": 0,
                "machine_y0_um": 0,
                "machine_z0_um": machine_z0_um,
                "machine_xy_rotation_mdeg": 0,
                "wcs": wcs,
            }
            for wcs in supported_wcs
        ],
        "wcs_offsets_evidence_id": "test-only-wcs-offsets",
        "wcs_offsets_evidence_sha256": _digest("test wcs offsets"),
        "wcs_offsets_evidence_version": "test-v1",
        "wcs_offsets_verified": True,
    }

    through_setups = {operation.setup_id for operation in source.operations if operation.through}
    physical_sheets = sorted({(setup.stock_id, setup.sheet_index) for setup in source.setups})
    actual_material_by_sheet = {
        physical_sheet: {
            "id": f"ci-accepted-material-{index:03d}",
            "version": f"ci-lot-{index:03d}",
            "evidence_id": f"ci-material-certificate-{index:03d}",
            "evidence_version": "ci-v1",
            "evidence_sha256": _digest(f"test material certificate {index:03d}"),
        }
        for index, physical_sheet in enumerate(physical_sheets, start=1)
    }
    setups = []
    for setup in sorted(source.setups, key=lambda item: item.setup_id):
        through = setup.setup_id in through_setups
        actual_material = actual_material_by_sheet[(setup.stock_id, setup.sheet_index)]
        setups.append(
            {
                "fixture": {
                    "clearance_z_um": 8_000,
                    "fixture_id": "test-vacuum-and-registration-fixture",
                    "fixture_sha256": _digest("test fixture"),
                    "fixture_version": "test-v1",
                    "keep_out_policy": "UNINFLATED_XY_FOOTPRINTS_MAX_Z_BOUND",
                },
                "keep_out_zones": [
                    {
                        "height_um": zone.height_um,
                        "width_um": zone.width_um,
                        "x_um": zone.x_um,
                        "y_um": zone.y_um,
                    }
                    for zone in setup.keep_out_zones
                ],
                "machine_wcs_origin": {"x_um": 0, "y_um": 0},
                "machine_wcs_xy_rotation_mdeg": 0,
                "machine_wcs_z0_um": machine_z0_um,
                "source_material_id": setup.material_id,
                "source_material_version": setup.material_version,
                "material_id": actual_material["id"],
                "material_version": actual_material["version"],
                "material_evidence_id": actual_material["evidence_id"],
                "material_evidence_version": actual_material["evidence_version"],
                "material_evidence_sha256": actual_material["evidence_sha256"],
                "minimum_rapid_clearance_um": 2_000,
                "orientation": setup.orientation,
                "probe_method": "TOUCH_PROBE_STOCK_TOP_AND_XY_V1",
                "raw_allowance_um": 0,
                "reference_surface": "STOCK_TOP_Z0",
                "safe_z_um": source_machine.safe_z_um,
                "setup_id": setup.setup_id,
                "sheet_index": setup.sheet_index,
                "side": setup.side.value,
                "source_setup_sha256": sha256_hex(canonical_json_bytes(setup)),
                "source_to_wcs_xy_transform": "IDENTITY_STOCK_XY_TO_WCS_XY",
                "spoilboard_id": "test-spoilboard" if through else None,
                "spoilboard_sha256": _digest("test spoilboard") if through else None,
                "spoilboard_version": "test-v1" if through else None,
                "stock_height_um": setup.stock_height_um,
                "stock_id": setup.stock_id,
                "stock_thickness_um": setup.stock_thickness_um,
                "stock_width_um": setup.stock_width_um,
                # 0.3 mm commanded overtravel + 0.1 mm process uncertainty,
                # bounded by the production contract's 0.5 mm maximum.
                "through_cut_allowance_um": 500 if through else 0,
                "wcs": setup.wcs,
            }
        )

    tool_ids: dict[str, tuple[str, str]] = {}
    tools = []
    for tool_number, source_tool in enumerate(
        sorted(source.tools, key=lambda item: item.tool_id),
        start=1,
    ):
        actual_id = f"test-actual-{source_tool.tool_id.lower()}"
        actual_version = "test-measured-v1"
        tool_ids[source_tool.tool_id] = (actual_id, actual_version)
        tools.append(
            {
                "assembly_collision_radius_um": 10_000,
                "center_cutting": True,
                "controller_tool_number": tool_number,
                "cutting_length_um": source_tool.cutting_length_um,
                "drill_point_length_um": 0,
                "effective_diameter_um": source_tool.effective_diameter_um,
                "expected_length_offset_x_um": 0,
                "expected_length_offset_y_um": 0,
                "expected_length_offset_z_um": 35_000 + (tool_number - 1) * 1_000,
                "geometry": "FLAT_END_MILL",
                "length_offset_number": tool_number + 10,
                "measured_stickout_um": source_tool.cutting_length_um + 10_000,
                "minimum_holder_clearance_um": 5_000,
                "source_tool_id": source_tool.tool_id,
                "source_tool_sha256": sha256_hex(canonical_json_bytes(source_tool)),
                "source_tool_version": source_tool.version,
                "spindle_direction": "CW",
                "tool_table_evidence_id": _TEST_TOOL_TABLE_EVIDENCE_ID,
                "tool_table_evidence_sha256": _TEST_TOOL_TABLE_EVIDENCE_SHA256,
                "tool_table_evidence_version": _TEST_TOOL_TABLE_EVIDENCE_VERSION,
                "tool_id": actual_id,
                "tool_version": actual_version,
            }
        )

    setup_by_id = {setup.setup_id: setup for setup in source.setups}
    recipe_bindings = sorted(
        {
            (
                actual_material_by_sheet[
                    (
                        setup_by_id[operation.setup_id].stock_id,
                        setup_by_id[operation.setup_id].sheet_index,
                    )
                ]["id"],
                actual_material_by_sheet[
                    (
                        setup_by_id[operation.setup_id].stock_id,
                        setup_by_id[operation.setup_id].sheet_index,
                    )
                ]["version"],
                operation.tool_id,
                operation.kind.value,
                operation.tolerance_um,
            )
            for operation in source.operations
        }
    )
    recipes = []
    for (
        material_id,
        material_version,
        source_tool_id,
        operation_kind,
        tolerance_um,
    ) in recipe_bindings:
        actual_tool_id, actual_tool_version = tool_ids[source_tool_id]
        contour = operation_kind == "CONTOUR"
        recipes.append(
            {
                "accepted_tolerance_um": tolerance_um or 500,
                "approach_clearance_um": 2_000,
                "countersink_included_angle_mdeg": None,
                "countersink_top_diameter_um": None,
                "diameter_tolerance_um": 0,
                "entry_strategy": "PLUNGE",
                "feed_um_min": 1_200_000,
                "machine_profile_id": machine_id,
                "machine_profile_version": machine_version,
                "material_id": material_id,
                "material_version": material_version,
                "operation_kind": operation_kind,
                "peck_depth_um": 2_000,
                "plunge_um_min": 300_000,
                "process_accuracy_um": 50 if tolerance_um else 100,
                "recipe_id": (
                    f"test:{material_id}:{source_tool_id.lower()}:{operation_kind.lower()}"
                ),
                "spindle_rpm": 18_000,
                "stepdown_um": 3_000,
                "stepover_ppm": 400_000,
                "tab_height_um": 3_000 if contour else 0,
                "tab_width_um": 20_000 if contour else 0,
                "through_overtravel_um": 300 if contour else 0,
                "tool_id": actual_tool_id,
                "tool_version": actual_tool_version,
                "version": "test-v1",
            }
        )

    payload = {
        "acceptance": {
            "evidence_id": "TEST_ONLY_ACCEPTANCE",
            "evidence_sha256": _digest("test acceptance"),
            "evidence_version": "TEST_ONLY_V1",
            "status": "TEST_ONLY",
        },
        "machine": {
            "controller_id": controller_id,
            "controller_version": controller_version,
            "machine_profile_id": machine_id,
            "machine_profile_version": machine_version,
            "machine_x_max_um": source_machine.work_width_um,
            "machine_x_min_um": 0,
            "machine_y_max_um": source_machine.work_height_um,
            "machine_y_min_um": 0,
            "machine_z_max_um": 0,
            "machine_z_min_um": -source_machine.work_z_um,
            "max_feed_um_min": 5_000_000,
            "max_plunge_um_min": 1_000_000,
            "max_spindle_rpm": source_machine.max_spindle_rpm,
            "min_spindle_rpm": 6_000,
            "postprocessor_profile_id": postprocessor_id,
            "postprocessor_profile_sha256": sha256_hex(canonical_json_bytes(postprocessor_profile)),
            "postprocessor_profile_version": postprocessor_version,
            "recipe_catalog_version": "test-recipes-v1",
            "source_machine_profile_fingerprint": sha256_hex(canonical_json_bytes(source_machine)),
            "source_machine_profile_id": source.machine_profile_id,
            "source_machine_profile_version": source.machine_profile_version,
            "tool_catalog_version": "test-measured-tools-v1",
            "work_height_um": source_machine.work_height_um,
            "work_width_um": source_machine.work_width_um,
            "work_z_um": source_machine.work_z_um,
        },
        "postprocessor_profile": postprocessor_profile,
        "profile_class": "TEST_ONLY",
        "recipes": recipes,
        "setups": setups,
        "tools": tools,
    }
    return canonical_json_bytes(
        {
            "payload": payload,
            "payload_sha256": sha256_hex(canonical_json_bytes(payload)),
            "schema_version": PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
        }
    )


@pytest.mark.integration
@pytest.mark.cad
def test_custom_shelving_compiles_to_a_complete_linuxcnc_candidate() -> None:
    review = _review_bundle_with_production_clearance()
    source = review.operations
    assert source is not None
    assert len(source.operations) == 67
    assert len({operation.instance_id for operation in source.operations}) == 22
    assert sum(operation.kind.value == "GROOVE" for operation in source.operations) == 45
    assert sum(operation.through for operation in source.operations) == 22

    profile_document = _test_only_profile(source)
    rotated_profile = json.loads(profile_document)
    rotated_profile["payload"]["setups"][0]["machine_wcs_xy_rotation_mdeg"] = 1
    rotated_profile["payload_sha256"] = sha256_hex(canonical_json_bytes(rotated_profile["payload"]))
    with pytest.raises(ProductionMachineProfileError, match="zero WCS XY rotation"):
        load_production_machine_profile(
            canonical_json_bytes(rotated_profile),
            allow_test_only=True,
        )

    pointed_tool_profile = json.loads(profile_document)
    pointed_tool_profile["payload"]["tools"][0]["drill_point_length_um"] = 1
    pointed_tool_profile["payload_sha256"] = sha256_hex(
        canonical_json_bytes(pointed_tool_profile["payload"])
    )
    with pytest.raises(
        ProductionMachineProfileError,
        match="requires exactly zero drill point length",
    ):
        load_production_machine_profile(
            canonical_json_bytes(pointed_tool_profile),
            allow_test_only=True,
        )

    candidate, receipt = compile_cam_candidate(
        review.zip_bytes,
        profile_document,
        allow_test_only=True,
    )

    assert receipt.status == "CUTTING_CANDIDATE_GENERATED"
    assert receipt.mode == "EXECUTABLE_CAM_CANDIDATE"
    assert receipt.program_count == 9
    assert receipt.physical_cutting_authorized is False
    assert receipt.workshop_acceptance_required is True
    producer_source_manifest_sha256 = receipt.software_provenance["code_root"]["sha256"]
    assert isinstance(producer_source_manifest_sha256, str)
    verifier_source_manifest_sha256 = _current_verifier_source_manifest_sha256()
    assert len(candidate.programs) == len(candidate.toolpaths.programs) == 9
    assert sum(len(program.moves) for program in candidate.toolpaths.programs) > 7_000
    assert candidate.cutting_program_report["result"] == "PASS"
    manifest_materials = candidate.manifest["materials"]
    assert candidate.program_index["materials"] == manifest_materials
    assert candidate.setup_instructions["materials"] == manifest_materials
    assert candidate.cutting_program_report["materials"] == manifest_materials
    materials_by_sheet = {(row["stock_id"], row["sheet_index"]): row for row in manifest_materials}
    source_setups_by_sheet: dict[tuple[str, int], list[Any]] = {}
    for source_setup in source.setups:
        source_setups_by_sheet.setdefault(
            (source_setup.stock_id, source_setup.sheet_index), []
        ).append(source_setup)
    assert set(materials_by_sheet) == set(source_setups_by_sheet)
    for index, physical_sheet in enumerate(sorted(source_setups_by_sheet), start=1):
        source_setups = source_setups_by_sheet[physical_sheet]
        material = materials_by_sheet[physical_sheet]
        assert {(setup.material_id, setup.material_version) for setup in source_setups} == {
            (material["source_material"]["id"], material["source_material"]["version"])
        }
        assert material["actual_material"] == {
            "id": f"ci-accepted-material-{index:03d}",
            "version": f"ci-lot-{index:03d}",
            "evidence": {
                "id": f"ci-material-certificate-{index:03d}",
                "version": "ci-v1",
                "sha256": _digest(f"test material certificate {index:03d}"),
            },
        }
    assert len({row["actual_material"]["version"] for row in manifest_materials}) == len(
        manifest_materials
    )
    manifest_wcs_preservation = candidate.manifest["production_machine_profile"]["runtime_safety"][
        "wcs_table_preservation"
    ]
    assert manifest_wcs_preservation["policy"] == M6_WCS_TABLE_POLICY
    assert manifest_wcs_preservation["m6_preserves_bound_wcs_table_verified"] is True
    assert manifest_wcs_preservation["exact_raw_g5x_xyz_r_preservation_required"] is True
    assert (
        candidate.program_index["production_machine_profile"]["runtime_safety"][
            "wcs_table_preservation"
        ]
        == manifest_wcs_preservation
    )
    independent = candidate.cutting_program_report["independent_source_to_removal"]
    assert independent["status"] == "PASS"
    assert independent["issue_count"] == 0
    verification = verify_cam_candidate(
        candidate.zip_bytes,
        review.zip_bytes,
        expected_candidate_sha256=receipt.bundle_sha256,
        expected_producer_source_manifest_sha256=producer_source_manifest_sha256,
        expected_verifier_source_manifest_sha256=verifier_source_manifest_sha256,
        allow_test_only=True,
    )
    assert verification.candidate_sha256 == receipt.bundle_sha256
    assert verification.design_review_bundle_sha256 == receipt.design_review_bundle_sha256
    assert verification.program_count == 9
    assert verification.physical_cutting_authorized is False
    assert verification.workshop_acceptance_required is True

    with zipfile.ZipFile(io.BytesIO(candidate.zip_bytes)) as archive:
        paths = set(archive.namelist())
        assert archive.read(CAM_CANDIDATE_SOURCE_OPERATIONS_PATH) == source.to_json()
        assert archive.read(CAM_CANDIDATE_TOOLPATH_PATH) == candidate.toolpaths.to_json()
        assert CAM_CANDIDATE_SOURCE_MACHINE_PROFILE_PATH in paths
        assert CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH in paths
        setup_instructions = json.loads(archive.read(CAM_CANDIDATE_SETUP_INSTRUCTIONS_PATH))
        live_state = setup_instructions["expected_live_controller_state"]
        assert live_state["observations_embedded"] is False
        assert live_state["observation_timing"] == "IMMEDIATELY_BEFORE_EACH_PROGRAM_START"
        homing = live_state["home_axes"]
        assert homing["policy"] == HOMING_PREFLIGHT_POLICY
        assert homing["required_axes"] == ["X", "Y", "Z"]
        assert homing["candidate_program_performs_homing"] is False
        assert homing["operator_verification_required"] is True
        assert homing["all_xyz_homed_before_auto_verified"] is True
        assert homing["no_force_homing_disabled_verified"] is True
        spindle = live_state["spindle_and_overrides"]["spindle_at_speed"]
        assert spindle["policy"] == SPINDLE_AT_SPEED_POLICY
        assert spindle["feedback_source"] == "test-vfd-encoder-rpm-feedback"
        assert spindle["tolerance_ppm"] == 50_000
        assert spindle["dwell_role"] == SPINDLE_DWELL_ROLE
        assert spindle["g4_is_speed_proof"] is False
        assert spindle["actual_rpm_must_be_nonzero"] is True
        assert spindle["live_feedback_must_be_within_tolerance_before_feed"] is True
        assert spindle["real_feedback_verified"] is True
        assert spindle["motion_interlock_verified"] is True
        assert spindle["vfd_fault_motion_inhibit_verified"] is True
        assert spindle["vfd_fault_spindle_stop_verified"] is True
        continuous_spindle = spindle["continuous_cutting_feed_interlock"]
        assert continuous_spindle["policy"] == CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY
        assert continuous_spindle["continuous_spindle_speed_feed_inhibit_verified"] is True
        external_offsets = live_state["external_axis_offsets"]
        assert external_offsets["policy"] == EXTERNAL_AXIS_OFFSET_POLICY
        assert external_offsets["required_state"] == "P0_XYZ_EXTERNAL_OFFSETS_DISABLED"
        assert external_offsets["external_xyz_offsets_disabled_verified"] is True
        spindle_and_overrides = live_state["spindle_and_overrides"]
        assert spindle_and_overrides["policy"] == FEED_SPINDLE_OVERRIDE_POLICY
        assert spindle_and_overrides["required_program_states"] == [
            "G8",
            "G97",
            "M9",
            "M49",
            "M52 P0",
            "M53 P1",
        ]
        assert spindle_and_overrides["m49_feed_and_spindle_overrides_disabled_verified"] is True
        assert spindle_and_overrides["g8_radius_mode_verified"] is True
        program_execution = live_state["program_execution"]
        assert program_execution["policy"] == PROGRAM_RESTART_POLICY
        assert program_execution["allowed_entry_point"] == "PROGRAM_START_ONLY"
        assert program_execution["run_from_line_disabled_verified"] is True
        assert program_execution["full_restart_after_abort_required"] is True
        tool_change = live_state["tool_change_and_tool_table"]
        assert tool_change["policy"] == M6_TOOL_TABLE_POLICY
        assert tool_change["m6_tool_change_verified"] is True
        assert tool_change["m6_preserves_axis_position"] is True
        assert tool_change["m6_preserves_bound_tool_table_verified"] is True
        assert tool_change["automatic_probe_or_remap_may_mutate_bound_h_row"] is False
        assert tool_change["required_continuity"] == (
            "EXACT_BOUND_T_AND_H_XYZ_TOOL_TABLE_ROW_FROM_PREFLIGHT_THROUGH_G43"
        )
        wcs_table = live_state["wcs_table"]
        assert (
            wcs_table["live_values_must_remain_equal_expected_through_m6_and_wcs_selection"] is True
        )
        wcs_preservation = wcs_table["m6_preservation"]
        assert wcs_preservation["policy"] == M6_WCS_TABLE_POLICY
        assert wcs_preservation["m6_preserves_bound_wcs_table_verified"] is True
        assert wcs_preservation["exact_raw_g5x_xyz_r_preservation_required"] is True
        assert wcs_preservation["automatic_probe_or_remap_may_mutate_bound_g5x_row"] is False
        assert wcs_preservation["required_continuity"] == (
            "EXACT_RAW_G5X_XYZ_AND_R_FROM_PREFLIGHT_THROUGH_POST_M6_WCS_SELECTION"
        )
        assert {
            "VERIFY_NO_FORCE_HOMING_0_ALL_XYZ_HOMED_BEFORE_AUTO_AND_G53",
            "VERIFY_M6_REMAP_AND_AUTO_PROBE_PRESERVE_EXACT_BOUND_T_AND_H_TOOL_TABLE_ROW",
            "VERIFY_M6_REMAP_AND_AUTO_PROBE_PRESERVE_EXACT_RAW_G5X_XYZ_AND_R_UNTIL_WCS_SELECTION",
            "VERIFY_SOURCE_MATERIAL_AND_EXACT_ACTUAL_MATERIAL_LOT_EVIDENCE",
            "VERIFY_P0_XYZ_EXTERNAL_AXIS_OFFSETS_DISABLED",
            "VERIFY_G8_RADIUS_MODE_AND_M49_FEED_SPINDLE_OVERRIDES_DISABLED",
            "VERIFY_PROGRAM_START_ONLY_RUN_FROM_LINE_DISABLED_FULL_RESTART_AFTER_ABORT",
            "VERIFY_G4_IS_MINIMUM_DWELL_NOT_SPINDLE_SPEED_PROOF",
            "VERIFY_ACTUAL_NONZERO_SPINDLE_RPM_WITHIN_PROFILE_PPM_BEFORE_FEED",
            "VERIFY_CONTINUOUS_ACTUAL_RPM_INTERLOCK_INHIBITS_CUTTING_FEED_OUTSIDE_TOLERANCE",
            "VERIFY_VFD_FAULT_INHIBITS_MOTION_AND_STOPS_SPINDLE",
        } <= set(setup_instructions["required_preflight_checks"])
        setup_rows = setup_instructions["setups"]
        program_rows = setup_instructions["program_sequence"]
        assert [
            (execution_order, setup["setup_id"])
            for setup in setup_rows
            for execution_order in setup["program_execution_orders"]
        ] == [(program["execution_order"], program["setup_id"]) for program in program_rows]
        for setup in setup_rows:
            physical_sheet = (setup["stock"]["stock_id"], setup["stock"]["sheet_index"])
            material = materials_by_sheet[physical_sheet]
            assert setup["stock"]["source_material"] == material["source_material"]
            assert setup["stock"]["actual_material"] == material["actual_material"]
            assert setup["coordinate_registration"]["machine_wcs_origin"]["xy_rotation_mdeg"] == 0
            for tool in setup["tools"]:
                assert tool["geometry"] == "FLAT_END_MILL"
                assert tool["drill_point_length_um"] == 0
                assert tool["expected_length_offset_um"] == {
                    "x": 0,
                    "y": 0,
                    "z": 35_000 + (tool["controller_tool_number"] - 1) * 1_000,
                }
                assert tool["tool_table_evidence"] == {
                    "id": _TEST_TOOL_TABLE_EVIDENCE_ID,
                    "version": _TEST_TOOL_TABLE_EVIDENCE_VERSION,
                    "sha256": _TEST_TOOL_TABLE_EVIDENCE_SHA256,
                }
        for program_row in program_rows:
            assert program_row["tool"]["expected_length_offset_um"] == {
                "x": 0,
                "y": 0,
                "z": 35_000,
            }
            assert program_row["tool"]["tool_table_evidence"]["sha256"] == (
                _TEST_TOOL_TABLE_EVIDENCE_SHA256
            )
        assert all(
            row["tool"]["drill_point_length_um"] == 0 for row in candidate.program_index["programs"]
        )
        material_bindings = {
            canonical_json_bytes(
                {
                    "source_material": row["source_material"],
                    "actual_material": row["actual_material"],
                }
            )
            for row in manifest_materials
        }
        assert all(
            canonical_json_bytes(row["material_binding"]) in material_bindings
            for row in candidate.cutting_program_report["postprocessor_round_trip"]["programs"]
        )
        setup_order_by_sheet_side = {
            (
                setup["stock"]["stock_id"],
                setup["stock"]["sheet_index"],
                setup["stock"]["side"],
            ): setup["setup_order"]
            for setup in setup_rows
        }
        two_sided_sheets = {
            (stock_id, sheet_index)
            for stock_id, sheet_index, side in setup_order_by_sheet_side
            if side == "B" and (stock_id, sheet_index, "A") in setup_order_by_sheet_side
        }
        assert two_sided_sheets
        assert all(
            setup_order_by_sheet_side[(*sheet, "B")] < setup_order_by_sheet_side[(*sheet, "A")]
            for sheet in two_sided_sheets
        )
        assert (
            setup_instructions["program_sequence"][-1]["sheet_release_state_after_program"]
            == "SHEET_RELEASED_NO_FURTHER_PROGRAMS"
        )
        assert CAM_CANDIDATE_REPORT_PATH in paths
        assert CAM_CANDIDATE_BACKPLOT_PATH in paths
        assert archive.read(CAM_CANDIDATE_BACKPLOT_PATH).startswith(b"<?xml")
        for expected_order, program in enumerate(candidate.programs, start=1):
            assert program.run_order == expected_order
            assert program.filename.startswith(f"{expected_order:03d}.")
            gcode = program.content.decode("ascii")
            assert "G53 G0 Z-1.000" in gcode
            assert gcode.count("G53 G0 Z-1.000") == 3
            assert "T1 M6" in gcode
            assert "G43 H11" in gcode
            assert "S18000 M3" in gcode
            lines = gcode.splitlines()
            # The post establishes the entire critical modal state before the
            # tool change and reasserts it after M6, which may run a remap.
            assert lines.count("G92.1") == 2
            assert not any(line.startswith("G52") for line in lines)
            assert not any(line.startswith("G92") and line != "G92.1" for line in lines)
            assert lines.count("G8") == 2
            assert lines.count("M49") == 2
            assert "M52 P0" in lines
            assert "M53 P1" in lines
            assert "(WCS_MACHINE_XY_ROTATION_MDEG=0)" in lines
            assert f"(HOMING_PREFLIGHT_POLICY={HOMING_PREFLIGHT_POLICY})" in lines
            assert f"(FEED_SPINDLE_OVERRIDE_POLICY={FEED_SPINDLE_OVERRIDE_POLICY})" in lines
            assert f"(PROGRAM_RESTART_POLICY={PROGRAM_RESTART_POLICY})" in lines
            assert f"(M6_TOOL_TABLE_POLICY={M6_TOOL_TABLE_POLICY})" in lines
            assert f"(M6_WCS_TABLE_POLICY={M6_WCS_TABLE_POLICY})" in lines
            assert f"(SPINDLE_AT_SPEED_POLICY={SPINDLE_AT_SPEED_POLICY})" in lines
            assert f"(EXTERNAL_AXIS_OFFSET_POLICY={EXTERNAL_AXIS_OFFSET_POLICY})" in lines
            assert (
                f"(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY="
                f"{CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY})"
            ) in lines
            assert "(SPINDLE_FEEDBACK_SOURCE=test-vfd-encoder-rpm-feedback)" in lines
            assert "(SPINDLE_AT_SPEED_TOLERANCE_PPM=50000)" in lines
            assert f"(SPINDLE_DWELL_ROLE={SPINDLE_DWELL_ROLE})" in lines
            assert "(EXPECTED_LENGTH_OFFSET_X_UM=0)" in lines
            assert "(EXPECTED_LENGTH_OFFSET_Y_UM=0)" in lines
            assert "(EXPECTED_LENGTH_OFFSET_Z_UM=35000)" in lines
            assert f"(TOOL_TABLE_EVIDENCE_SHA256={_TEST_TOOL_TABLE_EVIDENCE_SHA256})" in lines
            assert "(PHYSICAL_CUTTING_AUTHORIZED=FALSE)" in gcode

    mutated_h_program = candidate.programs[0].content.replace(
        b"G43 H11\n",
        b"G43 H12\n",
        1,
    )
    assert mutated_h_program != candidate.programs[0].content
    with pytest.raises(GCodeSafetyError, match="wrong G43 H length offset"):
        validate_production_program(
            mutated_h_program,
            document=candidate.toolpaths,
            program=candidate.toolpaths.programs[0],
            machine_profile=candidate.production_machine_profile,
        )

    transferred_tamper = candidate.zip_bytes[:-1] + bytes([candidate.zip_bytes[-1] ^ 1])
    with pytest.raises(CAMCandidateVerificationError, match="independently supplied"):
        verify_cam_candidate(
            transferred_tamper,
            review.zip_bytes,
            expected_candidate_sha256=receipt.bundle_sha256,
            expected_producer_source_manifest_sha256=producer_source_manifest_sha256,
            expected_verifier_source_manifest_sha256=verifier_source_manifest_sha256,
            allow_test_only=True,
        )

    repacked_tamper = _tamper_first_program(candidate.zip_bytes)
    with pytest.raises(ManufacturingError, match="artifact checksum mismatch"):
        verify_cam_candidate(
            repacked_tamper,
            review.zip_bytes,
            expected_candidate_sha256=sha256_hex(repacked_tamper),
            expected_producer_source_manifest_sha256=producer_source_manifest_sha256,
            expected_verifier_source_manifest_sha256=verifier_source_manifest_sha256,
            allow_test_only=True,
        )
