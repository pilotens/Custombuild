from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest
from custombuild_cam import MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex
from custombuild_manufacturing.production_machine_profile import (
    MAX_PRODUCTION_MACHINE_PROFILE_BYTES,
    PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    ProductionMachineProfileError,
    load_production_execution_context,
    load_production_machine_profile,
    production_machine_profile_job_binding,
    production_machine_profile_job_binding_json,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_HASH_D = "d" * 64
_HASH_E = "e" * 64


def _postprocessor_profile() -> dict[str, Any]:
    return {
        "controller_id": "linuxcnc",
        "controller_version": "2.9.4",
        "g4_p_seconds_dwell_verified": True,
        "g43_h_length_offset_verified": True,
        "g52_g92_offset_reset_evidence_id": "offset-reset-report-2026-09",
        "g52_g92_offset_reset_evidence_sha256": _HASH_B,
        "g52_g92_offset_reset_evidence_version": "1.0.0",
        "g52_g92_offset_reset_policy": "G92.1_CLEAR_AND_DO_NOT_RESTORE",
        "g92_1_clears_g52_g92_offsets_verified": True,
        "g53_machine_coordinates_verified": True,
        "g53_tool_change_path": ("G53_Z_TOOLCHANGE_XY_M6_G53_Z_THEN_ENTRY_XY_AT_GLOBAL_CLEARANCE"),
        "g53_tool_change_path_clearance_evidence_id": "clearance-aircut-2026-09",
        "g53_tool_change_path_clearance_evidence_sha256": _HASH_D,
        "g53_tool_change_path_clearance_evidence_version": "1.0.0",
        "g53_tool_change_path_clearance_verified": True,
        "g8_radius_mode_verified": True,
        "g97_rpm_mode_verified": True,
        "homing_preflight_evidence_id": "homing-interlock-2026-09",
        "homing_preflight_evidence_sha256": _HASH_D,
        "homing_preflight_evidence_version": "1.0.0",
        "homing_preflight_policy": "NO_FORCE_HOMING_0_ALL_XYZ_HOMED_BEFORE_AUTO",
        "all_xyz_homed_before_auto_verified": True,
        "no_force_homing_disabled_verified": True,
        "program_restart_evidence_id": "restart-interlock-2026-09",
        "program_restart_evidence_sha256": _HASH_B,
        "program_restart_evidence_version": "1.0.0",
        "program_restart_policy": (
            "PROGRAM_START_ONLY_RUN_FROM_LINE_DISABLED_FULL_RESTART_AFTER_ABORT"
        ),
        "m6_tool_table_evidence_id": "m6-tool-table-invariance-2026-09",
        "m6_tool_table_evidence_sha256": _HASH_E,
        "m6_tool_table_evidence_version": "1.0.0",
        "m6_tool_table_policy": "M6_PRESERVES_EXACT_BOUND_TOOL_TABLE",
        "m6_preserves_bound_tool_table_verified": True,
        "m6_wcs_table_evidence_id": "m6-wcs-table-invariance-2026-09",
        "m6_wcs_table_evidence_sha256": _HASH_D,
        "m6_wcs_table_evidence_version": "1.0.0",
        "m6_wcs_table_policy": "M6_PRESERVES_EXACT_BOUND_WCS_TABLE",
        "m6_preserves_bound_wcs_table_verified": True,
        "metric_xyz_identity_kinematics_evidence_id": "metric-xyz-kinematics-2026-09",
        "metric_xyz_identity_kinematics_evidence_sha256": _HASH_A,
        "metric_xyz_identity_kinematics_evidence_version": "1.0.0",
        "metric_xyz_identity_kinematics_policy": (
            "LINEAR_UNITS_MM_COORDINATES_XYZ_IDENTITY_TRIVKINS_"
            "JOINTS_3_"
            "JOINT_0_X_JOINT_1_Y_JOINT_2_Z_NO_EXTRA_AXES"
        ),
        "linear_units_mm_verified": True,
        "coordinates_xyz_verified": True,
        "identity_trivkins_verified": True,
        "exactly_three_joints_verified": True,
        "joint_0_x_1_y_2_z_verified": True,
        "no_extra_controlled_axes_verified": True,
        "run_from_line_disabled_verified": True,
        "full_restart_after_abort_required": True,
        "feed_spindle_override_evidence_id": "disabled-overrides-2026-09",
        "feed_spindle_override_evidence_sha256": _HASH_A,
        "feed_spindle_override_evidence_version": "1.0.0",
        "feed_spindle_override_policy": ("PROGRAM_DISABLES_FEED_AND_SPINDLE_OVERRIDES_WITH_M49"),
        "external_axis_offset_evidence_id": "disabled-eoffsets-2026-09",
        "external_axis_offset_evidence_sha256": _HASH_D,
        "external_axis_offset_evidence_version": "1.0.0",
        "external_axis_offset_policy": ("XYZ_OFFSET_AV_RATIO_ZERO_EOFFSETS_DISABLED"),
        "external_xyz_offsets_disabled_verified": True,
        "m3_clockwise_spindle_verified": True,
        "m49_feed_and_spindle_overrides_disabled_verified": True,
        "m52_p0_adaptive_feed_disabled_verified": True,
        "m53_p1_feed_hold_enabled_verified": True,
        "m6_preserves_axis_position": True,
        "m6_tool_change_verified": True,
        "m9_coolant_off_verified": True,
        "real_spindle_feedback_verified": True,
        "continuous_spindle_speed_feed_inhibit_verified": True,
        "continuous_spindle_speed_interlock_evidence_id": ("continuous-spindle-interlock-2026-09"),
        "continuous_spindle_speed_interlock_evidence_sha256": _HASH_E,
        "continuous_spindle_speed_interlock_evidence_version": "1.0.0",
        "continuous_spindle_speed_interlock_policy": (
            "ACTUAL_RPM_OUT_OF_TOLERANCE_INHIBITS_CUTTING_FEED_CONTINUOUSLY"
        ),
        "spindle_at_speed_evidence_id": "spindle-interlock-2026-09",
        "spindle_at_speed_evidence_sha256": _HASH_C,
        "spindle_at_speed_evidence_version": "1.0.0",
        "spindle_at_speed_motion_interlock_verified": True,
        "spindle_at_speed_policy": "ACTUAL_RPM_GT_0_WITHIN_TOLERANCE_BEFORE_FEED",
        "spindle_at_speed_tolerance_ppm": 50_000,
        "spindle_feedback_source": "spindle-encoder-rpm",
        "vfd_fault_motion_inhibit_verified": True,
        "vfd_fault_spindle_stop_verified": True,
        "machine_profile_id": "shop-router-01",
        "machine_profile_version": "3.2.1",
        "machine_x_max_um": 1_300_000,
        "machine_x_min_um": 0,
        "machine_y_max_um": 2_500_000,
        "machine_y_min_um": 0,
        "machine_z_max_um": 0,
        "machine_z_min_um": -100_000,
        "profile_id": "shop-router-01-linuxcnc",
        "schema_version": "custombuild.linuxcnc-production-machine-profile.v1",
        "spindle_spinup_ms": 2_500,
        "supported_wcs": ["G54", "G55"],
        "wcs_offsets": [
            {
                "machine_x0_um": 50_000,
                "machine_y0_um": 50_000,
                "machine_z0_um": -20_000,
                "machine_xy_rotation_mdeg": 0,
                "wcs": "G54",
            },
            {
                "machine_x0_um": 50_000,
                "machine_y0_um": 700_000,
                "machine_z0_um": -20_000,
                "machine_xy_rotation_mdeg": 0,
                "wcs": "G55",
            },
        ],
        "wcs_offsets_evidence_id": "wcs-probe-report-2026-09",
        "wcs_offsets_evidence_sha256": _HASH_C,
        "wcs_offsets_evidence_version": "1.0.0",
        "wcs_offsets_verified": True,
        "tool_change_x_um": 100_000,
        "tool_change_y_um": 2_400_000,
        "tool_change_z_um": -5_000,
        "version": "2.0.0",
    }


def _recipe(
    *,
    tool_id: str,
    operation_kind: str,
    countersink: bool = False,
) -> dict[str, Any]:
    contour = operation_kind == "CONTOUR"
    drill = operation_kind == "DRILL"
    return {
        "approach_clearance_um": 2_000,
        "countersink_included_angle_mdeg": 90_000 if countersink else None,
        "countersink_top_diameter_um": 12_000 if countersink else None,
        "diameter_tolerance_um": 100 if drill else 0,
        "entry_strategy": "PLUNGE",
        "feed_um_min": 1_200_000,
        "machine_profile_id": "shop-router-01",
        "machine_profile_version": "3.2.1",
        "material_id": "birch-ply-18",
        "material_version": "2026.09",
        "operation_kind": operation_kind,
        "peck_depth_um": 2_000,
        "plunge_um_min": 300_000,
        "process_accuracy_um": 100,
        "recipe_id": f"birch-{tool_id}-{operation_kind.lower()}",
        "spindle_rpm": 18_000,
        "stepdown_um": 3_000,
        "stepover_ppm": 400_000,
        "tab_height_um": 3_000 if contour else 0,
        "tab_width_um": 20_000 if contour else 0,
        "through_overtravel_um": 300 if contour else 0,
        "accepted_tolerance_um": 500,
        "tool_id": tool_id,
        "tool_version": "measured-2026.09.04",
        "version": "1.0.0",
    }


def _payload() -> dict[str, Any]:
    postprocessor = _postprocessor_profile()
    return {
        "acceptance": {
            "evidence_id": "TEST_ONLY_ACCEPTANCE",
            "evidence_sha256": _HASH_E,
            "evidence_version": "TEST_ONLY_V1",
            "status": "TEST_ONLY",
        },
        "machine": {
            "controller_id": "linuxcnc",
            "controller_version": "2.9.4",
            "machine_profile_id": "shop-router-01",
            "machine_profile_version": "3.2.1",
            "machine_x_max_um": 1_300_000,
            "machine_x_min_um": 0,
            "machine_y_max_um": 2_500_000,
            "machine_y_min_um": 0,
            "machine_z_max_um": 0,
            "machine_z_min_um": -100_000,
            "max_feed_um_min": 5_000_000,
            "max_plunge_um_min": 1_000_000,
            "max_spindle_rpm": 24_000,
            "min_spindle_rpm": 6_000,
            "postprocessor_profile_id": "shop-router-01-linuxcnc",
            "postprocessor_profile_sha256": sha256_hex(canonical_json_bytes(postprocessor)),
            "postprocessor_profile_version": "2.0.0",
            "recipe_catalog_version": "birch-recipes-2026.09",
            "source_machine_profile_fingerprint": _HASH_A,
            "source_machine_profile_id": "linuxcnc-reference-router-1325",
            "source_machine_profile_version": "1.0.0",
            "tool_catalog_version": "measured-tools-2026.09.04",
            "work_height_um": 2_500_000,
            "work_width_um": 1_300_000,
            "work_z_um": 100_000,
        },
        "postprocessor_profile": postprocessor,
        "profile_class": "TEST_ONLY",
        "recipes": [
            _recipe(
                tool_id="production-countersink-12",
                operation_kind="COUNTERSINK",
                countersink=True,
            ),
            _recipe(tool_id="production-drill-06", operation_kind="DRILL"),
            _recipe(tool_id="production-mill-06", operation_kind="CONTOUR"),
            _recipe(tool_id="production-mill-06", operation_kind="GROOVE"),
            _recipe(tool_id="production-mill-06", operation_kind="POCKET"),
        ],
        "setups": [
            {
                "fixture": {
                    "clearance_z_um": 8_000,
                    "fixture_id": "shop-vacuum-fixture-01",
                    "fixture_sha256": _HASH_B,
                    "fixture_version": "4.0.0",
                    "keep_out_policy": "UNINFLATED_XY_FOOTPRINTS_MAX_Z_BOUND",
                },
                "keep_out_zones": [
                    {
                        "height_um": 20_000,
                        "width_um": 20_000,
                        "x_um": 0,
                        "y_um": 0,
                    }
                ],
                "machine_wcs_origin": {"x_um": 50_000, "y_um": 50_000},
                "machine_wcs_z0_um": -20_000,
                "machine_wcs_xy_rotation_mdeg": 0,
                "source_material_id": "birch-ply-18",
                "source_material_version": "2026.09",
                "material_id": "birch-ply-18",
                "material_version": "2026.09",
                "material_evidence_id": "accepted-birch-stock-certificate",
                "material_evidence_version": "2026.09",
                "material_evidence_sha256": _HASH_A,
                "orientation": "A_SIDE_UP_LOWER_LEFT",
                "probe_method": "TOUCH_PROBE_XY_AND_STOCK_TOP_V2",
                "raw_allowance_um": 0,
                "reference_surface": "STOCK_TOP_Z0",
                "safe_z_um": 15_000,
                "minimum_rapid_clearance_um": 2_000,
                "spoilboard_id": "shop-spoilboard-01",
                "spoilboard_sha256": _HASH_D,
                "spoilboard_version": "1.0.0",
                "through_cut_allowance_um": 400,
                "setup_id": "setup:sheet:001:A",
                "sheet_index": 0,
                "side": "A",
                "source_setup_sha256": _HASH_C,
                "source_to_wcs_xy_transform": "IDENTITY_STOCK_XY_TO_WCS_XY",
                "stock_height_um": 600_000,
                "stock_id": "sheet",
                "stock_thickness_um": 18_000,
                "stock_width_um": 1_000_000,
                "wcs": "G54",
            }
        ],
        "tools": [
            {
                "assembly_collision_radius_um": 8_000,
                "center_cutting": True,
                "controller_tool_number": 1,
                "cutting_length_um": 30_000,
                "drill_point_length_um": 0,
                "effective_diameter_um": 6_000,
                "geometry": "DRILL",
                "length_offset_number": 11,
                "expected_length_offset_x_um": 0,
                "expected_length_offset_y_um": 0,
                "expected_length_offset_z_um": 40_000,
                "tool_table_evidence_id": "accepted-tool-table-snapshot-2026",
                "tool_table_evidence_version": "2026.09.04",
                "tool_table_evidence_sha256": _HASH_E,
                "measured_stickout_um": 40_000,
                "minimum_holder_clearance_um": 5_000,
                "source_tool_id": "T01",
                "source_tool_sha256": _HASH_C,
                "source_tool_version": "1.0.0",
                "spindle_direction": "CW",
                "tool_id": "production-drill-06",
                "tool_version": "measured-2026.09.04",
            },
            {
                "assembly_collision_radius_um": 10_000,
                "center_cutting": True,
                "controller_tool_number": 2,
                "cutting_length_um": 30_000,
                "drill_point_length_um": 0,
                "effective_diameter_um": 6_000,
                "geometry": "FLAT_END_MILL",
                "length_offset_number": 12,
                "expected_length_offset_x_um": 0,
                "expected_length_offset_y_um": 0,
                "expected_length_offset_z_um": 40_000,
                "tool_table_evidence_id": "accepted-tool-table-snapshot-2026",
                "tool_table_evidence_version": "2026.09.04",
                "tool_table_evidence_sha256": _HASH_E,
                "measured_stickout_um": 40_000,
                "minimum_holder_clearance_um": 5_000,
                "source_tool_id": "T02",
                "source_tool_sha256": _HASH_D,
                "source_tool_version": "1.0.0",
                "spindle_direction": "CW",
                "tool_id": "production-mill-06",
                "tool_version": "measured-2026.09.04",
            },
            {
                "assembly_collision_radius_um": 9_000,
                "center_cutting": True,
                "controller_tool_number": 3,
                "cutting_length_um": 15_000,
                "drill_point_length_um": 0,
                "effective_diameter_um": 12_000,
                "geometry": "COUNTERSINK",
                "length_offset_number": 13,
                "expected_length_offset_x_um": 0,
                "expected_length_offset_y_um": 0,
                "expected_length_offset_z_um": 25_000,
                "tool_table_evidence_id": "accepted-tool-table-snapshot-2026",
                "tool_table_evidence_version": "2026.09.04",
                "tool_table_evidence_sha256": _HASH_E,
                "measured_stickout_um": 25_000,
                "minimum_holder_clearance_um": 5_000,
                "source_tool_id": "T03",
                "source_tool_sha256": _HASH_E,
                "source_tool_version": "1.0.0",
                "spindle_direction": "CW",
                "tool_id": "production-countersink-12",
                "tool_version": "measured-2026.09.04",
            },
        ],
    }


def _document(payload: dict[str, Any] | None = None) -> bytes:
    value = payload or _payload()
    return canonical_json_bytes(
        {
            "payload": value,
            "payload_sha256": sha256_hex(canonical_json_bytes(value)),
            "schema_version": PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
        }
    )


def _resign_postprocessor(payload: dict[str, Any]) -> None:
    payload["machine"]["postprocessor_profile_sha256"] = sha256_hex(
        canonical_json_bytes(payload["postprocessor_profile"])
    )


def test_loads_one_atomic_test_only_profile_and_returns_immutable_receipt() -> None:
    source = _document()
    loaded = load_production_machine_profile(source, allow_test_only=True)

    assert loaded.canonical_document_json == source
    assert loaded.document_sha256 == sha256_hex(source)
    assert loaded.payload_sha256 == sha256_hex(loaded.canonical_payload_json)
    assert loaded.execution_context.machine_profile_id == "shop-router-01"
    assert loaded.execution_context.source_machine_profile_id.startswith("linuxcnc-reference")
    assert loaded.postprocessor_profile.work_width_um == (loaded.execution_context.work_width_um)
    assert loaded.postprocessor_profile.profile_id == "shop-router-01-linuxcnc"
    assert (
        load_production_execution_context(source, allow_test_only=True) == loaded.execution_context
    )
    with pytest.raises(FrozenInstanceError):
        loaded.payload_sha256 = _HASH_A  # type: ignore[misc]


def test_job_binding_is_canonical_and_complete() -> None:
    loaded = load_production_machine_profile(_document(), allow_test_only=True)
    binding = production_machine_profile_job_binding(loaded)

    assert canonical_json_bytes(binding) == production_machine_profile_job_binding_json(loaded)
    assert binding == {
        "acceptance": {
            "evidence_id": "TEST_ONLY_ACCEPTANCE",
            "evidence_sha256": _HASH_E,
            "evidence_version": "TEST_ONLY_V1",
            "status": "TEST_ONLY",
        },
        "execution_context_sha256": loaded.execution_context.fingerprint,
        "document_sha256": loaded.document_sha256,
        "payload_sha256": loaded.payload_sha256,
        "postprocessor_profile": {
            "config_sha256": loaded.postprocessor_profile.config_sha256,
            "profile_id": "shop-router-01-linuxcnc",
            "version": "2.0.0",
        },
        "profile_class": "TEST_ONLY",
        "schema_version": PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    }


def test_test_only_profile_requires_explicit_opt_in() -> None:
    with pytest.raises(ProductionMachineProfileError, match="test-harness opt-in"):
        load_production_machine_profile(_document())


def test_production_profile_requires_real_acceptance_and_rejects_placeholders() -> None:
    payload = _payload()
    payload["profile_class"] = "SERVER_OWNED_PRODUCTION"
    payload["acceptance"] = {
        "evidence_id": "shop-acceptance-2026-09",
        "evidence_sha256": _HASH_E,
        "evidence_version": "1.0.0",
        "status": "WORKSHOP_ACCEPTED",
    }
    load_production_machine_profile(_document(payload))

    payload["setups"][0]["fixture"]["fixture_id"] = "fixture-TBD"
    with pytest.raises(ProductionMachineProfileError, match="placeholder token: TBD"):
        load_production_machine_profile(_document(payload))


def test_production_profile_separates_source_and_actual_material_trust() -> None:
    payload = _payload()
    payload["profile_class"] = "SERVER_OWNED_PRODUCTION"
    payload["acceptance"] = {
        "evidence_id": "shop-acceptance-2026-09",
        "evidence_sha256": _HASH_E,
        "evidence_version": "1.0.0",
        "status": "WORKSHOP_ACCEPTED",
    }
    payload["setups"][0]["source_material_version"] = "screening-2026.1"
    loaded = load_production_machine_profile(_document(payload))
    assert loaded.execution_context.setups[0].source_material_version == "screening-2026.1"
    assert loaded.execution_context.setups[0].material_version == "2026.09"

    payload["setups"][0]["material_version"] = "screening-2026.1"
    payload["recipes"] = [
        {**recipe, "material_version": "screening-2026.1"} for recipe in payload["recipes"]
    ]
    with pytest.raises(ProductionMachineProfileError, match="placeholder token: SCREENING"):
        load_production_machine_profile(_document(payload))


def test_rejects_digest_mismatch_noncanonical_bytes_duplicate_keys_and_floats() -> None:
    source = _document()
    parsed = json.loads(source)
    parsed["payload_sha256"] = _HASH_A
    with pytest.raises(ProductionMachineProfileError, match="payload_sha256 mismatch"):
        load_production_machine_profile(canonical_json_bytes(parsed), allow_test_only=True)

    parsed = json.loads(source)
    parsed["request_override"] = True
    with pytest.raises(ProductionMachineProfileError, match="unknown fields: request_override"):
        load_production_machine_profile(canonical_json_bytes(parsed), allow_test_only=True)

    with pytest.raises(ProductionMachineProfileError, match="canonical JSON encoding"):
        load_production_machine_profile(source + b"\n", allow_test_only=True)

    duplicate = (
        b'{"payload":{},"payload_sha256":"'
        + b"0" * 64
        + b'","schema_version":"x","schema_version":"x"}'
    )
    with pytest.raises(ProductionMachineProfileError, match="duplicate JSON key"):
        load_production_machine_profile(duplicate, allow_test_only=True)

    float_source = source.replace(b'"work_width_um":1300000', b'"work_width_um":1300000.0')
    with pytest.raises(ProductionMachineProfileError, match="exact JSON integers"):
        load_production_machine_profile(float_source, allow_test_only=True)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("machine", "controller_id"), "fanuc", "unsupported production controller"),
        (("machine", "work_width_um"), 0, "work_width_um must be an integer"),
        (("machine", "min_spindle_rpm"), 25_000, "minimum spindle speed exceeds"),
        (
            ("postprocessor_profile", "g53_tool_change_path_clearance_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            (
                "postprocessor_profile",
                "g53_tool_change_path_clearance_evidence_sha256",
            ),
            "bad",
            "lowercase SHA-256",
        ),
        (
            ("postprocessor_profile", "wcs_offsets_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "g97_rpm_mode_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            (
                "postprocessor_profile",
                "m49_feed_and_spindle_overrides_disabled_verified",
            ),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "g8_radius_mode_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "external_xyz_offsets_disabled_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "run_from_line_disabled_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "m6_preserves_bound_tool_table_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "m6_preserves_bound_wcs_table_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "all_xyz_homed_before_auto_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "real_spindle_feedback_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            (
                "postprocessor_profile",
                "continuous_spindle_speed_feed_inhibit_verified",
            ),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "metric_xyz_identity_kinematics_policy"),
            "ALLOW_NONIDENTITY_OR_EXTRA_AXES",
            "native-unit/axis/kinematics policy",
        ),
        (
            (
                "postprocessor_profile",
                "metric_xyz_identity_kinematics_evidence_sha256",
            ),
            "bad",
            "lowercase SHA-256",
        ),
        (
            ("postprocessor_profile", "linear_units_mm_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "coordinates_xyz_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "identity_trivkins_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "exactly_three_joints_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "joint_0_x_1_y_2_z_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "no_extra_controlled_axes_verified"),
            False,
            "capabilities must be explicitly verified",
        ),
        (
            ("postprocessor_profile", "spindle_at_speed_tolerance_ppm"),
            0,
            "spindle_at_speed_tolerance_ppm",
        ),
        (
            (
                "postprocessor_profile",
                "wcs_offsets",
                0,
                "machine_xy_rotation_mdeg",
            ),
            1,
            "XY rotation must be exactly zero",
        ),
        (("setups", 0, "wcs"), "G59.1", "setup WCS"),
        (
            ("setups", 0, "machine_wcs_xy_rotation_mdeg"),
            1,
            "zero WCS XY rotation",
        ),
        (
            ("setups", 0, "source_to_wcs_xy_transform"),
            "ROTATE_90",
            "identity source-to-WCS",
        ),
        (("setups", 0, "source_setup_sha256"), "bad", "lowercase SHA-256"),
        (("setups", 0, "material_evidence_sha256"), "bad", "lowercase SHA-256"),
        (
            ("setups", 0, "fixture", "clearance_z_um"),
            15_000,
            "safe Z lacks the accepted minimum rapid clearance",
        ),
        (("tools", 0, "length_offset_number"), 0, "length_offset_number"),
        (
            ("tools", 0, "expected_length_offset_x_um"),
            1,
            "zero expected X/Y tool-length offsets",
        ),
        (
            ("tools", 0, "expected_length_offset_z_um"),
            True,
            "expected_length_offset_z_um must be an integer",
        ),
        (("tools", 0, "tool_table_evidence_sha256"), "bad", "lowercase SHA-256"),
        (("tools", 0, "measured_stickout_um"), 20_000, "cutting length cannot exceed"),
        (("tools", 0, "minimum_holder_clearance_um"), 0, "minimum_holder_clearance_um"),
        (("tools", 0, "source_tool_sha256"), "bad", "lowercase SHA-256"),
        (
            ("tools", 0, "drill_point_length_um"),
            1,
            "exactly zero drill point length",
        ),
        (("recipes", 1, "spindle_rpm"), 25_000, "outside machine spindle limits"),
        (("recipes", 1, "feed_um_min"), 5_000_001, "exceeds machine feed limit"),
        (("recipes", 1, "plunge_um_min"), 1_000_001, "exceeds machine plunge limit"),
        (("recipes", 1, "entry_strategy"), "RAMP", "entry_strategy must be PLUNGE"),
        (("recipes", 2, "through_overtravel_um"), 0, "positive through overtravel"),
        (("recipes", 3, "tab_width_um"), 100, "tabs are only valid"),
        (("recipes", 4, "stepover_ppm"), 0, "stepover_ppm"),
        (
            ("recipes", 0, "countersink_included_angle_mdeg"),
            None,
            "countersink recipe requires",
        ),
    ],
)
def test_rejects_unsafe_machine_setup_tool_and_recipe_mutations(
    path: tuple[str | int, ...], value: Any, message: str
) -> None:
    payload = _payload()
    target: Any = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises((ProductionMachineProfileError, ValueError), match=message):
        load_production_machine_profile(_document(payload), allow_test_only=True)


