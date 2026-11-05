"""Projected area of declared backing pieces in a rectangular rear opening."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import FrozenModel

Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
Coord = Annotated[int, Field(strict=True, ge=-20_000_000, le=20_000_000)]


class BackOpening(FrozenModel):
    id: Key
    label: str = Field(min_length=1, max_length=150)
    target: Literal["covered", "open"]
    # Opposite corners in world X/Y/Z. The Y interval is the allowed backing layer.
    low_um: tuple[Coord, Coord, Coord]
    high_um: tuple[Coord, Coord, Coord]

    @model_validator(mode="after")
    def valid_extent(self) -> BackOpening:
        if not all(
            -20_000_000 <= a < b <= 20_000_000
            for a, b in zip(self.low_um, self.high_um, strict=True)
        ):
            raise ValueError("Bakytans gränser måste bilda en positiv volym.")
        return self


class Backing(FrozenModel):
    part_ids: tuple[Key, ...] = Field(max_length=512)
    openings: tuple[BackOpening, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_entries(self) -> Backing:
        if len(set(self.part_ids)) != len(self.part_ids) or len(
            {o.id for o in self.openings}
        ) != len(self.openings):
            raise ValueError("Ryggdelar och granskningsytor behöver unika ID:n.")
        return self


class BackCoverage(FrozenModel):
    id: str
    required_area_um2: int
    covered_area_um2: float
    exposure_on_removal_um2: dict[str, float]
    state: Literal["met", "mismatch", "unresolved"]


Bounds = tuple[float, float, float, float, float, float]
Rect = tuple[float, float, float, float, str]


def _rects(opening: BackOpening, backs: dict[str, tuple[Bounds, ...]]) -> set[Rect]:
    x, y, z = opening.low_um
    X, Y, Z = opening.high_um
    result = set()
    for part_id, cells in backs.items():
        for a, b, c, A, B, C in cells:
            if y >= B or b >= Y:
                continue
            x1, z1, x2, z2 = max(x, a), max(z, c), min(X, A), min(Z, C)
            if x1 < x2 and z1 < z2:
                result.add((x1, z1, x2, z2, part_id))
    return result


def _strip(rectangles: list[Rect], width: float, unique: dict[str, float]) -> float:
    changes: dict[float, list[tuple[str, int]]] = defaultdict(list)
    for _, z, _, Z, part in rectangles:
        changes[z].append((part, 1))
        changes[Z].append((part, -1))
    active: Counter[str] = Counter()
    area = 0.0
    for low, high in pairwise(sorted(changes)):
        for part, n in changes[low]:
            active[part] += n
            if active[part] == 0:
                del active[part]
        if active:
            amount = width * (high - low)
            area += amount
            if len(active) == 1:
                unique[next(iter(active))] += amount
    return area


def review_backing(
    declared: Backing,
    backs: dict[str, tuple[Bounds, ...]],
    unknown: set[str],
) -> tuple[BackCoverage, ...]:
    checks = []
    budget = 3_000_000
    for o in declared.openings:
        rects = _rects(o, backs)
        edges = sorted({edge for a, _, b, _, _ in rects for edge in (a, b)})
        budget -= len(rects) * len(edges)
        if budget < 0:
            raise ValueError("Ryggens geometri överstiger granskarens omfattning.")
        unique: dict[str, float] = defaultdict(float)
        covered = 0.0
        for left, right in pairwise(edges):
            strip = [item for item in rects if item[0] <= left < item[2]]
            covered += _strip(strip, right - left, unique)
        area = (o.high_um[0] - o.low_um[0]) * (o.high_um[2] - o.low_um[2])
        matches = covered == area if o.target == "covered" else covered == 0
        checks.append(
            BackCoverage(
                id=o.id,
                required_area_um2=area,
                covered_area_um2=covered,
                exposure_on_removal_um2=dict(sorted(unique.items())),
                state="unresolved" if unknown else "met" if matches else "mismatch",
            )
        )
    return tuple(checks)
