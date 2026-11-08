"""Straight insertion paths for the fixed shelving trial, in world micrometres."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import product

from .models import DesignResult, PartInstance
from .units import um_to_mm

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
            f"Delarnas svepta utrymme med fast stomdatum: X {um_to_mm(lo[0])} till "
            f"{um_to_mm(hi[0])}, Y {um_to_mm(lo[1])} till {um_to_mm(hi[1])}, "
            f"Z {um_to_mm(lo[2])} till {um_to_mm(hi[2])} mm. Vid stående montering är Z "
            "höjden. Lägg till utrymme för händer, lyfthjälp och fixturer. Vid "
            "liggande montering måste Z-sträckan rymmas på golvet."
        )


def part_bounds(part: PartInstance) -> Bounds:
    pos, size = part.placement, part.finished_size
    if pos.rotation_x_mdeg or pos.rotation_y_mdeg or pos.rotation_z_mdeg:
        raise ValueError("Monteringsprovet stöder axelparallella trädelar.")
    start = (pos.x_um, pos.y_um, pos.z_um)
    return start, (pos.x_um + size.width_um, pos.y_um + size.depth_um, pos.z_um + size.height_um)


def swept_bounds(stock: Bounds, axis: int, offset: int) -> Bounds:
    lo, hi = map(list, stock)
    lo[axis] += min(offset, 0)
    hi[axis] += max(offset, 0)
    return tuple(lo), tuple(hi)


def envelope(boxes: list[Bounds]) -> Bounds:
    return (
        tuple(min(lo[i] for lo, _ in boxes) for i in range(3)),
        tuple(max(hi[i] for _, hi in boxes) for i in range(3)),
    )


@dataclass(frozen=True)
class AssemblyStroke:
    travel: AssemblyTravel
    axis: int
    start_offset_um: int
    pairs: list[tuple[str, str]]
    moving_bounds: dict[str, Bounds]


def assembly_strokes(design: DesignResult) -> Iterator[AssemblyStroke]:
    boxes = {p.part_id: part_bounds(p) for p in design.parts}
    present: set[str] = set()
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
        sweeps = {a: swept_bounds(boxes[a], axis, start_offset) for a in moving}
        lo, hi = envelope([*sweeps.values(), *(boxes[b] for b in fixed)])
        yield AssemblyStroke(
            AssemblyTravel(step.step_number, direction, travel, lo, hi),
            axis,
            start_offset,
            pairs,
            sweeps,
        )
    if present != set(boxes):
        raise ValueError("Monteringsrörelserna omfattar inte alla trädelar i modellen.")
