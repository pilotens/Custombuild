"""Output contracts for executable CAM candidates.

These models are intentionally separate from :mod:`custombuild_postprocessors.model`.
The existing ``MachineProgram`` type is, and must remain, validation-only.  A
production candidate contains cutting moves but never grants permission to start
a physical machine; that authorization belongs to the workshop trust boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Never

from custombuild_cam.production_model import EXECUTABLE_CAM_CANDIDATE_MODE
from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION = (
    "custombuild.linuxcnc-production-machine-profile.v1"
)
LINUXCNC_PRODUCTION_POSTPROCESSOR_ID = "linuxcnc-3axis-production"
LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION = "1.1.0"
G53_TOOL_CHANGE_PATH_COMPLETE = "G53_Z_TOOLCHANGE_XY_M6_G53_Z_THEN_ENTRY_XY_AT_GLOBAL_CLEARANCE"
G52_G92_OFFSET_RESET_POLICY = "G92.1_CLEAR_AND_DO_NOT_RESTORE"
FEED_SPINDLE_OVERRIDE_POLICY = "PROGRAM_DISABLES_FEED_AND_SPINDLE_OVERRIDES_WITH_M49"
EXTERNAL_AXIS_OFFSET_POLICY = "XYZ_OFFSET_AV_RATIO_ZERO_EOFFSETS_DISABLED"
HOMING_PREFLIGHT_POLICY = "NO_FORCE_HOMING_0_ALL_XYZ_HOMED_BEFORE_AUTO"
SPINDLE_AT_SPEED_POLICY = "ACTUAL_RPM_GT_0_WITHIN_TOLERANCE_BEFORE_FEED"
CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY = (
    "ACTUAL_RPM_OUT_OF_TOLERANCE_INHIBITS_CUTTING_FEED_CONTINUOUSLY"
)
SPINDLE_DWELL_ROLE = "MINIMUM_DWELL_NOT_SPEED_PROOF"
PROGRAM_RESTART_POLICY = "PROGRAM_START_ONLY_RUN_FROM_LINE_DISABLED_FULL_RESTART_AFTER_ABORT"
M6_TOOL_TABLE_POLICY = "M6_PRESERVES_EXACT_BOUND_TOOL_TABLE"
M6_WCS_TABLE_POLICY = "M6_PRESERVES_EXACT_BOUND_WCS_TABLE"
METRIC_XYZ_IDENTITY_KINEMATICS_POLICY = (
    "LINEAR_UNITS_MM_COORDINATES_XYZ_IDENTITY_TRIVKINS_JOINTS_3_"
    "JOINT_0_X_JOINT_1_Y_JOINT_2_Z_NO_EXTRA_AXES"
)


@dataclass(frozen=True, slots=True)
class LinuxCNCWCSOffset:
    """Attested raw LinuxCNC G5x offset row and XY rotation.

    With the canonical G92.1 reset, zero rotation and an active G43 H row,
    LinuxCNC's G53 controlled-point endpoint is ``programmed + G5x + H`` on X/Y/Z.  These
    fields are therefore controller-table offsets, not already compensated
    tool-tip positions.
    """

    wcs: str
    machine_x0_um: int
    machine_y0_um: int
    machine_z0_um: int
    machine_xy_rotation_mdeg: int

    def __post_init__(self) -> None:
        if self.wcs not in {"G54", "G55", "G56", "G57", "G58", "G59"}:
            raise ValueError("WCS offset must use a canonical G54-G59 code")
        for label, coordinate in (
            ("machine_x0_um", self.machine_x0_um),
            ("machine_y0_um", self.machine_y0_um),
            ("machine_z0_um", self.machine_z0_um),
            ("machine_xy_rotation_mdeg", self.machine_xy_rotation_mdeg),
        ):
            if type(coordinate) is not int:
                raise ValueError(f"{label} must be an integer")
        if self.machine_xy_rotation_mdeg != 0:
            raise ValueError("LinuxCNC production WCS XY rotation must be exactly zero")


@dataclass(frozen=True, slots=True)
class LinuxCNCProductionMachineProfile:
    """Attested controller behavior required before executable output exists.

    LinuxCNC installations can remap ``M6`` and controller I/O.  A controller
    name and version therefore do not prove tool-change semantics.  This
    postprocessor-owned profile makes those assumptions explicit and binds the
    exact machine-coordinate tool-change position and spindle spin-up dwell.

    Production v1 is intentionally narrower than LinuxCNC itself.  The metric
    XYZ kinematics policy binds native ``LINEAR_UNITS=mm``, exactly
    ``COORDINATES=XYZ``, identity ``trivkins``, exactly three joints mapped
    ``0:X, 1:Y, 2:Z``, and no additional controlled axes.  Without retained
    evidence and explicit verification of every one of those facts, the
    programmed/G5x/H controlled-point transform is not accepted.

    ``g53_tool_change_path_clearance_verified`` attests that the configured
    raw machine Z (with G49 active) is a global clearance plane for every bound
    physical tool assembly across the declared machine XY bounds.  It covers
    both the outbound path to M6 and the post-M6 G53 return to a program's
    first XY position before G43 is applied.
    ``g92_1_clears_g52_g92_offsets_verified`` attests the controller-specific
    semantics of the unconditional, state-mutating G92.1 preflight.  The
    canonical policy intentionally clears and does not restore persistent
    G52/G92 offsets.  The two M6 table policies separately attest that a
    remapped tool change cannot mutate either the exact bound H row or any
    bound raw G5x XYZ/R row before the post-M6 G43/WCS entry sequence.  All
    XYZ ``OFFSET_AV_RATIO`` values must be zero because LinuxCNC external
    offsets otherwise alter coordinated motion outside the validated
    G5x/G92/H endpoint transform.  Native ``spindle.N.at-speed`` only gates
    the first feed after a speed change, so a separate controller interlock
    must continuously inhibit cutting feed whenever measured RPM leaves the
    accepted tolerance.  All capability flags are deliberately fail-closed.
    """

    profile_id: str
    version: str
    machine_profile_id: str
    machine_profile_version: str
    controller_id: str
    controller_version: str
    supported_wcs: tuple[str, ...]
    wcs_offsets: tuple[LinuxCNCWCSOffset, ...]
    machine_x_min_um: int
    machine_x_max_um: int
    machine_y_min_um: int
    machine_y_max_um: int
    machine_z_min_um: int
    machine_z_max_um: int
    tool_change_x_um: int
    tool_change_y_um: int
    tool_change_z_um: int
    spindle_spinup_ms: int
    g53_tool_change_path: str
    g53_tool_change_path_clearance_evidence_id: str
    g53_tool_change_path_clearance_evidence_version: str
    g53_tool_change_path_clearance_evidence_sha256: str
    wcs_offsets_evidence_id: str
    wcs_offsets_evidence_version: str
    wcs_offsets_evidence_sha256: str
    g52_g92_offset_reset_policy: str
    g52_g92_offset_reset_evidence_id: str
    g52_g92_offset_reset_evidence_version: str
    g52_g92_offset_reset_evidence_sha256: str
    feed_spindle_override_policy: str
    feed_spindle_override_evidence_id: str
    feed_spindle_override_evidence_version: str
    feed_spindle_override_evidence_sha256: str
    external_axis_offset_policy: str
    external_axis_offset_evidence_id: str
    external_axis_offset_evidence_version: str
    external_axis_offset_evidence_sha256: str
    homing_preflight_policy: str
    homing_preflight_evidence_id: str
    homing_preflight_evidence_version: str
    homing_preflight_evidence_sha256: str
    program_restart_policy: str
    program_restart_evidence_id: str
    program_restart_evidence_version: str
    program_restart_evidence_sha256: str
    m6_tool_table_policy: str
    m6_tool_table_evidence_id: str
    m6_tool_table_evidence_version: str
    m6_tool_table_evidence_sha256: str
    m6_wcs_table_policy: str
    m6_wcs_table_evidence_id: str
    m6_wcs_table_evidence_version: str
    m6_wcs_table_evidence_sha256: str
    metric_xyz_identity_kinematics_policy: str
    metric_xyz_identity_kinematics_evidence_id: str
    metric_xyz_identity_kinematics_evidence_version: str
    metric_xyz_identity_kinematics_evidence_sha256: str
    spindle_at_speed_policy: str
    spindle_feedback_source: str
    spindle_at_speed_evidence_id: str
    spindle_at_speed_evidence_version: str
    spindle_at_speed_evidence_sha256: str
    spindle_at_speed_tolerance_ppm: int
    continuous_spindle_speed_interlock_policy: str
    continuous_spindle_speed_interlock_evidence_id: str
    continuous_spindle_speed_interlock_evidence_version: str
    continuous_spindle_speed_interlock_evidence_sha256: str
    g53_machine_coordinates_verified: bool
    g53_tool_change_path_clearance_verified: bool
    wcs_offsets_verified: bool
    g92_1_clears_g52_g92_offsets_verified: bool
    m6_tool_change_verified: bool
    m6_preserves_axis_position: bool
    m6_preserves_bound_tool_table_verified: bool
    m6_preserves_bound_wcs_table_verified: bool
    linear_units_mm_verified: bool
    coordinates_xyz_verified: bool
    identity_trivkins_verified: bool
    exactly_three_joints_verified: bool
    joint_0_x_1_y_2_z_verified: bool
    no_extra_controlled_axes_verified: bool
    g43_h_length_offset_verified: bool
    g8_radius_mode_verified: bool
    g97_rpm_mode_verified: bool
    m9_coolant_off_verified: bool
    m49_feed_and_spindle_overrides_disabled_verified: bool
    m52_p0_adaptive_feed_disabled_verified: bool
    m53_p1_feed_hold_enabled_verified: bool
    external_xyz_offsets_disabled_verified: bool
    all_xyz_homed_before_auto_verified: bool
    no_force_homing_disabled_verified: bool
    run_from_line_disabled_verified: bool
    full_restart_after_abort_required: bool
    real_spindle_feedback_verified: bool
    spindle_at_speed_motion_interlock_verified: bool
    continuous_spindle_speed_feed_inhibit_verified: bool
    vfd_fault_motion_inhibit_verified: bool
    vfd_fault_spindle_stop_verified: bool
    m3_clockwise_spindle_verified: bool
    g4_p_seconds_dwell_verified: bool
    schema_version: str = LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for label, identity in (
            ("profile_id", self.profile_id),
            ("version", self.version),
            ("machine_profile_id", self.machine_profile_id),
            ("machine_profile_version", self.machine_profile_version),
            ("controller_id", self.controller_id),
            ("controller_version", self.controller_version),
            (
                "g53_tool_change_path_clearance_evidence_id",
                self.g53_tool_change_path_clearance_evidence_id,
            ),
            (
                "g53_tool_change_path_clearance_evidence_version",
                self.g53_tool_change_path_clearance_evidence_version,
            ),
            ("wcs_offsets_evidence_id", self.wcs_offsets_evidence_id),
            ("wcs_offsets_evidence_version", self.wcs_offsets_evidence_version),
            (
                "g52_g92_offset_reset_evidence_id",
                self.g52_g92_offset_reset_evidence_id,
            ),
            (
                "g52_g92_offset_reset_evidence_version",
                self.g52_g92_offset_reset_evidence_version,
            ),
            (
                "feed_spindle_override_evidence_id",
                self.feed_spindle_override_evidence_id,
            ),
            (
                "feed_spindle_override_evidence_version",
                self.feed_spindle_override_evidence_version,
            ),
            ("external_axis_offset_evidence_id", self.external_axis_offset_evidence_id),
            (
                "external_axis_offset_evidence_version",
                self.external_axis_offset_evidence_version,
            ),
            ("homing_preflight_evidence_id", self.homing_preflight_evidence_id),
            (
                "homing_preflight_evidence_version",
                self.homing_preflight_evidence_version,
            ),
            ("program_restart_evidence_id", self.program_restart_evidence_id),
            (
                "program_restart_evidence_version",
                self.program_restart_evidence_version,
            ),
            ("m6_tool_table_evidence_id", self.m6_tool_table_evidence_id),
            (
                "m6_tool_table_evidence_version",
                self.m6_tool_table_evidence_version,
            ),
            ("m6_wcs_table_evidence_id", self.m6_wcs_table_evidence_id),
            (
                "m6_wcs_table_evidence_version",
                self.m6_wcs_table_evidence_version,
            ),
            (
                "metric_xyz_identity_kinematics_evidence_id",
                self.metric_xyz_identity_kinematics_evidence_id,
            ),
            (
                "metric_xyz_identity_kinematics_evidence_version",
                self.metric_xyz_identity_kinematics_evidence_version,
            ),
            ("spindle_feedback_source", self.spindle_feedback_source),
            ("spindle_at_speed_evidence_id", self.spindle_at_speed_evidence_id),
            (
                "spindle_at_speed_evidence_version",
                self.spindle_at_speed_evidence_version,
            ),
            (
                "continuous_spindle_speed_interlock_evidence_id",
                self.continuous_spindle_speed_interlock_evidence_id,
            ),
            (
                "continuous_spindle_speed_interlock_evidence_version",
                self.continuous_spindle_speed_interlock_evidence_version,
            ),
        ):
            if _IDENTITY_PATTERN.fullmatch(identity) is None:
                raise ValueError(f"{label} must be a canonical identity")
        if self.schema_version != LINUXCNC_PRODUCTION_MACHINE_PROFILE_SCHEMA_VERSION:
            raise ValueError("unsupported LinuxCNC production machine-profile schema")
        if self.controller_id.casefold() != "linuxcnc":
            raise ValueError("production machine profile must bind LinuxCNC")
        if (
            not self.supported_wcs
            or len(set(self.supported_wcs)) != len(self.supported_wcs)
            or tuple(sorted(self.supported_wcs)) != self.supported_wcs
            or any(
                code not in {"G54", "G55", "G56", "G57", "G58", "G59"}
                for code in self.supported_wcs
            )
        ):
            raise ValueError("supported_wcs must be a canonical sorted subset of G54-G59")
        if (
            not self.wcs_offsets
            or tuple(sorted(self.wcs_offsets, key=lambda item: item.wcs)) != self.wcs_offsets
            or len({offset.wcs for offset in self.wcs_offsets}) != len(self.wcs_offsets)
            or tuple(offset.wcs for offset in self.wcs_offsets) != self.supported_wcs
        ):
            raise ValueError("wcs_offsets must exactly and canonically bind every supported WCS")
        for label, coordinate in (
            ("machine_x_min_um", self.machine_x_min_um),
            ("machine_x_max_um", self.machine_x_max_um),
            ("machine_y_min_um", self.machine_y_min_um),
            ("machine_y_max_um", self.machine_y_max_um),
            ("machine_z_min_um", self.machine_z_min_um),
            ("machine_z_max_um", self.machine_z_max_um),
            ("tool_change_x_um", self.tool_change_x_um),
            ("tool_change_y_um", self.tool_change_y_um),
            ("tool_change_z_um", self.tool_change_z_um),
        ):
            if type(coordinate) is not int:
                raise ValueError(f"{label} must be integer micrometres")
        for axis, minimum, maximum, tool_change in (
            ("X", self.machine_x_min_um, self.machine_x_max_um, self.tool_change_x_um),
            ("Y", self.machine_y_min_um, self.machine_y_max_um, self.tool_change_y_um),
            ("Z", self.machine_z_min_um, self.machine_z_max_um, self.tool_change_z_um),
        ):
            if minimum >= maximum:
                raise ValueError(f"machine {axis} minimum must be below its maximum")
            if not minimum <= tool_change <= maximum:
                raise ValueError(f"tool-change {axis} is outside the declared machine bounds")
        for offset in self.wcs_offsets:
            if not (
                self.machine_x_min_um <= offset.machine_x0_um <= self.machine_x_max_um
                and self.machine_y_min_um <= offset.machine_y0_um <= self.machine_y_max_um
                and self.machine_z_min_um <= offset.machine_z0_um <= self.machine_z_max_um
            ):
                raise ValueError(f"{offset.wcs} origin is outside the declared machine bounds")
        if type(self.spindle_spinup_ms) is not int or not 1 <= self.spindle_spinup_ms <= 120_000:
            raise ValueError("spindle_spinup_ms must be an integer from 1 to 120000")
        if self.g53_tool_change_path != G53_TOOL_CHANGE_PATH_COMPLETE:
            raise ValueError("unsupported G53 tool-change path")
        if self.g52_g92_offset_reset_policy != G52_G92_OFFSET_RESET_POLICY:
            raise ValueError("unsupported G52/G92 offset-reset policy")
        if self.feed_spindle_override_policy != FEED_SPINDLE_OVERRIDE_POLICY:
            raise ValueError("unsupported feed/spindle-override policy")
        if self.external_axis_offset_policy != EXTERNAL_AXIS_OFFSET_POLICY:
            raise ValueError("unsupported external-axis-offset policy")
        if self.homing_preflight_policy != HOMING_PREFLIGHT_POLICY:
            raise ValueError("unsupported homing-preflight policy")
        if self.program_restart_policy != PROGRAM_RESTART_POLICY:
            raise ValueError("unsupported program-restart policy")
        if self.m6_tool_table_policy != M6_TOOL_TABLE_POLICY:
            raise ValueError("unsupported M6 tool-table policy")
        if self.m6_wcs_table_policy != M6_WCS_TABLE_POLICY:
            raise ValueError("unsupported M6 WCS-table policy")
        if self.metric_xyz_identity_kinematics_policy != METRIC_XYZ_IDENTITY_KINEMATICS_POLICY:
            raise ValueError("unsupported native-unit/axis/kinematics policy")
        if self.spindle_at_speed_policy != SPINDLE_AT_SPEED_POLICY:
            raise ValueError("unsupported spindle-at-speed policy")
        if (
            self.continuous_spindle_speed_interlock_policy
            != CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY
        ):
            raise ValueError("unsupported continuous spindle-speed-interlock policy")
        if (
            type(self.spindle_at_speed_tolerance_ppm) is not int
            or not 1 <= self.spindle_at_speed_tolerance_ppm <= 100_000
        ):
            raise ValueError("spindle_at_speed_tolerance_ppm must be an integer from 1 to 100000")
        for label, digest in (
            (
                "g53_tool_change_path_clearance_evidence_sha256",
                self.g53_tool_change_path_clearance_evidence_sha256,
            ),
            ("wcs_offsets_evidence_sha256", self.wcs_offsets_evidence_sha256),
            (
                "g52_g92_offset_reset_evidence_sha256",
                self.g52_g92_offset_reset_evidence_sha256,
            ),
            (
                "feed_spindle_override_evidence_sha256",
                self.feed_spindle_override_evidence_sha256,
            ),
            (
                "external_axis_offset_evidence_sha256",
                self.external_axis_offset_evidence_sha256,
            ),
            (
                "homing_preflight_evidence_sha256",
                self.homing_preflight_evidence_sha256,
            ),
            (
                "program_restart_evidence_sha256",
                self.program_restart_evidence_sha256,
            ),
            (
                "m6_tool_table_evidence_sha256",
                self.m6_tool_table_evidence_sha256,
            ),
            (
                "m6_wcs_table_evidence_sha256",
                self.m6_wcs_table_evidence_sha256,
            ),
            (
                "metric_xyz_identity_kinematics_evidence_sha256",
                self.metric_xyz_identity_kinematics_evidence_sha256,
            ),
            (
                "spindle_at_speed_evidence_sha256",
                self.spindle_at_speed_evidence_sha256,
            ),
            (
                "continuous_spindle_speed_interlock_evidence_sha256",
                self.continuous_spindle_speed_interlock_evidence_sha256,
            ),
        ):
            if _HASH_PATTERN.fullmatch(digest) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        attestations = (
            self.g53_machine_coordinates_verified,
            self.g53_tool_change_path_clearance_verified,
            self.wcs_offsets_verified,
            self.g92_1_clears_g52_g92_offsets_verified,
            self.m6_tool_change_verified,
            self.m6_preserves_axis_position,
            self.m6_preserves_bound_tool_table_verified,
            self.m6_preserves_bound_wcs_table_verified,
            self.linear_units_mm_verified,
            self.coordinates_xyz_verified,
            self.identity_trivkins_verified,
            self.exactly_three_joints_verified,
            self.joint_0_x_1_y_2_z_verified,
            self.no_extra_controlled_axes_verified,
            self.g43_h_length_offset_verified,
            self.g8_radius_mode_verified,
            self.g97_rpm_mode_verified,
            self.m9_coolant_off_verified,
            self.m49_feed_and_spindle_overrides_disabled_verified,
            self.m52_p0_adaptive_feed_disabled_verified,
            self.m53_p1_feed_hold_enabled_verified,
            self.external_xyz_offsets_disabled_verified,
            self.all_xyz_homed_before_auto_verified,
            self.no_force_homing_disabled_verified,
            self.run_from_line_disabled_verified,
            self.full_restart_after_abort_required,
            self.real_spindle_feedback_verified,
            self.spindle_at_speed_motion_interlock_verified,
            self.continuous_spindle_speed_feed_inhibit_verified,
            self.vfd_fault_motion_inhibit_verified,
            self.vfd_fault_spindle_stop_verified,
            self.m3_clockwise_spindle_verified,
            self.g4_p_seconds_dwell_verified,
        )
        if any(attestation is not True for attestation in attestations):
            raise ValueError("all LinuxCNC production capabilities must be explicitly verified")

    @property
    def config_sha256(self) -> str:
        return sha256_hex(canonical_json_bytes(self))

    @property
    def work_width_um(self) -> int:
        return self.machine_x_max_um - self.machine_x_min_um

    @property
    def work_height_um(self) -> int:
        return self.machine_y_max_um - self.machine_y_min_um

    @property
    def work_z_um(self) -> int:
        return self.machine_z_max_um - self.machine_z_min_um

    @property
    def fingerprint(self) -> str:
        """Compatibility spelling for the canonical configuration hash."""

        return self.config_sha256

    def to_json(self) -> bytes:
        return canonical_json_bytes(self)

    @classmethod
    def from_json(cls, payload: bytes | str) -> LinuxCNCProductionMachineProfile:
        """Load a closed, canonical profile document without lossy coercion."""

        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if not isinstance(raw, bytes) or not raw or len(raw) > 64_000:
            raise ValueError("LinuxCNC production machine profile has invalid byte size")
        try:
            parsed = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_float=_reject_json_number,
                parse_constant=_reject_json_number,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("LinuxCNC production machine profile is not valid UTF-8 JSON") from exc
        if not isinstance(parsed, dict) or any(type(key) is not str for key in parsed):
            raise ValueError("LinuxCNC production machine profile must be a JSON object")
        expected = {
            "profile_id",
            "version",
            "machine_profile_id",
            "machine_profile_version",
            "controller_id",
            "controller_version",
            "supported_wcs",
            "wcs_offsets",
            "machine_x_min_um",
            "machine_x_max_um",
            "machine_y_min_um",
            "machine_y_max_um",
            "machine_z_min_um",
            "machine_z_max_um",
            "tool_change_x_um",
            "tool_change_y_um",
            "tool_change_z_um",
            "spindle_spinup_ms",
            "g53_tool_change_path",
            "g53_tool_change_path_clearance_evidence_id",
            "g53_tool_change_path_clearance_evidence_version",
            "g53_tool_change_path_clearance_evidence_sha256",
            "wcs_offsets_evidence_id",
            "wcs_offsets_evidence_version",
            "wcs_offsets_evidence_sha256",
            "g52_g92_offset_reset_policy",
            "g52_g92_offset_reset_evidence_id",
            "g52_g92_offset_reset_evidence_version",
            "g52_g92_offset_reset_evidence_sha256",
            "feed_spindle_override_policy",
            "feed_spindle_override_evidence_id",
            "feed_spindle_override_evidence_version",
            "feed_spindle_override_evidence_sha256",
            "external_axis_offset_policy",
            "external_axis_offset_evidence_id",
            "external_axis_offset_evidence_version",
            "external_axis_offset_evidence_sha256",
            "homing_preflight_policy",
            "homing_preflight_evidence_id",
            "homing_preflight_evidence_version",
            "homing_preflight_evidence_sha256",
            "program_restart_policy",
            "program_restart_evidence_id",
            "program_restart_evidence_version",
            "program_restart_evidence_sha256",
            "m6_tool_table_policy",
            "m6_tool_table_evidence_id",
            "m6_tool_table_evidence_version",
            "m6_tool_table_evidence_sha256",
            "m6_wcs_table_policy",
            "m6_wcs_table_evidence_id",
            "m6_wcs_table_evidence_version",
            "m6_wcs_table_evidence_sha256",
            "metric_xyz_identity_kinematics_policy",
            "metric_xyz_identity_kinematics_evidence_id",
            "metric_xyz_identity_kinematics_evidence_version",
            "metric_xyz_identity_kinematics_evidence_sha256",
            "spindle_at_speed_policy",
            "spindle_feedback_source",
            "spindle_at_speed_evidence_id",
            "spindle_at_speed_evidence_version",
            "spindle_at_speed_evidence_sha256",
            "spindle_at_speed_tolerance_ppm",
            "continuous_spindle_speed_interlock_policy",
            "continuous_spindle_speed_interlock_evidence_id",
            "continuous_spindle_speed_interlock_evidence_version",
            "continuous_spindle_speed_interlock_evidence_sha256",
            "g53_machine_coordinates_verified",
            "g53_tool_change_path_clearance_verified",
            "wcs_offsets_verified",
            "g92_1_clears_g52_g92_offsets_verified",
            "m6_tool_change_verified",
            "m6_preserves_axis_position",
            "m6_preserves_bound_tool_table_verified",
            "m6_preserves_bound_wcs_table_verified",
            "linear_units_mm_verified",
            "coordinates_xyz_verified",
            "identity_trivkins_verified",
            "exactly_three_joints_verified",
            "joint_0_x_1_y_2_z_verified",
            "no_extra_controlled_axes_verified",
            "g43_h_length_offset_verified",
            "g8_radius_mode_verified",
            "g97_rpm_mode_verified",
            "m9_coolant_off_verified",
            "m49_feed_and_spindle_overrides_disabled_verified",
            "m52_p0_adaptive_feed_disabled_verified",
            "m53_p1_feed_hold_enabled_verified",
            "external_xyz_offsets_disabled_verified",
            "all_xyz_homed_before_auto_verified",
            "no_force_homing_disabled_verified",
            "run_from_line_disabled_verified",
            "full_restart_after_abort_required",
            "real_spindle_feedback_verified",
            "spindle_at_speed_motion_interlock_verified",
            "continuous_spindle_speed_feed_inhibit_verified",
            "vfd_fault_motion_inhibit_verified",
            "vfd_fault_spindle_stop_verified",
            "m3_clockwise_spindle_verified",
            "g4_p_seconds_dwell_verified",
            "schema_version",
        }
        if set(parsed) != expected:
            raise ValueError("LinuxCNC production machine profile has missing or unknown fields")
        supported_wcs = parsed["supported_wcs"]
        if not isinstance(supported_wcs, list) or any(
            type(value) is not str for value in supported_wcs
        ):
            raise ValueError("supported_wcs must be a JSON string array")
        raw_wcs_offsets = parsed["wcs_offsets"]
        if not isinstance(raw_wcs_offsets, list):
            raise ValueError("wcs_offsets must be a JSON object array")
        wcs_offsets = tuple(
            _parse_wcs_offset(value, index=index) for index, value in enumerate(raw_wcs_offsets)
        )
        profile = cls(
            profile_id=_json_string(parsed, "profile_id"),
            version=_json_string(parsed, "version"),
            machine_profile_id=_json_string(parsed, "machine_profile_id"),
            machine_profile_version=_json_string(parsed, "machine_profile_version"),
            controller_id=_json_string(parsed, "controller_id"),
            controller_version=_json_string(parsed, "controller_version"),
            supported_wcs=tuple(supported_wcs),
            wcs_offsets=wcs_offsets,
            machine_x_min_um=_json_int(parsed, "machine_x_min_um"),
            machine_x_max_um=_json_int(parsed, "machine_x_max_um"),
            machine_y_min_um=_json_int(parsed, "machine_y_min_um"),
            machine_y_max_um=_json_int(parsed, "machine_y_max_um"),
            machine_z_min_um=_json_int(parsed, "machine_z_min_um"),
            machine_z_max_um=_json_int(parsed, "machine_z_max_um"),
            tool_change_x_um=_json_int(parsed, "tool_change_x_um"),
            tool_change_y_um=_json_int(parsed, "tool_change_y_um"),
            tool_change_z_um=_json_int(parsed, "tool_change_z_um"),
            spindle_spinup_ms=_json_int(parsed, "spindle_spinup_ms"),
            g53_tool_change_path=_json_string(parsed, "g53_tool_change_path"),
            g53_tool_change_path_clearance_evidence_id=_json_string(
                parsed, "g53_tool_change_path_clearance_evidence_id"
            ),
            g53_tool_change_path_clearance_evidence_version=_json_string(
                parsed, "g53_tool_change_path_clearance_evidence_version"
            ),
            g53_tool_change_path_clearance_evidence_sha256=_json_string(
                parsed, "g53_tool_change_path_clearance_evidence_sha256"
            ),
            wcs_offsets_evidence_id=_json_string(parsed, "wcs_offsets_evidence_id"),
            wcs_offsets_evidence_version=_json_string(parsed, "wcs_offsets_evidence_version"),
            wcs_offsets_evidence_sha256=_json_string(parsed, "wcs_offsets_evidence_sha256"),
            g52_g92_offset_reset_policy=_json_string(parsed, "g52_g92_offset_reset_policy"),
            g52_g92_offset_reset_evidence_id=_json_string(
                parsed, "g52_g92_offset_reset_evidence_id"
            ),
            g52_g92_offset_reset_evidence_version=_json_string(
                parsed, "g52_g92_offset_reset_evidence_version"
            ),
            g52_g92_offset_reset_evidence_sha256=_json_string(
                parsed, "g52_g92_offset_reset_evidence_sha256"
            ),
            feed_spindle_override_policy=_json_string(parsed, "feed_spindle_override_policy"),
            feed_spindle_override_evidence_id=_json_string(
                parsed, "feed_spindle_override_evidence_id"
            ),
            feed_spindle_override_evidence_version=_json_string(
                parsed, "feed_spindle_override_evidence_version"
            ),
            feed_spindle_override_evidence_sha256=_json_string(
                parsed, "feed_spindle_override_evidence_sha256"
            ),
            external_axis_offset_policy=_json_string(parsed, "external_axis_offset_policy"),
            external_axis_offset_evidence_id=_json_string(
                parsed, "external_axis_offset_evidence_id"
            ),
            external_axis_offset_evidence_version=_json_string(
                parsed, "external_axis_offset_evidence_version"
            ),
            external_axis_offset_evidence_sha256=_json_string(
                parsed, "external_axis_offset_evidence_sha256"
            ),
            homing_preflight_policy=_json_string(parsed, "homing_preflight_policy"),
            homing_preflight_evidence_id=_json_string(parsed, "homing_preflight_evidence_id"),
            homing_preflight_evidence_version=_json_string(
                parsed, "homing_preflight_evidence_version"
            ),
            homing_preflight_evidence_sha256=_json_string(
                parsed, "homing_preflight_evidence_sha256"
            ),
            program_restart_policy=_json_string(parsed, "program_restart_policy"),
            program_restart_evidence_id=_json_string(parsed, "program_restart_evidence_id"),
            program_restart_evidence_version=_json_string(
                parsed, "program_restart_evidence_version"
            ),
            program_restart_evidence_sha256=_json_string(parsed, "program_restart_evidence_sha256"),
            m6_tool_table_policy=_json_string(parsed, "m6_tool_table_policy"),
            m6_tool_table_evidence_id=_json_string(parsed, "m6_tool_table_evidence_id"),
            m6_tool_table_evidence_version=_json_string(parsed, "m6_tool_table_evidence_version"),
            m6_tool_table_evidence_sha256=_json_string(parsed, "m6_tool_table_evidence_sha256"),
            m6_wcs_table_policy=_json_string(parsed, "m6_wcs_table_policy"),
            m6_wcs_table_evidence_id=_json_string(parsed, "m6_wcs_table_evidence_id"),
            m6_wcs_table_evidence_version=_json_string(parsed, "m6_wcs_table_evidence_version"),
            m6_wcs_table_evidence_sha256=_json_string(parsed, "m6_wcs_table_evidence_sha256"),
            metric_xyz_identity_kinematics_policy=_json_string(
                parsed, "metric_xyz_identity_kinematics_policy"
            ),
            metric_xyz_identity_kinematics_evidence_id=_json_string(
                parsed, "metric_xyz_identity_kinematics_evidence_id"
            ),
            metric_xyz_identity_kinematics_evidence_version=_json_string(
                parsed, "metric_xyz_identity_kinematics_evidence_version"
            ),
            metric_xyz_identity_kinematics_evidence_sha256=_json_string(
                parsed, "metric_xyz_identity_kinematics_evidence_sha256"
            ),
            spindle_at_speed_policy=_json_string(parsed, "spindle_at_speed_policy"),
            spindle_feedback_source=_json_string(parsed, "spindle_feedback_source"),
            spindle_at_speed_evidence_id=_json_string(parsed, "spindle_at_speed_evidence_id"),
            spindle_at_speed_evidence_version=_json_string(
                parsed, "spindle_at_speed_evidence_version"
            ),
            spindle_at_speed_evidence_sha256=_json_string(
                parsed, "spindle_at_speed_evidence_sha256"
            ),
            spindle_at_speed_tolerance_ppm=_json_int(parsed, "spindle_at_speed_tolerance_ppm"),
            continuous_spindle_speed_interlock_policy=_json_string(
                parsed, "continuous_spindle_speed_interlock_policy"
            ),
            continuous_spindle_speed_interlock_evidence_id=_json_string(
                parsed, "continuous_spindle_speed_interlock_evidence_id"
            ),
            continuous_spindle_speed_interlock_evidence_version=_json_string(
                parsed, "continuous_spindle_speed_interlock_evidence_version"
            ),
            continuous_spindle_speed_interlock_evidence_sha256=_json_string(
                parsed, "continuous_spindle_speed_interlock_evidence_sha256"
            ),
            g53_machine_coordinates_verified=_json_bool(parsed, "g53_machine_coordinates_verified"),
            g53_tool_change_path_clearance_verified=_json_bool(
                parsed, "g53_tool_change_path_clearance_verified"
            ),
            wcs_offsets_verified=_json_bool(parsed, "wcs_offsets_verified"),
            g92_1_clears_g52_g92_offsets_verified=_json_bool(
                parsed, "g92_1_clears_g52_g92_offsets_verified"
            ),
            m6_tool_change_verified=_json_bool(parsed, "m6_tool_change_verified"),
            m6_preserves_axis_position=_json_bool(parsed, "m6_preserves_axis_position"),
            m6_preserves_bound_tool_table_verified=_json_bool(
                parsed, "m6_preserves_bound_tool_table_verified"
            ),
            m6_preserves_bound_wcs_table_verified=_json_bool(
                parsed, "m6_preserves_bound_wcs_table_verified"
            ),
            linear_units_mm_verified=_json_bool(parsed, "linear_units_mm_verified"),
            coordinates_xyz_verified=_json_bool(parsed, "coordinates_xyz_verified"),
            identity_trivkins_verified=_json_bool(parsed, "identity_trivkins_verified"),
            exactly_three_joints_verified=_json_bool(parsed, "exactly_three_joints_verified"),
            joint_0_x_1_y_2_z_verified=_json_bool(parsed, "joint_0_x_1_y_2_z_verified"),
            no_extra_controlled_axes_verified=_json_bool(
                parsed, "no_extra_controlled_axes_verified"
            ),
            g43_h_length_offset_verified=_json_bool(parsed, "g43_h_length_offset_verified"),
            g8_radius_mode_verified=_json_bool(parsed, "g8_radius_mode_verified"),
            g97_rpm_mode_verified=_json_bool(parsed, "g97_rpm_mode_verified"),
            m9_coolant_off_verified=_json_bool(parsed, "m9_coolant_off_verified"),
            m49_feed_and_spindle_overrides_disabled_verified=_json_bool(
                parsed, "m49_feed_and_spindle_overrides_disabled_verified"
            ),
            m52_p0_adaptive_feed_disabled_verified=_json_bool(
                parsed, "m52_p0_adaptive_feed_disabled_verified"
            ),
            m53_p1_feed_hold_enabled_verified=_json_bool(
                parsed, "m53_p1_feed_hold_enabled_verified"
            ),
            external_xyz_offsets_disabled_verified=_json_bool(
                parsed, "external_xyz_offsets_disabled_verified"
            ),
            all_xyz_homed_before_auto_verified=_json_bool(
                parsed, "all_xyz_homed_before_auto_verified"
            ),
            no_force_homing_disabled_verified=_json_bool(
                parsed, "no_force_homing_disabled_verified"
            ),
            run_from_line_disabled_verified=_json_bool(parsed, "run_from_line_disabled_verified"),
            full_restart_after_abort_required=_json_bool(
                parsed, "full_restart_after_abort_required"
            ),
            real_spindle_feedback_verified=_json_bool(parsed, "real_spindle_feedback_verified"),
            spindle_at_speed_motion_interlock_verified=_json_bool(
                parsed, "spindle_at_speed_motion_interlock_verified"
            ),
            continuous_spindle_speed_feed_inhibit_verified=_json_bool(
                parsed, "continuous_spindle_speed_feed_inhibit_verified"
            ),
            vfd_fault_motion_inhibit_verified=_json_bool(
                parsed, "vfd_fault_motion_inhibit_verified"
            ),
            vfd_fault_spindle_stop_verified=_json_bool(parsed, "vfd_fault_spindle_stop_verified"),
            m3_clockwise_spindle_verified=_json_bool(parsed, "m3_clockwise_spindle_verified"),
            g4_p_seconds_dwell_verified=_json_bool(parsed, "g4_p_seconds_dwell_verified"),
            schema_version=_json_string(parsed, "schema_version"),
        )
        if profile.to_json() != raw:
            raise ValueError("LinuxCNC production machine profile must use canonical JSON bytes")
        return profile


def _parse_wcs_offset(value: Any, *, index: int) -> LinuxCNCWCSOffset:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError(f"wcs_offsets[{index}] must be a JSON object")
    expected = {
        "wcs",
        "machine_x0_um",
        "machine_y0_um",
        "machine_z0_um",
        "machine_xy_rotation_mdeg",
    }
    if set(value) != expected:
        raise ValueError(f"wcs_offsets[{index}] has missing or unknown fields")
    return LinuxCNCWCSOffset(
        wcs=_json_string(value, "wcs"),
        machine_x0_um=_json_int(value, "machine_x0_um"),
        machine_y0_um=_json_int(value, "machine_y0_um"),
        machine_z0_um=_json_int(value, "machine_z0_um"),
        machine_xy_rotation_mdeg=_json_int(value, "machine_xy_rotation_mdeg"),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in LinuxCNC production profile: {key}")
        result[key] = value
    return result


def _reject_json_number(value: str) -> Never:
    raise ValueError(f"LinuxCNC production profile requires exact JSON integers: {value}")


def _json_string(value: dict[str, Any], key: str) -> str:
    result = value[key]
    if type(result) is not str:
        raise ValueError(f"{key} must be a JSON string")
    return result


def _json_int(value: dict[str, Any], key: str) -> int:
    result = value[key]
    if type(result) is not int:
        raise ValueError(f"{key} must be a JSON integer")
    return result


def _json_bool(value: dict[str, Any], key: str) -> bool:
    result = value[key]
    if type(result) is not bool:
        raise ValueError(f"{key} must be a JSON boolean")
    return result


@dataclass(frozen=True, slots=True)
class ProductionMachineProgram:
    """One deterministic LinuxCNC program awaiting workshop acceptance."""

    filename: str
    program_id: str
    run_order: int
    setup_id: str
    tool_id: str
    controller: str
    controller_version: str
    postprocessor_id: str
    postprocessor_version: str
    source_toolpaths_sha256: str
    production_machine_profile_sha256: str
    content: bytes
    mode: str = EXECUTABLE_CAM_CANDIDATE_MODE
    machine_executable: bool = True
    physical_cutting_authorized: bool = False
    workshop_acceptance_required: bool = True

    def __post_init__(self) -> None:
        for label, value in (
            ("program_id", self.program_id),
            ("setup_id", self.setup_id),
            ("tool_id", self.tool_id),
            ("controller", self.controller),
            ("controller_version", self.controller_version),
            ("postprocessor_id", self.postprocessor_id),
            ("postprocessor_version", self.postprocessor_version),
        ):
            if _IDENTITY_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{label} must be a canonical identity")
        if (
            not self.filename
            or "/" in self.filename
            or "\\" in self.filename
            or self.filename in {".", ".."}
            or not self.filename.endswith(".production.ngc")
        ):
            raise ValueError("production program filename is not canonical")
        if type(self.run_order) is not int or self.run_order < 1:
            raise ValueError("production program run_order must be a positive integer")
        if not self.filename.startswith(f"{self.run_order:03d}."):
            raise ValueError("production program filename must bind its run_order ordinal")
        if _HASH_PATTERN.fullmatch(self.source_toolpaths_sha256) is None:
            raise ValueError("production program requires the exact toolpath-document hash")
        if _HASH_PATTERN.fullmatch(self.production_machine_profile_sha256) is None:
            raise ValueError("production program requires the exact machine-profile config hash")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("production program content must be non-empty bytes")
        if self.mode != EXECUTABLE_CAM_CANDIDATE_MODE:
            raise ValueError("production program must remain an executable CAM candidate")
        if self.machine_executable is not True:
            raise ValueError("production candidate must truthfully identify executable motion")
        if self.physical_cutting_authorized is not False:
            raise ValueError("a postprocessor cannot authorize physical cutting")
        if self.workshop_acceptance_required is not True:
            raise ValueError("workshop acceptance cannot be bypassed")

    @property
    def sha256(self) -> str:
        return sha256_hex(self.content)
