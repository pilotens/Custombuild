"""Parametric four-post furniture. Geometry and fastener locations share one datum."""

from __future__ import annotations

from math import ceil, cos, floor, pi, radians, sin, sqrt

from .enums import (
    AssemblyDirection,
    FaceName,
    FeatureKind,
    FurnitureJointType,
    GrainDirection,
    PartRole,
)
from .furniture import ChairIntent, DiningTableIntent, FurnitureDesign
from .furniture_catalog import resolve_material
from .identity import stable_id
from .models import (
    Dimensions3D,
    FeatureDimensions,
    Joint,
    JointMember,
    ManufacturingFeature,
    PartInstance,
    Placement,
    Point3D,
)

XYZ = tuple[int, int, int]
FACES = (
    (FaceName.LEFT, FaceName.RIGHT),
    (FaceName.FRONT, FaceName.BACK),
    (FaceName.BOTTOM, FaceName.TOP),
)
DIR = (
    (AssemblyDirection.POS_X, AssemblyDirection.NEG_X),
    (AssemblyDirection.POS_Y, AssemblyDirection.NEG_Y),
    (AssemblyDirection.POS_Z, AssemblyDirection.NEG_Z),
)


def rotated_yz(y: float, z: float, angle_mdeg: int) -> tuple[float, float]:
    angle = radians(angle_mdeg / 1000)
    return y * cos(angle) - z * sin(angle), y * sin(angle) + z * cos(angle)


def frame_bounds(part: PartInstance) -> tuple[XYZ, XYZ]:
    size, loc = part.finished_size, part.placement
    corners = [
        rotated_yz(y, z, loc.rotation_x_mdeg)
        for y in (0, size.depth_um)
        for z in (0, size.height_um)
    ]
    return (
        (
            loc.x_um,
            loc.y_um + round(min(y for y, z in corners)),
            loc.z_um + round(min(z for y, z in corners)),
        ),
        (
            loc.x_um + size.width_um,
            loc.y_um + round(max(y for y, z in corners)),
            loc.z_um + round(max(z for y, z in corners)),
        ),
    )


def screw_joint(
    design_id: str,
    parts: dict[str, PartInstance],
    holes: dict[str, list[ManufacturingFeature]],
    joints: list[Joint],
    clear: str,
    pilot: str,
    axis: int,
    sign: int,
    at: XYZ,
    *,
    length_um: int | None = None,
    screw_mm: int = 5,
    head_recess_um: int = 0,
) -> None:
    # 'at' is the common assembly-plane centre; both local bores are derived from it.
    jid = stable_id("joint", design_id, clear, pilot, str(len(holes[pilot])))
    members = []
    clearance_part = parts[clear]
    clear_len = (
        clearance_part.finished_size.width_um,
        clearance_part.finished_size.depth_um,
        clearance_part.finished_size.height_um,
    )[axis]
    length = length_um or 10_000 * ceil((clear_len + 22_000) / 10_000)
    for key, through in ((clear, True), (pilot, False)):
        p = parts[key]
        loc = p.placement
        size = p.finished_size
        coords = [at[0] - loc.x_um, at[1] - loc.y_um, at[2] - loc.z_um]
        coords[1], coords[2] = (
            round(v) for v in rotated_yz(coords[1], coords[2], -loc.rotation_x_mdeg)
        )
        dims = (size.width_um, size.depth_um, size.height_um)
        face = FACES[axis][int(sign < 0)]
        mating_coordinate = (
            dims[axis] if (through and sign > 0) or (not through and sign < 0) else 0
        )
        if abs(coords[axis] - mating_coordinate) > 1:
            raise ValueError("Förbandets två anliggningsytor möts inte.")
        coords[axis] = dims[axis] if sign < 0 else 0
        depth = clear_len if through else length + head_recess_um - clear_len + 1_000
        if (
            depth <= 0
            or depth > dims[axis]
            or (not through and depth == dims[axis])
            or any(not 4_000 <= coords[a] <= dims[a] - 4_000 for a in range(3) if a != axis)
        ):
            raise ValueError("Skruvförbandet får otillräckligt kantavstånd eller borrdjup.")
        feature = ManufacturingFeature(
            feature_id=stable_id("bore", jid, key),
            joint_id=jid,
            part_id=p.part_id,
            kind=FeatureKind.DRILL,
            face=face,
            origin=Point3D(x_um=coords[0], y_um=coords[1], z_um=coords[2]),
            dimensions=FeatureDimensions(
                diameter_um=screw_mm * 1000 + 500 if through else screw_mm * 1000 - 1500,
                depth_um=depth,
            ),
            through=through,
        )
        holes[key].append(feature)
        feature_ids = [feature.feature_id]
        if through and head_recess_um:
            recess = feature.model_copy(
                update={
                    "feature_id": stable_id("head-recess", jid, key),
                    "through": False,
                    "dimensions": FeatureDimensions(diameter_um=11_000, depth_um=head_recess_um),
                }
            )
            holes[key].append(recess)
            feature_ids.append(recess.feature_id)
        members.append(
            JointMember(
                part_id=p.part_id,
                mating_face=FACES[axis][int(sign > 0)] if through else face,
                feature_ids=tuple(feature_ids),
            )
        )
    joints.append(
        Joint(
            joint_id=jid,
            joint_type=FurnitureJointType.WOOD_SCREW,
            members=(members[0], members[1]),
            mating_origin=Point3D(x_um=at[0], y_um=at[1], z_um=at[2]),
            assembly_direction=DIR[axis][int(sign < 0)],
            hardware_sku=f"wood-screw-pan-{screw_mm}x{length // 1000}",
            hardware_count=1,
        )
    )


