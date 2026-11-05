"""Exact material removal for placed measured parts, including curved profiles.

This adapter never delegates profiled machining to bounding boxes. Its audit is
geometric evidence, deliberately separate from physical load qualification.
"""

from __future__ import annotations

from itertools import combinations
from math import pi
from typing import Any

from custombuild_domain.measured_assembly import (
    NORMAL,
    MeasuredAssembly,
    MeasuredJoint,
    MeasuredOperation,
    MeasuredPart,
    measured_assembly_hash,
)

from .adapter import CADDependencyUnavailable, UnsupportedCADFeatureError
from .measured_reference import reference_intersections, reference_solid

VOLUME_TOL = 1e-6


def _cq() -> Any:
    try:
        import cadquery as cq
    except ImportError as exc:
        raise CADDependencyUnavailable("CadQuery krävs för bearbetad geometri.") from exc
    return cq


def operation_cutter(part: MeasuredPart, op: MeasuredOperation) -> Any:
    cq = _cq()
    if part.origin_um is None or part.frame_id:
        raise UnsupportedCADFeatureError("Bearbetningen saknar globalt monteringsläge.")
    n, sign = NORMAL[op.face]
    u, v = ("xyz".index(a) for a in op.axes)
    if any(
        (2 * center - extent < 0 or 2 * center + extent > 2 * part.size_um[axis])
        for axis, center, extent in ((u, op.u_um, op.width_um), (v, op.v_um, op.height_um))
    ):
        raise UnsupportedCADFeatureError(f"{part.id}/{op.id}: utanför delens omslutande mått.")
    if op.depth_um > part.size_um[n] or (op.through and op.depth_um != part.size_um[n]):
        raise UnsupportedCADFeatureError(f"{part.id}/{op.id}: inkonsekvent borrdjup.")
    if (
        not op.through
        and op.shape in ("drill", "pocket", "round_slot")
        and op.depth_um == part.size_um[n]
    ):
        raise UnsupportedCADFeatureError(f"{part.id}/{op.id}: angivet blindhål går igenom delen.")
    low = [p / 1000 for p in part.origin_um]
    low[n] += 0 if sign < 0 else (part.size_um[n] - op.depth_um) / 1000
    low[u] += (op.u_um - op.width_um / 2) / 1000
    low[v] += (op.v_um - op.height_um / 2) / 1000
    size = [0.0, 0.0, 0.0]
    size[n], size[u], size[v] = op.depth_um / 1000, op.width_um / 1000, op.height_um / 1000
    direction = [0, 0, 0]
    direction[n] = 1

    def cylinder(center: list[float], radius: float) -> Any:
        return cq.Solid.makeCylinder(radius, size[n], cq.Vector(*center), cq.Vector(*direction))

    if op.shape == "drill":
        center = low.copy()
        center[u] += size[u] / 2
        center[v] += size[v] / 2
        return cylinder(center, size[u] / 2)
    if op.shape in ("notch", "pocket", "slot"):
        return cq.Solid.makeBox(*size, cq.Vector(*low))
    if op.shape != "round_slot":
        raise UnsupportedCADFeatureError(f"{part.id}/{op.id}: ej stödd bearbetning {op.shape}.")
    # A stadium/capsule, not a rectangle with incorrectly square end corners.
    long_axis, short_axis = (u, v) if size[u] >= size[v] else (v, u)
    radius = size[short_axis] / 2
    start = low.copy()
    start[short_axis] += radius
    start[long_axis] += radius
    end = start.copy()
    end[long_axis] += size[long_axis] - 2 * radius
    first = cylinder(start, radius)
    if abs(end[long_axis] - start[long_axis]) < 1e-9:
        return first
    bridge_low, bridge_size = low.copy(), size.copy()
    bridge_low[long_axis] += radius
    bridge_size[long_axis] -= 2 * radius
    return (
        first.fuse(cylinder(end, radius))
        .fuse(cq.Solid.makeBox(*bridge_size, cq.Vector(*bridge_low)))
        .clean()
    )


def machined_solid(part: MeasuredPart, omit: str | None = None) -> Any:
    original = reference_solid(part.model_copy(update={"operations": ()}))
    solid = original
    for op in part.operations:
        if op.id != omit:
            cutter = operation_cutter(part, op)
            # Every declared cut must meet the original contour. A hole in the
            # empty corner of a sloping part must not disappear silently.
            if original.intersect(cutter).Volume() <= VOLUME_TOL:
                raise UnsupportedCADFeatureError(f"{part.id}/{op.id}: träffar inget material.")
            solid = solid.cut(cutter)
    solid = solid.clean()
    if not solid.isValid() or len(solid.Solids()) != 1 or solid.Volume() <= VOLUME_TOL:
        raise UnsupportedCADFeatureError(f"{part.id}: bearbetningen lämnar inte en hel del.")
    return solid


