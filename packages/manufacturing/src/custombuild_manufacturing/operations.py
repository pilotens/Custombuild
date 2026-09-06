"""Semantic manufacturing features to machine-neutral operations.json."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Never

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
    DFMIssue,
    DFMReport,
    FeatureKind,
    MachineProfile,
    NestingLayout,
    OperationKind,
    OperationsDocument,
    PartInstance,
    PartSpec,
    Point2D,
    Rect,
    Setup,
    Severity,
    Side,
    coerce_part_instances,
)
from .profiles import tool_catalog_fingerprint

OPERATIONS_SCHEMA_VERSION = "custombuild.operations.v2"
OPERATIONS_ENGINE_VERSION = "semantic-operations-1.3.0"
SETUP_PLAN_ENGINE_VERSION = "setup-plan-1.1.0"
CLIENT_DECLARED_AUTHORITY = "CLIENT_DECLARED"
MIN_VALIDATION_CONTOUR_KERF_UM = 6_000
MIN_REGISTRATION_PIN_DIAMETER_UM = 1_000
MAX_REGISTRATION_PIN_DIAMETER_UM = 50_000
MIN_REGISTRATION_POSITION_TOLERANCE_UM = 1
MAX_REGISTRATION_POSITION_TOLERANCE_UM = 10_000
MIN_REGISTRATION_USABLE_BASELINE_UM = 100_000
_REGISTRATION_IDENTITY_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
)


@dataclass(frozen=True, slots=True)
class TwoSidedRegistration:
    """Caller-declared stock coordinates for a two-sided validation setup.

    Every value is an unverified client declaration in the stock XY frame. It
    is sufficient for deterministic validation geometry, never physical setup
    approval or cutting authorization.
    """

    declaration_authority: str
    method_id: str
    fixture_method_version: str
    pin_diameter_um: int
    position_tolerance_um: int
    points: tuple[Point2D, ...]

    def __post_init__(self) -> None:
        if self.declaration_authority != CLIENT_DECLARED_AUTHORITY:
            raise ValueError("registration authority must be CLIENT_DECLARED")
        for label, value in (
            ("registration method ID", self.method_id),
            ("registration method version", self.fixture_method_version),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 64
                or value != value.strip()
                or any(character not in _REGISTRATION_IDENTITY_CHARACTERS for character in value)
            ):
                raise ValueError(f"{label} is invalid")
        if (
            type(self.pin_diameter_um) is not int
            or not MIN_REGISTRATION_PIN_DIAMETER_UM
            <= self.pin_diameter_um
            <= MAX_REGISTRATION_PIN_DIAMETER_UM
        ):
            raise ValueError("registration pin diameter is outside the validation envelope")
        if (
            type(self.position_tolerance_um) is not int
            or not MIN_REGISTRATION_POSITION_TOLERANCE_UM
            <= self.position_tolerance_um
            <= MAX_REGISTRATION_POSITION_TOLERANCE_UM
            or self.position_tolerance_um * 2 >= self.pin_diameter_um
        ):
            raise ValueError("registration position tolerance must be less than the pin radius")
        if not isinstance(self.points, tuple) or not 2 <= len(self.points) <= 16:
            raise ValueError("registration requires two to sixteen points")
        if any(not isinstance(point, Point2D) for point in self.points):
            raise ValueError("registration points must be Point2D values")
        if any(
            type(point.x_um) is not int or type(point.y_um) is not int for point in self.points
        ):
            raise ValueError("registration point coordinates must be integers")
        coordinates = tuple((point.x_um, point.y_um) for point in self.points)
        if len(set(coordinates)) != len(coordinates):
            raise ValueError("registration points must be unique")
        minimum_center_distance_um = (
            MIN_REGISTRATION_USABLE_BASELINE_UM
            + 2 * registration_pin_keep_out_radius_um(self)
        )
        if any(
            (first.x_um - second.x_um) ** 2 + (first.y_um - second.y_um) ** 2
            < minimum_center_distance_um**2
            for index, first in enumerate(self.points)
            for second in self.points[index + 1 :]
        ):
            raise ValueError(
                "every registration pin pair must retain a 100000 um usable baseline"
            )


def registration_pin_keep_out_radius_um(plan: TwoSidedRegistration) -> int:
    """Return the conservative integer radius including declared position tolerance."""

    return (plan.pin_diameter_um + 1) // 2 + plan.position_tolerance_um


def registration_pin_keep_out_rectangles(
    plan: TwoSidedRegistration,
) -> tuple[Rect, ...]:
    """Return deterministic conservative pin footprints for nesting exclusion."""

    radius_um = registration_pin_keep_out_radius_um(plan)
    return tuple(
        Rect(
            point.x_um - radius_um,
            point.y_um - radius_um,
            2 * radius_um,
            2 * radius_um,
        )
        for point in plan.points
    )


def generate_operations_document(
    *,
    design_hash: str,
    parts: Iterable[PartSpec] | Iterable[PartInstance],
    layout: NestingLayout,
    machine: MachineProfile,
    validate: bool = True,
    two_sided_registration_by_sheet: Mapping[int, TwoSidedRegistration] | None = None,
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
    sides_by_sheet: dict[int, set[Side]] = defaultdict(set)
    for placement in layout.placements:
        instance = instance_by_id.get(placement.instance_id)
        if instance is None:
            raise ProductionBlockedError(
                f"placement references unknown instance {placement.instance_id}"
            )
        sides_by_sheet[placement.sheet_index].update(
            feature.side for feature in instance.part.features if feature.side != Side.EDGE
        )

    registrations: dict[int, TwoSidedRegistration] = {}
    for sheet_index, sides in sorted(sides_by_sheet.items()):
        if not {Side.A, Side.B} <= sides:
            continue
        plan = (two_sided_registration_by_sheet or {}).get(sheet_index)
        registrations[sheet_index] = _require_two_sided_registration(
            layout,
            sheet_index,
            plan,
        )

    setup_operations: dict[tuple[int, Side], list[CAMOperation]] = defaultdict(list)
    release_operation_ids: set[str] = set()

    for placement in sorted(
        layout.placements,
        key=lambda item: (item.sheet_index, item.y_um, item.x_um, item.instance_id),
    ):
        instance = instance_by_id[placement.instance_id]
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
                            open_end_reliefs=feature.open_end_reliefs,
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
                cutter_envelope = transform_rect_to_machine(
                    feature.machining_bounds(),
                    placement,
                    layout.stock.height_um,
                    feature.side,
                )
                operation = CAMOperation(
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
                    cutter_envelope_x_um=cutter_envelope.x_um,
                    cutter_envelope_y_um=cutter_envelope.y_um,
                    cutter_envelope_width_um=cutter_envelope.width_um,
                    cutter_envelope_length_um=cutter_envelope.height_um,
                    stepdown_um=min(feature.depth_um, max(500, tool.effective_diameter_um // 2)),
                    stepover_ppm=400_000 if operation_kind.value in {"POCKET", "GROOVE"} else None,
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
                    open_end_reliefs=feature.open_end_reliefs,
                    tolerance_um=feature.tolerance_um,
                    fit_clearance_um=feature.fit_clearance_um,
                )
                setup_operations[(placement.sheet_index, feature.side)].append(operation)
                if feature.kind == FeatureKind.OUTER_CONTOUR and feature.through:
                    release_operation_ids.add(operation.operation_id)

    setups: list[Setup] = []
    operations: list[CAMOperation] = []
    setup_keys = sorted(
        setup_operations,
        key=lambda item: (item[0], _side_sequence(item[1])),
    )
    for setup_key in setup_keys:
        sheet_index, side = setup_key
        current_operations = sorted(
            setup_operations[setup_key],
            key=lambda item: (
                item.operation_id in release_operation_ids,
                item.kind == OperationKind.CONTOUR,
                item.tool_id,
                item.kind.value,
                item.operation_id,
            ),
        )
        tool_ids = tuple(sorted({operation.tool_id for operation in current_operations}))
        setups.append(
            _make_setup(
                layout,
                machine,
                sheet_index,
                side,
                tool_ids,
                registration=registrations.get(sheet_index),
            )
        )

    for sheet_index in sorted({key[0] for key in setup_keys}):
        sheet_operations = [
            operation
            for key in setup_keys
            if key[0] == sheet_index
            for operation in setup_operations[key]
        ]
        _require_compatible_global_order(
            layout,
            sheet_index,
            sheet_operations,
            release_operation_ids,
        )
        operations.extend(
            sorted(
                sheet_operations,
                key=lambda item: (
                    item.operation_id in release_operation_ids,
                    _side_sequence(item.side),
                    item.kind == OperationKind.CONTOUR,
                    item.tool_id,
                    item.kind.value,
                    item.operation_id,
                ),
            )
        )

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


def _side_sequence(side: Side) -> int:
    """Sequence the flip side before the final A-side setup."""

    return {Side.B: 0, Side.A: 1, Side.EDGE: 2}[side]


def _require_two_sided_registration(
    layout: NestingLayout,
    sheet_index: int,
    plan: TwoSidedRegistration | None,
) -> TwoSidedRegistration:
    if plan is None:
        _block_setup_plan(
            layout,
            sheet_index,
            code="TWO_SIDED_REGISTRATION_MISSING",
            message=(
                "Two-sided machining requires a caller-declared registration method "
                "with stock-frame XY coordinates."
            ),
        )

    footprints = registration_pin_keep_out_rectangles(plan)
    sheet_bounds = Rect(0, 0, layout.stock.width_um, layout.stock.height_um)
    outside_stock = any(not sheet_bounds.contains(footprint) for footprint in footprints)
    missing_keep_out = any(
        footprint not in layout.stock.clamp_zones for footprint in footprints
    )
    if outside_stock or missing_keep_out:
        _block_setup_plan(
            layout,
            sheet_index,
            code="TWO_SIDED_REGISTRATION_INVALID",
            message=(
                "Two-sided registration pin footprints must lie inside the sheet and be "
                "reserved as deterministic nesting keep-outs."
            ),
            inputs={
                "points_inside_stock": not outside_stock,
                "pin_keep_outs_reserved": not missing_keep_out,
            },
        )
    return plan


def _require_compatible_global_order(
    layout: NestingLayout,
    sheet_index: int,
    operations: list[CAMOperation],
    release_operation_ids: set[str],
) -> None:
    b_release = any(
        operation.side == Side.B and operation.operation_id in release_operation_ids
        for operation in operations
    )
    a_before_release = any(
        operation.side == Side.A and operation.operation_id not in release_operation_ids
        for operation in operations
    )
    if b_release and a_before_release:
        _block_setup_plan(
            layout,
            sheet_index,
            code="SETUP_SEQUENCE_CONFLICT",
            message=(
                "The sheet has a through outer contour on Side B and remaining Side A work. "
                "A single B-before-A plan cannot also keep every release contour globally last."
            ),
        )


def _block_setup_plan(
    layout: NestingLayout,
    sheet_index: int,
    *,
    code: str,
    message: str,
    inputs: Mapping[str, object] | None = None,
) -> Never:
    report = DFMReport(
        issues=(
            DFMIssue(
                code=code,
                severity=Severity.BLOCK,
                message=message,
                setup_id=_setup_id(layout.stock.stock_id, sheet_index, Side.B),
                inputs={"sheet_index": sheet_index, **dict(inputs or {})},
                suggestion=(
                    "Bind an externally specified coordinate registration and setup plan; "
                    "do not infer WCS, pins or fixtures."
                ),
            ),
        ),
        engine_version=SETUP_PLAN_ENGINE_VERSION,
    )
    raise ProductionBlockedError(
        f"CAM generation blocked by setup plan: {code}",
        report=report,
    )


def _make_setup(
    layout: NestingLayout,
    machine: MachineProfile,
    sheet_index: int,
    side: Side,
    tool_ids: tuple[str, ...],
    *,
    registration: TwoSidedRegistration | None,
) -> Setup:
    if registration is None:
        registration_instruction = "Bind an external coordinate registration before setup."
        probe_method = "EXTERNAL_COORDINATE_REGISTRATION_REQUIRED"
    else:
        coordinates = "|".join(f"{point.x_um},{point.y_um}" for point in registration.points)
        registration_instruction = (
            "Use the unverified client-declared registration only as validation input: "
            f"{registration.method_id}@{registration.fixture_method_version}, "
            f"pin diameter {registration.pin_diameter_um} um, position tolerance "
            f"{registration.position_tolerance_um} um. Verify the physical fixture, pins and "
            "coordinates independently during external work preparation."
        )
        probe_method = (
            "DECLARED_COORDINATE_REGISTRATION;"
            f"DECLARATION_AUTHORITY={registration.declaration_authority};"
            f"METHOD={registration.method_id};"
            f"METHOD_VERSION={registration.fixture_method_version};"
            f"PIN_DIAMETER_UM={registration.pin_diameter_um};"
            f"POSITION_TOLERANCE_UM={registration.position_tolerance_um};"
            f"STOCK_XY_UM={coordinates};"
            "EXTERNAL_SETUP_VERIFICATION_REQUIRED"
        )

    steps: tuple[str, ...]
    if side == Side.B:
        orientation = "FLIP_STOCK_ABOUT_X_AXIS; MACHINE_Y=STOCK_HEIGHT-DESIGN_Y"
        wcs_index = 1
        steps = (
            "Stop the machine and verify that the spindle is off.",
            "Flip the complete sheet about the X axis as defined by the validation transform.",
            registration_instruction,
            "Bind an external fixture plan, safe Z and keep-out verification before use.",
        )
    else:
        orientation = "A_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT"
        wcs_index = 0
        steps = (
            "Place Side A up using the declared stock-coordinate convention.",
            registration_instruction,
            "Bind an external fixture plan and verify every keep-out zone.",
            "Measure stock thickness and verify safe Z before validation.",
        )
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
        reference_surface="EXTERNAL_STOCK_TOP_MEASUREMENT_REQUIRED",
        orientation=orientation,
        fixture="EXTERNAL_FIXTURE_PLAN_REQUIRED; DECLARED_KEEP_OUT_ZONES_ONLY",
        keep_out_zones=tuple(layout.stock.clamp_zones) + tuple(machine.keep_out_zones),
        tool_ids=tool_ids,
        probe_method=probe_method,
        operator_steps=steps,
    )
