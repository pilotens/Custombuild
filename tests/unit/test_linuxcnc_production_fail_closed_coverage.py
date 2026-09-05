from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from custombuild_cam.production_model import ProductionMoveKind, ProductionMoveRole
from custombuild_postprocessors import (
    PHYSICAL_AUTHORIZATION_MARKER,
    PRODUCTION_CANDIDATE_POLICY_MARKER,
    WORKSHOP_ACCEPTANCE_MARKER,
    GCodeParseError,
    GCodeSafetyError,
    LinuxCNCProductionMachineProfile,
    LinuxCNCProductionPostprocessor,
    LinuxCNCWCSOffset,
    ProductionMachineProgram,
    linuxcnc_production,
    parse_production_program,
    production_parser,
    validate_production_program,
)

from tests.unit.test_linuxcnc_production_postprocessor import (
    generated,
    production_document,
    production_machine_profile,
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _profile_json(**changes: object) -> bytes:
    value = json.loads(production_machine_profile().to_json())
    value.update(changes)
    return _canonical_json(value)


def _copy_with_unsafe_attribute(value: Any, attribute: str, replacement: object) -> Any:
    copied = replace(value)
    object.__setattr__(copied, attribute, replacement)
    return copied


def _document_with_program(program: Any) -> Any:
    document = replace(production_document())
    object.__setattr__(document, "programs", (program,))
    return document


def _document_with_context_attribute(attribute: str, value: object) -> Any:
    document = replace(production_document())
    context = _copy_with_unsafe_attribute(document.execution_context, attribute, value)
    object.__setattr__(document, "execution_context", context)
    return document


def _minimal_program(*commands: str) -> str:
    return "\n".join(
        (
            "%",
            PRODUCTION_CANDIDATE_POLICY_MARKER,
            PHYSICAL_AUTHORIZATION_MARKER,
            WORKSHOP_ACCEPTANCE_MARKER,
            *commands,
            "%",
            "",
        )
    )


def _body_lines(payload: bytes | str | None = None) -> tuple[Any, ...]:
    document, machine_program = generated()
    lines = parse_production_program(payload if payload is not None else machine_program.content)
    move_count = len(document.programs[0].moves)
    return lines[-(move_count + 6) : -6]


def _round_trip(
    *,
    body_lines: tuple[Any, ...] | None = None,
    program: Any | None = None,
    **overrides: int,
) -> tuple[Any, ...]:
    document = production_document()
    arguments = {
        "setup_width_um": 1_000_000,
        "setup_height_um": 600_000,
        "stock_thickness_um": 18_000,
        "through_cut_allowance_um": 0,
        "safe_z_um": 15_000,
        "work_z_um": 100_000,
        "machine_z_min_um": -100_000,
        "machine_z_max_um": 0,
        "machine_x_min_um": 0,
        "machine_x_max_um": 1_300_000,
        "machine_y_min_um": 0,
        "machine_y_max_um": 2_500_000,
        "machine_wcs_x0_um": 0,
        "machine_wcs_y0_um": 0,
        "machine_wcs_z0_um": -60_000,
        "expected_length_offset_x_um": 0,
        "expected_length_offset_y_um": 0,
        "expected_length_offset_z_um": 40_000,
    }
    arguments.update(overrides)
    return production_parser._validate_and_round_trip_moves(
        body_lines if body_lines is not None else _body_lines(),
        program=program if program is not None else document.programs[0],
        **arguments,
    )


def _modal_lines(payload: str) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    document = production_document()
    lines = parse_production_program(payload)
    move_count = len(document.programs[0].moves)
    return lines, lines[-(move_count + 6) : -6]


def _validate_modal(payload: str) -> None:
    lines, body = _modal_lines(payload)
    production_parser._validate_modal_execution(
        lines,
        body_lines=body,
        safe_z_um=15_000,
        machine_profile=production_machine_profile(),
        entry_machine_x_um=10_000,
        entry_machine_y_um=20_000,
    )


def test_wcs_offset_rejects_unsupported_code_and_non_integer_coordinate() -> None:
    with pytest.raises(ValueError, match="canonical G54-G59"):
        LinuxCNCWCSOffset("G53", 0, 0, 0, 0)
    with pytest.raises(ValueError, match="must be an integer"):
        LinuxCNCWCSOffset("G54", False, 0, 0, 0)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"schema_version": "legacy"}, "unsupported.*schema"),
        ({"controller_id": "grbl"}, "bind LinuxCNC"),
        ({"supported_wcs": ()}, "canonical sorted subset"),
        ({"supported_wcs": ("G54", "G54")}, "canonical sorted subset"),
        ({"supported_wcs": ("G55", "G54")}, "canonical sorted subset"),
        ({"supported_wcs": ("G53",)}, "canonical sorted subset"),
        ({"machine_x_min_um": False}, "integer micrometres"),
        ({"machine_x_min_um": 1_300_000}, "minimum must be below"),
        ({"spindle_spinup_ms": True}, "spindle_spinup_ms"),
        (
            {"metric_xyz_identity_kinematics_policy": "ALLOW_NONIDENTITY"},
            "native-unit/axis/kinematics",
        ),
    ),
)
def test_machine_profile_rejects_closed_machine_contract_mutations(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(production_machine_profile(), **changes)


def test_machine_profile_rejects_noncanonical_wcs_rows_and_out_of_bounds_origin() -> None:
    profile = production_machine_profile()
    with pytest.raises(ValueError, match="exactly and canonically"):
        replace(profile, wcs_offsets=())
    with pytest.raises(ValueError, match="exactly and canonically"):
        replace(profile, wcs_offsets=tuple(reversed(profile.wcs_offsets)))
    with pytest.raises(ValueError, match="outside the declared machine bounds"):
        replace(
            profile,
            wcs_offsets=(
                replace(profile.wcs_offsets[0], machine_x0_um=1_300_001),
                profile.wcs_offsets[1],
            ),
        )


@pytest.mark.parametrize("payload", (b"", b"{", b"\xff", b"[]"))
def test_profile_loader_rejects_invalid_size_encoding_json_and_root(payload: bytes) -> None:
    with pytest.raises(ValueError):
        LinuxCNCProductionMachineProfile.from_json(payload)


def test_profile_loader_rejects_missing_field_and_malformed_collections() -> None:
    missing = json.loads(production_machine_profile().to_json())
    missing.pop("profile_id")
    with pytest.raises(ValueError, match="missing or unknown"):
        LinuxCNCProductionMachineProfile.from_json(_canonical_json(missing))

    with pytest.raises(ValueError, match="supported_wcs must be"):
        LinuxCNCProductionMachineProfile.from_json(_profile_json(supported_wcs=None))
    with pytest.raises(ValueError, match="wcs_offsets must be"):
        LinuxCNCProductionMachineProfile.from_json(_profile_json(wcs_offsets=None))


def test_profile_loader_rejects_malformed_wcs_rows() -> None:
    root = json.loads(production_machine_profile().to_json())
    root["wcs_offsets"][0] = []
    with pytest.raises(ValueError, match=r"wcs_offsets\[0\] must be"):
        LinuxCNCProductionMachineProfile.from_json(_canonical_json(root))

    root = json.loads(production_machine_profile().to_json())
    root["wcs_offsets"][0].pop("wcs")
    with pytest.raises(ValueError, match=r"wcs_offsets\[0\].*missing or unknown"):
        LinuxCNCProductionMachineProfile.from_json(_canonical_json(root))


def test_profile_loader_rejects_duplicate_keys_and_inexact_numbers() -> None:
    payload = production_machine_profile().to_json()
    duplicate = b'{"profile_id":"duplicate",' + payload[1:]
    with pytest.raises(ValueError, match="duplicate JSON key"):
        LinuxCNCProductionMachineProfile.from_json(duplicate)

    floating = payload.replace(b'"spindle_spinup_ms":2500', b'"spindle_spinup_ms":2500.0')
    with pytest.raises(ValueError, match="exact JSON integers"):
        LinuxCNCProductionMachineProfile.from_json(floating)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"profile_id": 1}, "profile_id must be a JSON string"),
        ({"machine_x_min_um": "0"}, "machine_x_min_um must be a JSON integer"),
        (
            {"exactly_three_joints_verified": 1},
            "exactly_three_joints_verified must be a JSON boolean",
        ),
    ),
)
def test_profile_loader_rejects_lossy_scalar_coercion(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        LinuxCNCProductionMachineProfile.from_json(_profile_json(**changes))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("program_id", "", "canonical identity"),
        ("filename", "", "filename is not canonical"),
        ("run_order", 0, "positive integer"),
        ("source_toolpaths_sha256", "bad", "toolpath-document hash"),
        ("production_machine_profile_sha256", "bad", "machine-profile config hash"),
        ("content", b"", "non-empty bytes"),
        ("mode", "VALIDATION_ONLY", "executable CAM candidate"),
        ("machine_executable", False, "truthfully identify executable motion"),
        ("workshop_acceptance_required", False, "acceptance cannot be bypassed"),
    ),
)
def test_machine_program_rejects_false_identity_and_authority_claims(
    field: str,
    value: object,
    message: str,
) -> None:
    _document, program = generated()
    with pytest.raises(ValueError, match=message):
        replace(program, **{field: value})


