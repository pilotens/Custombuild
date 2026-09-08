"""Deterministic furniture-family geometry sharing the existing part model."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .engine import build_bookcase
from .enums import BackPanelType, FaceName, GrainDirection, PartRole, ShelfMount
from .furniture import (
    FURNITURE_ENGINE_VERSION,
    ChestIntent,
    FurnitureDesign,
    ShelvingIntent,
    TableIntent,
)
from .furniture_catalog import HardwareProfile, resolve_hardware, resolve_material
from .identity import content_hash, stable_id
from .models import (
    BookcaseDesignSpec,
    BookcaseParameters,
    DesignResult,
    Dimensions3D,
    FrozenModel,
    Joint,
    MaterialVersion,
    PartInstance,
    Placement,
)


class MovingGroup(FrozenModel):
    group_id: str
    part_ids: tuple[str, ...]
    axis: Literal["y"] = "y"
    travel_um: int = Field(ge=0)
    rated_load_n: int = Field(ge=0)


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
            position, size = part.placement, part.finished_size
            if (
                position.x_um + size.width_um > p.width_um
                or position.y_um + size.depth_um > p.depth_um
                or position.z_um + size.height_um > p.height_um
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
                shelf_load_n=shelf.shelf_load_n if shelving and shelf else 0,
                vertical_divider_count=shelf.divider_count if shelving and shelf else 0,
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
) -> tuple[tuple[PartInstance, ...], tuple[MovingGroup, ...]]:
    m, back = resolve_material(design.material), resolve_material(design.back_material)
    t, bottom = design.material.measured_thickness_um, design.back_material.measured_thickness_um
    w, d, h = p.width_um, p.depth_um, p.height_um
    gap, count = p.front_gap_um, p.drawer_count
    inner_width, inner_height = w - 2 * t, h - 2 * t
    drawer_width = inner_width - 2 * hardware.side_clearance_um
    drawer_depth = hardware.runner_length_um
    box_y = t + 2_000
    # The inset back has a 12 mm setback; respect its front surface and the
    # hardware profile's rear service clearance, including a changed batch.
    usable_depth = d - 12_000 - bottom - box_y - hardware.rear_clearance_um
    if drawer_depth > usable_depth or drawer_width - 2 * t < 100_000:
        raise ValueError("selected drawer profile does not fit the locked width/depth")
    base_height, remainder = divmod(inner_height, count)
    parts: list[PartInstance] = []
    groups: list[MovingGroup] = []
    cursor = t
    for index in range(count):
        row_height = base_height + (1 if index < remainder else 0)
        front_height = row_height - 2 * gap
        box_height = front_height - 10_000
        if box_height < 60_000 or box_height <= bottom:
            raise ValueError("drawer count and front gaps leave insufficient drawer height")
        left, z = t + hardware.side_clearance_um, cursor + gap + 10_000
        key = f"drawer-{index + 1}"
        boards = (
            _panel(
                design,
                f"{key}-front",
                PartRole.DRAWER_FRONT,
                (inner_width - 2 * gap, t, front_height),
                (t + gap, 0, cursor + gap),
                m,
                t,
                thin_axis="y",
            ),
            _panel(
                design,
                f"{key}-left",
                PartRole.DRAWER_SIDE,
                (t, drawer_depth, box_height),
                (left, box_y, z),
                m,
                t,
                thin_axis="x",
            ),
            _panel(
                design,
                f"{key}-right",
                PartRole.DRAWER_SIDE,
                (t, drawer_depth, box_height),
                (left + drawer_width - t, box_y, z),
                m,
                t,
                thin_axis="x",
            ),
            _panel(
                design,
                f"{key}-box-front",
                PartRole.DRAWER_FRONT,
                (drawer_width - 2 * t, t, box_height),
                (left + t, box_y, z),
                m,
                t,
                thin_axis="y",
            ),
            _panel(
                design,
                f"{key}-back",
                PartRole.DRAWER_BACK,
                (drawer_width - 2 * t, t, box_height),
                (left + t, box_y + drawer_depth - t, z),
                m,
                t,
                thin_axis="y",
            ),
            _panel(
                design,
                f"{key}-bottom",
                PartRole.DRAWER_BOTTOM,
                (drawer_width - 2 * t, drawer_depth - 2 * t, bottom),
                (left + t, box_y + t, z),
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
                rated_load_n=p.drawer_load_n,
            )
        )
        cursor += row_height
    return tuple(parts), tuple(groups)


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
        case TableIntent() as table:
            parts = _table_parts(design, table)
        case ChestIntent() as chest:
            if hardware is None:  # validated by the input contract
                raise ValueError("a drawer hardware profile is required")
            carcass = _carcass(design, shelving=False)
            drawers, groups = _drawer_parts(design, chest, hardware)
            parts, joints = (*carcass.parts, *drawers), carcass.joints
    used_materials = [material.model_dump(mode="json")]
    selections = [design.material.model_dump(mode="json")]
    if design.intent.family != "table":
        used_materials.append(resolve_material(design.back_material).model_dump(mode="json"))
        selections.append(design.back_material.model_dump(mode="json"))
    return FurnitureResult(
        design_hash=content_hash(
            {
                "engine": FURNITURE_ENGINE_VERSION,
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
