"""S1 exact wood and nominal connector checks; no invented hinge kinematics."""

from itertools import combinations
from typing import Any

from custombuild_domain.measured_assembly import NORMAL, MeasuredPart, measured_assembly_hash
from custombuild_domain.sideboard_construction import SideboardConstruction
from custombuild_domain.sideboard_supports import HAFELE, LEGACY

from .measured_machining import _cq, machined_solid, operation_cutter
from .measured_reference import reference_solid


def _touch(a: Any, b: Any) -> bool:
    x, y = a.BoundingBox(), b.BoundingBox()
    return all(
        getattr(x, k + "min") < getattr(y, k + "max") - 1e-7
        and getattr(y, k + "min") < getattr(x, k + "max") - 1e-7
        for k in "xyz"
    )


def _collision(a: Any, b: Any) -> bool:
    return _touch(a, b) and a.intersect(b).Volume() > 1e-6


def _entry(part: MeasuredPart, op: Any) -> list[float]:
    assert part.origin_um is not None
    p = [x / 1000 for x in part.origin_um]
    for axis, coord in zip(op.axes, (op.u_um, op.v_um), strict=True):
        p["xyz".index(axis)] += coord / 1000
    axis, sign = NORMAL[op.face]
    if sign > 0:
        p[axis] += part.size_um[axis] / 1000
    return p


def audit_sideboard(package: SideboardConstruction) -> tuple[dict[str, Any], dict[str, Any]]:
    report, wood, _ = audit_sideboard_with_supports(package)
    return report, wood


