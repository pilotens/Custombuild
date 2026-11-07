"""An explicitly selected dry assembly trial. Catalogue dimensions, never a load rating."""

from collections import defaultdict
from itertools import combinations
from typing import Literal

from .enums import FaceName, FeatureKind, PartRole
from .identity import stable_id
from .models import (
    FeatureDimensions,
    Joint,
    JointRetentionApplicationClass,
    ManufacturingFeature,
    PartInstance,
    Point3D,
    TrialAngle,
)

ANGLE_SOURCE = "https://live-katalog.ganternorm.com/pdf/ganter/EN/970gs.pdf"
SCREW_SOURCE = "https://shop.schaefer-peters.com/sp/se/din-96-a4-5-0x16-p71826/"
# GN 970 B: 30 x 30 x 30 mm, 4 mm legs, one Ø6.2 bore per leg.
LEG = 30_000
HOLE_SETBACK = 18_000
PLATE = 4_000
# Design targets for a coupon; the screw vendor does not rate this wood connection.
PILOT_DIAMETER = 3_500
PILOT_DEPTH = 16_000 - PLATE

Box = tuple[int, int, int, int, int, int]


def _box(part: PartInstance) -> Box:
    p, d = part.placement, part.finished_size
    return (p.x_um, p.y_um, p.z_um, p.x_um + d.width_um, p.y_um + d.depth_um, p.z_um + d.height_um)


def _overlap(a: Box, b: Box) -> bool:
    return all(min(a[i + 3], b[i + 3]) > max(a[i], b[i]) for i in range(3))


def angle_box(angle: TrialAngle) -> Box:
    p = angle.corner
    x2, z2 = p.x_um + LEG * angle.x_sign, p.z_um + LEG * angle.z_sign
    return (
        min(p.x_um, x2),
        p.y_um - LEG // 2,
        min(p.z_um, z2),
        max(p.x_um, x2),
        p.y_um + LEG // 2,
        max(p.z_um, z2),
    )


def _pilot(
    part: PartInstance, fid: str, world: tuple[int, int, int], face: FaceName
) -> ManufacturingFeature:
    p = part.placement
    if part.actual_thickness_um <= PILOT_DEPTH:
        raise ValueError("Vinkelprovets pilothål skulle gå genom virket. Kontrollera tjockleken.")
    return ManufacturingFeature(
        feature_id=fid,
        part_id=part.part_id,
        kind=FeatureKind.DRILL,
        face=face,
        origin=Point3D(x_um=world[0] - p.x_um, y_um=world[1] - p.y_um, z_um=world[2] - p.z_um),
        dimensions=FeatureDimensions(diameter_um=PILOT_DIAMETER, depth_um=PILOT_DEPTH),
        tolerance_um=0,
    )


def fit_trial_angles(
    parts: tuple[PartInstance, ...], joints: tuple[Joint, ...]
) -> tuple[tuple[PartInstance, ...], tuple[TrialAngle, ...]]:
    by_id = {p.part_id: p for p in parts}
    pilots: dict[str, list[ManufacturingFeature]] = defaultdict(list)
    angles: list[TrialAngle] = []
    for j in joints:
        if (
            j.retention_application_class
            != JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
        ):
            continue
        members = [by_id[m.part_id] for m in j.members]
        upright = next(
            p
            for p in members
            if p.role in {PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER}
        )
        horizontal = next(p for p in members if p is not upright)
        # A shelf meeting the left face of a divider ends to its left. For a full-width
        # top/bottom across a divider use its right bay. No screws enter opposing faces
        # at the same depth station.
        xs: Literal[-1, 1] = (
            -1
            if (
                upright.role == PartRole.RIGHT_SIDE
                or (
                    horizontal.role == PartRole.SHELF
                    and horizontal.placement.x_um < upright.placement.x_um
                )
            )
            else 1
        )
        zs: Literal[-1, 1] = 1 if horizontal.role == PartRole.BOTTOM else -1
        vx, vy, _, vx2, vy2, _ = _box(upright)
        hx, hy, hz, hx2, hy2, hz2 = _box(horizontal)
        x = vx2 if xs == 1 else vx
        z = hz2 if zs == 1 else hz
        # Set the near edge 5 mm behind the later front. Offset opposite brackets by one
        # complete bracket plus 5 mm to separate screws from opposite divider faces.
        y = max(vy, hy) + LEG // 2 + 5_000 + (LEG + 5_000 if xs == -1 else 0)
        angle = TrialAngle(
            joint_id=j.joint_id,
            upright_id=upright.part_id,
            horizontal_id=horizontal.part_id,
            upright_hole=stable_id("feature", j.joint_id, "angle-upright"),
            horizontal_hole=stable_id("feature", j.joint_id, "angle-horizontal"),
            corner=Point3D(x_um=x, y_um=y, z_um=z),
            x_sign=xs,
            z_sign=zs,
        )
        box = angle_box(angle)
        if (
            box[1] < max(vy, hy)
            or box[4] > min(vy2, hy2)
            or box[0] < hx
            or box[3] > hx2
            or box[2] < upright.placement.z_um
            or box[5] > _box(upright)[5]
        ):
            raise ValueError("Vinkelns anliggning ryms inte. Öka djupet eller fackets fria mått.")
        for other in parts:
            if other.part_id not in (upright.part_id, horizontal.part_id) and _overlap(
                box, _box(other)
            ):
                raise ValueError(
                    f"Vinkelprovet träffar {other.semantic_key}. Öka det fria fackmåttet."
                )
        pilots[upright.part_id].append(
            _pilot(
                upright,
                angle.upright_hole,
                (x, y, z + zs * HOLE_SETBACK),
                FaceName.RIGHT if xs == 1 else FaceName.LEFT,
            )
        )
        pilots[horizontal.part_id].append(
            _pilot(
                horizontal,
                angle.horizontal_hole,
                (x + xs * HOLE_SETBACK, y, z),
                FaceName.TOP if zs == 1 else FaceName.BOTTOM,
            )
        )
        angles.append(angle)
    for a, b in combinations(angles, 2):
        if _overlap(angle_box(a), angle_box(b)):
            raise ValueError("Två låsvinklar behöver samma utrymme. Öka avståndet mellan hyllorna.")
    return (
        tuple(p.model_copy(update={"features": (*p.features, *pilots[p.part_id])}) for p in parts),
        tuple(angles),
    )


