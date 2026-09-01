"""Reference LinuxCNC 3-axis postprocessor in non-cutting validation mode."""

from __future__ import annotations

import re
from decimal import Decimal

from custombuild_cam import CAMValidationError, build_validation_backplot, require_valid_operations
from custombuild_manufacturing.model import MachineProfile, OperationsDocument, Setup, um_to_mm
from custombuild_manufacturing.profiles import (
    LARGE_FORMAT_MACHINE_PROFILE_ID,
    REFERENCE_MACHINE_PROFILE_ID,
    linuxcnc_reference_router_1325,
    linuxcnc_reference_router_5125,
)

from .model import MachineProgram
from .parser import EXECUTION_POLICY_MARKER, validate_validation_program


class LinuxCNCValidationPostprocessor:
    """Emit only safe-Z XY traces; never spindle-on or cutting-depth motion."""

    name = "LinuxCNC 3-axis reference validation postprocessor"
    controller = "LinuxCNC"
    version = "linuxcnc-validation-1.1.0"
    mode = "VALIDATION_DRY_RUN"

    def generate(
        self,
        document: OperationsDocument,
        *,
        machine: MachineProfile | None = None,
    ) -> tuple[MachineProgram, ...]:
        bound_machine = machine or _resolve_reference_machine(document)
        require_valid_operations(document, machine=bound_machine)
        backplot = build_validation_backplot(document, machine=bound_machine)
        programs: list[MachineProgram] = []
        for setup in document.setups:
            lines = self._preamble(document, setup)
            setup_moves = [move for move in backplot.moves if move.setup_id == setup.setup_id]
            previous_operation = ""
            for move in setup_moves:
                if move.operation_id != previous_operation:
                    lines.append(f"(OPERATION {self._safe_comment(move.operation_id)})")
                    lines.append("(CUTTING DEPTH OMITTED - VALIDATION TRACE ONLY)")
                    previous_operation = move.operation_id
                if move.x_um is None or move.y_um is None:
                    lines.append(f"G0 Z{um_to_mm(setup.safe_z_um)}")
                else:
                    lines.append(f"G0 X{um_to_mm(move.x_um)} Y{um_to_mm(move.y_um)}")
            lines.extend(
                (
                    f"G0 Z{um_to_mm(setup.safe_z_um)}",
                    "M5",
                    "M9",
                    "G92.3",
                    "M2",
                    "%",
                )
            )
            content = ("\n".join(lines) + "\n").encode("ascii")
            validate_validation_program(
                content,
                required_safe_z_mm=Decimal(um_to_mm(setup.safe_z_um)),
                maximum_z_mm=Decimal(um_to_mm(setup.safe_z_um)),
                x_bounds_mm=(Decimal(0), Decimal(um_to_mm(setup.stock_width_um))),
                y_bounds_mm=(Decimal(0), Decimal(um_to_mm(setup.stock_height_um))),
                required_wcs=setup.wcs,
            )
            programs.append(
                MachineProgram(
                    filename=f"{_safe_filename(setup.setup_id)}.validation.ngc",
                    setup_id=setup.setup_id,
                    controller=self.controller,
                    postprocessor_version=self.version,
                    mode=self.mode,
                    content=content,
                    production_approved=False,
                )
            )
        return tuple(programs)

    def _preamble(self, document: OperationsDocument, setup: Setup) -> list[str]:
        return [
            "%",
            "(CUSTOMBUILD LINUXCNC REFERENCE)",
            "(VALIDATION DRY RUN - NOT PRODUCTION APPROVED)",
            "(NO SPINDLE START; NO CUTTING Z MOVES)",
            EXECUTION_POLICY_MARKER,
            "(EXACT WCS XYZ OFFSETS AND WCS ROTATION REQUIRE EXTERNAL VERIFICATION)",
            f"(DESIGN HASH {self._safe_comment(document.design_hash)})",
            f"(SETUP {self._safe_comment(setup.setup_id)})",
            f"(ORIENTATION {self._safe_comment(setup.orientation)})",
            "G21",
            "G17 G40 G49 G80 G90 G94",
            "M5",
            "M9",
            "G92.2",
            setup.wcs,
            f"G0 Z{um_to_mm(setup.safe_z_um)}",
        ]

    @staticmethod
    def _safe_comment(value: str) -> str:
        return str(value).replace("(", "[").replace(")", "]").replace("\n", " ").replace("\r", " ")


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "setup"


def _resolve_reference_machine(document: OperationsDocument) -> MachineProfile:
    factories = {
        REFERENCE_MACHINE_PROFILE_ID: linuxcnc_reference_router_1325,
        LARGE_FORMAT_MACHINE_PROFILE_ID: linuxcnc_reference_router_5125,
    }
    factory = factories.get(document.machine_profile_id)
    if factory is None:
        raise CAMValidationError(
            "validation postprocessor cannot resolve the document machine profile"
        )
    return factory()
