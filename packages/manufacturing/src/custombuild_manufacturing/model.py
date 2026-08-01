"""Canonical manufacturing types.

The domain package owns furniture semantics.  This module deliberately contains
only the immutable data required to manufacture rectangular parts.  Dimensions
and coordinates are integers in micrometres; conversion to millimetres happens
only in exporters and postprocessors.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import StrEnum
from typing import Any, cast

UM_PER_MM = 1_000


class Side(StrEnum):
    A = "A"
    B = "B"
    EDGE = "EDGE"


class FeatureKind(StrEnum):
    DRILL = "DRILL"
    DRILL_PATTERN = "DRILL_PATTERN"
    COUNTERSINK = "COUNTERSINK"
    POCKET = "POCKET"
    GROOVE = "GROOVE"
    RABBET = "RABBET"
    INNER_CONTOUR = "INNER_CONTOUR"
    OUTER_CONTOUR = "OUTER_CONTOUR"
    ENGRAVE = "ENGRAVE"
    LABEL = "LABEL"


class Severity(StrEnum):
    PASS = "PASS"  # noqa: S105 -- manufacturing status, not a credential
    WARNING = "WARNING"
    BLOCK = "BLOCK"


class OperationKind(StrEnum):
    DRILL = "DRILL"
    COUNTERSINK = "COUNTERSINK"
    POCKET = "POCKET"
    GROOVE = "GROOVE"
    CONTOUR = "CONTOUR"
    ENGRAVE = "ENGRAVE"


@dataclass(frozen=True, slots=True)
class Point2D:
    x_um: int
    y_um: int


@dataclass(frozen=True, slots=True)
class Rect:
    x_um: int
    y_um: int
    width_um: int
    height_um: int

    @property
    def right_um(self) -> int:
        return self.x_um + self.width_um

    @property
    def top_um(self) -> int:
        return self.y_um + self.height_um

    @property
    def area_um2(self) -> int:
        return self.width_um * self.height_um

    def intersects(self, other: Rect, *, clearance_um: int = 0) -> bool:
        return not (
            self.right_um + clearance_um <= other.x_um
            or other.right_um + clearance_um <= self.x_um
            or self.top_um + clearance_um <= other.y_um
            or other.top_um + clearance_um <= self.y_um
        )

    def contains(self, other: Rect) -> bool:
        return (
            other.x_um >= self.x_um
            and other.y_um >= self.y_um
            and other.right_um <= self.right_um
            and other.top_um <= self.top_um
        )


@dataclass(frozen=True, slots=True)
class PanelAxisMapping:
    """Mapping between the domain part axes and the flat panel U/V axes."""

    u_axis: str = "x"
    v_axis: str = "y"
    thickness_axis: str = "z"

    def __post_init__(self) -> None:
        axes = (self.u_axis, self.v_axis, self.thickness_axis)
        if set(axes) != {"x", "y", "z"}:
            raise ValueError("panel axes must be a permutation of x, y and z")


@dataclass(frozen=True, slots=True)
class ManufacturingFeature:
    feature_id: str
    part_id: str
    kind: FeatureKind
    side: Side
    x_um: int
    y_um: int
    depth_um: int
    diameter_um: int | None = None
    width_um: int | None = None
    length_um: int | None = None
    radius_um: int | None = None
    pattern_count: int = 1
    pitch_um: int | None = None
    through: bool = False
    corner_strategy: str | None = None
    corner_relief_radius_um: int | None = None
    tolerance_um: int = 0
    fit_clearance_um: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.feature_id or not self.part_id:
            raise ValueError("feature_id and part_id are required")
        if self.depth_um <= 0:
            raise ValueError("feature depth must be positive")
        if self.pattern_count < 1:
            raise ValueError("pattern_count must be at least one")
        if self.pattern_count > 1 and (self.pitch_um is None or self.pitch_um <= 0):
            raise ValueError("a drill pattern requires a positive pitch")
        for value in (self.diameter_um, self.width_um, self.length_um, self.radius_um):
            if value is not None and value <= 0:
                raise ValueError("feature dimensions must be positive")
        if self.corner_relief_radius_um is not None and self.corner_relief_radius_um <= 0:
            raise ValueError("corner relief radius must be positive")
        if self.tolerance_um < 0 or self.fit_clearance_um < 0:
            raise ValueError("feature tolerance and fit clearance cannot be negative")

    def points(self) -> tuple[Point2D, ...]:
        if self.pattern_count == 1:
            return (Point2D(self.x_um, self.y_um),)
        assert self.pitch_um is not None
        return tuple(
            Point2D(self.x_um + index * self.pitch_um, self.y_um)
            for index in range(self.pattern_count)
        )

    def bounds(self) -> Rect:
        """Conservative local 2D bounds used for DFM and collision checks."""

        if self.kind in {
            FeatureKind.DRILL,
            FeatureKind.DRILL_PATTERN,
            FeatureKind.COUNTERSINK,
        }:
            diameter = self.diameter_um or (2 * (self.radius_um or 0))
            radius = diameter // 2
            points = self.points()
            left = min(point.x_um for point in points) - radius
            right = max(point.x_um for point in points) + radius
            bottom = min(point.y_um for point in points) - radius
            top = max(point.y_um for point in points) + radius
            return Rect(left, bottom, right - left, top - bottom)

        width = self.width_um or self.diameter_um or 0
        length = self.length_um or width
        return Rect(self.x_um, self.y_um, width, length)


@dataclass(frozen=True, slots=True)
class PartSpec:
    part_id: str
    name: str
    width_um: int
    height_um: int
    thickness_um: int
    material_id: str
    material_version: str
    features: tuple[ManufacturingFeature, ...] = ()
    grain_direction: str = "NONE"
    allow_rotation: bool = True
    quantity: int = 1
    weight_g: int = 0
    raw_width_um: int | None = None
    raw_height_um: int | None = None
    axis_mapping: PanelAxisMapping = field(default_factory=PanelAxisMapping)
    edge_bands: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.part_id:
            raise ValueError("part_id is required")
        if min(self.width_um, self.height_um, self.thickness_um) <= 0:
            raise ValueError("part dimensions must be positive")
        if self.quantity < 1:
            raise ValueError("quantity must be positive")
        if any(feature.part_id != self.part_id for feature in self.features):
            raise ValueError("all features must belong to the part")

    @property
    def blank_width_um(self) -> int:
        return self.raw_width_um or self.width_um

    @property
    def blank_height_um(self) -> int:
        return self.raw_height_um or self.height_um


@dataclass(frozen=True, slots=True)
class PartInstance:
    instance_id: str
    part: PartSpec


@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool_id: str
    name: str
    diameter_um: int
    cutting_length_um: int
    supported_operations: tuple[OperationKind, ...]
    spindle_rpm: int
    feed_um_min: int
    plunge_um_min: int
    measured_diameter_um: int | None = None
    runout_um: int = 0
    version: str = "1"

    def __post_init__(self) -> None:
        if min(self.diameter_um, self.cutting_length_um, self.spindle_rpm) <= 0:
            raise ValueError("invalid tool dimensions or spindle speed")
        if min(self.feed_um_min, self.plunge_um_min) <= 0:
            raise ValueError("tool feeds must be positive")

    @property
    def effective_diameter_um(self) -> int:
        return self.measured_diameter_um or self.diameter_um


@dataclass(frozen=True, slots=True)
class MachineProfile:
    profile_id: str
    name: str
    version: str
    controller: str
    work_width_um: int
    work_height_um: int
    work_z_um: int
    safe_z_um: int
    max_spindle_rpm: int
    supported_operations: tuple[OperationKind, ...]
    tools: tuple[ToolSpec, ...]
    tool_library_version: str = "unversioned"
    supported_sides: tuple[Side, ...] = (Side.A,)
    can_flip_stock: bool = False
    edge_aggregate: bool = False
    wcs_codes: tuple[str, ...] = ("G54", "G55")
    keep_out_zones: tuple[Rect, ...] = ()
    accuracy_um: int = 100

    def __post_init__(self) -> None:
        if min(self.work_width_um, self.work_height_um, self.work_z_um) <= 0:
            raise ValueError("machine work envelope must be positive")
        if self.safe_z_um <= 0 or self.max_spindle_rpm <= 0:
            raise ValueError("safe Z and spindle limit must be positive")
        if self.safe_z_um > self.work_z_um:
            raise ValueError("safe Z exceeds machine Z travel")


@dataclass(frozen=True, slots=True)
class StockSheet:
    stock_id: str
    material_id: str
    material_version: str
    width_um: int
    height_um: int
    thickness_um: int
    quantity: int = 1
    margin_um: int = 10_000
    kerf_um: int = 6_000
    grain_direction: str = "X"
    allow_rotation: bool = True
    defect_zones: tuple[Rect, ...] = ()
    clamp_zones: tuple[Rect, ...] = ()

    def __post_init__(self) -> None:
        if min(self.width_um, self.height_um, self.thickness_um, self.quantity) <= 0:
            raise ValueError("stock dimensions and quantity must be positive")
        if self.margin_um < 0 or self.kerf_um < 0:
            raise ValueError("stock margin and kerf cannot be negative")
        if self.margin_um * 2 >= min(self.width_um, self.height_um):
            raise ValueError("stock margin consumes the usable sheet")

    @property
    def usable_rect(self) -> Rect:
        return Rect(
            self.margin_um,
            self.margin_um,
            self.width_um - 2 * self.margin_um,
            self.height_um - 2 * self.margin_um,
        )


@dataclass(frozen=True, slots=True)
class Placement:
    instance_id: str
    part_id: str
    stock_id: str
    sheet_index: int
    x_um: int
    y_um: int
    width_um: int
    height_um: int
    rotated_90: bool

    @property
    def rect(self) -> Rect:
        return Rect(self.x_um, self.y_um, self.width_um, self.height_um)


@dataclass(frozen=True, slots=True)
class NestingLayout:
    stock: StockSheet
    placements: tuple[Placement, ...]
    unplaced_instance_ids: tuple[str, ...]
    used_sheet_count: int
    utilization_ppm: int
    algorithm: str = "deterministic-bottom-left-v1"

    @property
    def is_complete(self) -> bool:
        return not self.unplaced_instance_ids

    @property
    def stock_id(self) -> str:
        return self.stock.stock_id


@dataclass(frozen=True, slots=True)
class DFMIssue:
    code: str
    severity: Severity
    message: str
    part_id: str | None = None
    feature_id: str | None = None
    setup_id: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=dict)
    suggestion: str | None = None


@dataclass(frozen=True, slots=True)
class DFMReport:
    issues: tuple[DFMIssue, ...]
    engine_version: str = "dfm-1.0.0"

    @property
    def status(self) -> Severity:
        if any(issue.severity == Severity.BLOCK for issue in self.issues):
            return Severity.BLOCK
        if any(issue.severity == Severity.WARNING for issue in self.issues):
            return Severity.WARNING
        return Severity.PASS

    @property
    def blocking_issues(self) -> tuple[DFMIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == Severity.BLOCK)


@dataclass(frozen=True, slots=True)
class Setup:
    setup_id: str
    stock_id: str
    material_id: str
    material_version: str
    sheet_index: int
    side: Side
    wcs: str
    origin: Point2D
    stock_width_um: int
    stock_height_um: int
    stock_thickness_um: int
    safe_z_um: int
    reference_surface: str
    orientation: str
    fixture: str
    keep_out_zones: tuple[Rect, ...]
    tool_ids: tuple[str, ...]
    probe_method: str
    operator_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CAMOperation:
    operation_id: str
    setup_id: str
    part_id: str
    instance_id: str
    feature_id: str
    kind: OperationKind
    side: Side
    tool_id: str
    x_um: int
    y_um: int
    depth_um: int
    diameter_um: int | None = None
    width_um: int | None = None
    length_um: int | None = None
    stepdown_um: int | None = None
    stepover_ppm: int | None = None
    through: bool = False
    source_rotation_90: bool = False
    compensation: str | None = None
    holding_strategy: str | None = None
    corner_strategy: str | None = None
    corner_relief_radius_um: int | None = None
    tolerance_um: int = 0
    fit_clearance_um: int = 0


@dataclass(frozen=True, slots=True)
class OperationsDocument:
    schema_version: str
    design_hash: str
    machine_profile_id: str
    machine_profile_version: str
    setups: tuple[Setup, ...]
    operations: tuple[CAMOperation, ...]
    mode: str = "VALIDATION"
    tool_catalog_version: str = "unversioned"
    tool_catalog_fingerprint: str = ""
    tools: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        if self.mode != "VALIDATION":
            raise ValueError("the MVP only supports machine-neutral VALIDATION output")
        if self.operations and not self.tools:
            raise ValueError("operations document requires an exact selected-tool snapshot")
        if self.tool_catalog_version == "unversioned" or len(self.tool_catalog_fingerprint) != 64:
            raise ValueError("operations document requires a versioned tool-catalog fingerprint")
        tool_ids = [tool.tool_id for tool in self.tools]
        if len(set(tool_ids)) != len(tool_ids):
            raise ValueError("operations document contains duplicate selected tool IDs")
        expected_fingerprint = sha256_hex(
            canonical_json_bytes(tuple(sorted(self.tools, key=lambda item: item.tool_id)))
        )
        if self.tool_catalog_fingerprint != expected_fingerprint:
            raise ValueError("operations document tool snapshot fingerprint does not match")

    def as_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], canonical_data(self))

    def to_json(self) -> bytes:
        return canonical_json_bytes(self)


def expand_part_instances(parts: Iterable[PartSpec]) -> tuple[PartInstance, ...]:
    instances: list[PartInstance] = []
    for part in sorted(parts, key=lambda item: item.part_id):
        for index in range(part.quantity):
            suffix = f"{index + 1:03d}"
            instances.append(PartInstance(f"{part.part_id}:{suffix}", part))
    return tuple(instances)


def coerce_part_instances(
    parts: Iterable[PartSpec] | Iterable[PartInstance],
) -> tuple[PartInstance, ...]:
    values = tuple(parts)
    if all(isinstance(value, PartSpec) for value in values):
        return expand_part_instances(cast(tuple[PartSpec, ...], values))
    if all(isinstance(value, PartInstance) for value in values):
        return cast(tuple[PartInstance, ...], values)
    raise TypeError("parts must contain only PartSpec or only PartInstance values")


def canonical_data(value: Any) -> Any:
    """Convert dataclasses and enums into JSON-safe, stably ordered data."""

    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return canonical_data(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {
            str(key): canonical_data(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple | list):
        return [canonical_data(item) for item in value]
    if isinstance(value, set):
        return [canonical_data(item) for item in sorted(value, key=str)]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def um_to_mm(value: int) -> str:
    """Format integer micrometres as a non-exponential millimetre string."""

    negative = value < 0
    absolute = abs(value)
    whole, remainder = divmod(absolute, UM_PER_MM)
    rendered = f"{whole}.{remainder:03d}".rstrip("0") if remainder else str(whole)
    return f"-{rendered}" if negative else rendered
