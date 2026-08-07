"""Versioned reference machine and tool catalogue entries."""

from __future__ import annotations

from collections.abc import Iterable

from .model import (
    MachineProfile,
    OperationKind,
    Side,
    ToolSpec,
    canonical_json_bytes,
    sha256_hex,
)

REFERENCE_MACHINE_PROFILE_ID = "custombuild-router-1325-linuxcnc"
REFERENCE_MACHINE_PROFILE_VERSION = "1.0.0-validation"
REFERENCE_TOOL_LIBRARY_VERSION = "reference-tools-1.0.0-validation"
REFERENCE_TOOL_VERSION = "1.0.0-validation"


def tool_catalog_fingerprint(tools: Iterable[ToolSpec]) -> str:
    """Hash every value that influences tool selection, fit or feed metadata."""

    return sha256_hex(
        canonical_json_bytes(tuple(sorted(tools, key=lambda item: item.tool_id)))
    )


def linuxcnc_reference_router_1325() -> MachineProfile:
    """Named validation profile for a generic 1325-format three-axis router.

    This is a software reference profile, not evidence of calibration or
    production approval for any physical machine.
    """

    router_operations = (
        OperationKind.POCKET,
        OperationKind.GROOVE,
        OperationKind.CONTOUR,
        OperationKind.ENGRAVE,
    )
    return MachineProfile(
        profile_id=REFERENCE_MACHINE_PROFILE_ID,
        name="Custombuild Reference Router 1325 / LinuxCNC",
        version=REFERENCE_MACHINE_PROFILE_VERSION,
        controller="LinuxCNC",
        work_width_um=2_500_000,
        work_height_um=1_300_000,
        work_z_um=150_000,
        safe_z_um=15_000,
        max_spindle_rpm=24_000,
        supported_operations=tuple(OperationKind),
        supported_sides=(Side.A,),
        can_flip_stock=True,
        edge_aggregate=False,
        tools=(
            ToolSpec(
                "T05",
                "5 mm brad-point reference drill",
                5_000,
                30_000,
                (OperationKind.DRILL,),
                12_000,
                1_500_000,
                500_000,
                version=REFERENCE_TOOL_VERSION,
            ),
            ToolSpec(
                "T08D",
                "8 mm reference drill/countersink",
                8_000,
                35_000,
                (OperationKind.DRILL, OperationKind.COUNTERSINK),
                12_000,
                1_500_000,
                500_000,
                version=REFERENCE_TOOL_VERSION,
            ),
            ToolSpec(
                "T06R",
                "6 mm two-flute reference router",
                6_000,
                30_000,
                router_operations,
                18_000,
                3_000_000,
                800_000,
                version=REFERENCE_TOOL_VERSION,
            ),
            ToolSpec(
                "T08R",
                "8 mm two-flute reference router",
                8_000,
                35_000,
                router_operations,
                18_000,
                4_000_000,
                1_000_000,
                version=REFERENCE_TOOL_VERSION,
            ),
        ),
        tool_library_version=REFERENCE_TOOL_LIBRARY_VERSION,
        wcs_codes=("G54", "G55"),
        accuracy_um=100,
    )