def audit_sideboard_with_supports(
    package: SideboardConstruction,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cq = _cq()
    assembly = package.assembly
    if len(assembly.parts) > 64 or len(assembly.joints) > 160:
        raise ValueError("S1-kontrollen överskrider sin avgränsade omfattning.")
    parts = {p.id: p for p in assembly.parts}
    solids = {p.id: machined_solid(p) for p in assembly.parts}
    origins = {p.id: p.origin_um for p in assembly.parts if p.origin_um is not None}
    if len(origins) != len(parts):
        raise ValueError("Alla trädelar behöver en global placering.")
    operations = {(p.id, o.id): o for p in assembly.parts for o in p.operations}
    errors = []
    cuts = {}
    if package.fittings != assembly.fittings:
        errors.append("Beslagsregistret avviker från monteringsmodellen.")
    for p in assembly.parts:
        original = reference_solid(p.model_copy(update={"operations": ()}))
        local = []
        for op in p.operations:
            cutter = operation_cutter(p, op)
            cuts[(p.id, op.id)] = cutter
            local.append((op.id, cutter))
            if abs(original.intersect(cutter).Volume() - cutter.Volume()) > 1e-5:
                errors.append(f"{p.id}/{op.id}: borrningen ligger inte helt i ämnet.")
        for (op_a, ca), (op_b, cb) in combinations(local, 2):
            if op_a + "-HEAD" == op_b or op_b + "-HEAD" == op_a:
                continue
            if _collision(ca, cb):
                errors.append(f"{p.id}: bearbetningarna {op_a} och {op_b} går in i varandra.")
    overlaps = [(a, b) for a, b in combinations(solids, 2) if _collision(solids[a], solids[b])]
    hardware = {}
    checks = []
    for joint in assembly.joints:
        reasons = []
        try:
            a, b = parts[joint.a.part], parts[joint.b.part]
            oa, ob = operations[(a.id, joint.a.operation)], operations[(b.id, joint.b.operation)]
        except KeyError:
            checks.append(dict(id=joint.id, passed=False, errors=["Motdel eller borrning saknas."]))
            continue
        n, sign = NORMAL[oa.face]
        nb, sb = NORMAL[ob.face]
        pa, pb = _entry(a, oa), _entry(b, ob)
        diameter = (joint.diameter_um or 0) / 1000
        length = (joint.length_um or 0) / 1000
        if n != nb or any(abs(pa[i] - pb[i]) > 0.0001 for i in range(3) if i != n):
            reasons.append("Borrningarnas axlar eller centrum avviker.")
        if min(oa.width_um, ob.width_um) <= 0 or diameter <= 0 or length <= 0:
            reasons.append("Förbandsdimension saknas.")
        start = pa.copy()
        direction = [0, 0, 0]
        if joint.kind == "dowel":
            if sign == sb or oa.through or ob.through or abs(pa[n] - pb[n]) > 0.0001:
                reasons.append("Pluggens blinda ingångar möts inte.")
            engagement = oa.depth_um / 1000 - 1
            other = length - engagement
            if not (engagement > 0 and 0 < other <= ob.depth_um / 1000 - 1):
                reasons.append("Pluggens längd ger otillräcklig ingreppslängd eller bottnar.")
            if min(oa.width_um, ob.width_um) < diameter * 1000:
                reasons.append("Pluggen ryms inte i båda hålen.")
            start[n] -= sign * engagement
            direction[n] = sign
        elif joint.kind == "screw":
            grip = a.size_um[n] / 1000 - (joint.head_recess_um or 0) / 1000
            penetration = length - grip
            if (
                sign != sb
                or not oa.through
                or ob.through
                or abs(pa[n] - sign * a.size_um[n] / 1000 - pb[n]) > 0.0001
            ):
                reasons.append("Genomgång och mottagande pilot har inte anliggande träytor.")
            if not 0 < penetration <= ob.depth_um / 1000:
                reasons.append("Skruvens inträngning ryms inte i piloten.")
            if oa.width_um < diameter * 1000 or ob.width_um >= diameter * 1000:
                reasons.append("Skruvens genomgång/pilot har fel diameter.")
            start[n] -= sign * (joint.head_recess_um or 0) / 1000
            direction[n] = -sign
        else:
            reasons.append("Förbandstypen ingår inte i S1-kontrollen.")
        if not reasons:
            shaft = cq.Solid.makeCylinder(
                diameter / 2, length, cq.Vector(*start), cq.Vector(*direction)
            )
            components = [shaft]
            if joint.kind == "screw":
                head_dir = [0, 0, 0]
                head_dir[n] = sign
                components.append(
                    cq.Solid.makeCylinder(
                        diameter,
                        (joint.head_recess_um or 0) / 1000,
                        cq.Vector(*start),
                        cq.Vector(*head_dir),
                    )
                )
            for key, solid in solids.items():
                candidates = components[1:] if joint.kind == "screw" and key == b.id else components
                if any(_collision(piece, solid) for piece in candidates):
                    reasons.append(f"Förbandets nominella volym krockar med {key}.")
            hardware[joint.id] = cq.Compound.makeCompound(components)
        checks.append(dict(id=joint.id, passed=not reasons, errors=reasons))
    fitting_checks = []
    pin_solids = {}
    seats: dict[str, list[tuple[int, float]]] = {}
    expected_pin = HAFELE if package.request.shelf_pin_article else LEGACY
    for f in package.fittings:
        reasons = []
        if any(k not in parts for k in f.connects):
            reasons.append("Beslagets motdel saknas.")
        try:
            ops = [operations[(m.part, m.operation)] for m in f.mounts]
            if f.kind == "hinge":
                cup, *rest = ops
                front = parts[f.connects[0]]
                carrier = parts[f.connects[1]]
                tb = 7 if f.article == "70T3750.TL" else 6
                z = _entry(front, cup)[2]
                if (
                    len(ops) != 5
                    or cup.face != "back"
                    or cup.axes != "xz"
                    or cup.width_um != 35000
                    or cup.depth_um != 13000
                    or cup.u_um != int((17.5 + tb) * 1000)
                    or front.size_um[1] != 19000
                    or f.article not in ("70T3750.TL", "70T3650.TL")
                ):
                    reasons.append(
                        "Gångjärnets kopp eller val av artikel avviker från S1-katalogprofilen."
                    )
                expected = [
                    (cup.u_um - 9500, cup.v_um - 22500),
                    (cup.u_um - 9500, cup.v_um + 22500),
                ]
                if [(o.u_um, o.v_um) for o in rest[:2]] != expected or any(
                    o.face != "back" or o.depth_um != 15000 or o.width_um != 2500 for o in rest[:2]
                ):
                    reasons.append("Koppens fästskruvar avviker från 45/9,5-borrbilden.")
                points = [_entry(carrier, o) for o in rest[2:]]
                if (
                    len(points) != 2
                    or any(
                        abs(p[1] - 57.5) > 0.0001
                        or abs(p[0] - (origins[carrier.id][0] + carrier.size_um[0]) / 1000) > 0.0001
                        for p in points
                    )
                    or [round(p[2] - z, 6) for p in points] != [-16, 16]
                    or any(
                        o.face != "right" or o.width_um != 2500 or o.depth_um != 16000
                        for o in rest[2:]
                    )
                ):
                    reasons.append(
                        "Montageplattan avviker från 32-delning eller rätt insättningsdjup."
                    )
                # Compare actual front overlay, not just drilling IDs.
                overlay = (
                    origins[carrier.id][0] + carrier.size_um[0] - origins[front.id][0]
                ) / 1000
                if abs(overlay - (-3 if tb == 7 else 7.5)) > 0.0001:
                    reasons.append("Luckans överlagring stämmer inte med vald gångjärnsarm.")
            elif f.kind == "shelf_pin":
                op = ops[0]
                support = parts[f.connects[1]]
                shelf = parts[f.connects[0]]
                point = _entry(support, op)
                axis, sign = NORMAL[op.face]
                if (
                    len(ops) != 1
                    or f.mounts[0].part != support.id
                    or axis != 0
                    or op.shape != "drill"
                    or op.through
                    or op.axes != "yz"
                    or op.width_um != 5000
                    or op.depth_um != round(expected_pin.bore * 1000)
                    or f.article != expected_pin.article
                ):
                    reasons.append("Hyllbärarens artikel eller blindhål avviker från ritningen.")
                if abs(origins[shelf.id][2] / 1000 - point[2] - 2.5) > 0.0001:
                    reasons.append("Hyllplanet vilar inte på hyllbäraren.")
                edge = (origins[shelf.id][0] + (shelf.size_um[0] if sign < 0 else 0)) / 1000
                gap = sign * (edge - point[0])
                y_min = origins[shelf.id][1] / 1000
                y_max = y_min + shelf.size_um[1] / 1000
                if abs(gap - 1) > 0.0001 or not y_min <= point[1] <= y_max:
                    reasons.append("Hyllbäraren når inte under hyllplanet från angiven sida.")
                if not reasons:
                    direction = cq.Vector(sign, 0, 0)
                    start = cq.Vector(*point) - direction * expected_pin.insert
                    shaft = cq.Solid.makeCylinder(
                        2.5, expected_pin.insert + expected_pin.projection, start, direction
                    )
                    if expected_pin.collar_width:
                        collar = cq.Solid.makeCylinder(
                            expected_pin.collar_diameter / 2,
                            expected_pin.collar_width,
                            cq.Vector(*point),
                            direction,
                        )
                        shaft = shaft.fuse(collar).clean()
                    for key, solid in solids.items():
                        if _collision(shaft, solid):
                            reasons.append(f"Hyllbärarens kropp krockar med {key}.")
                    pin_solids[f.id] = shaft
                    seats.setdefault(shelf.id, []).append((sign, point[1]))
            else:
                body, catch = ops
                block, door = parts[f.mounts[0].part], parts[f.mounts[1].part]
                body_point, catch_point = _entry(block, body), _entry(door, catch)
                if (
                    body.width_um != 10000
                    or body.depth_um != 76000
                    or body.face != "front"
                    or catch.face != "back"
                    or catch.width_um != 2500
                    or catch.depth_um != 15000
                ):
                    reasons.append("Trycköppnarens långversion kräver Ø10 x 76 mm.")
                if (
                    any(abs(body_point[a] - catch_point[a]) > 0.0001 for a in (0, 2))
                    or abs(body_point[1] - catch_point[1] - 3.1) > 0.0001
                ):
                    reasons.append(
                        "Trycköppnare och slutbleck ligger inte mitt emot varandra "
                        "med 3,1 mm spalt."
                    )
        except (KeyError, IndexError, ValueError, TypeError):
            reasons.append("Beslagets kompletta borrbild saknas.")
        fitting_checks.append(dict(id=f.id, kind=f.kind, passed=not reasons, errors=reasons))
    support_sets = []
    for index in range(1, 6):
        key = f"SHELF-{index}"
        locations = seats.get(key, [])
        center_y = (origins[key][1] + parts[key].size_um[1] / 2) / 1000 if key in origins else 0
        quadrants = {(sign, y > center_y) for sign, y in locations if y != center_y}
        support_sets.append(
            dict(
                shelf=key,
                article=expected_pin.article,
                count=len(locations),
                passed=len(locations) == 4 and len(quadrants) == 4,
            )
        )
    if package.hardware.get(expected_pin.article) != len(pin_solids):
        errors.append("Antal hyllbärare i beslaglistan avviker från CAD-modellen.")
    hardware.update(pin_solids)
    fastener_hits = [
        (a, b) for a, b in combinations(hardware, 2) if _collision(hardware[a], hardware[b])
    ]
    # Coverage follows the aperture, not the outer bounding box or the top strip.
    backs = [p for p in assembly.parts if p.id.startswith("BACK-")]
    coverage = []
    for opening in assembly.backing.openings if assembly.backing else ():
        lo = [v / 1000 for v in opening.low_um]
        hi = [v / 1000 for v in opening.high_um]
        slab = cq.Solid.makeBox(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], cq.Vector(*lo))
        area = sum(solids[p.id].intersect(slab).Volume() / (hi[1] - lo[1]) for p in backs)
        required = (hi[0] - lo[0]) * (hi[2] - lo[2])
        coverage.append(
            dict(
                id=opening.id,
                required_mm2=required,
                covered_mm2=area,
                passed=abs(area - required) < 1e-4,
            )
        )
    press = []
    for key in (f"DOOR-{i}" for i in range(1, 6)):
        if key not in solids:
            press.append(dict(part=key, passed=False, collisions=["missing"]))
            continue
        moved = solids[key].translate((0, 2, 0))
        hits = [other for other in solids if other != key and _collision(moved, solids[other])]
        press.append(dict(part=key, travel_mm=2, passed=not hits, collisions=hits))
    graph: dict[str, set[str]] = {key: set() for key in solids}
    for joint, check in zip(assembly.joints, checks, strict=True):
        if check["passed"]:
            graph[joint.a.part].add(joint.b.part)
            graph[joint.b.part].add(joint.a.part)
    for fitting, check in zip(package.fittings, fitting_checks, strict=True):
        if check["passed"]:
            first, second = fitting.connects
            graph[first].add(second)
            graph[second].add(first)
    seen = set()
    pending = [next(iter(graph))]
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            pending.extend(graph[key] - seen)
    passed = (
        not errors
        and not overlaps
        and not fastener_hits
        and len(seen) == len(parts)
        and all(c["passed"] for c in checks + fitting_checks + coverage + press + support_sets)
    )
    return (
        dict(
            assembly_sha256=measured_assembly_hash(assembly),
            wood_and_boring_checks_passed=passed,
            parts=len(parts),
            operations=len(operations),
            pairs_checked=len(parts) * (len(parts) - 1) // 2,
            errors=errors,
            wood_intersections=overlaps,
            fastener_intersections=fastener_hits,
            shelf_support_sets=support_sets,
            joints=checks,
            fittings=fitting_checks,
            back_coverage=coverage,
            door_press=press,
            connected_parts=len(seen),
            hinge_motion_verified=False,
            hardware_body_collision_verified=False,
            load_rating_verified=False,
            physical_cutting_authorized=False,
        ),
        solids,
        pin_solids,
    )
