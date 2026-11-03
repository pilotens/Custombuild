"""One component and its mates, in the same units and views used by the drawing exporters."""

from collections import Counter
from typing import Literal

from custombuild_domain.enums import PartRole
from custombuild_domain.furniture_catalog import RUNNER_FIXING_SKU, HardwareProfile
from custombuild_domain.furniture_engine import FurnitureResult
from custombuild_domain.identity import content_hash
from custombuild_domain.models import DesignResult, FrozenModel, Joint, PartInstance
from custombuild_domain.rear_coverage import (
    RearOpening,
    ReferenceConstruction,
    ReferenceObject,
    rear_openings,
    reference_objects,
)
from pydantic import computed_field

from .adapters import adapt_domain_part
from .exporters import svg_for_part
from .furniture_faces import drawing_faces
from .model import FeatureKind


class PartMate(FrozenModel):
    part_id: str
    semantic_key: str
    joint: Joint


class DrillPosition(FrozenModel):
    feature_id: str
    u_um: int
    v_um: int
    diameter_um: int
    depth_um: int
    through: bool


class PartDrawing(FrozenModel):
    view: str
    physical_face: str
    u_axis: str
    v_axis: str
    holes: tuple[DrillPosition, ...]
    svg: str


class PartFastener(FrozenModel):
    sku: str
    quantity: int
    feature_ids: tuple[str, ...]


class RemovalInspection(FrozenModel):
    separated_groups: tuple[tuple[str, ...], ...]
    remaining_frame_feet: tuple[str, ...] | None
    affected_moving_groups: tuple[str, ...]


class InspectionStock(FrozenModel):
    source: str
    width_um: int
    height_um: int
    margin_um: int
    grain: str | None
    fits: bool | None


class PartInspection(FrozenModel):
    design_hash: str
    part: PartInstance
    mates: tuple[PartMate, ...]
    hardware: tuple[PartFastener, ...]
    drawings: tuple[PartDrawing, ...]
    removal: RemovalInspection
    rear_openings: tuple[RearOpening, ...]
    reference_objects: tuple[ReferenceObject, ...]
    runner: HardwareProfile | None
    stock: tuple[InspectionStock, ...]
    part_names: dict[str, str]
    physical_cutting_authorized: Literal[False] = False

    @computed_field  # type: ignore[prop-decorator]  # Pydantic read-only receipt.
    @property
    def sheet_sha256(self) -> str:
        return content_hash(self.model_dump(mode="json", exclude={"sheet_sha256"}))


def _connected(start: str, graph: dict[str, set[str]], excluded: set[str]) -> set[str]:
    reached: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in reached or current in excluded:
            continue
        reached.add(current)
        pending.extend(graph[current] - reached - excluded)
    return reached


def inspect_part(
    result: DesignResult | FurnitureResult,
    part: PartInstance,
    reference: ReferenceConstruction | None = None,
    stock: tuple[InspectionStock, ...] = (),
) -> PartInspection:
    parts = {p.part_id: p for p in result.parts}
    graph: dict[str, set[str]] = {key: set() for key in parts}
    mates = []
    counts: Counter[str] = Counter()
    holes: dict[str, list[str]] = {}
    for joint in result.joints:
        a, b = joint.members
        graph[a.part_id].add(b.part_id)
        graph[b.part_id].add(a.part_id)
        if part.part_id not in (a.part_id, b.part_id):
            continue
        own, other = (a, b) if a.part_id == part.part_id else (b, a)
        mates.append(
            PartMate(
                part_id=other.part_id, semantic_key=parts[other.part_id].semantic_key, joint=joint
            )
        )
        if joint.hardware_sku:
            counts[joint.hardware_sku] += joint.hardware_count
            holes.setdefault(joint.hardware_sku, []).extend(own.feature_ids)

    remainder = _connected(part.part_id, graph, set()) - {part.part_id}
    groups = []
    while remainder:
        group = _connected(min(remainder), graph, {part.part_id})
        groups.append(tuple(sorted(group)))
        remainder -= group
    legs = [p for p in result.parts if p.role == PartRole.FRAME_LEG and p.placement.z_um == 0]
    feet = tuple(p.part_id for p in legs if p.part_id != part.part_id) if legs else None
    moving: tuple[str, ...] = ()
    attached_runner = None
    native = result if isinstance(result, DesignResult) else result.shelving_result
    if isinstance(result, FurnitureResult):
        moving = tuple(g.group_id for g in result.moving_groups if part.part_id in g.part_ids)
        runner = result.hardware_profile
        runner_holes = [f.feature_id for f in part.features if f.feature_id.startswith("run_")]
        if runner and runner.drawer_holes_um and runner_holes:
            attached_runner = runner
            sku = RUNNER_FIXING_SKU
            counts[sku] += len(runner_holes)
            holes.setdefault(sku, []).extend(runner_holes)
    drawings = []
    for view in drawing_faces(part, adapt_domain_part(part)):
        mapping = view.part.axis_mapping
        face = {"A": part.a_side.value, "B": part.b_side.value}.get(view.name, view.name.lower())
        drawings.append(
            PartDrawing(
                view=view.name,
                physical_face=face,
                u_axis=mapping.u_axis,
                v_axis=mapping.v_axis,
                holes=tuple(
                    DrillPosition(
                        feature_id=f.feature_id,
                        u_um=point.x_um,
                        v_um=point.y_um,
                        diameter_um=f.diameter_um or 0,
                        depth_um=f.depth_um,
                        through=f.through,
                    )
                    for f in view.part.features
                    if f.side == view.side
                    and f.kind in {FeatureKind.DRILL, FeatureKind.DRILL_PATTERN}
                    for point in f.points()
                ),
                svg=svg_for_part(view.part, view.side).decode(),
            )
        )
    return PartInspection(
        design_hash=result.design_hash,
        runner=attached_runner,
        stock=stock,
        part_names={p.part_id: p.semantic_key for p in result.parts},
        reference_objects=tuple(reference_objects(reference, native))
        if reference and native
        else (),
        part=part,
        mates=tuple(mates),
        hardware=tuple(
            PartFastener(sku=sku, quantity=counts[sku], feature_ids=tuple(holes[sku]))
            for sku in sorted(counts)
        ),
        drawings=tuple(drawings),
        removal=RemovalInspection(
            separated_groups=tuple(groups), remaining_frame_feet=feet, affected_moving_groups=moving
        ),
        rear_openings=tuple(rear_openings(native)) if native is not None else (),
    )
