"""Continuous straight-line timber assembly checks for the fixed shelving trial."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from custombuild_domain.bookcase_motion import (
    AssemblySpace,
    Bounds,
    assembly_strokes,
    envelope,
    part_bounds,
    swept_bounds,
)
from custombuild_domain.models import DesignResult


def _empty_in_sweep(solid: Any, fixed: Bounds, sweep: Bounds) -> bool:
    import cadquery as cq

    lo = tuple(max(a, b) / 1000 for a, b in zip(fixed[0], sweep[0], strict=True))
    hi = tuple(min(a, b) / 1000 for a, b in zip(fixed[1], sweep[1], strict=True))
    lengths = tuple(b - a for a, b in zip(lo, hi, strict=True))
    if min(lengths) <= 0:
        return True
    region = cq.Solid.makeBox(lengths[0], lengths[1], lengths[2], cq.Vector(lo[0], lo[1], lo[2]))
    return float(solid.intersect(region).Volume()) <= 1e-6  # mm³, kernel noise at touching faces


def verify_assembly_space(design: DesignResult, step_bytes: bytes) -> AssemblySpace:
    import cadquery as cq

    with TemporaryDirectory(prefix="custombuild-assembly-motion-") as folder:
        path = Path(folder) / "timber.step"
        path.write_bytes(step_bytes)
        assembly = cq.Assembly.load(str(path))
    boxes = {p.part_id: part_bounds(p) for p in design.parts}
    shapes = {pid: assembly.objects[pid].toCompound() for pid in boxes}
    travels = []
    for stroke in assembly_strokes(design):
        for a, b in stroke.pairs:
            # Prove continuous clearance using the full sweep or the reverse sweep.
            if _empty_in_sweep(shapes[b], boxes[b], stroke.moving_bounds[a]):
                continue
            reverse = swept_bounds(boxes[b], stroke.axis, -stroke.start_offset_um)
            if not _empty_in_sweep(shapes[a], boxes[a], reverse):
                raise ValueError(
                    f"Monteringssteg {stroke.travel.step_number}: fri införingsväg för {a} mot {b} "
                    "kan inte verifieras. Ändra monteringsordning eller spårgeometri."
                )
        travels.append(stroke.travel)
    lo, hi = envelope([(s.world_min_um, s.world_max_um) for s in travels])
    return AssemblySpace(lo, hi, tuple(travels))
