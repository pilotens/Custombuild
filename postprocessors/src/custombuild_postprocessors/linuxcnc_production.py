"""Machine-specific LinuxCNC output for executable CAM candidates."""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal

from custombuild_cam.production_model import (
    BoundSetup,
    ProductionMove,
    ProductionMoveKind,
    ProductionMoveRole,
    ProductionProgram,
    ProductionToolBinding,
    ProductionToolpathDocument,
)

from .parser import GCodeSafetyError
from .production_model import (
    LINUXCNC_PRODUCTION_POSTPROCESSOR_ID,
    LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION,
    SPINDLE_DWELL_ROLE,
    LinuxCNCProductionMachineProfile,
    ProductionMachineProgram,
)
from .production_parser import (
    PHYSICAL_AUTHORIZATION_MARKER,
    PRODUCTION_CANDIDATE_POLICY_MARKER,
    PRODUCTION_GCODE_PARSER_VERSION,
    PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION,
    WORKSHOP_ACCEPTANCE_MARKER,
    validate_production_program,
)

_CANONICAL_STATE_LINES = (
    "G21",
    "G8",
    "G17 G40 G49 G80 G90 G94 G97",
    "G61",
    "M5",
    "M9",
    "M49",
    "M52 P0",
    "M53 P1",
    "G92.1",
)


