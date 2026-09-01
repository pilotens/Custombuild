"""Strict parser and safety validator for reference LinuxCNC dry-run code."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .model import GCodeWord, ParsedLine, ParsedProgram

GCODE_PARSER_VERSION = "linuxcnc-gcode-parser-1.2.0"
GCODE_SAFETY_VALIDATOR_VERSION = "validation-program-safety-1.2.0"
EXECUTION_POLICY_MARKER = (
    "(CUSTOMBUILD_EXECUTION_POLICY="
    "PROHIBITED_UNTIL_EXACT_WCS_AND_CONTROLLER_STATE_ATTESTED)"
)


class GCodeParseError(ValueError):
    pass


class GCodeSafetyError(ValueError):
    pass


_WORD = re.compile(r"([A-Za-z])([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
_ALLOWED_G = {
    Decimal("0"),
    Decimal("17"),
    Decimal("21"),
    Decimal("40"),
    Decimal("49"),
    Decimal("54"),
    Decimal("55"),
    Decimal("56"),
    Decimal("57"),
    Decimal("58"),
    Decimal("59"),
    Decimal("80"),
    Decimal("90"),
    Decimal("92.2"),
    Decimal("92.3"),
    Decimal("94"),
}
_ALLOWED_M = {Decimal("2"), Decimal("5"), Decimal("9")}
_ALLOWED_LETTERS = {"G", "M", "N", "X", "Y", "Z"}
_CANONICAL_MODAL_PREAMBLE = (
    (("G", Decimal("21")),),
    (
        ("G", Decimal("17")),
        ("G", Decimal("40")),
        ("G", Decimal("49")),
        ("G", Decimal("80")),
        ("G", Decimal("90")),
        ("G", Decimal("94")),
    ),
    (("M", Decimal("5")),),
    (("M", Decimal("9")),),
    (("G", Decimal("92.2")),),
)


def parse_gcode(payload: bytes | str) -> ParsedProgram:
    try:
        text = payload.decode("ascii") if isinstance(payload, bytes) else payload
    except UnicodeDecodeError as exc:
        raise GCodeParseError("machine program must be ASCII") from exc

    lines: list[ParsedLine] = []
    units = "UNKNOWN"
    absolute = False
    spindle_start_seen = False
    minimum_z: Decimal | None = None
    for line_number, raw in enumerate(text.splitlines(), start=1):
        code = _strip_comments(raw, line_number).strip()
        if not code or code == "%":
            continue
        words = _parse_words(code, line_number)
        for word in words:
            if word.letter == "G" and word.value == 21:
                units = "MM"
            elif word.letter == "G" and word.value == 20:
                units = "INCH"
            elif word.letter == "G" and word.value == 90:
                absolute = True
            elif word.letter == "G" and word.value == 91:
                absolute = False
            elif word.letter == "M" and word.value in {3, 4}:
                spindle_start_seen = True
            elif word.letter == "Z":
                minimum_z = word.value if minimum_z is None else min(minimum_z, word.value)
        lines.append(ParsedLine(line_number, words))
    return ParsedProgram(tuple(lines), units, absolute, spindle_start_seen, minimum_z)


def validate_validation_program(
    payload: bytes | str,
    *,
    required_safe_z_mm: Decimal,
    maximum_z_mm: Decimal,
    x_bounds_mm: tuple[Decimal, Decimal],
    y_bounds_mm: tuple[Decimal, Decimal],
    required_wcs: str,
) -> ParsedProgram:
    """Validate a non-cutting program against its exact setup envelope.

    A program without exact safe-Z, travel and WCS constraints is not a safety-
    validated program.  Historical evidence that lacks these inputs may still
    be parsed with :func:`parse_gcode`, but it must not pass this validator.
    """

    _validate_bounds("X", x_bounds_mm)
    _validate_bounds("Y", y_bounds_mm)
    if maximum_z_mm < required_safe_z_mm:
        raise GCodeSafetyError("maximum Z cannot be below the required safe Z")
    required_wcs_number = _parse_required_wcs(required_wcs)

    parsed = parse_gcode(payload)
    _require_canonical_program_envelope(payload, parsed)
    if parsed.units != "MM":
        raise GCodeSafetyError("validation program must explicitly select G21 millimetres")
    if not parsed.absolute_coordinates:
        raise GCodeSafetyError("validation program must explicitly remain in G90 absolute mode")
    if parsed.spindle_start_seen:
        raise GCodeSafetyError("spindle start M3/M4 is forbidden in validation output")
    if parsed.minimum_z_mm is not None and parsed.minimum_z_mm < 0:
        raise GCodeSafetyError("negative Z is forbidden in validation output")

    current_z: Decimal | None = None
    seen_m5 = False
    seen_m2 = False
    seen_g21 = False
    seen_g90 = False
    active_wcs: int | None = None
    m2_count = 0
    offsets_suspended = False
    offsets_restored = False
    for line in parsed.lines:
        if seen_m2:
            raise GCodeSafetyError("M2 must be the final executable line")
        words = line.words
        letters: dict[str, list[Decimal]] = {}
        for word in words:
            letters.setdefault(word.letter, []).append(word.value)
        unknown_letters = set(letters) - _ALLOWED_LETTERS
        if unknown_letters:
            unknown = ", ".join(sorted(unknown_letters))
            raise GCodeSafetyError(
                f"word(s) {unknown} are forbidden in validation output at line {line.line_number}"
            )
        for value in letters.get("G", []):
            if value not in _ALLOWED_G:
                raise GCodeSafetyError(
                    f"G{value} is forbidden in validation output at line {line.line_number}"
                )
            if value == 21:
                seen_g21 = True
            if value == 90:
                seen_g90 = True
            if 54 <= value <= 59:
                if active_wcs is not None and active_wcs != int(value):
                    raise GCodeSafetyError(
                        f"WCS changes are forbidden in validation output at line "
                        f"{line.line_number}"
                    )
                active_wcs = int(value)
                if active_wcs != required_wcs_number:
                    raise GCodeSafetyError(
                        f"unexpected WCS G{active_wcs} at line {line.line_number}"
                    )
            if value == Decimal("92.2"):
                if offsets_suspended or offsets_restored:
                    raise GCodeSafetyError("G92.2 offset suspension must occur exactly once")
                offsets_suspended = True
            if value == Decimal("92.3"):
                if not offsets_suspended or offsets_restored:
                    raise GCodeSafetyError("G92.3 must restore one suspended offset state")
                offsets_restored = True
        for value in letters.get("M", []):
            if value not in _ALLOWED_M:
                raise GCodeSafetyError(
                    f"M{value} is forbidden in validation output at line {line.line_number}"
                )
            if value == 5:
                seen_m5 = True
            if value == 2:
                seen_m2 = True
                m2_count += 1
        if seen_m2:
            executable_letters = set(letters) - {"N", "M"}
            if (
                m2_count != 1
                or letters.get("M") != [Decimal(2)]
                or executable_letters
            ):
                raise GCodeSafetyError(
                    f"M2 must be the only executable word at line {line.line_number}"
                )
        if any(axis in letters for axis in ("X", "Y", "Z")):
            if not seen_g21 or not seen_g90 or not seen_m5:
                raise GCodeSafetyError(
                    f"motion before G21/G90/M5 safety preamble at line {line.line_number}"
                )
            if active_wcs != required_wcs_number:
                raise GCodeSafetyError(
                    f"motion before required WCS {required_wcs} at line {line.line_number}"
                )
            if not offsets_suspended or offsets_restored:
                raise GCodeSafetyError(
                    f"motion outside the suspended G52/G92 window at line {line.line_number}"
                )
            if "Z" in letters:
                if len(letters["Z"]) != 1:
                    raise GCodeSafetyError(f"duplicate Z word at line {line.line_number}")
                current_z = letters["Z"][0]
                if current_z < required_safe_z_mm:
                    raise GCodeSafetyError(
                        f"Z below required safe height at line {line.line_number}: {current_z}"
                    )
                if current_z > maximum_z_mm:
                    raise GCodeSafetyError(
                        f"Z exceeds setup maximum at line {line.line_number}: {current_z}"
                    )
            if Decimal(0) not in letters.get("G", []):
                raise GCodeSafetyError(
                    f"axis motion without an explicit G0 at line {line.line_number}"
                )
            for axis in ("X", "Y"):
                if len(letters.get(axis, [])) > 1:
                    raise GCodeSafetyError(f"duplicate {axis} word at line {line.line_number}")
            _validate_axis_words("X", letters.get("X", []), x_bounds_mm, line.line_number)
            _validate_axis_words("Y", letters.get("Y", []), y_bounds_mm, line.line_number)
            if ("X" in letters or "Y" in letters) and (
                current_z is None or current_z < required_safe_z_mm
            ):
                raise GCodeSafetyError(
                    f"XY move without established safe Z at line {line.line_number}"
                )
    if m2_count != 1:
        raise GCodeSafetyError("validation program requires M2 terminator")
    if not offsets_suspended or not offsets_restored:
        raise GCodeSafetyError("validation program must suspend and restore G52/G92 offsets")
    _require_canonical_safety_structure(
        parsed,
        required_safe_z_mm=required_safe_z_mm,
        required_wcs_number=required_wcs_number,
    )
    return parsed


def _require_canonical_program_envelope(
    payload: bytes | str,
    parsed: ParsedProgram,
) -> None:
    try:
        text = payload.decode("ascii") if isinstance(payload, bytes) else payload
        text.encode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise GCodeParseError("machine program must be ASCII") from exc

    nonblank = [
        (index, line.strip())
        for index, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]
    percent_lines = [index for index, line in nonblank if line == "%"]
    if (
        len(nonblank) < 2
        or nonblank[0][1] != "%"
        or nonblank[-1][1] != "%"
        or percent_lines != [nonblank[0][0], nonblank[-1][0]]
    ):
        raise GCodeSafetyError("validation program requires one canonical percent envelope")

    marker_lines = [
        index for index, line in nonblank if line == EXECUTION_POLICY_MARKER
    ]
    first_executable_line = parsed.lines[0].line_number if parsed.lines else 0
    if len(marker_lines) != 1 or marker_lines[0] >= first_executable_line:
        raise GCodeSafetyError(
            "validation program requires one preamble execution-prohibition marker"
        )


def _require_canonical_safety_structure(
    parsed: ParsedProgram,
    *,
    required_safe_z_mm: Decimal,
    required_wcs_number: int,
) -> None:
    signatures = tuple(_line_signature(line) for line in parsed.lines)
    expected_preamble = (
        *_CANONICAL_MODAL_PREAMBLE,
        (("G", Decimal(required_wcs_number)),),
        (("G", Decimal("0")), ("Z", required_safe_z_mm)),
    )
    expected_trailer = (
        (("G", Decimal("0")), ("Z", required_safe_z_mm)),
        (("M", Decimal("5")),),
        (("M", Decimal("9")),),
        (("G", Decimal("92.3")),),
        (("M", Decimal("2")),),
    )
    if len(signatures) < len(expected_preamble) + len(expected_trailer):
        raise GCodeSafetyError("validation program is missing its canonical safety structure")
    if signatures[: len(expected_preamble)] != expected_preamble:
        raise GCodeSafetyError("validation program safety preamble is not canonical")
    if signatures[-len(expected_trailer) :] != expected_trailer:
        raise GCodeSafetyError("validation program safety trailer is not canonical")

    motion_signatures = signatures[len(expected_preamble) : -len(expected_trailer)]
    for signature in motion_signatures:
        letters = tuple(letter for letter, _value in signature)
        if (
            not signature
            or signature[0] != ("G", Decimal("0"))
            or any(letter not in {"G", "X", "Y", "Z"} for letter in letters)
            or letters.count("G") != 1
            or not any(letter in {"X", "Y", "Z"} for letter in letters)
        ):
            raise GCodeSafetyError(
                "validation program body may contain only explicit G0 axis moves"
            )


def _line_signature(line: ParsedLine) -> tuple[tuple[str, Decimal], ...]:
    return tuple((word.letter, word.value) for word in line.words)


def _validate_bounds(
    axis: str,
    bounds: tuple[Decimal, Decimal],
) -> None:
    if bounds[0] > bounds[1]:
        raise GCodeSafetyError(f"{axis} bounds are reversed")


def _validate_axis_words(
    axis: str,
    values: list[Decimal],
    bounds: tuple[Decimal, Decimal],
    line_number: int,
) -> None:
    if not values:
        return
    minimum, maximum = bounds
    value = values[0]
    if value < minimum or value > maximum:
        raise GCodeSafetyError(
            f"{axis} exceeds setup bounds at line {line_number}: {value}"
        )


def _parse_required_wcs(required_wcs: str) -> int:
    match = re.fullmatch(r"G(5[4-9])", required_wcs)
    if match is None:
        raise GCodeSafetyError(f"unsupported required WCS: {required_wcs}")
    return int(match.group(1))


def _strip_comments(raw: str, line_number: int) -> str:
    output: list[str] = []
    depth = 0
    for character in raw:
        if character == "(" and depth == 0:
            depth = 1
            continue
        if character == "(" and depth:
            raise GCodeParseError(f"nested comment at line {line_number}")
        if character == ")":
            if depth == 0:
                raise GCodeParseError(f"unmatched comment close at line {line_number}")
            depth = 0
            continue
        if character == ";" and depth == 0:
            break
        if depth == 0:
            output.append(character)
    if depth:
        raise GCodeParseError(f"unclosed comment at line {line_number}")
    return "".join(output)


def _parse_words(code: str, line_number: int) -> tuple[GCodeWord, ...]:
    words: list[GCodeWord] = []
    position = 0
    while position < len(code):
        while position < len(code) and code[position].isspace():
            position += 1
        if position >= len(code):
            break
        match = _WORD.match(code, position)
        if not match:
            raise GCodeParseError(f"unsupported token at line {line_number}: {code[position:]!r}")
        letter, raw_value = match.groups()
        try:
            value = Decimal(raw_value)
        except InvalidOperation as exc:
            raise GCodeParseError(f"invalid numeric word at line {line_number}") from exc
        words.append(GCodeWord(letter.upper(), value))
        position = match.end()
    if not words:
        raise GCodeParseError(f"empty executable line at {line_number}")
    return tuple(words)
