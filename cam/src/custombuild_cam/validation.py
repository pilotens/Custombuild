"""Machine-neutral CAM validation and safe-Z backplot generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from custombuild_manufacturing.model import (
    CAMOperation,
    OperationKind,
    OperationsDocument,
    Setup,
    canonical_json_bytes,
    sha256_hex,
    um_to_mm,
)

from .model import MoveKind, RemovalEnvelope, ValidationBackplot, ValidationMove

CAM_VALIDATION_VERSION = "cam-validation-1.0.0"
CAM_BACKPLOT_VERSION = "validation-backplot-1.0.0"


class CAMValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CAMValidationResult:
    valid: bool
    errors: tuple[str, ...]


def validate_operations_document(document: OperationsDocument) -> CAMValidationResult:
    errors: list[str] = []
    if document.mode != "VALIDATION":
        errors.append("only VALIDATION operations documents are accepted")
    if not document.design_hash:
        errors.append("design_hash is required")

    tool_by_id = {tool.tool_id: tool for tool in document.tools}
    if len(tool_by_id) != len(document.tools):
        errors.append("duplicate tool_id in selected-tool snapshot")
    expected_tool_fingerprint = sha256_hex(
        canonical_json_bytes(tuple(sorted(document.tools, key=lambda item: item.tool_id)))
    )
    if document.tool_catalog_fingerprint != expected_tool_fingerprint:
        errors.append("selected-tool snapshot fingerprint mismatch")
    if not document.tool_catalog_version or document.tool_catalog_version == "unversioned":
        errors.append("selected-tool snapshot has no catalogue version")

    setup_by_id = {setup.setup_id: setup for setup in document.setups}
    if len(setup_by_id) != len(document.setups):
        errors.append("duplicate setup_id")
    operations_by_setup: dict[str, set[str]] = {setup.setup_id: set() for setup in document.setups}
    operation_tool_ids: set[str] = set()
    operation_ids: set[str] = set()
    for operation in document.operations:
        if operation.operation_id in operation_ids:
            errors.append(f"duplicate operation_id: {operation.operation_id}")
        operation_ids.add(operation.operation_id)
        setup = setup_by_id.get(operation.setup_id)
        if setup is None:
            errors.append(f"operation references unknown setup: {operation.operation_id}")
            continue
        operations_by_setup[setup.setup_id].add(operation.tool_id)
        operation_tool_ids.add(operation.tool_id)
        if operation.tool_id not in tool_by_id:
            errors.append(
                f"operation references tool absent from snapshot: {operation.operation_id}"
            )
        errors.extend(_validate_operation(operation, setup))
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


def require_valid_operations(document: OperationsDocument) -> None:
    result = validate_operations_document(document)
    if not result.valid:
        raise CAMValidationError("; ".join(result.errors))


def build_validation_backplot(document: OperationsDocument) -> ValidationBackplot:
    """Build a path containing safe-Z positioning moves and no cutting moves."""

    require_valid_operations(document)
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


def theoretical_removal_envelopes(document: OperationsDocument) -> tuple[RemovalEnvelope, ...]:
    """Return conservative removal volumes for validation, not physical simulation."""

    require_valid_operations(document)
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


def backplot_svg(document: OperationsDocument) -> bytes:
    backplot = build_validation_backplot(document)
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
    if value.get("schema_version") != "custombuild.operations.v1":
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


def _validate_operation(operation: CAMOperation, setup: Setup) -> list[str]:
    errors: list[str] = []
    if operation.side != setup.side:
        errors.append(f"operation/setup side mismatch: {operation.operation_id}")
    if operation.tool_id not in setup.tool_ids:
        errors.append(f"operation tool absent from setup: {operation.operation_id}")
    if operation.depth_um <= 0:
        errors.append(f"non-positive operation depth: {operation.operation_id}")
    if operation.x_um < 0 or operation.y_um < 0:
        errors.append(f"negative XY coordinate: {operation.operation_id}")
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        radius = (operation.diameter_um or 0) // 2
        if operation.diameter_um is None or operation.diameter_um <= 0:
            errors.append(f"drilling operation missing diameter: {operation.operation_id}")
        if operation.x_um - radius < 0 or operation.y_um - radius < 0:
            errors.append(f"drilling envelope outside stock: {operation.operation_id}")
        if (
            operation.x_um + radius > setup.stock_width_um
            or operation.y_um + radius > setup.stock_height_um
        ):
            errors.append(f"drilling envelope outside stock: {operation.operation_id}")
    else:
        if not operation.width_um or not operation.length_um:
            errors.append(f"area operation missing extent: {operation.operation_id}")
        elif (
            operation.x_um + operation.width_um > setup.stock_width_um
            or operation.y_um + operation.length_um > setup.stock_height_um
        ):
            errors.append(f"operation envelope outside stock: {operation.operation_id}")
    return errors


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
