"""Select complete solids from the verified order STEP in their installed coordinates."""

from pathlib import Path
from tempfile import TemporaryDirectory

import cadquery as cq

from .adapter import CADExportError, _normalise_step


def sample_step(source: bytes, ids: set[str]) -> bytes:
    with TemporaryDirectory(prefix="custombuild-bay-sample-") as directory:
        path = Path(directory) / "order.step"
        path.write_bytes(source)
        full = cq.Assembly.load(str(path))
        sample = cq.Assembly(name="bay-sample")
        for part_id in sorted(ids):
            sample.add(full.objects[part_id].toCompound(), name=part_id)
        path = Path(directory) / "sample.step"
        sample.export(str(path), exportType="STEP")
        result = cq.Assembly.load(str(path))
        if {node.name for node in result.children} != ids:
            raise CADExportError("Provets STEP saknar en vald orderdel.")
        for node in result.children:
            actual = node.toCompound()
            original = full.objects[node.name].toCompound()
            bounds, reference = actual.BoundingBox(), original.BoundingBox()
            if (
                len(actual.Solids()) != 1
                or abs(actual.Volume() - original.Volume()) > 0.001
                or any(
                    abs(getattr(bounds, axis) - getattr(reference, axis)) > 0.00001
                    for axis in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")
                )
            ):
                raise CADExportError(
                    f"Provets STEP har ändrad geometri eller placering för {node.name}."
                )
        return _normalise_step(path.read_bytes())