def test_linuxcnc_tool_and_h_numbers_are_bounded_by_signed_controller_integer() -> None:
    payload = _payload()
    payload["tools"][0]["controller_tool_number"] = MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
    payload["tools"][0]["length_offset_number"] = MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
    loaded = load_production_machine_profile(_document(payload), allow_test_only=True)
    assert (
        loaded.execution_context.tool_bindings[0].controller_tool_number
        == MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
    )
    assert (
        loaded.execution_context.tool_bindings[0].length_offset_number
        == MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER
    )

    for field in ("controller_tool_number", "length_offset_number"):
        overflow = _payload()
        overflow["tools"][0][field] = MAX_LINUXCNC_TOOL_OR_OFFSET_NUMBER + 1
        with pytest.raises(ProductionMachineProfileError, match="less than or equal"):
            load_production_machine_profile(_document(overflow), allow_test_only=True)


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("machine",),
        ("acceptance",),
        ("postprocessor_profile",),
        ("setups", 0),
        ("setups", 0, "machine_wcs_origin"),
        ("setups", 0, "fixture"),
        ("setups", 0, "keep_out_zones", 0),
        ("tools", 0),
        ("recipes", 0),
    ],
)
def test_every_object_is_closed_to_unknown_fields(path: tuple[str | int, ...]) -> None:
    payload = _payload()
    target: Any = payload
    for key in path:
        target = target[key]
    target["surprise"] = True
    if path and path[0] == "postprocessor_profile":
        _resign_postprocessor(payload)

    with pytest.raises((ProductionMachineProfileError, ValueError), match="unknown"):
        load_production_machine_profile(_document(payload), allow_test_only=True)