def _center(part: MeasuredPart, op: MeasuredOperation) -> list[float]:
    assert part.origin_um is not None
    point = [p / 1000 for p in part.origin_um]
    for axis, value in zip(op.axes, (op.u_um, op.v_um), strict=True):
        point["xyz".index(axis)] += value / 1000
    return point


def _joint_audit(
    joint: MeasuredJoint, parts: dict[str, MeasuredPart], solids: dict[str, Any]
) -> tuple[dict[str, Any], list[Any]]:
    cq = _cq()
    result: dict[str, Any] = {"id": joint.id, "passed": False, "errors": []}
    errors = result["errors"]
    if joint.kind not in ("through_bolt", "screw"):
        errors.append("Förbandstypen stöds inte av denna begränsade CAD-kontroll.")
        return result, []
    if not joint.diameter_um or not joint.length_um:
        errors.append("Beslagets diameter eller längd saknas.")
        return result, []
    try:
        a, b = parts[joint.a.part], parts[joint.b.part]
        oa = next(o for o in a.operations if o.id == joint.a.operation)
        ob = next(o for o in b.operations if o.id == joint.b.operation)
    except (KeyError, StopIteration):
        errors.append("Motdel eller bearbetning saknas.")
        return result, []
    n, sign = NORMAL[oa.face]
    if sign != -1 or NORMAL[ob.face] != (n, sign):
        errors.append("Denna kontroll kräver parallella ingångar från negativ axelsida.")
        return result, []
    pa, pb = _center(a, oa), _center(b, ob)
    if any(abs(pa[i] - pb[i]) > 0.0001 for i in range(3) if i != n):
        errors.append("Motdelarnas borrcentrum sammanfaller inte.")
    if not oa.through or (joint.kind == "through_bolt" and not ob.through):
        errors.append("Bulten kräver genomgång i båda delarna; skruven genomgång i första.")
    if min(oa.width_um, oa.height_um) < joint.diameter_um:
        errors.append("Genomgången är för trång.")
    if joint.kind == "through_bolt" and min(ob.width_um, ob.height_um) < joint.diameter_um:
        errors.append("Bultens andra genomgång är för trång.")
    if joint.kind == "screw" and (ob.through or ob.width_um >= joint.diameter_um):
        errors.append("Skruven saknar en blind pilot med mindre diameter.")
    if errors:
        return result, []
    assert a.origin_um is not None and b.origin_um is not None
    datum = a.origin_um[n] / 1000
    head = datum + ((joint.head_recess_um or 0) - joint.head_allowance_um) / 1000
    end = head + joint.length_um / 1000
    exit_plane = (b.origin_um[n] + b.size_um[n] - (joint.nut_recess_um or 0)) / 1000
    if joint.kind == "screw":
        penetration = end - b.origin_um[n] / 1000
        reserve = ob.depth_um / 1000 - penetration
        result.update(penetration_mm=penetration, pilot_reserve_mm=reserve)
        if penetration <= 0 or reserve < 0:
            errors.append("Skruvens inträngning ryms inte i piloten.")
    else:
        # 2 mm washer + 6.5 mm nut + two M8 coarse thread pitches (2.5 mm).
        projection = end - exit_plane - 2 - 6.5
        result["thread_projection_mm"] = projection
        if projection < 2.5 - 1e-6:
            errors.append("Bulten når inte genom bricka och mutter med två fria gängvarv.")
    direction = [0, 0, 0]
    direction[n] = 1

    def cylinder(radius: float, start: float, depth: float) -> Any:
        p = pa.copy()
        p[n] = start
        return cq.Solid.makeCylinder(radius, depth, cq.Vector(*p), cq.Vector(*direction))

    shaft = cylinder(joint.diameter_um / 2000, head, joint.length_um / 1000)
    washer_outer = cylinder(12, head, 2)
    washer = washer_outer.cut(cylinder(joint.diameter_um / 2000 + 0.2, head, 2))
    head_envelope = cylinder(8, head - 6.4, 6.4)
    hardware = [shaft, washer, head_envelope]
    if joint.kind == "through_bolt":
        hardware += [
            cylinder(12, exit_plane, 2).cut(cylinder(4.2, exit_plane, 2)),
            cylinder(8.5, exit_plane + 2, 6.5).cut(cylinder(4, exit_plane + 2, 6.5)),
        ]
    for key, solid in solids.items():
        # Thread engagement is intentional only in the receiving screw pilot.
        pieces = hardware[1:] if joint.kind == "screw" and key == b.id else hardware
        if any(solid.intersect(h).Volume() > VOLUME_TOL for h in pieces):
            errors.append(f"Beslagets volym krockar med {key}.")
    # Restore ONLY this hole to measure material along the actual fastening line.
    # This detects holes which coincide in the bounding box but miss the profile.
    intervals = []
    probe_radius = joint.diameter_um / 2000 + 2
    probe = cylinder(
        probe_radius,
        min(datum, b.origin_um[n] / 1000) - 1,
        max(a.size_um[n], b.size_um[n]) / 1000 * 2 + 2,
    )
    for part, op in ((a, oa), (b, ob)):
        contact = machined_solid(part, omit=op.id).intersect(probe)
        fragments = contact.Solids()
        if not fragments:
            errors.append(f"{part.id}: inget trä längs infästningens centrumlinje.")
            continue
        for fragment in fragments:
            box = fragment.BoundingBox()
            lo, hi = getattr(box, "xyz"[n] + "min"), getattr(box, "xyz"[n] + "max")
            intervals.append((lo, hi))
            if abs(fragment.Volume() - pi * probe_radius**2 * (hi - lo)) > 1e-5:
                errors.append(f"{part.id}: mindre än 2 mm material runt infästningens skaft.")
    intervals.sort()
    if len(intervals) != 2 or abs(intervals[0][1] - intervals[1][0]) > 0.001:
        errors.append("Infästningen har inte två sammanhängande anliggande träavsnitt.")
    result["material_intervals_mm"] = intervals
    result["passed"] = not errors
    return result, hardware


