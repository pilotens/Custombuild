"""Explicit reference contours; never silently treated as orthogonal joinery."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import FrozenModel

Positive = Annotated[int, Field(strict=True, gt=0, le=20_000_000)]
Coordinate = Annotated[int, Field(strict=True, ge=0, le=20_000_000)]


class ConvexProfile(FrozenModel):
    kind: Literal["convex_prism"] = "convex_prism"
    axes: Literal["xy", "xz", "yz"]
    points_um: tuple[tuple[Coordinate, Coordinate], ...] = Field(min_length=3, max_length=32)
    source: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def convex_outline(self) -> ConvexProfile:
        points = self.points_um
        if len(set(points)) != len(points):
            raise ValueError("Profilens hörn måste vara unika.")
        signs = []
        for a, b in zip(points, (*points[1:], points[0]), strict=True):
            crosses = [
                (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
                for p in points
                if p not in (a, b)
            ]
            if not crosses or 0 in crosses or min(crosses) < 0 < max(crosses):
                raise ValueError("Profilen måste vara strikt konvex utan korsande kanter.")
            signs.append(crosses[0] > 0)
        if len(set(signs)) != 1:
            raise ValueError("Profilens kanter måste följa samma riktning.")
        return self


class RoundedProfile(FrozenModel):
    kind: Literal["rounded_rectangle"] = "rounded_rectangle"
    axes: Literal["xy", "xz", "yz"]
    radius_um: Positive
    source: str = Field(min_length=1, max_length=600)


Profile = Annotated[ConvexProfile | RoundedProfile, Field(discriminator="kind")]


def check_profile_size(profile: Profile, size: tuple[int, int, int]) -> None:
    u, v = ("xyz".index(a) for a in profile.axes)
    if isinstance(profile, RoundedProfile):
        if 2 * profile.radius_um > min(size[u], size[v]):
            raise ValueError("Hörnradien ryms inte i profilens mått.")
    elif (
        min(p[0] for p in profile.points_um) != 0
        or min(p[1] for p in profile.points_um) != 0
        or max(p[0] for p in profile.points_um) != size[u]
        or max(p[1] for p in profile.points_um) != size[v]
    ):
        raise ValueError("Profilens omslutande mått måste stämma med delens mått.")
