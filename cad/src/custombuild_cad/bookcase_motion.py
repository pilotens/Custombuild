"""Continuous straight-line timber assembly checks for the fixed shelving trial."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from custombuild_domain.models import DesignResult, PartInstance

Bounds = tuple[tuple[int, ...], tuple[int, ...]]


@dataclass(frozen=True)
class AssemblyTravel:
    step_number: int
    direction: str
    travel_um: int
    world_min_um: tuple[int, ...]
    world_max_um: tuple[int, ...]


@dataclass(frozen=True)
class AssemblySpace:
    world_min_um: tuple[int, ...]
    world_max_um: tuple[int, ...]
    steps: tuple[AssemblyTravel, ...]

    def description(self) -> str:
        lo, hi = self.world_min_um, self.world_max_um
        return (
            f"Delarnas svepta utrymme med fast stomdatum: X {lo[0] / 1000:g} till "
            f"{hi[0] / 1000:g}, Y {lo[1] / 1000:g} till {hi[1] / 1000:g}, "
            f"Z {lo[2] / 1000:g} till {hi[2] / 1000:g} mm. Vid stående montering är Z "
            "höjden. Lägg till utrymme för händer, lyfthjälp och fixturer. Vid "
            "liggande montering måste Z-sträckan rymmas på golvet."
        )


def _bounds(part: PartInstance) -> Bounds:
    pos, size = part.placement, part.finished_size
    if pos.rotation_x_mdeg or pos.rotation_y_mdeg or pos.rotation_z_mdeg:
        raise ValueError("Monteringsprovet stöder axelparallella trädelar.")
    start = (pos.x_um, pos.y_um, pos.z_um)
    return start, (pos.x_um + size.width_um, pos.y_um + size.depth_um, pos.z_um + size.height_um)


def _swept(stock: Bounds, axis: int, offset: int) -> Bounds:
    lo, hi = map(list, stock)
    lo[axis] += min(offset, 0)
    hi[axis] += max(offset, 0)
    return tuple(lo), tuple(hi)


def _envelope(boxes: list[Bounds]) -> Bounds:
    return (
        tuple(min(lo[i] for lo, _ in boxes) for i in range(3)),
        tuple(max(hi[i] for _, hi in boxes) for i in range(3)),
    )


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
    boxes = {p.part_id: _bounds(p) for p in design.parts}
    shapes = {pid: assembly.objects[pid].toCompound() for pid in boxes}
    present: set[str] = set()
    travels = []
    for step in design.assembly_graph.steps:
        if step.motion_path != (step.direction,):
            raise ValueError("Provprofilen kräver en verifierbar rak monteringsrörelse per steg.")
        direction = step.direction.value
        axis = "xyz".index(direction[-1])
        positive = direction.startswith("+")
        moving = set(step.moving_part_ids)
        present.update(step.part_ids)
        fixed = present - moving

        # Start each moving group on the free side of all previously assembled
        # components that cross its path. Every member moves the same distance.
        pairs = [
            (a, b)
            for a, b in product(sorted(moving), sorted(fixed))
            if all(
                min(boxes[a][1][i], boxes[b][1][i]) > max(boxes[a][0][i], boxes[b][0][i])
                for i in range(3)
                if i != axis
            )
        ]
        offsets = (
            boxes[a][1][axis] - boxes[b][0][axis]
            if positive
            else boxes[b][1][axis] - boxes[a][0][axis]
            for a, b in pairs
        )
        travel = max(0, max(offsets, default=0))
        start_offset = -travel if positive else travel
        sweeps = {a: _swept(boxes[a], axis, start_offset) for a in moving}
        for a, b in pairs:
            # Either sufficient proof covers the full continuous stroke: the
            # fixed timber is absent from the moving blank's entire sweep, or
            # the moving timber is absent from the fixed blank's reverse sweep.
            if _empty_in_sweep(shapes[b], boxes[b], sweeps[a]):
                continue
            if not _empty_in_sweep(shapes[a], boxes[a], _swept(boxes[b], axis, -start_offset)):
                raise ValueError(
                    f"Monteringssteg {step.step_number}: fri införingsväg för {a} mot {b} "
                    "kan inte verifieras. Ändra monteringsordning eller spårgeometri."
                )
        lo, hi = _envelope([*sweeps.values(), *(boxes[b] for b in fixed)])
        travels.append(AssemblyTravel(step.step_number, direction, travel, lo, hi))
    if present != set(boxes):
        raise ValueError("Monteringsrörelserna omfattar inte alla trädelar i modellen.")
    lo, hi = _envelope([(s.world_min_um, s.world_max_um) for s in travels])
    return AssemblySpace(lo, hi, tuple(travels))
