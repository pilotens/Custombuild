"""Interference of finite, nominal fastener cylinders in a common assembly frame."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from typing import Literal

from .models import FrozenModel


class FastenerInterference(FrozenModel):
    joint_a: str
    joint_b: str
    state: Literal["mismatch", "unresolved"]


Interval = tuple[int, int]


@dataclass(frozen=True)
class Shaft:
    joint: str
    frame: str | None
    axis: int
    center: tuple[int, int, int]
    diameter: int
    certain: Interval
    possible: Interval

    def bounds(self, axis: int) -> Interval:
        # Doubled micrometres keep odd diameters and tangencies exact.
        if axis == self.axis:
            return (2 * self.possible[0], 2 * self.possible[1])
        return (2 * self.center[axis] - self.diameter, 2 * self.center[axis] + self.diameter)


def _distance(point: int, span: Interval) -> int:
    return max(span[0] - point, point - span[1], 0)


def _intersect(a: Shaft, b: Shaft, span_a: Interval, span_b: Interval) -> bool:
    i, j = a.axis, b.axis
    if i == j:
        distance2 = sum((a.center[n] - b.center[n]) ** 2 for n in range(3) if n != i)
        return max(span_a[0], span_b[0]) < min(span_a[1], span_b[1]) and (
            4 * distance2 < (a.diameter + b.diameter) ** 2
        )
    # For perpendicular shafts, each end cap clips the other's circular section.
    squared_a = a.diameter**2 - (2 * _distance(a.center[j], span_b)) ** 2
    squared_b = b.diameter**2 - (2 * _distance(b.center[i], span_a)) ** 2
    if min(squared_a, squared_b) <= 0:
        return False
    k = 3 - i - j
    remainder = (2 * (a.center[k] - b.center[k])) ** 2 - squared_a - squared_b
    return remainder < 0 or remainder**2 < 4 * squared_a * squared_b


def review_fasteners(shafts: list[Shaft]) -> tuple[FastenerInterference, ...]:
    shafts = sorted(shafts, key=lambda s: (s.frame or "", s.bounds(0)[0], s.joint))
    conflicts: list[FastenerInterference] = []
    for _, group in groupby(shafts, key=lambda s: s.frame):
        ordered = list(group)
        for index, first in enumerate(ordered):
            xmax = first.bounds(0)[1]
            for other_index in range(index + 1, len(ordered)):
                second = ordered[other_index]
                if second.bounds(0)[0] >= xmax:
                    break
                if not _intersect(first, second, first.possible, second.possible):
                    continue
                fixed = _intersect(first, second, first.certain, second.certain)
                a, b = sorted((first.joint, second.joint))
                conflicts.append(
                    FastenerInterference(
                        joint_a=a, joint_b=b, state="mismatch" if fixed else "unresolved"
                    )
                )
                if len(conflicts) > 2048:
                    raise ValueError("För många överlappande beslag för en fullständig granskning.")
    return tuple(sorted(conflicts, key=lambda c: (c.joint_a, c.joint_b)))
