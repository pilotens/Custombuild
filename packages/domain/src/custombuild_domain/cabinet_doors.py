"""The J95 inset drilling pattern for the walnut base row, in integer micrometres."""

from collections import defaultdict
from dataclasses import dataclass

from .enums import FaceName, FeatureKind, PartRole
from .models import (
    BookcaseParameters,
    FeatureDimensions,
    ManufacturingFeature,
    PartInstance,
    Point3D,
)

HINGE = "J95-C24/0T-NI"
PLATE = "J95-P6T-NI"
SCREW = "SPAX YELLOX 5x16 countersunk full thread"
CATALOGUE = "https://www.sugatsune.com/content/site-assets/201B_PDF/286_287.pdf"


@dataclass(frozen=True)
class DoorMount:
    door: PartInstance
    support: PartInstance
    holes: tuple[ManufacturingFeature, ...]


def door_mounts(parts: tuple[PartInstance, ...], p: BookcaseParameters) -> list[DoorMount]:
    doors = sorted(
        (part for part in parts if part.role == PartRole.CABINET_FRONT),
        key=lambda part: part.instance_index,
    )
    by_key = {part.semantic_key: part for part in parts}
    if [door.instance_index for door in doors] != list(range(p.base_cabinet_count)) or not doors:
        raise ValueError("Varje underskåpsfack behöver en egen lucka.")
    return [
        _mount(door, by_key["left-side" if i == 0 else f"base-side-{i}"], p)
        for i, door in enumerate(doors)
    ]


def _mount(door: PartInstance, left: PartInstance, p: BookcaseParameters) -> DoorMount:
    i = door.instance_index
    size = door.finished_size
    if (
        door.material_id != "solid-walnut"
        or left.material_id != door.material_id
        or door.actual_thickness_um != 18_000
        or left.actual_thickness_um != 18_000
        or size.depth_um != 18_000
    ):
        raise ValueError("J95-provet kräver 18 mm massiv valnöt i luckan och dess gavel.")
    if size.width_um > 800_000 or door.weight_g > 25_000:
        raise ValueError(f"Lucka {i + 1} överskrider 800 mm eller 25 kg. Lägg till ett fack.")
    if size.height_um != 660_000 or p.base_cabinet_height_um != 700_000:
        raise ValueError("Gångjärnsprofilen omfattar en 700 mm hög underskåpsrad.")
    if door.placement.x_um - left.placement.x_um - left.finished_size.width_um != 2000:
        raise ValueError(f"Lucka {i + 1} kräver 2 mm spel vid gångjärnsgaveln.")
    holes: list[ManufacturingFeature] = []

    def bore(
        owner: PartInstance,
        tag: str,
        u: int,
        v: int,
        diameter: int,
        depth: int,
        *,
        through: bool = False,
    ) -> None:
        on_door = owner == door
        face = FaceName.BACK if on_door else FaceName.RIGHT
        width = owner.finished_size.width_um if on_door else owner.finished_size.depth_um
        if (
            min(u, width - u, v, owner.finished_size.height_um - v) * 2 <= diameter
            or (depth >= owner.actual_thickness_um) != through
        ):
            raise ValueError(f"Borrning utanför ämnet på {owner.semantic_key}: {tag}.")
        holes.append(
            ManufacturingFeature(
                feature_id=f"J95-{i + 1}-{tag}",
                part_id=owner.part_id,
                kind=FeatureKind.DRILL,
                face=face,
                origin=Point3D(
                    x_um=u if on_door else owner.actual_thickness_um,
                    y_um=owner.actual_thickness_um if on_door else u,
                    z_um=v,
                ),
                dimensions=FeatureDimensions(diameter_um=diameter, depth_um=depth),
                through=through,
                tolerance_um=100,
            )
        )

    for n, z in enumerate((80_000, size.height_um - 80_000), 1):
        # Wood diagram p.286: C=7 gives b=2. The cup screw line is 32.5
        # from the near rim, not from the centre. Mount H = door T + 42.
        bore(door, f"{n}-cup", 27_000, z, 40_000, 15_000)
        for j, dz in enumerate((-24_000, 24_000), 1):
            bore(door, f"{n}-cup-screw-{j}", 39_500, z + dz, 3500, 15_000)
        support_z = door.placement.z_um + z - left.placement.z_um
        front = door.placement.y_um + size.depth_um - left.placement.y_um
        for row, dy in enumerate((42_000, 68_000), 1):
            for col, dz in enumerate((-16_000, 16_000), 1):
                bore(left, f"{n}-plate-{row}-{col}", front + dy, support_z + dz, 3500, 15_000)
    # A through finger pull works without another screw or latch on the closing side.
    bore(
        door,
        "finger-pull",
        size.width_um - 45_000,
        size.height_um - 65_000,
        30_000,
        size.depth_um,
        through=True,
    )
    return DoorMount(door, left, tuple(holes))


def fit_doors(parts: tuple[PartInstance, ...], p: BookcaseParameters) -> tuple[PartInstance, ...]:
    holes: dict[str, list[ManufacturingFeature]] = defaultdict(list)
    for mount in door_mounts(parts, p):
        for hole in mount.holes:
            holes[hole.part_id].append(hole)
    return tuple(
        part.model_copy(update={"features": (*part.features, *holes[part.part_id])})
        if part.part_id in holes
        else part
        for part in parts
    )