class LinuxCNCProductionPostprocessor:
    """Emit G0/G1-only LinuxCNC programs from an exact cutting-path document."""

    name = "LinuxCNC 3-axis executable CAM candidate postprocessor"
    controller = "LinuxCNC"
    postprocessor_id = LINUXCNC_PRODUCTION_POSTPROCESSOR_ID
    version = LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION
    mode = "EXECUTABLE_CAM_CANDIDATE"

    def __init__(self, machine_profile: LinuxCNCProductionMachineProfile) -> None:
        self.machine_profile = machine_profile

    def generate(
        self,
        document: ProductionToolpathDocument,
    ) -> tuple[ProductionMachineProgram, ...]:
        context = document.execution_context
        if context.controller_id.casefold() != self.controller.casefold():
            raise GCodeSafetyError(
                "LinuxCNC production postprocessor requires an exact LinuxCNC controller binding"
            )
        profile = self.machine_profile
        profile_binding = (
            profile.machine_profile_id,
            profile.machine_profile_version,
            profile.controller_id,
            profile.controller_version,
        )
        context_binding = (
            context.machine_profile_id,
            context.machine_profile_version,
            context.controller_id,
            context.controller_version,
        )
        profile_bounds = (
            profile.machine_x_min_um,
            profile.machine_x_max_um,
            profile.machine_y_min_um,
            profile.machine_y_max_um,
            profile.machine_z_min_um,
            profile.machine_z_max_um,
        )
        context_bounds = (
            context.machine_x_min_um,
            context.machine_x_max_um,
            context.machine_y_min_um,
            context.machine_y_max_um,
            context.machine_z_min_um,
            context.machine_z_max_um,
        )
        if profile_binding != context_binding or profile_bounds != context_bounds:
            raise GCodeSafetyError(
                "LinuxCNC production machine profile does not exactly match the toolpath context"
            )
        setup_by_id = {setup.setup_id: setup for setup in context.setups}
        tool_by_id = {tool.tool_id: tool for tool in context.tool_bindings}
        recipes_by_id = {recipe.recipe_id: recipe for recipe in context.recipes}
        if len(setup_by_id) != len(context.setups) or len(tool_by_id) != len(context.tool_bindings):
            raise GCodeSafetyError("production context contains duplicate setup or tool bindings")

        output: list[ProductionMachineProgram] = []
        filenames: set[str] = set()
        for program in document.programs:
            try:
                setup = setup_by_id[program.setup_id]
                tool = tool_by_id[program.tool_id]
                recipes = tuple(recipes_by_id[item] for item in program.recipe_ids)
            except KeyError as exc:
                raise GCodeSafetyError(
                    "production program references an unresolved setup, tool or recipe"
                ) from exc
            spindle_rpms = {recipe.spindle_rpm for recipe in recipes}
            if len(spindle_rpms) != 1:
                raise GCodeSafetyError(
                    "one setup/tool program requires one exact spindle speed across its recipes"
                )
            spindle_rpm = next(iter(spindle_rpms))
            if setup.wcs not in profile.supported_wcs:
                raise GCodeSafetyError(
                    f"setup WCS {setup.wcs} is not attested by the production machine profile"
                )
            _require_exact_setup_offset_and_clearance(profile, setup, tool)
            entry_machine_x_um, entry_machine_y_um = _require_program_entry(
                profile,
                setup,
                program,
                tool,
            )
            lines = self._header(
                document,
                program,
                setup,
                tool,
                entry_machine_x_um=entry_machine_x_um,
                entry_machine_y_um=entry_machine_y_um,
            )
            lines.extend(
                (
                    *_CANONICAL_STATE_LINES,
                    f"G53 G0 Z{_format_mm(profile.tool_change_z_um)}",
                    f"G53 G0 X{_format_mm(profile.tool_change_x_um)} "
                    f"Y{_format_mm(profile.tool_change_y_um)}",
                    f"T{tool.controller_tool_number} M6",
                    *_CANONICAL_STATE_LINES,
                    f"G53 G0 Z{_format_mm(profile.tool_change_z_um)}",
                    f"G53 G0 X{_format_mm(entry_machine_x_um)} Y{_format_mm(entry_machine_y_um)}",
                    f"G43 H{tool.length_offset_number}",
                    setup.wcs,
                    f"G0 Z{_format_mm(setup.safe_z_um)}",
                    f"S{spindle_rpm} M3",
                    f"G4 P{_format_seconds(profile.spindle_spinup_ms)}",
                )
            )
            previous_operation = ""
            for move in program.moves:
                if move.operation_id != previous_operation:
                    lines.append(f"(OPERATION_ID={_safe_comment(move.operation_id)})")
                    previous_operation = move.operation_id
                lines.append(_move_line(move))
            lines.extend(
                (
                    f"G0 Z{_format_mm(setup.safe_z_um)}",
                    "M5",
                    "M9",
                    "G49",
                    f"G53 G0 Z{_format_mm(profile.tool_change_z_um)}",
                    "M2",
                    "%",
                )
            )
            content = ("\n".join(lines) + "\n").encode("ascii")
            filename = (
                f"{program.run_order:03d}.{_safe_filename_component(program.setup_id)}."
                f"{_safe_filename_component(program.tool_id)}.production.ngc"
            )
            if filename in filenames:
                raise GCodeSafetyError("production program filenames are not unique")
            filenames.add(filename)
            validate_production_program(
                content,
                document=document,
                program=program,
                machine_profile=profile,
            )
            output.append(
                ProductionMachineProgram(
                    filename=filename,
                    program_id=program.program_id,
                    run_order=program.run_order,
                    setup_id=program.setup_id,
                    tool_id=program.tool_id,
                    controller=context.controller_id,
                    controller_version=context.controller_version,
                    postprocessor_id=self.postprocessor_id,
                    postprocessor_version=self.version,
                    source_toolpaths_sha256=document.fingerprint,
                    production_machine_profile_sha256=profile.config_sha256,
                    content=content,
                )
            )
        if tuple(item.run_order for item in output) != tuple(range(1, len(output) + 1)):
            raise GCodeSafetyError("production programs are not in canonical run order")
        return tuple(output)

    def _header(
        self,
        document: ProductionToolpathDocument,
        program: ProductionProgram,
        setup: BoundSetup,
        tool: ProductionToolBinding,
        *,
        entry_machine_x_um: int,
        entry_machine_y_um: int,
    ) -> list[str]:
        return [
            "%",
            "(CUSTOMBUILD LINUXCNC 3-AXIS PRODUCTION CANDIDATE)",
            PRODUCTION_CANDIDATE_POLICY_MARKER,
            PHYSICAL_AUTHORIZATION_MARKER,
            WORKSHOP_ACCEPTANCE_MARKER,
            f"(DESIGN_HASH={document.design_hash})",
            f"(TOOLPATH_DOCUMENT_SHA256={document.fingerprint})",
            f"(MACHINE_PROFILE={document.execution_context.machine_profile_id}@"
            f"{document.execution_context.machine_profile_version})",
            f"(MACHINE_PROFILE_SHA256={document.machine_profile_fingerprint})",
            f"(LINUXCNC_PRODUCTION_PROFILE={self.machine_profile.profile_id}@"
            f"{self.machine_profile.version})",
            f"(LINUXCNC_PRODUCTION_PROFILE_SHA256={self.machine_profile.config_sha256})",
            f"(POSTPROCESSOR={self.postprocessor_id}@{self.version})",
            f"(PRODUCTION_PARSER={PRODUCTION_GCODE_PARSER_VERSION})",
            f"(PRODUCTION_SAFETY_VALIDATOR={PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION})",
            f"(PROGRAM_ID={_safe_comment(program.program_id)})",
            f"(RUN_ORDER={program.run_order})",
            f"(SETUP_ID={_safe_comment(program.setup_id)})",
            f"(STOCK_ID={_safe_comment(setup.stock_id)})",
            f"(SHEET_INDEX={setup.sheet_index})",
            f"(SETUP_SIDE={setup.side.value})",
            f"(SOURCE_MATERIAL_ID={_safe_comment(setup.source_material_id)})",
            f"(SOURCE_MATERIAL_VERSION={_safe_comment(setup.source_material_version)})",
            f"(ACTUAL_MATERIAL_ID={_safe_comment(setup.material_id)})",
            f"(ACTUAL_MATERIAL_VERSION={_safe_comment(setup.material_version)})",
            f"(MATERIAL_EVIDENCE_ID={_safe_comment(setup.material_evidence_id)})",
            f"(MATERIAL_EVIDENCE_VERSION={_safe_comment(setup.material_evidence_version)})",
            f"(MATERIAL_EVIDENCE_SHA256={setup.material_evidence_sha256})",
            f"(STOCK_THICKNESS_UM={setup.stock_thickness_um})",
            f"(SETUP_WCS={setup.wcs})",
            f"(WCS_MACHINE_ORIGIN_X_UM={setup.machine_wcs_origin.x_um})",
            f"(WCS_MACHINE_ORIGIN_Y_UM={setup.machine_wcs_origin.y_um})",
            f"(WCS_MACHINE_ORIGIN_Z_UM={setup.machine_wcs_z0_um})",
            f"(WCS_MACHINE_XY_ROTATION_MDEG={setup.machine_wcs_xy_rotation_mdeg})",
            f"(REFERENCE_SURFACE={setup.reference_surface})",
            f"(SAFE_Z_UM={setup.safe_z_um})",
            f"(PROGRAM_ENTRY_MACHINE_X_UM={entry_machine_x_um})",
            f"(PROGRAM_ENTRY_MACHINE_Y_UM={entry_machine_y_um})",
            f"(G53_TOOL_CHANGE_PATH={self.machine_profile.g53_tool_change_path})",
            "(G53_TOOL_CHANGE_CLEARANCE_EVIDENCE_SHA256="
            f"{self.machine_profile.g53_tool_change_path_clearance_evidence_sha256})",
            f"(WCS_OFFSETS_EVIDENCE_SHA256={self.machine_profile.wcs_offsets_evidence_sha256})",
            f"(G52_G92_OFFSET_RESET_POLICY={self.machine_profile.g52_g92_offset_reset_policy})",
            "(G52_G92_OFFSET_RESET_EVIDENCE_SHA256="
            f"{self.machine_profile.g52_g92_offset_reset_evidence_sha256})",
            f"(FEED_SPINDLE_OVERRIDE_POLICY={self.machine_profile.feed_spindle_override_policy})",
            "(FEED_SPINDLE_OVERRIDE_EVIDENCE_SHA256="
            f"{self.machine_profile.feed_spindle_override_evidence_sha256})",
            f"(EXTERNAL_AXIS_OFFSET_POLICY={self.machine_profile.external_axis_offset_policy})",
            "(EXTERNAL_AXIS_OFFSET_EVIDENCE_SHA256="
            f"{self.machine_profile.external_axis_offset_evidence_sha256})",
            f"(HOMING_PREFLIGHT_POLICY={self.machine_profile.homing_preflight_policy})",
            "(HOMING_PREFLIGHT_EVIDENCE_SHA256="
            f"{self.machine_profile.homing_preflight_evidence_sha256})",
            f"(PROGRAM_RESTART_POLICY={self.machine_profile.program_restart_policy})",
            "(PROGRAM_RESTART_EVIDENCE_SHA256="
            f"{self.machine_profile.program_restart_evidence_sha256})",
            f"(M6_TOOL_TABLE_POLICY={self.machine_profile.m6_tool_table_policy})",
            f"(M6_TOOL_TABLE_EVIDENCE_SHA256={self.machine_profile.m6_tool_table_evidence_sha256})",
            f"(M6_WCS_TABLE_POLICY={self.machine_profile.m6_wcs_table_policy})",
            f"(M6_WCS_TABLE_EVIDENCE_SHA256={self.machine_profile.m6_wcs_table_evidence_sha256})",
            "(METRIC_XYZ_IDENTITY_KINEMATICS_POLICY="
            f"{self.machine_profile.metric_xyz_identity_kinematics_policy})",
            "(METRIC_XYZ_IDENTITY_KINEMATICS_EVIDENCE_SHA256="
            f"{self.machine_profile.metric_xyz_identity_kinematics_evidence_sha256})",
            "(EXACTLY_THREE_JOINTS_VERIFIED=TRUE)",
            f"(SPINDLE_AT_SPEED_POLICY={self.machine_profile.spindle_at_speed_policy})",
            f"(SPINDLE_FEEDBACK_SOURCE={self.machine_profile.spindle_feedback_source})",
            "(SPINDLE_AT_SPEED_TOLERANCE_PPM="
            f"{self.machine_profile.spindle_at_speed_tolerance_ppm})",
            "(SPINDLE_AT_SPEED_EVIDENCE_SHA256="
            f"{self.machine_profile.spindle_at_speed_evidence_sha256})",
            "(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY="
            f"{self.machine_profile.continuous_spindle_speed_interlock_policy})",
            "(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_EVIDENCE_SHA256="
            f"{self.machine_profile.continuous_spindle_speed_interlock_evidence_sha256})",
            f"(SPINDLE_DWELL_ROLE={SPINDLE_DWELL_ROLE})",
            f"(FIXTURE={setup.fixture_id}@{setup.fixture_version})",
            f"(FIXTURE_SHA256={setup.fixture_sha256})",
            f"(TOOL_ID={_safe_comment(program.tool_id)}@{_safe_comment(program.tool_version)})",
            f"(CONTROLLER_TOOL_NUMBER={tool.controller_tool_number})",
            f"(LENGTH_OFFSET_NUMBER={tool.length_offset_number})",
            f"(EXPECTED_LENGTH_OFFSET_X_UM={tool.expected_length_offset_x_um})",
            f"(EXPECTED_LENGTH_OFFSET_Y_UM={tool.expected_length_offset_y_um})",
            f"(EXPECTED_LENGTH_OFFSET_Z_UM={tool.expected_length_offset_z_um})",
            f"(TOOL_TABLE_EVIDENCE_SHA256={tool.tool_table_evidence_sha256})",
        ]