def test_parser_requires_each_policy_marker_and_executable_content() -> None:
    valid = _minimal_program("M2")
    with pytest.raises(GCodeSafetyError, match="requires exactly one"):
        parse_production_program(valid.replace(WORKSHOP_ACCEPTANCE_MARKER + "\n", ""))
    with pytest.raises(GCodeSafetyError, match="contains no executable lines"):
        parse_production_program(_minimal_program())


@pytest.mark.parametrize(
    "payload",
    (
        _minimal_program("M2").removeprefix("%\n"),
        _minimal_program("M2").replace("%\n", "%\n%\n", 1),
    ),
)
def test_parser_requires_one_percent_envelope(payload: str) -> None:
    with pytest.raises(GCodeSafetyError, match="canonical percent envelope"):
        parse_production_program(payload)


@pytest.mark.parametrize(
    "payload",
    (_minimal_program("M2").rstrip("\n"), _minimal_program("M2").replace("\n", "\r\n")),
)
def test_parser_requires_canonical_lf_termination(payload: str) -> None:
    with pytest.raises(GCodeSafetyError, match="LF lines"):
        parse_production_program(payload)


@pytest.mark.parametrize("payload", (b"\xff", "räksmörgås"))
def test_parser_rejects_non_ascii_bytes_and_text(payload: bytes | str) -> None:
    with pytest.raises(GCodeParseError, match="must be ASCII"):
        parse_production_program(payload)


