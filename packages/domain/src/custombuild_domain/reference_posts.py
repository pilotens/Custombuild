"""Measured, unconnected reference solids in the native bookcase coordinate system."""

from typing import Literal

from pydantic import Field, model_validator

from .models import FrozenModel, PartInstance


class ReferencePost(FrozenModel):
    key: str = Field(pattern=r"^ref-post-[0-9]{1,2}$")
    name: str = Field(min_length=1, max_length=70)
    shape: Literal["rectangular", "round"]
    x_um: int = Field(strict=True, ge=-10_000_000, le=10_000_000)
    y_um: int = Field(strict=True, ge=-10_000_000, le=10_000_000)
    z_um: int = Field(strict=True, ge=-10_000_000, le=10_000_000)
    width_um: int = Field(strict=True, gt=0, le=1_000_000)
    depth_um: int = Field(strict=True, gt=0, le=1_000_000)
    height_um: int = Field(strict=True, gt=0, le=10_000_000)

    @model_validator(mode="after")
    def round_section(self) -> "ReferencePost":
        if self.shape == "round" and self.width_um != self.depth_um:
            raise ValueError("En rund stolpe kräver samma diameter i X och Y.")
        return self


def contact(a: ReferencePost, other: ReferencePost | PartInstance) -> str | None:
    """Positive volume is overlap. Touching surfaces alone give no evidence of retention."""
    if isinstance(other, PartInstance):
        x, y, z = other.placement.x_um, other.placement.y_um, other.placement.z_um
        w, d, h = (
            other.finished_size.width_um,
            other.finished_size.depth_um,
            other.finished_size.height_um,
        )
        round_b = other.circular_outline
    else:
        x, y, z = other.x_um, other.y_um, other.z_um
        w, d, h = other.width_um, other.depth_um, other.height_um
        round_b = other.shape == "round"
    vertical = min(a.z_um + a.height_um, z + h) - max(a.z_um, z)
    dx = min(a.x_um + a.width_um, x + w) - max(a.x_um, x)
    dy = min(a.y_um + a.depth_um, y + d) - max(a.y_um, y)
    if min(vertical, dx, dy) < 0:
        return None
    planar = min(dx, dy)
    # Work in doubled micrometres so odd diameters retain their exact centres.
    if a.shape == "round" and round_b:
        cx = 2 * (a.x_um - x) + a.width_um - w
        cy = 2 * (a.y_um - y) + a.depth_um - d
        planar = (a.width_um + w) ** 2 - cx**2 - cy**2
    elif a.shape == "round" or round_b:
        if a.shape == "round":
            cx, cy, diameter = 2 * a.x_um + a.width_um, 2 * a.y_um + a.depth_um, a.width_um
            rx, ry, rw, rd = x, y, w, d
        else:
            cx, cy, diameter = 2 * x + w, 2 * y + d, w
            rx, ry, rw, rd = a.x_um, a.y_um, a.width_um, a.depth_um
        gap_x = max(2 * rx - cx, cx - 2 * (rx + rw), 0)
        gap_y = max(2 * ry - cy, cy - 2 * (ry + rd), 0)
        planar = diameter**2 - gap_x**2 - gap_y**2
    if planar < 0:
        return None
    return "overlap" if planar > 0 and vertical > 0 else "touch"