def _require_exact_setup_offset_and_clearance(
    profile: LinuxCNCProductionMachineProfile,
    setup: BoundSetup,
    tool: ProductionToolBinding,
) -> None:
    offsets = tuple(offset for offset in profile.wcs_offsets if offset.wcs == setup.wcs)
    if len(offsets) != 1:
        raise GCodeSafetyError(
            f"setup WCS {setup.wcs} has no unique attested machine-coordinate offset"
        )
    offset = offsets[0]
    if (
        offset.machine_x0_um,
        offset.machine_y0_um,
        offset.machine_z0_um,
        offset.machine_xy_rotation_mdeg,
    ) != (
        setup.machine_wcs_origin.x_um,
        setup.machine_wcs_origin.y_um,
        setup.machine_wcs_z0_um,
        setup.machine_wcs_xy_rotation_mdeg,
    ):
        raise GCodeSafetyError(
            f"setup WCS {setup.wcs} differs from its attested machine-coordinate offset"
        )
    safe_machine_z_um = setup.machine_wcs_z0_um + setup.safe_z_um + tool.expected_length_offset_z_um
    if not profile.machine_z_min_um <= safe_machine_z_um <= profile.machine_z_max_um:
        raise GCodeSafetyError("G43-transformed setup safe Z leaves machine bounds")
    if profile.tool_change_z_um < safe_machine_z_um:
        raise GCodeSafetyError("attested G53 tool-change traverse is below the setup safe plane")


