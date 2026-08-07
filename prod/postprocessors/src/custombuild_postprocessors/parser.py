"""Strict parser and safety validator for reference LinuxCNC dry-run code."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .model import GCodeWord, ParsedLine, ParsedProgram

GCODE_PARSER_VERSION = "linuxcnc-gcode-parser-1.0.0"
GCODE_SAFETY_VALIDATOR_VERSION = "validation-program-safety-1.0.0"


class GCodeParseError(ValueError):
    pass


class GCodeSafetyError(ValueError):
    pass


_WORD = re.compile(r"([A-Za-z])([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
_ALLOWED_G = {0, 17, 21, 40, 49, 54, 55, 56, 57, 58, 59, 80, 90, 94}
_ALLOWED_M = {5, 30}
_ALLOWED_LETTERS = {"G", "M", "N", "X", "Y", "Z"}


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
    required_safe_z_mm: Decimal = Decimal("0"),
) -> ParsedProgram:
    parsed = parse_gcode(payload)
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
    seen_m30 = False
    seen_g21 = False
    seen_g90 = False
    for line in parsed.lines:
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
            if value != value.to_integral_value() or int(value) not in _ALLOWED_G:
                raise GCodeSafetyError(
                    f"G{value} is forbidden in validation output at line {line.line_number}"
                )
            if value == 21:
                seen_g21 = True
            if value == 90:
                seen_g90 = True
        for value in letters.get("M", []):
            if value != value.to_integral_value() or int(value) not in _ALLOWED_M:
                raise GCodeSafetyError(
                    f"M{value} is forbidden in validation output at line {line.line_number}"
                )
            if value == 5:
                seen_m5 = True
            if value == 30:
                seen_m30 = True
        if any(axis in letters for axis in ("X", "Y", "Z")):
            if not seen_g21 or not seen_g90 or not seen_m5:
                raise GCodeSafetyError(
                    f"motion before G21/G90/M5 safety preamble at line {line.line_number}"
                )
            if "Z" in letters:
                if len(letters["Z"]) != 1:
                    raise GCodeSafetyError(f"duplicate Z word at line {line.line_number}")
                current_z = letters["Z"][0]
                if current_z < required_safe_z_mm:
                    raise GCodeSafetyError(
                        f"Z below required safe height at line {line.line_number}: {current_z}"
                    )
            if Decimal(0) not in letters.get("G", []):
                raise GCodeSafetyError(
                    f"axis motion without an explicit G0 at line {line.line_number}"
                )
            for axis in ("X", "Y"):
                if len(letters.get(axis, [])) > 1:
                    raise GCodeSafetyError(f"duplicate {axis} word at line {line.line_number}")
            if ("X" in letters or "Y" in letters) and (
                current_z is None or current_z < required_safe_z_mm
            ):
                raise GCodeSafetyError(
                    f"XY move without established safe Z at line {line.line_number}"
                )
        if seen_m30 and line is not parsed.lines[-1]:
            raise GCodeSafetyError("M30 must be the final executable line")
    if not seen_m30:
        raise GCodeSafetyError("validation program requires M30 terminator")
    return parsed


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
