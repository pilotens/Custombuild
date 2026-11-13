"""Mounted trial angle bodies on the independently reimported timber assembly."""

from pathlib import Path
from tempfile import TemporaryDirectory

import cadquery as cq
from custombuild_domain.bookcase_angles import HOLE_SETBACK, LEG, PLATE
from custombuild_domain.models import TrialAngle

from .adapter import CADExportError, _normalise_step


def _angle_solid(a: TrialAngle) -> cq.Shape:
    x, y, z = (v / 1000 for v in (a.corner.x_um, a.corner.y_um, a.corner.z_um))
    sx, sz = a.x_sign, a.z_sign
    length, wall = LEG / 1000, PLATE / 1000

    def plate(dx: float, dz: float) -> cq.Solid:
        return cq.Solid.makeBox(
            dx, length, dz, (x + min(0, sx * dx), y - length / 2, z + min(0, sz * dz))
        )

    body = plate(length, wall).fuse(plate(wall, length))
    inset = HOLE_SETBACK / 1000
    for start, direction in (
        ((x - sx, y, z + sz * inset), (sx, 0, 0)),
        ((x + sx * inset, y, z - sz), (0, 0, sz)),
    ):
        body = body.cut(cq.Solid.makeCylinder(3.1, wall + 2, start, direction))
    return body.clean()


def mounted_trial_angles(timber_step: bytes, angles: tuple[TrialAngle, ...]) -> bytes:
    """Bind purchased bodies to timber placements, checking the exported assembly again."""
    with TemporaryDirectory(prefix="custombuild-bookcase-angles-") as directory:
        file = Path(directory) / "timber.step"
        file.write_bytes(timber_step)
        timber = cq.Assembly.load(str(file))
        assembly = cq.Assembly(name="bookcase-with-trial-angles")
        assembly.add(timber, name="timber")
        hardware = {"angle-" + a.joint_id: _angle_solid(a) for a in angles}
        for name, body in hardware.items():
            assembly.add(body, name=name)
        file = Path(directory) / "mounted.step"
        assembly.export(str(file), exportType="STEP")
        roundtrip = cq.Assembly.load(str(file))
        timber_nodes = {
            f"timber/{name}": n.toCompound()
            for name, n in timber.objects.items()
            if n.obj is not None
        }
        expected_shapes = {**timber_nodes, **hardware}
        if len(roundtrip.toCompound().Solids()) != len(expected_shapes):
            raise CADExportError("Mounted assembly has unexpected solids after STEP reimport")
        occupied = [(solid, solid.BoundingBox()) for solid in timber_nodes.values()]
        for name, expected in expected_shapes.items():
            node = roundtrip.objects.get(name)
            if node is None or node.obj is None:
                raise CADExportError("Mounted assembly component missing after STEP reimport")
            body = node.toCompound()
            bounds = body.BoundingBox()
            ref = expected.BoundingBox()
            if (
                len(body.Solids()) != 1
                or abs(body.Volume() - expected.Volume()) > 0.1
                or any(
                    abs(getattr(bounds, key) - getattr(ref, key)) > 0.01
                    for key in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
                )
            ):
                raise CADExportError("Mounted assembly component changed shape or position in STEP")
            if name in hardware:
                for previous, bbox in occupied:
                    if (
                        all(
                            min(getattr(bounds, i + "max"), getattr(bbox, i + "max"))
                            - max(getattr(bounds, i + "min"), getattr(bbox, i + "min"))
                            > 0.00001
                            for i in "xyz"
                        )
                        and body.intersect(previous).Volume() > 0.001
                    ):
                        raise CADExportError(f"Purchased fitting {name} intersects a mounted part")
                occupied.append((body, bounds))
        return _normalise_step(file.read_bytes())