def _require_program_entry(
    profile: LinuxCNCProductionMachineProfile,
    setup: BoundSetup,
    program: ProductionProgram,
    tool: ProductionToolBinding,
) -> tuple[int, int]:
    if not program.moves:
        raise GCodeSafetyError("production program has no bound entry move")
    first = program.moves[0]
    if (
        first.kind is not ProductionMoveKind.RAPID
        or first.role is not ProductionMoveRole.POSITION
        or first.z_um != setup.safe_z_um
    ):
        raise GCodeSafetyError(
            "the first planned move must position at the established setup safe Z"
        )
    entry_machine_x_um = (
        setup.machine_wcs_origin.x_um + first.x_um + tool.expected_length_offset_x_um
    )
    entry_machine_y_um = (
        setup.machine_wcs_origin.y_um + first.y_um + tool.expected_length_offset_y_um
    )
    if not (
        profile.machine_x_min_um <= entry_machine_x_um <= profile.machine_x_max_um
        and profile.machine_y_min_um <= entry_machine_y_um <= profile.machine_y_max_um
    ):
        raise GCodeSafetyError("program-entry G53 XY leaves absolute machine bounds")
    return entry_machine_x_um, entry_machine_y_um


def _move_line(move: ProductionMove) -> str:
    # Keep the serializer narrowly structural: all path geometry is already
    # frozen in ProductionMove and no controller compensation is inferred here.
    kind = move.kind
    prefix = "G0" if kind is ProductionMoveKind.RAPID else "G1"
    line = f"{prefix} X{_format_mm(move.x_um)} Y{_format_mm(move.y_um)} Z{_format_mm(move.z_um)}"
    if kind is ProductionMoveKind.LINEAR:
        feed_um_min = move.feed_um_min
        if not isinstance(feed_um_min, int) or isinstance(feed_um_min, bool):
            raise GCodeSafetyError("linear production move has no integer feed")
        line += f" F{_format_mm(feed_um_min)}"
    return line


def _format_mm(value_um: int) -> str:
    if type(value_um) is not int:
        raise GCodeSafetyError("LinuxCNC coordinates and feeds must be integer micrometres")
    value = Decimal(value_um) / Decimal(1_000)
    if value == 0:
        value = Decimal(0)
    return f"{value:.3f}"


def _format_seconds(value_ms: int) -> str:
    if type(value_ms) is not int or value_ms <= 0:
        raise GCodeSafetyError("LinuxCNC dwell must be positive integer milliseconds")
    value = Decimal(value_ms) / Decimal(1_000)
    return f"{value:.3f}"


def _safe_comment(value: str) -> str:
    return value.replace("(", "[").replace(")", "]").replace("\r", " ").replace("\n", " ")


def _safe_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "program"
    if cleaned == value and len(cleaned) <= 64:
        return cleaned
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:48]}-{digest}"
