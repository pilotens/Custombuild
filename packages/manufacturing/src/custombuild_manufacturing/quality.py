"""Deterministic, non-authorizing workshop quality documents."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from typing import Any

from .model import (
    NestingLayout,
    OperationsDocument,
    PartInstance,
    PartSpec,
    canonical_json_bytes,
    expand_part_instances,
    um_to_mm,
)

LABEL_INDEX_SCHEMA_VERSION = "custombuild.label-index.v1"
QUALITY_MEASUREMENT_PLAN_SCHEMA_VERSION = "custombuild.quality-measurement-plan.v1"


def label_index_csv(
    *,
    parts: Iterable[PartSpec],
    layouts: NestingLayout | Iterable[NestingLayout],
    operations: OperationsDocument,
) -> bytes:
    """Return one traceable label row for every placed part instance.

    The QR payload is an identifier, not an approval token.  It binds the
    physical label to the immutable design hash and exact expanded instance.
    """

    part_values = tuple(parts)
    layout_values = _coerce_layouts(layouts)
    instances = _instances_by_id(part_values)
    placements = _validated_placements(layout_values, instances)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "schema_version",
            "design_hash",
            "instance_id",
            "part_id",
            "part_name",
            "material_id",
            "material_version",
            "finished_width_mm",
            "finished_height_mm",
            "finished_thickness_mm",
            "stock_id",
            "sheet_number",
            "placement_x_mm",
            "placement_y_mm",
            "placement_width_mm",
            "placement_height_mm",
            "rotated_90",
            "qr_payload",
            "physical_release_authorized",
        )
    )
    for placement in placements:
        instance = instances[placement.instance_id]
        part = instance.part
        writer.writerow(
            (
                LABEL_INDEX_SCHEMA_VERSION,
                operations.design_hash,
                instance.instance_id,
                part.part_id,
                part.name,
                part.material_id,
                part.material_version,
                um_to_mm(part.width_um),
                um_to_mm(part.height_um),
                um_to_mm(part.thickness_um),
                placement.stock_id,
                placement.sheet_index + 1,
                um_to_mm(placement.x_um),
                um_to_mm(placement.y_um),
                um_to_mm(placement.width_um),
                um_to_mm(placement.height_um),
                str(placement.rotated_90).lower(),
                f"custombuild:part:{operations.design_hash}:{instance.instance_id}",
                "false",
            )
        )
    return stream.getvalue().encode("utf-8")


def quality_measurement_plan_json(
    *,
    parts: Iterable[PartSpec],
    layouts: NestingLayout | Iterable[NestingLayout],
    operations: OperationsDocument,
) -> bytes:
    """Return a complete inspection plan with deliberately blank results."""

    part_values = tuple(parts)
    layout_values = _coerce_layouts(layouts)
    instances = _instances_by_id(part_values)
    placements = _validated_placements(layout_values, instances)
    placed_ids = {placement.instance_id for placement in placements}
    unplaced_ids = tuple(sorted(set(instances) - placed_ids))

    dimension_checks: list[dict[str, Any]] = []
    for instance in sorted(instances.values(), key=lambda item: item.instance_id):
        for axis, nominal_um in (
            ("width", instance.part.width_um),
            ("height", instance.part.height_um),
            ("thickness", instance.part.thickness_um),
        ):
            dimension_checks.append(
                {
                    "check_id": f"dimension:{instance.instance_id}:{axis}",
                    "instance_id": instance.instance_id,
                    "part_id": instance.part.part_id,
                    "kind": "FINISHED_DIMENSION",
                    "axis": axis,
                    "nominal_um": nominal_um,
                    "tolerance_um": None,
                    "tolerance_status": "EXTERNAL_TOLERANCE_REQUIRED",
                    "measured_um": None,
                    "result": None,
                    "measured_by": None,
                    "measured_at": None,
                }
            )

    operation_checks: list[dict[str, Any]] = []
    for operation in sorted(operations.operations, key=lambda item: item.operation_id):
        declared_tolerance = operation.tolerance_um or None
        operation_checks.append(
            {
                "check_id": f"operation:{operation.operation_id}",
                "operation_id": operation.operation_id,
                "setup_id": operation.setup_id,
                "instance_id": operation.instance_id,
                "part_id": operation.part_id,
                "feature_id": operation.feature_id,
                "kind": operation.kind.value,
                "side": operation.side.value,
                "nominal": {
                    "x_um": operation.x_um,
                    "y_um": operation.y_um,
                    "depth_um": operation.depth_um,
                    "diameter_um": operation.diameter_um,
                    "width_um": operation.width_um,
                    "length_um": operation.length_um,
                },
                "tolerance_um": declared_tolerance,
                "tolerance_status": (
                    "DECLARED_IN_DESIGN"
                    if declared_tolerance is not None
                    else "EXTERNAL_TOLERANCE_REQUIRED"
                ),
                "fit_clearance_um": operation.fit_clearance_um or None,
                "measured": None,
                "result": None,
                "measured_by": None,
                "measured_at": None,
            }
        )

    payload = {
        "schema_version": QUALITY_MEASUREMENT_PLAN_SCHEMA_VERSION,
        "design_hash": operations.design_hash,
        "release_scope": "DESIGN_REVIEW",
        "physical_release_authorized": False,
        "approval_state": "PENDING_EXTERNAL_MEASUREMENT",
        "instructions": (
            "Record actual measurements and an authorized result externally; "
            "blank fields are intentional and never imply approval."
        ),
        "coverage": {
            "part_instance_count": len(instances),
            "placed_instance_count": len(placed_ids),
            "unplaced_instance_ids": unplaced_ids,
            "dimension_check_count": len(dimension_checks),
            "operation_count": len(operations.operations),
            "operation_check_count": len(operation_checks),
        },
        "dimension_checks": dimension_checks,
        "operation_checks": operation_checks,
    }
    return canonical_json_bytes(payload)


def _coerce_layouts(
    layouts: NestingLayout | Iterable[NestingLayout],
) -> tuple[NestingLayout, ...]:
    return (layouts,) if isinstance(layouts, NestingLayout) else tuple(layouts)


def _instances_by_id(parts: tuple[PartSpec, ...]) -> dict[str, PartInstance]:
    instances = expand_part_instances(parts)
    by_id = {instance.instance_id: instance for instance in instances}
    if len(by_id) != len(instances):
        raise ValueError("expanded part instances must have unique IDs")
    return by_id


def _validated_placements(
    layouts: tuple[NestingLayout, ...],
    instances: dict[str, PartInstance],
) -> tuple[Any, ...]:
    placements = tuple(
        sorted(
            (placement for layout in layouts for placement in layout.placements),
            key=lambda item: (item.stock_id, item.sheet_index, item.instance_id),
        )
    )
    seen: set[str] = set()
    for placement in placements:
        instance = instances.get(placement.instance_id)
        if instance is None:
            raise ValueError(f"placement references unknown instance {placement.instance_id}")
        if placement.part_id != instance.part.part_id:
            raise ValueError(
                f"placement {placement.instance_id} does not match part {instance.part.part_id}"
            )
        if placement.instance_id in seen:
            raise ValueError(f"part instance {placement.instance_id} is placed more than once")
        seen.add(placement.instance_id)
    return placements
