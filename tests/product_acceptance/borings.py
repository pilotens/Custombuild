"""Read actual named STEP cylinders and numeric DXF entities in the delivered bundle."""

from collections import Counter, defaultdict
from decimal import Decimal
from math import cos, radians, sin

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface

# Normal axis and inward sign on the physical machining face.
INWARD = {
    "left": (0, 1),
    "right": (0, -1),
    "front": (1, 1),
    "back": (1, -1),
    "bottom": (2, 1),
    "top": (2, -1),
}
DIMENSIONS = ("width_um", "depth_um", "height_um")
TOLERANCE_MM = 0.002


def dxf_circles(source):
    """Group circles by their layer and feature annotation, retaining duplicates."""
    lines = source.decode().splitlines()
    tags = list(zip(lines[::2], lines[1::2], strict=True))
    circles = defaultdict(Counter)
    feature = None
    for index, (code, value) in enumerate(tags):
        if code == "999" and value.startswith("FEATURE:"):
            feature = value.removeprefix("FEATURE:")
        if (code, value) == ("0", "CIRCLE"):
            entity = {}
            for next_code, next_value in tags[index + 1 :]:
                if next_code in ("0", "999"):
                    break
                entity[next_code] = next_value
            circle = tuple(Decimal(entity[k]) for k in ("10", "20", "40"))
            circles[entity["8"]][feature, circle] += 1
    return circles


def rotate(point, placement):
    x, y, z = point
    rx, ry, rz = [radians(placement[f"rotation_{axis}_mdeg"] / 1000) for axis in "xyz"]
    y, z = y * cos(rx) - z * sin(rx), y * sin(rx) + z * cos(rx)
    x, z = x * cos(ry) + z * sin(ry), -x * sin(ry) + z * cos(ry)
    return cq.Vector(x * cos(rz) - y * sin(rz), x * sin(rz) + y * cos(rz), z)


def step_walls(shape):
    walls = []
    for face in shape.Faces():
        if face.geomType() == "CYLINDER":
            cylinder = BRepAdaptor_Surface(face.wrapped).Cylinder()
            walls.append(
                (
                    cylinder.Radius(),
                    cq.Vector(cylinder.Location()),
                    cq.Vector(cylinder.Axis().Direction()),
                    [v.Center() for v in face.Vertices()],
                )
            )
    return walls


def verify_part_borings(archive, part, shape):
    placement = part["placement"]
    offset = cq.Vector(*(placement[f"{axis}_um"] / 1000 for axis in "xyz"))
    walls = step_walls(shape)
    expected = defaultdict(Counter)
    total = 0
    prefix = f"parts/{part['part_id']}/"
    for feature in part["features"]:
        if feature["kind"] not in ("drill", "drill_pattern"):
            continue
        face = {"a": part["a_side"], "b": part["b_side"]}.get(feature["face"], feature["face"])
        axis, sign = INWARD[face]
        u, v = [index for index in range(3) if index != axis]
        view = {part["a_side"]: "A", part["b_side"]: "B"}.get(face, face.upper())
        direction = rotate([sign if i == axis else 0 for i in range(3)], placement)
        radius = Decimal(feature["dimensions"]["diameter_um"]) / 2000
        depth_um = (
            part["finished_size"][DIMENSIONS[axis]]
            if feature["through"]
            else feature["dimensions"]["depth_um"]
        )
        for index in range(feature["pattern_count"]):
            center = [feature["origin"][f"{a}_um"] for a in "xyz"]
            center[u] += index * (feature["pitch_um"] or 0)
            circle = Decimal(center[u]) / 1000, Decimal(center[v]) / 1000, radius
            expected[view][feature["feature_id"], circle] += 1
            entry = rotate([x / 1000 for x in center], placement) + offset
            depth_mm = depth_um / 1000
            context = part["semantic_key"], feature["feature_id"], index
            # The cylindrical wall must share the feature axis and terminate at its depth.
            assert any(
                abs(r - float(radius)) <= TOLERANCE_MM
                and abs(abs(direction.dot(normal)) - 1) < 1e-7
                and (entry - line_point).cross(normal).Length <= TOLERANCE_MM
                and abs(max((vertex - entry).dot(direction) for vertex in vertices) - depth_mm)
                <= TOLERANCE_MM
                for r, line_point, normal, vertices in walls
            ), ("STEP bore wall", context)
            assert not shape.isInside(entry + direction * (depth_mm / 2), 1e-5), context
            total += 1
    for name in archive.namelist():
        if name.startswith(prefix) and name.endswith(".dxf"):
            view = name.removeprefix(prefix).removesuffix(".dxf")
            assert dxf_circles(archive.read(name))["DRILL"] == expected.pop(view, Counter()), name
    assert not expected, ("missing DXF views", part["part_id"], list(expected))
    return total
