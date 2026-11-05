"""Static tipping about four edges of the S1 foot rectangle with closed doors."""

from typing import Any

from custombuild_domain.materials import screening_birch_plywood_18, screening_solid_walnut_18
from custombuild_domain.sideboard_construction import SideboardConstruction


def closed_stability(package: SideboardConstruction, solids: dict[str, Any]) -> dict[str, Any]:
    inputs = package.request.review
    assert inputs is not None
    wood_mass = mx = my = 0.0
    payloads = []
    feet: list[tuple[float, float]] = []
    densities = (
        screening_birch_plywood_18().density_kg_m3,
        screening_solid_walnut_18().density_kg_m3,
    )
    for p in package.assembly.parts:
        solid = solids[p.id]
        density = float(densities[p.material == "Massiv valnöt"])
        if inputs.shelf_material and p.id.startswith(("SHELF-", "DIV-")):
            density = inputs.shelf_material.density_kg_m3
        mass = solid.Volume() * density / 1e9
        center = solid.Center()
        wood_mass += mass
        mx += mass * center.x
        my += mass * center.y
        if p.id.startswith("SHELF-"):
            payloads.append((inputs.shelf_payload_kg, center.x, center.y))
        if p.id.startswith("LEG-"):
            assert p.origin_um is not None and p.profile and p.profile.kind == "convex_prism"
            feet.extend(
                ((p.origin_um[0] + x) / 1000, (p.origin_um[1] + dy) / 1000)
                for x, z in p.profile.points_um
                if z == 0
                for dy in (0, p.size_um[1])
            )
    w, d, h = package.request.width_mm, package.request.depth_mm, package.request.height_mm
    edges = [
        ("vänster", 0, min(x for x, y in feet), 1, 0),
        ("höger", 0, max(x for x, y in feet), -1, w),
        ("fram", 1, min(y for x, y in feet), 1, 0),
        ("bak", 1, max(y for x, y in feet), -1, d),
    ]
    push_nm = inputs.horizontal_force_n * h / 1000
    rows = []
    for label, axis, edge, sign, far in edges:
        empty = sign * ((mx, my)[axis] - wood_mass * edge) * 9.81 / 1000
        on_shelves = sum(
            mass * ((x, y)[axis] - edge) * sign * 9.81 / 1000 for mass, x, y in payloads
        )
        on_top = inputs.top_payload_kg * (far - edge) * sign * 9.81 / 1000
        # All intermediate loading cases are bounded: each shelf and the top
        # can be loaded independently. Choose the adverse end of each interval.
        worst_shelves = sum(
            min(0, mass * ((x, y)[axis] - edge) * sign * 9.81 / 1000) for mass, x, y in payloads
        )
        minimum_reserve = empty + min(0, on_top) + worst_shelves - push_nm
        rows.append(
            dict(
                edge=label,
                empty_reserve_nm=empty - push_nm,
                fully_loaded_reserve_nm=empty + on_top + on_shelves - push_nm,
                minimum_reserve_nm=minimum_reserve,
            )
        )
    total_vertical = (wood_mass + sum(p[0] for p in payloads) + inputs.top_payload_kg) * 9.81
    test = inputs.base_test
    comparison = "missing"
    if test is not None:
        if test.assembly_sha256 != package.assembly_sha256:
            comparison = "wrong_assembly"
        elif (
            inputs.measurements is None
            or inputs.measurements.batch != test.batch
            or package.assessment is None
            or package.assessment.batch_state != "matches_nominal"
        ):
            comparison = "wrong_batch"
        elif (
            total_vertical > test.total_vertical_n or inputs.horizontal_force_n > test.horizontal_n
        ):
            comparison = "outside_recorded_case"
        else:
            comparison = "within_recorded_case"
    return dict(
        total_vertical_n=total_vertical,
        base_test_comparison=comparison,
        wood_mass_kg=wood_mass,
        horizontal_force_n=inputs.horizontal_force_n,
        top_payload_kg=inputs.top_payload_kg,
        push_moment_nm=push_nm,
        edges=rows,
        positive_reserve=all(r["minimum_reserve_nm"] > 0 for r in rows),
        basis="Stängda luckor, plant golv, sex bärande fötter, ingen vägginfästning. "
        "Bearbetade trävolymer, indikativa densiteter, beslagmassa utelämnad. "
        "Jämnt lastade hyllor och topplast vid ogynnsam kant. "
        "Horisontell kraft vid överskivan. Ingen fysisk lastklassning.",
    )
