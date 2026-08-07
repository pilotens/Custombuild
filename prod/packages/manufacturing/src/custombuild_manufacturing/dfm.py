"""Design-for-manufacturing and machine capability validation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .model import (
    DFMIssue,
    DFMReport,
    FeatureKind,
    MachineProfile,
    ManufacturingFeature,
    NestingLayout,
    OperationKind,
    PartInstance,
    PartSpec,
    Placement,
    Rect,
    Severity,
    Side,
    ToolSpec,
    coerce_part_instances,
)
from .nesting import validate_layout

FEATURE_TO_OPERATION: dict[FeatureKind, OperationKind] = {
    FeatureKind.DRILL: OperationKind.DRILL,
    FeatureKind.DRILL_PATTERN: OperationKind.DRILL,
    FeatureKind.COUNTERSINK: OperationKind.COUNTERSINK,
    FeatureKind.POCKET: OperationKind.POCKET,
    FeatureKind.GROOVE: OperationKind.GROOVE,
    FeatureKind.RABBET: OperationKind.GROOVE,
    FeatureKind.INNER_CONTOUR: OperationKind.CONTOUR,
    FeatureKind.OUTER_CONTOUR: OperationKind.CONTOUR,
    FeatureKind.ENGRAVE: OperationKind.ENGRAVE,
    FeatureKind.LABEL: OperationKind.ENGRAVE,
}

DFM_ENGINE_VERSION = "dfm-1.0.0"


class DFMValidator:
    engine_version = DFM_ENGINE_VERSION

    def validate(
        self,
        parts: Iterable[PartSpec] | Iterable[PartInstance],
        layout: NestingLayout,
        machine: MachineProfile,
    ) -> DFMReport:
        instances = coerce_part_instances(parts)

        issues = list(validate_layout(layout, instances))
        issues.extend(self._validate_machine_envelope(layout, machine))

        placement_by_instance = {item.instance_id: item for item in layout.placements}
        for instance in sorted(instances, key=lambda item: item.instance_id):
            issues.extend(self._validate_part(instance.part, machine))
            placement = placement_by_instance.get(instance.instance_id)
            if placement:
                issues.extend(self._validate_keepouts(instance.part, placement, layout, machine))

        issues.extend(self._validate_feature_collisions(instances))
        return DFMReport(
            tuple(
                sorted(
                    _deduplicate(issues),
                    key=lambda issue: (
                        {Severity.BLOCK: 0, Severity.WARNING: 1, Severity.PASS: 2}[issue.severity],
                        issue.code,
                        issue.part_id or "",
                        issue.feature_id or "",
                    ),
                )
            ),
            self.engine_version,
        )

    def _validate_machine_envelope(
        self,
        layout: NestingLayout,
        machine: MachineProfile,
    ) -> list[DFMIssue]:
        issues: list[DFMIssue] = []
        stock = layout.stock
        if stock.width_um > machine.work_width_um or stock.height_um > machine.work_height_um:
            issues.append(
                DFMIssue(
                    "MACHINE_STOCK_ENVELOPE",
                    Severity.BLOCK,
                    "Selected stock exceeds the machine XY work envelope.",
                    inputs={
                        "stock_um": (stock.width_um, stock.height_um),
                        "machine_um": (machine.work_width_um, machine.work_height_um),
                    },
                    suggestion="Select smaller stock or another machine profile.",
                )
            )
        if stock.thickness_um + machine.safe_z_um > machine.work_z_um:
            issues.append(
                DFMIssue(
                    "MACHINE_Z_ENVELOPE",
                    Severity.BLOCK,
                    "Stock thickness plus safe clearance exceeds machine Z travel.",
                    inputs={
                        "stock_thickness_um": stock.thickness_um,
                        "safe_z_um": machine.safe_z_um,
                        "work_z_um": machine.work_z_um,
                    },
                )
            )
        return issues

    def _validate_part(self, part: PartSpec, machine: MachineProfile) -> list[DFMIssue]:
        issues: list[DFMIssue] = []
        if min(part.width_um, part.height_um, part.thickness_um) <= 0:
            issues.append(
                DFMIssue(
                    "PART_INVALID_DIMENSIONS",
                    Severity.BLOCK,
                    "Part dimensions must be positive.",
                    part_id=part.part_id,
                )
            )
            return issues

        for feature in sorted(part.features, key=lambda item: item.feature_id):
            issues.extend(self._validate_feature(part, feature, machine))
        issues.extend(self._validate_opposing_wall(part))
        return issues

    def _validate_feature(
        self,
        part: PartSpec,
        feature: ManufacturingFeature,
        machine: MachineProfile,
    ) -> list[DFMIssue]:
        issues: list[DFMIssue] = []
        operation_kind = FEATURE_TO_OPERATION[feature.kind]
        bounds = feature.bounds()
        panel = Rect(0, 0, part.width_um, part.height_um)

        if bounds.width_um <= 0 or bounds.height_um <= 0:
            issues.append(
                DFMIssue(
                    "FEATURE_INVALID_GEOMETRY",
                    Severity.BLOCK,
                    "Feature does not define a positive 2D machining extent.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                )
            )
        elif not panel.contains(bounds):
            issues.append(
                DFMIssue(
                    "FEATURE_OUTSIDE_PART",
                    Severity.BLOCK,
                    "Feature extends beyond the finished part boundary.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    inputs={"feature_bounds": bounds, "part_um": (part.width_um, part.height_um)},
                )
            )

        if feature.side == Side.EDGE and not machine.edge_aggregate:
            issues.append(
                DFMIssue(
                    "EDGE_ACCESS_UNAVAILABLE",
                    Severity.BLOCK,
                    "The selected 3-axis machine has no edge-machining aggregate.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    suggestion=(
                        "Document a separate setup or choose a machine with an edge aggregate."
                    ),
                )
            )
        elif feature.side == Side.B and not (
            Side.B in machine.supported_sides or machine.can_flip_stock
        ):
            issues.append(
                DFMIssue(
                    "B_SIDE_ACCESS_UNAVAILABLE",
                    Severity.BLOCK,
                    (
                        "B-side feature requires a documented flip setup that the machine "
                        "profile lacks."
                    ),
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                )
            )
        elif feature.side not in machine.supported_sides and feature.side != Side.B:
            issues.append(
                DFMIssue(
                    "FEATURE_SIDE_UNSUPPORTED",
                    Severity.BLOCK,
                    "Feature side is not supported by the selected machine profile.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    inputs={"side": feature.side.value},
                )
            )

        if operation_kind not in machine.supported_operations:
            issues.append(
                DFMIssue(
                    "OPERATION_UNSUPPORTED",
                    Severity.BLOCK,
                    "The selected machine profile does not support this operation.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    inputs={"operation": operation_kind.value},
                )
            )

        maximum_overcut_um = int(feature.metadata.get("maximum_overcut_um", 500))
        if feature.through:
            if feature.depth_um < part.thickness_um:
                issues.append(
                    DFMIssue(
                        "THROUGH_DEPTH_INCOMPLETE",
                        Severity.BLOCK,
                        "Through feature does not reach through the measured material thickness.",
                        part_id=part.part_id,
                        feature_id=feature.feature_id,
                        inputs={"depth_um": feature.depth_um, "thickness_um": part.thickness_um},
                    )
                )
            if feature.depth_um > part.thickness_um + maximum_overcut_um:
                issues.append(
                    DFMIssue(
                        "THROUGH_DEPTH_EXCESSIVE",
                        Severity.BLOCK,
                        "Through-cut overtravel exceeds the declared spoilboard allowance.",
                        part_id=part.part_id,
                        feature_id=feature.feature_id,
                        inputs={"maximum_overcut_um": maximum_overcut_um},
                    )
                )
        elif feature.depth_um >= part.thickness_um:
            issues.append(
                DFMIssue(
                    "NONTHROUGH_DEPTH",
                    Severity.BLOCK,
                    "Non-through feature reaches or exceeds measured material thickness.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    inputs={"depth_um": feature.depth_um, "thickness_um": part.thickness_um},
                )
            )
        else:
            remaining_um = part.thickness_um - feature.depth_um
            minimum_wall_um = int(feature.metadata.get("minimum_remaining_wall_um", 3_000))
            if remaining_um < minimum_wall_um:
                issues.append(
                    DFMIssue(
                        "REMAINING_WALL_TOO_THIN",
                        Severity.BLOCK,
                        "Feature leaves less material than the declared minimum wall.",
                        part_id=part.part_id,
                        feature_id=feature.feature_id,
                        inputs={"remaining_um": remaining_um, "minimum_um": minimum_wall_um},
                    )
                )

        if (
            feature.kind
            in {
                FeatureKind.DRILL,
                FeatureKind.DRILL_PATTERN,
                FeatureKind.COUNTERSINK,
            }
            and feature.diameter_um is None
        ):
            issues.append(
                DFMIssue(
                    "DRILL_DIAMETER_MISSING",
                    Severity.BLOCK,
                    "Drilling feature has no declared diameter.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                )
            )

        if bool(feature.metadata.get("requires_square_corners")):
            if not feature.corner_strategy:
                issues.append(
                    DFMIssue(
                        "INNER_CORNER_STRATEGY_MISSING",
                        Severity.BLOCK,
                        (
                            "Square internal corners require an explicit dogbone, T-bone or "
                            "equivalent strategy."
                        ),
                        part_id=part.part_id,
                        feature_id=feature.feature_id,
                        suggestion="Select and validate an internal-corner relief strategy.",
                    )
                )
            elif feature.corner_strategy != "dogbone-v1" or feature.corner_relief_radius_um is None:
                issues.append(
                    DFMIssue(
                        "INNER_CORNER_STRATEGY_UNSUPPORTED",
                        Severity.BLOCK,
                        "The declared internal-corner strategy is incomplete or unsupported.",
                        part_id=part.part_id,
                        feature_id=feature.feature_id,
                        inputs={"corner_strategy": feature.corner_strategy},
                    )
                )

        tool = select_tool(feature, machine)
        if tool is None:
            issues.append(
                DFMIssue(
                    "COMPATIBLE_TOOL_MISSING",
                    Severity.BLOCK,
                    (
                        "No selected tool can perform the feature with the required diameter "
                        "and cutting length."
                    ),
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    inputs={"operation": operation_kind.value, "depth_um": feature.depth_um},
                    suggestion="Add a verified tool or revise the operation.",
                )
            )
        elif tool.spindle_rpm > machine.max_spindle_rpm:
            issues.append(
                DFMIssue(
                    "TOOL_SPINDLE_LIMIT",
                    Severity.BLOCK,
                    "Tool recipe exceeds the machine spindle speed limit.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    inputs={
                        "tool_rpm": tool.spindle_rpm,
                        "machine_max_rpm": machine.max_spindle_rpm,
                    },
                )
            )
        elif (
            feature.corner_strategy == "dogbone-v1"
            and feature.corner_relief_radius_um is not None
            and 2 * feature.corner_relief_radius_um < tool.effective_diameter_um
        ):
            issues.append(
                DFMIssue(
                    "CORNER_RELIEF_TOO_SMALL",
                    Severity.BLOCK,
                    "Dogbone relief is smaller than the selected tool diameter.",
                    part_id=part.part_id,
                    feature_id=feature.feature_id,
                    inputs={
                        "relief_diameter_um": 2 * feature.corner_relief_radius_um,
                        "tool_diameter_um": tool.effective_diameter_um,
                    },
                )
            )

        if tool is not None and feature.fit_clearance_um:
            consumed_um = 2 * feature.tolerance_um + 2 * machine.accuracy_um + 2 * tool.runout_um
            margin_um = feature.fit_clearance_um - consumed_um
            if margin_um <= 0:
                issues.append(
                    DFMIssue(
                        "FIT_TOLERANCE_BUDGET_EXHAUSTED",
                        Severity.BLOCK,
                        "Machine, feature and tool uncertainty consume the declared fit clearance.",
                        part_id=part.part_id,
                        feature_id=feature.feature_id,
                        inputs={
                            "fit_clearance_um": feature.fit_clearance_um,
                            "feature_tolerance_um": feature.tolerance_um,
                            "machine_accuracy_um": machine.accuracy_um,
                            "tool_runout_um": tool.runout_um,
                            "remaining_margin_um": margin_um,
                        },
                        suggestion=(
                            "Use a calibrated machine/tool stack or revise the versioned fit."
                        ),
                    )
                )

        minimum_edge_um = int(feature.metadata.get("minimum_edge_distance_um", 2_000))
        if (
            feature.kind
            in {
                FeatureKind.DRILL,
                FeatureKind.DRILL_PATTERN,
                FeatureKind.COUNTERSINK,
            }
            and feature.diameter_um
        ):
            radius = feature.diameter_um // 2
            required = radius + minimum_edge_um
            for point in feature.points():
                distance = min(
                    point.x_um,
                    point.y_um,
                    part.width_um - point.x_um,
                    part.height_um - point.y_um,
                )
                if distance < required:
                    issues.append(
                        DFMIssue(
                            "HOLE_EDGE_DISTANCE",
                            Severity.BLOCK,
                            "Hole centre is too close to a finished part edge.",
                            part_id=part.part_id,
                            feature_id=feature.feature_id,
                            inputs={"actual_um": distance, "required_um": required},
                        )
                    )
                    break
        return issues

    @staticmethod
    def _validate_opposing_wall(part: PartSpec) -> list[DFMIssue]:
        """Check the shared core where A- and B-side removals overlap."""

        issues: list[DFMIssue] = []
        side_a = sorted(
            (
                feature
                for feature in part.features
                if feature.side == Side.A and not feature.through
            ),
            key=lambda item: item.feature_id,
        )
        side_b = sorted(
            (
                feature
                for feature in part.features
                if feature.side == Side.B and not feature.through
            ),
            key=lambda item: item.feature_id,
        )
        for first in side_a:
            for second in side_b:
                if not first.bounds().intersects(second.bounds()):
                    continue
                remaining_um = part.thickness_um - first.depth_um - second.depth_um
                minimum_um = max(
                    int(first.metadata.get("minimum_remaining_wall_um", 3_000)),
                    int(second.metadata.get("minimum_remaining_wall_um", 3_000)),
                )
                if remaining_um < minimum_um:
                    issues.append(
                        DFMIssue(
                            "OPPOSING_FEATURE_WALL_TOO_THIN",
                            Severity.BLOCK,
                            "Opposing A/B operations leave less than the minimum shared wall.",
                            part_id=part.part_id,
                            feature_id=first.feature_id,
                            inputs={
                                "other_feature_id": second.feature_id,
                                "remaining_um": remaining_um,
                                "minimum_um": minimum_um,
                            },
                            suggestion="Reduce depths, increase thickness or move a feature.",
                        )
                    )
        return issues

    def _validate_keepouts(
        self,
        part: PartSpec,
        placement: Placement,
        layout: NestingLayout,
        machine: MachineProfile,
    ) -> list[DFMIssue]:
        issues: list[DFMIssue] = []
        zones = tuple(machine.keep_out_zones) + tuple(layout.stock.clamp_zones)
        for feature in part.features:
            if feature.side == Side.EDGE:
                continue
            transformed = transform_rect_to_machine(
                feature.bounds(),
                placement,
                layout.stock.height_um,
                feature.side,
            )
            if any(transformed.intersects(zone) for zone in zones):
                issues.append(
                    DFMIssue(
                        "FEATURE_KEEPOUT_COLLISION",
                        Severity.BLOCK,
                        "Machining feature intersects a clamp or machine keep-out zone.",
                        part_id=part.part_id,
                        feature_id=feature.feature_id,
                        inputs={"machine_bounds": transformed},
                        suggestion="Re-nest the part or revise the fixture plan.",
                    )
                )
        return issues

    def _validate_feature_collisions(
        self,
        instances: Iterable[PartInstance],
    ) -> list[DFMIssue]:
        issues: list[DFMIssue] = []
        checked_parts: set[str] = set()
        for instance in instances:
            part = instance.part
            if part.part_id in checked_parts:
                continue
            checked_parts.add(part.part_id)
            per_side: dict[Side, list[ManufacturingFeature]] = defaultdict(list)
            for feature in part.features:
                per_side[feature.side].append(feature)
            for features in per_side.values():
                ordered = sorted(features, key=lambda item: item.feature_id)
                for index, first in enumerate(ordered):
                    for second in ordered[index + 1 :]:
                        if FeatureKind.OUTER_CONTOUR in {first.kind, second.kind}:
                            continue
                        first_allowed = set(first.metadata.get("allow_overlap_with", ()))
                        second_allowed = set(second.metadata.get("allow_overlap_with", ()))
                        if second.feature_id in first_allowed or first.feature_id in second_allowed:
                            continue
                        if first.bounds().intersects(second.bounds()):
                            issues.append(
                                DFMIssue(
                                    "FEATURE_COLLISION",
                                    Severity.BLOCK,
                                    (
                                        "Machining features overlap without an explicit "
                                        "compatible-feature rule."
                                    ),
                                    part_id=part.part_id,
                                    feature_id=first.feature_id,
                                    inputs={"other_feature_id": second.feature_id},
                                )
                            )
        return issues


def select_tool(feature: ManufacturingFeature, machine: MachineProfile) -> ToolSpec | None:
    operation_kind = FEATURE_TO_OPERATION[feature.kind]
    candidates: list[ToolSpec] = []
    for tool in machine.tools:
        if operation_kind not in tool.supported_operations:
            continue
        if tool.cutting_length_um < feature.depth_um:
            continue
        if feature.kind in {
            FeatureKind.DRILL,
            FeatureKind.DRILL_PATTERN,
            FeatureKind.COUNTERSINK,
        }:
            if feature.diameter_um is None:
                continue
            diameter_tolerance_um = max(
                machine.accuracy_um,
                int(feature.metadata.get("diameter_tolerance_um", 100)),
            )
            if abs(tool.effective_diameter_um - feature.diameter_um) > diameter_tolerance_um:
                continue
        elif feature.width_um is not None and tool.effective_diameter_um > feature.width_um:
            continue
        candidates.append(tool)
    if not candidates:
        return None
    return min(
        candidates, key=lambda tool: (tool.effective_diameter_um, tool.tool_id, tool.version)
    )


def transform_point_to_machine(
    x_um: int,
    y_um: int,
    placement: Placement,
    stock_height_um: int,
    side: Side,
) -> tuple[int, int]:
    """Map part-local coordinates through nesting and the documented B flip.

    Rotation is 90 degrees counter-clockwise.  B-side setup then flips the
    complete stock about its X axis, so machine Y is measured from the opposite
    long edge.  This convention is recorded in every generated setup.
    """

    if placement.rotated_90:
        nested_x = placement.x_um + (placement.width_um - y_um)
        nested_y = placement.y_um + x_um
    else:
        nested_x = placement.x_um + x_um
        nested_y = placement.y_um + y_um
    if side == Side.B:
        nested_y = stock_height_um - nested_y
    return nested_x, nested_y


def transform_rect_to_machine(
    rect: Rect,
    placement: Placement,
    stock_height_um: int,
    side: Side,
) -> Rect:
    corners = (
        (rect.x_um, rect.y_um),
        (rect.right_um, rect.y_um),
        (rect.x_um, rect.top_um),
        (rect.right_um, rect.top_um),
    )
    transformed = [
        transform_point_to_machine(x_um, y_um, placement, stock_height_um, side)
        for x_um, y_um in corners
    ]
    xs = [point[0] for point in transformed]
    ys = [point[1] for point in transformed]
    return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _deduplicate(issues: Iterable[DFMIssue]) -> list[DFMIssue]:
    unique: dict[tuple[str, str | None, str | None, str | None], DFMIssue] = {}
    for issue in issues:
        key = (issue.code, issue.part_id, issue.feature_id, issue.setup_id)
        unique.setdefault(key, issue)
    return list(unique.values())
