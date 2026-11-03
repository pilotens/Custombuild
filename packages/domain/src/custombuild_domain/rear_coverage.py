"""Measure which clear openings the generated back pieces actually cover."""

from typing import Literal

from pydantic import Field, model_validator

from .enums import PartRole
from .models import DesignResult, FrozenModel, PartInstance
from .reference_posts import ReferencePost, contact


class RearOpening(FrozenModel):
    zone: Literal["upper", "base"]
    bay: int
    left_um: int
    right_um: int
    bottom_um: int
    top_um: int
    state: Literal["covered", "partial", "open"]
    back_part_ids: tuple[str, ...]


def _opening(
    zone: Literal["upper", "base"],
    bay: int,
    left: int,
    right: int,
    bottom: int,
    top: int,
    backs: list[PartInstance],
) -> RearOpening:
    rectangles = []
    for part in backs:
        x, z = part.placement.x_um, part.placement.z_um
        a, b = max(left, x), min(right, x + part.finished_size.width_um)
        c, d = max(bottom, z), min(top, z + part.finished_size.height_um)
        if a < b and c < d:
            rectangles.append((a, b, c, d, part.part_id))
    # Check the union, not the sum of panel areas: overlaps cannot fill a gap.
    boundaries = sorted({left, right, *(x for a, b, _, _, _ in rectangles for x in (a, b))})
    complete = bool(rectangles)
    for x1, x2 in zip(boundaries, boundaries[1:], strict=False):
        covered_to = bottom
        for lo, hi in sorted((c, d) for a, b, c, d, _ in rectangles if a <= x1 and b >= x2):
            if lo > covered_to:
                break
            covered_to = max(covered_to, hi)
        if covered_to < top:
            complete = False
            break
    return RearOpening(
        zone=zone,
        bay=bay,
        left_um=left,
        right_um=right,
        bottom_um=bottom,
        top_um=top,
        state="covered" if complete else "partial" if rectangles else "open",
        back_part_ids=tuple(rect[-1] for rect in rectangles),
    )


def rear_openings(result: DesignResult) -> list[RearOpening]:
    by_name = {part.semantic_key: part for part in result.parts}
    backs = [part for part in result.parts if part.role == PartRole.BACK]
    bottom = by_name["bottom"]
    left, right = by_name["left-side"], by_name["right-side"]
    zones: list[tuple[Literal["upper", "base"], PartRole, int, int]] = [
        (
            "upper",
            PartRole.DIVIDER,
            bottom.placement.z_um + bottom.finished_size.height_um,
            by_name["top"].placement.z_um,
        )
    ]
    if result.spec.parameters.base_cabinet_count:
        floor = by_name["base-bottom-0"]
        zones.append(
            (
                "base",
                PartRole.BASE_SIDE,
                floor.placement.z_um + floor.finished_size.height_um,
                bottom.placement.z_um,
            )
        )
    openings = []
    for zone, role, z1, z2 in zones:
        walls = sorted((p for p in result.parts if p.role == role), key=lambda p: p.placement.x_um)
        for bay, (a, b) in enumerate(zip([left, *walls], [*walls, right], strict=True), start=1):
            openings.append(
                _opening(
                    zone,
                    bay,
                    a.placement.x_um + a.finished_size.width_um,
                    b.placement.x_um,
                    z1,
                    z2,
                    backs,
                )
            )
    return openings


class ReferenceConstruction(FrozenModel):
    uprights: Literal["unknown", "panels", "separate_posts"]
    back: Literal["unknown", "all_openings", "upper_only", "open"]
    shelves: Literal["unknown", "per_bay", "continuous"] = "unknown"
    material: Literal["unknown", "mdf", "birch-plywood", "solid-walnut"] = "unknown"
    unresolved_detail: str = Field(default="", max_length=300)
    posts: tuple[ReferencePost, ...] = Field(default=(), max_length=32, exclude_if=lambda v: not v)

    @model_validator(mode="after")
    def unique_posts(self) -> "ReferenceConstruction":
        if len({p.key for p in self.posts}) != len(self.posts):
            raise ValueError("Varje referensstolpe behöver ett eget ID.")
        return self


class ReferenceObject(FrozenModel):
    key: str
    label: str
    state: Literal["represented", "different", "unresolved"]
    compared_parts: tuple[str, ...]
    observation: str
    measured_post: ReferencePost | None = Field(default=None, exclude_if=lambda v: v is None)
    touching: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)
    overlapping: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)


