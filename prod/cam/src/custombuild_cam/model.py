"""Validation-only toolpath and removal-envelope models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MoveKind(StrEnum):
    RETRACT = "RETRACT"
    RAPID_XY = "RAPID_XY"


@dataclass(frozen=True, slots=True)
class ValidationMove:
    sequence: int
    setup_id: str
    operation_id: str
    kind: MoveKind
    x_um: int | None
    y_um: int | None
    z_um: int


@dataclass(frozen=True, slots=True)
class ValidationBackplot:
    mode: str
    moves: tuple[ValidationMove, ...]
    omitted_cutting_operation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode != "VALIDATION_DRY_RUN":
            raise ValueError("backplot mode must remain validation-only")


@dataclass(frozen=True, slots=True)
class RemovalEnvelope:
    operation_id: str
    setup_id: str
    x_min_um: int
    y_min_um: int
    x_max_um: int
    y_max_um: int
    z_min_um: int
    z_max_um: int
    theoretical_only: bool = True