def build_frame(
    design: FurnitureDesign, intent: DiningTableIntent | ChairIntent
) -> tuple[tuple[PartInstance, ...], tuple[Joint, ...]]:
    parts: dict[str, PartInstance] = {}
    holes: dict[str, list[ManufacturingFeature]] = {}
    joints: list[Joint] = []
    panel = resolve_material(design.material)
    walnut = panel.material_id == "solid-walnut"
    leg_stock_id = "walnut-square-stock" if walnut else "birch-square-stock"
    leg_density = panel.density_kg_m3 if walnut else 650
    t = design.material.measured_thickness_um
    if not 16_000 <= t <= 25_000:
        raise ValueError("Ramkonstruktionen kräver 16–25 mm skivmaterial.")
    w, d, h = intent.width_um, intent.depth_um, intent.height_um
    s = intent.leg_section_um
    seat = isinstance(intent, ChairIntent)
    circular = isinstance(intent, DiningTableIntent) and intent.top_shape == "round"

    def board(
        key: str,
        role: PartRole,
        dims: XYZ,
        pos: XYZ,
        axis: int,
        *,
        angle: int = 0,
        leg: bool = False,
        circle: bool = False,
    ) -> PartInstance:
        if min(dims) <= 0 or min(pos) < 0:
            raise ValueError("Måtten lämnar inte plats för ramens delar.")
        size = Dimensions3D(width_um=dims[0], depth_um=dims[1], height_um=dims[2])
        # Legs use separately procured planed square stock. Density estimates give mass only.
        volume = dims[0] * dims[1] * dims[2]
        weight = ceil(
            volume
            * (leg_density if leg else panel.density_kg_m3)
            * (pi / 4 if circle else 1)
            / 10**15
        )
        part = PartInstance(
            part_id=stable_id("part", design.design_id, key),
            semantic_key=key,
            role=role,
            instance_index=0,
            revision=design.revision,
            finished_size=size,
            raw_size=size,
            placement=Placement(x_um=pos[0], y_um=pos[1], z_um=pos[2], rotation_x_mdeg=angle),
            material_id=leg_stock_id if leg else panel.material_id,
            material_version="dimensional-v1" if leg else panel.version,
            actual_thickness_um=dims[axis],
            a_side=FACES[axis][1],
            b_side=FACES[axis][0],
            grain_direction=GrainDirection.Z
            if leg
            else (
                GrainDirection.NONE
                if panel.grain_direction == GrainDirection.NONE
                else GrainDirection.Y
                if role == PartRole.FRAME_RAIL_Y
                else GrainDirection.X
            ),
            weight_g=max(1, weight),
            circular_outline=circle,
        )
        parts[key] = part
        holes[key] = []
        return part

    if isinstance(intent, ChairIntent):
        level, apron = intent.seat_height_um, 70_000
        rear, x0 = d - s, 0
        front = 6_000
        if intent.seat_depth_um > rear or h - level < 160_000:
            raise ValueError("Kontrollera sittdjupet och ryggens höjd över sitsen.")
        board(
            "chair-seat",
            PartRole.CHAIR_SEAT,
            (w, intent.seat_depth_um, t),
            (0, 0, level - t),
            2,
        )
        top = "chair-seat"
    else:
        level, apron = h, intent.stretcher_height_um
        if circular and w != d:
            raise ValueError("En rund bordsskiva kräver samma diameter i båda riktningarna.")
        # For a circle the offset is measured inwards from the inscribed frame square.
        extra = ceil((w - w / sqrt(2)) / 2) if circular else 0
        x0 = front = extra + intent.end_inset_um
        rear = d - front - s
        board("table-top", PartRole.TOP, (w, d, t), (0, 0, h - t), 2, circle=circular)
        top = "table-top"
    x1 = w - x0 - s
    underside = level - t
    if min(x1 - x0 - s, rear - front - s) < 100_000 or underside - apron < 100_000:
        raise ValueError("Ben, sargar och skiva ryms inte inom de valda måtten.")
    for side, x in (("left", x0), ("right", x1)):
        for end, y in (("front", front), ("rear", rear)):
            board(
                f"leg-{end}-{side}",
                PartRole.FRAME_LEG,
                (s, s, h if seat and end == "rear" else underside),
                (x, y, 0),
                0,
                leg=True,
            )
    for end, y in (("front", front), ("rear", rear)):
        rail = f"rail-{end}"
        cy = y + s // 2
        board(
            rail,
            PartRole.FRAME_RAIL_X,
            (x1 - x0 - s, t, apron),
            (x0 + s, cy - t // 2, underside - apron),
            1,
        )
        for side, sign, x in (("left", 1, x0 + s), ("right", -1, x1)):
            for drop in (14_000, 42_000):
                screw_joint(
                    design.design_id,
                    parts,
                    holes,
                    joints,
                    f"leg-{end}-{side}",
                    rail,
                    0,
                    sign,
                    (x, cy, underside - drop),
                )
    for side, x in (("left", x0), ("right", x1)):
        rail = f"rail-{side}"
        cx = x + s // 2
        board(
            rail,
            PartRole.FRAME_RAIL_Y,
            (t, rear - front - s, apron),
            (cx - t // 2, front + s, underside - apron),
            0,
        )
        for end, sign, y in (("front", 1, front + s), ("rear", -1, rear)):
            for drop in (26_000, 54_000):
                screw_joint(
                    design.design_id,
                    parts,
                    holes,
                    joints,
                    f"leg-{end}-{side}",
                    rail,
                    1,
                    sign,
                    (cx, y, underside - drop),
                )
        # Keep the four head recesses distinct and on the actual seat or tabletop.
        # The inset from the rail ends also clears both horizontal corner screws.
        mount_start = front + s
        mount_end = min(rear, intent.seat_depth_um) if isinstance(intent, ChairIntent) else rear
        if mount_end - mount_start < 100_000:
            raise ValueError("Sitsen eller skivan täcker för lite av sidosargen för två skruvar.")
        margin = min(45_000, (mount_end - mount_start - 16_000) // 2)
        for y in (mount_start + margin, mount_end - margin):
            screw_joint(
                design.design_id,
                parts,
                holes,
                joints,
                top,
                rail,
                2,
                -1,
                (cx, y, underside),
                head_recess_um=4_000,
            )
    if isinstance(intent, ChairIntent):
        angle = -intent.back_angle_mdeg
        bh = min(120_000, h - level - 30_000)
        dy, dz = rotated_yz(t, bh, angle)
        if dy > s - 2_000:
            raise ValueError("Rygglutningen kräver grövre bakben eller mindre vinkel.")
        by = rear + floor((s - dy) / 2)
        bz = h - ceil(bh * cos(radians(angle / 1000)))
        board(
            "chair-back",
            PartRole.CHAIR_BACK,
            (x1 - x0 - s, t, bh),
            (x0 + s, by, bz),
            1,
            angle=angle,
        )
        for side, sign, x in (("left", 1, x0 + s), ("right", -1, x1)):
            for z in (22_000, bh - 22_000):
                yy, zz = rotated_yz(t / 2, z, angle)
                screw_joint(
                    design.design_id,
                    parts,
                    holes,
                    joints,
                    "leg-rear-" + side,
                    "chair-back",
                    0,
                    sign,
                    (x, by + round(yy), bz + round(zz)),
                )
    return (
        tuple(p.model_copy(update={"features": tuple(holes[k])}) for k, p in parts.items()),
        tuple(joints),
    )
