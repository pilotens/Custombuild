"""Reconstruct rectangular furniture stock and cuts independently of the CAD/DXF emitters."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import cadquery as cq
from custombuild_domain.enums import FeatureKind
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.models import ManufacturingFeature, PartInstance, TrialAngle

# Linear and volume tolerances apply only to digital reimport of the same nominal model.
DIGITAL_LINEAR_MM = 0.00001
DIGITAL_VOLUME_MM3 = 0.001
# U, V, thickness axis, and direction of drilling into the material.
PLANES = {
    "left": (1, 2, 0, 1),
    "right": (1, 2, 0, -1),
    "front": (0, 2, 1, 1),
    "back": (0, 2, 1, -1),
    "bottom": (0, 1, 2, 1),
    "top": (0, 1, 2, -1),
}


def size(part: PartInstance, *, raw: bool = False) -> tuple[int, int, int]:
    d = part.raw_size if raw else part.finished_size
    return d.width_um, d.depth_um, d.height_um


@dataclass(frozen=True)
class PanelCut:
    feature: ManufacturingFeature
    u: int
    v: int
    width: int
    length: int
    depth: int
    radius: int
    corners: tuple[tuple[int, int], ...]


def physical_face(part: PartInstance, feature: ManufacturingFeature) -> str:
    return {"a": part.a_side.value, "b": part.b_side.value}.get(
        feature.face.value, feature.face.value
    )


def panel_cut(part: PartInstance, feature: ManufacturingFeature) -> PanelCut:
    a, b, _, _ = PLANES[physical_face(part, feature)]
    coords = (feature.origin.x_um, feature.origin.y_um, feature.origin.z_um)
    u, v = coords[a], coords[b]
    d = feature.dimensions
    if feature.feature_id.startswith("J95-") and feature.kind == FeatureKind.DRILL:
        allowed = {(40_000, 15_000, False), (3500, 15_000, False), (30_000, 18_000, True)}
        if (
            d.diameter_um,
            d.depth_um,
            feature.through,
        ) not in allowed or feature.pattern_count != 1:
            raise ValueError("Ogiltig borrdiameter eller borrdjup för underskåpsluckan.")
        assert d.depth_um and d.diameter_um
        return PanelCut(feature, u, v, 0, 0, d.depth_um, d.diameter_um // 2, ())
    if not d.depth_um or feature.through or feature.pattern_count != 1:
        raise ValueError(f"Bearbetning utanför den fasta provprofilen: {feature.feature_id}")
    if feature.kind == FeatureKind.DRILL and d.diameter_um == 3500 and d.depth_um == 12500:
        return PanelCut(feature, u, v, 0, 0, d.depth_um, 1750, ())
    if (
        feature.kind != FeatureKind.GROOVE
        or feature.corner_strategy != "dogbone-v2"
        or not d.width_um
        or not d.length_um
        or not d.radius_um
    ):
        raise ValueError(f"Okänd spår- eller hålprofil: {feature.feature_id}")
    corners = tuple(
        (x, y)
        for x, ux in ((u, "u_min"), (u + d.width_um, "u_max"))
        for y, vy in ((v, "v_min"), (v + d.length_um, "v_max"))
        if ux not in feature.open_end_reliefs and vy not in feature.open_end_reliefs
    )
    return PanelCut(feature, u, v, d.width_um, d.length_um, d.depth_um, d.radius_um, corners)


def _panel(part: PartInstance) -> cq.Shape:
    dimensions = size(part)
    pos = part.placement
    if any((pos.rotation_x_mdeg, pos.rotation_y_mdeg, pos.rotation_z_mdeg)):
        raise ValueError("Provprofilen kräver axelparallella paneler.")
    dx, dy, dz = dimensions
    stock = cq.Solid.makeBox(dx / 1000, dy / 1000, dz / 1000)
    cutters = []
    for f in part.features:
        c = panel_cut(part, f)
        a, b, normal, sign = PLANES[physical_face(part, f)]
        direction = [0, 0, 0]
        direction[normal] = sign
        surface = 0 if sign == 1 else dimensions[normal]
        start = [0.0, 0.0, 0.0]
        start[normal] = surface / 1000
        start[a], start[b] = c.u / 1000, c.v / 1000
        if c.width:
            lengths = [0.0, 0.0, 0.0]
            lengths[a], lengths[b], lengths[normal] = (
                c.width / 1000,
                c.length / 1000,
                c.depth / 1000,
            )
            bottom = start.copy()
            bottom[normal] -= lengths[normal] if sign < 0 else 0
            cutters.append(cq.Solid.makeBox(lengths[0], lengths[1], lengths[2], cq.Vector(*bottom)))
        for u, v in c.corners if c.width else ((c.u, c.v),):
            start[a], start[b] = u / 1000, v / 1000
            cutters.append(
                cq.Solid.makeCylinder(
                    c.radius / 1000, c.depth / 1000, cq.Vector(*start), cq.Vector(*direction)
                )
            )
    solid = stock.cut(*cutters) if cutters else stock
    return solid.translate((pos.x_um / 1000, pos.y_um / 1000, pos.z_um / 1000))


def _catalogue_angle(angle: TrialAngle) -> cq.Shape:
    # Supplier GN 970-B, 30 x 30 x 30, two 4 mm legs. Build a canonical L then
    # mirror its axes via signed dimensions. Dimensions do not come from the STEP writer.
    sx, sz = angle.x_sign, angle.z_sign

    def box(dx: int, dz: int) -> cq.Solid:
        return cq.Solid.makeBox(dx, 30, dz, (min(0, sx * dx), -15, min(0, sz * dz)))

    body = box(30, 4).fuse(box(4, 30))
    vertical = cq.Solid.makeCylinder(3.1, 4, (0, 0, sz * 18), (sx, 0, 0))
    horizontal = cq.Solid.makeCylinder(3.1, 4, (sx * 18, 0, 0), (0, 0, sz))
    c = angle.corner
    return body.cut(vertical, horizontal).translate((c.x_um / 1000, c.y_um / 1000, c.z_um / 1000))


def _step_components(data: bytes, path: Path) -> dict[str, cq.Shape]:
    if b"SI_UNIT(.MILLI.,.METRE.)" not in data:
        raise ValueError("STEP saknar den förväntade millimeterenheten.")
    path.write_bytes(data)
    assembly = cq.Assembly.load(str(path))
    components = {}

    def visit(node: cq.Assembly, parent: cq.Location) -> None:
        location = parent * node.loc
        if node.obj is not None:
            if node.name in components or not isinstance(node.obj, cq.Shape):
                raise ValueError(f"Ogiltig STEP-komponent: {node.name}")
            components[node.name] = node.obj.moved(location)
        for child in node.children:
            visit(child, location)

    visit(assembly, cq.Location())
    if len(components) != len(assembly.toCompound().Solids()):
        raise ValueError("STEP har fel antal solider.")
    return components


def _same_solid(actual: cq.Shape, expected: cq.Shape, label: str) -> None:
    a, b = actual.BoundingBox(), expected.BoundingBox()
    if (
        not actual.isValid()
        or len(actual.Solids()) != 1
        or any(
            abs(getattr(a, s) - getattr(b, s)) > DIGITAL_LINEAR_MM
            for s in ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")
        )
    ):
        raise ValueError(f"STEP har fel placering eller mått: {label}")
    if (
        abs(actual.Volume() - expected.Volume()) > DIGITAL_VOLUME_MM3
        or actual.cut(expected).Volume() > DIGITAL_VOLUME_MM3
        or expected.cut(actual).Volume() > DIGITAL_VOLUME_MM3
    ):
        raise ValueError(f"STEP avviker från orderns material, hål eller spår: {label}")


def verify_cabinet_step(result: FurnitureResult, data: bytes) -> None:
    with TemporaryDirectory(prefix="cabinet-readback-") as directory:
        actual = _step_components(data, Path(directory) / "panels.step")
        if set(actual) != {p.part_id for p in result.parts}:
            raise ValueError("STEP saknar rätt trädelar för underskåpsordern.")
        for part in result.parts:
            _same_solid(actual[part.part_id], _panel(part), part.semantic_key)


def verify_bookcase_steps(result: FurnitureResult, timber: bytes, mounted: bytes) -> None:
    with TemporaryDirectory(prefix="bookcase-readback-") as directory:
        bare = _step_components(timber, Path(directory) / "timber.step")
        assembled = _step_components(mounted, Path(directory) / "mounted.step")
        names = {p.part_id for p in result.parts}
        brackets = {"angle-" + a.joint_id for a in result.trial_angles}
        if set(bare) != names or set(assembled) != names | brackets:
            raise ValueError("STEP-delarna avviker från orderns delar och beslag.")
        for part in result.parts:
            wanted = _panel(part)
            _same_solid(bare[part.part_id], wanted, part.part_id)
            _same_solid(assembled[part.part_id], wanted, "monterad " + part.part_id)
        for angle in result.trial_angles:
            _same_solid(
                assembled["angle-" + angle.joint_id], _catalogue_angle(angle), angle.joint_id
            )
