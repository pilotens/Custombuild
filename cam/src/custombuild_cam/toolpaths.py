"""Deterministic 2.5D cutting-path generation for the supported shelving slice.

The generator consumes the existing machine-neutral ``OperationsDocument`` and
an independently accepted :class:`ProductionExecutionContext`.  It never
changes or upgrades the validation document in place.  Unsupported or
ambiguous geometry fails closed before a move is returned.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from math import isqrt
from typing import TypeVar

from custombuild_manufacturing.model import (
    CAMOperation,
    OperationKind,
    OperationsDocument,
    Rect,
    Setup,
    Side,
    ToolSpec,
    canonical_json_bytes,
    sha256_hex,
)
from custombuild_manufacturing.profiles import (
    LARGE_FORMAT_MACHINE_PROFILE_ID,
    REFERENCE_MACHINE_PROFILE_ID,
    linuxcnc_reference_router_1325,
    linuxcnc_reference_router_5125,
)

from .production_model import (
    MAX_THROUGH_OVERTRAVEL_UM,
    BoundSetup,
    CuttingRecipe,
    ProductionCAMError,
    ProductionExecutionContext,
    ProductionMove,
    ProductionMoveKind,
    ProductionMoveRole,
    ProductionProgram,
    ProductionToolBinding,
    ProductionToolGeometry,
    ProductionToolpathDocument,
)
from .validation import CAMValidationError, require_valid_operations

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_OPERATIONS_SCHEMA_VERSION = "custombuild.operations.v2"
_SUPPORTED_KINDS = frozenset(
    {
        OperationKind.DRILL,
        OperationKind.POCKET,
        OperationKind.GROOVE,
        OperationKind.CONTOUR,
    }
)
_AREA_KINDS = frozenset({OperationKind.POCKET, OperationKind.GROOVE})
_DOGBONE_STRATEGIES = frozenset({"dogbone-v1", "dogbone-v2"})
_T = TypeVar("_T")

# sin/cos values for 11.25-degree increments, in parts per million.  A fixed
# integer table avoids platform-dependent floating-point output while keeping
# outside-contour chord error far below normal woodworking machine accuracy.
_QUARTER_CIRCLE_PPM = (
    (1_000_000, 0),
    (980_785, 195_090),
    (923_880, 382_683),
    (831_470, 555_570),
    (707_107, 707_107),
    (555_570, 831_470),
    (382_683, 923_880),
    (195_090, 980_785),
    (0, 1_000_000),
)


@dataclass(frozen=True, slots=True)
class _PartOutline:
    """One finished part outline expressed in the unflipped physical stock frame."""

    stock_id: str
    sheet_index: int
    instance_id: str
    part_id: str
    source_rotation_90: bool
    rect: Rect


def depth_levels_um(depth_um: int, stepdown_um: int) -> tuple[int, ...]:
    """Return dense, deterministic negative-Z depth levels ending exactly at depth."""

    if type(depth_um) is not int or depth_um <= 0:
        raise ProductionCAMError("depth_um must be a positive integer")
    if type(stepdown_um) is not int or stepdown_um <= 0:
        raise ProductionCAMError("stepdown_um must be a positive integer")
    return tuple(
        -min(level, depth_um) for level in range(stepdown_um, depth_um + stepdown_um, stepdown_um)
    )


@dataclass(slots=True)
class _ProgramBuilder:
    context: ProductionExecutionContext
    setup: BoundSetup
    tool: ProductionToolBinding
    part_outlines: tuple[_PartOutline, ...]
    moves: list[ProductionMove] = field(default_factory=list)

    def add(
        self,
        operation: CAMOperation,
        pass_index: int,
        kind: ProductionMoveKind,
        role: ProductionMoveRole,
        x_um: int,
        y_um: int,
        z_um: int,
        feed_um_min: int | None = None,
    ) -> None:
        move = ProductionMove(
            sequence=len(self.moves) + 1,
            operation_id=operation.operation_id,
            pass_index=pass_index,
            kind=kind,
            role=role,
            x_um=x_um,
            y_um=y_um,
            z_um=z_um,
            feed_um_min=feed_um_min,
        )
        _require_tool_length_compensated_endpoint_within_machine(
            self.context,
            self.setup,
            self.tool,
            move,
        )
        self.moves.append(move)

    def position(
        self,
        operation: CAMOperation,
        pass_index: int,
        x_um: int,
        y_um: int,
        approach_z_um: int,
    ) -> None:
        _require_point_in_stock(self.setup, x_um, y_um, operation.operation_id)
        self.add(
            operation,
            pass_index,
            ProductionMoveKind.RAPID,
            ProductionMoveRole.POSITION,
            x_um,
            y_um,
            self.setup.safe_z_um,
        )
        self.add(
            operation,
            pass_index,
            ProductionMoveKind.RAPID,
            ProductionMoveRole.APPROACH,
            x_um,
            y_um,
            approach_z_um,
        )

    def cut(
        self,
        operation: CAMOperation,
        pass_index: int,
        x_um: int,
        y_um: int,
        z_um: int,
        feed_um_min: int,
        process_accuracy_um: int,
        *,
        role: ProductionMoveRole = ProductionMoveRole.CUT,
    ) -> None:
        previous = self.moves[-1]
        radius_um = self.tool.effective_diameter_um // 2
        _require_cut_segment_clear(
            self.setup,
            previous.x_um,
            previous.y_um,
            x_um,
            y_um,
            radius_um,
            self.tool.assembly_collision_radius_um,
            process_accuracy_um,
            operation,
            self.part_outlines,
        )
        self.add(
            operation,
            pass_index,
            ProductionMoveKind.LINEAR,
            role,
            x_um,
            y_um,
            z_um,
            feed_um_min,
        )

    def retract(
        self,
        operation: CAMOperation,
        pass_index: int,
        x_um: int,
        y_um: int,
        *,
        role: ProductionMoveRole = ProductionMoveRole.RETRACT,
    ) -> None:
        self.add(
            operation,
            pass_index,
            ProductionMoveKind.RAPID,
            role,
            x_um,
            y_um,
            self.setup.safe_z_um,
        )


def generate_production_toolpaths(
    document: OperationsDocument,
    context: ProductionExecutionContext,
) -> ProductionToolpathDocument:
    """Generate a deterministic cutting candidate or raise ``ProductionCAMError``.

    Physical cutting is never authorized by this function.  The result embeds
    the exact execution context and remains subject to workshop acceptance and
    postprocessor-specific validation.
    """

    setup_by_id, bound_setup_by_id, tool_by_id, binding_by_source_id = _validate_source_binding(
        document, context
    )
    unsupported = next(
        (operation for operation in document.operations if operation.kind not in _SUPPORTED_KINDS),
        None,
    )
    if unsupported is not None:
        raise ProductionCAMError(f"unsupported production operation: {unsupported.operation_id}")
    part_outlines, outline_by_instance = _bind_part_outlines(document, setup_by_id)
    recipe_by_key = {
        (
            recipe.material_id,
            recipe.material_version,
            recipe.tool_id,
            recipe.tool_version,
            recipe.operation_kind,
        ): recipe
        for recipe in context.recipes
    }

    operation_recipe: dict[str, CuttingRecipe] = {}
    release_operation_ids: set[str] = set()
    for operation in document.operations:
        if operation.kind not in _SUPPORTED_KINDS:
            raise ProductionCAMError(f"unsupported production operation: {operation.operation_id}")
        setup = setup_by_id.get(operation.setup_id)
        bound_setup = bound_setup_by_id.get(operation.setup_id)
        tool = tool_by_id.get(operation.tool_id)
        binding = binding_by_source_id.get(operation.tool_id)
        if setup is None or bound_setup is None:
            raise ProductionCAMError(
                f"operation references an unbound setup: {operation.operation_id}"
            )
        if tool is None or binding is None:
            raise ProductionCAMError(
                f"operation references an unbound tool: {operation.operation_id}"
            )
        _validate_part_outline_binding(
            operation,
            setup,
            outline_by_instance,
        )
        recipe = recipe_by_key.get(
            (
                bound_setup.material_id,
                bound_setup.material_version,
                binding.tool_id,
                binding.tool_version,
                operation.kind,
            )
        )
        if recipe is None:
            raise ProductionCAMError(
                f"operation has no exact material/tool/kind recipe: {operation.operation_id}"
            )
        _validate_operation_contract(operation, bound_setup, tool, binding, recipe)
        operation_recipe[operation.operation_id] = recipe
        if _is_release_contour(operation):
            release_operation_ids.add(operation.operation_id)

    setup_order = {setup.setup_id: index for index, setup in enumerate(document.setups)}
    grouped: dict[tuple[str, str, int, bool], list[CAMOperation]] = defaultdict(list)
    for operation in document.operations:
        recipe = operation_recipe[operation.operation_id]
        grouped[
            (
                operation.setup_id,
                operation.tool_id,
                recipe.spindle_rpm,
                operation.operation_id in release_operation_ids,
            )
        ].append(operation)
    group_keys = sorted(
        grouped,
        key=lambda key: (
            setup_order[key[0]],
            key[3],
            key[1],
            key[2],
        ),
    )
    _require_safe_physical_sheet_order(document, group_keys)

    programs: list[ProductionProgram] = []
    for run_order, (setup_id, source_tool_id, spindle_rpm, release_phase) in enumerate(
        group_keys, start=1
    ):
        operations = tuple(
            sorted(
                grouped[(setup_id, source_tool_id, spindle_rpm, release_phase)],
                key=lambda operation: (
                    operation.kind == OperationKind.CONTOUR,
                    operation.kind.value,
                    operation.operation_id,
                ),
            )
        )
        binding = binding_by_source_id[source_tool_id]
        builder = _ProgramBuilder(
            context,
            bound_setup_by_id[setup_id],
            binding,
            part_outlines,
        )
        for operation in operations:
            _generate_operation_moves(
                builder,
                operation,
                operation_recipe[operation.operation_id],
            )
        release_ids = tuple(
            operation.operation_id
            for operation in operations
            if operation.operation_id in release_operation_ids
        )
        programs.append(
            ProductionProgram(
                program_id=(f"program:{run_order:03d}:{setup_id}:{binding.tool_id}:S{spindle_rpm}"),
                run_order=run_order,
                setup_id=setup_id,
                tool_id=binding.tool_id,
                tool_version=binding.tool_version,
                recipe_ids=tuple(
                    sorted(
                        {
                            operation_recipe[operation.operation_id].recipe_id
                            for operation in operations
                        }
                    )
                ),
                operation_ids=tuple(operation.operation_id for operation in operations),
                release_operation_ids=release_ids,
                moves=tuple(builder.moves),
            )
        )

    return ProductionToolpathDocument(
        design_hash=document.design_hash,
        operations_sha256=sha256_hex(document.to_json()),
        execution_context=context,
        machine_profile_fingerprint=context.machine_profile_fingerprint,
        tool_catalog_fingerprint=context.tool_catalog_fingerprint,
        recipe_catalog_fingerprint=context.recipe_catalog_fingerprint,
        programs=tuple(programs),
    )


def _require_tool_length_compensated_endpoint_within_machine(
    context: ProductionExecutionContext,
    setup: BoundSetup,
    tool: ProductionToolBinding,
    move: ProductionMove,
) -> None:
    """Validate one G43-active WCS endpoint against actual axis limits.

    LinuxCNC resolves an absolute WCS endpoint, with G52/G92 cleared and zero
    WCS rotation, as ``machine = programmed + G5x + tool_offset``.  The setup
    origins are the literal G5x offsets from machine origin; they are never
    pre-adjusted for stickout or tool length.  Fixture and removal checks stay
    in programmed tool-tip/WCS coordinates and therefore do not use this
    G53 controlled-point transform.
    """

    machine_endpoint = (
        setup.machine_wcs_origin.x_um + move.x_um + tool.expected_length_offset_x_um,
        setup.machine_wcs_origin.y_um + move.y_um + tool.expected_length_offset_y_um,
        setup.machine_wcs_z0_um + move.z_um + tool.expected_length_offset_z_um,
    )
    machine_bounds = (
        ("X", context.machine_x_min_um, context.machine_x_max_um),
        ("Y", context.machine_y_min_um, context.machine_y_max_um),
        ("Z", context.machine_z_min_um, context.machine_z_max_um),
    )
    outside_axes = tuple(
        axis
        for endpoint, (axis, minimum, maximum) in zip(
            machine_endpoint,
            machine_bounds,
            strict=True,
        )
        if not minimum <= endpoint <= maximum
    )
    if outside_axes:
        raise ProductionCAMError(
            "tool-length-compensated programmed endpoint exceeds absolute machine "
            f"{'/'.join(outside_axes)} bounds: {move.operation_id}"
        )


def _validate_source_binding(
    document: OperationsDocument,
    context: ProductionExecutionContext,
) -> tuple[
    dict[str, Setup],
    dict[str, BoundSetup],
    dict[str, ToolSpec],
    dict[str, ProductionToolBinding],
]:
    if document.schema_version != _OPERATIONS_SCHEMA_VERSION:
        raise ProductionCAMError("production planning received an unsupported operations schema")
    if document.mode != "VALIDATION":
        raise ProductionCAMError("production planning only accepts the validation source contract")
    if _HASH_PATTERN.fullmatch(document.design_hash) is None:
        raise ProductionCAMError("source design_hash must be a lowercase SHA-256 digest")
    if (
        document.machine_profile_id != context.source_machine_profile_id
        or document.machine_profile_version != context.source_machine_profile_version
    ):
        raise ProductionCAMError("source validation-machine profile binding differs")
    source_machine_factories = {
        REFERENCE_MACHINE_PROFILE_ID: linuxcnc_reference_router_1325,
        LARGE_FORMAT_MACHINE_PROFILE_ID: linuxcnc_reference_router_5125,
    }
    source_machine_factory = source_machine_factories.get(document.machine_profile_id)
    if source_machine_factory is None:
        raise ProductionCAMError("source validation-machine profile is not trusted")
    source_machine = source_machine_factory()
    source_machine_fingerprint = sha256_hex(canonical_json_bytes(source_machine))
    if source_machine_fingerprint != context.source_machine_profile_fingerprint:
        raise ProductionCAMError("source validation-machine profile fingerprint differs")
    try:
        require_valid_operations(document, machine=source_machine)
    except CAMValidationError as exc:
        raise ProductionCAMError(f"source operations document is invalid: {exc}") from exc
    if not document.setups or not document.operations:
        raise ProductionCAMError("source document requires setups and operations")

    setup_by_id = _unique_map("source setup", document.setups, lambda item: item.setup_id)
    bound_setup_by_id = _unique_map("bound setup", context.setups, lambda item: item.setup_id)
    if set(setup_by_id) != set(bound_setup_by_id):
        raise ProductionCAMError("production setup binding must exactly cover source setups")
    for setup_id, setup in setup_by_id.items():
        bound = bound_setup_by_id[setup_id]
        source_geometry_binding = (
            setup.stock_id,
            setup.material_id,
            setup.material_version,
            setup.sheet_index,
            setup.side,
            setup.stock_width_um,
            setup.stock_height_um,
            setup.stock_thickness_um,
        )
        accepted_geometry_binding = (
            bound.stock_id,
            bound.source_material_id,
            bound.source_material_version,
            bound.sheet_index,
            bound.side,
            bound.stock_width_um,
            bound.stock_height_um,
            bound.stock_thickness_um,
        )
        if source_geometry_binding != accepted_geometry_binding:
            raise ProductionCAMError(f"bound setup differs from source geometry: {setup_id}")
        if bound.orientation != setup.orientation:
            raise ProductionCAMError(
                f"production setup orientation differs from source XY transform: {setup_id}"
            )
        if bound.source_setup_sha256 != sha256_hex(canonical_json_bytes(setup)):
            raise ProductionCAMError(f"bound setup source fingerprint mismatch: {setup_id}")
        if not set(setup.keep_out_zones) <= set(bound.keep_out_zones):
            raise ProductionCAMError(f"bound setup omits a source keep-out zone: {setup_id}")
        setup_has_through_cut = any(
            operation.through for operation in document.operations if operation.setup_id == setup_id
        )
        if setup_has_through_cut and bound.through_cut_allowance_um <= 0:
            raise ProductionCAMError(
                f"through-cut setup lacks an exact spoilboard allowance: {setup_id}"
            )
        if not setup_has_through_cut and bound.through_cut_allowance_um != 0:
            raise ProductionCAMError(
                f"setup without through cuts declares a spoilboard allowance: {setup_id}"
            )

    tool_by_id = _unique_map("source tool", document.tools, lambda item: item.tool_id)
    binding_by_source_id = _unique_map(
        "production source-tool binding",
        context.tool_bindings,
        lambda item: item.source_tool_id,
    )
    operation_tool_ids = {operation.tool_id for operation in document.operations}
    if set(tool_by_id) != operation_tool_ids:
        raise ProductionCAMError("source tool snapshot must exactly match operation tools")
    if operation_tool_ids != set(binding_by_source_id):
        raise ProductionCAMError(
            "production source-tool bindings must exactly cover every operation tool"
        )
    for tool_id, tool in tool_by_id.items():
        binding = binding_by_source_id[tool_id]
        if binding.source_tool_version != tool.version:
            raise ProductionCAMError(f"source tool version mismatch: {tool_id}")
        if binding.source_tool_sha256 != sha256_hex(canonical_json_bytes(tool)):
            raise ProductionCAMError(f"production tool source fingerprint mismatch: {tool_id}")
        if binding.effective_diameter_um % 2:
            raise ProductionCAMError(
                f"production CAM v1 requires an even cutter diameter: {tool_id}"
            )

    operation_ids = [operation.operation_id for operation in document.operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise ProductionCAMError("source operation IDs must be unique")
    for setup in document.setups:
        if len(setup.tool_ids) != len(set(setup.tool_ids)):
            raise ProductionCAMError(f"source setup has duplicate tools: {setup.setup_id}")
        actual_tools = {
            operation.tool_id
            for operation in document.operations
            if operation.setup_id == setup.setup_id
        }
        if set(setup.tool_ids) != actual_tools:
            raise ProductionCAMError(f"source setup tool list mismatch: {setup.setup_id}")
    return setup_by_id, bound_setup_by_id, tool_by_id, binding_by_source_id


def _bind_part_outlines(
    document: OperationsDocument,
    setup_by_id: dict[str, Setup],
) -> tuple[tuple[_PartOutline, ...], dict[str, _PartOutline]]:
    """Reconstruct an exact physical-stock outline for every source instance.

    ``OperationsDocument`` deliberately carries no independent nesting-layout
    snapshot.  Production planning must therefore fail closed unless each
    referenced instance has exactly one terminal outside/through contour.  The
    contour is the sole authoritative finished-outline binding for this v1
    contract.  Side-B rectangles are unflipped into the physical Side-A stock
    frame so both setups can be compared without coordinate ambiguity.
    """

    release_by_instance: dict[str, CAMOperation] = {}
    instance_bindings: dict[str, tuple[str, bool, str, int]] = {}
    for operation in document.operations:
        setup = setup_by_id[operation.setup_id]
        physical_sheet = (setup.stock_id, setup.sheet_index)
        binding = (
            operation.part_id,
            operation.source_rotation_90,
            physical_sheet[0],
            physical_sheet[1],
        )
        previous_binding = instance_bindings.setdefault(operation.instance_id, binding)
        if previous_binding != binding:
            raise ProductionCAMError(
                f"source instance changes part, rotation or physical sheet: {operation.instance_id}"
            )
        if not _is_release_contour(operation):
            continue
        if operation.instance_id in release_by_instance:
            raise ProductionCAMError(
                f"source instance has multiple finished outlines: {operation.instance_id}"
            )
        release_by_instance[operation.instance_id] = operation

    if set(release_by_instance) != set(instance_bindings):
        missing = sorted(set(instance_bindings) - set(release_by_instance))
        raise ProductionCAMError(
            "every production instance requires exactly one outside/through "
            f"finished-outline contour; missing: {', '.join(missing)}"
        )

    outlines: list[_PartOutline] = []
    for instance_id in sorted(release_by_instance):
        operation = release_by_instance[instance_id]
        setup = setup_by_id[operation.setup_id]
        if operation.width_um is None or operation.length_um is None:
            raise ProductionCAMError(f"finished outline lacks dimensions: {operation.operation_id}")
        local_rect = Rect(
            operation.x_um,
            operation.y_um,
            operation.width_um,
            operation.length_um,
        )
        outlines.append(
            _PartOutline(
                stock_id=setup.stock_id,
                sheet_index=setup.sheet_index,
                instance_id=operation.instance_id,
                part_id=operation.part_id,
                source_rotation_90=operation.source_rotation_90,
                rect=_rect_to_physical_stock(local_rect, setup),
            )
        )

    canonical = tuple(
        sorted(
            outlines,
            key=lambda item: (item.stock_id, item.sheet_index, item.instance_id),
        )
    )
    for index, first in enumerate(canonical):
        for second in canonical[index + 1 :]:
            if (first.stock_id, first.sheet_index) != (second.stock_id, second.sheet_index):
                continue
            if _rects_touch_or_overlap(first.rect, second.rect):
                raise ProductionCAMError(
                    "finished part outlines touch or overlap on physical sheet: "
                    f"{first.instance_id}, {second.instance_id}"
                )
    return canonical, {outline.instance_id: outline for outline in canonical}


def _validate_part_outline_binding(
    operation: CAMOperation,
    setup: Setup,
    outline_by_instance: dict[str, _PartOutline],
) -> None:
    outline = outline_by_instance[operation.instance_id]
    if (
        operation.part_id != outline.part_id
        or operation.source_rotation_90 != outline.source_rotation_90
        or (setup.stock_id, setup.sheet_index) != (outline.stock_id, outline.sheet_index)
    ):
        raise ProductionCAMError(
            f"operation differs from its exact part-instance outline: {operation.operation_id}"
        )
    nominal = _operation_nominal_footprint(operation)
    physical_nominal = _rect_to_physical_stock(nominal, setup)
    if not outline.rect.contains(physical_nominal):
        raise ProductionCAMError(
            f"operation nominal geometry leaves its part-instance outline: {operation.operation_id}"
        )
    if _is_release_contour(operation) and physical_nominal != outline.rect:
        raise ProductionCAMError(
            f"release contour differs from its part-instance outline: {operation.operation_id}"
        )


def _operation_nominal_footprint(operation: CAMOperation) -> Rect:
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        if operation.diameter_um is None or operation.diameter_um <= 0:
            raise ProductionCAMError(
                f"drilling operation lacks a nominal footprint: {operation.operation_id}"
            )
        radius_um = operation.diameter_um // 2
        return Rect(
            operation.x_um - radius_um,
            operation.y_um - radius_um,
            operation.diameter_um,
            operation.diameter_um,
        )
    if operation.width_um is None or operation.length_um is None:
        raise ProductionCAMError(
            f"area operation lacks a nominal footprint: {operation.operation_id}"
        )
    return Rect(operation.x_um, operation.y_um, operation.width_um, operation.length_um)


def _rect_to_physical_stock(rectangle: Rect, setup: Setup | BoundSetup) -> Rect:
    if setup.side != Side.B:
        return rectangle
    return Rect(
        rectangle.x_um,
        setup.stock_height_um - rectangle.top_um,
        rectangle.width_um,
        rectangle.height_um,
    )


def _point_to_physical_stock(
    x_um: int,
    y_um: int,
    setup: BoundSetup,
) -> tuple[int, int]:
    return (x_um, setup.stock_height_um - y_um) if setup.side == Side.B else (x_um, y_um)


def _require_safe_physical_sheet_order(
    document: OperationsDocument,
    group_keys: list[tuple[str, str, int, bool]],
) -> None:
    setups = {setup.setup_id: setup for setup in document.setups}
    released_sheets: set[tuple[str, int]] = set()
    for setup_id, _source_tool_id, _spindle_rpm, release_phase in group_keys:
        setup = setups[setup_id]
        physical_sheet = (setup.stock_id, setup.sheet_index)
        if physical_sheet in released_sheets:
            raise ProductionCAMError(
                "a program group follows a release contour on physical sheet "
                f"{setup.stock_id}:{setup.sheet_index}"
            )
        if release_phase:
            released_sheets.add(physical_sheet)


def _unique_map(
    label: str,
    values: tuple[_T, ...],
    key: Callable[[_T], str],
) -> dict[str, _T]:
    result: dict[str, _T] = {}
    for value in values:
        identity = key(value)
        if identity in result:
            raise ProductionCAMError(f"duplicate {label}: {identity}")
        result[identity] = value
    return result


def _validate_operation_contract(
    operation: CAMOperation,
    setup: BoundSetup,
    tool: ToolSpec,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
) -> None:
    if operation.kind not in _SUPPORTED_KINDS:
        raise ProductionCAMError(f"unsupported production operation: {operation.operation_id}")
    if type(operation.through) is not bool:
        raise ProductionCAMError(
            f"operation through flag must be boolean: {operation.operation_id}"
        )
    if operation.setup_id != setup.setup_id or operation.side != setup.side:
        raise ProductionCAMError(f"operation/setup binding mismatch: {operation.operation_id}")
    if operation.tool_id != tool.tool_id or operation.tool_id != binding.source_tool_id:
        raise ProductionCAMError(f"operation/tool binding mismatch: {operation.operation_id}")
    if operation.kind not in tool.supported_operations:
        raise ProductionCAMError(
            f"source tool does not support operation: {operation.operation_id}"
        )
    if recipe.operation_kind != operation.kind:
        raise ProductionCAMError(f"recipe operation mismatch: {operation.operation_id}")
    if recipe.tool_id != binding.tool_id or recipe.tool_version != binding.tool_version:
        raise ProductionCAMError(f"recipe production-tool mismatch: {operation.operation_id}")
    if recipe.approach_clearance_um >= setup.safe_z_um:
        raise ProductionCAMError(
            f"recipe approach clearance reaches safe Z: {operation.operation_id}"
        )
    if recipe.process_accuracy_um >= setup.minimum_rapid_clearance_um:
        raise ProductionCAMError(
            f"recipe uncertainty exhausts minimum rapid clearance: {operation.operation_id}"
        )
    if operation.tolerance_um < 0 or operation.fit_clearance_um < 0:
        raise ProductionCAMError(f"negative tolerance or fit clearance: {operation.operation_id}")
    if operation.tolerance_um and recipe.accepted_tolerance_um != operation.tolerance_um:
        raise ProductionCAMError(
            f"recipe does not accept the operation tolerance exactly: {operation.operation_id}"
        )
    if operation.fit_clearance_um:
        uncertainty_budget_um = 2 * (recipe.accepted_tolerance_um + recipe.process_accuracy_um)
        if uncertainty_budget_um >= operation.fit_clearance_um:
            raise ProductionCAMError(
                f"production fit-clearance budget is exhausted: {operation.operation_id}"
            )
    target_depth_um = _target_depth_um(operation, setup, recipe)
    uncertainty_depth_um = target_depth_um + recipe.process_accuracy_um
    if uncertainty_depth_um > binding.cutting_length_um:
        raise ProductionCAMError(f"operation exceeds tool cutting length: {operation.operation_id}")
    if uncertainty_depth_um + binding.minimum_holder_clearance_um > binding.measured_stickout_um:
        raise ProductionCAMError(
            f"operation violates minimum tool-holder clearance: {operation.operation_id}"
        )
    if operation.through and (recipe.through_overtravel_um <= recipe.process_accuracy_um):
        raise ProductionCAMError(
            "through-cut overtravel must exceed worst-case process uncertainty: "
            f"{operation.operation_id}"
        )
    if operation.through and (
        recipe.through_overtravel_um + recipe.process_accuracy_um > setup.through_cut_allowance_um
    ):
        raise ProductionCAMError(
            "recipe overtravel and uncertainty exceed verified spoilboard allowance: "
            f"{operation.operation_id}"
        )
    if not operation.through and uncertainty_depth_um >= setup.stock_thickness_um:
        raise ProductionCAMError(
            f"non-through operation uncertainty reaches stock bottom: {operation.operation_id}"
        )

    if operation.kind == OperationKind.COUNTERSINK:
        raise ProductionCAMError("production countersink requires an exact tip/profile depth model")
    if operation.kind == OperationKind.DRILL:
        _validate_drill_contract(operation, setup, binding, recipe)
    elif operation.kind in _AREA_KINDS:
        _validate_area_contract(operation, setup, binding, recipe)
    else:
        _validate_contour_contract(operation, setup, binding, recipe)


def _validate_drill_contract(
    operation: CAMOperation,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
) -> None:
    if operation.through:
        raise ProductionCAMError(
            "through drill requires an exact point-geometry and full-diameter "
            f"breakthrough model: {operation.operation_id}"
        )
    if operation.diameter_um is None:
        raise ProductionCAMError(f"drill diameter/tool mismatch: {operation.operation_id}")
    diameter_deviation_um = abs(operation.diameter_um - binding.effective_diameter_um)
    if diameter_deviation_um > recipe.diameter_tolerance_um:
        raise ProductionCAMError(f"drill diameter/tool mismatch: {operation.operation_id}")
    if diameter_deviation_um + recipe.process_accuracy_um > recipe.accepted_tolerance_um:
        raise ProductionCAMError(
            "drill diameter/tool mismatch plus process uncertainty exceeds accepted "
            f"tolerance: {operation.operation_id}"
        )
    if any(value is not None for value in (operation.width_um, operation.length_um)):
        raise ProductionCAMError(
            f"drill operation contains area geometry: {operation.operation_id}"
        )
    if any(
        value is not None
        for value in (
            operation.cutter_envelope_x_um,
            operation.cutter_envelope_y_um,
            operation.cutter_envelope_width_um,
            operation.cutter_envelope_length_um,
        )
    ):
        raise ProductionCAMError(
            f"drill operation contains an area cutter envelope: {operation.operation_id}"
        )
    if operation.compensation is not None or _has_corner_contract(operation):
        raise ProductionCAMError(
            f"drill operation contains unsupported geometry: {operation.operation_id}"
        )
    if (
        binding.geometry != ProductionToolGeometry.DRILL
        or not binding.center_cutting
        or type(binding.drill_point_length_um) is not int
        or binding.drill_point_length_um != 0
    ):
        raise ProductionCAMError(
            "drill operation requires a center-cutting, zero-point-length production "
            f"drill: {operation.operation_id}"
        )
    radius_um = binding.effective_diameter_um // 2
    _require_footprint_clear(
        setup,
        Rect(
            operation.x_um - radius_um,
            operation.y_um - radius_um,
            binding.effective_diameter_um,
            binding.effective_diameter_um,
        ),
        operation.operation_id,
    )


def _validate_area_contract(
    operation: CAMOperation,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
) -> None:
    if binding.geometry != ProductionToolGeometry.FLAT_END_MILL or not binding.center_cutting:
        raise ProductionCAMError(
            f"area operation requires a center-cutting end mill: {operation.operation_id}"
        )
    if operation.width_um is None or operation.length_um is None:
        raise ProductionCAMError(
            f"area operation requires rectangular dimensions: {operation.operation_id}"
        )
    if operation.diameter_um is not None:
        raise ProductionCAMError(
            f"diameter-based/circular pocket shape is unsupported: {operation.operation_id}"
        )
    if min(operation.width_um, operation.length_um) <= binding.effective_diameter_um:
        raise ProductionCAMError(
            f"area rectangle must exceed cutter diameter: {operation.operation_id}"
        )
    if operation.through or operation.compensation is not None:
        raise ProductionCAMError(
            f"area operation has unsupported through/compensation: {operation.operation_id}"
        )
    stepover_um = binding.effective_diameter_um * recipe.stepover_ppm // 1_000_000
    if stepover_um + 2 * recipe.process_accuracy_um > binding.effective_diameter_um:
        raise ProductionCAMError(
            "recipe stepover exceeds process-accuracy-adjusted cutter coverage: "
            f"{operation.operation_id}"
        )
    nominal = Rect(operation.x_um, operation.y_um, operation.width_um, operation.length_um)
    _require_footprint_clear(setup, nominal, operation.operation_id)
    _validate_corner_contract(operation, binding, nominal)
    expected_envelope = (
        _dogbone_envelope(operation, nominal, binding.effective_diameter_um // 2)
        if _has_corner_contract(operation)
        else nominal
    )
    _require_declared_cutter_envelope(operation, expected_envelope)


def _validate_contour_contract(
    operation: CAMOperation,
    setup: BoundSetup,
    binding: ProductionToolBinding,
    recipe: CuttingRecipe,
) -> None:
    if binding.geometry != ProductionToolGeometry.FLAT_END_MILL or not binding.center_cutting:
        raise ProductionCAMError(
            f"contour requires a center-cutting end mill: {operation.operation_id}"
        )
    if operation.width_um is None or operation.length_um is None:
        raise ProductionCAMError(
            f"contour requires rectangular dimensions: {operation.operation_id}"
        )
    if operation.diameter_um is not None or _has_corner_contract(operation):
        raise ProductionCAMError(
            f"contour contains unsupported shape metadata: {operation.operation_id}"
        )
    if operation.compensation not in {"INSIDE", "OUTSIDE"}:
        raise ProductionCAMError(
            f"contour requires inside/outside compensation: {operation.operation_id}"
        )
    if operation.through and operation.compensation != "OUTSIDE":
        raise ProductionCAMError(
            f"through inner contours lack a slug-holding contract: {operation.operation_id}"
        )
    radius_um = binding.effective_diameter_um // 2
    nominal = Rect(operation.x_um, operation.y_um, operation.width_um, operation.length_um)
    if min(nominal.width_um, nominal.height_um) <= 2 * radius_um:
        raise ProductionCAMError(
            f"contour is too small for cutter compensation: {operation.operation_id}"
        )
    if operation.compensation == "OUTSIDE":
        actual_sweep = Rect(
            nominal.x_um - 2 * radius_um,
            nominal.y_um - 2 * radius_um,
            nominal.width_um + 4 * radius_um,
            nominal.height_um + 4 * radius_um,
        )
    else:
        actual_sweep = nominal
    _require_declared_cutter_envelope(operation, nominal)
    _require_footprint_clear(setup, actual_sweep, operation.operation_id)
    if operation.compensation == "OUTSIDE":
        interpolation_error_um = _outside_contour_interpolation_error_um(radius_um)
        if recipe.process_accuracy_um < interpolation_error_um:
            raise ProductionCAMError(
                "recipe process accuracy omits outside-contour interpolation error: "
                f"{operation.operation_id} requires at least {interpolation_error_um} um"
            )
    if _is_release_contour(operation):
        if recipe.tab_height_um >= setup.stock_thickness_um:
            raise ProductionCAMError(f"tab height reaches stock top: {operation.operation_id}")
        if recipe.tab_height_um <= recipe.process_accuracy_um:
            raise ProductionCAMError(
                f"recipe uncertainty consumes the holding-tab height: {operation.operation_id}"
            )
        if recipe.tab_width_um <= 2 * recipe.process_accuracy_um:
            raise ProductionCAMError(
                f"recipe uncertainty consumes the holding-tab width: {operation.operation_id}"
            )
        tab_centreline_width_um = recipe.tab_width_um + binding.effective_diameter_um
        minimum_edge_um = tab_centreline_width_um + 2 * binding.effective_diameter_um
        if min(nominal.width_um, nominal.height_um) <= minimum_edge_um:
            raise ProductionCAMError(
                f"contour edge is too short for four safe tabs: {operation.operation_id}"
            )


def _require_declared_cutter_envelope(
    operation: CAMOperation,
    expected: Rect,
) -> None:
    supplied_values = (
        operation.cutter_envelope_x_um,
        operation.cutter_envelope_y_um,
        operation.cutter_envelope_width_um,
        operation.cutter_envelope_length_um,
    )
    if any(value is None for value in supplied_values):
        raise ProductionCAMError(
            f"operation lacks an exact declared cutter envelope: {operation.operation_id}"
        )
    supplied = Rect(
        operation.cutter_envelope_x_um or 0,
        operation.cutter_envelope_y_um or 0,
        operation.cutter_envelope_width_um or 0,
        operation.cutter_envelope_length_um or 0,
    )
    if supplied != expected:
        raise ProductionCAMError(
            f"operation declared cutter envelope differs from exact geometry: "
            f"{operation.operation_id}"
        )


def _target_depth_um(
    operation: CAMOperation,
    setup: BoundSetup,
    recipe: CuttingRecipe,
) -> int:
    if type(operation.depth_um) is not int or operation.depth_um <= 0:
        raise ProductionCAMError(f"operation depth must be positive: {operation.operation_id}")
    if operation.through:
        if (
            not setup.stock_thickness_um
            <= operation.depth_um
            <= (setup.stock_thickness_um + MAX_THROUGH_OVERTRAVEL_UM)
        ):
            raise ProductionCAMError(f"source through depth is unsafe: {operation.operation_id}")
        if recipe.through_overtravel_um <= 0:
            raise ProductionCAMError(
                f"through operation lacks positive recipe overtravel: {operation.operation_id}"
            )
        return setup.stock_thickness_um + recipe.through_overtravel_um
    if operation.depth_um >= setup.stock_thickness_um:
        raise ProductionCAMError(
            f"non-through operation reaches stock bottom: {operation.operation_id}"
        )
    return operation.depth_um


def _generate_operation_moves(
    builder: _ProgramBuilder,
    operation: CAMOperation,
    recipe: CuttingRecipe,
) -> None:
    if operation.kind in {OperationKind.DRILL, OperationKind.COUNTERSINK}:
        _generate_peck_moves(builder, operation, recipe)
    elif operation.kind in _AREA_KINDS:
        _generate_raster_moves(builder, operation, recipe)
    else:
        _generate_contour_moves(builder, operation, recipe)


def _generate_peck_moves(
    builder: _ProgramBuilder,
    operation: CAMOperation,
    recipe: CuttingRecipe,
) -> None:
    target_depth_um = _target_depth_um(operation, builder.setup, recipe)
    levels = depth_levels_um(target_depth_um, recipe.peck_depth_um)
    builder.position(
        operation,
        1,
        operation.x_um,
        operation.y_um,
        recipe.approach_clearance_um,
    )
    for pass_index, depth_z_um in enumerate(levels, start=1):
        builder.cut(
            operation,
            pass_index,
            operation.x_um,
            operation.y_um,
            depth_z_um,
            recipe.plunge_um_min,
            recipe.process_accuracy_um,
        )
        if pass_index != len(levels):
            builder.add(
                operation,
                pass_index,
                ProductionMoveKind.RAPID,
                ProductionMoveRole.PECK_RETRACT,
                operation.x_um,
                operation.y_um,
                recipe.approach_clearance_um,
            )
    builder.retract(operation, len(levels), operation.x_um, operation.y_um)


def _generate_raster_moves(
    builder: _ProgramBuilder,
    operation: CAMOperation,
    recipe: CuttingRecipe,
) -> None:
    assert operation.width_um is not None
    assert operation.length_um is not None
    radius_um = builder.tool.effective_diameter_um // 2
    left_um = operation.x_um + radius_um
    right_um = operation.x_um + operation.width_um - radius_um
    bottom_um = operation.y_um + radius_um
    top_um = operation.y_um + operation.length_um - radius_um
    stepover_um = builder.tool.effective_diameter_um * recipe.stepover_ppm // 1_000_000
    if stepover_um <= 0:
        raise ProductionCAMError(f"recipe stepover rounds to zero: {operation.operation_id}")
    raster = _raster_lines(left_um, right_um, bottom_um, top_um, stepover_um)
    relief_centres = _dogbone_centres(operation)
    target_depth_um = _target_depth_um(operation, builder.setup, recipe)
    for pass_index, depth_z_um in enumerate(
        depth_levels_um(target_depth_um, recipe.stepdown_um), start=1
    ):
        first_x_um, first_y_um = raster[0][0]
        builder.position(
            operation,
            pass_index,
            first_x_um,
            first_y_um,
            recipe.approach_clearance_um,
        )
        builder.cut(
            operation,
            pass_index,
            first_x_um,
            first_y_um,
            depth_z_um,
            recipe.plunge_um_min,
            recipe.process_accuracy_um,
        )
        for line_index, (start, end) in enumerate(raster):
            if line_index:
                builder.cut(
                    operation,
                    pass_index,
                    start[0],
                    start[1],
                    depth_z_um,
                    recipe.feed_um_min,
                    recipe.process_accuracy_um,
                )
            builder.cut(
                operation,
                pass_index,
                end[0],
                end[1],
                depth_z_um,
                recipe.feed_um_min,
                recipe.process_accuracy_um,
            )
        end_x_um, end_y_um = raster[-1][1]
        builder.retract(operation, pass_index, end_x_um, end_y_um)
        for relief_x_um, relief_y_um in relief_centres:
            builder.position(
                operation,
                pass_index,
                relief_x_um,
                relief_y_um,
                recipe.approach_clearance_um,
            )
            builder.cut(
                operation,
                pass_index,
                relief_x_um,
                relief_y_um,
                depth_z_um,
                recipe.plunge_um_min,
                recipe.process_accuracy_um,
            )
            builder.retract(operation, pass_index, relief_x_um, relief_y_um)


def _generate_contour_moves(
    builder: _ProgramBuilder,
    operation: CAMOperation,
    recipe: CuttingRecipe,
) -> None:
    assert operation.width_um is not None
    assert operation.length_um is not None
    nominal = Rect(operation.x_um, operation.y_um, operation.width_um, operation.length_um)
    radius_um = builder.tool.effective_diameter_um // 2
    target_depth_um = _target_depth_um(operation, builder.setup, recipe)
    levels = depth_levels_um(target_depth_um, recipe.stepdown_um)
    tab_floor_z_um = -(builder.setup.stock_thickness_um - recipe.tab_height_um)
    for pass_index, depth_z_um in enumerate(levels, start=1):
        tabs_active = _is_release_contour(operation) and depth_z_um < tab_floor_z_um
        if operation.compensation == "OUTSIDE":
            path = _outside_contour_path(
                nominal,
                radius_um,
                depth_z_um,
                tab_floor_z_um if tabs_active else None,
                recipe.tab_width_um + builder.tool.effective_diameter_um,
            )
        else:
            path = _inside_contour_path(nominal, radius_um, depth_z_um)
        start_x_um, start_y_um, _, _ = path[0]
        builder.position(
            operation,
            pass_index,
            start_x_um,
            start_y_um,
            recipe.approach_clearance_um,
        )
        builder.cut(
            operation,
            pass_index,
            start_x_um,
            start_y_um,
            depth_z_um,
            recipe.plunge_um_min,
            recipe.process_accuracy_um,
        )
        for x_um, y_um, z_um, role in path[1:]:
            move_feed_um_min = (
                recipe.plunge_um_min if z_um != builder.moves[-1].z_um else recipe.feed_um_min
            )
            builder.cut(
                operation,
                pass_index,
                x_um,
                y_um,
                z_um,
                move_feed_um_min,
                recipe.process_accuracy_um,
                role=role,
            )
        end_x_um, end_y_um, _, _ = path[-1]
        builder.retract(operation, pass_index, end_x_um, end_y_um)


def _raster_lines(
    left_um: int,
    right_um: int,
    bottom_um: int,
    top_um: int,
    stepover_um: int,
) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    if right_um < left_um or top_um < bottom_um:
        raise ProductionCAMError("cutter does not fit rectangular area")
    lines: list[tuple[tuple[int, int], tuple[int, int]]] = []
    if right_um - left_um >= top_um - bottom_um:
        lanes = _inclusive_lanes(bottom_um, top_um, stepover_um)
        for index, y_um in enumerate(lanes):
            endpoints = ((left_um, y_um), (right_um, y_um))
            lines.append(endpoints if index % 2 == 0 else (endpoints[1], endpoints[0]))
    else:
        lanes = _inclusive_lanes(left_um, right_um, stepover_um)
        for index, x_um in enumerate(lanes):
            endpoints = ((x_um, bottom_um), (x_um, top_um))
            lines.append(endpoints if index % 2 == 0 else (endpoints[1], endpoints[0]))
    return tuple(lines)


def _inclusive_lanes(minimum_um: int, maximum_um: int, step_um: int) -> tuple[int, ...]:
    values = list(range(minimum_um, maximum_um + 1, step_um))
    if values[-1] != maximum_um:
        values.append(maximum_um)
    return tuple(values)


def _outside_contour_path(
    nominal: Rect,
    radius_um: int,
    depth_z_um: int,
    tab_z_um: int | None,
    tab_width_um: int,
) -> tuple[tuple[int, int, int, ProductionMoveRole], ...]:
    left, right = nominal.x_um, nominal.right_um
    bottom, top = nominal.y_um, nominal.top_um
    path: list[tuple[int, int, int, ProductionMoveRole]] = [
        (left, bottom - radius_um, depth_z_um, ProductionMoveRole.CUT)
    ]
    _append_edge_with_optional_tab(
        path,
        (left, bottom - radius_um),
        (right, bottom - radius_um),
        depth_z_um,
        tab_z_um,
        tab_width_um,
    )
    _append_arc(path, right, bottom, radius_um, quadrant=0, depth_z_um=depth_z_um)
    _append_edge_with_optional_tab(
        path,
        (right + radius_um, bottom),
        (right + radius_um, top),
        depth_z_um,
        tab_z_um,
        tab_width_um,
    )
    _append_arc(path, right, top, radius_um, quadrant=1, depth_z_um=depth_z_um)
    _append_edge_with_optional_tab(
        path,
        (right, top + radius_um),
        (left, top + radius_um),
        depth_z_um,
        tab_z_um,
        tab_width_um,
    )
    _append_arc(path, left, top, radius_um, quadrant=2, depth_z_um=depth_z_um)
    _append_edge_with_optional_tab(
        path,
        (left - radius_um, top),
        (left - radius_um, bottom),
        depth_z_um,
        tab_z_um,
        tab_width_um,
    )
    _append_arc(path, left, bottom, radius_um, quadrant=3, depth_z_um=depth_z_um)
    return tuple(path)


def _inside_contour_path(
    nominal: Rect,
    radius_um: int,
    depth_z_um: int,
) -> tuple[tuple[int, int, int, ProductionMoveRole], ...]:
    left = nominal.x_um + radius_um
    right = nominal.right_um - radius_um
    bottom = nominal.y_um + radius_um
    top = nominal.top_um - radius_um
    return (
        (left, bottom, depth_z_um, ProductionMoveRole.CUT),
        (left, top, depth_z_um, ProductionMoveRole.CUT),
        (right, top, depth_z_um, ProductionMoveRole.CUT),
        (right, bottom, depth_z_um, ProductionMoveRole.CUT),
        (left, bottom, depth_z_um, ProductionMoveRole.CUT),
    )


def _append_edge_with_optional_tab(
    path: list[tuple[int, int, int, ProductionMoveRole]],
    start: tuple[int, int],
    end: tuple[int, int],
    depth_z_um: int,
    tab_z_um: int | None,
    tab_width_um: int,
) -> None:
    if tab_z_um is None:
        path.append((end[0], end[1], depth_z_um, ProductionMoveRole.CUT))
        return
    dx_um, dy_um = end[0] - start[0], end[1] - start[1]
    edge_length_um = abs(dx_um) + abs(dy_um)
    before_um = (edge_length_um - tab_width_um) // 2
    after_um = before_um + tab_width_um
    direction_x = 0 if dx_um == 0 else (1 if dx_um > 0 else -1)
    direction_y = 0 if dy_um == 0 else (1 if dy_um > 0 else -1)
    tab_start = (
        start[0] + direction_x * before_um,
        start[1] + direction_y * before_um,
    )
    tab_end = (
        start[0] + direction_x * after_um,
        start[1] + direction_y * after_um,
    )
    path.extend(
        (
            (tab_start[0], tab_start[1], depth_z_um, ProductionMoveRole.CUT),
            (tab_start[0], tab_start[1], tab_z_um, ProductionMoveRole.TAB_RAMP),
            (tab_end[0], tab_end[1], tab_z_um, ProductionMoveRole.TAB_BRIDGE),
            (tab_end[0], tab_end[1], depth_z_um, ProductionMoveRole.TAB_RAMP),
            (end[0], end[1], depth_z_um, ProductionMoveRole.CUT),
        )
    )


def _append_arc(
    path: list[tuple[int, int, int, ProductionMoveRole]],
    centre_x_um: int,
    centre_y_um: int,
    radius_um: int,
    *,
    quadrant: int,
    depth_z_um: int,
) -> None:
    for cosine_ppm, sine_ppm in _QUARTER_CIRCLE_PPM[1:]:
        dx_ppm, dy_ppm = _arc_offset_ppm(cosine_ppm, sine_ppm, quadrant)
        point = (
            centre_x_um + radius_um * dx_ppm // 1_000_000,
            centre_y_um + radius_um * dy_ppm // 1_000_000,
            depth_z_um,
            ProductionMoveRole.CUT,
        )
        if point[:2] != path[-1][:2]:
            path.append(point)


def _arc_offset_ppm(
    cosine_ppm: int,
    sine_ppm: int,
    quadrant: int,
) -> tuple[int, int]:
    if quadrant == 0:
        return sine_ppm, -cosine_ppm
    if quadrant == 1:
        return cosine_ppm, sine_ppm
    if quadrant == 2:
        return -sine_ppm, cosine_ppm
    return -cosine_ppm, -sine_ppm


def _outside_contour_interpolation_error_um(radius_um: int) -> int:
    """Return an integer upper bound for the emitted rounded-chord radial error.

    The bound is calculated from the exact integer points that ``_append_arc``
    emits.  It covers both ppm-coordinate rounding and the inward sagitta of
    every 11.25-degree chord without floating-point arithmetic.
    """

    if radius_um <= 0:
        raise ProductionCAMError("outside-contour radius must be positive")
    starts = ((0, -radius_um), (radius_um, 0), (0, radius_um), (-radius_um, 0))
    maximum_error_um = 0
    for quadrant, start in enumerate(starts):
        points = [start]
        for cosine_ppm, sine_ppm in _QUARTER_CIRCLE_PPM[1:]:
            dx_ppm, dy_ppm = _arc_offset_ppm(cosine_ppm, sine_ppm, quadrant)
            point = (
                radius_um * dx_ppm // 1_000_000,
                radius_um * dy_ppm // 1_000_000,
            )
            if point != points[-1]:
                points.append(point)
        for first, second in zip(points, points[1:], strict=False):
            first_radius_squared = first[0] ** 2 + first[1] ** 2
            second_radius_squared = second[0] ** 2 + second[1] ** 2
            maximum_radius_squared = max(first_radius_squared, second_radius_squared)
            maximum_radius_floor = isqrt(maximum_radius_squared)
            maximum_radius_ceil = maximum_radius_floor + (
                maximum_radius_floor**2 != maximum_radius_squared
            )
            outward_error_um = max(0, maximum_radius_ceil - radius_um)

            segment_x = second[0] - first[0]
            segment_y = second[1] - first[1]
            segment_length_squared = segment_x**2 + segment_y**2
            projection_numerator = -(first[0] * segment_x + first[1] * segment_y)
            if 0 < projection_numerator < segment_length_squared:
                cross = first[0] * second[1] - first[1] * second[0]
                minimum_radius_floor = isqrt(cross**2 // segment_length_squared)
            else:
                minimum_radius_floor = isqrt(min(first_radius_squared, second_radius_squared))
            inward_error_um = max(0, radius_um - minimum_radius_floor)
            maximum_error_um = max(
                maximum_error_um,
                outward_error_um,
                inward_error_um,
            )
    return maximum_error_um


def _dogbone_centres(operation: CAMOperation) -> tuple[tuple[int, int], ...]:
    if operation.corner_strategy not in _DOGBONE_STRATEGIES:
        return ()
    assert operation.width_um is not None
    assert operation.length_um is not None
    declared = set(operation.open_end_reliefs)
    centres: list[tuple[int, int]] = []
    for u_boundary, v_boundary in (
        ("u_min", "v_min"),
        ("u_max", "v_min"),
        ("u_min", "v_max"),
        ("u_max", "v_max"),
    ):
        suppressed = (
            {u_boundary, v_boundary} <= declared
            if operation.corner_strategy == "dogbone-v1"
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
        centres.append(
            (
                operation.x_um if x_boundary == "x_min" else operation.x_um + operation.width_um,
                operation.y_um if y_boundary == "y_min" else operation.y_um + operation.length_um,
            )
        )
    return tuple(centres)


def _validate_corner_contract(
    operation: CAMOperation,
    binding: ProductionToolBinding,
    nominal: Rect,
) -> None:
    if not _has_corner_contract(operation):
        return
    if operation.corner_strategy not in _DOGBONE_STRATEGIES:
        raise ProductionCAMError(f"unsupported corner strategy: {operation.operation_id}")
    radius_um = binding.effective_diameter_um // 2
    if operation.corner_relief_radius_um != radius_um:
        raise ProductionCAMError(f"dogbone radius/tool mismatch: {operation.operation_id}")
    if len(operation.open_end_reliefs) != len(set(operation.open_end_reliefs)) or not set(
        operation.open_end_reliefs
    ) <= {"u_min", "u_max", "v_min", "v_max"}:
        raise ProductionCAMError(f"invalid dogbone open ends: {operation.operation_id}")
    expected = _dogbone_envelope(operation, nominal, radius_um)
    supplied_values = (
        operation.cutter_envelope_x_um,
        operation.cutter_envelope_y_um,
        operation.cutter_envelope_width_um,
        operation.cutter_envelope_length_um,
    )
    if any(value is None for value in supplied_values):
        raise ProductionCAMError(
            f"dogbone operation lacks an exact cutter envelope: {operation.operation_id}"
        )
    supplied = Rect(
        operation.cutter_envelope_x_um or 0,
        operation.cutter_envelope_y_um or 0,
        operation.cutter_envelope_width_um or 0,
        operation.cutter_envelope_length_um or 0,
    )
    if supplied != expected:
        raise ProductionCAMError(f"dogbone cutter envelope mismatch: {operation.operation_id}")


def _dogbone_envelope(operation: CAMOperation, nominal: Rect, radius_um: int) -> Rect:
    centres = _dogbone_centres(operation)
    if not centres:
        return nominal
    left = min(nominal.x_um, *(x_um - radius_um for x_um, _ in centres))
    right = max(nominal.right_um, *(x_um + radius_um for x_um, _ in centres))
    bottom = min(nominal.y_um, *(y_um - radius_um for _, y_um in centres))
    top = max(nominal.top_um, *(y_um + radius_um for _, y_um in centres))
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


def _has_corner_contract(operation: CAMOperation) -> bool:
    return (
        operation.corner_strategy is not None
        or operation.corner_relief_radius_um is not None
        or bool(operation.open_end_reliefs)
    )


def _is_release_contour(operation: CAMOperation) -> bool:
    return (
        operation.kind == OperationKind.CONTOUR
        and operation.through
        and operation.compensation == "OUTSIDE"
    )


def _require_point_in_stock(
    setup: BoundSetup,
    x_um: int,
    y_um: int,
    operation_id: str,
) -> None:
    if not (0 <= x_um <= setup.stock_width_um and 0 <= y_um <= setup.stock_height_um):
        raise ProductionCAMError(f"tool centre leaves stock bounds: {operation_id}")


def _require_cut_segment_clear(
    setup: BoundSetup,
    start_x_um: int,
    start_y_um: int,
    end_x_um: int,
    end_y_um: int,
    radius_um: int,
    assembly_collision_radius_um: int,
    process_accuracy_um: int,
    operation: CAMOperation,
    part_outlines: tuple[_PartOutline, ...],
) -> None:
    effective_radius_um = radius_um + process_accuracy_um
    cutter_swept = Rect(
        min(start_x_um, end_x_um) - effective_radius_um,
        min(start_y_um, end_y_um) - effective_radius_um,
        abs(end_x_um - start_x_um) + 2 * effective_radius_um,
        abs(end_y_um - start_y_um) + 2 * effective_radius_um,
    )
    _require_footprint_clear(setup, cutter_swept, operation.operation_id)
    effective_assembly_radius_um = assembly_collision_radius_um + process_accuracy_um
    assembly_swept = Rect(
        min(start_x_um, end_x_um) - effective_assembly_radius_um,
        min(start_y_um, end_y_um) - effective_assembly_radius_um,
        abs(end_x_um - start_x_um) + 2 * effective_assembly_radius_um,
        abs(end_y_um - start_y_um) + 2 * effective_assembly_radius_um,
    )
    if any(_rects_touch_or_overlap(assembly_swept, zone) for zone in setup.keep_out_zones):
        raise ProductionCAMError(
            f"tool assembly sweep reaches setup keep-out zone: {operation.operation_id}"
        )

    own_outlines = tuple(
        outline
        for outline in part_outlines
        if outline.instance_id == operation.instance_id
        and (outline.stock_id, outline.sheet_index) == (setup.stock_id, setup.sheet_index)
    )
    if len(own_outlines) != 1:
        raise ProductionCAMError(
            f"operation has no unique own part outline: {operation.operation_id}"
        )
    if not _is_release_contour(operation):
        _require_accuracy_expanded_segment_within_own_part(
            start=(start_x_um, start_y_um),
            end=(end_x_um, end_y_um),
            effective_radius_um=effective_radius_um,
            operation=operation,
            setup=setup,
            outline=own_outlines[0],
        )

    physical_start = _point_to_physical_stock(start_x_um, start_y_um, setup)
    physical_end = _point_to_physical_stock(end_x_um, end_y_um, setup)
    for outline in part_outlines:
        if outline.instance_id == operation.instance_id or (
            outline.stock_id,
            outline.sheet_index,
        ) != (setup.stock_id, setup.sheet_index):
            continue
        if _segment_radius_touches_rect(
            physical_start,
            physical_end,
            effective_radius_um,
            outline.rect,
        ):
            raise ProductionCAMError(
                "accuracy-expanded cutter sweep reaches another part instance: "
                f"{operation.operation_id} -> {outline.instance_id}"
            )


def _require_accuracy_expanded_segment_within_own_part(
    *,
    start: tuple[int, int],
    end: tuple[int, int],
    effective_radius_um: int,
    operation: CAMOperation,
    setup: BoundSetup,
    outline: _PartOutline,
) -> None:
    """Keep actual removal inside the finished part, except declared open edges.

    A line-segment capsule is contained by an axis-aligned rectangle exactly
    when both endpoints have the cutter radius of clearance to every closed
    edge.  Grooves may cross only an edge explicitly declared open in source
    coordinates, and only when their nominal rectangle ends on that same
    finished-part boundary.  Release contours are handled by their dedicated
    outside-contour contract and never enter this function.
    """

    local_outline = _rect_to_physical_stock(outline.rect, setup)
    allowed_edges = _declared_open_finished_part_edges(operation, setup, local_outline)
    leaves_closed_edge = (
        (
            "x_min" not in allowed_edges
            and min(start[0], end[0]) - effective_radius_um < local_outline.x_um
        )
        or (
            "x_max" not in allowed_edges
            and max(start[0], end[0]) + effective_radius_um > local_outline.right_um
        )
        or (
            "y_min" not in allowed_edges
            and min(start[1], end[1]) - effective_radius_um < local_outline.y_um
        )
        or (
            "y_max" not in allowed_edges
            and max(start[1], end[1]) + effective_radius_um > local_outline.top_um
        )
    )
    if leaves_closed_edge:
        raise ProductionCAMError(
            "accuracy-expanded cutter sweep leaves its own finished part outline: "
            f"{operation.operation_id}"
        )


def _declared_open_finished_part_edges(
    operation: CAMOperation,
    setup: BoundSetup,
    outline: Rect,
) -> frozenset[str]:
    if operation.kind != OperationKind.GROOVE or not operation.open_end_reliefs:
        return frozenset()
    nominal = _operation_nominal_footprint(operation)
    edge_coordinates = {
        "x_min": (nominal.x_um, outline.x_um),
        "x_max": (nominal.right_um, outline.right_um),
        "y_min": (nominal.y_um, outline.y_um),
        "y_max": (nominal.top_um, outline.top_um),
    }
    allowed: set[str] = set()
    for source_edge in operation.open_end_reliefs:
        machine_edge = _source_edge_machine_boundary(
            source_edge,
            rotated_90=operation.source_rotation_90,
            side=setup.side,
        )
        if edge_coordinates[machine_edge][0] == edge_coordinates[machine_edge][1]:
            allowed.add(machine_edge)
    return frozenset(allowed)


def _source_edge_machine_boundary(
    source_edge: str,
    *,
    rotated_90: bool,
    side: Side,
) -> str:
    if source_edge == "u_min":
        boundary = "y_min" if rotated_90 else "x_min"
    elif source_edge == "u_max":
        boundary = "y_max" if rotated_90 else "x_max"
    elif source_edge == "v_min":
        boundary = "x_max" if rotated_90 else "y_min"
    elif source_edge == "v_max":
        boundary = "x_min" if rotated_90 else "y_max"
    else:  # Already rejected by the operation corner contract; retain fail-closed locality.
        raise ProductionCAMError(f"unsupported open-end edge: {source_edge}")
    if side == Side.B and boundary.startswith("y_"):
        return "y_max" if boundary == "y_min" else "y_min"
    return boundary


def _require_footprint_clear(setup: BoundSetup, footprint: Rect, operation_id: str) -> None:
    stock = Rect(0, 0, setup.stock_width_um, setup.stock_height_um)
    if min(footprint.width_um, footprint.height_um) <= 0 or not stock.contains(footprint):
        raise ProductionCAMError(f"cutting sweep leaves stock bounds: {operation_id}")
    if any(_rects_touch_or_overlap(footprint, zone) for zone in setup.keep_out_zones):
        raise ProductionCAMError(f"cutting sweep reaches setup keep-out zone: {operation_id}")


def _rects_touch_or_overlap(first: Rect, second: Rect) -> bool:
    return not (
        first.right_um < second.x_um
        or second.right_um < first.x_um
        or first.top_um < second.y_um
        or second.top_um < first.y_um
    )


def _segment_radius_touches_rect(
    start: tuple[int, int],
    end: tuple[int, int],
    radius_um: int,
    rectangle: Rect,
) -> bool:
    """Return whether an exact line-segment capsule touches an axis-aligned rect."""

    if _segment_intersects_rect(start, end, rectangle):
        return True
    radius_squared = radius_um**2
    if (
        _point_rect_distance_squared(start, rectangle) <= radius_squared
        or _point_rect_distance_squared(end, rectangle) <= radius_squared
    ):
        return True
    corners = (
        (rectangle.x_um, rectangle.y_um),
        (rectangle.right_um, rectangle.y_um),
        (rectangle.right_um, rectangle.top_um),
        (rectangle.x_um, rectangle.top_um),
    )
    return any(_point_segment_within_radius(corner, start, end, radius_um) for corner in corners)


def _segment_intersects_rect(
    start: tuple[int, int],
    end: tuple[int, int],
    rectangle: Rect,
) -> bool:
    if _point_in_rect(start, rectangle) or _point_in_rect(end, rectangle):
        return True
    corners = (
        (rectangle.x_um, rectangle.y_um),
        (rectangle.right_um, rectangle.y_um),
        (rectangle.right_um, rectangle.top_um),
        (rectangle.x_um, rectangle.top_um),
    )
    return any(
        _segments_touch_or_cross(start, end, first, second)
        for first, second in zip(corners, (*corners[1:], corners[0]), strict=True)
    )


def _point_in_rect(point: tuple[int, int], rectangle: Rect) -> bool:
    return (
        rectangle.x_um <= point[0] <= rectangle.right_um
        and rectangle.y_um <= point[1] <= rectangle.top_um
    )


def _segments_touch_or_cross(
    first_start: tuple[int, int],
    first_end: tuple[int, int],
    second_start: tuple[int, int],
    second_end: tuple[int, int],
) -> bool:
    first_orientation = _orientation(first_start, first_end, second_start)
    second_orientation = _orientation(first_start, first_end, second_end)
    third_orientation = _orientation(second_start, second_end, first_start)
    fourth_orientation = _orientation(second_start, second_end, first_end)
    if first_orientation == 0 and _point_on_segment(second_start, first_start, first_end):
        return True
    if second_orientation == 0 and _point_on_segment(second_end, first_start, first_end):
        return True
    if third_orientation == 0 and _point_on_segment(first_start, second_start, second_end):
        return True
    if fourth_orientation == 0 and _point_on_segment(first_end, second_start, second_end):
        return True
    return (first_orientation > 0) != (second_orientation > 0) and (third_orientation > 0) != (
        fourth_orientation > 0
    )


def _orientation(
    start: tuple[int, int],
    end: tuple[int, int],
    point: tuple[int, int],
) -> int:
    return (end[0] - start[0]) * (point[1] - start[1]) - (end[1] - start[1]) * (point[0] - start[0])


def _point_on_segment(
    point: tuple[int, int],
    start: tuple[int, int],
    end: tuple[int, int],
) -> bool:
    return min(start[0], end[0]) <= point[0] <= max(start[0], end[0]) and min(
        start[1], end[1]
    ) <= point[1] <= max(start[1], end[1])


def _point_rect_distance_squared(point: tuple[int, int], rectangle: Rect) -> int:
    dx_um = max(rectangle.x_um - point[0], 0, point[0] - rectangle.right_um)
    dy_um = max(rectangle.y_um - point[1], 0, point[1] - rectangle.top_um)
    return dx_um**2 + dy_um**2


def _point_segment_within_radius(
    point: tuple[int, int],
    start: tuple[int, int],
    end: tuple[int, int],
    radius_um: int,
) -> bool:
    segment_x = end[0] - start[0]
    segment_y = end[1] - start[1]
    point_x = point[0] - start[0]
    point_y = point[1] - start[1]
    segment_length_squared = segment_x**2 + segment_y**2
    if segment_length_squared == 0:
        return point_x**2 + point_y**2 <= radius_um**2
    projection = point_x * segment_x + point_y * segment_y
    if projection <= 0:
        return point_x**2 + point_y**2 <= radius_um**2
    if projection >= segment_length_squared:
        end_x = point[0] - end[0]
        end_y = point[1] - end[1]
        return end_x**2 + end_y**2 <= radius_um**2
    cross = point_x * segment_y - point_y * segment_x
    return cross**2 <= radius_um**2 * segment_length_squared
