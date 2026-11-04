"""Nominal clearance of dimensioned parts against an explicitly supplied room."""

from __future__ import annotations

from itertools import combinations
from math import ceil, hypot
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from .models import FrozenModel

if TYPE_CHECKING:
    from .measured_assembly import Bounds, MeasuredAssembly, MeasuredPart

Length = Annotated[int, Field(strict=True, gt=0, le=20_000_000)]
State = Literal["fits", "blocked", "unresolved"]


class MeasuredRoom(FrozenModel):
    size_um: tuple[Length, Length, Length] = Field(
        description="Clear width, depth and height. The assembly's front and bottom extents "
        "are aligned with the room front and floor. Rectangular space without obstructions."
    )
    source: str = Field(min_length=1, max_length=600)
    rear_tool_depth_um: Length | None = None


class StockCheck(FrozenModel):
    part: str
    state: State
    margin_lbt_um: tuple[int, int, int] | None = None


class RearAccess(FrozenModel):
    joint: str
    part: str
    gap_um: float
    required_um: int
    state: State


class InstallationCheck(FrozenModel):
    state: State
    known_parts: int
    unresolved_parts: tuple[str, ...]
    extent_xyz_um: tuple[float, float, float] | None = None
    margin_xyz_um: tuple[float, float, float] | None = None
    raise_height_um: tuple[int, int] | None = None
    raise_states: tuple[State, State] = ("unresolved", "unresolved")
    rear_access: tuple[RearAccess, ...] = ()


def review_stock(part: MeasuredPart) -> StockCheck:
    if part.stock_lbt_um is None:
        return StockCheck(part=part.id, state="unresolved")
    length, width, thickness = part.stock_lbt_um
    blank_length, blank_width, blank_thickness = part.blank_lbt_um
    margin = length - blank_length, width - blank_width, thickness - blank_thickness
    return StockCheck(
        part=part.id,
        state="blocked" if min(margin) < 0 else "fits",
        margin_lbt_um=margin,
    )


def _hull(points: set[tuple[float, float]]) -> list[tuple[float, float]]:
    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    ordered = sorted(points)
    halves = []
    for sequence in (ordered, list(reversed(ordered))):
        edge: list[tuple[float, float]] = []
        for point in sequence:
            while len(edge) >= 2 and cross(edge[-2], edge[-1], point) <= 0:
                edge.pop()
            edge.append(point)
        halves.extend(edge[:-1])
    return halves


def _raise_heights(cells: list[Bounds]) -> tuple[int, int] | None:
    points = {(box[y], box[z]) for box in cells for y in (1, 4) for z in (2, 5)}
    hull = _hull(points)
    # Bound the independent all-pairs clearance calculation for unusual work files.
    if len(hull) > 512:
        return None
    maxima = [0.0, 0.0]
    for a, b in combinations(hull, 2):
        dy, dz = b[0] - a[0], b[1] - a[1]
        for index, sign in enumerate((1, -1)):
            # A*sin(theta) + B*cos(theta), over either 90-degree tipping direction.
            height = hypot(dy, dz) if sign * dy * dz > 0 else max(abs(dy), abs(dz))
            maxima[index] = max(maxima[index], height)
    return ceil(maxima[0]), ceil(maxima[1])


def _state(margin: float, incomplete: bool) -> State:
    if margin < 0:
        return "blocked"
    return "unresolved" if incomplete else "fits"


def review_installation(
    assembly: MeasuredAssembly, world: dict[str, tuple[Bounds, tuple[Bounds, ...]]]
) -> InstallationCheck | None:
    room = assembly.room
    if room is None:
        return None
    parts = {p.id: p for p in assembly.parts}
    known = {
        p.id: world[p.id][1]
        for p in assembly.parts
        if not p.frame_id and p.id in world and world[p.id][1]
    }
    missing = tuple(sorted(set(parts).difference(known)))
    cells = [cell for pieces in known.values() for cell in pieces]
    if not cells:
        return InstallationCheck(state="unresolved", known_parts=0, unresolved_parts=missing)
    lo = [min(box[i] for box in cells) for i in range(3)]
    hi = [max(box[i + 3] for box in cells) for i in range(3)]
    extent = hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]
    margin = tuple(room.size_um[i] - extent[i] for i in range(3))
    heights = _raise_heights(cells)
    rear = []
    for joint in assembly.joints:
        if joint.kind != "screw" or not joint.length_um or joint.a.part not in known:
            continue
        part = parts[joint.a.part]
        op = next((o for o in part.operations if o.id == joint.a.operation), None)
        if op is None or op.face != "back":
            continue
        assert part.origin_um is not None
        head = part.origin_um[1] + part.size_um[1] + joint.head_allowance_um
        gap = lo[1] + room.size_um[1] - head
        required = joint.length_um + (room.rear_tool_depth_um or 0)
        rear.append(
            RearAccess(
                joint=joint.id,
                part=part.id,
                gap_um=gap,
                required_um=required,
                state=_state(gap - required, bool(missing) or room.rear_tool_depth_um is None),
            )
        )
    return InstallationCheck(
        state=_state(min(margin), bool(missing)),
        known_parts=len(known),
        unresolved_parts=missing,
        extent_xyz_um=extent,
        margin_xyz_um=(margin[0], margin[1], margin[2]),
        raise_height_um=heights,
        raise_states=(
            _state(room.size_um[2] - heights[0], bool(missing)),
            _state(room.size_um[2] - heights[1], bool(missing)),
        )
        if heights is not None
        else ("unresolved", "unresolved"),
        rear_access=tuple(rear),
    )
