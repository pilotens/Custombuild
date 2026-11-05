"""Continuous clearance for one open door and stationary wood, using supplied poses."""

from math import cos, hypot, radians, sin
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .measured_assembly import MeasuredAssembly
from .models import FrozenModel


class HingePose(FrozenModel):
    angle_deg: int = Field(strict=True, ge=0, le=110)
    dx_mm: float = Field(strict=True, ge=-100, le=100, allow_inf_nan=False)
    dy_mm: float = Field(strict=True, ge=-100, le=100, allow_inf_nan=False)


class HingePath(FrozenModel):
    article: Literal["70T3750.TL", "70T3650.TL"]
    plate: Literal["175H7100"]
    front_mm: Literal[19]
    cup_distance_mm: Literal[6, 7]
    source: Annotated[str, Field(min_length=1, max_length=180, pattern=r"\S")]
    poses: tuple[HingePose, ...] = Field(min_length=23, max_length=111)

    @model_validator(mode="after")
    def complete_motion(self) -> "HingePath":
        expected = 7 if self.article == "70T3750.TL" else 6
        if self.cup_distance_mm != expected:
            raise ValueError("Fel koppavstånd för gångjärnsartikeln.")
        start = self.poses[0]
        if (start.angle_deg, start.dx_mm, start.dy_mm) != (0, 0, 0):
            raise ValueError("Banan ska börja i luckans stängda läge.")
        if self.poses[-1].angle_deg != 110 or any(
            not 0 < b.angle_deg - a.angle_deg <= 5
            for a, b in zip(self.poses, self.poses[1:], strict=False)
        ):
            raise ValueError("Banan ska täcka 0–110 grader i stigande steg om högst 5 grader.")
        return self


class MotionResult(FrozenModel):
    door_id: str
    article: str
    state: Literal["missing_path", "clear_in_model", "envelope_contact", "inconclusive"]
    obstacle: str | None = None
    angle_deg: float | None = None
    source: str | None = None


def _separate(
    x: float,
    y: float,
    w: float,
    t: float,
    obstacles: list[tuple[str, float, float, float, float]],
    angle: float,
    tx: float,
    ty: float,
) -> tuple[float, str]:
    c, s = cos(radians(angle)), sin(radians(angle))
    cx = x + tx + (w * c + t * s) / 2
    cy = y + ty + (-w * s + t * c) / 2
    best = (float("inf"), "")
    for key, ox, oy, hw, hd in obstacles:
        dx, dy = ox - cx, oy - cy
        # Max separating-axis gap is a conservative distance lower bound.
        sep = max(
            abs(dx) - hw - (w * abs(c) + t * abs(s)) / 2,
            abs(dy) - hd - (w * abs(s) + t * abs(c)) / 2,
            abs(dx * c - dy * s) - w / 2 - hw * abs(c) - hd * abs(s),
            abs(dx * s + dy * c) - t / 2 - hw * abs(s) - hd * abs(c),
        )
        best = min(best, (sep, key))
    return best


def door_motion(
    assembly: MeasuredAssembly, paths: tuple[HingePath, ...], margin: float
) -> tuple[MotionResult, ...]:
    parts = {p.id: p for p in assembly.parts}
    by_article: dict[str, HingePath] = {p.article: p for p in paths}
    results = []
    for i in range(1, 6):
        door = parts[f"DOOR-{i}"]
        article = next(
            f.article for f in assembly.fittings if f.kind == "hinge" and f.connects[0] == door.id
        )
        path = by_article.get(article)
        if path is None:
            results.append(MotionResult(door_id=door.id, article=article, state="missing_path"))
            continue
        assert door.origin_um is not None
        x, y, z = (v / 1000 for v in door.origin_um)
        w, t, h = (v / 1000 for v in door.size_um)
        radius = hypot(w, t)
        obstacles = []
        for other in parts.values():
            if other.id == door.id or other.origin_um is None:
                continue
            ox, oy, oz = (v / 1000 for v in other.origin_um)
            ow, od, oh = (v / 1000 for v in other.size_um)
            # Strict separation in Z holds over the entire horizontal motion.
            if oz - (z + h) > margin or z - (oz + oh) > margin:
                continue
            obstacles.append((other.id, ox + ow / 2, oy + od / 2, ow / 2, od / 2))

        outcome = MotionResult(
            door_id=door.id, article=article, state="clear_in_model", source=path.source
        )
        # Translations are linear between poses. Bound movement of *every* point
        # by translation + angular travel times radius, not just sampled positions.
        for a, b in zip(path.poses, path.poses[1:], strict=False):
            turn = b.angle_deg - a.angle_deg
            dx, dy = b.dx_mm - a.dx_mm, b.dy_mm - a.dy_mm
            travel = hypot(dx, dy) + radius * radians(turn)
            pending = [(0.0, 1.0, 0)]
            while pending:
                lo, hi, depth = pending.pop()
                mid = (lo + hi) / 2
                angle = a.angle_deg + mid * turn
                sep, obstacle = _separate(
                    x, y, w, t, obstacles, angle, a.dx_mm + mid * dx, a.dy_mm + mid * dy
                )
                if sep > margin + (hi - lo) * travel / 2:
                    continue
                if sep <= margin or depth >= 8:
                    outcome = outcome.model_copy(
                        update={
                            "state": "envelope_contact" if sep <= margin else "inconclusive",
                            "obstacle": obstacle,
                            "angle_deg": round(angle, 3),
                        }
                    )
                    break
                pending.extend([(mid, hi, depth + 1), (lo, mid, depth + 1)])
            if outcome.state != "clear_in_model":
                break
        results.append(outcome)
    return tuple(results)