def test_parser_rejects_duplicate_non_g_word() -> None:
    with pytest.raises(GCodeSafetyError, match="duplicate word"):
        parse_production_program(_minimal_program("G0 X1 X2"))


def test_parser_rejects_noncanonical_parenthesized_comment() -> None:
    with pytest.raises(GCodeSafetyError, match="non-canonical comment"):
        parse_production_program(_minimal_program("(UNDECLARED_SAFETY_CLAIM=TRUE)", "M2"))


def test_identity_and_operation_comments_are_closed_and_ordered() -> None:
    document, machine_program = generated()
    extra_comment = machine_program.content.decode().replace("\nM2\n", "\n(RUN_ORDER=1)\nM2\n")
    with pytest.raises(GCodeSafetyError, match="comments do not exactly match"):
        validate_production_program(
            extra_comment,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )

    changed_operation = machine_program.content.decode().replace(
        "(OPERATION_ID=op:panel:001:pocket)",
        "(OPERATION_ID=op:panel:999:pocket)",
    )
    with pytest.raises(GCodeSafetyError, match="operation comments"):
        production_parser._require_operation_comment_sequence(
            changed_operation,
            document.programs[0],
        )


def test_machine_profile_binding_rejects_identity_envelope_and_absolute_bounds() -> None:
    profile = production_machine_profile()
    arguments = {
        "machine_profile_id": profile.machine_profile_id,
        "machine_profile_version": profile.machine_profile_version,
        "controller_id": profile.controller_id,
        "controller_version": profile.controller_version,
        "work_width_um": profile.work_width_um,
        "work_height_um": profile.work_height_um,
        "work_z_um": profile.work_z_um,
        "machine_x_min_um": profile.machine_x_min_um,
        "machine_x_max_um": profile.machine_x_max_um,
        "machine_y_min_um": profile.machine_y_min_um,
        "machine_y_max_um": profile.machine_y_max_um,
        "machine_z_min_um": profile.machine_z_min_um,
        "machine_z_max_um": profile.machine_z_max_um,
    }
    for key, value, message in (
        ("controller_version", "different", "does not exactly match"),
        ("work_width_um", profile.work_width_um - 1, "work envelope"),
        ("machine_x_min_um", 1, "absolute machine-axis bounds"),
    ):
        changed = {**arguments, key: value}
        with pytest.raises(GCodeSafetyError, match=message):
            production_parser._require_machine_profile_binding(profile, **changed)


