"""Machine-neutral CAM validation and safe-Z backplot generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from custombuild_manufacturing.model import (
    CAMOperation,
    MachineProfile,
    OperationKind,
    OperationsDocument,
    Rect,
    Setup,
    Side,
    ToolSpec,
    canonical_json_bytes,
    sha256_hex,
    um_to_mm,
)
from custombuild_manufacturing.profiles import (
    LARGE_FORMAT_MACHINE_PROFILE_ID,
    REFERENCE_MACHINE_PROFILE_ID,
    linuxcnc_reference_router_1325,
    linuxcnc_reference_router_5125,
)

from .model import MoveKind, RemovalEnvelope, ValidationBackplot, ValidationMove

CAM_VALIDATION_VERSION = "cam-validation-1.3.0"
CAM_BACKPLOT_VERSION = "validation-backplot-1.0.0"
OPERATIONS_SCHEMA_VERSION = "custombuild.operations.v2"
_DESIGN_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_WCS_PATTERN = re.compile(r"G5[4-9]")
_CANONICAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_DECLARED_PROBE_PATTERN = re.compile(
    r"DECLARED_COORDINATE_REGISTRATION;"
    r"METHOD=([A-Za-z0-9][A-Za-z0-9._:-]{0,159});"
    r"STOCK_XY_UM=([0-9]+,[0-9]+(?:\|[0-9]+,[0-9]+)+);"
    r"EXTERNAL_SETUP_VERIFICATION_REQUIRED"
)
_DEFAULT_THROUGH_OVERTRAVEL_UM = 500
_EXTERNAL_REFERENCE_SURFACE = "EXTERNAL_STOCK_TOP_MEASUREMENT_REQUIRED"
_EXTERNAL_FIXTURE = "EXTERNAL_FIXTURE_PLAN_REQUIRED; DECLARED_KEEP_OUT_ZONES_ONLY"
_EXTERNAL_PROBE = "EXTERNAL_COORDINATE_REGISTRATION_REQUIRED"
_SIDE_A_ORIENTATION = "A_SIDE_UP; STOCK_ORIGIN_AT_LOWER_LEFT"
_SIDE_B_ORIENTATION = "FLIP_STOCK_ABOUT_X_AXIS; MACHINE_Y=STOCK_HEIGHT-DESIGN_Y"
_RELEASE_HOLDING_STRATEGY = "TABS_OR_ONION_SKIN_REQUIRES_SETUP_APPROVAL"
_ALLOWED_COMPENSATION = frozenset({"CENTER", "INSIDE", "OUTSIDE"})
_ALLOWED_OPEN_END_RELIEFS = frozenset({"u_min", "u_max", "v_min", "v_max"})
_ALLOWED_CORNER_STRATEGIES = frozenset({"dogbone-v1", "dogbone-v2"})


class CAMValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CAMValidationResult:
    valid: bool
    errors: tuple[str, ...]


def validate_operations_document(
    document: OperationsDocument,
    *,
    machine: MachineProfile | None = None,
) -> CAMValidationResult:
    """Validate the serialized CAM boundary, optionally against its exact machine.

    The document intentionally contains a selected-tool snapshot but not the
    complete machine envelope.  Callers that can resolve the versioned machine
    profile should therefore pass it here; identity, travel, WCS, safe-Z and
    tool catalogue bindings then become part of the same fail-closed check.
    """

    errors: list[str] = []
    if document.schema_version != OPERATIONS_SCHEMA_VERSION:
        errors.append("unsupported operations schema_version")
    if document.mode != "VALIDATION":
        errors.append("only VALIDATION operations documents are accepted")
    if _DESIGN_HASH_PATTERN.fullmatch(document.design_hash) is None:
        errors.append("design_hash must be a lowercase SHA-256 hex digest")
    if not document.machine_profile_id or not document.machine_profile_version:
        errors.append("versioned machine profile identity is required")

    trusted_machine = _resolve_reference_machine(document.machine_profile_id)
    if trusted_machine is None:
        errors.append("operations document references an unresolved machine profile")
    if machine is not None and machine != trusted_machine:
        errors.append("caller-supplied machine profile differs from the trusted canonical profile")
        if document.machine_profile_id != machine.profile_id:
            errors.append("operations document machine profile ID mismatch")
    # A caller may provide a machine only as an exact assertion of the
    # server-owned profile.  Never let it replace the trusted resolver merely
    # by reusing a known ID and version with broader travel or weaker limits.
    bound_machine = trusted_machine

    if not document.setups:
        errors.append("operations document requires at least one setup")
    if not document.operations:
        errors.append("operations document requires at least one operation")

    tool_by_id = {tool.tool_id: tool for tool in document.tools}
    if len(tool_by_id) != len(document.tools):
        errors.append("duplicate tool_id in selected-tool snapshot")
    for tool in document.tools:
        errors.extend(_validate_tool_snapshot(tool))
    try:
        expected_tool_fingerprint = sha256_hex(
            canonical_json_bytes(tuple(sorted(document.tools, key=lambda item: item.tool_id)))
        )
    except (AttributeError, TypeError, ValueError):
        expected_tool_fingerprint = ""
        errors.append("selected-tool snapshot cannot be canonicalized")
    if (
        _DESIGN_HASH_PATTERN.fullmatch(document.tool_catalog_fingerprint) is None
        or document.tool_catalog_fingerprint != expected_tool_fingerprint
    ):
        errors.append("selected-tool snapshot fingerprint mismatch")
    if not document.tool_catalog_version or document.tool_catalog_version == "unversioned":
        errors.append("selected-tool snapshot has no catalogue version")

    setup_by_id = {setup.setup_id: setup for setup in document.setups}
    if len(setup_by_id) != len(document.setups):
        errors.append("duplicate setup_id")
    for setup in document.setups:
        errors.extend(_validate_setup(setup, machine=bound_machine))
    errors.extend(_validate_setup_stock_bindings(document.setups))

    machine_tool_by_id: dict[str, ToolSpec] = {}
    if bound_machine is not None:
        errors.extend(_validate_machine_binding(document, bound_machine))
        machine_tool_by_id = {tool.tool_id: tool for tool in bound_machine.tools}

    operations_by_setup: dict[str, set[str]] = {setup.setup_id: set() for setup in document.setups}
    operation_tool_ids: set[str] = set()
    operation_ids: set[str] = set()
    for operation in document.operations:
        if operation.operation_id in operation_ids:
            errors.append(f"duplicate operation_id: {operation.operation_id}")
        operation_ids.add(operation.operation_id)
        operation_setup = setup_by_id.get(operation.setup_id)
        if operation_setup is None:
            errors.append(f"operation references unknown setup: {operation.operation_id}")
            continue
        operations_by_setup[operation_setup.setup_id].add(operation.tool_id)
        operation_tool_ids.add(operation.tool_id)
        if operation.tool_id not in tool_by_id:
            errors.append(
                f"operation references tool absent from snapshot: {operation.operation_id}"
            )
        errors.extend(
            _validate_operation(
                operation,
                operation_setup,
                tool=tool_by_id.get(operation.tool_id),
                machine=bound_machine,
                machine_tool=machine_tool_by_id.get(operation.tool_id),
            )
        )
    for setup in document.setups:
        snapshot_tool_ids = set(setup.tool_ids)
        if any(tool_id not in tool_by_id for tool_id in snapshot_tool_ids):
            errors.append(f"setup references tool absent from snapshot: {setup.setup_id}")
        if snapshot_tool_ids != operations_by_setup[setup.setup_id]:
            errors.append(
                f"setup tool list does not exactly match its operations: {setup.setup_id}"
            )
    if set(tool_by_id) != operation_tool_ids:
        errors.append("selected-tool snapshot does not exactly match operation tools")
    return CAMValidationResult(not errors, tuple(sorted(set(errors))))


def require_valid_operations(
    document: OperationsDocument,
    *,
    machine: MachineProfile | None = None,
) -> None:
    result = validate_operations_document(document, machine=machine)
    if not result.valid:
        raise CAMValidationError("; ".join(result.errors))


def build_validation_backplot(
    document: OperationsDocument,
    *,
    machine: MachineProfile | None = None,
) -> ValidationBackplot:
    """Build a path containing safe-Z positioning moves and no cutting moves."""

    require_valid_operations(document, machine=machine)
    setup_by_id = {setup.setup_id: setup for setup in document.setups}
    moves: list[ValidationMove] = []
    sequence = 1
    for operation in document.operations:
        setup = setup_by_id[operation.setup_id]
        moves.append(
            ValidationMove(
                sequence,
                operation.setup_id,
                operation.operation_id,
                MoveKind.RETRACT,
                None,
                None,
                setup.safe_z_um,
            )
        )
        sequence += 1
        for x_um, y_um in _validation_xy_points(operation):
            moves.append(
                ValidationMove(
                    sequence,
                    operation.setup_id,
                    operation.operation_id,
                    MoveKind.RAPID_XY,
                    x_um,
                    y_um,
                    setup.safe_z_um,
                )
            )
            sequence += 1
    return ValidationBackplot(
        mode="VALIDATION_DRY_RUN",
        moves=tuple(moves),
        omitted_cutting_operation_ids=tuple(
            operation.operation_id for operation in document.operations
        ),
    )


def theoretical_removal_envelopes(
    document: OperationsDocument,
    *,
    machine: MachineProfile | None = None,
) -> tuple[RemovalEnvelope, ...]:
    """Return conservative removal volumes for validation, not physical simulation."""

    require_valid_operations(document, machine=machine)
    envelopes: list[RemovalEnvelope] = []
    for operation in document.operations:
        if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
            radius = (operation.diameter_um or 0) // 2
            x_min, y_min = operation.x_um - radius, operation.y_um - radius
            x_max, y_max = operation.x_um + radius, operation.y_um + radius
        else:
            x_min, y_min = operation.x_um, operation.y_um
            x_max = operation.x_um + (operation.width_um or 0)
            y_max = operation.y_um + (operation.length_um or 0)
        envelopes.append(
            RemovalEnvelope(
                operation.operation_id,
                operation.setup_id,
                x_min,
                y_min,
                x_max,
                y_max,
                -operation.depth_um,
                0,
                True,
            )
        )
    return tuple(envelopes)


def backplot_svg(
    document: OperationsDocument,
    *,
    machine: MachineProfile | None = None,
) -> bytes:
    backplot = build_validation_backplot(document, machine=machine)
    groups: list[str] = []
    y_offset_um = 0
    for setup in document.setups:
        current = [
            move
            for move in backplot.moves
            if move.setup_id == setup.setup_id and move.x_um is not None
        ]
        circles = "".join(
            f'<circle cx="{um_to_mm(move.x_um or 0)}" '
            f'cy="{um_to_mm((move.y_um or 0) + y_offset_um)}" r="2"/>'
            for move in current
        )
        groups.append(
            f'<g data-setup-id="{setup.setup_id}"><rect x="0" y="{um_to_mm(y_offset_um)}" '
            f'width="{um_to_mm(setup.stock_width_um)}" height="{um_to_mm(setup.stock_height_um)}"/>'
            f"{circles}</g>"
        )
        y_offset_um += setup.stock_height_um + 20_000
    width_um = max((setup.stock_width_um for setup in document.setups), default=100_000)
    height_um = max(y_offset_um, 100_000)
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {um_to_mm(width_um)} {um_to_mm(height_um)}" '
        'data-mode="VALIDATION_DRY_RUN"><style>rect{fill:none;stroke:#111;stroke-width:1}'
        "circle{fill:#0ea5e9;stroke:none}</style>"
        f"{''.join(groups)}</svg>"
    )
    return svg.encode("utf-8")


def parse_operations_json(payload: bytes | str) -> dict[str, Any]:
    """Strictly parse the machine-neutral JSON envelope before model loading."""

    try:
        decoded = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CAMValidationError("invalid UTF-8 operations JSON") from exc
    if not isinstance(value, dict):
        raise CAMValidationError("operations document must be a JSON object")
    if value.get("schema_version") != "custombuild.operations.v2":
        raise CAMValidationError("unsupported operations schema_version")
    if value.get("mode") != "VALIDATION":
        raise CAMValidationError("operations JSON is not validation-only")
    for field in (
        "design_hash",
        "machine_profile_id",
        "machine_profile_version",
        "tool_catalog_version",
        "tool_catalog_fingerprint",
        "tools",
        "setups",
        "operations",
    ):
        if field not in value:
            raise CAMValidationError(f"operations JSON missing {field}")
    if (
        not isinstance(value["tools"], list)
        or not isinstance(value["setups"], list)
        or not isinstance(value["operations"], list)
    ):
        raise CAMValidationError("tools, setups and operations must be arrays")
    return value


def _resolve_reference_machine(profile_id: str) -> MachineProfile | None:
    factories = {
        REFERENCE_MACHINE_PROFILE_ID: linuxcnc_reference_router_1325,
        LARGE_FORMAT_MACHINE_PROFILE_ID: linuxcnc_reference_router_5125,
    }
    factory = factories.get(profile_id)
    return factory() if factory is not None else None


def _validate_setup(
    setup: Setup,
    *,
    machine: MachineProfile | None,
) -> list[str]:
    errors: list[str] = []
    if not _is_canonical_id(setup.setup_id) or not _is_canonical_id(setup.stock_id):
        errors.append("setup and stock identity are required")
    if not _is_canonical_id(setup.material_id) or not _is_canonical_id(setup.material_version):
        errors.append(f"setup material identity is required: {setup.setup_id}")
    if setup.sheet_index < 0:
        errors.append(f"negative setup sheet index: {setup.setup_id}")
    expected_setup_id = f"setup:{setup.stock_id}:{setup.sheet_index + 1:03d}:{setup.side.value}"
    if setup.setup_id != expected_setup_id:
        errors.append(f"setup identity is not traceable to stock, sheet and side: {setup.setup_id}")
    if min(setup.stock_width_um, setup.stock_height_um, setup.stock_thickness_um) <= 0:
        errors.append(f"non-positive setup stock dimensions: {setup.setup_id}")
    if setup.safe_z_um <= 0:
        errors.append(f"non-positive setup safe Z: {setup.setup_id}")
    if setup.origin.x_um != 0 or setup.origin.y_um != 0:
        errors.append(f"setup origin must match the stock-frame origin: {setup.setup_id}")
    if _WCS_PATTERN.fullmatch(setup.wcs) is None:
        errors.append(f"unsupported setup WCS: {setup.setup_id}")
    if setup.side.value == "EDGE":
        errors.append(f"edge setup is unsupported by the 3-axis CAM boundary: {setup.setup_id}")
    if len(set(setup.tool_ids)) != len(setup.tool_ids):
        errors.append(f"duplicate tool ID in setup: {setup.setup_id}")
    if any(not _is_canonical_id(tool_id) for tool_id in setup.tool_ids):
        errors.append(f"setup contains a non-canonical tool ID: {setup.setup_id}")
    if setup.reference_surface != _EXTERNAL_REFERENCE_SURFACE:
        errors.append(f"setup has an unsupported reference surface: {setup.setup_id}")
    if setup.fixture != _EXTERNAL_FIXTURE:
        errors.append(f"setup has an unsupported or blank fixture declaration: {setup.setup_id}")
    expected_orientation = _SIDE_B_ORIENTATION if setup.side.value == "B" else _SIDE_A_ORIENTATION
    if setup.orientation != expected_orientation:
        errors.append(f"setup orientation does not match its side: {setup.setup_id}")
    errors.extend(_validate_probe_method(setup))
    if not setup.operator_steps or any(
        not isinstance(step, str)
        or not step
        or step != step.strip()
        or len(step) > 500
        or any(ord(character) < 32 for character in step)
        for step in setup.operator_steps
    ):
        errors.append(f"setup operator steps are blank or unsafe: {setup.setup_id}")
    for zone in setup.keep_out_zones:
        if zone.width_um <= 0 or zone.height_um <= 0:
            errors.append(f"invalid keep-out zone in setup: {setup.setup_id}")

    if machine is None:
        return errors
    if setup.stock_width_um > machine.work_width_um:
        errors.append(f"setup stock exceeds machine X travel: {setup.setup_id}")
    if setup.stock_height_um > machine.work_height_um:
        errors.append(f"setup stock exceeds machine Y travel: {setup.setup_id}")
    if setup.stock_thickness_um + setup.safe_z_um > machine.work_z_um:
        errors.append(f"setup stock and safe Z exceed machine Z travel: {setup.setup_id}")
    if setup.safe_z_um != machine.safe_z_um:
        errors.append(f"setup safe Z does not match machine profile: {setup.setup_id}")
    if setup.wcs not in machine.wcs_codes:
        errors.append(f"setup WCS is absent from machine profile: {setup.setup_id}")
    if setup.side not in machine.supported_sides and not (
        setup.side.value == "B" and machine.can_flip_stock
    ):
        errors.append(f"setup side is unsupported by machine profile: {setup.setup_id}")
    if any(zone not in setup.keep_out_zones for zone in machine.keep_out_zones):
        errors.append(f"setup omits a machine keep-out zone: {setup.setup_id}")
    return errors


def _validate_setup_stock_bindings(setups: tuple[Setup, ...]) -> list[str]:
    errors: list[str] = []
    stock_binding_by_sheet: dict[tuple[str, int], tuple[object, ...]] = {}
    side_by_sheet: dict[tuple[str, int], set[object]] = {}
    for setup in setups:
        key = (setup.stock_id, setup.sheet_index)
        binding = (
            setup.material_id,
            setup.material_version,
            setup.stock_width_um,
            setup.stock_height_um,
            setup.stock_thickness_um,
        )
        previous = stock_binding_by_sheet.setdefault(key, binding)
        if previous != binding:
            errors.append(
                f"setups disagree on stock/material binding: {setup.stock_id}:{setup.sheet_index}"
            )
        sides = side_by_sheet.setdefault(key, set())
        if setup.side in sides:
            errors.append(f"duplicate side setup for stock sheet: {setup.setup_id}")
        sides.add(setup.side)
    return errors


def _validate_machine_binding(
    document: OperationsDocument,
    machine: MachineProfile,
) -> list[str]:
    errors: list[str] = []
    if document.machine_profile_id != machine.profile_id:
        errors.append("operations document machine profile ID mismatch")
    if document.machine_profile_version != machine.version:
        errors.append("operations document machine profile version mismatch")
    if document.tool_catalog_version != machine.tool_library_version:
        errors.append("selected-tool snapshot catalogue version differs from machine profile")

    machine_tools = {tool.tool_id: tool for tool in machine.tools}
    if len(machine_tools) != len(machine.tools):
        errors.append("machine profile contains duplicate tool IDs")
    for tool in document.tools:
        machine_tool = machine_tools.get(tool.tool_id)
        if machine_tool is None:
            errors.append(f"selected tool is absent from machine profile: {tool.tool_id}")
        elif machine_tool != tool:
            errors.append(f"selected tool differs from machine profile: {tool.tool_id}")
        if tool.spindle_rpm > machine.max_spindle_rpm:
            errors.append(f"selected tool exceeds machine spindle limit: {tool.tool_id}")
    return errors


def _validate_tool_snapshot(tool: ToolSpec) -> list[str]:
    errors: list[str] = []
    if not _is_canonical_id(tool.tool_id):
        errors.append("selected tool has a blank or non-canonical ID")
    if not isinstance(tool.name, str) or not tool.name or tool.name != tool.name.strip():
        errors.append(f"selected tool has a blank name: {tool.tool_id}")
    if not _is_canonical_id(tool.version):
        errors.append(f"selected tool has a blank or non-canonical version: {tool.tool_id}")
    if (
        type(tool.diameter_um) is not int
        or type(tool.cutting_length_um) is not int
        or type(tool.spindle_rpm) is not int
        or type(tool.feed_um_min) is not int
        or type(tool.plunge_um_min) is not int
        or min(
            tool.diameter_um,
            tool.cutting_length_um,
            tool.spindle_rpm,
            tool.feed_um_min,
            tool.plunge_um_min,
        )
        <= 0
    ):
        errors.append(f"selected tool has invalid dimensions or cutting parameters: {tool.tool_id}")
    if not tool.supported_operations or len(set(tool.supported_operations)) != len(
        tool.supported_operations
    ):
        errors.append(f"selected tool operations are empty or duplicated: {tool.tool_id}")
    if tool.measured_diameter_um is not None and (
        type(tool.measured_diameter_um) is not int or tool.measured_diameter_um <= 0
    ):
        errors.append(f"selected tool has an invalid measured diameter: {tool.tool_id}")
    if type(tool.runout_um) is not int or tool.runout_um < 0:
        errors.append(f"selected tool has invalid runout: {tool.tool_id}")
    elif 2 * tool.runout_um >= tool.effective_diameter_um:
        errors.append(f"selected tool runout reaches the cutter radius: {tool.tool_id}")
    return errors


def _validate_probe_method(setup: Setup) -> list[str]:
    if setup.probe_method == _EXTERNAL_PROBE:
        return []
    if not isinstance(setup.probe_method, str):
        return [f"setup probe method is unsupported or blank: {setup.setup_id}"]
    match = _DECLARED_PROBE_PATTERN.fullmatch(setup.probe_method)
    if match is None:
        return [f"setup probe method is unsupported or blank: {setup.setup_id}"]
    coordinates = tuple(
        tuple(int(value) for value in point.split(",")) for point in match.group(2).split("|")
    )
    if len(coordinates) < 2 or len(set(coordinates)) != len(coordinates):
        return [f"setup probe coordinates are not unique: {setup.setup_id}"]
    if any(
        x_um < 0 or y_um < 0 or x_um > setup.stock_width_um or y_um > setup.stock_height_um
        for x_um, y_um in coordinates
    ):
        return [f"setup probe coordinates are outside stock: {setup.setup_id}"]
    return []


def _is_canonical_id(value: object) -> bool:
    return isinstance(value, str) and _CANONICAL_ID_PATTERN.fullmatch(value) is not None


def _validate_operation(
    operation: CAMOperation,
    setup: Setup,
    *,
    tool: ToolSpec | None,
    machine: MachineProfile | None,
    machine_tool: ToolSpec | None,
) -> list[str]:
    errors: list[str] = []
    identities = {
        "operation_id": operation.operation_id,
        "setup_id": operation.setup_id,
        "part_id": operation.part_id,
        "instance_id": operation.instance_id,
        "feature_id": operation.feature_id,
        "tool_id": operation.tool_id,
    }
    for label, value in identities.items():
        if not _is_canonical_id(value):
            errors.append(f"operation has a blank or non-canonical {label}")
    expected_operation_prefix = f"op:{operation.instance_id}:{operation.feature_id}"
    operation_suffix = operation.operation_id.removeprefix(expected_operation_prefix)
    if operation.operation_id == expected_operation_prefix:
        pass
    elif not re.fullmatch(r":[0-9]{3}", operation_suffix):
        errors.append(
            f"operation identity is not traceable to instance and feature: {operation.operation_id}"
        )
    if operation.side != setup.side:
        errors.append(f"operation/setup side mismatch: {operation.operation_id}")
    if operation.tool_id not in setup.tool_ids:
        errors.append(f"operation tool absent from setup: {operation.operation_id}")
    if operation.depth_um <= 0:
        errors.append(f"non-positive operation depth: {operation.operation_id}")
    elif operation.through:
        if operation.depth_um < setup.stock_thickness_um:
            errors.append(f"through operation does not reach stock depth: {operation.operation_id}")
        if operation.depth_um > setup.stock_thickness_um + _DEFAULT_THROUGH_OVERTRAVEL_UM:
            errors.append(
                f"through operation exceeds spoilboard allowance: {operation.operation_id}"
            )
    elif operation.depth_um >= setup.stock_thickness_um:
        errors.append(f"non-through operation reaches stock depth: {operation.operation_id}")
    if operation.x_um < 0 or operation.y_um < 0:
        errors.append(f"negative XY coordinate: {operation.operation_id}")
    if operation.stepdown_um is None or operation.stepdown_um <= 0:
        errors.append(f"operation requires a positive stepdown: {operation.operation_id}")
    elif operation.stepdown_um > operation.depth_um:
        errors.append(f"operation stepdown exceeds depth: {operation.operation_id}")
    if operation.stepover_ppm is not None and not 0 < operation.stepover_ppm <= 1_000_000:
        errors.append(f"operation stepover is outside (0, 1]: {operation.operation_id}")
    if operation.tolerance_um < 0 or operation.fit_clearance_um < 0:
        errors.append(f"operation tolerance or clearance is negative: {operation.operation_id}")
    if operation.compensation is not None and operation.compensation not in _ALLOWED_COMPENSATION:
        errors.append(f"operation compensation is unsupported: {operation.operation_id}")
    if operation.kind is not OperationKind.CONTOUR and operation.compensation is not None:
        errors.append(f"non-contour operation declares compensation: {operation.operation_id}")
    if operation.through and operation.kind is OperationKind.CONTOUR:
        if operation.compensation not in {"INSIDE", "OUTSIDE"}:
            errors.append(
                f"through contour requires explicit inside/outside compensation: "
                f"{operation.operation_id}"
            )
        if operation.holding_strategy != _RELEASE_HOLDING_STRATEGY:
            errors.append(
                f"through contour requires an explicit release holding strategy: "
                f"{operation.operation_id}"
            )
    elif operation.holding_strategy is not None:
        errors.append(f"operation holding strategy is unsupported: {operation.operation_id}")
    if operation.corner_strategy is not None and operation.corner_strategy not in (
        _ALLOWED_CORNER_STRATEGIES
    ):
        errors.append(f"operation corner strategy is unsupported: {operation.operation_id}")
    if operation.corner_relief_radius_um is not None and operation.corner_relief_radius_um <= 0:
        errors.append(f"operation corner relief radius is invalid: {operation.operation_id}")
    if (
        operation.corner_strategy in _ALLOWED_CORNER_STRATEGIES
        and operation.corner_relief_radius_um is None
    ):
        errors.append(
            f"operation corner strategy requires a relief radius: {operation.operation_id}"
        )
    if (
        operation.corner_relief_radius_um is not None
        and operation.corner_strategy not in _ALLOWED_CORNER_STRATEGIES
    ):
        errors.append(
            f"operation corner relief radius requires a supported strategy: "
            f"{operation.operation_id}"
        )
    if (
        len(set(operation.open_end_reliefs)) != len(operation.open_end_reliefs)
        or not set(operation.open_end_reliefs) <= _ALLOWED_OPEN_END_RELIEFS
        or (
            operation.open_end_reliefs
            and operation.corner_strategy not in _ALLOWED_CORNER_STRATEGIES
        )
    ):
        errors.append(f"operation open-end relief declaration is invalid: {operation.operation_id}")

    if tool is not None:
        if operation.kind not in tool.supported_operations:
            errors.append(f"operation is unsupported by selected tool: {operation.operation_id}")
        if tool.cutting_length_um < operation.depth_um:
            errors.append(
                f"operation exceeds selected-tool cutting length: {operation.operation_id}"
            )
        if operation.stepdown_um is not None and operation.stepdown_um > tool.effective_diameter_um:
            errors.append(f"operation stepdown exceeds cutter diameter: {operation.operation_id}")
        if (
            operation.corner_strategy in _ALLOWED_CORNER_STRATEGIES
            and operation.corner_relief_radius_um is not None
            and 2 * operation.corner_relief_radius_um < tool.effective_diameter_um
        ):
            errors.append(
                f"operation corner relief is smaller than selected tool: {operation.operation_id}"
            )
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK} and (
        operation.corner_strategy is not None
        or operation.corner_relief_radius_um is not None
        or operation.open_end_reliefs
    ):
        errors.append(f"drilling operation declares area-corner relief: {operation.operation_id}")
    if machine is not None:
        if operation.kind not in machine.supported_operations:
            errors.append(f"operation is unsupported by machine profile: {operation.operation_id}")
        if machine_tool is None:
            errors.append(
                f"operation tool is absent from machine profile: {operation.operation_id}"
            )
        if setup.safe_z_um + operation.depth_um > machine.work_z_um:
            errors.append(f"operation exceeds machine Z travel: {operation.operation_id}")

    operation_envelope: Rect | None = None
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        effective_diameter_um = max(
            operation.diameter_um or 0,
            tool.effective_diameter_um if tool is not None else 0,
        )
        radius = effective_diameter_um // 2
        drill_envelope = Rect(
            operation.x_um - radius,
            operation.y_um - radius,
            effective_diameter_um,
            effective_diameter_um,
        )
        if operation.diameter_um is None or operation.diameter_um <= 0:
            errors.append(f"drilling operation missing diameter: {operation.operation_id}")
        elif tool is not None:
            diameter_tolerance_um = machine.accuracy_um if machine is not None else 0
            if abs(tool.effective_diameter_um - operation.diameter_um) > diameter_tolerance_um:
                errors.append(
                    f"drilling diameter differs from selected tool: {operation.operation_id}"
                )
        stock_envelope = Rect(0, 0, setup.stock_width_um, setup.stock_height_um)
        if effective_diameter_um <= 0 or not stock_envelope.contains(drill_envelope):
            errors.append(f"drilling envelope outside stock: {operation.operation_id}")
        if effective_diameter_um > 0:
            operation_envelope = drill_envelope
    else:
        if (
            operation.width_um is None
            or operation.length_um is None
            or operation.width_um <= 0
            or operation.length_um <= 0
        ):
            errors.append(f"area operation missing extent: {operation.operation_id}")
        envelope = (
            operation.cutter_envelope_x_um,
            operation.cutter_envelope_y_um,
            operation.cutter_envelope_width_um,
            operation.cutter_envelope_length_um,
        )
        if any(value is None for value in envelope):
            errors.append(f"area operation missing cutter envelope: {operation.operation_id}")
        else:
            envelope_x, envelope_y, envelope_width, envelope_length = envelope
            assert envelope_x is not None
            assert envelope_y is not None
            assert envelope_width is not None
            assert envelope_length is not None
            cutter_envelope = Rect(
                envelope_x,
                envelope_y,
                envelope_width,
                envelope_length,
            )
            stock_envelope = Rect(0, 0, setup.stock_width_um, setup.stock_height_um)
            if (
                envelope_width <= 0
                or envelope_length <= 0
                or not stock_envelope.contains(cutter_envelope)
            ):
                errors.append(f"operation cutter envelope outside stock: {operation.operation_id}")
            if (
                tool is not None
                and min(envelope_width, envelope_length) < tool.effective_diameter_um
            ):
                errors.append(
                    f"operation cutter envelope is smaller than its tool: {operation.operation_id}"
                )
            if operation.width_um is not None and operation.length_um is not None:
                nominal_envelope = Rect(
                    operation.x_um,
                    operation.y_um,
                    operation.width_um,
                    operation.length_um,
                )
                if not cutter_envelope.contains(nominal_envelope):
                    errors.append(
                        f"cutter envelope does not contain operation: {operation.operation_id}"
                    )
                if (
                    operation.corner_strategy in _ALLOWED_CORNER_STRATEGIES
                    and operation.corner_relief_radius_um is not None
                    and cutter_envelope
                    != _expected_versioned_dogbone_envelope(operation, nominal_envelope)
                ):
                    errors.append(
                        "operation cutter envelope does not match versioned corner semantics: "
                        f"{operation.operation_id}"
                    )
            operation_envelope = cutter_envelope

    if operation_envelope is not None and any(
        operation_envelope.intersects(zone) for zone in setup.keep_out_zones
    ):
        errors.append(f"operation intersects setup keep-out zone: {operation.operation_id}")
    return errors


def _expected_versioned_dogbone_envelope(
    operation: CAMOperation,
    nominal: Rect,
) -> Rect:
    """Derive the cutter AABB from nominal geometry and dogbone contract fields.

    ``open_end_reliefs`` stays in source-part U/V coordinates in an operations
    document.  Map each active source corner through the declared nesting
    rotation and B-side flip before expanding the machine-coordinate envelope.
    """

    strategy = operation.corner_strategy
    radius_um = operation.corner_relief_radius_um
    if strategy not in _ALLOWED_CORNER_STRATEGIES or radius_um is None:
        return nominal
    declared = set(operation.open_end_reliefs)
    left = nominal.x_um
    right = nominal.right_um
    bottom = nominal.y_um
    top = nominal.top_um
    for u_boundary, v_boundary in (
        ("u_min", "v_min"),
        ("u_max", "v_min"),
        ("u_min", "v_max"),
        ("u_max", "v_max"),
    ):
        suppressed = (
            {u_boundary, v_boundary} <= declared
            if strategy == "dogbone-v1"
            else bool({u_boundary, v_boundary} & declared)
        )
        if suppressed:
            continue
        x_boundary, y_boundary = _source_corner_machine_boundaries(
            u_boundary,
            v_boundary,
            rotated_90=operation.source_rotation_90,
            side=operation.side,
        )
        x_um = nominal.x_um if x_boundary == "x_min" else nominal.right_um
        y_um = nominal.y_um if y_boundary == "y_min" else nominal.top_um
        left = min(left, x_um - radius_um)
        right = max(right, x_um + radius_um)
        bottom = min(bottom, y_um - radius_um)
        top = max(top, y_um + radius_um)
    return Rect(left, bottom, right - left, top - bottom)


def _source_corner_machine_boundaries(
    u_boundary: str,
    v_boundary: str,
    *,
    rotated_90: bool,
    side: Side,
) -> tuple[str, str]:
    if rotated_90:
        x_boundary = "x_max" if v_boundary == "v_min" else "x_min"
        y_boundary = "y_min" if u_boundary == "u_min" else "y_max"
    else:
        x_boundary = "x_min" if u_boundary == "u_min" else "x_max"
        y_boundary = "y_min" if v_boundary == "v_min" else "y_max"
    if side == Side.B:
        y_boundary = "y_max" if y_boundary == "y_min" else "y_min"
    return x_boundary, y_boundary


def _validation_xy_points(operation: CAMOperation) -> tuple[tuple[int, int], ...]:
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        return ((operation.x_um, operation.y_um),)
    width = operation.width_um or 0
    length = operation.length_um or 0
    return (
        (operation.x_um, operation.y_um),
        (operation.x_um + width, operation.y_um),
        (operation.x_um + width, operation.y_um + length),
        (operation.x_um, operation.y_um + length),
        (operation.x_um, operation.y_um),
    )
