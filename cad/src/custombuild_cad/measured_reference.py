"""Exact unmachined reference solids, deliberately separate from a production export.

No holes, joint qualification or machining authorization are inferred. A reference
containing operations must not be exported by this limited contour adapter.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from custombuild_domain.measured_assembly import MeasuredAssembly, MeasuredPart
from custombuild_domain.measured_profiles import ConvexProfile, RoundedProfile

from .adapter import CADDependencyUnavailable, UnsupportedCADFeatureError


def reference_solid(part: MeasuredPart) -> Any:
    try:
        import cadquery as cq
    except ImportError as exc:
        raise CADDependencyUnavailable("CadQuery krävs för profilens referensgeometri.") from exc
    if part.operations or part.origin_um is None or part.frame_id:
        raise UnsupportedCADFeatureError(
            "Referensadaptern kräver placerade, obearbetade delar i hela möbelns koordinatsystem."
        )
    dimensions = [n / 1000 for n in part.size_um]
    profile = part.profile
    if profile is None:
        solid = cq.Solid.makeBox(*dimensions)
    else:
        u, v = ("xyz".index(a) for a in profile.axes)
        n = next(i for i in range(3) if i not in (u, v))

        def point(a: float, b: float) -> Any:
            p = [0.0, 0.0, 0.0]
            p[u], p[v] = a, b
            return cq.Vector(*p)

        if isinstance(profile, ConvexProfile):
            wire = cq.Wire.makePolygon(
                [point(a / 1000, b / 1000) for a, b in profile.points_um], close=True
            )
        elif isinstance(profile, RoundedProfile):
            # The local plane follows the declared U/V orientation, including XZ.
            plane = cq.Plane(
                origin=(0, 0, 0), xDir=point(1, 0), normal=point(1, 0).cross(point(0, 1))
            )
            w, h, r = dimensions[u], dimensions[v], profile.radius_um / 1000
            if 2 * r >= min(w, h):
                raise UnsupportedCADFeatureError("Referensadaptern kräver fyra separata hörnbågar.")
            wire = (
                cq.Workplane(plane)
                .moveTo(r, 0)
                .lineTo(w - r, 0)
                .radiusArc((w, r), -r)
                .lineTo(w, h - r)
                .radiusArc((w - r, h), -r)
                .lineTo(r, h)
                .radiusArc((0, h - r), -r)
                .lineTo(0, r)
                .radiusArc((r, 0), -r)
                .close()
                .wire()
                .val()
            )
        vector = [0.0, 0.0, 0.0]
        vector[n] = dimensions[n]
        solid = cq.Solid.extrudeLinear(wire, [], cq.Vector(*vector))
    return solid.translate(cq.Vector(*(n / 1000 for n in part.origin_um)))


def reference_solids(assembly: MeasuredAssembly) -> dict[str, Any]:
    return {part.id: reference_solid(part) for part in assembly.parts}


def reference_intersections(solids: dict[str, Any]) -> list[dict[str, object]]:
    """Positive solid intersections only, with the existing CAD volume tolerance."""
    result = []
    for a, b in combinations(solids, 2):
        volume = solids[a].intersect(solids[b]).Volume()
        if volume > 1e-6:
            result.append({"parts": [a, b], "volume_mm3": volume})
    return result