def check_trial_angles(
    article: str | None,
    parts: tuple[PartInstance, ...],
    joints: tuple[Joint, ...],
    angles: tuple[TrialAngle, ...],
) -> None:
    """Check a serialized result, including deleted fittings and detached pilot holes."""
    if article is None and not angles:
        return
    owners = {part.part_id: part for part in parts}
    expected = {
        joint.joint_id: {m.part_id for m in joint.members}
        for joint in joints
        if joint.retention_application_class
        == JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    }
    if (
        article is None
        or set(expected) != {a.joint_id for a in angles}
        or len(angles) != len(expected)
    ):
        raise ValueError("Vinkelregistret måste täcka varje bärande fog exakt en gång.")
    bound_holes: set[str] = set()
    for a in angles:
        if {a.upright_id, a.horizontal_id} != expected[a.joint_id]:
            raise ValueError("Vinkelns delar avviker från fogens delar.")
        c = a.corner
        for pid, fid, face, centre in (
            (
                a.upright_id,
                a.upright_hole,
                FaceName.RIGHT if a.x_sign == 1 else FaceName.LEFT,
                (c.x_um, c.y_um, c.z_um + HOLE_SETBACK * a.z_sign),
            ),
            (
                a.horizontal_id,
                a.horizontal_hole,
                FaceName.TOP if a.z_sign == 1 else FaceName.BOTTOM,
                (c.x_um + HOLE_SETBACK * a.x_sign, c.y_um, c.z_um),
            ),
        ):
            owner = owners[pid]
            bore = next((f for f in owner.features if f.feature_id == fid), None)
            if fid in bound_holes or bore != _pilot(owner, fid, centre, face):
                raise ValueError(
                    "Vinkelns pilothål avviker från hålbilden eller används två gånger."
                )
            bound_holes.add(fid)
    unbound_drills = {
        f.feature_id
        for p in parts
        for f in p.features
        if f.kind == FeatureKind.DRILL and f.joint_id is None
    }
    screw_access(parts, angles)
    if unbound_drills != bound_holes:
        raise ValueError("En obunden borrning saknas i vinkelregistret.")


def screw_access(
    parts: tuple[PartInstance, ...], angles: tuple[TrialAngle, ...]
) -> dict[str, int | None]:
    """Clear axial space beyond each plate for a 10 mm-wide straight tool envelope."""
    wood = [_box(p) for p in parts]
    brackets = {a.joint_id: angle_box(a) for a in angles}
    result: dict[str, int | None] = {}
    for a in angles:
        corner = a.corner
        solids = [*wood, *(b for jid, b in brackets.items() if jid != a.joint_id)]
        for fid, axis, sign, start in (
            (
                a.upright_hole,
                0,
                a.x_sign,
                (
                    corner.x_um + a.x_sign * PLATE,
                    corner.y_um,
                    corner.z_um + a.z_sign * HOLE_SETBACK,
                ),
            ),
            (
                a.horizontal_hole,
                2,
                a.z_sign,
                (
                    corner.x_um + a.x_sign * HOLE_SETBACK,
                    corner.y_um,
                    corner.z_um + a.z_sign * PLATE,
                ),
            ),
        ):
            distances = []
            for b in solids:
                if any(
                    b[i] >= start[i] + 5000 or b[i + 3] <= start[i] - 5000
                    for i in range(3)
                    if i != axis
                ):
                    continue
                far = b[axis + 3] if sign > 0 else b[axis]
                if (far - start[axis]) * sign <= 0:
                    continue
                near = b[axis] if sign > 0 else b[axis + 3]
                distances.append(max(0, (near - start[axis]) * sign))
            available = min(distances, default=None)
            if available is not None and available < 26_000:
                raise ValueError("För liten rak införingsväg för skruven. Öka fackets fria mått.")
            result[fid] = available
    return result
