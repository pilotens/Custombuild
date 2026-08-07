"""Semantic manufacturing features to machine-neutral operations.json."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .dfm import (
    FEATURE_TO_OPERATION,
    DFMValidator,
    select_tool,
    transform_point_to_machine,
    transform_rect_to_machine,
)
from .errors import ProductionBlockedError
from .model import (
    CAMOperation,
    FeatureKind,
    MachineProfile,
    NestingLayout,
    OperationKind,
    OperationsDocument,
    PartInstance,
    PartSpec,
    Point2D,
    Setup,
    Side,
    coerce_part_instances,
)
from .profiles import tool_catalog_fingerprint

OPERATIONS_SCHEMA_VERSION = "custombuild.operations.v1"
OPERATIONS_ENGINE_VERSION = "semantic-operations-1.0.0"


def generate_operations_document(
    *,
    design_hash: str,
    parts: Iterable[PartSpec] | Iterable[PartInstance],
    layout: NestingLayout,
    machine: MachineProfile,
    validate: bool = True,
) -> OperationsDocument:
    instances = coerce_part_instances(parts)

    if validate:
        report = DFMValidator().validate(instances, layout, machine)
        if report.blocking_issues:
            codes = ", ".join(sorted({issue.code for issue in report.blocking_issues}))
            raise ProductionBlockedError(
                f"CAM generation blocked by DFM: {codes}",
                report=report,
            )

    instance_by_id = {item.instance_id: item for item in instances}
    setup_operations: dict[tuple[int, Side], list[CAMOperation]] = defaultdict(list)

    for placement in sorted(
        layout.placements,
        key=lambda item: (item.sheet_index, item.y_um, item.x_um, item.instance_id),
    ):
        instance = instance_by_id.get(placement.instance_id)
        if instance is None:
            raise ProductionBlockedError(
                f"placement references unknown instance {placement.instance_id}"
            )
        for feature in sorted(instance.part.features, key=lambda item: item.feature_id):
            if feature.side == Side.EDGE:
                raise ProductionBlockedError(
                    f"edge feature {feature.feature_id} cannot be emitted for the "
                    "reference 3-axis setup"
                )
            tool = select_tool(feature, machine)
            if tool is None:
                raise ProductionBlockedError(
                    f"feature {feature.feature_id} has no compatible verified tool"
                )
            setup_id = _setup_id(layout.stock.stock_id, placement.sheet_index, feature.side)
            operation_kind = FEATURE_TO_OPERATION[feature.kind]

            if feature.kind in {
                FeatureKind.DRILL,
                FeatureKind.DRILL_PATTERN,
                FeatureKind.COUNTERSINK,
            }:
                source_points = feature.points()
                for point_index, source in enumerate(source_points, start=1):
                    x_um, y_um = transform_point_to_machine(
                        source.x_um,
                        source.y_um,
                        placement,
                        layout.stock.height_um,
                        feature.side,
                    )
                    suffix = f":{point_index:03d}" if len(source_points) > 1 else ""
                    setup_operations[(placement.sheet_index, feature.side)].append(
                        CAMOperation(
                            operation_id=f"op:{placement.instance_id}:{feature.feature_id}{suffix}",
                            setup_id=setup_id,
                            part_id=instance.part.part_id,
                            instance_id=placement.instance_id,
                            feature_id=feature.feature_id,
                            kind=operation_kind,
                            side=feature.side,
                            tool_id=tool.tool_id,
                            x_um=x_um,
                            y_um=y_um,
                            depth_um=feature.depth_um,
                            diameter_um=feature.diameter_um,
                            stepdown_um=min(
                                feature.depth_um, max(500, tool.effective_diameter_um // 2)
                            ),
                            through=feature.through,
                            source_rotation_90=placement.rotated_90,
                            corner_strategy=feature.corner_strategy,
                            corner_relief_radius_um=feature.corner_relief_radius_um,
                            tolerance_um=feature.tolerance_um,
                            fit_clearance_um=feature.fit_clearance_um,
                        )
                    )
            else:
                transformed = transform_rect_to_machine(
                    feature.bounds(),
                    placement,
                    layout.stock.height_um,
                    feature.side,
                )
                setup_operations[(placement.sheet_index, feature.side)].append(
                    CAMOperation(
                        operation_id=f"op:{placement.instance_id}:{feature.feature_id}",
                        setup_id=setup_id,
                        part_id=instance.part.part_id,
                        instance_id=placement.instance_id,
                        feature_id=feature.feature_id,
                        kind=operation_kind,
                        side=feature.side,
                        tool_id=tool.tool_id,
                        x_um=transformed.x_um,
                        y_um=transformed.y_um,
                        depth_um=feature.depth_um,
                        diameter_um=feature.diameter_um,
                        width_um=transformed.width_um,
                        length_um=transformed.height_um,
                        stepdown_um=min(
                            feature.depth_um, max(500, tool.effective_diameter_um // 2)
                        ),
                        stepover_ppm=400_000
                        if operation_kind.value in {"POCKET", "GROOVE"}
                        else None,
                        through=feature.through,
                        source_rotation_90=placement.rotated_90,
                        compensation=str(feature.metadata.get("compensation"))
                        if feature.metadata.get("compensation")
                        else None,
                        holding_strategy=str(feature.metadata.get("holding_strategy"))
                        if feature.metadata.get("holding_strategy")
                        else None,
                        corner_strategy=feature.corner_strategy,
                        corner_relief_radius_um=feature.corner_relief_radius_um,
                        tolerance_um=feature.tolerance_um,
                        fit_clearance_um=feature.fit_clearance_um,
                    )
                )

    setups: list[Setup] = []
    operations: list[CAMOperation] = []
    for setup_key in sorted(setup_operations, key=lambda item: (item[0], item[1].value)):
        sheet_index, side = setup_key
        current_operations = sorted(
            setup_operations[setup_key],
            key=lambda item: (
                item.kind == OperationKind.CONTOUR,
                item.tool_id,
                item.kind.value,
                item.operation_id,
            ),
        )
        operations.extend(current_operations)
        tool_ids = tuple(sorted({operation.tool_id for operation in current_operations}))
        setups.append(_make_setup(layout, machine, sheet_index, side, tool_ids))

    selected_tool_ids = {operation.tool_id for operation in operations}
    selected_tools = tuple(
        sorted(
            (tool for tool in machine.tools if tool.tool_id in selected_tool_ids),
            key=lambda item: item.tool_id,
        )
    )

    return OperationsDocument(
        schema_version=OPERATIONS_SCHEMA_VERSION,
        design_hash=design_hash,
        machine_profile_id=machine.profile_id,
        machine_profile_version=machine.version,
        setups=tuple(setups),
        operations=tuple(operations),
        mode="VALIDATION",
        tool_catalog_version=machine.tool_library_version,
        tool_catalog_fingerprint=tool_catalog_fingerprint(selected_tools),
        tools=selected_tools,
    )


def _setup_id(stock_id: str, sheet_index: int, side: Side) -> str:
    return f"setup:{stock_id}:{sheet_index + 1:03d}:{side.value}"


def _make_setup(
    layout: NestingLayout,
    machine: MachineProfile,
    sheet_index: int,
    side: Side,
    tool_ids: tuple[str, ...],
) -> Setup:
    steps: tuple[str, ...]
    if side == Side.B:
        orientation = "FLIP_STOCK_ABOUT_X_AXIS; MACHINE_Y=STOCK_HEIGHT-DESIGN_Y"
        steps = (
            "Stoppa maskinen och verifiera att spindeln är avstängd.",
            "Vänd hela skivan runt X-axeln enligt setupbladet.",
            "Referera om arbetsnollan och kontrollmät två registreringspunkter.",
            "Verifiera klämmor, vakuum och säker Z innan valideringskörning.",
        )
        wcs_index = 1
    else:
        orientation = "A_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT"
        steps = (
            "Placera A-sidan uppåt med stockens nedre vänstra hörn vid arbetsnollan.",
            "Verifiera klämmor, vakuum och samtliga keep-out-zoner.",
            "Mät materialtjocklek och verifiera säker Z innan valideringskörning.",
        )
        wcs_index = 0
    wcs = machine.wcs_codes[min(wcs_index, len(machine.wcs_codes) - 1)]
    return Setup(
        setup_id=_setup_id(layout.stock.stock_id, sheet_index, side),
        stock_id=layout.stock.stock_id,
        material_id=layout.stock.material_id,
        material_version=layout.stock.material_version,
        sheet_index=sheet_index,
        side=side,
        wcs=wcs,
        origin=Point2D(0, 0),
        stock_width_um=layout.stock.width_um,
        stock_height_um=layout.stock.height_um,
        stock_thickness_um=layout.stock.thickness_um,
        safe_z_um=machine.safe_z_um,
        reference_surface="MEASURED_STOCK_TOP_Z0",
        orientation=orientation,
        fixture="VACUUM_OR_CLAMPS_AS_DECLARED_IN_KEEP_OUT_ZONES",
        keep_out_zones=tuple(layout.stock.clamp_zones) + tuple(machine.keep_out_zones),
        tool_ids=tool_ids,
        probe_method="MANUAL_OR_VERIFIED_PROBE; REFERENCE TWO XY REGISTRATION POINTS AND STOCK TOP",
        operator_steps=steps,
    )