def reference_objects(
    request: ReferenceConstruction, result: DesignResult
) -> list[ReferenceObject]:
    objects = []
    parts = result.parts
    side = next(p for p in parts if p.role == PartRole.LEFT_SIDE)
    objects.append(
        ReferenceObject(
            key="carcass-material",
            label="Stommaterial",
            state="unresolved"
            if request.material == "unknown"
            else "represented"
            if request.material == side.material_id
            else "different",
            compared_parts=(side.part_id,),
            observation=f"Referensval: {request.material}. Modell: {side.material_id}.",
        )
    )
    for p in parts:
        if p.role not in (PartRole.LEFT_SIDE, PartRole.DIVIDER, PartRole.RIGHT_SIDE):
            continue
        objects.append(
            ReferenceObject(
                key="support-" + p.semantic_key,
                label="Stödlinje · " + p.semantic_key,
                state="unresolved"
                if request.uprights == "unknown"
                else "different"
                if request.uprights == "separate_posts"
                else "represented",
                compared_parts=(p.part_id,),
                observation="Modellen har en skivsida eller avdelarskiva på denna plats.",
            )
        )

    rows: dict[str, list[PartInstance]] = {}
    for p in parts:
        if p.role == PartRole.SHELF:
            rows.setdefault(p.semantic_key.split("-b", 1)[0], []).append(p)
        elif p.role == PartRole.CABINET_FRONT:
            objects.append(
                ReferenceObject(
                    key=p.semantic_key,
                    label="Front · " + p.semantic_key,
                    state="represented",
                    compared_parts=(p.part_id,),
                    observation="Front modellerad. Gångjärn och borrbild behöver kontrolleras.",
                )
            )
    for row, shelves in rows.items():
        objects.append(
            ReferenceObject(
                key=row,
                label="Hyllrad · " + row,
                state="unresolved"
                if request.shelves == "unknown"
                else "different"
                if request.shelves == "continuous" and len(shelves) > 1
                else "represented",
                compared_parts=tuple(p.part_id for p in shelves),
                observation=f"Raden består av {len(shelves)} separata hyllplan.",
            )
        )
    for opening in rear_openings(result):
        zone = "underskåp" if opening.zone == "base" else "överdel"
        covered = request.back == "all_openings" or (
            request.back == "upper_only" and opening.zone == "upper"
        )
        actual = {"covered": "täckt", "partial": "delvis täckt", "open": "öppet"}[opening.state]
        objects.append(
            ReferenceObject(
                key=f"back-{opening.zone}-{opening.bay}",
                label=f"Rygg · {zone}, fack {opening.bay}",
                compared_parts=opening.back_part_ids,
                state="unresolved"
                if request.back == "unknown"
                else "represented"
                if opening.state == ("covered" if covered else "open")
                else "different",
                observation=f"Det fria facket är {actual}.",
            )
        )
    solids: list[tuple[str, ReferencePost | PartInstance]] = [(p.part_id, p) for p in parts]
    solids.extend((p.key, p) for p in request.posts)
    for post in request.posts:
        relations = [(key, contact(post, solid)) for key, solid in solids if key != post.key]
        touching = tuple(key for key, relation in relations if relation == "touch")
        overlapping = tuple(key for key, relation in relations if relation == "overlap")
        objects.append(
            ReferenceObject(
                key=post.key,
                label=post.name,
                measured_post=post,
                state="different",
                compared_parts=(),
                touching=touching,
                overlapping=overlapping,
                observation=(
                    f"Separat måttsatt referensdel utan förband. Berör {len(touching)} "
                    f"delar och överlappar {len(overlapping)} solider före bearbetning."
                ),
            )
        )
    if request.unresolved_detail.strip():
        objects.append(
            ReferenceObject(
                key="unmodeled-detail",
                label="Detalj från referensen",
                state="different",
                compared_parts=(),
                observation=request.unresolved_detail.strip(),
            )
        )
    return objects


def reference_discrepancies(
    request: ReferenceConstruction | None, result: DesignResult
) -> list[str]:
    if request is None:
        return ["Bekräfta stödens form, hyllornas indelning och ryggens omfattning."]
    return [
        f"{obj.label}: {obj.observation}"
        for obj in reference_objects(request, result)
        if obj.state != "represented"
    ]
