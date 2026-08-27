from __future__ import annotations

from decimal import Decimal

import pytest
from custombuild_manufacturing import (
    DeterministicNester,
    FeatureKind,
    ManufacturingFeature,
    PartSpec,
    Side,
    StockSheet,
    generate_operations_document,
    linuxcnc_reference_router_1325,
)
from custombuild_postprocessors import (
    GCodeSafetyError,
    LinuxCNCValidationPostprocessor,
    parse_gcode,
    validate_validation_program,
)


def operations_document():
    feature = ManufacturingFeature(
        "hole",
        "panel",
        FeatureKind.DRILL,
        Side.A,
        50_000,
        50_000,
        10_000,
        diameter_um=8_000,
    )
    panel = PartSpec(
        "panel",
        "Panel",
        300_000,
        200_000,
        18_000,
        "mdf",
        "v1",
        features=(feature,),
        grain_direction="NONE",
    )
    source_stock = StockSheet(
        "sheet",
        "mdf",
        "v1",
        1_000_000,
        600_000,
        18_000,
        grain_direction="NONE",
    )
    layout = DeterministicNester().nest((panel,), source_stock)
    return generate_operations_document(
        design_hash="c" * 64,
        parts=(panel,),
        layout=layout,
        machine=linuxcnc_reference_router_1325(),
    )


def test_generated_linuxcnc_program_has_no_spindle_or_cutting_z() -> None:
    program = LinuxCNCValidationPostprocessor().generate(operations_document())[0]
    parsed = validate_validation_program(program.content, required_safe_z_mm=Decimal("15"))

    executable = "\n".join(
        line for line in program.content.decode("ascii").splitlines() if not line.startswith("(")
    )
    executable_lines = executable.splitlines()
    assert "M3" not in executable_lines
    assert "M4" not in executable_lines
    assert all(
        word.value != 1 for line in parsed.lines for word in line.words if word.letter == "G"
    )
    assert "Z-" not in executable
    assert parsed.minimum_z_mm == Decimal("15")
    assert program.production_approved is False
    assert program.mode == "VALIDATION_DRY_RUN"


@pytest.mark.parametrize(
    "unsafe_line",
    (
        "M3",
        "M4",
        "G1 X10 Y10",
        "G0 Z-0.1",
        "S18000",
        "M6",
    ),
)
def test_parser_rejects_unsafe_constructs(unsafe_line: str) -> None:
    payload = f"%\nG21\nG90\nM5\nG0 Z15\n{unsafe_line}\nM30\n%\n"

    with pytest.raises(GCodeSafetyError):
        validate_validation_program(payload, required_safe_z_mm=Decimal("15"))


def test_parser_ignores_codes_inside_comments() -> None:
    payload = "%\n(M3 and Z-10 are documentation only)\nG21\nG90\nM5\nG0 Z15\nM30\n%\n"

    parsed = validate_validation_program(payload, required_safe_z_mm=Decimal("15"))

    assert parsed.spindle_start_seen is False
    assert parse_gcode(payload).minimum_z_mm == Decimal("15")


@pytest.mark.parametrize(
    ("mode_line", "message"),
    (
        ("G20", "G21 millimetres"),
        ("G91", "G90 absolute mode"),
    ),
)
def test_validation_program_rejects_non_metric_or_incremental_modal_state(
    mode_line: str,
    message: str,
) -> None:
    payload = f"%\nG21\nG90\nM5\nG0 Z15\n{mode_line}\nM30\n%\n"

    with pytest.raises(GCodeSafetyError, match=message):
        validate_validation_program(payload, required_safe_z_mm=Decimal("15"))


def test_validation_program_requires_safe_z_before_any_xy_traverse() -> None:
    payload = "%\nG21\nG90\nM5\nG0 X10 Y10\nM30\n%\n"

    with pytest.raises(GCodeSafetyError, match="without established safe Z"):
        validate_validation_program(payload, required_safe_z_mm=Decimal("15"))


def test_validation_program_rejects_z_below_configured_safe_height() -> None:
    payload = "%\nG21\nG90\nM5\nG0 Z14.999\nM30\n%\n"

    with pytest.raises(GCodeSafetyError, match="below required safe height"):
        validate_validation_program(payload, required_safe_z_mm=Decimal("15"))


def test_semicolon_comment_cannot_inject_spindle_or_cutting_words() -> None:
    payload = "%\nG21 ; G20\nG90\nM5 ; M3 Z-10\nG0 Z15\nM30\n%\n"

    parsed = validate_validation_program(payload, required_safe_z_mm=Decimal("15"))

    assert parsed.units == "MM"
    assert parsed.spindle_start_seen is False
    assert parsed.minimum_z_mm == Decimal("15")
