"""Check the complete declared movement envelope of each tabletop attachment."""

from typing import Any

from custombuild_domain.trestle_construction import TrestleConstruction

from .measured_machining import _cq, operation_cutter


def audit_trestle_movement(package: TrestleConstruction, solids: dict[str, Any]) -> dict[str, Any]:
    cq = _cq()
    parts = {p.id: p for p in package.assembly.parts}
    checks = []
    for joint in package.assembly.joints:
        if joint.kind != "screw" or joint.id == "MOUNT-5":
            continue
        part = parts[joint.a.part]
        assert part.origin_um is not None
        op = next(o for o in part.operations if o.id == joint.a.operation)
        cutter = operation_cutter(part, op)
        x = (part.origin_um[0] + op.u_um) / 1000
        y = (part.origin_um[1] + op.v_um) / 1000
        z = part.origin_um[2] / 1000
        delta_mc = package.request.moisture_max_pct - package.request.moisture_min_pct
        dx = 1 + abs(x - package.request.length_mm / 2) * 0.0001 * delta_mc
        dy = package.sizing.mounting_travel_each_way_mm
        passed = True
        # The allowed-centre regions of the stadium cutouts are convex. If all
        # four rectangle corners fit, its entire continuous interior fits too.
        for sx in (-1, 1):
            for sy in (-1, 1):
                shaft = cq.Solid.makeCylinder(2.5, 140, cq.Vector(x + sx * dx, y + sy * dy, z))
                washer = cq.Solid.makeCylinder(12, 2, cq.Vector(x + sx * dx, y + sy * dy, z + 118))
                passed = passed and shaft.cut(cutter).Volume() <= 1e-6
                passed = passed and washer.intersect(solids[part.id]).Volume() <= 1e-6
        checks.append(dict(joint=joint.id, travel_x_mm=dx, travel_y_mm=dy, passed=passed))
    return dict(
        passed=len(checks) == 4 and all(c["passed"] for c in checks),
        mounts=checks,
        qualification="Geometrisk rörelsemån; friktion och fysisk glidning är inte provade.",
    )
