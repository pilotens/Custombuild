"""Deterministic furniture-family geometry sharing the existing part model."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .engine import build_bookcase
from .enums import BackPanelType, FaceName, FeatureKind, GrainDirection, PartRole, ShelfMount
from .furniture import (
    FURNITURE_ENGINE_VERSION,
    ChairIntent,
    ChestIntent,
    DiningTableIntent,
    FurnitureDesign,
    ShelvingIntent,
    TableIntent,
)
from .furniture_catalog import HardwareProfile, resolve_hardware, resolve_material
from .furniture_frames import build_frame, frame_bounds, screw_joint
from .identity import content_hash, stable_id
from .models import (
    BookcaseDesignSpec,
    BookcaseParameters,
    DesignResult,
    Dimensions3D,
    FeatureDimensions,
    FrozenModel,
    Joint,
    ManufacturingFeature,
    MaterialVersion,
    PartInstance,
    Placement,
    Point3D,
)


class MovingGroup(FrozenModel):
    group_id: str
    part_ids: tuple[str, ...]
    axis: Literal["y"] = "y"
    travel_um: int = Field(ge=0)
    requested_load_n: int = Field(ge=0)
    rated_pair_load_kg: int | None = Field(default=None, exclude_if=lambda v: v is None)


class FurnitureResult(FrozenModel):
    design_hash: str
    intent_hash: str
    engine_version: str = FURNITURE_ENGINE_VERSION
    template_version: str = "1.0.0"
    spec: FurnitureDesign
    parts: tuple[PartInstance, ...]
    joints: tuple[Joint, ...] = ()
    moving_groups: tuple[MovingGroup, ...] = ()
    hardware_profile: HardwareProfile | None = None
    total_weight_g: int = Field(gt=0)
    # Retain the existing, validated result for the shelving rule adapter.
    shelving_result: DesignResult | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_parts(self) -> FurnitureResult:
        ids = {part.part_id for part in self.parts}
        if len(ids) != len(self.parts) or not ids:
            raise ValueError("furniture must have unique, nonempty parts")
        if self.total_weight_g != sum(part.weight_g for part in self.parts):
            raise ValueError("furniture mass differs from its part inventory")
        moving = [part for group in self.moving_groups for part in group.part_ids]
        if len(moving) != len(set(moving)) or not set(moving) <= ids:
            raise ValueError("moving groups must reference distinct existing parts")
        p = self.spec.intent
        for part in self.parts:
            lower, upper = frame_bounds(part)
            if min(lower) < 0 or any(
                v > limit
                for v, limit in zip(upper, (p.width_um, p.depth_um, p.height_um), strict=True)
            ):
                raise ValueError("generated part exceeds the locked furniture dimensions")
        return self


def _panel(
    design: FurnitureDesign,
    key: str,
    role: PartRole,
    size: tuple[int, int, int],
    position: tuple[int, int, int],
    material: MaterialVersion,
    thickness: int,
    *,
    thin_axis: Literal["x", "y", "z"],
) -> PartInstance:
    axes = {"x": 0, "y": 1, "z": 2}
    if size[axes[thin_axis]] != thickness or min(size) <= 0:
        raise ValueError("panel dimensions do not match its material thickness")
    faces = {
        "x": (FaceName.RIGHT, FaceName.LEFT),
        "y": (FaceName.FRONT, FaceName.BACK),
        "z": (FaceName.TOP, FaceName.BOTTOM),
    }[thin_axis]
    grain = GrainDirection.Z if thin_axis == "x" else GrainDirection.X
    dimensions = Dimensions3D(width_um=size[0], depth_um=size[1], height_um=size[2])
    return PartInstance(
        part_id=stable_id("part", design.design_id, key),
        semantic_key=key,
        role=role,
        instance_index=0,
        revision=design.revision,
        finished_size=dimensions,
        raw_size=dimensions,
        placement=Placement(x_um=position[0], y_um=position[1], z_um=position[2]),
        material_id=material.material_id,
        material_version=material.version,
        actual_thickness_um=thickness,
        grain_direction=GrainDirection.NONE
        if material.grain_direction == GrainDirection.NONE
        else grain,
        a_side=faces[0],
        b_side=faces[1],
        weight_g=max(
            1, (size[0] * size[1] * size[2] * material.density_kg_m3 + 10**15 - 1) // 10**15
        ),
    )


def _carcass(design: FurnitureDesign, *, shelving: bool) -> DesignResult:
    p = design.intent
    shelf = p if isinstance(p, ShelvingIntent) else None
    material, back = resolve_material(design.material), resolve_material(design.back_material)
    if material.nominal_thickness_um != 18_000 or back.nominal_thickness_um != 6_000:
        raise ValueError("this carcass family requires 18 mm panels and a 6 mm back profile")
    return build_bookcase(
        BookcaseDesignSpec(
            design_id=design.design_id,
            revision=design.revision,
            material=material,
            back_material=None if shelf and shelf.back_panel == BackPanelType.NONE else back,
            parameters=BookcaseParameters(
                width_um=p.width_um,
                height_um=p.height_um,
                depth_um=p.depth_um,
                actual_thickness_um=design.material.measured_thickness_um,
                back_thickness_um=design.back_material.measured_thickness_um,
                plinth_height_um=shelf.plinth_height_um if shelf else 0,
                back_panel=shelf.back_panel if shelf else BackPanelType.INSET_GROOVE,
                shelf_mount=shelf.shelf_mount if shelf else ShelfMount.FIXED,
                shelf_count=shelf.shelf_count if shelving and shelf else 0,
                shelf_load_n=shelf.resolved_shelf_load_n if shelving and shelf else 0,
                vertical_divider_count=shelf.divider_count
                if shelving and shelf
                else (p.drawer_columns - 1 if isinstance(p, ChestIntent) else 0),
                shelf_height_ratios_ppm=shelf.shelf_height_ratios_ppm if shelf else (),
                bay_width_ratios_ppm=shelf.bay_width_ratios_ppm if shelf else (),
            ),
        )
    )


def _table_parts(design: FurnitureDesign, p: TableIntent) -> tuple[PartInstance, ...]:
    m = resolve_material(design.material)
    t = design.material.measured_thickness_um
    w, d, h = p.width_um, p.depth_um, p.height_um
    inset, apron = p.end_inset_um, p.stretcher_height_um
    clear_width = w - 2 * (inset + t)
    if w < 400_000 or h < 300_000 or d < 300_000 or clear_width < 250_000:
        raise ValueError("table dimensions leave insufficient space between the end supports")
    if h - t - apron < 200_000 or d <= 4 * t:
        raise ValueError("table height/depth leaves no usable clearance below the top")
    return (
        _panel(design, "table-top", PartRole.TOP, (w, d, t), (0, 0, h - t), m, t, thin_axis="z"),
        _panel(
            design,
            "table-end-left",
            PartRole.TABLE_END,
            (t, d, h - t),
            (inset, 0, 0),
            m,
            t,
            thin_axis="x",
        ),
        _panel(
            design,
            "table-end-right",
            PartRole.TABLE_END,
            (t, d, h - t),
            (w - inset - t, 0, 0),
            m,
            t,
            thin_axis="x",
        ),
        _panel(
            design,
            "table-stretcher-front",
            PartRole.TABLE_STRETCHER,
            (clear_width, t, apron),
            (inset + t, t, h - t - apron),
            m,
            t,
            thin_axis="y",
        ),
        _panel(
            design,
            "table-stretcher-back",
            PartRole.TABLE_STRETCHER,
            (clear_width, t, apron),
            (inset + t, d - 2 * t, h - t - apron),
            m,
            t,
            thin_axis="y",
        ),
    )


def _drawer_parts(
    design: FurnitureDesign,
    p: ChestIntent,
    hardware: HardwareProfile,
    carcass: DesignResult,
) -> tuple[tuple[PartInstance, ...], tuple[MovingGroup, ...], tuple[Joint, ...]]:
    m, back = resolve_material(design.material), resolve_material(design.back_material)
    t, bottom = design.material.measured_thickness_um, design.back_material.measured_thickness_um
    d, h = p.depth_um, p.height_um
    gap, count = p.front_gap_um, p.drawer_count
    inner_height = h - 2 * t
    walls = sorted(
        (
            part
            for part in carcass.parts
            if part.role in (PartRole.LEFT_SIDE, PartRole.RIGHT_SIDE, PartRole.DIVIDER)
        ),
        key=lambda part: part.placement.x_um,
    )
    rows = count // p.drawer_columns
    drawer_depth = hardware.runner_length_um
    box_y = t if hardware.drawer_holes_um else t + 2_000
    # The inset back has a 12 mm setback; respect its front surface and the
    # hardware profile's rear service clearance, including a changed batch.
    usable_depth = d - 12_000 - bottom - box_y - hardware.rear_clearance_um
    if drawer_depth > usable_depth:
        raise ValueError("selected drawer profile does not fit the locked width/depth")
    gaps = (
        [gap] * (rows + 1)
        if p.gap_basis == "clear_reveal"
        else [gap] + [2 * gap] * (rows - 1) + [gap]
    )
    remaining = inner_height - sum(gaps) - sum(p.row_front_heights_um)
    base_height, remainder = divmod(remaining, rows - len(p.row_front_heights_um))
    heights = list(p.row_front_heights_um) + [
        base_height + int(i < remainder) for i in range(rows - len(p.row_front_heights_um))
    ]
    parts = list(carcass.parts)
    groups: list[MovingGroup] = []
    fittings: list[Joint] = []
    bores: dict[str, list[ManufacturingFeature]] = {part.semantic_key: [] for part in parts}
    by_key = {part.semantic_key: part for part in parts}
    for index in range(count):
        row, column = divmod(index, p.drawer_columns)
        left_wall, right_wall = walls[column : column + 2]
        start = left_wall.placement.x_um + t
        inner_width = right_wall.placement.x_um - start
        drawer_width = inner_width - 2 * hardware.side_clearance_um
        if drawer_width - 2 * t < 100_000:
            raise ValueError("Lådskenorna ryms inte inom valt fack.")
        front_z = t + sum(heights[:row]) + sum(gaps[: row + 1])
        front_height = heights[row]
        box_height = front_height - 10_000
        if box_height < 60_000 or box_height <= bottom:
            raise ValueError("drawer count and front gaps leave insufficient drawer height")
        left, z = start + hardware.side_clearance_um, front_z + 10_000
        under = bottom if hardware.drawer_holes_um else 0
        key = f"drawer-{index + 1}"
        boards = (
            _panel(
                design,
                f"{key}-front",
                PartRole.DRAWER_FRONT,
                (inner_width - 2 * gap, t, front_height),
                (start + gap, 0, front_z),
                m,
                t,
                thin_axis="y",
            ),
            _panel(
                design,
                f"{key}-left",
                PartRole.DRAWER_SIDE,
                (t, drawer_depth, box_height - under),
                (left, box_y, z + under),
                m,
                t,
                thin_axis="x",
            ),
            _panel(
                design,
                f"{key}-right",
                PartRole.DRAWER_SIDE,
                (t, drawer_depth, box_height - under),
                (left + drawer_width - t, box_y, z + under),
                m,
                t,
                thin_axis="x",
            ),
            _panel(
                design,
                f"{key}-box-front",
                PartRole.DRAWER_FRONT,
                (drawer_width - 2 * t, t, box_height - under),
                (left + t, box_y, z + under),
                m,
                t,
                thin_axis="y",
            ),
            _panel(
                design,
                f"{key}-back",
                PartRole.DRAWER_BACK,
                (drawer_width - 2 * t, t, box_height - under),
                (left + t, box_y + drawer_depth - t, z + under),
                m,
                t,
                thin_axis="y",
            ),
            _panel(
                design,
                f"{key}-bottom",
                PartRole.DRAWER_BOTTOM,
                (drawer_width, drawer_depth, bottom)
                if under
                else (drawer_width - 2 * t, drawer_depth - 2 * t, bottom),
                (left, box_y, z) if under else (left + t, box_y + t, z),
                back,
                bottom,
                thin_axis="z",
            ),
        )
        parts.extend(boards)
        groups.append(
            MovingGroup(
                group_id=key,
                part_ids=tuple(part.part_id for part in boards),
                travel_um=hardware.travel_um,
                requested_load_n=p.drawer_load_n,
                rated_pair_load_kg=hardware.rated_pair_load_kg,
            )
        )
        if not hardware.drawer_holes_um:
            continue
        for part in boards:
            by_key[part.semantic_key] = part
            bores[part.semantic_key] = []
        mid_z = z + under + (box_height - under) // 2
        if p.drawer_columns == 2:
            mid_z += 6_000 if column == 0 else -6_000
        if min(mid_z - (z + under), z + box_height - mid_z) < 23_000:
            raise ValueError("Lådsidan är för låg för den 46 mm höga skenan.")
        for side, wall, wall_face, box_face in (
            ("left", left_wall, FaceName.RIGHT, FaceName.LEFT),
            ("right", right_wall, FaceName.LEFT, FaceName.RIGHT),
        ):
            side_key = f"{key}-{side}"
            for member, face, offsets, pilot_depth in (
                (wall, wall_face, hardware.cabinet_holes_um, 12_000),
                (by_key[side_key], box_face, hardware.drawer_holes_um, 12_000),
            ):
                for k, offset in enumerate(offsets):
                    bores[member.semantic_key].append(
                        ManufacturingFeature(
                            feature_id=stable_id("runner", key, member.part_id, k),
                            part_id=member.part_id,
                            kind=FeatureKind.DRILL,
                            face=face,
                            origin=Point3D(
                                x_um=t if face == FaceName.RIGHT else 0,
                                y_um=box_y + offset - member.placement.y_um,
                                z_um=mid_z - member.placement.z_um,
                            ),
                            dimensions=FeatureDimensions(diameter_um=2_500, depth_um=pilot_depth),
                        )
                    )
            sign = 1 if side == "left" else -1
            mating_x = left + t if side == "left" else left + drawer_width - t
            # Opposing screws in the drawer walls and the separate bottom panel.
            for end, cy in (("box-front", box_y + t // 2), ("back", box_y + drawer_depth - t // 2)):
                for cz in (z + under + 13_000, z + box_height - 13_000):
                    screw_joint(
                        design.design_id,
                        by_key,
                        bores,
                        fittings,
                        side_key,
                        f"{key}-{end}",
                        0,
                        sign,
                        (mating_x, cy, cz),
                        length_um=40_000,
                        screw_mm=4,
                    )
            centre_x = mating_x - sign * (t // 2)
            for cy in (box_y + 55_000, box_y + drawer_depth // 2, box_y + drawer_depth - 55_000):
                screw_joint(
                    design.design_id,
                    by_key,
                    bores,
                    fittings,
                    key + "-bottom",
                    side_key,
                    2,
                    1,
                    (centre_x, cy, z + under),
                    length_um=20_000,
                    screw_mm=4,
                )
        for cx in (left + 2 * t, left + drawer_width - 2 * t):
            screw_joint(
                design.design_id,
                by_key,
                bores,
                fittings,
                key + "-box-front",
                key + "-front",
                1,
                -1,
                (cx, box_y, z + box_height // 2),
                length_um=30_000,
                screw_mm=4,
            )
    return (
        tuple(
            part.model_copy(
                update={"features": (*part.features, *bores.get(part.semantic_key, ()))}
            )
            for part in parts
        ),
        tuple(groups),
        tuple(fittings),
    )


def build_furniture(design: FurnitureDesign) -> FurnitureResult:
    material = resolve_material(design.material)
    hardware = resolve_hardware(design.hardware, design.intent.family) if design.hardware else None
    if hardware and not (
        hardware.minimum_thickness_um
        <= design.material.measured_thickness_um
        <= hardware.maximum_thickness_um
    ):
        raise ValueError("hardware does not support the selected panel thickness")
    carcass: DesignResult | None = None
    groups: tuple[MovingGroup, ...] = ()
    joints: tuple[Joint, ...] = ()
    match design.intent:
        case ShelvingIntent():
            carcass = _carcass(design, shelving=True)
            parts, joints = carcass.parts, carcass.joints
        case DiningTableIntent() | ChairIntent():
            parts, joints = build_frame(design, design.intent)
        case TableIntent() as table:
            parts = _table_parts(design, table)
        case ChestIntent() as chest:
            if hardware is None:  # validated by the input contract
                raise ValueError("a drawer hardware profile is required")
            carcass = _carcass(design, shelving=False)
            parts, groups, fasteners = _drawer_parts(design, chest, hardware, carcass)
            joints = (*carcass.joints, *fasteners)
    used_materials = [material.model_dump(mode="json")]
    selections = [design.material.model_dump(mode="json")]
    if design.intent.family in {"chest_of_drawers", "shelving"}:
        used_materials.append(resolve_material(design.back_material).model_dump(mode="json"))
        selections.append(design.back_material.model_dump(mode="json"))
    # Keep previously frozen shelving sources valid whenever their compiler is unchanged.
    engine_version = (
        "furniture-families-1.0.0"
        if isinstance(design.intent, ShelvingIntent | TableIntent)
        else FURNITURE_ENGINE_VERSION
    )
    return FurnitureResult(
        engine_version=engine_version,
        design_hash=content_hash(
            {
                "engine": engine_version,
                "intent": design.intent,
                **({"installation": design.installation} if design.installation else {}),
                "parts": [p.model_dump(mode="json", exclude={"revision"}) for p in parts],
                "joints": joints,
                "groups": groups,
                "materials": used_materials,
                "selections": selections,
                "hardware": hardware,
            }
        ),
        intent_hash=content_hash(
            {"intent": design.intent, "installation": design.installation}
            if design.installation
            else design.intent
        ),
        spec=design,
        parts=parts,
        joints=joints,
        moving_groups=groups,
        hardware_profile=hardware,
        total_weight_g=sum(part.weight_g for part in parts),
        shelving_result=carcass if design.intent.family == "shelving" else None,
    )
