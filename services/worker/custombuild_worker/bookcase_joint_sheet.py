"""Pair the two actual DXF pilot bores with the measured STEP angle's catalogue axes."""

import csv
import io

from custombuild_cad.bookcase_readback import PLANES, size
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.models import PartInstance

from .bookcase_drawings import ReadbackPilot, mm


def _installed_centre(part: PartInstance, bore: ReadbackPilot) -> tuple[int, int, int]:
    u, v, n, sign = PLANES[bore.face]
    p = part.placement
    origin = [p.x_um, p.y_um, p.z_um]
    origin[u] += bore.u_um
    origin[v] += bore.v_um
    origin[n] += size(part)[n] if sign < 0 else 0
    return origin[0], origin[1], origin[2]


def joint_measurement_sheet(result: FurnitureResult, pilots: list[ReadbackPilot]) -> bytes:
    by_id = {p.feature_id: p for p in pilots}
    parts = {p.part_id: p for p in result.parts}
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "revision",
            "design_hash",
            "joint_id",
            "angle",
            "screw",
            "part_id",
            "part_name",
            "mate_id",
            "pilot_id",
            "drawing",
            "side",
            "physical_face",
            "U_mm",
            "V_mm",
            "diameter_mm",
            "depth_mm",
            "model_tolerance_mm",
            "world_X_mm",
            "world_Y_mm",
            "world_Z_mm",
            "material",
            "batch",
            "measured_U_mm",
            "measured_V_mm",
            "measured_diameter_mm",
            "measured_depth_mm",
            "measurement_uncertainty_mm",
            "inspection_result",
        )
    )
    if len(by_id) != len(pilots):
        raise ValueError("Ett pilothål förekommer flera gånger i DXF-ritningarna.")
    for a in result.trial_angles:
        x, y, z = a.corner.x_um, a.corner.y_um, a.corner.z_um
        # Catalogue dimensions of GN 970-B in mm: centreline Y, 18 from the bend.
        for pid, mate, fid, centre in (
            (a.upright_id, a.horizontal_id, a.upright_hole, (x, y, z + 18000 * a.z_sign)),
            (a.horizontal_id, a.upright_id, a.horizontal_hole, (x + 18000 * a.x_sign, y, z)),
        ):
            bore = by_id.pop(fid, None)
            part = parts[pid]
            if bore is None or bore.part_id != pid or _installed_centre(part, bore) != centre:
                raise ValueError(f"Pilothålet på motdelen ligger inte på vinkelns hålaxel: {fid}")
            writer.writerow(
                (
                    result.spec.revision,
                    result.design_hash,
                    a.joint_id,
                    a.article,
                    a.screw,
                    pid,
                    part.semantic_key,
                    mate,
                    fid,
                    f"parts/{pid}/{bore.side}.dxf",
                    bore.side,
                    bore.face,
                    mm(bore.u_um),
                    mm(bore.v_um),
                    mm(bore.diameter_um),
                    mm(bore.depth_um),
                    mm(bore.tolerance_um),
                    *(mm(c) for c in centre),
                    part.material_id,
                    result.spec.material.batch_id or "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                )
            )
    if by_id:
        raise ValueError("Ägarlöst pilothål i leveransens DXF.")
    return output.getvalue().encode("utf-8-sig")
