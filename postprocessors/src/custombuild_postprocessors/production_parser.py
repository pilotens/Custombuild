"""Strict LinuxCNC parser and validator for executable CAM candidates.

The validation-only parser deliberately keeps its much smaller command
allowlist.  This module accepts cutting commands only while comparing every
motion with an immutable :class:`~custombuild_cam.production_model.ProductionProgram`.
It is not an independent material-removal simulator and does not authorize a
physical machine start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from custombuild_cam.production_model import (
    BoundSetup,
    ProductionMoveKind,
    ProductionMoveRole,
    ProductionProgram,
    ProductionToolBinding,
    ProductionToolpathDocument,
)

from .model import ParsedLine
from .parser import GCodeParseError, GCodeSafetyError, parse_gcode
from .production_model import (
    LINUXCNC_PRODUCTION_POSTPROCESSOR_ID,
    LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION,
    SPINDLE_DWELL_ROLE,
    LinuxCNCProductionMachineProfile,
)

PRODUCTION_GCODE_PARSER_VERSION = "linuxcnc-production-parser-1.3.0"
PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION = "linuxcnc-production-safety-1.3.0"
# LinuxCNC's file reader uses ``fgets(buffer, LINELEN=255)`` and treats a
# 254-byte read as an overlong command.  Because our canonical files always
# use one LF byte, at most 252 ASCII bytes may precede it without entering
# that error path.  Keep this stricter, source-derived file limit here rather
# than the looser interactive-command limit.
LINUXCNC_FILE_MAX_LINE_BYTES = 252
PRODUCTION_CANDIDATE_POLICY_MARKER = "(CUSTOMBUILD_MACHINE_PROGRAM_MODE=EXECUTABLE_CAM_CANDIDATE)"
PHYSICAL_AUTHORIZATION_MARKER = "(PHYSICAL_CUTTING_AUTHORIZED=FALSE)"
WORKSHOP_ACCEPTANCE_MARKER = "(WORKSHOP_ACCEPTANCE_REQUIRED=TRUE)"

_ALLOWED_G = frozenset(
    {
        Decimal("0"),
        Decimal("1"),
        Decimal("4"),
        Decimal("8"),
        Decimal("17"),
        Decimal("21"),
        Decimal("40"),
        Decimal("43"),
        Decimal("49"),
        Decimal("53"),
        Decimal("54"),
        Decimal("55"),
        Decimal("56"),
        Decimal("57"),
        Decimal("58"),
        Decimal("59"),
        Decimal("61"),
        Decimal("80"),
        Decimal("90"),
        Decimal("92.1"),
        Decimal("94"),
        Decimal("97"),
    }
)
_ALLOWED_M = frozenset(
    {
        Decimal("2"),
        Decimal("3"),
        Decimal("5"),
        Decimal("6"),
        Decimal("9"),
        Decimal("49"),
        Decimal("52"),
        Decimal("53"),
    }
)
_ALLOWED_LETTERS = frozenset({"F", "G", "H", "M", "P", "S", "T", "X", "Y", "Z"})
_MODAL_PREAMBLE = (
    (("G", Decimal("21")),),
    (("G", Decimal("8")),),
    (
        ("G", Decimal("17")),
        ("G", Decimal("40")),
        ("G", Decimal("49")),
        ("G", Decimal("80")),
        ("G", Decimal("90")),
        ("G", Decimal("94")),
        ("G", Decimal("97")),
    ),
    (("G", Decimal("61")),),
    (("M", Decimal("5")),),
    (("M", Decimal("9")),),
    (("M", Decimal("49")),),
    (("M", Decimal("52")), ("P", Decimal("0"))),
    (("M", Decimal("53")), ("P", Decimal("1"))),
    (("G", Decimal("92.1")),),
)
_STATIC_COMMENTS = frozenset(
    {
        "(CUSTOMBUILD LINUXCNC 3-AXIS PRODUCTION CANDIDATE)",
        PRODUCTION_CANDIDATE_POLICY_MARKER,
        PHYSICAL_AUTHORIZATION_MARKER,
        WORKSHOP_ACCEPTANCE_MARKER,
    }
)
_DYNAMIC_COMMENT = re.compile(
    r"\((?:DESIGN_HASH|TOOLPATH_DOCUMENT_SHA256|MACHINE_PROFILE|"
    r"MACHINE_PROFILE_SHA256|LINUXCNC_PRODUCTION_PROFILE|"
    r"LINUXCNC_PRODUCTION_PROFILE_SHA256|POSTPROCESSOR|PRODUCTION_PARSER|"
    r"PRODUCTION_SAFETY_VALIDATOR|PROGRAM_ID|RUN_ORDER|SETUP_ID|STOCK_ID|"
    r"SHEET_INDEX|SETUP_SIDE|SOURCE_MATERIAL_ID|SOURCE_MATERIAL_VERSION|"
    r"ACTUAL_MATERIAL_ID|ACTUAL_MATERIAL_VERSION|MATERIAL_EVIDENCE_ID|"
    r"MATERIAL_EVIDENCE_VERSION|MATERIAL_EVIDENCE_SHA256|STOCK_THICKNESS_UM|SETUP_WCS|"
    r"WCS_MACHINE_ORIGIN_X_UM|WCS_MACHINE_ORIGIN_Y_UM|"
    r"WCS_MACHINE_ORIGIN_Z_UM|WCS_MACHINE_XY_ROTATION_MDEG|"
    r"REFERENCE_SURFACE|SAFE_Z_UM|"
    r"PROGRAM_ENTRY_MACHINE_X_UM|PROGRAM_ENTRY_MACHINE_Y_UM|"
    r"G53_TOOL_CHANGE_PATH|G53_TOOL_CHANGE_CLEARANCE_EVIDENCE_SHA256|"
    r"WCS_OFFSETS_EVIDENCE_SHA256|G52_G92_OFFSET_RESET_POLICY|"
    r"G52_G92_OFFSET_RESET_EVIDENCE_SHA256|"
    r"FEED_SPINDLE_OVERRIDE_POLICY|FEED_SPINDLE_OVERRIDE_EVIDENCE_SHA256|"
    r"EXTERNAL_AXIS_OFFSET_POLICY|EXTERNAL_AXIS_OFFSET_EVIDENCE_SHA256|"
    r"HOMING_PREFLIGHT_POLICY|HOMING_PREFLIGHT_EVIDENCE_SHA256|"
    r"PROGRAM_RESTART_POLICY|PROGRAM_RESTART_EVIDENCE_SHA256|"
    r"M6_TOOL_TABLE_POLICY|M6_TOOL_TABLE_EVIDENCE_SHA256|"
    r"M6_WCS_TABLE_POLICY|M6_WCS_TABLE_EVIDENCE_SHA256|"
    r"METRIC_XYZ_IDENTITY_KINEMATICS_POLICY|"
    r"METRIC_XYZ_IDENTITY_KINEMATICS_EVIDENCE_SHA256|"
    r"EXACTLY_THREE_JOINTS_VERIFIED|"
    r"SPINDLE_AT_SPEED_POLICY|SPINDLE_FEEDBACK_SOURCE|"
    r"SPINDLE_AT_SPEED_TOLERANCE_PPM|SPINDLE_AT_SPEED_EVIDENCE_SHA256|"
    r"CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY|"
    r"CONTINUOUS_SPINDLE_SPEED_INTERLOCK_EVIDENCE_SHA256|"
    r"SPINDLE_DWELL_ROLE|"
    r"FIXTURE|FIXTURE_SHA256|TOOL_ID|CONTROLLER_TOOL_NUMBER|"
    r"LENGTH_OFFSET_NUMBER|EXPECTED_LENGTH_OFFSET_X_UM|"
    r"EXPECTED_LENGTH_OFFSET_Y_UM|EXPECTED_LENGTH_OFFSET_Z_UM|"
    r"TOOL_TABLE_EVIDENCE_SHA256|"
    r"OPERATION_ID)=[A-Za-z0-9._:@+\-]+\)"
)


@dataclass(frozen=True, slots=True)
class ParsedProductionMove:
    line_number: int
    kind: ProductionMoveKind
    x_um: int
    y_um: int
    z_um: int
    feed_um_min: int | None


@dataclass(frozen=True, slots=True)
class ParsedProductionProgram:
    lines: tuple[ParsedLine, ...]
    moves: tuple[ParsedProductionMove, ...]
    wcs: str
    controller_tool_number: int
    length_offset_number: int
    spindle_rpm: int
    spindle_spinup_ms: int
    production_machine_profile_sha256: str
    physical_cutting_authorized: bool = False


def parse_production_program(payload: bytes | str) -> tuple[ParsedLine, ...]:
    """Lex and envelope-check candidate G-code without asserting a setup."""

    text = _ascii_text(payload)
    _require_canonical_lexical_form(text)
    _require_percent_envelope(text)
    for marker in (
        PRODUCTION_CANDIDATE_POLICY_MARKER,
        PHYSICAL_AUTHORIZATION_MARKER,
        WORKSHOP_ACCEPTANCE_MARKER,
    ):
        if sum(line.strip() == marker for line in text.splitlines()) != 1:
            raise GCodeSafetyError(f"production program requires exactly one {marker}")
    parsed = parse_gcode(text)
    if not parsed.lines:
        raise GCodeSafetyError("production program contains no executable lines")
    _require_whitelisted_words(parsed.lines)
    return parsed.lines


def validate_production_program(
    payload: bytes | str,
    *,
    document: ProductionToolpathDocument,
    program: ProductionProgram,
    machine_profile: LinuxCNCProductionMachineProfile,
) -> ParsedProductionProgram:
    """Validate one program and round-trip every emitted motion to its plan."""

    if program not in document.programs:
        raise GCodeSafetyError("planned program is absent from the toolpath document")
    context = document.execution_context
    _require_machine_profile_binding(
        machine_profile,
        machine_profile_id=context.machine_profile_id,
        machine_profile_version=context.machine_profile_version,
        controller_id=context.controller_id,
        controller_version=context.controller_version,
        work_width_um=context.work_width_um,
        work_height_um=context.work_height_um,
        work_z_um=context.work_z_um,
        machine_x_min_um=context.machine_x_min_um,
        machine_x_max_um=context.machine_x_max_um,
        machine_y_min_um=context.machine_y_min_um,
        machine_y_max_um=context.machine_y_max_um,
        machine_z_min_um=context.machine_z_min_um,
        machine_z_max_um=context.machine_z_max_um,
    )
    setup_matches = tuple(item for item in context.setups if item.setup_id == program.setup_id)
    tool_matches = tuple(item for item in context.tool_bindings if item.tool_id == program.tool_id)
    if len(setup_matches) != 1 or len(tool_matches) != 1:
        raise GCodeSafetyError("production program setup or tool binding is not unique")
    setup = setup_matches[0]
    tool = tool_matches[0]
    if setup.wcs not in machine_profile.supported_wcs:
        raise GCodeSafetyError(
            f"setup WCS {setup.wcs} is not attested by the production machine profile"
        )
    wcs_offsets = tuple(offset for offset in machine_profile.wcs_offsets if offset.wcs == setup.wcs)
    if len(wcs_offsets) != 1:
        raise GCodeSafetyError("setup WCS has no unique attested machine-coordinate offset")
    wcs_offset = wcs_offsets[0]
    if (
        wcs_offset.machine_x0_um,
        wcs_offset.machine_y0_um,
        wcs_offset.machine_z0_um,
        wcs_offset.machine_xy_rotation_mdeg,
    ) != (
        setup.machine_wcs_origin.x_um,
        setup.machine_wcs_origin.y_um,
        setup.machine_wcs_z0_um,
        setup.machine_wcs_xy_rotation_mdeg,
    ):
        raise GCodeSafetyError("setup WCS differs from its attested machine-coordinate offset")
    if not (
        machine_profile.machine_x_min_um
        <= setup.machine_wcs_origin.x_um + tool.expected_length_offset_x_um
        <= setup.machine_wcs_origin.x_um + setup.stock_width_um + tool.expected_length_offset_x_um
        <= machine_profile.machine_x_max_um
        and machine_profile.machine_y_min_um
        <= setup.machine_wcs_origin.y_um + tool.expected_length_offset_y_um
        <= setup.machine_wcs_origin.y_um + setup.stock_height_um + tool.expected_length_offset_y_um
        <= machine_profile.machine_y_max_um
    ):
        raise GCodeSafetyError(
            "G43-transformed setup WCS stock footprint leaves machine-coordinate bounds"
        )
    transformed_safe_z_um = (
        setup.machine_wcs_z0_um + setup.safe_z_um + tool.expected_length_offset_z_um
    )
    if (
        not machine_profile.machine_z_min_um
        <= transformed_safe_z_um
        <= (machine_profile.machine_z_max_um)
    ):
        raise GCodeSafetyError("G43-transformed setup safe Z leaves machine-coordinate bounds")
    if machine_profile.tool_change_z_um < transformed_safe_z_um:
        raise GCodeSafetyError("G53 tool-change traverse is below the setup safe plane")
    if tool.tool_version != program.tool_version:
        raise GCodeSafetyError("production program tool version differs from its binding")

    recipes_by_id = {recipe.recipe_id: recipe for recipe in context.recipes}
    try:
        recipes = tuple(recipes_by_id[recipe_id] for recipe_id in program.recipe_ids)
    except KeyError as exc:
        raise GCodeSafetyError("production program references an unknown cutting recipe") from exc
    if any(
        recipe.tool_id != program.tool_id
        or recipe.tool_version != program.tool_version
        or recipe.material_id != setup.material_id
        or recipe.material_version != setup.material_version
        for recipe in recipes
    ):
        raise GCodeSafetyError("production program recipe binding is inconsistent")
    spindle_rpms = {recipe.spindle_rpm for recipe in recipes}
    if len(spindle_rpms) != 1:
        raise GCodeSafetyError(
            "one setup/tool program requires one exact spindle speed across its recipes"
        )
    spindle_rpm = next(iter(spindle_rpms))
    allowed_feeds = {
        value for recipe in recipes for value in (recipe.feed_um_min, recipe.plunge_um_min)
    }
    for move in program.moves:
        if move.kind is ProductionMoveKind.LINEAR and move.feed_um_min not in allowed_feeds:
            raise GCodeSafetyError("planned feed is absent from the bound cutting recipes")

    text = _ascii_text(payload)
    _require_identity_header(
        text,
        document=document,
        program=program,
        machine_profile=machine_profile,
        setup=setup,
        tool=tool,
    )
    _require_operation_comment_sequence(text, program)
    lines = parse_production_program(text)
    signatures = tuple(_signature(line) for line in lines)
    wcs_number = Decimal(setup.wcs.removeprefix("G"))
    safe_z_mm = _um_as_mm(setup.safe_z_um)
    tool_change_x_mm = _um_as_mm(machine_profile.tool_change_x_um)
    tool_change_y_mm = _um_as_mm(machine_profile.tool_change_y_um)
    tool_change_z_mm = _um_as_mm(machine_profile.tool_change_z_um)
    first_move = program.moves[0]
    entry_machine_x_um = (
        setup.machine_wcs_origin.x_um + first_move.x_um + tool.expected_length_offset_x_um
    )
    entry_machine_y_um = (
        setup.machine_wcs_origin.y_um + first_move.y_um + tool.expected_length_offset_y_um
    )
    if not (
        machine_profile.machine_x_min_um <= entry_machine_x_um <= machine_profile.machine_x_max_um
        and machine_profile.machine_y_min_um
        <= entry_machine_y_um
        <= machine_profile.machine_y_max_um
    ):
        raise GCodeSafetyError("program-entry G53 XY leaves absolute machine bounds")
    entry_machine_x_mm = _um_as_mm(entry_machine_x_um)
    entry_machine_y_mm = _um_as_mm(entry_machine_y_um)
    spinup_seconds = _ms_as_seconds(machine_profile.spindle_spinup_ms)
    expected_preamble = (
        *_MODAL_PREAMBLE,
        (("G", Decimal("53")), ("G", Decimal("0")), ("Z", tool_change_z_mm)),
        (
            ("G", Decimal("53")),
            ("G", Decimal("0")),
            ("X", tool_change_x_mm),
            ("Y", tool_change_y_mm),
        ),
        (
            ("T", Decimal(tool.controller_tool_number)),
            ("M", Decimal("6")),
        ),
        *_MODAL_PREAMBLE,
        (("G", Decimal("53")), ("G", Decimal("0")), ("Z", tool_change_z_mm)),
        (
            ("G", Decimal("53")),
            ("G", Decimal("0")),
            ("X", entry_machine_x_mm),
            ("Y", entry_machine_y_mm),
        ),
        (
            ("G", Decimal("43")),
            ("H", Decimal(tool.length_offset_number)),
        ),
        (("G", wcs_number),),
        (("G", Decimal("0")), ("Z", safe_z_mm)),
        (("S", Decimal(spindle_rpm)), ("M", Decimal("3"))),
        (("G", Decimal("4")), ("P", spinup_seconds)),
    )
    expected_trailer = (
        (("G", Decimal("0")), ("Z", safe_z_mm)),
        (("M", Decimal("5")),),
        (("M", Decimal("9")),),
        (("G", Decimal("49")),),
        (("G", Decimal("53")), ("G", Decimal("0")), ("Z", tool_change_z_mm)),
        (("M", Decimal("2")),),
    )
    if len(signatures) != len(expected_preamble) + len(program.moves) + len(expected_trailer):
        raise GCodeSafetyError("production program executable structure has unexpected lines")

    _require_bound_preamble(
        signatures[: len(expected_preamble)],
        expected_preamble,
        required_wcs=setup.wcs,
        controller_tool_number=tool.controller_tool_number,
        length_offset_number=tool.length_offset_number,
        spindle_rpm=spindle_rpm,
        spindle_spinup_ms=machine_profile.spindle_spinup_ms,
    )
    if signatures[-len(expected_trailer) :] != expected_trailer:
        raise GCodeSafetyError("production program safety trailer is not canonical")

    body_lines = lines[len(expected_preamble) : -len(expected_trailer)]
    parsed_moves = _validate_and_round_trip_moves(
        body_lines,
        program=program,
        setup_width_um=setup.stock_width_um,
        setup_height_um=setup.stock_height_um,
        stock_thickness_um=setup.stock_thickness_um,
        through_cut_allowance_um=setup.through_cut_allowance_um,
        safe_z_um=setup.safe_z_um,
        work_z_um=context.work_z_um,
        machine_z_min_um=machine_profile.machine_z_min_um,
        machine_z_max_um=machine_profile.machine_z_max_um,
        machine_x_min_um=machine_profile.machine_x_min_um,
        machine_x_max_um=machine_profile.machine_x_max_um,
        machine_y_min_um=machine_profile.machine_y_min_um,
        machine_y_max_um=machine_profile.machine_y_max_um,
        machine_wcs_x0_um=setup.machine_wcs_origin.x_um,
        machine_wcs_y0_um=setup.machine_wcs_origin.y_um,
        machine_wcs_z0_um=setup.machine_wcs_z0_um,
        expected_length_offset_x_um=tool.expected_length_offset_x_um,
        expected_length_offset_y_um=tool.expected_length_offset_y_um,
        expected_length_offset_z_um=tool.expected_length_offset_z_um,
    )
    _validate_modal_execution(
        lines,
        body_lines=body_lines,
        safe_z_um=setup.safe_z_um,
        machine_profile=machine_profile,
        entry_machine_x_um=entry_machine_x_um,
        entry_machine_y_um=entry_machine_y_um,
    )
    return ParsedProductionProgram(
        lines=lines,
        moves=parsed_moves,
        wcs=setup.wcs,
        controller_tool_number=tool.controller_tool_number,
        length_offset_number=tool.length_offset_number,
        spindle_rpm=spindle_rpm,
        spindle_spinup_ms=machine_profile.spindle_spinup_ms,
        production_machine_profile_sha256=machine_profile.config_sha256,
    )


def _require_bound_preamble(
    actual: tuple[tuple[tuple[str, Decimal], ...], ...],
    expected: tuple[tuple[tuple[str, Decimal], ...], ...],
    *,
    required_wcs: str,
    controller_tool_number: int,
    length_offset_number: int,
    spindle_rpm: int,
    spindle_spinup_ms: int,
) -> None:
    if len(actual) != len(expected):
        raise GCodeSafetyError("production program safety preamble is incomplete")
    if actual[: len(_MODAL_PREAMBLE)] != _MODAL_PREAMBLE:
        raise GCodeSafetyError("production program modal safety preamble is not canonical")
    outbound_index = len(_MODAL_PREAMBLE)
    post_m6_state_index = outbound_index + 3
    return_index = post_m6_state_index + len(_MODAL_PREAMBLE)
    if actual[post_m6_state_index:return_index] != _MODAL_PREAMBLE:
        raise GCodeSafetyError("post-M6 canonical state reassertion is incomplete")
    checks = (
        (
            actual[outbound_index],
            expected[outbound_index],
            "wrong outbound G53 tool-change Z position",
        ),
        (
            actual[outbound_index + 1],
            expected[outbound_index + 1],
            "wrong G53 tool-change XY position",
        ),
        (
            actual[outbound_index + 2],
            expected[outbound_index + 2],
            f"wrong T/M6 tool selection; required T{controller_tool_number}",
        ),
        (
            actual[return_index],
            expected[return_index],
            "wrong post-M6 G53 return Z position",
        ),
        (
            actual[return_index + 1],
            expected[return_index + 1],
            "wrong post-M6 G53 program-entry XY position",
        ),
        (
            actual[return_index + 2],
            expected[return_index + 2],
            f"wrong G43 H length offset; required H{length_offset_number}",
        ),
        (
            actual[return_index + 3],
            expected[return_index + 3],
            f"unexpected WCS; required {required_wcs}",
        ),
        (
            actual[return_index + 4],
            expected[return_index + 4],
            "setup safe Z must be established after G43",
        ),
        (
            actual[return_index + 5],
            expected[return_index + 5],
            f"wrong spindle command; required S{spindle_rpm} M3",
        ),
        (
            actual[return_index + 6],
            expected[return_index + 6],
            f"wrong spindle spin-up dwell; required {spindle_spinup_ms} ms",
        ),
    )
    for value, required, message in checks:
        if value != required:
            raise GCodeSafetyError(message)


def _validate_and_round_trip_moves(
    body_lines: tuple[ParsedLine, ...],
    *,
    program: ProductionProgram,
    setup_width_um: int,
    setup_height_um: int,
    stock_thickness_um: int,
    through_cut_allowance_um: int,
    safe_z_um: int,
    work_z_um: int,
    machine_z_min_um: int,
    machine_z_max_um: int,
    machine_x_min_um: int,
    machine_x_max_um: int,
    machine_y_min_um: int,
    machine_y_max_um: int,
    machine_wcs_x0_um: int,
    machine_wcs_y0_um: int,
    machine_wcs_z0_um: int,
    expected_length_offset_x_um: int,
    expected_length_offset_y_um: int,
    expected_length_offset_z_um: int,
) -> tuple[ParsedProductionMove, ...]:
    output: list[ParsedProductionMove] = []
    if (
        program.moves[0].kind is not ProductionMoveKind.RAPID
        or program.moves[0].role is not ProductionMoveRole.POSITION
        or program.moves[0].z_um != safe_z_um
    ):
        raise GCodeSafetyError(
            "the first planned move must position at the established setup safe Z"
        )
    previous_x_um = 0
    previous_y_um = 0
    previous_z_um = safe_z_um
    for line, expected in zip(body_lines, program.moves, strict=True):
        words = _words_by_letter(line)
        expected_words = {"G", "X", "Y", "Z"} | (
            {"F"} if expected.kind is ProductionMoveKind.LINEAR else set()
        )
        if set(words) != expected_words:
            raise GCodeSafetyError(f"motion has non-canonical words at line {line.line_number}")
        required_g = Decimal("0" if expected.kind is ProductionMoveKind.RAPID else "1")
        if words["G"] != [required_g]:
            raise GCodeSafetyError(f"motion mode differs from plan at line {line.line_number}")
        x_um = _word_um(words, "X", line.line_number)
        y_um = _word_um(words, "Y", line.line_number)
        z_um = _word_um(words, "Z", line.line_number)
        feed_um_min = (
            _word_um(words, "F", line.line_number)
            if expected.kind is ProductionMoveKind.LINEAR
            else None
        )
        if not 0 <= x_um <= setup_width_um or not 0 <= y_um <= setup_height_um:
            raise GCodeSafetyError(f"motion leaves setup XY bounds at line {line.line_number}")
        if z_um > safe_z_um or z_um < -(stock_thickness_um + through_cut_allowance_um):
            raise GCodeSafetyError(f"motion leaves setup Z bounds at line {line.line_number}")
        if safe_z_um + max(0, -z_um) > work_z_um:
            raise GCodeSafetyError(f"motion exceeds machine Z travel at line {line.line_number}")
        machine_x_um = machine_wcs_x0_um + x_um + expected_length_offset_x_um
        machine_y_um = machine_wcs_y0_um + y_um + expected_length_offset_y_um
        machine_z_um = machine_wcs_z0_um + z_um + expected_length_offset_z_um
        if not (
            machine_x_min_um <= machine_x_um <= machine_x_max_um
            and machine_y_min_um <= machine_y_um <= machine_y_max_um
        ):
            raise GCodeSafetyError(
                f"motion leaves G43-transformed machine XY bounds at line {line.line_number}"
            )
        if not machine_z_min_um <= machine_z_um <= machine_z_max_um:
            raise GCodeSafetyError(
                f"motion leaves G43-transformed machine Z bounds at line {line.line_number}"
            )
        xy_changes = x_um != previous_x_um or y_um != previous_y_um
        if expected.kind is ProductionMoveKind.RAPID:
            if z_um < 0:
                raise GCodeSafetyError(f"rapid motion below stock top at line {line.line_number}")
            if xy_changes and (previous_z_um < safe_z_um or z_um < safe_z_um):
                raise GCodeSafetyError(f"rapid XY motion below safe Z at line {line.line_number}")
            if z_um < safe_z_um and expected.role not in {
                ProductionMoveRole.APPROACH,
                ProductionMoveRole.PECK_RETRACT,
            }:
                raise GCodeSafetyError(
                    f"rapid below safe Z is not an explicit approach at line {line.line_number}"
                )
            if expected.role is ProductionMoveRole.RETRACT and z_um != safe_z_um:
                raise GCodeSafetyError(
                    f"rapid retract must reach safe Z at line {line.line_number}"
                )
        elif feed_um_min is None or feed_um_min <= 0:
            raise GCodeSafetyError(f"feed motion lacks a positive feed at line {line.line_number}")
        actual_identity = (
            x_um,
            y_um,
            z_um,
            feed_um_min,
        )
        expected_identity = (
            expected.x_um,
            expected.y_um,
            expected.z_um,
            expected.feed_um_min,
        )
        if actual_identity != expected_identity:
            raise GCodeSafetyError(f"motion differs from immutable plan at line {line.line_number}")
        output.append(
            ParsedProductionMove(
                line.line_number,
                expected.kind,
                x_um,
                y_um,
                z_um,
                feed_um_min,
            )
        )
        previous_x_um, previous_y_um, previous_z_um = x_um, y_um, z_um
    return tuple(output)


def _validate_modal_execution(
    lines: tuple[ParsedLine, ...],
    *,
    body_lines: tuple[ParsedLine, ...],
    safe_z_um: int,
    machine_profile: LinuxCNCProductionMachineProfile,
    entry_machine_x_um: int,
    entry_machine_y_um: int,
) -> None:
    body_numbers = {line.line_number for line in body_lines}
    spindle_on = False
    tool_loaded = False
    length_compensation = False
    spindle_spinup_complete = False
    g52_g92_offsets_cleared = False
    coolant_off = False
    feed_and_spindle_overrides_disabled = False
    adaptive_feed_disabled = False
    feed_hold_enabled = False
    rpm_mode = False
    radius_mode = False
    post_m6_clearance_z_reasserted = False
    program_entry_reached_at_clearance = False
    current_wcs_z_um: int | None = None
    current_machine_x_um: int | None = None
    current_machine_y_um: int | None = None
    current_machine_z_um: int | None = None
    for line in lines:
        words = _words_by_letter(line)
        g_words = words.get("G", [])
        has_axis_motion = any(axis in words for axis in ("X", "Y", "Z"))
        if Decimal("92.1") in g_words:
            if has_axis_motion:
                raise GCodeSafetyError("G92.1 offset reset cannot contain axis words")
            g52_g92_offsets_cleared = True
        if Decimal("97") in g_words:
            rpm_mode = True
        if Decimal("8") in g_words:
            radius_mode = True
        runtime_modes_ready = (
            g52_g92_offsets_cleared
            and coolant_off
            and feed_and_spindle_overrides_disabled
            and adaptive_feed_disabled
            and feed_hold_enabled
            and rpm_mode
            and radius_mode
        )
        if has_axis_motion and not runtime_modes_ready:
            raise GCodeSafetyError(
                f"motion precedes the canonical live-state preflight at line {line.line_number}"
            )
        uses_machine_coordinates = Decimal("53") in g_words
        if uses_machine_coordinates:
            if "X" in words:
                current_machine_x_um = _word_um(words, "X", line.line_number)
            if "Y" in words:
                current_machine_y_um = _word_um(words, "Y", line.line_number)
            if "Z" in words:
                current_machine_z_um = _word_um(words, "Z", line.line_number)
                if tool_loaded and current_machine_z_um == machine_profile.tool_change_z_um:
                    post_m6_clearance_z_reasserted = True
            if tool_loaded and ("X" in words or "Y" in words):
                if (
                    not post_m6_clearance_z_reasserted
                    or current_machine_z_um != machine_profile.tool_change_z_um
                ):
                    raise GCodeSafetyError(
                        "post-M6 G53 XY return requires reasserted global clearance Z"
                    )
                program_entry_reached_at_clearance = (
                    current_machine_x_um == entry_machine_x_um
                    and current_machine_y_um == entry_machine_y_um
                )
        elif "Z" in words:
            current_wcs_z_um = _word_um(words, "Z", line.line_number)
        if words.get("M") == [Decimal("5")]:
            spindle_on = False
        if words.get("M") == [Decimal("9")]:
            coolant_off = True
        if words.get("M") == [Decimal("49")]:
            feed_and_spindle_overrides_disabled = True
        if words.get("M") == [Decimal("52")] and words.get("P") == [Decimal("0")]:
            adaptive_feed_disabled = True
        if words.get("M") == [Decimal("53")] and words.get("P") == [Decimal("1")]:
            feed_hold_enabled = True
        if Decimal("6") in words.get("M", []):
            at_tool_change_position = (
                current_machine_x_um == machine_profile.tool_change_x_um
                and current_machine_y_um == machine_profile.tool_change_y_um
                and current_machine_z_um == machine_profile.tool_change_z_um
            )
            if spindle_on or not coolant_off or length_compensation or not at_tool_change_position:
                raise GCodeSafetyError(
                    "M6 requires spindle/coolant off, G49 and the exact G53 tool-change position"
                )
            tool_loaded = True
            # An M6 implementation is controller-remappable.  Preserve only
            # the separately attested axis position and require every modal,
            # I/O and persistent-offset assumption to be re-established before
            # the first post-M6 motion.
            spindle_on = False
            length_compensation = False
            spindle_spinup_complete = False
            g52_g92_offsets_cleared = False
            coolant_off = False
            feed_and_spindle_overrides_disabled = False
            adaptive_feed_disabled = False
            feed_hold_enabled = False
            rpm_mode = False
            radius_mode = False
            current_wcs_z_um = None
        if Decimal("43") in g_words:
            if (
                not tool_loaded
                or not program_entry_reached_at_clearance
                or current_machine_z_um != machine_profile.tool_change_z_um
            ):
                raise GCodeSafetyError(
                    "G43 requires the exact tool at the G53 program entry and clearance Z"
                )
            length_compensation = True
        if Decimal("49") in g_words:
            length_compensation = False
        if Decimal("3") in words.get("M", []):
            spindle_words = words.get("S", [])
            if (
                not tool_loaded
                or not length_compensation
                or len(spindle_words) != 1
                or spindle_words[0] <= 0
                or current_wcs_z_um != safe_z_um
                or not feed_and_spindle_overrides_disabled
                or not adaptive_feed_disabled
                or not feed_hold_enabled
                or not rpm_mode
            ):
                raise GCodeSafetyError(
                    "M3 requires loaded tool, G43 H, setup safe Z and positive S"
                )
            spindle_on = True
            spindle_spinup_complete = False
        if Decimal("4") in g_words:
            if not spindle_on:
                raise GCodeSafetyError("G4 spindle dwell requires spindle on")
            spindle_spinup_complete = True
        if line.line_number in body_numbers:
            if not spindle_on or not spindle_spinup_complete:
                raise GCodeSafetyError(
                    f"planned motion requires completed spindle spin-up at line {line.line_number}"
                )
            if current_wcs_z_um is not None and current_wcs_z_um < 0 and not spindle_on:
                raise GCodeSafetyError(f"negative Z requires spindle on at line {line.line_number}")
    if not g52_g92_offsets_cleared:
        raise GCodeSafetyError("production program did not clear live G52/G92 offsets")


def _require_whitelisted_words(lines: tuple[ParsedLine, ...]) -> None:
    for line in lines:
        letters = {word.letter for word in line.words}
        unknown = letters - _ALLOWED_LETTERS
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise GCodeSafetyError(
                f"word(s) {rendered} are forbidden in production output at line {line.line_number}"
            )
        for word in line.words:
            if word.letter == "G" and word.value not in _ALLOWED_G:
                raise GCodeSafetyError(
                    f"G{word.value} is forbidden in production output at line {line.line_number}"
                )
            if word.letter == "M" and word.value not in _ALLOWED_M:
                raise GCodeSafetyError(
                    f"M{word.value} is forbidden in production output at line {line.line_number}"
                )
        by_letter = _words_by_letter(line)
        if any(len(values) != 1 for letter, values in by_letter.items() if letter != "G"):
            raise GCodeSafetyError(f"duplicate word at line {line.line_number}")


def _require_identity_header(
    text: str,
    *,
    document: ProductionToolpathDocument,
    program: ProductionProgram,
    machine_profile: LinuxCNCProductionMachineProfile,
    setup: BoundSetup,
    tool: ProductionToolBinding,
) -> None:
    entry_machine_x_um = (
        setup.machine_wcs_origin.x_um + program.moves[0].x_um + tool.expected_length_offset_x_um
    )
    entry_machine_y_um = (
        setup.machine_wcs_origin.y_um + program.moves[0].y_um + tool.expected_length_offset_y_um
    )
    expected = (
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
        f"(LINUXCNC_PRODUCTION_PROFILE={machine_profile.profile_id}@{machine_profile.version})",
        f"(LINUXCNC_PRODUCTION_PROFILE_SHA256={machine_profile.config_sha256})",
        f"(POSTPROCESSOR={LINUXCNC_PRODUCTION_POSTPROCESSOR_ID}@"
        f"{LINUXCNC_PRODUCTION_POSTPROCESSOR_VERSION})",
        f"(PRODUCTION_PARSER={PRODUCTION_GCODE_PARSER_VERSION})",
        f"(PRODUCTION_SAFETY_VALIDATOR={PRODUCTION_GCODE_SAFETY_VALIDATOR_VERSION})",
        f"(PROGRAM_ID={program.program_id})",
        f"(RUN_ORDER={program.run_order})",
        f"(SETUP_ID={program.setup_id})",
        f"(STOCK_ID={setup.stock_id})",
        f"(SHEET_INDEX={setup.sheet_index})",
        f"(SETUP_SIDE={setup.side.value})",
        f"(SOURCE_MATERIAL_ID={setup.source_material_id})",
        f"(SOURCE_MATERIAL_VERSION={setup.source_material_version})",
        f"(ACTUAL_MATERIAL_ID={setup.material_id})",
        f"(ACTUAL_MATERIAL_VERSION={setup.material_version})",
        f"(MATERIAL_EVIDENCE_ID={setup.material_evidence_id})",
        f"(MATERIAL_EVIDENCE_VERSION={setup.material_evidence_version})",
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
        f"(G53_TOOL_CHANGE_PATH={machine_profile.g53_tool_change_path})",
        "(G53_TOOL_CHANGE_CLEARANCE_EVIDENCE_SHA256="
        f"{machine_profile.g53_tool_change_path_clearance_evidence_sha256})",
        f"(WCS_OFFSETS_EVIDENCE_SHA256={machine_profile.wcs_offsets_evidence_sha256})",
        f"(G52_G92_OFFSET_RESET_POLICY={machine_profile.g52_g92_offset_reset_policy})",
        "(G52_G92_OFFSET_RESET_EVIDENCE_SHA256="
        f"{machine_profile.g52_g92_offset_reset_evidence_sha256})",
        f"(FEED_SPINDLE_OVERRIDE_POLICY={machine_profile.feed_spindle_override_policy})",
        "(FEED_SPINDLE_OVERRIDE_EVIDENCE_SHA256="
        f"{machine_profile.feed_spindle_override_evidence_sha256})",
        f"(EXTERNAL_AXIS_OFFSET_POLICY={machine_profile.external_axis_offset_policy})",
        "(EXTERNAL_AXIS_OFFSET_EVIDENCE_SHA256="
        f"{machine_profile.external_axis_offset_evidence_sha256})",
        f"(HOMING_PREFLIGHT_POLICY={machine_profile.homing_preflight_policy})",
        f"(HOMING_PREFLIGHT_EVIDENCE_SHA256={machine_profile.homing_preflight_evidence_sha256})",
        f"(PROGRAM_RESTART_POLICY={machine_profile.program_restart_policy})",
        f"(PROGRAM_RESTART_EVIDENCE_SHA256={machine_profile.program_restart_evidence_sha256})",
        f"(M6_TOOL_TABLE_POLICY={machine_profile.m6_tool_table_policy})",
        f"(M6_TOOL_TABLE_EVIDENCE_SHA256={machine_profile.m6_tool_table_evidence_sha256})",
        f"(M6_WCS_TABLE_POLICY={machine_profile.m6_wcs_table_policy})",
        f"(M6_WCS_TABLE_EVIDENCE_SHA256={machine_profile.m6_wcs_table_evidence_sha256})",
        "(METRIC_XYZ_IDENTITY_KINEMATICS_POLICY="
        f"{machine_profile.metric_xyz_identity_kinematics_policy})",
        "(METRIC_XYZ_IDENTITY_KINEMATICS_EVIDENCE_SHA256="
        f"{machine_profile.metric_xyz_identity_kinematics_evidence_sha256})",
        "(EXACTLY_THREE_JOINTS_VERIFIED=TRUE)",
        f"(SPINDLE_AT_SPEED_POLICY={machine_profile.spindle_at_speed_policy})",
        f"(SPINDLE_FEEDBACK_SOURCE={machine_profile.spindle_feedback_source})",
        f"(SPINDLE_AT_SPEED_TOLERANCE_PPM={machine_profile.spindle_at_speed_tolerance_ppm})",
        f"(SPINDLE_AT_SPEED_EVIDENCE_SHA256={machine_profile.spindle_at_speed_evidence_sha256})",
        "(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_POLICY="
        f"{machine_profile.continuous_spindle_speed_interlock_policy})",
        "(CONTINUOUS_SPINDLE_SPEED_INTERLOCK_EVIDENCE_SHA256="
        f"{machine_profile.continuous_spindle_speed_interlock_evidence_sha256})",
        f"(SPINDLE_DWELL_ROLE={SPINDLE_DWELL_ROLE})",
        f"(FIXTURE={setup.fixture_id}@{setup.fixture_version})",
        f"(FIXTURE_SHA256={setup.fixture_sha256})",
        f"(TOOL_ID={program.tool_id}@{program.tool_version})",
        f"(CONTROLLER_TOOL_NUMBER={tool.controller_tool_number})",
        f"(LENGTH_OFFSET_NUMBER={tool.length_offset_number})",
        f"(EXPECTED_LENGTH_OFFSET_X_UM={tool.expected_length_offset_x_um})",
        f"(EXPECTED_LENGTH_OFFSET_Y_UM={tool.expected_length_offset_y_um})",
        f"(EXPECTED_LENGTH_OFFSET_Z_UM={tool.expected_length_offset_z_um})",
        f"(TOOL_TABLE_EVIDENCE_SHA256={tool.tool_table_evidence_sha256})",
    )
    nonblank = tuple(line.strip() for line in text.splitlines() if line.strip())
    if nonblank[: len(expected)] != expected:
        raise GCodeSafetyError(
            "production program identity header does not match its toolpath plan"
        )
    expected_operation_comments: list[str] = []
    previous_operation = ""
    for move in program.moves:
        if move.operation_id != previous_operation:
            expected_operation_comments.append(f"(OPERATION_ID={move.operation_id})")
            previous_operation = move.operation_id
    actual_comments = tuple(line for line in nonblank if line.startswith("("))
    expected_comments = (*expected[1:], *expected_operation_comments)
    if actual_comments != expected_comments:
        raise GCodeSafetyError(
            "production program comments do not exactly match its immutable plan"
        )


def _require_percent_envelope(text: str) -> None:
    nonblank = tuple(line.strip() for line in text.splitlines() if line.strip())
    percent_indices = tuple(index for index, line in enumerate(nonblank) if line == "%")
    if (
        len(nonblank) < 2
        or nonblank[0] != "%"
        or nonblank[-1] != "%"
        or percent_indices != (0, len(nonblank) - 1)
    ):
        raise GCodeSafetyError("production program requires one canonical percent envelope")


def _require_canonical_lexical_form(text: str) -> None:
    if "\r" in text or not text.endswith("\n"):
        raise GCodeSafetyError("production program must use LF lines and one terminal newline")
    lines = text.splitlines()
    if not lines or any(not line or line != line.strip() for line in lines):
        raise GCodeSafetyError("production program contains blank or padded lines")
    for line_number, line in enumerate(lines, start=1):
        if len(line.encode("ascii")) > LINUXCNC_FILE_MAX_LINE_BYTES:
            raise GCodeSafetyError(
                f"production program command exceeds LinuxCNC file line limit at line {line_number}"
            )
        if ";" in line:
            raise GCodeSafetyError("semicolon comments are forbidden in production output")
        if "(" in line or ")" in line:
            if line not in _STATIC_COMMENTS and _DYNAMIC_COMMENT.fullmatch(line) is None:
                raise GCodeSafetyError("production program contains a non-canonical comment")
            continue
        if line != line.upper():
            raise GCodeSafetyError("production executable words must use canonical uppercase")


def _require_operation_comment_sequence(text: str, program: ProductionProgram) -> None:
    actual = tuple(
        line.removeprefix("(OPERATION_ID=").removesuffix(")")
        for line in text.splitlines()
        if line.startswith("(OPERATION_ID=")
    )
    expected: list[str] = []
    previous = ""
    for move in program.moves:
        if move.operation_id != previous:
            expected.append(move.operation_id)
            previous = move.operation_id
    if actual != tuple(expected):
        raise GCodeSafetyError("operation comments do not match immutable move order")


def _ascii_text(payload: bytes | str) -> str:
    try:
        if isinstance(payload, bytes):
            return payload.decode("ascii")
        payload.encode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise GCodeParseError("production machine program must be ASCII") from exc
    return payload


def _signature(line: ParsedLine) -> tuple[tuple[str, Decimal], ...]:
    return tuple((word.letter, word.value) for word in line.words)


def _words_by_letter(line: ParsedLine) -> dict[str, list[Decimal]]:
    output: dict[str, list[Decimal]] = {}
    for word in line.words:
        output.setdefault(word.letter, []).append(word.value)
    return output


def _word_um(words: dict[str, list[Decimal]], letter: str, line_number: int) -> int:
    values = words.get(letter, [])
    if len(values) != 1:
        raise GCodeSafetyError(f"motion requires one {letter} word at line {line_number}")
    scaled = values[0] * 1_000
    if scaled != scaled.to_integral_value():
        raise GCodeSafetyError(
            f"{letter} cannot be represented as integer micrometres at line {line_number}"
        )
    return int(scaled)


def _um_as_mm(value_um: int) -> Decimal:
    return Decimal(value_um) / Decimal(1_000)


def _ms_as_seconds(value_ms: int) -> Decimal:
    return Decimal(value_ms) / Decimal(1_000)


def _require_machine_profile_binding(
    profile: LinuxCNCProductionMachineProfile,
    *,
    machine_profile_id: str,
    machine_profile_version: str,
    controller_id: str,
    controller_version: str,
    work_width_um: int,
    work_height_um: int,
    work_z_um: int,
    machine_x_min_um: int,
    machine_x_max_um: int,
    machine_y_min_um: int,
    machine_y_max_um: int,
    machine_z_min_um: int,
    machine_z_max_um: int,
) -> None:
    if (
        profile.machine_profile_id,
        profile.machine_profile_version,
        profile.controller_id,
        profile.controller_version,
    ) != (
        machine_profile_id,
        machine_profile_version,
        controller_id,
        controller_version,
    ):
        raise GCodeSafetyError(
            "LinuxCNC production machine profile does not exactly match the toolpath context"
        )
    if (profile.work_width_um, profile.work_height_um, profile.work_z_um) != (
        work_width_um,
        work_height_um,
        work_z_um,
    ):
        raise GCodeSafetyError(
            "LinuxCNC machine-axis bounds do not match the toolpath work envelope"
        )
    if (
        profile.machine_x_min_um,
        profile.machine_x_max_um,
        profile.machine_y_min_um,
        profile.machine_y_max_um,
        profile.machine_z_min_um,
        profile.machine_z_max_um,
    ) != (
        machine_x_min_um,
        machine_x_max_um,
        machine_y_min_um,
        machine_y_max_um,
        machine_z_min_um,
        machine_z_max_um,
    ):
        raise GCodeSafetyError(
            "LinuxCNC absolute machine-axis bounds do not match the toolpath context"
        )