def test_postprocessor_identity_wcs_and_travel_are_cross_checked() -> None:
    payload = _payload()
    payload["machine"]["postprocessor_profile_version"] = "wrong"
    with pytest.raises(ProductionMachineProfileError, match="identity or config_sha256"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["postprocessor_profile"]["supported_wcs"] = ["G55"]
    payload["postprocessor_profile"]["wcs_offsets"] = [
        payload["postprocessor_profile"]["wcs_offsets"][1]
    ]
    _resign_postprocessor(payload)
    with pytest.raises(ProductionMachineProfileError, match="does not support.*G54"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["postprocessor_profile"]["machine_x_max_um"] = 1_200_000
    _resign_postprocessor(payload)
    with pytest.raises(ValueError, match="X maximum differs"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["postprocessor_profile"]["wcs_offsets"][0]["machine_z0_um"] = -21_000
    _resign_postprocessor(payload)
    with pytest.raises(ValueError, match="WCS offset differs from setup origin"):
        load_production_machine_profile(_document(payload), allow_test_only=True)


def test_setup_clearance_spoilboard_and_absolute_z_are_required_facts() -> None:
    signed_xy_payload = _payload()
    signed_xy_payload["machine"]["machine_x_min_um"] = -100_000
    signed_xy_payload["machine"]["machine_x_max_um"] = 1_200_000
    signed_xy_payload["postprocessor_profile"]["machine_x_min_um"] = -100_000
    signed_xy_payload["postprocessor_profile"]["machine_x_max_um"] = 1_200_000
    signed_xy_payload["postprocessor_profile"]["wcs_offsets"][0]["machine_x0_um"] = -50_000
    signed_xy_payload["setups"][0]["machine_wcs_origin"]["x_um"] = -50_000
    _resign_postprocessor(signed_xy_payload)
    load_production_machine_profile(_document(signed_xy_payload), allow_test_only=True)

    non_through_payload = _payload()
    non_through_payload["setups"][0]["through_cut_allowance_um"] = 0
    non_through_payload["setups"][0]["spoilboard_id"] = None
    non_through_payload["setups"][0]["spoilboard_version"] = None
    non_through_payload["setups"][0]["spoilboard_sha256"] = None
    load_production_machine_profile(_document(non_through_payload), allow_test_only=True)

    payload = _payload()
    payload["setups"][0]["minimum_rapid_clearance_um"] = 0
    with pytest.raises(ValueError, match="minimum_rapid_clearance_um"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["setups"][0]["spoilboard_sha256"] = None
    with pytest.raises(ValueError, match="exact spoilboard binding"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["setups"][0]["through_cut_allowance_um"] = 300
    with pytest.raises(ValueError, match="does not cover contour overtravel"):
        load_production_machine_profile(_document(payload), allow_test_only=True)


def test_ordering_coverage_keepouts_and_geometry_fail_closed() -> None:
    payload = _payload()
    payload["tools"].reverse()
    with pytest.raises(ProductionMachineProfileError, match="tools must use canonical order"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["recipes"] = [
        recipe for recipe in payload["recipes"] if recipe["tool_id"] != "production-mill-06"
    ]
    with pytest.raises(ProductionMachineProfileError, match="recipe tool bindings must exactly"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["setups"][0]["keep_out_zones"][0]["width_um"] = 1_100_000
    with pytest.raises(ProductionMachineProfileError, match="within stock"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["tools"][1]["geometry"] = "DRILL"
    with pytest.raises(ProductionMachineProfileError, match="end mill"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["recipes"][0]["countersink_top_diameter_um"] = 10_000
    with pytest.raises(ProductionMachineProfileError, match="diameter mismatch"):
        load_production_machine_profile(_document(payload), allow_test_only=True)


def test_input_mapping_is_copied_into_an_immutable_context() -> None:
    payload = _payload()
    document = {
        "payload": payload,
        "payload_sha256": sha256_hex(canonical_json_bytes(payload)),
        "schema_version": PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION,
    }
    loaded = load_production_machine_profile(document, allow_test_only=True)
    snapshot = deepcopy(loaded.execution_context)

    payload["machine"]["machine_profile_id"] = "mutated-after-load"
    assert loaded.execution_context == snapshot


def test_profile_envelope_rejects_wrong_schema_class_and_acceptance_state() -> None:
    document = json.loads(_document())
    document["schema_version"] = "custombuild.production-machine-profile.v0"
    with pytest.raises(ProductionMachineProfileError, match="unsupported production machine"):
        load_production_machine_profile(
            canonical_json_bytes(document),
            allow_test_only=True,
        )

    payload = _payload()
    payload["acceptance"]["status"] = "WORKSHOP_ACCEPTED"
    with pytest.raises(ProductionMachineProfileError, match="TEST_ONLY profile must have"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["profile_class"] = "SERVER_OWNED_PRODUCTION"
    with pytest.raises(ProductionMachineProfileError, match="not workshop accepted"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["profile_class"] = "UNTRUSTED_PROFILE_CLASS"
    with pytest.raises(ProductionMachineProfileError, match="unsupported production profile class"):
        load_production_machine_profile(_document(payload), allow_test_only=True)


def test_cross_catalog_uniqueness_and_geometry_constraints_fail_closed() -> None:
    mutations = (
        (("tools", 1, "controller_tool_number"), 1, "duplicate controller tool number"),
        (("tools", 1, "length_offset_number"), 11, "duplicate tool length-offset number"),
        (
            ("recipes", 1, "recipe_id"),
            "birch-production-countersink-12-countersink",
            "duplicate cutting recipe identity",
        ),
        (("setups", 0, "material_id"), "other-material", "material bindings must exactly"),
        (("recipes", 1, "stepdown_um"), 30_001, "stepdown exceeds tool cutting length"),
        (("recipes", 1, "stepover_ppm"), 1, "stepover rounds to zero"),
        (("tools", 0, "geometry"), "FLAT_END_MILL", "drill recipe requires drill geometry"),
        (
            ("tools", 2, "geometry"),
            "FLAT_END_MILL",
            "countersink recipe requires countersink geometry",
        ),
    )
    for path, value, message in mutations:
        payload = _payload()
        target: Any = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ProductionMachineProfileError, match=message):
            load_production_machine_profile(_document(payload), allow_test_only=True)


def test_profile_parser_rejects_non_json_types_and_open_shapes() -> None:
    with pytest.raises(ProductionMachineProfileError, match="mapping is not canonical JSON data"):
        load_production_machine_profile({"value": object()}, allow_test_only=True)
    with pytest.raises(ProductionMachineProfileError, match="size is outside"):
        load_production_machine_profile(
            {"padding": "x" * (MAX_PRODUCTION_MACHINE_PROFILE_BYTES + 1)},
            allow_test_only=True,
        )
    with pytest.raises(ProductionMachineProfileError, match="bytes, text or mapping"):
        load_production_machine_profile(cast(Any, None), allow_test_only=True)
    with pytest.raises(ProductionMachineProfileError, match="size is outside"):
        load_production_machine_profile(b"", allow_test_only=True)
    with pytest.raises(ProductionMachineProfileError, match="not valid UTF-8 JSON"):
        load_production_machine_profile(b"\xff", allow_test_only=True)
    with pytest.raises(ProductionMachineProfileError, match="profile document must be an object"):
        load_production_machine_profile(b"[]", allow_test_only=True)
    with pytest.raises(ProductionMachineProfileError, match="keys must be strings"):
        load_production_machine_profile(cast(Any, {1: "value"}), allow_test_only=True)

    payload = _payload()
    payload["setups"] = {}
    with pytest.raises(ProductionMachineProfileError, match="setups must be an array"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    del payload["machine"]["controller_version"]
    with pytest.raises(ProductionMachineProfileError, match="missing fields: controller_version"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["machine"]["controller_version"] = " "
    with pytest.raises(ProductionMachineProfileError, match="canonical non-blank string"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["tools"][0]["center_cutting"] = 1
    with pytest.raises(ProductionMachineProfileError, match="center_cutting must be a boolean"):
        load_production_machine_profile(_document(payload), allow_test_only=True)


def test_profile_enum_setup_and_postprocessor_cross_bindings_fail_closed() -> None:
    payload = _payload()
    payload["setups"][0]["keep_out_zones"].append(
        deepcopy(payload["setups"][0]["keep_out_zones"][0])
    )
    with pytest.raises(ProductionMachineProfileError, match="keep_out_zones contains duplicates"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    mutations = (
        (("setups", 0, "setup_id"), "setup:wrong:001:A", "setup_id is not bound"),
        (("setups", 0, "side"), "TOP", "side is unsupported"),
        (("setups", 0, "side"), "EDGE", "cannot be EDGE"),
        (("tools", 0, "geometry"), "BALL_END_MILL", "geometry is unsupported"),
        (("recipes", 1, "operation_kind"), "LASER", "operation_kind is unsupported"),
        (("recipes", 1, "operation_kind"), "ENGRAVE", "outside production CAM v1"),
    )
    for path, value, message in mutations:
        payload = _payload()
        target: Any = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ProductionMachineProfileError, match=message):
            load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["postprocessor_profile"]["machine_profile_id"] = "other-machine"
    _resign_postprocessor(payload)
    with pytest.raises(ProductionMachineProfileError, match="bound to another production machine"):
        load_production_machine_profile(_document(payload), allow_test_only=True)

    payload = _payload()
    payload["postprocessor_profile"]["controller_version"] = "other-controller-version"
    _resign_postprocessor(payload)
    with pytest.raises(ProductionMachineProfileError, match="bound to another controller"):
        load_production_machine_profile(_document(payload), allow_test_only=True)