def audit_machined_assembly(assembly: MeasuredAssembly) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(assembly.parts) > 32 or len(assembly.joints) > 64:
        raise UnsupportedCADFeatureError("CAD-provet är begränsat till 32 delar och 64 förband.")
    parts = {p.id: p for p in assembly.parts}
    solids = {key: machined_solid(part) for key, part in parts.items()}
    overlaps = reference_intersections(solids)
    joints, hardware = [], {}
    for joint in assembly.joints:
        check, pieces = _joint_audit(joint, parts, solids)
        joints.append(check)
        if pieces:
            hardware[joint.id] = _cq().Compound.makeCompound(pieces)
    hardware_collisions = [
        [a, b]
        for a, b in combinations(hardware, 2)
        if hardware[a].intersect(hardware[b]).Volume() > VOLUME_TOL
    ]
    bearings = []
    for bearing in assembly.bearings:
        if bearing.part not in solids or bearing.support not in solids:
            bearings.append(dict(id=bearing.id, passed=False, area_mm2=0))
            continue
        top, support = solids[bearing.part], solids[bearing.support]
        gap = top.BoundingBox().zmin - support.BoundingBox().zmax
        # A 0.01 mm slice through the two planar bearing faces. This family has
        # no tapered Z sections at these interfaces; volume / thickness is area.
        area = top.intersect(support.translate((0, 0, 0.01))).Volume() / 0.01
        bearings.append(dict(id=bearing.id, passed=abs(gap) <= 0.001 and area > 0, area_mm2=area))
    graph: dict[str, set[str]] = {key: set() for key in parts}
    for joint, check in zip(assembly.joints, joints, strict=True):
        if check["passed"]:
            graph[joint.a.part].add(joint.b.part)
            graph[joint.b.part].add(joint.a.part)
    seen, pending = set(), [next(iter(parts))]
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            pending.extend(graph[key] - seen)
    report = dict(
        assembly_sha256=measured_assembly_hash(assembly),
        geometric_checks_passed=(
            not overlaps
            and not hardware_collisions
            and len(seen) == len(parts)
            and all(c["passed"] for c in joints)
            and all(c["passed"] for c in bearings)
        ),
        parts=len(parts),
        operations=sum(len(p.operations) for p in parts.values()),
        pairs_checked=len(parts) * (len(parts) - 1) // 2,
        joints=joints,
        bearings=bearings,
        wood_intersections=overlaps,
        hardware_intersections=hardware_collisions,
        connected_parts=len(seen),
        physical_cutting_authorized=False,
        load_rating_verified=False,
    )
    return report, solids