def test_validator_rejects_program_absent_from_document() -> None:
    document, machine_program = generated()
    absent = replace(document.programs[0], program_id="absent-program")
    with pytest.raises(GCodeSafetyError, match="absent from the toolpath document"):
        validate_production_program(
            machine_program.content,
            document=document,
            program=absent,
            machine_profile=production_machine_profile(),
        )


def test_validator_rejects_unresolved_setup_and_unattested_wcs() -> None:
    source = production_document().programs[0]
    unresolved = replace(source, setup_id="unknown-setup")
    with pytest.raises(GCodeSafetyError, match="setup or tool binding is not unique"):
        validate_production_program(
            generated()[1].content,
            document=_document_with_program(unresolved),
            program=unresolved,
            machine_profile=production_machine_profile(),
        )

    profile = _copy_with_unsafe_attribute(production_machine_profile(), "supported_wcs", ("G55",))
    document, machine_program = generated()
    with pytest.raises(GCodeSafetyError, match="not attested"):
        validate_production_program(
            machine_program.content,
            document=document,
            program=document.programs[0],
            machine_profile=profile,
        )


def test_validator_rejects_missing_wcs_row_and_unsafe_setup_transform() -> None:
    document, machine_program = generated()
    profile = _copy_with_unsafe_attribute(
        production_machine_profile(),
        "wcs_offsets",
        (production_machine_profile().wcs_offsets[1],),
    )
    with pytest.raises(GCodeSafetyError, match="no unique attested"):
        validate_production_program(
            machine_program.content,
            document=document,
            program=document.programs[0],
            machine_profile=profile,
        )

    wide_setup = _copy_with_unsafe_attribute(
        document.execution_context.setups[0],
        "stock_width_um",
        1_400_000,
    )
    wide_document = _document_with_context_attribute("setups", (wide_setup,))
    with pytest.raises(GCodeSafetyError, match="stock footprint"):
        validate_production_program(
            machine_program.content,
            document=wide_document,
            program=wide_document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_validator_rejects_parser_level_wcs_drift_and_transformed_safe_z() -> None:
    document, machine_program = generated()
    profile = production_machine_profile()
    shifted_profile = replace(
        profile,
        wcs_offsets=(
            replace(profile.wcs_offsets[0], machine_x0_um=1),
            profile.wcs_offsets[1],
        ),
    )
    with pytest.raises(GCodeSafetyError, match="differs from its attested"):
        validate_production_program(
            machine_program.content,
            document=document,
            program=document.programs[0],
            machine_profile=shifted_profile,
        )

    unsafe_setup = _copy_with_unsafe_attribute(
        document.execution_context.setups[0],
        "safe_z_um",
        30_000,
    )
    unsafe_document = _document_with_context_attribute("setups", (unsafe_setup,))
    with pytest.raises(GCodeSafetyError, match="safe Z leaves machine-coordinate bounds"):
        validate_production_program(
            machine_program.content,
            document=unsafe_document,
            program=unsafe_document.programs[0],
            machine_profile=profile,
        )


def test_validator_rejects_unsafe_z_tool_version_and_unknown_recipe() -> None:
    document, machine_program = generated()
    profile = replace(production_machine_profile(), tool_change_z_um=-46_000)
    with pytest.raises(GCodeSafetyError, match="below the setup safe plane"):
        validate_production_program(
            machine_program.content,
            document=document,
            program=document.programs[0],
            machine_profile=profile,
        )

    wrong_tool = replace(document.programs[0], tool_version="different")
    with pytest.raises(GCodeSafetyError, match="tool version differs"):
        validate_production_program(
            machine_program.content,
            document=_document_with_program(wrong_tool),
            program=wrong_tool,
            machine_profile=production_machine_profile(),
        )

    unknown_recipe = replace(document.programs[0], recipe_ids=("unknown-recipe",))
    with pytest.raises(GCodeSafetyError, match="unknown cutting recipe"):
        validate_production_program(
            machine_program.content,
            document=_document_with_program(unknown_recipe),
            program=unknown_recipe,
            machine_profile=production_machine_profile(),
        )


def test_validator_rejects_inconsistent_recipe_rpm_feed_and_entry() -> None:
    document, machine_program = generated()
    recipe = document.execution_context.recipes[0]
    inconsistent = replace(recipe, material_id="different-material")
    inconsistent_document = _document_with_context_attribute("recipes", (inconsistent,))
    with pytest.raises(GCodeSafetyError, match="recipe binding is inconsistent"):
        validate_production_program(
            machine_program.content,
            document=inconsistent_document,
            program=inconsistent_document.programs[0],
            machine_profile=production_machine_profile(),
        )

    second = replace(recipe, recipe_id="second-recipe", spindle_rpm=17_000)
    mixed_program = replace(document.programs[0], recipe_ids=(recipe.recipe_id, second.recipe_id))
    mixed_document = _document_with_context_attribute("recipes", (recipe, second))
    object.__setattr__(mixed_document, "programs", (mixed_program,))
    with pytest.raises(GCodeSafetyError, match="one exact spindle speed"):
        validate_production_program(
            machine_program.content,
            document=mixed_document,
            program=mixed_program,
            machine_profile=production_machine_profile(),
        )

    moves = list(document.programs[0].moves)
    moves[2] = replace(moves[2], feed_um_min=123)
    bad_feed_program = replace(document.programs[0], moves=tuple(moves))
    with pytest.raises(GCodeSafetyError, match="planned feed is absent"):
        validate_production_program(
            machine_program.content,
            document=_document_with_program(bad_feed_program),
            program=bad_feed_program,
            machine_profile=production_machine_profile(),
        )

    moves = list(document.programs[0].moves)
    moves[0] = replace(moves[0], x_um=1_400_000)
    bad_entry_program = replace(document.programs[0], moves=tuple(moves))
    bad_entry_document = _document_with_program(bad_entry_program)
    bad_entry_payload = (
        machine_program.content.decode()
        .replace(
            document.fingerprint,
            bad_entry_document.fingerprint,
        )
        .replace(
            "(PROGRAM_ENTRY_MACHINE_X_UM=10000)",
            "(PROGRAM_ENTRY_MACHINE_X_UM=1400000)",
        )
    )
    with pytest.raises(GCodeSafetyError, match="program-entry G53 XY"):
        validate_production_program(
            bad_entry_payload,
            document=bad_entry_document,
            program=bad_entry_program,
            machine_profile=production_machine_profile(),
        )


def test_validator_rejects_extra_structure_and_noncanonical_trailer() -> None:
    document, machine_program = generated()
    extra_line = machine_program.content.decode().replace("\nG54\n", "\nG61\nG54\n")
    with pytest.raises(GCodeSafetyError, match="unexpected lines"):
        validate_production_program(
            extra_line,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )

    changed_trailer = machine_program.content.decode().replace(
        "\nM5\nM9\nG49\nG53 G0 Z-5.000\nM2\n%\n",
        "\nM5\nM5\nG49\nG53 G0 Z-5.000\nM2\n%\n",
    )
    with pytest.raises(GCodeSafetyError, match="safety trailer"):
        validate_production_program(
            changed_trailer,
            document=document,
            program=document.programs[0],
            machine_profile=production_machine_profile(),
        )


def test_bound_preamble_rejects_length_and_initial_modal_state() -> None:
    document, machine_program = generated()
    lines = parse_production_program(machine_program.content)
    preamble = lines[: -len(document.programs[0].moves) - 6]
    signatures = tuple(production_parser._signature(line) for line in preamble)
    arguments = {
        "required_wcs": "G54",
        "controller_tool_number": 7,
        "length_offset_number": 17,
        "spindle_rpm": 18_000,
        "spindle_spinup_ms": 2_500,
    }
    with pytest.raises(GCodeSafetyError, match="preamble is incomplete"):
        production_parser._require_bound_preamble(signatures[:-1], signatures, **arguments)

    wrong_modal = ((("G", Decimal("20")),), *signatures[1:])
    with pytest.raises(GCodeSafetyError, match="modal safety preamble"):
        production_parser._require_bound_preamble(wrong_modal, signatures, **arguments)


def test_round_trip_rejects_planned_entry_words_and_motion_mode() -> None:
    document = production_document()
    moves = list(document.programs[0].moves)
    moves[0] = replace(moves[0], role=ProductionMoveRole.APPROACH)
    wrong_entry = replace(document.programs[0], moves=tuple(moves))
    with pytest.raises(GCodeSafetyError, match="first planned move"):
        _round_trip(program=wrong_entry)

    _, machine_program = generated()
    extra_feed = machine_program.content.decode().replace(
        "G0 X10.000 Y20.000 Z15.000",
        "G0 X10.000 Y20.000 Z15.000 F1.000",
        1,
    )
    with pytest.raises(GCodeSafetyError, match="non-canonical words"):
        _round_trip(body_lines=_body_lines(extra_feed))

    wrong_mode = machine_program.content.decode().replace(
        "G0 X10.000 Y20.000 Z15.000",
        "G1 X10.000 Y20.000 Z15.000",
        1,
    )
    with pytest.raises(GCodeSafetyError, match="motion mode differs"):
        _round_trip(body_lines=_body_lines(wrong_mode))


def test_round_trip_rejects_setup_travel_and_machine_xy_bounds() -> None:
    with pytest.raises(GCodeSafetyError, match="setup Z bounds"):
        _round_trip(stock_thickness_um=1_000)
    with pytest.raises(GCodeSafetyError, match="machine Z travel"):
        _round_trip(work_z_um=16_000)
    with pytest.raises(GCodeSafetyError, match="machine XY bounds"):
        _round_trip(machine_x_max_um=40_000)


def test_round_trip_rejects_unsafe_rapid_roles_and_nonpositive_feed() -> None:
    document, machine_program = generated()
    moves = list(document.programs[0].moves)
    moves[1] = replace(moves[1], z_um=-1)
    below_stock = replace(document.programs[0], moves=tuple(moves))
    changed = machine_program.content.decode().replace(
        "G0 X10.000 Y20.000 Z2.000",
        "G0 X10.000 Y20.000 Z-0.001",
    )
    with pytest.raises(GCodeSafetyError, match="rapid motion below stock top"):
        _round_trip(body_lines=_body_lines(changed), program=below_stock)

    moves = list(document.programs[0].moves)
    moves[1] = replace(moves[1], role=ProductionMoveRole.POSITION)
    wrong_approach = replace(document.programs[0], moves=tuple(moves))
    with pytest.raises(GCodeSafetyError, match="not an explicit approach"):
        _round_trip(program=wrong_approach)

    moves = list(document.programs[0].moves)
    invalid_feed = replace(moves[2])
    object.__setattr__(invalid_feed, "feed_um_min", 0)
    moves[2] = invalid_feed
    no_feed = _copy_with_unsafe_attribute(document.programs[0], "moves", tuple(moves))
    changed = machine_program.content.decode().replace("F500.000", "F0.000", 1)
    with pytest.raises(GCodeSafetyError, match="lacks a positive feed"):
        _round_trip(body_lines=_body_lines(changed), program=no_feed)


def test_word_conversion_requires_one_exact_micrometre_value() -> None:
    with pytest.raises(GCodeSafetyError, match="requires one X word"):
        production_parser._word_um({}, "X", 1)
    with pytest.raises(GCodeSafetyError, match="integer micrometres"):
        production_parser._word_um({"X": [Decimal("0.0001")]}, "X", 1)


def test_modal_validator_rejects_offset_reset_with_axes_and_early_motion() -> None:
    _, machine_program = generated()
    reset_with_axis = machine_program.content.decode().replace(
        "\nG92.1\n",
        "\nG92.1 X0.000\n",
        1,
    )
    with pytest.raises(GCodeSafetyError, match="cannot contain axis words"):
        _validate_modal(reset_with_axis)

    early_motion = machine_program.content.decode().replace("\nG21\n", "\nG53 G0 Z-5.000\n", 1)
    with pytest.raises(GCodeSafetyError, match="precedes the canonical live-state"):
        _validate_modal(early_motion)


def test_modal_validator_rejects_unsafe_m6_return_and_g43_sequence() -> None:
    _, machine_program = generated()
    wrong_m6_position = machine_program.content.decode().replace(
        "G53 G0 X100.000 Y2400.000",
        "G53 G0 X101.000 Y2400.000",
        1,
    )
    with pytest.raises(GCodeSafetyError, match="M6 requires"):
        _validate_modal(wrong_m6_position)

    missing_return_z = machine_program.content.decode()
    first = missing_return_z.index("G53 G0 Z-5.000")
    second = missing_return_z.index("G53 G0 Z-5.000", first + 1)
    missing_return_z = (
        missing_return_z[:second] + "G61" + missing_return_z[second + len("G53 G0 Z-5.000") :]
    )
    with pytest.raises(GCodeSafetyError, match="requires reasserted global clearance Z"):
        _validate_modal(missing_return_z)

    missing_entry = machine_program.content.decode().replace(
        "G53 G0 X10.000 Y20.000\nG43 H17",
        "G61\nG43 H17",
    )
    with pytest.raises(GCodeSafetyError, match="G43 requires"):
        _validate_modal(missing_entry)


def test_modal_validator_rejects_unsafe_spindle_and_missing_final_reset() -> None:
    _, machine_program = generated()
    wrong_safe_z = machine_program.content.decode().replace(
        "\nG54\nG0 Z15.000\n",
        "\nG54\nG0 Z14.000\n",
    )
    with pytest.raises(GCodeSafetyError, match="M3 requires"):
        _validate_modal(wrong_safe_z)

    spindle_off = machine_program.content.decode().replace("S18000 M3", "S18000 M5")
    with pytest.raises(GCodeSafetyError, match="dwell requires spindle on"):
        _validate_modal(spindle_off)

    no_dwell = machine_program.content.decode().replace("G4 P2.500", "G61")
    with pytest.raises(GCodeSafetyError, match="requires completed spindle spin-up"):
        _validate_modal(no_dwell)

    with pytest.raises(GCodeSafetyError, match="did not clear live G52/G92"):
        production_parser._validate_modal_execution(
            (),
            body_lines=(),
            safe_z_um=15_000,
            machine_profile=production_machine_profile(),
            entry_machine_x_um=10_000,
            entry_machine_y_um=20_000,
        )


def test_postprocessor_rejects_duplicate_or_unresolved_context_bindings() -> None:
    document = production_document()
    duplicate_context = _copy_with_unsafe_attribute(
        document.execution_context,
        "setups",
        (document.execution_context.setups[0],) * 2,
    )
    duplicate_document = replace(document)
    object.__setattr__(duplicate_document, "execution_context", duplicate_context)
    with pytest.raises(GCodeSafetyError, match="duplicate setup or tool"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(duplicate_document)

    unresolved_program = replace(document.programs[0], setup_id="unknown-setup")
    unresolved_document = _document_with_program(unresolved_program)
    with pytest.raises(GCodeSafetyError, match="unresolved setup, tool or recipe"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(unresolved_document)


def test_postprocessor_rejects_unattested_wcs_duplicate_filename_and_run_order_gap() -> None:
    document = production_document()
    unsupported_profile = _copy_with_unsafe_attribute(
        production_machine_profile(),
        "supported_wcs",
        ("G55",),
    )
    with pytest.raises(GCodeSafetyError, match="not attested"):
        LinuxCNCProductionPostprocessor(unsupported_profile).generate(document)

    duplicate_programs = replace(document)
    object.__setattr__(duplicate_programs, "programs", (document.programs[0],) * 2)
    with pytest.raises(GCodeSafetyError, match="filenames are not unique"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(duplicate_programs)

    third = replace(document.programs[0], run_order=3)
    skipped_order = replace(document)
    object.__setattr__(skipped_order, "programs", (document.programs[0], third))
    with pytest.raises(GCodeSafetyError, match="canonical run order"):
        LinuxCNCProductionPostprocessor(production_machine_profile()).generate(skipped_order)


def test_postprocessor_helpers_reject_missing_offsets_unsafe_entry_and_bad_feed() -> None:
    document = production_document()
    setup = document.execution_context.setups[0]
    tool = document.execution_context.tool_bindings[0]
    profile = _copy_with_unsafe_attribute(
        production_machine_profile(),
        "wcs_offsets",
        (production_machine_profile().wcs_offsets[1],),
    )
    with pytest.raises(GCodeSafetyError, match="no unique attested"):
        linuxcnc_production._require_exact_setup_offset_and_clearance(profile, setup, tool)

    unsafe_setup = _copy_with_unsafe_attribute(setup, "safe_z_um", 30_000)
    with pytest.raises(GCodeSafetyError, match="safe Z leaves machine bounds"):
        linuxcnc_production._require_exact_setup_offset_and_clearance(
            production_machine_profile(),
            unsafe_setup,
            tool,
        )

    empty_program = _copy_with_unsafe_attribute(document.programs[0], "moves", ())
    with pytest.raises(GCodeSafetyError, match="no bound entry move"):
        linuxcnc_production._require_program_entry(
            production_machine_profile(),
            setup,
            empty_program,
            tool,
        )

    moves = list(document.programs[0].moves)
    moves[0] = replace(moves[0], x_um=1_400_000)
    unsafe_entry = _copy_with_unsafe_attribute(document.programs[0], "moves", tuple(moves))
    with pytest.raises(GCodeSafetyError, match="program-entry G53 XY"):
        linuxcnc_production._require_program_entry(
            production_machine_profile(),
            setup,
            unsafe_entry,
            tool,
        )

    linear = replace(document.programs[0].moves[2])
    object.__setattr__(linear, "feed_um_min", None)
    with pytest.raises(GCodeSafetyError, match="no integer feed"):
        linuxcnc_production._move_line(linear)


def test_postprocessor_numeric_formatters_are_exact_and_fail_closed() -> None:
    with pytest.raises(GCodeSafetyError, match="integer micrometres"):
        linuxcnc_production._format_mm(False)
    assert linuxcnc_production._format_mm(0) == "0.000"
    with pytest.raises(GCodeSafetyError, match="positive integer milliseconds"):
        linuxcnc_production._format_seconds(0)


def test_machine_program_type_is_the_generated_contract() -> None:
    _document, program = generated()
    assert isinstance(program, ProductionMachineProgram)
    assert program.sha256
    assert program.machine_executable is True
    assert program.physical_cutting_authorized is False
    assert program.workshop_acceptance_required is True
    assert program.mode == "EXECUTABLE_CAM_CANDIDATE"
    assert program.controller == "LinuxCNC"
    assert program.content.endswith(b"%\n")
    assert program.filename.endswith(".production.ngc")
    assert program.run_order == 1
    assert program.setup_id
    assert program.tool_id
    assert program.postprocessor_id
    assert program.postprocessor_version
    assert program.source_toolpaths_sha256
    assert program.production_machine_profile_sha256
    assert program.program_id
    assert program.controller_version
    assert ProductionMoveKind.RAPID.value == "RAPID"
