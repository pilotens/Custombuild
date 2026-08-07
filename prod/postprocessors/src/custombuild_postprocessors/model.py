from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from custombuild_manufacturing.model import sha256_hex


@dataclass(frozen=True, slots=True)
class GCodeWord:
    letter: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class ParsedLine:
    line_number: int
    words: tuple[GCodeWord, ...]


@dataclass(frozen=True, slots=True)
class ParsedProgram:
    lines: tuple[ParsedLine, ...]
    units: str
    absolute_coordinates: bool
    spindle_start_seen: bool
    minimum_z_mm: Decimal | None


@dataclass(frozen=True, slots=True)
class MachineProgram:
    filename: str
    setup_id: str
    controller: str
    postprocessor_version: str
    mode: str
    content: bytes
    production_approved: bool = False

    def __post_init__(self) -> None:
        if self.mode != "VALIDATION_DRY_RUN":
            raise ValueError("reference program must remain validation-only")
        if self.production_approved:
            raise ValueError("validation postprocessor cannot approve a production program")

    @property
    def sha256(self) -> str:
        return sha256_hex(self.content)
