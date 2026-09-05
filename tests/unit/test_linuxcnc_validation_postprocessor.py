from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from custombuild_manufacturing import (
    DeterministicNester,
    FeatureKind,
    ManufacturingFeature,
    OperationsDocument,
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
from custombuild_postprocessors.model import ParsedProgram
from custombuild_postprocessors.parser import EXECUTION_POLICY_MARKER


def _validate_program(
    payload: bytes | str,
    *,
    safe_z_mm: Decimal = Decimal("15"),
    maximum_z_mm: Decimal = Decimal("15"),
    x_bounds_mm: tuple[Decimal, Decimal] = (Decimal("0"), Decimal("1000")),
    y_bounds_mm: tuple[Decimal, Decimal] = (Decimal("0"), Decimal("600")),
    required_wcs: str = "G54",
) -> ParsedProgram:
    return validate_validation_program(
        payload,
        required_safe_z_mm=safe_z_mm,
        maximum_z_mm=maximum_z_mm,
        x_bounds_mm=x_bounds_mm,
        y_bounds_mm=y_bounds_mm,
        required_wcs=required_wcs,
    )


def _canonical_program(*body: str, wcs: str = "G54") -> str:
    return (
        "\n".join(
            (
                "%",
                EXECUTION_POLICY_MARKER,
                "G21",
                "G17 G40 G49 G80 G90 G94",
                "M5",
                "M9",
                "G92.2",
                wcs,
                "G0 Z15",
                *body,
                "G0 Z15",
                "M5",
                "M9",
                "G92.3",
                "M2",
                "%",
            )
        )
        + "\n"
    )


def operations_document() -> OperationsDocument:
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
    parsed = _validate_program(program.content)

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
    lines = program.content.decode("ascii").splitlines()
    assert EXECUTION_POLICY_MARKER in lines
    assert lines.index("M9") < lines.index("G92.2") < lines.index("G54")
    assert lines.index("G92.2") < lines.index("G0 Z15")
    assert lines[-5:] == ["M5", "M9", "G92.3", "M2", "%"]


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
    payload = _canonical_program(unsafe_line)

    with pytest.raises(GCodeSafetyError):
        _validate_program(payload)


def test_parser_ignores_codes_inside_comments() -> None:
    payload = _canonical_program().replace(
        EXECUTION_POLICY_MARKER,
        f"{EXECUTION_POLICY_MARKER}\n(M3 and Z-10 are documentation only)",
    )

    parsed = _validate_program(payload)

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
    payload = _canonical_program(mode_line)

    with pytest.raises(GCodeSafetyError, match=message):
        _validate_program(payload)


def test_validation_program_requires_safe_z_before_any_xy_traverse() -> None:
    payload = _canonical_program().replace("G0 Z15", "G0 X10 Y10", 1)

    with pytest.raises(GCodeSafetyError, match="without established safe Z"):
        _validate_program(payload)


def test_validation_program_rejects_z_below_configured_safe_height() -> None:
    payload = _canonical_program().replace("G0 Z15", "G0 Z14.999", 1)

    with pytest.raises(GCodeSafetyError, match="below required safe height"):
        _validate_program(payload)


def test_semicolon_comment_cannot_inject_spindle_or_cutting_words() -> None:
    payload = _canonical_program().replace("G21", "G21 ; G20", 1).replace(
        "M5", "M5 ; M3 Z-10", 1
    )

    parsed = _validate_program(payload)

    assert parsed.units == "MM"
    assert parsed.spindle_start_seen is False
    assert parsed.minimum_z_mm == Decimal("15")


@pytest.mark.parametrize(
    ("unsafe_line", "message"),
    (
        ("G0 X-0.001 Y10", "X exceeds setup bounds"),
        ("G0 X10 Y200.001", "Y exceeds setup bounds"),
        ("G0 Z15.001", "Z exceeds setup maximum"),
    ),
)
def test_validation_program_rejects_motion_outside_bound_setup(
    unsafe_line: str,
    message: str,
) -> None:
    payload = _canonical_program(unsafe_line)

    with pytest.raises(GCodeSafetyError, match=message):
        validate_validation_program(
            payload,
            required_safe_z_mm=Decimal("15"),
            maximum_z_mm=Decimal("15"),
            x_bounds_mm=(Decimal("0"), Decimal("300")),
            y_bounds_mm=(Decimal("0"), Decimal("200")),
            required_wcs="G54",
        )


@pytest.mark.parametrize(
    ("wcs_line", "message"),
    (
        ("", "motion before required WCS"),
        ("G55", "unexpected WCS"),
    ),
)
def test_validation_program_requires_the_bound_wcs(wcs_line: str, message: str) -> None:
    payload = _canonical_program().replace("\nG54\n", f"\n{wcs_line}\n", 1)

    with pytest.raises(GCodeSafetyError, match=message):
        _validate_program(
            payload,
            required_wcs="G54",
        )


@pytest.mark.parametrize("terminator", ("G0 X10 M2", "M5 M2", "M2 M2"))
def test_m2_must_be_a_single_standalone_final_terminator(terminator: str) -> None:
    payload = _canonical_program().replace("\nM2\n%\n", f"\n{terminator}\n%\n", 1)

    with pytest.raises(GCodeSafetyError, match="M2 must be the only executable word"):
        _validate_program(payload)


def test_m30_is_rejected_because_pallet_behavior_is_not_attested() -> None:
    payload = _canonical_program().replace("\nM2\n%\n", "\nM30\n%\n", 1)

    with pytest.raises(GCodeSafetyError, match="M30 is forbidden"):
        _validate_program(payload)


@pytest.mark.parametrize("unsafe_transform", ("G10", "G52", "G92", "G92.1", "G51", "G68"))
def test_validation_program_rejects_coordinate_transform_mutation(
    unsafe_transform: str,
) -> None:
    with pytest.raises(GCodeSafetyError, match="forbidden"):
        _validate_program(_canonical_program(unsafe_transform))


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.replace(f"{EXECUTION_POLICY_MARKER}\n", "", 1),
        lambda payload: payload.replace(
            EXECUTION_POLICY_MARKER,
            f"{EXECUTION_POLICY_MARKER}\n{EXECUTION_POLICY_MARKER}",
            1,
        ),
        lambda payload: payload.replace("M5\nM9\nG92.2", "M9\nM5\nG92.2", 1),
        lambda payload: payload.replace("M9\nG92.2", "G92.2\nM9", 1),
        lambda payload: payload.replace("M9\nG92.3\nM2", "G92.3\nM9\nM2", 1),
        lambda payload: payload.replace("G92.3\n", "", 1),
    ),
)
def test_validation_program_requires_exact_offset_and_coolant_order(
    mutation: Callable[[str], str],
) -> None:
    with pytest.raises(GCodeSafetyError):
        _validate_program(mutation(_canonical_program("G0 X10 Y10")))
