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
        for a in angles:
            assembly.add(_angle_solid(a), name="angle-" + a.joint_id)
        file = Path(directory) / "mounted.step"
        assembly.export(str(file), exportType="STEP")
        roundtrip = cq.Assembly.load(str(file))
        for a in angles:
            name = "angle-" + a.joint_id
            node = roundtrip.objects.get(name)
            if node is None or node.obj is None:
                raise CADExportError("Mounted trial angle missing after STEP reimport")
            body = node.toCompound()
            bounds, expected = body.BoundingBox(), _angle_solid(a)
            ref = expected.BoundingBox()
            if (
                len(body.Solids()) != 1
                or abs(body.Volume() - expected.Volume()) > 0.1
                or any(
                    abs(getattr(bounds, key) - getattr(ref, key)) > 0.01
                    for key in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
                )
            ):
                raise CADExportError("Mounted trial angle changed shape or position in STEP")
        return _normalise_step(file.read_bytes())
